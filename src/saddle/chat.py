"""Interactive streaming chat (`saddle up`): REPL over stream_chat plus tools."""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from time import perf_counter
from typing import IO, Any, Final

from rich.console import Console

from saddle.journal import append_record, append_span, build_record, build_span
from saddle.mcpclient import McpConfigError
from saddle.procs import ProcessLedger
from saddle.timeline import Timeline
from saddle.tools import ToolContext, attach_mcp, execute_tool, scope_turn
from saddle.vllm import StreamUsage, ToolCall, VllmClient, VllmError

MAX_TOOL_ROUNDS: Final = 10


@dataclass(frozen=True)
class ChatOptions:
    """Resolved `up` inputs: tool workdir, journal, plus sampling knobs."""

    workdir: Path = Path(".")
    journal: Path = Path(".saddle/chat.jsonl")
    max_tokens: int = 8192
    temperature: float = 0.0
    reasoning_effort: str = "medium"
    mode: str = "ask"
    """The lane, as in the web chat's `Session.mode`: "ask" offers read-only
    tools and refuses any other call; "edit" may write files and run
    commands in `workdir`, unaudited. Ask is the default in both chats."""
    full_access: bool = False
    """`saddle up --full-access`, confirmed at the prompt: Edit-lane commands
    run outside the sandbox, as in the web chat's `Session.full_access`."""


def _stream_response(
    client: VllmClient,
    messages: list[dict[str, Any]],
    options: ChatOptions,
    *,
    display: Timeline,
    tools: list[dict[str, Any]],
) -> tuple[str, str, list[ToolCall]]:
    """Stream one response to the timeline; return text, reasoning, calls.

    `tools` is what the model is offered: the turn's `scope_turn` list."""
    parts: list[str] = []
    thoughts: list[str] = []
    calls: list[ToolCall] = []
    for event in client.stream_chat(
        messages,
        max_tokens=options.max_tokens,
        temperature=options.temperature,
        reasoning_effort=options.reasoning_effort,
        tools=tools,
    ):
        if isinstance(event, ToolCall):
            calls.append(event)
        elif isinstance(event, StreamUsage):
            continue
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
        requirement_ids=[],  # chat-driven edits declare no requirements
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
    context: ToolContext | None = None,
) -> str:
    """Run one user turn: stream, journal tool calls, seal the turn proof.

    `context` is the session's shared `ToolContext`, carrying the sandbox and
    its background terminals across turns. A caller with no session (a
    direct test, or any future caller that wants one turn in isolation)
    passes none, and this builds exactly one for the whole turn -- never one
    per tool call, which would strand a terminal after its first read.
    """
    node_id = f"chat#{turn}"
    ctx = context or ToolContext(workdir=options.workdir, processes=ProcessLedger())
    offered = scope_turn(ctx, options.mode)
    messages.append({"role": "user", "content": text})
    rounds: list[dict[str, Any]] = []
    thinking: list[str] = []
    for _ in range(MAX_TOOL_ROUNDS):
        reply, reasoning, calls = _stream_response(
            client, messages, options, display=display, tools=offered
        )
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
            result = execute_tool(call, workdir=options.workdir, context=ctx)
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
    # One context for the whole session: a background terminal started by
    # run_command in turn N must still be there for read_terminal or
    # wait_for_terminal in turn N+1. Built here, not inside the loop, so two
    # sessions (two `run_chat` calls) never share it.
    context = ToolContext(
        workdir=options.workdir, full_access=options.full_access, processes=ProcessLedger()
    )
    if options.mode == "edit":
        try:
            attach_mcp(context)  # the person's allowlist
        except McpConfigError as exc:
            display.show_error(str(exc))  # a broken allowlist is named, never skipped
            return 1
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
                        context=context,
                    )
            except VllmError as exc:
                display.show_error(str(exc))
    except KeyboardInterrupt:
        console.print()
        return 130
    finally:
        context.stop_processes()  # nothing the session started outlives it
