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
from dataclasses import replace
from pathlib import Path
from typing import Any, cast

import pytest

from saddle import engine
from saddle.engine import (
    INPUT_SAFETY,
    MIN_OUTPUT,
    OUTPUT_MARGIN,
    REPLY_ROOM,
    TurnOptions,
    _user_message,
    run_turn,
)
from saddle.events import (
    Compaction,
    ContentDelta,
    Context,
    ErrorEvent,
    Event,
    MessageDelivered,
    ToolEnd,
    ToolStart,
    TurnEnd,
    TurnStart,
)
from saddle.memory import estimate_tokens
from saddle.tools import ToolContext
from saddle.undo import UndoLog
from saddle.vision import images_message
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
    assert messages[0]["content"].startswith("be terse")


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


LONG_TASK_ROUNDS = 60
"""More tool rounds than the old caps (10, then 24) allowed: a setup task's
turn in a watched trial needed far more than 24 (the same ask took another
agent about 121 calls)."""


def test_an_empty_reply_is_named_to_the_model_and_the_turn_goes_on(options: TurnOptions) -> None:
    """F35: a reply with reasoning but no text and no tool call (the model wrote
    its call inside its reasoning) ended a live setup turn silently. Known-bad:
    the turn ends there. Known-good: the model is told, and its next reply is
    the turn's answer."""
    client = FakeClient([[reasoning("next I will run objdump")], [content("done")]])
    events = run(client, options)
    assert len(client.asked) == 2
    assert client.asked[1]["messages"][-1] == {"role": "user", "content": engine.EMPTY_REPLY_NUDGE}
    assert "".join(e.text for e in events if isinstance(e, ContentDelta)) == "done"


def test_empty_replies_past_the_retries_end_the_turn_saying_so(options: TurnOptions) -> None:
    client = FakeClient([[], [], [], [content("never asked")]])
    events = run(client, options)
    assert len(client.asked) == engine.EMPTY_REPLY_RETRIES + 1
    errors = [e.message for e in events if isinstance(e, ErrorEvent)]
    assert errors == [engine.EMPTY_REPLIES.format(n=engine.EMPTY_REPLY_RETRIES + 1)]


F39_REPLY = (
    "Research came back empty on the dgVoodoo-under-Wine question, so I'll "
    "determine it empirically from the DLL imports."
)
ANNOUNCED = [
    F39_REPLY,
    "Let me check the log.",
    "Now I'll write the config.",
    "The build failed.\n\nNext, I check the import table.",
    "Found it \u2014 I\u2019m going to patch the loader",
]
NOT_ANNOUNCED = [
    "The game is installed and runs.",
    "Which renderer do you want: A or B?",
    "I'll need your answer before continuing — which one?",
    "Let me explain what changed. I'll keep it short.\n\n"
    "The config now points at the new renderer.\n\nThe game starts and the menu draws.",
    "Let me know if you want changes.",
]


def announce_nudges(client: FakeClient) -> int:
    return sum(
        m == {"role": "user", "content": engine.ANNOUNCE_NUDGE}
        for m in client.asked[-1]["messages"]
    )


@pytest.mark.parametrize("reply", ANNOUNCED)
def test_a_reply_that_announces_an_action_and_stops_is_nudged(
    options: TurnOptions, reply: str
) -> None:
    """F39: a live chat turn ended on "so I'll determine it empirically ..."
    with no tool call; the person had to type "continue". Known-bad: the turn
    ends there. Known-good: the model is told, and its next reply follows."""
    client = FakeClient([[content(reply)], [content("done")]])
    events = run(client, options)
    assert len(client.asked) == 2
    assert client.asked[1]["messages"][-1] == {"role": "user", "content": engine.ANNOUNCE_NUDGE}
    assert client.asked[1]["messages"][-2] == {"role": "assistant", "content": reply}
    assert not [e for e in events if isinstance(e, ErrorEvent)]


@pytest.mark.parametrize("reply", NOT_ANNOUNCED)
def test_answers_questions_and_invitations_are_not_nudged(options: TurnOptions, reply: str) -> None:
    client = FakeClient([[content(reply)], [content("never asked")]])
    run(client, options)
    assert len(client.asked) == 1


@pytest.mark.parametrize("reply", ANNOUNCED)
def test_the_detector_reads_announcements(reply: str) -> None:
    assert engine.announces_action(reply)


@pytest.mark.parametrize("reply", [*NOT_ANNOUNCED, "", "   "])
def test_the_detector_passes_everything_else(reply: str) -> None:
    assert not engine.announces_action(reply)


def test_a_second_announcement_in_one_turn_ends_it(options: TurnOptions) -> None:
    client = FakeClient(
        [[content("Let me check the log.")], [content("Let me check it again.")], [content("x")]]
    )
    events = run(client, options)
    assert len(client.asked) == 2
    assert announce_nudges(client) == 1
    assert isinstance(events[-1], TurnEnd)
    assert not [e for e in events if isinstance(e, ErrorEvent)]


def test_the_announcement_nudge_comes_once_even_after_tool_rounds(options: TurnOptions) -> None:
    (options.workdir / "a.txt").write_text("a\n")
    client = FakeClient(
        [
            [content("Let me check the log.")],
            [tool("read_file", path="a.txt")],
            [content("Let me check it again.")],
            [content("never asked")],
        ]
    )
    run(client, options)
    assert len(client.asked) == 3
    assert announce_nudges(client) == 1


def test_an_autonomous_run_is_not_given_the_announcement_nudge(options: TurnOptions) -> None:
    run_state = engine.AutoRun(budget=engine.RunBudget(time_s=60, tokens=1000), run_span="s")
    auto_options = replace(options, auto=run_state)
    client = FakeClient([[content("Let me check the log.")], [content("ok")], [content("ok")]])
    run(client, auto_options)
    assert client.asked[1]["messages"][-1] == {"role": "user", "content": engine.AUTO_NUDGE}
    assert all(
        m.get("content") != engine.ANNOUNCE_NUDGE for a in client.asked for m in a["messages"]
    )


def waiting_nudges(client: FakeClient) -> list[str]:
    return [
        m["content"]
        for m in client.asked[-1]["messages"]
        if m.get("role") == "user"
        and str(m.get("content", "")).startswith(engine.WAITING_NUDGE_HEAD)
    ]


def _stop_terminals(ctx: ToolContext) -> None:
    for terminal in ctx.sandbox.terminals.values() if ctx.sandbox is not None else ():
        if terminal.process is not None and terminal.running:
            terminal.process.kill()


B8_REPLY = "Rendering (larger plane and denser leaves). Waiting for completion."


def test_a_turn_that_ends_while_its_own_background_command_runs_is_nudged(
    options: TurnOptions,
) -> None:
    """B8 (2026-10-04): the model started a render in the background, said
    "Waiting for completion." and made no call; the turn ended with no report
    and nothing waited for the render. The announcement guard needs "I'll" or
    "Let me", so it let this through. Known-bad: the turn ends there.
    Known-good: the model is told the command is still running, by id."""
    ctx = ToolContext(workdir=options.workdir)
    client = FakeClient(
        [
            [tool("run_command", command="sleep 30", background=True)],
            [content(B8_REPLY)],
            [content("done")],
        ]
    )
    try:
        events = run(client, options, context=ctx)
    finally:
        _stop_terminals(ctx)
    assert len(client.asked) == 3
    told = waiting_nudges(client)
    assert len(told) == 1
    (terminal_id,) = ctx.sandbox.terminals if ctx.sandbox is not None else [""]
    assert terminal_id in told[0]
    assert "sleep 30" in told[0]
    assert client.asked[2]["messages"][-2] == {"role": "assistant", "content": B8_REPLY}
    assert not [e for e in events if isinstance(e, ErrorEvent)]


def test_a_finished_background_command_does_not_nudge(options: TurnOptions) -> None:
    """Known-good: a background command that has exited is nothing to wait for."""
    ctx = ToolContext(workdir=options.workdir)
    client = FakeClient(
        [
            [tool("run_command", command="true", background=True)],
            [tool("run_command", call_id="c2", command="sleep 0.5")],
            [content("All done.")],
            [content("never asked")],
        ]
    )
    try:
        run(client, options, context=ctx)
    finally:
        _stop_terminals(ctx)
    assert len(client.asked) == 3
    assert waiting_nudges(client) == []


def test_the_waiting_nudge_comes_once_per_turn(options: TurnOptions) -> None:
    ctx = ToolContext(workdir=options.workdir)
    client = FakeClient(
        [
            [tool("run_command", command="sleep 30", background=True)],
            [content(B8_REPLY)],
            [content("Still waiting.")],
            [content("never asked")],
        ]
    )
    try:
        run(client, options, context=ctx)
    finally:
        _stop_terminals(ctx)
    assert len(client.asked) == 3
    assert len(waiting_nudges(client)) == 1


def test_a_command_left_running_by_an_earlier_turn_does_not_nudge(options: TurnOptions) -> None:
    """A server started on purpose last turn is not this turn's unfinished work."""
    ctx = ToolContext(workdir=options.workdir)
    first = FakeClient(
        [
            [tool("run_command", command="sleep 30", background=True)],
            [content(B8_REPLY)],
            [content("It is serving.")],
        ]
    )
    second = FakeClient(
        [[content("The server from before is still up.")], [content("never asked")]]
    )
    try:
        run(first, options, context=ctx)
        run(second, options, context=ctx)
    finally:
        _stop_terminals(ctx)
    assert len(second.asked) == 1


def test_a_long_tool_chain_runs_until_the_model_answers(options: TurnOptions) -> None:
    """Known-good: a turn needing many tool rounds is not cut off; it ends when
    the model answers, sealed, with no error."""
    (options.workdir / "note.txt").write_text("x")
    rounds: list[list[Any] | BaseException] = [
        [tool("read_file", path="note.txt")] for _ in range(LONG_TASK_ROUNDS)
    ]
    rounds.append([content("all set up")])
    client = FakeClient(rounds)
    events = run(client, options)
    assert those(events, ErrorEvent) == []
    assert len([e for e in events if e.kind == "tool.start"]) == LONG_TASK_ROUNDS
    assert len(client.asked) == LONG_TASK_ROUNDS + 1
    assert events[-1].kind == "turn.end"


def test_a_looping_turn_ends_when_the_person_stops_it(options: TurnOptions) -> None:
    """What the cap protected: a model that never stops calling tools. With no
    cap, the person's Stop ends it between rounds, and the turn is sealed."""
    (options.workdir / "note.txt").write_text("x")
    client = FakeClient([[tool("read_file", path="note.txt")]] * 200)
    stopped = {"now": False}
    events = []
    for event in run_turn(
        cast(VllmClient, client), [], "hi", options, turn=1, cancel=lambda: stopped["now"]
    ):
        events.append(event)
        if len([e for e in events if e.kind == "tool.end"]) == 30:
            stopped["now"] = True
    assert len(client.asked) == 30
    assert events[-1].kind == "turn.end"


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


def test_a_turn_survives_compacting_away_an_earlier_image_upload(
    options: TurnOptions, tmp_path: Path
) -> None:
    # compact() used to be called before run_turn's own try/except, and that
    # except only catches VllmError -- so an AttributeError popping a
    # list-content message (an uploaded image) killed the whole turn rather
    # than being compacted around.
    options.context_tokens = 2_000
    shot = tmp_path / "shot.png"
    shot.write_bytes(b"\x89PNG\r\n\x1a\n")
    messages: list[dict[str, Any]] = [_user_message("look at this screenshot", [shot])]
    for index in range(12):
        messages.append({"role": "assistant", "content": "a" * 4_000})
        messages.append({"role": "user", "content": f"question {index} " + "q" * 4_000})

    events = run(FakeClient([[content("ok")]]), options, messages=messages)

    assert those(events, Compaction)
    assert events[-1].kind == "turn.end"


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


# -- a retry answers the question already there -------------------------------


def _retry(
    client: FakeClient, options: TurnOptions, messages: list[dict[str, Any]]
) -> tuple[list[Event], list[dict[str, Any]]]:
    """Run a retry (`text=None`) with an undo log; return events and log records."""
    log = UndoLog(options.workdir / "undo")
    events = list(
        run_turn(
            cast(VllmClient, client),
            messages,
            None,
            options,
            turn=2,
            context=ToolContext(workdir=options.workdir, undo=log),
        )
    )
    records = [json.loads(line) for line in (log.root / "turns.jsonl").read_text().splitlines()]
    return events, records


def test_a_retry_of_an_image_question_records_its_text_and_where_it_sits(
    options: TurnOptions,
) -> None:
    # The question is the last *user* message, not the last message, and an
    # image upload stores it as parts: the turn's prompt is the text part,
    # and the undo log keys the turn to the question's own index.
    messages: list[dict[str, Any]] = [
        {"role": "system", "content": "be brief"},
        {
            "role": "user",
            "content": [
                {"type": "image_url", "image_url": {"url": "data:image/png;base64,AA=="}},
                {"type": "text", "text": "what is in this picture?"},
            ],
        },
        {"role": "assistant", "content": "a frog"},
    ]
    events, records = _retry(FakeClient([[content("a toad")]]), options, messages)
    assert one(events[0], TurnStart).prompt == "what is in this picture?"
    assert records[0] == {"kind": "turn", "turn": 2, "start_index": 1}


def test_a_retry_with_no_question_has_an_empty_prompt_and_no_index(
    options: TurnOptions,
) -> None:
    messages: list[dict[str, Any]] = [{"role": "system", "content": "be brief"}]
    events, records = _retry(FakeClient([[content("ok")]]), options, messages)
    assert one(events[0], TurnStart).prompt == ""
    assert records[0]["start_index"] == -1


def _retry_seal(events: list[Event], options: TurnOptions) -> str:
    """The diff hash sealed by a retry's proof (turn 2)."""
    proof = one(events[-1], TurnEnd).proof
    records = [json.loads(line) for line in options.journal.read_text().splitlines()]
    return str(next(r for r in records if r.get("record_hash") == proof)["diff_hash"])


def _retry_hash(prompt: str | None, reply: str) -> str:
    """What a turn-2 proof with no parent and no reasoning seals for `prompt`."""
    from saddle.journal import build_record

    return build_record(
        evidence_id="chat#2",
        node_id="chat#2",
        diff=json.dumps({"prompt": prompt, "rounds": [{"reply": reply, "tools": []}]}),
        parent_proofs=[],
        gate_outputs=[],
        requirement_ids=[],
        thinking="",
    ).diff_hash


def test_a_retry_is_sealed_with_the_question_it_answers(options: TurnOptions) -> None:
    # A retry passes no text, but the turn it seals answered a question: the
    # proof must commit to that question, as TurnStart reports it, not to null.
    messages: list[dict[str, Any]] = [
        {"role": "user", "content": "name three frogs"},
        {"role": "assistant", "content": "no"},
    ]
    events, _ = _retry(FakeClient([[content("tree, bull, glass")]]), options, messages)
    assert one(events[0], TurnStart).prompt == "name three frogs"
    assert _retry_seal(events, options) == _retry_hash("name three frogs", "tree, bull, glass")
    assert _retry_seal(events, options) != _retry_hash(None, "tree, bull, glass")


def test_a_retry_with_no_question_is_sealed_with_an_empty_prompt_not_null(
    options: TurnOptions,
) -> None:
    messages: list[dict[str, Any]] = [{"role": "system", "content": "be brief"}]
    events, _ = _retry(FakeClient([[content("ok")]]), options, messages)
    assert _retry_seal(events, options) == _retry_hash("", "ok")
    assert _retry_seal(events, options) != _retry_hash(None, "ok")


# -- a message written while the turn runs (F40) -------------------------------


def _later(*batches: list[str]) -> Any:
    """A `steer` that hands over one batch per request: nothing before the
    first, then each batch in turn, so a message arrives mid-turn."""
    pending = [[], *batches]

    def steer() -> list[tuple[str, list[Path]]]:
        batch = pending.pop(0) if pending else []
        return [(text, []) for text in batch]

    return steer


def test_a_message_written_mid_turn_opens_the_next_request_after_the_tool_results(
    options: TurnOptions, monkeypatch: pytest.MonkeyPatch
) -> None:
    sealed: list[list[dict[str, Any]]] = []
    real_seal = engine._seal

    def seal(journal: Path, **kw: Any) -> str:
        sealed.append(kw["rounds"])
        return real_seal(journal, **kw)

    monkeypatch.setattr(engine, "_seal", seal)
    (options.workdir / "note.txt").write_text("contents")
    client = FakeClient(
        [
            [tool("read_file", call_id="r1", path="note.txt")],
            [content("I see the dialog")],
        ]
    )
    messages: list[dict[str, Any]] = []
    events = run(
        client,
        options,
        messages=messages,
        steer=_later(["an error dialog is on screen", "it says disk full"]),
    )
    second = client.asked[1]["messages"]
    # After the round's tool result, in the order written, as the person's words.
    assert [m["role"] for m in second[-3:]] == ["tool", "user", "user"]
    assert second[-3]["tool_call_id"] == "r1"
    assert [m["content"] for m in second[-2:]] == [
        "an error dialog is on screen",
        "it says disk full",
    ]
    # The turn went on: not stopped, and the model answered.
    assert not any("stopped" in e.message for e in those(events, ErrorEvent))
    assert messages[-1] == {"role": "assistant", "content": "I see the dialog"}
    delivered = [e.text for e in those(events, MessageDelivered)]
    assert delivered == ["an error dialog is on screen", "it says disk full"]
    # Announced before the request it rides on is answered.
    assert kinds(events).index("message.delivered") < kinds(events).index("content.delta")
    # The turn's sealed record holds what the person said, between the rounds.
    assert [list(r) for r in sealed[0]] == [
        ["reply", "tools"],
        ["person"],
        ["person"],
        ["reply", "tools"],
    ]
    assert sealed[0][2] == {"person": "it says disk full"}


def test_a_message_with_a_screenshot_reaches_the_model_as_an_image(options: TurnOptions) -> None:
    shot = options.workdir / "dialog.png"
    shot.write_bytes(b"\x89PNG\r\n\x1a\n")
    client = FakeClient([[tool("read_file", path="dialog.png")], [content("ok")]])
    calls = iter([[], [("look at this", [shot])]])
    run(client, options, steer=lambda: next(calls, []))
    said = client.asked[1]["messages"][-1]
    assert said["role"] == "user"
    assert [p["type"] for p in said["content"]] == ["text", "image_url"]


def test_without_a_message_the_requests_are_unchanged(options: TurnOptions) -> None:
    # Known good for the other side: an empty steer adds nothing.
    (options.workdir / "note.txt").write_text("contents")
    client = FakeClient([[tool("read_file", path="note.txt")], [content("done")]])
    events = run(client, options, steer=lambda: [])
    assert client.asked[1]["messages"][-1]["role"] == "tool"
    assert not those(events, MessageDelivered)


def test_a_stopped_turn_takes_no_message_so_the_next_turn_gets_it(options: TurnOptions) -> None:
    taken: list[str] = []

    def steer() -> list[tuple[str, list[Path]]]:
        taken.append("asked")
        return [("late", [])]

    stopped = {"now": True}
    events = run(FakeClient([[content("x")]]), options, steer=steer, cancel=lambda: stopped["now"])
    assert taken == []
    assert not those(events, MessageDelivered)


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


# -- sizing the reply ---------------------------------------------------------

# The bug these pin: `budget` sized the reply from `estimate_tokens`, which
# counts characters over four and does not count the tool schemas at all.
# On a fresh session -- which asks for nearly the whole window -- the request
# overshot by a single token and the server refused it outright:
#
#   HTTP 400: maximum context length is 175000 tokens. However, you
#   requested 170901 output tokens and your prompt contains at least 4100
#   input tokens, for a total of at least 175001
#
# The user saw an empty reply and a persona that "did not work".


def test_the_budget_uses_the_servers_own_count_when_it_has_one(options: TurnOptions) -> None:
    options.context_tokens = 100_000
    asked: list[dict[str, Any]] = []

    def counter(messages: list[dict[str, Any]], *, tools: Any = None) -> int:
        asked.append({"messages": messages, "tools": tools})
        return 4_100

    assert options.budget([{"role": "user", "content": "hi"}], counter) == (100_000 - 4_100 - 2048)
    # The schemas are part of the prompt, so they are part of what is counted.
    assert asked[0]["tools"] == options.tools


def test_a_counted_request_fits_the_window(options: TurnOptions) -> None:
    # The property that was violated: prompt + reply must fit, with the
    # prompt measured the way the server measures it.
    options.context_tokens = 175_000
    for real in (74, 948, 4_100, 90_000, 174_000):

        def counter(_m: Any, *, tools: Any = None, n: int = real) -> int:
            return n

        total = real + options.budget([{"role": "user", "content": "x"}], counter)
        assert total <= options.context_tokens or total == real + MIN_OUTPUT, (
            f"{real} + budget overflows the window"
        )


def test_a_server_that_cannot_count_falls_back_and_still_leaves_room(options: TurnOptions) -> None:
    options.context_tokens = 175_000
    messages = [{"role": "user", "content": "Hello, can you help me?" * 30}]

    def no_count(_m: Any, *, tools: Any = None) -> None:
        return None

    guessed = options.budget(messages, no_count)
    assert guessed == options.budget(messages)  # same as having no counter

    # The contract, not a proxy for it: the guess must exceed the raw sum by
    # the safety factor, because the raw sum is what was measured too low --
    # 2,732 estimated against 4,100 charged, a ratio of 1.5.
    raw = estimate_tokens(messages) + options.tool_tokens()
    assert options.input_estimate(messages) >= int(raw * 1.5)
    assert options.input_estimate(messages) == int(raw * INPUT_SAFETY)


def test_the_tool_schemas_are_counted_at_all(options: TurnOptions) -> None:
    # They were not, and they are sent with every single request.
    assert options.tool_tokens() > 0
    assert options.input_estimate([]) >= options.tool_tokens()


def test_an_explicit_max_tokens_still_bypasses_all_of_this(options: TurnOptions) -> None:
    options.max_tokens = 4_096

    def counter(_m: Any, *, tools: Any = None) -> int:
        return 999_999

    assert options.budget([{"role": "user", "content": "x"}], counter) == 4_096


def test_a_conversation_compacted_to_the_limit_still_leaves_room_to_answer(
    options: TurnOptions,
) -> None:
    """The invariant, stated as the thing that must hold.

    Compaction used to be handed the whole window as its ceiling, so it was
    content to let the conversation fill every token of it -- and `budget`
    would then ask for MIN_OUTPUT on top of a full prompt. Asserting that
    the limit is merely "smaller than the window" does not catch that; the
    limit has to be small enough that a conversation sitting exactly on it
    still fits alongside a minimum reply.
    """
    for window in (32_000, 100_000, 175_000, 1_000_000):
        options.context_tokens = window
        at_the_limit = options.compaction_limit() + options.tool_tokens()
        worst_case_prompt = int(at_the_limit * INPUT_SAFETY)
        assert worst_case_prompt + MIN_OUTPUT + OUTPUT_MARGIN <= window, (
            f"window {window}: a conversation compacted to the limit leaves no room for a reply"
        )


def test_the_turn_asks_the_client_to_count(options: TurnOptions) -> None:
    class Counting(FakeClient):
        def __init__(self) -> None:
            super().__init__([[content("ok")]])
            self.counted = 0

        def count_tokens(self, _messages: Any, *, tools: Any = None) -> int:
            self.counted += 1
            return 1_234

    client = Counting()
    run(client, options)
    assert client.counted == 1
    assert client.asked[0]["max_tokens"] == options.context_tokens - 1_234 - 2048


def test_a_client_with_no_counter_still_runs(options: TurnOptions) -> None:
    # FakeClient has no count_tokens, which is the older-server case.
    events = run(FakeClient([[content("ok")]]), options)
    assert events[-1].kind == "turn.end"


class _RealCounts(FakeClient):
    """A server whose tokenizer says each message costs `per` real tokens."""

    def __init__(self, per: int) -> None:
        super().__init__([[content("ok")]])
        self.per = per

    def count_tokens(self, messages: Any, *, tools: Any = None) -> int:
        notes = sum(str(m.get("content", "")).startswith("[Earlier conversation") for m in messages)
        return self.per * (len(messages) - notes) + NOTE_TOKENS * notes


NOTE_TOKENS = 300
"""What the stand-in charges compaction's own note: a few hundred tokens,
not a full message's worth."""


def test_compaction_waits_for_the_real_token_count_not_the_estimate(
    options: TurnOptions,
) -> None:
    """Red before: a conversation the chars-based estimate put over its limit
    (20 messages of 20,000 characters, 100,000 estimated) was compacted at
    60,000 real tokens of a 175,000 window. A dogfood run compacted at
    85,550 real tokens this way and then re-read what it had lost."""
    options.context_tokens = 175_000
    messages = [{"role": "user", "content": "x" * 20_000} for _ in range(20)]
    # 21 x 5,000 = 105,000 real tokens: over the old ceiling (compaction_limit,
    # about 80,000), under the real one (140,184). Only the real one keeps it.
    assert options.compaction_limit() < 105_000 <= options.compaction_limit_exact()
    events = run(_RealCounts(5_000), options, messages=messages)
    assert not those(events, Compaction)
    assert len(messages) >= 21


def test_over_the_real_limit_compaction_brings_the_real_count_under_it(
    options: TurnOptions,
) -> None:
    options.context_tokens = 175_000
    messages = [{"role": "user", "content": "x" * 20_000} for _ in range(20)]
    events = run(_RealCounts(8_000), options, messages=messages)
    compaction = those(events, Compaction)[0]
    assert compaction.dropped_messages > 0
    real = (compaction.kept_messages - 1) * 8_000 + NOTE_TOKENS  # one of them is the note
    assert real <= options.compaction_limit_exact()
    assert options.compaction_limit_exact() == 175_000 - 32_768 - 2048


def test_old_screenshots_are_trimmed_before_a_request_and_said_so(options: TurnOptions) -> None:
    """Live: 38 screenshots were ~46k of a ~52k-token context six minutes into a
    LibreOffice run. Known-good: the request carries only the newest three, and
    the trim is announced like any compaction."""
    messages: list[dict[str, Any]] = []
    for n in range(7):
        messages.append(
            images_message([(f"c{n}", f"screenshot {n}", "data:image/png;base64,AA==")])
        )
    client = FakeClient([[content("ok")]])
    events = run(client, options, messages=messages)
    sent = client.asked[0]["messages"]
    pictures = [p for m in sent if isinstance(m.get("content"), list) for p in m["content"]]
    assert sum(p["type"] == "image_url" for p in pictures) == 3
    (trim,) = those(events, Compaction)
    assert trim.dropped_messages == 0
    assert trim.summary == "4 older screenshots elided"


def test_the_system_prompt_names_the_working_folder(options: TurnOptions) -> None:
    """Live (rung 5): asked to export art.png "in this folder", the model did
    not know which folder that was; the prompt never said, and called it "the
    user's repository", so it searched another checkout and read files there.
    Known-good: the system message names the working folder."""
    options.system_prompt = "You are careful."
    client = FakeClient([[content("ok")]])
    run(client, options)
    system = client.asked[0]["messages"][0]
    assert system["role"] == "system"
    assert system["content"].startswith("You are careful.")
    assert f"Your working folder is {options.workdir}" in system["content"]


def test_the_waiting_nudge_names_a_command_as_one_short_line() -> None:
    assert engine._clip_command("blender -b --python x.py\necho done") == "blender -b --python x.py"
    assert engine._clip_command("x" * 200) == "x" * 117 + "..."
    assert engine._clip_command("   ") == "   "


B9_CUT = '{"path": "build_forest.py", "content": "import bpy\\nfor x in range('


def test_a_cut_off_tool_call_is_kept_sendable_and_explained(options: TurnOptions) -> None:
    """B9 (2026-10-04): a write_file reply hit the output token limit, so its
    arguments ended mid-string. The engine kept that broken JSON in the
    assistant message; the server parses tool-call arguments and refused every
    later request (HTTP 400, "Unterminated string ..."), so the session died
    silently. Known-bad: the history carries the broken arguments. Known-good:
    the history carries valid JSON, the tool result says the call was cut off
    and nothing ran, and the model's next reply follows."""
    client = FakeClient(
        [
            [ToolCall(id="c1", name="write_file", arguments=B9_CUT)],
            [content("Writing it in parts.")],
        ]
    )
    events = run(client, options)
    assert len(client.asked) == 2
    sent = client.asked[1]["messages"]
    call = next(m for m in sent if m.get("tool_calls"))["tool_calls"][0]
    json.loads(call["function"]["arguments"])  # the server parses this; it must be valid
    result = next(m for m in sent if m.get("role") == "tool")["content"]
    assert result.startswith("error: ")
    assert "cut off" in result
    assert "nothing ran" in result
    assert not (options.workdir / "build_forest.py").exists()
    assert not [e for e in events if isinstance(e, ErrorEvent)]


def test_a_complete_tool_call_is_sent_back_as_it_came(options: TurnOptions) -> None:
    (options.workdir / "a.txt").write_text("a\n")
    arguments = json.dumps({"path": "a.txt"})
    client = FakeClient(
        [[ToolCall(id="c1", name="read_file", arguments=arguments)], [content("ok")]]
    )
    run(client, options)
    call = next(m for m in client.asked[1]["messages"] if m.get("tool_calls"))["tool_calls"][0]
    assert call["function"]["arguments"] == arguments


def test_a_cut_off_call_already_in_the_history_is_made_sendable(options: TurnOptions) -> None:
    """B9: the session that died kept the broken call in its saved history, so
    any later turn was refused too. A turn sends earlier tool calls with valid
    JSON, so such a session continues."""
    history: list[dict[str, Any]] = [
        {"role": "user", "content": "make a forest"},
        {
            "role": "assistant",
            "content": "",
            "tool_calls": [
                {
                    "id": "c1",
                    "type": "function",
                    "function": {"name": "write_file", "arguments": B9_CUT},
                }
            ],
        },
        {"role": "tool", "tool_call_id": "c1", "content": "error: arguments are not valid JSON"},
    ]
    client = FakeClient([[content("Continuing.")]])
    run(client, options, text="continue", messages=history)
    sent = client.asked[0]["messages"]
    call = next(m for m in sent if m.get("tool_calls"))["tool_calls"][0]
    json.loads(call["function"]["arguments"])


def test_without_a_token_count_compaction_still_leaves_a_working_reply_room(
    options: TurnOptions,
) -> None:
    """B9 (2026-10-04): Strata serves no /tokenize, so the estimate path ran,
    and its ceiling kept only MIN_OUTPUT for the reply. At 120 messages the
    prompt filled a 131,072 window to ~120k real tokens, the reply was capped
    at 8,774 tokens, and a 25 KB write_file was cut off mid-string. The
    estimate path keeps the same room as the exact one (`REPLY_ROOM`), or a
    quarter of a small window, never less than MIN_OUTPUT."""
    for window in (32_000, 131_072, 175_000, 1_000_000):
        options.context_tokens = window
        worst_case_prompt = int((options.compaction_limit() + options.tool_tokens()) * INPUT_SAFETY)
        room = max(MIN_OUTPUT, min(REPLY_ROOM, window // 4))
        assert worst_case_prompt + room + OUTPUT_MARGIN <= window, f"window {window}"


def test_without_a_token_count_a_small_window_keeps_no_more_than_a_quarter_for_the_reply(
    options: TurnOptions,
) -> None:
    """The other half of the reply room: a small window keeps a quarter of
    itself for the reply, not all of REPLY_ROOM, so its history is not
    compacted harder than the reply needs (64,000: 16,000 kept, not 32,768)."""
    for window in (40_000, 64_000, 100_000):
        options.context_tokens = window
        kept = window // 4
        assert kept < REPLY_ROOM
        expected = int((window - kept - OUTPUT_MARGIN) / INPUT_SAFETY) - options.tool_tokens()
        assert options.compaction_limit() == expected, f"window {window}"
