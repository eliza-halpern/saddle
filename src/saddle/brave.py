"""Brave Search for the web reader's `search` tool, under a budget (#93).

The free engines behind a local SearXNG rate-limit within an evening. When the
person supplies a Brave Search API key the reader searches with Brave instead,
and this module is what keeps that to the free tier:

- Nothing here is on by default. The key comes from `$SADDLE_BRAVE_API_KEY` or
  from `~/.config/saddle/brave.env` (`$SADDLE_BRAVE_ENV_FILE`), a line
  `BRAVE_API_KEY=...` in a file only its owner can read; a group- or
  world-readable file is refused, and the key is never put in a result, an
  error, a journal or a log.
- Budget. Searches accrue continuously at `quota / hours_in_this_calendar_month`
  per hour (about 1.34 an hour, 6.7 per five hours, in a 31-day month) and a
  search costs one. Unused searches carry forward: there is no five-hour cap.
  The only ceiling is what is left of the month's quota (the quota minus what was
  used this month, or Brave's own remaining count when that says less). Balance,
  count and cache all reset at the start of each calendar month UTC, so carry-over
  never crosses a month. Requests are at least one second apart.
- Cache. A query is normalised (case, whitespace and punctuation ignored) and its
  results are kept for seven days; a hit costs nothing and is labelled `(cached)`.
- Exhausted. With no balance, no month left, or a 429 from Brave, `search` does
  not call Brave and says when it refills; the reader falls back to SearXNG,
  labelled, or fails with a named error. It never answers an empty list.

Privacy: a query the reader sends goes to Brave.
"""

from __future__ import annotations

import json
import math
import os
import re
import stat
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Final

import httpx

KEY_ENV: Final = "SADDLE_BRAVE_API_KEY"
KEY_FILE_ENV: Final = "SADDLE_BRAVE_ENV_FILE"
DEFAULT_KEY_FILE: Final = Path("~/.config/saddle/brave.env")
KEY_VARIABLE: Final = "BRAVE_API_KEY"
STATE_ENV: Final = "SADDLE_BRAVE_STATE"
DEFAULT_STATE: Final = Path("~/.local/state/saddle/brave-budget.json")
QUOTA_ENV: Final = "SADDLE_BRAVE_MONTHLY_QUOTA"
MONTHLY_QUOTA: Final = 1000
"""Brave's free tier: searches per calendar month."""
URL: Final = "https://api.search.brave.com/res/v1/web/search"
RESULTS: Final = 10
MIN_GAP_S: Final = 1.0
"""Brave's free tier allows one request a second."""
CACHE_S: Final = 7 * 24 * 3600.0
CACHE_ENTRIES: Final = 500
PACE_HOURS: Final = 5
"""The span the model is told the pace over."""
DEFAULT_429_S: Final = 60.0
"""How long a 429 that reports no reset marks the window exhausted."""

RULES: Final = (
    "Plan one specific query; read result pages before searching again; never repeat "
    "a query; prefer fetching a page you already know over searching."
)


class BraveKeyError(ValueError):
    """The key file could not be used. Says why and never contains the key."""


def key_file(environ: Mapping[str, str] | None = None) -> Path:
    env = os.environ if environ is None else environ
    return Path(env.get(KEY_FILE_ENV) or DEFAULT_KEY_FILE).expanduser()


def load_key(environ: Mapping[str, str] | None = None) -> str | None:
    """The Brave key: `$SADDLE_BRAVE_API_KEY`, else the `BRAVE_API_KEY` line of the
    key file; None when neither is there. A file that group or others can read, or
    that has no such line, is a `BraveKeyError`."""
    return read_key(environ, KEY_ENV, KEY_VARIABLE, required=True)


def read_key(
    environ: Mapping[str, str] | None, env_name: str, variable: str, *, required: bool
) -> str | None:
    """One key of the Brave key file, or of `env_name`. The mode check is the same
    for every key. A file with no `variable` line is an error when `required`, and
    None otherwise (the file holds another product's key only)."""
    env = os.environ if environ is None else environ
    given = env.get(env_name, "").strip()
    if given:
        return given
    path = key_file(env)
    try:
        mode = stat.S_IMODE(path.stat().st_mode)
    except FileNotFoundError:
        return None
    except OSError as exc:
        msg = f"cannot read the Brave key file {path}: {exc.strerror or 'unreadable'}"
        raise BraveKeyError(msg) from exc
    if mode & 0o077:
        msg = (
            f"the Brave key file {path} is readable by others (mode {mode:03o}); "
            f"refusing to use it. Run `chmod 600 {path}`"
        )
        raise BraveKeyError(msg)
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        msg = f"cannot read the Brave key file {path}: {type(exc).__name__}"
        raise BraveKeyError(msg) from exc
    for line in text.splitlines():
        name, _, value = line.strip().removeprefix("export ").partition("=")
        if name.strip() == variable and value.strip().strip("'\"").strip():
            return value.strip().strip("'\"").strip()
    if not required:
        return None
    msg = f"the Brave key file {path} has no {variable}=... line"
    raise BraveKeyError(msg)


def normalise(query: str) -> str:
    """The cache key: lower case, punctuation as space, whitespace collapsed."""
    return " ".join(re.sub(r"[\W_]+", " ", query.casefold()).split())


def _month(now: float) -> tuple[str, float, float]:
    """(label, start, end) of the UTC calendar month holding `now`."""
    moment = datetime.fromtimestamp(now, UTC)
    start = moment.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    year, month = (start.year + 1, 1) if start.month == 12 else (start.year, start.month + 1)
    end = start.replace(year=year, month=month)
    return start.strftime("%Y-%m"), start.timestamp(), end.timestamp()


def describe_wait(seconds: float) -> str:
    minutes = max(1, math.ceil(seconds / 60))
    if minutes <= 90:
        return f"~{minutes} min"
    hours = math.ceil(minutes / 60)
    if hours <= 48:
        return f"~{hours} h"
    return f"~{math.ceil(hours / 24)} days"


def _numbers(value: str | None) -> list[int]:
    """The integers of a rate-limit header such as `1, 14999` (per second, per
    month); anything else yields none."""
    found: list[int] = []
    for part in (value or "").split(","):
        try:
            found.append(int(part.strip()))
        except ValueError:
            return []
    return found


USAGE_LIMIT_WORDS: Final = ("quota", "usage limit", "usage_limit", "spend limit", "credit")
"""What a refusal's body says when the account's own limit is reached, as against
the per-second `RATE_LIMITED` a short wait cures."""


def _usage_limited(reply: httpx.Response) -> bool:
    """Whether `reply` says the account's usage, quota or spend limit is reached:
    HTTP 402, or a 403 or 429 whose body says so."""
    if reply.status_code == 402:
        return True
    if reply.status_code not in (403, 429):
        return False
    text = reply.text.lower()
    return any(word in text for word in USAGE_LIMIT_WORDS)


@dataclass
class Outcome:
    """What one search came to: rows, a failure, or an exhausted budget."""

    rows: list[dict[str, str]] = field(default_factory=list)
    cached: bool = False
    error: str = ""
    exhausted: bool = False
    refill: str = ""
    """When Brave refills, for example `~12 min`; set with `exhausted`."""


@dataclass
class BraveSearch:
    key: str = field(repr=False)
    state_path: Path
    quota: int = MONTHLY_QUOTA
    http: httpx.Client | None = None
    clock: Callable[[], float] = time.time
    sleep: Callable[[float], None] = time.sleep

    @classmethod
    def from_env(
        cls,
        environ: Mapping[str, str] | None = None,
        *,
        http: httpx.Client | None = None,
    ) -> BraveSearch | None:
        """A searcher when a key is configured, else None. May raise `BraveKeyError`."""
        env = os.environ if environ is None else environ
        key = load_key(env)
        if key is None:
            return None
        try:
            quota = int(env.get(QUOTA_ENV, "") or MONTHLY_QUOTA)
        except ValueError:
            quota = MONTHLY_QUOTA
        path = Path(env.get(STATE_ENV) or DEFAULT_STATE).expanduser()
        return cls(key, path, max(1, quota), http)

    # -- the budget ----------------------------------------------------------

    def _load(self) -> dict[str, Any]:
        try:
            data = json.loads(self.state_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            data = {}
        return data if isinstance(data, dict) else {}

    def _save(self, state: dict[str, Any]) -> None:
        self.state_path.parent.mkdir(parents=True, exist_ok=True)
        text = json.dumps(state, sort_keys=True)
        # The state holds queries: owner-only, written whole.
        descriptor = os.open(self.state_path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            handle.write(text)

    def rate(self, now: float) -> float:
        """Searches accrued per hour in the month holding `now`."""
        _, start, end = _month(now)
        return self.quota / ((end - start) / 3600.0)

    def _current(self, now: float) -> dict[str, Any]:
        """The state brought up to `now`: a new month starts empty; within the
        month, searches accrue at `rate` with no cap but what is left of the
        month."""
        label, start, _ = _month(now)
        state = self._load()
        if state.get("month") != label:
            first = "month" not in state  # a new person: one search without waiting
            state = {
                "month": label,
                "tokens": 1.0 if first else 0.0,
                "stamp": now if first else start,
                "used": 0,
                "header_left": None,
                "blocked_until": 0.0,
                "last_request": 0.0,
                "cache": state.get("cache", {}),
            }
            if first:
                self._save(state)  # the start of this person's accrual is kept
        hours = max(0.0, now - float(state["stamp"])) / 3600.0
        state["tokens"] = float(state["tokens"]) + hours * self.rate(now)
        state["stamp"] = now
        state["tokens"] = min(state["tokens"], float(self._left(state)))
        return state

    def _left(self, state: Mapping[str, Any]) -> int:
        """What is left of the month's quota: ours, or Brave's count if lower."""
        left = self.quota - int(state["used"])
        header = state.get("header_left")
        if isinstance(header, int):
            left = min(left, header)
        return max(0, left)

    def balance(self) -> tuple[int, int, float]:
        """(searches available now, searches left this month, seconds until the
        next one accrues). The third is 0 when one is available."""
        now = self.clock()
        state = self._current(now)
        left = self._left(state)
        tokens = float(state["tokens"])
        wait = 0.0 if tokens >= 1.0 else (1.0 - tokens) / self.rate(now) * 3600.0
        if left < 1:
            wait = _month(now)[2] - now
        return int(tokens), left, wait

    def budget_line(self) -> str:
        """The live budget, as the model reads it."""
        now = self.clock()
        available, left, wait = self.balance()
        pace = self.rate(now) * PACE_HOURS
        line = f"{available} searches available (accrues ~{pace:.1f} per {PACE_HOURS} hours"
        if available < 1:
            line += f"; the next in {describe_wait(wait)}"
        return f"{line}; monthly {left} left)"

    def reader_text(self) -> str:
        """Appended to the reader's prompt. The same words whatever the balance: a
        visible count makes a model want to spend it, so no number reaches the
        model; it learns the budget is used up only when a search is refused."""
        return f"\n\nSearches are scarce and costly. {RULES}"

    def acting_text(self) -> str:
        """One short line for the acting session's `research` tool (no numbers)."""
        return "Searches are scarce: ask one sharp question."

    # -- one search ----------------------------------------------------------

    def _redact(self, text: str) -> str:
        return text.replace(self.key, "[key]")

    def search(self, query: str) -> Outcome:
        wanted = normalise(query)
        if not wanted:
            return Outcome(error="search needs a query")
        now = self.clock()
        state = self._current(now)
        cache: dict[str, Any] = state["cache"]
        hit = cache.get(wanted)
        if isinstance(hit, dict) and now - float(hit.get("at", 0)) < CACHE_S:
            return Outcome(rows=list(hit["rows"]), cached=True)
        _, _, end = _month(now)
        left = self._left(state)
        blocked = float(state["blocked_until"])
        if left < 1 or state["tokens"] < 1.0 or blocked > now:
            _, _, wait = self.balance()
            if blocked > now and left >= 1:
                wait = max(wait, blocked - now)
            return Outcome(exhausted=True, refill=describe_wait(wait))
        gap = MIN_GAP_S - (now - float(state["last_request"]))
        if gap > 0:
            self.sleep(gap)
            now = self.clock()
        client = self.http or httpx.Client(timeout=20)
        try:
            reply = client.get(
                URL,
                params={"q": query, "count": RESULTS},
                headers={"X-Subscription-Token": self.key, "Accept": "application/json"},
            )
        except httpx.HTTPError as exc:
            return Outcome(error=self._redact(f"Brave search did not answer ({exc})"))
        state["last_request"] = now
        remaining = _numbers(reply.headers.get("X-RateLimit-Remaining"))
        # A monthly limit of 0 is a metered plan with no quota: its remaining
        # count of 0 says nothing, and saddle's own budget governs.
        limits = _numbers(reply.headers.get("X-RateLimit-Limit"))
        quota = not (len(limits) >= 2 and limits[-1] == 0)
        if len(remaining) >= 2 and quota:
            state["header_left"] = remaining[-1]
        if _usage_limited(reply):
            # The account's usage or spend limit: this month is over, not a wait.
            state["header_left"] = 0
            state["blocked_until"] = end
            self._save(state)
            return Outcome(exhausted=True, refill=describe_wait(end - now))
        if reply.status_code in (401, 403):
            self._save(state)
            return Outcome(
                error=(
                    f"the Brave API key was refused (HTTP {reply.status_code}); it is "
                    f"invalid or revoked: check {key_file()} or ${KEY_ENV}"
                )
            )
        if reply.status_code == 429:
            resets = _numbers(reply.headers.get("X-RateLimit-Reset"))
            spent = quota and len(remaining) >= 2 and remaining[-1] <= 0
            if resets:
                seconds = float(resets[-1] if spent else resets[0])
            else:
                seconds = end - now if spent else DEFAULT_429_S
            state["blocked_until"] = now + max(seconds, 1.0)
            self._save(state)
            return Outcome(exhausted=True, refill=describe_wait(seconds))
        try:
            reply.raise_for_status()
            rows = reply.json().get("web", {}).get("results", [])
        except (httpx.HTTPError, ValueError, AttributeError, TypeError) as exc:
            return Outcome(error=self._redact(f"Brave search answered badly: {exc!r}"))
        if not isinstance(rows, list):
            return Outcome(error="Brave search answered badly: no results list")
        found = [
            {
                "title": str(r.get("title", "")),
                "url": str(r.get("url", "")),
                "description": str(r.get("description", "")),
            }
            for r in rows
            if isinstance(r, dict)
        ][:RESULTS]
        state["used"] = int(state["used"]) + 1
        state["tokens"] = min(float(state["tokens"]) - 1.0, float(self._left(state)))
        if found:
            cache[wanted] = {"at": now, "rows": found}
            for old in sorted(cache, key=lambda k: float(cache[k].get("at", 0)))[
                : max(0, len(cache) - CACHE_ENTRIES)
            ]:
                del cache[old]
        self._save(state)
        return Outcome(rows=found)
