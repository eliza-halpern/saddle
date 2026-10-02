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
import os
import shutil
import sys
import time
from collections.abc import Iterator
from pathlib import Path
from typing import Any, cast

import pytest
from test_chat_engine import FakeClient, content, tool
from test_memcap import needs_cgroup

from saddle.engine import TurnOptions, run_turn
from saddle.mcp_cmd import run_mcp
from saddle.mcpclient import (
    Approvals,
    McpConfigError,
    McpError,
    McpHost,
    ServerSpec,
    ToolInfo,
    approvals_path,
    clip_result,
    config_path,
    fingerprint,
    load_config,
    render_review,
    tool_schema,
)
from saddle.procs import ProcessLedger
from saddle.sandbox import isolation_problem
from saddle.tools import ToolContext, attach_mcp, execute_tool, scope_turn
from saddle.vllm import ToolCall, VllmClient

FIXTURE = Path(__file__).with_name("mcp_fixture_server.py")
BWRAP = isolation_problem() is None


def server_entry(
    workdir: Path,
    *,
    tools: list[str],
    access: str = "acting",
    extra: list[str] | None = None,
) -> dict[str, Any]:
    """An allowlist entry for the fixture server, which lives in `workdir` so a
    sandboxed session can run it."""
    script = workdir / "srv.py"
    if not script.exists():
        shutil.copy(FIXTURE, script)
    return {
        "command": [
            sys.executable,
            str(script),
            "--log",
            str(workdir / "calls.log"),
            "--pin",
            "1.0.0",
            *(extra or []),
        ],
        "version": "1.0.0",
        "access": access,
        "tools": tools,
    }


def write_config(path: Path, servers: dict[str, Any]) -> Path:
    path.write_text(json.dumps({"servers": servers}), encoding="utf-8")
    return path


def mcp_names(tools: list[dict[str, Any]]) -> list[str]:
    return [t["function"]["name"] for t in tools if t["function"]["name"].startswith("mcp__")]


def calls(workdir: Path) -> list[str]:
    log = workdir / "calls.log"
    return log.read_text(encoding="utf-8").splitlines() if log.exists() else []


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


# -- the allowlist ------------------------------------------------------------


def one_server(**changes: Any) -> dict[str, Any]:
    entry: dict[str, Any] = {
        "command": ["uvx", "some-server==1.2.3"],
        "version": "1.2.3",
        "access": "acting",
        "tools": ["read"],
    }
    entry.update(changes)
    return {"srv": entry}


def test_a_well_formed_allowlist_is_read(tmp_path: Path) -> None:
    config = load_config(write_config(tmp_path / "c.json", one_server()))
    assert config["srv"] == ServerSpec(
        "srv", ("uvx", "some-server==1.2.3"), "1.2.3", "acting", ("read",)
    )


def test_no_allowlist_file_means_no_servers(tmp_path: Path) -> None:
    assert load_config(tmp_path / "absent.json") == {}


@pytest.mark.parametrize(
    ("changes", "fault"),
    [
        ({"command": ["uvx", "some-server"]}, "pinned version"),
        ({"command": []}, "command must be"),
        ({"version": ""}, "version is required"),
        ({"access": "both"}, "access must be"),
        ({"access": None}, "access must be"),
        ({"tools": ["*"]}, "exact tool names"),
        ({"tools": []}, "exact tool names"),
        ({"tools": ["a", "a"]}, "listed twice"),
        ({"tools": ["x" * 60]}, "over 64"),
        ({"surprise": 1}, "unknown key"),
    ],
)
def test_an_allowlist_entry_that_breaks_a_rule_is_refused_naming_the_fault(
    tmp_path: Path, changes: dict[str, Any], fault: str
) -> None:
    path = write_config(tmp_path / "c.json", one_server(**changes))
    with pytest.raises(McpConfigError, match=fault):
        load_config(path)


@pytest.mark.parametrize(
    "text",
    [
        "{not json",
        "[]",
        '{"servers": []}',
        '{"servers": {"Bad Name": {}}}',
        '{"servers": {"ok": 3}}',
    ],
)
def test_a_malformed_allowlist_is_an_error_never_an_empty_list(tmp_path: Path, text: str) -> None:
    path = tmp_path / "c.json"
    path.write_text(text)
    with pytest.raises(McpConfigError):
        load_config(path)


def test_an_unreadable_allowlist_is_an_error(tmp_path: Path) -> None:
    with pytest.raises(McpConfigError, match="cannot read"):
        load_config(tmp_path)  # a directory


# -- approval -----------------------------------------------------------------

SPEC = ServerSpec("srv", ("uvx", "s==1"), "1", "acting", ("read",))
TOOLS = [ToolInfo("read", "Reads a thing.", {"type": "object", "properties": {"p": {}}})]


def test_an_approval_holds_for_exactly_what_was_shown(tmp_path: Path) -> None:
    approvals = Approvals(tmp_path / "a.json")
    assert not approvals.is_approved(SPEC, TOOLS)
    approvals.approve(SPEC, TOOLS)
    assert approvals.is_approved(SPEC, TOOLS)
    assert Approvals(tmp_path / "a.json").is_approved(SPEC, TOOLS)


@pytest.mark.parametrize(
    "changed",
    [
        [ToolInfo("read", "Reads a thing, then mails it.", TOOLS[0].schema)],
        [ToolInfo("read", "Reads a thing.", {"type": "object", "properties": {"q": {}}})],
        [ToolInfo("other", "Reads a thing.", TOOLS[0].schema)],
    ],
    ids=["description", "schema", "name"],
)
def test_a_change_after_approval_is_not_approved(tmp_path: Path, changed: list[ToolInfo]) -> None:
    approvals = Approvals(tmp_path / "a.json")
    approvals.approve(SPEC, TOOLS)
    assert not approvals.is_approved(SPEC, changed)
    assert fingerprint(SPEC, changed) != fingerprint(SPEC, TOOLS)


def test_a_new_command_or_access_is_not_approved(tmp_path: Path) -> None:
    approvals = Approvals(tmp_path / "a.json")
    approvals.approve(SPEC, TOOLS)
    assert not approvals.is_approved(
        ServerSpec("srv", ("uvx", "s==2"), "1", "acting", ("read",)), TOOLS
    )
    assert not approvals.is_approved(
        ServerSpec("srv", SPEC.command, "1", "reader", ("read",)), TOOLS
    )


@pytest.mark.parametrize("text", ["{broken", "[1]", '{"srv": 7}'])
def test_an_unreadable_approval_file_approves_nothing(tmp_path: Path, text: str) -> None:
    path = tmp_path / "a.json"
    path.write_text(text)
    assert not Approvals(path).is_approved(SPEC, TOOLS)


def test_the_review_shows_each_description_verbatim_and_says_whose_they_are() -> None:
    shown = render_review(SPEC, [*TOOLS, ToolInfo("bare", "", {"type": "object"})])
    assert "Reads a thing." in shown
    assert "(no description)" in shown
    assert "come from the server, not from saddle" in shown
    assert "uvx s==1" in shown


def test_a_tool_the_model_sees_is_named_for_its_server_and_a_non_object_schema_is_made_one() -> (
    None
):
    schema = tool_schema(SPEC, ToolInfo("read", "Reads.", {"type": "string"}))
    assert schema["function"]["name"] == "mcp__srv__read"
    assert schema["function"]["parameters"] == {"type": "object"}
    assert schema["function"]["description"].startswith("[MCP server srv]")


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


# -- results --------------------------------------------------------------------


def test_a_long_result_is_cut_to_whole_lines_and_says_what_was_left_out() -> None:
    text = "".join(f"line {i}\n" for i in range(100))
    by_tokens = clip_result(text, 30, lambda t: len(t.split()))
    assert by_tokens.startswith("line 0\n")
    assert "more lines of the server's result were left out" in by_tokens
    assert len(by_tokens.splitlines()) < 100
    assert clip_result(text, 1_000_000, lambda t: len(t.split())) == text
    by_lines = clip_result(text, 40, None)  # no tokenizer: limit // 4 lines
    assert by_lines.count("line ") == 10
    assert clip_result("short\n", 40, None) == "short\n"
    assert clip_result(text, 30, lambda t: None).count("line ") == 7  # a count that fails


def test_a_result_with_no_text_is_not_an_empty_string(full: Session) -> None:
    from mcp_types import CallToolResult, ImageContent

    from saddle.mcpclient import render_result

    image = ImageContent(type="image", data="AAAA", mime_type="image/png")
    shown = render_result("t", CallToolResult(content=[image]))
    assert "image content from the server was not shown" in shown
    structured = render_result("t", CallToolResult(content=[], structured_content={"a": 1}))
    assert structured == '{"a": 1}'
    assert render_result("t", CallToolResult(content=[])) == "(the server returned no content)"


# -- config paths, paging, attaching to a session ------------------------------


def test_the_allowlist_and_approvals_live_in_the_saddle_config_directory_unless_told_otherwise(
    tmp_path: Path,
) -> None:
    assert config_path({}) == Path("~/.config/saddle/mcp.json").expanduser()
    assert approvals_path({}) == Path("~/.config/saddle/mcp-approved.json").expanduser()
    assert config_path({"SADDLE_MCP_CONFIG": str(tmp_path / "x.json")}) == tmp_path / "x.json"
    assert approvals_path({"SADDLE_MCP_APPROVALS": str(tmp_path / "y")}) == tmp_path / "y"
    assert config_path() != Path("~/.config/saddle/mcp.json").expanduser()  # the suite's own


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

    from saddle.mcpclient import _list_all

    paged = Paged()
    listed = asyncio.run(_list_all(cast(Any, paged)))
    assert [t.name for t in listed] == ["one", "two"]
    assert paged.asked == [None, "page2"]


def test_a_session_gets_mcp_only_when_the_person_has_an_allowlist(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ctx = ToolContext(workdir=tmp_path)
    attach_mcp(ctx)
    assert ctx.mcp is None  # the suite's allowlist does not exist
    config = write_config(tmp_path / "c.json", one_server())
    monkeypatch.setenv("SADDLE_MCP_CONFIG", str(config))
    attach_mcp(ctx)
    assert ctx.mcp is not None
    assert list(ctx.mcp.config) == ["srv"]
    config.write_text("{broken")
    with pytest.raises(McpConfigError):
        attach_mcp(ctx)


def test_a_task_context_has_no_mcp_and_offers_none(tmp_path: Path) -> None:
    # Task runs build a bare ToolContext and never call attach_mcp or scope_turn.
    assert ToolContext(workdir=tmp_path).mcp is None
    assert execute_tool(
        ToolCall(id="c", name="mcp__fx__echo", arguments="{}"), workdir=tmp_path
    ).startswith("error: unknown tool")


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


def test_list_says_what_is_allowlisted_and_nothing_when_nothing_is(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    code, out, _ = cli(tmp_path, monkeypatch, "list", None)
    assert code == 0
    assert "no MCP servers are allowlisted" in out
    write_config(
        Path(os.environ["SADDLE_MCP_CONFIG"]), {"fx": server_entry(tmp_path, tools=["echo"])}
    )
    code, out, _ = cli(tmp_path, monkeypatch, "list", None)
    assert code == 0
    assert "fx: acting, tools echo" in out


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
    for name in (None, "nope"):
        code, _, err = cli(tmp_path, monkeypatch, "approve", name, "y")
        assert code == 1
        assert "is not in the MCP allowlist" in err
    entry = server_entry(tmp_path, tools=["echo"])
    entry["command"] = ["/nonexistent/server", "--pin", "1.0.0"]
    write_config(allowlist, {"fx": entry})
    code, _, err = cli(tmp_path, monkeypatch, "approve", "fx", "y")
    assert code == 1
    assert "could not start" in err
    allowlist.write_text("{broken")
    code, _, err = cli(tmp_path, monkeypatch, "list", None)
    assert code == 1
    assert "not valid JSON" in err


def test_the_cli_routes_mcp_and_needs_no_model_key(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    from saddle.cli import main

    assert main(["mcp", "list"]) == 0
    assert "no MCP servers are allowlisted" in capsys.readouterr().out


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
    out = io.StringIO()
    code = run_chat(
        ChatOptions(journal=tmp_path / "chat.jsonl", workdir=tmp_path, mode="edit"),
        cast(VllmClient, FakeClient([])),
        stdin=io.StringIO("hello\n/quit\n"),
        console=Console(file=out, width=100),
    )
    assert code == 1
    assert "not valid JSON" in out.getvalue()
    # Ask never reads the allowlist at all.
    code = run_chat(
        ChatOptions(journal=tmp_path / "chat.jsonl", workdir=tmp_path, mode="ask"),
        cast(VllmClient, FakeClient([])),
        stdin=io.StringIO("/quit\n"),
        console=Console(file=io.StringIO(), width=100),
    )
    assert code == 0
