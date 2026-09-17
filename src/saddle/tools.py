"""Agent tools for `saddle up`: file and shell access inside a workdir.

Tools run as the invoking user with no sandbox: the workdir only sets the
default directory, not a boundary. Every failure (bad arguments, OS errors,
unknown tools) becomes a result string so the model can self-correct.
"""

from __future__ import annotations

import json
import subprocess
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any, Final

from saddle.vllm import ToolCall

COMMAND_TIMEOUT: Final = 60

TOOLS: Final[list[dict[str, Any]]] = [
    {
        "type": "function",
        "function": {
            "name": "read_file",
            "description": (
                "Read a UTF-8 text file under the working directory; returns its content."
            ),
            "parameters": {
                "type": "object",
                "properties": {"path": {"type": "string"}},
                "required": ["path"],
                "additionalProperties": False,
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "write_file",
            "description": (
                "Write content to a UTF-8 text file under the working "
                "directory, creating parent directories; returns a "
                "confirmation."
            ),
            "parameters": {
                "type": "object",
                "properties": {"path": {"type": "string"}, "content": {"type": "string"}},
                "required": ["path", "content"],
                "additionalProperties": False,
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "run_command",
            "description": (
                "Run a shell command in the working directory; returns "
                "the exit code plus stdout and stderr."
            ),
            "parameters": {
                "type": "object",
                "properties": {"command": {"type": "string"}},
                "required": ["command"],
                "additionalProperties": False,
            },
        },
    },
]


def _read_file(arguments: Mapping[str, Any], workdir: Path) -> str:
    """Read workdir/path; missing args or OS failures become error strings."""
    path = arguments.get("path")
    if not isinstance(path, str):
        return "error: read_file needs a string path argument"
    try:
        return (workdir / path).read_text()
    except OSError:
        return f"error: cannot read {path!r}"


def _write_file(arguments: Mapping[str, Any], workdir: Path) -> str:
    """Write content to workdir/path, creating parents; errors become strings."""
    path = arguments.get("path")
    content = arguments.get("content")
    if not isinstance(path, str) or not isinstance(content, str):
        return "error: write_file needs string path and content arguments"
    target = workdir / path
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content)
    except OSError:
        return f"error: cannot write {path!r}"
    return f"wrote {path!r}"


def _run_command(
    arguments: Mapping[str, Any], workdir: Path, timeout: float = COMMAND_TIMEOUT
) -> str:
    """Run a shell command in workdir; exit code plus outputs become the result."""
    command = arguments.get("command")
    if not isinstance(command, str):
        return "error: run_command needs a string command argument"
    try:
        completed = subprocess.run(
            command,
            shell=True,
            capture_output=True,
            text=True,
            cwd=workdir,
            timeout=timeout,
        )
    except subprocess.TimeoutExpired:
        return f"error: command timed out after {timeout} seconds"
    return f"exit {completed.returncode}\nstdout:\n{completed.stdout}stderr:\n{completed.stderr}"


_HANDLERS: Final[dict[str, Callable[[Mapping[str, Any], Path], str]]] = {
    "read_file": _read_file,
    "write_file": _write_file,
    "run_command": _run_command,
}


def execute_tool(call: ToolCall, *, workdir: Path) -> str:
    """Run one tool call against workdir; every failure is a result string."""
    try:
        arguments = json.loads(call.arguments)
    except json.JSONDecodeError as exc:
        return f"error: arguments are not valid JSON: {exc}"
    if not isinstance(arguments, dict):
        return "error: arguments must be a JSON object"
    handler = _HANDLERS.get(call.name)
    if handler is None:
        return f"error: unknown tool {call.name!r}"
    return handler(arguments, workdir)
