"""The pace of requests to a SearXNG (`searx.SearxLimiter`).

Contract: saddle never sends a SearXNG two requests closer than
`SEARX_MIN_INTERVAL_S`, nor more than `SEARX_PER_MINUTE` in any 60-second
window; a request over the pace waits, it is never refused. One limiter serves
the whole process: the reader's searches, the Brave fallback's and the
readiness probe, across sessions.

Known-bad: two searches sent back to back with no gap; a 21st request inside a
minute. Known-good: the second waits exactly what is left of the interval; the
21st waits for the window and is then sent.
"""

from __future__ import annotations

import itertools
import random
import threading
from pathlib import Path

import httpx

from saddle import searx
from saddle.brave import BraveSearch, Outcome
from saddle.mcpclient import Approvals
from saddle.research import ReaderGate, Researcher
from saddle.searx import (
    SEARX_MIN_INTERVAL_S,
    SEARX_PER_MINUTE,
    SearxLimiter,
    reachable,
    shared_limiter,
)


class Clock:
    """A clock that only the limiter's sleeps (and the test) move."""

    def __init__(self) -> None:
        self.now = 0.0
        self.sleeps: list[float] = []

    def __call__(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.now += seconds


def limiter(clock: Clock) -> SearxLimiter:
    return SearxLimiter(clock=clock, sleep=clock.sleep)


def recording(clock: Clock, sent: list[float]) -> httpx.Client:
    """A SearXNG that notes the clock at each request it receives."""

    def answer(request: httpx.Request) -> httpx.Response:
        sent.append(clock.now)
        return httpx.Response(200, json={"results": []})

    return httpx.Client(transport=httpx.MockTransport(answer))


def researcher(tmp_path: Path, http: httpx.Client, pace: SearxLimiter | None) -> Researcher:
    return Researcher({}, Approvals(tmp_path / "a.json"), tmp_path, http=http, searx_limiter=pace)


def test_two_searches_back_to_back_are_never_sent_closer_than_the_interval(
    tmp_path: Path,
) -> None:
    clock, sent = Clock(), list[float]()
    reader = researcher(tmp_path, recording(clock, sent), limiter(clock))
    reader._search("first query", ReaderGate(), None)
    reader._search("second query", ReaderGate(), None)
    assert sent == [0.0, SEARX_MIN_INTERVAL_S]  # known-bad: [0.0, 0.0]
    assert clock.sleeps == [SEARX_MIN_INTERVAL_S]


def test_the_second_waits_exactly_what_is_left_of_the_interval(tmp_path: Path) -> None:
    clock, sent = Clock(), list[float]()
    reader = researcher(tmp_path, recording(clock, sent), limiter(clock))
    reader._search("first query", ReaderGate(), None)
    clock.now = 0.5
    reader._search("second query", ReaderGate(), None)
    assert clock.sleeps == [1.5]
    assert sent == [0.0, 2.0]
    clock.now = 10.0  # well after the interval: no wait at all
    reader._search("third query", ReaderGate(), None)
    assert clock.sleeps == [1.5]
    assert sent[-1] == 10.0


def test_the_twenty_first_request_in_a_minute_waits_for_the_window() -> None:
    clock = Clock()
    pace = limiter(clock)
    times: list[float] = []
    for _ in range(SEARX_PER_MINUTE):
        pace.wait()
        times.append(clock.now)
    assert times[-1] == (SEARX_PER_MINUTE - 1) * SEARX_MIN_INTERVAL_S  # 38 s: the interval alone
    assert pace.wait() == 60.0 - times[-1]  # waits for the window, is not refused
    assert clock.now == 60.0
    pace.wait()  # the window has moved on: the interval governs again
    assert clock.now == 62.0


def test_no_minute_ever_holds_more_than_the_limit_and_no_gap_is_short() -> None:
    clock = Clock()
    pace = limiter(clock)
    noise = random.Random(7)
    sent: list[float] = []
    for _ in range(120):
        clock.now += noise.choice([0.0, 0.25, 0.5, 1.75, 3.0])  # exact in binary
        pace.wait()
        sent.append(clock.now)
    gaps = [b - a for a, b in itertools.pairwise(sent)]
    assert min(gaps) >= SEARX_MIN_INTERVAL_S
    busiest = max(sum(1 for t in sent if start <= t < start + 60.0) for start in sent)
    assert busiest == SEARX_PER_MINUTE  # the limit is reached, and never passed


def test_a_sleep_that_ends_early_still_records_the_paced_time() -> None:
    clock = Clock()
    pace = SearxLimiter(clock=clock, sleep=lambda s: None)
    pace.wait()
    pace.wait()
    assert list(pace.sent) == [0.0, SEARX_MIN_INTERVAL_S]


def test_two_sessions_without_their_own_limiter_share_the_process_pace(tmp_path: Path) -> None:
    sent: list[float] = []
    shared = shared_limiter()
    clock = Clock()
    clock.now = shared.clock()
    first = researcher(tmp_path, recording(clock, sent), None)
    second = researcher(tmp_path, recording(clock, sent), None)
    first._search("one query", ReaderGate(), None)
    second._search("another query", ReaderGate(), None)
    assert len(shared.sent) == 2
    assert shared.sent[1] - shared.sent[0] == SEARX_MIN_INTERVAL_S


class Spent(BraveSearch):
    """A Brave whose budget is used up, so `search` falls back to SearXNG."""

    def search(self, query: str) -> Outcome:
        return Outcome(exhausted=True, refill="~5 min")


def test_the_brave_fallback_keeps_the_same_pace(tmp_path: Path) -> None:
    clock, sent = Clock(), list[float]()
    pace = limiter(clock)
    reader = researcher(tmp_path, recording(clock, sent), pace)
    reader.brave = Spent("k", tmp_path / "brave.json")
    first = reader._search("first query", ReaderGate(), None)
    reader._search("second query", ReaderGate(), None)
    assert first.startswith("(budget used up: free search")
    assert sent == [0.0, SEARX_MIN_INTERVAL_S]


def test_the_readiness_probe_keeps_the_pace(tmp_path: Path) -> None:
    clock, sent = Clock(), list[float]()
    pace = limiter(clock)
    http = recording(clock, sent)
    assert reachable("http://search.test", http, pace) is None
    assert reachable("http://search.test", http, pace) is None
    assert sent == [0.0, SEARX_MIN_INTERVAL_S]
    assert reachable("http://search.test", http) is None  # the process's own by default
    assert len(searx.shared_limiter().sent) == 1


def test_concurrent_requests_take_turns() -> None:
    """While one session waits its turn, another cannot slip a request in."""
    clock = Clock()
    pace = limiter(clock)
    pace.wait()
    other = threading.Thread(target=pace.wait)
    seen: list[bool] = []

    def sleep(seconds: float) -> None:
        pace.sleep = clock.sleep  # only the first sleep starts the other session
        other.start()
        other.join(timeout=0.5)
        seen.append(other.is_alive())  # still waiting for the lock
        clock.sleep(seconds)

    pace.sleep = sleep
    pace.wait()
    other.join(timeout=5)
    assert seen == [True]
    assert list(pace.sent) == [0.0, 2.0, 4.0]
