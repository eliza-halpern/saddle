"""Interactive streaming chat (`saddle up`): REPL over stream_chat plus tools."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import IO, Any, Final

from saddle.tools import TOOLS, execute_tool
from saddle.vllm import ToolCall, VllmClient, VllmError

MAX_TOOL_ROUNDS: Final = 10


@dataclass(frozen=True)
class ChatOptions:
    """Resolved `up` inputs: tool workdir plus sampling knobs."""

    workdir: Path = Path(".")
    max_tokens: int = 8192
    temperature: float = 0.0
    reasoning_effort: str = "medium"


def _stream_response(
    client: VllmClient,
    messages: list[dict[str, Any]],
    options: ChatOptions,
    *,
    stdout: IO[str],
) -> tuple[str, list[ToolCall]]:
    """Stream one response: print tokens live; return text plus tool calls."""
    # None is falsy like False, so None mutants are behaviorally identical
    # in the truthiness tests below: unkillable, hence pragma (both lines).
    thinking = False  # pragma: no mutate
    parts: list[str] = []
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
    return "".join(parts), calls


def _run_turn(
    client: VllmClient,
    messages: list[dict[str, Any]],
    text: str,
    options: ChatOptions,
    *,
    stdout: IO[str],
) -> None:
    """Run one user turn: stream, execute tool calls, re-prompt until answered."""
    messages.append({"role": "user", "content": text})
    for _ in range(MAX_TOOL_ROUNDS):
        reply, calls = _stream_response(client, messages, options, stdout=stdout)
        if not calls:
            messages.append({"role": "assistant", "content": reply})
            return
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
        for call in calls:
            result = execute_tool(call, workdir=options.workdir)
            stdout.write(f"$ {call.name} {call.arguments}\n{result}\n")
            stdout.flush()
            messages.append({"role": "tool", "tool_call_id": call.id, "content": result})
    stdout.write(f"error: gave up after {MAX_TOOL_ROUNDS} tool rounds\n")


def run_chat(options: ChatOptions, client: VllmClient, *, stdin: IO[str], stdout: IO[str]) -> int:
    """Read turns until /quit or EOF; stream each reply with its tool calls."""
    messages: list[dict[str, Any]] = []
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
            try:
                _run_turn(client, messages, text, options, stdout=stdout)
            except VllmError as exc:
                stdout.write(f"error: {exc}\n")
    except KeyboardInterrupt:
        stdout.write("\n")
        return 130
