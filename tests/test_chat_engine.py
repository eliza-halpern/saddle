"""The turn engine: what run_turn yields, and when it stops.

The engine owns no formatting -- it yields typed events and the renderers
agree by construction -- so what is worth pinning is the *sequence*: that a
tool round is announced before it is run and labelled after, that a stop
request is honoured at every point it is checked, and that a turn is sealed
even when it ended badly. A turn that is not sealed is work with no record.
"""

from __future__ import annotations

import base64
import json
from collections.abc import Iterable
from pathlib import Path
from typing import Any, cast

import pytest

from saddle.engine import MAX_TOOL_ROUNDS, TurnOptions, run_turn
from saddle.events import (
    Compaction,
    ContentDelta,
    Context,
    ErrorEvent,
    Event,
    ToolEnd,
    ToolStart,
    TurnEnd,
    TurnStart,
)
from saddle.vllm import StreamToken, ToolCall, VllmClient, VllmRequestError


class FakeClient:
    """Replays scripted rounds; records what each round was asked for."""

    def __init__(self, rounds: list[list[Any] | BaseException]) -> None:
        self.rounds = list(rounds)
        self.asked: list[dict[str, Any]] = []

    def stream_chat(self, messages: Any, **kwargs: Any) -> Any:
        self.asked.append({"messages": [dict(m) for m in messages], **kwargs})
        if not self.rounds:
            return iter(())
        step = self.rounds.pop(0)
        if isinstance(step, BaseException):
            raise step
        return iter(step)


def reasoning(text: str) -> StreamToken:
    return StreamToken(stream="reasoning", text=text)


def content(text: str) -> StreamToken:
    return StreamToken(stream="content", text=text)


def tool(name: str, call_id: str = "c1", **arguments: Any) -> ToolCall:
    return ToolCall(id=call_id, name=name, arguments=json.dumps(arguments))


@pytest.fixture
def options(tmp_path: Path) -> TurnOptions:
    return TurnOptions(workdir=tmp_path, journal=tmp_path / "journal.jsonl")


def kinds(events: list[Event]) -> list[str]:
    return [e.kind for e in events]


def one[E: Event](event: Event, cls: type[E]) -> E:
    """Narrow a single event, asserting that is what it is.

    The stream is heterogeneous, so an index into it is an `Event`, and
    reading a subclass field off that is what `strict` rejects. Narrowing
    here also turns "this position holds a ToolEnd" from an assumption the
    test makes silently into one it states.
    """
    assert isinstance(event, cls), f"expected {cls.__name__}, got {type(event).__name__}"
    return event


def those[E: Event](events: Iterable[Event], cls: type[E]) -> list[E]:
    """Every event of one type, in order."""
    return [event for event in events if isinstance(event, cls)]


def run(
    client: FakeClient,
    options: TurnOptions,
    text: str = "hi",
    messages: list[dict[str, Any]] | None = None,
    **kwargs: Any,
) -> list[Event]:
    return list(
        run_turn(
            cast(VllmClient, client),
            messages if messages is not None else [],
            text,
            options,
            turn=1,
            **kwargs,
        )
    )


# -- the shape of a turn ------------------------------------------------------


def test_a_plain_answer_is_start_reasoning_content_context_end(options: TurnOptions) -> None:
    client = FakeClient([[reasoning("thinking"), content("hello")]])
    events = run(client, options)
    assert kinds(events) == [
        "turn.start",
        "reasoning.delta",
        "content.delta",
        "context",
        "turn.end",
    ]
    assert one(events[0], TurnStart).prompt == "hi"
    assert one(events[-1], TurnEnd).proof  # every turn is sealed


def test_a_tool_round_is_announced_before_it_runs_and_labelled_after(options: TurnOptions) -> None:
    (options.workdir / "note.txt").write_text("contents")
    client = FakeClient(
        [
            [tool("read_file", path="note.txt")],
            [content("it says contents")],
        ]
    )
    events = run(client, options)
    assert kinds(events) == [
        "turn.start",
        "tool.start",
        "tool.end",
        "content.delta",
        "context",
        "turn.end",
    ]
    start, end = one(events[1], ToolStart), one(events[2], ToolEnd)
    assert start.present == "Reading note.txt"
    assert end.label == "Read note.txt"
    assert end.ok is True
    assert "contents" in end.detail
    assert end.duration_ms >= 0


def test_a_failing_tool_is_reported_in_the_failed_tense_not_hidden(options: TurnOptions) -> None:
    client = FakeClient(
        [
            [tool("read_file", path="absent.txt")],
            [content("that file is not there")],
        ]
    )
    events = run(client, options)
    end = those(events, ToolEnd)[0]
    assert end.ok is False
    assert end.label == "Failed to read absent.txt"
    assert end.detail.startswith("error: ")


def test_the_conversation_is_mutated_in_place_so_the_caller_keeps_it(options: TurnOptions) -> None:
    messages: list[dict[str, Any]] = []
    client = FakeClient([[content("hello")]])
    run(client, options, messages=messages)
    assert [m["role"] for m in messages] == ["user", "assistant"]
    assert messages[-1]["content"] == "hello"


def test_a_system_prompt_is_inserted_once_not_once_per_turn(options: TurnOptions) -> None:
    options.system_prompt = "be terse"
    messages: list[dict[str, Any]] = []
    run(FakeClient([[content("a")]]), options, messages=messages)
    run(FakeClient([[content("b")]]), options, messages=messages)
    assert [m["role"] for m in messages].count("system") == 1
    assert messages[0]["content"] == "be terse"


def test_a_tool_result_is_appended_as_a_tool_message_the_model_can_read(
    options: TurnOptions,
) -> None:
    (options.workdir / "note.txt").write_text("contents")
    messages: list[dict[str, Any]] = []
    client = FakeClient(
        [
            [tool("read_file", call_id="abc", path="note.txt")],
            [content("done")],
        ]
    )
    run(client, options, messages=messages)
    assistant = next(m for m in messages if m.get("tool_calls"))
    assert assistant["tool_calls"][0]["id"] == "abc"
    assert assistant["tool_calls"][0]["function"]["name"] == "read_file"
    result = next(m for m in messages if m["role"] == "tool")
    assert result["tool_call_id"] == "abc"
    assert "contents" in result["content"]


def test_several_calls_in_one_round_all_run(options: TurnOptions) -> None:
    (options.workdir / "a.txt").write_text("A")
    (options.workdir / "b.txt").write_text("B")
    client = FakeClient(
        [
            [
                tool("read_file", call_id="1", path="a.txt"),
                tool("read_file", call_id="2", path="b.txt"),
            ],
            [content("both read")],
        ]
    )
    events = run(client, options)
    ends = those(events, ToolEnd)
    assert [e.id for e in ends] == ["1", "2"]


# -- limits -------------------------------------------------------------------


def test_a_tool_loop_is_cut_off_rather_than_run_forever(options: TurnOptions) -> None:
    (options.workdir / "note.txt").write_text("x")
    client = FakeClient([[tool("read_file", path="note.txt")]] * (MAX_TOOL_ROUNDS + 4))
    events = run(client, options)
    error = those(events, ErrorEvent)[0]
    assert error.message == f"stopped after {MAX_TOOL_ROUNDS} tool rounds"
    assert len([e for e in events if e.kind == "tool.start"]) == MAX_TOOL_ROUNDS
    assert events[-1].kind == "turn.end"  # still sealed


def test_a_transport_failure_is_reported_and_the_turn_is_still_sealed(options: TurnOptions) -> None:
    client = FakeClient([VllmRequestError("the server hung up")])
    events = run(client, options)
    assert kinds(events) == ["turn.start", "error", "context", "turn.end"]
    assert one(events[1], ErrorEvent).message == "the server hung up"
    assert one(events[-1], TurnEnd).proof


def test_the_budget_is_what_the_client_is_asked_for(options: TurnOptions) -> None:
    options.context_tokens = 100_000
    client = FakeClient([[content("hi")]])
    run(client, options)
    asked = client.asked[0]
    assert asked["max_tokens"] == options.budget(asked["messages"])
    assert asked["reasoning_effort"] == options.reasoning_effort
    assert asked["temperature"] == options.temperature


def test_the_context_event_reports_the_conversation_against_its_window(
    options: TurnOptions,
) -> None:
    options.context_tokens = 50_000
    events = run(FakeClient([[content("hello")]]), options)
    context = those(events, Context)[0]
    assert context.limit == 50_000
    assert 0 < context.used < 50_000


def test_an_overlong_conversation_is_compacted_and_says_so(options: TurnOptions) -> None:
    options.context_tokens = 2_000
    messages = [{"role": "user", "content": "x" * 4_000} for _ in range(20)]
    events = run(FakeClient([[content("ok")]]), options, messages=messages)
    compaction = those(events, Compaction)[0]
    assert compaction.dropped_messages > 0
    # 20 sent in, plus this turn's own user message, less what was dropped,
    # plus the one in-band note that says so. Counted at compaction time: the
    # assistant's reply is appended after.
    assert compaction.kept_messages == 21 - compaction.dropped_messages + 1
    assert compaction.summary.startswith(f"{compaction.dropped_messages} earlier message")


def test_compaction_leaves_a_note_in_the_conversation_the_model_will_read(
    options: TurnOptions,
) -> None:
    options.context_tokens = 2_000
    messages = [{"role": "user", "content": "x" * 4_000} for _ in range(20)]
    run(FakeClient([[content("ok")]]), options, messages=messages)
    note = next(m for m in messages if "compacted" in (m.get("content") or ""))
    assert "Ask the user if you need detail" in note["content"]


def test_a_conversation_that_fits_is_not_compacted(options: TurnOptions) -> None:
    events = run(FakeClient([[content("ok")]]), options)
    assert "compaction" not in kinds(events)


# -- stopping -----------------------------------------------------------------

# Stop is checked at three points, and each is a separate promise: mid-stream,
# between the calls of one round, and between rounds. A stop that is only
# honoured at one of them looks like a stop button that sometimes does
# nothing, so each gets its own test.


def test_a_stop_mid_stream_cuts_the_reply_short_and_still_seals(options: TurnOptions) -> None:
    stopped = {"now": False}
    client = FakeClient([[content("one"), content("two"), content("three")]])

    def cancel() -> bool:
        return stopped["now"]

    events = []
    for event in run_turn(cast(VllmClient, client), [], "hi", options, turn=1, cancel=cancel):
        events.append(event)
        if event.kind == "content.delta":
            stopped["now"] = True  # stop after the first chunk

    assert [e.text for e in those(events, ContentDelta)] == ["one"]
    assert any(e.message == "stopped by you" for e in those(events, ErrorEvent))
    assert events[-1].kind == "turn.end"
    assert one(events[-1], TurnEnd).proof  # the work done is still recorded


def test_a_stop_between_two_calls_runs_the_first_and_not_the_second(options: TurnOptions) -> None:
    (options.workdir / "a.txt").write_text("A")
    (options.workdir / "b.txt").write_text("B")
    stopped = {"now": False}
    client = FakeClient(
        [
            [
                tool("read_file", call_id="1", path="a.txt"),
                tool("read_file", call_id="2", path="b.txt"),
            ],
        ]
    )

    events = []
    for event in run_turn(
        cast(VllmClient, client), [], "hi", options, turn=1, cancel=lambda: stopped["now"]
    ):
        events.append(event)
        if event.kind == "tool.end":
            stopped["now"] = True

    assert [e.id for e in those(events, ToolStart)] == ["1"]
    assert events[-1].kind == "turn.end"


def test_a_stop_between_rounds_does_not_start_another_one(options: TurnOptions) -> None:
    (options.workdir / "a.txt").write_text("A")
    stopped = {"now": False}
    client = FakeClient(
        [
            [tool("read_file", path="a.txt")],
            [content("a second round that must not happen")],
        ]
    )

    events = []
    for event in run_turn(
        cast(VllmClient, client), [], "hi", options, turn=1, cancel=lambda: stopped["now"]
    ):
        events.append(event)
        if event.kind == "tool.end":
            stopped["now"] = True

    assert "content.delta" not in kinds(events)
    assert len(client.asked) == 1  # the second round was never asked for


def test_a_turn_that_was_not_stopped_says_nothing_about_stopping(options: TurnOptions) -> None:
    events = run(FakeClient([[content("ok")]]), options, cancel=lambda: False)
    assert not any(e.kind == "error" for e in events)


# -- images -------------------------------------------------------------------


def test_an_uploaded_image_is_sent_as_content_parts_not_as_a_filename(options: TurnOptions) -> None:
    shot = options.workdir / "shot.png"
    shot.write_bytes(b"\x89PNG\r\n\x1a\n")
    client = FakeClient([[content("I see it")]])
    messages: list[dict[str, Any]] = []
    run(client, options, text="what is this", messages=messages, images=[shot])

    parts = messages[0]["content"]
    assert [p["type"] for p in parts] == ["text", "image_url"]
    assert parts[0]["text"] == "what is this"
    url = parts[1]["image_url"]["url"]
    assert url.startswith("data:image/png;base64,")
    assert base64.b64decode(url.split(",", 1)[1]) == b"\x89PNG\r\n\x1a\n"


def test_a_text_only_turn_stays_a_plain_string(options: TurnOptions) -> None:
    messages: list[dict[str, Any]] = []
    run(FakeClient([[content("ok")]]), options, messages=messages)
    assert messages[0]["content"] == "hi"


def test_an_unreadable_image_is_skipped_rather_than_failing_the_turn(options: TurnOptions) -> None:
    # The user still asked a question; losing the whole turn over one
    # attachment would be the worse failure.
    missing = options.workdir / "gone.png"
    good = options.workdir / "here.jpg"
    good.write_bytes(b"\xff\xd8\xff")
    messages: list[dict[str, Any]] = []
    events = run(FakeClient([[content("ok")]]), options, messages=messages, images=[missing, good])

    parts = messages[0]["content"]
    assert [p["type"] for p in parts] == ["text", "image_url"]
    assert parts[1]["image_url"]["url"].startswith("data:image/jpeg;base64,")
    assert events[-1].kind == "turn.end"


def test_an_unknown_extension_is_still_offered_as_an_image(options: TurnOptions) -> None:
    odd = options.workdir / "capture.weird"
    odd.write_bytes(b"bytes")
    messages: list[dict[str, Any]] = []
    run(FakeClient([[content("ok")]]), options, messages=messages, images=[odd])
    assert messages[0]["content"][1]["image_url"]["url"].startswith("data:image/png;base64,")


# -- the journal --------------------------------------------------------------


def test_a_turn_is_journalled_with_its_reasoning_and_chains_to_its_parent(
    options: TurnOptions,
) -> None:
    client = FakeClient([[reasoning("deliberating"), content("answer")]])
    events = run(client, options, parent="proof-of-the-previous-turn")
    sealed = one(events[-1], TurnEnd)

    records = [json.loads(line) for line in options.journal.read_text().splitlines()]
    record = next(r for r in records if r.get("record_hash") == sealed.proof)
    assert record["node_id"] == "chat#1"
    assert record["thinking"] == "deliberating"
    assert record["parent_proofs"] == ["proof-of-the-previous-turn"]

    # The turn's content is hashed into the chain, not stored in it, so the
    # check is that the hash commits to what actually happened: the same
    # prompt and rounds reproduce it, a different prompt does not.
    from saddle.journal import build_record

    def sealed_hash(prompt: str) -> str:
        return build_record(
            evidence_id="chat#1",
            node_id="chat#1",
            diff=json.dumps({"prompt": prompt, "rounds": [{"reply": "answer", "tools": []}]}),
            parent_proofs=["proof-of-the-previous-turn"],
            gate_outputs=[],
            requirement_ids=[],
            thinking="deliberating",
        ).diff_hash

    assert record["diff_hash"] == sealed_hash("hi")
    assert record["diff_hash"] != sealed_hash("a different question")


def test_a_first_turn_has_no_parent_rather_than_a_null_one(options: TurnOptions) -> None:
    events = run(FakeClient([[content("answer")]]), options)
    records = [json.loads(line) for line in options.journal.read_text().splitlines()]
    record = next(r for r in records if r.get("record_hash") == one(events[-1], TurnEnd).proof)
    assert record["parent_proofs"] == []


def test_each_tool_call_leaves_a_span_with_its_outcome(options: TurnOptions) -> None:
    (options.workdir / "a.txt").write_text("A")
    client = FakeClient(
        [
            [
                tool("read_file", call_id="ok", path="a.txt"),
                tool("read_file", call_id="bad", path="absent.txt"),
            ],
            [content("done")],
        ]
    )
    run(client, options)
    lines = [json.loads(line) for line in options.journal.read_text().splitlines()]
    spans = [line for line in lines if "argv" in line]
    assert [s["exit_code"] for s in spans] == [0, 1]
    assert all(s["node_id"] == "chat#1" for s in spans)
