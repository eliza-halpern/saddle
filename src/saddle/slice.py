"""Vertical-slice driver (ARCHITECTURE.md §6 step 1).

Runs one validated DAG through scheduler, Tier-1 gates, and journal, then
renders the transcript from the sealed records. Emission and validation
stay caller-side: this module is the deterministic schedule→gate→seal→
transcribe path. One node completes fully in the slice; wider graphs ride
the same path.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from saddle.dag import Dag, ExecutionConstraints, Node
from saddle.evidence import run_stdin
from saddle.gates import GateCheck, Tier1Result
from saddle.journal import (
    ProofRecord,
    append_record,
    build_from_gate,
    read_records,
    verify_journal,
)
from saddle.runner import run_node_gate
from saddle.scheduler import Proof, schedule
from saddle.transcript import NodeTranscript, RunTranscript, render_transcript


class NodeGateFailedError(Exception):
    """A node worker failed its Tier-1 gate; carries the verdict."""

    def __init__(self, result: Tier1Result) -> None:
        super().__init__(f"node {result.node_id!r} failed its Tier-1 gate")
        self.result = result


@dataclass(frozen=True)
class SliceResult:
    """Slice outcome: verdict, readable transcript, proven node hashes."""

    passed: bool
    transcript: str
    proofs: dict[str, str]


def _utcnow() -> str:
    """Current UTC time as an ISO string for transcripts."""
    return datetime.now(UTC).isoformat()


def _apply_diff(workdir: Path, diff: str) -> None:
    """Apply a proposed diff from stdin and stage it; gates diff tracked content."""
    exit_code = run_stdin(["git", "apply", "--index", "--recount", "-"], workdir, diff)
    if exit_code != 0:
        msg = f"worker diff did not apply cleanly in {str(workdir)!r}"
        raise RuntimeError(msg)


async def _run_node(
    node: Node,
    workdir: Path,
    journal_path: Path,
    propose: Callable[[Node], str],
    proofs: dict[str, str],
) -> Proof:
    """Execute one node: propose a diff, apply, gate, seal, append.

    Fully synchronous inside, so a worker never yields mid-node and the
    proof map stays consistent without locks.
    """
    diff = propose(node)
    _apply_diff(workdir, diff)
    result = run_node_gate(node, workdir)
    if not result.passed:
        raise NodeGateFailedError(result)
    parents = [proofs[dep] for dep in node.dependencies]
    record = build_from_gate(node, diff, result, parents, f"{node.id}#1")
    append_record(journal_path, record)
    proofs[node.id] = record.record_hash
    return Proof(node_id=node.id)


def _transcribe(
    node: Node, sealed: ProofRecord | None, failure: BaseException | None
) -> NodeTranscript:
    """One node's transcript row from its sealed record or its failure."""
    if sealed is not None:
        checks = tuple(
            GateCheck(name=output.name, passed=output.passed, detail=output.detail)
            for output in sealed.gate_outputs
        )
        return NodeTranscript(node.id, tuple(node.requirement_ids), checks, sealed.record_hash)
    if isinstance(failure, NodeGateFailedError):
        checks = failure.result.checks
    else:
        checks = ()
    return NodeTranscript(node.id, tuple(node.requirement_ids), checks, None)


def run_slice(
    task: str,
    dag: Dag,
    *,
    workdir: Path,
    journal_path: Path,
    propose: Callable[[Node], str],
    now: Callable[[], str] = _utcnow,
) -> SliceResult:
    """Run one validated DAG through gates and journal; return its transcript.

    The journal must be fresh: resuming onto existing proofs would append
    duplicate records, so that waits for the resume design.
    """
    if read_records(journal_path):
        msg = f"journal {str(journal_path)!r} is not fresh; resume is not supported"
        raise ValueError(msg)
    started = now()
    proofs: dict[str, str] = {}

    async def worker(node: Node, _constraints: ExecutionConstraints) -> Proof:
        return await _run_node(node, workdir, journal_path, propose, proofs)

    outcome = asyncio.run(schedule(dag, worker))
    sealed = {record.node_id: record for record in read_records(journal_path)}
    transcripts = tuple(
        _transcribe(node, sealed.get(node.id), outcome.failures.get(node.id)) for node in dag.nodes
    )
    tail = [issue for issue in verify_journal(journal_path) if issue.code == "torn-tail"]
    if tail:
        msg = f"journal {str(journal_path)!r} has a torn tail after our own writes"
        raise RuntimeError(msg)
    passed = not outcome.failures and not outcome.undispatched
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
