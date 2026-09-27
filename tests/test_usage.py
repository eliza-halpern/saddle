"""The token budget is charged with the server's usage, not an estimate.

Each half both ways, against a scripted fake server (an httpx transport
speaking vLLM's SSE): a stream whose final chunk carries `usage` is charged
its `completion_tokens` and sealed `token_source: "usage"`; a stream with
no usage chunk falls back to characters / 4 and is sealed "estimate".
"""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any, cast
from unittest.mock import MagicMock

import httpx
import pytest

from saddle.auto import AutoOptions, run_auto
from saddle.chat import ChatOptions, _stream_response
from saddle.engine import RunBudget
from saddle.journal import attempt_sidecar_path, read_spans, verify_journal
from saddle.vllm import StreamToken, StreamUsage, VllmClient, VllmResponseError
from tests.test_auto import repo  # noqa: F401  (fixture)


def _sse(*chunks: dict[str, Any]) -> str:
    return "".join(f"data: {json.dumps(c)}\n\n" for c in chunks) + "data: [DONE]\n\n"


def _delta(delta: dict[str, Any]) -> dict[str, Any]:
    # vLLM sends "usage": null on every delta chunk once include_usage is on
    return {"choices": [{"index": 0, "delta": delta}], "usage": None}


def _usage_chunk(prompt: int, completion: int) -> dict[str, Any]:
    return {
        "choices": [],
        "usage": {
            "prompt_tokens": prompt,
            "completion_tokens": completion,
            "total_tokens": prompt + completion,
        },
    }


def _finish_call() -> dict[str, Any]:
    arguments = json.dumps({"summary": "done"})
    return _delta(
        {
            "tool_calls": [
                {
                    "index": 0,
                    "id": "f1",
                    "type": "function",
                    "function": {"name": "finish", "arguments": arguments},
                }
            ]
        }
    )


class FakeServer:
    """Answers each chat request with the next scripted SSE body; 404s /tokenize."""

    def __init__(self, bodies: list[str], tail: str | None = None) -> None:
        self.bodies, self.tail = list(bodies), tail
        self.payloads: list[dict[str, Any]] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        if not request.url.path.endswith("/chat/completions"):
            return httpx.Response(404, json={})
        self.payloads.append(json.loads(request.content))
        body = self.bodies.pop(0) if self.bodies else self.tail
        assert body is not None, "script exhausted"
        return httpx.Response(200, text=body, headers={"Content-Type": "text/event-stream"})

    def client(self) -> VllmClient:
        return VllmClient(api_key="test-key", transport=httpx.MockTransport(self))


# -- the client ------------------------------------------------------------


def test_stream_requests_usage_and_yields_it_last() -> None:
    server = FakeServer([_sse(_delta({"content": "hi"}), _usage_chunk(40, 7))])
    events = list(server.client().stream_chat([{"role": "user", "content": "q"}]))
    assert events == [
        StreamToken(stream="content", text="hi"),
        StreamUsage(prompt_tokens=40, completion_tokens=7),
    ]
    assert server.payloads[0]["stream_options"] == {"include_usage": True}


def test_stream_without_usage_yields_none() -> None:
    server = FakeServer([_sse(_delta({"content": "hi"}))])
    events = list(server.client().stream_chat([{"role": "user", "content": "q"}]))
    assert events == [StreamToken(stream="content", text="hi")]


def test_usage_without_completion_tokens_is_not_usage() -> None:
    body = _sse(_delta({"content": "hi"}), {"choices": [], "usage": {"prompt_tokens": 3}})
    events = list(FakeServer([body]).client().stream_chat([{"role": "user", "content": "q"}]))
    assert events == [StreamToken(stream="content", text="hi")]


def test_usage_on_a_delta_chunk_is_read_and_missing_prompt_is_zero() -> None:
    chunk = {"choices": [{"delta": {"content": "hi"}}], "usage": {"completion_tokens": 2}}
    client = FakeServer([_sse(chunk)]).client()
    events = list(client.stream_chat([{"role": "user", "content": "q"}]))
    assert events[-1] == StreamUsage(prompt_tokens=0, completion_tokens=2)


def test_an_empty_choices_chunk_without_usage_is_still_malformed() -> None:
    body = _sse({"choices": []})
    with pytest.raises(VllmResponseError, match="stream chunk has no choices"):
        list(FakeServer([body]).client().stream_chat([{"role": "user", "content": "q"}]))


def test_chat_repl_ignores_usage_events() -> None:
    server = FakeServer([_sse(_delta({"content": "hi"}), _usage_chunk(4, 1))])
    reply, reasoning, calls = _stream_response(
        server.client(),
        [{"role": "user", "content": "q"}],
        ChatOptions(),
        display=MagicMock(),
        tools=[],
    )
    assert (reply, reasoning, calls) == ("hi", "", [])


# -- the budget ------------------------------------------------------------


def test_budget_charges_usage_when_present_else_estimate() -> None:
    budget = RunBudget(time_s=60, tokens=1000)
    assert budget.source() == "none"
    assert budget.charge(400, StreamUsage(prompt_tokens=9, completion_tokens=250)) == (250, "usage")
    assert (budget.spent_tokens, budget.source()) == (250, "usage")
    assert budget.charge(400) == (100, "estimate")
    assert budget.by_source == {"usage": 250, "estimate": 100}
    assert budget.source() == "mixed"
    only = RunBudget(time_s=60, tokens=1000)
    only.charge(40)
    assert only.source() == "estimate"


# -- the run, end to end ---------------------------------------------------


def _run(repo_path: Path, server: FakeServer, tokens: int = 1000) -> Any:
    options = AutoOptions(task="make add add", repo=repo_path, run_id="r1", token_budget=tokens)
    return run_auto(options, server.client())


def _sealed(result: Any) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    spans = read_spans(result.journal)
    outcome = [s for s in spans if s.name.startswith("auto:") and s.name != "auto:spend"][-1]
    evidence = json.loads(attempt_sidecar_path(result.journal, outcome.span_id).read_text())
    spends = [json.loads(s.argv[1]) for s in spans if s.name == "auto:spend"]
    return cast(dict[str, Any], evidence), spends


def test_a_run_with_usage_is_charged_and_sealed_as_usage(repo: Path) -> None:  # noqa: F811
    server = FakeServer([_sse(_finish_call(), _usage_chunk(1234, 321))])
    result = _run(repo, server)
    assert server.payloads[0]["stream_options"] == {"include_usage": True}
    evidence, spends = _sealed(result)
    old = ("completion_tokens", "prompt_tokens", "token_source")
    assert [{k: sp[k] for k in old} for sp in spends] == [
        {"completion_tokens": 321, "prompt_tokens": 1234, "token_source": "usage"}
    ]
    assert evidence["tokens_spent"] == 321
    assert evidence["token_source"] == "usage"
    assert evidence["tokens_by_source"] == {"usage": 321, "estimate": 0}
    assert verify_journal(result.journal) == []


def test_a_run_without_usage_falls_back_and_is_sealed_as_estimate(repo: Path) -> None:  # noqa: F811
    server = FakeServer([_sse(_finish_call())])
    result = _run(repo, server)
    evidence, spends = _sealed(result)
    estimate = max(1, len(json.dumps({"summary": "done"})) // 4)
    old = ("completion_tokens", "prompt_tokens", "token_source")
    assert [{k: sp[k] for k in old} for sp in spends] == [
        {"completion_tokens": estimate, "prompt_tokens": None, "token_source": "estimate"}
    ]
    assert evidence["tokens_spent"] == estimate
    assert evidence["token_source"] == "estimate"


def test_the_budget_stop_uses_the_real_count(repo: Path) -> None:  # noqa: F811
    # "hm" estimates to 1 token; the server says 1200 were generated (reasoning
    # the stream never showed). Only the real count can stop the run here.
    server = FakeServer([], tail=_sse(_delta({"content": "hm"}), _usage_chunk(50, 1200)))
    result = _run(repo, server, tokens=1000)
    assert result.outcome == "stopped"
    assert result.reason.startswith("token budget exhausted")
    assert len(server.payloads) == 1


def test_without_usage_the_same_run_is_not_stopped_by_tokens(repo: Path) -> None:  # noqa: F811
    server = FakeServer([_sse(_delta({"content": "hm"})), _sse(_finish_call())], tail=None)
    result = _run(repo, server, tokens=1000)
    # Not stopped by tokens: the run reaches its finish call. The script edits
    # nothing, so under the default auditor arm that finish ends `unchanged`,
    # not `finished` (FEEDFIX item 5).
    assert result.outcome == "unchanged"
    assert len(server.payloads) == 2


# -- what each round spent (SPEED F-a; REASON commit A) -----------------------


class SlowServer(FakeServer):
    """A FakeServer that takes `delay_s` to answer, so round-trip time is real."""

    def __init__(self, bodies: list[str], delay_s: float) -> None:
        super().__init__(bodies)
        self.delay_s = delay_s

    def __call__(self, request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/chat/completions"):
            time.sleep(self.delay_s)
        return super().__call__(request)


def _usage_with_reasoning(prompt: int, completion: int, reasoning: int) -> dict[str, Any]:
    chunk = _usage_chunk(prompt, completion)
    chunk["usage"]["completion_tokens_details"] = {"reasoning_tokens": reasoning}
    return chunk


def test_stream_usage_carries_the_servers_reasoning_count() -> None:
    server = FakeServer([_sse(_delta({"content": "hi"}), _usage_with_reasoning(40, 7, 5))])
    events = list(server.client().stream_chat([{"role": "user", "content": "q"}]))
    assert events[-1] == StreamUsage(prompt_tokens=40, completion_tokens=7, reasoning_tokens=5)


def test_a_spend_records_reasoning_from_usage_and_the_round_trip(repo: Path) -> None:  # noqa: F811
    # The reasoning text is 40 chars (10 estimated tokens); the server says 23.
    # Only the usage source gives 23, and the 50 ms answer must show in model_ms.
    thought = "x" * 40
    body = _sse(_delta({"reasoning": thought}), _finish_call(), _usage_with_reasoning(99, 60, 23))
    result = _run(repo, SlowServer([body], delay_s=0.05))
    _, spends = _sealed(result)
    (spend,) = spends
    assert (spend["reasoning_tokens"], spend["reasoning_source"]) == (23, "usage")
    assert (spend["completion_tokens"], spend["prompt_tokens"]) == (60, 99)
    assert spend["model_ms"] >= 50
    assert 0 <= spend["ttft_ms"] <= spend["model_ms"]
    assert spend["ttft_ms"] >= 50
    assert verify_journal(result.journal) == []


def test_without_usage_reasoning_is_the_texts_token_count(repo: Path) -> None:  # noqa: F811
    thought = "y" * 48  # 12 tokens at CHARS_PER_TOKEN=4
    result = _run(repo, FakeServer([_sse(_delta({"reasoning": thought}), _finish_call())]))
    _, spends = _sealed(result)
    assert (spends[0]["reasoning_tokens"], spends[0]["reasoning_source"]) == (12, "estimate")


@pytest.mark.parametrize("with_usage", [False, True])
def test_a_round_with_no_reasoning_records_zero_not_missing(
    repo: Path,  # noqa: F811
    with_usage: bool,
) -> None:
    tail = [_usage_chunk(5, 4)] if with_usage else []
    (spend,) = _sealed(_run(repo, FakeServer([_sse(_finish_call(), *tail)])))[1]
    assert spend["reasoning_tokens"] == 0
    assert isinstance(spend["model_ms"], int)
    assert isinstance(spend["ttft_ms"], int)
