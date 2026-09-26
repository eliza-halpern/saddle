"""Autonomous runs: a task string and a repo in, a branch and a ledger out.

This is arm E of Phase 2: saddle's own executor, `engine.run_turn`, run
with no human turn and no auditor. It is not a second tool loop (T5-7,
clause 2): it sets up a worktree, turns on the tier-0 guards and a budget,
and hands one turn to the same engine the chat uses.

- The run happens in a fresh `git worktree` under
  `.saddle/worktrees/<run-id>/` on branch `saddle/auto/<run-id>`, never in
  the user's checkout. The result is that branch.
- The ledger is `.saddle/runs/<run-id>/proofs.jsonl`, outside the
  worktree, so the tools cannot reach it; `saddle verify` reads it.
- It ends `finished` only when the model calls `finish`, and otherwise
  `stopped`, naming the budget or error. Neither is a claim that the change
  works: no gate runs here.
"""

from __future__ import annotations

import subprocess
import tomllib
import uuid
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from pathlib import Path
from time import monotonic
from typing import Final

from saddle.engine import AutoRun, RunBudget, TurnOptions, run_turn
from saddle.events import Event
from saddle.journal import append_span, build_span
from saddle.sandbox import Sandbox
from saddle.tools import FINISH_SCHEMA, TOOLS, ToolContext
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

COMMAND_ENV: Final = {"PYTHONDONTWRITEBYTECODE": "1"}
"""No command the run starts writes bytecode. Python trusts a `.pyc` whose
recorded source mtime (one-second resolution) and size match, so a
same-length edit followed by a run in the same second would execute the
old code and fail a correct fix (CLAUDE.md, harness rule 3). The worktree
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
        ["git", "-C", str(repo), *GIT_IDENTITY, *args],
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


def changed_files(worktree: Path) -> list[str]:
    """Paths the run changed, added or deleted, relative to the worktree."""
    out = _git(worktree, "status", "--porcelain", "--untracked-files=all", "-z", "--", *UNSTAGED)
    return sorted({entry[3:] for entry in out.split("\0") if len(entry) > 3})


def run_auto(
    options: AutoOptions,
    client: VllmClient,
    *,
    on_event: Callable[[Event], None] | None = None,
) -> AutoResult:
    """Run one task to `finish` or a budget, then commit what it left."""
    repo = options.repo.resolve()
    run_id = options.run_id or uuid.uuid4().hex[:12]
    worktree, branch = create_worktree(repo, run_id)
    root = worktree.parent.parent.parent
    journal = root / ".saddle" / "runs" / run_id / "proofs.jsonl"
    start = build_span(
        node_id="chat#1",
        argv=["auto:start", options.task],
        duration_ms=0,
        exit_code=0,
        detail=f"branch {branch}; budgets {options.time_budget_s:.0f}s, "
        f"{options.token_budget} generated tokens; test edits "
        f"{'allowed' if options.allow_test_edits else 'refused'}",
        kind="agent",
    )
    append_span(journal, start)
    auto = AutoRun(
        budget=RunBudget(
            time_s=options.time_budget_s, tokens=options.token_budget, clock=options.clock
        ),
        run_span=start.span_id,
        changed_files=lambda: changed_files(worktree),
    )
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
        system_prompt=SYSTEM_PROMPT.format(tests=tests),
        context_tokens=options.context_tokens,
        tools=[*TOOLS, FINISH_SCHEMA],
        auto=auto,
    )
    context = ToolContext(
        workdir=worktree,
        sandbox=Sandbox.for_workdir(worktree, env=COMMAND_ENV),
        protected_tests=roots,
        syntax_guard=True,
    )
    events: Iterator[Event] = run_turn(
        client, [], options.task, turn_options, turn=1, context=context
    )
    for event in events:
        if on_event is not None:
            on_event(event)
    _git(worktree, "add", "-A", "--", *UNSTAGED)
    message = f"saddle auto {run_id}: {auto.outcome} ({auto.reason})"
    if auto.narrative:
        message += f"\n\nNarrative (model-written, not evidence):\n{auto.narrative}"
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
