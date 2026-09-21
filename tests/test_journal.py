"""Tests for saddle.journal: sealing, verification, crash rebuild."""

from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
import sys
import time
from datetime import UTC, datetime
from pathlib import Path

import pytest
from pydantic import ValidationError

from saddle.dag import Node
from saddle.gates import GateCheck, Tier1Result
from saddle.journal import (
    MAX_THINKING_CHARS,
    GateOutput,
    ProofRecord,
    SpanRecord,
    SpanRecorder,
    append_plan,
    append_record,
    append_span,
    attempt_sidecar_path,
    build_from_gate,
    build_plan,
    build_record,
    build_span,
    hash_node,
    proven_records,
    read_entries,
    read_plans,
    read_records,
    read_spans,
    rebuild_proven,
    scrub_thinking,
    started_before,
    tool_spans_by_node,
    tool_spans_for_node,
    verify_journal,
    write_attempt_sidecar,
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
        # T2-4: a new record dumps `basis` even when None; the hash covers it.
        "gate_outputs": [{"name": "tests", "passed": True, "detail": "ok", "basis": None}],
        "requirement_ids": ["REQ-001"],
        "thinking": "",
        "attempts": 1,
        # T3-9: a record built without them still dumps the four, so the
        # hash a caller recomputes and the one build_record sealed agree.
        "task_hash": "",
        "node_hash": "",
        "kind": "",
        "target_files": [],
        "tree_hash": "",
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
        # T2-4: a new record dumps `basis` even when None; the hash covers it.
        "gate_outputs": [{"name": "tests", "passed": True, "detail": "ok", "basis": None}],
        "requirement_ids": ["REQ-001"],
        "thinking": "extract the helper",
        "attempts": 1,
        "task_hash": "",
        "node_hash": "",
        "kind": "",
        "target_files": [],
        "tree_hash": "",
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


PRE_BASIS_JOURNAL = Path(__file__).parent / "fixtures" / "pre-basis-proofs.jsonl"


def test_pre_basis_journal_still_verifies(tmp_path: Path) -> None:
    """A journal sealed before GateOutput.basis existed keeps its hash.

    The fixture was generated at 9854b5f by the slice fixture and must not
    be regenerated: its records carry no `basis` key, so the field parses
    unset and the verification payload (exclude_unset=True) omits it,
    reproducing the hash that was sealed at the time (T2-4, known-good for
    the compatibility half of the contract).
    """
    raw = PRE_BASIS_JOURNAL.read_text().splitlines()
    proofs = [json.loads(line) for line in raw if json.loads(line)["record_type"] == "proof"]
    assert proofs, "fixture holds no proof record"
    assert all("basis" not in output for record in proofs for output in record["gate_outputs"])
    assert verify_journal(PRE_BASIS_JOURNAL) == []
    (record,) = read_records(PRE_BASIS_JOURNAL)
    assert record.node_id == "n1"
    assert all(output.basis is None for output in record.gate_outputs)
    assert rebuild_proven(PRE_BASIS_JOURNAL) == {"n1": record.record_hash}


def _proof_node(**overrides: object) -> Node:
    """A validated node: what a proof record has to name (T3-9)."""
    spec: dict[str, object] = {
        "id": "n1",
        "kind": "impl",
        "dependencies": [],
        "task_prompt": "Do n1.",
        "requirements": [
            {"id": "REQ-001", "statement": "REQ-001 holds.", "accepts": ["2"], "rejects": ["3"]}
        ],
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
                "max_mutants": 100,
                "kill_threshold": 85.0,
            },
        },
        "target_files": ["n.py"],
    }
    return Node.model_validate({**spec, **overrides})


def _passing_result() -> Tier1Result:
    return Tier1Result(
        node_id="n1", passed=True, checks=(GateCheck(name="tests", passed=True, detail="ok"),)
    )


def test_build_from_gate_seals_the_task_hash_node_hash_and_kind(tmp_path: Path) -> None:
    """Known-good (T3-9): a sealed record names what it is a proof *of*.

    Before this the record held `node_id` and nothing that said which
    task was being worked on or what the node said, so a journal from
    task A counted every same-id node of task B as already proven.
    """
    node = _proof_node()
    record = build_from_gate(
        node,
        "diff n1\n",
        _passing_result(),
        [],
        "e1",
        thinking="why n1",
        task_hash="a" * 64,
    )
    assert record.task_hash == "a" * 64
    assert record.node_hash == hash_node(node)
    assert re.fullmatch(r"[0-9a-f]{64}", record.node_hash)
    assert record.kind == "impl"
    assert record.target_files == ["n.py"]
    # Known-bad for the node half: a different node hashes differently.
    assert hash_node(_proof_node(task_prompt="Do n1, differently.")) != record.node_hash
    path = tmp_path / "proofs.jsonl"
    append_record(path, record)
    assert verify_journal(path) == []
    assert read_records(path) == [record]


def test_build_from_gate_seals_the_tree_hash_it_was_proven_on(tmp_path: Path) -> None:
    """Known-good (T3-10): the record names the worktree its gate passed
    on, so a resume can tell whether it is resuming onto that tree rather
    than assuming the edits are still there.

    Known-bad below: the field is inside the record hash, so a record
    re-pointed at another tree is corruption, not a reusable proof.
    """
    record = build_from_gate(
        _proof_node(),
        "diff n1\n",
        _passing_result(),
        [],
        "e1",
        thinking="why n1",
        task_hash="a" * 64,
        tree_hash="b" * 40,
    )
    assert record.tree_hash == "b" * 40
    good = tmp_path / "good.jsonl"
    append_record(good, record)
    assert verify_journal(good) == []
    assert read_records(good)[0].tree_hash == "b" * 40
    bad = tmp_path / "bad.jsonl"
    append_record(bad, record.model_copy(update={"tree_hash": "c" * 40}))
    (issue,) = verify_journal(bad)
    assert issue.code == "bad-hash"


def test_altering_a_sealed_task_hash_fails_verification(tmp_path: Path) -> None:
    """Known-bad (T3-9c): the new fields are inside the hash, so a record
    re-pointed at another task is corruption, not a reusable proof."""
    record = build_from_gate(
        _proof_node(), "diff n1\n", _passing_result(), [], "e1", thinking="", task_hash="a" * 64
    )
    good = tmp_path / "good.jsonl"
    append_record(good, record)
    assert verify_journal(good) == []
    bad = tmp_path / "bad.jsonl"
    append_record(bad, record.model_copy(update={"task_hash": "b" * 64}))
    (issue,) = verify_journal(bad)
    assert issue.code == "bad-hash"


def test_node_hash_pins_the_serialisation_a_resume_keys_on(tmp_path: Path) -> None:
    """The node hash is the resume key, so a quiet change in how pydantic
    spells `model_dump_json` would re-run every journalled node with no
    other symptom. A red here is that change, not a stale constant: the
    answer is to canonicalise (sorted `model_dump(mode="json")`), not to
    paste the new digest in.
    """
    assert (
        hash_node(_proof_node())
        == "406304034176f5faf098021720b6a31554c7f4766f9b6f70c1cf8a2e42cc859f"
    )


def test_proven_records_returns_the_last_record_per_node(tmp_path: Path) -> None:
    """`rebuild_proven` keeps its narrower contract; this one hands back
    the whole record, which is what a resume needs to check it (T3-9)."""
    path = tmp_path / "proofs.jsonl"
    first = _record("n1")
    append_record(path, first)
    child = _record("n2", [first.record_hash])
    append_record(path, child)
    again = build_record(
        evidence_id="e-n1#2",
        node_id="n1",
        diff="diff n1 again\n",
        parent_proofs=[],
        gate_outputs=[GateOutput(name="tests", passed=True, detail="ok")],
        requirement_ids=["REQ-001"],
        thinking="second go",
    )
    append_record(path, again)
    assert again.record_hash != first.record_hash
    assert proven_records(path) == {"n1": again, "n2": child}
    assert rebuild_proven(path) == {"n1": again.record_hash, "n2": child.record_hash}
    assert proven_records(tmp_path / "missing.jsonl") == {}


def test_basis_round_trips_and_reverifies(tmp_path: Path) -> None:
    """Known-good: a record sealed with a basis reads back with it and
    still verifies; a record with an unknown extra key is still refused."""
    path = tmp_path / "proofs.jsonl"
    record = build_record(
        evidence_id="e-n1",
        node_id="n1",
        diff="diff n1\n",
        parent_proofs=[],
        gate_outputs=[
            GateOutput(name="tests", passed=True, detail="ok"),
            GateOutput(name="mutation", passed=True, detail="100.0% over 5", basis="sampled n=5"),
        ],
        requirement_ids=["REQ-001"],
        thinking="",
    )
    append_record(path, record)
    (back,) = read_records(path)
    assert [output.basis for output in back.gate_outputs] == [None, "sampled n=5"]
    assert back == record
    assert verify_journal(path) == []
    # New records dump the key even when None, so build and verify agree.
    assert '"basis": null' in path.read_text().splitlines()[0]
    with pytest.raises(ValidationError):
        GateOutput.model_validate(
            {"name": "mutation", "passed": True, "detail": "x", "evidence": "sampled n=5"}
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
            "kind": "refactor",
            "dependencies": [],
            "task_prompt": "Do n1.",
            "requirements": [
                {"id": "REQ-001", "statement": "REQ-001 holds.", "accepts": ["2"], "rejects": ["3"]}
            ],
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
                    "max_mutants": 100,
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
    # Unset fields stay off the line (T6-12: `attempt_hash` is written only
    # when an attempt sidecar exists), so the sealed payload and the line agree.
    assert lines[0] == json.dumps(span.model_dump(exclude_unset=True), sort_keys=True)


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


def _plan_node(node_id: str = "n1", *, files: list[str] | None = None) -> Node:
    return Node.model_validate(
        {
            "id": node_id,
            "kind": "impl",
            "dependencies": [],
            "target_files": files if files is not None else ["n.py"],
            "task_prompt": f"Do {node_id}.",
            "requirements": [
                {"id": "REQ-001", "statement": "REQ-001 holds.", "accepts": ["2"], "rejects": ["3"]}
            ],
            "execution_constraints": {
                "reasoning_budget": "medium",
                "allowed_tools": ["read_file"],
                "max_context_tokens": 9000,
            },
            "deterministic_gate": {
                "test_command": "pytest tests/",
                "changed_line_coverage_min": 100.0,
                "red_phase_required": True,
                "mutation_sample": {
                    "scope": "changed-lines",
                    "max_mutants": 100,
                    "kill_threshold": 85.0,
                },
            },
        }
    )


def test_plan_record_seals_what_was_asked_and_round_trips(tmp_path: Path) -> None:
    """Known-good (T6-13): the plan is in the chain before any outcome, with
    every node's kind, scope, budgets and hash; a replan names what it
    replaces; `verify` accepts it and `read_plans` returns it in order."""
    path = tmp_path / "proofs.jsonl"
    node = _plan_node()
    plan = build_plan([node], task_hash="t" * 64)
    append_plan(path, plan)
    replan = build_plan(
        [_plan_node("n1.r1", files=["n.py", "m.py"])], task_hash="t" * 64, replaces="n1"
    )
    append_plan(path, replan)
    assert verify_journal(path) == []
    plans = read_plans(path)
    assert [p.replaces for p in plans] == ["", "n1"]
    (only,) = plans[0].nodes
    assert (
        only.id,
        only.kind,
        only.target_files,
        only.reasoning_budget,
        only.max_context_tokens,
    ) == (
        "n1",
        "impl",
        ["n.py"],
        "medium",
        9000,
    )
    assert only.node_hash == hash_node(node)
    assert '"replaces"' not in path.read_text().splitlines()[0]
    assert read_entries(path)[0] == plan


def test_verify_rejects_a_tampered_plan_and_a_proof_no_plan_asked_for(tmp_path: Path) -> None:
    """Known-bad (T6-13): editing a plan record breaks its hash; a proof
    sealed after a plan whose node hash no plan names is `unplanned-proof`.
    A proof sealed before any plan predates T6-13 and is not judged."""
    path = tmp_path / "proofs.jsonl"
    node = _plan_node()
    legacy = build_record(
        evidence_id="old#1",
        node_id="old",
        diff="diff old\n",
        parent_proofs=[],
        gate_outputs=[GateOutput(name="tests", passed=True, detail="ok")],
        requirement_ids=["REQ-001"],
        thinking="",
        node_hash="9" * 64,
    )
    append_record(path, legacy)
    append_plan(path, build_plan([node], task_hash="t" * 64))
    planned = build_record(
        evidence_id="n1#1",
        node_id="n1",
        diff="diff n1\n",
        parent_proofs=[],
        gate_outputs=[GateOutput(name="tests", passed=True, detail="ok")],
        requirement_ids=["REQ-001"],
        thinking="",
        node_hash=hash_node(node),
    )
    append_record(path, planned)
    assert verify_journal(path) == []
    stray = build_record(
        evidence_id="ghost#1",
        node_id="ghost",
        diff="diff ghost\n",
        parent_proofs=[],
        gate_outputs=[GateOutput(name="tests", passed=True, detail="ok")],
        requirement_ids=["REQ-001"],
        thinking="",
        node_hash="8" * 64,
    )
    append_record(path, stray)
    (issue,) = verify_journal(path)
    assert (issue.code, issue.line) == ("unplanned-proof", 4)
    assert "node 'ghost' matches no plan record" in issue.message
    lines = path.read_text().splitlines()
    raw = json.loads(lines[1])
    raw["nodes"][0]["kind"] = "refactor"
    lines[1] = json.dumps(raw, sort_keys=True)
    path.write_text("\n".join(lines) + "\n")
    codes = [(issue.code, issue.line) for issue in verify_journal(path)]
    assert ("bad-hash", 2) in codes
    line_two = next(i for i in verify_journal(path) if i.line == 2)
    assert "record hash mismatch for plan" in line_two.message


def test_attempt_sidecar_is_sealed_into_its_span_and_verified(tmp_path: Path) -> None:
    """Known-good (T6-12): the sidecar's hash rides in the agent span and
    `verify` checks it; known-bad: one edited byte, or a missing file, is
    an `attempt-sidecar` issue naming the span."""
    path = tmp_path / "proofs.jsonl"
    evidence = {
        "node_id": "n1",
        "thinking": "Bearer sk-secret then thought",
        "finish_reason": "length",
    }
    digest = write_attempt_sidecar(path, "abc123", evidence)
    sidecar = attempt_sidecar_path(path, "abc123")
    assert sidecar == tmp_path / "attempts" / "abc123.json"
    stored = json.loads(sidecar.read_bytes())
    assert stored["finish_reason"] == "length"
    assert "sk-secret" not in stored["thinking"]
    span = build_span(
        node_id="n1",
        argv=[],
        duration_ms=1,
        exit_code=1,
        detail="truncated",
        kind="agent",
        name="worker:n1",
        span_id="abc123",
        attempt_hash=digest,
    )
    append_span(path, span)
    assert verify_journal(path) == []
    assert read_spans(path)[0].attempt_hash == digest
    plain = build_span(node_id="n1", argv=["pytest"], duration_ms=1, exit_code=0, detail="")
    assert plain.attempt_hash == ""
    assert "attempt_hash" not in plain.model_dump(exclude_unset=True)
    sidecar.write_bytes(sidecar.read_bytes().replace(b"length", b"stop  "))
    (issue,) = verify_journal(path)
    assert (issue.code, issue.line) == ("attempt-sidecar", 1)
    assert "span 'abc123' does not hash" in issue.message
    sidecar.unlink()
    (issue,) = verify_journal(path)
    assert "span 'abc123' missing" in issue.message


def test_verify_rejects_a_malformed_plan_line(tmp_path: Path) -> None:
    """A line that claims to be a plan and is not one is `invalid-record`,
    like a malformed span or proof; it is never silently skipped."""
    path = tmp_path / "proofs.jsonl"
    path.write_text(json.dumps({"record_type": "plan", "bogus": 1}) + "\n")
    (issue,) = verify_journal(path)
    assert (issue.code, issue.line, issue.message) == (
        "invalid-record",
        1,
        "line is not a plan record",
    )


def test_span_started_at_is_sealed_when_given_and_absent_when_not() -> None:
    """T6-27 known-good: a span built with `started_at` carries it under the
    hash, so editing it fails verification; known-bad for compatibility:
    a span built without one has no such key, which is what keeps every
    pre-T6-27 journal (and the pre-basis fixture) verifying.
    """
    when = "2026-09-20T17:22:00+00:00"
    stamped = build_span(
        node_id="n1", argv=["git", "status"], duration_ms=5, exit_code=0, detail="", started_at=when
    )
    assert stamped.started_at == when
    payload = stamped.model_dump(exclude={"record_hash"}, exclude_unset=True)
    assert payload["started_at"] == when

    def digest(p: dict[str, object]) -> str:
        return hashlib.sha256(
            json.dumps(p, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()

    assert digest(payload) == stamped.record_hash
    assert digest({**payload, "started_at": "2026-09-20T17:23:00+00:00"}) != stamped.record_hash
    bare = build_span(node_id="n1", argv=["git", "status"], duration_ms=5, exit_code=0, detail="")
    assert "started_at" not in bare.model_dump(exclude_unset=True)


def test_recorder_stamps_tool_spans_with_the_start_derived_from_its_clock(tmp_path: Path) -> None:
    """T6-27: a tool span's start is the recorder's clock less its duration."""
    fixed = datetime(2026, 9, 20, 17, 22, 10, tzinfo=UTC)
    journal = tmp_path / "proofs.jsonl"
    recorder = SpanRecorder(journal, "n1", None, clock=lambda: fixed)
    recorder.record(argv=["git", "status"], duration_ms=2500, exit_code=0, detail="")
    (span,) = read_spans(journal)
    assert span.started_at == "2026-09-20T17:22:07.500000+00:00"
    assert started_before(0, fixed) == fixed.isoformat()
    assert verify_journal(journal) == []


def test_plan_record_carries_each_nodes_allowed_tools() -> None:
    """T6-27: F21.13d could not be settled because the plan omitted the tools."""
    node = _plan_node()
    plan = build_plan([node], task_hash="t")
    assert plan.nodes[0].allowed_tools == list(node.execution_constraints.allowed_tools)
    assert plan.nodes[0].allowed_tools


def test_verify_flags_a_retained_diff_that_does_not_hash_to_its_diff_hash(tmp_path: Path) -> None:
    """T6-27 known-bad: a sidecar retaining a diff whose sha256 is not the
    sealed `diff_hash` is a sidecar telling two stories, and verify says
    so with its own code. Known-good: a matching diff verifies clean, and a
    sidecar with no retained diff (pre-T6-27) is not judged.
    """
    journal = tmp_path / "proofs.jsonl"
    diff = "diff --git a/n.py b/n.py\n"
    good: dict[str, object] = {"diff": diff, "diff_hash": hashlib.sha256(diff.encode()).hexdigest()}

    def seal(evidence: dict[str, object], span_id: str) -> None:
        attempt_hash = write_attempt_sidecar(journal, span_id, evidence)
        append_span(
            journal,
            build_span(
                node_id="n1",
                argv=[],
                duration_ms=1,
                exit_code=1,
                detail="x",
                kind="agent",
                name="worker:n1",
                span_id=span_id,
                attempt_hash=attempt_hash,
            ),
        )

    seal(good, "a" * 32)
    seal({"diff_hash": good["diff_hash"]}, "b" * 32)
    assert verify_journal(journal) == []
    seal({**good, "diff": diff + "+x\n"}, "c" * 32)
    issues = verify_journal(journal)
    assert [issue.code for issue in issues] == ["sidecar-diff-hash"]
    assert issues[0].line == 3


def test_sidecar_diff_check_ignores_sidecars_that_are_not_json_objects(tmp_path: Path) -> None:
    """T6-27: the retained-diff check judges only a JSON object; a sidecar
    that is not JSON, or is a JSON list, hashes as sealed and is left to
    the sidecar-hash check alone."""
    journal = tmp_path / "proofs.jsonl"
    for span_id, raw in (("a" * 32, b"not json"), ("b" * 32, b"[1, 2]")):
        path = attempt_sidecar_path(journal, span_id)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(raw)
        append_span(
            journal,
            build_span(
                node_id="n1",
                argv=[],
                duration_ms=1,
                exit_code=1,
                detail="x",
                kind="agent",
                name="worker:n1",
                span_id=span_id,
                attempt_hash=hashlib.sha256(raw).hexdigest(),
            ),
        )
    assert verify_journal(journal) == []
