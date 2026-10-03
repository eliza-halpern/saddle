"""Brave search under a budget (#93): bucket, cache, fallback, key handling.

Contract: with a Brave key the reader's `search` uses Brave; searches accrue at
quota / hours-in-the-month per hour with no cap but the month's remaining quota, a
search costs one, a cached query costs nothing, and a used-up budget falls back
labelled (or fails named, with the refill time); no number of the budget and no
part of the key ever reaches the model or a result.

All with a fake HTTP transport and a fake clock; nothing reads the person's key.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx
import pytest
from test_mcp_config import BOTH
from test_research import Rig, Scripted, needs_bwrap, tool

from saddle.brave import (
    BraveKeyError,
    BraveSearch,
    load_key,
    normalise,
)
from saddle.capabilities import Switches, status
from saddle.capabilities import run as run_capabilities
from saddle.mcpclient import Approvals
from saddle.research import SEARCH_SCHEMA, ReaderGate, Researcher
from saddle.searx import run_search
from saddle.tools import ToolContext, attach_mcp, scope_turn

KEY = "brv-" + "fake" + "-key-" + "7431"
OCT = datetime(2026, 10, 1, tzinfo=UTC).timestamp()
HOUR = 3600.0
R_OCT = 1000 / 744  # 31 days


class Clock:
    def __init__(self, at: float = OCT) -> None:
        self.t = at
        self.slept: list[float] = []

    def __call__(self) -> float:
        return self.t

    def sleep(self, seconds: float) -> None:
        self.slept.append(seconds)
        self.t += seconds


class Fake:
    """Brave's API: records requests, answers from `reply`."""

    def __init__(self) -> None:
        self.requests: list[httpx.Request] = []
        self.status = 200
        self.headers: dict[str, str] = {}
        self.body: Any = None
        self.raises: Exception | None = None

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if self.raises is not None:
            raise self.raises
        body = self.body
        if body is None:
            query = request.url.params["q"]
            body = {
                "web": {
                    "results": [
                        {
                            "title": f"Title {query}",
                            "url": f"https://docs.example/{len(self.requests)}",
                            "description": "desc",
                        }
                    ]
                }
            }
        return httpx.Response(self.status, headers=self.headers, json=body)


def make(tmp: Path, clock: Clock, fake: Fake, quota: int = 1000) -> BraveSearch:
    return BraveSearch(
        KEY,
        tmp / "state.json",
        quota,
        httpx.Client(transport=httpx.MockTransport(fake)),
        clock,
        clock.sleep,
    )


def write_state(tmp: Path, **over: Any) -> None:
    state: dict[str, Any] = {
        "month": "2026-10",
        "tokens": 0.0,
        "stamp": OCT,
        "used": 0,
        "header_left": None,
        "blocked_until": 0.0,
        "last_request": 0.0,
        "cache": {},
    }
    state.update(over)
    (tmp / "state.json").write_text(json.dumps(state))


# -- bucket math ----------------------------------------------------------------------


@pytest.mark.parametrize(
    ("moment", "hours"),
    [
        (datetime(2026, 10, 15, tzinfo=UTC), 744),
        (datetime(2026, 11, 15, tzinfo=UTC), 720),
        (datetime(2027, 2, 15, tzinfo=UTC), 672),
        (datetime(2028, 2, 15, tzinfo=UTC), 696),
        (datetime(2026, 12, 31, 23, tzinfo=UTC), 744),
    ],
)
def test_the_rate_is_the_quota_over_the_hours_of_that_calendar_month(
    tmp_path: Path, moment: datetime, hours: int
) -> None:
    brave = make(tmp_path, Clock(), Fake())
    assert brave.rate(moment.timestamp()) == pytest.approx(1000 / hours)
    assert brave.rate(moment.timestamp()) != pytest.approx(1000 / 720) or hours == 720


@pytest.mark.parametrize("idle", [3, 100, 500])
def test_unused_searches_carry_forward_with_no_five_hour_cap(tmp_path: Path, idle: int) -> None:
    clock = Clock()
    brave = make(tmp_path, clock, Fake())
    assert brave.balance()[0] == 1  # a new person's first search needs no wait
    clock.t += idle * HOUR
    available, left, _ = brave.balance()
    assert left == 1000
    assert available == int(1 + idle * R_OCT)
    assert available > 6 or idle == 3  # past the old five-hour capacity


def test_the_balance_never_exceeds_the_months_remaining_quota(tmp_path: Path) -> None:
    clock = Clock(OCT + 700 * HOUR)
    write_state(tmp_path, used=600)
    brave = make(tmp_path, clock, Fake())
    available, left, _ = brave.balance()
    assert (available, left) == (400, 400)  # min(700 h * r, 1000 - 600)


def test_a_lower_remaining_count_from_brave_wins(tmp_path: Path) -> None:
    clock = Clock(OCT + 700 * HOUR)
    write_state(tmp_path, header_left=50, used=10)
    brave = make(tmp_path, clock, Fake())
    assert brave.balance()[:2] == (50, 50)


def test_a_new_calendar_month_starts_empty_and_carries_nothing_over(tmp_path: Path) -> None:
    clock = Clock(datetime(2026, 11, 1, 0, 30, tzinfo=UTC).timestamp())
    write_state(tmp_path, month="2026-10", tokens=500.0, used=900, header_left=3)
    brave = make(tmp_path, clock, Fake())
    available, left, wait = brave.balance()
    assert (available, left) == (0, 1000)
    assert wait == pytest.approx((1 - 0.5 * 1000 / 720) / (1000 / 720) * HOUR)
    clock.t += 2.5 * HOUR  # 3 h into November at 1000/720 an hour
    assert brave.balance()[0] == 4


def test_the_monthly_counter_stops_searches_at_the_quota(tmp_path: Path) -> None:
    clock = Clock(OCT + 700 * HOUR)
    fake = Fake()
    write_state(tmp_path, tokens=5.0, used=1000)
    found = make(tmp_path, clock, fake).search("anything")
    assert found.exhausted
    assert fake.requests == []
    assert found.refill.startswith("~")  # the start of next month, in days or hours


# -- cost, cache, pacing ------------------------------------------------------------


def test_a_search_costs_one_and_a_normalised_repeat_is_free_and_cached(tmp_path: Path) -> None:
    clock, fake = Clock(OCT + 10 * HOUR), Fake()
    brave = make(tmp_path, clock, fake)
    before = brave.balance()
    first = brave.search("Python  asyncio, Queue!")
    assert not first.cached
    assert [r["url"] for r in first.rows] == ["https://docs.example/1"]
    after = brave.balance()
    assert after[1] == before[1] - 1
    assert after[0] == before[0] - 1
    again = brave.search("  python ASYNCIO queue ")
    assert again.cached
    assert again.rows == first.rows
    assert len(fake.requests) == 1
    assert brave.balance() == after  # a hit cost nothing


def test_a_cached_answer_is_served_even_when_the_budget_is_used_up(tmp_path: Path) -> None:
    clock, fake = Clock(OCT + 10 * HOUR), Fake()
    brave = make(tmp_path, clock, fake)
    brave.search("known")
    write_state(
        tmp_path,
        used=1000,
        cache=json.loads((tmp_path / "state.json").read_text())["cache"],
    )
    assert brave.search("KNOWN").cached


def test_a_cache_entry_older_than_seven_days_is_searched_again(tmp_path: Path) -> None:
    clock, fake = Clock(OCT + 10 * HOUR), Fake()
    brave = make(tmp_path, clock, fake)
    brave.search("old question")
    clock.t += 6.9 * 24 * HOUR
    assert brave.search("old question").cached
    clock.t += 0.2 * 24 * HOUR
    assert not brave.search("old question").cached
    assert len(fake.requests) == 2


def test_normalise_ignores_case_whitespace_and_punctuation() -> None:
    assert normalise(" Hello,   WORLD?! ") == normalise("hello world") == "hello world"
    assert normalise("a-b") != normalise("ab")
    assert normalise("???") == ""


def test_an_empty_query_is_a_named_error_and_costs_nothing(tmp_path: Path) -> None:
    fake = Fake()
    found = make(tmp_path, Clock(), fake).search(" ?! ")
    assert found.error == "search needs a query"
    assert fake.requests == []


def test_requests_are_at_least_one_second_apart(tmp_path: Path) -> None:
    clock, fake = Clock(OCT + 10 * HOUR), Fake()
    write_state(tmp_path, tokens=10.0, stamp=clock.t)
    brave = make(tmp_path, clock, fake)
    brave.search("one")
    start = clock.t
    brave.search("two")
    assert clock.t - start >= 1.0
    assert clock.slept
    assert clock.slept[-1] == pytest.approx(1.0)


def test_the_request_names_the_key_in_a_header_and_never_in_the_url(tmp_path: Path) -> None:
    clock, fake = Clock(OCT + 10 * HOUR), Fake()
    make(tmp_path, clock, fake).search("one two")
    (request,) = fake.requests
    assert request.headers["X-Subscription-Token"] == KEY
    assert request.url.params["q"] == "one two"
    assert KEY not in str(request.url)


def test_brave_headers_that_report_less_remaining_win_and_a_malformed_one_is_ignored(
    tmp_path: Path,
) -> None:
    clock, fake = Clock(OCT + 300 * HOUR), Fake()
    write_state(tmp_path, tokens=400.0, stamp=clock.t)
    brave = make(tmp_path, clock, fake)
    fake.headers = {"X-RateLimit-Remaining": "1, 2", "X-RateLimit-Limit": "1, 1000"}
    brave.search("first")
    assert brave.balance()[:2] == (2, 2)  # 300 h would allow ~400
    fake.headers = {"X-RateLimit-Remaining": "garbage"}
    clock.t += 1
    brave.search("second")
    saved = json.loads((tmp_path / "state.json").read_text())
    assert (saved["used"], saved["header_left"]) == (2, 2)  # garbage changed nothing


def test_a_metered_plan_with_no_monthly_quota_is_governed_by_saddles_own_budget(
    tmp_path: Path,
) -> None:
    """A live key on a metered plan answered `X-RateLimit-Limit: 50, 0` and
    `X-RateLimit-Remaining: 49, 0`: no monthly quota, not a spent one. Read as
    spent, one successful search left saddle believing the month was over.
    Known-good: such headers leave saddle's own budget in charge, and a 429 under
    them is the per-second limit. Known-bad stays refused: a real quota of 1000
    with 0 left (covered above, and here)."""
    clock, fake = Clock(OCT + 300 * HOUR), Fake()
    write_state(tmp_path, tokens=400.0, stamp=clock.t)
    brave = make(tmp_path, clock, fake)
    fake.headers = {"X-RateLimit-Limit": "50, 0", "X-RateLimit-Remaining": "49, 0"}
    assert brave.search("first").rows
    clock.t += 2
    assert brave.search("second").rows
    saved = json.loads((tmp_path / "state.json").read_text())
    assert saved["header_left"] is None
    fake.status = 429
    fake.headers = {
        "X-RateLimit-Limit": "50, 0",
        "X-RateLimit-Remaining": "0, 0",
        "X-RateLimit-Reset": "1, 2423154",
    }
    clock.t += 2
    assert brave.search("third").refill == "~1 min"  # the second, not the month
    fake.status = 200
    fake.headers = {"X-RateLimit-Limit": "50, 1000", "X-RateLimit-Remaining": "49, 0"}
    clock.t += 2
    brave.search("fourth")
    clock.t += 2
    assert brave.search("fifth").exhausted  # a real quota, spent


# -- failures -----------------------------------------------------------------------


@pytest.mark.parametrize("code", [401, 403])
def test_an_invalid_key_is_named_and_never_echoed(tmp_path: Path, code: int) -> None:
    clock, fake = Clock(OCT + 10 * HOUR), Fake()
    fake.status = code
    fake.body = {"error": {"detail": f"bad token {KEY}"}}
    before = make(tmp_path, clock, fake).balance()
    found = make(tmp_path, clock, fake).search("q")
    assert "key was refused" in found.error
    assert "invalid" in found.error
    assert KEY not in found.error
    assert not found.exhausted
    assert make(tmp_path, clock, fake).balance()[1] == before[1]  # a refusal is not charged


def test_a_429_marks_the_window_exhausted_until_the_reset_brave_reports(tmp_path: Path) -> None:
    clock, fake = Clock(OCT + 10 * HOUR), Fake()
    brave = make(tmp_path, clock, fake)
    fake.status = 429
    fake.headers = {"X-RateLimit-Remaining": "0, 500", "X-RateLimit-Reset": "1, 2700"}
    found = brave.search("q")
    assert found.exhausted
    assert found.refill == "~1 min"  # the per-second reset: one second away, 500 left
    # not a monthly exhaustion: bucket empty, blocked for the reported second only
    fake.status = 200
    clock.t += 2
    assert not brave.search("another").exhausted


def test_a_429_with_the_month_spent_waits_for_the_monthly_reset(tmp_path: Path) -> None:
    clock, fake = Clock(OCT + 10 * HOUR), Fake()
    brave = make(tmp_path, clock, fake)
    fake.status = 429
    fake.headers = {"X-RateLimit-Remaining": "0, 0", "X-RateLimit-Reset": "1, 86400"}
    assert brave.search("q").refill == "~24 h"
    fake.status = 200
    n = len(fake.requests)
    again = brave.search("other")
    assert again.exhausted
    assert len(fake.requests) == n  # no call while it is blocked


def test_a_transport_failure_that_echoes_the_key_is_redacted(tmp_path: Path) -> None:
    clock, fake = Clock(OCT + 10 * HOUR), Fake()
    fake.raises = httpx.ConnectError(f"refused with X-Subscription-Token: {KEY}")
    found = make(tmp_path, clock, fake).search("q")
    assert "did not answer" in found.error
    assert KEY not in found.error
    assert "[key]" in found.error


def test_a_bad_answer_is_named_not_empty(tmp_path: Path) -> None:
    clock, fake = Clock(OCT + 10 * HOUR), Fake()
    fake.status = 500
    assert "answered badly" in make(tmp_path, clock, fake).search("q").error
    fake.status = 200
    fake.body = {"web": {"results": "nope"}}
    assert "no results list" in make(tmp_path, clock, fake).search("q2").error


# -- the key file ---------------------------------------------------------------------


def test_the_key_file_must_be_private(tmp_path: Path) -> None:
    path = tmp_path / "brave.env"
    path.write_text(f"BRAVE_API_KEY={KEY}\n")
    for mode in (0o644, 0o640, 0o604):
        path.chmod(mode)
        with pytest.raises(BraveKeyError) as refused:
            load_key({"SADDLE_BRAVE_ENV_FILE": str(path)})
        assert "chmod 600" in str(refused.value)
        assert KEY not in str(refused.value)
    path.chmod(0o600)
    assert load_key({"SADDLE_BRAVE_ENV_FILE": str(path)}) == KEY


def test_the_key_comes_from_the_environment_first_and_is_none_when_absent(tmp_path: Path) -> None:
    path = tmp_path / "brave.env"
    path.write_text("export BRAVE_API_KEY='file-" + "value'\n")
    path.chmod(0o600)
    env = {"SADDLE_BRAVE_ENV_FILE": str(path)}
    assert load_key(env) == "file-value"
    assert load_key({**env, "SADDLE_BRAVE_API_KEY": KEY}) == KEY
    assert load_key({"SADDLE_BRAVE_ENV_FILE": str(tmp_path / "missing")}) is None
    path.write_text("# nothing\nOTHER=1\n")
    with pytest.raises(BraveKeyError, match="no BRAVE_API_KEY"):
        load_key(env)


def test_from_env_reads_the_state_path_and_quota_and_none_without_a_key(tmp_path: Path) -> None:
    assert BraveSearch.from_env({"SADDLE_BRAVE_ENV_FILE": str(tmp_path / "none")}) is None
    env = {
        "SADDLE_BRAVE_API_KEY": KEY,
        "SADDLE_BRAVE_STATE": str(tmp_path / "s.json"),
        "SADDLE_BRAVE_MONTHLY_QUOTA": "200",
    }
    brave = BraveSearch.from_env(env)
    assert brave is not None
    assert (brave.state_path, brave.quota) == (tmp_path / "s.json", 200)
    junk = BraveSearch.from_env({**env, "SADDLE_BRAVE_MONTHLY_QUOTA": "lots"})
    assert junk is not None
    assert junk.quota == 1000


def test_the_state_file_is_owner_only_and_holds_no_key(tmp_path: Path) -> None:
    clock, fake = Clock(OCT + 10 * HOUR), Fake()
    make(tmp_path, clock, fake).search("q")
    path = tmp_path / "state.json"
    assert path.stat().st_mode & 0o077 == 0
    assert KEY not in path.read_text()


# -- the reader: provider, fallback, labels -----------------------------------------------


class Reader(Researcher):
    def unavailable(self) -> str | None:
        return None


def searx_client(down: bool = False) -> httpx.Client:
    def handler(request: httpx.Request) -> httpx.Response:
        if down:
            refusal = "refused"
            raise httpx.ConnectError(refusal)
        return httpx.Response(
            200,
            json={"results": [{"title": "Free", "url": "https://free.example/a", "content": "c"}]},
        )

    return httpx.Client(transport=httpx.MockTransport(handler))


def reader(
    tmp: Path, clock: Clock, fake: Fake, *, searx_down: bool = False, **state: Any
) -> Reader:
    if state:
        write_state(tmp, **state)
    return Reader(
        {},
        Approvals(),
        tmp / "dl",
        search_enabled=True,
        brave=make(tmp, clock, fake),
        http=searx_client(searx_down),
        search_url="http://search.test",
    )


def test_search_uses_brave_when_a_key_is_configured_and_labels_a_cached_repeat(
    tmp_path: Path,
) -> None:
    clock, fake = Clock(OCT + 10 * HOUR), Fake()
    researcher = reader(tmp_path, clock, fake)
    gate = ReaderGate()
    first = researcher._search("pytest fixtures", gate, None)
    assert first.startswith("Title pytest fixtures\nhttps://docs.example/1")
    assert "(cached)" not in first
    assert gate.check("fetch", {"url": "https://docs.example/1"}) is None  # a search result
    second = researcher._search("PYTEST, fixtures", ReaderGate(), None)
    assert second.startswith("(cached)\n")
    assert len(fake.requests) == 1


def test_without_a_key_search_is_the_searxng_as_before(tmp_path: Path) -> None:
    researcher = Reader({}, Approvals(), tmp_path / "dl", search_enabled=True, http=searx_client())
    out = researcher._search("q", ReaderGate(), None)
    assert out.startswith("Free\nhttps://free.example/a")
    assert "budget" not in out


def test_a_used_up_budget_falls_back_to_the_free_search_and_says_so(tmp_path: Path) -> None:
    clock, fake = Clock(OCT + 1 * HOUR), Fake()
    researcher = reader(tmp_path, clock, fake, tokens=0.2, stamp=OCT + 1 * HOUR)
    out = researcher._search("q", ReaderGate(), None)
    assert out.startswith("(budget used up: free search, lower quality; Brave refills in ~")
    assert "Free\nhttps://free.example/a" in out
    assert fake.requests == []


def test_a_used_up_budget_with_no_free_search_is_a_named_failure_with_the_refill_time(
    tmp_path: Path,
) -> None:
    clock, fake = Clock(OCT + 1 * HOUR), Fake()
    researcher = reader(tmp_path, clock, fake, searx_down=True, tokens=0.2, stamp=OCT + 1 * HOUR)
    out = researcher._search("q", ReaderGate(), None)
    assert out.startswith("error: the Brave search budget is used up (refills in ~")
    assert "free search is not available" in out
    assert "no results" not in out


def test_a_brave_failure_is_an_error_result_without_the_key(tmp_path: Path) -> None:
    clock, fake = Clock(OCT + 10 * HOUR), Fake()
    fake.status = 401
    researcher = reader(tmp_path, clock, fake)
    out = researcher._search("q", ReaderGate(), None)
    assert out.startswith("error: the Brave API key was refused")
    assert KEY not in out


def test_a_brave_search_with_no_hits_says_so_after_charging_one(tmp_path: Path) -> None:
    clock, fake = Clock(OCT + 10 * HOUR), Fake()
    fake.body = {"query": {}}
    researcher = reader(tmp_path, clock, fake)
    assert researcher._search("zzzz", ReaderGate(), None) == "(the search returned no results)"


# -- what the model is shown: no numbers -------------------------------------------------


def distinctive(tmp: Path) -> tuple[Clock, Fake]:
    """437 left this month and 5 searches in hand: neither may reach the model."""
    clock = Clock(OCT + 10 * HOUR)
    write_state(tmp, tokens=5.0, stamp=clock.t, used=563)
    return clock, Fake()


def test_the_reader_prompt_carries_the_fixed_guidance_and_no_count(tmp_path: Path) -> None:
    clock, fake = distinctive(tmp_path)
    researcher = reader(tmp_path, clock, fake)
    researcher.client = model = Scripted([[tool("report", kind="none", reason="not_found")]])
    researcher._loop("q", "summary", [SEARCH_SCHEMA], ReaderGate(), [])
    prompt = model.asked[0]["messages"][0]["content"]
    assert "scarce" in prompt
    for rule in ("one specific query", "read result pages", "never repeat", "already know"):
        assert rule in prompt
    for digits in ("437", "563", "1000", "5 searches", "6.7", "1.3"):
        assert digits not in prompt
    # a reader with no search tool gets the plain prompt
    plain = Scripted([[tool("report", kind="none", reason="not_found")]])
    researcher.client = plain
    researcher._loop("q", "summary", [], ReaderGate(), [])
    assert "scarce" not in plain.asked[0]["messages"][0]["content"]


def test_the_research_tool_description_has_one_short_line_and_no_count(tmp_path: Path) -> None:
    clock, fake = distinctive(tmp_path)
    ctx = ToolContext(workdir=tmp_path, research=reader(tmp_path, clock, fake))
    for lane in ("ask", "edit"):
        (research,) = [t for t in scope_turn(ctx, lane) if t["function"]["name"] == "research"]
        text = research["function"]["description"]
        assert text.endswith("Searches are scarce: ask one sharp question.")
        assert "437" not in text
        assert "5 searches" not in text
    ctx.research.brave = None  # type: ignore[union-attr]
    (plain,) = [t for t in scope_turn(ctx, "ask") if t["function"]["name"] == "research"]
    assert "scarce" not in plain["function"]["description"]
    ctx.research.brave = make(tmp_path, clock, fake)  # type: ignore[union-attr]
    ctx.research.search_enabled = False  # type: ignore[union-attr]
    (off,) = [t for t in scope_turn(ctx, "ask") if t["function"]["name"] == "research"]
    assert "scarce" not in off["function"]["description"]


def test_no_search_result_the_model_sees_carries_the_count(tmp_path: Path) -> None:
    clock, fake = distinctive(tmp_path)
    researcher = reader(tmp_path, clock, fake)
    shown = [
        researcher._search("first", ReaderGate(), None),
        researcher._search("first", ReaderGate(), None),  # cached
    ]
    write_state(tmp_path, tokens=0.0, stamp=clock.t, used=563)
    shown.append(researcher._search("fresh", ReaderGate(), None))  # fallback
    fallback_down = reader(tmp_path, clock, fake, searx_down=True)
    shown.append(fallback_down._search("fresh2", ReaderGate(), None))  # named failure
    for text in shown:
        assert "437" not in text
        assert "563" not in text
    assert "(cached)" in shown[1]
    assert "budget used up" in shown[2]
    assert "budget is used up" in shown[3]


def test_the_person_sees_the_numbers_in_capabilities_and_search_status(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("SADDLE_BRAVE_API_KEY", KEY)
    monkeypatch.setenv("SADDLE_BRAVE_STATE", str(tmp_path / "state.json"))
    write_state(tmp_path, tokens=5.0, stamp=datetime.now(UTC).timestamp(), used=563)
    rows = {r.name: r for r in status(Switches(research=False, search=True))}
    assert rows["search"].state == "on"
    assert rows["search"].reason.startswith("used by research")
    assert "Brave: 5 searches available" in rows["search"].reason
    assert "monthly 437 left" in rows["search"].reason
    assert KEY not in json.dumps([r.as_json() for r in rows.values()])
    import io

    out, err = io.StringIO(), io.StringIO()
    code = run_search("status", stdout=out, stderr=err, docker=lambda _a: _docker_down())
    assert code == 0
    assert "provider: Brave" in out.getvalue()
    assert "monthly 437 left" in out.getvalue()
    assert KEY not in out.getvalue() + err.getvalue()


def _docker_down() -> Any:
    import subprocess

    return subprocess.CompletedProcess([], 1, "", "no docker")


def test_search_status_names_searxng_without_a_key_and_a_refused_key_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import io

    out = io.StringIO()
    run_search("status", stdout=out, stderr=io.StringIO(), docker=lambda _a: _docker_down())
    assert "provider: SearXNG (no Brave key configured)" in out.getvalue()
    path = tmp_path / "brave.env"
    path.write_text(f"BRAVE_API_KEY={KEY}\n")
    path.chmod(0o644)
    monkeypatch.setenv("SADDLE_BRAVE_ENV_FILE", str(path))
    out = io.StringIO()
    run_search("status", stdout=out, stderr=io.StringIO(), docker=lambda _a: _docker_down())
    assert "Brave key refused" in out.getvalue()
    assert KEY not in out.getvalue()
    rows = {r.name: r for r in status(Switches(search=True), http=searx_client(down=True))}
    assert rows["search"].state == "unavailable"
    rows = {r.name: r for r in status(Switches(search=True), http=searx_client())}
    assert "Brave key refused" in rows["search"].reason
    assert KEY not in rows["search"].reason


def test_capabilities_command_prints_the_provider_line(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import io

    monkeypatch.setenv("SADDLE_BRAVE_API_KEY", KEY)
    monkeypatch.setenv("SADDLE_CAPABILITIES", "search")
    out = io.StringIO()
    assert run_capabilities("status", None, stdout=out, stderr=io.StringIO()) == 0
    assert "Brave:" in out.getvalue()
    assert KEY not in out.getvalue()


# -- wiring --------------------------------------------------------------------------------


def test_a_session_gets_brave_only_with_search_on_and_a_key(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from mcp_support import write_config

    monkeypatch.setenv("SADDLE_MCP_CONFIG", str(write_config(tmp_path / "c.json", BOTH)))
    monkeypatch.setenv("SADDLE_BRAVE_STATE", str(tmp_path / "state.json"))
    ctx = ToolContext(workdir=tmp_path)
    down = tmp_path / "downloads"
    attach_mcp(ctx, down, Switches(research=True, search=True))
    assert ctx.research is not None
    assert ctx.research.brave is None  # no key: SearXNG, as before
    monkeypatch.setenv("SADDLE_BRAVE_API_KEY", KEY)
    attach_mcp(ctx, down, Switches(research=True, search=True))
    assert ctx.research.brave is not None
    assert ctx.research.brave.state_path == tmp_path / "state.json"
    attach_mcp(ctx, down, Switches(research=True))  # search off: no Brave either
    assert ctx.research.brave is None
    monkeypatch.delenv("SADDLE_BRAVE_API_KEY")
    bad = tmp_path / "brave.env"
    bad.write_text(f"BRAVE_API_KEY={KEY}\n")
    bad.chmod(0o644)
    monkeypatch.setenv("SADDLE_BRAVE_ENV_FILE", str(bad))
    attach_mcp(ctx, down, Switches(research=True, search=True))
    assert ctx.research.brave is None
    assert "chmod 600" in ctx.research.brave_problem
    assert KEY not in ctx.research.brave_problem


# -- the account's own usage limit --------------------------------------------------------


@pytest.mark.parametrize(
    ("code", "body"),
    [
        (402, {"error": {"detail": "payment required"}}),
        (429, {"error": {"code": "QUOTA_LIMITED", "detail": f"token {KEY}"}}),
        (403, {"error": {"detail": "Usage limit reached for this subscription"}}),
        (429, {"message": "spend limit hit"}),
    ],
)
def test_a_usage_limit_refusal_exhausts_the_month_and_is_never_retried(
    tmp_path: Path, code: int, body: Any
) -> None:
    clock, fake = Clock(OCT + 10 * HOUR), Fake()
    write_state(tmp_path, tokens=50.0, stamp=clock.t)
    brave = make(tmp_path, clock, fake)
    fake.status, fake.body = code, body
    found = brave.search("q")
    assert found.exhausted
    assert not found.error  # never an invalid-key error
    assert found.refill == "~31 days"  # the start of November, 30.6 days away
    assert KEY not in repr(found)
    assert brave.balance()[:2] == (0, 0)
    fake.status, fake.body = 200, None
    clock.t += 3 * 24 * HOUR
    again = brave.search("other")
    assert again.exhausted
    assert len(fake.requests) == 1  # never retried within the month
    clock.t = datetime(2026, 11, 1, 1, tzinfo=UTC).timestamp()
    assert brave.balance()[1] == 1000  # the new month starts fresh


def test_a_usage_limit_refusal_falls_back_with_the_usual_label(tmp_path: Path) -> None:
    clock, fake = Clock(OCT + 10 * HOUR), Fake()
    researcher = reader(tmp_path, clock, fake, tokens=50.0, stamp=clock.t)
    fake.status, fake.body = 402, {"error": "limit"}
    out = researcher._search("q", ReaderGate(), None)
    assert out.startswith("(budget used up: free search, lower quality; Brave refills in ~31 days)")
    assert "Free\nhttps://free.example/a" in out
    assert "invalid" not in out


@pytest.mark.parametrize("code", [401, 403])
def test_an_invalid_key_refusal_without_a_limit_message_stays_an_invalid_key(
    tmp_path: Path, code: int
) -> None:
    clock, fake = Clock(OCT + 10 * HOUR), Fake()
    write_state(tmp_path, tokens=50.0, stamp=clock.t)
    fake.status, fake.body = code, {"error": {"detail": "subscription token invalid"}}
    found = make(tmp_path, clock, fake).search("q")
    assert "key was refused" in found.error
    assert not found.exhausted


def test_a_per_second_429_is_a_short_wait_not_a_spent_month(tmp_path: Path) -> None:
    clock, fake = Clock(OCT + 10 * HOUR), Fake()
    write_state(tmp_path, tokens=50.0, stamp=clock.t)
    brave = make(tmp_path, clock, fake)
    fake.status = 429
    fake.body = {"error": {"code": "RATE_LIMITED", "detail": "Request rate limit exceeded"}}
    fake.headers = {"X-RateLimit-Remaining": "0, 900", "X-RateLimit-Reset": "1, 1"}
    found = brave.search("q")
    assert (found.exhausted, found.refill) == (True, "~1 min")
    assert brave.balance()[1] == 900  # the month is not spent
    assert brave.search("again").exhausted  # still inside the reported second
    assert len(fake.requests) == 1
    fake.status, fake.body, fake.headers = 200, None, {}
    clock.t += 2
    assert brave.search("later").rows


# -- remaining branches ---------------------------------------------------------------------


def test_a_key_file_that_cannot_be_read_is_named_without_the_key(tmp_path: Path) -> None:
    blocker = tmp_path / "plain"
    blocker.write_text("x")
    with pytest.raises(BraveKeyError, match="cannot read the Brave key file"):
        load_key({"SADDLE_BRAVE_ENV_FILE": str(blocker / "brave.env")})
    binary = tmp_path / "brave.env"
    binary.write_bytes(b"\xff\xfe" + KEY.encode())
    binary.chmod(0o600)
    with pytest.raises(BraveKeyError, match="UnicodeDecodeError") as refused:
        load_key({"SADDLE_BRAVE_ENV_FILE": str(binary)})
    assert KEY not in str(refused.value)


def test_the_budget_line_says_when_the_next_search_accrues_and_waits_are_readable(
    tmp_path: Path,
) -> None:
    from saddle.brave import describe_wait

    clock = Clock(OCT + 1 * HOUR)
    write_state(tmp_path, tokens=0.0, stamp=clock.t)
    line = make(tmp_path, clock, Fake()).budget_line()
    assert (
        line
        == "0 searches available (accrues ~6.7 per 5 hours; the next in ~45 min; monthly 1000 left)"
    )
    assert describe_wait(0) == "~1 min"
    assert describe_wait(100 * 60) == "~2 h"
    assert describe_wait(3 * 24 * HOUR) == "~3 days"


def test_odd_rows_are_skipped_and_an_empty_answer_is_charged_but_not_cached(
    tmp_path: Path,
) -> None:
    clock, fake = Clock(OCT + 10 * HOUR), Fake()
    write_state(tmp_path, tokens=5.0, stamp=clock.t)
    brave = make(tmp_path, clock, fake)
    fake.body = {"web": {"results": [7, {"title": "T", "url": "https://x.example/"}]}}
    assert [r["url"] for r in brave.search("odd").rows] == ["https://x.example/"]
    fake.body = {"web": {"results": []}}
    assert brave.search("nothing").rows == []
    clock.t += 1
    assert not brave.search("nothing").cached  # an empty answer is not remembered
    assert len(fake.requests) == 3


def test_the_cache_is_pruned_to_its_limit_oldest_first(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import saddle.brave as module

    monkeypatch.setattr(module, "CACHE_ENTRIES", 2)
    clock, fake = Clock(OCT + 10 * HOUR), Fake()
    write_state(tmp_path, tokens=50.0, stamp=clock.t)
    brave = make(tmp_path, clock, fake)
    for word in ("alpha", "beta", "gamma"):
        brave.search(word)
        clock.t += 2
    assert sorted(json.loads((tmp_path / "state.json").read_text())["cache"]) == ["beta", "gamma"]


@needs_bwrap
def test_the_reader_is_offered_search_and_uses_brave_and_never_sees_a_count(
    tmp_path: Path,
) -> None:
    rig = Rig(tmp_path, search=True)
    clock, fake = distinctive(tmp_path)
    rig.researcher.brave = make(tmp_path, clock, fake)
    rig.http_requests.clear()
    try:
        result, model = rig.run(
            [
                [tool("search", query="install guide")],
                [tool("report", kind="none", reason="not_found")],
            ]
        )
        assert any(t["function"]["name"] == "search" for t in model.asked[0]["tools"])
        assert "scarce" in model.asked[0]["messages"][0]["content"]
        assert "https://docs.example/1" in json.dumps(model.asked[1]["messages"])
        assert rig.http_requests == []  # SearXNG was never asked
        assert [r.url.host for r in fake.requests] == ["api.search.brave.com"]
        assert "437" not in result + json.dumps(model.asked)
    finally:
        rig.close()


@needs_bwrap
def test_a_refused_key_file_with_the_free_search_down_is_said_in_the_result(
    tmp_path: Path,
) -> None:
    rig = Rig(tmp_path, search=True)
    rig.search_body = httpx.ConnectError("refused")
    rig.researcher.brave_problem = "the Brave key file is readable by others; chmod 600"
    try:
        result, model = rig.run([[tool("report", kind="none", reason="not_found")]])
        assert "search is unavailable (not answering" in result
        assert "Brave: the Brave key file is readable by others" in result
        assert all(t["function"]["name"] != "search" for t in model.asked[0]["tools"])
    finally:
        rig.close()


def test_a_429_that_reports_no_reset_waits_a_minute_or_to_the_month_end_if_spent(
    tmp_path: Path,
) -> None:
    clock, fake = Clock(OCT + 10 * HOUR), Fake()
    write_state(tmp_path, tokens=50.0, stamp=clock.t)
    brave = make(tmp_path, clock, fake)
    fake.status = 429
    assert brave.search("q").refill == "~1 min"
    clock.t += 30
    assert brave.search("again").exhausted  # still inside the minute
    fake.status = 200
    clock.t += 40
    assert brave.search("later").rows
    fake.status, fake.headers = 429, {"X-RateLimit-Remaining": "0, 0"}
    clock.t += 5
    assert brave.search("spent").refill == "~31 days"
