"""A command never outlives its run.

Known-good: in an autonomous run, a command's timeout and a wait are capped at
the seconds left in the run's time budget, and say so, and a real run
passes its budget to its tools. Known-bad (unchanged): with no
budget (chat), a command gets the timeout it asked for.
"""

from __future__ import annotations

import json
import time
from pathlib import Path

from test_auto import Scripted, auto, call
from test_auto import repo as repo  # the fixture: a git repo with a failing add()

from saddle.tools import ToolContext, execute_tool
from saddle.vllm import ToolCall


def tool(ctx: ToolContext, name: str, **args: object) -> str:
    return execute_tool(
        ToolCall(id="t", name=name, arguments=json.dumps(args)), workdir=ctx.workdir, context=ctx
    )


def test_a_command_is_capped_at_the_time_the_run_has_left(tmp_path: Path) -> None:
    ctx = ToolContext(workdir=tmp_path, time_left=lambda: 1.0)
    started = time.monotonic()
    out = tool(ctx, "run_command", command="sleep 5", timeout=600)
    assert time.monotonic() - started < 4
    assert out.startswith(
        "still running after 1s (capped at the 1s left in this run's time budget)"
    )


def test_a_wait_is_capped_at_the_time_the_run_has_left(tmp_path: Path) -> None:
    ctx = ToolContext(workdir=tmp_path, time_left=lambda: 1.0)
    started_line = tool(ctx, "run_command", command="sleep 5", background=True)
    terminal = started_line.split("terminal ", 1)[1].split(" ", 1)[0]
    began = time.monotonic()
    out = tool(ctx, "wait_for_terminal", id=terminal, timeout=600)
    assert time.monotonic() - began < 4
    assert "still running after 1s (capped at the 1s left" in out


def test_without_a_budget_a_command_gets_the_timeout_it_asked_for(tmp_path: Path) -> None:
    ctx = ToolContext(workdir=tmp_path)
    out = tool(ctx, "run_command", command="sleep 2 && echo done", timeout=30)
    assert out.startswith("exit 0")
    assert "done" in out


def test_a_real_run_caps_a_long_command_at_its_budget(repo: Path) -> None:
    """The run passes its own budget to the tools: a 60 s command in a run with
    a few seconds left returns at the cap, and the run stops on its budget."""
    long = call("run_command", "c1", command="sleep 60", timeout=600)
    tail = [call("read_file", "r", path="calc.py")]
    began = time.monotonic()
    result = auto(repo, Scripted([[long]], tail=tail), time_budget_s=4)
    assert time.monotonic() - began < 30
    assert result.outcome == "stopped"
    assert result.reason.startswith("time budget exhausted")
