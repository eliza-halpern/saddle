"""The evidence packet: compiled from the ledger alone, every claim cited.

Pinned both ways: a packet compiled from a real `saddle auto` run cites only
records that are in its ledger; a packet with an uncited claim, a cite the
ledger lacks, or a narrative row promoted to evidence is refused. The
verdict comes from the outcome span only, so a run with no finish or stop
record is never shown finished.
"""

from __future__ import annotations

import json
import subprocess
from collections.abc import Iterator
from dataclasses import replace
from pathlib import Path
from typing import Any, cast

import pytest

from saddle.auto import AutoOptions, AutoResult, run_auto
from saddle.events import AuditFinding, Event, Question
from saddle.journal import append_span, build_span, read_entries
from saddle.packet import (
    NARRATIVE_LABEL,
    Packet,
    PacketError,
    Row,
    Status,
    check_packet,
    compile_packet,
    flag_narrative,
    render_packet_text,
)
from saddle.vllm import ToolCall, VllmClient

BUGGY = "def add(a, b):\n    return a - b\n"
TEST = "from calc import add\n\n\ndef test_add():\n    assert add(2, 2) == 4\n"


def git(repo: Path, *args: str) -> None:
    subprocess.run(
        ["git", "-C", str(repo), "-c", "user.name=t", "-c", "user.email=t@t", *args],
        check=True,
        capture_output=True,
    )


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    root = tmp_path / "repo"
    (root / "tests").mkdir(parents=True)
    (root / "calc.py").write_text(BUGGY)
    (root / "tests" / "test_calc.py").write_text(TEST)
    (root / "pyproject.toml").write_text(
        '[tool.pytest.ini_options]\ntestpaths = ["tests"]\npythonpath = ["."]\n'
    )
    (root / ".gitignore").write_text("__pycache__/\n.pytest_cache/\n")
    git(root, "init", "-q", "-b", "main")
    git(root, "add", "-A")
    git(root, "commit", "-q", "-m", "init")
    return root


def call(name: str, cid: str, **arguments: Any) -> ToolCall:
    return ToolCall(id=cid, name=name, arguments=json.dumps(arguments))


class Scripted:
    def __init__(self, rounds: list[list[Any]], tail: list[Any] | None = None) -> None:
        self.rounds = list(rounds)
        self.tail = tail or []

    def __enter__(self) -> Scripted:
        return self

    def __exit__(self, *_: object) -> None:
        return None

    def stream_chat(self, messages: Any, **_: Any) -> Iterator[Any]:
        return iter(self.rounds.pop(0) if self.rounds else list(self.tail))


# The fix changes the file's length on purpose: a same-length edit made within
# a second of the failing run leaves pytest reading the stale .pyc (source
# mtime and size both still match), and the second run fails a correct fix.
FIX = [
    [call("run_command", "c1", command="python -m pytest -q")],
    [call("edit_file", "c2", path="tests/test_calc.py", old="4", new="0")],
    [call("edit_file", "c3", path="calc.py", old="a - b", new="(a + b)")],
    [call("run_command", "c4", command="python -m pytest -q")],
    [call("finish", "c5", summary="add now adds. All tests pass now.")],
]


def run(repo: Path, rounds: list[list[Any]], **kwargs: Any) -> AutoResult:
    options = AutoOptions(task="make add add", repo=repo, run_id="r1", **kwargs.pop("opts", {}))
    return run_auto(options, cast(VllmClient, Scripted(rounds, kwargs.pop("tail", None))), **kwargs)


def asking_auditor(answer: str | None) -> dict[str, Any]:
    def audit(name: str, arguments: str, result: str) -> list[Event]:
        if name == "edit_file" and '"calc.py"' in arguments:
            return [Question(id="q1", text="Should add(0, 0) be 0?", options=["yes", "no"])]
        if name == "run_command" and result.startswith("exit 0"):
            return [AuditFinding(gate="changed-line-coverage", ok=True, detail="1 of 1 covered")]
        return []

    return {"audit": audit, "answer": lambda _q: answer}


def hashes(journal: Path) -> set[str]:
    return {e.record_hash for e in read_entries(journal)}


# -- known-good ---------------------------------------------------------------


def test_every_cite_in_a_real_runs_packet_resolves_to_a_ledger_record(repo: Path) -> None:
    result = run(repo, FIX, **asking_auditor("yes"))
    packet = compile_packet(result.journal)
    ledger = hashes(result.journal)
    cited = [c for row in packet.rows for c in row.cites]
    assert cited, "a finished run's packet cites something"
    assert set(cited) <= ledger
    assert set(packet.records) == set(cited)
    assert packet.verdict == "finished"
    keys = [row.key for row in packet.rows]
    assert keys == [
        "contract",
        "tests",
        "mutation",
        "scope",
        "audit",
        "not-proven",
        "narrative",
        "cost",
        "reproduce",
    ]
    rows = {row.key: row for row in packet.rows}
    assert rows["contract"].status == "observed"
    assert "you answered: yes" in rows["contract"].items[0]
    assert rows["audit"].status == "proven"
    assert rows["mutation"].status == "absent"  # absent evidence still shown, saying so
    assert "No mutation record" in rows["mutation"].text
    assert rows["scope"].items[0] == "calc.py"
    assert any(i.startswith("refused:") for i in rows["scope"].items)
    assert rows["narrative"].status == "narrative"
    assert rows["narrative"].title.lower().startswith("narrative")
    assert [s.flagged for s in packet.narrative] == [False, True]
    assert rows["cost"].status == "cost"
    assert rows["cost"].cites
    text = render_packet_text(packet)
    assert text.startswith("verdict: finished")
    assert "add now adds" not in text  # the recap carries no model narrative
    assert render_packet_text(compile_packet(result.journal)) == text  # deterministic (T5-9)
    assert packet.payload()["narrative_label"] == NARRATIVE_LABEL


def test_the_packet_is_refused_when_its_ledger_is_tampered_with(repo: Path) -> None:
    result = run(repo, FIX)
    lines = result.journal.read_text().splitlines()
    first = json.loads(lines[0])
    first["detail"] = "rewritten"
    result.journal.write_text("\n".join([json.dumps(first), *lines[1:]]) + "\n")
    packet = compile_packet(result.journal)
    assert packet.verdict == "unrecorded"
    assert packet.rows[0].key == "not-proven"


def test_a_run_with_no_auditor_is_finished_not_proven_done(repo: Path) -> None:
    result = run(repo, FIX)
    packet = compile_packet(result.journal)
    rows = {row.key: row for row in packet.rows}
    assert packet.verdict == "finished"
    assert "not proven done" in packet.verdict_text
    assert rows["tests"].status == "observed"
    assert "audit" not in rows
    assert any("No auditor verdict" in i for i in rows["not-proven"].items)


def test_a_failed_audit_finding_is_named_in_the_verdict(repo: Path) -> None:
    def audit(name: str, _a: str, _r: str) -> list[Event]:
        return [AuditFinding(gate="lint", ok=False, detail="E501")] if name == "run_command" else []

    result = run(repo, FIX, audit=audit)
    packet = compile_packet(result.journal)
    assert "failed" in packet.verdict_text
    rows = {row.key: row for row in packet.rows}
    assert rows["audit"].status == "failed"


def test_a_budget_stop_is_stopped_with_every_evidence_row_absent(repo: Path) -> None:
    reading = [call("read_file", "r", path="calc.py")]
    result = run(repo, [], tail=reading, opts={"token_budget": 1})
    packet = compile_packet(result.journal)
    assert packet.verdict == "stopped"
    assert "token budget" in packet.verdict_text
    rows = {row.key: row for row in packet.rows}
    assert rows["tests"].status == rows["contract"].status == "absent"
    assert any(
        i.startswith("The run stopped before finishing: token") for i in rows["not-proven"].items
    )
    assert packet.narrative == ()


def test_an_unanswered_question_leaves_the_run_needing_you_then_stopped(repo: Path) -> None:
    result = run(repo, FIX, **asking_auditor(None))
    packet = compile_packet(result.journal)
    assert packet.verdict == "stopped"
    assert "needs you" in packet.verdict_text


def test_a_question_with_no_outcome_yet_is_needs_you(repo: Path) -> None:
    result = run(repo, FIX, **asking_auditor(None))
    entries = result.journal.read_text().splitlines()
    cut = next(i for i, line in enumerate(entries) if '"auto:stopped"' in line)
    trimmed = result.journal.with_name("trimmed.jsonl")
    trimmed.write_text("\n".join(entries[:cut]) + "\n")
    packet = compile_packet(trimmed, run_id="r1")
    assert packet.verdict == "needs_you"
    assert "Should add(0, 0) be 0?" in packet.verdict_text


def test_no_ledger_is_unrecorded(tmp_path: Path) -> None:
    packet = compile_packet(tmp_path / "runs" / "x" / "proofs.jsonl")
    assert packet.verdict == "unrecorded"
    assert packet.verdict_text == "There is no ledger for this run yet."
    assert packet.run_id == "x"


# -- known-bad: the chat cannot mark a task done without a finish/stop record --


def test_a_ledger_with_no_outcome_record_is_never_finished(tmp_path: Path) -> None:
    journal = tmp_path / "proofs.jsonl"
    append_span(
        journal,
        build_span(
            node_id="auto",
            argv=["auto", "t"],
            duration_ms=0,
            exit_code=0,
            detail="",
            kind="agent",
            name="auto:start",
        ),
    )
    packet = compile_packet(journal)
    assert packet.verdict == "unrecorded"
    assert "No finish or stop record" in packet.verdict_text


# -- known-bad: check_packet ---------------------------------------------------


def packet_of(*rows: Row) -> Packet:
    return Packet(run_id="r", task="t", verdict="finished", verdict_text="", header=(), rows=rows)


@pytest.mark.parametrize("status", ["proven", "failed", "observed", "cost"])
def test_a_claim_row_with_no_cite_is_refused(status: Status) -> None:
    with pytest.raises(PacketError, match="no cite"):
        check_packet(packet_of(Row("tests", "Tests", status, "all green")), [])


def test_a_cite_the_ledger_does_not_hold_is_refused() -> None:
    with pytest.raises(PacketError, match="not in the ledger"):
        check_packet(
            packet_of(Row("tests", "Tests", "proven", "ok", cites=("f" * 64,))), ["a" * 64]
        )


def test_a_narrative_row_marked_as_evidence_is_refused() -> None:
    row = Row("narrative", "Narrative", "proven", "it works", cites=("a" * 64,))
    with pytest.raises(PacketError, match="never evidence"):
        check_packet(packet_of(row), ["a" * 64])


def test_known_good_rows_pass_the_check() -> None:
    check_packet(
        packet_of(
            Row("tests", "Tests", "proven", "ok", cites=("a" * 64,)),
            Row("mutation", "Mutation", "absent", "no record"),
            Row("narrative", "Narrative", "narrative", ""),
        ),
        ["a" * 64],
    )


def test_compile_refuses_what_it_would_have_rendered_uncited(
    repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import saddle.packet as module

    result = run(repo, FIX)
    real = module.Row

    def uncited(*args: Any, **kwargs: Any) -> Row:
        return replace(real(*args, **kwargs), cites=())

    monkeypatch.setattr(module, "Row", uncited)
    with pytest.raises(PacketError):
        compile_packet(result.journal)


# -- the narrative flag, and its documented limits -----------------------------


@pytest.mark.parametrize(
    ("sentence", "flagged"),
    [
        ("All tests pass now.", True),
        ("Coverage is 100%.", True),
        ("The mutation score is green.", True),
        ("I changed add to use +.", False),
        ("total() now sums every line.", False),
        # documented limits: no check noun -> missed; edit report -> over-flagged
        ("Everything is fine now.", False),
        ("I fixed the failing test.", True),
    ],
)
def test_narrative_sentences_asserting_a_check_result_are_flagged(
    sentence: str, flagged: bool
) -> None:
    assert [s.flagged for s in flag_narrative(sentence)] == [flagged]


def test_the_narrative_is_split_into_sentences() -> None:
    got = flag_narrative("One.  Two passes the tests!\nThree")
    assert [s.text for s in got] == ["One.", "Two passes the tests!", "Three"]


# -- evidence that is missing, tampered or of another kind ---------------------


def test_a_tampered_outcome_sidecar_makes_the_run_unrecorded_not_finished(repo: Path) -> None:
    from saddle.journal import attempt_sidecar_path, read_spans

    result = run(repo, FIX)
    outcome = next(s for s in read_spans(result.journal) if s.name == "auto:finished")
    path = attempt_sidecar_path(result.journal, outcome.span_id)
    path.write_text(path.read_text().replace("calc.py", "evil.py"))
    packet = compile_packet(result.journal)
    assert packet.verdict == "unrecorded"
    assert "does not verify" in packet.verdict_text


def test_evidence_that_vanishes_after_the_read_is_shown_unknown(
    repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import saddle.packet as module

    result = run(repo, FIX)
    monkeypatch.setattr(module, "_sidecar", lambda _j, _s: None)
    rows = {row.key: row for row in compile_packet(result.journal).rows}
    assert rows["scope"].status == rows["cost"].status == "absent"
    assert any("does not hash" in i for i in rows["not-proven"].items)


def test_a_torn_tail_is_reported_in_not_proven(repo: Path) -> None:
    result = run(repo, FIX)
    with result.journal.open("a") as fh:
        fh.write('{"record_type": "span"')
    rows = {row.key: row for row in compile_packet(result.journal).rows}
    assert any("torn-tail" in i for i in rows["not-proven"].items)


def test_a_sidecar_read_is_refused_when_missing_or_mismatched(tmp_path: Path) -> None:
    from saddle.journal import attempt_sidecar_path
    from saddle.packet import _sidecar

    journal = tmp_path / "proofs.jsonl"
    span = build_span(
        node_id="a",
        argv=["a"],
        duration_ms=0,
        exit_code=0,
        detail="",
        kind="agent",
        name="auto:finished",
        attempt_hash="0" * 64,
    )
    assert _sidecar(journal, span) is None  # missing
    path = attempt_sidecar_path(journal, span.span_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("{}")
    assert _sidecar(journal, span) is None  # does not hash to the span


@pytest.mark.parametrize("raw", [b"not json", b"[1, 2]"])
def test_a_sidecar_that_hashes_but_is_not_an_object_is_no_evidence(
    tmp_path: Path, raw: bytes
) -> None:
    import hashlib

    from saddle.journal import attempt_sidecar_path
    from saddle.packet import _sidecar

    journal = tmp_path / "proofs.jsonl"
    span = build_span(
        node_id="a",
        argv=["a"],
        duration_ms=0,
        exit_code=0,
        detail="",
        kind="agent",
        name="auto:finished",
        attempt_hash=hashlib.sha256(raw).hexdigest(),
    )
    path = attempt_sidecar_path(journal, span.span_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(raw)
    assert _sidecar(journal, span) is None
    bare = build_span(
        node_id="a",
        argv=["a"],
        duration_ms=0,
        exit_code=0,
        detail="",
        kind="agent",
        name="auto:finished",
    )
    assert _sidecar(journal, bare) is None


def test_a_run_command_with_unreadable_arguments_is_not_a_test_run() -> None:
    from saddle.packet import _command

    bad = build_span(
        node_id="a",
        argv=["run_command", "{not json"],
        duration_ms=0,
        exit_code=0,
        detail="",
        kind="tool",
        name="run_command",
    )
    assert _command(bad) == ""
    listed = build_span(
        node_id="a",
        argv=["run_command", "[1]"],
        duration_ms=0,
        exit_code=0,
        detail="",
        kind="tool",
        name="run_command",
    )
    assert _command(listed) == ""


@pytest.mark.parametrize(("ok", "status"), [(True, "proven"), (False, "failed")])
def test_suite_and_mutation_audits_fill_their_own_rows(repo: Path, ok: bool, status: str) -> None:
    def audit(name: str, _a: str, _r: str) -> list[Event]:
        if name == "run_command":
            return [
                AuditFinding(gate="suite", ok=ok, detail="12 passed"),
                AuditFinding(gate="mutation", ok=ok, detail="3 of 3 killed"),
            ]
        return []

    result = run(repo, FIX, audit=audit)
    rows = {row.key: row for row in compile_packet(result.journal).rows}
    assert rows["tests"].status == status
    assert "12 passed" in rows["tests"].text
    assert rows["mutation"].status == status
    assert rows["mutation"].text == "3 of 3 killed"
    assert "audit" not in rows  # nothing left over for the seam row
