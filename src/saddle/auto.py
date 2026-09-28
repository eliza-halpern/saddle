"""Autonomous runs: a task string and a repo in, a branch and a ledger out.

This is Phase 2's executor, `engine.run_turn`, run with no human turn.
Arm E runs it with no auditor; arms E+A and E+A+F (the default) attach a
`feed.AuditFeed` that audits each checkpoint and the finished tree.
It is not a second tool loop: it sets up a worktree,
turns on the tier-0 guards and a budget, and hands one turn to the same engine the chat uses.

- The run happens in a fresh `git worktree` under
  `.saddle/worktrees/<run-id>/` on branch `saddle/auto/<run-id>`, never in
  the user's checkout. The result is that branch.
- The ledger is `.saddle/runs/<run-id>/proofs.jsonl`, outside the
  worktree, so the tools cannot reach it; `saddle verify` reads it.
- It ends `finished` only when the model calls `finish`, and otherwise
  `stopped`, naming the budget or error. In arm E+A+F `finish` is refused
  while the audit of the tree fails; in E and E+A a `finished` outcome is
  no claim that the change works (E+A records the audit verdict beside it).
"""

from __future__ import annotations

import subprocess
import tomllib
import uuid
from collections.abc import Callable, Iterator, Sequence
from dataclasses import dataclass
from pathlib import Path
from time import monotonic
from typing import Final

from saddle import prompt_constants
from saddle.anchor import anchor_trailers, outcome_hash
from saddle.auditor import Tier2Mode, _test_side
from saddle.engine import DEFAULT_FINISH_REFUSAL_CAP, AutoRun, RunBudget, TurnOptions, run_turn
from saddle.events import Event, Question
from saddle.feed import ARMS, Arm, AuditFeed, AuditorFactory, default_auditor
from saddle.gates import DEFAULT_MUTANT_SHORTLIST
from saddle.journal import append_span, build_span
from saddle.sandbox import HOST_GIT_GUARD, Sandbox
from saddle.tools import CHECK_SCHEMA, FINISH_SCHEMA, TOOLS, ToolContext
from saddle.vllm import VllmClient

DEFAULT_TIME_BUDGET_S: Final = 1800
"""30 minutes: 3x the untouched arm's ~10 min on a T5-class task (the
Daily Driver page's timing chart). Saddle's own loop has never been timed
autonomously, so the headroom is wide on purpose; arm E measures it."""

DEFAULT_TOKEN_BUDGET: Final = 100_000
"""~2x the untouched arm's median 47k generated tokens on T5 (the page's
"The money" table). Generated tokens only; the prompt is not charged."""

DEFAULT_TEST_ROOTS: Final = ("tests",)

SYSTEM_PROMPT: Final = (
    "You are working alone on one task in a git worktree of a repository. "
    "Nobody will answer questions. Read the code, make the change with the "
    "file tools, and run the tests with run_command to check it. "
    "{tests} "
    "When the task is done, call finish once with a short account of what you "
    "changed and why. If it cannot be done honestly, call finish and say so. "
    "Your account is recorded as narrative; it does not count as proof."
)

CHECK_PROMPT: Final = (
    " You may call check to run the audit's fast checks on the tree as it is now; "
    "finish runs the same checks plus mutation testing, so a passing check does "
    "not guarantee finish passes."
)
"""Appended to the system prompt only with `--check-tool`, so a run without
the flag sends the same prompt bytes as before."""

COMMAND_ENV: Final = {"PYTHONDONTWRITEBYTECODE": "1"}
"""No command the run starts writes bytecode. Python trusts a `.pyc` whose
recorded source mtime (one-second resolution) and size match, so a
same-length edit followed by a run in the same second would execute the
old code and fail a correct fix (CONTRIBUTING.md, harness rule 3). The worktree
is a fresh checkout, so with nothing written there is nothing stale."""

UNSTAGED: Final = (
    ".",
    ":(exclude,glob)**/*.pyc",
    ":(exclude,glob).saddle/**",
)
"""Pathspec for what a run may list or commit: never bytecode (`*.pyc`,
which is all `__pycache__/` holds) or saddle's own state, whether or not
the repo has a `.gitignore` that says so. A worktree's `info/exclude` is
shared with the user's repo, so it is not used."""

GIT_IDENTITY: Final = ("-c", "user.name=saddle", "-c", "user.email=saddle@localhost")

SELF_PACKAGE: Final = "src/saddle/__init__.py"
"""What marks a worktree as saddle's own source: the `saddle` package itself,
at the path its build ships (`pyproject.toml`, `packages = ["src/saddle"]`).
Read at the baseline, before the model acts, so a run cannot switch the guard
off by deleting it, and by layout rather than distribution name, so a renamed
or forked distribution of the same package is still guarded."""

GUARDED_MODULES: Final = (
    "gates",
    "evidence",
    "auditor",
    "audit",
    "feed",
    "engine",
    "memcap",
    "sandbox",
    "auto",
)
"""The modules a run on saddle's own source may not finish on: the judges
(`gates`, `evidence`, `auditor`, `audit`), what decides whether a finish is
accepted (`feed`, `engine`), what confines and caps the auditor's runs
(`sandbox`, `memcap`), and `auto` itself, which holds these lists: a run that
could shorten them must not finish either. The one module list the self-guard
reads."""

GUARDED_TESTS: Final = (
    "tests/test_gates.py",
    "tests/test_evidence.py",
    "tests/test_auditor.py",
    "tests/test_audit.py",
    "tests/test_feed.py",
    "tests/test_feed_covtext.py",
    "tests/test_feed_said_once.py",
    "tests/test_feed_tally.py",
    "tests/test_chat_engine.py",
    "tests/test_memcap.py",
    "tests/test_sandbox_reach.py",
    "tests/test_auto.py",
    "tests/test_self_guard.py",
    "tests/conftest.py",
)
"""The test files that pin the guarded modules, and `tests/conftest.py`,
which replaces parts of `evidence` for the whole suite. Listed by name
because not every module's tests live at `tests/test_<module>.py`."""

GUARDED_PATHS: Final = frozenset(
    [f"src/saddle/{m}.py" for m in GUARDED_MODULES] + list(GUARDED_TESTS)
)
"""Paths a run on saddle's own source may change but not finish on: the
guarded modules and their tests. Named files, never a pattern."""


class AutoError(RuntimeError):
    """The run could not be set up (not a git repo, worktree failed)."""


@dataclass(frozen=True)
class AutoOptions:
    task: str
    repo: Path
    time_budget_s: float = DEFAULT_TIME_BUDGET_S
    token_budget: int = DEFAULT_TOKEN_BUDGET
    allow_test_edits: bool = False
    temperature: float = 0.0
    """Greedy, as `saddle run` is: an arm is a measurement, and chat's 1.0
    (engine.CHAT_TEMPERATURE) is argued there for conversation only."""
    reasoning_effort: str = "medium"
    context_tokens: int = 175_000
    run_id: str = ""
    clock: Callable[[], float] = monotonic
    arm: Arm = "E+A+F"
    """E+A+F (default): audits at checkpoints and at finish, delivered as
    tool results, and a failing finish audit refuses `finish`. E+A
    (`--no-feedback`): the same audits, journaled, never delivered, never
    refusing. E (`--no-audit`): no auditor at all."""
    auditor_factory: AuditorFactory = default_auditor
    finish_refusal_cap: int = DEFAULT_FINISH_REFUSAL_CAP
    """Consecutive `finish` refusals on an unchanged failing finding set before
    an honest stop, reason `audit unresolved` (`engine.AutoRun`)."""
    keep_reasoning: bool = True
    """Send each round's reasoning back within the run
    (engine.TurnOptions.keep_reasoning). On by default for `saddle auto` and
    chat-started runs, so every run has one prompt shape; `--no-keep-reasoning`
    turns it off (without it the served chat templates put an empty reasoning
    block in every past round). Sealed as `prompt_shape.keep_reasoning` either
    way."""
    sanctioned_test_rewrites: tuple[str, ...] = ()
    """Test functions the task orders rewritten (T5 rule 8). A failing
    assertion-preservation finding naming only these is classed `sanctioned`:
    reported as information, never delivered as a failure, never refusing
    `finish`. Sealed in the start span and the outcome sidecar."""
    tier2: Tier2Mode = "score"
    """`--tier2`: "score" (default) is the auditor as it was before the
    per-survivor gate, byte for byte; "shortlist" is that gate
    (`auditor.AuditorConfig.tier2`). Sealed in the outcome sidecar only
    when "shortlist"."""
    mutant_shortlist: int = DEFAULT_MUTANT_SHORTLIST
    """`--mutant-shortlist N`: how many surviving mutants a finish refusal names
    (`--tier2 shortlist` only). Sealed with it."""
    check_tool: bool = False
    """`--check-tool` (arm E+A+F only; scope widened): offer the model a
    `check` tool that runs audit tiers 0 and 1 on demand (`feed.AuditFeed.check`).
    Off by default; off, the tool list, prompt and sealed records are those
    of a run without it."""


@dataclass(frozen=True)
class AutoResult:
    run_id: str
    worktree: Path
    branch: str
    journal: Path
    outcome: str
    reason: str
    commit: str


def _git(repo: Path, *args: str) -> str:
    done = subprocess.run(
        ["git", "-C", str(repo), *HOST_GIT_GUARD, *GIT_IDENTITY, *args],
        capture_output=True,
        text=True,
        check=False,
    )
    if done.returncode != 0:
        msg = f"git {' '.join(args)} failed: {done.stderr.strip()}"
        raise AutoError(msg)
    return done.stdout


def guarded_test_roots(repo: Path) -> tuple[str, ...]:
    """The repo's pytest `testpaths`, or `tests` when it configures none."""
    try:
        config = tomllib.loads((repo / "pyproject.toml").read_text(encoding="utf-8"))
    except (OSError, tomllib.TOMLDecodeError):
        return DEFAULT_TEST_ROOTS
    paths = config.get("tool", {}).get("pytest", {}).get("ini_options", {}).get("testpaths")
    if isinstance(paths, list) and paths and all(isinstance(p, str) for p in paths):
        return tuple(paths)
    return DEFAULT_TEST_ROOTS


def create_worktree(repo: Path, run_id: str) -> tuple[Path, str]:
    """A fresh worktree of HEAD on a new branch, inside `.saddle/`.

    `.saddle/.gitignore` holding `*` keeps the whole directory out of the
    user's `git status`, so the checkout reads exactly as it did.
    """
    root = Path(_git(repo, "rev-parse", "--show-toplevel").strip())
    saddle_dir = root / ".saddle"
    saddle_dir.mkdir(exist_ok=True)
    ignore = saddle_dir / ".gitignore"
    if not ignore.exists():
        ignore.write_text("*\n", encoding="utf-8")
    worktree = saddle_dir / "worktrees" / run_id
    branch = f"saddle/auto/{run_id}"
    _git(root, "worktree", "add", "-q", "-b", branch, str(worktree), "HEAD")
    return worktree, branch


def ledger_path(root: Path, run_id: str) -> Path:
    """Where a run's ledger lives: outside its worktree, beside the others."""
    return root / ".saddle" / "runs" / run_id / "proofs.jsonl"


def repo_root(repo: Path) -> Path:
    """The top of the git checkout `repo` is in (an `AutoError` if none)."""
    return Path(_git(repo.resolve(), "rev-parse", "--show-toplevel").strip())


def changed_files(worktree: Path) -> list[str]:
    """Paths the run changed, added or deleted, relative to the worktree."""
    out = _git(worktree, "status", "--porcelain", "--untracked-files=all", "-z", "--", *UNSTAGED)
    return sorted({entry[3:] for entry in out.split("\0") if len(entry) > 3})


def self_guard(worktree: Path) -> Callable[[], list[str]] | None:
    """For a run on saddle's own source: which guarded paths it has changed.

    None when the worktree is not saddle (no `SELF_PACKAGE` at the baseline):
    a repository that merely has a `gates.py` is not saddle's judge, and the
    guard does not apply to it.
    """
    if not (worktree / SELF_PACKAGE).is_file():
        return None
    return lambda: sorted(p for p in changed_files(worktree) if p in GUARDED_PATHS)


def run_auto(
    options: AutoOptions,
    client: VllmClient,
    *,
    on_event: Callable[[Event], None] | None = None,
    audit: Callable[[str, str, str], Sequence[Event]] | None = None,
    answer: Callable[[Question], str | None] | None = None,
    cancel: Callable[[], bool] | None = None,
    on_budget: Callable[[RunBudget], None] | None = None,
) -> AutoResult:
    """Run one task to `finish` or a budget, then commit what it left.

    `audit` and `answer` are the auditor seam (`engine.AutoRun`); `cancel`
    is the chat's stop button; `on_budget` is handed the run's own
    `RunBudget` -- the object the engine charges -- once it exists, so a
    watcher reads spend from the run's accounting rather than keeping its
    own clock. The CLI passes none of them.
    """
    if options.arm not in ARMS:
        msg = f"unknown arm {options.arm!r}; expected one of {', '.join(ARMS)}"
        raise AutoError(msg)
    if options.finish_refusal_cap < 1:
        msg = f"finish refusal cap must be at least 1, got {options.finish_refusal_cap}"
        raise AutoError(msg)
    if options.check_tool and options.arm != "E+A+F":
        msg = f"--check-tool needs arm E+A+F (it delivers audit findings); got {options.arm}"
        raise AutoError(msg)
    repo = options.repo.resolve()
    run_id = options.run_id or uuid.uuid4().hex[:12]
    worktree, branch = create_worktree(repo, run_id)
    root = worktree.parent.parent.parent
    journal = ledger_path(root, run_id)
    start = build_span(
        node_id="chat#1",
        argv=["auto:start", options.task],
        duration_ms=0,
        exit_code=0,
        detail=f"arm {options.arm}; temperature {options.temperature}; "
        f"effort {options.reasoning_effort}; sanctioned test rewrites "
        f"{','.join(options.sanctioned_test_rewrites) or 'none'}; branch {branch}; "
        f"budgets {options.time_budget_s:.0f}s, "
        f"{options.token_budget} generated tokens; test edits "
        f"{'allowed' if options.allow_test_edits else 'refused'}"
        + ("; check tool offered" if options.check_tool else ""),
        kind="agent",
    )
    append_span(journal, start)
    feed = (
        None
        if options.arm == "E"
        else AuditFeed(
            worktree=worktree,
            baseline=_git(worktree, "rev-parse", "HEAD").strip(),
            journal=journal,
            run_span=start.span_id,
            feedback=options.arm == "E+A+F",
            factory=options.auditor_factory,
            sanctioned_test_rewrites=options.sanctioned_test_rewrites,
            tier2=options.tier2,
            mutant_shortlist=options.mutant_shortlist,
        )
    )
    guard = self_guard(worktree)
    auto = AutoRun(
        budget=RunBudget(
            time_s=options.time_budget_s, tokens=options.token_budget, clock=options.clock
        ),
        run_span=start.span_id,
        changed_files=lambda: changed_files(worktree),
        prompt_check=(
            (
                lambda: prompt_constants.check(
                    options.task, prompt_constants.tree_sources(worktree, _test_side)
                )
            )
            if prompt_constants.named(options.task)
            else None
        ),
        feed=feed,
        guard=guard,
        finish_refusal_cap=options.finish_refusal_cap,
        arm=options.arm,
        sealed={
            "temperature": options.temperature,
            "reasoning_effort": options.reasoning_effort,
            "allow_test_edits": options.allow_test_edits,
            "sanctioned_test_rewrites": list(options.sanctioned_test_rewrites),
            "prompt_shape": {"keep_reasoning": options.keep_reasoning},
            **({"self_guard": True} if guard is not None else {}),
            **({"check_tool": True} if options.check_tool else {}),
            **(
                {"tier2": "shortlist", "mutant_shortlist": options.mutant_shortlist}
                if options.tier2 == "shortlist"
                else {}
            ),
        },
        audit=audit,
        answer=answer,
        check_tool=options.check_tool,
    )
    if on_budget is not None:
        on_budget(auto.budget)
    roots = None if options.allow_test_edits else guarded_test_roots(worktree)
    tests = (
        "You may edit tests."
        if roots is None
        else f"Test files ({', '.join(r + '/' for r in roots)}, test_*.py, conftest.py) "
        "are read-only: an edit to one is refused."
    )
    turn_options = TurnOptions(
        workdir=worktree,
        journal=journal,
        temperature=options.temperature,
        reasoning_effort=options.reasoning_effort,
        system_prompt=SYSTEM_PROMPT.format(tests=tests)
        + (CHECK_PROMPT if options.check_tool else ""),
        context_tokens=options.context_tokens,
        tools=[*TOOLS, FINISH_SCHEMA, *([CHECK_SCHEMA] if options.check_tool else [])],
        auto=auto,
        keep_reasoning=options.keep_reasoning,
    )
    context = ToolContext(
        workdir=worktree,
        sandbox=Sandbox.for_workdir(
            worktree, env=COMMAND_ENV, require_isolation=True, network="none"
        ),
        protected_tests=roots,
        syntax_guard=True,
    )
    events: Iterator[Event] = run_turn(
        client, [], options.task, turn_options, turn=1, context=context, cancel=cancel
    )
    for event in events:
        if on_event is not None:
            on_event(event)
    _git(worktree, "add", "-A", "--", *UNSTAGED)
    message = f"saddle auto {run_id}: {auto.outcome} ({auto.reason})"
    if auto.narrative:
        message += f"\n\nNarrative (model-written, not evidence):\n{auto.narrative}"
    # The anchor: the outcome span's hash, outside the ledger, as the last paragraph.
    ledger = journal.relative_to(root).as_posix()
    message += f"\n\n{anchor_trailers(outcome_hash(journal, start.span_id), ledger)}"
    _git(worktree, "commit", "-q", "--allow-empty", "--no-verify", "-m", message)
    commit = _git(worktree, "rev-parse", "HEAD").strip()
    return AutoResult(
        run_id=run_id,
        worktree=worktree,
        branch=branch,
        journal=journal,
        outcome=auto.outcome,
        reason=auto.reason,
        commit=commit,
    )
