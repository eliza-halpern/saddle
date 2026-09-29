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

from saddle.auditor import TASK_REQUIREMENTS, Finding
from saddle.journal import (
    JOURNAL_QUESTION_EXIT,
    MAX_SPAN_DETAIL_CHARS,
    append_span,
    build_span,
    read_spans,
)
from saddle.packet import Row, compile_packet
from saddle.transcript import LINE_CUT, session_line, tier_finding

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
