"""Saddle command line interface.

`run` drives one mechanical task end to end: guided DAG emission with
bounded recompile, plan confirmation, then the schedule-gate-seal path.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from collections.abc import Callable, Collection, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import IO, Final

from pydantic import ValidationError
from rich.console import Console

from saddle import __version__
from saddle.chat import ChatOptions, run_chat
from saddle.dag import REQ_NEAR_MISS_K, Dag, Node, validate_dag
from saddle.edits import EDIT_GRAMMAR
from saddle.evidence import (
    RUFF_RULES,
    SADDLE_COMMIT_IDENTITY,
    git_ls_files,
    ruff_version,
    run_argv,
)
from saddle.gates import (
    plan_prescribes_deletion,
    plan_restates_the_gate,
    plan_retargets_reserved_files,
)
from saddle.journal import (
    JournalIssue,
    SpanRecord,
    attempt_sidecar_path,
    read_entries,
    read_plans,
    read_records,
    read_spans,
    verify_journal,
)
from saddle.slice import DEADLINE_EXIT, SURVIVOR_SAMPLES, ReplanFailedError, TestDrawer, run_slice
from saddle.transcript import is_run_end, render_event, render_journal_transcript, render_plan
from saddle.ux import ask_confirm
from saddle.vllm import (
    DEFAULT_BASE_URL,
    DEFAULT_MODEL,
    DIFF_GRAMMAR,
    REASONING_EFFORTS,
    DiffProposal,
    VllmAuthError,
    VllmClient,
    VllmError,
    VllmRequestError,
    VllmResponseError,
)

# Every name a plan may list, and the harness behaviour listing it buys
# (T3-4). The list was decorative until this registry existed: it was
# validated against emissions and consumed nowhere, so a plan that dropped
# `read_file` still got the whole repo inlined. A name with no binding here
# is a name the node cannot be given, so `RUN_ALLOWLIST` is derived rather
# than written twice.
TOOL_BINDINGS: Final[dict[str, str]] = {
    "read_file": ("the prompt carries the repo files' contents; without it, the file names only"),
    "write_file": (
        "the node may create files, subject to its kind's scope rule; "
        "without it, creating any file fails the node-scope gate"
    ),
    "run_tests": (
        "a failed attempt's repair prompt carries the test command's output; "
        "without it, the gate verdict lines only"
    ),
    "lint": ("the repair prompt carries ruff's output; without it, the gate verdict lines only"),
}
RUN_ALLOWLIST: Final[tuple[str, ...]] = tuple(TOOL_BINDINGS)
CONTENTS_WITHHELD: Final = "(file contents withheld: read_file not in allowed_tools)"
CONTEXT_CEILING: Final = 30000
EMIT_ROUNDS: Final = 3
MAX_FILES_IN_PROMPT: Final = 100
NODE_CONTEXT_TOKENS: Final = 30000
CHARS_PER_TOKEN: Final = 4
MAX_CONTEXT_CHARS: Final = NODE_CONTEXT_TOKENS * CHARS_PER_TOKEN
BUDGET_TO_EFFORT: Final[dict[str, str]] = {
    "zero": "none",
    "low": "low",
    "medium": "medium",
    "xhigh": "xhigh",
}
# `max_context_tokens` is the worker's INPUT ceiling (ARCHITECTURE.md §2:
# "each subagent receives a clean ~28,000-30,000-token ceiling"). Generation
# is not budgeted at all (T6-17): this model's strategy is long test-time
# compute, vLLM enforces no split between thinking and content, and every
# cap the harness tried was paid for by the diff, not the thinking -- F21.10
# arm (c) reasoned 17571 tokens at effort `low` against a 16384 "allowance"
# and truncated 3/3. So a worker call asks for everything the context has
# left after its prompt; `finish_reason=length` is then the model's ceiling,
# not the harness's, and stays a retryable attempt failure whose evidence
# is in the sidecar (T6-12). The window comes from the server
# (`max_model_len` on `GET /models`), else `--context-window`, else the
# container's `MAX_LEN`.
DEFAULT_CONTEXT_WINDOW: Final = 175000
# Code tokenises denser than prose; F21.10's 24739-char worker prompt was
# 6586 tokens (3.76 chars/token). Three over-counts the prompt, so the
# request can never exceed the window, at the cost of room no diff needs.
PROMPT_CHARS_PER_TOKEN: Final = 3
# Slack under the window for the chat template and the count's error.
OUTPUT_MARGIN: Final = 2048


def worker_max_tokens(prompt: str, context_window: int) -> int:
    """The `max_tokens` a worker call sends: the window left after `prompt` (T6-17)."""
    left = context_window - len(prompt) // PROMPT_CHARS_PER_TOKEN - OUTPUT_MARGIN
    return max(left, OUTPUT_MARGIN)


def diff_budget(node: Node, context_window: int) -> int:
    """Most a node's diff may be estimated at and still be planned (T6-8).

    The window minus the node's own read ceiling and the margin: a diff
    that cannot fit in the room its prompt leaves is a node no retry can
    rescue, so it is rejected before any worker call and the planner
    splits it. Since T6-17 nothing is subtracted for reasoning, because
    nothing caps it.
    """
    return context_window - node.execution_constraints.max_context_tokens - OUTPUT_MARGIN


def server_context_window(client: VllmClient, override: int | None) -> int:
    """The context window worker calls size against: flag, server, default."""
    if override is not None:
        return override
    try:
        reported = client.max_model_len()
    except VllmError:
        reported = None
    return reported or DEFAULT_CONTEXT_WINDOW


def _file_lines(repo: Path, files: Sequence[str]) -> dict[str, int]:
    """Baseline line count per listed file that exists and reads as text."""
    counts: dict[str, int] = {}
    for name in files:
        path = repo / name
        try:
            counts[name] = path.read_text().count("\n")
        except (OSError, UnicodeDecodeError):
            continue
    return counts


class RunError(Exception):
    """Operational `run` failure with a message fit to print."""


SURVIVOR_MAX_TOKENS: Final = 6000
"""Token cap for one candidate test draw: one test file and its reasoning at effort `low`."""


@dataclass(frozen=True)
class RunOptions:
    """Resolved `run` inputs: task, repo, journal, and sampling knobs."""

    task: str
    repo: Path
    journal: Path
    max_tokens: int = 8192
    temperature: float = 0.0
    sample_temperature: float = 0.7
    reasoning_effort: str = "medium"
    worker_effort: str | None = None
    yes: bool = False
    context_window: int = DEFAULT_CONTEXT_WINDOW
    recovery_temperature: float | None = None
    deadline_s: float | None = None
    # Sealed on the run span (T6-27): rounds were compared on the
    # assumption the model never moved, and nothing could have said if it had.
    model: str = DEFAULT_MODEL
    server_version: str = "unknown"
    # Survivor rounds (T6-29c): candidate tests are drawn at their own
    # effort and token cap, k at a time. F21.14: `none` yielded two usable
    # drafts in ten and no kills; numeric length instructions in the brief
    # do not land, so the cap is the harness's, not the prompt's.
    survivor_effort: str = "low"
    survivor_samples: int = SURVIVOR_SAMPLES
    survivor_max_tokens: int = SURVIVOR_MAX_TOKENS
    # Which envelope the worker emits in. "whole-file" restates every file
    # it touches; "edit" names the sites it changes, so a one-line change
    # costs one line. The prompt and the grammar have to agree, so both are
    # chosen from this single field rather than set independently.
    emission: str = "whole-file"


def survivor_drawer(client: VllmClient, options: RunOptions) -> TestDrawer:
    """The test-candidate draw a survivor round makes (T6-29c).

    One grammar-constrained diff call per seed at `survivor_effort`,
    capped at `survivor_max_tokens` (and by the window left after the
    brief), at the sample temperature: the brief is the whole prompt,
    and the harness cuts what the cap truncates back to the last
    complete test before judging it.
    """

    def draw(_node: Node, brief: str, seed: int) -> DiffProposal:
        return client.propose_diff(
            brief,
            max_tokens=min(
                options.survivor_max_tokens, worker_max_tokens(brief, options.context_window)
            ),
            temperature=options.sample_temperature,
            reasoning_effort=options.survivor_effort,
            seed=seed,
        )

    return draw


def worker_temperature(options: RunOptions, failure: str | None) -> float:
    """The temperature a worker diff call samples at (T6-15).

    First attempts use `sample_temperature`; retries use it too unless
    `recovery_temperature` is given. Until T6-15 a retry dropped to
    `temperature` (0.0 by default) for a reproducible repair (T2-1); F21.10
    arm (c) measured that: three seeds at 0.0 were one byte-identical
    sample and truncated 3/3, and a retry that resends a greedy walk after
    a degenerate one walks the same way. `temperature` still governs
    planning and the recovery-plan prose.
    """
    if failure is None:
        return options.sample_temperature
    if options.recovery_temperature is not None:
        return options.recovery_temperature
    return options.sample_temperature


@dataclass(frozen=True)
class DagOptions:
    """Resolved `dag` inputs: task, server, emission knobs, and the repo to list.

    `repo` is only read (`git ls-files`) so the planner sees the files it
    is planning for (T3-19); None lists nothing, for callers without one.
    """

    task: str
    base_url: str
    model: str
    max_tokens: int = 8192
    temperature: float = 0.0
    reasoning_effort: str = "medium"
    repo: Path | None = None
    context_window: int = DEFAULT_CONTEXT_WINDOW


def _file_listing(files: Sequence[str]) -> str:
    """The tracked files, capped like the worker prompt's list."""
    listed = list(files)
    if not listed:
        return "(no tracked files)"
    shown = "\n".join(listed[:MAX_FILES_IN_PROMPT])
    if len(listed) > MAX_FILES_IN_PROMPT:
        shown += f"\n... and {len(listed) - MAX_FILES_IN_PROMPT} more"
    return shown


def build_emit_prompt(task: str, files: Sequence[str] = ()) -> str:
    """Decomposition prompt: task, the repository's files, and the DAG rules.

    The tool rules render `TOOL_BINDINGS`, so a name the harness does not
    honour cannot reach the planner and a binding cannot change without
    the prompt saying so.

    `files` is the repository's tracked listing (T3-19). A planner that
    sees only the task sentence plans blind: the smoke run of 2026-09-19
    built a parallel `src/f.py` beside the `n.py` that already defined
    `f`, so the task's subject ended up twice with different behaviour.
    """
    tools = ", ".join(TOOL_BINDINGS)
    bindings = "\n".join(f"  - {name}: {effect}." for name, effect in TOOL_BINDINGS.items())
    return f"""Decompose the mechanical coding task below into a DAG of 1 to 4 nodes.

Task: {task}

Repository files (tracked):
{_file_listing(files)}

Rules:
- A task about behaviour an existing file already owns changes that file.
  Create a new module only when no listed file owns the behaviour; never
  create a parallel copy of a function the repository already defines.
  Never name a package "src": the mutation gate cannot instrument a
  module whose name starts with "src.".
- Each node has a kind: "test", "impl" or "refactor".
- Split behaviour changes into a "test" node and an "impl" node that
  depends on it. A "test" node writes the failing tests and may not
  change source files; an "impl" node makes them pass and may not change
  test files. One worker writing both sides encodes a misreading of the
  requirement twice, and grades itself on the suite it just rewrote.
  A "test" node's tests are expected to fail when it runs: its tests
  gate passes on a red run (failing tests, or an import of a module the
  "impl" node will create) and fails if they already pass.
- Use "refactor" only when behaviour is preserved: the code and its tests
  move together and no test can fail beforehand.
  A refactor node may not create or rename files; it edits existing code and tests in place.
- The first node has no dependencies; every other node depends on at least one earlier node.
- Each node carries at least one requirement: {{"id": "REQ-001", "statement": ...}}.
- The statement is one testable sentence saying what must hold, in the
  shape "<when/where>, the system shall <observable behaviour>". Write it
  so a test can fail when it is violated: "Rejects a local part ending in
  a dot", not "Validates email correctly". A statement no test can
  contradict states nothing.
- A statement describes behaviour a test can falsify, never how the gate
  measures. "quantize is never called with a JPY amount" is a
  requirement; "every changed line is executed by tests/test_fees.py" is
  the coverage gate restated, and a function whose body is `pass`
  satisfies it without implementing anything.
- Requirement IDs are REQ- followed by exactly three digits.
- Each requirement also carries "accepts" and "rejects": at least one
  literal input the system shall accept and at least one it shall reject.
  A reject is a near-miss, within {REQ_NEAR_MISS_K} edits of some accept,
  so that a test on it tells this requirement from a looser one. For
  accepts ["user@example.com"], "user@example.com." and "user@@example.com"
  are rejects; "user" is 12 edits away and rejects nothing a lazy
  validator would not, and the plan is invalid with it.
- A "test" node's tests assert on every accept and every reject, literally:
  the binding gate fails a test node whose tests assert on none of a
  cited example.
- A "test" node's tests cite the requirement id of every node they specify,
  the "impl" node's included: the binding gate fails a node whose id no
  test cites, and fails a node whose tests cite an id no node of the plan
  declares.
- reasoning_budget is one of: zero, low, medium, xhigh.
- Size reasoning_budget to the node: mechanical nodes (implement, wire, test)
  take low or zero; reserve medium/xhigh for complex algorithmic nodes.
- max_context_tokens is the node's READ budget (8000-30000). Pick the
  smallest figure that covers the files this node actually has to read.
  Context is a cost, not an allowance: a model attends worst to the middle
  of a long prompt, so padding the budget buries the file the node has to
  change underneath ones it does not.
- allowed_tools uses only: {tools}. Each name is a capability the node
  gets only because it listed it, so list what the node needs and nothing
  else:
{bindings}
- target_files lists the repo-relative files the node may touch, e.g.
  ["src/app/login.py"]. Required for impl and refactor nodes: the harness
  sizes the node's output budget from those files, and a node whose
  files are too large to diff in one response is rejected before it runs
  (split it by module or behaviour). A "test" node may leave it empty.
  Entries look like the example: never start one with "/" and never use "..".
  A node that touches a file outside its list fails.
  List every file the node will create as well as edit, including a new
  package's __init__.py: a node that adds a file its list omits fails.
  A "test" node's target_files names every test file it will write; the
  gate rejects any other.
- red_phase_required is always true.
- test_command is a pytest invocation over test files only,
  e.g. "pytest tests/test_login.py" (never a source file).
- changed_line_coverage_min is always 100.0. That is how the gate
  measures the node; it is not a requirement, and no node description or
  requirement statement may restate it. A plan that asks for lines to be
  executed or covered is rejected and redrawn.
- kill_threshold is one of 85.0, 90.0, 95.0, 100.0. There is no lower
  setting; a node you consider mechanical still clears 85.
- mutation_sample.scope is always "changed-lines"; max_mutants is always 100.
- Size the plan to the work, and size each node to one worker's single
  diff. A node that must rewrite several modules, or more code than fits
  comfortably in one response, is too big: split it by module or by
  behaviour until each node is one coherent, separately gateable change.
  A trivial task still needs only one node -- fewness is the result of a
  small task, never a target of its own.
- Think through the decomposition first; then emit the plan.
"""


WHOLE_FILE_RULES: Final = """\
Produce the COMPLETE NEW CONTENTS of every file you change, implementing
exactly that. You are not writing a patch: there is no original side to
reproduce and no context to match.
Rules:
- Start each file section with a "diff --git a/<file> b/<file>" header line.
- Follow it with exactly these two lines, verbatim:
  "--- /dev/null" and "+++ b/<file>".
- Then one "@@ -0,0 +1,<n> @@" line, where <n> is however many lines the
  file now has. It is not checked -- do not spend effort counting.
- Then EVERY line of the finished file, each prefixed with "+".
  Unchanged lines get a "+" too. A line starting with " " or "-" is
  rejected: those belong to a patch, and this is not one.
- Write each file at most once. Emitting a file twice is refused, not
  merged.
- To delete a file, emit its header, then "deleted file mode 100644",
  "--- a/<file>", "+++ /dev/null", and no body.
- A file you do not name is left exactly as it is. Only name the files
  you are changing.
"""

# The same instruction in the envelope that costs the change's size
# rather than the file's. Blank-line fidelity is deliberately not
# demanded: a live draw dropped one and was correct anyway, and the
# matcher now tolerates that (see edits.loose_spans).
EDIT_RULES: Final = """\
Produce EDITS to the files you change, implementing exactly that.
Rules:
- To change part of a file: "edit <file>", then the exact lines you are
  replacing each prefixed with "-", then a "=======" line, then the lines
  that replace them each prefixed with "+", then ">>>>>>>".
- The prefix is a single character and NOTHING follows it before the
  line's own text. Write "-def fee(amount):", never "- def fee(amount):".
  A line's own indentation is part of the line and is kept exactly.
- The "-" lines must reproduce the file exactly and must name ONE place in
  it. If they match twice they name no single site and the edit is
  refused, so include enough surrounding lines to be unique.
- Emit only the lines you are changing plus the few needed to locate
  them. Do not reproduce parts of the file you are not touching: a long
  block is slower and more likely to differ from the file somewhere.
- To create a new file: "create <file>", then every line prefixed with
  "+", then ">>>>>>>".
- To delete a file: "delete <file>" on its own line, nothing after it.
- Make as many edits as you need, in any order, to any files in scope.
- A file you do not name is left exactly as it is. Only name the files
  you are changing.

Worked example. Given fees.py containing:

    FLAT = 0.30

    def fee(amount):
        return amount * 0.03

to change only the rate, emit exactly this and nothing else:

edit fees.py
-    return amount * 0.03
=======
+    return amount * RATE
>>>>>>>

One line named, one line replacing it, its four spaces of indentation
carried through, and FLAT and the "def" line left alone because they are
not changing.
"""


def build_worker_prompt(
    *,
    task: str,
    node: Node,
    files: Sequence[str],
    contents: Mapping[str, str],
    emission: str = "whole-file",
) -> str:
    """Node work prompt: task, requirements, repo files, diff format rules.

    File context is truncated to the node's own `max_context_tokens`, which
    is what that field means; the global constant is only the hard ceiling.

    The node's task, requirements and gate command appear twice, bracketing
    the file contents. Attention follows a U-shaped curve -- strong at the
    start and end of a prompt, weakest in the middle -- and the file
    contents are both the longest section and the one that has to sit in
    the middle. An instruction stated only ahead of them is stated in the
    position the model reads best and then buried under everything it
    reads worst.

    `read_file` is the binding that decides whether the contents appear
    at all (T3-4): without it the node still gets the file *names*, which
    is what makes omitting it a context-cost lever rather than blindness.
    """
    listed = list(files)
    if not listed:
        shown = "(no tracked files)"
    else:
        shown = "\n".join(listed[:MAX_FILES_IN_PROMPT])
        if len(listed) > MAX_FILES_IN_PROMPT:
            shown += f"\n... and {len(listed) - MAX_FILES_IN_PROMPT} more"
    tools = node.execution_constraints.allowed_tools
    if "read_file" in tools:
        context = "\n\n".join(f"--- {name} ---\n{text}" for name, text in contents.items())
        budget = min(
            node.execution_constraints.max_context_tokens * CHARS_PER_TOKEN, MAX_CONTEXT_CHARS
        )
        if len(context) > budget:
            context = context[:budget] + "\n[file context truncated]"
    else:
        context = CONTENTS_WITHHELD
    reqs = "\n".join(
        f"  {req.id}: {req.statement}\n"
        f"    accepts: {', '.join(repr(text) for text in req.accepts)}\n"
        f"    rejects: {', '.join(repr(text) for text in req.rejects)}"
        for req in node.requirements
    )
    rules = EDIT_RULES if emission == "edit" else WHOLE_FILE_RULES
    scope = ""
    if node.target_files:
        # The planner's list reaches the gate; the worker has to hear it
        # too, or it writes the extra test file 20b's node-2 wrote (R3).
        scope = (
            f"- Touch only these files: {', '.join(node.target_files)}. "
            "The gate rejects a diff that names any other file.\n"
        )
    return f"""Task: {task}

Node {node.id}: {node.task_prompt}
Requirements (each test must fail if its statement is violated):
{reqs}
Gate command: {node.deterministic_gate.test_command}

Repo files:
{shown}

File contents:
{context}

Node {node.id}, restated now that you have the files: {node.task_prompt}
Requirements (each test must fail if its statement is violated):
{reqs}
Gate command: {node.deterministic_gate.test_command}

{rules}- Mention each requirement ID in the new or changed test source.
{scope}- A "test" node binds every listed accept and reject, and the gate
  fails one whose tests bind none of them. An example written as a call
  is bound by a test that PERFORMS that operation and asserts on the
  constant values it was handed; an example written as data is bound by
  a test that asserts on the values inside it. Quoting an example as
  text binds nothing, and quote style never matters -- write the call
  the way you would write any other test.
- A "test" node must include at least one hypothesis property, not only
  examples: `@given(...)` over generated inputs. Examples probe the cases
  you already thought of; a property probes the ones you did not. For a
  requirement about a text format, `hypothesis.strategies.from_regex`
  generates witnesses directly. At least one property must reject an
  input (`assert not ...`, `is False`, or `pytest.raises`): a suite whose
  every property is positive cannot tell the code from one that accepts
  everything.
- Make sure the gate command above passes after the diff applies.
- Keep new code ruff-clean: double quotes, 4-space indent,
  two blank lines between top-level definitions, final newline, no unused imports,
  sorted import blocks with stdlib, third-party, and local groups separated by blank lines.
- Every behaviour you add or change needs a test that fails if that
  behaviour changes. A test that only runs a line proves nothing about it.

Output ONLY the file sections, no commentary.
"""


def build_replan_task(*, task: str, node: Node, history: str) -> str:
    """Recovery scope for re-emission: the failed node plus its failure history."""
    reqs = ", ".join(node.requirement_ids)
    return (
        f"Original task: {task}\n\n"
        f"Node {node.id} failed and must be re-planned: {node.task_prompt}\n"
        f"Requirements to cover: {reqs}\n"
        f"Gate command the new nodes must satisfy: {node.deterministic_gate.test_command}\n\n"
        f"Failure history (do not repeat it):\n{history}"
    )


def build_recovery_plan_prompt(
    *,
    task: str,
    node: Node,
    files: Sequence[str],
    contents: Mapping[str, str],
    failure: str,
    emission: str = "whole-file",
) -> str:
    """Diagnosis prompt: root-cause the failure and outline the minimal fix."""
    base = build_worker_prompt(
        task=task, node=node, files=files, contents=contents, emission=emission
    )
    return (
        base + "\nThe previous attempt failed as described below. Diagnose the "
        "root cause against the CURRENT tree state above, then outline the "
        "minimal fix steps. Write a short numbered plan, no diff, no commentary "
        "outside the plan.\n\n" + failure
    )


def build_repair_prompt(
    *,
    task: str,
    node: Node,
    files: Sequence[str],
    contents: Mapping[str, str],
    failure: str,
    plan: str | None,
    emission: str = "whole-file",
) -> str:
    """Repair prompt: the worker brief plus evidence and the recovery plan.

    The tree already holds the failed attempt, so `contents` above is
    what the previous attempt left, and the worker fixes forward against
    that rather than against the node's baseline.

    Under the whole-file envelope "fix forward" is about WHICH TREE to
    write against, not about how much to emit (T6-62/A1). Every write is
    the complete file either way; the instruction that used to read "do
    not restate the whole change" was a diff-envelope economy and is now
    the opposite of what the worker must do.

    `plan` is `None` when the diagnosis step produced an instruction a
    gate would reject (T6-54); the section is then absent rather than
    replaced, so the worker fixes forward on the failure alone and the
    harness does not hand it a plan it cannot legally follow.
    """
    base = build_worker_prompt(
        task=task, node=node, files=files, contents=contents, emission=emission
    )
    recovery = "" if plan is None else "\n\nRecovery plan:\n" + plan
    return (
        base + "\nThe previous attempt failed. Fix forward: write the files above "
        "as they should now be, starting from the CURRENT tree state shown, so "
        "that the failure below is repaired. The contents above already include "
        "the previous attempt's work -- keep what was right and change what was "
        "not." + recovery + "\n\n" + failure
    )


def _git_ok(argv: Sequence[str], repo: Path, message: str) -> None:
    """Run a git setup command, raising RunError with `message` on failure."""
    if run_argv(argv, repo) != 0:
        raise RunError(message)


def _ensure_repo(repo: Path) -> bool:
    """Create and baseline a repo when missing; True when anything was set up."""
    if repo.exists() and not repo.is_dir():
        msg = f"{str(repo)!r} is not a directory"
        raise RunError(msg)
    repo.mkdir(parents=True, exist_ok=True)
    if (repo / ".git").exists():
        # -e exits 0 iff HEAD resolves (silent, so no --quiet needed).
        if run_argv(["git", "cat-file", "-e", "HEAD^{commit}"], repo) == 0:
            return False
    else:
        init = f"could not init a git repo in {str(repo)!r}"
        _git_ok(["git", "init", "--quiet"], repo, init)
    stage = f"could not stage baseline in {str(repo)!r}"
    _git_ok(["git", "add", "-A"], repo, stage)
    commit = f"could not commit baseline in {str(repo)!r}"
    _git_ok(
        [
            "git",
            *SADDLE_COMMIT_IDENTITY,
            "commit",
            "--quiet",
            "--allow-empty",
            "-m",
            "saddle baseline",
        ],
        repo,
        commit,
    )
    return True


def _ensure_clean(repo: Path) -> None:
    """Refuse repos with uncommitted changes against HEAD (tracked tree).

    This is also the first half of the resume flow after a crash (T3-10).
    A run that died left its proven edits staged, so this check refuses
    the repo; the user commits them (`git add -u && git commit`) and runs
    again. `run_slice`'s tree check then passes, because a commit names
    the tracked tree without changing it -- the worktree still hashes to
    the `tree_hash` the last reused proof was sealed on. Reverting those
    edits instead is what the tree check exists to catch.
    """
    if run_argv(["git", "diff-index", "--quiet", "HEAD", "--"], repo) != 0:
        msg = f"{str(repo)!r} has uncommitted changes; commit or stash first"
        raise RunError(msg)


def _emit_valid_dag(
    client: VllmClient,
    task: str,
    *,
    files: Sequence[str],
    file_lines: Mapping[str, int],
    max_tokens: int,
    temperature: float,
    reasoning_effort: str,
    context_window: int = DEFAULT_CONTEXT_WINDOW,
    reserved: Collection[str] = (),
) -> Dag:
    """Emit a DAG, feeding validation errors back (bounded recompile).

    `file_lines` (baseline lines per file) turns on the scope checks: an
    impl/refactor node must declare its files and its estimated diff must
    fit the room the window leaves it (T6-8, `diff_budget`). Both come
    back to the planner as validation errors, like every other issue.

    `reserved` is empty for the first plan and carries the pending nodes'
    files for a replan (T6-65): the replan prompt states the whole task,
    so nothing else stops a subplan re-planning a sibling's work.
    """
    prompt = build_emit_prompt(task, files)
    errors: list[str] = []
    for _ in range(EMIT_ROUNDS):
        attempt = prompt
        if errors:
            attempt += "\nPrevious attempt failed:\n" + "\n".join(errors)
        try:
            emission = client.emit_dag(
                attempt,
                max_tokens=max_tokens,
                temperature=temperature,
                reasoning_effort=reasoning_effort,
            )
        except VllmResponseError as exc:
            errors.append(str(exc))
            continue
        except (VllmAuthError, VllmRequestError) as exc:
            raise RunError(str(exc)) from exc
        try:
            dag = Dag.model_validate(emission.dag)
        except ValidationError as exc:
            errors.append(str(exc))
            continue
        required = {req for node in dag.nodes for req in node.requirement_ids}
        issues = validate_dag(
            dag,
            allowed_tools=RUN_ALLOWLIST,
            context_ceiling=CONTEXT_CEILING,
            required_ids=sorted(required),
            file_lines=file_lines,
            emission_budget=lambda node: diff_budget(node, context_window),
        )
        round_errors = [f"{issue.code}: {issue.message}" for issue in issues]
        # T6-58: a plan that restates the gate is refused and REDRAWN, never
        # rewritten here -- editing the statement would leave the node held
        # to a requirement no model ever wrote, and the worker reads the
        # requirement block as binding.
        restated = plan_restates_the_gate(dag.nodes)
        if restated is not None:
            round_errors.append(
                "plan-restates-gate: a node description or requirement statement restates "
                f"the coverage gate: {restated!r}. State behaviour a test can falsify."
            )
        # T6-65: the same redraw, for a subplan that would do a pending
        # node's work. Naming the files is what tells the planner a
        # sibling exists at all -- the replan prompt does not.
        retargeted = plan_retargets_reserved_files(dag.nodes, reserved)
        if retargeted is not None:
            round_errors.append(
                "plan-retargets-reserved: a replacement node takes a file another pending "
                f"node is already to change: {retargeted}. Scope the subplan to the failed node."
            )
        if not round_errors:
            return dag
        errors.extend(round_errors)
    msg = f"could not emit a valid DAG in {EMIT_ROUNDS} rounds: {'; '.join(errors)}"
    raise RunError(msg)


def served_version(client: VllmClient) -> str:
    """The server's version for the run span (T6-27); `unknown` when it will not say."""
    try:
        return client.server_version() or "unknown"
    except VllmRequestError:
        return "unknown"


def check_server(client: VllmClient, *, base_url: str, model: str) -> list[str]:
    """Preflight: reachability, key, model match. Returns served ids."""
    try:
        ids = client.list_models()
    except VllmError as exc:
        msg = f"preflight failed at {base_url}: {exc}"
        raise RunError(msg) from exc
    if model not in ids:
        served = ", ".join(ids) if ids else "(none)"
        msg = f"preflight failed at {base_url}: model {model!r} is not served (served: {served})"
        raise RunError(msg)
    return ids


def run_task(options: RunOptions, client: VllmClient, *, stdin: IO[str], stdout: IO[str]) -> int:
    """Drive one task: emit, confirm, schedule, gate, seal, transcribe."""
    try:
        if _ensure_repo(options.repo):
            stdout.write(f"created baseline commit in {str(options.repo)!r}\n")
        _ensure_clean(options.repo)
        files = git_ls_files(options.repo)
        dag = _emit_valid_dag(
            client,
            options.task,
            files=files,
            file_lines=_file_lines(options.repo, files),
            max_tokens=options.max_tokens,
            temperature=options.temperature,
            reasoning_effort=options.reasoning_effort,
            context_window=options.context_window,
        )
    except RunError as exc:
        stdout.write(f"error: {exc}\n")
        return 1
    ids = ", ".join(node.id for node in dag.nodes)
    stdout.write(f"Plan: {len(dag.nodes)} node(s): {ids}\n")
    # A killed run (round 3: `timeout` SIGTERM) took its block-buffered log
    # with it; the plan line reaches the file before any node runs.
    stdout.flush()
    if not options.yes and not ask_confirm(
        f"Run {len(dag.nodes)} node(s)?", stdin=stdin, stdout=stdout
    ):
        stdout.write("aborted.\n")
        return 1

    def propose(node: Node, failure: str | None, seed: int = 0) -> DiffProposal:
        budget = node.execution_constraints.reasoning_budget
        effort = options.worker_effort or BUDGET_TO_EFFORT[budget]
        files = git_ls_files(options.repo)
        # Every call gets the window left after its own prompt (T6-17):
        # a longer prompt (a repair brief, a tree a failed attempt bloated)
        # buys less room, never more, and nothing is held back for thinking.
        contents = {
            name: (options.repo / name).read_text() for name in files if name.endswith(".py")
        }
        if failure is None:
            prompt = build_worker_prompt(
                task=options.task,
                node=node,
                files=files,
                contents=contents,
                emission=options.emission,
            )
        else:
            plan_prompt = build_recovery_plan_prompt(
                task=options.task,
                node=node,
                files=files,
                contents=contents,
                failure=failure,
                emission=options.emission,
            )
            plan = client.complete(
                plan_prompt,
                max_tokens=worker_max_tokens(plan_prompt, options.context_window),
                temperature=options.temperature,
                reasoning_effort=effort,
            )
            prescribed = plan_prescribes_deletion(plan, contents)
            prompt = build_repair_prompt(
                task=options.task,
                node=node,
                files=files,
                contents=contents,
                failure=failure,
                plan=None if prescribed is not None else plan,
                emission=options.emission,
            )
        return client.propose_diff(
            prompt,
            max_tokens=worker_max_tokens(prompt, options.context_window),
            temperature=worker_temperature(options, failure),
            reasoning_effort=effort,
            seed=seed,
            grammar=EDIT_GRAMMAR if options.emission == "edit" else DIFF_GRAMMAR,
        )

    def replan(node: Node, history: str, reserved: Sequence[str] = ()) -> Dag:
        try:
            files = git_ls_files(options.repo)
            return _emit_valid_dag(
                client,
                build_replan_task(task=options.task, node=node, history=history),
                files=files,
                file_lines=_file_lines(options.repo, files),
                max_tokens=options.max_tokens,
                temperature=options.temperature,
                reasoning_effort=options.reasoning_effort,
                context_window=options.context_window,
                reserved=reserved,
            )
        except RunError as exc:
            # Message unobserved: the scheduler swallows it with a bare continue.
            raise ReplanFailedError(str(exc)) from exc  # pragma: no mutate

    settings = {
        "model": options.model,
        "server": options.server_version,
        "context_window": str(options.context_window),
        "temperature": str(options.temperature),
        "sample_temperature": str(options.sample_temperature),
        "emission": options.emission,
        "recovery_temperature": str(worker_temperature(options, "retry")),
        "reasoning_effort": options.reasoning_effort,
        "worker_effort": options.worker_effort or "node budget",
        "survivor_effort": options.survivor_effort,
        "survivor_samples": str(options.survivor_samples),
        # What the ruff gate ran with (T6-37, T6-34): the verdict is a
        # function of both, and round 3d's trees could not say which ruff
        # autofixed them.
        "ruff": ruff_version(),
        "ruff_rules": ",".join(RUFF_RULES),
    }
    try:
        result = run_slice(
            options.task,
            dag,
            workdir=options.repo,
            journal_path=options.journal,
            propose=propose,
            replan=replan,
            deadline_s=options.deadline_s,
            settings=settings,
            survivor_draw=survivor_drawer(client, options),
            survivor_samples=options.survivor_samples,
        )
    except (ValueError, RuntimeError) as exc:
        stdout.write(f"error: {exc}\n")
        return 1
    stdout.write(result.transcript)
    if result.deadline_hit:
        return DEADLINE_EXIT
    return 0 if result.passed else 1


def run_doctor(base_url: str, model: str, client: VllmClient, *, stdout: IO[str]) -> int:
    """Report the server preflight verdict; 0 when the server is usable."""
    try:
        ids = check_server(client, base_url=base_url, model=model)
    except RunError as exc:
        stdout.write(f"error: {exc}\n")
        return 1
    stdout.write(f"OK: {base_url} serves {model} (models: {', '.join(ids)})\n")
    return 0


def render_dag_plan(task: str, dag: Dag) -> str:
    """Render the emitted plan as an indented node list with gates."""
    ids = ", ".join(node.id for node in dag.nodes)
    lines = [f"Task: {task}", f"Plan: {len(dag.nodes)} node(s): {ids}"]
    last = len(dag.nodes) - 1
    for index, node in enumerate(dag.nodes):
        branch = "└──" if index == last else "├──"
        pad = "    " if index == last else "│   "
        constraints = node.execution_constraints
        gate = node.deterministic_gate
        sample = gate.mutation_sample
        depends = ", ".join(node.dependencies) if node.dependencies else "(none)"
        lines.append(
            f"{branch} {node.id} [budget: {constraints.reasoning_budget}, "
            f"context: {constraints.max_context_tokens} tokens]"
        )
        lines.append(f"{pad}task: {node.task_prompt}")
        lines.append(f"{pad}requirements: {', '.join(node.requirement_ids)}")
        lines.append(f"{pad}depends on: {depends}")
        lines.append(f"{pad}tools: {', '.join(constraints.allowed_tools)}")
        if node.target_files:
            lines.append(f"{pad}targets: {', '.join(node.target_files)}")
        lines.append(
            f"{pad}gate: {gate.test_command} (coverage >= {gate.changed_line_coverage_min}%, "
            f"red-phase required, mutation {sample.max_mutants} @ "
            f"{sample.kill_threshold}% {sample.scope})"
        )
    return "\n".join(lines) + "\n"


def _listable_files(repo: Path | None) -> list[str]:
    """The repo's tracked files for `saddle dag`; none outside a repository.

    `dag` is a preview and may run anywhere, so a cwd that is not a git
    repository lists nothing rather than failing (T3-19).
    """
    if repo is None:
        return []
    try:
        return git_ls_files(repo)
    except RuntimeError:
        return []


def run_dag(options: DagOptions, client: VllmClient, *, stdout: IO[str]) -> int:
    """Emit the plan and print it; execute nothing."""
    try:
        check_server(client, base_url=options.base_url, model=options.model)
        files = _listable_files(options.repo)
        dag = _emit_valid_dag(
            client,
            options.task,
            files=files,
            file_lines=_file_lines(options.repo, files) if options.repo is not None else {},
            max_tokens=options.max_tokens,
            temperature=options.temperature,
            reasoning_effort=options.reasoning_effort,
            context_window=options.context_window,
        )
    except RunError as exc:
        stdout.write(f"error: {exc}\n")
        return 1
    stdout.write(render_dag_plan(options.task, dag))
    return 0


def run_verify(journal: Path, *, stdout: IO[str]) -> int:
    """Audit one journal: chain plus orphan rule, then its transcript."""
    issues = verify_journal(journal)
    if issues:
        for issue in issues:
            stdout.write(f"{issue.code}@line {issue.line}: {issue.message}\n")
        return 1
    records = read_records(journal)
    spans = read_spans(journal)
    plans = read_plans(journal)
    stdout.write(
        f"OK: {journal}: {len(records)} proof(s), {len(spans)} span(s), "
        f"{len(plans)} plan(s), chain verifies\n"
    )
    for plan in plans:
        stdout.write("".join(f"{line}\n" for line in render_plan(plan)))
    stdout.write("\n")
    stdout.write(render_journal_transcript(records, spans, str(journal)))
    return 0


EXPLAIN_REDACTED: Final = ("prompt", "diff", "thinking")


def _explain_attempt(journal: Path, span: SpanRecord) -> list[str]:
    """One worker span's redacted line: times, call knobs, verdict."""
    when = span.started_at or "?"
    knobs = " ".join(span.argv[2:])
    line = f"  {when}  {span.duration_ms / 1000:8.1f}s  exit {span.exit_code}  {knobs}"
    lines = [line, f"    {span.detail}"]
    path = attempt_sidecar_path(journal, span.span_id)
    if not span.attempt_hash or not path.exists():
        return lines
    evidence = json.loads(path.read_text())
    call = {k: evidence.get(k) for k in ("seed", "temperature", "wall_s", "finish_reason")}
    usage = evidence.get("usage") or {}
    call["completion_tokens"] = usage.get("completion_tokens")
    call["cached_tokens"] = usage.get("cached_tokens")
    shown = ", ".join(f"{k}={v}" for k, v in call.items() if v is not None)
    samples = evidence.get("samples") or []
    outcomes = ", ".join(str(s.get("outcome", "?")) for s in samples if isinstance(s, dict))
    lines.append(f"    sidecar {span.span_id[:12]}: {shown or 'no call details'}")
    if outcomes:
        lines.append(f"    samples: {outcomes}")
    return lines


def run_explain(journal: Path, *, attempt: str | None, stdout: IO[str]) -> int:
    """Explain a run from its journal (T6-27): times, calls, verdicts, findings.

    The default tier is redacted -- identifiers, start times, durations,
    seeds, temperatures, token counts, gate verdicts, verify findings --
    and never prints a prompt, a diff or the model's reasoning. `attempt`
    names one worker span (a prefix of its id) and prints that attempt's
    raw sidecar in full, which does contain them.
    """
    issues: list[JournalIssue] = verify_journal(journal)
    try:
        spans = read_spans(journal)
    except ValueError as exc:
        stdout.write(f"error: {exc}\n")
        return 1
    if attempt is not None:
        matches = [s for s in spans if s.kind == "agent" and s.span_id.startswith(attempt)]
        if len(matches) != 1:
            stdout.write(f"error: {len(matches)} attempt(s) match {attempt!r}\n")
            return 1
        path = attempt_sidecar_path(journal, matches[0].span_id)
        if not path.exists():
            stdout.write(f"error: no sidecar for {matches[0].span_id}\n")
            return 1
        stdout.write(json.dumps(json.loads(path.read_text()), indent=2, sort_keys=True) + "\n")
        return 0
    stdout.write(f"journal: {journal}\n")
    stdout.write(
        "verify: "
        + (", ".join(f"{i.code}@line {i.line}" for i in issues) if issues else "clean")
        + "\n"
    )
    for plan in read_plans(journal):
        stdout.write(f"plan{' replacing ' + plan.replaces if plan.replaces else ''}:\n")
        for node in plan.nodes:
            tools = ", ".join(node.allowed_tools) if node.allowed_tools else "not recorded"
            stdout.write(f"  {node.id} {node.kind} budget={node.reasoning_budget} tools: {tools}\n")
    runs = [s for s in spans if s.name == "run"]
    for run in runs:
        stdout.write(
            f"run: {run.started_at or '?'} {run.duration_ms / 1000:.1f}s exit {run.exit_code}\n"
        )
        for part in run.argv[1:]:
            stdout.write(f"  {part}\n")
        stdout.write(f"  {run.detail}\n")
    workers = [s for s in spans if s.kind == "agent" and s.name.startswith("worker:")]
    tools_by_node: dict[str, int] = {}
    for span in spans:
        if span.kind == "tool":
            tools_by_node[span.node_id] = tools_by_node.get(span.node_id, 0) + 1
    for span in workers:
        stdout.write(f"{span.name} ({tools_by_node.get(span.node_id, 0)} tool span(s) on node)\n")
        stdout.write("".join(f"{line}\n" for line in _explain_attempt(journal, span)))
    return 0


def run_tail(
    journal: Path,
    *,
    stdout: IO[str],
    sleep: Callable[[float], None] = time.sleep,
    poll_interval: float = 0.2,
) -> int:
    """Follow a journal, rendering each entry as it lands; exit on run end."""
    if not journal.exists():
        stdout.write(f"waiting for {journal} to appear...\n")
    shown = 0
    try:
        while True:
            try:
                entries = read_entries(journal)
            except ValueError as exc:
                stdout.write(f"error: {exc}\n")
                return 1
            if len(entries) < shown:
                stdout.write(f"error: {journal} was truncated; restart tail\n")
                return 1
            for entry in entries[shown:]:
                for line in render_event(entry):
                    stdout.write(f"{line}\n")
                stdout.flush()
                if is_run_end(entry) and entry is entries[-1]:
                    return 0
            shown = len(entries)
            sleep(poll_interval)
    except KeyboardInterrupt:
        return 130


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="saddle", description="Deterministic harness for local LLMs."
    )
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    sub = parser.add_subparsers(dest="command")
    doctor = sub.add_parser("doctor", help="Check the server is usable.")
    doctor.add_argument("--base-url", default=DEFAULT_BASE_URL, help="vLLM base URL.")
    doctor.add_argument("--model", default=DEFAULT_MODEL, help="Model id.")
    dag = sub.add_parser("dag", help="Show the plan before it runs.")
    dag.add_argument("task", help="Task description to decompose into a plan.")
    dag.add_argument("--repo", default=".", help="Repository whose files the planner is shown.")
    dag.add_argument("--base-url", default=DEFAULT_BASE_URL, help="vLLM base URL.")
    dag.add_argument("--model", default=DEFAULT_MODEL, help="Model id.")
    dag.add_argument("--max-tokens", type=int, default=8192, help="Emission max tokens.")
    dag.add_argument(
        "--context-window",
        type=int,
        help="Model context length in tokens (default: what the server reports, else 175000).",
    )
    dag.add_argument("--temperature", type=float, default=0.0, help="Sampling temperature.")
    dag.add_argument(
        "--reasoning-effort",
        choices=list(REASONING_EFFORTS),
        default="medium",
        help="Emission reasoning effort.",
    )
    tail = sub.add_parser("tail", help="Follow a live run as it happens.")
    tail.add_argument(
        "journal",
        nargs="?",
        default=".saddle/proofs.jsonl",
        help="Journal path (default: .saddle/proofs.jsonl).",
    )
    verify = sub.add_parser("verify", help="Audit a journal and re-render its transcript.")
    verify.add_argument(
        "journal",
        nargs="?",
        default=".saddle/proofs.jsonl",
        help="Journal path (default: .saddle/proofs.jsonl).",
    )
    explain = sub.add_parser("explain", help="Explain a run from its journal (T6-27).")
    explain.add_argument(
        "journal",
        nargs="?",
        default=".saddle/proofs.jsonl",
        help="Journal path (default: .saddle/proofs.jsonl).",
    )
    explain.add_argument(
        "--attempt",
        help="Print one attempt's raw sidecar (prefix of its span id); includes prompt and diff.",
    )
    run = sub.add_parser("run", help="Drive one mechanical task end to end.")
    run.add_argument("task", help="Task description to decompose and execute.")
    run.add_argument("--repo", default=".", help="Directory to work in (repo created if missing).")
    run.add_argument("--journal", help="Journal path (default: REPO/.saddle/proofs.jsonl).")
    run.add_argument("--base-url", default=DEFAULT_BASE_URL, help="vLLM base URL.")
    run.add_argument("--model", default=DEFAULT_MODEL, help="Model id.")
    run.add_argument("--max-tokens", type=int, default=8192, help="Emission max tokens.")
    run.add_argument(
        "--context-window",
        type=int,
        help="Model context length in tokens (default: what the server reports, else 175000).",
    )
    run.add_argument("--temperature", type=float, default=0.0, help="Sampling temperature.")
    run.add_argument(
        "--sample-temperature",
        type=float,
        default=0.7,
        help="Temperature for worker diff samples, first attempt and retries alike.",
    )
    run.add_argument(
        "--recovery-temperature",
        type=float,
        help="Temperature for retry diff samples (default: --sample-temperature).",
    )
    run.add_argument(
        "--emission",
        choices=("whole-file", "edit"),
        default="whole-file",
        help=(
            "What the worker emits: 'whole-file' restates every file it touches; "
            "'edit' names the sites it changes, so a change costs its own size."
        ),
    )
    run.add_argument(
        "--reasoning-effort",
        choices=list(REASONING_EFFORTS),
        default="medium",
        help="Emission reasoning effort.",
    )
    run.add_argument(
        "--worker-effort",
        choices=list(REASONING_EFFORTS),
        help="Worker effort override (default: per-node budget).",
    )
    run.add_argument(
        "--deadline",
        type=float,
        help="Seconds after which no node or attempt starts; the run seals what it has (exit 3).",
    )
    run.add_argument(
        "--survivor-effort",
        choices=list(REASONING_EFFORTS),
        default="low",
        help="Effort for survivor-round test draws.",
    )
    run.add_argument(
        "--survivor-samples",
        type=int,
        default=SURVIVOR_SAMPLES,
        help="Candidate test draws per survivor round.",
    )
    run.add_argument("--yes", action="store_true", help="Skip the plan confirmation.")
    web = sub.add_parser("web", help="Open the chat UI in a browser.")
    web.add_argument("--workdir", default=".", help="Default folder for new sessions.")
    web.add_argument("--host", default="127.0.0.1", help="Bind address (default: loopback).")
    web.add_argument("--port", type=int, default=8777)
    web.add_argument(
        "--sessions",
        default=None,
        help="Session store (default: ~/.saddle/sessions).",
    )
    web.add_argument("--base-url", default=DEFAULT_BASE_URL, help="vLLM base URL.")
    web.add_argument("--model", default=DEFAULT_MODEL, help="Model id.")
    web.add_argument("--no-open", action="store_true", help="Do not open a browser.")
    up = sub.add_parser("up", help="Open an interactive streaming chat session.")
    up.add_argument("--workdir", default=".", help="Directory tools run in (default: .).")
    up.add_argument(
        "--journal",
        default=".saddle/chat.jsonl",
        help="Journal path (default: .saddle/chat.jsonl).",
    )
    up.add_argument("--base-url", default=DEFAULT_BASE_URL, help="vLLM base URL.")
    up.add_argument("--model", default=DEFAULT_MODEL, help="Model id.")
    up.add_argument("--max-tokens", type=int, default=8192, help="Reply max tokens.")
    up.add_argument("--temperature", type=float, default=0.0, help="Sampling temperature.")
    up.add_argument(
        "--reasoning-effort",
        choices=list(REASONING_EFFORTS),
        default="medium",
        help="Reply reasoning effort.",
    )
    return parser


def _api_key() -> str | None:
    return os.environ.get("SADDLE_VLLM_API_KEY") or os.environ.get("VLLM_API_KEY")


def main(
    argv: list[str] | None = None,
    *,
    stdin: IO[str] | None = None,
    stdout: IO[str] | None = None,
    stderr: IO[str] | None = None,
) -> int:
    args = build_parser().parse_args(argv)
    if args.command not in ("run", "doctor", "dag", "verify", "tail", "up", "explain", "web"):
        return 0
    if args.command == "verify":
        return run_verify(Path(args.journal), stdout=stdout or sys.stdout)
    if args.command == "explain":
        return run_explain(Path(args.journal), attempt=args.attempt, stdout=stdout or sys.stdout)
    if args.command == "tail":
        return run_tail(Path(args.journal), stdout=stdout or sys.stdout)
    key = _api_key()
    if not key:
        print("error: set SADDLE_VLLM_API_KEY (or VLLM_API_KEY)", file=stderr or sys.stderr)
        return 1
    if args.command == "doctor":
        with VllmClient(api_key=key, base_url=args.base_url, model=args.model) as client:
            return run_doctor(args.base_url, args.model, client, stdout=stdout or sys.stdout)
    if args.command == "dag":
        with VllmClient(api_key=key, base_url=args.base_url, model=args.model) as client:
            dag_options = DagOptions(
                task=args.task,
                repo=Path(args.repo),
                base_url=args.base_url,
                model=args.model,
                max_tokens=args.max_tokens,
                temperature=args.temperature,
                reasoning_effort=args.reasoning_effort,
                context_window=server_context_window(client, args.context_window),
            )
            return run_dag(dag_options, client, stdout=stdout or sys.stdout)
    if args.command == "web":
        from saddle.web.app import serve

        url = f"http://{args.host}:{args.port}/"
        print(f"saddle chat UI on {url}", file=stdout or sys.stdout)
        if not args.no_open:
            import webbrowser

            webbrowser.open(url)
        serve(
            host=args.host,
            port=args.port,
            api_key=key,
            base_url=args.base_url,
            model=args.model,
            workdir=Path(args.workdir).resolve(),
            sessions_root=Path(args.sessions) if args.sessions else None,
        )
        return 0
    if args.command == "up":
        with VllmClient(api_key=key, base_url=args.base_url, model=args.model) as client:
            try:
                check_server(client, base_url=args.base_url, model=args.model)
            except RunError as exc:
                print(f"error: {exc}", file=stderr or sys.stderr)
                return 1
            chat_options = ChatOptions(
                workdir=Path(args.workdir),
                journal=Path(args.journal),
                max_tokens=args.max_tokens,
                temperature=args.temperature,
                reasoning_effort=args.reasoning_effort,
            )
            return run_chat(
                chat_options,
                client,
                stdin=stdin or sys.stdin,
                console=Console(file=stdout or sys.stdout),
            )
    repo = Path(args.repo).resolve()
    journal = Path(args.journal) if args.journal else repo / ".saddle" / "proofs.jsonl"
    with VllmClient(api_key=key, base_url=args.base_url, model=args.model) as client:
        try:
            check_server(client, base_url=args.base_url, model=args.model)
        except RunError as exc:
            print(f"error: {exc}", file=stderr or sys.stderr)
            return 1
        options = RunOptions(
            task=args.task,
            repo=repo,
            journal=journal,
            max_tokens=args.max_tokens,
            temperature=args.temperature,
            sample_temperature=args.sample_temperature,
            emission=args.emission,
            reasoning_effort=args.reasoning_effort,
            worker_effort=args.worker_effort,
            yes=args.yes,
            context_window=server_context_window(client, args.context_window),
            recovery_temperature=args.recovery_temperature,
            deadline_s=args.deadline,
            model=args.model,
            server_version=served_version(client),
            survivor_effort=args.survivor_effort,
            survivor_samples=args.survivor_samples,
        )
        return run_task(
            options,
            client,
            stdin=stdin or sys.stdin,
            stdout=stdout or sys.stdout,
        )
