"""Tests for saddle.chat: the `saddle up` streaming REPL."""

from __future__ import annotations

import hashlib
import io
import json
import os
from pathlib import Path
from typing import Any

import httpx
import pytest

from saddle.chat import ChatOptions, _run_turn, _seal_turn, _stream_response, run_chat
from saddle.journal import read_records, read_spans, verify_journal
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

    text, reasoning, calls = _stream_response(
        _sse_client(body),
        [{"role": "user", "content": "hi"}],
        ChatOptions(),
        stdout=stdout,
    )

    assert stdout.getvalue() == "thinking: Let me think. \nHi there!\n"
    assert text == "Hi there!"
    assert reasoning == "Let me think. "
    assert calls == []


def test_stream_response_content_without_reasoning() -> None:
    stdout = io.StringIO()

    text, reasoning, calls = _stream_response(
        _sse_client(_chunk({"content": "Hi!"}) + "data: [DONE]\n\n"),
        [{"role": "user", "content": "hi"}],
        ChatOptions(),
        stdout=stdout,
    )

    assert stdout.getvalue() == "Hi!\n"
    assert text == "Hi!"
    assert reasoning == ""
    assert calls == []


def test_stream_response_reasoning_without_content() -> None:
    stdout = io.StringIO()

    text, reasoning, calls = _stream_response(
        _sse_client(_chunk({"reasoning": "Hmm. "}) + "data: [DONE]\n\n"),
        [{"role": "user", "content": "hi"}],
        ChatOptions(),
        stdout=stdout,
    )

    assert stdout.getvalue() == "thinking: Hmm. \n"
    assert text == ""
    assert reasoning == "Hmm. "
    assert calls == []


def test_stream_response_empty_response_prints_nothing() -> None:
    stdout = io.StringIO()

    text, reasoning, calls = _stream_response(
        _sse_client("data: [DONE]\n\n"),
        [{"role": "user", "content": "hi"}],
        ChatOptions(),
        stdout=stdout,
    )

    assert stdout.getvalue() == ""
    assert text == ""
    assert reasoning == ""
    assert calls == []


def test_stream_response_collects_tool_calls() -> None:
    body = (
        _chunk({"content": "Ok. "})
        + _chunk({"tool_calls": [{"id": "c1", "index": 0, "function": {"name": "add"}}]})
        + _chunk({"tool_calls": [{"index": 0, "function": {"arguments": '{"a": 1}'}}]})
        + "data: [DONE]\n\n"
    )
    stdout = io.StringIO()

    text, reasoning, calls = _stream_response(
        _sse_client(body),
        [{"role": "user", "content": "hi"}],
        ChatOptions(),
        stdout=stdout,
    )

    assert stdout.getvalue() == "Ok. \n"
    assert text == "Ok. "
    assert reasoning == ""
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


def test_seal_turn_chains_proofs_with_hashed_turns(tmp_path: Path) -> None:
    journal = tmp_path / "chat.jsonl"
    plain = [{"reply": "Hi!", "tools": []}]

    first = _seal_turn(
        journal, turn=1, prompt="hello", rounds=plain, reasoning="Think. ", parent=None
    )

    records = read_records(journal)
    assert len(records) == 1
    assert records[0].evidence_id == "chat#1"
    assert records[0].node_id == "chat#1"
    assert records[0].parent_proofs == []
    assert records[0].gate_outputs == []
    assert records[0].requirement_ids == []
    assert records[0].thinking == "Think. "
    assert records[0].diff_hash == (
        hashlib.sha256(json.dumps({"prompt": "hello", "rounds": plain}).encode()).hexdigest()
    )
    assert first == records[0].record_hash

    tooled = [
        {
            "reply": "Running. ",
            "tools": [{"name": "read_file", "arguments": '{"path": "x"}', "result": "hi"}],
        }
    ]
    second = _seal_turn(
        journal, turn=2, prompt="again", rounds=tooled, reasoning="More. ", parent=first
    )

    records = read_records(journal)
    assert len(records) == 2
    assert records[1].evidence_id == "chat#2"
    assert records[1].node_id == "chat#2"
    assert records[1].parent_proofs == [first]
    assert records[1].diff_hash == (
        hashlib.sha256(json.dumps({"prompt": "again", "rounds": tooled}).encode()).hexdigest()
    )
    assert second == records[1].record_hash
    assert verify_journal(journal) == []


def test_run_turn_appends_plain_reply(tmp_path: Path) -> None:
    journal = tmp_path / "chat.jsonl"
    messages: list[dict[str, Any]] = []
    stdout = io.StringIO()

    new_parent = _run_turn(
        _sse_client(_chunk({"content": "Hi!"}) + "data: [DONE]\n\n"),
        messages,
        "hello",
        ChatOptions(journal=journal),
        turn=1,
        parent=None,
        stdout=stdout,
    )

    assert stdout.getvalue() == "Hi!\n"
    assert messages == [
        {"role": "user", "content": "hello"},
        {"role": "assistant", "content": "Hi!"},
    ]
    (record,) = read_records(journal)
    assert record.evidence_id == "chat#1"
    assert record.node_id == "chat#1"
    assert record.parent_proofs == []
    assert record.thinking == ""
    expected = json.dumps({"prompt": "hello", "rounds": [{"reply": "Hi!", "tools": []}]})
    assert record.diff_hash == hashlib.sha256(expected.encode()).hexdigest()
    assert new_parent == record.record_hash
    assert read_spans(journal) == []
    assert verify_journal(journal) == []


def test_run_turn_executes_tool_calls_then_answers(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (tmp_path / "note.txt").write_text("hello\n")
    journal = tmp_path / "chat.jsonl"
    parent = _seal_turn(
        journal,
        turn=1,
        prompt="first",
        rounds=[{"reply": "R.", "tools": []}],
        reasoning="",
        parent=None,
    )
    tool_body = (
        _chunk({"reasoning": "R1. "})
        + _chunk({"content": "Running. "})
        + _chunk({"tool_calls": [{"id": "c1", "index": 0, "function": {"name": "read_file"}}]})
        + _chunk({"tool_calls": [{"index": 0, "function": {"arguments": '{"path": "note.txt"}'}}]})
        + "data: [DONE]\n\n"
    )
    final_body = _chunk({"reasoning": "R2. "}) + _chunk({"content": "Seen."}) + "data: [DONE]\n\n"
    ticks = iter([100.0, 110.5])
    monkeypatch.setattr("saddle.chat.perf_counter", lambda: next(ticks))
    seen: list[httpx.Request] = []
    messages: list[dict[str, Any]] = []
    stdout = io.StringIO()

    new_parent = _run_turn(
        _scripted_client([tool_body, final_body], seen),
        messages,
        "read it",
        ChatOptions(workdir=tmp_path, journal=journal),
        turn=2,
        parent=parent,
        stdout=stdout,
    )

    assert stdout.getvalue() == (
        "thinking: R1. \n"
        "Running. \n"
        '$ read_file {"path": "note.txt"}\n'
        "hello\n"
        "\n"
        "thinking: R2. \n"
        "Seen.\n"
    )
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
    first_record, record = read_records(journal)
    assert record.evidence_id == "chat#2"
    assert record.node_id == "chat#2"
    assert record.parent_proofs == [parent]
    assert record.thinking == "R1. R2. "
    expected_rounds = [
        {
            "reply": "Running. ",
            "tools": [
                {
                    "name": "read_file",
                    "arguments": '{"path": "note.txt"}',
                    "result": "hello\n",
                }
            ],
        },
        {"reply": "Seen.", "tools": []},
    ]
    expected = json.dumps({"prompt": "read it", "rounds": expected_rounds})
    assert record.diff_hash == hashlib.sha256(expected.encode()).hexdigest()
    assert new_parent == record.record_hash
    assert first_record.record_hash == parent
    (span,) = read_spans(journal)
    assert span.node_id == "chat#2"
    assert span.argv == ["read_file", '{"path": "note.txt"}']
    assert span.exit_code == 0
    assert span.duration_ms == 10500
    assert span.detail == "hello\n"
    assert verify_journal(journal) == []


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
    journal = tmp_path / "chat.jsonl"
    parent = _seal_turn(
        journal,
        turn=1,
        prompt="first",
        rounds=[{"reply": "R.", "tools": []}],
        reasoning="",
        parent=None,
    )

    new_parent = _run_turn(
        _sse_client(tool_body, seen),
        [],
        "go",
        ChatOptions(workdir=tmp_path, journal=journal),
        turn=2,
        parent=parent,
        stdout=stdout,
    )

    assert len(seen) == 10
    out = stdout.getvalue()
    assert out.count("$ read_file") == 10
    assert out.endswith("error: gave up after 10 tool rounds\n")
    _, record = read_records(journal)
    assert record.evidence_id == "chat#2"
    assert record.parent_proofs == [parent]
    assert record.thinking == ""
    one_round = {
        "reply": "",
        "tools": [
            {
                "name": "read_file",
                "arguments": '{"path": "missing.txt"}',
                "result": "error: cannot read 'missing.txt'",
            }
        ],
    }
    expected = json.dumps({"prompt": "go", "rounds": [one_round] * 10})
    assert record.diff_hash == hashlib.sha256(expected.encode()).hexdigest()
    assert new_parent == record.record_hash
    spans = read_spans(journal)
    assert len(spans) == 10
    assert all(span.exit_code == 1 for span in spans)
    assert all(span.node_id == "chat#2" for span in spans)
    assert spans[0].detail == "error: cannot read 'missing.txt'"
    assert verify_journal(journal) == []


def test_run_chat_keeps_history_across_turns(tmp_path: Path) -> None:
    seen: list[httpx.Request] = []
    stdout = io.StringIO()
    journal = tmp_path / "chat.jsonl"

    code = run_chat(
        ChatOptions(journal=journal),
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
    first, second = read_records(journal)
    assert first.evidence_id == "chat#1"
    assert first.parent_proofs == []
    assert second.evidence_id == "chat#2"
    assert second.parent_proofs == [first.record_hash]
    assert verify_journal(journal) == []


def test_run_chat_eof_exits(tmp_path: Path) -> None:
    seen: list[httpx.Request] = []
    stdout = io.StringIO()
    journal = tmp_path / "chat.jsonl"

    code = run_chat(
        ChatOptions(journal=journal),
        _sse_client("data: [DONE]\n\n", seen),
        stdin=io.StringIO(""),
        stdout=stdout,
    )

    assert code == 0
    assert stdout.getvalue() == "you> "
    assert seen == []
    assert not journal.exists()


def test_run_chat_skips_blank_lines(tmp_path: Path) -> None:
    seen: list[httpx.Request] = []
    stdout = io.StringIO()
    journal = tmp_path / "chat.jsonl"

    code = run_chat(
        ChatOptions(journal=journal),
        _sse_client("data: [DONE]\n\n", seen),
        stdin=io.StringIO("\n  \n/quit\n"),
        stdout=stdout,
    )

    assert code == 0
    assert stdout.getvalue() == "you> you> you> "
    assert seen == []
    assert not journal.exists()


def test_run_chat_server_error_keeps_session(tmp_path: Path) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        msg = "refused"
        raise httpx.ConnectError(msg)

    client = VllmClient(api_key="k", transport=httpx.MockTransport(handler))
    stdout = io.StringIO()
    journal = tmp_path / "chat.jsonl"

    code = run_chat(
        ChatOptions(journal=journal),
        client,
        stdin=io.StringIO("hi\n/quit\n"),
        stdout=stdout,
    )

    assert code == 0
    assert stdout.getvalue() == "you> error: request failed: refused\nyou> "
    assert not journal.exists()


def test_run_chat_failed_turn_leaves_gap_then_chains(tmp_path: Path) -> None:
    ok = httpx.Response(
        200,
        text=_chunk({"content": "Hi!"}) + "data: [DONE]\n\n",
        headers={"Content-Type": "text/event-stream"},
    )
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        if calls == 1:
            return httpx.Response(500, text="boom")
        return ok

    client = VllmClient(api_key="k", transport=httpx.MockTransport(handler))
    stdout = io.StringIO()
    journal = tmp_path / "chat.jsonl"

    code = run_chat(
        ChatOptions(journal=journal),
        client,
        stdin=io.StringIO("one\ntwo\n/quit\n"),
        stdout=stdout,
    )

    assert code == 0
    assert stdout.getvalue() == "you> error: server returned HTTP 500: boom\nyou> Hi!\nyou> "
    (record,) = read_records(journal)
    assert record.evidence_id == "chat#2"
    assert record.parent_proofs == []
    assert verify_journal(journal) == []


def test_run_chat_keyboard_interrupt_returns_130(tmp_path: Path) -> None:
    class _InterruptingStdin(io.StringIO):
        def readline(self, size: int = -1) -> str:  # type: ignore[override]
            raise KeyboardInterrupt

    stdout = io.StringIO()
    journal = tmp_path / "chat.jsonl"

    code = run_chat(
        ChatOptions(journal=journal),
        _sse_client("data: [DONE]\n\n"),
        stdin=_InterruptingStdin("hi\n"),
        stdout=stdout,
    )

    assert code == 130
    assert stdout.getvalue() == "you> \n"
    assert not journal.exists()


@needs_live
def test_live_chat_session_streams_reasoning_and_tool_calls(tmp_path: Path) -> None:
    assert _LIVE_KEY is not None
    (tmp_path / "note.txt").write_text("The launch code is 7-7-0.\n")
    stdin = io.StringIO("Read note.txt and quote the launch code back to me.\n/quit\n")
    stdout = io.StringIO()
    journal = tmp_path / "chat.jsonl"
    options = ChatOptions(
        workdir=tmp_path, journal=journal, max_tokens=1024, reasoning_effort="low"
    )

    with VllmClient(api_key=_LIVE_KEY) as client:
        code = run_chat(options, client, stdin=stdin, stdout=stdout)

    out = stdout.getvalue()
    assert code == 0
    assert "thinking: " in out
    assert "$ read_file" in out
    assert "7-7-0" in out
    assert verify_journal(journal) == []
    assert len(read_records(journal)) == 1
