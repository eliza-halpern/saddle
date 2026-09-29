"""A finding's sealed ledger line: its verdict is what was sealed, never
whether the line parses.

`journal.build_span` caps a span's detail at `MAX_SPAN_DETAIL_CHARS`. The
auditor sealed each finding as its whole JSON, so a long one was cut there
and lost `verdict`, the last of its sorted keys. The readers then called it
"unreadable": a question counted as a failure, a failure lost its word, and
an exit-0 not-proven finding (a shortlist's surviving mutants) read as
proven. Of 3,907 finding lines in the ledgers on the development box, 235
were cut: coverage 193, mutation 38, ruff 2 and task-requirements 2.
"""

from __future__ import annotations

import dataclasses
import json
from pathlib import Path

import pytest
from test_auditor import clean_tree

from saddle import coverage_text
from saddle.auditor import (
    TASK_REQUIREMENTS,
    Auditor,
    AuditorConfig,
    Finding,
    Findings,
    sealed_finding,
)
from saddle.journal import (
    JOURNAL_QUESTION_EXIT,
    MAX_SPAN_DETAIL_CHARS,
    SEALED_CUT,
    append_span,
    build_span,
    read_spans,
    verify_journal,
)
from saddle.packet import Row, compile_packet
from saddle.transcript import LINE_CUT, session_line, tier_finding

__all__ = ["clean_tree"]

CUT_P1_LINE = (
    '{"cites": ["saddle.gates.check_task_requirements", "question strength: dev-probe floor '
    "unmet; 0 of 7 candidate unit(s) judged; 0 of 0 example(s) judged: 0 pass, 0 code-wrong, "
    "0 question; not executable S-001: prose-only; not executable S-002: prose-only; not "
    "executable S-003: prose-only; not executable S-004: prose-only; not executable S-005: "
    'prose-only; not executable S-006: prose-only; not executable S-007: prose-only"], '
    '"detail": "P1 could not run: no example could be judged (0 example(s): 0 '
)
"""The task-requirements line a dogfood run of `saddle auto --extract-requirements`
sealed (exit 4), verbatim: 500 characters, cut inside `detail`."""

SURVIVORS = Finding(
    "mutation",
    2,
    "not-proven",
    "evidence-thin",
    "15 surviving mutant(s) on changed lines have no accepted reason (killed 328 of 355); "
    "shortlist:\n"
    + "\n".join(
        f"- m.py:{n} `x = {n}`: mutant m.x__mutmut_{n} (survived): -    x = {n}\n+    x = None"
        for n in range(10)
    ),
    ("saddle.gates.check_mutation_shortlist", "sampled n=355; score 92.4% vs 85.0%"),
)
"""A `--tier2 shortlist` mutation finding naming its survivors: not proven,
sealed with exit 0 like a pass."""

UNCOVERED = Finding(
    "coverage",
    1,
    "fail",
    "evidence-thin",
    "no test runs " + ", ".join(f"money.py:{n}" for n in range(30, 90)),
    ("saddle.gates.check_changed_line_coverage", "changed-lines=142"),
)


def old_line(finding: Finding) -> str:
    """A finding as the auditor sealed it before it fitted findings to the
    line: its whole JSON, which `build_span` then cuts at the cap."""
    return json.dumps(dataclasses.asdict(finding), sort_keys=True)


def seal(journal: Path, name: str, detail: str, exit_code: int) -> None:
    """One finding span, spelled as `Auditor._journal` spells it."""
    tier, gate = name.removeprefix("audit-tier").split(":", 1)
    append_span(
        journal,
        build_span(
            node_id="audit",
            argv=["saddle-audit", f"tier{tier}", gate, "k"],
            duration_ms=0,
            exit_code=exit_code,
            detail=detail,
            name=name,
        ),
    )


def rows(journal: Path) -> dict[str, Row]:
    return {row.key: row for row in compile_packet(journal).rows}


def test_the_dogfood_p1_line_reads_as_the_question_it_sealed(tmp_path: Path) -> None:
    journal = tmp_path / "proofs.jsonl"
    name = f"audit-tier1:{TASK_REQUIREMENTS}"
    seal(journal, name, CUT_P1_LINE, JOURNAL_QUESTION_EXIT)
    assert len(read_spans(journal)[0].detail) == MAX_SPAN_DETAIL_CHARS
    finding = tier_finding(name, CUT_P1_LINE, JOURNAL_QUESTION_EXIT)
    assert finding is not None
    assert finding.verdict == "question"
    assert finding.detail == (
        "P1 could not run: no example could be judged (0 example(s): 0" + LINE_CUT
    )
    audit = rows(journal)["audit"]
    assert audit.status == "question"
    assert audit.text == "0 of 1 finding passed, 1 need you."
    assert audit.items == (f"? {TASK_REQUIREMENTS}: tier 1, question: {finding.detail}",)


def test_an_old_cut_exit_zero_line_is_never_read_as_proven(tmp_path: Path) -> None:
    """Known-bad: a shortlist's surviving mutants, cut, read as a proven Mutation row."""
    journal = tmp_path / "proofs.jsonl"
    seal(journal, "audit-tier2:mutation", old_line(SURVIVORS), 0)
    line = read_spans(journal)[0].detail
    with pytest.raises(json.JSONDecodeError):
        json.loads(line)
    row = rows(journal)["mutation"]
    assert row.status == "not-proven"
    assert row.text.startswith(
        "tier 2, not-proven: 15 surviving mutant(s) on changed lines have no accepted reason"
    )
    assert row.text.endswith(LINE_CUT)


@pytest.mark.parametrize(
    ("verdict", "code", "read"),
    [
        ("fail", 1, "fail"),
        ("blocked", 2, "blocked"),
        ("question", JOURNAL_QUESTION_EXIT, "question"),
        ("not-proven", 0, "not-proven"),
        ("pass", 0, "not-proven"),  # exit 0 cannot tell a pass from a not-proven one
    ],
)
def test_an_old_cut_line_reads_the_verdict_its_exit_code_seals(
    verdict: str, code: int, read: str
) -> None:
    whole = old_line(dataclasses.replace(UNCOVERED, verdict=verdict))  # type: ignore[arg-type]
    cut = build_span(node_id="audit", argv=[], duration_ms=0, exit_code=code, detail=whole).detail
    finding = tier_finding("audit-tier1:coverage", cut, code)
    assert finding is not None
    assert (finding.gate, finding.tier, finding.verdict) == ("coverage", 1, read)
    assert finding.detail.startswith("no test runs money.py:30, money.py:31")
    assert finding.detail.endswith(LINE_CUT)
    # Without the code there is nothing sealed to read: as before.
    unread = tier_finding("audit-tier1:coverage", cut)
    assert unread is not None
    assert unread.verdict == "unreadable"


def test_an_old_cut_failure_is_a_failure_on_the_card(tmp_path: Path) -> None:
    journal = tmp_path / "proofs.jsonl"
    seal(journal, "audit-tier1:coverage", old_line(UNCOVERED), 1)
    line = session_line(read_spans(journal)[0])
    assert line is not None
    assert (line.mark, line.tone) == ("◇", "fail")
    assert line.text.startswith("audit tier 1 coverage fail · no test runs money.py:30,")
    item = rows(journal)["audit"].items[0]
    assert item.startswith("✗ coverage: tier 1, fail: no test runs money.py:30, money.py:31")


@pytest.mark.parametrize(
    ("line", "detail"),
    [
        # cut inside a \u escape: what is left of the escape is dropped, never more
        ('{"cites": [], "detail": "ab\\u00', "ab" + LINE_CUT),
        ('{"cites": [], "detail": "ab\\', "ab" + LINE_CUT),
        ('{"cites": [], "detail": "caf\\u00e9 au lait', "café au lait" + LINE_CUT),
        # the detail ended before the cut: it is whole, and says nothing of a cut
        ('{"cites": [], "detail": "whole", "gate": "cov', "whole"),
        # nothing of the detail survives: the raw line is all there is
        ('{"cites": ["saddle.gates.check_changed_line_cov', ""),
        ('{"cites": [], "detail": "', ""),
        # not the auditor's JSON (a raw control character): nothing is read
        ('{"cites": [], "detail": "a\nbcdefghij', ""),
    ],
)
def test_a_cut_line_gives_back_what_it_still_holds_of_its_detail(line: str, detail: str) -> None:
    finding = tier_finding("audit-tier1:coverage", line, 1)
    assert finding is not None
    assert finding.verdict == "fail"
    assert finding.detail == (detail or line)


def test_a_line_that_parses_but_names_no_verdict_reads_its_exit_code() -> None:
    finding = tier_finding("audit-tier1:ruff", '{"detail": "E501 line too long"}', 1)
    assert finding is not None
    assert (finding.verdict, finding.detail) == ("fail", "E501 line too long")
    other = tier_finding("audit-tier1:ruff", "not json", 3)  # no finding seals exit 3
    assert other is not None
    assert (other.verdict, other.detail) == ("unreadable", "not json")


# -- the writer: every finding's line parses, by construction --------------------------

VERDICTS = ("pass", "fail", "not-applicable", "blocked", "not-proven", "question")
SECRET = "pass" + "word=" + "hunter2"  # built from fragments for the leak guard
DETAILS = {
    "short": "2 of 3 lines run",
    "long": "no test runs " + ", ".join(f"money.py:{n}" for n in range(200)),
    "huge": "y" * 50_000,
    "escapes": 'quote " backslash \\ newline \n tab \t ' * 40,
    "unicode": "é∑ " * 300,  # json.dumps escapes each to six characters
    "secret-last": f"S105 hardcoded: {SECRET}",
    "secret-long": "z" * 460 + f" {SECRET}",
    "would-refuse": "S-001 expected [1], got [] [would refuse at full strength] " * 12,
}
CITES = {
    "gate": ("saddle.gates.check_ruff",),
    "basis": ("saddle.gates.check_ruff", "not executable S-009: environment; " * 60),
}


@pytest.mark.parametrize("verdict", VERDICTS)
@pytest.mark.parametrize("shape", sorted(DETAILS))
@pytest.mark.parametrize("cites", sorted(CITES))
def test_every_finding_line_parses_as_its_verdict(verdict: str, shape: str, cites: str) -> None:
    finding = Finding("ruff", 1, verdict, "code-wrong", DETAILS[shape], CITES[cites])  # type: ignore[arg-type]
    text = sealed_finding(finding)
    line = build_span(node_id="audit", argv=[], duration_ms=0, exit_code=0, detail=text).detail
    body = json.loads(line)
    assert (body["gate"], body["tier"], body["verdict"], body["reason"]) == (
        "ruff",
        1,
        verdict,
        "code-wrong",
    )
    assert body["cites"][0] == "saddle.gates.check_ruff"
    whole = old_line(finding)
    if len(whole) <= MAX_SPAN_DETAIL_CHARS and SECRET not in whole:
        assert text == whole  # a finding the line holds is sealed as before, byte for byte
    else:
        assert text != whole
    if body["detail"] != finding.detail and "would refuse at full strength" in finding.detail:
        assert body["detail"].endswith("[would refuse at full strength]")


def test_a_secret_last_in_a_short_detail_no_longer_breaks_its_line() -> None:
    """Known-bad: redaction's `name=value` rule ate the detail's closing quote."""
    finding = Finding("ruff", 1, "fail", "code-wrong", DETAILS["secret-last"], CITES["gate"])
    broken = build_span(
        node_id="audit", argv=[], duration_ms=0, exit_code=1, detail=old_line(finding)
    ).detail
    with pytest.raises(json.JSONDecodeError):
        json.loads(broken)
    sealed = build_span(
        node_id="audit", argv=[], duration_ms=0, exit_code=1, detail=sealed_finding(finding)
    ).detail
    assert json.loads(sealed)["verdict"] == "fail"
    assert SECRET not in sealed


def test_the_auditor_seals_long_findings_that_read_back(tmp_path: Path) -> None:
    journal = tmp_path / "proofs.jsonl"
    findings = (UNCOVERED, SURVIVORS, dataclasses.replace(UNCOVERED, gate="ruff", tier=1))
    auditor = Auditor(tmp_path, config=AuditorConfig(journal=journal))
    auditor._journal(Findings(tier=1, key="k", findings=findings), {})
    spans = read_spans(journal)
    assert [json.loads(s.detail)["verdict"] for s in spans] == ["fail", "not-proven", "fail"]
    assert all(len(s.detail) <= MAX_SPAN_DETAIL_CHARS for s in spans)
    assert all(SEALED_CUT in json.loads(s.detail)["detail"] for s in spans)
    assert verify_journal(journal) == []
    assert rows(journal)["mutation"].status == "not-proven"


BIG = "def h():\n" + "".join(f"    v{i} = {i}\n" for i in range(70)) + "    return v0\n"


def test_a_long_coverage_finding_keeps_every_line_in_the_packet(
    clean_tree: Path, tmp_path: Path
) -> None:
    """The real tier-1 audit of a tree whose new module no test runs: the
    finding names 72 lines, more than its ledger line can hold."""
    (clean_tree / "big.py").write_text(BIG)
    journal = tmp_path / "proofs.jsonl"
    result = Auditor(clean_tree, config=AuditorConfig(journal=journal)).tier1()
    coverage = next(f for f in result.findings if f.gate == "coverage")
    assert coverage.verdict == "fail"
    assert len(coverage_text.uncovered_lines(coverage.detail)) == 72
    span = next(s for s in read_spans(journal) if s.name == "audit-tier1:coverage")
    assert json.loads(span.detail)["verdict"] == "fail"  # the line parses: it was fitted
    assert SEALED_CUT in json.loads(span.detail)["detail"]
    assert verify_journal(journal) == []
    audit = rows(journal)["audit"]
    assert any(i.startswith("✗ coverage: tier 1, fail: no test runs big.py:") for i in audit.items)
    assert audit.summary.startswith(
        "Not proven by any test: 72 changed lines no test runs [record: detail names 72 lines; "
        "changed-lines="
    )
    assert "big.py h: 72 of 72 changed lines never run -- nothing exercises h" in audit.summary
