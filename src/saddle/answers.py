"""Brave Answers for the web reader's `ask_answers` tool, under a dollar budget.

Brave's Answers API (a chat-completions endpoint) returns a written answer with
citations. It is costly and it can be wrong, so here it is a lead:

- Nothing is on by default: the `answers` capability is off until the person turns
  it on, and it needs its own key, `BRAVE_ANSWERS_API_KEY=...` in the same
  owner-only file as the search key (`$SADDLE_BRAVE_ANSWERS_API_KEY` for one
  process). The key is never put in a result, an error, a journal or a log.
- Only the reader has the tool. What it returns is labelled a lead, not a source;
  its cited addresses become openable, and the reader's report may still cite
  only pages it read. The acting session never sees the answer or the tool.
- Money. A call is charged what the reply's own `<usage>` tag says it cost
  (`X-Request-Total-Cost`), else $0.004 plus $5 per million input and output
  tokens from the usage it reports, else a conservative estimate, recorded as
  such. A monthly cap (default $4.50, under the account's own $5
  credit) is checked before every call against a conservative per-call estimate.
  Spend, call count and the exhausted flag reset at the start of each calendar
  month UTC. Brave's usage- or spend-limit refusals mark the month exhausted.
- The model is never shown a balance or a count, only fixed guidance and, when
  the tool cannot be used, one count-free sentence.

Doc pages relied on: the Brave API documentation for Answers (endpoint
`/res/v1/chat/completions`, header `x-subscription-token`, OpenAI-style body with
`model: "brave"`, `stream`, `enable_citations`; usage in `X-Request-*` response
headers or a trailing `<usage>` tag; citations as inline `<citation>` tags) and
its pricing page. Brave refuses `enable_citations` on a blocking call (HTTP 422,
"Blocking response doesn't support 'enable_citations' option"), so `ask` streams:
the reply is server-sent events whose chunks carry the text, the tags inside it,
and a final chunk with `finish_reason` and an OpenAI-style `usage` object.

Privacy: a question the reader sends goes to Brave.
"""

from __future__ import annotations

import json
import os
import re
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import IO, Any, Final

import httpx

from saddle.brave import BraveKeyError, _month, _usage_limited, key_file, read_key

KEY_ENV: Final = "SADDLE_BRAVE_ANSWERS_API_KEY"
KEY_VARIABLE: Final = "BRAVE_ANSWERS_API_KEY"
STATE_ENV: Final = "SADDLE_BRAVE_ANSWERS_STATE"
DEFAULT_STATE: Final = Path("~/.local/state/saddle/brave-answers.json")
CAP_ENV: Final = "SADDLE_BRAVE_ANSWERS_CAP"
MONTHLY_CAP: Final = 4.50
"""Dollars a calendar month may spend: under the account's own $5 credit."""
URL: Final = "https://api.search.brave.com/res/v1/chat/completions"
QUERY_COST: Final = 0.004
TOKEN_COST: Final = 5e-6
ESTIMATE_IN: Final = 4000
ESTIMATE_OUT: Final = 2000
ESTIMATE_COST: Final = QUERY_COST + (ESTIMATE_IN + ESTIMATE_OUT) * TOKEN_COST
"""Charged when a response reports no usage, and held back before every call."""
TIMEOUT_S: Final = 60

UNAVAILABLE: Final = "Answers is not available now; use search and read pages"
LABEL: Final = (
    "[AI answer from Brave — a lead, not a source; open and read the pages it "
    "cites before relying on it]"
)
RULES: Final = (
    "Answers is costly: use it only when searching and reading have not settled the "
    "question; ask one precise question."
)

_TAGGED: Final = re.compile(r"<(citation|usage)>(.*?)</\1>", re.DOTALL)


def cost(tokens_in: int, tokens_out: int) -> float:
    """Dollars for one call that used these tokens."""
    return QUERY_COST + tokens_in * TOKEN_COST + tokens_out * TOKEN_COST


def _count(value: Any) -> int | None:
    try:
        number = int(str(value).strip())
    except ValueError:
        return None
    return number if number >= 0 else None


def _dollars(value: Any) -> float | None:
    try:
        number = float(str(value).strip())
    except ValueError:
        return None
    return number if 0 <= number < float("inf") else None


def _streamed(body: str) -> tuple[str, Any]:
    """(the text the stream's chunks carry, the last usage object it reports).
    Raises ValueError for a stream that never says it ended: no `[DONE]` and no
    chunk with a `finish_reason`."""
    pieces: list[str] = []
    usage: Any = None
    ended = False
    for line in body.splitlines():
        if not line.startswith("data:"):
            continue
        data = line.removeprefix("data:").strip()
        if data == "[DONE]":
            ended = True
            break
        chunk = json.loads(data)
        if not isinstance(chunk, dict):
            msg = "an event that is not a chunk"
            raise TypeError(msg)
        usage = chunk.get("usage") or usage
        for choice in chunk.get("choices") or []:
            piece = choice["delta"].get("content") or ""
            if not isinstance(piece, str):
                msg = "no answer text"
                raise TypeError(msg)
            pieces.append(piece)
            ended = ended or choice.get("finish_reason") is not None
    if not ended:
        msg = "the stream did not end"
        raise ValueError(msg)
    return "".join(pieces), usage


@dataclass
class Answer:
    """What one question came to: text with its cited addresses, or unavailable."""

    text: str = ""
    urls: list[str] = field(default_factory=list)
    unavailable: bool = False


def parse(reply: httpx.Response) -> tuple[str, list[str], float | None]:
    """(answer text with its tags removed, cited addresses in citation-number
    order and once each, dollars the call cost or None when the reply reports no
    usage) from a streamed reply. Raises ValueError, TypeError or KeyError for a
    stream that cannot be read."""
    content, usage = _streamed(reply.text)
    cited: list[tuple[float, str]] = []
    reported: dict[str, Any] = {}
    for kind, raw in _TAGGED.findall(content):
        try:
            data = json.loads(raw)
        except ValueError:
            continue
        if kind == "usage" and isinstance(data, dict):
            reported.update(data)
        elif isinstance(data, dict):
            number = data.get("number")
            rank = number if isinstance(number, int) else float("inf")
            cited.append((rank, str(data.get("url", ""))))
    if isinstance(usage, dict):
        reported.setdefault("X-Request-Tokens-In", usage.get("prompt_tokens"))
        reported.setdefault("X-Request-Tokens-Out", usage.get("completion_tokens"))
    for name in ("X-Request-Tokens-In", "X-Request-Tokens-Out"):
        if name in reply.headers:
            reported[name] = reply.headers[name]
    tokens_in = _count(reported.get("X-Request-Tokens-In"))
    tokens_out = _count(reported.get("X-Request-Tokens-Out"))
    text = _TAGGED.sub("", content).strip()
    urls = [url for _, url in sorted(cited, key=lambda pair: pair[0])]
    found = [u for u in dict.fromkeys(urls) if u.startswith(("http://", "https://"))]
    spend = _dollars(reported.get("X-Request-Total-Cost"))
    if spend is None and tokens_in is not None and tokens_out is not None:
        spend = cost(tokens_in, tokens_out)
    return text, found, spend


@dataclass
class BraveAnswers:
    key: str = field(repr=False)
    state_path: Path
    cap: float = MONTHLY_CAP
    http: httpx.Client | None = None
    clock: Callable[[], float] = time.time

    @classmethod
    def from_env(
        cls, environ: Mapping[str, str] | None = None, *, http: httpx.Client | None = None
    ) -> BraveAnswers | None:
        """A client when an Answers key is configured, else None. May raise
        `BraveKeyError` (a key file others can read)."""
        env = os.environ if environ is None else environ
        key = read_key(env, KEY_ENV, KEY_VARIABLE, required=False)
        if key is None:
            return None
        try:
            cap = float(env.get(CAP_ENV, "") or MONTHLY_CAP)
        except ValueError:
            cap = MONTHLY_CAP
        path = Path(env.get(STATE_ENV) or DEFAULT_STATE).expanduser()
        return cls(key, path, max(0.0, cap), http)

    # -- the budget ----------------------------------------------------------

    def _load(self) -> dict[str, Any]:
        try:
            data = json.loads(self.state_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            data = {}
        return data if isinstance(data, dict) else {}

    def _save(self, state: dict[str, Any]) -> None:
        self.state_path.parent.mkdir(parents=True, exist_ok=True)
        descriptor = os.open(self.state_path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            handle.write(json.dumps(state, sort_keys=True))

    def _current(self, now: float) -> dict[str, Any]:
        """The state for the month holding `now`: a new month starts at zero."""
        label = _month(now)[0]
        state = self._load()
        if state.get("month") != label:
            state = {"month": label, "spent": 0.0, "calls": 0, "estimated": 0, "exhausted": False}
        return state

    def snapshot(self) -> dict[str, Any]:
        """For the person: month, spent, remaining, calls, estimated calls, whether
        Brave said the month is over, and the last problem."""
        state = self._current(self.clock())
        spent = float(state.get("spent", 0.0))
        return {
            "month": state["month"],
            "spent": spent,
            "remaining": max(0.0, self.cap - spent),
            "cap": self.cap,
            "calls": int(state.get("calls", 0)),
            "estimated": int(state.get("estimated", 0)),
            "exhausted": bool(state.get("exhausted", False)),
            "problem": str(state.get("problem", "")),
        }

    def status_lines(self) -> list[str]:
        """The spend as the person reads it (never the model)."""
        s = self.snapshot()
        lines = [
            f"Answers, {s['month']} (UTC): spent ${s['spent']:.4f} of ${s['cap']:.2f}, "
            f"remaining ${s['remaining']:.4f}, {s['calls']} calls"
        ]
        if s["estimated"]:
            lines.append(
                f"{s['estimated']} calls reported no usage and were charged a "
                f"${ESTIMATE_COST:.3f} estimate each"
            )
        if s["exhausted"]:
            lines.append(
                "Brave reported its usage or spend limit reached: no more calls this month"
            )
        if s["problem"]:
            lines.append(f"last problem: {s['problem']}")
        return lines

    def available(self) -> bool:
        """Whether a call would be made: not exhausted, and the credit left covers
        a conservative estimate of one."""
        state = self._current(self.clock())
        left = self.cap - float(state.get("spent", 0.0))
        return not state.get("exhausted") and left >= ESTIMATE_COST

    # -- one question --------------------------------------------------------

    def _redact(self, text: str) -> str:
        return text.replace(self.key, "[key]")

    def ask(self, question: str) -> Answer:
        now = self.clock()
        state = self._current(now)
        left = self.cap - float(state.get("spent", 0.0))
        if state.get("exhausted") or left < ESTIMATE_COST:
            return Answer(unavailable=True)
        client = self.http or httpx.Client(timeout=TIMEOUT_S)
        try:
            reply = client.post(
                URL,
                json={
                    "model": "brave",
                    "messages": [{"role": "user", "content": question}],
                    "stream": True,
                    "enable_citations": True,
                },
                headers={"x-subscription-token": self.key, "Accept": "text/event-stream"},
            )
        except httpx.HTTPError as exc:
            state["problem"] = self._redact(f"Answers did not answer ({exc})")
            self._save(state)
            return Answer(unavailable=True)
        if _usage_limited(reply):
            state["exhausted"] = True
            state["problem"] = f"Brave's usage or spend limit (HTTP {reply.status_code})"
            self._save(state)
            return Answer(unavailable=True)
        if reply.status_code != 200:
            state["problem"] = f"Answers refused the request (HTTP {reply.status_code})"
            if reply.status_code in (401, 403):
                state["problem"] += "; the key is invalid or does not cover Answers"
            self._save(state)
            return Answer(unavailable=True)
        try:
            text, urls, spend = parse(reply)
        except (ValueError, KeyError, IndexError, TypeError, AttributeError) as exc:
            spend, text, urls = None, "", []
            state["problem"] = self._redact(f"Answers answered badly: {type(exc).__name__}")
        # Brave bills a 200 whether or not it can be read.
        state["spent"] = float(state.get("spent", 0.0)) + (
            ESTIMATE_COST if spend is None else spend
        )
        state["calls"] = int(state.get("calls", 0)) + 1
        state["estimated"] = int(state.get("estimated", 0)) + (spend is None)
        if text:
            state.pop("problem", None)
        self._save(state)
        if not text:
            return Answer(unavailable=True)
        return Answer(text=text, urls=urls)


def run_answers(action: str, *, stdout: IO[str], stderr: IO[str]) -> int:
    """`saddle answers status`: the spend this month, for the person."""
    assert action == "status"
    try:
        client = BraveAnswers.from_env()
    except BraveKeyError as exc:
        print(f"error: {exc}", file=stderr)
        return 1
    if client is None:
        print(
            f"no Answers key: add {KEY_VARIABLE}=... to {key_file()} (mode 600) or set ${KEY_ENV}",
            file=stdout,
        )
        return 0
    for line in client.status_lines():
        print(line, file=stdout)
    return 0
