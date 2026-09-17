"""Tests for saddle.vllm: guided emission over a mocked httpx transport."""

from __future__ import annotations

import json
from typing import Any

import httpx
import pytest

from saddle.dag import dag_json_schema
from saddle.vllm import (
    DEFAULT_MODEL,
    DIFF_SCHEMA,
    DiffProposal,
    VllmAuthError,
    VllmClient,
    VllmRequestError,
    VllmResponseError,
)

PLAN_PROMPT = "Plan a two-node DAG that adds input validation to the login form."


def _ok_body(
    *, content: str, reasoning: Any = "decomposing the task...", finish_reason: Any = "stop"
) -> dict[str, Any]:
    message: dict[str, Any] = {"role": "assistant", "content": content}
    if reasoning is not None:
        message["reasoning"] = reasoning
    choice: dict[str, Any] = {"message": message}
    if finish_reason is not None:
        choice["finish_reason"] = finish_reason
    return {"choices": [choice], "model": DEFAULT_MODEL}


def _client_for(response: httpx.Response) -> tuple[VllmClient, list[httpx.Request]]:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return response

    client = VllmClient(api_key="test-key", transport=httpx.MockTransport(handler))
    return client, seen


def _json_client(payload: Any, *, status: int = 200) -> tuple[VllmClient, list[httpx.Request]]:
    return _client_for(httpx.Response(status, json=payload))


def _failing_client(exc: httpx.HTTPError) -> VllmClient:
    def handler(request: httpx.Request) -> httpx.Response:
        raise exc

    return VllmClient(api_key="test-key", transport=httpx.MockTransport(handler))


def test_emit_posts_guided_payload() -> None:
    dag: dict[str, Any] = {"nodes": []}
    client, seen = _json_client(_ok_body(content=json.dumps(dag)))
    emission = client.emit_dag(PLAN_PROMPT)

    assert len(seen) == 1
    request = seen[0]
    assert request.url.host == "127.0.0.1"
    assert request.url.port == 18020
    assert request.url.path == "/v1/chat/completions"
    assert (b"Authorization", b"Bearer test-key") in request.headers.raw
    assert json.loads(request.content) == {
        "model": DEFAULT_MODEL,
        "messages": [{"role": "user", "content": PLAN_PROMPT}],
        "temperature": 0.0,
        "max_tokens": 4096,
        "reasoning_effort": "medium",
        "include_reasoning": True,
        "structured_outputs": {"json": dag_json_schema()},
    }
    assert emission.dag == dag
    assert emission.reasoning == "decomposing the task..."
    assert emission.raw_content == json.dumps(dag)


def test_emit_honors_sampling_overrides() -> None:
    client, seen = _json_client(_ok_body(content=json.dumps({"nodes": []})))
    client.emit_dag(PLAN_PROMPT, max_tokens=128, temperature=0.5, reasoning_effort="xhigh")

    body = json.loads(seen[0].content)
    assert body["max_tokens"] == 128
    assert body["temperature"] == 0.5
    assert body["reasoning_effort"] == "xhigh"


def test_client_uses_configured_timeout() -> None:
    client, _ = _json_client(_ok_body(content=json.dumps({"nodes": []})))
    assert client._client.timeout.read == 300.0
    assert client._client.timeout.connect == 300.0


def test_base_url_trailing_slash_normalized() -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json=_ok_body(content=json.dumps({"nodes": []})))

    client = VllmClient(
        api_key="test-key",
        base_url="http://127.0.0.1:18020/v1/",
        transport=httpx.MockTransport(handler),
    )
    client.emit_dag(PLAN_PROMPT)
    assert seen[0].url.path == "/v1/chat/completions"


def test_init_rejects_empty_api_key() -> None:
    with pytest.raises(ValueError, match="api_key must not be empty") as exc_info:
        VllmClient(api_key="")
    assert str(exc_info.value) == "api_key must not be empty"


def test_emit_rejects_blank_prompt() -> None:
    client, _ = _json_client(_ok_body(content=json.dumps({"nodes": []})))
    with pytest.raises(ValueError, match="prompt must not be empty") as exc_info:
        client.emit_dag("")
    assert str(exc_info.value) == "prompt must not be empty"
    with pytest.raises(ValueError, match="prompt must not be empty") as exc_info:
        client.emit_dag("   ")
    assert str(exc_info.value) == "prompt must not be empty"


def test_emit_rejects_non_model_efforts() -> None:
    client, _ = _json_client(_ok_body(content=json.dumps({"nodes": []})))
    for effort in ("minimal", "high", "max", "bogus", "off"):
        expected = f"reasoning_effort must be one of none, low, medium, xhigh; got {effort!r}"
        with pytest.raises(ValueError, match="must be one of") as exc_info:
            client.emit_dag(PLAN_PROMPT, reasoning_effort=effort)
        assert str(exc_info.value) == expected


def test_emit_accepts_all_model_efforts() -> None:
    for effort in ("none", "low", "medium", "xhigh"):
        client, seen = _json_client(_ok_body(content=json.dumps({"nodes": []})))
        client.emit_dag(PLAN_PROMPT, reasoning_effort=effort)
        assert json.loads(seen[0].content)["reasoning_effort"] == effort


def test_emit_missing_reasoning_defaults_to_empty() -> None:
    client, _ = _json_client(_ok_body(content=json.dumps({"nodes": []}), reasoning=None))
    assert client.emit_dag(PLAN_PROMPT).reasoning == ""


def test_emit_non_string_reasoning_defaults_to_empty() -> None:
    client, _ = _json_client(_ok_body(content=json.dumps({"nodes": []}), reasoning=42))
    assert client.emit_dag(PLAN_PROMPT).reasoning == ""


def test_emit_missing_finish_reason_is_ok() -> None:
    client, _ = _json_client(_ok_body(content=json.dumps({"nodes": []}), finish_reason=None))
    assert client.emit_dag(PLAN_PROMPT).dag == {"nodes": []}


def test_emit_truncated_completion_raises() -> None:
    client, _ = _json_client(_ok_body(content=json.dumps({"nodes": []}), finish_reason="length"))
    with pytest.raises(VllmResponseError) as exc_info:
        client.emit_dag(PLAN_PROMPT)
    assert (
        str(exc_info.value)
        == "completion truncated (finish_reason=length); retry with more max_tokens"
    )


def test_emit_auth_failures_raise_auth_error() -> None:
    for status in (401, 403):
        client, _ = _client_for(httpx.Response(status, json={"error": "Unauthorized"}))
        with pytest.raises(VllmAuthError) as exc_info:
            client.emit_dag(PLAN_PROMPT)
        assert str(exc_info.value) == f"server rejected the API key (HTTP {status})"


def test_emit_server_errors_raise_with_snippet() -> None:
    for status in (400, 429, 500):
        client, _ = _client_for(httpx.Response(status, text="boom"))
        with pytest.raises(VllmRequestError) as exc_info:
            client.emit_dag(PLAN_PROMPT)
        assert str(exc_info.value) == f"server returned HTTP {status}: boom"


def test_emit_server_error_snippet_is_bounded() -> None:
    client, _ = _client_for(httpx.Response(500, text="x" * 500))
    with pytest.raises(VllmRequestError) as exc_info:
        client.emit_dag(PLAN_PROMPT)
    assert str(exc_info.value) == "server returned HTTP 500: " + "x" * 200


def test_emit_transport_errors_raise_request_error() -> None:
    with pytest.raises(VllmRequestError) as exc_info:
        _failing_client(httpx.ConnectError("refused")).emit_dag(PLAN_PROMPT)
    assert str(exc_info.value) == "request failed: refused"
    with pytest.raises(VllmRequestError) as exc_info:
        _failing_client(httpx.TimeoutException("slow")).emit_dag(PLAN_PROMPT)
    assert str(exc_info.value) == "request failed: slow"


def test_emit_non_json_envelope_raises() -> None:
    client, _ = _client_for(httpx.Response(200, text="<html>not json"))
    with pytest.raises(VllmResponseError) as exc_info:
        client.emit_dag(PLAN_PROMPT)
    assert (
        str(exc_info.value)
        == "response is not valid JSON: Expecting value: line 1 column 1 (char 0)"
    )


def test_emit_malformed_envelopes_raise() -> None:
    cases: list[tuple[Any, str]] = [
        ([1, 2], "expected a JSON object envelope, got list"),
        ({}, "response envelope has no choices"),
        ({"choices": []}, "response envelope has no choices"),
        ({"choices": "nope"}, "response envelope has no choices"),
        ({"choices": [None]}, "first choice is not an object"),
        ({"choices": [{}]}, "first choice has no message object"),
        ({"choices": [{"message": None}]}, "first choice has no message object"),
        ({"choices": [{"message": {}}]}, "message has no text content"),
        ({"choices": [{"message": {"content": None}}]}, "message has no text content"),
        ({"choices": [{"message": {"content": 7}}]}, "message has no text content"),
        ({"choices": [{"message": {"content": ""}}]}, "message has no text content"),
        ({"choices": [{"message": {"content": "   "}}]}, "message has no text content"),
        (
            {"choices": [{"message": {"content": "{oops"}}]},
            "content is not valid JSON: Expecting property name enclosed in double "
            "quotes: line 1 column 2 (char 1)",
        ),
        ({"choices": [{"message": {"content": "[1, 2]"}}]}, "content JSON must be an object"),
    ]
    for payload, expected in cases:
        client, _ = _json_client(payload)
        with pytest.raises(VllmResponseError) as exc_info:
            client.emit_dag(PLAN_PROMPT)
        assert str(exc_info.value) == expected


def test_client_context_manager() -> None:
    client = VllmClient(api_key="test-key")
    with client as entered:
        assert entered is client
    closer = VllmClient(api_key="test-key")
    closer.close()


def test_diff_posts_guided_payload() -> None:
    prompt = "Add a pure add() function with a test."
    client, seen = _json_client(_ok_body(content=json.dumps({"diff": "diff --git x"})))
    assert client.propose_diff(prompt) == DiffProposal(
        diff="diff --git x", reasoning="decomposing the task..."
    )
    assert json.loads(seen[0].content) == {
        "model": DEFAULT_MODEL,
        "messages": [{"role": "user", "content": prompt}],
        "temperature": 0.0,
        "max_tokens": 4096,
        "reasoning_effort": "medium",
        "include_reasoning": True,
        "structured_outputs": {"json": DIFF_SCHEMA},
    }


def test_diff_rejects_bad_input() -> None:
    client, _ = _json_client(_ok_body(content=json.dumps({"diff": "x"})))
    with pytest.raises(ValueError, match="must not be empty") as prompt_info:
        client.propose_diff("  ")
    assert str(prompt_info.value) == "prompt must not be empty"
    with pytest.raises(ValueError, match="must be one of") as effort_info:
        client.propose_diff("Do x.", reasoning_effort="bogus")
    assert str(effort_info.value) == (
        "reasoning_effort must be one of none, low, medium, xhigh; got 'bogus'"
    )


def test_diff_missing_or_blank_content_fails() -> None:
    for content in ('{"other": 1}', '{"diff": "  "}', '{"diff": 5}'):
        client, _ = _json_client(_ok_body(content=content))
        with pytest.raises(VllmResponseError, match="no diff string") as exc_info:
            client.propose_diff("Do x.")
        assert str(exc_info.value) == "content has no diff string"


def test_list_models_returns_served_ids() -> None:
    payload = {"data": [{"id": "a"}, {"id": "b"}], "object": "list"}
    client, seen = _json_client(payload)
    assert client.list_models() == ["a", "b"]
    assert seen[0].method == "GET"
    assert seen[0].url.path == "/v1/models"
    assert seen[0].extensions["timeout"] == {
        "connect": 10.0,
        "read": 10.0,
        "write": 10.0,
        "pool": 10.0,
    }
    empty, _ = _json_client({"data": []})
    assert empty.list_models() == []


def test_list_models_auth_and_server_errors() -> None:
    for status in (401, 403):
        client, _ = _client_for(httpx.Response(status, json={"error": "nope"}))
        with pytest.raises(VllmAuthError) as auth_info:
            client.list_models()
        assert str(auth_info.value) == f"server rejected the API key (HTTP {status})"
    client, _ = _client_for(httpx.Response(503, text="down"))
    with pytest.raises(VllmRequestError) as req_info:
        client.list_models()
    assert str(req_info.value) == "server returned HTTP 503: down"


def test_list_models_transport_and_json_errors() -> None:
    with pytest.raises(VllmRequestError) as req_info:
        _failing_client(httpx.ConnectError("refused")).list_models()
    assert str(req_info.value) == "request failed: refused"
    client, _ = _client_for(httpx.Response(200, text="<html>not json"))
    with pytest.raises(VllmResponseError) as resp_info:
        client.list_models()
    assert str(resp_info.value).startswith("response is not valid JSON: ")


def test_list_models_malformed_envelopes_raise() -> None:
    cases: list[tuple[Any, str]] = [
        ([{"id": "a"}], "models envelope must be an object"),
        ({"data": {"id": "a"}}, "models envelope has no data list"),
        ({"data": ["a"]}, "models entry has no string id"),
        ({"data": [{"id": 5}]}, "models entry has no string id"),
        ({"data": [{"name": "a"}]}, "models entry has no string id"),
    ]
    for payload, message in cases:
        client, _ = _json_client(payload)
        with pytest.raises(VllmResponseError, match=r"models envelope|models entry") as exc:
            client.list_models()
        assert str(exc.value) == message
