"""Tests for saddle.chat: the `saddle up` streaming REPL."""

from __future__ import annotations

import hashlib
import io
import json
import os
import uuid
from pathlib import Path
from typing import Any

import httpx
import pytest
from rich.console import Console

from saddle import cli, conditions, engine
from saddle.chat import ChatOptions, _run_turn, _seal_turn, _stream_response, run_chat
from saddle.journal import read_records, read_spans, verify_journal
from saddle.timeline import Timeline
from saddle.tools import TOOLS
from saddle.vllm import ToolCall, VllmClient

RULE = "─" * 80 + "\n"

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


def _display(out: io.StringIO) -> Timeline:
    return Timeline(Console(file=out, width=80))


def test_stream_response_prints_reasoning_then_content() -> None:
    body = (
        _chunk({"reasoning": "Let me "})
        + _chunk({"reasoning": "think. "})
        + _chunk({"content": "Hi "})
        + _chunk({"content": "there!"})
        + "data: [DONE]\n\n"
    )
    out = io.StringIO()
    display = _display(out)

    with display.live_turn():
        text, reasoning, calls = _stream_response(
            _sse_client(body),
            [{"role": "user", "content": "hi"}],
            ChatOptions(),
            display=display,
            tools=TOOLS,
        )

    assert out.getvalue() == (
        "thinking: Let me think. \n" + "saddle> \n" + "Hi there!" + " " * 71 + "\n"
    )
    assert text == "Hi there!"
    assert reasoning == "Let me think. "
    assert calls == []


def test_stream_response_content_without_reasoning() -> None:
    out = io.StringIO()
    display = _display(out)

    with display.live_turn():
        text, reasoning, calls = _stream_response(
            _sse_client(_chunk({"content": "Hi!"}) + "data: [DONE]\n\n"),
            [{"role": "user", "content": "hi"}],
            ChatOptions(),
            display=display,
            tools=TOOLS,
        )

    assert out.getvalue() == "saddle> \n" + "Hi!" + " " * 77 + "\n"
    assert text == "Hi!"
    assert reasoning == ""
    assert calls == []


def test_stream_response_reasoning_without_content() -> None:
    out = io.StringIO()
    display = _display(out)

    with display.live_turn():
        text, reasoning, calls = _stream_response(
            _sse_client(_chunk({"reasoning": "Hmm. "}) + "data: [DONE]\n\n"),
            [{"role": "user", "content": "hi"}],
            ChatOptions(),
            display=display,
            tools=TOOLS,
        )

    assert out.getvalue() == "thinking: Hmm. \n"
    assert text == ""
    assert reasoning == "Hmm. "
    assert calls == []


def test_stream_response_empty_response_prints_nothing() -> None:
    out = io.StringIO()
    display = _display(out)

    with display.live_turn():
        text, reasoning, calls = _stream_response(
            _sse_client("data: [DONE]\n\n"),
            [{"role": "user", "content": "hi"}],
            ChatOptions(),
            display=display,
            tools=TOOLS,
        )

    assert out.getvalue() == ""
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
    out = io.StringIO()
    display = _display(out)

    with display.live_turn():
        text, reasoning, calls = _stream_response(
            _sse_client(body),
            [{"role": "user", "content": "hi"}],
            ChatOptions(),
            display=display,
            tools=TOOLS,
        )

    assert out.getvalue() == "saddle> \n" + "Ok." + " " * 77 + "\n"
    assert text == "Ok. "
    assert reasoning == ""
    assert calls == [ToolCall(id="c1", name="add", arguments='{"a": 1}')]


def test_stream_response_sends_knobs_and_tools() -> None:
    seen: list[httpx.Request] = []
    out = io.StringIO()
    options = ChatOptions(max_tokens=128, temperature=0.5, reasoning_effort="low")

    _stream_response(
        _sse_client("data: [DONE]\n\n", seen),
        [{"role": "user", "content": "hi"}],
        options,
        display=_display(out),
        tools=TOOLS[:1],
    )

    body = json.loads(seen[0].content)
    assert body["max_tokens"] == 128
    assert body["temperature"] == 0.5
    assert body["reasoning_effort"] == "low"
    assert body["tools"] == TOOLS[:1]
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


@pytest.mark.parametrize("keep", [True, False])
def test_terminal_chat_keeps_each_rounds_reasoning_unless_turned_off(
    tmp_path: Path, keep: bool
) -> None:
    """Every mode keeps reasoning: the terminal chat's next round carries the
    last one's think block (Strata's spelling), and `--no-keep-reasoning`
    drops it. Red before: the terminal loop never sent it back."""
    (tmp_path / "calc.py").write_text("x = 1\n")
    call = {"index": 0, "id": "r1", "type": "function"}
    call["function"] = {"name": "read_file", "arguments": json.dumps({"path": "calc.py"})}
    seen: list[httpx.Request] = []
    client = _scripted_client(
        [
            _chunk({"reasoning_content": "read calc.py first"})
            + _chunk({"tool_calls": [call]})
            + "data: [DONE]\n\n",
            _chunk({"content": "done"}) + "data: [DONE]\n\n",
        ],
        seen,
    )
    display = _display(io.StringIO())
    with display.live_turn():
        _run_turn(
            client,
            [],
            "look",
            ChatOptions(workdir=tmp_path, journal=tmp_path / "c.jsonl", keep_reasoning=keep),
            turn=1,
            parent=None,
            display=display,
        )
    (sent,) = [m for m in json.loads(seen[1].content)["messages"] if m["role"] == "assistant"]
    assert sent.get("reasoning_content") == ("read calc.py first" if keep else None)


def test_saddle_up_passes_its_keep_reasoning_flag(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    got: list[bool] = []
    monkeypatch.setenv("SADDLE_VLLM_API_KEY", "k")
    monkeypatch.setattr(cli, "check_server", lambda *_a, **_k: None)
    # The start report's look at the capabilities asks nothing here.
    monkeypatch.setattr(conditions, "capability_rows", lambda *_a, **_k: [])

    def fake_chat(options: ChatOptions, *_a: Any, **_k: Any) -> int:
        got.append(options.keep_reasoning)
        return 0

    monkeypatch.setattr(cli, "run_chat", fake_chat)
    for extra in ([], ["--no-keep-reasoning"]):
        cli.main(["up", "--journal", str(tmp_path / "j.jsonl"), *extra])
    assert got == [True, False]


def test_run_turn_appends_plain_reply(tmp_path: Path) -> None:
    journal = tmp_path / "chat.jsonl"
    messages: list[dict[str, Any]] = []
    out = io.StringIO()
    display = _display(out)

    with display.live_turn():
        new_parent = _run_turn(
            _sse_client(_chunk({"content": "Hi!"}) + "data: [DONE]\n\n"),
            messages,
            "hello",
            ChatOptions(journal=journal),
            turn=1,
            parent=None,
            display=display,
        )

    assert out.getvalue() == "saddle> \n" + "Hi!" + " " * 77 + "\n"
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
    out = io.StringIO()
    display = _display(out)

    with display.live_turn():
        new_parent = _run_turn(
            _scripted_client([tool_body, final_body], seen),
            messages,
            "read it",
            ChatOptions(workdir=tmp_path, journal=journal),
            turn=2,
            parent=parent,
            display=display,
        )

    assert out.getvalue() == (
        "thinking: R1. \n"
        + "saddle> \n"
        + "Running."
        + " " * 72
        + "\n"
        + '$ read_file {"path": "note.txt"}\n'
        + "hello\n"
        + "\n"
        + "thinking: R2. \n"
        + "saddle> \n"
        + "Seen."
        + " " * 75
        + "\n"
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
            # every mode keeps each round's reasoning, under both spellings
            "reasoning_content": "R1. ",
            "reasoning": "R1. ",
        },
        {"role": "tool", "tool_call_id": "c1", "content": "hello\n"},
        {"role": "assistant", "content": "Seen.", "reasoning_content": "R2. ", "reasoning": "R2. "},
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


def test_run_turn_tool_results_carry_exit_colors(tmp_path: Path) -> None:
    (tmp_path / "note.txt").write_text("hello\n")
    tool_body = (
        _chunk({"content": "Go. "})
        + _chunk(
            {
                "tool_calls": [
                    {"id": "c1", "index": 0, "function": {"name": "read_file"}},
                    {"id": "c2", "index": 1, "function": {"name": "read_file"}},
                ]
            }
        )
        + _chunk({"tool_calls": [{"index": 0, "function": {"arguments": '{"path": "note.txt"}'}}]})
        + _chunk({"tool_calls": [{"index": 1, "function": {"arguments": '{"path": "x"}'}}]})
        + "data: [DONE]\n\n"
    )
    final_body = _chunk({"content": "Done."}) + "data: [DONE]\n\n"
    out = io.StringIO()
    # Colour on, set here: rich would read `NO_COLOR` from the environment,
    # and saddle's own command environment sets it.
    console = Console(
        file=out, width=80, force_terminal=True, color_system="truecolor", no_color=False
    )
    display = Timeline(console)
    journal = tmp_path / "chat.jsonl"

    new_parent = _run_turn(
        _scripted_client([tool_body, final_body]),
        [],
        "read both",
        ChatOptions(workdir=tmp_path, journal=journal),
        turn=1,
        parent=None,
        display=display,
    )

    console.print(display._render())
    captured = out.getvalue()
    assert "\x1b[32mhello\x1b[0m\n" in captured
    assert "\x1b[31merror: cannot read 'x'\x1b[0m" in captured
    assert "\x1b[35m$ read_file" in captured
    assert new_parent == read_records(journal)[0].record_hash


class _Terminal(io.StringIO):
    """Output a console takes for a terminal, as `saddle up`'s stdout is."""

    def isatty(self) -> bool:
        return True


@pytest.mark.parametrize(("no_color", "coloured"), [(None, True), ("", True), ("1", False)])
def test_saddle_up_colours_its_display_unless_no_color_is_set(
    monkeypatch: pytest.MonkeyPatch, no_color: str | None, coloured: bool
) -> None:
    """The product honours `NO_COLOR` (a set, non-empty value): the console
    `saddle up` builds (`cli.chat_console`) on a colour terminal, the
    environment set here."""
    for name in ("NO_COLOR", "FORCE_COLOR", "TTY_COMPATIBLE", "TTY_INTERACTIVE"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("TERM", "xterm-256color")
    monkeypatch.setenv("COLORTERM", "truecolor")
    monkeypatch.setenv("COLUMNS", "80")
    if no_color is not None:
        monkeypatch.setenv("NO_COLOR", no_color)
    out = _Terminal()
    Timeline(cli.chat_console(out)).show_error("boom")
    assert "error: boom" in out.getvalue()
    assert ("\x1b[31m" in out.getvalue()) is coloured


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
    out = io.StringIO()
    journal = tmp_path / "chat.jsonl"
    parent = _seal_turn(
        journal,
        turn=1,
        prompt="first",
        rounds=[{"reply": "R.", "tools": []}],
        reasoning="",
        parent=None,
    )
    display = _display(out)

    with display.live_turn():
        new_parent = _run_turn(
            _sse_client(tool_body, seen),
            [],
            "go",
            ChatOptions(workdir=tmp_path, journal=journal),
            turn=2,
            parent=parent,
            display=display,
        )

    assert len(seen) == 10
    captured = out.getvalue()
    assert captured.count("$ read_file") == 10
    assert captured.endswith("error: gave up after 10 tool rounds\n")
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
    out = io.StringIO()
    journal = tmp_path / "chat.jsonl"
    prompt = RULE + "you> "
    reply = "saddle> \n" + "Hi!" + " " * 77 + "\n"

    code = run_chat(
        ChatOptions(journal=journal),
        _sse_client(_chunk({"content": "Hi!"}) + "data: [DONE]\n\n", seen),
        stdin=io.StringIO("one\ntwo\n/quit\n"),
        console=Console(file=out, width=80),
    )

    assert code == 0
    assert out.getvalue() == (
        prompt + "you> one\n" + RULE + reply + prompt + "you> two\n" + RULE + reply + prompt
    )
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


def test_run_chat_tty_stdin_skips_recap(tmp_path: Path) -> None:
    class _TtyStdin(io.StringIO):
        def isatty(self) -> bool:
            return True

    out = io.StringIO()
    journal = tmp_path / "chat.jsonl"
    prompt = RULE + "you> "

    code = run_chat(
        ChatOptions(journal=journal),
        _sse_client(_chunk({"content": "Hi!"}) + "data: [DONE]\n\n"),
        stdin=_TtyStdin("one\n/quit\n"),
        console=Console(file=out, width=80),
    )

    assert code == 0
    assert out.getvalue() == (prompt + RULE + "saddle> \n" + "Hi!" + " " * 77 + "\n" + prompt)
    assert len(read_records(journal)) == 1


def test_run_chat_eof_exits(tmp_path: Path) -> None:
    seen: list[httpx.Request] = []
    out = io.StringIO()
    journal = tmp_path / "chat.jsonl"

    code = run_chat(
        ChatOptions(journal=journal),
        _sse_client("data: [DONE]\n\n", seen),
        stdin=io.StringIO(""),
        console=Console(file=out, width=80),
    )

    assert code == 0
    assert out.getvalue() == RULE + "you> "
    assert seen == []
    assert not journal.exists()


def test_run_chat_skips_blank_lines(tmp_path: Path) -> None:
    seen: list[httpx.Request] = []
    out = io.StringIO()
    journal = tmp_path / "chat.jsonl"

    code = run_chat(
        ChatOptions(journal=journal),
        _sse_client("data: [DONE]\n\n", seen),
        stdin=io.StringIO("\n  \n/quit\n"),
        console=Console(file=out, width=80),
    )

    assert code == 0
    assert out.getvalue() == (RULE + "you> ") * 3
    assert seen == []
    assert not journal.exists()


def test_run_chat_server_error_keeps_session(tmp_path: Path) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        msg = "refused"
        raise httpx.ConnectError(msg)

    client = VllmClient(api_key="k", transport=httpx.MockTransport(handler))
    out = io.StringIO()
    journal = tmp_path / "chat.jsonl"
    prompt = RULE + "you> "

    code = run_chat(
        ChatOptions(journal=journal),
        client,
        stdin=io.StringIO("hi\n/quit\n"),
        console=Console(file=out, width=80),
    )

    assert code == 0
    assert out.getvalue() == (
        prompt + "you> hi\n" + RULE + "error: request failed: refused\n" + prompt
    )
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
    out = io.StringIO()
    journal = tmp_path / "chat.jsonl"
    prompt = RULE + "you> "

    code = run_chat(
        ChatOptions(journal=journal),
        client,
        stdin=io.StringIO("one\ntwo\n/quit\n"),
        console=Console(file=out, width=80),
    )

    reply = "saddle> \n" + "Hi!" + " " * 77 + "\n"
    assert code == 0
    assert out.getvalue() == (
        prompt
        + "you> one\n"
        + RULE
        + "error: server returned HTTP 500: boom\n"
        + prompt
        + "you> two\n"
        + RULE
        + reply
        + prompt
    )
    (record,) = read_records(journal)
    assert record.evidence_id == "chat#2"
    assert record.parent_proofs == []
    assert verify_journal(journal) == []


def test_run_chat_keyboard_interrupt_returns_130(tmp_path: Path) -> None:
    class _InterruptingStdin(io.StringIO):
        def readline(self, size: int = -1) -> str:  # type: ignore[override]
            raise KeyboardInterrupt

    out = io.StringIO()
    journal = tmp_path / "chat.jsonl"

    code = run_chat(
        ChatOptions(journal=journal),
        _sse_client("data: [DONE]\n\n"),
        stdin=_InterruptingStdin("hi\n"),
        console=Console(file=out, width=80),
    )

    assert code == 130
    assert out.getvalue() == RULE + "you> \n"
    assert not journal.exists()


def test_run_chat_terminal_survives_across_turns(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A background terminal started in turn 1 is still reachable in turn 2.

    `run_chat` must run every tool call in one session against the same
    `ToolContext`, so the `Sandbox` (and its `terminals` dict) started by
    `run_command(background=true)` in an earlier turn is the same one a
    later turn's `wait_for_terminal` looks in. A context built fresh per
    call, or even fresh per turn, loses the terminal: this test crosses two
    turns precisely so a per-turn context (which would pass a single-turn
    version of this test) still fails it.
    """
    monkeypatch.setattr("saddle.sandbox.uuid.uuid4", lambda: uuid.UUID("1234abcd" + "0" * 24))
    journal = tmp_path / "chat.jsonl"
    start_body = (
        _chunk({"content": "Starting. "})
        + _chunk({"tool_calls": [{"id": "c1", "index": 0, "function": {"name": "run_command"}}]})
        + _chunk(
            {
                "tool_calls": [
                    {
                        "index": 0,
                        "function": {"arguments": '{"command": "echo ready", "background": true}'},
                    }
                ]
            }
        )
        + "data: [DONE]\n\n"
    )
    started_body = _chunk({"content": "Started."}) + "data: [DONE]\n\n"
    wait_body = (
        _chunk({"content": "Waiting. "})
        + _chunk(
            {"tool_calls": [{"id": "c2", "index": 0, "function": {"name": "wait_for_terminal"}}]}
        )
        + _chunk(
            {
                "tool_calls": [
                    {"index": 0, "function": {"arguments": '{"id": "1234abcd", "timeout": 10}'}}
                ]
            }
        )
        + "data: [DONE]\n\n"
    )
    done_body = _chunk({"content": "Done."}) + "data: [DONE]\n\n"
    seen: list[httpx.Request] = []

    code = run_chat(
        ChatOptions(workdir=tmp_path, journal=journal, mode="edit"),
        _scripted_client([start_body, started_body, wait_body, done_body], seen),
        stdin=io.StringIO("start it\nwait for it\n/quit\n"),
        console=Console(file=io.StringIO(), width=80),
    )

    assert code == 0
    assert len(seen) == 4
    spans = read_spans(journal)
    wait_span = next(span for span in spans if span.argv[0] == "wait_for_terminal")
    assert wait_span.detail.startswith("exit 0")
    assert "ready" in wait_span.detail


def test_run_turn_without_a_context_shares_one_across_its_tool_rounds(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`_run_turn(context=None)` builds one `ToolContext` for the whole turn.

    A background terminal started in round 1 is reachable by
    `wait_for_terminal` in round 2 of the same turn. A context built inside
    the round loop would give round 2 a fresh `Sandbox` with no terminals,
    and the wait would fail instead of reporting `exit 0`. Calls `_run_turn`
    directly, so no `run_chat` session context can mask the per-round build.
    """
    monkeypatch.setattr("saddle.sandbox.uuid.uuid4", lambda: uuid.UUID("1234abcd" + "0" * 24))
    journal = tmp_path / "chat.jsonl"
    start_body = (
        _chunk({"content": "Starting. "})
        + _chunk({"tool_calls": [{"id": "c1", "index": 0, "function": {"name": "run_command"}}]})
        + _chunk(
            {
                "tool_calls": [
                    {
                        "index": 0,
                        "function": {"arguments": '{"command": "echo ready", "background": true}'},
                    }
                ]
            }
        )
        + "data: [DONE]\n\n"
    )
    wait_body = (
        _chunk({"content": "Waiting. "})
        + _chunk(
            {"tool_calls": [{"id": "c2", "index": 0, "function": {"name": "wait_for_terminal"}}]}
        )
        + _chunk(
            {
                "tool_calls": [
                    {"index": 0, "function": {"arguments": '{"id": "1234abcd", "timeout": 10}'}}
                ]
            }
        )
        + "data: [DONE]\n\n"
    )
    done_body = _chunk({"content": "Done."}) + "data: [DONE]\n\n"
    seen: list[httpx.Request] = []
    display = _display(io.StringIO())

    with display.live_turn():
        _run_turn(
            _scripted_client([start_body, wait_body, done_body], seen),
            [],
            "start it and wait for it",
            ChatOptions(workdir=tmp_path, journal=journal, mode="edit"),
            turn=1,
            parent=None,
            display=display,
        )

    assert len(seen) == 3
    wait_span = next(span for span in read_spans(journal) if span.argv[0] == "wait_for_terminal")
    assert wait_span.detail.startswith("exit 0")
    assert "ready" in wait_span.detail


def test_run_chat_sessions_do_not_share_terminals(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Two `run_chat` sessions never see each other's background terminals."""
    monkeypatch.setattr("saddle.sandbox.uuid.uuid4", lambda: uuid.UUID("1234abcd" + "0" * 24))
    start_body = (
        _chunk({"content": "Starting. "})
        + _chunk({"tool_calls": [{"id": "c1", "index": 0, "function": {"name": "run_command"}}]})
        + _chunk(
            {
                "tool_calls": [
                    {
                        "index": 0,
                        "function": {"arguments": '{"command": "echo ready", "background": true}'},
                    }
                ]
            }
        )
        + "data: [DONE]\n\n"
    )
    started_body = _chunk({"content": "Started."}) + "data: [DONE]\n\n"
    journal_a = tmp_path / "a.jsonl"

    code_a = run_chat(
        ChatOptions(workdir=tmp_path, journal=journal_a, mode="edit"),
        _scripted_client([start_body, started_body]),
        stdin=io.StringIO("start it\n/quit\n"),
        console=Console(file=io.StringIO(), width=80),
    )
    assert code_a == 0

    wait_body = (
        _chunk({"content": "Waiting. "})
        + _chunk(
            {"tool_calls": [{"id": "c2", "index": 0, "function": {"name": "wait_for_terminal"}}]}
        )
        + _chunk(
            {
                "tool_calls": [
                    {"index": 0, "function": {"arguments": '{"id": "1234abcd", "timeout": 10}'}}
                ]
            }
        )
        + "data: [DONE]\n\n"
    )
    done_body = _chunk({"content": "Done."}) + "data: [DONE]\n\n"
    journal_b = tmp_path / "b.jsonl"

    code_b = run_chat(
        ChatOptions(workdir=tmp_path, journal=journal_b, mode="edit"),
        _scripted_client([wait_body, done_body]),
        stdin=io.StringIO("wait for it\n/quit\n"),
        console=Console(file=io.StringIO(), width=80),
    )
    assert code_b == 0

    (wait_span,) = read_spans(journal_b)
    assert wait_span.argv[0] == "wait_for_terminal"
    assert wait_span.detail.startswith("error: no terminal")


@needs_live
def test_live_chat_session_streams_reasoning_and_tool_calls(tmp_path: Path) -> None:
    assert _LIVE_KEY is not None
    (tmp_path / "note.txt").write_text("The launch code is 7-7-0.\n")
    stdin = io.StringIO("Read note.txt and quote the launch code back to me.\n/quit\n")
    out = io.StringIO()
    journal = tmp_path / "chat.jsonl"
    options = ChatOptions(
        workdir=tmp_path, journal=journal, max_tokens=1024, reasoning_effort="low"
    )

    with VllmClient(api_key=_LIVE_KEY) as client:
        code = run_chat(options, client, stdin=stdin, console=Console(file=out, width=80))

    captured = out.getvalue()
    assert code == 0
    assert "thinking: " in captured
    assert "$ read_file" in captured
    assert "7-7-0" in captured
    assert verify_journal(journal) == []
    assert len(read_records(journal)) == 1


def test_an_empty_terminal_reply_is_named_to_the_model_and_the_turn_goes_on(
    tmp_path: Path,
) -> None:
    """F35 parity with the web chat: a reply with no text and no tool call (the
    model wrote its call inside its reasoning) ended the terminal turn silently.
    Known-bad: the turn ends there. Known-good: the model gets the web chat's
    nudge, and its next reply is the turn's answer, with no error shown."""
    seen: list[httpx.Request] = []
    messages: list[dict[str, Any]] = []
    out = io.StringIO()
    display = _display(out)
    bodies = [
        _chunk({"reasoning_content": "next I will run objdump"}) + "data: [DONE]\n\n",
        _chunk({"content": "done"}) + "data: [DONE]\n\n",
    ]
    with display.live_turn():
        _run_turn(
            _scripted_client(bodies, seen),
            messages,
            "go",
            ChatOptions(journal=tmp_path / "chat.jsonl"),
            turn=1,
            parent=None,
            display=display,
        )
    assert len(seen) == 2
    asked = json.loads(seen[1].content)["messages"]
    assert asked[-1] == {"role": "user", "content": engine.EMPTY_REPLY_NUDGE}
    assert messages[-1] == {"role": "assistant", "content": "done"}
    assert "error:" not in out.getvalue()


def test_empty_terminal_replies_past_the_retries_end_the_turn_saying_so(
    tmp_path: Path,
) -> None:
    seen: list[httpx.Request] = []
    out = io.StringIO()
    display = _display(out)
    with display.live_turn():
        _run_turn(
            _sse_client("data: [DONE]\n\n", seen),
            [],
            "go",
            ChatOptions(journal=tmp_path / "chat.jsonl"),
            turn=1,
            parent=None,
            display=display,
        )
    assert len(seen) == engine.EMPTY_REPLY_RETRIES + 1
    message = engine.EMPTY_REPLIES.format(n=engine.EMPTY_REPLY_RETRIES + 1)
    assert out.getvalue().endswith(f"error: {message}\n")
    assert out.getvalue().count("error:") == 1
