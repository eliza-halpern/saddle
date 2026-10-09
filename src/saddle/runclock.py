"""The run's clock in plain words, as the worker reads it (#204).

The worker has no clock of its own. Before this, the only time an autonomous
run saw was the budget line's raw seconds ("13816s used, no time limit") and
how long a check took. In #132's second run the worker mentioned the elapsed
time four times in 3.9 hours of reasoning; once it used it well ("I need to
converge; this run is already ~2 hours").

So a run says when it started, once, in its system prompt. That text is fixed
for the run, so every request begins with the same bytes. Each tool result the
worker reads then ends with one clock line: the time of day and how long the
run has gone. The line is written into that message, so a later request
repeats it byte for byte, and the prefix the server cached is unchanged. It is
information, never a limit: an uncapped run stays uncapped (`engine.NO_LIMIT`).
"""

from __future__ import annotations

import re
from collections.abc import Callable
from datetime import datetime
from typing import Any, Final

CLOCK_OPEN: Final = "[clock: "
"""How every clock line starts."""

CLOCK_LINE: Final = re.compile(r"\[clock: \d{2}:\d{2}(?: [^,\]\n]+)?, [^\]\n]+ into this task\]")
"""A whole clock line as `clock_line` writes it. Only a line this matches whole is
taken off: a tool's own output that merely starts with `[clock: ` is kept."""

Wall = Callable[[], datetime]
"""A wall clock: the time of day, in a time zone."""


def local_now() -> datetime:
    """The wall clock in the machine's own time zone."""
    return datetime.now().astimezone()


def plain_duration(seconds: float) -> str:
    """`seconds` as a person says it: `48 s`, `6 min 12 s`, `4 h 08 min`."""
    whole = max(0, round(seconds))
    if whole < 60:
        return f"{whole} s"
    if whole < 3600:
        return f"{whole // 60} min {whole % 60:02d} s"
    return f"{whole // 3600} h {whole % 3600 // 60:02d} min"


def _time(at: datetime) -> str:
    """`20:41 EDT`: the time of day and, when `at` has one, its zone's name."""
    zone = at.tzname()
    return f"{at:%H:%M} {zone}" if zone else f"{at:%H:%M}"


def clock_prompt(started: datetime) -> str:
    """The system prompt's clock sentences: when the run started, and what a
    clock line is, so the worker never reads one as its tool's output."""
    return (
        f" This run started at {_time(started)} on {started:%A} {started.day} "
        f"{started:%B %Y}. Each tool result ends with a line saddle adds, in square "
        "brackets, reading clock, the time now and how long this run has been going; "
        "it is not part of the tool's output."
    )


def clock_line(now: datetime, elapsed_s: float) -> str:
    """The line a tool result ends with: `[clock: 20:41 EDT, 4 h 08 min into this task]`."""
    return f"{CLOCK_OPEN}{_time(now)}, {plain_duration(elapsed_s)} into this task]"


def _is_clock(text: str) -> bool:
    return CLOCK_LINE.fullmatch(text) is not None


def unstamped(text: str) -> str:
    """`text` without the clock line `stamp` ended it with, if it has one."""
    head, sep, tail = text.rpartition("\n\n")
    if sep and _is_clock(tail):
        return head
    return "" if _is_clock(text) else text  # an empty result, stamped, is the line alone


def stamp(messages: list[dict[str, Any]], line: str) -> bool:
    """End the newest message with `line` when it is a tool result; say whether it was.

    A result stamped already (the same request sent again after an empty
    reply) has its clock line replaced, never a second one added. A result
    given as parts (a screenshot beside its text) gets the line as its last
    text part."""
    if not messages or messages[-1].get("role") != "tool":
        return False
    newest = messages[-1]
    content = newest.get("content")
    if isinstance(content, list):
        kept = [
            part
            for part in content
            if not (part.get("type") == "text" and _is_clock(str(part.get("text", ""))))
        ]
        newest["content"] = [*kept, {"type": "text", "text": line}]
    else:
        said = unstamped(str(content or ""))
        newest["content"] = f"{said}\n\n{line}" if said else line
    return True
