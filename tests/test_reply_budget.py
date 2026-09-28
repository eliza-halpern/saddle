"""A run's budgets hold inside one reply, not only between replies (#115, #98).

Contracts:

1. No reply spends past the token budget: its `max_tokens` is what the
   budget has left, so a server honouring it stops the reply at the limit.
2. No reply carries the run past 80% of its token budget without the
   budget question on record before the next round: while the question has
   not been put, a reply's `max_tokens` stops at the 80% mark, and the
   question is asked before the next request.
3. A reply in flight when the time budget runs out is cancelled within
   `STREAM_POLL_S` of the limit, and the run stops saying so, instead of
   waiting for the reply to end (a stalled server never ends it).

The fakes model the server: `Server` stops a reply at `max_tokens` and
reports usage, as vLLM does. #115's own spend (104, 49, 789, then one
reply of 99,058 tokens against a 100k budget) is replayed exactly.
"""

from __future__ import annotations

import json
import subprocess
import threading
import time
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any, cast

import pytest

from saddle import engine
from saddle.auto import AutoOptions, AutoResult, run_auto
from saddle.engine import (
    EXTEND_BUDGET,
    AutoRun,
    RunBudget,
    TurnOptions,
    run_turn,
)
from saddle.events import ErrorEvent, Event, Question, ReasoningDelta, RunProgress
from saddle.journal import attempt_sidecar_path, read_spans, verify_journal
from saddle.vllm import StreamToken, StreamUsage, ToolCall, VllmClient

FOREVER = 10**9
"""A reply that would never end on its own: only `max_tokens` stops it."""


def call(name: str, cid: str, **arguments: Any) -> ToolCall:
    return ToolCall(id=cid, name=name, arguments=json.dumps(arguments))


class Server:
    """Replies of scripted lengths, each stopped at `max_tokens` like vLLM.

    A reply shorter than its cap ends in a tool call (`read_file`, or
    `finish` for a reply scripted as 0); a reply the cap stops is cut
    mid-reasoning and has none. Usage is always reported, so every count is
    the server's, never an estimate.
    """

    def __init__(self, replies: list[int]) -> None:
        self.replies = list(replies)
        self.caps: list[int] = []

    def __enter__(self) -> Server:
        return self

    def __exit__(self, *_: object) -> None:
        return None

    def stream_chat(self, messages: Any, *, max_tokens: int, **_: Any) -> Iterator[Any]:
        self.caps.append(max_tokens)
        wanted = self.replies.pop(0) if self.replies else 0
        if wanted == 0:
            yield call("finish", "f", summary="done")
            yield StreamUsage(prompt_tokens=10, completion_tokens=1)
            return
        made = min(wanted, max_tokens)
        yield StreamToken(stream="reasoning", text="x" * 16)
        if made < wanted:
            yield StreamUsage(prompt_tokens=10, completion_tokens=made)
            return
        yield call("read_file", f"r{len(self.caps)}", path="calc.py")
        yield StreamUsage(prompt_tokens=10, completion_tokens=made)


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
    root.mkdir()
    (root / "calc.py").write_text("x = 1\n")
    git(root, "init", "-q", "-b", "main")
    git(root, "add", "-A")
    git(root, "commit", "-q", "-m", "init")
    return root


def auto(repo: Path, client: Any, **kwargs: Any) -> AutoResult:
    answer = kwargs.pop("answer", None)
    options = AutoOptions(task="t", repo=repo, run_id="r1", arm="E", **kwargs)
    return run_auto(options, cast(VllmClient, client), answer=answer)


def spends(journal: Path) -> list[tuple[dict[str, Any], str]]:
    return [
        (json.loads(s.argv[1]), s.detail) for s in read_spans(journal) if s.name == "auto:spend"
    ]


def sidecar(journal: Path) -> dict[str, Any]:
    [end] = [s for s in read_spans(journal) if s.name in ("auto:stopped", "auto:finished")]
    return cast(dict[str, Any], json.loads(attempt_sidecar_path(journal, end.span_id).read_text()))


def budget_breaches(journal: Path, tokens: int) -> list[str]:
    """Every reply that broke contract 1 or 2, in journal order.

    Contract 1: a reply ends at or under the budget. Contract 2: no reply
    ends past the 80% mark, and none starts at or past it, while no
    budget question is on record.
    """
    mark = tokens * engine.BUDGET_ASK_AT
    spent, asked, breaches = 0, False, []
    for span in read_spans(journal):
        if span.name == "question" and span.argv[1].startswith("This run has used"):
            asked = True
        if span.name != "auto:spend":
            continue
        before = spent
        spent += json.loads(span.argv[1])["completion_tokens"]
        if spent > tokens:
            breaches.append(f"past the budget: {before} -> {spent}")
        if not asked and (spent > mark or before >= mark):
            breaches.append(f"past 80% unasked: {before} -> {spent}")
    return breaches


# -- #115: the runaway reply --------------------------------------------------


def test_the_runaway_reply_is_stopped_at_80_percent_and_the_question_asked(repo: Path) -> None:
    server = Server([104, 49, 789, FOREVER, FOREVER])
    result = auto(repo, server, token_budget=100_000)

    assert budget_breaches(result.journal, 100_000) == []
    # the fourth reply was stopped at the mark by its own max_tokens
    assert server.caps[3] == 80_000 - 942
    records = spends(result.journal)
    assert [r["completion_tokens"] for r, _ in records] == [104, 49, 789, 79_058, 20_000]
    fourth, detail = records[3]
    assert fourth["max_tokens"] == 79_058
    assert fourth["cut"] == engine.CUT_AT_MARK
    assert detail.endswith(f"; {engine.CUT_AT_MARK}")
    last, detail = records[4]
    assert (last["max_tokens"], last["cut"]) == (20_000, engine.CUT_AT_LIMIT)
    assert detail.endswith(f"; {engine.CUT_AT_LIMIT}")
    assert "cut" not in records[0][0]
    # headless: the question is sealed with its default, and the run stops
    # at the original limit, having spent exactly the budget and no more
    names = [s.name for s in read_spans(result.journal)]
    assert names.index("question") < [i for i, n in enumerate(names) if n == "auto:spend"][4]
    assert result.outcome == "stopped"
    assert result.reason.startswith("token budget exhausted: ~100000 of 100000")
    assert sidecar(result.journal)["tokens_spent"] == 100_000
    assert verify_journal(result.journal) == []


def test_extend_at_the_mark_lets_the_run_go_on(repo: Path) -> None:
    server = Server([104, 49, 789, FOREVER, 0])
    result = auto(repo, server, token_budget=100_000, answer=lambda q: EXTEND_BUDGET)
    assert (result.outcome, result.reason) == ("finished", "finish called")
    assert server.caps[4] == 200_000 - 80_000
    assert budget_breaches(result.journal, 200_000) == []
    assert verify_journal(result.journal) == []


def test_a_reply_cut_at_the_mark_is_told_so_and_is_not_an_empty_round(tmp_path: Path) -> None:
    # Three cut-short replies in a row would be three empty rounds, the stop
    # cap; the one cut at the mark is the harness's doing and is not counted.
    server = Server([FOREVER, 5, 0])
    run = AutoRun(budget=RunBudget(time_s=3600, tokens=1000), run_span="s")
    run.empty_rounds = engine.EMPTY_ROUND_CAP - 1
    (tmp_path / "calc.py").write_text("x = 1\n")
    options = TurnOptions(workdir=tmp_path, journal=tmp_path / "j.jsonl", auto=run)
    messages: list[dict[str, Any]] = []
    list(run_turn(cast(VllmClient, server), messages, "t", options, turn=1))
    assert (run.outcome, run.reason) == ("finished", "finish called")
    assert server.caps[:2] == [800, 200]
    nudges = [m["content"] for m in messages if m["role"] == "user"][1:]
    assert nudges[0] == engine.CUT_NUDGE


def test_a_reply_under_the_mark_is_not_capped_below_the_budget_once_asked(
    tmp_path: Path,
) -> None:
    server = Server([10, 0])
    run = AutoRun(budget=RunBudget(time_s=3600, tokens=1000), run_span="s", asked={"budget"})
    (tmp_path / "calc.py").write_text("x = 1\n")
    options = TurnOptions(workdir=tmp_path, journal=tmp_path / "j.jsonl", auto=run)
    list(run_turn(cast(VllmClient, server), [], "t", options, turn=1))
    assert server.caps == [1000, 990]


# -- #98: the time budget inside a reply ---------------------------------------


class Stalled:
    """A server that sends nothing for the first reply until released."""

    def __init__(self) -> None:
        self.release = threading.Event()
        self.closed = threading.Event()

    def __enter__(self) -> Stalled:
        return self

    def __exit__(self, *_: object) -> None:
        return None

    def stream_chat(self, messages: Any, **_: Any) -> Iterator[Any]:
        try:
            self.release.wait(timeout=5)
            yield StreamToken(stream="reasoning", text="late")
            yield call("finish", "f", summary="done late")
        finally:
            self.closed.set()


class Trickle:
    """A server that streams a token every 10 ms for up to `seconds`."""

    def __init__(self, seconds: float, *, ends: bool = True) -> None:
        self.seconds, self.ends = seconds, ends

    def __enter__(self) -> Trickle:
        return self

    def __exit__(self, *_: object) -> None:
        return None

    def stream_chat(self, messages: Any, **_: Any) -> Iterator[Any]:
        ends = time.monotonic() + self.seconds
        while time.monotonic() < ends:
            time.sleep(0.01)
            yield StreamToken(stream="reasoning", text="abcd")
        if self.ends:
            yield call("finish", "f", summary="done")


@pytest.fixture
def fast_poll(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(engine, "STREAM_POLL_S", 0.02, raising=False)


def timed_run(
    tmp_path: Path, client: Any, time_s: float, answer: Callable[[Question], str | None] | None
) -> tuple[AutoRun, list[Event], float]:
    run = AutoRun(budget=RunBudget(time_s=time_s, tokens=10**6), run_span="s", answer=answer)
    options = TurnOptions(workdir=tmp_path, journal=tmp_path / "j.jsonl", auto=run)
    began = time.monotonic()
    events = list(run_turn(cast(VllmClient, client), [], "t", options, turn=1))
    return run, events, time.monotonic() - began


@pytest.mark.usefixtures("fast_poll")
def test_a_stalled_reply_is_cancelled_at_the_time_limit(tmp_path: Path) -> None:
    server = Stalled()
    try:
        run, events, took = timed_run(tmp_path, server, 0.3, None)
        assert took < 0.3 + 1.0
        assert run.outcome == "stopped"
        assert run.reason.startswith("time budget exhausted: ")
        assert run.reason.endswith(f"; {engine.CUT_AT_TIME}")
        assert any(isinstance(e, ErrorEvent) and engine.CUT_AT_TIME in e.message for e in events)
        [(record, detail)] = spends(tmp_path / "j.jsonl")
        assert record["cut"] == engine.CUT_AT_TIME
        assert detail.endswith(f"; {engine.CUT_AT_TIME}")
    finally:
        server.release.set()
    # the abandoned reply is closed once it next yields, not left to run on
    assert server.closed.wait(timeout=5)


@pytest.mark.usefixtures("fast_poll")
def test_a_run_stopped_at_the_time_limit_seals_a_verified_ledger(repo: Path) -> None:
    server = Stalled()
    try:
        result = auto(repo, server, time_budget_s=0.3)
    finally:
        server.release.set()
    assert result.outcome == "stopped"
    assert result.reason.endswith(f"; {engine.CUT_AT_TIME}")
    assert sidecar(result.journal)["reason"] == result.reason
    [(record, _)] = spends(result.journal)
    assert record["cut"] == engine.CUT_AT_TIME
    assert verify_journal(result.journal) == []


@pytest.mark.usefixtures("fast_poll")
def test_a_reply_that_keeps_streaming_is_cancelled_at_the_time_limit(tmp_path: Path) -> None:
    run, events, took = timed_run(tmp_path, Trickle(3.0), 0.3, None)
    assert took < 0.3 + 1.0
    assert run.reason.endswith(f"; {engine.CUT_AT_TIME}")
    # the card's meter moves while the reply streams, labelled as partial
    partial = [e for e in events if isinstance(e, RunProgress) and getattr(e, "partial", False)]
    assert len(partial) >= 2
    assert partial[-1].tokens > partial[0].tokens > 0


class Timed:
    """Streams one reply across the 80% mark of a 100 s budget on a fake clock."""

    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now

    def __enter__(self) -> Timed:
        return self

    def __exit__(self, *_: object) -> None:
        return None

    def stream_chat(self, messages: Any, **_: Any) -> Iterator[Any]:
        for second in range(0, 191, 10):
            self.now = float(second)
            time.sleep(0.05)
            yield StreamToken(stream="reasoning", text="abcd")
        yield call("finish", "f", summary="done")


@pytest.mark.usefixtures("fast_poll")
def test_the_time_question_is_asked_while_the_reply_streams(tmp_path: Path) -> None:
    server = Timed()
    asked_at: list[float] = []

    def answer(q: Question) -> str:
        asked_at.append(server.now)
        return EXTEND_BUDGET

    run = AutoRun(
        budget=RunBudget(time_s=100, tokens=10**6, clock=server), run_span="s", answer=answer
    )
    options = TurnOptions(workdir=tmp_path, journal=tmp_path / "j.jsonl", auto=run)
    list(run_turn(cast(VllmClient, server), [], "t", options, turn=1))
    assert (run.outcome, run.reason) == ("finished", "finish called")
    assert run.budget.time_s == 200
    [at] = asked_at
    assert 80 <= at < 100  # mid-reply, before the reply ended at 190 s
    names = [s.name for s in read_spans(tmp_path / "j.jsonl")]
    assert names.index("question") < names.index("auto:spend")


@pytest.mark.usefixtures("fast_poll")
def test_stop_works_while_the_server_sends_nothing(tmp_path: Path) -> None:
    # Before, the stop button was read only when a token arrived.
    server = Stalled()
    pressed = time.monotonic() + 0.2
    run = AutoRun(budget=RunBudget(time_s=3600, tokens=10**6), run_span="s")
    options = TurnOptions(workdir=tmp_path, journal=tmp_path / "j.jsonl", auto=run)
    began = time.monotonic()
    try:
        events = list(
            run_turn(
                cast(VllmClient, server),
                [],
                "t",
                options,
                turn=1,
                cancel=lambda: time.monotonic() >= pressed,
            )
        )
    finally:
        server.release.set()
    assert time.monotonic() - began < 0.2 + 1.0
    assert run.reason == "cancelled"
    assert any(isinstance(e, ErrorEvent) and e.message == "stopped by you" for e in events)


@pytest.mark.usefixtures("fast_poll")
def test_a_chat_turn_streams_through_the_look_ups_untouched(tmp_path: Path) -> None:
    options = TurnOptions(workdir=tmp_path, journal=tmp_path / "j.jsonl")
    events = list(run_turn(cast(VllmClient, Trickle(0.2, ends=False)), [], "t", options, turn=1))
    assert not any(isinstance(e, RunProgress) for e in events)
    thought = [e for e in events if isinstance(e, ReasoningDelta)]
    assert len(thought) >= 10  # the look-ups (every 20 ms here) dropped nothing
    assert not any(isinstance(e, ErrorEvent) for e in events)


@pytest.mark.usefixtures("fast_poll")
def test_the_card_counts_rounds_not_meter_readings(tmp_path: Path, repo: Path) -> None:
    from saddle.events import TaskEvent, TaskPhase
    from saddle.web.tasks import TaskRun, execute

    run = TaskRun(run_id="r1", session_id="s", task="t", time_budget_s=0.3, token_budget=10**6)
    run.answers.put(engine.STOP_AT_LIMIT)  # the time question comes mid-reply
    published: list[Event] = []
    execute(
        run,
        workdir=repo,
        client=Trickle(3.0),
        publish=published.append,
        chat_journal=tmp_path / "chat.jsonl",
        reasoning_effort="low",
        audit=None,
        arm="E",
    )
    meter = [
        e.event for e in published if isinstance(e, TaskEvent) and e.event["kind"] == "run.progress"
    ]
    assert sum(m["partial"] for m in meter) >= 2  # the meter moved during the reply
    assert max(p.round for p in published if isinstance(p, TaskPhase)) == 2  # one round ended
