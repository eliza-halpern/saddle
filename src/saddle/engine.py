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
import mimetypes
from collections.abc import Callable, Generator, Iterator, Sequence
from dataclasses import dataclass, field, replace
from pathlib import Path
from time import monotonic, perf_counter
from typing import Any, Final

from saddle.events import (
    Answered,
    AuditFinding,
    Compaction,
    ContentDelta,
    Context,
    ErrorEvent,
    Event,
    Question,
    ReasoningDelta,
    RunProgress,
    ToolEnd,
    ToolStart,
    TurnEnd,
    TurnStart,
)
from saddle.journal import (
    append_record,
    append_span,
    build_record,
    build_span,
    write_attempt_sidecar,
)
from saddle.labels import label_for
from saddle.memory import CHARS_PER_TOKEN, compact, estimate_tokens
from saddle.tools import FINISH_TOOL, REFUSED, TOOLS, ToolContext, execute_tool, preview_for
from saddle.vllm import ToolCall, VllmClient, VllmError

type TokenCounter = Callable[..., int | None]
"""`VllmClient.count_tokens`: the server's own tokeniser, or None if it has
no /tokenize endpoint."""

MAX_TOOL_ROUNDS: Final = 24
"""Raised from 10: agentic turns legitimately chain many calls, and the old
cap truncated real work. A run that hits this is reported, not silent.

Applies to a chat turn only. An autonomous run (`AutoRun`) has no round
cap; it is bounded by its `RunBudget` of wall time and generated tokens."""

AUTO_NUDGE: Final = (
    "No one is here to reply. Keep working with the tools, or call finish "
    "when the task is done. If it cannot be done, call finish and say why."
)
"""What an autonomous run is told when a round ends with no tool call. In
chat that ends the turn; here it would end the run with nothing recorded
as its outcome, so the run goes on until `finish` or a budget."""


@dataclass
class RunBudget:
    """Wall time and generated tokens an autonomous run may spend.

    Generated tokens are estimated from the streamed text (reasoning,
    content and tool-call arguments, `CHARS_PER_TOKEN` per token) because
    the streaming client reports no usage. The clock is injectable so a
    test can exhaust time without waiting for it.
    """

    time_s: float
    tokens: int
    clock: Callable[[], float] = monotonic
    started: float | None = None
    spent_tokens: int = 0

    def start(self) -> None:
        if self.started is None:
            self.started = self.clock()

    def elapsed(self) -> float:
        return 0.0 if self.started is None else self.clock() - self.started

    def charge(self, text_chars: int) -> None:
        self.spent_tokens += max(1, text_chars // CHARS_PER_TOKEN)

    def remaining_tokens(self) -> int:
        return max(self.tokens - self.spent_tokens, 0)

    def exhausted(self) -> str | None:
        """Which budget has run out, in words, or None if neither has."""
        if self.spent_tokens >= self.tokens:
            return (
                f"token budget exhausted: ~{self.spent_tokens} of {self.tokens} "
                "generated tokens spent"
            )
        elapsed = self.elapsed()
        if elapsed >= self.time_s:
            return f"time budget exhausted: {elapsed:.0f}s of {self.time_s:.0f}s spent"
        return None


@dataclass
class AutoRun:
    """What makes a turn an autonomous run, and what it ended as.

    The engine fills `outcome`: "finished" only when the model called
    `finish`; "stopped" for a budget, a model error or a cancel. There is
    no third ending, and a stop never reads as done.

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
    audit: Callable[[str, str, str], Sequence[Event]] | None = None
    answer: Callable[[Question], str | None] | None = None

    def stop(self, reason: str) -> None:
        if not self.outcome:
            self.outcome, self.reason = "stopped", reason

    def finish(self, narrative: str) -> None:
        if not self.outcome:
            self.outcome, self.reason, self.narrative = "finished", "finish called", narrative


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
"""Floor for a reply's budget even in a nearly-full window. Below this a
reply gets cut mid-sentence, which is worse than compacting harder."""


@dataclass
class TurnOptions:
    workdir: Path = Path(".")
    journal: Path = Path(".saddle/chat.jsonl")
    max_tokens: int | None = None
    """`None` means size it from the window left after the conversation, the
    way `saddle run` has since T6-17. A flat 8192 was the old chat default
    and it was stingy: this server reports a 175,000-token window, and a
    turn reasoning at `xhigh` can spend 40,000-80,000 tokens thinking before
    it writes a word. A reply truncated mid-thought is the single most
    annoying failure a chat UI has."""
    temperature: float = CHAT_TEMPERATURE
    reasoning_effort: str = "medium"
    system_prompt: str = ""
    context_tokens: int = 175_000
    tools: list[dict[str, Any]] = field(default_factory=lambda: list(TOOLS))
    auto: AutoRun | None = None
    """Set for an autonomous run: no round cap, a budget, `finish`."""

    def tool_tokens(self) -> int:
        """What the tool schemas cost, which they do on every single request.

        This was missing from the budget entirely. The schemas are ~680
        tokens here and they are sent with the prompt each turn, so the
        window was being promised to the reply twice.
        """
        return len(json.dumps(self.tools)) // CHARS_PER_TOKEN

    def input_estimate(self, messages: list[dict[str, Any]]) -> int:
        """A guess at the prompt size, for when the server cannot be asked."""
        raw = estimate_tokens(messages) + self.tool_tokens()
        return int(raw * INPUT_SAFETY)

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

    def compaction_limit(self) -> int:
        """Estimate-scale ceiling that still leaves room to answer.

        Compaction used to be told the whole window, so it was content to
        let the conversation fill every token of it and leave the reply the
        `MIN_OUTPUT` floor -- which `budget` would then request on top of a
        full prompt.
        """
        room = self.context_tokens - MIN_OUTPUT - OUTPUT_MARGIN
        return max(int(room / INPUT_SAFETY) - self.tool_tokens(), MIN_OUTPUT)


def _stream(
    client: VllmClient, messages: list[dict[str, Any]], options: TurnOptions
) -> Iterator[tuple[str, Any]]:
    """Yield ('reasoning'|'content', text) or ('call', ToolCall)."""
    cap = options.budget(messages, getattr(client, "count_tokens", None))
    if options.auto is not None:
        cap = max(min(cap, options.auto.budget.remaining_tokens()), 1)
    for event in client.stream_chat(
        messages,
        max_tokens=cap,
        temperature=options.temperature,
        reasoning_effort=options.reasoning_effort,
        tools=options.tools,
    ):
        if isinstance(event, ToolCall):
            yield "call", event
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
) -> Iterator[Event]:
    """Run one user turn, yielding events as they happen.

    `messages` is mutated in place so the caller keeps the conversation; the
    final `TurnEnd.proof` chains the next turn.

    `text` of None means "answer what is already there" -- a retry. Sampling
    at CHAT_TEMPERATURE makes that worth having: the same question asked
    again gives a different answer, which is the point of the button.
    """
    ctx = context or ToolContext(workdir=options.workdir)
    stop = cancel or (lambda: False)
    yield TurnStart(turn=turn, prompt=text if text is not None else _last_asked(messages))
    if options.system_prompt and not any(m.get("role") == "system" for m in messages):
        messages.insert(0, {"role": "system", "content": options.system_prompt})
    if text is not None:
        messages.append(_user_message(text, images))
    if ctx.undo is not None:
        # Keyed to the question this turn answers, not to len(messages)
        # before it was added -- the system prompt is inserted at 0 on the
        # first turn, so the two differ and a rewind to the question found
        # no turn to undo.
        ctx.undo.begin(turn, _last_user_index(messages))

    dropped, summary = compact(messages, limit_tokens=options.compaction_limit())
    if dropped:
        yield Compaction(dropped_messages=dropped, kept_messages=len(messages), summary=summary)

    node_id = f"chat#{turn}"
    auto = options.auto
    if auto is not None:
        auto.budget.start()
    rounds: list[dict[str, Any]] = []
    thinking: list[str] = []
    proof = parent
    taken = 0
    try:
        while True:
            if auto is None:
                if taken >= MAX_TOOL_ROUNDS:
                    yield ErrorEvent(message=f"stopped after {MAX_TOOL_ROUNDS} tool rounds")
                    break
            else:
                spent = auto.budget.exhausted()
                if spent is not None:
                    auto.stop(spent)
                    yield ErrorEvent(message=f"stopped: {spent}")
                    break
            taken += 1
            parts: list[str] = []
            thoughts: list[str] = []
            calls: list[ToolCall] = []
            for stream, item in _stream(client, messages, options):
                if stop():
                    break
                if stream == "call":
                    calls.append(item)
                elif stream == "reasoning":
                    thoughts.append(item)
                    yield ReasoningDelta(text=item)
                else:
                    parts.append(item)
                    yield ContentDelta(text=item)
            reply, reasoning = "".join(parts), "".join(thoughts)
            thinking.append(reasoning)
            if auto is not None:
                auto.budget.charge(
                    len(reply) + len(reasoning) + sum(len(c.arguments) for c in calls)
                )
                yield _progress(auto)

            if not calls:
                messages.append({"role": "assistant", "content": reply})
                rounds.append({"reply": reply, "tools": []})
                if auto is not None and not stop():
                    messages.append({"role": "user", "content": AUTO_NUDGE})
                    continue
                break

            messages.append(
                {
                    "role": "assistant",
                    "content": reply,
                    "tool_calls": [
                        {
                            "id": c.id,
                            "type": "function",
                            "function": {"name": c.name, "arguments": c.arguments},
                        }
                        for c in calls
                    ],
                }
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
                if auto is not None and call.name == FINISH_TOOL:
                    result = _finish(auto, call.arguments)
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
                    if auto.audit is not None:
                        result += yield from _consult(auto, options.journal, node_id, call, result)
                tools.append({"name": call.name, "arguments": call.arguments, "result": result})
                messages.append({"role": "tool", "tool_call_id": call.id, "content": result})
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
        _seal_outcome(options.journal, node_id, auto, rounds)

    yield Context(used=estimate_tokens(messages), limit=options.context_tokens)
    proof = _seal(
        options.journal,
        turn=turn,
        prompt=text,
        rounds=rounds,
        reasoning="".join(thinking),
        parent=parent,
        kind=f"auto-{auto.outcome}" if auto is not None else "",
    )
    yield TurnEnd(turn=turn, proof=proof)


def _progress(auto: AutoRun) -> RunProgress:
    return RunProgress(
        elapsed_s=round(auto.budget.elapsed(), 3),
        time_budget_s=auto.budget.time_s,
        tokens=auto.budget.spent_tokens,
        token_budget=auto.budget.tokens,
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
    auto: AutoRun, journal: Path, node_id: str, question: Question
) -> Generator[Event, None, str | None]:
    """Seal the question, wait for the user, seal the answer beside it.

    Time spent waiting is not charged to the time budget: the budget bounds
    the run's own work, and charging a person's reading time to it would
    stop a run for the user having been slow to reply.
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
        return None
    answered = build_span(
        node_id=node_id,
        argv=["answer", reply],
        duration_ms=0,
        exit_code=0,
        detail=reply,
        kind="agent",
        name="answer",
        parent_id=asked.span_id,
    )
    append_span(journal, answered)
    yield Answered(id=question.id, text=reply, span_id=answered.span_id)
    return reply


def _finish(auto: AutoRun, arguments: str) -> str:
    """Record the model's `finish`: its narrative, labelled as narrative."""
    try:
        args = json.loads(arguments) if arguments.strip() else {}
    except ValueError:
        args = None
    summary = args.get("summary") if isinstance(args, dict) else None
    if not isinstance(summary, str):
        return "error: finish needs a string summary argument"
    auto.finish(summary)
    return "finished. Your summary is recorded as narrative, not as evidence."


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
        f"{auto.outcome}: {auto.reason}; files changed: {', '.join(files) or 'none'}; "
        f"commands run: {len(commands)}; refusals: {auto.refusals}"
    )
    span = build_span(
        node_id=node_id,
        argv=[f"auto:{auto.outcome}"],
        duration_ms=int(auto.budget.elapsed() * 1000),
        exit_code=0 if auto.outcome == "finished" else 3,
        detail=detail,
        kind="agent",
        parent_id=auto.run_span,
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
        "tokens_spent_estimate": auto.budget.spent_tokens,
        "token_budget": auto.budget.tokens,
        "elapsed_s": round(auto.budget.elapsed(), 3),
        "time_budget_s": auto.budget.time_s,
        "tool_span_hashes": list(auto.span_hashes),
    }
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
    )
    append_span(journal, span)


def _is_object(arguments: str) -> bool:
    try:
        return isinstance(json.loads(arguments), dict)
    except ValueError:
        return False
