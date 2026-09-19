"""Tests for saddle.transcript: golden rendering of run transcripts."""

from __future__ import annotations

from saddle.gates import GateCheck
from saddle.journal import GateOutput, ProofRecord, SpanRecord, build_record, build_span
from saddle.transcript import (
    MAX_THOUGHT_EXCERPT_CHARS,
    NodeTranscript,
    RunTranscript,
    is_run_end,
    render_event,
    render_journal_transcript,
    render_transcript,
)


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
    assert "- Attempts:" not in text


def test_render_transcript_shows_attempts_when_recovered() -> None:
    run = RunTranscript(
        task="Add email validation.",
        started="2026-09-16T00:00:00+00:00",
        finished="2026-09-16T00:01:00+00:00",
        verdict="PASS",
        nodes=(
            NodeTranscript(
                node_id="n1",
                requirement_ids=("REQ-001",),
                checks=(GateCheck(name="tests", passed=True, detail="ok"),),
                proof_hash="ab12",
                attempts=3,
            ),
        ),
        journal_path="/tmp/proofs.jsonl",
    )
    text = render_transcript(run)
    assert "- Requirements: REQ-001\n- Attempts: 3\n- Gate tests: PASS (ok)\n" in text


def test_render_journal_transcript_carries_record_attempts() -> None:
    record = build_record(
        evidence_id="e1",
        node_id="n1",
        diff="diff\n",
        parent_proofs=[],
        gate_outputs=[GateOutput(name="tests", passed=True, detail="ok")],
        requirement_ids=["REQ-001"],
        thinking="",
        attempts=2,
    )
    text = render_journal_transcript([record], [], "/tmp/proofs.jsonl")
    assert "- Attempts: 2\n" in text
    assert "- Issues: none (chain verifies)\n" in text


def _sealed(node_id: str, passed: bool, parents: list[str], thinking: str = "") -> ProofRecord:
    record = build_record(
        evidence_id=f"{node_id}#1",
        node_id=node_id,
        diff="diff",
        parent_proofs=parents,
        gate_outputs=[GateOutput(name="tests", passed=passed, detail="ok")],
        requirement_ids=["REQ-001"],
        thinking=thinking,
    )
    return record


def test_render_journal_transcript_golden() -> None:
    first = _sealed("n1", True, [], thinking="Fix f first.\nThen test it.")
    second = _sealed("n2", True, [first.record_hash])
    worker = build_span(
        node_id="n1",
        argv=[],
        duration_ms=30,
        exit_code=0,
        detail="",
        kind="agent",
        name="worker:n1",
    )
    spans = [
        _tool(["pytest", "test_n.py"], node_id="n1"),
        worker,
        _tool(["git", "apply", "-"], duration_ms=3, node_id="n2"),
    ]
    assert render_journal_transcript([first, second], spans, "/tmp/proofs.jsonl") == (
        "# Saddle slice transcript\n"
        "\n"
        "- Task: (unknown)\n"
        "- Started: (unknown)\n"
        "- Finished: (unknown)\n"
        "- Verdict: PASS\n"
        "\n"
        "## Node n1\n"
        "\n"
        "- Requirements: REQ-001\n"
        "- Gate tests: PASS (ok)\n"
        f"- Proof: {first.record_hash}\n"
        "- Timeline:\n"
        "  - thought: Fix f first.\n"
        "    Then test it.\n"
        "  - tool pytest: exit 0 in 12ms: pytest test_n.py\n"
        "\n"
        "## Node n2\n"
        "\n"
        "- Requirements: REQ-001\n"
        "- Gate tests: PASS (ok)\n"
        f"- Proof: {second.record_hash}\n"
        "- Timeline:\n"
        "  - tool git: exit 0 in 3ms: git apply -\n"
        "\n"
        "## Journal\n"
        "\n"
        "- Path: /tmp/proofs.jsonl\n"
        "- Proven nodes: 2\n"
        "- Issues: none (chain verifies)\n"
    )


def test_render_journal_transcript_recomputes_fail_verdict() -> None:
    record = _sealed("n1", False, [])
    text = render_journal_transcript([record], [], "/tmp/proofs.jsonl")
    assert "- Verdict: FAIL\n" in text
    assert "- Gate tests: FAIL (ok)\n" in text


def _tool(
    argv: list[str], duration_ms: int = 12, exit_code: int = 0, node_id: str = "n1"
) -> SpanRecord:
    return build_span(
        node_id=node_id, argv=argv, duration_ms=duration_ms, exit_code=exit_code, detail=""
    )


def _render_node(node: NodeTranscript) -> str:
    run = RunTranscript(
        task="t",
        started="s",
        finished="f",
        verdict="PASS",
        nodes=(node,),
        journal_path="j",
    )
    return render_transcript(run)


def test_render_timeline_golden() -> None:
    node = NodeTranscript(
        node_id="n1",
        requirement_ids=("REQ-001",),
        checks=(GateCheck(name="tests", passed=True, detail="ok"),),
        proof_hash="ab12",
        thinking="Fix f to return 2.\nAdd a test for it.",
        tool_spans=(
            _tool(["pytest", "test_n.py"]),
            _tool(["git", "apply", "-"], duration_ms=3),
        ),
    )
    assert _render_node(node) == (
        "# Saddle slice transcript\n"
        "\n"
        "- Task: t\n"
        "- Started: s\n"
        "- Finished: f\n"
        "- Verdict: PASS\n"
        "\n"
        "## Node n1\n"
        "\n"
        "- Requirements: REQ-001\n"
        "- Gate tests: PASS (ok)\n"
        "- Proof: ab12\n"
        "- Timeline:\n"
        "  - thought: Fix f to return 2.\n"
        "    Add a test for it.\n"
        "  - tool pytest: exit 0 in 12ms: pytest test_n.py\n"
        "  - tool git: exit 0 in 3ms: git apply -\n"
        "\n"
        "## Journal\n"
        "\n"
        "- Path: j\n"
        "- Proven nodes: 1\n"
        "- Issues: none (chain verifies)\n"
    )


def test_render_timeline_truncates_long_thinking() -> None:
    exact = _render_node(
        NodeTranscript(
            node_id="n1",
            requirement_ids=(),
            checks=(),
            proof_hash=None,
            thinking="x" * MAX_THOUGHT_EXCERPT_CHARS,
        )
    )
    assert f"  - thought: {'x' * MAX_THOUGHT_EXCERPT_CHARS}\n" in exact
    assert "..." not in exact
    over = _render_node(
        NodeTranscript(
            node_id="n1",
            requirement_ids=(),
            checks=(),
            proof_hash=None,
            thinking="y" * (MAX_THOUGHT_EXCERPT_CHARS + 1),
        )
    )
    assert f"  - thought: {'y' * MAX_THOUGHT_EXCERPT_CHARS}...\n" in over


def test_render_timeline_tools_without_thinking() -> None:
    text = _render_node(
        NodeTranscript(
            node_id="n1",
            requirement_ids=(),
            checks=(),
            proof_hash=None,
            tool_spans=(_tool(["pytest"], exit_code=1),),
        )
    )
    assert "- Timeline:\n" in text
    assert "  - thought:" not in text
    assert "  - tool pytest: exit 1 in 12ms: pytest\n" in text


def _agent_span(name: str, node_id: str = "n1", detail: str = "", exit_code: int = 0) -> SpanRecord:
    return build_span(
        node_id=node_id,
        argv=[],
        duration_ms=30,
        exit_code=exit_code,
        detail=detail,
        kind="agent",
        name=name,
    )


def test_is_run_end_matches_only_the_run_span() -> None:
    run = _agent_span("run", node_id="")
    assert is_run_end(run) is True
    assert is_run_end(_sealed("n1", True, [])) is False
    assert is_run_end(_tool(["pytest"])) is False
    assert is_run_end(_agent_span("worker:n1")) is False
    assert is_run_end(_agent_span("run", node_id="n1")) is False
    assert is_run_end(_tool(["run"])) is False


def test_render_event_tool_span() -> None:
    assert render_event(_tool(["pytest", "test_n.py"])) == [
        "[n1] tool pytest: exit 0 in 12ms: pytest test_n.py"
    ]


def test_render_event_proof_with_thought() -> None:
    record = _sealed("n1", True, [], thinking="Fix f first.\nThen test it.")
    assert render_event(record) == [
        f"[n1] sealed {record.record_hash[:8]} (1/1 gates passed)",
        "[n1] thought: Fix f first.",
        "    Then test it.",
    ]


def test_render_event_proof_counts_failed_gates() -> None:
    record = build_record(
        evidence_id="n1#1",
        node_id="n1",
        diff="diff",
        parent_proofs=[],
        gate_outputs=[
            GateOutput(name="tests", passed=True, detail="ok"),
            GateOutput(name="ruff", passed=False, detail="dirty"),
        ],
        requirement_ids=["REQ-001"],
        thinking="",
    )
    assert render_event(record) == [f"[n1] sealed {record.record_hash[:8]} (1/2 gates passed)"]


def test_render_event_agent_spans() -> None:
    assert render_event(_agent_span("worker:n1")) == ["[n1] worker:n1: exit 0 in 30ms"]
    assert render_event(_agent_span("worker:n1", detail="boom", exit_code=1)) == [
        "[n1] worker:n1: exit 1 in 30ms: boom"
    ]
    run = _agent_span("run", node_id="", detail="1 proven, 0 failed, 0 undispatched")
    assert render_event(run) == ["run: exit 0 in 30ms: 1 proven, 0 failed, 0 undispatched"]


def test_render_journal_transcript_empty_journal_is_fail() -> None:
    """A journal that proved nothing is FAIL, never a vacuous PASS.

    Regression: `all()` over no checks is true, so a run whose first worker
    call died before sealing any proof audited as a success.
    """
    text = render_journal_transcript([], [], "/tmp/proofs.jsonl")
    assert "- Verdict: FAIL\n" in text
    assert "- Proven nodes: 0\n" in text


def test_render_journal_transcript_node_without_gate_outputs_is_fail() -> None:
    """A sealed record carrying no gate outputs proves nothing either."""
    record = build_record(
        evidence_id="e1",
        node_id="n1",
        diff="diff\n",
        parent_proofs=[],
        gate_outputs=[],
        requirement_ids=["REQ-001"],
        thinking="",
    )
    assert "- Verdict: FAIL\n" in render_journal_transcript([record], [], "/tmp/proofs.jsonl")


def _run_span(exit_code: int, detail: str) -> SpanRecord:
    return build_span(
        node_id="",
        argv=[],
        duration_ms=1,
        exit_code=exit_code,
        detail=detail,
        kind="agent",
        name="run",
    )


def test_render_journal_transcript_failed_run_span_is_fail() -> None:
    """T3-21 known-bad: the smoke journal's shape -- one sealed proof, a run
    span saying a second node failed -- re-rendered as PASS, because only
    proven nodes have records and the run span was never read."""
    record = _sealed("n1", True, [])
    spans = [_run_span(1, "1 proven, 1 failed, 0 undispatched, merge exit 2")]
    text = render_journal_transcript([record], spans, "/tmp/proofs.jsonl")
    assert "- Verdict: FAIL\n" in text
    assert "- Proven nodes: 1\n" in text


def test_render_journal_transcript_passed_run_span_stays_pass() -> None:
    """T3-21 known-good: a run span that exited 0 changes nothing."""
    record = _sealed("n1", True, [])
    spans = [_run_span(0, "1 proven, 0 failed, 0 undispatched, merge exit 0")]
    assert "- Verdict: PASS\n" in render_journal_transcript([record], spans, "/tmp/proofs.jsonl")


def test_render_journal_transcript_last_run_span_governs() -> None:
    """A resumed journal carries one run span per run; the latest is the verdict."""
    record = _sealed("n1", True, [])
    spans = [
        _run_span(1, "1 proven, 1 failed, 0 undispatched, merge exit 2"),
        _run_span(0, "2 proven, 0 failed, 0 undispatched, merge exit 0"),
    ]
    assert "- Verdict: PASS\n" in render_journal_transcript([record], spans, "/tmp/proofs.jsonl")
