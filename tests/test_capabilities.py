"""Opt-in capabilities: nothing is on until the person turns it on, and a tool is
offered only when its switch is on and the thing it needs works.

Known-good: a switch the person turned on, with its server (and the SDK, and
isolation, and for search a running backend) in place, is `on`; `saddle
capabilities enable research` writes the switch.

Known-bad: with no file, every capability is off; an on switch whose
requirement is missing is `unavailable` with the reason, never silently on; a
broken file or setting is an error naming itself; and a saddle without the MCP
extra still runs and reports MCP unavailable.
"""

from __future__ import annotations

import io
import json
import sys
from pathlib import Path
from typing import Any, Final

import httpx
import pytest
from mcp_support import one_server, write_config

from saddle import research as research_module
from saddle.capabilities import (
    NAMES,
    CapabilityError,
    Status,
    Switches,
    file_path,
    load,
    run,
    save,
    status,
)

READER = {
    "web": {
        "command": ["python3", "web==1.0"],
        "version": "1.0",
        "access": "reader",
        "tools": ["fetch"],
    }
}
BROWSER = {
    "pw": {
        "command": ["python3", "pw==1.0"],
        "version": "1.0",
        "access": "reader",
        "tools": ["browser_navigate", "browser_snapshot"],
    }
}


def states(rows: list[Status]) -> dict[str, str]:
    return {row.name: row.state for row in rows}


def reason(rows: list[Status], name: str) -> str:
    return next(row.reason for row in rows if row.name == name)


@pytest.fixture
def servers(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Any:
    def put(entries: dict[str, Any]) -> None:
        monkeypatch.setenv("SADDLE_MCP_CONFIG", str(write_config(tmp_path / "mcp.json", entries)))

    return put


def search_client(answer: Any) -> httpx.Client:
    def handle(request: httpx.Request) -> httpx.Response:
        if isinstance(answer, Exception):
            raise answer
        return httpx.Response(answer, json={"results": []})

    return httpx.Client(transport=httpx.MockTransport(handle))


# -- the switches -------------------------------------------------------------


NO_FILE: Final = {"SADDLE_CAPABILITIES_FILE": "/nonexistent/saddle-capabilities.json"}
"""An environment naming a file that does not exist: `load({})` alone falls back to the
person's real ~/.config/saddle/capabilities.json, so a machine with a switch on failed."""


def test_every_capability_is_off_by_default() -> None:
    assert load(NO_FILE) == Switches()
    assert [Switches().get(name) for name in NAMES] == [False] * 5
    assert states(status(Switches())) == dict.fromkeys(NAMES, "off")
    assert states(status()) == dict.fromkeys(NAMES, "off")  # the suite's file does not exist


def test_the_file_turns_a_capability_on_and_only_that_one(tmp_path: Path) -> None:
    path = tmp_path / "c.json"
    path.write_text('{"research": true, "search": false}')
    assert load({"SADDLE_CAPABILITIES_FILE": str(path)}) == Switches(research=True)


def test_the_environment_overrides_the_file_for_one_process(tmp_path: Path) -> None:
    path = tmp_path / "c.json"
    path.write_text('{"research": true, "mcp": true}')
    env = {
        "SADDLE_CAPABILITIES_FILE": str(path),
        "SADDLE_CAPABILITIES": "search, research=off, browser=1",
    }
    assert load(env) == Switches(mcp=True, search=True, browser=True)
    assert load({**NO_FILE, "SADDLE_CAPABILITIES": "mcp=on"}) == Switches(mcp=True)


@pytest.mark.parametrize("text", ["mcpp", "research=maybe", "=on", "all", "research=2"])
def test_a_setting_that_is_not_a_capability_is_an_error_naming_it(text: str) -> None:
    with pytest.raises(CapabilityError, match="SADDLE_CAPABILITIES"):
        load({"SADDLE_CAPABILITIES": text})


def test_an_empty_value_means_on_like_the_bare_name() -> None:
    assert load({**NO_FILE, "SADDLE_CAPABILITIES": "research="}) == Switches(research=True)


@pytest.mark.parametrize(
    ("text", "fault"),
    [
        ("{broken", "not valid JSON"),
        ("[true]", "must be an object"),
        ('{"researc": true}', "must be one of"),
        ('{"research": "yes"}', "must be one of"),
        ('{"research": 1}', "must be one of"),
    ],
)
def test_a_broken_capabilities_file_is_an_error_naming_the_file(
    tmp_path: Path, text: str, fault: str
) -> None:
    path = tmp_path / "c.json"
    path.write_text(text)
    with pytest.raises(CapabilityError, match=fault):
        load({"SADDLE_CAPABILITIES_FILE": str(path)})


def test_an_unreadable_capabilities_file_is_an_error(tmp_path: Path) -> None:
    with pytest.raises(CapabilityError, match="cannot read"):
        load({"SADDLE_CAPABILITIES_FILE": str(tmp_path)})  # a directory


def test_the_file_lives_beside_the_allowlist_unless_told_otherwise(tmp_path: Path) -> None:
    assert file_path({}) == Path("~/.config/saddle/capabilities.json").expanduser()
    assert file_path({"SADDLE_CAPABILITIES_FILE": str(tmp_path / "x")}) == tmp_path / "x"


def test_enable_and_disable_write_the_switch_and_keep_the_others() -> None:
    path = save("research", True)
    assert path == file_path()
    save("search", True)
    save("research", False)
    assert json.loads(path.read_text()) == {"research": False, "search": True}
    with pytest.raises(CapabilityError, match="is not a capability"):
        save("everything", True)


# -- status: on only when it works -----------------------------------------------


def test_mcp_on_without_the_sdk_is_unavailable_and_says_which_extra(
    servers: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    servers(one_server())
    monkeypatch.setitem(sys.modules, "mcp", None)
    monkeypatch.delitem(sys.modules, "saddle.mcpsdk", raising=False)
    rows = status(Switches(mcp=True, research=True, browser=True))
    assert states(rows) == {
        "mcp": "unavailable",
        "research": "unavailable",
        "search": "off",
        "browser": "unavailable",
        "answers": "off",
    }
    for name in ("mcp", "research", "browser"):
        assert "saddle-harness[mcp]" in reason(rows, name)


def test_mcp_needs_an_acting_server_in_the_allowlist(servers: Any) -> None:
    rows = status(Switches(mcp=True))
    assert reason(rows, "mcp") == "no `access: acting` server is in the MCP allowlist"
    servers(READER)  # a reader server is not an acting one
    assert states(status(Switches(mcp=True)))["mcp"] == "unavailable"
    servers(one_server())
    assert status(Switches(mcp=True))[0] == Status("mcp", "on")


def test_a_broken_allowlist_makes_what_needs_it_unavailable_and_names_the_fault(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    broken = tmp_path / "mcp.json"
    broken.write_text("{broken")
    monkeypatch.setenv("SADDLE_MCP_CONFIG", str(broken))
    rows = status(Switches(mcp=True, research=True))
    assert states(rows)["mcp"] == "unavailable"
    assert "the MCP allowlist is broken" in reason(rows, "mcp")
    assert "not valid JSON" in reason(rows, "research")


def test_research_needs_a_reader_server_whose_command_is_installed_and_isolation(
    servers: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    assert "no `access: reader` server" in reason(status(Switches(research=True)), "research")
    servers(one_server())  # acting only
    assert states(status(Switches(research=True)))["research"] == "unavailable"
    missing = {"web": {**READER["web"], "command": ["/no/such/program", "web==1.0"]}}
    servers(missing)
    assert (
        reason(status(Switches(research=True)), "research") == "/no/such/program is not installed"
    )
    servers(READER)
    monkeypatch.setattr(research_module, "isolation_problem", lambda: "bwrap cannot start")
    assert "cannot give it: bwrap cannot start" in reason(
        status(Switches(research=True)), "research"
    )
    monkeypatch.setattr(research_module, "isolation_problem", lambda: None)
    assert status(Switches(research=True))[1] == Status("research", "on")


def test_a_reader_with_only_browser_tools_is_unavailable_until_the_browser_is_on(
    servers: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(research_module, "isolation_problem", lambda: None)
    servers(BROWSER)
    off = status(Switches(research=True))
    assert "browser tools need the browser capability" in reason(off, "research")
    assert states(status(Switches(research=True, browser=True)))["research"] == "on"
    assert states(status(Switches(research=True, browser=True)))["browser"] == "on"


def test_the_browser_needs_a_reader_server_that_offers_browser_tools(
    servers: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(research_module, "isolation_problem", lambda: None)
    servers(READER)  # a fetch server, no browser
    rows = status(Switches(research=True, browser=True))
    assert states(rows) == {
        "mcp": "off",
        "research": "on",
        "search": "off",
        "browser": "unavailable",
        "answers": "off",
    }
    assert reason(rows, "browser") == "no reader server offers a browser tool"


def test_search_is_on_only_when_the_backend_answers_json() -> None:
    up = status(Switches(research=True, search=True), http=search_client(200))
    assert Status("search", "on") in up
    down = status(Switches(search=True), http=search_client(httpx.ConnectError("refused")))
    assert states(down)["search"] == "unavailable"
    assert "not answering" in reason(down, "search")
    assert "saddle search setup" in reason(down, "search")
    forbidden = status(Switches(search=True), http=search_client(403))
    assert "JSON output is off" in reason(forbidden, "search")


def test_search_and_browser_without_research_are_on_with_a_note(
    servers: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(research_module, "isolation_problem", lambda: None)
    servers(BROWSER)
    rows = status(Switches(search=True, browser=True), http=search_client(200))
    assert reason(rows, "search") == "used by research, which is off"
    assert reason(rows, "browser") == "used by research, which is off"
    assert states(rows)["research"] == "off"


# -- `saddle capabilities` ----------------------------------------------------------


def cli(action: str = "status", name: str | None = None) -> tuple[int, str, str]:
    out, err = io.StringIO(), io.StringIO()
    code = run(action, name, stdout=out, stderr=err, http=search_client(httpx.ConnectError("x")))
    return code, out.getvalue(), err.getvalue()


def table_line(out: str, name: str) -> str:
    """The status row for `name` (not the line that says it was switched)."""
    return next(
        line
        for line in out.splitlines()
        if line.split()[0] == name and line.split()[1] in ("on", "off", "unavailable")
    )


def test_the_cli_prints_each_capability_and_nothing_is_on_by_default() -> None:
    code, out, _ = cli()
    assert code == 0
    assert [line.split()[:2] for line in out.splitlines()] == [[n, "off"] for n in NAMES]


def test_enable_turns_it_on_and_the_status_shows_why_it_is_not_working(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    code, out, _ = cli("enable", "search")
    assert code == 0
    assert "search is now on" in out
    search_line = table_line(out, "search")
    assert "unavailable" in search_line
    assert "saddle search setup" in search_line
    code, out, _ = cli("disable", "search")
    assert "search is now off" in out
    assert table_line(out, "search").split()[1] == "off"


def test_enable_without_a_name_or_with_a_wrong_one_or_a_broken_file_is_an_error() -> None:
    code, _, err = cli("enable")
    assert code == 2
    assert "needs a capability" in err
    code, _, err = cli("enable", "everything")
    assert code == 1
    assert "is not a capability" in err
    file_path().write_text("{broken")
    code, _, err = cli()
    assert code == 1
    assert "not valid JSON" in err


def test_the_cli_routes_capabilities_and_needs_no_model_key(
    capsys: pytest.CaptureFixture[str],
) -> None:
    from saddle.cli import main

    assert main(["capabilities"]) == 0
    assert capsys.readouterr().out.split()[:2] == ["mcp", "off"]
    assert main(["capabilities", "enable", "mcp"]) == 0
    assert "mcp is now on" in capsys.readouterr().out


# -- the endpoint the page will read -------------------------------------------------


def test_one_get_endpoint_reports_the_status_and_names_a_broken_file(tmp_path: Path) -> None:
    from starlette.testclient import TestClient
    from test_chat_server import FakeClient

    from saddle.sessions import SessionStore
    from saddle.web.app import build_app

    app = build_app(SessionStore(tmp_path / "s"), FakeClient, default_workdir=tmp_path)
    with TestClient(app) as client:
        reply = client.get("/api/capabilities").json()
        assert reply == {"capabilities": [{"name": n, "state": "off", "reason": ""} for n in NAMES]}
        file_path().write_text("{broken")
        broken = client.get("/api/capabilities")
        assert broken.status_code == 500
        assert "not valid JSON" in broken.json()["error"]
