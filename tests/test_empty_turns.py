"""Empty turns: a run whose model stops calling tools is bounded.

The finish refusal cap counts `finish` calls. A model that answers a refused
finish with rounds that call no tool never calls `finish` again, so that cap
never fires; before this bound the run went on, one nudge per round, until
its wall budget ran out. Pinned both ways: consecutive rounds with no tool
call stop the run, `stopped` with a named reason; the occasional empty round
that is followed by work does not.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from saddle.engine import AUTO_NUDGE, FINISH_REFUSED
from saddle.journal import read_spans
from tests.test_feed import (  # noqa: F401
    EDIT_COMMENT,
    FINISH,
    FakeAuditor,
    call,
    repo,
    run,
    sidecar,
)

STOP = "no tool call in 3 consecutive rounds"
WALL_S = 3600.0
ROUND_S = 10.0


class EmptyAfter:
    """Replays `script`, then answers every request with an empty turn.

    Each request costs ROUND_S on the injected clock, so a run that is never
    stopped reaches the wall budget in WALL_S / ROUND_S requests, not hours.
    """

    def __init__(self, script: list[list[Any]]) -> None:
        self.script = list(script)
        self.now = 0.0
        self.asked: list[list[dict[str, Any]]] = []

    def clock(self) -> float:
        return self.now

    def stream_chat(self, messages: Any, **kwargs: Any) -> Any:
        self.now += ROUND_S
        self.asked.append([dict(m) for m in messages])
        return iter(self.script.pop(0) if self.script else [])


def test_empty_turns_after_a_refused_finish_stop_the_run_not_the_wall(repo: Path) -> None:  # noqa: F811
    client = EmptyAfter([[EDIT_COMMENT], [FINISH]])
    result, _ = run(
        repo,
        client,
        "E+A+F",
        auditor=FakeAuditor(),
        time_budget_s=WALL_S,
        clock=client.clock,
        finish_refusal_cap=10,
    )
    assert (result.outcome, result.reason) == ("stopped", STOP)
    # the edit round, the refused finish, then three empty rounds
    assert len(client.asked) == 5
    assert client.now < WALL_S / 10
    finishes = [s.detail for s in read_spans(result.journal) if s.argv[:1] == ["finish"]]
    assert len(finishes) == 1
    assert finishes[0].startswith(FINISH_REFUSED)
    # the first two empty rounds were nudged; the third ends the run
    nudges = [m for m in client.asked[-1] if m == {"role": "user", "content": AUTO_NUDGE}]
    assert len(nudges) == 2
    record = sidecar(result)
    assert (record["outcome"], record["reason"]) == ("stopped", STOP)
    assert record["finish_refusals"] == 1
    span = [s for s in read_spans(result.journal) if s.name.startswith("auto:")][-1]
    assert span.argv == ["auto:stopped"]
    assert span.detail.startswith(f"stopped: {STOP};")


def test_empty_turns_from_the_start_stop_the_run_too(repo: Path) -> None:  # noqa: F811
    client = EmptyAfter([])
    result, _ = run(repo, client, "E", time_budget_s=WALL_S, clock=client.clock)
    assert (result.outcome, result.reason) == ("stopped", STOP)
    assert len(client.asked) == 3


def test_occasional_empty_turns_that_then_continue_are_not_stopped(repo: Path) -> None:  # noqa: F811
    """Two empty rounds, work, two more, work, finish: never three in a row,
    four in all, so only a count of consecutive rounds lets this finish."""
    fix = call("edit_file", "e2", path="calc.py", old="a - b", new="a + b")
    client = EmptyAfter([[], [], [EDIT_COMMENT], [], [], [fix], [FINISH]])
    result, _ = run(repo, client, "E+A+F", auditor=FakeAuditor(), clock=client.clock)
    assert (result.outcome, result.reason) == ("finished", "finish called")
    assert len(client.asked) == 7
