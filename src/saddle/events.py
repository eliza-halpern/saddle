"""The event stream a chat turn emits, independent of any display.

`chat.py` used to call `Timeline` methods directly, which tied the engine to
one renderer. The engine now yields these, and a renderer consumes them: the
terminal timeline and the web UI are two consumers of one stream, so neither
can drift from what actually happened.

Every event is JSON-serialisable, because the web transport is server-sent
events and the journal already stores JSON.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any, Literal

Phase = Literal["running", "ok", "failed"]


class Event:
    """Base with no fields, so subclasses control their own argument order.

    `kind` is the discriminator the transport and the UI switch on; each
    subclass declares it last, with a default, so its own fields stay
    positional. It is annotated here, without a value, so a consumer holding
    an `Event` can read `.kind`: a bare annotation on a non-dataclass base is
    not collected as a field by `@dataclass`, so subclass argument order is
    untouched.
    """

    kind: str

    def payload(self) -> dict[str, Any]:
        return dict(asdict(self))  # type: ignore[call-overload]


@dataclass(frozen=True)
class TurnStart(Event):
    turn: int
    prompt: str
    kind: str = "turn.start"


@dataclass(frozen=True)
class ReasoningDelta(Event):
    """One chunk of reasoning. The UI streams these into a collapsible block.

    Never truncated: the model's own reasoning is the most informative
    artifact a failed turn leaves behind, and cutting it is how a diagnosis
    becomes unavailable two hours later.
    """

    text: str
    kind: str = "reasoning.delta"


@dataclass(frozen=True)
class ContentDelta(Event):
    text: str
    kind: str = "content.delta"


@dataclass(frozen=True)
class ToolStart(Event):
    """A tool call beginning. `present` is the label while it runs."""

    id: str
    name: str
    arguments: str
    present: str
    kind: str = "tool.start"


@dataclass(frozen=True)
class ToolEnd(Event):
    """A tool call finished. `label` is past tense, or "Failed to ..."."""

    id: str
    ok: bool
    label: str
    detail: str
    duration_ms: int
    preview: str | None = None
    """Workdir-relative path of an image this call wrote, if it wrote one."""
    version: str | None = None
    """Which stored version of it, so an older message keeps showing what it
    produced rather than whatever the file says now."""
    kind: str = "tool.end"


@dataclass(frozen=True)
class TerminalOutput(Event):
    """A chunk of live output from a long-running command."""

    id: str
    chunk: str
    stream: Literal["stdout", "stderr"] = "stdout"
    kind: str = "terminal.output"


@dataclass(frozen=True)
class Context(Event):
    """How full the window is. Sent each turn so filling up is visible
    before compaction evicts anything, rather than announced after."""

    used: int
    limit: int
    kind: str = "context"


@dataclass(frozen=True)
class TurnEnd(Event):
    turn: int
    proof: str
    kind: str = "turn.end"


@dataclass(frozen=True)
class ErrorEvent(Event):
    message: str
    kind: str = "error"


@dataclass(frozen=True)
class AuditNote(Event):
    """Audit findings an autonomous run delivered to the model (arm E+A+F).

    `text` is exactly what was appended to the model's next tool result (or
    its nudge): one block per completed audit, headed
    `[audit <checkpoint n|finish> on tree <id>: PASS|FAIL]`, then one line
    per failing finding (`- gate (tier t): verdict, reason: detail`). A UI
    can render it as-is beside the tool call it arrived with. Findings that
    were withheld (arm E+A) are journaled but never emitted as this event;
    a refused `finish` shows its findings in that call's `ToolEnd` detail.
    """

    text: str
    kind: str = "audit"


@dataclass(frozen=True)
class Compaction(Event):
    """Older turns were summarised to fit the window (see memory.py)."""

    dropped_messages: int
    kept_messages: int
    summary: str
    kind: str = "compaction"


@dataclass(frozen=True)
class SessionTitle(Event):
    """A session named itself from the message that opened it."""

    session_id: str
    title: str
    kind: str = "session.title"


@dataclass(frozen=True)
class SessionInfo(Event):
    """Sent once on connect so a reloading client can rebuild its state."""

    session_id: str
    title: str
    workdir: str
    persona: str
    reasoning_effort: str = "xhigh"
    temperature: float = 1.0
    context_used: int = 0
    context_limit: int = 175_000
    messages: list[dict[str, Any]] = field(default_factory=list)
    kind: str = "session.info"
