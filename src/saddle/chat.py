"""Interactive streaming chat (`saddle up`): REPL over stream_chat plus tools."""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from time import perf_counter
from typing import IO, Any, Final

from saddle.journal import append_record, append_span, build_record, build_span
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
    stdout: IO[str],
) -> tuple[str, str, list[ToolCall]]:
    """Stream one response: print tokens live; return text, reasoning, calls."""
    # None is falsy like False, so None mutants are behaviorally identical
    # in the truthiness tests below: unkillable, hence pragma (both lines).
    thinking = False  # pragma: no mutate
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
            if not thinking:
                stdout.write("thinking: ")
                thinking = True
            stdout.write(event.text)
            stdout.flush()
            thoughts.append(event.text)
        else:
            if thinking:
                stdout.write("\n")
                thinking = False  # pragma: no mutate
            stdout.write(event.text)
            stdout.flush()
            parts.append(event.text)
    if thinking:
        stdout.write("\n")
    if parts:
        stdout.write("\n")
    stdout.flush()
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
    stdout: IO[str],
) -> str:
    """Run one user turn: stream, journal tool calls, seal the turn proof."""
    node_id = f"chat#{turn}"
    messages.append({"role": "user", "content": text})
    rounds: list[dict[str, Any]] = []
    thinking: list[str] = []
    for _ in range(MAX_TOOL_ROUNDS):
        reply, reasoning, calls = _stream_response(client, messages, options, stdout=stdout)
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
            start = perf_counter()
            result = execute_tool(call, workdir=options.workdir)
            duration_ms = int((perf_counter() - start) * 1000)
            # execute_tool reports every failure as an "error: ..." string, so
            # the prefix is the success signal (a file starting that way
            # misreads as failure; the full text stays in the span detail).
            exit_code = 1 if result.startswith("error: ") else 0
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
            stdout.write(f"$ {call.name} {call.arguments}\n{result}\n")
            stdout.flush()
            messages.append({"role": "tool", "tool_call_id": call.id, "content": result})
        rounds.append({"reply": reply, "tools": tools})
    stdout.write(f"error: gave up after {MAX_TOOL_ROUNDS} tool rounds\n")
    return _seal_turn(
        options.journal,
        turn=turn,
        prompt=text,
        rounds=rounds,
        reasoning="".join(thinking),
        parent=parent,
    )


def run_chat(options: ChatOptions, client: VllmClient, *, stdin: IO[str], stdout: IO[str]) -> int:
    """Read turns until /quit or EOF; stream each reply with its tool calls."""
    messages: list[dict[str, Any]] = []
    turn = 0
    parent: str | None = None
    try:
        while True:
            stdout.write("you> ")
            stdout.flush()
            line = stdin.readline()
            if not line:
                return 0
            text = line.strip()
            if not text:
                continue
            if text == "/quit":
                return 0
            turn += 1
            try:
                parent = _run_turn(
                    client, messages, text, options, turn=turn, parent=parent, stdout=stdout
                )
            except VllmError as exc:
                stdout.write(f"error: {exc}\n")
    except KeyboardInterrupt:
        stdout.write("\n")
        return 130
