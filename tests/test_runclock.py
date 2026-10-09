"""The run's clock in plain words (#204).

Contract: an autonomous run states its start once in the system prompt, and
each request whose newest message is a tool result ends that result with one
clock line, the time of day and how long the run has gone; the line is written
into the message, so a later request repeats the earlier messages byte for byte.

Known-bad: a run that says nothing of the time (before #204, the worker's only
clock was "13816s used" in a checkpoint); a second clock line stacked on a
result sent again; a line on a later request only, which changes what an
earlier request sent. Known-good: one line per tool result, the time from the
wall clock and the elapsed time from the run's own budget clock.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from test_auto import Scripted, auto, call, finish, git

from saddle.runclock import (
    CLOCK_OPEN,
    clock_line,
    clock_prompt,
    plain_duration,
    stamp,
    unstamped,
)

EDT = timezone(timedelta(hours=-4), "EDT")
START = datetime(2026, 10, 8, 16, 33, 30, tzinfo=EDT)


def test_a_duration_reads_as_a_person_says_it() -> None:
    assert plain_duration(0) == "0 s"
    assert plain_duration(48) == "48 s"
    assert plain_duration(59.4) == "59 s"
    assert plain_duration(59.6) == "1 min 00 s"  # rounded, never "60 s"
    assert plain_duration(372) == "6 min 12 s"
    assert plain_duration(3599.4) == "59 min 59 s"
    assert plain_duration(3600) == "1 h 00 min"
    assert plain_duration(13_816) == "3 h 50 min"  # the run that read "13816s used"
    assert plain_duration(-5) == "0 s"


def test_the_prompt_says_when_the_run_started_and_what_a_clock_line_is() -> None:
    said = clock_prompt(START)
    assert said.startswith(" This run started at 16:33 EDT on Thursday 8 October 2026. ")
    assert "reading clock" in said
    assert "not part of the tool's output" in said


def test_a_clock_line_names_the_time_and_how_long_the_run_has_gone() -> None:
    now = START + timedelta(hours=4, minutes=8)
    assert clock_line(now, 14_880) == "[clock: 20:41 EDT, 4 h 08 min into this task]"
    assert clock_line(now.replace(tzinfo=None), 30) == "[clock: 20:41, 30 s into this task]"


def tool(content: Any) -> dict[str, Any]:
    return {"role": "tool", "tool_call_id": "c1", "content": content}


def test_a_tool_result_ends_with_one_clock_line_even_when_sent_again() -> None:
    messages = [{"role": "user", "content": "task"}, tool("4 passed")]
    assert stamp(messages, "[clock: 17:00 EDT, 27 min 00 s into this task]")
    assert messages[-1]["content"] == "4 passed\n\n[clock: 17:00 EDT, 27 min 00 s into this task]"
    # The same request sent again (an empty reply): the line is replaced, not stacked.
    assert stamp(messages, "[clock: 17:01 EDT, 28 min 00 s into this task]")
    assert messages[-1]["content"] == "4 passed\n\n[clock: 17:01 EDT, 28 min 00 s into this task]"
    assert unstamped(messages[-1]["content"]) == "4 passed"


def test_only_a_tool_result_is_stamped() -> None:
    for role in ("user", "assistant", "system"):
        messages = [{"role": role, "content": "said"}]
        assert not stamp(messages, "[clock: 17:00 EDT, 1 s into this task]")
        assert messages == [{"role": role, "content": "said"}]
    assert not stamp([], "[clock: 17:00 EDT, 1 s into this task]")


def test_an_empty_result_and_a_result_in_parts_get_one_line() -> None:
    empty = [tool("")]
    stamp(empty, "[clock: 17:00 EDT, 1 s into this task]")
    stamp(empty, "[clock: 17:00 EDT, 2 s into this task]")
    assert empty[-1]["content"] == "[clock: 17:00 EDT, 2 s into this task]"
    assert unstamped(empty[-1]["content"]) == ""
    image = {"type": "image_url", "image_url": {"url": "data:image/png;base64,AA=="}}
    parts = [tool([{"type": "text", "text": "a screenshot"}, image])]
    stamp(parts, "[clock: 17:00 EDT, 1 s into this task]")
    stamp(parts, "[clock: 17:00 EDT, 2 s into this task]")
    assert parts[-1]["content"] == [
        {"type": "text", "text": "a screenshot"},
        image,
        {"type": "text", "text": "[clock: 17:00 EDT, 2 s into this task]"},
    ]


def test_a_line_that_is_not_the_last_one_is_not_taken_for_the_clock() -> None:
    text = "log\n\n[clock: 17:00 EDT, 1 s into this task]\nmore output"
    assert unstamped(text) == text
    assert unstamped("[clock: is a word a tool printed") == "[clock: is a word a tool printed"
    assert unstamped("no clock here") == "no clock here"


def test_a_tools_own_output_that_starts_like_a_clock_line_is_kept() -> None:
    """Known-bad, found by the test above: a one-line result starting `[clock: ` was
    taken for a stamped empty result, and the next stamp replaced the tool's output."""
    said = [tool("[clock: is a word a tool printed")]
    stamp(said, "[clock: 17:00 EDT, 1 s into this task]")
    assert said[-1]["content"] == (
        "[clock: is a word a tool printed\n\n[clock: 17:00 EDT, 1 s into this task]"
    )
    parts = [tool([{"type": "text", "text": "[clock: printed by the tool"}])]
    stamp(parts, "[clock: 17:00 EDT, 1 s into this task]")
    assert parts[-1]["content"][0] == {"type": "text", "text": "[clock: printed by the tool"}
    assert len(parts[-1]["content"]) == 2


class Hours:
    """The run's budget clock and the wall clock, both moved by the test."""

    def __init__(self) -> None:
        self.s = 0.0

    def monotonic(self) -> float:
        return self.s

    def wall(self) -> datetime:
        return START + timedelta(seconds=self.s)


class Slow(Scripted):
    """A model whose replies take `took` seconds each, in turn."""

    def __init__(self, rounds: list[list[Any] | BaseException], hours: Hours, took: list[float]):
        super().__init__(rounds)
        self.hours, self.took = hours, list(took)

    def stream_chat(self, messages: Any, **kwargs: Any) -> Any:
        replies = super().stream_chat(messages, **kwargs)
        self.hours.s += self.took.pop(0) if self.took else 0.0
        return replies


def _repo(root: Path) -> Path:
    root.mkdir()
    (root / "calc.py").write_text("def add(a, b):\n    return a + b\n")
    git(root, "init", "-q", "-b", "main")
    git(root, "add", "-A")
    git(root, "commit", "-q", "-m", "init")
    return root


def test_a_run_tells_its_worker_the_time_on_every_tool_result(tmp_path: Path) -> None:
    """Red before #204: no request carried the time. Each reply here takes an hour
    or two of the test's own clock, so the lines can be read exactly."""
    hours = Hours()
    reads = [[call("read_file", f"r{n}", path="calc.py")] for n in (1, 2)]
    client = Slow([*reads, finish()], hours, took=[3600, 4080])
    result = auto(_repo(tmp_path / "repo"), client, clock=hours.monotonic, wall=hours.wall)
    assert result.outcome == "finished"
    first, second, third = (asked["messages"] for asked in client.asked)
    assert clock_prompt(START) in first[0]["content"]
    assert CLOCK_OPEN not in str(first[-1]["content"])  # the task: no tool result yet
    assert second[-1]["content"].endswith("\n\n[clock: 17:33 EDT, 1 h 00 min into this task]")
    assert third[-1]["content"].endswith("\n\n[clock: 18:41 EDT, 2 h 08 min into this task]")
    # Written into the message, not only sent: the next request repeats it byte for byte.
    assert third[: len(second)] == second
    for request in (second, third):
        for message in request:
            assert str(message.get("content")).count("\n\n" + CLOCK_OPEN) <= 1
