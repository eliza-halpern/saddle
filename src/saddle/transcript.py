"""Human-readable run transcripts, rendered from sealed records.

The transcript is a view, not a source of truth: every claim in it comes
from a verified proof record or a gate verdict, so it cannot drift from
what the machines checked.
"""

from __future__ import annotations

import json
import re
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Final

from saddle.gates import GateCheck
from saddle.journal import (
    AUTO_OUTCOMES,
    AUTO_START,
    COMPACTION_SPAN,
    JournalEntry,
    PlanRecord,
    ProofRecord,
    SpanRecord,
    tool_spans_by_node,
    tool_spans_for_node,
)
from saddle.labels import label_for

MAX_THOUGHT_EXCERPT_CHARS: Final = 200
# The run span a rule D question halt seals (slice.QUESTION_EXIT and its
# detail on phase2-rule-d, P2-3b). Mirrored, not imported: this module sits
# below slice, and a journal sealed there must render wherever it is read.
QUESTION_RUN_EXIT: Final = 4
QUESTION_RUN_DETAIL: Final = " halted on a question"


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
    passed, so an auditor recomputes it instead of trusting it. A run span
    sealed as a rule D question halt (exit 4, "halted on a question") with
    no sealed gate failure renders QUESTION; anything else not PASS is FAIL.

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
    checks_ok = all(check.passed for node in nodes for check in node.checks)
    # A rule D question halt (phase2-rule-d, P2-3b) seals its run span with
    # exit QUESTION_RUN_EXIT and a detail counting the halts. Both halves are
    # read: exit 4 alone is not that shape. A sealed gate failure still wins.
    asked = bool(runs) and runs[-1].exit_code == QUESTION_RUN_EXIT
    asked = asked and QUESTION_RUN_DETAIL in runs[-1].detail
    if run_ok and proven and checks_ok:
        verdict = "PASS"
    elif asked and checks_ok:
        verdict = "QUESTION"
    else:
        verdict = "FAIL"
    task = "(unknown)"
    starts = [s for s in spans if s.kind == "agent" and s.name == AUTO_START]
    if starts:
        task, verdict = _auto_verdict(starts[-1], spans)
    return render_transcript(
        RunTranscript(
            task=task,
            started="(unknown)",
            finished="(unknown)",
            verdict=verdict,
            nodes=nodes,
            journal_path=journal_path,
        )
    )


def _auto_verdict(start: SpanRecord, spans: Sequence[SpanRecord]) -> tuple[str, str]:
    """An autonomous run's task (its start span) and verdict (its outcome span).

    FIX-2: an autonomous run seals no gate outputs and no `run` span, so the
    slice rule above reads every one of them as FAIL, finished or not. Its
    verdict is its own `auto:finished`/`auto:stopped` span under `start`.
    FINISHED means the executor called finish, not that the change is proven:
    the packet's Audit, Tests and Mutation rows say what was checked.
    """
    task = start.argv[1] if len(start.argv) > 1 else "(unknown)"
    outcome = next(
        (s for s in reversed(spans) if s.parent_id == start.span_id and s.name in AUTO_OUTCOMES),
        None,
    )
    if outcome is None:
        return task, "NO OUTCOME (no auto:finished or auto:stopped span)"
    if outcome.name == "auto:finished":
        return task, "FINISHED"
    return task, f"STOPPED ({outcome.detail.split(';')[0].removeprefix('stopped: ')})"


@dataclass(frozen=True)
class SessionLine:
    """One ledger entry of an autonomous run, as the chat's task card shows it.

    `mark` carries the outcome at a glance and `tone` names it for styling
    (ok, fail, refused, audit, ask, info); `cite` is the entry's record hash,
    so every line on the card resolves to the record it was drawn from.
    """

    mark: str
    text: str
    tone: str
    cite: str


AUDIT_TIER: Final = re.compile(r"audit-tier(\d+):(.+)")
"""The real auditor's per-finding span name (`auditor.Auditor._journal`)."""

FEED_SPANS: Final = frozenset({"audit:delivered", "audit:withheld", "audit:check"})
"""The audit feed's delivery records (`feed.AuditFeed._journal`): one per
completed audit, not a gate's verdict; `audit:check` is a `check` call's
(`feed.CHECK_SPAN`), delivered as that call's result."""


@dataclass(frozen=True)
class TierFinding:
    """One `audit-tier<N>:<gate>` span, read back from its sealed detail."""

    gate: str
    tier: int
    verdict: str
    detail: str


def tier_finding(name: str, detail: str) -> TierFinding | None:
    """The finding an auditor span seals, or None if `name` is not one.

    The span's detail is the `auditor.Finding` as JSON; a detail that does
    not parse still names its gate and tier, with the raw text as detail.
    """
    match = AUDIT_TIER.fullmatch(name)
    if match is None:
        return None
    try:
        body = json.loads(detail)
    except ValueError:
        body = None
    if not isinstance(body, dict):
        return TierFinding(match.group(2), int(match.group(1)), "unreadable", detail)
    return TierFinding(
        match.group(2),
        int(match.group(1)),
        str(body.get("verdict", "unreadable")),
        str(body.get("detail", "")),
    )


def start_field(detail: str, key: str) -> str:
    """One `key value` field of an `auto:start` span's `;`-separated detail."""
    for part in detail.split(";"):
        part = part.strip()
        if part.startswith(f"{key} "):
            return part.removeprefix(f"{key} ")
    return ""


def _first_line(text: str, limit: int = 140) -> str:
    line = text.strip().splitlines()[0] if text.strip() else ""
    return line if len(line) <= limit else line[: limit - 1] + "…"


def _seconds(duration_ms: int) -> str:
    return f"{duration_ms}ms" if duration_ms < 1000 else f"{duration_ms / 1000:.1f}s"


def session_line(entry: JournalEntry) -> SessionLine | None:
    """An auto-run ledger entry as one session line, or None for a plan or a spend.

    The words for a tool call are `labels.label_for`, the same the chat's
    own tool rows use, so the card and the transcript say the same thing.
    The pass mark is drawn only from a zero exit code.
    """
    if isinstance(entry, PlanRecord):
        return None
    if isinstance(entry, ProofRecord):
        proof = entry.record_hash
        return SessionLine("■", f"sealed turn proof {proof[:8]}", "info", proof)
    cite = entry.record_hash
    name = entry.name
    if name == "auto:start":
        tests = "tests read-only" if entry.detail.endswith("refused") else "test edits allowed"
        where = start_field(entry.detail, "branch")
        arm = start_field(entry.detail, "arm")
        return SessionLine(
            "▸", f"started on {where}{f' · arm {arm}' if arm else ''} · {tests}", "info", cite
        )
    if name == "auto:spend":
        return None  # a round's spend: the card's meters show it, not a line
    if name == COMPACTION_SPAN:
        return SessionLine("≈", f"context compacted · {_first_line(entry.detail)}", "info", cite)
    if name.startswith("auto:"):
        outcome = name.removeprefix("auto:")
        return SessionLine(
            "✓" if entry.exit_code == 0 else "■",
            f"{outcome} · {entry.detail}",
            "ok" if entry.exit_code == 0 else "fail",
            cite,
        )
    if name.startswith("refused:"):
        return SessionLine(
            "⊘",
            f"refused by the tier-0 guard · {_first_line(entry.detail.split(': ', 2)[-1])}",
            "refused",
            cite,
        )
    finding = tier_finding(name, entry.detail)
    if finding is not None:
        ok = entry.exit_code == 0
        return SessionLine(
            "◆" if ok else "◇",
            f"audit tier {finding.tier} {finding.gate} {finding.verdict} · "
            f"{_first_line(finding.detail)}",
            "audit" if ok else "fail",
            cite,
        )
    if name in FEED_SPANS:
        ok = entry.exit_code == 0
        point = entry.argv[1] if len(entry.argv) > 1 else "audit"
        how = {
            "audit:delivered": "delivered to the model",
            "audit:check": "returned by check",
        }.get(name, "withheld from the model")
        body = entry.detail.splitlines()
        return SessionLine(
            "◆" if ok else "◇",
            f"audit {point} {'passed' if ok else 'failed'}, {how}"
            + (f" · {_first_line(body[1].removeprefix('- '))}" if len(body) > 1 else ""),
            "audit" if ok else "fail",
            cite,
        )
    if name.startswith("audit:"):
        gate = name.removeprefix("audit:")
        ok = entry.exit_code == 0
        return SessionLine(
            "◆" if ok else "◇",
            f"audit {gate} {'passed' if ok else 'failed'} · {_first_line(entry.detail)}",
            "audit" if ok else "fail",
            cite,
        )
    if name == "question":
        return SessionLine("?", f"asked you · {_first_line(entry.detail)}", "ask", cite)
    if name == "answer":
        return SessionLine("↳", f"you answered · {_first_line(entry.detail)}", "ask", cite)
    if entry.kind == "tool" and len(entry.argv) == 2:
        ok = entry.exit_code == 0
        label = label_for(entry.argv[0], entry.argv[1], ok=ok)
        # A command the tool ran is "ok" to the tool whatever it exited
        # with; the command's own exit code is what a reader is looking for.
        exited = re.match(r"exit (-?\d+)", entry.detail) if entry.argv[0] == "run_command" else None
        if ok and exited is not None and exited.group(1) != "0":
            took = _seconds(entry.duration_ms)
            return SessionLine("✗", f"{label} · exit {exited.group(1)} · {took}", "fail", cite)
        suffix = " · exit 0" if exited is not None else ""
        return SessionLine(
            "✓" if ok else "✗",
            f"{label}{suffix} · {_seconds(entry.duration_ms)}",
            "ok" if ok else "fail",
            cite,
        )
    return SessionLine(
        "✓" if entry.exit_code == 0 else "✗",
        f"{name} · exit {entry.exit_code}",
        "ok" if entry.exit_code == 0 else "fail",
        cite,
    )
