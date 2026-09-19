"""Vertical-slice driver (ARCHITECTURE.md §6 step 1).

Runs one validated DAG through scheduler, Tier-1 gates, and journal, then
renders the transcript from the sealed records. Emission and validation
stay caller-side: this module is the deterministic schedule→gate→seal→
transcribe path. Nodes run one at a time, not concurrently (see F7); wider
graphs ride the same path.
"""

from __future__ import annotations

import asyncio
import shlex
import shutil
import tempfile
import uuid
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from time import perf_counter
from typing import Final

from saddle.dag import Dag, ExecutionConstraints, Node
from saddle.evidence import CapturedRun, git_changed_files, run_argv, run_shell, run_stdin
from saddle.gates import SHELL_TIMEOUT, GateCheck, Tier1Result
from saddle.journal import (
    ProofRecord,
    SpanRecord,
    SpanRecorder,
    append_record,
    append_span,
    build_from_gate,
    build_span,
    read_records,
    read_spans,
    rebuild_proven,
    tool_spans_by_node,
    tool_spans_for_node,
    verify_journal,
)
from saddle.runner import run_node_gate
from saddle.scheduler import Proof, schedule
from saddle.transcript import NodeTranscript, RunTranscript, render_transcript
from saddle.vllm import DiffProposal, VllmResponseError


class NodeGateFailedError(Exception):
    """A node worker failed its Tier-1 gate; carries the verdict."""

    def __init__(self, result: Tier1Result, attempts: int = 1, failure: str | None = None) -> None:
        super().__init__(f"node {result.node_id!r} failed its Tier-1 gate")
        self.result = result
        self.attempts = attempts
        self.failure = failure


class NodeUnappliableError(RuntimeError):
    """No applicable diff emerged; carries attempts and last evidence."""

    def __init__(
        self, node_id: str, detail: str, attempts: int = 1, failure: str | None = None
    ) -> None:
        super().__init__(f"node {node_id!r}: {detail}")
        self.node_id = node_id
        self.attempts = attempts
        self.failure = failure


class ReplanFailedError(Exception):
    """A replan attempt produced nothing schedulable; the node stays failed."""


Proposer = Callable[[Node, str | None], DiffProposal]
"""Propose a diff for a node; `failure` carries prior-attempt evidence."""

Replanner = Callable[[Node, str], Dag]
"""Re-emit a failed node's scope; history carries the failure evidence."""

MAX_RECOVERY_RETRIES: Final = 2
# Independent proposals drawn for the first attempt. Sequential retry
# conditions each sample on the last rejection, which optimises against
# whichever gate pushes back hardest: on T7 recovery drove the node from
# four failing gates to one and then spent its budget on ruff while an
# infinite loop sat untouched (F13). SpecBench measures the same effect
# -- "longer search increases the severity of reward hacking". Drawing
# unconditioned samples and keeping the best is the parallel half of the
# compute-optimal split; sequential recovery still follows.
PROPOSAL_SAMPLES: Final = 3
RECOVERY_OUTPUT_CHARS: Final = 4000


class _HaltRecoveryError(Exception):
    """Identical re-proposal: the attempt span is sealed, exit the loop."""

    def __init__(self, result: Tier1Result | None, attempts: int, failure: str | None) -> None:
        msg = "worker re-proposed an identical diff"
        super().__init__(msg)
        self.result = result
        self.attempts = attempts
        self.failure = failure


@dataclass(frozen=True)
class SliceResult:
    """Slice outcome: verdict, readable transcript, proven node hashes."""

    passed: bool
    transcript: str
    proofs: dict[str, str]


def _utcnow() -> str:
    """Current UTC time as an ISO string for transcripts."""
    return datetime.now(UTC).isoformat()


def _elapsed_ms(start: float) -> int:
    """Whole milliseconds elapsed since a `perf_counter` reading."""
    return int((perf_counter() - start) * 1000)


# Progressively tolerant `git apply` modes, strictest first. Each is
# deterministic and costs no model call, which is the point: T1 spent two
# of three worker calls on diffs that would not apply and T7 lost a whole
# run to three consecutive failures one lint fix from passing (F4, F11,
# F13). The dominant cause is context reproduced from memory with drifted
# whitespace, not a wrong edit. Order matters -- a strict apply is tried
# first so a fuzzy mode never pre-empts an exact match.
_APPLY_MODES: Final = (
    ("strict", ()),
    ("ignore-whitespace", ("--ignore-whitespace",)),
    ("reduced-context", ("--ignore-whitespace", "-C1")),
    ("three-way", ("--3way",)),
)


def _apply_diff(workdir: Path, diff: str, *, recorder: SpanRecorder | None = None) -> str:
    """Apply a proposed diff from stdin and stage it; gates diff tracked content.

    Returns the mode that applied. Every attempt journals its own span, so
    a diff that needed loosening says so in the record rather than passing
    as though it had matched exactly.
    """
    # Primary defence, not a backstop. DIFF_SCHEMA carried a
    # `^diff --git ` pattern until the v3 T1 arm proved the token mask
    # cannot be trusted with this constraint: the decoder compiles
    # `pattern` as a full match, so it admitted the header and nothing
    # else. Every regex that is correct under those semantics let the
    # model emit a raw quote and break its own JSON packet; every regex
    # that kept the packet intact excluded code containing a quote. So
    # structure is checked here, where a violation is deterministic,
    # inspectable and retryable -- prose is exactly the case a fresh
    # attempt can fix, and a fatal parse error would throw the run away.
    if not diff.lstrip().startswith("diff --git "):
        msg = f"worker content is not a unified diff (no 'diff --git' header) in {str(workdir)!r}"
        raise RuntimeError(msg)
    # A header with no hunk applies nothing, and git reports it as "No
    # valid patches in input" -- four times, once per _APPLY_MODES entry,
    # none of them informative. Catching it here costs no subprocess and
    # gives the retry loop a message that actually names the defect.
    if "@@ " not in diff:
        msg = f"worker diff has a header but no hunk ('@@ ' marker) in {str(workdir)!r}"
        raise RuntimeError(msg)
    for mode, flags in _APPLY_MODES:
        exit_code = run_stdin(
            ["git", "apply", "--index", "--recount", *flags, "-"],
            workdir,
            diff,
            recorder=recorder,
        )
        if exit_code == 0:
            return mode
    msg = f"worker diff did not apply cleanly in {str(workdir)!r}"
    raise RuntimeError(msg)


def autofix(workdir: Path, *, baseline: str = "HEAD", recorder: SpanRecorder | None = None) -> None:
    """Apply ruff's mechanical fixes before any gate measures the tree.

    Repair attempts are scarce (MAX_RECOVERY_RETRIES) and iterative
    refinement follows the cheapest feedback signal, not the most
    important defect: lint emits precise localised errors while a failing
    property emits a counterexample that needs diagnosis. T7 spent its
    budget on ruff with an infinite loop untouched (F13), and the v3 T1
    re-run reproduced it -- 4 gates failing, then 2, with ruff red
    throughout. Formatting is entirely machine-solvable and most lint
    findings carry safe fixes, so spending a stochastic worker call on
    them buys nothing and displaces the correctness work.

    Scoped to the node's own changed files: formatting the whole tree
    would mark every pre-existing unformatted file changed, and
    `changed_line_coverage_min` would then demand coverage of lines the
    node never touched. Unsafe fixes stay off, and `check_ruff` still
    fails closed on whatever is left, so this narrows what the worker is
    asked to repair rather than lowering the bar.
    """
    changed = git_changed_files(workdir, baseline, recorder=recorder)
    targets = sorted(path for path in changed if path.endswith(".py"))
    if not targets:
        return
    # Order matters, and the test for this caught it: `--fix` rewrites
    # code (removing an unused import leaves a stray blank line), so
    # formatting has to be the last word or `ruff format --check` fails
    # on the mess the fixer just made. Exit codes are deliberately
    # ignored -- `ruff check --fix` reports what it could not fix, and
    # judging that is check_ruff's job.
    run_argv(["ruff", "check", "--fix", *targets], workdir, recorder=recorder)
    run_argv(["ruff", "format", *targets], workdir, recorder=recorder)
    run_argv(["git", "add", "--", *targets], workdir, recorder=recorder)


def format_attempt_failure(
    result: Tier1Result,
    captured: Sequence[CapturedRun],
    *,
    attempt: int,
    max_attempts: int,
) -> str:
    """Render one failed attempt as repair evidence: gates plus failing output."""
    failed = [check for check in result.checks if not check.passed]
    lines = [f"Attempt {attempt} of {max_attempts} failed {len(failed)} gate(s):"]
    lines.extend(f"- {check.name}: {check.detail}" for check in failed)
    for run in captured:
        if run.exit_code == 0:
            continue
        output = (run.stdout + "\n" + run.stderr).strip()
        if len(output) > RECOVERY_OUTPUT_CHARS:
            output = "[earlier output truncated]\n" + output[-RECOVERY_OUTPUT_CHARS:]
        lines.append(f"--- `{' '.join(run.argv)}` (exit {run.exit_code}) ---")
        lines.append(output or "(no output)")
    return "\n".join(lines) + "\n"


def _seal_attempt(
    journal_path: Path,
    node_id: str,
    run_span_id: str,
    worker_id: str,
    start: float,
    exit_code: int,
    detail: str,
) -> None:
    """Append one attempt's agent span under the run span."""
    append_span(
        journal_path,
        build_span(
            node_id=node_id,
            argv=[],
            duration_ms=_elapsed_ms(start),
            exit_code=exit_code,
            detail=detail,
            kind="agent",
            name=f"worker:{node_id}",
            parent_id=run_span_id,
            span_id=worker_id,
        ),
    )


def _evaluate_candidate(
    node: Node, workdir: Path, diff: str
) -> tuple[Tier1Result | None, str | None]:
    """Gate `diff` on a throwaway copy of `workdir`; never touches it.

    Returns the gate result, or an error string when the diff does not
    apply. The copy carries `.git`, so the baseline ref the gate diffs
    against resolves exactly as it would in place.

    Nothing here reaches the journal. These are gate runs against trees
    that are discarded, and the chain records what was proven about the
    tree that was *sealed* -- a tool span with no sealed attempt to
    parent it would be unlinkable by construction. The sampling is still
    auditable: the attempt seal carries how many distinct candidates were
    drawn.
    """
    with tempfile.TemporaryDirectory() as tmp:
        candidate = Path(tmp) / "tree"
        shutil.copytree(workdir, candidate, symlinks=True)
        # copytree rewrites mtimes, so git reads every file as modified and
        # `git apply --index` refuses the patch. Refreshing the index
        # restores the stat cache; the same trap cost a benchmark arm a
        # run when `cp -r` produced "uncommitted changes" worktrees.
        run_argv(["git", "update-index", "--refresh"], candidate)
        try:
            _apply_diff(candidate, diff)
        except RuntimeError as exc:
            return None, str(exc)
        return run_node_gate(node, candidate), None


def _best_of_samples(
    node: Node, workdir: Path, propose: Proposer
) -> tuple[DiffProposal | None, int]:
    """Draw PROPOSAL_SAMPLES unconditioned proposals; keep the best.

    Returns the winning diff and how many distinct diffs were drawn --
    agreement is the correlation signal. LLM samples "often fail on the
    same inputs", so k buys little when they agree, and recording it
    means rho is measured rather than assumed.
    """
    scored: list[tuple[int, int, DiffProposal]] = []
    drawn: list[str] = []
    last: DiffProposal | None = None
    for index in range(PROPOSAL_SAMPLES):
        # A truncated or malformed completion loses this draw, not the
        # other two: the samples are independent, so one bad packet is
        # not evidence about the rest.
        try:
            proposal = propose(node, None)
        except VllmResponseError:
            continue
        last = proposal
        drawn.append(proposal.diff)
        if any(proposal.diff == earlier for earlier in drawn[:-1]):
            continue
        result, unappliable = _evaluate_candidate(node, workdir, proposal.diff)
        if unappliable is not None or result is None:
            continue
        failures = sum(1 for check in result.checks if not check.passed)
        # `index` breaks ties toward the earliest sample, so selection is
        # deterministic rather than dependent on sort stability.
        scored.append((failures, index, proposal))
        if failures == 0:
            break
    if not scored:
        # Nothing gated cleanly and nothing applied. Hand back the last
        # sample rather than paying for another call: the attempt still
        # needs a diff to seal a failure against, and a fourth draw buys
        # no information the three already spent did not.
        return last, len(set(drawn))
    scored.sort(key=lambda entry: entry[:2])
    return scored[0][2], len(set(drawn))


async def _run_node(
    node: Node,
    workdir: Path,
    journal_path: Path,
    propose: Proposer,
    proofs: dict[str, str],
    run_span_id: str,
) -> Proof:
    """Execute one node: propose, apply, gate, seal — with bounded recovery.

    A failed gate (or a diff that does not apply) retries in a fresh
    worker call carrying the failure evidence, at most
    MAX_RECOVERY_RETRIES times; an identical re-proposal or exhausted
    retries fail the node. Fully synchronous inside, so a worker never
    yields mid-node and the proof map stays consistent without locks.
    """
    max_attempts = 1 + MAX_RECOVERY_RETRIES
    failure: str | None = None
    sampling = ""
    seen: list[str] = []
    applied: list[str] = []
    last_result: Tier1Result | None = None
    attempt = 0
    while attempt < max_attempts:
        attempt += 1
        start = perf_counter()
        worker_id = uuid.uuid4().hex
        recorder = SpanRecorder(path=journal_path, node_id=node.id, parent_id=worker_id)
        try:
            # A worker call that comes back truncated or malformed is a
            # spent attempt, not a dead node. The planner already retries
            # this exact condition (`_emit_valid_dag`); the worker path
            # raised instead, so T5's `finish_reason=length` failed the
            # node with the worktree untouched and nothing retried.
            try:
                if attempt == 1:
                    best, distinct = _best_of_samples(node, workdir, propose)
                    proposal = best if best is not None else propose(node, failure)
                    # Agreement across independent samples is the correlation
                    # signal: LLM samples "often fail on the same inputs", so
                    # k buys least exactly when they agree. Sealed so rho is
                    # measured across runs rather than assumed.
                    sampling = f"{distinct} distinct of {PROPOSAL_SAMPLES} sample(s)"
                else:
                    proposal = propose(node, failure)
            except VllmResponseError as exc:
                failure = f"Attempt {attempt} of {max_attempts}: worker call failed: {exc}"
                _seal_attempt(journal_path, node.id, run_span_id, worker_id, start, 1, failure)
                continue
            if proposal.diff in seen:
                detail = (
                    f"attempt {attempt}/{max_attempts}: "
                    "worker re-proposed an identical diff; stopping recovery"
                )
                # No tool spans precede this seal, so its span id is unlinkable by design.
                _seal_attempt(journal_path, node.id, run_span_id, worker_id, start, 1, detail)
                raise _HaltRecoveryError(last_result, attempt, failure)
            seen.append(proposal.diff)
            try:
                _apply_diff(workdir, proposal.diff, recorder=recorder)
            except RuntimeError as exc:
                failure = f"Attempt {attempt} of {max_attempts}: diff did not apply: {exc}"
                _seal_attempt(journal_path, node.id, run_span_id, worker_id, start, 1, failure)
                continue
            applied.append(proposal.diff)
            autofix(workdir, recorder=recorder)
            captured: list[CapturedRun] = []
            result = run_node_gate(node, workdir, recorder=recorder, capture=captured)
            if result.passed:
                parents = [proofs[dep] for dep in node.dependencies]
                record = build_from_gate(
                    node,
                    "\n".join(applied),
                    result,
                    parents,
                    f"{node.id}#{attempt}",
                    thinking=proposal.reasoning,
                    attempts=attempt,
                )
                append_record(journal_path, record)
                proofs[node.id] = record.record_hash
                detail = sampling if attempt == 1 else f"recovered after {attempt} attempts"
                _seal_attempt(journal_path, node.id, run_span_id, worker_id, start, 0, detail)
                return Proof(node_id=node.id)
            last_result = result
            failure = format_attempt_failure(
                result, captured, attempt=attempt, max_attempts=max_attempts
            )
            failed_count = sum(1 for check in result.checks if not check.passed)
            _seal_attempt(
                journal_path,
                node.id,
                run_span_id,
                worker_id,
                start,
                1,
                f"attempt {attempt}/{max_attempts}: {failed_count} gate(s) failed",
            )
        except _HaltRecoveryError as exc:
            if exc.result is None:
                detail = f"identical diff re-proposed after {exc.attempts} non-applying attempt(s)"
                raise NodeUnappliableError(node.id, detail, exc.attempts, exc.failure) from exc
            raise NodeGateFailedError(exc.result, exc.attempts, exc.failure) from exc
        except BaseException as exc:
            _seal_attempt(journal_path, node.id, run_span_id, worker_id, start, 1, str(exc))
            raise
    if last_result is None:
        detail = f"no proposed diff applied in {max_attempts} attempts"
        raise NodeUnappliableError(node.id, detail, max_attempts, failure)
    raise NodeGateFailedError(last_result, max_attempts, failure)


def splice_replan(dag: Dag, failed_id: str, new: Dag) -> tuple[Dag, list[str]]:
    """Replace `failed_id` with `new`'s nodes, rewiring dependents to new leaves.

    New ids are namespaced under the failed id; new roots inherit its
    dependencies. The failed node itself stays put so the transcript
    keeps its verdict. Acyclicity survives splicing: every new edge runs
    from proven nodes to new nodes, or new nodes to downstream nodes.
    """
    existing = {node.id for node in dag.nodes}
    mapping = {node.id: f"{failed_id}.r{index}" for index, node in enumerate(new.nodes, 1)}
    if set(mapping.values()) & existing:
        msg = f"replan for node {failed_id!r} collides with existing node ids"
        raise ReplanFailedError(msg)
    internal = {node.id for node in new.nodes}
    for node in new.nodes:
        for dep in node.dependencies:
            if dep not in internal:
                msg = f"replan node {node.id!r} depends on unknown node {dep!r}"
                raise ReplanFailedError(msg)
    failed = next(node for node in dag.nodes if node.id == failed_id)
    remapped = [
        node.model_copy(
            update={
                "id": mapping[node.id],
                "dependencies": (
                    [mapping[dep] for dep in node.dependencies]
                    if node.dependencies
                    else list(failed.dependencies)
                ),
            }
        )
        for node in new.nodes
    ]
    leaves = {node.id for node in remapped} - {
        dep for node in remapped for dep in node.dependencies
    }
    if not leaves:
        msg = f"replan for node {failed_id!r} has no leaf nodes"
        raise ReplanFailedError(msg)
    merged = []
    for node in dag.nodes:
        if failed_id not in node.dependencies:
            merged.append(node)
            continue
        deps: list[str] = []
        for dep in node.dependencies:
            deps.extend(sorted(leaves) if dep == failed_id else [dep])
        merged.append(node.model_copy(update={"dependencies": deps}))
    merged.extend(remapped)
    return Dag(nodes=merged), [node.id for node in remapped]


def format_replan_history(node_id: str, error: NodeGateFailedError | NodeUnappliableError) -> str:
    """One-paragraph failure history for a replan prompt."""
    if isinstance(error, NodeGateFailedError):
        failed = [check for check in error.result.checks if not check.passed]
        lines = [f"Node {node_id!r} failed Tier-1 after {error.attempts} attempt(s):"]
        lines.extend(f"- {check.name}: {check.detail}" for check in failed)
    else:
        lines = [f"Node {node_id!r} produced no applicable diff after {error.attempts} attempt(s)."]
    if error.failure is not None:
        lines.extend(["Last attempt evidence:", error.failure])
    return "\n".join(lines) + "\n"


def _schedulable_nodes(
    dag: Dag, proofs: dict[str, str], ever_failed: dict[str, BaseException]
) -> list[Node]:
    """Nodes ready to (re)schedule: unproven, never failed, unblocked.

    Proven dependencies strip out since their proofs already exist;
    anything behind a failure stays out so proof-gating holds.
    """
    ready = []
    for node in dag.nodes:
        if node.id in proofs or node.id in ever_failed:
            continue
        if any(dep in ever_failed for dep in node.dependencies):
            continue
        ready.append(
            node.model_copy(
                update={"dependencies": [dep for dep in node.dependencies if dep not in proofs]}
            )
        )
    return ready


def _transcribe(
    node: Node,
    sealed: ProofRecord | None,
    failure: BaseException | None,
    tool_spans: tuple[SpanRecord, ...],
) -> NodeTranscript:
    """One node's transcript row from its sealed record or its failure."""
    if sealed is not None:
        checks = tuple(
            GateCheck(name=output.name, passed=output.passed, detail=output.detail)
            for output in sealed.gate_outputs
        )
        return NodeTranscript(
            node.id,
            tuple(node.requirement_ids),
            checks,
            sealed.record_hash,
            thinking=sealed.thinking,
            tool_spans=tool_spans,
            attempts=sealed.attempts,
        )
    if isinstance(failure, NodeGateFailedError):
        checks = failure.result.checks
        attempts = failure.attempts
    elif isinstance(failure, NodeUnappliableError):
        checks = ()
        attempts = failure.attempts
    else:
        checks = ()
        attempts = 1
    return NodeTranscript(
        node.id, tuple(node.requirement_ids), checks, None, tool_spans=tool_spans, attempts=attempts
    )


def run_slice(
    task: str,
    dag: Dag,
    *,
    workdir: Path,
    journal_path: Path,
    propose: Proposer,
    replan: Replanner | None = None,
    merge_command: str | None = "pytest -q",
    now: Callable[[], str] = _utcnow,
) -> SliceResult:
    """Run one validated DAG through gates and journal; return its transcript.

    A journal that verifies is resumed, not refused (T3-1): its proven
    nodes seed `proofs`, so only unproven nodes are scheduled and a crash
    loses at most the in-flight node, as `rebuild_proven` promises. A
    journal that does not verify raises before anything runs. When
    `replan` is given, each exhausted node recompiles once into a
    replacement subgraph; replanned nodes that fail again stay failed.

    Every node is gated by its own scoped `test_command`; nothing checks
    the union of their diffs until `merge_command` runs, once, unscoped,
    in `workdir` after the schedule loop ends (ARCHITECTURE Tier 2, #60).
    It runs only if at least one node was proven, seals a tool span named
    `merge-suite` under the run span, and a non-zero exit fails the run
    without revisiting any per-node verdict. `None` disables it.
    """
    proofs: dict[str, str] = rebuild_proven(journal_path)
    started = now()
    run_start = perf_counter()
    run_span_id = uuid.uuid4().hex
    remaining = dag
    replanned_from: set[str] = set()
    generated: set[str] = set()
    ever_failed: dict[str, BaseException] = {}

    async def worker(node: Node, _constraints: ExecutionConstraints) -> Proof:
        # The scheduler sees a copy with proven dependencies stripped; the
        # proof record must cite every parent, so run the original node.
        original = next(candidate for candidate in remaining.nodes if candidate.id == node.id)
        return await _run_node(original, workdir, journal_path, propose, proofs, run_span_id)

    while True:
        ready = _schedulable_nodes(remaining, proofs, ever_failed)
        if not ready:
            # Only reachable on resume: every node already proven (or
            # blocked). A fresh DAG always has at least one root to run.
            break
        schedulable = Dag(nodes=ready)
        outcome = asyncio.run(schedule(schedulable, worker))
        ever_failed.update(outcome.failures)
        if replan is None:
            break
        eligible: dict[str, NodeGateFailedError | NodeUnappliableError] = {}
        for node_id, exc in outcome.failures.items():
            if (
                isinstance(exc, (NodeGateFailedError, NodeUnappliableError))
                and node_id not in replanned_from
                and node_id not in generated
            ):
                eligible[node_id] = exc
        if not eligible:
            break
        by_id = {node.id: node for node in remaining.nodes}
        progressed = False  # Observed only via `not`; falsy-init mutants are equivalent.
        for node_id, exc in eligible.items():
            try:
                new = replan(by_id[node_id], format_replan_history(node_id, exc))
                remaining, gen_ids = splice_replan(remaining, node_id, new)
            except ReplanFailedError:
                continue
            replanned_from.add(node_id)
            generated.update(gen_ids)
            progressed = True
        if not progressed:
            break
    sealed = {record.node_id: record for record in read_records(journal_path)}
    merge_exit = 0
    merge_ran = merge_command is not None and bool(proofs)
    if merge_command is not None and proofs:
        merge_start = perf_counter()
        merge_exit = run_shell(merge_command, workdir)
        timed_out = " (timed out)" if merge_exit == SHELL_TIMEOUT else ""
        append_span(
            journal_path,
            build_span(
                node_id="",
                argv=shlex.split(merge_command),
                duration_ms=_elapsed_ms(merge_start),
                exit_code=merge_exit,
                detail=f"merge-time full suite: exit {merge_exit}{timed_out}",
                name="merge-suite",
                parent_id=run_span_id,
            ),
        )
    tools = tool_spans_by_node(read_spans(journal_path))
    transcripts = tuple(
        _transcribe(
            node,
            sealed.get(node.id),
            ever_failed.get(node.id),
            tool_spans_for_node(tools, node.id),
        )
        for node in remaining.nodes
    )
    failed_unexcused = {node_id for node_id in ever_failed if node_id not in replanned_from}
    undispatched = {node.id for node in remaining.nodes} - set(proofs) - set(ever_failed)
    passed = not failed_unexcused and not undispatched and merge_exit == 0
    append_span(
        journal_path,
        build_span(
            node_id="",
            argv=[],
            duration_ms=_elapsed_ms(run_start),
            exit_code=0 if passed else 1,
            detail=(
                f"{len(proofs)} proven, "
                f"{len(failed_unexcused)} failed, "
                f"{len(undispatched)} undispatched"
                + (f", merge exit {merge_exit}" if merge_ran else "")
            ),
            kind="agent",
            name="run",
            span_id=run_span_id,
        ),
    )
    tail = [issue for issue in verify_journal(journal_path) if issue.code == "torn-tail"]
    if tail:
        msg = f"journal {str(journal_path)!r} has a torn tail after our own writes"
        raise RuntimeError(msg)
    text = render_transcript(
        RunTranscript(
            task=task,
            started=started,
            finished=now(),
            verdict="PASS" if passed else "FAIL",
            nodes=transcripts,
            journal_path=str(journal_path),
        )
    )
    return SliceResult(passed=passed, transcript=text, proofs=proofs)
