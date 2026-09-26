"""TOOLSCOPE: the terminal chat (`saddle up`) is scoped by lane exactly as the web chat is.

Contract: for each chat lane, the tool list the terminal model is offered
equals the list the web model is offered, and a call to a tool outside that
list is refused before it runs on both. Both paths go through one function,
`tools.scope_turn`, so they cannot drift.

Known-good: Ask offers a strict subset of Edit, on both paths; an Edit turn
in the terminal edits. Known-bad: in the terminal's default lane (Ask) a
scripted `edit_file` or `run_command` call gets a refusal result, the file
is byte-for-byte unchanged, and the journal records the refusal.
"""

from __future__ import annotations

import io
import json
import time
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from rich.console import Console
from starlette.testclient import TestClient

from saddle.chat import ChatOptions, _run_turn
from saddle.cli import build_parser
from saddle.journal import read_spans
from saddle.sessions import SessionStore
from saddle.timeline import Timeline
from saddle.tools import READ_ONLY_TOOLS, REFUSED, TOOLS
from saddle.vllm import ToolCall
from saddle.web.app import ChatServer, build_app

ORIGINAL = "def add(a, b):\n    return a - b\n"
EDIT = ToolCall(
    id="e1",
    name="edit_file",
    arguments=json.dumps({"path": "calc.py", "old": "a - b", "new": "a + b"}),
)
SHELL = ToolCall(id="s1", name="run_command", arguments=json.dumps({"command": "rm calc.py"}))


class Scripted:
    """A client that emits `first` on its first round and nothing after it,
    recording the full tool schemas each round was offered."""

    def __init__(self, first: list[Any], seen: list[list[dict[str, Any]]]) -> None:
        self.first = list(first)
        self.seen = seen

    def __enter__(self) -> Scripted:
        return self

    def __exit__(self, *_: object) -> None:
        return None

    def max_model_len(self) -> int:
        return 200_000

    def stream_chat(self, messages: Any, **kwargs: Any) -> Iterator[Any]:
        self.seen.append(list(kwargs["tools"]))
        first, self.first = self.first, []
        return iter(first)


def _names(tools: list[dict[str, Any]]) -> list[str]:
    return [t["function"]["name"] for t in tools]


def _workdir(tmp_path: Path) -> Path:
    work = tmp_path / "work"
    work.mkdir(parents=True)
    (work / "calc.py").write_text(ORIGINAL)
    return work


def _server_of(app: Any) -> ChatServer:
    return next(
        cell.cell_contents
        for route in app.routes
        for cell in (getattr(getattr(route, "endpoint", None), "__closure__", None) or ())
        if isinstance(cell.cell_contents, ChatServer)
    )


def _web_turn(tmp_path: Path, mode: str, first: list[Any]) -> list[list[dict[str, Any]]]:
    work = _workdir(tmp_path)
    seen: list[list[dict[str, Any]]] = []
    app = build_app(
        SessionStore(tmp_path / "s"), lambda: Scripted(first, seen), default_workdir=work
    )
    server = _server_of(app)
    with TestClient(app) as client:
        sid = client.post("/api/sessions", json={}).json()["id"]
        assert client.patch(f"/api/sessions/{sid}", json={"mode": mode}).status_code == 200
        assert client.post(f"/api/sessions/{sid}/message", json={"text": "go"}).status_code == 200
        deadline = time.monotonic() + 10
        while server._live(sid).busy and time.monotonic() < deadline:
            time.sleep(0.02)
    return seen


def _terminal_turn(
    tmp_path: Path, options: ChatOptions, first: list[Any]
) -> tuple[list[list[dict[str, Any]]], list[dict[str, Any]]]:
    seen: list[list[dict[str, Any]]] = []
    messages: list[dict[str, Any]] = []
    display = Timeline(Console(file=io.StringIO(), width=80))
    with display.live_turn():
        _run_turn(
            Scripted(first, seen),  # type: ignore[arg-type]
            messages,
            "go",
            options,
            turn=1,
            parent=None,
            display=display,
        )
    return seen, messages


def _tool_results(messages: list[dict[str, Any]]) -> list[str]:
    return [str(m["content"]) for m in messages if m.get("role") == "tool"]


@pytest.mark.parametrize("mode", ["ask", "edit"])
def test_the_terminal_model_is_offered_exactly_what_the_web_model_is(
    tmp_path: Path, mode: str
) -> None:
    web = _web_turn(tmp_path / "web", mode, [])
    terminal, _ = _terminal_turn(
        tmp_path,
        ChatOptions(workdir=_workdir(tmp_path), journal=tmp_path / "j.jsonl", mode=mode),
        [],
    )
    assert web
    assert terminal
    assert terminal == web


def test_ask_is_a_strict_subset_of_edit_on_the_terminal(tmp_path: Path) -> None:
    ask, _ = _terminal_turn(
        tmp_path / "a",
        ChatOptions(workdir=_workdir(tmp_path / "a"), journal=tmp_path / "a.jsonl"),
        [],
    )
    edit, _ = _terminal_turn(
        tmp_path / "e",
        ChatOptions(workdir=_workdir(tmp_path / "e"), journal=tmp_path / "e.jsonl", mode="edit"),
        [],
    )
    assert _names(ask[0]) == list(READ_ONLY_TOOLS)
    assert _names(edit[0]) == _names(TOOLS)
    assert set(_names(ask[0])) < set(_names(edit[0]))


@pytest.mark.parametrize("call", [EDIT, SHELL], ids=["edit_file", "run_command"])
def test_a_terminal_ask_turn_refuses_a_tool_outside_its_list(
    tmp_path: Path, call: ToolCall
) -> None:
    work = _workdir(tmp_path)
    journal = tmp_path / "chat.jsonl"
    seen, messages = _terminal_turn(tmp_path, ChatOptions(workdir=work, journal=journal), [call])
    assert call.name not in _names(seen[0])
    [result] = _tool_results(messages)
    assert result.startswith(REFUSED)
    assert "not changed" in result
    assert (work / "calc.py").read_text() == ORIGINAL
    [span] = read_spans(journal)
    assert span.argv[0] == call.name
    assert span.exit_code == 1
    assert span.detail == result


def test_a_terminal_edit_turn_edits(tmp_path: Path) -> None:
    work = _workdir(tmp_path)
    _, messages = _terminal_turn(
        tmp_path, ChatOptions(workdir=work, journal=tmp_path / "j.jsonl", mode="edit"), [EDIT]
    )
    [result] = _tool_results(messages)
    assert not result.startswith("error: ")
    assert (work / "calc.py").read_text() == ORIGINAL.replace("a - b", "a + b")


def test_saddle_up_defaults_to_ask_as_the_web_chat_does() -> None:
    assert ChatOptions().mode == "ask"
    assert build_parser().parse_args(["up"]).mode == "ask"
    assert build_parser().parse_args(["up", "--mode", "edit"]).mode == "edit"
    with pytest.raises(SystemExit):
        build_parser().parse_args(["up", "--mode", "task"])
