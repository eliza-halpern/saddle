"""Tests for saddle.tools: file and shell tools plus the dispatcher."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from saddle.tools import _read_file, _run_command, _write_file, execute_tool
from saddle.vllm import ToolCall


def test_read_file_returns_content(tmp_path: Path) -> None:
    (tmp_path / "note.txt").write_text("hello\n")

    assert _read_file({"path": "note.txt"}, tmp_path) == "hello\n"


def test_read_file_rejects_non_string_path(tmp_path: Path) -> None:
    for arguments in ({"path": 7}, {}, {"path": None}):
        assert _read_file(arguments, tmp_path) == "error: read_file needs a string path argument"


def test_read_file_missing_file_is_an_error_string(tmp_path: Path) -> None:
    assert _read_file({"path": "missing.txt"}, tmp_path) == ("error: cannot read 'missing.txt'")


def test_read_file_directory_is_an_error_string(tmp_path: Path) -> None:
    (tmp_path / "sub").mkdir()

    assert _read_file({"path": "sub"}, tmp_path) == "error: cannot read 'sub'"


def test_write_file_creates_parents_and_reports(tmp_path: Path) -> None:
    result = _write_file({"path": "sub/deep/note.txt", "content": "hello\n"}, tmp_path)

    assert result == "wrote 'sub/deep/note.txt'"
    assert (tmp_path / "sub" / "deep" / "note.txt").read_text() == "hello\n"


def test_write_file_rejects_non_string_arguments(tmp_path: Path) -> None:
    expected = "error: write_file needs string path and content arguments"
    assert _write_file({"path": 7, "content": "x"}, tmp_path) == expected
    assert _write_file({"path": "a.txt", "content": 7}, tmp_path) == expected
    assert _write_file({}, tmp_path) == expected


def test_write_file_directory_is_an_error_string(tmp_path: Path) -> None:
    (tmp_path / "sub").mkdir()

    assert _write_file({"path": "sub", "content": "x"}, tmp_path) == ("error: cannot write 'sub'")


def test_run_command_reports_exit_and_outputs(tmp_path: Path) -> None:
    assert _run_command({"command": "echo hi"}, tmp_path) == ("exit 0\nstdout:\nhi\nstderr:\n")
    assert _run_command({"command": "echo oops >&2; exit 3"}, tmp_path) == (
        "exit 3\nstdout:\nstderr:\noops\n"
    )


def test_run_command_runs_in_workdir(tmp_path: Path) -> None:
    (tmp_path / "marker.txt").write_text("here\n")

    assert _run_command({"command": "cat marker.txt"}, tmp_path) == (
        "exit 0\nstdout:\nhere\nstderr:\n"
    )


def test_run_command_rejects_non_string_command(tmp_path: Path) -> None:
    assert _run_command({"command": 7}, tmp_path) == (
        "error: run_command needs a string command argument"
    )
    assert _run_command({}, tmp_path) == ("error: run_command needs a string command argument")


def test_run_command_timeout_is_an_error_string(tmp_path: Path) -> None:
    assert _run_command({"command": "sleep 5"}, tmp_path, timeout=0.1) == (
        "error: command timed out after 0.1 seconds"
    )


def test_run_command_default_timeout_is_sixty_seconds(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def raiser(*args: object, **kwargs: object) -> subprocess.CompletedProcess[str]:
        cmd = "sleep 5"
        raise subprocess.TimeoutExpired(cmd, 60)

    monkeypatch.setattr(subprocess, "run", raiser)

    assert _run_command({"command": "sleep 5"}, tmp_path) == (
        "error: command timed out after 60 seconds"
    )


def test_execute_tool_dispatches_all_handlers(tmp_path: Path) -> None:
    (tmp_path / "note.txt").write_text("hello\n")

    assert (
        execute_tool(
            ToolCall(id="1", name="read_file", arguments='{"path": "note.txt"}'), workdir=tmp_path
        )
        == "hello\n"
    )
    assert (
        execute_tool(
            ToolCall(
                id="2",
                name="write_file",
                arguments='{"path": "out.txt", "content": "hi"}',
            ),
            workdir=tmp_path,
        )
        == "wrote 'out.txt'"
    )
    assert (tmp_path / "out.txt").read_text() == "hi"
    assert (
        execute_tool(
            ToolCall(id="3", name="run_command", arguments='{"command": "echo hi"}'),
            workdir=tmp_path,
        )
        == "exit 0\nstdout:\nhi\nstderr:\n"
    )


def test_execute_tool_rejects_bad_arguments(tmp_path: Path) -> None:
    bad = ToolCall(id="1", name="read_file", arguments="{oops")
    assert execute_tool(bad, workdir=tmp_path) == (
        "error: arguments are not valid JSON: Expecting property name "
        "enclosed in double quotes: line 1 column 2 (char 1)"
    )
    listed = ToolCall(id="2", name="read_file", arguments="[1]")
    assert execute_tool(listed, workdir=tmp_path) == ("error: arguments must be a JSON object")


def test_execute_tool_rejects_unknown_tool(tmp_path: Path) -> None:
    call = ToolCall(id="1", name="frobnicate", arguments="{}")

    assert execute_tool(call, workdir=tmp_path) == "error: unknown tool 'frobnicate'"
