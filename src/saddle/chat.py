"""Interactive streaming chat (`saddle up`): REPL over stream_chat plus tools."""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from time import perf_counter
from typing import IO, Any, Final

from rich.console import Console

from saddle.journal import append_record, append_span, build_record, build_span
from saddle.timeline import Timeline
from saddle.tools import TOOLS, execute_tool
from saddle.vllm import ToolCall, VllmClient, VllmError

MAX_TOOL_ROUNDS: Final = 10


@dataclass(frozen=True)
class ChatOptions:
    """Resolved `up` inputs: tool workdir, journal, plus sampling knobs."""

    workdir: Path = Path(".")
    journal: Path = Path(".saddle/chat.jsonl")
    max_tokens: int = 8192
    temperature: float = 0.0
    reasoning_effort: str = "medium"


def _stream_response(
    client: VllmClient,
    messages: list[dict[str, Any]],
    options: ChatOptions,
    *,
    display: Timeline,
) -> tuple[str, str, list[ToolCall]]:
    """Stream one response to the timeline; return text, reasoning, calls."""
    parts: list[str] = []
    thoughts: list[str] = []
    calls: list[ToolCall] = []
    for event in client.stream_chat(
        messages,
        max_tokens=options.max_tokens,
        temperature=options.temperature,
        reasoning_effort=options.reasoning_effort,
        tools=TOOLS,
    ):
        if isinstance(event, ToolCall):
            calls.append(event)
        elif event.stream == "reasoning":
            thoughts.append(event.text)
            display.token("reasoning", event.text)
        else:
            parts.append(event.text)
            display.token("content", event.text)
    return "".join(parts), "".join(thoughts), calls


def _seal_turn(
    journal: Path,
    *,
    turn: int,
    prompt: str,
    rounds: list[dict[str, Any]],
    reasoning: str,
    parent: str | None,
) -> str:
    """Append one chat-turn proof; return its hash for chaining."""
    node_id = f"chat#{turn}"
    diff = json.dumps({"prompt": prompt, "rounds": rounds})
    record = build_record(
        evidence_id=node_id,
        node_id=node_id,
        diff=diff,
        parent_proofs=[parent] if parent is not None else [],
        gate_outputs=[],
        requirement_ids=[],
        thinking=reasoning,
    )
    append_record(journal, record)
    return record.record_hash


def _run_turn(
    client: VllmClient,
    messages: list[dict[str, Any]],
    text: str,
    options: ChatOptions,
    *,
    turn: int,
    parent: str | None,
    display: Timeline,
) -> str:
    """Run one user turn: stream, journal tool calls, seal the turn proof."""
    node_id = f"chat#{turn}"
    messages.append({"role": "user", "content": text})
    rounds: list[dict[str, Any]] = []
    thinking: list[str] = []
    for _ in range(MAX_TOOL_ROUNDS):
        reply, reasoning, calls = _stream_response(client, messages, options, display=display)
        thinking.append(reasoning)
        if not calls:
            messages.append({"role": "assistant", "content": reply})
            rounds.append({"reply": reply, "tools": []})
            return _seal_turn(
                options.journal,
                turn=turn,
                prompt=text,
                rounds=rounds,
                reasoning="".join(thinking),
                parent=parent,
            )
        messages.append(
            {
                "role": "assistant",
                "content": reply,
                "tool_calls": [
                    {
                        "id": call.id,
                        "type": "function",
                        "function": {"name": call.name, "arguments": call.arguments},
                    }
                    for call in calls
                ],
            }
        )
        tools: list[dict[str, Any]] = []
        for call in calls:
            display.tool_call(call)
            start = perf_counter()
            result = execute_tool(call, workdir=options.workdir)
            duration_ms = int((perf_counter() - start) * 1000)
            # execute_tool reports every failure as an "error: ..." string, so
            # the prefix is the success signal (a file starting that way
            # misreads as failure; the full text stays in the span detail).
            exit_code = 1 if result.startswith("error: ") else 0
            display.tool_result(result, exit_code)
            append_span(
                options.journal,
                build_span(
                    node_id=node_id,
                    argv=[call.name, call.arguments],
                    duration_ms=duration_ms,
                    exit_code=exit_code,
                    detail=result,
                ),
            )
            tools.append({"name": call.name, "arguments": call.arguments, "result": result})
            messages.append({"role": "tool", "tool_call_id": call.id, "content": result})
        rounds.append({"reply": reply, "tools": tools})
    display.show_error(f"gave up after {MAX_TOOL_ROUNDS} tool rounds")
    return _seal_turn(
        options.journal,
        turn=turn,
        prompt=text,
        rounds=rounds,
        reasoning="".join(thinking),
        parent=parent,
    )


def run_chat(options: ChatOptions, client: VllmClient, *, stdin: IO[str], console: Console) -> int:
    """Read turns until /quit or EOF; stream each reply with its tool calls."""
    messages: list[dict[str, Any]] = []
    turn = 0
    parent: str | None = None
    display = Timeline(console)
    try:
        while True:
            display.show_prompt()
            line = stdin.readline()
            if not line:
                return 0
            text = line.strip()
            if not text:
                continue
            if text == "/quit":
                return 0
            turn += 1
            if not stdin.isatty():
                display.user_turn(text)
            display.show_rule()
            try:
                with display.live_turn():
                    parent = _run_turn(
                        client,
                        messages,
                        text,
                        options,
                        turn=turn,
                        parent=parent,
                        display=display,
                    )
            except VllmError as exc:
                display.show_error(str(exc))
    except KeyboardInterrupt:
        console.print()
        return 130
