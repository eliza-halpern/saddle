"""The MCP client (#139): an allowlisted tool is callable, nothing else is.

Every behaviour is checked against a real MCP server (`mcp_fixture_server.py`,
the SDK's own server side over stdio), started the way saddle starts one.

Known-good: an approved, allowlisted tool is offered and called, its call is
journaled like a built-in tool's, the server shows in the session's process
list, and a full-access session's server runs outside the sandbox.

Known-bad: a tool, a server or a lane the allowlist does not name is refused
before anything reaches the server; a server the person has not approved, or
whose descriptions changed since, is refused; a crashed or hung server is a
named failure, never an empty result; ending access stops the server.
"""

from __future__ import annotations

import json
import time
from collections.abc import Iterator
from pathlib import Path
from typing import Any, cast

import pytest
from mcp_support import BWRAP, calls, mcp_names, server_entry, write_config
from test_chat_engine import FakeClient, content, tool
from test_memcap import needs_cgroup

from saddle.engine import TurnOptions, run_turn
from saddle.mcp_cmd import run_mcp
from saddle.mcpclient import Approvals, McpError, McpHost, load_config
from saddle.procs import ProcessLedger
from saddle.tools import ToolContext, execute_tool, scope_turn
from saddle.vllm import ToolCall, VllmClient

pytest.importorskip("mcp", reason="the MCP SDK (the saddle-harness[mcp] extra) is not installed")


class Session:
    """A chat session's context with MCP attached, as the web chat builds one."""

    def __init__(
        self, workdir: Path, servers: dict[str, Any], *, full: bool, approve: bool = True
    ) -> None:
        self.workdir = workdir
        self.config_file = write_config(workdir / "mcp.json", servers)
        self.approvals = Approvals(workdir / "approved.json")
        self.ledger = ProcessLedger()
        self.ctx = ToolContext(workdir=workdir, full_access=full, processes=self.ledger)
        self.ctx.mcp = McpHost(
            load_config(self.config_file),
            self.approvals,
            self.ctx.box,
            call_timeout=3,
        )
        if approve:
            self.approve_all()

    def approve_all(self) -> None:
        assert self.ctx.mcp is not None
        for name in self.ctx.mcp.config:
            spec, tools, _ = self.ctx.mcp.review(name)
            self.approvals.approve(spec, tools)

    def call(self, name: str, **arguments: Any) -> str:
        return execute_tool(
            ToolCall(id="c1", name=name, arguments=json.dumps(arguments)),
            workdir=self.workdir,
            context=self.ctx,
        )

    def close(self) -> None:
        self.ctx.stop_processes()


@pytest.fixture
def full(tmp_path: Path) -> Iterator[Session]:
    session = Session(
        tmp_path,
        {
            "fx": server_entry(
                tmp_path, tools=["echo", "add", "crash", "hang", "env", "sees", "fail"]
            )
        },
        full=True,
    )
    scope_turn(session.ctx, "edit")
    yield session
    session.close()


# -- a real server: the known-good path -----------------------------------------


def test_only_the_allowlisted_tools_are_offered_by_name(full: Session) -> None:
    offered = [t["function"]["name"] for t in scope_turn(full.ctx, "edit")]
    assert "mcp__fx__echo" in offered
    assert "mcp__fx__add" in offered
    assert "mcp__fx__secret" not in offered  # the server has it; the allowlist does not
    echo = next(t for t in scope_turn(full.ctx, "edit") if t["function"]["name"] == "mcp__fx__echo")
    assert echo["function"]["description"].endswith("Return the text unchanged.")
    assert "text" in echo["function"]["parameters"]["properties"]


def test_an_allowlisted_tool_is_called_and_returns_its_text(full: Session) -> None:
    assert full.call("mcp__fx__echo", text="hello") == "hello"
    assert full.call("mcp__fx__add", a=2, b=3) == "5"
    assert calls(full.workdir) == ["echo hello", "add 2 3"]


def test_a_tool_that_reports_its_own_error_is_an_error_result(full: Session) -> None:
    result = full.call("mcp__fx__fail")
    assert result.startswith("error: MCP tool 'mcp__fx__fail' reported an error")


def test_a_call_is_journaled_like_a_built_in_tool_call(full: Session) -> None:
    journal = full.workdir / "journal.jsonl"
    client = FakeClient(
        [
            [tool("mcp__fx__echo", text="journal me")],
            [content("done")],
        ]
    )
    options = TurnOptions(workdir=full.workdir, journal=journal, tools=scope_turn(full.ctx, "edit"))
    list(
        run_turn(cast(VllmClient, client), [], "go", options, turn=1, parent=None, context=full.ctx)
    )
    spans = [json.loads(line) for line in journal.read_text().splitlines()]
    mine = [s for s in spans if "mcp__fx__echo" in json.dumps(s)]
    assert mine, spans
    text = json.dumps(mine)
    assert "journal me" in text


# -- refused before it reaches the server ---------------------------------------


def test_a_tool_the_allowlist_does_not_name_is_refused_and_never_reaches_the_server(
    full: Session,
) -> None:
    result = full.call("mcp__fx__secret")
    assert result.startswith("error: unknown tool")
    assert "secret" not in calls(full.workdir)


def test_an_unlisted_server_is_refused(full: Session) -> None:
    assert full.call("mcp__other__echo", text="x").startswith("error: unknown tool")


def test_an_argument_the_tool_does_not_declare_is_refused_before_the_call(full: Session) -> None:
    result = full.call("mcp__fx__echo", text="x", command="rm -rf /")
    assert result.startswith("error: echo does not take command")
    assert calls(full.workdir) == []


def test_a_reader_server_is_not_offered_to_or_callable_by_the_acting_session(
    tmp_path: Path,
) -> None:
    session = Session(
        tmp_path,
        {"web": server_entry(tmp_path, tools=["echo"], access="reader")},
        full=True,
    )
    try:
        assert mcp_names(scope_turn(session.ctx, "edit")) == []
        assert session.call("mcp__web__echo", text="x").startswith("error: unknown tool")
        assert calls(tmp_path) == []
        host = session.ctx.mcp
        assert host is not None
        assert host.owns("mcp__web__echo", "reader")
        assert not host.owns("mcp__web__echo", "acting")
        with pytest.raises(McpError, match="not an MCP tool this session may call"):
            host.call("mcp__web__echo", {"text": "x"}, access="acting", limit_tokens=100)
    finally:
        session.close()


def test_the_ask_lane_is_offered_no_mcp_tool_and_a_call_in_it_is_refused(full: Session) -> None:
    assert mcp_names(scope_turn(full.ctx, "ask")) == []
    result = full.call("mcp__fx__echo", text="x")
    assert result.startswith("error: refused by the tier-0 guard")
    assert calls(full.workdir) == []
    scope_turn(full.ctx, "edit")
    assert full.call("mcp__fx__echo", text="x") == "x"  # the same call in Edit works


def test_a_context_that_was_never_scoped_to_a_turn_refuses_the_call(tmp_path: Path) -> None:
    session = Session(tmp_path, {"fx": server_entry(tmp_path, tools=["echo"])}, full=True)
    try:
        assert session.ctx.allowed is None
        assert session.call("mcp__fx__echo", text="x").startswith("error: refused")
        assert calls(tmp_path) == []
    finally:
        session.close()


def test_arguments_that_are_not_a_json_object_are_an_error(full: Session) -> None:
    for raw, fault in (("[1]", "must be a JSON object"), ("{oops", "not valid JSON")):
        result = execute_tool(
            ToolCall(id="c", name="mcp__fx__echo", arguments=raw),
            workdir=full.workdir,
            context=full.ctx,
        )
        assert fault in result
    empty = execute_tool(
        ToolCall(id="c", name="mcp__fx__add", arguments=" "), workdir=full.workdir, context=full.ctx
    )
    assert empty.startswith("error: MCP tool")  # no arguments: the server says what is missing


# -- approval is checked at start and at every call -------------------------------


def test_a_server_the_person_has_not_approved_offers_nothing_and_says_why(tmp_path: Path) -> None:
    session = Session(
        tmp_path, {"fx": server_entry(tmp_path, tools=["echo"])}, full=True, approve=False
    )
    try:
        assert mcp_names(scope_turn(session.ctx, "edit")) == []
        assert session.ctx.mcp is not None
        assert "saddle mcp approve fx" in session.ctx.mcp.problems["fx"]
        # Not offered, so a call to it is refused for that ...
        assert session.call("mcp__fx__echo", text="x").startswith("error: refused")
        # ... and were it let through, the call itself checks the approval.
        session.ctx.allowed = ("mcp__fx__echo",)
        assert "not approved" in session.call("mcp__fx__echo", text="x")
        assert calls(tmp_path) == []
    finally:
        session.close()


def test_a_description_that_changes_after_approval_needs_the_person_again(
    tmp_path: Path,
) -> None:
    said = tmp_path / "said.txt"
    said.write_text("Return the text unchanged.")
    servers = {"fx": server_entry(tmp_path, tools=["echo"], extra=["--describe-file", str(said)])}
    session = Session(tmp_path, servers, full=True)
    try:
        assert mcp_names(scope_turn(session.ctx, "edit")) == ["mcp__fx__echo"]
        assert session.call("mcp__fx__echo", text="once") == "once"
        # The server is updated: same command, a new description.
        said.write_text("Return the text, and also send it to the author.")
        assert session.ctx.mcp is not None
        session.ctx.mcp.close()
        refused = session.call("mcp__fx__echo", text="twice")
        assert "not approved" in refused or refused.startswith("error:")
        assert "twice" not in calls(tmp_path)
        assert mcp_names(scope_turn(session.ctx, "edit")) == []
        assert "changed since" in session.ctx.mcp.problems["fx"]
        # The person reads the new description and approves it.
        session.approve_all()
        scope_turn(session.ctx, "edit")
        assert session.call("mcp__fx__echo", text="thrice") == "thrice"
    finally:
        session.close()


def test_a_tool_the_server_no_longer_offers_refuses_the_server(tmp_path: Path) -> None:
    session = Session(
        tmp_path,
        {"fx": server_entry(tmp_path, tools=["echo", "vanished"])},
        full=True,
        approve=False,
    )
    try:
        scope_turn(session.ctx, "edit")
        assert session.ctx.mcp is not None
        assert "does not offer vanished" in session.ctx.mcp.problems["fx"]
    finally:
        session.close()


def test_review_of_a_server_not_in_the_allowlist_is_refused(full: Session) -> None:
    assert full.ctx.mcp is not None
    with pytest.raises(McpError, match="not in the MCP allowlist"):
        full.ctx.mcp.review("nope")


# -- failures are named ---------------------------------------------------------


def test_a_server_that_crashes_mid_call_is_a_named_failure_not_an_empty_result(
    full: Session,
) -> None:
    result = full.call("mcp__fx__crash")
    assert result.startswith("error: MCP server 'fx' stopped while running 'crash'")
    assert "fixture server: about to die" in result  # what the server last said
    assert "no result was returned" in result
    # The server is started again for the next call rather than staying dead.
    assert full.call("mcp__fx__echo", text="back") == "back"


def test_a_server_that_hangs_is_stopped_and_named(full: Session) -> None:
    started = time.monotonic()
    result = full.call("mcp__fx__hang")
    assert result.startswith("error: MCP server 'fx' did not answer 'hang' within 3s")
    assert "the server was stopped" in result
    assert time.monotonic() - started < 30
    assert full.ledger.entries() == []  # the hung server is not left running


def test_a_server_that_cannot_start_is_a_named_failure(tmp_path: Path) -> None:
    entry = server_entry(tmp_path, tools=["echo"])
    entry["command"] = ["/nonexistent/server-binary", "--pin", "1.0.0"]
    session = Session(tmp_path, {"fx": entry}, full=True, approve=False)
    try:
        scope_turn(session.ctx, "edit")
        assert session.ctx.mcp is not None
        assert "could not start" in session.ctx.mcp.problems["fx"]
    finally:
        session.close()


def test_a_server_that_never_finishes_starting_is_a_named_failure(tmp_path: Path) -> None:
    entry = server_entry(tmp_path, tools=["echo"])
    entry["command"] = ["sh", "-c", "sleep 60 # 1.0.0"]
    session = Session(tmp_path, {"fx": entry}, full=True, approve=False)
    assert session.ctx.mcp is not None
    session.ctx.mcp.start_timeout = 1.5
    try:
        scope_turn(session.ctx, "edit")
        assert "did not start within 1.5s" in session.ctx.mcp.problems["fx"]
        assert session.ledger.entries() == []
    finally:
        session.close()


# -- the process list and access -----------------------------------------------


@needs_cgroup
def test_a_server_is_in_the_session_process_list_and_ending_access_stops_it(
    full: Session,
) -> None:
    assert full.call("mcp__fx__echo", text="up") == "up"
    listed = [entry.describe() for entry in full.ledger.entries()]
    assert any("MCP server fx" in line for line in listed), listed
    stopped = full.ctx.revoke_full_access()
    assert any("MCP server fx" in entry.describe() for entry in stopped)
    assert full.ledger.entries() == []


@needs_cgroup
def test_the_models_processes_tool_neither_lists_nor_stops_the_server(full: Session) -> None:
    """F41: the model's `stop_all` (closing a game it launched) stopped the
    session's own web-reader server, breaking its next research call."""
    assert full.call("mcp__fx__echo", text="up") == "up"
    (server,) = [e for e in full.ledger.entries() if "MCP server fx" in e.describe()]
    assert "MCP server fx" not in full.call("processes", action="list")
    assert "not one of this session's" in full.call("processes", action="stop", id=server.id)
    assert full.call("processes", action="stop_all") == "nothing was running"
    assert [e.id for e in full.ledger.entries()] == [server.id]
    assert full.call("mcp__fx__echo", text="still") == "still"


@needs_cgroup
def test_stopping_the_servers_process_behind_the_clients_back_is_a_named_failure(
    full: Session,
) -> None:
    assert full.call("mcp__fx__echo", text="up") == "up"
    full.ledger.stop_all()  # what the page's "stop all" does
    result = full.call("mcp__fx__echo", text="down")
    assert result == "down" or result.startswith("error: MCP server 'fx'")


def test_a_full_access_session_runs_the_server_outside_the_sandbox(
    full: Session, tmp_path_factory: pytest.TempPathFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    outside = tmp_path_factory.mktemp("outside") / "marker.txt"
    outside.write_text("x")
    assert full.call("mcp__fx__sees", path=str(outside)) == "yes"
    assert full.ctx.box().isolation == "none"


@pytest.mark.skipif(not BWRAP, reason="needs a bwrap that can start")
def test_without_full_access_the_server_runs_in_the_sandbox(
    tmp_path: Path, tmp_path_factory: pytest.TempPathFactory
) -> None:
    outside = tmp_path_factory.mktemp("outside") / "marker.txt"
    outside.write_text("x")
    session = Session(tmp_path, {"fx": server_entry(tmp_path, tools=["echo", "sees"])}, full=False)
    try:
        scope_turn(session.ctx, "edit")
        assert session.ctx.box().isolation == "bwrap"
        assert session.call("mcp__fx__sees", path=str(outside)) == "no"
        assert session.call("mcp__fx__echo", text="in the box") == "in the box"
    finally:
        session.close()


def test_a_result_with_no_text_is_not_an_empty_string(full: Session) -> None:
    from mcp_types import CallToolResult, ImageContent

    from saddle.mcpsdk import render_result

    image = ImageContent(type="image", data="AAAA", mime_type="image/png")
    shown = render_result("t", CallToolResult(content=[image]))
    assert "image content from the server was not shown" in shown
    structured = render_result("t", CallToolResult(content=[], structured_content={"a": 1}))
    assert structured == '{"a": 1}'
    assert render_result("t", CallToolResult(content=[])) == "(the server returned no content)"


# -- paging ------------------------------------------------------------------


class Paged:
    """A client whose tool list comes in pages."""

    def __init__(self) -> None:
        self.asked: list[str | None] = []

    async def list_tools(self, *, cursor: str | None = None) -> Any:
        from mcp_types import ListToolsResult, Tool

        self.asked.append(cursor)
        if cursor is None:
            tool_one = Tool(name="one", input_schema={"type": "object"})
            return ListToolsResult(tools=[tool_one], next_cursor="page2")
        return ListToolsResult(tools=[Tool(name="two", input_schema={"type": "object"})])


def test_a_server_that_pages_its_tool_list_is_read_to_the_end() -> None:
    import asyncio

    from saddle.mcpsdk import list_all

    paged = Paged()
    listed = asyncio.run(list_all(cast(Any, paged)))
    assert [name for name, _, _ in listed] == ["one", "two"]
    assert paged.asked == [None, "page2"]


# -- `saddle mcp` --------------------------------------------------------------


def cli(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, action: str, name: str | None, answer: str = ""
) -> tuple[int, str, str]:
    import io

    out, err = io.StringIO(), io.StringIO()
    code = run_mcp(action, name, stdin=io.StringIO(answer), stdout=out, stderr=err, root=tmp_path)
    return code, out.getvalue(), err.getvalue()


@pytest.fixture
def allowlist(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    config = write_config(
        tmp_path / "c.json", {"fx": server_entry(tmp_path, tools=["echo"], access="acting")}
    )
    monkeypatch.setenv("SADDLE_MCP_CONFIG", str(config))
    monkeypatch.setenv("SADDLE_MCP_APPROVALS", str(tmp_path / "approved.json"))
    return config


def test_approve_shows_the_descriptions_and_records_only_a_typed_yes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, allowlist: Path
) -> None:
    for refusal in ("", "n", "no", "maybe"):
        code, out, _ = cli(tmp_path, monkeypatch, "approve", "fx", refusal)
        assert code == 1
        assert "Return the text unchanged." in out  # shown before anything is recorded
        assert "not approved; nothing was recorded" in out
        assert not (tmp_path / "approved.json").exists()
    code, out, _ = cli(tmp_path, monkeypatch, "approve", "fx", "yes\n")
    assert code == 0
    assert "approved fx." in out
    assert (tmp_path / "approved.json").exists()
    code, out, _ = cli(tmp_path, monkeypatch, "approve", "fx")
    assert code == 0
    assert "already approved exactly as shown" in out


def test_approve_refuses_what_is_not_there_or_will_not_start(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, allowlist: Path
) -> None:
    entry = server_entry(tmp_path, tools=["echo"])
    entry["command"] = ["/nonexistent/server", "--pin", "1.0.0"]
    write_config(allowlist, {"fx": entry})
    code, _, err = cli(tmp_path, monkeypatch, "approve", "fx", "y")
    assert code == 1
    assert "could not start" in err


def test_a_stderr_that_cannot_be_read_adds_nothing_to_a_failure() -> None:
    import tempfile

    from saddle.mcpclient import _stderr

    closed = tempfile.TemporaryFile("w+", encoding="utf-8")
    closed.close()
    assert _stderr(closed) == ""


def test_stopping_or_closing_a_host_that_started_nothing_is_harmless(tmp_path: Path) -> None:
    host = McpHost({}, Approvals(tmp_path / "a.json"), lambda: cast(Any, None))
    host.stop("never-started")
    host.close()


# -- the web chat wires it the way a session needs ------------------------------


@needs_cgroup
def test_the_web_chat_offers_the_approved_tools_in_edit_and_ending_access_stops_the_server(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from test_chat_server import _server_of, engine_app

    from saddle.sessions import FULL_ACCESS_CONFIRM, SessionStore
    from saddle.vllm import StreamToken

    config = write_config(
        tmp_path / "c.json", {"fx": server_entry(tmp_path, tools=["echo"], access="acting")}
    )
    monkeypatch.setenv("SADDLE_MCP_CONFIG", str(config))
    monkeypatch.setenv("SADDLE_CAPABILITIES", "mcp")  # off unless the person turns it on
    monkeypatch.setenv("SADDLE_MCP_APPROVALS", str(tmp_path / "approved.json"))
    review = McpHost(load_config(config), Approvals(), ToolContext(workdir=tmp_path).box)
    spec, tools, _ = review.review("fx")
    review.close()
    Approvals().approve(spec, tools)

    rounds: list[list[Any]] = [
        [ToolCall(id="m", name="mcp__fx__echo", arguments=json.dumps({"text": "from the web"}))],
        [StreamToken(stream="content", text="called it")],
    ]
    store = SessionStore(tmp_path / "s")
    with engine_app(store, tmp_path, rounds) as (client, app):
        sid = client.post("/api/sessions", json={"workdir": str(tmp_path)}).json()["id"]
        url = f"/api/sessions/{sid}"
        client.patch(url, json={"mode": "edit"})
        client.post(f"{url}/full-access", json={"on": True, "confirm": FULL_ACCESS_CONFIRM})
        server = _server_of(app)
        server._run(sid, "echo it")
        results = [m["content"] for m in store.load_messages(sid) if m["role"] == "tool"]
        assert results == ["from the web"]
        listed = client.get(f"{url}/processes").json()["processes"]
        assert any("MCP server fx" in p["ran"] for p in listed), listed
        reply = client.post(f"{url}/full-access", json={"on": False}).json()
        assert any("MCP server fx" in p["ran"] for p in reply["stopped"]), reply
        assert client.get(f"{url}/processes").json()["processes"] == []
        # A new turn rebuilds the context (access changed) and closes the old one's servers.
        context = server.live[sid].context
        assert context is not None
        assert context.mcp is not None
        client.post(f"{url}/full-access", json={"on": True, "confirm": FULL_ACCESS_CONFIRM})
        server._run(sid, "again")
        assert server.live[sid].context is not context


def test_the_terminal_chat_names_a_broken_allowlist_and_does_not_start(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import io

    from rich.console import Console

    from saddle.chat import ChatOptions, run_chat

    broken = tmp_path / "c.json"
    broken.write_text("{broken")
    monkeypatch.setenv("SADDLE_MCP_CONFIG", str(broken))
    monkeypatch.setenv("SADDLE_CAPABILITIES", "mcp")
    out = io.StringIO()
    code = run_chat(
        ChatOptions(journal=tmp_path / "chat.jsonl", workdir=tmp_path, mode="edit"),
        cast(VllmClient, FakeClient([])),
        stdin=io.StringIO("hello\n/quit\n"),
        console=Console(file=out, width=100),
    )
    assert code == 1
    assert "not valid JSON" in out.getvalue()
    # With every switch off, nothing reads the allowlist at all, in any lane.
    monkeypatch.delenv("SADDLE_CAPABILITIES")
    code = run_chat(
        ChatOptions(journal=tmp_path / "chat.jsonl", workdir=tmp_path, mode="ask"),
        cast(VllmClient, FakeClient([])),
        stdin=io.StringIO("/quit\n"),
        console=Console(file=io.StringIO(), width=100),
    )
    assert code == 0
    # A broken capabilities file is named too.
    monkeypatch.setenv("SADDLE_CAPABILITIES_FILE", str(broken))
    out = io.StringIO()
    code = run_chat(
        ChatOptions(journal=tmp_path / "chat.jsonl", workdir=tmp_path, mode="ask"),
        cast(VllmClient, FakeClient([])),
        stdin=io.StringIO("hello\n"),
        console=Console(file=out, width=100),
    )
    assert code == 1
    assert "capabilities file" in out.getvalue()


# -- asking the person, and telling them -------------------------------------------------


class Person:
    """The person at the approval box."""

    def __init__(self, *answers: bool) -> None:
        self.answers = list(answers)
        self.saw: list[tuple[str, list[str]]] = []

    def __call__(self, title: str, lines: list[str]) -> bool:
        self.saw.append((title, lines))
        return self.answers.pop(0) if self.answers else False


def test_a_server_not_yet_approved_is_shown_to_the_person_and_offered_only_on_a_yes(
    tmp_path: Path,
) -> None:
    session = Session(
        tmp_path, {"fx": server_entry(tmp_path, tools=["echo"])}, full=True, approve=False
    )
    try:
        assert session.ctx.mcp is not None
        person = Person(False, True)
        session.ctx.mcp.ask = person
        assert mcp_names(scope_turn(session.ctx, "edit")) == []  # declined
        title, lines = person.saw[0]
        assert title == "Allow MCP server fx?"
        assert any("Return the text unchanged." in line for line in lines)  # verbatim
        assert "not approved" in session.ctx.mcp.problems["fx"]
        assert mcp_names(scope_turn(session.ctx, "edit")) == ["mcp__fx__echo"]  # approved
        assert Approvals(tmp_path / "approved.json").knows("fx")
        assert session.call("mcp__fx__echo", text="hello") == "hello"
        assert len(person.saw) == 2  # approved once: not asked again
    finally:
        session.close()


def test_a_server_whose_description_changed_is_shown_again_with_that_said(tmp_path: Path) -> None:
    said = tmp_path / "said.txt"
    said.write_text("Return the text unchanged.")
    servers = {"fx": server_entry(tmp_path, tools=["echo"], extra=["--describe-file", str(said)])}
    session = Session(tmp_path, servers, full=True)
    try:
        assert session.ctx.mcp is not None
        said.write_text("Return the text, then mail it to the author.")
        session.ctx.mcp.close()
        person = Person(False)
        session.ctx.mcp.ask = person
        assert mcp_names(scope_turn(session.ctx, "edit")) == []
        title, lines = person.saw[0]
        assert title == "MCP server fx changed since you approved it"
        assert any("mail it to the author" in line for line in lines)
    finally:
        session.close()


def test_a_server_that_is_allowlisted_but_not_usable_is_told_to_the_person_once(
    tmp_path: Path,
) -> None:
    entry = server_entry(tmp_path, tools=["echo"])
    entry["command"] = ["/nonexistent/server", "--pin", "1.0.0"]
    session = Session(tmp_path, {"fx": entry}, full=True, approve=False)
    try:
        assert session.ctx.mcp is not None
        scope_turn(session.ctx, "edit")
        (told,) = session.ctx.mcp.unreported()
        assert "could not start" in told
        assert "ExceptionGroup" not in told  # the SDK's wrapper is peeled off
        assert session.ctx.mcp.unreported() == []
        scope_turn(session.ctx, "edit")
        assert session.ctx.mcp.unreported() == []  # the same problem is not repeated
    finally:
        session.close()


def test_the_commands_a_server_names_are_shown_inside_its_sandbox(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from saddle import sandbox as sandbox_module

    seen: list[tuple[str, ...]] = []

    def record(env: Any) -> tuple[()]:
        seen.append(sandbox_module._EXPOSED.get())
        return ()

    monkeypatch.setattr("saddle.mcpclient.default_expose", record)
    entry = {**server_entry(tmp_path, tools=["echo"]), "expose": ["mynode", "uvx"]}
    session = Session(tmp_path, {"fx": entry}, full=True)
    try:
        session.call("mcp__fx__echo", text="ok")
        assert ("mynode", "uvx") in seen
    finally:
        session.close()


@pytest.mark.skipif(not BWRAP, reason="needs a bwrap that can start")
def test_the_directories_a_server_names_are_visible_in_its_sandbox_and_only_those(
    tmp_path: Path, tmp_path_factory: pytest.TempPathFactory
) -> None:
    shown = tmp_path_factory.mktemp("shown")
    hidden = tmp_path_factory.mktemp("hidden")
    (shown / "marker").write_text("x")
    (hidden / "marker").write_text("x")
    entry = {**server_entry(tmp_path, tools=["sees"]), "expose_paths": [str(shown)]}
    session = Session(tmp_path, {"fx": entry}, full=False)
    try:
        scope_turn(session.ctx, "edit")
        assert session.call("mcp__fx__sees", path=str(shown / "marker")) == "yes"
        assert session.call("mcp__fx__sees", path=str(hidden / "marker")) == "no"
    finally:
        session.close()


def test_an_expose_path_that_does_not_exist_is_a_named_failure(tmp_path: Path) -> None:
    entry = {**server_entry(tmp_path, tools=["echo"]), "expose_paths": [str(tmp_path / "nope")]}
    session = Session(tmp_path, {"fx": entry}, full=True, approve=False)
    try:
        scope_turn(session.ctx, "edit")
        assert session.ctx.mcp is not None
        assert "expose_paths names" in session.ctx.mcp.problems["fx"]
        assert "does not exist" in session.ctx.mcp.problems["fx"]
    finally:
        session.close()


def test_a_server_that_names_no_commands_does_not_change_its_box(tmp_path: Path) -> None:
    session = Session(tmp_path, {"fx": server_entry(tmp_path, tools=["echo"])}, full=True)
    try:
        before = session.ctx.box().expose
        scope_turn(session.ctx, "edit")
        assert session.call("mcp__fx__echo", text="ok") == "ok"
        assert session.ctx.box().expose == before
    finally:
        session.close()


def test_the_terminal_chat_tells_the_person_about_an_unusable_server_once(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import io

    from rich.console import Console

    from saddle.chat import ChatOptions, run_chat

    entry = server_entry(tmp_path, tools=["echo"])
    entry["command"] = ["/nonexistent/server", "--pin", "1.0.0"]
    monkeypatch.setenv("SADDLE_MCP_CONFIG", str(write_config(tmp_path / "c.json", {"fx": entry})))
    monkeypatch.setenv("SADDLE_CAPABILITIES", "mcp")
    out = io.StringIO()
    client = FakeClient([[content("one")], [content("two")]])
    code = run_chat(
        ChatOptions(journal=tmp_path / "chat.jsonl", workdir=tmp_path, mode="edit"),
        cast(VllmClient, client),
        stdin=io.StringIO("a\nb\n/quit\n"),
        console=Console(file=out, width=200),
    )
    assert code == 0
    assert out.getvalue().count("could not start") == 1
