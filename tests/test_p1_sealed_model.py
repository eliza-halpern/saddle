"""The P1 file an extraction seals names the model its passes asked.

`saddle requirements extract` passes `--model` to `task_passes.extract`;
`saddle auto --extract-requirements` (and a chat task run, which calls
`run_auto`) passed nothing, and a dogfood run sealed `"model": ""`. Both
paths are driven here through a real `VllmClient` over a fake server, as the
CLI and the web server build it.
"""

from __future__ import annotations

import json
from pathlib import Path

import httpx
from test_feed import repo
from test_p1_auto import Factory
from test_usage import _finish_call, _sse

from saddle.auto import P1_FILE, AutoOptions, run_auto
from saddle.vllm import DEFAULT_MODEL, VllmClient
from saddle.web.tasks import TaskRun, execute

__all__ = ["repo"]

SERVED = "served-model-27b"


class Server:
    """Streams the worker a `finish`; answers each P1 pass with prose (no inputs)."""

    def __init__(self) -> None:
        self.models: list[tuple[bool, str]] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        if not request.url.path.endswith("/chat/completions"):
            return httpx.Response(404, json={})
        payload = json.loads(request.content)
        stream = bool(payload.get("stream"))
        self.models.append((stream, str(payload.get("model"))))
        if stream:
            return httpx.Response(
                200, text=_sse(_finish_call()), headers={"Content-Type": "text/event-stream"}
            )
        message = {"role": "assistant", "content": "no inputs, not json"}
        return httpx.Response(
            200, json={"choices": [{"index": 0, "message": message, "finish_reason": "stop"}]}
        )

    def client(self) -> VllmClient:
        return VllmClient(api_key="test-key", model=SERVED, transport=httpx.MockTransport(self))


def sealed_model(path: Path) -> str:
    return str(json.loads(path.read_text(encoding="utf-8"))["model"])


def test_saddle_auto_seals_the_model_its_passes_asked(repo: Path) -> None:
    server = Server()
    options = AutoOptions(
        task="make add add",
        repo=repo,
        run_id="named",
        auditor_factory=Factory(),
        extract_requirements=True,
    )
    result = run_auto(options, server.client())
    passes = [model for stream, model in server.models if not stream]
    assert passes == [SERVED]  # P-a; its prose names no input, so no P-b
    assert sealed_model(result.journal.parent / P1_FILE) == SERVED


def test_a_chat_task_run_seals_the_model_its_passes_asked(
    repo: Path,
    tmp_path: Path,
) -> None:
    server = Server()
    run = TaskRun(run_id="web", session_id="s", task="make add add", time_budget_s=60,
                  token_budget=10_000)  # fmt: skip
    execute(
        run,
        workdir=repo,
        client=server.client(),
        publish=lambda _e: None,
        chat_journal=tmp_path / "chat.jsonl",
        reasoning_effort="low",
        audit=None,
        feed_auditor=Factory(),
        extract_requirements=True,
    )
    assert run.journal is not None
    assert [model for stream, model in server.models if not stream] == [SERVED]
    assert sealed_model(run.journal.parent / P1_FILE) == SERVED


def test_a_client_names_the_model_it_was_built_with() -> None:
    assert VllmClient(api_key="k", model="m-1").model == "m-1"
    assert VllmClient(api_key="k").model == DEFAULT_MODEL
