"""The evidence packet: compiled from the ledger alone, every claim cited.

Pinned both ways: a packet compiled from a real `saddle auto` run cites only
records that are in its ledger; a packet with an uncited claim, a cite the
ledger lacks, or a narrative row promoted to evidence is refused. The
verdict comes from the outcome span only, so a run with no finish or stop
record is never shown finished.
"""

from __future__ import annotations

import json
import re
import subprocess
import uuid
from collections.abc import Iterator
from dataclasses import replace
from pathlib import Path
from typing import Any, cast

import pytest

from saddle import mutant_text
from saddle.auditor import Finding, Findings
from saddle.auto import AutoOptions, AutoResult, run_auto
from saddle.events import AuditFinding, Event, Question
from saddle.journal import (
    SpanRecord,
    append_span,
    attempt_sidecar_path,
    build_span,
    read_entries,
    write_attempt_sidecar,
)
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
    options = AutoOptions(
        task="make add add", repo=repo, run_id="r1", **{"arm": "E", **kwargs.pop("opts", {})}
    )
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


class PassingAuditor:
    """The feed's auditor, passing every tree: the run still journals its audits."""

    def tier0(self, path: str, new_text: str) -> Findings:
        return Findings(0, "k0", (Finding("ruff", 0, "pass", "code-wrong", "ruff clean", ()),))

    def tier1(self, tree: Path | None = None) -> Findings:
        return Findings(1, "k1", (Finding("tests", 1, "pass", "code-wrong", "1 passed", ()),))

    def tier2(self, tree: Path | None = None) -> Findings:
        return Findings(2, "k2", (Finding("mutation", 2, "pass", "code-wrong", "1 of 1", ()),))


def test_the_cost_row_counts_the_models_tool_calls_not_audit_records(repo: Path) -> None:
    """FIX-1 (out/DOCS/report.md: a 4-tool-call run's Cost row said 18).

    The feed journals its audit records as `tool` spans, but the model did
    not make them. Three calls are scripted; a count that admits the audit
    records reads more.
    """
    script = [
        [call("edit_file", "c1", path="calc.py", old="a - b", new="(a + b)")],
        [call("run_command", "c2", command="true")],
        [call("finish", "c3", summary="done")],
    ]
    auditor = PassingAuditor()
    result = run(repo, script, opts={"arm": "E+A+F", "auditor_factory": lambda *a: auditor})
    spans = [e for e in read_entries(result.journal) if isinstance(e, SpanRecord)]
    audit_tools = [s for s in spans if s.kind == "tool" and s.name.startswith("audit:")]
    assert len(audit_tools) >= 1
    cost = next(row for row in compile_packet(result.journal).rows if row.key == "cost")
    assert "· 3 tool calls over" in cost.text


def tier_span(
    tier: int,
    gate: str,
    verdict: str,
    detail: str,
    span_id: str | None = None,
    attempt_hash: str = "",
) -> SpanRecord:
    body = {"gate": gate, "tier": tier, "verdict": verdict, "reason": "evidence-thin",
            "detail": detail, "cites": []}  # fmt: skip
    exit_code = {"pass": 0, "fail": 1, "blocked": 2}[verdict]
    return build_span(node_id="n", argv=["saddle-audit", f"tier{tier}", gate, "k"],
                      duration_ms=0, exit_code=exit_code, detail=json.dumps(body),
                      name=f"audit-tier{tier}:{gate}", span_id=span_id,
                      attempt_hash=attempt_hash)  # fmt: skip


@pytest.mark.parametrize(
    ("verdict", "detail", "status", "text"),
    [
        (
            "blocked",
            "tier 1 failed (coverage); tier 2 not run",
            "not-proven",
            "blocked: tier 1 failed (coverage); tier 2 not run",
        ),
        ("fail", "2 of 5 killed", "failed", "tier 2, fail: 2 of 5 killed"),
        ("pass", "5 of 5 killed", "proven", "tier 2, pass: 5 of 5 killed"),
    ],
)
def test_a_blocked_tier_2_is_reported_blocked_not_failed(
    tmp_path: Path, verdict: str, detail: str, status: str, text: str
) -> None:
    """FIX-3 (out/DOCS/report.md): a tier 2 that never ran because tier 1
    failed was shown as a failed mutation result, which does not exist."""
    journal = tmp_path / "proofs.jsonl"
    append_span(journal, tier_span(1, "coverage", "fail", "calc.py:2 uncovered"))
    span = tier_span(2, "mutation", verdict, detail)
    append_span(journal, span)
    row = next(r for r in compile_packet(journal).rows if r.key == "mutation")
    assert (row.status, row.text, row.cites) == (status, text, (span.record_hash,))


# -- the mutation row's English summary (PACKETHOOK) ---------------------------------

MUTANT_FIXTURES = Path(__file__).parent / "fixtures" / "mutant_text"


def mutation_outcome(name: str = "E-t5-s1") -> dict[str, Any]:
    """A CALIB tree's sealed `MutationOutcome` plus its `survivor_detail`."""
    data = json.loads((MUTANT_FIXTURES / f"{name}.json").read_text())
    return {**data["outcome"], "survivor_detail": data["survivor_detail"]}


def sealed_mutation_span(
    journal: Path, verdict: str, detail: str, outcome: dict[str, Any] | None
) -> SpanRecord:
    """A tier-2 mutation finding span; with `outcome`, its sidecar is sealed in it."""
    if outcome is None:
        span = tier_span(2, "mutation", verdict, detail)
        append_span(journal, span)
        return span
    span_id = uuid.uuid4().hex
    digest = write_attempt_sidecar(journal, span_id, outcome)
    span = tier_span(2, "mutation", verdict, detail, span_id=span_id, attempt_hash=digest)
    append_span(journal, span)
    return span


def test_a_sealed_mutation_outcome_renders_the_grouped_english_beneath_the_count(
    tmp_path: Path,
) -> None:
    """Known-good: the Mutation row keeps its count line; the sealed outcome's
    English (MUTSUMMARY) sits beneath it, in the text recap and the payload."""
    journal = tmp_path / "proofs.jsonl"
    detail = "killed 220 of 322 sampled mutants; 21 untested"
    span = sealed_mutation_span(journal, "fail", detail, mutation_outcome())
    packet = compile_packet(journal)
    row = next(r for r in packet.rows if r.key == "mutation")
    assert (row.status, row.text) == ("failed", f"tier 2, fail: {detail}")
    assert row.cites == (span.record_hash,)
    assert row.summary.startswith(
        "220 of 322 sampled mutants were caught by the suite [record: killed=220 total=322]\n"
    )
    assert "Left untested:\n  boundary:\n" in row.summary
    assert (
        "    - accounts.py Account.withdraw: `if debit > current:` -> `if debit >= current:`"
        in row.summary
    )
    assert "[accounts.xǁAccountǁwithdraw__mutmut_18]" in row.summary
    text = render_packet_text(packet)
    assert f"Mutation [failed]: tier 2, fail: {detail}\n  220 of 322 sampled mutants" in text
    # The recap prints the COMPACT form (PACKETHOOK-3: it must not scroll):
    # survivors capped at 5, worst group first, "and N more in the packet".
    # Each summary line sits two spaces under the row, so a bullet is "    - ".
    assert "\n  Left untested:\n    - accounts.py Account.withdraw:" in text
    assert "    boundary:\n" not in text
    assert re.search(r"\n    and \d+ more in the packet\n", text)
    assert text.count("\n    - ") == 5
    assert row.recap == mutant_text.render_text(
        mutant_text.describe_mutation(mutation_outcome()), compact=True
    )
    # The payload carries the FULL form for the web fold, and only that.
    payload_row = next(r for r in packet.payload()["rows"] if r["key"] == "mutation")
    assert payload_row["summary"] == row.summary
    assert "recap" not in payload_row


def test_killers_in_the_sealed_outcome_name_the_test_that_caught_a_mutant(tmp_path: Path) -> None:
    """`killers` is threaded: a killed mutant reads "caught by <test>", whether
    the record spells the map as a dict or as (name, test) pairs."""
    show = "--- n.py\n+++ n.py\n@@ -2 +2 @@\n-    if a > b:\n+    if a >= b:\n"
    detail = [{"name": "n.x_f__mutmut_1", "status": "killed", "show": show}]
    base = {"killed": 1, "total": 1, "generated": 1, "survivors": [], "mutant_detail": detail}
    as_map = {"n.x_f__mutmut_1": "test_n.py::test_f"}
    as_pairs = [["n.x_f__mutmut_1", "test_n.py::test_f"]]
    for killers in (as_map, as_pairs):
        journal = tmp_path / type(killers).__name__ / "proofs.jsonl"
        sealed_mutation_span(journal, "pass", "killed 1 of 1", {**base, "killers": killers})
        row = next(r for r in compile_packet(journal).rows if r.key == "mutation")
        assert "Caught:\n  boundary:\n" in row.summary
        assert "caught by test_n.py::test_f [n.x_f__mutmut_1]" in row.summary


@pytest.mark.parametrize("verdict", ["blocked", "fail", "pass"])
def test_a_mutation_record_without_a_sealed_outcome_adds_nothing(
    tmp_path: Path, verdict: str
) -> None:
    """Known-bad: a blocked tier 2 (nothing ran), or a finding with no sidecar,
    renders no summary, and the payload carries no `summary` key at all, so a
    packet without one is byte-identical to before this hook."""
    journal = tmp_path / "proofs.jsonl"
    append_span(journal, tier_span(1, "coverage", "fail" if verdict == "blocked" else "pass", "x"))
    sealed_mutation_span(journal, verdict, "tier 1 failed (coverage); tier 2 not run", None)
    packet = compile_packet(journal)
    row = next(r for r in packet.rows if r.key == "mutation")
    assert row.summary == ""
    assert all("summary" not in r for r in packet.payload()["rows"])
    # The recap as it was before this hook: verdict, header, then each row's
    # line and items, and nothing beneath any row.
    expected = [f"verdict: {packet.verdict} — {packet.verdict_text}"]
    expected += [f"  {h}" for h in packet.header]
    for r in packet.rows:
        if r.key != "narrative":
            expected += [f"{r.title} [{r.status}]: {r.text}", *(f"  - {i}" for i in r.items)]
    assert render_packet_text(packet).splitlines() == expected


def test_a_blocked_tier_2_renders_no_summary_even_with_a_sidecar(tmp_path: Path) -> None:
    """A blocked finding never ran the gate; a sidecar on it is not an outcome."""
    journal = tmp_path / "proofs.jsonl"
    append_span(journal, tier_span(1, "coverage", "fail", "x"))
    blocked = "tier 1 failed (coverage); tier 2 not run"
    sealed_mutation_span(journal, "blocked", blocked, mutation_outcome())
    row = next(r for r in compile_packet(journal).rows if r.key == "mutation")
    assert (row.status, row.summary) == ("not-proven", "")


def test_a_mutation_sidecar_that_does_not_hash_makes_the_ledger_unrecorded(tmp_path: Path) -> None:
    """The sealed outcome is a sidecar like any other (T6-12): an edited one
    fails `verify_journal`, and the packet says so instead of describing it."""
    journal = tmp_path / "proofs.jsonl"
    span = sealed_mutation_span(journal, "fail", "killed 220 of 322", mutation_outcome())
    sidecar = attempt_sidecar_path(journal, span.span_id)
    sidecar.write_text(sidecar.read_text().replace("220", "221", 1))
    packet = compile_packet(journal)
    assert packet.verdict == "unrecorded"
    assert "attempt-sidecar" in packet.verdict_text
    assert not any(r.summary for r in packet.rows)


def test_a_seam_mutation_audit_has_no_summary_and_no_summary_key(repo: Path) -> None:
    """A chat seam's `audit:mutation` span records a verdict, not an outcome."""

    def audit(name: str, _a: str, _r: str) -> list[Event]:
        if name == "run_command":
            return [AuditFinding(gate="mutation", ok=True, detail="3 of 3 killed")]
        return []

    result = run(repo, FIX, audit=audit)
    packet = compile_packet(result.journal)
    row = next(r for r in packet.rows if r.key == "mutation")
    assert (row.text, row.summary) == ("3 of 3 killed", "")
    assert all("summary" not in r for r in packet.payload()["rows"])


# -- the Audit row's coverage English (PACKETHOOK, COVTEXT) -------------------------

COVERAGE_FIXTURE = Path(__file__).parent / "fixtures" / "coverage_text" / "E-t5-s1"


def coverage_finding() -> dict[str, Any]:
    finding: dict[str, Any] = json.loads((COVERAGE_FIXTURE / "finding.json").read_text())
    return finding


def coverage_sources() -> dict[str, str]:
    return {
        p.name.removesuffix(".txt"): p.read_text() for p in (COVERAGE_FIXTURE / "tree").iterdir()
    }


def coverage_evidence(tmp_path: Path, sources: dict[str, str] | None = None) -> dict[str, Any]:
    """What the auditor seals beside a failing coverage finding, for E-t5-s1."""
    from saddle.evidence import changed_statements

    tree = tmp_path / "tree"
    tree.mkdir(exist_ok=True)
    for name, text in coverage_sources().items():
        (tree / name).write_text(text)
    changed = changed_statements(tree, (COVERAGE_FIXTURE / "changes.diff").read_text())
    relative = sorted((str(Path(p).relative_to(tree)), n) for p, n in changed)
    named = coverage_sources() if sources is None else sources
    return {
        # The auditor's spelling: each file as its lines, under the sidecar's cap.
        "sources": {name: text.splitlines() for name, text in named.items()},
        "changed": [[p, n] for p, n in relative],
    }


def coverage_span(
    journal: Path,
    finding: dict[str, Any],
    sealed: dict[str, Any] | None,
    detail: str | None = None,
) -> Any:
    body = {**finding, "tier": 1}
    exit_code = {"pass": 0, "fail": 1}[finding["verdict"]]
    kwargs: dict[str, Any] = {}
    if sealed is not None:
        kwargs["span_id"] = uuid.uuid4().hex
        kwargs["attempt_hash"] = write_attempt_sidecar(journal, kwargs["span_id"], sealed)
    if detail is None:
        detail = json.dumps(body, sort_keys=True)
    span = build_span(node_id="n", argv=["saddle-audit", "tier1", "coverage", "k"], duration_ms=0,
                      exit_code=exit_code, detail=detail, name="audit-tier1:coverage",
                      **kwargs)  # fmt: skip
    append_span(journal, span)
    return span


def test_a_failing_coverage_finding_with_sealed_sources_renders_its_english_under_audit(
    tmp_path: Path,
) -> None:
    """Known-good: the Audit row keeps its count and items; beneath them, the
    coverage English (COVTEXT) for E-t5-s1, linked to the mutation summary
    sealed on the same ledger, with the uncovered lines' own text."""
    journal = tmp_path / "proofs.jsonl"
    span = coverage_span(journal, coverage_finding(), coverage_evidence(tmp_path))
    sealed_mutation_span(journal, "fail", "killed 220 of 322 sampled mutants", mutation_outcome())
    packet = compile_packet(journal)
    audit = next(r for r in packet.rows if r.key == "audit")
    assert audit.text == "0 of 1 finding passed."
    assert audit.items[0].startswith("✗ coverage: tier 1, fail: no test runs money.py:42,")
    assert audit.cites == (span.record_hash,)
    assert audit.summary.startswith(
        "Not proven by any test: 19 changed lines no test runs [record: detail names 19 lines; "
        "changed-lines=155 compelled-lines=5]\n"
    )
    assert (
        "  - money.py convert: 10 of 16 changed lines never run -- nothing exercises convert "
        '("Convert a Decimal amount from source to target."); and a mutant there survived '
        "[lines 131, 132, 133, 134, 136, 137, 138, 139, 140, 141]\n"
        '      131:     if source == "USD":\n'
    ) in audit.summary
    assert "not placed" not in audit.summary
    text = render_packet_text(packet)
    assert "\nAudit [failed]: 0 of 1 finding passed.\n  - ✗ coverage: tier 1, fail:" in text
    assert "\n  Not proven by any test: 19 changed lines no test runs" in text
    assert '\n        131:     if source == "USD":\n        132:         usd = value\n' in text
    # Compact in the recap (PACKETHOOK-3), full in the payload for the web fold.
    assert "\n        and 8 more lines\n" in text
    assert "\n    and 1 more functions in the packet\n" in text
    assert "store.py _from_record_v1" not in text
    assert "store.py _from_record_v1" in audit.summary
    assert "more functions in the packet" not in audit.summary
    payload_row = next(r for r in packet.payload()["rows"] if r["key"] == "audit")
    assert payload_row["summary"] == audit.summary
    assert "recap" not in payload_row


def test_coverage_english_without_a_mutation_record_has_no_mutant_link(tmp_path: Path) -> None:
    journal = tmp_path / "proofs.jsonl"
    coverage_span(journal, coverage_finding(), coverage_evidence(tmp_path))
    audit = next(r for r in compile_packet(journal).rows if r.key == "audit")
    assert "money.py convert: 10 of 16 changed lines never run" in audit.summary
    assert "mutant there survived" not in audit.summary


def test_a_source_missing_from_the_sealed_evidence_is_not_placed(tmp_path: Path) -> None:
    """The module's path for a file the auditor could not read: the lines are
    named and "not placed", the other file is still placed, no text invented."""
    journal = tmp_path / "proofs.jsonl"
    sources = {"money.py": coverage_sources()["money.py"]}
    coverage_span(journal, coverage_finding(), coverage_evidence(tmp_path, sources))
    audit = next(r for r in compile_packet(journal).rows if r.key == "audit")
    assert (
        "  - store.py:43: file not in the tree read; not placed [record: store.py:43]"
        in audit.summary
    )
    assert "  - money.py convert:" in audit.summary
    assert "store.py load_accounts" not in audit.summary


@pytest.mark.parametrize("sealed", [False, True])
def test_a_passing_coverage_finding_renders_no_english(tmp_path: Path, sealed: bool) -> None:
    """Known-bad: a passing finding adds nothing, even with a sidecar on it."""
    journal = tmp_path / "proofs.jsonl"
    finding = {**coverage_finding(), "verdict": "pass", "detail": "every changed line is run"}
    coverage_span(journal, finding, coverage_evidence(tmp_path) if sealed else None)
    packet = compile_packet(journal)
    audit = next(r for r in packet.rows if r.key == "audit")
    assert (audit.status, audit.summary) == ("proven", "")
    assert all("summary" not in r for r in packet.payload()["rows"])


def test_a_failing_coverage_finding_with_nothing_sealed_renders_no_english(tmp_path: Path) -> None:
    journal = tmp_path / "proofs.jsonl"
    coverage_span(journal, coverage_finding(), None)
    packet = compile_packet(journal)
    audit = next(r for r in packet.rows if r.key == "audit")
    assert audit.status == "failed"
    assert audit.summary == ""
    assert all("summary" not in r for r in packet.payload()["rows"])


def test_a_ledger_with_no_coverage_finding_has_no_audit_summary(tmp_path: Path) -> None:
    journal = tmp_path / "proofs.jsonl"
    append_span(journal, tier_span(1, "dead-code", "fail", "x"))
    packet = compile_packet(journal)
    audit = next(r for r in packet.rows if r.key == "audit")
    assert audit.summary == ""


def test_a_source_sealed_as_one_string_over_the_sidecar_cap_is_not_placed(tmp_path: Path) -> None:
    """Known-bad for the cap: `write_attempt_sidecar` cuts a string at 4000
    characters. money.py sealed whole no longer parses; its lines are named
    and "not placed", store.py (sealed as lines) is still placed, and the
    packet does not crash."""
    journal = tmp_path / "proofs.jsonl"
    sealed = coverage_evidence(tmp_path)
    assert len(coverage_sources()["money.py"]) > 4000
    sealed["sources"]["money.py"] = coverage_sources()["money.py"]
    coverage_span(journal, coverage_finding(), sealed)
    audit = next(r for r in compile_packet(journal).rows if r.key == "audit")
    assert (
        "  - money.py:131: file not in the tree read; not placed [record: money.py:131]"
        in audit.summary
    )
    assert "  - store.py load_accounts: 2 of 16 changed lines never run" in audit.summary
    assert "money.py convert" not in audit.summary


@pytest.mark.parametrize("detail", ["no test runs money.py:131", "[1, 2]"])
def test_a_coverage_span_whose_detail_is_not_a_finding_renders_no_english(
    tmp_path: Path, detail: str
) -> None:
    """Known-bad: a sealed sidecar beside a span whose detail is not the JSON
    finding (a bare sentence, or JSON that is not an object) yields no
    summary rather than a crash or invented text. The packet still compiles."""
    journal = tmp_path / "proofs.jsonl"
    coverage_span(journal, coverage_finding(), coverage_evidence(tmp_path), detail=detail)
    packet = compile_packet(journal)
    audit = next(r for r in packet.rows if r.key == "audit")
    assert audit.summary == ""
    assert audit.recap == ""


def test_sources_sealed_as_anything_but_a_map_place_no_line(tmp_path: Path) -> None:
    """Known-bad: `sources` sealed as a list (not the auditor's map of file to
    lines) places nothing; every judged line is "not placed", none is
    attributed to a function whose text was never read."""
    journal = tmp_path / "proofs.jsonl"
    sealed = coverage_evidence(tmp_path)
    sealed["sources"] = list(sealed["sources"].values())
    coverage_span(journal, coverage_finding(), sealed)
    audit = next(r for r in compile_packet(journal).rows if r.key == "audit")
    assert "not placed [record: money.py:131]" in audit.summary
    assert "not placed [record: store.py:43]" in audit.summary
    assert "changed lines never run" not in audit.summary


def test_every_row_has_a_recap_exactly_when_it_has_a_summary(tmp_path: Path) -> None:
    journal = tmp_path / "proofs.jsonl"
    coverage_span(journal, coverage_finding(), coverage_evidence(tmp_path))
    sealed_mutation_span(journal, "fail", "killed 220 of 322 sampled mutants", mutation_outcome())
    packet = compile_packet(journal)
    with_summary = {r.key for r in packet.rows if r.summary}
    assert with_summary == {"mutation", "audit"}
    assert {r.key for r in packet.rows if r.recap} == with_summary
    for r in packet.rows:
        if r.summary:
            assert len(r.recap.splitlines()) < len(r.summary.splitlines())
