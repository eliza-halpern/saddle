"""Human-readable run transcripts, rendered from sealed records.

The transcript is a view, not a source of truth: every claim in it comes
from a verified proof record or a gate verdict, so it cannot drift from
what the machines checked.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Final

from saddle.gates import GateCheck
from saddle.journal import (
    JournalEntry,
    PlanRecord,
    ProofRecord,
    SpanRecord,
    tool_spans_by_node,
    tool_spans_for_node,
)

MAX_THOUGHT_EXCERPT_CHARS: Final = 200


@dataclass(frozen=True)
class NodeTranscript:
    """One node's story: what was claimed, checked, and sealed (or not)."""

    node_id: str
    requirement_ids: tuple[str, ...]
    checks: tuple[GateCheck, ...]
    proof_hash: str | None
    thinking: str = ""
    tool_spans: tuple[SpanRecord, ...] = ()
    attempts: int = 1


@dataclass(frozen=True)
class RunTranscript:
    """Everything a reader needs to audit one slice run."""

    task: str
    started: str
    finished: str
    verdict: str
    nodes: tuple[NodeTranscript, ...]
    journal_path: str


def _thought_excerpt(thinking: str) -> list[str]:
    """First 200 chars of thinking, split into display lines."""
    excerpt = thinking[:MAX_THOUGHT_EXCERPT_CHARS]
    if len(thinking) > MAX_THOUGHT_EXCERPT_CHARS:
        excerpt += "..."
    return excerpt.split("\n")


def _timeline_lines(node: NodeTranscript) -> list[str]:
    """Per-node timeline: the thought first, then each tool call in order."""
    if not node.thinking and not node.tool_spans:
        return []
    lines = ["- Timeline:"]
    if node.thinking:
        first, *rest = _thought_excerpt(node.thinking)
        lines.append(f"  - thought: {first}")
        lines.extend(f"    {line}" for line in rest)
    for span in node.tool_spans:
        command = " ".join(span.argv)
        lines.append(
            f"  - tool {span.name}: exit {span.exit_code} in {span.duration_ms}ms: {command}"
        )
    return lines


def is_run_end(entry: JournalEntry) -> bool:
    """True only for the run span that seals a finished run."""
    return (
        isinstance(entry, SpanRecord)
        and entry.kind == "agent"
        and entry.name == "run"
        and entry.node_id == ""
    )


def render_plan(plan: PlanRecord) -> list[str]:
    """A plan record as one header and one line per node (T6-13)."""
    head = f"replan of {plan.replaces}" if plan.replaces else "plan"
    lines = [f"{head}: {len(plan.nodes)} node(s)"]
    for node in plan.nodes:
        targets = ", ".join(node.target_files) if node.target_files else "(no target_files)"
        lines.append(
            f"  {node.id}  {node.kind}  budget={node.reasoning_budget}"
            f"  ctx={node.max_context_tokens}  targets: {targets}"
        )
    return lines


def render_event(entry: JournalEntry) -> list[str]:
    """One journal entry as live-tail lines (no trailing newlines)."""
    if isinstance(entry, PlanRecord):
        return render_plan(entry)
    if isinstance(entry, ProofRecord):
        passed = sum(1 for output in entry.gate_outputs if output.passed)
        total = len(entry.gate_outputs)
        lines = [
            f"[{entry.node_id}] sealed {entry.record_hash[:8]} ({passed}/{total} gates passed)"
        ]
        if entry.thinking:
            excerpt = _thought_excerpt(entry.thinking)
            lines.append(f"[{entry.node_id}] thought: {excerpt[0]}")
            lines.extend(f"    {rest}" for rest in excerpt[1:])
        return lines
    if entry.kind == "tool":
        command = " ".join(entry.argv)
        return [
            f"[{entry.node_id}] tool {entry.name}:"
            f" exit {entry.exit_code} in {entry.duration_ms}ms: {command}"
        ]
    label = entry.name if not entry.node_id else f"[{entry.node_id}] {entry.name}"
    line = f"{label}: exit {entry.exit_code} in {entry.duration_ms}ms"
    if entry.detail:
        line += f": {entry.detail}"
    return [line]


def render_transcript(run: RunTranscript) -> str:
    """Render one slice run as Markdown."""
    lines = [
        "# Saddle slice transcript",
        "",
        f"- Task: {run.task}",
        f"- Started: {run.started}",
        f"- Finished: {run.finished}",
        f"- Verdict: {run.verdict}",
        "",
    ]
    for node in run.nodes:
        lines.append(f"## Node {node.node_id}")
        lines.append("")
        lines.append(f"- Requirements: {', '.join(node.requirement_ids)}")
        if node.attempts > 1:
            lines.append(f"- Attempts: {node.attempts}")
        for check in node.checks:
            mark = "PASS" if check.passed else "FAIL"
            lines.append(f"- Gate {check.name}: {mark} ({check.detail})")
        lines.append(f"- Proof: {node.proof_hash or 'none'}")
        lines.extend(_timeline_lines(node))
        lines.append("")
    lines.append("## Journal")
    lines.append("")
    lines.append(f"- Path: {run.journal_path}")
    proven = sum(1 for node in run.nodes if node.proof_hash is not None)
    lines.append(f"- Proven nodes: {proven}")
    lines.append("- Issues: none (ledger verifies)")
    return "\n".join(lines) + "\n"


def render_journal_transcript(
    records: Sequence[ProofRecord], spans: Sequence[SpanRecord], journal_path: str
) -> str:
    """Re-render a transcript from sealed records alone (the audit view).

    Run metadata the journal never stores (task, timestamps) renders as
    "(unknown)"; the verdict is PASS only when every sealed gate output
    passed, so an auditor recomputes it instead of trusting it.

    Only proven nodes have sealed records, so proofs alone cannot see a
    failed node: the smoke run of 2026-09-19 (`1 proven, 1 failed, merge
    exit 2`) re-rendered as PASS. The run's own span is the record of
    what happened to the rest of the DAG, so when the journal has one its
    exit governs too (T3-21); a journal with no run span (in flight, or
    older than run spans) keeps the proof-only verdict.

    A journal with no sealed proofs is FAIL, never PASS: `all()` over an
    empty sequence is vacuously true, which reported runs that proved
    nothing -- a truncated worker, a crash before the first node -- as
    successes.
    """
    tools = tool_spans_by_node(spans)
    nodes = tuple(
        NodeTranscript(
            node_id=record.node_id,
            requirement_ids=tuple(record.requirement_ids),
            checks=tuple(
                GateCheck(name=output.name, passed=output.passed, detail=output.detail)
                for output in record.gate_outputs
            ),
            proof_hash=record.record_hash,
            thinking=record.thinking,
            tool_spans=tool_spans_for_node(tools, record.node_id),
            attempts=record.attempts,
        )
        for record in records
    )
    proven = any(node.checks for node in nodes)
    runs = [span for span in spans if span.name == "run"]
    run_ok = runs[-1].exit_code == 0 if runs else True
    verdict = (
        "PASS"
        if run_ok and proven and all(check.passed for node in nodes for check in node.checks)
        else "FAIL"
    )
    return render_transcript(
        RunTranscript(
            task="(unknown)",
            started="(unknown)",
            finished="(unknown)",
            verdict=verdict,
            nodes=nodes,
            journal_path=journal_path,
        )
    )
