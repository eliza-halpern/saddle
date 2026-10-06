"""The turn engine: one user message in, a stream of events out.

`chat.py` drove a `Timeline` directly, which welded the loop to one
renderer. This yields `events.Event` instead, so the terminal REPL and the
web UI consume the same stream and cannot disagree about what happened.

The engine owns: streaming, the tool-call rounds, journaling each call as a
span, sealing the turn as a proof, and memory compaction. It owns no
formatting at all.
"""

from __future__ import annotations

import base64
import json
import math
import mimetypes
import queue
import re
import threading
import uuid
from collections.abc import Callable, Generator, Iterator, Sequence
from contextlib import closing
from dataclasses import dataclass, field, replace
from pathlib import Path
from time import monotonic, perf_counter
from typing import Any, Final, Protocol

from saddle.events import (
    Answered,
    AuditFinding,
    AuditNote,
    Compaction,
    ContentDelta,
    Context,
    ErrorEvent,
    Event,
    MessageDelivered,
    Question,
    ReasoningDelta,
    RunProgress,
    ToolEnd,
    ToolStart,
    TurnEnd,
    TurnStart,
)
from saddle.installs import INSTALL, INSTALL_REFUSED, REFUSE_INSTALL, Installs
from saddle.journal import (
    AUDIT_QUESTION_STOP,
    AUDIT_SPAN_HASHES,
    BLOCKED_STOP,
    COMPACTION_SPAN,
    GUARDED_STOP_PREFIX,
    MAX_THINKING_CHARS,
    PREMISE_DISPUTED_STOP,
    REFUSED_STOP,
    STALL_STOP,
    append_record,
    append_span,
    build_record,
    build_span,
    redact_secrets,
    run_audit_hashes,
    started_before,
    utc_now,
    write_attempt_sidecar,
)
from saddle.labels import label_for
from saddle.memory import (
    ASK_USER,
    CHARS_PER_TOKEN,
    REREAD,
    compact,
    estimate_tokens,
    is_test_command,
    run_state,
    trim_screenshots,
)
from saddle.recall import Recall
from saddle.sandbox import Terminal
from saddle.tools import (
    BLOCKED_TOOL,
    CHECK_TOOL,
    DISPUTE_TOOL,
    FINISH_TOOL,
    INSTALL_TOOL,
    PREMISE_TOOL,
    REFUSE_TOOL,
    REFUSED,
    TOOLS,
    ToolContext,
    embeddings_answer,
    execute_tool,
    offer_code_search,
    offer_computer,
    offer_image_tools,
    offer_screenshot,
    preview_for,
    state_image_fact,
)
from saddle.vision import images_message, server_accepts_images
from saddle.vllm import StreamUsage, ToolCall, VllmClient, VllmError, assistant_message

RECALL_HINT: Final = (
    " The full text of what was dropped or shortened can be searched with `recall`."
)

type TokenCounter = Callable[..., int | None]
"""`VllmClient.count_tokens`: the server's own tokeniser, or None if it has
no /tokenize endpoint."""

AUTO_NUDGE: Final = (
    "No one is here to reply. Keep working with the tools, or call finish "
    "when the task is done. If you cannot go on without something only the "
    "person can give, call blocked."
)
"""What an autonomous run is told when a round ends with no tool call. In
chat that ends the turn; here it would end the run with nothing recorded
as its outcome, so the run goes on until `finish`, a budget, or
`EMPTY_ROUND_CAP` such rounds in a row."""

WORKDIR_LINE: Final = (
    'Your working folder is {workdir}: files asked for "here" or "in this folder" '
    "go there, and your commands start there."
)
"""Said in every chat's system message. Live (rung 5), asked to export a file
"in this folder", the model did not know which folder that was and searched
another checkout; the prompt never said."""

EMPTY_REPLY_RETRIES: Final = 2
"""Empty chat replies (no text, no tool call) the model is told about in one
turn before the turn ends, saying so. F35: a model wrote its next tool call
inside its reasoning, the reply came back empty, and the turn ended silently
mid-work."""
EMPTY_REPLY_NUDGE: Final = (
    "Your last reply was empty: no text and no tool call reached the person. "
    "If you meant to call a tool, call it now; otherwise answer."
)
EMPTY_REPLIES: Final = "the model sent {n} empty replies in a row; the turn ended"


def empty_reply_nudge(empty_replies: int) -> str | None:
    """What the model is told after a turn's `empty_replies`-th empty reply:
    EMPTY_REPLY_NUDGE within EMPTY_REPLY_RETRIES, else None, and the caller
    ends the turn showing EMPTY_REPLIES. Shared by the web and terminal chats."""
    return EMPTY_REPLY_NUDGE if empty_replies <= EMPTY_REPLY_RETRIES else None


ANNOUNCED_ACTION: Final = re.compile(
    r"(?:^|[,;:\u2014\u2013]\s*|\s-\s+)"
    r"(?:(?:so|and|now|next|then|first|ok|okay)\b,?\s+)*"
    r"(?:I['\u2019]ll|I\s+will|I['\u2019]m\s+going\s+to|I\s+am\s+going\s+to"
    r"|let\s+me(?!\s+know\b))\b"
    r"|(?:^|[,;:\u2014\u2013]\s*)next,?\s+I\s+[a-z]",
    re.IGNORECASE,
)
"""A first-person announcement of the model's own next step ("I'll ...",
"I will ...", "I'm going to ...", "Let me ...", "Now I'll ...", "Next, I
...") at the start of a clause. F39: a live chat turn ended on "..., so I'll
determine it empirically from the DLL imports." with no tool call, and the
person had to type "continue". "Let me know ..." is excluded by the
lookahead: it invites the person to act, it does not announce the model's
action. Read by `announces_action` on the reply's last sentence only."""
ANNOUNCE_NUDGE: Final = (
    "You said you would do something next, but you made no tool call, so "
    "nothing happened. Make the call now; if you need the person first, ask them."
)
WAITING_NUDGE_HEAD: Final = "Your background command is still running"
WAITING_NUDGE: Final = (
    WAITING_NUDGE_HEAD + " (terminal {id}: {command}) and you made no tool call, "
    "so nothing is waiting for it. If you need its result, call wait_for_terminal "
    "with id {id}; if you meant to leave it running, say so."
)
"""B8 (2026-10-04): the model started a render with `background`, ended its
reply on "Waiting for completion." and made no call, so the turn ended with no
report while the render ran on. `ANNOUNCED_ACTION` needs a first-person "I'll"
or "Let me" and missed it; the sandbox knows the command is still running, so
the harness says so instead of guessing from the wording."""


CUT_CALL: Final = (
    "error: your {name} call was cut off before it was complete (its arguments are "
    "not valid JSON: {why}), most likely because your reply reached its output token "
    "limit; nothing ran. Make the call again with less in it: split a large file into "
    "several smaller files, or write it in parts."
)
"""B9 (2026-10-04): a write_file reply reached its output limit mid-string. The
call is answered with this instead of being run."""


def _cut_arguments(arguments: str) -> str | None:
    """Why `arguments` is not a complete JSON value, or None when it is."""
    try:
        json.loads(arguments)
    except json.JSONDecodeError as exc:
        return str(exc)
    return None


def _sendable_arguments(arguments: str) -> str:
    """The arguments as kept in the history. The server parses every earlier
    tool call's arguments, so a cut-off one kept as it came made it refuse each
    later request (HTTP 400) and ended the session (B9); it is kept as `{}`."""
    return arguments if _cut_arguments(arguments) is None else "{}"


def _clip_command(command: str, limit: int = 120) -> str:
    """A command as one short line for a nudge: its first line, cut at `limit`."""
    first = command.strip().splitlines()[0] if command.strip() else command
    return first if len(first) <= limit else first[: limit - 3] + "..."


def running_since(ctx: ToolContext, since: float) -> Terminal | None:
    """The first background command started at or after `since` (this turn)
    that is still running, or None. A command left running by an earlier turn
    (a server started on purpose) is not this turn's unfinished work."""
    if ctx.sandbox is None:
        return None
    for terminal in ctx.sandbox.terminals.values():
        if terminal.running and terminal.started >= since:
            return terminal
    return None


_SENTENCE_BREAK: Final = re.compile(r"(?<=[.!?])\s+|\n")


def announces_action(reply: str) -> bool:
    """True when the reply's last sentence announces the model's next action
    (`ANNOUNCED_ACTION`) and asks the person nothing (no "?" in it).

    Only the last sentence counts: an answer that says "Let me explain" early
    and ends on a statement is finished. A last sentence with a question mark
    hands the turn to the person, whatever else it says."""
    sentences = [s.strip() for s in _SENTENCE_BREAK.split(reply) if s.strip()]
    if not sentences:
        return False
    last = sentences[-1]
    return "?" not in last and ANNOUNCED_ACTION.search(last) is not None


EMPTY_ROUND_CAP: Final = 3
"""Consecutive rounds with no tool call before an autonomous run stops.

The finish refusal cap counts `finish` calls, so a model that answers a
refused finish with empty turns never reaches it and ran to its wall budget
(#81). In the recorded `saddle auto` ledgers (202 runs, 2,280 rounds) a
round with no tool call is rare (6) and never came twice in a row: each was
followed by a tool call. Three is one more than any run has needed, so two
empty rounds in a row are still nudged and not stopped. Tightened: the loop
is bounded; the tree is never marked finished by it."""

EMPTY_ROUNDS: Final = "no tool call in {n} consecutive rounds"
"""The sealed stop reason when `EMPTY_ROUND_CAP` is reached."""

STALL_WARMUP_S: Final = 600.0
"""`--stall-check`: the rolling stall window is not armed until this many
seconds have elapsed. A run that has only been exploring is indistinguishable
from a stuck one before this: on the recorded runs the productive ones made
their first edit as late as ~19 min (`run3`), so an earlier arm ejects real
work. Ten minutes clears every productive first-edit in the sample."""

STALL_WINDOW_S: Final = 300.0
"""The trailing span of reasoning the stall check scores (five minutes)."""

STALL_HEDGE_PER_K: Final = 5.0
"""Hedge-marker matches per 1,000 reasoning words, over the window, at or above
which a run that has never acted is ejected. Tuned, NOT yet held-out: on the 30
recorded WATCH runs, at the moment a run is ejectable (past the warmup, not yet
acted), the stuck ones scored 5.3-7.1 and the productive ones 1.4-4.4 under this
lexicon; 5.0 caught 4 of 5 stalls with no false positives, but the margin is
thin (4.4 vs 5.3) and the threshold is fitted on that sample. Re-tune on
outcome-labelled held-out runs before trusting it off this branch."""

_HEDGE = re.compile(
    r"\bmaybe\b|\bperhaps\b|\bnot sure\b|\bunclear\b|\bunsure\b|\bconfus|\bi think\b"
    r"|\bi guess\b|\bseems?\b|\bapparently\b|\bpresumably\b|\bsomehow\b"
    r"|\bhold on\b|\bwhat (is|does|if|the)\b|\?\?|\bor is it\b"
    r"|\bmust (be|have|mean)\b|\bi'?m (confused|lost|missing)\b|\bdoesn'?t make sense\b"
    r"|\bwhy (is|does|would)\b|\bre-?read\b|\bcan'?t tell\b|\bnot clear\b",
    re.IGNORECASE,
)
"""Epistemic-hedging / confusion markers. The stuck-run signal is uncertainty,
not negative polarity (a productive debugging run is just as negative), so this
scores hedging, not sentiment. `hmm`, `wait` and `actually` are deliberately
excluded: on the recorded runs they were pure discourse noise, used as often by
productive deep-dives as by stuck ones (run9/10/11, same task, same
productivity, diverged only on those three words), so counting them measured
writing style, not confusion, and false-ejected 4/15 productive runs."""


def hedge_density(text: str) -> float:
    """Hedge-marker matches per 1,000 words of `text`; 0.0 for <50 words (too
    little to score). Deterministic and lexical, so it can vary and be tested."""
    words = len(text.split())
    if words < 50:
        return 0.0
    return len(_HEDGE.findall(text)) / words * 1000


STALLED: Final = (
    STALL_STOP + " -- {mins:.0f} min with no edit, premise_check or dispute "
    "and still turning the task over (hedging {density:.1f} of {threshold:.1f} per "
    "1k words). Read the reasoning and decide."
)
"""The sealed stop reason when `--stall-check` ejects a never-acted run. Built on
`STALL_STOP`, which the packet matches to render a needs-you verdict; no
semicolon, so the verdict line keeps the whole reason."""

STALL_TOOLS: Final = frozenset(
    {"write_file", "edit_file", "premise_check", "dispute", "refuse", "blocked", "finish"}
)
"""A call to any of these is a progress-action: it exempts the run from the
stall check for the rest of the run. An attempt counts (the run engaged), so a
refused edit or a premise_check that failed to reproduce still exempts."""


NO_LIMIT: Final = 0
"""A time or token budget of 0 (or less) means no limit: the run is never
stopped for spending, only by `finish`, an error, a cancel or the stall
check. The default for every run; a positive value opts back into a cap."""
_UNBOUNDED_TOKENS: Final = 1 << 62
"""What `remaining_tokens` reports with no token budget: large enough that
only the context window ever caps a reply, small enough to stay a plain int."""
_UNBOUNDED_S: Final = 365 * 24 * 3600.0
"""What `time_left` reports with no time budget: a year, so `int()` of it is
safe and no run reaches it, without the edge cases of `inf`."""


@dataclass
class RunBudget:
    """Wall time and generated tokens an autonomous run may spend.

    Generated tokens are the server's `completion_tokens` when the stream
    reported usage. Only a round without usage is estimated from the
    streamed text (reasoning, content and tool-call arguments,
    `CHARS_PER_TOKEN` per token); `by_source` keeps the two apart so the
    record never passes an estimate off as a measurement. The clock is
    injectable so a test can exhaust time without waiting for it.
    """

    time_s: float
    tokens: int
    clock: Callable[[], float] = monotonic
    started: float | None = None
    spent_tokens: int = 0
    by_source: dict[str, int] = field(default_factory=lambda: {"usage": 0, "estimate": 0})

    def start(self) -> None:
        if self.started is None:
            self.started = self.clock()

    def elapsed(self) -> float:
        return 0.0 if self.started is None else self.clock() - self.started

    def charge(self, text_chars: int, usage: StreamUsage | None = None) -> tuple[int, str]:
        """Charge one round; return (tokens, "usage" | "estimate")."""
        if usage is not None:
            tokens, source = usage.completion_tokens, "usage"
        else:
            tokens, source = max(1, text_chars // CHARS_PER_TOKEN), "estimate"
        self.spent_tokens += tokens
        self.by_source[source] += tokens
        return tokens, source

    def source(self) -> str:
        """What the total rests on: "usage", "estimate", "mixed", or "none"."""
        used = [name for name, tokens in self.by_source.items() if tokens]
        return used[0] if len(used) == 1 else ("mixed" if used else "none")

    def remaining_tokens(self) -> int:
        if self.tokens <= NO_LIMIT:  # no token budget: only the context window caps a reply
            return _UNBOUNDED_TOKENS
        return max(self.tokens - self.spent_tokens, 0)

    def time_left(self) -> float:
        """Seconds until the time budget runs out; a year when there is none
        (kept finite so a caller can `int()` it)."""
        if self.time_s <= NO_LIMIT:
            return _UNBOUNDED_S
        return self.time_s - self.elapsed()

    def near(self, fraction: float) -> str | None:
        """ "token" or "time" once that budget is `fraction` spent but not gone.

        A budget of 0 or less is no limit, so it is never "near": the extend
        question is never put and the run is never stopped for spending."""
        if self.tokens > NO_LIMIT and self.tokens * fraction <= self.spent_tokens < self.tokens:
            return "token"
        if self.time_s > NO_LIMIT and self.time_s * fraction <= self.elapsed() < self.time_s:
            return "time"
        return None

    def exhausted(self) -> str | None:
        """Which budget has run out, in words, or None (a budget of 0 or less
        is no limit and never runs out)."""
        if self.tokens > NO_LIMIT and self.spent_tokens >= self.tokens:
            return (
                f"token budget exhausted: ~{self.spent_tokens} of {self.tokens} "
                "generated tokens spent"
            )
        if self.time_s > NO_LIMIT:
            elapsed = self.elapsed()
            if elapsed >= self.time_s:
                return f"time budget exhausted: {elapsed:.0f}s of {self.time_s:.0f}s spent"
        return None


class AuditHooks(Protocol):
    """What the engine calls on an autonomous run's auditor (`feed.AuditFeed`).

    The engine imports no auditor; it only knows these four points.
    """

    def before_tool(self, name: str) -> None: ...
    def after_tool(self, name: str, ok: bool) -> None: ...
    def collect(self) -> str: ...
    def final(self) -> tuple[bool, str]: ...
    def check(self) -> str: ...
    @property
    def checks(self) -> Sequence[object]: ...
    def unresolved(self) -> list[dict[str, object]]: ...
    def close(self) -> None: ...
    def last(self) -> dict[str, object] | None: ...
    def accepted_unchanged(self) -> bool: ...
    def unchanged(self) -> bool: ...
    def waivers(self) -> list[str]: ...
    def questions(self) -> list[str]: ...


FINISH_REFUSED: Final = "error: finish refused: the audit of this tree failed. "
"""Prefix of `finish`'s result when the audit refuses it (arm E+A+F)."""

FINISH_SURFACED: Final = (
    "finish accepted: the audit passed and refuses nothing, but it could not prove "
    "what follows. Read it before you end the run: write tests that prove these "
    "behaviours if you can, then call finish again. Calling finish again with no "
    "change ends the run finished.\n\n"
)
"""Prefix of `finish`'s result when the audit accepts it and surfaces
not-proven findings (`feed.AuditFeed.final`). Not an
error and not a refusal: `finish_refusals` and the cap are untouched."""

DEFAULT_FINISH_REFUSAL_CAP: Final = 3
"""Consecutive `finish` refusals on an unchanged failing finding set before
the run stops (in a measured run a correct T5 tree was refused 7,244 times until the
token budget ran out). Tightened: the loop is bounded; the tree is never
marked finished."""

UNCHANGED: Final = "unchanged"
"""The third ending: `finish` on a tree equal to the
baseline. Not accepted (not `finished`), not a refusal (no count, no cap),
sealed as `auto:unchanged`."""

FINISH_UNCHANGED: Final = (
    "unchanged: the tree equals the baseline, so there is nothing to audit. "
    "The run ends `unchanged`: not finished, and not a refusal. Your summary is "
    "recorded as narrative, not as evidence."
)
"""`finish`'s result when the run ends `unchanged`."""

FINISH_QUESTION: Final = (
    "finish recorded, but the audit asks a question only a person can answer, "
    "so the run ends here, needs you. Your summary is recorded as narrative.\n\n"
)
"""`finish`'s result when the finish audit accepted the tree with a `question`."""

QUESTION_REASON_CHARS: Final = 300
"""How much of the questions the stop reason carries; the audit sidecar keeps them whole."""


def needs_you_reason(asked: Sequence[str]) -> str:
    """The stop reason for a finish whose audit asked `asked`: rule D's
    "needs you: ..." form, with no semicolon, since the packet's verdict
    line keeps only what precedes the first one."""
    text = " | ".join(asked).replace(";", ",")
    if len(text) > QUESTION_REASON_CHARS:
        text = text[: QUESTION_REASON_CHARS - 3] + "..."
    return f"{AUDIT_QUESTION_STOP}{len(asked)} question(s): {text}"


AUDIT_UNRESOLVED: Final = "audit unresolved"
"""The sealed stop reason when the finish refusal cap is reached."""

GUARDED_STOP: Final = (
    GUARDED_STOP_PREFIX + " ({paths}), so a person must review it before it counts as finished"
)
"""The stop reason when a finished run on saddle's own source changed a
guarded path (`auto.GUARDED_PATHS`). Rule D's stop form ("needs you: ..."):
the outcome is `stopped`, never `finished`. No semicolon, so the packet's
verdict line (`packet.compile_packet`) keeps the whole reason."""

TEST_CLOSES: Final = frozenset({"coverage", "evidence-thin"})
"""A finding gate or reason that a new test is the repair for (`packet.TEST_CLOSES`)."""

ALLOW_TEST_EDITS: Final = "Allow"
KEEP_READ_ONLY: Final = "Keep read-only"
"""The test-edit question's options; the second is the default."""

UNANSWERED: Final = "unanswered"
"""Third argv word of an `answer` span sealed without a reply: nobody could
answer (a headless run) or the run was stopped while it waited, so the
question's conservative default was taken."""

EXTEND_BUDGET: Final = "Extend"
STOP_AT_LIMIT: Final = "Stop at limit"
"""The budget question's options; the second is the default."""

BUDGET_ASK_AT: Final = 0.8
"""The share of the time or token budget spent at which a running run asks,
once, whether to extend it by the same amount again."""

CUT_AT_MARK: Final = "reply cut at 80% of the token budget for the budget question"
CUT_AT_LIMIT: Final = "reply cut at the token limit"
CUT_AT_TIME: Final = "reply cancelled at the time limit"
"""How a reply that did not end by itself is named in its `auto:spend`
record (`cut`) and, for the time limit, in the stop reason."""

CUT_NUDGE: Final = (
    "Your last reply was cut off at a token limit before it called a tool. " + AUTO_NUDGE
)
"""What the model is told after a reply cut at the 80% mark: the cut is the
harness's, so the round does not count toward `EMPTY_ROUND_CAP`."""

STREAM_POLL_S: Final = 1.0
"""How often, in real seconds, a run looks up from a streaming reply, whether
or not the reply has sent anything: to cancel it at the time limit, to ask
the time budget's question, and to report the reply's tokens so far. A reply
in flight is cancelled at most this long after the time budget runs out."""

_CITE: Final = re.compile(r"[\w./-]+\.\w+:\d+")


@dataclass
class AutoRun:
    """What makes a turn an autonomous run, and what it ended as.

    The engine fills `outcome`: "finished" only when the model called
    `finish`; "stopped" for a budget, a model error or a cancel; and, with
    an auditor, "unchanged" when `finish` is called on a tree equal to the
    baseline. A stop never reads as done, and neither
    does an unchanged tree.

    `feed`, when set (arms E+A and E+A+F), is called before and after each
    tool, its completed audits are appended to the next tool result, and it
    decides whether `finish` is accepted (`feed.AuditFeed`). It is the
    source of audit findings.

    The auditor seam: `audit`, if set, is called with each tool call's
    name, arguments and result, and returns `AuditFinding` or `Question`
    events. Each is sealed in the ledger, shown to the caller, and appended
    to the tool result the model reads. A `Question` halts the run until
    `answer` returns the user's reply; with no `answer`, or a None reply,
    the run stops "needs you" rather than guessing (rule D).
    """

    budget: RunBudget
    run_span: str
    """The `auto:start` span every tool span of this run cites as parent."""
    changed_files: Callable[[], list[str]] = list
    outcome: str = ""
    reason: str = ""
    narrative: str = ""
    span_hashes: list[str] = field(default_factory=list)
    refusals: int = 0
    feed: AuditHooks | None = None
    finish_refusals: int = 0
    finish_refusal_cap: int = DEFAULT_FINISH_REFUSAL_CAP
    """Stop after this many consecutive refusals whose failing finding set
    (gate, reason, cites) is unchanged; a changed set restarts the count."""
    unchanged_refusals: int = 0
    """Length of the current run of refusals on one unchanged finding set."""
    unresolved: list[dict[str, object]] = field(default_factory=list)
    """The failing findings of the last refusal (gate, reason, cites)."""
    arm: str = "E"
    """Which Phase 2 arm this run is ("E", "E+A", "E+A+F"); sealed in the
    outcome sidecar so the ledger alone tells the arms apart."""
    sealed: dict[str, object] = field(default_factory=dict)
    """The run's settings (sampling, test-edit policy, sanctioned rewrites),
    copied into the outcome sidecar so a reader need not trust argv."""
    audit: Callable[[str, str, str], Sequence[Event]] | None = None
    answer: Callable[[Question], str | None] | None = None
    check_tool: bool = False
    """`saddle auto --check-tool`: a `check` call runs `feed.check()` (tiers 0
    and 1). Off, the tool is not offered and a `check` call is an unknown tool."""
    asked: set[str] = field(default_factory=set)
    """Which of the run's own questions ("test-edits", "budget") were put; each at most once."""
    installs: Installs | None = None
    """`saddle auto --allow-installs`: an `install` call is checked by
    `installs.plan`, put to the user as a question every time, and carried
    out only on Install (`_install`). None: the tool is not offered, and a
    call to it is an unknown tool."""
    require_premise: bool = False
    """`--premise-check`: edits are refused until `premise_check` ran (`_premise`)."""
    premise: dict[str, Any] | None = None
    """The model's `premise_check`: its claim and each command with saddle's rerun
    output; sealed in the outcome. None until called."""
    dispute: dict[str, Any] | None = None
    """The model's `dispute` (`_dispute`): its claim, finding and each evidence
    command with saddle's own rerun output; sealed in the outcome. None unless called."""
    refusal: dict[str, Any] | None = None
    """The model's `refuse` (`_refuse`): its reason for declining the task; sealed
    in the outcome. None unless called. No evidence -- a refusal is not a claim
    about the code."""
    blocked: dict[str, Any] | None = None
    """The model's `blocked` (`_blocked`): what blocks it and what it already
    tried; sealed in the outcome. None unless called."""
    surfaced: str | None = None
    """The summary of the finish that was accepted with surfaced not-proven
    findings (`FINISH_SURFACED`); None until one is. A run that then stops
    on that same tree ends finished with it."""
    tell_summary: Callable[[str], None] | None = None
    """Hands `finish`'s summary to the auditor before the finish audit
    (`feed.AuditFeed.tell_summary`): a `flip:` line is read from it. None does nothing."""
    before_finish: Callable[[], str] | None = None
    """Run before each `finish` audit (`saddle auto --format-at-finish`):
    formats the run's changed Python files and says what it did, which the
    model is told. None does nothing."""
    guard: Callable[[], list[str]] | None = None
    """The self-guard (`auto.self_guard`): set only when the run is on saddle's
    own source, it names the guarded paths the run changed. A run that would
    end `finished` with any ends `stopped`, reason `GUARDED_STOP`, and seals
    them as `guarded_paths`."""
    prompt_check: Callable[[], dict[str, object]] | None = None
    """`prompt_constants.check` over the run's tree, called once at the seal
    and sealed as `prompt_constants`; None when the task
    names no constant, and then nothing is sealed."""
    waivers: list[str] | None = None
    """`feed.waivers` of the last accepted finish audit; None until one is.
    Sealed on a finished run with an auditor."""
    last_test: tuple[str, str] | None = None
    """The newest test command the run ran and its full result, for the
    compaction state block (`memory.run_state`)."""
    delivered_audit: str = ""
    """The newest audit text the model was shown (a delivered checkpoint, a
    refused finish's findings, or an accepted finish's surfaced not-proven
    findings). A withheld audit (arm E+A) never lands here."""
    compactions: int = 0
    empty_rounds: int = 0
    """Length of the current run of rounds with no tool call."""
    stall_check: bool = False
    """`--stall-check`: after `STALL_WARMUP_S`, a run that has never called an
    edit, premise_check or dispute and whose trailing reasoning stays at or
    above `STALL_HEDGE_PER_K` hedging is stopped and returned to the user
    (`STALLED`). Off: nothing is scored and the run is never ejected for it."""
    ever_acted: bool = False
    """Set once the run calls any `STALL_TOOLS` tool. A run that has acted is
    exempt from the stall check for the rest of the run."""
    reasoning_log: list[tuple[float, str]] = field(default_factory=list)
    """(elapsed_seconds, round reasoning) per round, for the trailing-window
    hedge score. Only appended when `stall_check` is on."""

    def stall_density(self, window_s: float = STALL_WINDOW_S) -> float:
        """Hedge density over the reasoning of the last `window_s` seconds."""
        if not self.reasoning_log:
            return 0.0
        now = self.reasoning_log[-1][0]
        window = " ".join(t for (s, t) in self.reasoning_log if s >= now - window_s)
        return hedge_density(window)

    def stop(self, reason: str) -> None:
        if not self.outcome:
            self.outcome, self.reason = "stopped", reason

    def finish(self, narrative: str) -> None:
        if not self.outcome:
            self.outcome, self.reason, self.narrative = "finished", "finish called", narrative

    def end_unchanged(self, narrative: str) -> None:
        if not self.outcome:
            self.outcome, self.narrative = UNCHANGED, narrative
            self.reason = "finish called on a tree equal to the baseline; no change was made"


OUTPUT_MARGIN: Final = 2048
"""Headroom kept back so a reply is never sized right up to the window."""

INPUT_SAFETY: Final = 2.0
"""Only for the fallback path, where the count has to be guessed.

The server tokenises the prompt itself and `VllmClient.count_tokens` asks
it, so the budget is normally exact. This factor applies when that endpoint
is missing and `estimate_tokens` -- characters over four -- is all there is.
Crude there means optimistic: measured on this server, 2,732 estimated
against 4,100 charged. 1.5 rounded up, so content that tokenises worse than
English prose still fits.

Erring high costs reply budget in a conversation large enough to be
compacting anyway. Erring low costs the whole turn: HTTP 400, empty reply."""

CALIBRATION_MARGIN: Final = 1.2
"""Headroom on a calibrated estimate: the measured ratio is the last
request's, and the next one may hold text that tokenises worse."""

RATIO_FLOOR: Final = 1.0
"""The lowest calibration ratio believed. Measured ratios on real servers run
1.22 (Strata) to 1.5 (vLLM, 2,732 estimated against 4,100); a lower one is a
fake or a misreported usage, and erring low costs the turn (HTTP 400)."""

COMPACT_TARGET: Final = 0.7
"""Compaction, once triggered, works down to this fraction of its trigger.

Compacting to just under the trigger made the next turn cross it again: one
run compacted 163 times, and each pass rewrites the prompt's head, so the
server re-reads it (time to first token 38 s after a pass, 14 s otherwise).
The shape is MemGPT's (trigger near 70% of the window, evict to ~50%); with
the exact trigger at ~73% of a 131,072 window, 0.7 of it is ~51%."""

CHAT_TEMPERATURE: Final = 1.0
"""Sampling temperature for a browser chat turn.

Deliberately not the 0.0 the rest of saddle uses, and the difference is a
contract change rather than a tweak: a run's claims depend on the same
prompt producing the same bytes, so `saddle run` stays greedy. A
conversation is not a measurement. Greedy decoding made chat answers
identical on every retry, pushed long generations toward repetition, and
flattened persona voices by always taking the single likeliest token.

This applies only here. `TurnOptions` is used by `saddle.web` and nothing
else -- `saddle up` has its own turn loop and `saddle run` never touches
this module -- so no measured path is affected. Session titling stays at
0.0 on purpose: a name should not change each time it is asked for."""

MIN_OUTPUT: Final = 8192
REPLY_ROOM: Final = 32_768
"""Real tokens kept free for the reply when the prompt is compacted: a
working reply (reasoning plus a tool call) runs to many thousands of tokens,
and MIN_OUTPUT is the floor a reply is refused below, not what it needs."""
"""Floor for a reply's budget even in a nearly-full window. Below this a
reply gets cut mid-sentence, which is worse than compacting harder."""


@dataclass
class TurnOptions:
    workdir: Path = Path(".")
    journal: Path = Path(".saddle/chat.jsonl")
    max_tokens: int | None = None
    """`None` means size it from the window left after the conversation, the
    way `saddle run` does: reasoning is not budgeted apart from the reply.
    A flat 8192 was the old chat default and it was stingy: this server
    reports a 175,000-token window, and a turn reasoning at `xhigh` can
    spend 40,000-80,000 tokens thinking before it writes a word. A reply
    truncated mid-thought is the single most annoying failure a chat UI
    has."""
    temperature: float = CHAT_TEMPERATURE
    reasoning_effort: str = "medium"
    system_prompt: str = ""
    context_tokens: int = 175_000
    tools: list[dict[str, Any]] = field(default_factory=lambda: list(TOOLS))
    auto: AutoRun | None = None
    """Set for an autonomous run: no round cap, a budget, `finish`."""
    prompt_ratio: float | None = None
    """The server's prompt tokens over `estimate_tokens` plus `tool_tokens`
    for the last request it reported usage on; `None` until one has. Set by
    the turn loop. Where the server cannot count (no /tokenize), it turns
    the estimate into real tokens instead of the blanket `INPUT_SAFETY`."""
    keep_reasoning: bool = True
    """Send each round's reasoning back on its assistant message, in every
    mode: autonomous runs (`AutoOptions.keep_reasoning`) and interactive chat
    alike, as the untouched agent does (SPEED F-a). The served template
    renders a past assistant turn's reasoning, so without it a long tool loop
    sees what the model did and never why. Off only where a person turns it
    off (`--no-keep-reasoning`)."""

    def tool_tokens(self) -> int:
        """What the tool schemas cost, which they do on every single request.

        This was missing from the budget entirely. The schemas are ~680
        tokens here and they are sent with the prompt each turn, so the
        window was being promised to the reply twice.
        """
        return len(json.dumps(self.tools)) // CHARS_PER_TOKEN

    def input_estimate(self, messages: list[dict[str, Any]]) -> int:
        """A guess at the prompt size, for when the server cannot be asked:
        calibrated on the server's own count of the last prompt when there is
        one (`prompt_ratio` x `CALIBRATION_MARGIN`, stricter or looser than
        `INPUT_SAFETY` as measured), else the blanket factor. Measured on
        Strata, the ratio was ~1.22: x2.0 had compaction fire at ~57k real
        tokens of a 131,072 window."""
        raw = estimate_tokens(messages) + self.tool_tokens()
        factor = (
            INPUT_SAFETY if self.prompt_ratio is None else self.prompt_ratio * CALIBRATION_MARGIN
        )
        return int(raw * factor)

    def input_tokens(
        self, messages: list[dict[str, Any]], counter: TokenCounter | None = None
    ) -> int:
        """The prompt size: exact if the server will say, guessed if not."""
        if counter is not None:
            exact = counter(messages, tools=self.tools)
            if exact is not None:
                return exact
        return self.input_estimate(messages)

    def budget(self, messages: list[dict[str, Any]], counter: TokenCounter | None = None) -> int:
        """Tokens this reply may use: the window minus what is already in it.

        The failure this guards against is asymmetric. Asking for too few
        tokens shortens one reply; asking for one token too many is an HTTP
        400 and no reply at all -- and it is a *fresh* session that asks for
        the most, so the bug showed up as "new sessions do not work".
        """
        if self.max_tokens:
            return self.max_tokens
        left = self.context_tokens - self.input_tokens(messages, counter) - OUTPUT_MARGIN
        return max(left, MIN_OUTPUT)

    def compaction_limit_exact(self) -> int:
        """The compaction ceiling in real tokens (the server's count, tool
        schemas included): the window less `REPLY_ROOM` for the reply and
        `OUTPUT_MARGIN`. About 80% of a 175,000 window."""
        return self.context_tokens - REPLY_ROOM - OUTPUT_MARGIN

    def compaction_limit(self) -> int:
        """Estimate-scale ceiling that still leaves room to answer.

        Compaction used to be told the whole window, so it was content to
        let the conversation fill every token of it and leave the reply the
        `MIN_OUTPUT` floor -- which `budget` would then request on top of a
        full prompt. The reply keeps `REPLY_ROOM` here too (a quarter of a
        small window, never under `MIN_OUTPUT`): with only MIN_OUTPUT kept, a
        server without /tokenize (Strata) filled a 131,072 window to ~120k
        and cut a 25 KB write_file off mid-string at 8,774 tokens (B9).
        """
        reply = max(MIN_OUTPUT, min(REPLY_ROOM, self.context_tokens // 4))
        room = self.context_tokens - reply - OUTPUT_MARGIN
        return max(int(room / INPUT_SAFETY) - self.tool_tokens(), MIN_OUTPUT)

    def compaction_limit_calibrated(self) -> int:
        """`compaction_limit` in real tokens, for an estimate calibrated on
        the server's count (`prompt_ratio`), which already holds the tool
        schemas and its own margin, so no `INPUT_SAFETY` divides it. On a
        131,072 window this is 96,256, the exact path's limit."""
        reply = max(MIN_OUTPUT, min(REPLY_ROOM, self.context_tokens // 4))
        return max(self.context_tokens - reply - OUTPUT_MARGIN, MIN_OUTPUT)


def _reply_cap(
    client: VllmClient,
    messages: list[dict[str, Any]],
    options: TurnOptions,
    counter: TokenCounter | None = None,
) -> tuple[int, str]:
    """The reply's `max_tokens`, and the cut it names if the reply reaches it.

    In a run the cap is also what the token budget has left, so no reply can
    spend past it (`CUT_AT_LIMIT`); and while the budget question has not
    been put, what is left before the 80% mark (`CUT_AT_MARK`), so a reply
    that would carry the run past the mark stops there and the question is
    asked before the next round. The server counts these tokens itself, so
    the mark holds exactly, which a count of streamed text could not. The
    cut is "" when the context window, not a budget, set the cap.
    """
    cap = options.budget(messages, counter or getattr(client, "count_tokens", None))
    auto = options.auto
    if auto is None:
        return cap, ""
    budget = auto.budget
    room, cut = budget.remaining_tokens(), CUT_AT_LIMIT
    to_mark = math.ceil(budget.tokens * BUDGET_ASK_AT) - budget.spent_tokens
    if "budget" not in auto.asked and 0 < to_mark < room:
        room, cut = to_mark, CUT_AT_MARK
    if room < cap:
        return max(room, 1), cut
    return cap, ""


class _Tick:
    """What `_pump` yields when `STREAM_POLL_S` passes."""


_TICK: Final = _Tick()


def _pump(source: Generator[Any, None, None]) -> Iterator[Any]:
    """`source`'s items, read on a daemon thread, with `_TICK` every `STREAM_POLL_S`.

    A stalled server sends nothing, so a loop that only wakes on items can
    never stop it; this one wakes on the clock too. When the caller stops
    early, the thread drops what arrives next and closes `source`, which
    closes the HTTP stream (the server then stops generating). A reply that
    never sends another byte holds only that daemon thread, until the
    client's own read timeout.
    """
    box: queue.Queue[tuple[bool, Any]] = queue.Queue()
    abandoned = threading.Event()

    def work() -> None:
        try:
            for item in source:
                if abandoned.is_set():
                    break
                box.put((True, item))
            box.put((False, None))
        except Exception as exc:  # re-raised on the caller's thread
            box.put((False, exc))
        finally:
            source.close()

    threading.Thread(target=work, name="saddle-reply", daemon=True).start()
    last = perf_counter()
    try:
        while True:
            now = perf_counter()
            if now - last >= STREAM_POLL_S:
                last = now
                yield _TICK
                continue
            try:
                more, item = box.get(timeout=STREAM_POLL_S - (now - last))
            except queue.Empty:
                continue
            if more:
                yield item
            elif item is None:
                return
            else:
                raise item
    finally:
        abandoned.set()


def _stream(
    client: VllmClient, messages: list[dict[str, Any]], options: TurnOptions, cap: int
) -> Generator[tuple[str, Any], None, None]:
    """Yield ('reasoning'|'content', text), ('call', ToolCall), ('usage', StreamUsage)
    or ('tick', None) each `STREAM_POLL_S`."""

    def source() -> Generator[Any, None, None]:
        yield from client.stream_chat(
            messages,
            max_tokens=cap,
            temperature=options.temperature,
            reasoning_effort=options.reasoning_effort,
            tools=options.tools,
        )

    for event in _pump(source()):
        if event is _TICK:
            yield "tick", None
        elif isinstance(event, ToolCall):
            yield "call", event
        elif isinstance(event, StreamUsage):
            yield "usage", event
        elif event.stream == "reasoning":
            yield "reasoning", event.text
        else:
            yield "content", event.text


def _seal(
    journal: Path,
    *,
    turn: int,
    prompt: str,
    rounds: list[dict[str, Any]],
    reasoning: str,
    parent: str | None,
    kind: str = "",
) -> str:
    node_id = f"chat#{turn}"
    record = build_record(
        kind=kind,
        evidence_id=node_id,
        node_id=node_id,
        diff=json.dumps({"prompt": prompt, "rounds": rounds}),
        parent_proofs=[parent] if parent is not None else [],
        gate_outputs=[],
        requirement_ids=[],
        thinking=reasoning,
    )
    append_record(journal, record)
    return record.record_hash


def _last_user_index(messages: list[dict[str, Any]]) -> int:
    """Where the question this turn answers sits, or -1 if there is none."""
    for index in range(len(messages) - 1, -1, -1):
        if messages[index].get("role") == "user":
            return index
    return -1


def _last_asked(messages: list[dict[str, Any]]) -> str:
    """The question a retry is answering again, for the turn's own record."""
    for message in reversed(messages):
        if message.get("role") != "user":
            continue
        content = message.get("content")
        if isinstance(content, list):
            return next((p.get("text", "") for p in content if p.get("type") == "text"), "")
        return str(content or "")
    return ""


def _user_message(text: str, images: Sequence[Path]) -> dict[str, Any]:
    """A user turn, as text or as text plus images.

    The server accepts OpenAI-style content parts and this model reads them,
    so an uploaded screenshot is something it can actually look at rather
    than a filename it is told about. A file that cannot be read is skipped
    rather than failing the turn: the user still asked a question.
    """
    if not images:
        return {"role": "user", "content": text}
    parts: list[dict[str, Any]] = [{"type": "text", "text": text}]
    for path in images:
        try:
            raw = path.read_bytes()
        except OSError:
            continue
        mime = mimetypes.guess_type(path.name)[0] or "image/png"
        encoded = base64.b64encode(raw).decode()
        parts.append(
            {
                "type": "image_url",
                "image_url": {"url": f"data:{mime};base64,{encoded}"},
            }
        )
    return {"role": "user", "content": parts}


def run_turn(
    client: VllmClient,
    messages: list[dict[str, Any]],
    text: str | None,
    options: TurnOptions,
    *,
    turn: int,
    parent: str | None = None,
    context: ToolContext | None = None,
    images: Sequence[Path] = (),
    cancel: Callable[[], bool] | None = None,
    steer: Callable[[], Sequence[tuple[str, Sequence[Path]]]] | None = None,
) -> Iterator[Event]:
    """Run one user turn, yielding events as they happen.

    `messages` is mutated in place so the caller keeps the conversation; the
    final `TurnEnd.proof` chains the next turn.

    `text` of None means "answer what is already there" -- a retry. Sampling
    at CHAT_TEMPERATURE makes that worth having: the same question asked
    again gives a different answer, which is the point of the button.

    `steer` hands over what the person wrote while the turn was running, as
    (text, images) in the order written. It is asked before every request to
    the model, so a message goes in after the tool results it waited behind
    and the turn carries on with it; a stopped turn asks nothing, and a
    message still waiting when the turn ends is the caller's to send next.
    """
    ctx = context or ToolContext(workdir=options.workdir)
    counter = getattr(client, "count_tokens", None)
    once = _OnceCounter(counter) if counter is not None else None
    if counter is not None:
        # This turn's client, every turn: a session's context outlives each
        # turn's client, and a counter bound to an earlier turn's closed client
        # raised on the next turn's first sized `read_file` (F28).
        ctx.count_tokens = lambda text: counter([{"role": "user", "content": text}])
    stop = cancel or (lambda: False)
    # A retry (text None) still answers a question; the turn's start and its
    # sealed proof both name that one.
    asked = text if text is not None else _last_asked(messages)
    yield TurnStart(turn=turn, prompt=asked)
    if options.system_prompt and not any(m.get("role") == "system" for m in messages):
        prompt = options.system_prompt
        if options.auto is None:  # a task run's prompt already names its worktree
            prompt += f"\n\n{WORKDIR_LINE.format(workdir=options.workdir)}"
        messages.insert(0, {"role": "system", "content": prompt})
    if text is not None:
        messages.append(_user_message(text, images))
    # The person's ocr, imagediff and embeddings switches, read every turn (`attach_mcp`).
    options = replace(options, tools=offer_code_search(offer_image_tools(options.tools, ctx), ctx))
    if ctx.images:
        # This turn's client: a session's context outlives each turn's client.
        ctx.accepts_images = lambda: server_accepts_images(client)
        seen_tools = state_image_fact(options.tools, ctx.accepts_images)
        seeing = offer_screenshot(seen_tools, ctx, ctx.accepts_images)
        options = replace(options, tools=offer_computer(seeing, ctx))
    if ctx.undo is not None:
        # Keyed to the question this turn answers, not to len(messages)
        # before it was added -- the system prompt is inserted at 0 on the
        # first turn, so the two differ and a rewind to the question found
        # no turn to undo.
        ctx.undo.begin(turn, _last_user_index(messages))

    node_id = f"chat#{turn}"
    if ctx.research is not None:
        ctx.research.attach_turn(client, options.journal, node_id, messages, stop)
    auto = options.auto
    if auto is not None:
        auto.budget.start()
    rounds: list[dict[str, Any]] = []
    thinking: list[str] = []
    proof = parent
    for (
        earlier
    ) in messages:  # a cut-off call saved by an earlier turn would get every request refused (B9)
        for tc in earlier.get("tool_calls") or ():
            tc["function"]["arguments"] = _sendable_arguments(tc["function"]["arguments"])
    empty_replies = 0
    announced = False
    waited = False
    turn_started = monotonic()
    try:
        # A chat turn has no round cap: it runs until the model answers or the
        # person stops it (`cancel`). A cap of 24 cut a real setup task off
        # mid-work, where the same ask took another agent about 121 calls.
        while True:
            for said, shown in steer() if steer is not None and not stop() else ():
                messages.append(_user_message(said, shown))
                rounds.append({"person": said})
                yield MessageDelivered(text=said)
            if auto is not None:
                spent = auto.budget.exhausted()
                if spent is not None:
                    auto.stop(spent)
                    yield ErrorEvent(message=f"stopped: {spent}")
                    break
                if not stop():
                    yield from _offer_budget(auto, options.journal, node_id)
            # Before every request, not once per turn: an autonomous run is
            # one turn, so a compaction before the loop only ever saw
            # [system, task] (pi-blackhole's CHANGELOG #38
            # fixed the same defect, OpenHands condenses at every step).
            trimmed = trim_screenshots(messages)
            if trimmed:
                yield Compaction(
                    dropped_messages=0,
                    kept_messages=len(messages),
                    summary=f"{trimmed} older screenshots elided",
                )
            yield from _compact(messages, options, node_id, once, ctx.recall)
            # A compaction may have archived the first pieces: offer `recall` from
            # this request on, not the next turn (an autonomous run is one turn).
            options = replace(options, tools=offer_code_search(options.tools, ctx))
            parts: list[str] = []
            thoughts: list[str] = []
            calls: list[ToolCall] = []
            usage: StreamUsage | None = None
            cap, cut = _reply_cap(client, messages, options, once)
            timed_out = False
            sent = perf_counter()
            first: float | None = None
            with closing(_stream(client, messages, options, cap)) as replies:
                for stream, item in replies:
                    if stream == "tick":
                        if stop():
                            break
                        if auto is None:
                            continue
                        if auto.budget.time_left() <= 0:
                            timed_out = True
                            break
                        # The time question, asked while the reply streams on.
                        yield from _offer_budget(auto, options.journal, node_id)
                        yield _progress(auto, sum(map(len, (*thoughts, *parts))))
                        continue
                    if first is None:
                        first = perf_counter()
                    if stop():
                        break
                    if stream == "call":
                        calls.append(item)
                    elif stream == "usage":
                        usage = item
                    elif stream == "reasoning":
                        thoughts.append(item)
                        yield ReasoningDelta(text=item)
                    else:
                        parts.append(item)
                        yield ContentDelta(text=item)
            done = perf_counter()
            timing = _RoundTiming(
                model_ms=int((done - sent) * 1000),
                ttft_ms=int(((first if first is not None else done) - sent) * 1000),
            )
            reply, reasoning = "".join(parts), "".join(thoughts)
            thinking.append(reasoning)
            if usage is not None and usage.prompt_tokens:
                # The server's count of what was just sent calibrates the next
                # estimate; `messages` is still exactly that request here.
                sent_estimate = estimate_tokens(messages) + options.tool_tokens()
                options.prompt_ratio = max(RATIO_FLOOR, usage.prompt_tokens / max(sent_estimate, 1))
            if auto is not None:
                cut = CUT_AT_TIME if timed_out else cut
                cut = _charge(
                    options.journal, node_id, auto, reply, reasoning, calls, usage, timing, cap, cut
                )
                yield _progress(auto)
                if timed_out:
                    rounds.append({"reply": reply, "tools": []})
                    elapsed = auto.budget.elapsed()
                    auto.stop(
                        f"time budget exhausted: {elapsed:.0f}s of {auto.budget.time_s:.0f}s "
                        f"spent; {CUT_AT_TIME}"
                    )
                    yield ErrorEvent(message=f"stopped: {auto.reason}")
                    break
                if auto.stall_check:
                    if any(c.name in STALL_TOOLS for c in calls):
                        auto.ever_acted = True
                    auto.reasoning_log.append((auto.budget.elapsed(), reasoning))
                    elapsed = auto.budget.elapsed()
                    density = auto.stall_density()
                    if (
                        not auto.ever_acted
                        and elapsed >= STALL_WARMUP_S
                        and density >= STALL_HEDGE_PER_K
                    ):
                        auto.stop(
                            STALLED.format(
                                mins=elapsed / 60, density=density, threshold=STALL_HEDGE_PER_K
                            )
                        )
                        yield ErrorEvent(message=f"stopped: {auto.reason}")
                        break

            keep = options.keep_reasoning and bool(reasoning)
            if not calls:
                messages.append(
                    assistant_message({"role": "assistant", "content": reply}, reasoning, keep)
                )
                rounds.append({"reply": reply, "tools": []})
                if auto is not None and not stop():
                    nudge = AUTO_NUDGE
                    if cut == CUT_AT_MARK:
                        nudge = CUT_NUDGE
                    else:
                        auto.empty_rounds += 1
                    if auto.empty_rounds >= EMPTY_ROUND_CAP:
                        auto.stop(EMPTY_ROUNDS.format(n=auto.empty_rounds))
                        yield ErrorEvent(message=f"stopped: {auto.reason}")
                        break
                    heard = auto.feed.collect() if auto.feed is not None else ""
                    if heard:
                        yield AuditNote(text=heard)
                        nudge = f"{nudge}\n\n{heard}"
                        auto.delivered_audit = heard
                    messages.append({"role": "user", "content": nudge})
                    continue
                if auto is None and not reply.strip() and not stop():
                    empty_replies += 1
                    told = empty_reply_nudge(empty_replies)
                    if told is not None:
                        messages.append({"role": "user", "content": told})
                        continue
                    yield ErrorEvent(message=EMPTY_REPLIES.format(n=empty_replies))
                # F39: "I'll do X." with no call ends nothing; once per turn,
                # so a model that keeps announcing still hands the turn back.
                if auto is None and not announced and not stop() and announces_action(reply):
                    announced = True
                    messages.append({"role": "user", "content": ANNOUNCE_NUDGE})
                    continue
                running = running_since(ctx, turn_started) if auto is None and not waited else None
                if running is not None and not stop():
                    waited = True
                    messages.append(
                        {
                            "role": "user",
                            "content": WAITING_NUDGE.format(
                                id=running.id, command=_clip_command(running.command)
                            ),
                        }
                    )
                    continue
                break

            if auto is not None:
                auto.empty_rounds = 0
            messages.append(
                assistant_message(
                    {
                        "role": "assistant",
                        "content": reply,
                        "tool_calls": [
                            {
                                "id": c.id,
                                "type": "function",
                                "function": {
                                    "name": c.name,
                                    "arguments": _sendable_arguments(c.arguments),
                                },
                            }
                            for c in calls
                        ],
                    },
                    reasoning,
                    keep,
                )
            )
            tools: list[dict[str, Any]] = []
            for call in calls:
                if stop():
                    break
                yield ToolStart(
                    id=call.id,
                    name=call.name,
                    arguments=call.arguments,
                    present=label_for(call.name, call.arguments, ok=None),
                )
                start = perf_counter()
                if auto is not None and auto.feed is not None:
                    auto.feed.before_tool(call.name)
                if auto is not None and call.name == FINISH_TOOL:
                    result = _finish(auto, call.arguments)
                    if result.startswith(FINISH_REFUSED):
                        result += yield from _offer_test_edits(auto, options.journal, node_id, ctx)
                elif (
                    auto is not None
                    and auto.check_tool
                    and auto.feed is not None
                    and call.name == CHECK_TOOL
                ):
                    result = auto.feed.check()
                elif auto is not None and call.name == DISPUTE_TOOL:
                    result = _dispute(auto, call.arguments, options.workdir, ctx)
                elif auto is not None and call.name == REFUSE_TOOL:
                    result = _refuse(auto, call.arguments)
                elif auto is not None and call.name == BLOCKED_TOOL:
                    result = _blocked(auto, call.arguments)
                elif auto is not None and call.name == PREMISE_TOOL:
                    result = _premise(auto, call.arguments, options.workdir, ctx)
                elif (
                    auto is not None
                    and auto.require_premise
                    and auto.premise is None
                    and call.name in PREMISE_GATED
                    and _in_worktree(call.arguments, options.workdir)
                ):
                    result = PREMISE_FIRST
                elif auto is not None and auto.installs is not None and call.name == INSTALL_TOOL:
                    result = yield from _install(auto, options.journal, node_id, call.arguments)
                elif (broken := _cut_arguments(call.arguments)) is not None:
                    # Only ordinary tools: finish, dispute and the rest above
                    # answer arguments they cannot parse in their own words.
                    result = CUT_CALL.format(name=call.name, why=broken)
                else:
                    result = execute_tool(call, workdir=options.workdir, context=ctx)
                duration_ms = int((perf_counter() - start) * 1000)
                ok = not result.startswith("error: ")
                refused = result.startswith(REFUSED)
                # An image this call wrote, and which stored version of it:
                # an older message must keep showing what it produced, not
                # whatever the file says by the end of the conversation.
                preview = preview_for(call.name, call.arguments, options.workdir)
                version = (
                    ctx.undo.versions().get(call.id) if preview and ctx.undo is not None else None
                )
                yield ToolEnd(
                    id=call.id,
                    ok=ok,
                    label=label_for(call.name, call.arguments, ok=ok),
                    detail=result,
                    duration_ms=duration_ms,
                    preview=preview,
                    version=version,
                )
                span = build_span(
                    node_id=node_id,
                    argv=[call.name, call.arguments],
                    duration_ms=duration_ms,
                    exit_code=0 if ok else (2 if refused else 1),
                    detail=result,
                    name=f"refused:{call.name}" if refused else None,
                    parent_id=auto.run_span if auto is not None else None,
                )
                append_span(options.journal, span)
                if auto is not None:
                    auto.span_hashes.append(span.record_hash)
                    auto.refusals += refused
                seen = result
                if auto is not None and auto.feed is not None:
                    auto.feed.after_tool(call.name, ok)
                    heard = auto.feed.collect()
                    if heard:
                        yield AuditNote(text=heard)
                        seen = f"{result}\n\n{heard}"
                        auto.delivered_audit = heard
                if auto is not None:
                    _note_round(auto, call, result)
                if auto is not None and auto.audit is not None:
                    seen += yield from _consult(auto, options.journal, node_id, call, seen)
                tools.append({"name": call.name, "arguments": call.arguments, "result": seen})
                messages.append({"role": "tool", "tool_call_id": call.id, "content": seen})
            if ctx.attachments:
                messages.append(images_message(ctx.attachments))
                ctx.attachments.clear()
            rounds.append({"reply": reply, "tools": tools})
            if stop() or (auto is not None and auto.outcome):
                break
    except VllmError as exc:
        yield ErrorEvent(message=str(exc))
        if auto is not None:
            auto.stop(f"model error: {exc}")
    if stop():
        # The turn is still sealed: what it did before being stopped is real
        # work and belongs in the record.
        yield ErrorEvent(message="stopped by you")
        if auto is not None:
            auto.stop("cancelled")
    if auto is not None:
        auto.stop("the loop ended without finish")  # a no-op once outcome is set
        if auto.feed is not None:
            auto.feed.close()
            if (
                auto.outcome == "stopped"
                and auto.surfaced is not None
                and auto.feed.accepted_unchanged()
            ):
                # The accept stands: the tree the finish audit accepted is the
                # tree the run ends on; surfacing its findings never loses it.
                auto.outcome, auto.narrative = "finished", auto.surfaced
                auto.reason = (
                    f"finish accepted with not-proven findings; the run then ended "
                    f"({auto.reason}) with the tree unchanged"
                )
        _hold_guarded(auto)
        _seal_outcome(options.journal, node_id, auto, rounds)

    yield Context(used=estimate_tokens(messages), limit=options.context_tokens)
    proof = _seal(
        options.journal,
        turn=turn,
        prompt=asked,
        rounds=rounds,
        reasoning="".join(thinking),
        parent=parent,
        kind=f"auto-{auto.outcome}" if auto is not None else "",
    )
    yield TurnEnd(turn=turn, proof=proof)


class _OnceCounter:
    """The server's count, asked once per distinct request: compaction and
    the reply cap both measure the same messages before one request, and
    the second question would only repeat the first."""

    def __init__(self, counter: TokenCounter) -> None:
        self._counter = counter
        self._key: str | None = None
        self._value: int | None = None

    def __call__(self, messages: Any, *, tools: Any = None) -> int | None:
        key = json.dumps([messages, tools], sort_keys=True, default=str)
        if key != self._key:
            self._key, self._value = key, self._counter(messages, tools=tools)
        return self._value


def _compact(
    messages: list[dict[str, Any]],
    options: TurnOptions,
    node_id: str,
    counter: TokenCounter | None = None,
    recall: Recall | None = None,
) -> Iterator[Event]:
    """Compact before one request; announce it, and seal it in a run's ledger.
    With `recall` (the `embeddings` switch), what goes is archived there, and
    the note names the `recall` tool when the embeddings server answers.

    With the server's tokenizer at hand the limit and every measurement are
    real tokens (`compaction_limit_exact`); without it, the old estimate. A
    dogfood run compacted at 85,550 real tokens of a 175,000 window: the
    estimate's safety factor had spent the other half."""
    auto = options.auto
    exact = counter(messages, tools=options.tools) if counter is not None else None
    before = exact if exact is not None else estimate_tokens(messages)
    measure: Callable[[list[dict[str, Any]]], int] | None = None
    limit = options.compaction_limit()
    if exact is not None and counter is not None:
        limit = options.compaction_limit_exact()
        count = counter

        def measure(msgs: list[dict[str, Any]]) -> int:
            counted = count(msgs, tools=options.tools)
            return counted if counted is not None else options.input_estimate(msgs)

    elif options.prompt_ratio is not None:
        # No server count, but a calibrated one: real-token scale, as above.
        limit, measure = options.compaction_limit_calibrated(), options.input_estimate
        before = options.input_estimate(messages)
    if exact is not None and exact <= limit:
        return
    scale = "tokens" if exact is not None else "calibrated" if measure is not None else "estimate"
    dropped, summary = compact(
        messages,
        limit_tokens=limit,
        target_tokens=int(limit * COMPACT_TARGET),
        measure=measure,
        pin="first" if auto is not None else "last",
        hint=(REREAD if auto is not None else ASK_USER)
        + (RECALL_HINT if recall is not None and embeddings_answer() else ""),
        state=(lambda: _run_state(auto)) if auto is not None else None,
        archive=recall.add if recall is not None else None,
    )
    if not summary:
        return
    event = Compaction(dropped_messages=dropped, kept_messages=len(messages), summary=summary)
    if auto is not None:
        auto.compactions += 1
        record = {
            "n": auto.compactions,
            "dropped": dropped,
            "kept": len(messages),
            "estimate_before": before,
            "estimate_after": estimate_tokens(messages),
            "limit": limit,
            "measured": scale,
        }
        append_span(
            options.journal,
            build_span(
                node_id=node_id,
                argv=[COMPACTION_SPAN, json.dumps(record, sort_keys=True)],
                duration_ms=0,
                exit_code=0,
                detail=summary,
                kind="agent",
                name=COMPACTION_SPAN,
                parent_id=auto.run_span,
            ),
        )
    yield event


def _run_state(auto: AutoRun) -> str:
    return run_state(
        files=auto.changed_files(),
        test=auto.last_test,
        audit=auto.delivered_audit,
        spent_tokens=auto.budget.spent_tokens,
        token_budget=auto.budget.tokens,
        elapsed_s=auto.budget.elapsed(),
        time_budget_s=auto.budget.time_s,
    )


def _note_round(auto: AutoRun, call: ToolCall, result: str) -> None:
    """Keep what the state block reads from one tool call of a run."""
    if call.name == FINISH_TOOL and result.startswith(FINISH_REFUSED):
        auto.delivered_audit = result.split("\n\n", 1)[-1]
    elif call.name == FINISH_TOOL and result.startswith(FINISH_SURFACED):
        auto.delivered_audit = result[len(FINISH_SURFACED) :]
    if call.name != "run_command" or not _is_object(call.arguments):
        return
    command = json.loads(call.arguments).get("command")
    if isinstance(command, str) and is_test_command(command):
        auto.last_test = (command, result)


def _progress(auto: AutoRun, streamed_chars: int | None = None) -> RunProgress:
    """The run's spend; with `streamed_chars`, including the reply still streaming.

    A reply's own count arrives only when it ends, so while it streams its
    share is estimated from its text (`CHARS_PER_TOKEN`), marked `partial`.
    """
    partial = streamed_chars is not None
    return RunProgress(
        elapsed_s=round(auto.budget.elapsed(), 3),
        time_budget_s=auto.budget.time_s,
        tokens=auto.budget.spent_tokens + (streamed_chars or 0) // CHARS_PER_TOKEN,
        token_budget=auto.budget.tokens,
        partial=partial,
    )


def _consult(
    auto: AutoRun, journal: Path, node_id: str, call: ToolCall, result: str
) -> Generator[Event, None, str]:
    """Ask the auditor about one tool result; seal and yield what it says.

    Returns the text appended to the tool result, so the model reads the
    finding or the user's answer at its next step, as a tool result.
    """
    assert auto.audit is not None
    extra: list[str] = []
    for note in auto.audit(call.name, call.arguments, result):
        if isinstance(note, AuditFinding):
            span = build_span(
                node_id=node_id,
                argv=["audit", note.gate],
                duration_ms=0,
                exit_code=0 if note.ok else 1,
                detail=note.detail,
                kind="agent",
                name=f"audit:{note.gate}",
                parent_id=auto.run_span,
            )
            append_span(journal, span)
            yield replace(note, span_id=span.span_id)
            extra.append(f"[audit {note.gate}: {'pass' if note.ok else 'FAIL'}] {note.detail}")
        elif isinstance(note, Question):
            reply = yield from _ask(auto, journal, node_id, note)
            if reply is None:
                auto.stop(f"needs you: {note.text}")
                extra.append(f"[question] {note.text}\n[no answer: the run stops here]")
                break
            extra.append(f"[question] {note.text}\n[the user answered] {reply}")
    return "".join(f"\n\n{line}" for line in extra)


def _ask(
    auto: AutoRun, journal: Path, node_id: str, question: Question, default: str | None = None
) -> Generator[Event, None, str | None]:
    """Seal the question, wait for the user, seal the answer beside it.

    Time spent waiting is not charged to the time budget: the budget bounds
    the run's own work, and charging a person's reading time to it would
    stop a run for the user having been slow to reply.

    With a `default`, a question nobody answers is not a stop: the answer
    span is sealed as unanswered, naming the default taken and why, and the
    default is returned. A reply that is not one of the options (compared
    without case) also takes the default, sealed beside the words typed.
    """
    asked = build_span(
        node_id=node_id,
        argv=["question", question.text, *question.options],
        duration_ms=0,
        exit_code=4,
        detail=question.text,
        kind="agent",
        name="question",
        parent_id=auto.run_span,
    )
    append_span(journal, asked)
    yield replace(question, span_id=asked.span_id)
    waited_from = auto.budget.clock()
    reply = auto.answer(question) if auto.answer is not None else None
    if auto.budget.started is not None:
        auto.budget.started += auto.budget.clock() - waited_from
    if reply is None:
        if default is None:
            return None
        why = "no answer channel: a headless run" if auto.answer is None else "the run was stopped"
        argv = ["answer", default, UNANSWERED]
        detail = f"unanswered, default taken: {default} ({why})"
        reply = default
    else:
        argv, detail = ["answer", reply], reply
        if default is not None:
            picked = next((o for o in question.options if o.lower() == reply.strip().lower()), None)
            if picked is None:
                detail = (
                    f"{reply} (not one of {', '.join(question.options)}; default taken: {default})"
                )
                argv = ["answer", reply, "default", default]
            reply = picked or default
    answered = build_span(
        node_id=node_id,
        argv=argv,
        duration_ms=0,
        exit_code=0,
        detail=detail,
        kind="agent",
        name="answer",
        parent_id=asked.span_id,
    )
    append_span(journal, answered)
    yield Answered(id=question.id, text=detail, span_id=answered.span_id)
    return reply


def _install(
    auto: AutoRun, journal: Path, node_id: str, arguments: str
) -> Generator[Event, None, str]:
    """An `install` call: checked, asked, and installed only on the user's Install.

    A request that fails a check (not a plain requirement, a wheel missing
    from the folder, a missing or empty folder) is refused without asking.
    Every other request is a question of its own, whose default, Refuse, is
    what an unanswered or stopped run takes (`_ask`)."""
    assert auto.installs is not None
    plan = auto.installs.plan(arguments)
    if isinstance(plan, str):
        return plan
    qid, text = auto.installs.question(plan)
    question = Question(id=qid, text=text, options=[INSTALL, REFUSE_INSTALL])
    choice = yield from _ask(auto, journal, node_id, question, default=REFUSE_INSTALL)
    if choice != INSTALL:
        return f"{INSTALL_REFUSED}the user did not approve it. Nothing was installed."
    return auto.installs.install(plan)


def _offer_budget(auto: AutoRun, journal: Path, node_id: str) -> Generator[Event, None, None]:
    """At BUDGET_ASK_AT of either budget, ask once whether to extend it.

    Extend raises that budget once by its own size, sealed in the outcome
    sidecar; Stop at limit (the default, and what a headless run takes)
    leaves it, so the run stops where it always would have.
    """
    which = auto.budget.near(BUDGET_ASK_AT)
    if which is None or "budget" in auto.asked:
        return
    auto.asked.add("budget")
    budget = auto.budget
    more = f"{budget.tokens} generated tokens" if which == "token" else f"{budget.time_s:.0f}s"
    question = Question(
        id="budget",
        text=f"This run has used {BUDGET_ASK_AT:.0%} of its {which} budget and has not "
        f"finished. Extend by {more} or stop at the limit?",
        options=[EXTEND_BUDGET, STOP_AT_LIMIT],
    )
    choice = yield from _ask(auto, journal, node_id, question, default=STOP_AT_LIMIT)
    if choice != EXTEND_BUDGET:
        return
    if which == "token":
        auto.sealed["budget_extended"] = {"budget": which, "by": budget.tokens}
        budget.tokens *= 2
    else:
        auto.sealed["budget_extended"] = {"budget": which, "by": budget.time_s}
        budget.time_s *= 2
    yield _progress(auto)


def _needs_a_test(auto: AutoRun) -> str | None:
    """Where the auditor wants a test, if a failing finding is one a test closes."""
    last = auto.feed.last() if auto.feed is not None else None
    findings = last.get("findings") if isinstance(last, dict) else None
    for f in findings if isinstance(findings, list) else []:
        if not isinstance(f, dict) or f.get("verdict") != "fail":
            continue
        if f.get("gate") in TEST_CLOSES or f.get("reason") in TEST_CLOSES:
            where = _CITE.findall(str(f.get("detail", "")))
            return ", ".join(where) or f"the {f.get('gate')} finding"
    return None


def _offer_test_edits(
    auto: AutoRun, journal: Path, node_id: str, ctx: ToolContext
) -> Generator[Event, None, str]:
    """After a refused finish: ask once whether tests may be edited.

    Asked only when tests are read-only and the audit wants a test. Allow
    lifts the guard for the rest of the run and, if this refusal hit the
    cap, reopens the run; Keep read-only (the default) leaves the cap path
    as it was. Returns the text appended to `finish`'s result.
    """
    where = _needs_a_test(auto)
    if ctx.protected_tests is None or where is None or "test-edits" in auto.asked:
        return ""
    auto.asked.add("test-edits")
    question = Question(
        id="test-edits",
        text=f"The auditor needs a test that covers {where}. Tests are read-only in this "
        "run. Allow test edits for the rest of this run?",
        options=[ALLOW_TEST_EDITS, KEEP_READ_ONLY],
    )
    choice = yield from _ask(auto, journal, node_id, question, default=KEEP_READ_ONLY)
    if choice != ALLOW_TEST_EDITS:
        return "\n\nThe user kept tests read-only."
    ctx.protected_tests = None
    auto.sealed["test_edits_granted"] = True
    if auto.reason == AUDIT_UNRESOLVED:
        auto.outcome = auto.reason = ""
        auto.unchanged_refusals = 0
    return "\n\nThe user allowed test edits for the rest of this run: write the test it names."


@dataclass(frozen=True)
class _RoundTiming:
    """One round's request timing, in whole milliseconds."""

    model_ms: int
    ttft_ms: int


def _charge(
    journal: Path,
    node_id: str,
    auto: AutoRun,
    reply: str,
    reasoning: str,
    calls: list[ToolCall],
    usage: StreamUsage | None,
    timing: _RoundTiming | None = None,
    cap: int | None = None,
    cut: str = "",
) -> str:
    """Charge one round to the budget and seal the spend, naming its source.

    Returns the cut the round was stopped by, or "": `CUT_AT_TIME` as given,
    and a budget cut (`_reply_cap`) only when the round reached its `cap`
    (`max_tokens`), which is then sealed beside it as `cut`.

    Also records what the round cost in time and reasoning (SPEED, F-a):
    `reasoning_tokens` is the server's count when its usage carried one,
    else the reasoning text at `CHARS_PER_TOKEN` (0 for none, never
    missing), with `reasoning_source` saying which; `model_ms` is the
    request round-trip and `ttft_ms` the wait for the first streamed
    event. `duration_ms` stays 0 as before; the new fields are additive.
    """
    chars = len(reply) + len(reasoning) + sum(len(c.arguments) for c in calls)
    tokens, source = auto.budget.charge(chars, usage)
    if cut != CUT_AT_TIME and (cap is None or tokens < cap):
        cut = ""
    if usage is not None and usage.reasoning_tokens is not None:
        reasoning_tokens, reasoning_source = usage.reasoning_tokens, "usage"
    else:
        reasoning_tokens, reasoning_source = len(reasoning) // CHARS_PER_TOKEN, "estimate"
    spend = {
        "completion_tokens": tokens,
        "prompt_tokens": usage.prompt_tokens if usage is not None else None,
        "token_source": source,
        "reasoning_tokens": reasoning_tokens,
        "reasoning_source": reasoning_source,
        "model_ms": timing.model_ms if timing is not None else 0,
        "ttft_ms": timing.ttft_ms if timing is not None else 0,
        **({"max_tokens": cap} if cap is not None else {}),
        **({"cut": cut} if cut else {}),
    }
    span_id = uuid.uuid4().hex
    # A cut reply's text is sealed beside its spend: the round's reasoning is
    # otherwise kept only inside the turn's capped proof record, so a
    # runaway reply cut at a limit could not be read back.
    digest = (
        write_attempt_sidecar(journal, span_id, partial_reply(reply, reasoning, cut)) if cut else ""
    )
    append_span(
        journal,
        build_span(
            node_id=node_id,
            argv=["auto:spend", json.dumps(spend, sort_keys=True)],
            duration_ms=0,
            exit_code=0,
            detail=f"{tokens} generated tokens ({source}); "
            f"{auto.budget.spent_tokens} of {auto.budget.tokens} spent"
            + (f"; {cut}" if cut else ""),
            kind="agent",
            name="auto:spend",
            parent_id=auto.run_span,
            span_id=span_id,
            attempt_hash=digest,
        ),
    )
    return cut


PARTIAL_ENDS: Final = MAX_THINKING_CHARS
"""How many characters of each end of a cut reply's reasoning, and of its
content, its spend sidecar keeps: the start shows what the reply set out to
do, the end what it was doing when it was cut."""


def partial_reply(reply: str, reasoning: str, cut: str) -> dict[str, Any]:
    """What a cut reply streamed before its cut, bounded and labelled partial.

    For its reasoning and its content: the length that streamed, and the
    first and the last `PARTIAL_ENDS` characters (the whole text when it is
    no longer than both together, with nothing repeated). Redacted before it
    is cut into ends, so no secret escapes split across the two; the
    sidecar writer redacts and caps again (`journal.write_attempt_sidecar`).
    It is kept as every attempt's reasoning is: beside the ledger, redacted,
    never in a ledger line (`journal._RETAINED_WHOLE`). `saddle explain
    --attempt <span id>` prints it.
    """

    def ends(text: str) -> dict[str, Any]:
        shown = redact_secrets(text)
        head = shown[:PARTIAL_ENDS]
        return {"chars": len(text), "head": head, "tail": shown[len(head) :][-PARTIAL_ENDS:]}

    return {"partial": True, "cut": cut, "reasoning": ends(reasoning), "content": ends(reply)}


def _hold_guarded(auto: AutoRun) -> None:
    """A finished run that changed a guarded path ends stopped, needing a person.

    Checked once, on the tree the run ends on, after every route to
    `finished` (an accepted finish, or the surfaced accept that stands), so no
    route skips it.
    """
    if auto.outcome != "finished" or auto.guard is None:
        return
    held = auto.guard()
    if held:
        auto.outcome, auto.reason = "stopped", GUARDED_STOP.format(paths=", ".join(held))
        auto.sealed["guarded_paths"] = held


def _finish(auto: AutoRun, arguments: str) -> str:
    """Record the model's `finish`: its narrative, labelled as narrative."""
    try:
        args = json.loads(arguments) if arguments.strip() else {}
    except ValueError:
        args = None
    summary = args.get("summary") if isinstance(args, dict) else None
    if not isinstance(summary, str):
        return "error: finish needs a string summary argument"
    if auto.tell_summary is not None:
        auto.tell_summary(summary)
    said = auto.before_finish() if auto.before_finish is not None else ""
    note = f"{said}\n\n" if said else ""
    if auto.feed is not None:
        accepted, findings = auto.feed.final()
        if auto.feed.unchanged():
            # Nothing to audit is no verdict: neither an accept nor a refusal.
            auto.end_unchanged(summary)
            return FINISH_UNCHANGED
        if not accepted:
            auto.finish_refusals += 1
            unresolved = auto.feed.unresolved()
            same = auto.unchanged_refusals > 0 and unresolved == auto.unresolved
            auto.unchanged_refusals = auto.unchanged_refusals + 1 if same else 1
            auto.unresolved = unresolved
            if auto.unchanged_refusals >= auto.finish_refusal_cap:
                auto.stop(AUDIT_UNRESOLVED)
                return note + (
                    f"{FINISH_REFUSED}The same findings refused finish "
                    f"{auto.unchanged_refusals} times in a row; the run stops "
                    f"({AUDIT_UNRESOLVED}).\n\n{findings}"
                )
            return note + f"{FINISH_REFUSED}Fix what it names and call finish again.\n\n{findings}"
        auto.waivers = auto.feed.waivers()
        asked = auto.feed.questions()
        if asked:
            # Accepted (a question refuses nothing), but a person must decide
            # it: the run ends "needs you", never finished on it silently.
            auto.stop(needs_you_reason(asked))
            return note + f"{FINISH_QUESTION}{chr(10).join(asked)}"
        if findings:
            # Accepted, with not-proven findings the model has not read: it
            # reads them in its next round; a later finish ends the run.
            auto.surfaced = summary
            return note + f"{FINISH_SURFACED}{findings}"
    auto.finish(summary)
    return note + "finished. Your summary is recorded as narrative, not as evidence."


DISPUTE_MAX_COMMANDS: Final = 5

PREMISE_GATED: Final = frozenset({"write_file", "edit_file"})


def _in_worktree(arguments: str, workdir: Path) -> bool:
    """Whether an edit tool's `path` lands in the run's worktree: a probe written
    to /tmp is not an edit of the code, and `--premise-check` asks for probes."""
    try:
        path = json.loads(arguments).get("path", "")
    except (ValueError, AttributeError):
        return True
    if not isinstance(path, str) or not path:
        return True
    target = (workdir / path).resolve()
    return target == workdir.resolve() or workdir.resolve() in target.parents


PYTHON_CRASH: Final = "Traceback (most recent call last):"
"""How a crashed Python probe announces itself; a pytest run's failures are results."""
"""The tools `--premise-check` holds back until `premise_check` ran."""

PREMISE_FIRST: Final = (
    "error: refused: edits wait for premise_check. Call it first with the claim "
    "you are checking and the command(s) whose output shows the problem the task "
    "describes on the current code."
)

PREMISE_QUESTION: Final = (
    "Read this output. Does it show the problem the task describes, on the current "
    "code? If it does, go on and fix it. If it does not, and the code explains why "
    "the problem cannot happen or is already fixed, call dispute now with these "
    "commands as evidence; do not look for another reading of the task."
)


def _premise(auto: AutoRun, arguments: str, workdir: Path, ctx: ToolContext) -> str:
    """The model shows the problem before its first edit (`--premise-check`):
    saddle reruns each command, seals claim and output, unlocks the edit tools,
    and returns the output with `PREMISE_QUESTION`. Refused like `dispute` when
    the claim or the commands are missing or cannot run."""
    try:
        args = json.loads(arguments) if arguments.strip() else {}
    except ValueError:
        args = None
    if not isinstance(args, dict):
        return "error: premise_check needs a JSON object with claim and commands"
    claim, commands = args.get("claim"), args.get("commands")
    if not isinstance(claim, str) or not claim.strip():
        return "error: premise_check refused: name the claim you are checking"
    if (
        not isinstance(commands, list)
        or not commands
        or len(commands) > DISPUTE_MAX_COMMANDS
        or not all(isinstance(c, str) and c.strip() for c in commands)
    ):
        return (
            f"error: premise_check refused: give one to {DISPUTE_MAX_COMMANDS} "
            "shell commands whose output shows the problem"
        )
    ran = []
    for index, command in enumerate(commands):
        call = ToolCall(
            id=f"premise-{index}", name="run_command", arguments=json.dumps({"command": command})
        )
        output = execute_tool(call, workdir=workdir, context=ctx)
        if output.startswith("error: "):
            return (
                f"error: premise_check refused: command {index + 1} did not run "
                f"({output.removeprefix('error: ')}); give commands that run"
            )
        if PYTHON_CRASH in output and "pytest" not in command:
            # A probe that crashed shows nothing either way; edits stay held.
            return (
                f"error: premise_check refused: command {index + 1} crashed (a Python "
                f"traceback, not a result). Fix it and call premise_check again.\n\n"
                f"{output[-2000:]}"
            )
        ran.append({"command": command, "output": output})
    auto.premise = {"claim": claim, "evidence": ran}
    shown = "\n\n".join(f"$ {r['command']}\n{r['output']}" for r in ran)
    return f"{shown}\n\n{PREMISE_QUESTION}"


def _dispute(auto: AutoRun, arguments: str, workdir: Path, ctx: ToolContext) -> str:
    """The model's ripcord: the task's premise is false. Each evidence command
    is rerun by saddle, as the model's own `run_command` runs it, and its output
    sealed; the run then ends "needs you" (`PREMISE_DISPUTED_STOP`). A dispute
    with no claim, no finding, no command, too many, or a command that cannot
    run is refused, and the run goes on: the ripcord is never a cheap way out."""
    try:
        args = json.loads(arguments) if arguments.strip() else {}
    except ValueError:
        args = None
    if not isinstance(args, dict):
        return "error: dispute needs a JSON object with claim, finding and evidence"
    claim, finding, evidence = args.get("claim"), args.get("finding"), args.get("evidence")
    if not isinstance(claim, str) or not claim.strip():
        return "error: dispute refused: name the claim the task makes"
    if not isinstance(finding, str) or not finding.strip():
        return "error: dispute refused: say what you found"
    if (
        not isinstance(evidence, list)
        or not evidence
        or not all(isinstance(c, str) and c.strip() for c in evidence)
    ):
        return "error: dispute refused: give one or more shell commands whose output shows it"
    if len(evidence) > DISPUTE_MAX_COMMANDS:
        return f"error: dispute refused: at most {DISPUTE_MAX_COMMANDS} evidence commands"
    ran = []
    for index, command in enumerate(evidence):
        call = ToolCall(
            id=f"dispute-{index}",
            name="run_command",
            arguments=json.dumps({"command": command}),
        )
        output = execute_tool(call, workdir=workdir, context=ctx)
        if output.startswith("error: "):
            return (
                f"error: dispute refused: evidence command {index + 1} did not run "
                f"({output.removeprefix('error: ')}); give commands that run"
            )
        ran.append({"command": command, "output": output})
    auto.dispute = {"claim": claim, "finding": finding, "evidence": ran}
    auto.stop(f"{PREMISE_DISPUTED_STOP}{claim.strip().splitlines()[0]}")
    return (
        "dispute recorded: the run ends here, needing a person, who reads your claim, "
        "your finding and saddle's rerun of each evidence command. It is not a pass."
    )


def _refuse(auto: AutoRun, arguments: str) -> str:
    """The model declines the task on grounds it will not act on -- harmful, out
    of scope, or against policy. Unlike `dispute` it carries no evidence: a
    refusal is not a factual claim about the code, so there is nothing to rerun.
    The reason is sealed and the run ends "needs you" (`REFUSED_STOP`) for a
    person to review. A refusal with no reason is not recorded; the run goes on."""
    try:
        args = json.loads(arguments) if arguments.strip() else {}
    except ValueError:
        args = None
    if not isinstance(args, dict):
        return "error: refuse needs a JSON object with a reason"
    reason = args.get("reason")
    if not isinstance(reason, str) or not reason.strip():
        return "error: refuse refused: say why you are declining the task"
    auto.refusal = {"reason": reason}
    auto.stop(f"{REFUSED_STOP}{reason.strip().splitlines()[0]}")
    return (
        "refusal recorded: the run ends here, needing a person, who reads your reason. "
        "It is not a pass and not a finish."
    )


def _blocked(auto: AutoRun, arguments: str) -> str:
    """The model is stuck: it needs information or a decision only a person can
    give. Not done, not a false premise, not a refusal. What blocks it and what
    it already tried are sealed, and the run ends "needs you" (`BLOCKED_STOP`).
    Both are required, so the exit names its attempt rather than being a bare
    bail-out; a call missing either is refused and the run goes on."""
    try:
        args = json.loads(arguments) if arguments.strip() else {}
    except ValueError:
        args = None
    if not isinstance(args, dict):
        return "error: blocked needs a JSON object with reason and tried"
    reason, tried = args.get("reason"), args.get("tried")
    if not isinstance(reason, str) or not reason.strip():
        return "error: blocked refused: say what is blocking you"
    if not isinstance(tried, str) or not tried.strip():
        return "error: blocked refused: say what you already tried"
    auto.blocked = {"reason": reason, "tried": tried}
    auto.stop(f"{BLOCKED_STOP}{reason.strip().splitlines()[0]}")
    return (
        "blocked recorded: the run ends here, needing a person, who reads what blocks "
        "you and what you tried. It is not a pass and not a finish."
    )


NARRATIVE_LABEL: Final = "narrative, not evidence"


def _seal_outcome(journal: Path, node_id: str, auto: AutoRun, rounds: list[dict[str, Any]]) -> None:
    """The run's last span: how it ended, and what it did, before the proof.

    Its detail names the ending and the budget, readable in `saddle tail`;
    the whole account (narrative labelled as such, files changed, commands
    run, spend) is a sidecar the span's `attempt_hash` makes tamper-evident.
    """
    files = auto.changed_files()
    commands = [
        json.loads(t["arguments"]).get("command", "")
        for r in rounds
        for t in r["tools"]
        if t["name"] == "run_command" and _is_object(t["arguments"])
    ]
    detail = (
        f"{auto.outcome}: {auto.reason}; arm {auto.arm}; "
        f"files changed: {', '.join(files) or 'none'}; "
        f"commands run: {len(commands)}; refusals: {auto.refusals}"
    )
    if auto.reason == AUDIT_UNRESOLVED:
        named = ", ".join(f"{f['gate']} ({f['reason']})" for f in auto.unresolved)
        detail += f"; unresolved findings: {named}"
    took = int(auto.budget.elapsed() * 1000)
    # The run's span of wall time: it began `took` before now and ends as it is
    # sealed, so a reader has when the run finished (`transcript`).
    began = started_before(took, utc_now())
    span = build_span(
        node_id=node_id,
        argv=[f"auto:{auto.outcome}"],
        duration_ms=took,
        exit_code=0 if auto.outcome == "finished" else 3,
        detail=detail,
        kind="agent",
        parent_id=auto.run_span,
        started_at=began,
    )
    evidence = {
        "outcome": auto.outcome,
        "reason": auto.reason,
        "narrative_label": NARRATIVE_LABEL,
        "narrative": auto.narrative,
        "files_changed": files,
        "commands": commands,
        "rounds": len(rounds),
        "refusals": auto.refusals,
        "tokens_spent": auto.budget.spent_tokens,
        "token_source": auto.budget.source(),
        "tokens_by_source": dict(auto.budget.by_source),
        "token_budget": auto.budget.tokens,
        "elapsed_s": round(auto.budget.elapsed(), 3),
        "time_budget_s": auto.budget.time_s,
        "tool_span_hashes": list(auto.span_hashes),
        # Every audit record the run's journal holds by now (the feed
        # was closed above), so a deleted or inserted one fails verify.
        AUDIT_SPAN_HASHES: run_audit_hashes(journal, auto.run_span),
        "arm": auto.arm,
        **auto.sealed,
        "finish_refusals": auto.finish_refusals,
        "finish_refusal_cap": auto.finish_refusal_cap,
        "unchanged_refusals": auto.unchanged_refusals,
        "unresolved_findings": auto.unresolved,
        **({"dispute": auto.dispute} if auto.dispute is not None else {}),
        **({"refusal": auto.refusal} if auto.refusal is not None else {}),
        **({"blocked": auto.blocked} if auto.blocked is not None else {}),
        **({"premise": auto.premise} if auto.premise is not None else {}),
        # The last completed audit (the finish audit if finish was called):
        # its tree id, findings and verdict. None for arm E, or when nothing
        # was audited before the run ended.
        "audit": auto.feed.last() if auto.feed is not None else None,
    }
    if auto.prompt_check is not None:
        # Reporting only; no verdict reads it.
        evidence["prompt_constants"] = auto.prompt_check()
    if auto.outcome == "finished" and auto.waivers is not None:
        # Every accepted finish seals what it stood on, [] if
        # nothing; arm E and ended-unaccepted runs seal the keys as before.
        evidence["waivers"] = auto.waivers
    if auto.surfaced is not None:
        # Only when an accepted finish surfaced not-proven
        # findings, so every other run seals the same keys as before.
        evidence["finish_surfaced"] = True
    if auto.check_tool and auto.feed is not None:
        # Only with the flag, so a run without it seals the same keys as before.
        evidence["checks"] = len(auto.feed.checks)
    digest = write_attempt_sidecar(journal, span.span_id, evidence)
    span = build_span(
        node_id=node_id,
        argv=[f"auto:{auto.outcome}"],
        duration_ms=span.duration_ms,
        exit_code=span.exit_code,
        detail=detail,
        kind="agent",
        parent_id=auto.run_span,
        span_id=span.span_id,
        attempt_hash=digest,
        started_at=began,
    )
    append_span(journal, span)


def _is_object(arguments: str) -> bool:
    try:
        return isinstance(json.loads(arguments), dict)
    except ValueError:
        return False
