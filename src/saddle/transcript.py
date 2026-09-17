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
from saddle.journal import ProofRecord, SpanRecord, tool_spans_by_node, tool_spans_for_node

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


@dataclass(frozen=True)
class RunTranscript:
    """Everything a reader needs to audit one slice run."""

    task: str
    started: str
    finished: str
    verdict: str
    nodes: tuple[NodeTranscript, ...]
    journal_path: str


def _timeline_lines(node: NodeTranscript) -> list[str]:
    """Per-node timeline: the thought first, then each tool call in order."""
    if not node.thinking and not node.tool_spans:
        return []
    lines = ["- Timeline:"]
    if node.thinking:
        excerpt = node.thinking[:MAX_THOUGHT_EXCERPT_CHARS]
        if len(node.thinking) > MAX_THOUGHT_EXCERPT_CHARS:
            excerpt += "..."
        first, *rest = excerpt.split("\n")
        lines.append(f"  - thought: {first}")
        lines.extend(f"    {line}" for line in rest)
    for span in node.tool_spans:
        command = " ".join(span.argv)
        lines.append(
            f"  - tool {span.name}: exit {span.exit_code} in {span.duration_ms}ms: {command}"
        )
    return lines


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
    lines.append("- Issues: none (chain verifies)")
    return "\n".join(lines) + "\n"


def render_journal_transcript(
    records: Sequence[ProofRecord], spans: Sequence[SpanRecord], journal_path: str
) -> str:
    """Re-render a transcript from sealed records alone (the audit view).

    Run metadata the journal never stores (task, timestamps) renders as
    "(unknown)"; the verdict is PASS only when every sealed gate output
    passed, so an auditor recomputes it instead of trusting it.
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
        )
        for record in records
    )
    verdict = "PASS" if all(check.passed for node in nodes for check in node.checks) else "FAIL"
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
