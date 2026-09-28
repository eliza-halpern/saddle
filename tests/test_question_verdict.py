"""`saddle verify` re-renders a rule D QUESTION run as QUESTION, not FAIL.

The journals under tests/fixtures/question_run/ were sealed by an earlier rule D
tree (b6fd1fa)'s own `slice._seal_run` (by a one-off generator script), one
per case of its test_rule_d_run_question_is_the_verdict_only_when_every_failure_is_a_question.
Only the run span's exit code (4) and detail ("halted on a question") are read;
nothing from that tree is imported here.
"""

from __future__ import annotations

import io
import json
import shutil
from pathlib import Path

import pytest
from test_packet import BUGGY, FIX, TEST, asking_auditor, call, git, run

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
        # stated, not only FAIL: the run stopped at its deadline (the sealed
        # verdict word stays FAIL; the parenthesis names the stop)
        (
            "question_deadline",
            "FAIL (stopped at its deadline, before finishing: "
            "0 proven, 0 failed, 1 halted on a question, 1 undispatched)",
        ),
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


# -- an autonomous run: the ledger's integrity and the run's outcome, apart ----


def _repo(tmp_path: Path) -> Path:
    root = tmp_path / "repo"
    (root / "tests").mkdir(parents=True)
    (root / "calc.py").write_text(BUGGY)
    (root / "tests" / "test_calc.py").write_text(TEST)
    (root / "pyproject.toml").write_text(
        '[tool.pytest.ini_options]\ntestpaths = ["tests"]\npythonpath = ["."]\n'
    )
    git(root, "init", "-q", "-b", "main")
    git(root, "add", "-A")
    git(root, "commit", "-q", "-m", "init")
    return root


def _verify(journal: Path, anchor: Path | None = None) -> tuple[int, str]:
    out = io.StringIO()
    return run_verify(journal, stdout=out, anchor=anchor), out.getvalue()


def _cut_before(journal: Path, marker: str) -> Path:
    """The run's ledger as it stood before its first line holding `marker`."""
    lines = journal.read_text().splitlines()
    cut = next(i for i, line in enumerate(lines) if marker in line)
    trimmed = journal.with_name("trimmed.jsonl")
    trimmed.write_text("\n".join(lines[:cut]) + "\n")
    return trimmed


@pytest.mark.parametrize(
    ("how", "verdict"),
    [
        ("question", "STOPPED (needs you: Should add(0, 0) be 0?)"),
        ("budget", "STOPPED (token budget exhausted: "),
        (
            "waiting",
            "NEEDS YOU (no outcome yet: the run is waiting on your answer: Should add(0, 0) be 0?)",
        ),
        # known-bad half: in flight with no question asked, or with the
        # question answered, is still "no outcome", never "needs you"
        ("in-flight", "NO OUTCOME (no auto:finished or auto:stopped span)"),
        ("answered", "NO OUTCOME (no auto:finished or auto:stopped span)"),
    ],
)
def test_a_run_that_did_not_finish_verifies_intact_with_its_outcome_stated(
    tmp_path: Path, how: str, verdict: str
) -> None:
    root = _repo(tmp_path)
    if how == "budget":
        reading = [call("read_file", "r", path="calc.py")]
        result = run(root, [], tail=reading, opts={"token_budget": 1})
    else:
        result = run(root, FIX, **asking_auditor("yes" if how == "answered" else None))
    journal = result.journal
    if how == "answered":
        journal = _cut_before(journal, '"auto:finished"')
    if how == "waiting":
        journal = _cut_before(journal, '"auto:stopped"')
    if how == "in-flight":
        journal = _cut_before(journal, '"question"')
    code, text = _verify(journal, anchor=root if how in ("question", "budget") else None)
    assert code == 0, text
    assert text.startswith("OK: ")
    assert f"- Verdict: {verdict}" in text


def test_a_broken_ledger_of_a_question_stopped_run_fails_verify(tmp_path: Path) -> None:
    """Known-bad: the same run with one record rewritten is a broken ledger, exit 1."""
    result = run(_repo(tmp_path), FIX, **asking_auditor(None))
    lines = result.journal.read_text().splitlines()
    index = next(i for i, line in enumerate(lines) if '"question"' in line)
    record = json.loads(lines[index])
    record["detail"] = "Should add(0, 0) be 1?"
    lines[index] = json.dumps(record)
    result.journal.write_text("\n".join(lines) + "\n")
    code, text = _verify(result.journal)
    assert code == 1
    assert not text.startswith("OK: ")
    assert "- Verdict:" not in text
