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

from saddle import cli
from saddle.auto import AutoOptions, run_auto
from saddle.engine import TurnOptions, run_turn
from tests.test_auto import namespace, repo  # noqa: F401  (fixture)
from tests.test_usage import FakeServer, _delta, _finish_call, _sealed, _sse

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
