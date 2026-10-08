"""A run's conversation as each request sent it, so any request can be rebuilt (#164).

The ledger keeps a tool result cut to 500 characters, so no request of a run could be
rebuilt from it. `saddle triage` reads where a run stopped making progress, and resuming
a run a harness defect trapped continues from a request the defect had not reached.
Both need each request exactly as the model received it.

`ConversationLog.record` is called with each request just before it is sent, and
appends one line saying what the request kept of the one before it, then one line
per message it added:

    {"request": 3, "sent": 1791480107.632, "max_tokens": 12000, "kept": 5, "messages": 7}
    {"role": "assistant", "content": "", "tool_calls": [...]}
    {"role": "tool", "tool_call_id": "c1", "content": "exit 0\\n"}

`kept` is how many leading messages are the previous request's, unchanged; a
compaction, or older screenshots trimmed, keeps fewer and writes the rest again. The
tool schemas are written beside `tools` only when they changed. A request is whole
only when all `messages - kept` of its lines are there, so a log cut short is seen as
cut, never as a run that ended there.

When the turn ends, `ConversationLog.end` writes what came after the last request in
the same shape, under `{"end": <requests before it>, "ended": <time>, ...}`: the last
reply and its tool results, often the very refusal a run ended on. A log with no end
is a run that did not end normally, or a log that stopped; `ending` says which it
holds and never invents one.

Like every other file in a run's directory, the log holds no secret: each string is
redacted (`redact_secrets`), and kept whole, never capped. The first line names the
messages the redaction changed (`redacted`, and `tools_redacted` for the schemas):
those are not exactly as sent, and a reader is told which.

A log that cannot be written stops, says why in `failed`, and never stops the run.
"""

from __future__ import annotations

import json
import time
from collections.abc import Callable, Iterator, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Final

from saddle.journal import redact_secrets

CONVERSATION_LOG: Final = "conversation.jsonl"
"""The log's name in a run's directory, beside its ledger."""


@dataclass
class ConversationLog:
    """Appends each request to `path` (module docstring)."""

    path: Path
    clock: Callable[[], float] = time.time
    requests: int = 0
    """How many requests have been recorded."""
    failed: str | None = None
    """Why the log stopped, or None while it is whole."""
    _sent: list[str] = field(default_factory=list)
    """Each message last written, as it was sent."""
    _tools: str | None = None
    """The last recorded request's tool schemas, as sent."""

    def record(
        self,
        messages: Sequence[Mapping[str, Any]],
        *,
        max_tokens: int,
        tools: Sequence[Mapping[str, Any]],
    ) -> None:
        """Log the request about to be sent: `messages`, `tools` and its reply cap."""
        number = self.requests + 1
        head = {"request": number, "sent": round(self.clock(), 3), "max_tokens": max_tokens}
        if self._write(f"request {number}", head, messages, tools):
            self.requests = number

    def end(self, messages: Sequence[Mapping[str, Any]]) -> None:
        """Log the conversation as the turn ended: what came after its last request."""
        head = {"end": self.requests, "ended": round(self.clock(), 3)}
        self._write(f"the end after request {self.requests}", head, messages, None)

    def _write(
        self,
        what: str,
        head: dict[str, Any],
        messages: Sequence[Mapping[str, Any]],
        tools: Sequence[Mapping[str, Any]] | None,
    ) -> bool:
        if self.failed is not None:
            return False
        try:
            texts = [json.dumps(m) for m in messages]
            kept = 0
            for now, before in zip(texts, self._sent, strict=False):
                if now != before:
                    break
                kept += 1
            written = [json.dumps(_redacted(messages[i])) for i in range(kept, len(texts))]
            head |= {"kept": kept, "messages": len(texts)}
            changed = [i for i, line in enumerate(written, kept) if line != texts[i]]
            if changed:
                head["redacted"] = changed
            schemas = None if tools is None else json.dumps(tools)
            if schemas is not None and schemas != self._tools:
                head["tools"] = _redacted(tools)
                if json.dumps(head["tools"]) != schemas:
                    head["tools_redacted"] = True
            with self.path.open("a", encoding="utf-8") as log:
                log.write("\n".join([json.dumps(head), *written]) + "\n")
        except (OSError, TypeError, ValueError) as exc:
            self.failed = f"{what}: {exc}"
            return False
        self._sent = texts
        if schemas is not None:
            self._tools = schemas
        return True


class ConversationError(RuntimeError):
    """The log does not hold what was asked of it, or is cut short."""


@dataclass(frozen=True)
class Request:
    """One request as it was sent."""

    number: int
    sent: float
    max_tokens: int
    kept: int
    """How many leading messages were the previous request's."""
    messages: list[dict[str, Any]]
    tools: list[dict[str, Any]]
    redacted: tuple[int, ...] = ()
    """The messages, by index, that secret redaction changed: not exactly as sent."""
    tools_redacted: bool = False
    """Whether redaction changed the tool schemas."""

    @property
    def added(self) -> list[dict[str, Any]]:
        """The messages this request sent that the previous one had not."""
        return self.messages[self.kept :]


@dataclass(frozen=True)
class Ending:
    """The conversation as the turn ended."""

    after: int
    """How many requests were sent before it."""
    ended: float
    kept: int
    """How many leading messages were the last request's."""
    messages: list[dict[str, Any]]
    redacted: tuple[int, ...] = ()

    @property
    def added(self) -> list[dict[str, Any]]:
        """What came after the last request: its reply and the reply's tool results."""
        return self.messages[self.kept :]


def requests(path: Path) -> Iterator[Request]:
    """Each request in the log at `path`, in the order sent.

    Raises `ConversationError` where the log is cut, after every whole request before it."""
    for head, messages, redacted, tools, tools_redacted in _replay(path):
        if "request" in head:
            yield Request(
                head["request"],
                head["sent"],
                head["max_tokens"],
                head["kept"],
                messages,
                tools,
                redacted,
                tools_redacted,
            )


def request_at(path: Path, number: int) -> Request:
    """Request `number` (the first is 1), rebuilt from the log at `path`."""
    last = 0
    for request in requests(path):
        if request.number == number:
            return request
        last = request.number
    msg = f"{path} holds {last} request(s), not request {number}"
    raise ConversationError(msg)


def ending(path: Path) -> Ending | None:
    """How the conversation stood when the turn ended, or None if the log holds no end:
    a run that did not end normally, or a log that stopped before it did."""
    last: Ending | None = None
    for head, messages, redacted, _tools, _tools_redacted in _replay(path):
        if "end" in head:
            last = Ending(head["end"], head["ended"], head["kept"], messages, redacted)
    return last


_FIELDS: Final = {"request": ("sent", "max_tokens"), "end": ("ended",)}
"""What each kind of first line holds beside `kept` and `messages`."""


def _replay(
    path: Path,
) -> Iterator[
    tuple[dict[str, Any], list[dict[str, Any]], tuple[int, ...], list[dict[str, Any]], bool]
]:
    """Each first line in the log, with the messages, redactions and tools it leaves."""
    messages: list[dict[str, Any]] = []
    tools: list[dict[str, Any]] = []
    redacted: list[int] = []
    tools_redacted = False
    with path.open(encoding="utf-8") as log:
        lines = enumerate(log, 1)
        for index, line in lines:
            head = _parse(path, index, line)
            kind = next((k for k in _FIELDS if k in head), None)
            if kind is None or any(k not in head for k in ("kept", "messages", *_FIELDS[kind])):
                msg = f"{path} line {index} is not a request's or an end's first line"
                raise ConversationError(msg)
            what = f"{kind} {head[kind]}"
            kept, total = head["kept"], head["messages"]
            if not 0 <= kept <= min(total, len(messages)):
                msg = f"{path} line {index}: {what} keeps {kept} of {len(messages)}"
                raise ConversationError(msg)
            messages = messages[:kept]
            while len(messages) < total:
                following = next(lines, None)
                if following is None:
                    msg = f"{path} is cut inside {what}: {len(messages)} of {total}"
                    raise ConversationError(msg)
                messages.append(_parse(path, *following))
            redacted = [i for i in redacted if i < kept] + head.get("redacted", [])
            if "tools" in head:
                tools, tools_redacted = head["tools"], head.get("tools_redacted", False)
            yield head, list(messages), tuple(redacted), tools, tools_redacted


def _redacted(value: Any) -> Any:
    """`value` with every string in it redacted, at any depth."""
    if isinstance(value, str):
        return redact_secrets(value)
    if isinstance(value, Mapping):
        return {key: _redacted(item) for key, item in value.items()}
    if isinstance(value, list | tuple):
        return [_redacted(item) for item in value]
    return value


def _parse(path: Path, index: int, line: str) -> dict[str, Any]:
    if not line.endswith("\n"):
        msg = f"{path} is cut at line {index}"
        raise ConversationError(msg)
    try:
        value = json.loads(line)
    except json.JSONDecodeError:
        msg = f"{path} line {index} is not JSON"
        raise ConversationError(msg) from None
    if not isinstance(value, dict):
        msg = f"{path} line {index} is not a record"
        raise ConversationError(msg)
    return value
