"""`--keep-reasoning`: each round's reasoning goes back within the autonomous turn.

Both halves on the wire (an httpx fake speaking vLLM's SSE): with the flag,
round N+1's request carries round N's reasoning on its assistant message,
in the `reasoning` field; without it, no request carries it at all, and the
sealed outcome says which shape ran. Interactive chat never keeps it.
"""

from __future__ import annotations

import io
import json
from pathlib import Path
from typing import Any

import jinja2

from saddle import cli
from saddle.auto import AutoOptions, run_auto
from saddle.engine import TurnOptions, run_turn
from tests.test_auto import namespace, repo  # noqa: F401  (fixture)
from tests.test_usage import FakeServer, _delta, _finish_call, _sealed, _sse

TEMPLATE = Path(__file__).parent / "fixtures" / "qwen38_chat_template.jinja"
"""The served Qwen3.8 chat template, byte for byte (sha256 c3cf9e34..., the
same file in every model directory of qwen38-27b-rtx3090; no start script
overrides it). It reads only `reasoning_content` on past assistant turns."""
BLANK = "<think>\n\n</think>"

R1 = "first-round reasoning about calc.py"
R2 = "second-round reasoning after the nudge"


def _read_call() -> dict[str, Any]:
    call = {"index": 0, "id": "r1", "type": "function"}
    call["function"] = {"name": "read_file", "arguments": json.dumps({"path": "calc.py"})}
    return _delta({"tool_calls": [call]})


def _script() -> list[str]:
    # a tool round, a plain reply (the loop nudges as role=user), then finish
    return [
        _sse(_delta({"reasoning": R1}), _read_call()),
        _sse(_delta({"reasoning": R2}), _delta({"content": "thinking aloud"})),
        _sse(_delta({"reasoning": "last"}), _finish_call()),
    ]


def _assistants(payload: dict[str, Any]) -> list[dict[str, Any]]:
    return [m for m in payload["messages"] if m["role"] == "assistant"]


def _run(repo_path: Path, keep: bool) -> tuple[FakeServer, Any]:
    server = FakeServer(_script())
    options = AutoOptions(task="make add add", repo=repo_path, run_id="k1", keep_reasoning=keep)
    return server, run_auto(options, server.client())


def test_with_the_flag_the_next_round_carries_the_reasoning(repo: Path) -> None:  # noqa: F811
    server, result = _run(repo, keep=True)
    assert len(server.payloads) == 3
    assert [m.get("reasoning") for m in _assistants(server.payloads[1])] == [R1]
    assert [m.get("reasoning") for m in _assistants(server.payloads[2])] == [R1, R2]
    # it survives the nudge (role=user) that follows the plain reply
    assert server.payloads[2]["messages"][-1]["role"] == "user"
    evidence, _ = _sealed(result)
    assert evidence["prompt_shape"] == {"keep_reasoning": True}


def test_without_the_flag_no_request_carries_reasoning(repo: Path) -> None:  # noqa: F811
    server, result = _run(repo, keep=False)
    assert len(server.payloads) == 3
    for payload in server.payloads:
        assert R1 not in json.dumps(payload)
        assert all(set(m) <= {"role", "content", "tool_calls"} for m in _assistants(payload))
    evidence, _ = _sealed(result)
    assert evidence["prompt_shape"] == {"keep_reasoning": False}


def test_the_cli_flag_reaches_the_wire(repo: Path) -> None:  # noqa: F811
    assert cli.build_parser().parse_args(["auto", "t"]).keep_reasoning is False
    server = FakeServer(_script())
    code = cli.run_auto_command(
        namespace(repo, keep_reasoning=True), server.client(), stdout=io.StringIO()
    )
    assert code == 0
    assert [m.get("reasoning") for m in _assistants(server.payloads[1])] == [R1]


def test_interactive_chat_never_keeps_reasoning(tmp_path: Path) -> None:
    server = FakeServer(
        [_sse(_delta({"reasoning": R1}), _read_call()), _sse(_delta({"content": "ok"}))]
    )
    (tmp_path / "calc.py").write_text("x = 1\n")
    options = TurnOptions(workdir=tmp_path, journal=tmp_path / "j.jsonl", keep_reasoning=True)
    list(run_turn(server.client(), [], "look", options, turn=1))
    assert len(server.payloads) == 2
    assert R1 not in json.dumps(server.payloads[1])


def _raise(message: str) -> None:
    raise ValueError(message)


def _render(payload: dict[str, Any]) -> str:
    """Render a request body saddle emitted through the served template.

    Tool-call arguments are parsed to mappings first, as vLLM does before
    templating (the template iterates `arguments|items`); nothing else of
    the body is touched, so which reasoning key saddle sent decides the
    outcome."""
    messages = json.loads(json.dumps(payload["messages"]))
    for message in messages:
        for call in message.get("tool_calls") or []:
            call["function"]["arguments"] = json.loads(call["function"]["arguments"])
    env = jinja2.Environment(extensions=["jinja2.ext.loopcontrols"])
    env.globals["raise_exception"] = _raise
    template = env.from_string(TEMPLATE.read_text())
    return template.render(
        messages=messages,
        tools=payload.get("tools"),
        add_generation_prompt=True,
        reasoning_effort=payload.get("reasoning_effort"),
    )


def test_contract_kept_reasoning_reaches_the_served_template(repo: Path) -> None:  # noqa: F811
    """The contract: round N's reasoning is in request N+1's *rendered* prompt."""
    server, _ = _run(repo, keep=True)
    first, second = _render(server.payloads[1]), _render(server.payloads[2])
    assert f"<think>\n{R1}\n</think>" in first
    assert f"<think>\n{R1}\n</think>" in second
    assert f"<think>\n{R2}\n</think>" in second
    assert BLANK not in second


def test_contract_without_the_flag_the_template_sees_a_blank_think(repo: Path) -> None:  # noqa: F811
    server, _ = _run(repo, keep=False)
    second = _render(server.payloads[2])
    assert R1 not in second
    assert R2 not in second
    assert second.count(BLANK) == 2
