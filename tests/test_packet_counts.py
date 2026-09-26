"""The packet's audit count: auditor verdicts only, each counted as what it was.

PACKETFIX-1 (out/CHECK/report.md, DISPATCH 12:30): the real auditor's
tier-0 findings (syntax, ruff, imports on one edited file) were counted as
audit verdicts, so a run whose auditor issued 9 verdicts read "12 of 12".
A tier-0 check is reported on its own "Edit checks" row; the Audit row and
the verdict line count tier-1 and tier-2 findings (and chat-seam
`audit:<gate>` records) only.

The ledgers here are written by the real `auditor.Auditor` (its `_journal`
is the only writer of `audit-tier<N>:<gate>` spans), wrapped in the
`auto:start` / `auto:finished` spans the engine seals.
"""

from __future__ import annotations

import subprocess
import uuid
from pathlib import Path

import pytest

from saddle.auditor import TIER0, Auditor, AuditorConfig
from saddle.journal import append_span, build_span, read_spans, write_attempt_sidecar
from saddle.packet import compile_packet, render_packet_text
from saddle.transcript import tier_finding
from saddle.web.branch_actions import merge_refusal

BASE = "def f():\n    return 1\n"
FIXED = "def f():\n    return 2\n"
TEST = "from n import f\n\n\ndef test_f():\n    assert f() == 2\n"


def git(root: Path, *argv: str) -> None:
    subprocess.run(
        ["git", "-C", str(root), "-c", "user.name=t", "-c", "user.email=t@t", *argv],
        check=True,
        capture_output=True,
    )


def audited_run(tmp_path: Path, test: str = TEST) -> Path:
    """A finished run's ledger holding one real `Auditor.audit()` (tiers 0, 1, 2)."""
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "n.py").write_text(BASE)
    (repo / ".gitignore").write_text("__pycache__/\n.saddle/\n")
    git(repo, "init", "-q", "-b", "main")
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", "baseline")
    (repo / "n.py").write_text(FIXED)
    (repo / "test_n.py").write_text(test)
    journal = repo / ".saddle" / "runs" / "r1" / "proofs.jsonl"
    start = build_span(
        node_id="auto",
        argv=["auto:start", "make f return 2"],
        duration_ms=0,
        exit_code=0,
        detail="arm E+A; branch saddle/auto/r1; test edits refused",
        kind="agent",
    )
    append_span(journal, start)
    Auditor(repo, config=AuditorConfig(journal=journal)).audit()
    span_id = uuid.uuid4().hex
    evidence = {
        "outcome": "finished",
        "files_changed": ["n.py", "test_n.py"],
        "rounds": 1,
        "tool_span_hashes": [],
    }
    digest = write_attempt_sidecar(journal, span_id, evidence)
    append_span(
        journal,
        build_span(
            node_id="auto",
            argv=["auto:finished"],
            duration_ms=0,
            exit_code=0,
            detail="finished: finish called; arm E+A",
            kind="agent",
            parent_id=start.span_id,
            span_id=span_id,
            attempt_hash=digest,
        ),
    )
    return journal


def gates_by_tier(journal: Path) -> dict[int, set[str]]:
    """The ledger's distinct finding gates per tier: the denominator, counted independently."""
    found: dict[int, set[str]] = {}
    for span in read_spans(journal):
        finding = tier_finding(span.name, span.detail)
        if finding is not None:
            found.setdefault(finding.tier, set()).add(finding.gate)
    return found


def test_tier_0_edit_checks_are_not_counted_as_audit_verdicts(tmp_path: Path) -> None:
    """Known-good: 9 verdicts and 3 edit checks read "9 of 9" plus an edit-check line.
    Known-bad: the old "12 of 12" reading."""
    journal = audited_run(tmp_path)
    gates = gates_by_tier(journal)
    assert gates[0] == set(TIER0)  # syntax, ruff, imports: 3 edit checks
    # tests/full-suite fill the Tests row and mutation the Mutation row.
    verdicts = (gates[1] | gates[2]) - {"tests", "full-suite", "mutation"}
    assert len(verdicts) == 9
    packet = compile_packet(journal)
    rows = {row.key: row for row in packet.rows}
    assert rows["audit"].text == "9 of 9 findings passed."
    assert rows["audit"].status == "proven"
    assert len(rows["audit"].cites) == 9
    assert not any("tier 0" in item for item in rows["audit"].items)
    edit = rows["edit-checks"]
    assert edit.title == "Edit checks"
    assert edit.status == "observed"  # an at-edit check is never an audit verdict
    assert edit.text.startswith("3 of 3 edit checks passed. ")
    assert [i.split(":")[0] for i in edit.items] == ["✓ syntax", "✓ ruff", "✓ imports"]
    assert len(edit.cites) == 3
    text = render_packet_text(packet)
    assert "12 of 12" not in text
    assert "Audit [proven]: 9 of 9 findings passed." in text
    assert "Edit checks [observed]: 3 of 3 edit checks passed." in text
    assert packet.verdict_text.endswith("every audit finding recorded passed.")


def test_a_failing_edit_check_is_its_own_failed_line_and_still_refuses_merge(
    tmp_path: Path,
) -> None:
    """Moving tier 0 out of the Audit row must not let a failing edit check merge.

    The unused import is in the file tier 0 checks last (`Auditor.audit`
    sorts by path): the row keeps the latest finding per gate, so an earlier
    file's failure is masked by a later file's pass (open, see
    out/PACKETFIX/report.md; the ledger does not record which file a tier-0
    span checked).
    """
    journal = audited_run(tmp_path, test="import os\n" + TEST)
    packet = compile_packet(journal)
    rows = {row.key: row for row in packet.rows}
    edit = rows["edit-checks"]
    assert edit.status == "failed"
    assert edit.text.startswith("2 of 3 edit checks passed. ")
    assert any(i.startswith("✗ ruff: tier 0, fail: ") for i in edit.items)
    assert not any("tier 0" in item for item in rows["audit"].items)
    assert rows["audit"].text.endswith(" of 9 findings passed.")
    assert "Edit checks" in merge_refusal(packet)


def test_a_ledger_with_only_edit_checks_has_no_audit_verdict(tmp_path: Path) -> None:
    """Tier 0 alone is not an audit: the verdict says so and there is no Audit row."""
    journal = tmp_path / "proofs.jsonl"
    auditor = Auditor(tmp_path, config=AuditorConfig(journal=journal))
    auditor.tier0("n.py", FIXED)
    packet = compile_packet(journal)
    rows = {row.key: row for row in packet.rows}
    assert "audit" not in rows
    assert rows["edit-checks"].text.startswith("3 of 3 edit checks passed.")
    assert any(i.startswith("No auditor verdict") for i in rows["not-proven"].items)


@pytest.mark.parametrize("key", ["audit", "edit-checks"])
def test_every_count_row_cites_the_records_it_counts(tmp_path: Path, key: str) -> None:
    journal = audited_run(tmp_path)
    row = {r.key: r for r in compile_packet(journal).rows}[key]
    by_hash = {s.record_hash: s for s in read_spans(journal)}
    tiers = {tier_finding(by_hash[c].name, by_hash[c].detail).tier for c in row.cites}  # type: ignore[union-attr]
    assert tiers == ({0} if key == "edit-checks" else {1, 2})
