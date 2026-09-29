"""`--keep-reasoning`: each round's reasoning goes back within the autonomous turn.

Both halves on the wire (an httpx fake speaking vLLM's SSE): with the flag,
round N+1's request carries round N's reasoning on its assistant message,
in the `reasoning` field; without it, no request carries it at all, and the
sealed outcome says which shape ran. Interactive chat never keeps it.
"""

from __future__ import annotations

import inspect
import io
import json
import subprocess
import time
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import jinja2
import pytest
from starlette.testclient import TestClient
from test_auto import namespace, repo  # noqa: F401  (fixture)
from test_ui3_mode import NoModel, _server_of
from test_usage import FakeServer, _delta, _finish_call, _sealed, _sse

from saddle import cli
from saddle.auto import AutoError, AutoOptions, run_auto
from saddle.engine import TurnOptions, run_turn
from saddle.sessions import SessionStore
from saddle.web import tasks
from saddle.web.app import ChatServer, build_app
from saddle.web.tasks import TaskRun

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
    assert cli.build_parser().parse_args(["auto", "t"]).keep_reasoning is True
    server = FakeServer(_script())
    code = cli.run_auto_command(
        namespace(repo, keep_reasoning=True), server.client(), stdout=io.StringIO()
    )
    assert code == 0
    assert [m.get("reasoning") for m in _assistants(server.payloads[1])] == [R1]


# -- the default (#72): on for `saddle auto` and chat-started runs; opt-out off --


def test_saddle_auto_keeps_reasoning_by_default_on_the_wire_and_in_the_seal(
    repo: Path,  # noqa: F811
) -> None:
    server = FakeServer(_script())
    args = cli.build_parser().parse_args(
        ["auto", "make add add", "--repo", str(repo), "--no-audit"]
    )
    code = cli.run_auto_command(args, server.client(), stdout=io.StringIO())
    assert code == 0
    assert [m.get("reasoning") for m in _assistants(server.payloads[2])] == [R1, R2]
    assert f"<think>\n{R1}\n</think>" in _render(server.payloads[2])
    evidence, _ = _sealed(_only_run(repo))
    assert evidence["prompt_shape"] == {"keep_reasoning": True}


def test_no_keep_reasoning_turns_it_off_on_the_wire_and_in_the_seal(
    repo: Path,  # noqa: F811
) -> None:
    server = FakeServer(_script())
    argv = ["auto", "make add add", "--repo", str(repo), "--no-audit", "--no-keep-reasoning"]
    code = cli.run_auto_command(
        cli.build_parser().parse_args(argv), server.client(), stdout=io.StringIO()
    )
    assert code == 0
    for payload in server.payloads:
        assert R1 not in json.dumps(payload)
    evidence, _ = _sealed(_only_run(repo))
    assert evidence["prompt_shape"] == {"keep_reasoning": False}


def _only_run(repo_path: Path) -> SimpleNamespace:
    (journal,) = (repo_path / ".saddle" / "runs").glob("*/proofs.jsonl")
    return SimpleNamespace(journal=journal)


def test_auto_options_default_is_on() -> None:
    assert AutoOptions(task="t", repo=Path()).keep_reasoning is True


def test_a_chat_started_run_keeps_reasoning_unless_turned_off(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The chat's Task path builds its own AutoOptions (web.tasks.execute)."""
    seen: list[bool] = []

    def fake_auto(options: AutoOptions, client: Any, **_: Any) -> None:
        seen.append(options.keep_reasoning)
        msg = "stop here"
        raise AutoError(msg)

    monkeypatch.setattr(tasks, "run_auto", fake_auto)
    repo_path = tmp_path / "r"
    repo_path.mkdir()
    subprocess.run(["git", "init", "-q", str(repo_path)], check=True)
    for extra in ({}, {"keep_reasoning": False}):
        run = TaskRun(run_id="r1", session_id="s", task="t", time_budget_s=60, token_budget=10)
        tasks.execute(
            run,
            workdir=repo_path,
            client=None,
            publish=lambda _: None,
            chat_journal=tmp_path / "chat.jsonl",
            reasoning_effort="none",
            audit=None,
            **extra,  # type: ignore[arg-type]
        )
    assert seen == [True, False]


def test_the_chat_server_passes_its_setting_to_each_run(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    got: list[Any] = []

    def fake_execute(run: TaskRun, **kwargs: Any) -> tuple[str, None]:
        got.append(kwargs["keep_reasoning"])
        run.state = "finished"
        return "finished", None

    monkeypatch.setattr(tasks, "execute", fake_execute)
    for keep in (True, False):
        store = SessionStore(tmp_path / f"s{keep}")
        sid = store.create(workdir=str(tmp_path)).id
        app = build_app(store, NoModel, default_workdir=tmp_path, keep_reasoning=keep)
        with TestClient(app) as client:
            client.post(f"/api/sessions/{sid}/task", json={"text": "fix it"})
            deadline = time.monotonic() + 5
            while len(got) < (1 if keep else 2):
                assert time.monotonic() < deadline, "timed out"
                time.sleep(0.02)
    assert got == [True, False]
    assert (
        _server_of(
            build_app(SessionStore(tmp_path / "d"), NoModel, default_workdir=tmp_path)
        ).keep_reasoning
        is True
    )
    direct = ChatServer(SessionStore(tmp_path / "c"), NoModel, default_workdir=tmp_path)
    assert direct.keep_reasoning is True


def test_saddle_chat_has_the_same_default_and_off_switch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import uvicorn

    from saddle.web import app as web_app

    served: list[bool] = []
    monkeypatch.setattr(
        uvicorn, "run", lambda app, **kw: served.append(_server_of(app).keep_reasoning)
    )
    monkeypatch.setenv("SADDLE_VLLM_API_KEY", "k")
    for extra in ([], ["--no-keep-reasoning"]):
        argv = ["chat", "--no-open", "--workdir", str(tmp_path), "--sessions", str(tmp_path / "s")]
        assert cli.main([*argv, *extra], stdout=io.StringIO()) == 0
    assert served == [True, False]
    # Through the signature, which follows `__wrapped__`: in mutmut's work
    # copy `serve` is a trampoline with no defaults of its own.
    assert inspect.signature(web_app.serve).parameters["keep_reasoning"].default is True


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
