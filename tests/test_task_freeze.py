"""A Task run is byte-identical with every capability on: it never gets MCP or research.

Known-good: a run with all four capabilities switched on and an allowlist of
servers is offered the same tools, in the same order, with the same system
prompt, as a run with them off; and the run's own tool context has neither.

Known-bad: no `mcp__*` tool and no `research` tool is in a Task run's tool list,
and a call to either is an unknown tool.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import cast

import pytest
from mcp_support import one_server, write_config
from test_auto import Scripted, finish, repo  # noqa: F401  (fixture)

from saddle.auto import AutoOptions, run_auto
from saddle.tools import TOOLS, ToolContext, execute_tool
from saddle.vllm import ToolCall, VllmClient


def offered_and_prompt(repo_path: Path, run_id: str) -> tuple[str, str]:
    client = Scripted([finish()])
    options = AutoOptions(task="make add add", repo=repo_path, run_id=run_id, arm="E")
    run_auto(options, cast(VllmClient, client))
    first = client.asked[0]
    prompt = first["messages"][0]["content"].replace(run_id, "RUN")  # the worktree's name
    return json.dumps(first["tools"], sort_keys=True), prompt


def test_a_task_run_is_the_same_with_every_capability_on(
    repo: Path,  # noqa: F811
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    off_tools, off_prompt = offered_and_prompt(repo, "r-0")
    servers = {
        **one_server(),
        "web": {**one_server()["srv"], "access": "reader", "command": ["uvx", "web==1.2.3"]},
    }
    monkeypatch.setenv("SADDLE_MCP_CONFIG", str(write_config(tmp_path / "mcp.json", servers)))
    monkeypatch.setenv("SADDLE_CAPABILITIES", "mcp,research,search,browser")
    on_tools, on_prompt = offered_and_prompt(repo, "r-1")
    names = [t["function"]["name"] for t in json.loads(on_tools)]
    assert not [n for n in names if n.startswith("mcp__") or n == "research"]
    assert names[: len(TOOLS)] == [t["function"]["name"] for t in TOOLS]
    assert on_tools == off_tools
    assert on_prompt == off_prompt


def test_a_task_runs_tool_context_has_no_mcp_or_research_to_call(tmp_path: Path) -> None:
    ctx = ToolContext(workdir=tmp_path)
    for name in ("mcp__srv__read", "research"):
        result = execute_tool(
            ToolCall(id="c", name=name, arguments='{"question": "x"}'),
            workdir=tmp_path,
            context=ctx,
        )
        assert result == f"error: unknown tool {name!r}"
