"""Saddle command line interface.

`run` drives one mechanical task end to end: guided DAG emission with
bounded recompile, plan confirmation, then the schedule-gate-seal path.
"""

from __future__ import annotations

import argparse
import os
import sys
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import IO, Final

from pydantic import ValidationError
from rich.console import Console

from saddle import __version__
from saddle.chat import ChatOptions, run_chat
from saddle.dag import Dag, Node, emission_estimate, validate_dag
from saddle.evidence import git_ls_files, run_argv
from saddle.journal import read_entries, read_records, read_spans, verify_journal
from saddle.slice import ReplanFailedError, run_slice
from saddle.transcript import is_run_end, render_event, render_journal_transcript
from saddle.ux import ask_confirm
from saddle.vllm import (
    DEFAULT_BASE_URL,
    DEFAULT_MODEL,
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
# is a separate budget: spending it as an output cap truncated real work
# mid-diff. Thinking counts against the same output cap on this server, so
# this table is the *reasoning allowance* per effort: what the think block
# may spend before the diff starts. It is also the whole cap for a node that
# declares no `target_files` (there is nothing to size the diff from). A
# scoped node's cap is its emission estimate (dag.py `emission_estimate`,
# from the repo) plus this allowance, so buying room to write never buys
# room to think (T6-14; round-3 T5 truncated three seeds at the medium
# entry with a ~4000-token diff outstanding).
WORKER_OUTPUT_TOKENS: Final[dict[str, int]] = {
    "none": 8192,
    "low": 16384,
    "medium": 32768,
    "xhigh": 65536,
}
# After a `finish_reason=length` failure the next attempt for that node runs
# one step up this ladder (the first step strictly above its current cap);
# the top is the most the harness ever asks for in one response. Before
# T6-14 the retry resent the identical cap, after a longer prompt.
WORKER_OUTPUT_STEPS: Final[tuple[int, ...]] = (8192, 16384, 32768, 65536, 131072)
MAX_WORKER_OUTPUT: Final = WORKER_OUTPUT_STEPS[-1]
TRUNCATED_MARK: Final = "finish_reason=length"


def worker_output_cap(
    node: Node, effort: str, file_lines: Mapping[str, int], escalations: int
) -> int:
    """The `max_tokens` a worker call for `node` sends (T6-14).

    Emission estimate plus reasoning allowance when the node declares
    scope; the allowance alone otherwise. Each escalation (one per
    truncation already suffered by this node) at least doubles the cap
    and lands no lower than the next ladder rung: round 3b (F21.9b)
    showed a cap of 32624 "escalating" to the 32768 rung, 144 tokens of
    room, so a step is measured from the cap, not from the rung table.
    `file_lines` must be the node's *baseline* tree: a failed attempt's
    diff is still applied when the next attempt is proposed, and sizing
    from it let a degenerate 699-line failure buy itself a bigger cap
    (F21.9a).
    """
    estimate = emission_estimate(node, file_lines)
    cap = WORKER_OUTPUT_TOKENS[effort] + (estimate or 0)
    for _ in range(escalations):
        rung = next((step for step in WORKER_OUTPUT_STEPS if step > cap), MAX_WORKER_OUTPUT)
        cap = max(rung, 2 * cap)
    return min(cap, MAX_WORKER_OUTPUT)


def emission_budget(node: Node) -> int:
    """Most a node's diff may be estimated at and still be planned (T6-8).

    The top of the ladder minus the node's own reasoning allowance: a diff
    that cannot fit beside its thinking at the largest cap the harness
    sends is a node no retry can rescue, so it is rejected before any
    worker call and the planner splits it.
    """
    effort = BUDGET_TO_EFFORT[node.execution_constraints.reasoning_budget]
    return MAX_WORKER_OUTPUT - WORKER_OUTPUT_TOKENS[effort]


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
- Requirement IDs are REQ- followed by exactly three digits.
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
- changed_line_coverage_min is always 100.0: every line you change must
  be executed by a test.
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


def build_worker_prompt(
    *, task: str, node: Node, files: Sequence[str], contents: Mapping[str, str]
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
    reqs = "\n".join(f"  {req.id}: {req.statement}" for req in node.requirements)
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

Produce a unified diff (git apply compatible) implementing exactly that.
Rules:
- Start each file section with a "diff --git a/<file> b/<file>" header line.
- Every file section needs at least one "@@ ... @@" hunk with the actual
  change; a header with no hunk applies nothing.
- The apply step passes --recount, so hunk header line numbers/counts
  (the "-a,b +c,d" part) do not need to be exact -- do not spend effort
  computing them.
- Mark new files with "new file mode 100644".
- Mention each requirement ID in the new or changed test source.
{scope}- A "test" node must include at least one hypothesis property, not only
  examples: `@given(...)` over generated inputs. Examples probe the cases
  you already thought of; a property probes the ones you did not. For a
  requirement about a text format, `hypothesis.strategies.from_regex`
  generates witnesses directly.
- Make sure the gate command above passes after the diff applies.
- Keep new code ruff-clean: double quotes, 4-space indent,
  two blank lines between top-level definitions, final newline, no unused imports,
  sorted import blocks with stdlib, third-party, and local groups separated by blank lines.
- Every changed line must be executed by the new tests.

Output ONLY the diff, no commentary.
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
) -> str:
    """Diagnosis prompt: root-cause the failure and outline the minimal fix."""
    base = build_worker_prompt(task=task, node=node, files=files, contents=contents)
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
    plan: str,
) -> str:
    """Repair prompt: the worker brief plus evidence and the recovery plan.

    The tree already holds the failed attempt, so the worker fixes
    forward against the current contents instead of restating the change.
    """
    base = build_worker_prompt(task=task, node=node, files=files, contents=contents)
    return (
        base + "\nThe previous attempt failed. Fix forward: propose a diff against "
        "the CURRENT tree state above that repairs the failure below. "
        "Do not restate the whole change.\n\nRecovery plan:\n" + plan + "\n\n" + failure
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
            "-c",
            "user.name=saddle",
            "-c",
            "user.email=saddle@local",
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
) -> Dag:
    """Emit a DAG, feeding validation errors back (bounded recompile).

    `file_lines` (baseline lines per file) turns on the scope checks: an
    impl/refactor node must declare its files and its estimated diff must
    fit its emission budget (T6-8). Both come back to the planner as
    validation errors, like every other issue.
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
            emission_budget=emission_budget,
        )
        if not issues:
            return dag
        errors.extend(f"{issue.code}: {issue.message}" for issue in issues)
    msg = f"could not emit a valid DAG in {EMIT_ROUNDS} rounds: {'; '.join(errors)}"
    raise RunError(msg)


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
        )
    except RunError as exc:
        stdout.write(f"error: {exc}\n")
        return 1
    ids = ", ".join(node.id for node in dag.nodes)
    stdout.write(f"Plan: {len(dag.nodes)} node(s): {ids}\n")
    if not options.yes and not ask_confirm(
        f"Run {len(dag.nodes)} node(s)?", stdin=stdin, stdout=stdout
    ):
        stdout.write("aborted.\n")
        return 1

    escalations: dict[str, int] = {}
    baseline_lines: dict[str, dict[str, int]] = {}

    def propose(node: Node, failure: str | None) -> DiffProposal:
        budget = node.execution_constraints.reasoning_budget
        effort = options.worker_effort or BUDGET_TO_EFFORT[budget]
        files = git_ls_files(options.repo)
        # The node's first proposal sees its baseline tree (nodes run
        # synchronously and a node's earlier attempts are the only edits
        # since); size every attempt from that snapshot, never from the
        # tree a failed attempt left behind (F21.9a).
        if node.id not in baseline_lines:
            baseline_lines[node.id] = _file_lines(options.repo, files)
        # `failure` is the immediately preceding attempt's verdict; a
        # truncation in it is one more step up the ladder for this node,
        # and the count persists so a later apply failure does not reset it.
        if failure is not None and TRUNCATED_MARK in failure:
            escalations[node.id] = escalations.get(node.id, 0) + 1
        output_tokens = worker_output_cap(
            node, effort, baseline_lines[node.id], escalations.get(node.id, 0)
        )
        contents = {
            name: (options.repo / name).read_text() for name in files if name.endswith(".py")
        }
        if failure is None:
            prompt = build_worker_prompt(
                task=options.task, node=node, files=files, contents=contents
            )
        else:
            plan = client.complete(
                build_recovery_plan_prompt(
                    task=options.task,
                    node=node,
                    files=files,
                    contents=contents,
                    failure=failure,
                ),
                max_tokens=output_tokens,
                temperature=options.temperature,
                reasoning_effort=effort,
            )
            prompt = build_repair_prompt(
                task=options.task,
                node=node,
                files=files,
                contents=contents,
                failure=failure,
                plan=plan,
            )
        return client.propose_diff(
            prompt,
            max_tokens=output_tokens,
            temperature=options.sample_temperature if failure is None else options.temperature,
            reasoning_effort=effort,
        )

    def replan(node: Node, history: str) -> Dag:
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
            )
        except RunError as exc:
            # Message unobserved: the scheduler swallows it with a bare continue.
            raise ReplanFailedError(str(exc)) from exc  # pragma: no mutate

    try:
        result = run_slice(
            options.task,
            dag,
            workdir=options.repo,
            journal_path=options.journal,
            propose=propose,
            replan=replan,
        )
    except (ValueError, RuntimeError) as exc:
        stdout.write(f"error: {exc}\n")
        return 1
    stdout.write(result.transcript)
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
    stdout.write(
        f"OK: {journal}: {len(records)} proof(s), {len(spans)} span(s), chain verifies\n\n"
    )
    stdout.write(render_journal_transcript(records, spans, str(journal)))
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
    run = sub.add_parser("run", help="Drive one mechanical task end to end.")
    run.add_argument("task", help="Task description to decompose and execute.")
    run.add_argument("--repo", default=".", help="Directory to work in (repo created if missing).")
    run.add_argument("--journal", help="Journal path (default: REPO/.saddle/proofs.jsonl).")
    run.add_argument("--base-url", default=DEFAULT_BASE_URL, help="vLLM base URL.")
    run.add_argument("--model", default=DEFAULT_MODEL, help="Model id.")
    run.add_argument("--max-tokens", type=int, default=8192, help="Emission max tokens.")
    run.add_argument("--temperature", type=float, default=0.0, help="Sampling temperature.")
    run.add_argument(
        "--sample-temperature",
        type=float,
        default=0.7,
        help="Temperature for first-attempt worker samples (recovery uses --temperature).",
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
    run.add_argument("--yes", action="store_true", help="Skip the plan confirmation.")
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
    if args.command not in ("run", "doctor", "dag", "verify", "tail", "up"):
        return 0
    if args.command == "verify":
        return run_verify(Path(args.journal), stdout=stdout or sys.stdout)
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
            )
            return run_dag(dag_options, client, stdout=stdout or sys.stdout)
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
    options = RunOptions(
        task=args.task,
        repo=repo,
        journal=journal,
        max_tokens=args.max_tokens,
        temperature=args.temperature,
        sample_temperature=args.sample_temperature,
        reasoning_effort=args.reasoning_effort,
        worker_effort=args.worker_effort,
        yes=args.yes,
    )
    with VllmClient(api_key=key, base_url=args.base_url, model=args.model) as client:
        try:
            check_server(client, base_url=args.base_url, model=args.model)
        except RunError as exc:
            print(f"error: {exc}", file=stderr or sys.stderr)
            return 1
        return run_task(
            options,
            client,
            stdin=stdin or sys.stdin,
            stdout=stdout or sys.stdout,
        )
