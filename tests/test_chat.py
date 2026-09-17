"""Tests for saddle.chat: the `saddle up` streaming REPL."""

from __future__ import annotations

import io
import json
import os
from pathlib import Path
from typing import Any

import httpx
import pytest

from saddle.chat import ChatOptions, _run_turn, _stream_response, run_chat
from saddle.tools import TOOLS
from saddle.vllm import ToolCall, VllmClient

_LIVE_KEY = os.environ.get("SADDLE_VLLM_API_KEY") or os.environ.get("VLLM_API_KEY")

needs_live = pytest.mark.skipif(not _LIVE_KEY, reason="live proof needs SADDLE_VLLM_API_KEY")


def _chunk(delta: dict[str, Any]) -> str:
    return "data: " + json.dumps({"choices": [{"delta": delta}]}) + "\n\n"


def _sse_client(body: str, seen: list[httpx.Request] | None = None) -> VllmClient:
    response = httpx.Response(200, text=body, headers={"Content-Type": "text/event-stream"})

    def handler(request: httpx.Request) -> httpx.Response:
        if seen is not None:
            seen.append(request)
        return response

    return VllmClient(api_key="k", transport=httpx.MockTransport(handler))


def _scripted_client(bodies: list[str], seen: list[httpx.Request] | None = None) -> VllmClient:
    queue = [
        httpx.Response(200, text=body, headers={"Content-Type": "text/event-stream"})
        for body in bodies
    ]

    def handler(request: httpx.Request) -> httpx.Response:
        if seen is not None:
            seen.append(request)
        return queue.pop(0)

    return VllmClient(api_key="k", transport=httpx.MockTransport(handler))


def test_stream_response_prints_reasoning_then_content() -> None:
    body = (
        _chunk({"reasoning": "Let me "})
        + _chunk({"reasoning": "think. "})
        + _chunk({"content": "Hi "})
        + _chunk({"content": "there!"})
        + "data: [DONE]\n\n"
    )
    stdout = io.StringIO()

    text, calls = _stream_response(
        _sse_client(body),
        [{"role": "user", "content": "hi"}],
        ChatOptions(),
        stdout=stdout,
    )

    assert stdout.getvalue() == "thinking: Let me think. \nHi there!\n"
    assert text == "Hi there!"
    assert calls == []


def test_stream_response_content_without_reasoning() -> None:
    stdout = io.StringIO()

    text, calls = _stream_response(
        _sse_client(_chunk({"content": "Hi!"}) + "data: [DONE]\n\n"),
        [{"role": "user", "content": "hi"}],
        ChatOptions(),
        stdout=stdout,
    )

    assert stdout.getvalue() == "Hi!\n"
    assert text == "Hi!"
    assert calls == []


def test_stream_response_reasoning_without_content() -> None:
    stdout = io.StringIO()

    text, calls = _stream_response(
        _sse_client(_chunk({"reasoning": "Hmm. "}) + "data: [DONE]\n\n"),
        [{"role": "user", "content": "hi"}],
        ChatOptions(),
        stdout=stdout,
    )

    assert stdout.getvalue() == "thinking: Hmm. \n"
    assert text == ""
    assert calls == []


def test_stream_response_empty_response_prints_nothing() -> None:
    stdout = io.StringIO()

    text, calls = _stream_response(
        _sse_client("data: [DONE]\n\n"),
        [{"role": "user", "content": "hi"}],
        ChatOptions(),
        stdout=stdout,
    )

    assert stdout.getvalue() == ""
    assert text == ""
    assert calls == []


def test_stream_response_collects_tool_calls() -> None:
    body = (
        _chunk({"content": "Ok. "})
        + _chunk({"tool_calls": [{"id": "c1", "index": 0, "function": {"name": "add"}}]})
        + _chunk({"tool_calls": [{"index": 0, "function": {"arguments": '{"a": 1}'}}]})
        + "data: [DONE]\n\n"
    )
    stdout = io.StringIO()

    text, calls = _stream_response(
        _sse_client(body),
        [{"role": "user", "content": "hi"}],
        ChatOptions(),
        stdout=stdout,
    )

    assert stdout.getvalue() == "Ok. \n"
    assert text == "Ok. "
    assert calls == [ToolCall(id="c1", name="add", arguments='{"a": 1}')]


def test_stream_response_sends_knobs_and_tools() -> None:
    seen: list[httpx.Request] = []
    stdout = io.StringIO()
    options = ChatOptions(max_tokens=128, temperature=0.5, reasoning_effort="low")

    _stream_response(
        _sse_client("data: [DONE]\n\n", seen),
        [{"role": "user", "content": "hi"}],
        options,
        stdout=stdout,
    )

    body = json.loads(seen[0].content)
    assert body["max_tokens"] == 128
    assert body["temperature"] == 0.5
    assert body["reasoning_effort"] == "low"
    assert body["tools"] == TOOLS
    assert body["tool_choice"] == "auto"


def test_run_turn_appends_plain_reply() -> None:
    messages: list[dict[str, Any]] = []
    stdout = io.StringIO()

    _run_turn(
        _sse_client(_chunk({"content": "Hi!"}) + "data: [DONE]\n\n"),
        messages,
        "hello",
        ChatOptions(),
        stdout=stdout,
    )

    assert stdout.getvalue() == "Hi!\n"
    assert messages == [
        {"role": "user", "content": "hello"},
        {"role": "assistant", "content": "Hi!"},
    ]


def test_run_turn_executes_tool_calls_then_answers(tmp_path: Path) -> None:
    (tmp_path / "note.txt").write_text("hello\n")
    tool_body = (
        _chunk({"content": "Running. "})
        + _chunk({"tool_calls": [{"id": "c1", "index": 0, "function": {"name": "read_file"}}]})
        + _chunk({"tool_calls": [{"index": 0, "function": {"arguments": '{"path": "note.txt"}'}}]})
        + "data: [DONE]\n\n"
    )
    final_body = _chunk({"content": "Seen."}) + "data: [DONE]\n\n"
    seen: list[httpx.Request] = []
    messages: list[dict[str, Any]] = []
    stdout = io.StringIO()

    _run_turn(
        _scripted_client([tool_body, final_body], seen),
        messages,
        "read it",
        ChatOptions(workdir=tmp_path),
        stdout=stdout,
    )

    assert stdout.getvalue() == ('Running. \n$ read_file {"path": "note.txt"}\nhello\n\nSeen.\n')
    assert messages == [
        {"role": "user", "content": "read it"},
        {
            "role": "assistant",
            "content": "Running. ",
            "tool_calls": [
                {
                    "id": "c1",
                    "type": "function",
                    "function": {
                        "name": "read_file",
                        "arguments": '{"path": "note.txt"}',
                    },
                }
            ],
        },
        {"role": "tool", "tool_call_id": "c1", "content": "hello\n"},
        {"role": "assistant", "content": "Seen."},
    ]
    assert len(seen) == 2
    assert json.loads(seen[1].content)["messages"] == messages[:3]


def test_run_turn_gives_up_after_ten_tool_rounds(tmp_path: Path) -> None:
    tool_body = (
        _chunk({"tool_calls": [{"id": "c1", "index": 0, "function": {"name": "read_file"}}]})
        + _chunk(
            {
                "tool_calls": [
                    {
                        "index": 0,
                        "function": {"arguments": '{"path": "missing.txt"}'},
                    }
                ]
            }
        )
        + "data: [DONE]\n\n"
    )
    seen: list[httpx.Request] = []
    stdout = io.StringIO()

    _run_turn(
        _sse_client(tool_body, seen),
        [],
        "go",
        ChatOptions(workdir=tmp_path),
        stdout=stdout,
    )

    assert len(seen) == 10
    out = stdout.getvalue()
    assert out.count("$ read_file") == 10
    assert out.endswith("error: gave up after 10 tool rounds\n")


def test_run_chat_keeps_history_across_turns() -> None:
    seen: list[httpx.Request] = []
    stdout = io.StringIO()

    code = run_chat(
        ChatOptions(),
        _sse_client(_chunk({"content": "Hi!"}) + "data: [DONE]\n\n", seen),
        stdin=io.StringIO("one\ntwo\n/quit\n"),
        stdout=stdout,
    )

    assert code == 0
    assert stdout.getvalue() == "you> Hi!\nyou> Hi!\nyou> "
    assert len(seen) == 2
    assert json.loads(seen[1].content)["messages"] == [
        {"role": "user", "content": "one"},
        {"role": "assistant", "content": "Hi!"},
        {"role": "user", "content": "two"},
    ]


def test_run_chat_eof_exits() -> None:
    seen: list[httpx.Request] = []
    stdout = io.StringIO()

    code = run_chat(
        ChatOptions(),
        _sse_client("data: [DONE]\n\n", seen),
        stdin=io.StringIO(""),
        stdout=stdout,
    )

    assert code == 0
    assert stdout.getvalue() == "you> "
    assert seen == []


def test_run_chat_skips_blank_lines() -> None:
    seen: list[httpx.Request] = []
    stdout = io.StringIO()

    code = run_chat(
        ChatOptions(),
        _sse_client("data: [DONE]\n\n", seen),
        stdin=io.StringIO("\n  \n/quit\n"),
        stdout=stdout,
    )

    assert code == 0
    assert stdout.getvalue() == "you> you> you> "
    assert seen == []


def test_run_chat_server_error_keeps_session() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        msg = "refused"
        raise httpx.ConnectError(msg)

    client = VllmClient(api_key="k", transport=httpx.MockTransport(handler))
    stdout = io.StringIO()

    code = run_chat(
        ChatOptions(),
        client,
        stdin=io.StringIO("hi\n/quit\n"),
        stdout=stdout,
    )

    assert code == 0
    assert stdout.getvalue() == "you> error: request failed: refused\nyou> "


def test_run_chat_keyboard_interrupt_returns_130() -> None:
    class _InterruptingStdin(io.StringIO):
        def readline(self, size: int = -1) -> str:  # type: ignore[override]
            raise KeyboardInterrupt

    stdout = io.StringIO()

    code = run_chat(
        ChatOptions(),
        _sse_client("data: [DONE]\n\n"),
        stdin=_InterruptingStdin("hi\n"),
        stdout=stdout,
    )

    assert code == 130
    assert stdout.getvalue() == "you> \n"


@needs_live
def test_live_chat_session_streams_reasoning_and_tool_calls(tmp_path: Path) -> None:
    assert _LIVE_KEY is not None
    (tmp_path / "note.txt").write_text("The launch code is 7-7-0.\n")
    stdin = io.StringIO("Read note.txt and quote the launch code back to me.\n/quit\n")
    stdout = io.StringIO()
    options = ChatOptions(workdir=tmp_path, max_tokens=1024, reasoning_effort="low")

    with VllmClient(api_key=_LIVE_KEY) as client:
        code = run_chat(options, client, stdin=stdin, stdout=stdout)

    out = stdout.getvalue()
    assert code == 0
    assert "thinking: " in out
    assert "$ read_file" in out
    assert "7-7-0" in out
