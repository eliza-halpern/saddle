"""The MCP allowlist, approvals and the pieces that need no SDK (#139).

Known-good: a well-formed allowlist is read, an approval holds for exactly what
was shown, a session gets MCP only when the person switched it on and has an
allowlist. Known-bad: a malformed allowlist, a change after approval and a
session whose switch is off are all refused, by name. None of this imports the
MCP SDK, so it runs on a saddle installed without the `mcp` extra.
"""

from __future__ import annotations

import io
from pathlib import Path
from typing import Any

import pytest
from mcp_support import one_server, server_entry, write_config

from saddle.capabilities import Switches
from saddle.mcp_cmd import run_mcp
from saddle.mcpclient import (
    Approvals,
    McpConfigError,
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
from saddle.tools import ToolContext, attach_mcp, execute_tool
from saddle.vllm import ToolCall


def test_a_well_formed_allowlist_is_read(tmp_path: Path) -> None:
    config = load_config(write_config(tmp_path / "c.json", one_server()))
    assert config["srv"] == ServerSpec(
        "srv", ("uvx", "some-server==1.2.3"), "1.2.3", "acting", ("read",)
    )


def test_the_commands_a_server_needs_shown_in_its_sandbox_are_named_in_its_entry(
    tmp_path: Path,
) -> None:
    config = load_config(write_config(tmp_path / "c.json", one_server(expose=["node", "npx"])))
    assert config["srv"].expose == ("node", "npx")
    assert load_config(write_config(tmp_path / "d.json", one_server()))["srv"].expose == ()


@pytest.mark.parametrize("expose", ["node", [1], ["node;rm"], ["a b"], [""], {"a": 1}])
def test_an_expose_that_is_not_a_list_of_command_names_is_refused(
    tmp_path: Path, expose: Any
) -> None:
    with pytest.raises(McpConfigError, match="expose must be a list of command names"):
        load_config(write_config(tmp_path / "c.json", one_server(expose=expose)))


def test_the_directories_a_server_needs_shown_in_its_sandbox_are_named_by_path(
    tmp_path: Path,
) -> None:
    entry = one_server(expose_paths=["/opt/node", "~/.local/share/pw"])
    config = load_config(write_config(tmp_path / "c.json", entry))
    assert config["srv"].expose_paths == ("/opt/node", "~/.local/share/pw")
    assert load_config(write_config(tmp_path / "d.json", one_server()))["srv"].expose_paths == ()


@pytest.mark.parametrize("paths", ["/opt/node", [1], ["relative/dir"], [""], {"a": 1}])
def test_an_expose_paths_that_is_not_a_list_of_absolute_paths_is_refused(
    tmp_path: Path, paths: Any
) -> None:
    with pytest.raises(McpConfigError, match="expose_paths must be a list of absolute paths"):
        load_config(write_config(tmp_path / "c.json", one_server(expose_paths=paths)))


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


def test_changing_what_a_server_may_see_needs_the_person_again(tmp_path: Path) -> None:
    approvals = Approvals(tmp_path / "a.json")
    approvals.approve(SPEC, TOOLS)
    wider = ServerSpec("srv", SPEC.command, "1", "acting", ("read",), ("node",))
    assert not approvals.is_approved(wider, TOOLS)
    deeper = ServerSpec("srv", SPEC.command, "1", "acting", ("read",), (), ("/opt/node",))
    assert not approvals.is_approved(deeper, TOOLS)
    assert approvals.knows("srv")
    assert not approvals.knows("other")


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


def test_the_allowlist_and_approvals_live_in_the_saddle_config_directory_unless_told_otherwise(
    tmp_path: Path,
) -> None:
    assert config_path({}) == Path("~/.config/saddle/mcp.json").expanduser()
    assert approvals_path({}) == Path("~/.config/saddle/mcp-approved.json").expanduser()
    assert config_path({"SADDLE_MCP_CONFIG": str(tmp_path / "x.json")}) == tmp_path / "x.json"
    assert approvals_path({"SADDLE_MCP_APPROVALS": str(tmp_path / "y")}) == tmp_path / "y"
    assert config_path() != Path("~/.config/saddle/mcp.json").expanduser()  # the suite's own


# -- a session gets only what the person switched on ----------------------------------


def attach(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, switches: Switches, servers: Any
) -> ToolContext:
    monkeypatch.setenv("SADDLE_MCP_CONFIG", str(write_config(tmp_path / "c.json", servers)))
    ctx = ToolContext(workdir=tmp_path)
    attach_mcp(ctx, tmp_path / "downloads", switches)
    return ctx


BOTH = {
    **one_server(),
    "web": {**one_server()["srv"], "access": "reader", "command": ["uvx", "web==1.2.3"]},
}


def test_nothing_is_attached_by_default_even_with_an_allowlist(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ctx = attach(tmp_path, monkeypatch, Switches(), BOTH)
    assert ctx.mcp is None
    assert ctx.research is None
    # With every switch off a broken allowlist is not even read.
    (tmp_path / "c.json").write_text("{broken")
    attach_mcp(ToolContext(workdir=tmp_path), tmp_path / "downloads", Switches())


def test_the_mcp_switch_attaches_the_host_and_only_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ctx = attach(tmp_path, monkeypatch, Switches(mcp=True), BOTH)
    assert ctx.mcp is not None
    assert sorted(ctx.mcp.config) == ["srv", "web"]
    assert ctx.research is None


def test_the_research_switch_attaches_the_reader_and_carries_search_and_browser(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ctx = attach(tmp_path, monkeypatch, Switches(research=True), BOTH)
    assert ctx.mcp is None
    assert ctx.research is not None
    assert (ctx.research.search_enabled, ctx.research.browser_enabled) == (False, False)
    ctx = attach(tmp_path, monkeypatch, Switches(research=True, search=True, browser=True), BOTH)
    assert ctx.research is not None
    assert (ctx.research.search_enabled, ctx.research.browser_enabled) == (True, True)
    assert ctx.research.downloads_dir == tmp_path / "downloads"


def test_research_without_a_reader_server_or_an_allowlist_attaches_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    assert attach(tmp_path, monkeypatch, Switches(research=True), one_server()).research is None
    assert attach(tmp_path, monkeypatch, Switches(mcp=True, research=True), {}).mcp is None


def test_a_broken_allowlist_is_named_when_a_switch_needs_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = write_config(tmp_path / "c.json", one_server())
    config.write_text("{broken")
    monkeypatch.setenv("SADDLE_MCP_CONFIG", str(config))
    with pytest.raises(McpConfigError, match="not valid JSON"):
        attach_mcp(ToolContext(workdir=tmp_path), tmp_path / "d", Switches(mcp=True))


def test_flipping_a_switch_applies_from_the_next_turn(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("SADDLE_MCP_CONFIG", str(write_config(tmp_path / "c.json", BOTH)))
    ctx = ToolContext(workdir=tmp_path)
    down = tmp_path / "downloads"
    attach_mcp(ctx, down, Switches(mcp=True, research=True))
    mcp, research = ctx.mcp, ctx.research
    assert mcp is not None
    assert research is not None
    attach_mcp(ctx, down, Switches(mcp=True, research=True, search=True, browser=True))
    assert ctx.mcp is mcp  # the same host, its servers kept
    assert ctx.research is research
    assert (research.search_enabled, research.browser_enabled) == (True, True)
    attach_mcp(ctx, down, Switches(mcp=True, research=True))
    assert (research.search_enabled, research.browser_enabled) == (False, False)
    attach_mcp(ctx, down, Switches(research=True))  # mcp turned off
    assert ctx.mcp is None
    assert ctx.research is research
    attach_mcp(ctx, down, Switches())  # everything off
    assert (ctx.mcp, ctx.research) == (None, None)
    attach_mcp(ctx, down, Switches(mcp=True))  # and on again, a fresh host
    assert ctx.mcp is not None
    assert ctx.mcp is not mcp
    monkeypatch.setenv("SADDLE_MCP_CONFIG", str(tmp_path / "gone.json"))
    attach_mcp(ctx, down, Switches(mcp=True))  # the allowlist emptied: nothing to offer
    assert ctx.mcp is None


def test_a_task_context_has_no_mcp_or_research_and_offers_none(tmp_path: Path) -> None:
    # Task runs build a bare ToolContext and never call attach_mcp or scope_turn.
    ctx = ToolContext(workdir=tmp_path)
    assert (ctx.mcp, ctx.research) == (None, None)
    for name in ("mcp__fx__echo", "research"):
        result = execute_tool(
            ToolCall(id="c", name=name, arguments="{}"), workdir=tmp_path, context=ctx
        )
        assert result.startswith("error: unknown tool")


# -- `saddle mcp` without the SDK ---------------------------------------------------


def cli(tmp_path: Path, action: str, name: str | None, answer: str = "") -> tuple[int, str, str]:
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
    code, out, _ = cli(tmp_path, "list", None)
    assert code == 0
    assert "no MCP servers are allowlisted" in out
    write_config(tmp_path / "again.json", {"fx": server_entry(tmp_path, tools=["echo"])})
    monkeypatch.setenv("SADDLE_MCP_CONFIG", str(tmp_path / "again.json"))
    code, out, _ = cli(tmp_path, "list", None)
    assert code == 0
    assert "fx: acting, tools echo" in out


def test_a_broken_allowlist_stops_the_cli_by_name(tmp_path: Path, allowlist: Path) -> None:
    allowlist.write_text("{broken")
    code, _, err = cli(tmp_path, "list", None)
    assert code == 1
    assert "not valid JSON" in err


def test_approve_names_a_server_that_is_not_there(tmp_path: Path, allowlist: Path) -> None:
    for name in (None, "nope"):
        code, _, err = cli(tmp_path, "approve", name, "y")
        assert code == 1
        assert "is not in the MCP allowlist" in err


def test_without_the_sdk_approve_says_so_and_records_nothing(
    tmp_path: Path, allowlist: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import sys

    monkeypatch.setitem(sys.modules, "mcp", None)  # `import mcp` now fails
    monkeypatch.delitem(sys.modules, "saddle.mcpsdk", raising=False)
    code, _, err = cli(tmp_path, "approve", "fx", "y")
    assert code == 1
    assert "the MCP SDK is not installed" in err
    assert "saddle-harness[mcp]" in err
    assert not (tmp_path / "approved.json").exists()


def test_the_cli_routes_mcp_and_needs_no_model_key(capsys: pytest.CaptureFixture[str]) -> None:
    from saddle.cli import main

    assert main(["mcp", "list"]) == 0
    assert "no MCP servers are allowlisted" in capsys.readouterr().out


# -- the person is asked and told ---------------------------------------------------


def test_what_attach_hands_the_host_and_the_reader_asks_through_the_sessions_approver(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ctx = attach(tmp_path, monkeypatch, Switches(mcp=True, research=True), BOTH)
    assert ctx.mcp is not None
    assert ctx.research is not None
    assert ctx.mcp.ask is not None
    assert ctx.research.approve is not None
    assert ctx.mcp.ask("Allow?", ["x"]) is False  # nobody to ask is a no
    assert ctx.research.approve("Run?", ["y"]) is False
    seen: list[tuple[str, list[str]]] = []

    def person(title: str, lines: list[str]) -> bool:
        seen.append((title, lines))
        return True

    ctx.approve = person  # a web session sets this when its context is built
    assert ctx.mcp.ask("Allow?", ["x"]) is True
    assert ctx.research.approve("Run?", ["y"]) is True
    assert seen == [("Allow?", ["x"]), ("Run?", ["y"])]


def test_the_web_chat_tells_the_person_about_a_server_it_cannot_use_once(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import queue

    from starlette.testclient import TestClient
    from test_chat_server import FakeClient as ServerClient
    from test_chat_server import _server_of

    from saddle.events import ErrorEvent
    from saddle.sessions import SessionStore
    from saddle.web.app import build_app

    entry = {**one_server()["srv"], "command": ["/nonexistent/server", "srv==1.2.3"]}
    monkeypatch.setenv("SADDLE_MCP_CONFIG", str(write_config(tmp_path / "c.json", {"srv": entry})))
    monkeypatch.setenv("SADDLE_CAPABILITIES", "mcp")
    app = build_app(SessionStore(tmp_path / "s"), ServerClient, default_workdir=tmp_path)
    with TestClient(app) as client:
        sid = client.post("/api/sessions", json={"workdir": str(tmp_path)}).json()["id"]
        client.patch(f"/api/sessions/{sid}", json={"mode": "edit"})
        server = _server_of(app)
        events = server._live(sid).subscribe()
        server._run(sid, "hello")
        server._run(sid, "again")
        told: list[str] = []
        while True:
            try:
                event = events.get_nowait()
            except queue.Empty:
                break
            if isinstance(event, ErrorEvent) and event.message.startswith("MCP server"):
                told.append(event.message)  # (this client cannot stream, so turns end in errors)
        assert len(told) == 1  # said once, not every turn
        assert "MCP server 'srv'" in told[0]
        server.end_processes(sid, revoke=True)
