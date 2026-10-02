"""The packet's audit count: auditor verdicts only, each counted as what it was.

Blocked is not failed: a finished run's verdict
line counted a `blocked` finding (tier 2 not run because tier 1 failed on
that tree) among "N audit findings failed". Blocked is reported as
blocked; failures count only gates that ran and failed.

Edit checks are not verdicts: the real auditor's
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

import json
import subprocess
import uuid
from collections.abc import Callable
from pathlib import Path

import pytest
from test_integ import repo as repo  # the E+A run's fixture
from test_integ import run as integ_run

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


def audited_run(
    tmp_path: Path,
    test: str = TEST,
    n: str = FIXED,
    audit: Callable[[Auditor, Path], object] = lambda auditor, _repo: auditor.audit(),
) -> Path:
    """A finished run's ledger holding the real auditor's records: by default
    one `Auditor.audit()` (tiers 0, 1, 2) over `n.py` and `test_n.py`."""
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "n.py").write_text(BASE)
    (repo / ".gitignore").write_text("__pycache__/\n.saddle/\n")
    git(repo, "init", "-q", "-b", "main")
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", "baseline")
    (repo / "n.py").write_text(n)
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
    audit(Auditor(repo, config=AuditorConfig(journal=journal)), repo)
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
    """Known-good: 9 verdicts and 6 edit checks (3 per file on the two changed
    files) read "9 of 9" plus an edit-check line naming each file.
    Known-bad: the old "12 of 12" reading."""
    journal = audited_run(tmp_path)
    gates = gates_by_tier(journal)
    assert gates[0] == set(TIER0)  # syntax, ruff, imports: 3 edit checks per file
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
    assert edit.status == "observed"  # an at-check check is never an audit verdict
    assert edit.text.startswith("6 of 6 edit checks passed. ")
    # More than one file: the row names each, in the text and in every item.
    assert "n.py, test_n.py" in edit.text
    assert [i.split(":")[0] for i in edit.items] == [
        "✓ syntax (n.py)",
        "✓ ruff (n.py)",
        "✓ imports (n.py)",
        "✓ syntax (test_n.py)",
        "✓ ruff (test_n.py)",
        "✓ imports (test_n.py)",
    ]
    assert len(edit.cites) == 6
    text = render_packet_text(packet)
    assert "12 of 12" not in text
    assert "Audit [proven]: 9 of 9 findings passed." in text
    assert "Edit checks [observed]: 6 of 6 edit checks passed." in text
    assert packet.verdict_text.endswith("every audit finding recorded passed.")


def test_a_failing_edit_check_is_its_own_failed_line_and_still_refuses_merge(
    tmp_path: Path,
) -> None:
    """Moving tier 0 out of the Audit row must not let a failing edit check merge.

    The unused import is in the file tier 0 checks last (`Auditor.audit`
    sorts by path): the row keeps the latest finding per gate per file, the
    failure stands, and the row names the file its record sealed.
    """
    journal = audited_run(tmp_path, test="import os\n" + TEST)
    packet = compile_packet(journal)
    rows = {row.key: row for row in packet.rows}
    edit = rows["edit-checks"]
    assert edit.status == "failed"
    assert edit.text.startswith("5 of 6 edit checks passed. ")
    assert "n.py, test_n.py" in edit.text
    assert any(i.startswith("✗ ruff (test_n.py): tier 0, fail: ") for i in edit.items)
    assert not any("tier 0" in item for item in rows["audit"].items)
    assert rows["audit"].text.endswith(" of 9 findings passed.")
    assert "Edit checks" in merge_refusal(packet)


def test_an_earlier_files_failed_edit_check_is_not_masked_by_a_later_file(
    tmp_path: Path,
) -> None:
    """Known-bad: the row once kept the latest finding per gate alone, so a
    failure on the file checked first read under the pass of the file
    checked last: n.py failed ruff, test_n.py passed it, and the row said
    "3 of 3 passed" with an empty merge refusal -- the failure was not on
    the record at all (the ledger did not name the file either).

    The tier-0 record now seals the file it checked under `path`, the row
    keeps the latest finding per gate per file, and with more than one
    file it names each: the failure stands, and the refusal names it.
    """
    journal = audited_run(tmp_path, n="import os\n\n\ndef f():\n    return 2\n")
    packet = compile_packet(journal)
    rows = {row.key: row for row in packet.rows}
    edit = rows["edit-checks"]
    assert edit.status == "failed"
    assert edit.text.startswith("5 of 6 edit checks passed. ")
    assert "n.py, test_n.py" in edit.text
    assert any(i.startswith("✗ ruff (n.py): tier 0, fail: ") for i in edit.items)
    assert any(i.startswith("✓ ruff (test_n.py): ") for i in edit.items)
    assert len(edit.cites) == 6
    # The failure is on the record, so the merge refusal names it.
    assert "Edit checks" in merge_refusal(packet)
    findings = [
        finding
        for finding in (
            tier_finding(span.name, span.detail, span.exit_code) for span in read_spans(journal)
        )
        if finding is not None
    ]
    # The ledger names the file in every tier-0 record, and in nothing else.
    assert sorted(f.path for f in findings if f.tier == 0) == [
        "n.py",
        "n.py",
        "n.py",
        "test_n.py",
        "test_n.py",
        "test_n.py",
    ]
    assert all(not f.path for f in findings if f.tier != 0)


def test_a_single_files_edit_checks_render_byte_identically_to_the_old_ledger(
    tmp_path: Path,
) -> None:
    """Done-when: a single-file ledger gives a packet byte-identical to
    before the records learned the file. The ledger the old auditor sealed
    for one file (no `path` key in the record's JSON) and the one the new
    auditor seals for the same file (the record names it) compile to
    byte-identical packets: the row's count, text and items read exactly
    as they did before the change.
    """
    old = tmp_path / "a" / ".saddle" / "runs" / "r1" / "proofs.jsonl"
    for gate, detail in (
        ("syntax", "1 file(s) parsed"),
        ("ruff", "1 file(s) clean"),
        ("imports", "0 name(s) resolved"),
    ):
        body = {
            "gate": gate,
            "tier": 0,
            "verdict": "pass",
            "reason": "code-wrong",
            "detail": detail,
            "cites": [],
        }  # fmt: skip
        append_span(
            old,
            build_span(
                node_id="n",
                argv=["saddle-audit", "tier0", gate, "k"],
                duration_ms=0,
                exit_code=0,
                detail=json.dumps(body, sort_keys=True),
                name=f"audit-tier0:{gate}",
            ),
        )
    for span in read_spans(old):
        assert "path" not in json.loads(span.detail)
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "n.py").write_text(FIXED)
    (repo / ".gitignore").write_text("__pycache__/\n.saddle/\n")
    # A project that chose ruff, so the clean detail is the old text (#130).
    (repo / "ruff.toml").write_text("line-length = 88\n")
    git(repo, "init", "-q", "-b", "main")
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", "baseline")
    new = tmp_path / "b" / ".saddle" / "runs" / "r1" / "proofs.jsonl"
    Auditor(repo, config=AuditorConfig(journal=new)).tier0("n.py", FIXED)
    for span in read_spans(new):
        assert json.loads(span.detail)["path"] == "n.py"
    # Same run id on purpose: the packets must be byte-identical, not merely
    # differ by the ledger's location in the file system.
    assert render_packet_text(compile_packet(old)) == render_packet_text(compile_packet(new))
    row = next(r for r in compile_packet(new).rows if r.key == "edit-checks")
    assert row.text == (
        "3 of 3 edit checks passed. Tier 0 checks one edited file (syntax, ruff, imports) "
        "when it is written; it is not an audit verdict and is not counted in Audit."
    )
    assert row.items == (
        "✓ syntax: tier 0, pass: 1 file(s) parsed",
        "✓ ruff: tier 0, pass: 1 file(s) clean",
        "✓ imports: tier 0, pass: 0 name(s) resolved",
    )


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


# -- blocked is not failed -------------------------------------------------------


BLOCKED = "tier 1 failed (coverage); tier 2 not run"


def test_a_finished_run_with_a_blocked_tier_2_counts_it_blocked_not_failed(repo: Path) -> None:
    """Known-good: arm E+A accepts finish over a coverage failure, and tier 2
    is blocked. Known-bad: the old line, "2 audit findings failed"."""
    result, _ = integ_run(repo, learns=False, arm="E+A")
    assert result.outcome == "finished", result.reason
    found = [
        f for s in read_spans(result.journal) if (f := tier_finding(s.name, s.detail)) is not None
    ]
    assert [(f.gate, f.verdict) for f in found if f.tier == 2] == [("mutation", "blocked")]
    packet = compile_packet(result.journal)
    assert packet.verdict == "finished"
    assert packet.verdict_text == (
        f"Finished, but 1 audit finding failed, and 1 audit finding blocked (not run): {BLOCKED}."
    )
    assert "2 audit findings failed" not in render_packet_text(packet)


def _blocked_then_cleared(auditor: Auditor, repo: Path) -> None:
    """Tier 2 blocked on a tree with an uncovered file; then that file goes
    and a checkpoint's tier 1 passes on the newer tree, with no tier 2 after."""
    (repo / "m.py").write_text("def g():\n    return 7\n")
    assert auditor.tier2().findings[0].verdict == "blocked"
    (repo / "m.py").unlink()
    assert auditor.tier1().passed


@pytest.mark.parametrize(
    ("audit", "text"),
    [
        (_blocked_then_cleared, f"Finished, but 1 audit finding blocked (not run): {BLOCKED}."),
        (
            lambda auditor, _repo: auditor.tier2(),
            "The executor called finish, and every audit finding recorded passed.",
        ),
    ],
    ids=["blocked-only", "all-ran-and-passed"],
)
def test_the_verdict_line_names_a_blocked_finding_and_never_calls_it_failed_or_passed(
    tmp_path: Path, audit: Callable[[Auditor, Path], object], text: str
) -> None:
    packet = compile_packet(audited_run(tmp_path, audit=audit))
    assert packet.verdict_text == text
