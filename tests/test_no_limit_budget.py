"""A non-positive RunBudget is no limit (engine.NO_LIMIT).

Contract: `time_s <= 0` and `tokens <= 0` each mean "no limit". On that
dimension `exhausted()` and `near()` return None however much is spent, and
`time_left()`/`remaining_tokens()` report an unreachable-but-finite headroom
so a caller can still `int()` them and compare. A *positive* budget on that
dimension runs out exactly as before.

Known-good (no limit): a run far past any real spend is neither near nor
exhausted. Known-bad (a real cap): a positive budget still reaches its 80%
mark and still runs out. Both halves, so a mutant that drops either the
`> NO_LIMIT` guard or the sentinel is killed.
"""

from __future__ import annotations

from saddle.engine import (
    _UNBOUNDED_S,
    _UNBOUNDED_TOKENS,
    NO_LIMIT,
    RunBudget,
)


class Clock:
    """A hand-wound monotonic clock, so a test can jump time without waiting."""

    def __init__(self) -> None:
        self.t = 0.0

    def __call__(self) -> float:
        return self.t


def test_no_time_limit_is_never_near_or_exhausted() -> None:
    clock = Clock()
    budget = RunBudget(time_s=NO_LIMIT, tokens=NO_LIMIT, clock=clock)
    budget.start()
    clock.t = 10_000.0  # far past any real run's wall time
    assert budget.exhausted() is None
    assert budget.near(0.8) is None
    assert budget.time_left() == _UNBOUNDED_S


def test_no_token_limit_is_never_near_or_exhausted() -> None:
    budget = RunBudget(time_s=NO_LIMIT, tokens=NO_LIMIT)
    budget.spent_tokens = 10_000_000  # far past any real run's tokens
    assert budget.exhausted() is None
    assert budget.near(0.8) is None
    assert budget.remaining_tokens() == _UNBOUNDED_TOKENS


def test_a_positive_time_budget_still_runs_out() -> None:
    clock = Clock()
    budget = RunBudget(time_s=100.0, tokens=NO_LIMIT, clock=clock)
    budget.start()
    clock.t = 80.0
    assert budget.near(0.8) == "time"  # the 80% mark, positive branch
    assert budget.exhausted() is None
    assert budget.time_left() == 20.0
    clock.t = 100.0
    assert budget.exhausted() == "time budget exhausted: 100s of 100s spent"


def test_a_positive_token_budget_still_runs_out() -> None:
    budget = RunBudget(time_s=NO_LIMIT, tokens=1000)
    budget.spent_tokens = 800
    assert budget.near(0.8) == "token"  # the 80% mark, positive branch
    assert budget.exhausted() is None
    assert budget.remaining_tokens() == 200
    budget.spent_tokens = 1000
    spent = budget.exhausted()
    assert spent is not None
    assert spent.startswith("token budget exhausted")
