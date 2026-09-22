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
from collections.abc import Iterator, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from time import perf_counter
from typing import Any, Final

from saddle.events import (
    Compaction,
    ContentDelta,
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
from saddle.memory import compact, estimate_tokens
from saddle.tools import TOOLS, ToolContext, execute_tool
from saddle.vllm import ToolCall, VllmClient, VllmError

MAX_TOOL_ROUNDS: Final = 24
"""Raised from 10: agentic turns legitimately chain many calls, and the old
cap truncated real work. A run that hits this is reported, not silent."""


OUTPUT_MARGIN: Final = 2048
"""Headroom kept back so a reply is never sized right up to the window."""

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
    temperature: float = 0.0
    reasoning_effort: str = "medium"
    system_prompt: str = ""
    context_tokens: int = 175_000
    tools: list[dict[str, Any]] = field(default_factory=lambda: list(TOOLS))

    def budget(self, messages: list[dict[str, Any]]) -> int:
        """Tokens this reply may use: the window minus what is already in it."""
        if self.max_tokens:
            return self.max_tokens
        left = self.context_tokens - estimate_tokens(messages) - OUTPUT_MARGIN
        return max(left, MIN_OUTPUT)


def _stream(
    client: VllmClient, messages: list[dict[str, Any]], options: TurnOptions
) -> Iterator[tuple[str, Any]]:
    """Yield ('reasoning'|'content', text) or ('call', ToolCall)."""
    for event in client.stream_chat(
        messages,
        max_tokens=options.budget(messages),
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
    journal: Path, *, turn: int, prompt: str, rounds: list[dict[str, Any]],
    reasoning: str, parent: str | None,
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
        parts.append({
            "type": "image_url",
            "image_url": {"url": f"data:{mime};base64,{encoded}"},
        })
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
) -> Iterator[Event]:
    """Run one user turn, yielding events as they happen.

    `messages` is mutated in place so the caller keeps the conversation; the
    final `TurnEnd.proof` chains the next turn.
    """
    ctx = context or ToolContext(workdir=options.workdir)
    yield TurnStart(turn=turn, prompt=text)
    if options.system_prompt and not any(m.get("role") == "system" for m in messages):
        messages.insert(0, {"role": "system", "content": options.system_prompt})
    messages.append(_user_message(text, images))

    dropped, summary = compact(messages, limit_tokens=options.context_tokens)
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

            messages.append({
                "role": "assistant", "content": reply,
                "tool_calls": [
                    {"id": c.id, "type": "function",
                     "function": {"name": c.name, "arguments": c.arguments}}
                    for c in calls
                ],
            })
            tools: list[dict[str, Any]] = []
            for call in calls:
                yield ToolStart(
                    id=call.id, name=call.name, arguments=call.arguments,
                    present=label_for(call.name, call.arguments, ok=None),
                )
                start = perf_counter()
                result = execute_tool(call, workdir=options.workdir, context=ctx)
                duration_ms = int((perf_counter() - start) * 1000)
                ok = not result.startswith("error: ")
                yield ToolEnd(
                    id=call.id, ok=ok,
                    label=label_for(call.name, call.arguments, ok=ok),
                    detail=result, duration_ms=duration_ms,
                )
                append_span(options.journal, build_span(
                    node_id=node_id, argv=[call.name, call.arguments],
                    duration_ms=duration_ms, exit_code=0 if ok else 1, detail=result,
                ))
                tools.append({"name": call.name, "arguments": call.arguments, "result": result})
                messages.append({"role": "tool", "tool_call_id": call.id, "content": result})
            rounds.append({"reply": reply, "tools": tools})
        else:
            yield ErrorEvent(message=f"stopped after {MAX_TOOL_ROUNDS} tool rounds")
    except VllmError as exc:
        yield ErrorEvent(message=str(exc))

    proof = _seal(
        options.journal, turn=turn, prompt=text, rounds=rounds,
        reasoning="".join(thinking), parent=parent,
    )
    yield TurnEnd(turn=turn, proof=proof)
