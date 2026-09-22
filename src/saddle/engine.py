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
from collections.abc import Callable, Iterator, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from time import perf_counter
from typing import Any, Final

from saddle.events import (
    Compaction,
    ContentDelta,
    Context,
    ErrorEvent,
    Event,
    ReasoningDelta,
    ToolEnd,
    ToolStart,
    TurnEnd,
    TurnStart,
)
from saddle.journal import append_record, append_span, build_record, build_span
from saddle.labels import label_for
from saddle.memory import CHARS_PER_TOKEN, compact, estimate_tokens
from saddle.tools import TOOLS, ToolContext, execute_tool, preview_for
from saddle.vllm import ToolCall, VllmClient, VllmError

type TokenCounter = Callable[..., int | None]
"""`VllmClient.count_tokens`: the server's own tokeniser, or None if it has
no /tokenize endpoint."""

MAX_TOOL_ROUNDS: Final = 24
"""Raised from 10: agentic turns legitimately chain many calls, and the old
cap truncated real work. A run that hits this is reported, not silent."""


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

    def budget(
        self, messages: list[dict[str, Any]], counter: TokenCounter | None = None
    ) -> int:
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
    for event in client.stream_chat(
        messages,
        max_tokens=options.budget(messages, getattr(client, "count_tokens", None)),
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
) -> str:
    node_id = f"chat#{turn}"
    record = build_record(
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
    text: str,
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
    """
    ctx = context or ToolContext(workdir=options.workdir)
    stop = cancel or (lambda: False)
    yield TurnStart(turn=turn, prompt=text)
    if options.system_prompt and not any(m.get("role") == "system" for m in messages):
        messages.insert(0, {"role": "system", "content": options.system_prompt})
    messages.append(_user_message(text, images))

    dropped, summary = compact(messages, limit_tokens=options.compaction_limit())
    if dropped:
        yield Compaction(dropped_messages=dropped, kept_messages=len(messages), summary=summary)

    node_id = f"chat#{turn}"
    rounds: list[dict[str, Any]] = []
    thinking: list[str] = []
    proof = parent
    try:
        for _ in range(MAX_TOOL_ROUNDS):
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

            if not calls:
                messages.append({"role": "assistant", "content": reply})
                rounds.append({"reply": reply, "tools": []})
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
                result = execute_tool(call, workdir=options.workdir, context=ctx)
                duration_ms = int((perf_counter() - start) * 1000)
                ok = not result.startswith("error: ")
                yield ToolEnd(
                    id=call.id,
                    ok=ok,
                    label=label_for(call.name, call.arguments, ok=ok),
                    detail=result,
                    duration_ms=duration_ms,
                    preview=preview_for(call.name, call.arguments, options.workdir),
                )
                append_span(
                    options.journal,
                    build_span(
                        node_id=node_id,
                        argv=[call.name, call.arguments],
                        duration_ms=duration_ms,
                        exit_code=0 if ok else 1,
                        detail=result,
                    ),
                )
                tools.append({"name": call.name, "arguments": call.arguments, "result": result})
                messages.append({"role": "tool", "tool_call_id": call.id, "content": result})
            rounds.append({"reply": reply, "tools": tools})
            if stop():
                break
        else:
            yield ErrorEvent(message=f"stopped after {MAX_TOOL_ROUNDS} tool rounds")
    except VllmError as exc:
        yield ErrorEvent(message=str(exc))
    if stop():
        # The turn is still sealed: what it did before being stopped is real
        # work and belongs in the record.
        yield ErrorEvent(message="stopped by you")

    yield Context(used=estimate_tokens(messages), limit=options.context_tokens)
    proof = _seal(
        options.journal,
        turn=turn,
        prompt=text,
        rounds=rounds,
        reasoning="".join(thinking),
        parent=parent,
    )
    yield TurnEnd(turn=turn, proof=proof)
