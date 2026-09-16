"""Tests for saddle.transcript: golden rendering of run transcripts."""

from __future__ import annotations

from saddle.gates import GateCheck
from saddle.transcript import NodeTranscript, RunTranscript, render_transcript


def test_render_pass_transcript_golden() -> None:
    run = RunTranscript(
        task="Add email validation.",
        started="2026-09-16T00:00:00+00:00",
        finished="2026-09-16T00:01:00+00:00",
        verdict="PASS",
        nodes=(
            NodeTranscript(
                node_id="n1",
                requirement_ids=("REQ-001", "REQ-002"),
                checks=(
                    GateCheck(name="tests", passed=True, detail="ok"),
                    GateCheck(name="coverage", passed=True, detail="100%"),
                ),
                proof_hash="ab12",
            ),
        ),
        journal_path="/tmp/proofs.jsonl",
    )
    assert render_transcript(run) == (
        "# Saddle slice transcript\n"
        "\n"
        "- Task: Add email validation.\n"
        "- Started: 2026-09-16T00:00:00+00:00\n"
        "- Finished: 2026-09-16T00:01:00+00:00\n"
        "- Verdict: PASS\n"
        "\n"
        "## Node n1\n"
        "\n"
        "- Requirements: REQ-001, REQ-002\n"
        "- Gate tests: PASS (ok)\n"
        "- Gate coverage: PASS (100%)\n"
        "- Proof: ab12\n"
        "\n"
        "## Journal\n"
        "\n"
        "- Path: /tmp/proofs.jsonl\n"
        "- Proven nodes: 1\n"
        "- Issues: none (chain verifies)\n"
    )


def test_render_fail_transcript_marks_failure() -> None:
    run = RunTranscript(
        task="Add email validation.",
        started="2026-09-16T00:00:00+00:00",
        finished="2026-09-16T00:01:00+00:00",
        verdict="FAIL",
        nodes=(
            NodeTranscript(
                node_id="n1",
                requirement_ids=("REQ-001",),
                checks=(GateCheck(name="tests", passed=False, detail="boom"),),
                proof_hash=None,
            ),
        ),
        journal_path="/tmp/proofs.jsonl",
    )
    text = render_transcript(run)
    assert "- Verdict: FAIL\n" in text
    assert "- Gate tests: FAIL (boom)\n" in text
    assert "- Proof: none\n" in text
    assert "- Proven nodes: 0\n" in text
    assert "- Issues: none (chain verifies)\n" in text
