"""Tests for saddle.journal: sealing, verification, crash rebuild."""

from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path

import pytest

from saddle.dag import Node
from saddle.gates import GateCheck, Tier1Result
from saddle.journal import (
    MAX_THINKING_CHARS,
    GateOutput,
    ProofRecord,
    SpanRecord,
    SpanRecorder,
    append_record,
    append_span,
    build_from_gate,
    build_record,
    build_span,
    read_entries,
    read_records,
    read_spans,
    rebuild_proven,
    scrub_thinking,
    tool_spans_by_node,
    tool_spans_for_node,
    verify_journal,
)


def test_build_record_seals_independently_verifiable_hash() -> None:
    outputs = [GateOutput(name="tests", passed=True, detail="ok")]
    record = build_record(
        evidence_id="e1",
        node_id="n1",
        diff="diff --git a/n.py b/n.py\n",
        parent_proofs=[],
        gate_outputs=outputs,
        requirement_ids=["REQ-001"],
        thinking="",
    )
    payload = {
        "record_type": "proof",
        "evidence_id": "e1",
        "node_id": "n1",
        "diff_hash": hashlib.sha256(b"diff --git a/n.py b/n.py\n").hexdigest(),
        "parent_proofs": [],
        "gate_outputs": [{"name": "tests", "passed": True, "detail": "ok"}],
        "requirement_ids": ["REQ-001"],
        "thinking": "",
        "attempts": 1,
    }
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    assert record.record_hash == hashlib.sha256(canonical.encode()).hexdigest()
    assert record.diff_hash == payload["diff_hash"]


def test_scrub_thinking_redacts_secrets() -> None:
    key = "sk-" + "f" * 12
    bearer = "Bearer " + "g" * 8
    aws = "AKIA" + "H" * 16
    text = f"key {key} and {bearer}, password=hunter2, {aws}"
    assert scrub_thinking(text) == ("key *** and Bearer ***, password=***, ***")


def test_scrub_thinking_caps_length() -> None:
    text = "t" * (MAX_THINKING_CHARS + 7)
    assert scrub_thinking(text) == "t" * MAX_THINKING_CHARS + "\n[truncated 7 chars]"


def test_scrub_thinking_leaves_short_clean_text_alone() -> None:
    assert scrub_thinking("I will extract the helper.") == "I will extract the helper."


def test_scrub_thinking_keeps_exactly_max_chars() -> None:
    assert scrub_thinking("t" * MAX_THINKING_CHARS) == "t" * MAX_THINKING_CHARS


def test_build_record_seals_thinking_into_hash() -> None:
    record = build_record(
        evidence_id="e1",
        node_id="n1",
        diff="diff\n",
        parent_proofs=[],
        gate_outputs=[GateOutput(name="tests", passed=True, detail="ok")],
        requirement_ids=["REQ-001"],
        thinking="extract the helper",
    )
    assert record.thinking == "extract the helper"
    payload = {
        "record_type": "proof",
        "evidence_id": "e1",
        "node_id": "n1",
        "diff_hash": hashlib.sha256(b"diff\n").hexdigest(),
        "parent_proofs": [],
        "gate_outputs": [{"name": "tests", "passed": True, "detail": "ok"}],
        "requirement_ids": ["REQ-001"],
        "thinking": "extract the helper",
        "attempts": 1,
    }
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    assert record.record_hash == hashlib.sha256(canonical.encode()).hexdigest()


def test_build_record_scrubs_thinking_before_sealing() -> None:
    record = build_record(
        evidence_id="e1",
        node_id="n1",
        diff="diff\n",
        parent_proofs=[],
        gate_outputs=[GateOutput(name="tests", passed=True, detail="ok")],
        requirement_ids=["REQ-001"],
        thinking="t" * (MAX_THINKING_CHARS + 3),
    )
    assert record.thinking == "t" * MAX_THINKING_CHARS + "\n[truncated 3 chars]"


def _record(
    node_id: str = "n1", parents: list[str] | None = None, thinking: str = ""
) -> ProofRecord:
    return build_record(
        evidence_id=f"e-{node_id}",
        node_id=node_id,
        diff=f"diff {node_id}\n",
        parent_proofs=parents if parents is not None else [],
        gate_outputs=[GateOutput(name="tests", passed=True, detail="ok")],
        requirement_ids=["REQ-001"],
        thinking=thinking,
    )


def test_append_record_creates_dirs_and_round_trips(tmp_path: Path) -> None:
    path = tmp_path / "sub" / "deep" / "proofs.jsonl"
    record = _record()
    append_record(path, record)
    append_record(path, _record("n2", [record.record_hash]))
    lines = path.read_text().splitlines()
    assert len(lines) == 2
    assert ProofRecord.model_validate(json.loads(lines[0])) == record
    assert lines[0] == json.dumps(record.model_dump(), sort_keys=True)


def test_verify_clean_chain_by_recomputation(tmp_path: Path) -> None:
    path = tmp_path / "proofs.jsonl"
    parent = _record("n1")
    append_record(path, parent)
    append_record(path, _record("n2", [parent.record_hash]))
    assert verify_journal(path) == []


def test_verify_pre_attempts_record_still_verifies(tmp_path: Path) -> None:
    path = tmp_path / "proofs.jsonl"
    record = _record("n1")
    legacy = record.model_dump(exclude={"record_hash", "attempts"})
    legacy_hash = hashlib.sha256(
        json.dumps(legacy, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    path.write_text(json.dumps({**legacy, "record_hash": legacy_hash}, sort_keys=True) + "\n")
    assert verify_journal(path) == []
    assert read_records(path)[0].attempts == 1


def test_verify_tampered_record_fails(tmp_path: Path) -> None:
    path = tmp_path / "proofs.jsonl"
    append_record(path, _record("n1"))
    raw = json.loads(path.read_text())
    raw["gate_outputs"][0]["passed"] = False
    path.write_text(json.dumps(raw) + "\n")
    (issue,) = verify_journal(path)
    assert issue.code == "bad-hash"
    assert issue.line == 1
    assert "n1" in issue.message


def test_verify_unknown_parent_fails(tmp_path: Path) -> None:
    path = tmp_path / "proofs.jsonl"
    append_record(path, _record("n2", ["0" * 64]))
    (issue,) = verify_journal(path)
    assert issue.code == "unknown-parent"
    assert issue.line == 1
    assert issue.message == "node 'n2' cites unknown parent proof"


def test_verify_unparseable_and_invalid_lines_fail(tmp_path: Path) -> None:
    garbage = tmp_path / "garbage.jsonl"
    garbage.write_text("not json\n")
    (unparseable,) = verify_journal(garbage)
    assert unparseable.code == "unparseable-line"
    assert unparseable.line == 1
    assert unparseable.message == "line is not valid JSON"
    wrong_shape = tmp_path / "shape.jsonl"
    wrong_shape.write_text("{}\n")
    (invalid,) = verify_journal(wrong_shape)
    assert invalid.code == "invalid-record"
    assert invalid.line == 1
    assert invalid.message == "line is not a proof record"


def test_verify_collects_every_line_issue_without_stopping(tmp_path: Path) -> None:
    path = tmp_path / "proofs.jsonl"
    tampered = _record("n3").model_dump()
    tampered["gate_outputs"][0]["passed"] = False
    orphan = _record("n4", ["0" * 64]).model_dump()
    path.write_text(
        "garbage\n{}\n" + json.dumps(tampered) + "\n" + json.dumps(orphan) + "\nmore garbage\n"
    )
    found = [(issue.code, issue.line) for issue in verify_journal(path)]
    assert found == [
        ("unparseable-line", 1),
        ("invalid-record", 2),
        ("bad-hash", 3),
        ("unknown-parent", 4),
        ("unparseable-line", 5),
    ]


def test_verify_torn_tail_discarded_prefix_valid(tmp_path: Path) -> None:
    path = tmp_path / "proofs.jsonl"
    append_record(path, _record("n1"))
    with path.open("a", encoding="utf-8") as handle:
        handle.write('{"evidence_id": "e-torn", "half-writ')
    (issue,) = verify_journal(path)
    assert issue.code == "torn-tail"
    assert issue.line == 2
    assert issue.message == "unterminated tail line discarded"


def test_verify_missing_and_empty_journal_valid(tmp_path: Path) -> None:
    assert verify_journal(tmp_path / "nope.jsonl") == []
    empty = tmp_path / "empty.jsonl"
    empty.write_text("")
    assert verify_journal(empty) == []


def test_rebuild_proven_maps_nodes_to_hashes(tmp_path: Path) -> None:
    path = tmp_path / "proofs.jsonl"
    parent = _record("n1")
    append_record(path, parent)
    child = _record("n2", [parent.record_hash])
    append_record(path, child)
    assert rebuild_proven(path) == {"n1": parent.record_hash, "n2": child.record_hash}
    assert rebuild_proven(tmp_path / "missing.jsonl") == {}
    assert read_records(path) == [parent, child]


def test_rebuild_proven_refuses_corruption_keeps_torn_tail(tmp_path: Path) -> None:
    bad = tmp_path / "bad.jsonl"
    append_record(bad, _record("n1"))
    with bad.open("a", encoding="utf-8") as handle:
        handle.write("garbage\nmore garbage\n")
    expected = (
        f"journal {str(bad)!r} failed verification: "
        "unparseable-line@line 2, unparseable-line@line 3"
    )
    with pytest.raises(ValueError, match=re.escape(expected)):
        rebuild_proven(bad)
    torn = tmp_path / "torn.jsonl"
    first = _record("n1")
    append_record(torn, first)
    second = _record("n2", [first.record_hash])
    append_record(torn, second)
    with torn.open("a", encoding="utf-8") as handle:
        handle.write('{"half": ')
    assert rebuild_proven(torn) == {"n1": first.record_hash, "n2": second.record_hash}


def test_build_from_gate_maps_verdict_to_record(tmp_path: Path) -> None:
    node = Node.model_validate(
        {
            "id": "n1",
            "dependencies": [],
            "task_prompt": "Do n1.",
            "requirement_ids": ["REQ-001"],
            "execution_constraints": {
                "reasoning_budget": "low",
                "allowed_tools": ["read_file"],
                "max_context_tokens": 8000,
            },
            "deterministic_gate": {
                "test_command": "pytest tests/test_n1.py",
                "changed_line_coverage_min": 100.0,
                "red_phase_required": True,
                "mutation_sample": {
                    "scope": "changed-lines",
                    "max_mutants": 10,
                    "kill_threshold": 85.0,
                },
            },
        }
    )
    result = Tier1Result(
        node_id="n1",
        passed=True,
        checks=(GateCheck(name="tests", passed=True, detail="ok"),),
    )
    parent = _record("n0")
    path = tmp_path / "proofs.jsonl"
    append_record(path, parent)
    record = build_from_gate(
        node, "diff n1\n", result, [parent.record_hash], "e1", thinking="why n1"
    )
    assert record.node_id == "n1"
    assert record.attempts == 1
    assert record.thinking == "why n1"
    assert record.requirement_ids == ["REQ-001"]
    assert record.parent_proofs == [parent.record_hash]
    assert record.gate_outputs == [GateOutput(name="tests", passed=True, detail="ok")]
    append_record(path, record)
    assert verify_journal(path) == []


def test_kill_minus_9_mid_run_rebuilds_state(tmp_path: Path) -> None:
    path = tmp_path / "proofs.jsonl"
    src = str(Path(__file__).parent.parent / "src")
    driver = (
        "import sys;"
        "from pathlib import Path;"
        "from saddle.journal import GateOutput, append_record, build_record;"
        "p = Path(sys.argv[1]);"
        "out = GateOutput(name='tests', passed=True, detail='ok');"
        "i = 0;\nwhile True:"
        " r = build_record(evidence_id=f'e{i}', node_id=f'n{i}', diff=f'd{i}',"
        " parent_proofs=[], gate_outputs=[out], requirement_ids=['REQ-001'],"
        " thinking='');"
        " append_record(p, r);"
        " i += 1"
    )
    env = {**os.environ, "PYTHONPATH": src}
    # Pin the driver's cwd to the project tree: under mutmut it imports
    # trampolined code whose config lookup searches upward from cwd.
    proc = subprocess.Popen(
        [sys.executable, "-c", driver, str(path)],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        text=True,
        env=env,
        cwd=Path(__file__).parent.parent,
    )
    try:
        time.sleep(0.5)
    finally:
        proc.kill()
    proc.wait(timeout=10)
    hard = [issue for issue in verify_journal(path) if issue.code != "torn-tail"]
    assert hard == []
    proven = rebuild_proven(path)
    assert len(proven) > 0
    assert all(key.startswith("n") for key in proven)


def _span(node_id: str = "n1") -> SpanRecord:
    return build_span(
        node_id=node_id,
        argv=["pytest", "test_n.py"],
        duration_ms=12,
        exit_code=0,
        detail="",
    )


def test_build_span_seals_name_and_args_hash() -> None:
    span = build_span(
        node_id="n1",
        argv=["/usr/bin/pytest", "test_n.py"],
        duration_ms=12,
        exit_code=0,
        detail="",
    )
    assert span.name == "pytest"
    assert span.record_type == "span"
    expected_args = json.dumps(["/usr/bin/pytest", "test_n.py"])
    assert span.args_hash == hashlib.sha256(expected_args.encode()).hexdigest()
    assert span.argv == ["/usr/bin/pytest", "test_n.py"]


def test_build_span_empty_argv_names_unknown() -> None:
    span = build_span(node_id="n1", argv=[], duration_ms=0, exit_code=1, detail="")
    assert span.name == "?"


def test_build_span_scrubs_secrets_and_caps_detail() -> None:
    secret = "sk-" + "f" * 12
    span = build_span(
        node_id="n1",
        argv=["run", secret],
        duration_ms=1,
        exit_code=0,
        detail="e" * 600,
    )
    assert span.argv == ["run", "***"]
    assert len(span.detail) == 500


def test_spans_round_trip_beside_proofs(tmp_path: Path) -> None:
    path = tmp_path / "proofs.jsonl"
    span = _span()
    proof = _record()
    append_span(path, span)
    append_record(path, proof)
    assert verify_journal(path) == []
    assert read_spans(path) == [span]
    assert read_records(path) == [proof]
    lines = path.read_text().splitlines()
    assert lines[0] == json.dumps(span.model_dump(), sort_keys=True)


def test_verify_catches_tampered_span(tmp_path: Path) -> None:
    path = tmp_path / "proofs.jsonl"
    append_span(path, _span())
    text = path.read_text()
    path.write_text(text.replace('"exit_code": 0', '"exit_code": 1'))
    (issue,) = verify_journal(path)
    assert issue.code == "bad-hash"


def test_verify_rejects_malformed_span_line(tmp_path: Path) -> None:
    path = tmp_path / "proofs.jsonl"
    path.write_text('{"record_type": "span", "node_id": "n1"}\n')
    (issue,) = verify_journal(path)
    assert issue.code == "invalid-record"
    assert issue.message == "line is not a span record"


def test_recorder_appends_node_linked_span(tmp_path: Path) -> None:
    path = tmp_path / "proofs.jsonl"
    recorder = SpanRecorder(path=path, node_id="n7")
    recorder.record(argv=["pytest", "test_n.py"], duration_ms=3, exit_code=0, detail="")
    (span,) = read_spans(path)
    assert span.node_id == "n7"
    assert span.name == "pytest"
    assert span.duration_ms == 3
    assert span.exit_code == 0


def _agent(node_id: str, name: str, parent_id: str | None = None) -> SpanRecord:
    return build_span(
        node_id=node_id,
        argv=[],
        duration_ms=5,
        exit_code=0,
        detail="",
        kind="agent",
        name=name,
        parent_id=parent_id,
    )


def test_agent_spans_link_parent_to_child(tmp_path: Path) -> None:
    path = tmp_path / "proofs.jsonl"
    run = _agent("", "run")
    worker = _agent("n1", "worker:n1", parent_id=run.span_id)
    sub = _agent("n1", "subagent:fixup", parent_id=worker.span_id)
    tool = build_span(
        node_id="n1",
        argv=["ruff", "check"],
        duration_ms=1,
        exit_code=0,
        detail="",
        parent_id=sub.span_id,
    )
    for span in (tool, sub, worker, run):
        append_span(path, span)
    assert verify_journal(path) == []
    assert [span.name for span in read_spans(path)] == [
        "ruff",
        "subagent:fixup",
        "worker:n1",
        "run",
    ]
    assert tool.parent_id == sub.span_id
    assert run.parent_id is None


def test_orphan_span_fails_verification(tmp_path: Path) -> None:
    path = tmp_path / "proofs.jsonl"
    append_span(path, _agent("n1", "worker:n1", parent_id="0" * 32))
    (issue,) = verify_journal(path)
    assert issue.code == "orphan-span"
    assert issue.line == 1
    assert "0" * 32 in issue.message


def test_recorder_parents_tool_spans(tmp_path: Path) -> None:
    path = tmp_path / "proofs.jsonl"
    run = _agent("", "run")
    append_span(path, run)
    recorder = SpanRecorder(path=path, node_id="n1", parent_id=run.span_id)
    recorder.record(argv=["pytest"], duration_ms=1, exit_code=0, detail="")
    (_, tool) = read_spans(path)
    assert tool.parent_id == run.span_id


def test_tool_spans_by_node_groups_tools_in_order() -> None:
    first = _span("n1")
    second = _span("n2")
    third = _span("n1")
    run = _agent("", "run")
    worker = _agent("n1", "worker:n1", parent_id=run.span_id)
    grouped = tool_spans_by_node([first, run, second, worker, third])
    assert grouped == {"n1": (first, third), "n2": (second,)}


def test_tool_spans_by_node_drops_agent_spans() -> None:
    assert tool_spans_by_node([]) == {}
    assert tool_spans_by_node([_agent("", "run")]) == {}


def test_tool_spans_for_node_returns_tuple_or_empty() -> None:
    first = _span("n1")
    grouped = tool_spans_by_node([first])
    assert tool_spans_for_node(grouped, "n1") == (first,)
    assert tool_spans_for_node(grouped, "n9") == ()


def test_read_entries_returns_proofs_and_spans_in_order(tmp_path: Path) -> None:
    path = tmp_path / "proofs.jsonl"
    record = _record("n1")
    tool = _span("n1")
    child = _record("n2", [record.record_hash])
    worker = _agent("n1", "worker:n1")
    append_record(path, record)
    append_span(path, tool)
    append_record(path, child)
    append_span(path, worker)
    assert read_entries(path) == [record, tool, child, worker]
    assert read_entries(tmp_path / "missing.jsonl") == []


def test_read_entries_refuses_corruption_tolerates_torn_tail(tmp_path: Path) -> None:
    bad = tmp_path / "bad.jsonl"
    append_record(bad, _record("n1"))
    with bad.open("a", encoding="utf-8") as handle:
        handle.write("garbage\n")
    with pytest.raises(ValueError, match="unparseable-line@line 2"):
        read_entries(bad)
    torn = tmp_path / "torn.jsonl"
    record = _record("n1")
    append_record(torn, record)
    with torn.open("a", encoding="utf-8") as handle:
        handle.write('{"half": ')
    assert read_entries(torn) == [record]
