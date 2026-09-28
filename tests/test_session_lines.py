"""Session lines: one ledger entry, one line on the task card.

The pass mark is drawn from an exit code only: a command that exited 1 is a
failure line even though the tool that ran it succeeded. Every line cites
the record it was drawn from.
"""

from __future__ import annotations

from typing import Any, Literal

import pytest

from saddle.journal import build_plan, build_record, build_span
from saddle.transcript import _first_line, _seconds, session_line


def span(
    name: str,
    *,
    argv: list[str] | None = None,
    exit_code: int = 0,
    detail: str = "",
    kind: Literal["tool", "agent"] = "agent",
    duration_ms: int = 5,
) -> Any:
    return build_span(
        node_id="auto",
        argv=argv or [name],
        duration_ms=duration_ms,
        exit_code=exit_code,
        detail=detail,
        kind=kind,
        name=name,
    )


@pytest.mark.parametrize(
    ("entry", "mark", "tone", "words"),
    [
        (
            span("auto:start", detail="branch saddle/auto/r1; tests refused"),
            "▸",
            "info",
            "started on saddle/auto/r1 · tests read-only",
        ),
        (span("auto:start", detail="branch b; tests allowed"), "▸", "info", "test edits allowed"),
        (span("auto:finished", detail="done"), "✓", "ok", "finished · done"),
        (span("auto:stopped", exit_code=3, detail="stopped: budget"), "■", "fail", "stopped"),
        (
            span(
                "refused:edit_file", exit_code=1, detail="refused: edit_file: tests are read-only"
            ),
            "⊘",
            "refused",
            "tier-0 guard · tests are read-only",
        ),
        (span("audit:coverage", detail="1 of 1"), "◆", "audit", "audit coverage passed · 1 of 1"),
        (span("audit:coverage", exit_code=1, detail="0 of 1"), "◇", "fail", "failed"),
        (span("question", exit_code=4, detail="Return 0?"), "?", "ask", "asked you · Return 0?"),
        (span("answer", detail="yes"), "↳", "ask", "you answered · yes"),
        (
            span(
                "run_command",
                argv=["run_command", '{"command": "pytest"}'],
                kind="tool",
                detail="exit 1\nFAILED",
                duration_ms=1500,
            ),
            "✗",
            "fail",
            "exit 1 · 1.5s",
        ),
        (
            span(
                "run_command",
                argv=["run_command", '{"command": "pytest"}'],
                kind="tool",
                detail="exit 0\nok",
            ),
            "✓",
            "ok",
            "· exit 0 · 5ms",
        ),
        (span("read_file", argv=["read_file", '{"path": "a.py"}'], kind="tool"), "✓", "ok", "a.py"),
        (
            span("read_file", argv=["read_file", '{"path": "a.py"}'], kind="tool", exit_code=1),
            "✗",
            "fail",
            "a.py",
        ),
        (span("odd", exit_code=2), "✗", "fail", "odd · exit 2"),
        (span("odd"), "✓", "ok", "odd · exit 0"),
    ],
)
def test_each_entry_becomes_one_cited_line(entry: Any, mark: str, tone: str, words: str) -> None:
    line = session_line(entry)
    assert line is not None
    assert (line.mark, line.tone) == (mark, tone)
    assert words in line.text
    assert line.cite == entry.record_hash


def test_a_proof_is_a_sealed_line_and_a_plan_is_none() -> None:
    record = build_record(
        evidence_id="e",
        node_id="turn",
        diff="",
        parent_proofs=[],
        gate_outputs=[],
        requirement_ids=[],
        thinking="",
    )
    line = session_line(record)
    assert line is not None
    assert line.mark == "■"
    assert line.cite == record.record_hash
    assert session_line(build_plan([], task_hash="t")) is None


def test_first_line_and_seconds() -> None:
    assert _first_line("") == ""
    assert _first_line("a\nb") == "a"
    assert _first_line("x" * 200, limit=10) == "x" * 9 + "…"
    assert _seconds(999) == "999ms"
    assert _seconds(1000) == "1.0s"
