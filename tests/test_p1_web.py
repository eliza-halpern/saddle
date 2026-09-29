"""P1 in `saddle web` task runs, and a question that never reads as a pass or a fail.

Contracts:

- W1: `saddle web --extract-requirements` (else `$SADDLE_EXTRACT_REQUIREMENTS`,
  else the key file, else off) makes every chat-started task run extract the
  task text's examples beside the worker, exactly as `saddle auto
  --extract-requirements` does; off, the run's options and records are as before.
- W2: a run whose finish audit asked a question ends needing you: packet
  verdict `needs_you`, card state `asked` (worded "needs you"), the question
  text whole, never "finished", "stopped", "passed" or "failed".
- W3: a `question` finding stays a question when its sealed JSON is longer
  than a ledger line: the auditor fits it (`auditor.sealed_finding`), and a
  reader of an older, cut line reads its exit code 4 as a question.
- W4: the packet and the card say that P1 ran, how long extraction took, and
  how many candidate units the last P1 finding judged, asked about, or left
  unjudged.
"""

from __future__ import annotations

import io
import json
import uuid
from collections.abc import Iterator
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

import pytest
from test_feed import FINISH, Reactive, call, repo
from test_p1_auto import Factory, P1Auditor
from test_task_passes import Scripted
from test_web_tasks import idle, wait_for

import saddle.auto
from saddle import cli
from saddle.auditor import (
    TASK_REQUIREMENTS,
    Auditor,
    AuditorConfig,
    Finding,
    Findings,
    sealed_finding,
)
from saddle.auto import P1_FILE, AutoOptions, AutoResult, create_worktree, ledger_path
from saddle.gates import check_task_requirements
from saddle.journal import (
    JOURNAL_QUESTION_EXIT,
    MAX_SPAN_DETAIL_CHARS,
    P1_EXTRACT_SPAN,
    SEALED_CUT,
    append_span,
    build_span,
    read_spans,
    write_attempt_sidecar,
)
from saddle.packet import compile_packet
from saddle.sessions import SessionStore
from saddle.task_examples import WOULD_REFUSE, Example
from saddle.task_units import task_units
from saddle.transcript import session_line
from saddle.web import tasks
from saddle.web.app import ChatServer, build_app
from saddle.web.tasks import ASKED, ended_state, latest_run_ref

__all__ = ["repo"]

FIX = call("edit_file", "e", path="calc.py", old="a - b", new="a + b")
LONG_QUESTION = (
    'S-002 "`b *= n` repeats the items n times in place." (+S-001) via executed-reference '
    "(3/3), probe (3/3): from box import Box; b = Box([2, 1]); b *= 2; list(b) expected "
    "[1, 1, 2, 2], got [1, 2, 1, 2]; ran changed lines box.py:9 (Box.__imul__) "
) * 3 + f"[{WOULD_REFUSE}]"
BASIS = "question strength: dev-probe floor unmet; " + "not executable S-009: environment; " * 20


def question_finding(detail: str = LONG_QUESTION) -> Finding:
    return Finding(
        TASK_REQUIREMENTS,
        1,
        "question",
        "code-wrong",
        detail,
        ("saddle.gates.check_task_requirements", BASIS),
    )


# -- W3: a question sealed longer than a ledger line --------------------------------


def test_w3_a_long_question_is_sealed_as_json_that_still_parses() -> None:
    text = sealed_finding(question_finding())
    assert len(text) <= MAX_SPAN_DETAIL_CHARS
    body = json.loads(text)
    assert body["verdict"] == "question"
    assert body["detail"].endswith(f"{SEALED_CUT} [{WOULD_REFUSE}]")
    assert body["cites"] == ["saddle.gates.check_task_requirements"]
    # A detail that fits once the basis is dropped is sealed whole, unmarked.
    fits = json.loads(sealed_finding(question_finding("S-001: expected [1], got []")))
    assert fits["detail"] == "S-001: expected [1], got []"
    assert fits["cites"] == ["saddle.gates.check_task_requirements"]


def test_w3_a_short_finding_and_a_long_non_question_are_sealed_as_before() -> None:
    short = Finding(TASK_REQUIREMENTS, 1, "question", "code-wrong", "S-001: got []", ("g", "b"))
    assert json.loads(sealed_finding(short))["cites"] == list(short.cites)
    # Scope: only a question is fitted; a long fail keeps its full JSON (its
    # exit code already says fail), cut by the ledger line as before.
    fail = Finding("coverage", 1, "fail", "evidence-thin", "x" * 900, ("c",))
    assert len(sealed_finding(fail)) > MAX_SPAN_DETAIL_CHARS


def test_w3_a_question_without_the_refusal_mark_gets_none() -> None:
    body = json.loads(sealed_finding(question_finding("P1 could not run: " + "y" * 900)))
    assert body["detail"].endswith(SEALED_CUT)


# -- a seeded run: what the packet and the card read ---------------------------------


def seed_run(
    repo: Path,
    *,
    tier_detail: str | None = None,
    p1_on: bool = True,
    extraction: tuple[int, int, str] | None = (412_300, 0, "12 candidate unit(s), 20 example(s)"),
    stop: str = "needs you: the audit asks 1 question(s): task-requirements (tier 1): S-002",
    findings: tuple[Finding, ...] | None = None,
    p1_field: str = "extracted at run start",
    strength: str = "question",
    final_audit: bool = True,
) -> Path:
    """A ledger as a stopped-needing-you run with P1 leaves it (no model)."""
    rid = uuid.uuid4().hex[:12]
    worktree, branch = create_worktree(repo, rid)
    journal = ledger_path(repo, rid)
    start = build_span(
        node_id="chat#1",
        argv=["auto:start", "make add add"],
        duration_ms=0,
        exit_code=0,
        detail=f"arm E+A+F; branch {branch}; test edits allowed"
        + (f"; task requirements {p1_field}" if p1_on else ""),
        kind="agent",
    )
    append_span(journal, start)
    final = findings if findings is not None else (question_finding(),)
    if tier_detail is None:
        Auditor(worktree, config=AuditorConfig(journal=journal))._journal(
            Findings(tier=1, key="k", findings=final),
            {
                TASK_REQUIREMENTS: {
                    "unjudged": ["not executable S-003: environment"],
                    "units": {"total": 12, "judged": 7, "asked": 2, "unjudged": 3},
                    "examples": {"total": 20, "pass": 15, "code-wrong": 0, "question": 2},
                    "strength": strength,
                }
            },
        )
    else:
        append_span(
            journal,
            build_span(
                node_id="audit",
                argv=["saddle-audit", "tier1", TASK_REQUIREMENTS, "k"],
                duration_ms=0,
                exit_code=JOURNAL_QUESTION_EXIT,
                detail=tier_detail,
                name=f"audit-tier1:{TASK_REQUIREMENTS}",
            ),
        )
    if extraction is not None:
        took, code, detail = extraction
        append_span(
            journal,
            build_span(
                node_id="chat#1",
                argv=[P1_EXTRACT_SPAN, P1_FILE],
                duration_ms=took,
                exit_code=code,
                detail=detail,
                kind="agent",
                name=P1_EXTRACT_SPAN,
                parent_id=start.span_id,
            ),
        )
    evidence: dict[str, Any] = {
        "outcome": "stopped",
        "reason": stop,
        "files_changed": [],
        "tool_span_hashes": [],
    }
    audit = {
        "point": "finish",
        "tree": "t",
        "passed": True,
        "needs_you": True,
        "findings": [
            {"gate": f.gate, "tier": f.tier, "verdict": f.verdict, "detail": f.detail}
            for f in final
        ],
    }
    if final_audit:
        evidence["audit"] = audit
    span_id = uuid.uuid4().hex
    append_span(
        journal,
        build_span(
            node_id="chat#1",
            argv=["auto:stopped"],
            duration_ms=1000,
            exit_code=3,
            detail=f"stopped: {stop}; arm E+A+F",
            kind="agent",
            parent_id=start.span_id,
            span_id=span_id,
            attempt_hash=write_attempt_sidecar(journal, span_id, evidence),
        ),
    )
    return journal


def rows(journal: Path) -> dict[str, Any]:
    return {r.key: r for r in compile_packet(journal).rows}


def test_w2_a_finish_question_ends_needing_you_with_the_question_whole(repo: Path) -> None:
    packet = compile_packet(seed_run(repo))
    assert packet.verdict == "needs_you"
    assert packet.verdict_text.startswith("Needs you: the audit asks 1 question(s)")
    assert "did not finish" in packet.verdict_text
    assert packet.questions == (f"{TASK_REQUIREMENTS} (tier 1): {LONG_QUESTION}",)
    assert packet.payload()["questions"] == list(packet.questions)
    audit = {r.key: r for r in packet.rows}["audit"]
    assert audit.status == "question"
    # The Audit row reads the finding whole from the outcome sidecar, not the cut line.
    assert audit.items == (f"? {TASK_REQUIREMENTS}: tier 1, question: {LONG_QUESTION}",)


def test_w2_a_stop_that_is_not_an_audit_question_stays_stopped(repo: Path) -> None:
    packet = compile_packet(seed_run(repo, stop="time budget spent"))
    assert packet.verdict == "stopped"
    assert packet.questions == ()
    assert "questions" not in packet.payload()


def test_w3_an_older_cut_question_line_reads_as_a_question_not_a_failure(repo: Path) -> None:
    whole = json.dumps({"gate": TASK_REQUIREMENTS, "verdict": "question", "detail": "z" * 900})
    journal = seed_run(repo, tier_detail=whole)  # build_span cuts it: unparseable
    with pytest.raises(json.JSONDecodeError, match="Unterminated"):
        json.loads(read_spans(journal)[1].detail)
    audit = rows(journal)["audit"]
    assert audit.status == "question"
    lines = [session_line(s) for s in read_spans(journal)]
    tier = [x for x in lines if x is not None and TASK_REQUIREMENTS in x.text]
    assert tier[0].tone == "ask"
    assert tier[0].mark == "?"


def test_w4_the_packet_says_p1_ran_how_long_and_what_it_judged(repo: Path) -> None:
    row = rows(seed_run(repo))["p1"]
    assert row.status == "observed"
    assert row.title == "Task text"
    assert row.text.startswith("P1 ran at question strength")
    assert "Extraction took 412.3 s: 12 candidate unit(s), 20 example(s)." in row.text
    assert "judged 7 of 12 candidate unit(s), asked about 2, and left 3 unjudged" in row.text
    assert row.items == (f"Asked: {LONG_QUESTION}",)
    assert len(row.cites) == 2


def test_w4_a_failed_extraction_is_named_and_judged_nothing(repo: Path) -> None:
    journal = seed_run(repo, extraction=(9_000, 1, "RuntimeError: server down"), tier_detail="x")
    row = rows(journal)["p1"]
    assert "Extraction failed after 9.0 s." in row.text
    assert "No P1 finding judged any unit." in row.text
    assert "Extraction failed: RuntimeError: server down" in row.items
    line = next(session_line(s) for s in read_spans(journal) if s.name == P1_EXTRACT_SPAN)
    assert line is not None
    assert line.tone == "ask"


def test_w4_without_p1_there_is_no_task_text_row(repo: Path) -> None:
    passing = Finding("tests", 1, "pass", "code-wrong", "1 passed", ("t",))
    journal = seed_run(repo, p1_on=False, extraction=None, findings=(passing,))
    assert "p1" not in rows(journal)


def test_w4_an_extraction_line_says_its_time_and_counts(repo: Path) -> None:
    journal = seed_run(repo)
    line = next(session_line(s) for s in read_spans(journal) if s.name == P1_EXTRACT_SPAN)
    assert line is not None
    assert line.text == "task text extracted in 412.3s · 12 candidate unit(s), 20 example(s)"
    assert line.tone == "info"


def test_w2_the_card_lines_never_read_a_question_as_passed_or_failed(repo: Path) -> None:
    journal = seed_run(repo)
    delivered = build_span(
        node_id="chat#1",
        argv=["audit", "finish", "t"],
        duration_ms=0,
        exit_code=0,  # a question refuses nothing, so the feed seals it as passed
        detail="[audit finish on tree t: PASS]\n"
        "(question for a person, does not refuse) task-requirements (tier 1): S-002 ...\n"
        "(2 other check(s) passed or not applicable)",
        name="audit:delivered",
    )
    line = session_line(delivered)
    assert line is not None
    assert line.mark == "?"
    assert line.tone == "ask"
    assert "passed" not in line.text
    assert line.text.startswith("audit finish asks a question, delivered to the model")
    stopped = next(session_line(s) for s in read_spans(journal) if s.name == "auto:stopped")
    assert stopped is not None
    assert (stopped.mark, stopped.tone) == ("?", "ask")
    plain = build_span(
        node_id="chat#1",
        argv=["audit", "finish", "t"],
        duration_ms=0,
        exit_code=0,
        detail="[audit finish on tree t: PASS]\n(3 other check(s) passed or not applicable)",
        name="audit:delivered",
    )
    passed = session_line(plain)
    assert passed is not None
    assert passed.tone == "audit"


def test_w2_the_card_state_of_an_ended_question_is_asked() -> None:
    assert ended_state("needs_you") == ASKED
    assert ended_state("stopped") == "stopped"
    assert ended_state("finished") == "finished"
    assert ended_state("unrecorded") == "failed"
    assert ASKED in tasks.ENDED_STATES


def test_w2_a_restarted_server_reads_an_ended_question_as_asked(tmp_path: Path) -> None:
    chat = tmp_path / "chat.jsonl"
    run = tasks.TaskRun(
        run_id="r1", session_id="s", task="t", time_budget_s=1, token_budget=1, journal=tmp_path
    )
    append_span(chat, tasks.run_ref_span(run, "needs_you", "Needs you: ...", "x"))
    assert latest_run_ref(chat) == (ASKED, "t")
    assert read_spans(chat)[0].exit_code == 3


# -- W4: the gate's unit tally varies ------------------------------------------------


def test_w4_the_tally_counts_each_unit_once_by_its_strongest_status() -> None:
    text = "The function must return 1.\n\nIt must return 2 for two.\n\nIt must never raise."
    units = task_units(text)
    ids = [u.id for u in units.units]
    assert len(ids) == 3
    check = check_task_requirements({}, units, [], cannot_run="no file")
    assert check.units == (3, 0, 0, 3)
    none = check_task_requirements({}, units, [])
    assert none.units == (3, 0, 0, 3)
    assert none.examples == (0, 0, 0, 0)


def test_w4_tally_from_rows(monkeypatch: pytest.MonkeyPatch) -> None:
    """judged / asked / unjudged each move with the rows' statuses."""
    from saddle import gates
    from saddle.task_examples import Row

    text = "The function must return 1.\n\nIt must return 2 for two.\n\nIt must never raise."
    units = task_units(text)
    a, b, c = (u.id for u in units.units)
    statuses = {"e1": "pass", "e2": "question", "e3": "pass"}

    def judge(ex: Example, klass: Any, got: Any, *, licensed: bool) -> Row:
        return Row(ex, statuses[ex.id], "", klass, "")  # type: ignore[arg-type]

    monkeypatch.setattr(gates, "judge", judge)
    stub = SimpleNamespace(expected=None, note="stub")
    monkeypatch.setattr(gates, "classify_example", lambda e, u: stub)
    exs = [
        Example(id="e1", units=(a,), setup=(), call="f()", predictions=()),
        Example(id="e2", units=(a, b), setup=(), call="f()", predictions=()),
        Example(id="e3", units=(a,), setup=(), call="f()", predictions=()),
    ]
    check = check_task_requirements({}, units, exs)
    assert check.units == (3, 0, 2, 1)  # a is asked (strongest), b asked, c unjudged
    assert check.examples == (3, 2, 0, 1)
    statuses["e2"] = "pass"
    check = check_task_requirements({}, units, exs)
    assert check.units == (3, 2, 0, 1)
    assert check.verdict == "pass"
    assert c not in {u for e in exs for u in e.units}


# -- W1: the switch --------------------------------------------------------------------


def web_args(*flags: str) -> Any:
    return cli.build_parser().parse_args(["web", *flags])


def key_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, text: str) -> None:
    path = tmp_path / "env"
    path.write_text(text)
    monkeypatch.setattr(cli, "KEY_FILE", str(path))


@pytest.mark.parametrize(
    ("flags", "env", "file", "want"),
    [
        ((), None, None, "off"),
        ((), None, "SADDLE_EXTRACT_REQUIREMENTS=1\n", "on"),
        ((), "0", "SADDLE_EXTRACT_REQUIREMENTS=1\n", "off"),
        ((), "yes", "SADDLE_EXTRACT_REQUIREMENTS=0\n", "on"),
        (("--extract-requirements",), "off", "SADDLE_EXTRACT_REQUIREMENTS=off\n", "on"),
        (("--no-extract-requirements",), "on", "SADDLE_EXTRACT_REQUIREMENTS=on\n", "off"),
    ],
)
def test_w1_the_switch_resolves_flag_then_environment_then_file_then_off(
    flags: tuple[str, ...],
    env: str | None,
    file: str | None,
    want: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    key_file(tmp_path, monkeypatch, file or "")
    if env is None:
        monkeypatch.delenv(cli.EXTRACT_REQUIREMENTS_ENV, raising=False)
    else:
        monkeypatch.setenv(cli.EXTRACT_REQUIREMENTS_ENV, env)
    assert cli.web_extract_requirements(web_args(*flags)).value == want


def test_w1_a_value_that_is_not_on_or_off_is_an_error_not_off(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    key_file(tmp_path, monkeypatch, "")
    monkeypatch.setenv(cli.EXTRACT_REQUIREMENTS_ENV, "onn")
    with pytest.raises(cli.SettingError, match="'onn'"):
        cli.web_extract_requirements(web_args())
    err = io.StringIO()
    argv = ["web", "--no-open", "--workdir", str(tmp_path / "w")]
    monkeypatch.setenv("SADDLE_VLLM_API_KEY", "k-test-value")
    assert cli.main(argv, stdout=io.StringIO(), stderr=err) == 2
    assert "SADDLE_EXTRACT_REQUIREMENTS 'onn'" in err.getvalue()


@pytest.mark.parametrize(("flags", "want"), [((), False), (("--extract-requirements",), True)])
def test_w1_the_resolved_switch_reaches_serve(
    flags: tuple[str, ...], want: bool, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from saddle.web import app as web_app

    key_file(tmp_path, monkeypatch, "")
    monkeypatch.delenv(cli.EXTRACT_REQUIREMENTS_ENV, raising=False)
    monkeypatch.setenv("SADDLE_VLLM_API_KEY", "k-test-value")
    served: dict[str, object] = {}
    monkeypatch.setattr(web_app, "serve", lambda **kw: served.update(kw))
    out = io.StringIO()
    argv = ["web", "--no-open", "--workdir", str(tmp_path / "w"), *flags]
    assert cli.main(argv, stdout=out) == 0
    assert served["extract_requirements"] is want
    assert ("task runs check the task text's examples" in out.getvalue()) is want


class Client(Reactive):
    """The worker's script and the extraction's passes, as a context manager."""

    def __init__(self, scripted: Scripted) -> None:
        super().__init__([[FIX], [FINISH]])
        self.scripted = scripted

    def __enter__(self) -> Client:
        return self

    def __exit__(self, *_: object) -> None:
        return None

    def max_model_len(self) -> int:
        return 200_000

    def complete(self, prompt: str, **kw: Any) -> str:
        return self.scripted.complete(prompt, **kw)


class Asking(P1Auditor):
    """Tier 1 asks a question once it has a P1 file (a real one is sealed)."""

    def tier1(self, tree: Path | None = None) -> Findings:
        got = super().tier1(tree)
        if self.config.task_requirements is None:
            return got
        kept = tuple(f for f in got.findings if f.gate != TASK_REQUIREMENTS)
        return Findings(tier=1, key="k1", findings=(*kept, question_finding()))


class AskingFactory(Factory):
    def __call__(self, repo: Path, baseline: str, config: AuditorConfig) -> P1Auditor:
        self.configs.append(config)
        return Asking(config)


def serve_task(
    store: SessionStore, repo: Path, factory: Factory, *, on: bool
) -> Iterator[tuple[Any, ChatServer, str, str]]:
    from starlette.testclient import TestClient

    app = build_app(
        store,
        lambda: Client(Scripted("no inputs, not json")),
        default_workdir=repo,
        feed_auditor=factory,
        extract_requirements=on,
    )
    server = next(
        cell.cell_contents
        for route in app.routes  # type: ignore[attr-defined]
        for cell in (getattr(getattr(route, "endpoint", None), "__closure__", None) or ())
        if isinstance(cell.cell_contents, ChatServer)
    )
    with TestClient(app) as client:
        sid = client.post("/api/sessions").json()["id"]
        assert client.get("/api/task-policy").json()["task_text_check"] is on
        rid = client.post(f"/api/sessions/{sid}/task", json={"text": "make add add"}).json()[
            "run_id"
        ]
        wait_for(lambda: idle(server, sid))
        yield client, server, sid, rid


def test_w1_w2_on_a_web_task_extracts_beside_the_worker_and_ends_needing_you(
    repo: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    seen: list[AutoOptions] = []
    real = saddle.auto.run_auto

    def spy(options: AutoOptions, *a: Any, **kw: Any) -> AutoResult:
        seen.append(options)
        return real(options, *a, **kw)

    monkeypatch.setattr(tasks, "run_auto", spy)
    factory = AskingFactory()
    store = SessionStore(tmp_path / "sessions")
    for client, server, sid, rid in serve_task(store, repo, factory, on=True):
        run = server.tasks[rid]
        packet = client.get(f"/api/sessions/{sid}/tasks/{rid}/packet").json()
    assert [o.extract_requirements for o in seen] == [True]
    assert run.journal is not None
    sealed = run.journal.parent / P1_FILE
    assert factory.configs[-1].task_requirements == sealed
    assert run.state == ASKED
    assert packet["verdict"] == "needs_you"
    assert packet["questions"] == [f"{TASK_REQUIREMENTS} (tier 1): {LONG_QUESTION}"]
    p1 = {r["key"]: r for r in packet["rows"]}["p1"]
    assert "Extraction took" in p1["text"]
    assert p1["items"] == [f"Asked: {LONG_QUESTION}"]
    extracted = [s for s in read_spans(run.journal) if s.name == P1_EXTRACT_SPAN]
    assert len(extracted) == 1
    assert extracted[0].exit_code == 0
    assert extracted[0].detail.endswith("0 probe(s)")
    assert extracted[0].started_at  # the trial report dates a run by it
    lines = [line.text for line in run.lines]
    assert any(t.startswith("task text extracted in ") for t in lines)
    assert latest_run_ref(store.journal_path(sid)) == (ASKED, "make add add")


def test_w1_off_a_web_task_runs_no_extraction(repo: Path, tmp_path: Path) -> None:
    factory = AskingFactory()
    store = SessionStore(tmp_path / "sessions")
    for client, server, sid, rid in serve_task(store, repo, factory, on=False):
        run = server.tasks[rid]
        packet = client.get(f"/api/sessions/{sid}/tasks/{rid}/packet").json()
    assert run.journal is not None
    assert not (run.journal.parent / P1_FILE).exists()
    assert {c.task_requirements for c in factory.configs} == {None}
    assert run.state == "finished"
    assert "p1" not in {r["key"] for r in packet["rows"]}
    assert not [s for s in read_spans(run.journal) if s.name == P1_EXTRACT_SPAN]


def test_w4_a_failed_extraction_seals_its_span_and_the_run_asks(
    repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from saddle import auto

    def down(*_: Any, **__: Any) -> Any:
        msg = "server down"
        raise RuntimeError(msg)

    monkeypatch.setattr(auto, "extract_requirements", down)
    options = AutoOptions(
        task="make add add",
        repo=repo,
        run_id="down",
        auditor_factory=Factory(),
        extract_requirements=True,
    )
    result = auto.run_auto(options, cast(Any, Client(Scripted("x"))))
    spans = [s for s in read_spans(result.journal) if s.name == P1_EXTRACT_SPAN]
    assert [(s.exit_code, s.detail) for s in spans] == [(1, "RuntimeError: server down")]
    packet = compile_packet(result.journal)
    assert packet.verdict == "needs_you"
    assert "the extraction failed: RuntimeError: server down" in packet.questions[0]
    assert (
        "Extraction failed: RuntimeError: server down"
        in {r.key: r for r in packet.rows}["p1"].items
    )


def test_w2_a_needs_you_run_does_not_merge_and_says_why_in_words(repo: Path) -> None:
    from saddle.web.branch_actions import merge_refusal

    packet = compile_packet(seed_run(repo))
    assert merge_refusal(packet).startswith("The run is needing you, not finished")


def test_w4_a_given_file_runs_no_extraction_and_says_so(repo: Path) -> None:
    row = rows(seed_run(repo, extraction=None, p1_field="/tmp/f.json"))["p1"]
    assert "No extraction ran: a sealed file was given." in row.text
    lost = rows(seed_run(repo, extraction=None))["p1"]
    assert "The extraction left no record." in lost.text


def test_w4_full_strength_and_a_question_read_from_the_ledger_alone(repo: Path) -> None:
    short = question_finding("S-001: expected [1], got [] [would refuse at full strength]")
    journal = seed_run(repo, findings=(short,), strength="full", final_audit=False)
    packet = compile_packet(journal)
    row = {r.key: r for r in packet.rows}["p1"]
    assert row.text.startswith("P1 ran at full strength. Extraction took")
    assert row.items == (f"Asked: {short.detail}",)
    assert packet.questions == (f"{TASK_REQUIREMENTS} (tier 1): {short.detail}",)


def test_w4_a_passing_p1_finding_asks_nothing(repo: Path) -> None:
    passed = Finding(TASK_REQUIREMENTS, 1, "pass", "code-wrong", "20 example(s) matched", ("g",))
    journal = seed_run(repo, findings=(passed,), stop="time budget spent")
    row = rows(journal)["p1"]
    assert row.status == "observed"
    assert row.items == ()
    assert "judged 7 of 12 candidate unit(s)" in row.text
