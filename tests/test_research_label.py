"""A session that read the web says "used web research" on its packet (#93 item).

The `research` tool's results are marked untrusted in the transcript, yet nothing on a
session's change said web content informed it: a person reviewing the change had to read the
whole transcript to find out. A `research` call seals one tool span in the session's own
journal, exactly as any other tool call does, so `sessions.session_used_research` reads the
fact back from spans the session stored, and `packet.compile_packet` puts `packet.USED_RESEARCH`
in the header the packet card paints, in the `packet.md` report held beside the run's ledger,
and in the recap the chat stores as a message. A session with no such call gets no label.

Known-good: a call counts whatever it returned -- a typed value, a gate's refusal (`exit 2`),
or a call that died mid-turn (exit 1); a session with several calls; the real `research` tool in
the real turn (the acting model asks, the reader reports a typed value) journalled in a session
directory; the packet, the report on disk and the branch recap of a run of that session, read
through a second store and a second app after the first is gone, which is what a reload is;
the recap a finished run leaves in the chat, kept as a message and read back after a restart;
and the packet card itself in headless Chrome, whose header band is painted by `renderPacket`
from `packet.header` in `src/saddle/web/static/tasks.js`, read twice through a reopened store
and a new app.

Known-bad: a session that only ran commands or wrote files; the reader's own calls, which are
not the session's call; `research` named by a span that was not sealed as a tool call; a tool
named `web_research`; a session that sealed nothing at all; the packet of a terminal `saddle
auto` run, compiled without the answer because no session read anything; a run of a session that
only touched research-named files; a finished run of a quiet session, whose card, report, recap
and `packet.md` name no research at all.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Literal, cast

import pytest
from chrome_page import drive_page
from packet_seed import make_repo, seed
from starlette.testclient import TestClient
from test_chat_engine import FakeClient, content, tool
from test_research import Rig, needs_bwrap
from test_ui3_mode import NoModel, serving

from saddle import packet, sessions
from saddle.engine import TurnOptions, run_turn
from saddle.journal import append_span, build_span, read_spans
from saddle.packet import compile_packet, render_packet_text
from saddle.research import BANNER, REPORT_TOOL, RESEARCH_TOOL
from saddle.sessions import SessionStore
from saddle.tools import ToolContext, scope_turn
from saddle.vllm import VllmClient
from saddle.web import tasks
from saddle.web.app import build_app

RESEARCH_CALL = [RESEARCH_TOOL, '{"question":"which version?","want":"value"}']
OFF_ALLOWLIST = [RESEARCH_TOOL, '{"question":"an address the gate turns down"}']
DEAD_READER = [RESEARCH_TOOL, '{"question":"which version?"}']
ANOTHER_CALL = [RESEARCH_TOOL, '{"question":"and its price?","want":"summary"}']
LOOK_ALIKE = ["web_research", '{"question":"which version?"}']
READER_FETCH = ["mcp__web__fetch", '{"url":"https://docs.example/downloads"}']
READER_REPORT = [REPORT_TOOL, '{"kind":"none","reason":"not_found"}']
COMMAND = ["run_command", '{"command":"python -m pytest -q"}']
LS = ["run_command", '{"command":"ls"}']
EDIT = ["write_file", '{"path":"calc.py","content":"x"}']
NOTE = ["read_file", '{"path":"research-notes.md"}']

Call = tuple[list[str], Literal["tool", "agent"], int]


def call(argv: list[str], *, kind: Literal["tool", "agent"] = "tool", exit_code: int = 0) -> Call:
    return (argv, kind, exit_code)


def seal(
    journal: Path, argv: list[str], *, kind: Literal["tool", "agent"] = "tool", exit_code: int = 0
) -> None:
    """Seal one completed call in a journal, with its kind and exit code, as `run_turn` seals it."""
    append_span(
        journal,
        build_span(
            node_id="chat#1",
            argv=argv,
            duration_ms=12,
            exit_code=exit_code,
            detail="seeded",
            kind=kind,
        ),
    )


def one_session(tmp_path: Path, calls: list[Call]) -> SessionStore:
    """A store holding one session whose own journal sealed `calls` (argv, kind, exit code)."""
    store = SessionStore(tmp_path / "sessions")
    sid = store.create(title="research work", workdir=str(tmp_path)).id
    for argv, kind, code in calls:
        seal(store.journal_path(sid), argv, kind=kind, exit_code=code)
    return store


# -- the span a session leaves, and the fact it carries ----------------------------------


CASES: list[tuple[list[Call], bool]] = [
    # the reader brought back the typed value the acting model asked for
    ([call(RESEARCH_CALL)], True),
    # the gate refused an address the acting model composed: still a call the session made
    ([call(OFF_ALLOWLIST, exit_code=2)], True),
    # the reader's server died mid-call
    ([call(DEAD_READER, exit_code=1)], True),
    # several calls, with commands and edits between them
    ([call(LS), call(RESEARCH_CALL), call(EDIT), call(ANOTHER_CALL)], True),
    # known-bad: a session that only ever worked on its checkout
    ([call(COMMAND)], False),
    ([call(EDIT), call(NOTE)], False),
    # known-bad: the reader's own calls, which are not the session's call
    ([call(READER_FETCH), call(READER_REPORT)], False),
    # known-bad: `research` named by a span that was not sealed as a tool call
    ([call(RESEARCH_CALL, kind="agent")], False),
    # known-bad: a tool whose name only looks like the research tool
    ([call(LOOK_ALIKE)], False),
    # known-bad: a session that sealed nothing at all
    ([], False),
]


@pytest.mark.parametrize(("calls", "want"), CASES)
def test_a_session_that_called_research_says_so_and_one_that_did_not_says_nothing(
    tmp_path: Path, calls: list[Call], want: bool
) -> None:
    store = one_session(tmp_path, calls)
    assert store.research_used(store.list()[0].id) is want


def test_the_call_is_read_from_the_journal_so_a_restarted_store_still_says_it(
    tmp_path: Path,
) -> None:
    """The label comes from what the session stored, not from anything a live turn kept."""
    sid = one_session(tmp_path, [call(RESEARCH_CALL)]).list()[0].id
    reopened = SessionStore(tmp_path / "sessions")
    assert reopened.research_used(sid) is True
    assert sessions.session_used_research(reopened.journal_path(sid)) is True


@needs_bwrap
def test_the_call_the_acting_model_made_through_the_reader_is_the_sessions_call(
    tmp_path: Path,
) -> None:
    """The real `research` tool in a real turn, journalled as a session journals its turns."""
    store = one_session(tmp_path, [])
    sid = store.list()[0].id
    journal = store.journal_path(sid)
    rig = Rig(tmp_path)
    try:
        client = FakeClient(
            [
                # the acting model asks
                [tool("research", question="what is the latest version?", want="value")],
                # the reader reads the page the person named, then reports
                [tool("mcp__web__fetch", url="https://docs.example/downloads")],
                [tool("report", kind="value", value_type="version", value="4.2.0")],
                # the acting model answers from the typed value
                [content("The latest version is 4.2.0.")],
            ]
        )
        ctx = ToolContext(workdir=tmp_path, research=rig.researcher)
        options = TurnOptions(workdir=tmp_path, journal=journal, tools=scope_turn(ctx, "ask"))
        messages: list[dict[str, Any]] = []
        list(
            run_turn(
                cast(VllmClient, client),
                messages,
                "Read https://docs.example/downloads and tell me the latest version",
                options,
                turn=1,
                context=ctx,
            )
        )
        told = [m["content"] for m in messages if m["role"] == "tool"]
        assert len(told) == 1
        assert told[0].startswith(BANNER)  # the call really reached the reader
        assert "value (version): 4.2.0" in told[0]  # and a typed value came back
    finally:
        rig.close()
    assert store.research_used(sid) is True
    sealed = read_spans(journal)
    nodes = [s.node_id for s in sealed if s.kind == "tool"]
    assert nodes.count("chat#1#reader") == 1  # the reader's own call is not the session's
    research_calls = [s for s in sealed if s.kind == "tool" and s.argv[:1] == [RESEARCH_TOOL]]
    assert [s.node_id for s in research_calls] == ["chat#1"]  # the session's call is
    # A neighbour session of the same store that made no call is not labelled by it.
    fresh = store.create(title="fresh", workdir=str(tmp_path)).id
    assert store.research_used(fresh) is False


# -- the packet a person reviewing the run reads -----------------------------------------


def run_in_a_session(tmp_path: Path, *, research: bool) -> tuple[SessionStore, str, str, Path]:
    """One seeded `audited` run in its own session: (store, session id, run id, run journal)."""
    repo = make_repo(tmp_path / "repo")
    store = SessionStore(tmp_path / "sessions")
    sid, rid, _branch = seed(store, repo, "audited")
    if research:
        seal(store.journal_path(sid), RESEARCH_CALL)
    journal = tasks.journal_for(store.journal_path(sid), rid)
    assert journal is not None
    return store, sid, rid, journal


def packet_of(tmp_path: Path, *, research: bool) -> tuple[str, ...]:
    """The header of that run's packet, given the answer the web routes give it."""
    store, sid, rid, journal = run_in_a_session(tmp_path, research=research)
    return compile_packet(journal, run_id=rid, used_research=store.research_used(sid)).header


def test_the_packet_of_a_run_whose_session_read_the_web_names_it(tmp_path: Path) -> None:
    store, sid, rid, journal = run_in_a_session(tmp_path, research=True)
    compiled = compile_packet(journal, run_id=rid, used_research=store.research_used(sid))
    assert compiled.header.count(packet.USED_RESEARCH) == 1  # the run a reviewer sees says it
    assert packet.USED_RESEARCH == "used web research"
    assert render_packet_text(compiled).count(packet.USED_RESEARCH) == 1  # once, in the header
    assert [
        row.title for row in compiled.rows if packet.USED_RESEARCH in row.title
    ] == []  # not a row, so the packet still says it once
    assert compiled.verdict == "finished"  # the label says nothing about the verdict


def test_a_packet_for_a_session_that_read_nothing_names_nothing(tmp_path: Path) -> None:
    quiet = packet_of(tmp_path, research=False)
    assert not [item for item in quiet if "research" in item], quiet
    # The terminal `saddle auto` path: no session read the web, so nothing is supplied.
    _store, _sid, rid, journal = run_in_a_session(tmp_path / "auto", research=False)
    plain = compile_packet(journal, run_id=rid)
    assert packet.USED_RESEARCH not in plain.header
    assert "research" not in " ".join(plain.header)


def test_a_run_of_a_session_that_only_touched_research_named_files_says_nothing(
    tmp_path: Path,
) -> None:
    store, sid, rid, journal = run_in_a_session(tmp_path, research=False)
    seal(store.journal_path(sid), LOOK_ALIKE)
    seal(store.journal_path(sid), NOTE)
    header = compile_packet(journal, run_id=rid, used_research=store.research_used(sid)).header
    assert packet.USED_RESEARCH not in header, header


# -- the routes a person reviewing the change reads, lived and reloaded -----------------


def client_for(store: SessionStore, workdir: Path) -> TestClient:
    return TestClient(build_app(store, NoModel, default_workdir=workdir))


def test_the_packet_route_names_the_session_that_read_the_web(tmp_path: Path) -> None:
    store, sid, rid, _journal = run_in_a_session(tmp_path, research=True)
    with client_for(store, tmp_path / "repo") as client:
        got = client.get(f"/api/sessions/{sid}/tasks/{rid}/packet")
        assert got.status_code == 200, got.text
        header = got.json()["header"]
        assert header.count(packet.USED_RESEARCH) == 1, header


def test_the_packet_route_says_nothing_for_a_session_that_did_not(tmp_path: Path) -> None:
    store, sid, rid, _journal = run_in_a_session(tmp_path, research=False)
    with client_for(store, tmp_path / "repo") as client:
        got = client.get(f"/api/sessions/{sid}/tasks/{rid}/packet")
        assert got.status_code == 200, got.text
        header = got.json()["header"]
        assert not [item for item in header if "research" in item], header


def test_the_full_report_and_the_branch_recap_carry_the_label(tmp_path: Path) -> None:
    store, sid, rid, journal = run_in_a_session(tmp_path, research=True)
    with client_for(store, tmp_path / "repo") as client:
        report = client.get(f"/api/sessions/{sid}/tasks/{rid}/packet.md")
        assert report.status_code == 200, report.text
        assert report.text.count(packet.USED_RESEARCH) == 1
        recap = client.get(f"/api/sessions/{sid}/tasks/{rid}/branch").json()["recap"]
        assert recap.count(packet.USED_RESEARCH) == 1, recap
    # The file a person downloads sits beside the run's ledger, label and all.
    written = (journal.parent / "packet.md").read_text(encoding="utf-8")
    assert written.count(packet.USED_RESEARCH) == 1, written


def test_the_report_and_recap_of_a_session_that_read_nothing_carry_nothing(tmp_path: Path) -> None:
    store, sid, rid, journal = run_in_a_session(tmp_path, research=False)
    with client_for(store, tmp_path / "repo") as client:
        report = client.get(f"/api/sessions/{sid}/tasks/{rid}/packet.md")
        assert packet.USED_RESEARCH not in report.text, report.text
        branch = client.get(f"/api/sessions/{sid}/tasks/{rid}/branch").json()
        assert packet.USED_RESEARCH not in branch["recap"], branch["recap"]
    assert packet.USED_RESEARCH not in (journal.parent / "packet.md").read_text(encoding="utf-8")


def test_the_label_is_still_there_when_a_second_server_reads_the_same_session(
    tmp_path: Path,
) -> None:
    """A reload: nothing the first server held is kept, so the session's own files decide."""
    store, sid, rid, _journal = run_in_a_session(tmp_path, research=True)
    with client_for(store, tmp_path / "repo") as client:
        first = client.get(f"/api/sessions/{sid}/tasks/{rid}/packet").json()["header"]
    assert first.count(packet.USED_RESEARCH) == 1, first

    reopened = SessionStore(tmp_path / "sessions")
    with client_for(reopened, tmp_path / "repo") as client:
        again = client.get(f"/api/sessions/{sid}/tasks/{rid}/packet").json()["header"]
    assert again == first, again

    quiet = run_in_a_session(tmp_path / "quiet", research=False)
    quiet_header = (
        client_for(quiet[0], tmp_path / "quiet" / "repo")
        .get(f"/api/sessions/{quiet[1]}/tasks/{quiet[2]}/packet")
        .json()["header"]
    )
    assert quiet_header.count(packet.USED_RESEARCH) == 0, quiet_header


def test_the_research_fact_reaches_a_packet_compiled_after_the_run_it_describes(
    tmp_path: Path,
) -> None:
    """The answer is read when the packet is compiled, not when the call happened."""
    store, sid, rid, _journal = run_in_a_session(tmp_path, research=False)
    with client_for(store, tmp_path / "repo") as client:
        before = client.get(f"/api/sessions/{sid}/tasks/{rid}/packet").json()["header"]
    assert before.count(packet.USED_RESEARCH) == 0, before
    seal(store.journal_path(sid), RESEARCH_CALL)
    with client_for(store, tmp_path / "repo") as client:
        after = client.get(f"/api/sessions/{sid}/tasks/{rid}/packet").json()["header"]
    assert after.count(packet.USED_RESEARCH) == 1, after


# -- and on the packet card a real browser paints ---------------------------------------

HEAD_ITEMS = """
await page.chat(args.sid);
await page.until(() => !!document.querySelector(".packet .verdict-word"));
return await page.js(
  () => [...document.querySelectorAll(".packet .verdict-meta span")].map((s) => s.textContent)
);
"""


def head_items(tmp_path: Path, *, research: bool, reloads: int = 1) -> list[list[str]]:
    """That run's packet-card header items, read from a fresh page `reloads` times."""
    repo = make_repo(tmp_path / "repo")
    store = SessionStore(tmp_path / "sessions")
    sid, _rid, _branch = seed(store, repo, "audited")
    if research:
        seal(store.journal_path(sid), RESEARCH_CALL)
    seen: list[list[str]] = []
    for _try in range(reloads):
        # A reopened store and a new app: the page can only read what the session stored.
        reopened = SessionStore(tmp_path / "sessions")
        with serving(build_app(reopened, NoModel, default_workdir=repo)) as base:
            seen.append(drive_page(base, HEAD_ITEMS, sid=sid, timeout=120))
    return seen


def test_a_packet_card_in_a_real_browser_says_the_session_read_the_web(tmp_path: Path) -> None:
    first, again = head_items(tmp_path, research=True, reloads=2)
    # The label a person is owed, spelled as they will read it, painted by the packet card.
    assert [item for item in first if "research" in item] == ["used web research"], first
    assert sorted(first) == sorted(again), (first, again)  # a reload says the same thing


def test_a_packet_card_for_a_session_that_read_nothing_says_nothing(tmp_path: Path) -> None:
    items = head_items(tmp_path, research=False)[0]
    assert not [item for item in items if "research" in item], items


# -- and into the recap the chat keeps, which is what a reloaded page draws ---------------


def execute_a_run(
    tmp_path: Path, store: SessionStore, sid: str, rid: str, monkeypatch: pytest.MonkeyPatch
) -> dict[str, Any] | None:
    """`tasks.execute` over the seeded ledger, with the model run itself stubbed out."""

    def no_model_run(_options: Any, _client: Any, **_: Any) -> None:
        return None

    monkeypatch.setattr(tasks, "run_auto", no_model_run)
    run = tasks.TaskRun(
        run_id=rid,
        session_id=sid,
        task="look up the version",
        time_budget_s=60.0,
        token_budget=100000,
    )
    _verdict, recap = tasks.execute(
        run,
        workdir=tmp_path / "repo",
        client=NoModel,  # the test's `run_auto` never reaches the model
        publish=lambda _event: None,
        chat_journal=store.journal_path(sid),
        reasoning_effort="medium",
        audit=None,
    )
    return recap


def test_the_recap_a_researching_run_leaves_in_the_chat_carries_the_label(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = make_repo(tmp_path / "repo")
    store = SessionStore(tmp_path / "sessions")
    sid, rid, _branch = seed(store, repo, "audited")
    seal(store.journal_path(sid), RESEARCH_CALL)
    recap = execute_a_run(tmp_path, store, sid, rid, monkeypatch)
    assert recap is not None, recap
    assert recap["content"].count(packet.USED_RESEARCH) == 1, recap["content"]
    # What a reloaded page draws is the stored message, and the label is inside it.
    store.save_messages(sid, [*store.load_messages(sid), recap])
    kept = SessionStore(tmp_path / "sessions").load_messages(sid)
    assert [m for m in kept if packet.USED_RESEARCH in str(m.get("content", ""))] == [recap]


def test_the_recap_a_run_of_a_quiet_session_leaves_says_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = make_repo(tmp_path / "repo")
    store = SessionStore(tmp_path / "sessions")
    sid, rid, _branch = seed(store, repo, "audited")
    seal(store.journal_path(sid), COMMAND)
    seal(store.journal_path(sid), NOTE)
    recap = execute_a_run(tmp_path, store, sid, rid, monkeypatch)
    assert recap is not None, recap
    assert packet.USED_RESEARCH not in recap["content"], recap["content"]
