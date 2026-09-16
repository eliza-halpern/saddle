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
    GateOutput,
    ProofRecord,
    append_record,
    build_from_gate,
    build_record,
    read_records,
    rebuild_proven,
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
    )
    payload = {
        "evidence_id": "e1",
        "node_id": "n1",
        "diff_hash": hashlib.sha256(b"diff --git a/n.py b/n.py\n").hexdigest(),
        "parent_proofs": [],
        "gate_outputs": [{"name": "tests", "passed": True, "detail": "ok"}],
        "requirement_ids": ["REQ-001"],
    }
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    assert record.record_hash == hashlib.sha256(canonical.encode()).hexdigest()
    assert record.diff_hash == payload["diff_hash"]


def _record(node_id: str = "n1", parents: list[str] | None = None) -> ProofRecord:
    return build_record(
        evidence_id=f"e-{node_id}",
        node_id=node_id,
        diff=f"diff {node_id}\n",
        parent_proofs=parents if parents is not None else [],
        gate_outputs=[GateOutput(name="tests", passed=True, detail="ok")],
        requirement_ids=["REQ-001"],
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
                "max_context_tokens": 5000,
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
    record = build_from_gate(node, "diff n1\n", result, [parent.record_hash], "e1")
    assert record.node_id == "n1"
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
        " parent_proofs=[], gate_outputs=[out], requirement_ids=['REQ-001']);"
        " append_record(p, r);"
        " i += 1"
    )
    env = {**os.environ, "PYTHONPATH": src}
    proc = subprocess.Popen(
        [sys.executable, "-c", driver, str(path)],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        text=True,
        env=env,
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
