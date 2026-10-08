"""A run's timeline: each round's request, reply, calls and whole results (#164).

Known-good: every round of a real autonomous run joins the request the model was
sent, the reply it gave and each call's whole result, where the ledger holds only the
first 500 characters; a compaction is placed before the round it compacted for; an
image-limit retry is a request with no round. Known-bad: a missing or cut log read as
an empty conversation instead of said; a reply taken from an earlier round; a long
call left without its span because the span sealed its arguments capped.
"""

from __future__ import annotations

import json
import subprocess
from collections.abc import Iterator
from pathlib import Path
from typing import Any, cast

import pytest

from saddle.auto import AutoOptions, run_auto
from saddle.conversation import CONVERSATION_LOG, ConversationLog
from saddle.engine import AutoRun, RunBudget, TurnOptions, run_turn
from saddle.journal import AUTO_START, MAX_SPAN_DETAIL_CHARS, append_span, build_span
from saddle.tools import ToolContext
from saddle.triage import Call, TriageError, timeline
from saddle.vision import (
    PROBE_QUESTION,
    PROBE_TEXT,
    reset_cache,
    server_accepts_images,
    solid_png,
)
from saddle.vllm import StreamToken, ToolCall, VllmClient, VllmRequestError

LINES = 3000


class Recording:
    """A model that replays rounds and keeps a copy of each request as sent."""

    def __init__(self, rounds: list[list[Any]]) -> None:
        self.rounds = list(rounds)
        self.asked: list[dict[str, Any]] = []

    def __enter__(self) -> Recording:
        return self

    def __exit__(self, *exc: object) -> None:
        return None

    def stream_chat(self, messages: Any, **kwargs: Any) -> Iterator[Any]:
        self.asked.append(json.loads(json.dumps({"messages": list(messages), **kwargs})))
        return iter(self.rounds.pop(0) if self.rounds else [])


def call(name: str, call_id: str, **arguments: Any) -> list[Any]:
    return [ToolCall(id=call_id, name=name, arguments=json.dumps(arguments))]


def git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(repo), "-c", "user.name=t", "-c", "user.email=t@t", *args],
        check=True,
        capture_output=True,
        text=True,
    ).stdout


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    root = tmp_path / "repo"
    (root / "tests").mkdir(parents=True)
    (root / "calc.py").write_text("def add(a, b):\n    return a - b\n")
    (root / "notes.txt").write_text("".join(f"note line {i}\n" for i in range(LINES)))
    (root / "tests" / "test_calc.py").write_text(
        "from calc import add\n\n\ndef test_add():\n    assert add(2, 2) == 4\n"
    )
    git(root, "init", "-q", "-b", "main")
    git(root, "add", "-A")
    git(root, "commit", "-q", "-m", "init")
    return root


def read_edit_finish() -> Recording:
    return Recording(
        [
            call("read_file", "c1", path="notes.txt"),
            call("edit_file", "c2", path="calc.py", old="a - b", new="a + b"),
            call("finish", "f1", summary="fixed add"),
        ]
    )


def run(repo: Path, client: Recording) -> Path:
    options = AutoOptions(task="make add add", repo=repo, run_id="r1", arm="E")
    return run_auto(options, cast(VllmClient, client)).journal


# -- a real run ---------------------------------------------------------------------


def test_each_round_joins_its_request_its_reply_and_each_calls_whole_result(
    repo: Path,
) -> None:
    client = read_edit_finish()
    seen = timeline(run(repo, client))
    assert seen.log == "whole"
    assert seen.outcome is not None
    assert seen.outcome.name == "auto:finished"
    assert [r.spend["request"] for r in seen.rounds] == [1, 2, 3]
    assert [r.request.messages if r.request else None for r in seen.rounds] == [
        asked["messages"] for asked in client.asked
    ]
    read, edit, finish = (r.calls for r in seen.rounds)
    assert [c.name for c in (*read, *edit, *finish)] == ["read_file", "edit_file", "finish"]
    # the ledger keeps the first 500 characters; the timeline has what the model read
    (whole,) = read
    assert whole.span is not None
    assert whole.result is not None
    assert len(whole.span.detail) <= MAX_SPAN_DETAIL_CHARS < len(whole.result)
    shown = "note line 399"  # the last of the 400 lines a read shows
    assert shown in whole.result
    assert shown not in whole.span.detail
    assert edit[0].edited == "calc.py"
    assert read[0].edited is None
    # the last round's result is the end the log kept, which no request carries
    assert finish[0].result is not None
    assert finish[0].span is not None
    assert finish[0].result.startswith(finish[0].span.detail[:40])
    assert all(r.prompt_tokens is None for r in seen.rounds)  # the model gave no count


def test_without_a_log_the_rounds_hold_the_ledger_and_say_nothing_more(repo: Path) -> None:
    ledger = run(repo, read_edit_finish())
    (ledger.parent / CONVERSATION_LOG).unlink()
    seen = timeline(ledger)
    assert seen.log == "none"
    assert len(seen.rounds) == 3
    assert all((r.request, r.reply, r.calls) == (None, None, ()) for r in seen.rounds)
    assert [s.name for s in seen.rounds[1].spans] == ["auto:spend", "edit_file"]


def test_a_cut_log_gives_the_rounds_it_reaches_and_says_where_it_was_cut(repo: Path) -> None:
    ledger = run(repo, read_edit_finish())
    log = ledger.parent / CONVERSATION_LOG
    text = log.read_text()
    third = text.index('{"request": 3,')
    log.write_text(text[: third + 20])
    seen = timeline(ledger)
    assert "is cut at line" in seen.log
    first, second, third_round = seen.rounds
    assert first.request is not None
    assert first.reply is not None  # carried by request 2, which is whole
    assert (second.request is not None, second.reply) == (True, None)
    assert (third_round.request, third_round.calls) == (None, ())


def test_a_ledger_that_is_not_a_runs_is_refused(tmp_path: Path) -> None:
    ledger = tmp_path / "proofs.jsonl"
    append_span(ledger, build_span(node_id="n", argv=["x"], duration_ms=0, exit_code=0, detail=""))
    with pytest.raises(TriageError, match="not an autonomous run's ledger"):
        timeline(ledger)


# -- turns the engine runs ----------------------------------------------------------


def started(tmp_path: Path, **settings: Any) -> tuple[TurnOptions, Path]:
    """An autonomous turn's options, its ledger opened as `run_auto` opens it."""
    ledger = tmp_path / "run" / "proofs.jsonl"
    ledger.parent.mkdir()
    start = build_span(
        node_id="chat#1",
        argv=[AUTO_START, "t"],
        duration_ms=0,
        exit_code=0,
        detail="",
        kind="agent",
    )
    append_span(ledger, start)
    auto = AutoRun(budget=RunBudget(time_s=600, tokens=10**6), run_span=start.span_id)
    options = TurnOptions(
        workdir=tmp_path,
        journal=ledger,
        auto=auto,
        conversation=ConversationLog(ledger.parent / CONVERSATION_LOG),
        **settings,
    )
    return options, ledger


def finish(call_id: str = "f1") -> list[Any]:
    return call("finish", call_id, summary="done")


class OneImage(Recording):
    """Refuses, as vLLM does, a request carrying more than one image."""

    server_key = "one-image"

    def stream_chat(self, messages: Any, **kwargs: Any) -> Iterator[Any]:
        content = messages[0]["content"]
        if isinstance(content, list) and content[0].get("text") == PROBE_QUESTION:
            return iter([StreamToken("content", f"It reads {PROBE_TEXT}.")])
        images = sum(
            1
            for m in messages
            if isinstance(m.get("content"), list)
            for p in m["content"]
            if p.get("type") == "image_url"
        )
        if images > 1:
            self.asked.append({"refused": True})
            msg = "server returned HTTP 400: At most 1 image(s) may be provided in one prompt."
            raise VllmRequestError(msg)
        return super().stream_chat(messages, **kwargs)


def test_an_image_limit_retry_is_a_request_with_no_round(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # the real probe, against this scripted server (conftest's `_no_image_probe`)
    monkeypatch.setattr("saddle.engine.server_accepts_images", server_accepts_images)
    reset_cache()
    for name, color in (("a.png", (255, 0, 0)), ("b.png", (0, 0, 255))):
        (tmp_path / name).write_bytes(solid_png(8, 8, color))
    options, ledger = started(tmp_path)
    client = OneImage(
        [call("read_file", "c1", path="a.png"), call("read_file", "c2", path="b.png"), finish()]
    )
    context = ToolContext(workdir=tmp_path, images=True)
    list(run_turn(cast(VllmClient, client), [], "look", options, turn=1, context=context))
    reset_cache()
    assert {"refused": True} in client.asked
    seen = timeline(ledger)
    assert [r.spend["request"] for r in seen.rounds] == [1, 2, 4]
    assert [r.request.number if r.request else None for r in seen.rounds] == [1, 2, 4]
    # round 2's reply rode on the refused request 3; each round's calls are its own
    assert [[c.id for c in r.calls] for r in seen.rounds] == [["c1"], ["c2"], ["f1"]]


def test_a_compaction_is_placed_before_the_round_it_compacted_for(tmp_path: Path) -> None:
    (tmp_path / "big.txt").write_text("word " * 6000)
    options, ledger = started(tmp_path, context_tokens=20_000)
    history = [{"role": "user", "content": f"question {i} " + "x" * 4000} for i in range(7)]
    client = Recording([call("read_file", "c1", path="big.txt"), finish()])
    list(run_turn(cast(VllmClient, client), history, "read it", options, turn=1))
    seen = timeline(ledger)
    first, second = seen.rounds
    assert second.compaction is not None
    assert second.compaction.name == "compaction"
    assert second.request is not None
    assert second.request.kept < len(first.request.messages if first.request else [])
    # the compaction kept round 1's reply, and the timeline still finds it
    assert [c.id for c in first.calls] == ["c1"]
    assert [c.id for c in second.calls] == ["f1"]


def test_a_call_whose_arguments_were_cut_off_still_finds_its_span(tmp_path: Path) -> None:
    options, ledger = started(tmp_path)
    cut = ToolCall(id="c1", name="read_file", arguments='{"path": "no')
    client = Recording([[cut], finish()])
    list(run_turn(cast(VllmClient, client), [], "go", options, turn=1))
    (first, _) = timeline(ledger).rounds
    (made,) = first.calls
    assert made.arguments == "{}"  # as the history keeps it
    assert made.span is not None
    assert made.span.argv[1] == '{"path": "no'  # as the ledger sealed it


def test_a_call_longer_than_the_ledgers_argument_cap_still_finds_its_span(
    tmp_path: Path,
) -> None:
    options, ledger = started(tmp_path)
    long = call("write_file", "c1", path="long.txt", content="z" * 9000)
    client = Recording([long, finish()])
    list(run_turn(cast(VllmClient, client), [], "go", options, turn=1))
    (first, _) = timeline(ledger).rounds
    (made,) = first.calls
    assert made.span is not None
    assert len(made.span.argv[1]) < len(made.arguments)
    assert made.edited == "long.txt"


# -- records the engine does not write in a normal run, read without a guess ---------


def ledger_of(tmp_path: Path, *spends: list[str]) -> Path:
    """A ledger holding a run's start and one spend span per argv given."""
    ledger = tmp_path / "proofs.jsonl"
    start = build_span(
        node_id="chat#1", argv=[AUTO_START, "t"], duration_ms=0, exit_code=0, detail=""
    )
    append_span(ledger, start)
    for argv in spends:
        append_span(
            ledger,
            build_span(
                node_id="chat#1", argv=argv, duration_ms=0, exit_code=0, detail="", kind="agent"
            ),
        )
    return ledger


def spend(request: int) -> list[str]:
    return ["auto:spend", json.dumps({"request": request})]


SYSTEM: dict[str, Any] = {"role": "system", "content": "s"}
ASK: dict[str, Any] = {"role": "user", "content": "go"}


def assistant(call_id: str) -> dict[str, Any]:
    function = {"name": "read_file", "arguments": json.dumps({"path": call_id})}
    return {
        "role": "assistant",
        "content": "",
        "tool_calls": [{"id": call_id, "function": function}],
    }


def result(call_id: str) -> dict[str, Any]:
    return {"role": "tool", "tool_call_id": call_id, "content": f"read {call_id}"}


def test_a_reply_the_request_already_held_is_not_this_rounds(tmp_path: Path) -> None:
    """A round cut before its reply was kept: the next request's last assistant
    message is an older round's, and is not taken for this one's."""
    ledger = ledger_of(tmp_path, spend(1), spend(2))
    log = ConversationLog(tmp_path / CONVERSATION_LOG)
    before = [SYSTEM, ASK, assistant("c0"), result("c0")]
    steer = {"role": "user", "content": "also check b"}
    log.record(before, max_tokens=9, tools=[])
    log.record([*before, steer], max_tokens=9, tools=[])
    log.end([*before, steer, assistant("c2"), result("c2")])
    first, second = timeline(ledger).rounds
    assert (first.reply, first.calls) == (None, ())
    (made,) = second.calls
    assert (made.id, made.result) == ("c2", "read c2")
    assert made.span is None  # the ledger holds no span for it: none is invented


def test_a_conversation_with_no_reply_in_it_gives_none(tmp_path: Path) -> None:
    ledger = ledger_of(tmp_path, spend(1), spend(2))
    log = ConversationLog(tmp_path / CONVERSATION_LOG)
    log.record([SYSTEM, ASK], max_tokens=9, tools=[])
    log.record([SYSTEM, ASK, {"role": "user", "content": "more"}], max_tokens=9, tools=[])
    seen = timeline(ledger)
    assert seen.log == "no end: the run did not end normally"
    assert [(r.reply, r.calls) for r in seen.rounds] == [(None, ()), (None, ())]


def test_a_spend_record_that_does_not_parse_is_read_as_empty(tmp_path: Path) -> None:
    ledger = ledger_of(tmp_path, ["auto:spend", "not json"], ["auto:spend", "[1]"], ["auto:spend"])
    seen = timeline(ledger)
    assert [dict(r.spend) for r in seen.rounds] == [{}, {}, {}]
    assert [r.number for r in seen.rounds] == [1, 2, 3]


@pytest.mark.parametrize(
    ("name", "arguments", "edited"),
    [
        ("edit_file", '{"path": "a.py"}', "a.py"),
        ("write_file", '{"path": "b.py", "content": ""}', "b.py"),
        ("edit_file", "not json", None),
        ("edit_file", "[1]", None),
        ("edit_file", '{"path": 3}', None),
        ("read_file", '{"path": "a.py"}', None),
    ],
)
def test_the_file_a_call_edited_is_its_edit_tools_path_and_nothing_else(
    name: str, arguments: str, edited: str | None
) -> None:
    assert Call(id="c", name=name, arguments=arguments, result=None, span=None).edited == edited
