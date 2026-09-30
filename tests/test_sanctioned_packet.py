"""The packet renders a sanctioned test rewrite as sanctioned, not failed.

`auditor.sanction` reclasses a failing `assertion-preservation` finding that
names only tests the task ordered rewritten: the verdict stays `fail` (the
gate did see the rewrite) but the `reason` becomes `sanctioned`, so the
finding neither blocks tier 2 nor refuses `finish`. The packet must carry
that waiver, not the failure it used to read: the Audit row and the verdict
line show the finding as *sanctioned* -- reported, not held against the run
-- and an unsanctioned rename still reads as the failure it is.

The ledger is seeded the way the auditor journals it: each finding is a
`audit-tier<N>:<gate>` span sealing the `Finding` as JSON (gate, tier,
verdict, reason, detail), and the run's end is an `auto:finished` span. No
model, no branch -- the packet is a pure reader of that ledger.
"""

from __future__ import annotations

import json
from pathlib import Path

from saddle.journal import append_span, build_span
from saddle.packet import Packet, Row, compile_packet

SANCTIONED_DETAIL = (
    "refactor node rewrote assertions in: test_fee_basic (all sanctioned by the task)"
)


def finding_span(
    journal: Path,
    tier: int,
    gate: str,
    verdict: str,
    reason: str,
    detail: str,
) -> None:
    """One `audit-tier<N>:<gate>` span, the finding as the auditor seals it."""
    body = {
        "gate": gate,
        "tier": tier,
        "verdict": verdict,
        "reason": reason,
        "detail": detail,
        "cites": [],
    }
    exit_code = {"pass": 0, "fail": 1, "blocked": 2, "not-proven": 0}[verdict]
    append_span(
        journal,
        build_span(
            node_id="n",
            argv=["saddle-audit", f"tier{tier}", gate, "k"],
            duration_ms=0,
            exit_code=exit_code,
            detail=json.dumps(body),
            name=f"audit-tier{tier}:{gate}",
        ),
    )


def finished(journal: Path) -> None:
    """The run's accepted end: the verdict the packet reads, and nothing else."""
    append_span(
        journal,
        build_span(
            node_id="n",
            argv=["auto:finished"],
            duration_ms=1000,
            exit_code=0,
            detail="finished: finish called; arm E+A+F",
            kind="agent",
            name="auto:finished",
        ),
    )


def audit_row(packet: Packet) -> Row:
    row: Row = next(r for r in packet.rows if r.key == "audit")
    return row


def test_a_sanctioned_rewrite_is_rendered_sanctioned_not_failed(tmp_path: Path) -> None:
    """A rewrite the task sanctioned is the waiver, not the failure.

    Known-bad: the same ledger read the assertion-preservation finding as a
    failed Audit row ("0 of 1 finding passed", a failed verdict line), which
    held a task-ordered rewrite against the run.
    """
    journal = tmp_path / "proofs.jsonl"
    finding_span(journal, 1, "assertion-preservation", "fail", "sanctioned", SANCTIONED_DETAIL)
    finished(journal)
    packet = compile_packet(journal)
    assert packet.verdict == "finished"
    row = audit_row(packet)
    # Not a failure and not a pass: the task's own waiver, its own status.
    assert row.status == "sanctioned", row
    assert "1 sanctioned" in row.text
    assert row.text.startswith("0 of 1 finding passed")
    # The item is the waiver, marked, not the failure mark.
    assert row.items == (f"↪ assertion-preservation: tier 1, fail: {SANCTIONED_DETAIL}",), row.items
    # The verdict names it as waived; it never reads as the refusal it is not.
    assert "was sanctioned" in packet.verdict_text
    assert "not held against the run" in packet.verdict_text
    assert "Finished, but" not in packet.verdict_text


def test_an_unsanctioned_rename_is_rendered_failed(tmp_path: Path) -> None:
    """A rewrite the task did not sanction is the failure it always was."""
    journal = tmp_path / "proofs.jsonl"
    finding_span(
        journal,
        1,
        "assertion-preservation",
        "fail",
        "evidence-thin",
        "refactor node rewrote assertions in: test_fee_basic",
    )
    finished(journal)
    packet = compile_packet(journal)
    row = audit_row(packet)
    assert row.status == "failed", row
    assert row.items[0].startswith("✗ assertion-preservation:"), row.items
    assert packet.verdict_text == "Finished, but 1 audit finding failed."


def test_a_sanctioned_rewrite_does_not_mask_a_real_failure(tmp_path: Path) -> None:
    """A sanctioned rewrite beside a genuine failure: only the failure refuses.

    The waiver is never the thing the verdict names, and it is not counted
    among the failures; the Audit row is failed on the real one alone.
    """
    journal = tmp_path / "proofs.jsonl"
    finding_span(journal, 1, "assertion-preservation", "fail", "sanctioned", SANCTIONED_DETAIL)
    finding_span(journal, 1, "coverage", "fail", "evidence-thin", "calc.py:2 uncovered")
    finished(journal)
    packet = compile_packet(journal)
    row = audit_row(packet)
    assert row.status == "failed", row
    # One refusal, named by the coverage failure: the sanctioned finding is
    # not the second one a naive count would add.
    assert packet.verdict_text == "Finished, but 1 audit finding failed."
    # Both findings stay listed; the waiver keeps its mark, the failure its own.
    assert any(i.startswith("↪ assertion-preservation:") for i in row.items), row.items
    assert any(i.startswith("✗ coverage:") for i in row.items), row.items


def test_a_sanctioned_rewrite_alongside_an_open_not_proven(tmp_path: Path) -> None:
    """A sanctioned rewrite and an open not-proven: neither refuses, both show.

    The not-proven finding outranks the waiver for the row's status; the
    verdict says nothing refused and names the waiver without calling it the
    failure its plain verdict is.
    """
    journal = tmp_path / "proofs.jsonl"
    finding_span(journal, 1, "assertion-preservation", "fail", "sanctioned", SANCTIONED_DETAIL)
    finding_span(
        journal,
        1,
        "coverage",
        "not-proven",
        "evidence-thin",
        "open shortlist: 3 survivors untested",
    )
    finished(journal)
    packet = compile_packet(journal)
    row = audit_row(packet)
    assert row.status == "not-proven", row
    assert "1 not proven" in row.text, row.text
    assert "1 sanctioned" in row.text, row.text
    # The verdict: nothing refused, the open one is named, the waiver is named
    # as a waiver, and neither is read as a failure.
    assert "No audit finding failed" in packet.verdict_text
    assert "was also sanctioned" in packet.verdict_text
    assert "not held against the run" in packet.verdict_text
    assert "Finished, but" not in packet.verdict_text
