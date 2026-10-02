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
- The worktree holds tracked files only, so a project's untracked `.venv`
  is looked for in the user's folder (`sandbox.project_env`). When there is
  one, the model's commands and the gates both run on it, and the start
  record seals which environment the gates used (`environment ...`).
- It ends `finished` only when the model calls `finish`, and otherwise
  `stopped`, naming the budget or error. In arm E+A+F `finish` is refused
  while the audit of the tree fails; in E and E+A a `finished` outcome is
  no claim that the change works (E+A records the audit verdict beside it).
"""

from __future__ import annotations

import configparser
import json
import math
import shutil
import subprocess
import tomllib
import uuid
from collections.abc import Callable, Iterator, Sequence
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path
from time import monotonic
from typing import Any, Final

from saddle import prompt_constants, sandbox
from saddle.anchor import COAUTHOR_TRAILER, anchor_trailers, outcome_hash
from saddle.audit import AUDIT_TEST_COMMAND
from saddle.auditor import Tier2Mode, _test_side
from saddle.engine import DEFAULT_FINISH_REFUSAL_CAP, AutoRun, RunBudget, TurnOptions, run_turn
from saddle.events import Event, Question
from saddle.evidence import (
    SuiteLimitError,
    format_overrides,
    ruff_argv,
    run_capture,
    sandbox_expose,
    src_layout_env,
    suite_workers,
)
from saddle.feed import ARMS, Arm, AuditFeed, AuditorFactory, default_auditor
from saddle.gates import DEFAULT_MUTANT_SHORTLIST
from saddle.installs import Installs, WheelFolder
from saddle.journal import (
    AUTO_COMMITTED,
    P1_EXTRACT_SPAN,
    append_span,
    build_span,
    coverage_path,
    started_before,
    utc_now,
)
from saddle.sandbox import HOST_GIT_GUARD, Sandbox
from saddle.task_passes import baseline_sources, cut_calls
from saddle.task_passes import extract as extract_requirements
from saddle.task_requirements import ProbeTree
from saddle.tools import (
    BLOCKED_SCHEMA,
    CHECK_SCHEMA,
    DISPUTE_SCHEMA,
    FINISH_SCHEMA,
    INSTALL_SCHEMA,
    PREMISE_SCHEMA,
    REFUSE_SCHEMA,
    TOOLS,
    ToolContext,
    provider_prompt,
    provider_schemas,
)
from saddle.vllm import VllmClient

DEFAULT_TIME_BUDGET_S: Final = 0
"""No time limit (engine.NO_LIMIT). The wall-clock cap existed to break the
endless loops early runs fell into; the loop guards (MAX_TOOL_ROUNDS, the
empty-round cap, the optional stall check) stop those now, and a hard clock
only ever killed good runs at the finish line. A positive value opts back in."""

DEFAULT_TOKEN_BUDGET: Final = 0
"""No token limit (engine.NO_LIMIT). Each reply is still bounded by the
model's context window; only the per-run cap is gone. A positive value opts
back in."""

DEFAULT_TEST_ROOTS: Final = ("tests",)

TASK_TEMPERATURE: Final = 1.0
"""A task run's sampling temperature: the model's own recommendation (its
generation_config samples at 1.0, with the server supplying its top_p 0.95
and top_k 20), not greedy. Greedy decoding looped: two watched dogfood runs
on saddle's own repo each spent six to twenty-nine minutes inside one
reasoning reply without a tool call, the later minutes repeating the earlier
text word for word, while the same task at 1.0 kept acting. Greedy was kept
for byte-identical replays, which vLLM does not give anyway (batching makes
greedy runs diverge). A measurement that wants greedy pins 0.0 itself."""

SYSTEM_PROMPT: Final = (
    "You are working alone on one task in a git worktree of a repository. "
    "Nobody will answer questions. "
    "First, check that the problem the task describes exists on the current code: "
    "reproduce it with a failing test or a short script. A task is written by a "
    "person and can be wrong, often because the code changed since. If your probe "
    "or your reading of the code shows the task's claim cannot hold, or the problem "
    "is already fixed, call dispute with the claim, what you found and the commands "
    "that show it. That is a correct outcome, as good as a fix; a person reviews "
    "it. Do not look for another reading of the task that would make it true, and "
    "do not reconstruct how older code behaved to make its claim true: if you catch "
    "yourself doing either, call dispute instead. "
    "Otherwise, read the code, make the change with the file tools, and run the "
    "tests with run_command to check it. "
    "{tests} "
    "When the task is done, call finish once with a short account of what you "
    "changed and why. Your account is recorded as narrative; it does not count "
    "as proof. Call finish only when the task is done. If the problem is real but "
    "you cannot do it here, because something it needs is missing (a tool, a "
    "package, network access, or information only the person has), call blocked "
    "with what is missing and what you tried."
)


PREMISE_PROMPT: Final = (
    " Before your first edit, call premise_check with the command(s) that show the "
    "problem on the current code; edits are refused until you do. It shows you "
    "their output: if that output does not show the problem, call dispute."
)
"""Appended to the system prompt with `--premise-check`."""

STALL_PROMPT: Final = (
    " You have about ten minutes to make your first real move -- an edit, a "
    "premise_check or a dispute. If ten minutes pass with none of those and you "
    "are still turning the task over, the run returns to the person with your "
    "reasoning, unfinished. So do not read and re-read without acting: once you "
    "have shown the task's claim cannot hold, call dispute."
)
"""Appended to the system prompt with `--stall-check`, so the eject is a stated
rule the model can satisfy (act, or dispute), not a silent trap."""

ENVIRONMENT_PROMPT: Final = (
    " Your working directory is {worktree}, a fresh git worktree of the "
    "repository; your commands run in it inside a sandbox with no network. The "
    "repository's own checkout outside this worktree is not visible to them, so "
    "work only here, and tools installed elsewhere (uv, for one) may not be "
    "reachable. Git is read-only there: status, diff, log and show work, but "
    "commands that write (commit, checkout, stash, reset) fail. saddle commits "
    "the worktree when the run ends, with your finish summary in the commit "
    "message. /tmp is private to this run and lasts for all of it, so keep "
    "scratch files there. {python} {src}The audit runs "
    "the tests with `{test_command}` in this worktree. The whole suite can take "
    "many minutes in some projects, so run the test files that cover your change "
    "first. This run has {minutes} and {tokens} generated tokens; it stops at "
    "either limit, so leave room to call finish.{workers}{coverage}{node}{feed}"
)

NODE_TOOLS_PROMPT: Final = (
    " The project's installed JavaScript packages are in `node_modules`, read-only. "
    "`npx` is not on PATH: where the project runs `npx --no-install <tool>` (its "
    "formatter, linter or type checker), run `node_modules/.bin/<tool>` with the same "
    "arguments. The audit runs those tools too, so run them on the files you change."
)
"""Said when the run's worktree has the checkout's `node_modules` (`node_modules_mount`).
A watched run's JavaScript fix failed the project's prettier stage, which the
finish audit runs, and it had no way to run prettier: its worktree held tracked
files only ("I can't run c8 here because there's no node_modules")."""


def node_modules_mount(checkout: Path, worktree: Path) -> tuple[tuple[Path, Path], ...]:
    """The checkout's `node_modules`, to mount read-only at the worktree's own
    (`Sandbox.mounted`); none when the checkout has none. A git worktree holds
    tracked files only, and `node_modules` is git-ignored."""
    modules = checkout / "node_modules"
    if not modules.is_dir():
        return ()
    return ((modules.resolve(), worktree / "node_modules"),)


FEED_PROMPT: Final = (
    " While you work, saddle audits snapshots of this worktree in the "
    "background and appends each result to your next tool result: a line in "
    "square brackets reading audit checkpoint N on tree <id>, then PASS or FAIL, "
    "with any failing checks after it. It describes the tree at that snapshot, "
    "not the command it "
    "follows. Every file left in the worktree is audited, so remove scratch "
    "files from it before you call finish, which runs the same audit on the "
    "final tree. That final audit also runs your new and changed tests against "
    "the original code, where at least one of them must fail (others may pass "
    "there: a test that pins behaviour the change keeps is fine), so you do not "
    "need to undo your change to show that. If your change must rewrite an "
    "assertion in an existing test because that test pins the behaviour you were "
    "asked to change, keep the rewrite: the audit asks a person to approve it when "
    "the run ends, so name each such test and why in your finish summary."
)
"""Said only when the run delivers audits to the model (arm E+A+F). A watched
dogfood run met its first checkpoint note inside its own script's output and
spent a paragraph guessing where it came from ("the repo's conftest??").
Another undid its own fix to show its new tests failing on the old code, a
check the finish audit makes itself (red-phase), and lost the fix."""
"""What the run's commands actually see, from facts saddle already holds
(`environment_prompt`). A dogfood run on saddle's own repo spent seven rounds
finding a Python that could import the project, because none of this was said.
The git and /tmp sentences: a watched run's `git checkout` hit the read-only
git directory, and the copies it had saved in a per-command /tmp were gone."""


def environment_prompt(
    worktree: Path,
    project: Path | None,
    env: dict[str, str],
    time_budget_s: float,
    token_budget: int,
    feed: bool = False,
    node_tools: bool = False,
) -> str:
    """`ENVIRONMENT_PROMPT` filled in: which Python the model's commands get
    (the project venv, else whatever `python` or `python3` their PATH has),
    whether `src/` leads their import path, the audit's test command, and the
    run's budgets as they stand at the start. A dogfood run spent ten of its
    thirty minutes on a whole-suite baseline and had not edited a file at
    minute eighteen: nothing had told it how long it had."""
    if project is not None:
        python = "`python` on PATH is the project's own environment."
    else:
        path = sandbox.command_env(env)["PATH"]
        found = shutil.which("python", path=path)
        found3 = shutil.which("python3", path=path)
        python = "The project has no virtual environment of its own" + (
            f"; `python` on PATH is {found}."
            if found is not None
            else f", and there is no `python` on PATH: use `python3` ({found3})."
            if found3 is not None
            else ", and no Python is on PATH."
        )
    src = (
        "This worktree's `src/` is first on PYTHONPATH, so importing the project's "
        "package loads the code you edit. "
        if "PYTHONPATH" in src_layout_env(worktree)
        else ""
    )
    coverage = (
        " This project's pytest options add coverage, so a run of a few test files "
        "reports a coverage failure that says nothing about your change: pass "
        "`--no-cov` for those quick runs."
        if "--cov" in pytest_addopts(worktree)
        else ""
    )
    minutes = max(1, math.ceil(time_budget_s / 60))
    try:
        count = suite_workers(worktree, "HEAD").count
    except SuiteLimitError:
        count = 1
    xdist = project is not None and any(project.glob("lib/python*/site-packages/xdist"))
    workers = (
        f" The audit runs that suite on {count} workers; when you run the whole suite, "
        f"pass `-n {count}` too, or it runs on one core and takes far longer."
        if count > 1 and xdist
        else ""
    )
    return ENVIRONMENT_PROMPT.format(
        worktree=worktree,
        python=python,
        src=src,
        test_command=AUDIT_TEST_COMMAND,
        minutes=f"{minutes} minute{'s' if minutes != 1 else ''}",
        tokens=f"{token_budget:,}",
        coverage=coverage,
        workers=workers,
        node=NODE_TOOLS_PROMPT if node_tools else "",
        feed=FEED_PROMPT if feed else "",
    )


def pytest_addopts(worktree: Path) -> str:
    """The `addopts` a pytest run in `worktree` picks up from `pyproject.toml`
    (`[tool.pytest.ini_options]`) or `pytest.ini` (`[pytest]`), joined; ""
    when neither sets any or a file does not parse. A dogfood run on saddle's
    own repo ran one test file and got the repo's `--cov-fail-under=100`
    failure for the whole package."""
    found: list[str] = []
    try:
        table = tomllib.loads((worktree / "pyproject.toml").read_text(encoding="utf-8"))
        opts = table.get("tool", {}).get("pytest", {}).get("ini_options", {}).get("addopts", "")
        found.append(" ".join(opts) if isinstance(opts, list) else str(opts))
    except (OSError, tomllib.TOMLDecodeError):
        pass
    ini = configparser.ConfigParser()
    try:
        ini.read_string((worktree / "pytest.ini").read_text(encoding="utf-8"))
        found.append(ini.get("pytest", "addopts", fallback=""))
    except (OSError, configparser.Error):
        pass
    return " ".join(part for part in found if part)


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
    "task_units",
    "task_examples",
    "task_requirements",
    "task_passes",
    "task_prompts",
    "auto",
)
"""The modules a run on saddle's own source may not finish on: the judges
(`gates`, `evidence`, `auditor`, `audit`), what decides whether a finish is
accepted (`feed`, `engine`), what confines and caps the auditor's runs
(`sandbox`, `memcap`), the task-requirements check (`task_units`,
`task_examples` and `task_requirements`, which the gates and the auditor
call, and `task_passes` with its `task_prompts`, which write the
requirements file a run is judged against), and `auto` itself, which holds these lists: a run that
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
    "tests/test_task_units.py",
    "tests/test_task_examples.py",
    "tests/test_task_requirements.py",
    "tests/test_task_passes.py",
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
    coauthor: bool = True
    format_at_finish: bool = False
    premise_check: bool = False
    """`--premise-check`: edits are refused until the model has shown the problem
    with `premise_check` (engine), which puts the real output in front of it."""
    stall_check: bool = False
    """`--stall-check`: after ~10 min, a run that has never edited, run
    premise_check or disputed and is still hedging is returned to the user
    (engine `STALLED`). Off: nothing is scored."""
    """`--format-at-finish`: before each finish audit, `ruff format` the run's
    changed Python files with the project's committed formatter settings. Off
    by default: the model owns its changes."""
    """End the run's commit with `anchor.COAUTHOR_TRAILER`: Saddle wrote the
    change. On unless the user asks for it off (`--no-coauthor`)."""
    temperature: float = TASK_TEMPERATURE
    """`TASK_TEMPERATURE`: the model's own recommended sampling. A measurement
    that wants greedy decoding pins `--temperature 0.0` explicitly."""
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
    task_requirements: Path | None = None
    """`--task-requirements FILE`: a sealed P1 file for this task text
    (`saddle requirements extract`); tier 1 then runs the task text's
    examples. Needs an auditor (arm E+A or E+A+F). Off by default."""
    extract_requirements: bool = False
    """`--extract-requirements`: run P1's extraction at run start, beside the
    worker, and seal the file in the run's own state; checkpoints before it
    is ready report it pending, and `finish` waits for it up to the run's
    remaining time. Needs an auditor. Off by default."""
    references: tuple[Path, ...] = ()
    """`--reference DIR` (repeatable, with `--extract-requirements`): reference
    implementations the user vouches for, run as known-correct probes at
    extraction and sealed by hash with source `user`. Without one, a route
    (b) example can only ask: in the product only literal examples refuse."""
    wheels: WheelFolder | None = None
    """`--allow-installs`: the wheel folder approved installs come from
    (`installs`). Set, the model is offered an `install` tool; each call is
    put to the user, and an approved one installs into an overlay on the
    project venv under the run's own state, removed when the run ends. The
    run needs a project venv and a folder holding wheels, or it does not
    start. None (the default): no tool, and records as before."""
    resume_messages: Path | None = None
    """Dev, `--resume-messages FILE`: a recorded request body (a relay's
    `reqs/<n>.json`) to continue instead of starting from the task. Its
    messages go to the model as they are, behind this run's system prompt,
    with the recorded worktree path rewritten to this run's. None: from the
    task, as before."""
    resume_patch: Path | None = None
    """Dev, `--resume-patch FILE`: a diff applied to the fresh worktree before
    the model continues: the files as they stood at the recorded request."""
    resume_tmp: Path | None = None
    """Dev, `--resume-tmp DIR`: copied into the run's /tmp, what the recorded
    run had left there."""


@dataclass(frozen=True)
class AutoResult:
    run_id: str
    worktree: Path
    branch: str
    journal: Path
    outcome: str
    reason: str
    commit: str
    base: str = ""
    """The commit the run's branch started from (a 40-hex sha, never a branch name)."""


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


def resumed(recorded: Path, worktree: Path, system_prompt: str) -> list[dict[str, Any]]:
    """The messages of a recorded request body, to continue in `worktree`.

    The recorded system prompt gives way to `system_prompt` (this harness's),
    and every mention of the recorded run's worktree becomes `worktree`, so a
    command the model repeats reaches this run's files. For resuming a watched
    run at a chosen request instead of replaying the minutes before it."""
    body = json.loads(recorded.read_text(encoding="utf-8"))
    recorded_messages = list(body["messages"])
    first = recorded_messages[0] if recorded_messages else {}
    old_system = str(first.get("content") or "") if first.get("role") == "system" else ""
    rest = recorded_messages[1:] if old_system else recorded_messages
    text = json.dumps(rest)
    marker = "Your working directory is "
    if marker in old_system:
        old = old_system.split(marker, 1)[1].split(",", 1)[0]
        text = text.replace(json.dumps(old)[1:-1], json.dumps(str(worktree))[1:-1])
    return [{"role": "system", "content": system_prompt}, *json.loads(text)]


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


def run_base(repo: Path) -> str:
    """What the run branch starts from: the checked-out branch's name, else HEAD's commit.

    The packet's Reproduce row prints `git log -p <base>..<run branch>`, so
    the base is recorded at the start, not assumed to be `main`. A detached
    HEAD, or a branch name holding the start record's `;` separator, is
    recorded as the 40-hex commit instead.
    """
    name = _git(repo, "rev-parse", "--abbrev-ref", "HEAD").strip()
    if name == "HEAD" or ";" in name:
        return _git(repo, "rev-parse", "HEAD").strip()
    return name


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


FORMATTED_AT_FINISH: Final = "saddle formatted {files} before the audit (--format-at-finish): "
"""How the finish result begins when `format_changed` changed a file."""


def format_changed(worktree: Path, base: str) -> str:
    """`ruff format` the run's changed Python files, with the formatter
    settings committed at `base`; what it changed, for the model, or "" when
    nothing needed it. The audit then judges the formatted tree."""
    files = [f for f in changed_files(worktree) if f.endswith(".py") and (worktree / f).is_file()]
    if not files:
        return ""
    before = {f: (worktree / f).read_bytes() for f in files}
    run_capture(ruff_argv("format", *format_overrides(worktree, base), *files), worktree)
    moved = [f for f in files if (worktree / f).read_bytes() != before[f]]
    return FORMATTED_AT_FINISH.format(files=", ".join(moved)) + "check the diff" if moved else ""


def self_guard(worktree: Path) -> Callable[[], list[str]] | None:
    """For a run on saddle's own source: which guarded paths it has changed.

    None when the worktree is not saddle (no `SELF_PACKAGE` at the baseline):
    a repository that merely has a `gates.py` is not saddle's judge, and the
    guard does not apply to it.
    """
    if not (worktree / SELF_PACKAGE).is_file():
        return None
    return lambda: sorted(p for p in changed_files(worktree) if p in GUARDED_PATHS)


P1_FILE: Final = "task-requirements.json"
"""Where `--extract-requirements` seals the file: beside the run's ledger."""


def extraction_counts(record: dict[str, object]) -> str:
    """What an extraction sealed, counted: the `p1:extract` span's detail."""

    def n(key: str) -> int:
        value = record.get(key)
        return len(value) if isinstance(value, list) else 0

    said = cut_calls(record)
    return (
        f"{n('units')} candidate unit(s), {n('examples')} example(s), "
        f"{n('not_executable')} not executable, {n('cut')} cut, "
        f"{n('unanswered')} unanswered, {n('probes')} probe(s)" + (f", {said}" if said else "")
    )


def _seal_extraction(journal: Path, run_span: str, began: float, code: int, detail: str) -> None:
    """The `p1:extract` span: the extraction's wall time and what it sealed or why it failed."""
    took = int((monotonic() - began) * 1000)
    append_span(
        journal,
        build_span(
            node_id="chat#1",
            argv=[P1_EXTRACT_SPAN, P1_FILE],
            duration_ms=took,
            exit_code=code,
            detail=detail,
            kind="agent",
            name=P1_EXTRACT_SPAN,
            parent_id=run_span,
            started_at=started_before(took, utc_now()),
        ),
    )


def _requirements(
    options: AutoOptions,
    client: VllmClient,
    worktree: Path,
    base: str,
    journal: Path,
    run_span: str,
) -> tuple[Future[Path] | None, ThreadPoolExecutor | None]:
    """The run's P1 file as a future (done at once for `--task-requirements`),
    and the pool an extraction runs on; (None, None) without P1. An
    extraction seals a `p1:extract` span when it ends, sealed or failed."""
    if options.task_requirements is not None:
        ready: Future[Path] = Future()
        ready.set_result(options.task_requirements.resolve())
        return ready, None
    if not options.extract_requirements:
        return None, None
    target = journal.parent / P1_FILE

    def extract() -> Path:
        began = monotonic()
        try:
            record = extract_requirements(
                options.task,
                client,
                sources=baseline_sources(worktree, base),
                # The name the passes' requests carry, sealed with their replies.
                model=client.model,
                probes=[ProbeTree(r.resolve(), "user") for r in options.references],
            )
            target.write_text(json.dumps(record, sort_keys=True), encoding="utf-8")
        except Exception as exc:
            _seal_extraction(journal, run_span, began, 1, f"{type(exc).__name__}: {exc}")
            raise
        _seal_extraction(journal, run_span, began, 0, extraction_counts(record))
        return target

    pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix="saddle-p1-extract")
    return pool.submit(extract), pool


def run_auto(
    options: AutoOptions,
    client: VllmClient,
    *,
    on_event: Callable[[Event], None] | None = None,
    audit: Callable[[str, str, str], Sequence[Event]] | None = None,
    answer: Callable[[Question], str | None] | None = None,
    cancel: Callable[[], bool] | None = None,
    on_budget: Callable[[RunBudget], None] | None = None,
    on_worktree: Callable[[Path, str, str], None] | None = None,
    on_commit: Callable[[str], None] | None = None,
) -> AutoResult:
    """Run one task to `finish` or a budget, then commit what it left.

    `audit` and `answer` are the auditor seam (`engine.AutoRun`); `cancel`
    is the chat's stop button; `on_budget` is handed the run's own
    `RunBudget` -- the object the engine charges -- once it exists, so a
    watcher reads spend from the run's accounting rather than keeping its
    own clock. `on_worktree` is handed the run's worktree, branch and base
    commit the moment the worktree exists, so a watcher can record where the
    run lives before it ends, and `on_commit` the sha its branch ends on. The
    CLI passes none of them.
    """
    if options.arm not in ARMS:
        msg = f"unknown arm {options.arm!r}; expected one of {', '.join(ARMS)}"
        raise AutoError(msg)
    if options.finish_refusal_cap < 1:
        msg = f"finish refusal cap must be at least 1, got {options.finish_refusal_cap}"
        raise AutoError(msg)
    if options.task_requirements is not None and options.extract_requirements:
        msg = "--task-requirements and --extract-requirements: give one, not both"
        raise AutoError(msg)
    if options.references and not options.extract_requirements:
        msg = "--reference needs --extract-requirements (a sealed file already holds its probes)"
        raise AutoError(msg)
    missing = [str(r) for r in options.references if not r.is_dir()]
    if missing:
        msg = f"--reference {missing[0]} is not a directory"
        raise AutoError(msg)
    if (options.task_requirements is not None or options.extract_requirements) and (
        options.arm == "E"
    ):
        msg = "P1 (--task-requirements/--extract-requirements) needs an auditor; arm E has none"
        raise AutoError(msg)
    if options.check_tool and options.arm != "E+A+F":
        msg = f"--check-tool needs arm E+A+F (it delivers audit findings); got {options.arm}"
        raise AutoError(msg)
    repo = options.repo.resolve()
    project = sandbox.project_env(repo_root(repo))
    problem = sandbox.project_env_problem(project) if project is not None else None
    if problem is not None and options.arm != "E":
        raise AutoError(problem)
    if options.wheels is not None:
        if project is None:
            msg = (
                "--allow-installs: approved installs go into an environment layered on the "
                "project's virtualenv, and there is none (a .venv or venv with a pyvenv.cfg "
                "in the folder, or an active VIRTUAL_ENV)"
            )
            raise AutoError(msg)
        folder_problem = options.wheels.problem()
        if folder_problem is not None:
            msg = f"--allow-installs: {folder_problem}"
            raise AutoError(msg)
    with sandbox.using_project_env(project):
        environment = sandbox.gate_environment()
    run_id = options.run_id or uuid.uuid4().hex[:12]
    base = run_base(repo)
    worktree, branch = create_worktree(repo, run_id)
    base_commit = _git(worktree, "rev-parse", "HEAD").strip()
    if on_worktree is not None:
        on_worktree(worktree, branch, base_commit)
    if options.resume_patch is not None:
        _git(worktree, "apply", str(options.resume_patch.resolve()))
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
        f"base {base}; "
        f"budgets {options.time_budget_s:.0f}s, "
        f"{options.token_budget} generated tokens; test edits "
        f"{'allowed' if options.allow_test_edits else 'refused'}; "
        f"environment {environment}"
        + ("; check tool offered" if options.check_tool else "")
        + (
            "; "
            + f"installs from {options.wheels.path} ({options.wheels.source})".replace(";", "%3B")
            if options.wheels is not None
            else ""
        )
        + (
            "; task requirements "
            + (
                str(options.task_requirements).replace(";", "%3B")
                if options.task_requirements is not None
                else "extracted at run start"
                + (
                    f" with {len(options.references)} user reference(s)"
                    if options.references
                    else ""
                )
            )
            if options.task_requirements is not None or options.extract_requirements
            else ""
        ),
        kind="agent",
        started_at=utc_now().isoformat(),
    )
    append_span(journal, start)
    p1, extraction = _requirements(options, client, worktree, base, journal, start.span_id)
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
            project_env=project,
            sanctioned_test_rewrites=options.sanctioned_test_rewrites,
            task=options.task,
            tier2=options.tier2,
            mutant_shortlist=options.mutant_shortlist,
            p1=p1,
            p1_wait=lambda: auto.budget.time_left(),
            impact_cache=root / ".saddle" / "impact",
        )
    )
    guard = self_guard(worktree)
    installed: list[str] = []
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
        tell_summary=feed.tell_summary if feed is not None else None,
        require_premise=options.premise_check,
        stall_check=options.stall_check,
        guard=guard,
        before_finish=(
            (lambda: format_changed(worktree, base)) if options.format_at_finish else None
        ),
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
                {
                    "task_requirements": str(options.task_requirements)
                    if options.task_requirements is not None
                    else "extracted at run start"
                }
                if p1 is not None
                else {}
            ),
            **(
                {"installs": {"wheel_dir": str(options.wheels.path), "installed": installed}}
                if options.wheels is not None
                else {}
            ),
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
    run_env = {**COMMAND_ENV, **sandbox.project_command_env(project), **src_layout_env(worktree)}
    mounted = node_modules_mount(repo_root(repo), worktree)
    turn_options = TurnOptions(
        workdir=worktree,
        journal=journal,
        temperature=options.temperature,
        reasoning_effort=options.reasoning_effort,
        system_prompt=SYSTEM_PROMPT.format(tests=tests)
        + environment_prompt(
            worktree,
            project,
            run_env,
            options.time_budget_s,
            options.token_budget,
            feed=options.arm == "E+A+F",
            node_tools=bool(mounted),
        )
        + (CHECK_PROMPT if options.check_tool else "")
        + (PREMISE_PROMPT if options.premise_check else "")
        + (STALL_PROMPT if options.stall_check else "")
        + provider_prompt(),
        context_tokens=options.context_tokens,
        tools=[
            *TOOLS,
            FINISH_SCHEMA,
            DISPUTE_SCHEMA,
            REFUSE_SCHEMA,
            BLOCKED_SCHEMA,
            *([PREMISE_SCHEMA] if options.premise_check else []),
            *([CHECK_SCHEMA] if options.check_tool else []),
            *([INSTALL_SCHEMA] if options.wheels is not None else []),
            *provider_schemas(),
        ],
        auto=auto,
        keep_reasoning=options.keep_reasoning,
    )
    # The run's own /tmp, shared by all its commands and never audited: it
    # sits beside the ledger, outside the worktree, and goes when the run ends.
    run_tmp = journal.parent / "tmp"
    run_tmp.mkdir(parents=True, exist_ok=True)
    if options.resume_tmp is not None:
        # symlinks kept as links: pytest leaves dangling `pytest-current` ones
        shutil.copytree(options.resume_tmp, run_tmp, dirs_exist_ok=True, symlinks=True)
    # The commands the project names for its gates' sandbox (`[tool.saddle]
    # sandbox-expose`, read at the run's start) are shown to the model's too: a
    # watched run's commands had no `node`, so its node tests failed and the
    # project's node_modules/.bin tools (`#!/usr/bin/env node`) could not start.
    try:
        named = sandbox_expose(worktree, "HEAD")
    except (SuiteLimitError, tomllib.TOMLDecodeError):
        # A setting the audit cannot read either: it names the file when it runs.
        named = ()
    with sandbox.also_exposing(named):
        command_sandbox = Sandbox.for_workdir(
            worktree,
            env=run_env,
            require_isolation=True,
            network="none",
            tmp=run_tmp,
            mounted=mounted,
        )
    context = ToolContext(
        workdir=worktree,
        sandbox=command_sandbox,
        protected_tests=roots,
        syntax_guard=True,
        time_left=lambda: auto.budget.time_left(),
    )
    if options.wheels is not None:
        assert project is not None  # checked before the run started
        box = context.sandbox
        assert box is not None

        def use_overlay(overlay: Path) -> None:
            """From now on the gates and the model's commands run on `overlay`."""
            if feed is not None:
                feed.project_env = overlay
            box.env = {
                **COMMAND_ENV,
                **sandbox.project_command_env(overlay),
                **src_layout_env(worktree),
            }
            box.expose = sandbox.default_expose(sandbox.command_env(box.env))

        auto.installs = Installs(
            folder=options.wheels,
            project=project,
            overlay=journal.parent / "overlay",
            on_ready=use_overlay,
            installed=installed,
        )
    messages: list[dict[str, Any]] = []
    text: str | None = options.task
    if options.resume_messages is not None:
        messages = resumed(options.resume_messages, worktree, turn_options.system_prompt or "")
        text = None  # answer what is there: the recorded conversation's next step
    events: Iterator[Event] = run_turn(
        client, messages, text, turn_options, turn=1, context=context, cancel=cancel
    )
    try:
        for event in events:
            if on_event is not None:
                on_event(event)
    finally:
        shutil.rmtree(run_tmp, ignore_errors=True)
        if auto.installs is not None:
            auto.installs.remove()
        if extraction is not None:
            # An extraction still running holds model calls; it is waited for,
            # never abandoned mid-seal.
            extraction.shutdown(wait=True)
    _git(worktree, "add", "-A", "--", *UNSTAGED)
    message = f"saddle auto {run_id}: {auto.outcome} ({auto.reason})"
    if auto.narrative:
        message += f"\n\nNarrative (model-written, not evidence):\n{auto.narrative}"
    # The anchor: the outcome span's hash, outside the ledger, as the last paragraph.
    ledger = journal.relative_to(root).as_posix()
    message += f"\n\n{anchor_trailers(outcome_hash(journal, start.span_id), ledger)}"
    if options.coauthor:
        message += f"\n{COAUTHOR_TRAILER}"
    _git(worktree, "commit", "-q", "--allow-empty", "--no-verify", "-m", message)
    commit = _git(worktree, "rev-parse", "HEAD").strip()
    # The commit's message holds the outcome's hash, so the outcome cannot name
    # the commit; this record, beside the ledger and bound to its outcome, says
    # which commit the ledger covers.
    tree = _git(worktree, "rev-parse", "HEAD^{tree}").strip()
    append_span(
        coverage_path(journal),
        build_span(
            node_id=start.node_id,
            argv=[AUTO_COMMITTED, commit, outcome_hash(journal, start.span_id)],
            duration_ms=0,
            exit_code=0,
            detail=f"branch {branch}; base {base_commit}; commit {commit}; tree {tree}",
            kind="agent",
            name=AUTO_COMMITTED,
            started_at=utc_now().isoformat(),
        ),
    )
    if on_commit is not None:
        on_commit(commit)
    return AutoResult(
        run_id=run_id,
        worktree=worktree,
        branch=branch,
        journal=journal,
        outcome=auto.outcome,
        reason=auto.reason,
        commit=commit,
        base=base_commit,
    )
