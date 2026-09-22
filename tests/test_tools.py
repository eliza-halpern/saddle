"""Tool behaviour, under the contract the chat UI needs.

This file was rewritten when the workdir became a **boundary** rather than a
default `cwd` (`sandbox.resolve_within`) and `run_command` moved into the
sandbox. Three parts of the old contract changed deliberately, and each is
pinned here in its new form rather than dropped:

- stdout and stderr are **merged**, because a live terminal shows one
  interleaved stream and splitting them reorders the output a user watches;
- a `run_command` timeout **no longer kills** the command: it reports the
  terminal id so the work can be waited on again, which is what makes a long
  build survive a turn;
- the default timeout is 120s, not 60.

Everything else the old file pinned is still pinned: non-string arguments
are refused rather than coerced, a missing file, a directory read and an
unknown tool are error strings, and every handler is reachable through
`execute_tool`.
"""

from __future__ import annotations

import json
from pathlib import Path

from saddle.tools import ToolContext, execute_tool
from saddle.vllm import ToolCall


def run(name: str, workdir: Path, **kwargs: object) -> str:
    call = ToolCall(id="t1", name=name, arguments=json.dumps(kwargs))
    return execute_tool(call, workdir=workdir, context=ToolContext(workdir=workdir))


# -- read_file ----------------------------------------------------------------

def test_read_file_returns_content(tmp_path: Path) -> None:
    (tmp_path / "note.txt").write_text("hello\n")
    assert run("read_file", tmp_path, path="note.txt") == "hello\n"


def test_read_file_rejects_a_non_string_path(tmp_path: Path) -> None:
    assert run("read_file", tmp_path, path=7) == "error: read_file needs a string path argument"
    assert run("read_file", tmp_path) == "error: read_file needs a string path argument"


def test_read_file_missing_file_is_an_error_string(tmp_path: Path) -> None:
    assert run("read_file", tmp_path, path="missing.txt").startswith("error:")


def test_read_file_directory_is_an_error_string(tmp_path: Path) -> None:
    (tmp_path / "sub").mkdir()
    assert run("read_file", tmp_path, path="sub").startswith("error:")


# -- write_file ---------------------------------------------------------------

def test_write_file_creates_parents_and_reports(tmp_path: Path) -> None:
    result = run("write_file", tmp_path, path="sub/deep/note.txt", content="hello\n")
    assert (tmp_path / "sub" / "deep" / "note.txt").read_text() == "hello\n"
    assert "sub/deep/note.txt" in result


def test_write_file_rejects_non_string_arguments(tmp_path: Path) -> None:
    assert run("write_file", tmp_path, path="a.txt", content=7) == (
        "error: write_file needs a string content argument"
    )
    assert run("write_file", tmp_path) == "error: write_file needs a string path argument"


def test_write_file_directory_is_an_error_string(tmp_path: Path) -> None:
    (tmp_path / "sub").mkdir()
    assert run("write_file", tmp_path, path="sub", content="x").startswith("error:")


# -- the workdir is a boundary, not a default ---------------------------------

def test_reads_and_writes_outside_the_workdir_are_refused(tmp_path: Path) -> None:
    (tmp_path.parent / "secret_outside.txt").write_text("secret")
    assert run("read_file", tmp_path, path="../secret_outside.txt").startswith("error:")
    assert run("write_file", tmp_path, path="/etc/evil", content="x").startswith("error:")


# -- run_command --------------------------------------------------------------

def test_run_command_reports_exit_and_merged_output(tmp_path: Path) -> None:
    result = run("run_command", tmp_path, command="echo hi")
    assert result.startswith("exit 0")
    assert "hi" in result
    failed = run("run_command", tmp_path, command="echo oops >&2; exit 3")
    assert failed.startswith("exit 3")
    assert "oops" in failed


def test_run_command_runs_in_the_workdir(tmp_path: Path) -> None:
    (tmp_path / "marker.txt").write_text("here\n")
    assert "here" in run("run_command", tmp_path, command="cat marker.txt")


def test_run_command_rejects_a_non_string_command(tmp_path: Path) -> None:
    assert run("run_command", tmp_path, command=7) == (
        "error: run_command needs a string command argument"
    )
    assert run("run_command", tmp_path) == "error: run_command needs a string command argument"


def test_a_run_command_timeout_reports_the_terminal_rather_than_killing_it(
    tmp_path: Path,
) -> None:
    result = run("run_command", tmp_path, command="sleep 4; echo done", timeout=1)
    assert "still running" in result
    assert "terminal" in result


# -- dispatch -----------------------------------------------------------------

def test_execute_tool_dispatches_every_declared_tool(tmp_path: Path) -> None:
    (tmp_path / "note.txt").write_text("hello\n")
    context = ToolContext(workdir=tmp_path)

    def call(name: str, **kwargs: object) -> str:
        return execute_tool(
            ToolCall(id="t", name=name, arguments=json.dumps(kwargs)),
            workdir=tmp_path, context=context,
        )

    assert call("read_file", path="note.txt") == "hello\n"
    assert "out.txt" in call("write_file", path="out.txt", content="hi")
    assert "note.txt" in call("list_dir")
    assert "note.txt:1:" in call("search", query="hello")
    started = call("run_command", command="echo x", background=True)
    assert "started terminal" in started
    terminal_id = started.split("terminal ")[1].split()[0]
    assert "x" in call("wait_for_terminal", id=terminal_id, timeout=20)
    assert "[" in call("read_terminal", id=terminal_id)


def test_execute_tool_rejects_bad_arguments(tmp_path: Path) -> None:
    result = execute_tool(
        ToolCall(id="t", name="read_file", arguments="{not json"), workdir=tmp_path
    )
    assert result.startswith("error: arguments are not valid JSON")


def test_execute_tool_rejects_an_unknown_tool(tmp_path: Path) -> None:
    result = execute_tool(ToolCall(id="t", name="nope", arguments="{}"), workdir=tmp_path)
    assert result == "error: unknown tool 'nope'"
