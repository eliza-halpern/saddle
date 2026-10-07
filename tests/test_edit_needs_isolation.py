"""Without full access, the chat runs no command unconfined (#123).

The chat lanes' sandbox (`ToolContext.box`) took `Sandbox.for_workdir`'s
default, `require_isolation=False`: where bwrap could not start it was built
with `isolation="none"`, and `run_command` ran the model's command as the
person, with the home directory and the host network, while a foreground
result read like any sandboxed one. Kept on the context, that box stayed
unconfined for the session's life, even after bwrap could start again.

Known-bad: isolation unavailable, an Edit-lane command (foreground or
background), a terminal call and an MCP server are refused with the reason,
and nothing runs. Known-good: once bwrap starts, the next command runs,
isolated, since nothing was kept from the refusal; with full access, the
person's own switch, a command still runs as them.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from mcp_support import calls, server_entry, write_config
from test_full_access import BWRAP, run

from saddle import sandbox
from saddle.tools import NO_ISOLATION, UNSANDBOXED, ToolContext

REASON = "bwrap cannot start in this test"
REFUSED = NO_ISOLATION.format(
    problem=f"this lane needs isolation and bwrap cannot provide it: {REASON}"
)


@pytest.fixture
def no_bwrap(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(sandbox, "isolation_problem", lambda: REASON)


@pytest.mark.parametrize("background", [False, True])
def test_without_isolation_an_edit_command_is_refused_and_nothing_runs(
    tmp_path: Path, no_bwrap: None, background: bool
) -> None:
    ctx = ToolContext(workdir=tmp_path)
    assert run(ctx, "run_command", command="touch ran", background=background) == REFUSED
    assert not (tmp_path / "ran").exists()
    assert ctx.sandbox is None  # nothing kept: the next call asks again


def test_a_terminal_call_without_isolation_is_refused_not_a_dead_turn(
    tmp_path: Path, no_bwrap: None
) -> None:
    ctx = ToolContext(workdir=tmp_path)
    assert run(ctx, "read_terminal", id="t1") == REFUSED
    assert run(ctx, "wait_for_terminal", id="t1") == REFUSED


@pytest.mark.skipif(not BWRAP, reason="needs a bwrap that can start")
def test_once_bwrap_starts_the_next_command_runs_isolated(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    problem: list[str | None] = [REASON]
    monkeypatch.setattr(sandbox, "isolation_problem", lambda: problem[0])
    ctx = ToolContext(workdir=tmp_path)
    assert run(ctx, "run_command", command="touch first") == REFUSED
    problem[0] = None
    assert run(ctx, "run_command", command="touch second").startswith("exit 0")
    assert ctx.box().isolation == "bwrap"
    assert (tmp_path / "second").exists()
    assert not (tmp_path / "first").exists()


def test_with_full_access_a_command_still_runs_without_isolation(
    tmp_path: Path, no_bwrap: None
) -> None:
    ctx = ToolContext(workdir=tmp_path, full_access=True)
    assert run(ctx, "run_command", command="touch ran").startswith(f"{UNSANDBOXED}\nexit 0")
    assert (tmp_path / "ran").exists()
    assert ctx.box().isolation == "none"


def test_an_mcp_server_without_isolation_cannot_start_and_nothing_runs(
    tmp_path: Path, no_bwrap: None
) -> None:
    pytest.importorskip("mcp", reason="the MCP SDK (the mcp extra) is not installed")
    from saddle.mcpclient import Approvals, McpError, McpHost, load_config

    config = write_config(tmp_path / "c.json", {"fx": server_entry(tmp_path, tools=["echo"])})
    box = ToolContext(workdir=tmp_path).box
    host = McpHost(load_config(config), Approvals(tmp_path / "a.json"), box)
    try:
        with pytest.raises(McpError, match="'fx' cannot start: this lane needs isolation"):
            host.review("fx")
        assert host.schemas() == []
        assert REASON in host.problems["fx"]
    finally:
        host.close()
    assert calls(tmp_path) == []
