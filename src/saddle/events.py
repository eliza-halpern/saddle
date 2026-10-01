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
    mode: str = "ask"
    full_access: bool = False
    """The session's Edit-lane commands run outside the sandbox
    (`Session.full_access`); the page shows a banner while it is on."""
    branch: str = ""
    """The folder's git branch, or "" when it is not a git checkout."""
    context_used: int = 0
    context_limit: int = 175_000
    messages: list[dict[str, Any]] = field(default_factory=list)
    kind: str = "session.info"


# -- an autonomous run, seen from the chat ------------------------------------
#
# The four below are what the engine and an auditor emit *during* a run, and
# what the chat's task card is drawn from. None of them is evidence: every one
# that matters is also sealed in the run's ledger, and the packet is compiled
# from the ledger, never from these.


@dataclass(frozen=True)
class RunProgress(Event):
    """What an autonomous run has spent so far, after each round.

    With `partial`, sent while a reply streams: `tokens` then includes that
    reply's tokens so far, estimated from its text, and no round has ended.
    """

    elapsed_s: float
    time_budget_s: float
    tokens: int
    token_budget: int
    partial: bool = False
    kind: str = "run.progress"


@dataclass(frozen=True)
class AuditFinding(Event):
    """One auditor verdict on the tree, sealed as an `audit:<gate>` span.

    The seam for the auditor lane: nothing in this branch computes one; an
    auditor hook passed to `run_auto` may return them after any tool call.
    """

    gate: str
    ok: bool
    detail: str
    span_id: str = ""
    kind: str = "audit.finding"


@dataclass(frozen=True)
class Question(Event):
    """The run halts on something only the user can decide (rule D)."""

    id: str
    text: str
    options: list[str] = field(default_factory=list)
    span_id: str = ""
    kind: str = "question"


@dataclass(frozen=True)
class Answered(Event):
    """The user's answer, sealed as an `answer` span chained to its question."""

    id: str
    text: str
    span_id: str = ""
    kind: str = "question.answered"


# -- the chat's envelope around a run -----------------------------------------


@dataclass(frozen=True)
class TaskState(Event):
    """A task card's state: running, needs_you, finished, stopped, failed."""

    run_id: str
    state: str
    task: str = ""
    detail: str = ""
    time_budget_s: float = 0.0
    token_budget: int = 0
    question: dict[str, Any] | None = None
    test_edits: bool = False
    """Whether this run may edit test files (`allow_test_edits`)."""
    elapsed_s: float | None = None
    """Time the run has spent, from its own budget (`engine.RunBudget`):
    time waiting on the user's answer is not in it. None before the run has
    a budget. A card rebuilt from this snapshot starts its meter here."""
    tokens: int | None = None
    """Generated tokens the run has spent, from the same budget."""
    kind: str = "task.state"


@dataclass(frozen=True)
class TaskEvent(Event):
    """One engine event of a run, wrapped so the chat's own turn ignores it."""

    run_id: str
    event: dict[str, Any]
    kind: str = "task.event"


@dataclass(frozen=True)
class TaskPhase(Event):
    """What a run is doing now, read from its events and ledger (`tasks.phase_for`).

    The card's head says `phase · round N · last event Xs ago`; the age is
    the page's own clock, so this is sent only when the phase or round moves.
    """

    run_id: str
    phase: str
    round: int = 1
    kind: str = "task.phase"


@dataclass(frozen=True)
class TaskLine(Event):
    """One sealed ledger entry of a run, as a session line."""

    run_id: str
    mark: str
    text: str
    cite: str
    tone: str = ""
    kind: str = "task.line"
