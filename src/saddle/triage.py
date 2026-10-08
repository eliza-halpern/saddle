"""Where an autonomous run stopped making progress, and whose block it was (#164).

This part builds the run's timeline from its own records: the ledger beside the run
(`proofs.jsonl`) and the conversation log beside it (`saddle.conversation`). A round
is one `auto:spend`: the request the model was sent, the reply it gave, each call it
made with the whole result it read, and every span sealed until the next round
(audits, refusals). Each part says where it came from, and a part a record does not
hold is None, never a guess.

The reply to a round's request is the last assistant message of the conversation the
next request (or the log's end) carries, and only if the round's own request did not
already hold it. A request with no round (an image-limit retry) carries no reply.
"""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Final

from saddle.conversation import (
    CONVERSATION_LOG,
    ConversationError,
    Ending,
    Request,
    ending,
    requests,
)
from saddle.journal import (
    AUTO_OUTCOMES,
    AUTO_START,
    COMPACTION_SPAN,
    SpanRecord,
    read_spans,
    scrub_thinking,
)

SPEND: Final = "auto:spend"
"""A round's spend span (`engine._charge`): one per reply the model gave."""

EDIT_TOOLS: Final = frozenset({"edit_file", "write_file"})
"""Tools whose `path` argument is a file the call changes."""


@dataclass(frozen=True)
class Call:
    """One tool call a round's reply made, and what came of it."""

    id: str
    name: str
    arguments: str
    result: str | None
    """The whole result the model read, from the conversation log; None without it."""
    span: SpanRecord | None
    """The call's sealed span (exit code, time, its result cut to the ledger's cap);
    None when the ledger holds none for it."""

    @property
    def edited(self) -> str | None:
        """The file an edit tool changed (its `path`), or None."""
        if self.name not in EDIT_TOOLS:
            return None
        try:
            path = json.loads(self.arguments).get("path")
        except (ValueError, AttributeError):
            return None
        return path if isinstance(path, str) else None


@dataclass(frozen=True)
class Round:
    """One round: a request, the reply to it, and what the reply did."""

    number: int
    spend: Mapping[str, Any]
    """The round's sealed spend: tokens, model time, its reply cap."""
    request: Request | None
    """The request as it was sent, from the conversation log; None without it."""
    reply: Mapping[str, Any] | None
    """The assistant message the model answered with; None when no record holds it."""
    calls: tuple[Call, ...]
    spans: tuple[SpanRecord, ...]
    """Every span sealed from this round's spend to the next one's, in order."""
    compaction: SpanRecord | None
    """The compaction sealed just before this round's request, if there was one."""

    @property
    def prompt_tokens(self) -> int | None:
        """The request's size as the server counted it, when it said."""
        value = self.spend.get("prompt_tokens")
        return value if isinstance(value, int) else None


@dataclass(frozen=True)
class Timeline:
    """A run's rounds, between its start and its outcome."""

    start: SpanRecord
    rounds: tuple[Round, ...]
    outcome: SpanRecord | None
    """The run's outcome span; None for a run that has not ended, or was killed."""
    log: str
    """What the conversation log gave: "whole", "none" (the run kept no log), or what
    was wrong with it; the rounds it could not reach have no request or reply."""


class TriageError(RuntimeError):
    """The ledger is not an autonomous run's."""


def timeline(ledger: Path) -> Timeline:
    """The timeline of the run whose ledger is `ledger`."""
    spans = read_spans(ledger)
    start = next((s for s in spans if s.name == AUTO_START), None)
    if start is None:
        msg = f"{ledger} holds no {AUTO_START} span: not an autonomous run's ledger"
        raise TriageError(msg)
    outcome = next((s for s in reversed(spans) if s.name in AUTO_OUTCOMES), None)
    sent, end, log = _read_log(ledger.parent / CONVERSATION_LOG)
    by_number = {request.number: request for request in sent}
    rounds: list[Round] = []
    pending: SpanRecord | None = None
    groups: list[tuple[Mapping[str, Any], list[SpanRecord], SpanRecord | None]] = []
    for span in spans:
        if span.name == SPEND:
            groups.append((_spend(span), [span], pending))
            pending = None
        elif span.name == COMPACTION_SPAN:
            pending = span
        elif groups and span.name not in AUTO_OUTCOMES:
            groups[-1][1].append(span)
    for number, (spend, round_spans, compaction) in enumerate(groups, 1):
        at = spend.get("request", number)
        request = by_number.get(at) if isinstance(at, int) else None
        following: Sequence[Mapping[str, Any]] | None = None
        if isinstance(at, int) and at + 1 in by_number:
            following = by_number[at + 1].messages
        elif end is not None and isinstance(at, int) and end.after == at:
            following = end.messages
        reply, after = _reply(request, following)
        rounds.append(
            Round(
                number=number,
                spend=spend,
                request=request,
                reply=reply,
                calls=_calls(reply, after, round_spans),
                spans=tuple(round_spans),
                compaction=compaction,
            )
        )
    return Timeline(start=start, rounds=tuple(rounds), outcome=outcome, log=log)


def _read_log(path: Path) -> tuple[list[Request], Ending | None, str]:
    """Every whole request in the log, its end, and what the log gave."""
    if not path.exists():
        return [], None, "none"
    whole: list[Request] = []
    try:
        whole.extend(requests(path))
        end = ending(path)
    except ConversationError as exc:
        return whole, None, str(exc)
    return whole, end, "whole" if end is not None else "no end: the run did not end normally"


def _spend(span: SpanRecord) -> Mapping[str, Any]:
    try:
        value = json.loads(span.argv[1])
    except (IndexError, ValueError):
        return {}
    return value if isinstance(value, dict) else {}


def _reply(
    request: Request | None, following: Sequence[Mapping[str, Any]] | None
) -> tuple[Mapping[str, Any] | None, Sequence[Mapping[str, Any]]]:
    """The round's reply and the messages after it, from the conversation that
    followed its request; nothing when either is missing or the reply is not new."""
    if request is None or following is None:
        return None, ()
    for index in range(len(following) - 1, -1, -1):
        message = following[index]
        if message.get("role") == "assistant":
            if message in request.messages:
                return None, ()
            return message, following[index + 1 :]
    return None, ()


def _calls(
    reply: Mapping[str, Any] | None,
    after: Sequence[Mapping[str, Any]],
    spans: Sequence[SpanRecord],
) -> tuple[Call, ...]:
    """Each call `reply` made, its whole result from `after`, and its span."""
    if reply is None:
        return ()
    results = {m.get("tool_call_id"): m.get("content") for m in after if m.get("role") == "tool"}
    unused = [s for s in spans if s.kind == "tool"]
    calls: list[Call] = []
    for tool_call in reply.get("tool_calls") or ():
        function = tool_call.get("function") or {}
        name, arguments = str(function.get("name", "")), str(function.get("arguments", ""))
        # A span seals its arguments redacted and capped (`journal.build_span`).
        sealed = [name, scrub_thinking(arguments)]
        span = next((s for s in unused if s.argv[:2] == sealed), None)
        if span is None and arguments == "{}":
            # A call cut off mid-arguments is kept as `{}` in the history and sealed
            # as it came (`engine._sendable_arguments`): the next span of its name.
            span = next((s for s in unused if s.argv[:1] == [name]), None)
        if span is not None:
            unused.remove(span)
        result = results.get(tool_call.get("id"))
        calls.append(
            Call(
                id=str(tool_call.get("id", "")),
                name=name,
                arguments=arguments,
                result=result if isinstance(result, str) else None,
                span=span,
            )
        )
    return tuple(calls)
