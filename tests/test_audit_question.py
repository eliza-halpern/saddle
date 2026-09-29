"""The auditor's `question` verdict: a finding a person must decide.

The contract, each half both ways: a `question` finding refuses nothing
(`Findings.passed`, tier 2 is not blocked by it, `finish` is accepted) and
never reads as a pass: `Findings.needs_you` carries it, its span exits 4,
the tiered CLI exits 4 with verdict `question`, the packet lists it as a
question, and a finish audit that carries one ends an E+A+F run stopped
"needs you: ...", never finished on it silently.
"""

from __future__ import annotations

import io
from pathlib import Path
from typing import cast

import pytest
from test_feed import FINISH, FakeAuditor, Reactive, call, repo, run, sidecar

from saddle import engine
from saddle.auditor import Auditor, AuditorConfig, Finding, Findings
from saddle.auto import AutoOptions, AutoResult, run_auto
from saddle.cli import AUDIT_EXIT_CODES, _report_tiered
from saddle.feed import AuditResult, render
from saddle.journal import read_spans, verify_journal
from saddle.packet import compile_packet
from saddle.transcript import session_line
from saddle.vllm import VllmClient

__all__ = ["repo"]  # the fixture, imported for pytest

ASK = Finding(
    "task-requirements", 1, "question", "code-wrong", "S-001: expected [1], got []", ("x",)
)
PASS = Finding("tests", 1, "pass", "code-wrong", "1 passed", ("x",))
FAIL = Finding("tests", 1, "fail", "code-wrong", "1 failed", ("x",))


def test_a_question_refuses_nothing_and_is_carried_by_needs_you() -> None:
    asked = Findings(tier=1, key="k", findings=(PASS, ASK))
    assert asked.passed
    assert asked.needs_you
    assert asked.to_dict()["needs_you"] is True
    assert Findings.from_dict(asked.to_dict()).needs_you
    # known-bad halves: a fail still refuses; a pass alone needs nobody
    assert not Findings(tier=1, key="k", findings=(FAIL, ASK)).passed
    plain = Findings(tier=1, key="k", findings=(PASS,))
    assert not plain.needs_you
    assert "needs_you" not in plain.to_dict()


def test_a_question_span_exits_four_never_zero(tmp_path: Path) -> None:
    journal = tmp_path / "j.jsonl"
    auditor = Auditor(tmp_path, config=AuditorConfig(journal=journal))
    auditor._journal(Findings(tier=1, key="k", findings=(PASS, ASK, FAIL)), {})
    exits = {s.name: s.exit_code for s in read_spans(journal)}
    assert exits["audit-tier1:task-requirements"] == 4
    assert exits["audit-tier1:tests"] == 1  # the last tests span, the fail
    assert verify_journal(journal) == []


@pytest.mark.parametrize(
    ("findings", "verdict"),
    [((PASS,), "accept"), ((PASS, ASK), "question"), ((ASK, FAIL), "refuse")],
)
def test_the_tiered_cli_says_question_with_its_own_exit_code(
    findings: tuple[Finding, ...], verdict: str
) -> None:
    out = io.StringIO()
    code = _report_tiered((Findings(tier=1, key="k", findings=findings),), False, out)
    assert code == AUDIT_EXIT_CODES[verdict]
    assert out.getvalue().endswith(f"verdict: {verdict}\n")
    assert AUDIT_EXIT_CODES["question"] not in (
        AUDIT_EXIT_CODES["accept"],
        AUDIT_EXIT_CODES["refuse"],
        AUDIT_EXIT_CODES["nothing-to-audit"],
    )


def test_render_lists_a_question_and_does_not_count_it_passed() -> None:
    text = render(AuditResult("finish", "t" * 12, (PASS, ASK)))
    assert text.startswith("[audit finish on tree tttttttttttt: PASS]")
    assert "(question for a person, does not refuse) task-requirements (tier 1): S-001" in text
    assert "(1 other check(s) passed or not applicable)" in text


class Asks(FakeAuditor):
    """Passes `tests`; tier 1 adds a question; seals the spans a real auditor would."""

    journal: Path | None = None

    def tier1(self, tree: Path | None = None) -> Findings:
        got = super().tier1(tree)
        found = Findings(tier=1, key="k1", findings=(*got.findings, ASK))
        if self.journal is not None:
            Auditor(Path("."), config=AuditorConfig(journal=self.journal))._journal(found, {})
        return found


def run_asking(repo: Path, arm: str) -> AutoResult:
    """`test_feed.run` with an `Asks` auditor that seals into the run's own ledger."""
    fake = Asks()

    def factory(repo: Path, baseline: str, config: AuditorConfig) -> Asks:
        fake.journal = config.journal
        return fake

    options = AutoOptions(
        task="make add add",
        repo=repo,
        run_id="q",
        arm=arm,  # type: ignore[arg-type]
        auditor_factory=factory,
    )
    return run_auto(options, cast(VllmClient, Reactive([[FIX], [FINISH]])))


FIX = call("edit_file", "e", path="calc.py", old="a - b", new="a + b")


def test_a_finish_audit_with_a_question_ends_the_run_needing_you(repo: Path) -> None:
    result = run_asking(repo, "E+A+F")
    assert result.outcome == "stopped"
    assert result.reason.startswith("needs you: the audit asks 1 question(s): task-requirements")
    assert ";" not in result.reason  # the packet's verdict line keeps it whole
    record = sidecar(result)
    assert record["finish_refusals"] == 0  # accepted: a question refuses nothing
    assert record["audit"]["needs_you"] is True
    finishes = [s.detail for s in read_spans(result.journal) if s.argv[:1] == ["finish"]]
    assert finishes[-1].startswith(engine.FINISH_QUESTION)
    packet = compile_packet(result.journal)
    # flip: was "stopped". The run ends needing you (spec D-6), neither
    # finished nor stopped on a fault; the card read "stopped" before.
    assert packet.verdict == "needs_you"
    assert packet.verdict_text.startswith("Needs you: the audit asks 1 question(s)")
    assert "did not finish" in packet.verdict_text
    audit = {r.key: r for r in packet.rows}["audit"]
    assert audit.status == "question"
    assert "1 need you" in audit.text
    assert audit.items == ("? task-requirements: tier 1, question: S-001: expected [1], got []",)
    lines = [session_line(e) for e in read_spans(result.journal)]
    asked = [x for x in lines if x is not None and "task-requirements question" in x.text]
    assert asked
    assert asked[0].tone == "ask"


def test_known_good_without_a_question_the_same_run_finishes(repo: Path) -> None:
    result, _ = run(repo, Reactive([[FIX], [FINISH]]), "E+A+F")
    assert result.outcome == "finished"
    assert "needs_you" not in sidecar(result)["audit"]


def test_arm_e_a_records_the_question_and_changes_nothing(repo: Path) -> None:
    result = run_asking(repo, "E+A")
    assert result.outcome == "finished"
    assert sidecar(result)["audit"]["needs_you"] is True
    packet = compile_packet(result.journal)
    assert packet.verdict_text.endswith("1 audit finding asked a question only you can answer.")


def test_the_stop_reason_is_capped_and_has_no_semicolon() -> None:
    reason = engine.needs_you_reason(["a; b" * 200, "c"])
    assert reason.startswith("needs you: the audit asks 2 question(s): a, b")
    assert ";" not in reason
    assert reason.endswith("...")
    assert len(reason) < 400
    assert engine.needs_you_reason(["x"]) == "needs you: the audit asks 1 question(s): x"
