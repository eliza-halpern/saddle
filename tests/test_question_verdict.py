"""`saddle verify` re-renders a rule D QUESTION run as QUESTION, not FAIL.

The journals under tests/fixtures/question_run/ were sealed by an earlier rule D
tree (b6fd1fa)'s own `slice._seal_run` (by a one-off generator script), one
per case of its test_rule_d_run_question_is_the_verdict_only_when_every_failure_is_a_question.
Only the run span's exit code (4) and detail ("halted on a question") are read;
nothing from that tree is imported here.
"""

from __future__ import annotations

import io
import shutil
from pathlib import Path

import pytest

from saddle.cli import run_verify
from saddle.journal import GateOutput, ProofRecord, build_record, read_spans
from saddle.transcript import render_journal_transcript

FIXTURES = Path(__file__).parent / "fixtures" / "question_run"


def verify(tmp_path: Path, name: str) -> tuple[int, str]:
    journal = tmp_path / "proofs.jsonl"
    shutil.copy(FIXTURES / f"{name}.jsonl", journal)
    out = io.StringIO()
    return run_verify(journal, stdout=out), out.getvalue()


@pytest.mark.parametrize(
    ("name", "verdict"),
    [
        ("question", "QUESTION"),
        ("two_questions", "QUESTION"),
        # known-bad: each keeps FAIL, as the sealed run verdict does
        ("question_and_gate", "FAIL"),
        ("question_red_merge", "FAIL"),
        ("question_deadline", "FAIL"),
        ("gate", "FAIL"),
    ],
)
def test_verify_renders_the_sealed_question_run_with_its_own_verdict(
    tmp_path: Path, name: str, verdict: str
) -> None:
    code, text = verify(tmp_path, name)
    assert code == 0  # the ledger verifies either way; only the rendered verdict differs
    assert f"- Verdict: {verdict}\n" in text


def _proof(passed: bool) -> ProofRecord:
    return build_record(
        evidence_id="n1#1",
        node_id="n1",
        diff="diff",
        parent_proofs=[],
        gate_outputs=[GateOutput(name="tests", passed=passed, detail="ok")],
        requirement_ids=["REQ-001"],
        thinking="",
    )


@pytest.mark.parametrize(("passed", "verdict"), [(True, "QUESTION"), (False, "FAIL")])
def test_a_question_run_with_a_failed_sealed_check_is_fail(passed: bool, verdict: str) -> None:
    """Known-bad: exit 4 does not excuse a sealed gate failure; a clean proof keeps QUESTION."""
    spans = read_spans(FIXTURES / "question.jsonl")
    text = render_journal_transcript([_proof(passed)], spans, "j")
    assert f"- Verdict: {verdict}\n" in text


def test_exit_4_without_the_question_detail_is_fail() -> None:
    """Known-bad: the exit code alone is not the sealed shape; both halves are read."""
    (span,) = read_spans(FIXTURES / "question.jsonl")
    other = span.model_copy(update={"detail": "0 proven, 1 failed, 1 undispatched"})
    assert "- Verdict: FAIL\n" in render_journal_transcript([], [other], "j")
