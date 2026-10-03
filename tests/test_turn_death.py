"""A chat turn that dies must say so, and keep what it did.

F28 (a watched trial): the second turn of a full-access session read a file
outside its folder; nothing was ever sent to the model again, the page showed
no error, the journal had no span for the read, and the whole turn -- the
person's message and every tool result -- was missing after a reload.

Two defects, one visible through the other:

* `ToolContext.count_tokens` was bound to the FIRST turn's client. A session's
  context outlives each turn's client, and that client is closed when its turn
  ends, so the next turn's first sized `read_file` asked a closed connection
  and raised.
* `ChatServer._run` saved the transcript only after the turn ended well, so
  any exception lost every message of the turn.

These tests drive the real `ChatServer._run` and the real `run_turn`; only the
model's replies and the tokenizer endpoint are scripted.
"""

from __future__ import annotations

import json
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

import httpx
import pytest
from starlette.testclient import TestClient

import saddle.web.app as module
from saddle.events import ErrorEvent, Event
from saddle.sessions import FULL_ACCESS_CONFIRM, SessionStore
from saddle.vllm import StreamToken, ToolCall, VllmClient
from saddle.web.app import ChatServer, build_app


def _tokenizer(request: httpx.Request) -> httpx.Response:
    return httpx.Response(200, json={"count": 7})


class ScriptedClient(VllmClient):
    """A real client (real transport, real close) whose replies are scripted."""

    rounds: list[list[Any]]  # shared across the clients one test builds

    def __init__(self, rounds: list[list[Any]]) -> None:
        super().__init__(api_key="k", transport=httpx.MockTransport(_tokenizer))
        self.rounds = rounds

    def stream_chat(self, messages: Any, **_kw: Any) -> Iterator[Any]:  # type: ignore[override]
        yield from self.rounds.pop(0) if self.rounds else [StreamToken("content", "done")]

    def max_model_len(self) -> int | None:
        return 200_000


def _read(path: Path, call_id: str) -> ToolCall:
    arguments = json.dumps({"path": str(path), "offset": 2, "limit": 3})
    return ToolCall(id=call_id, name="read_file", arguments=arguments)


@pytest.fixture
def served(tmp_path: Path) -> Iterator[tuple[ChatServer, str, list[list[Any]], Path]]:
    rounds: list[list[Any]] = []
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "Default.ini").write_text("".join(f"line {n}\n" for n in range(1, 30)))
    store = SessionStore(tmp_path / "sessions")
    work = tmp_path / "work"
    work.mkdir()
    app = build_app(store, lambda: ScriptedClient(rounds), default_workdir=work)
    with TestClient(app) as web:
        sid = web.post("/api/sessions", json={"workdir": str(work)}).json()["id"]
        store.update(sid, mode="edit")
        store.set_full_access(sid, True, confirm=FULL_ACCESS_CONFIRM)
        server = next(
            cell.cell_contents
            for route in app.routes
            for cell in (getattr(getattr(route, "endpoint", None), "__closure__", None) or ())
            if isinstance(cell.cell_contents, ChatServer)
        )
        yield server, sid, rounds, outside / "Default.ini"


def _drain(server: ChatServer, sid: str, turn: Callable[[], None]) -> list[Event]:
    channel = server._live(sid).subscribe()
    turn()
    seen: list[Event] = []
    while (event := channel.get_nowait()) is not None:
        seen.append(event)
    return seen


def test_a_second_turn_can_read_a_sized_window_outside_the_folder(
    served: tuple[ChatServer, str, list[list[Any]], Path],
) -> None:
    server, sid, rounds, ini = served
    rounds += [[StreamToken("content", "one")]]
    server._run(sid, "first")  # turn 1: its client is closed when it ends
    rounds += [[_read(ini, "r1")], [StreamToken("content", "read it")]]
    seen = _drain(server, sid, lambda: server._run(sid, "second"))
    assert not [e for e in seen if isinstance(e, ErrorEvent)]
    saved = server.store.load_messages(sid)
    tool_result = next(m for m in saved if m.get("tool_call_id") == "r1")
    assert "line 2" in tool_result["content"]


def _die_at(point: str, monkeypatch: pytest.MonkeyPatch) -> None:
    def boom(*_a: Any, **_k: Any) -> Any:
        message = "injected"
        raise RuntimeError(message)

    if point == "execute":
        import saddle.engine as engine

        monkeypatch.setattr(engine, "execute_tool", boom)
    elif point == "span":
        import saddle.engine as engine

        monkeypatch.setattr(engine, "append_span", boom)
    else:
        monkeypatch.setattr(module, "attach_mcp", boom)


@pytest.mark.parametrize("point", ["execute", "span"])
def test_a_turn_that_dies_mid_tool_publishes_the_error_and_keeps_its_messages(
    served: tuple[ChatServer, str, list[list[Any]], Path],
    point: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    server, sid, rounds, ini = served
    rounds += [[_read(ini, "r1")]]
    _die_at(point, monkeypatch)
    seen = _drain(server, sid, lambda: server._run(sid, "please read"))
    errors = [e for e in seen if isinstance(e, ErrorEvent)]
    assert [e.message for e in errors] == ["the turn stopped: RuntimeError: injected"]
    saved = server.store.load_messages(sid)
    assert [m["content"] for m in saved if m["role"] == "user"] == ["please read"]
    assert any(m.get("tool_calls") for m in saved)  # what it had done so far
    journal = server.store.journal_path(sid).read_text()
    assert '"name": "turn_failed"' in journal
    assert "injected" in journal


def test_a_turn_that_dies_before_the_model_is_asked_still_says_so(
    served: tuple[ChatServer, str, list[list[Any]], Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    server, sid, _rounds, _ini = served
    _die_at("attach", monkeypatch)
    seen = _drain(server, sid, lambda: server._run(sid, "hello"))
    assert [e.message for e in seen if isinstance(e, ErrorEvent)] == [
        "the turn stopped: RuntimeError: injected"
    ]
    assert server._live(sid).busy is False


def test_a_turn_that_dies_before_the_engine_keeps_the_persons_question(
    served: tuple[ChatServer, str, list[list[Any]], Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    server, sid, _rounds, _ini = served
    _die_at("attach", monkeypatch)
    _drain(server, sid, lambda: server._run(sid, "hello"))
    saved = server.store.load_messages(sid)
    assert [m["content"] for m in saved if m["role"] == "user"] == ["hello"]


def test_a_request_left_without_a_result_is_answered_so_the_next_turn_is_valid(
    served: tuple[ChatServer, str, list[list[Any]], Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    server, sid, rounds, ini = served
    rounds += [[_read(ini, "r1")]]
    _die_at("execute", monkeypatch)
    _drain(server, sid, lambda: server._run(sid, "please read"))
    saved = server.store.load_messages(sid)
    asked = [c["id"] for m in saved for c in m.get("tool_calls") or []]
    answered = [m["tool_call_id"] for m in saved if m["role"] == "tool"]
    assert asked == answered == ["r1"]


def test_a_failing_journal_or_save_cannot_hide_the_error_or_each_other(
    served: tuple[ChatServer, str, list[list[Any]], Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    server, sid, _rounds, _ini = served
    _die_at("attach", monkeypatch)

    def refuse(*_a: Any, **_k: Any) -> Any:
        message = "disk full"
        raise OSError(message)

    monkeypatch.setattr(module, "append_span", refuse)
    seen = _drain(server, sid, lambda: server._run(sid, "hello"))
    assert [e.message for e in seen if isinstance(e, ErrorEvent)] == [
        "the turn stopped: RuntimeError: injected"
    ]
    assert [m["content"] for m in server.store.load_messages(sid) if m["role"] == "user"] == [
        "hello"
    ]
    monkeypatch.setattr(server.store, "save_messages", refuse)
    seen = _drain(server, sid, lambda: server._run(sid, "again"))
    assert len([e for e in seen if isinstance(e, ErrorEvent)]) == 1
    assert server._live(sid).busy is False


def test_a_session_that_cannot_be_loaded_has_nothing_to_keep(
    served: tuple[ChatServer, str, list[list[Any]], Path],
) -> None:
    server, _sid, _rounds, _ini = served
    seen = _drain(server, "nosuchsession", lambda: server._run("nosuchsession", "hi"))
    assert [type(e) for e in seen] == [ErrorEvent]
