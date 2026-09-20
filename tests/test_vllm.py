"""Tests for saddle.vllm: guided emission over a mocked httpx transport."""

from __future__ import annotations

import json
import re
from typing import Any

import httpx
import pytest

from saddle.dag import dag_json_schema
from saddle.vllm import (
    DEFAULT_MODEL,
    DEFAULT_TIMEOUT,
    DIFF_GRAMMAR,
    DiffProposal,
    StreamToken,
    ToolCall,
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
    assert client._client.timeout.read == DEFAULT_TIMEOUT
    assert client._client.timeout.connect == DEFAULT_TIMEOUT
    # Must outlast the largest worker output budget; 300s silently capped
    # every long generation regardless of max_tokens.
    assert DEFAULT_TIMEOUT >= 1800.0


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
    """The diff is raw grammar-constrained text, not a JSON-wrapped string."""
    prompt = "Add a pure add() function with a test."
    client, seen = _json_client(_ok_body(content=REAL_DIFF))
    assert client.propose_diff(prompt) == DiffProposal(
        diff=REAL_DIFF, reasoning="decomposing the task..."
    )
    assert json.loads(seen[0].content) == {
        "model": DEFAULT_MODEL,
        "messages": [{"role": "user", "content": prompt}],
        "temperature": 0.0,
        "max_tokens": 4096,
        "reasoning_effort": "medium",
        "include_reasoning": True,
        "structured_outputs": {"grammar": DIFF_GRAMMAR},
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
    """Blank content is refused upstream; the diff parser adds no second check."""
    for content in ("", "   ", "\n"):
        client, _ = _json_client(_ok_body(content=content))
        with pytest.raises(VllmResponseError, match="no text content") as exc_info:
            client.propose_diff("Do x.")
        assert str(exc_info.value) == "message has no text content"


def test_complete_posts_unguided_payload_and_returns_prose() -> None:
    prompt = "Diagnose this failure and plan the fix."
    client, seen = _json_client(_ok_body(content="1. Fix the import.\n2. Re-run."))
    assert client.complete(prompt) == "1. Fix the import.\n2. Re-run."
    assert json.loads(seen[0].content) == {
        "model": DEFAULT_MODEL,
        "messages": [{"role": "user", "content": prompt}],
        "temperature": 0.0,
        "max_tokens": 4096,
        "reasoning_effort": "medium",
        "include_reasoning": True,
    }


def test_complete_honors_sampling_overrides() -> None:
    client, seen = _json_client(_ok_body(content="a plan"))
    client.complete("Do x.", max_tokens=128, temperature=0.5, reasoning_effort="xhigh")
    body = json.loads(seen[0].content)
    assert body["max_tokens"] == 128
    assert body["temperature"] == 0.5
    assert body["reasoning_effort"] == "xhigh"


def test_complete_rejects_bad_input() -> None:
    client, _ = _json_client(_ok_body(content="a plan"))
    with pytest.raises(ValueError, match="must not be empty") as prompt_info:
        client.complete("  ")
    assert str(prompt_info.value) == "prompt must not be empty"
    with pytest.raises(ValueError, match="must be one of") as effort_info:
        client.complete("Do x.", reasoning_effort="bogus")
    assert str(effort_info.value) == (
        "reasoning_effort must be one of none, low, medium, xhigh; got 'bogus'"
    )


def test_complete_blank_content_and_truncation_raise() -> None:
    blank, _ = _json_client(_ok_body(content="  "))
    with pytest.raises(VllmResponseError, match="no text content") as exc_info:
        blank.complete("Do x.")
    assert str(exc_info.value) == "message has no text content"
    cut, _ = _json_client(_ok_body(content="half", finish_reason="length"))
    with pytest.raises(VllmResponseError, match="truncated") as cut_info:
        cut.complete("Do x.")
    assert str(cut_info.value) == (
        "completion truncated (finish_reason=length); retry with more max_tokens"
    )


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


def _chunk(delta: dict[str, Any]) -> str:
    return "data: " + json.dumps({"choices": [{"delta": delta}]}) + "\n\n"


def _sse_client(body: str, *, status: int = 200) -> tuple[VllmClient, list[httpx.Request]]:
    response = httpx.Response(status, text=body, headers={"Content-Type": "text/event-stream"})
    return _client_for(response)


def test_stream_chat_yields_reasoning_and_content_in_order() -> None:
    body = (
        _chunk({"role": "assistant", "content": ""})
        + _chunk({"reasoning": "Let me "})
        + _chunk({"reasoning": "think."})
        + _chunk({"content": "Hi!"})
        + _chunk({"reasoning": "r", "content": "c"})
        + _chunk({"reasoning": None})
        + "data: [DONE]\n\n"
    )
    client, seen = _sse_client(body)
    messages = [{"role": "user", "content": "hi"}]
    assert list(client.stream_chat(messages)) == [
        StreamToken(stream="reasoning", text="Let me "),
        StreamToken(stream="reasoning", text="think."),
        StreamToken(stream="content", text="Hi!"),
        StreamToken(stream="reasoning", text="r"),
        StreamToken(stream="content", text="c"),
    ]
    assert len(seen) == 1
    assert seen[0].method == "POST"
    assert seen[0].url.path == "/v1/chat/completions"
    assert json.loads(seen[0].content) == {
        "model": DEFAULT_MODEL,
        "messages": messages,
        "temperature": 0.0,
        "max_tokens": 4096,
        "reasoning_effort": "medium",
        "include_reasoning": True,
        "stream": True,
    }


def test_stream_chat_rejects_empty_messages_and_bad_effort() -> None:
    client, _ = _sse_client("data: [DONE]\n\n")
    with pytest.raises(ValueError, match="messages must not be empty") as empty_info:
        list(client.stream_chat([]))
    assert str(empty_info.value) == "messages must not be empty"
    with pytest.raises(ValueError, match="reasoning_effort must be one of") as effort_info:
        list(client.stream_chat([{"role": "user", "content": "hi"}], reasoning_effort="bogus"))
    assert str(effort_info.value) == (
        "reasoning_effort must be one of none, low, medium, xhigh; got 'bogus'"
    )


def test_stream_chat_maps_auth_and_request_errors() -> None:
    for status in (401, 403):
        client, _ = _sse_client("denied", status=status)
        with pytest.raises(VllmAuthError) as auth_info:
            list(client.stream_chat([{"role": "user", "content": "hi"}]))
        assert str(auth_info.value) == f"server rejected the API key (HTTP {status})"
    client, _ = _sse_client("boom", status=500)
    with pytest.raises(VllmRequestError) as req_info:
        list(client.stream_chat([{"role": "user", "content": "hi"}]))
    assert str(req_info.value) == "server returned HTTP 500: boom"
    client, _ = _sse_client("bad", status=400)
    with pytest.raises(VllmRequestError) as bad_info:
        list(client.stream_chat([{"role": "user", "content": "hi"}]))
    assert str(bad_info.value) == "server returned HTTP 400: bad"
    client, _ = _sse_client("x" * 250, status=500)
    with pytest.raises(VllmRequestError) as long_info:
        list(client.stream_chat([{"role": "user", "content": "hi"}]))
    assert str(long_info.value) == f"server returned HTTP 500: {'x' * 200}"
    failing = _failing_client(httpx.ConnectError("refused"))
    with pytest.raises(VllmRequestError) as conn_info:
        list(failing.stream_chat([{"role": "user", "content": "hi"}]))
    assert str(conn_info.value) == "request failed: refused"


def test_stream_chat_malformed_chunks_raise() -> None:
    client, _ = _sse_client("data: {oops\n\n")
    with pytest.raises(VllmResponseError) as json_info:
        list(client.stream_chat([{"role": "user", "content": "hi"}]))
    assert str(json_info.value).startswith("stream chunk is not valid JSON: ")
    cases = [
        ("data: " + json.dumps({"choices": []}) + "\n\n", "stream chunk has no choices"),
        (
            "data: " + json.dumps({"choices": [{"delta": None}]}) + "\n\n",
            "stream chunk has no delta",
        ),
        ("data: [1, 2]\n\n", "stream chunk must be an object, got list"),
        (
            "data: " + json.dumps({"choices": [7]}) + "\n\n",
            "stream chunk has no delta",
        ),
    ]
    for body, message in cases:
        client, _ = _sse_client(body)
        with pytest.raises(VllmResponseError) as exc_info:
            list(client.stream_chat([{"role": "user", "content": "hi"}]))
        assert str(exc_info.value) == message


def test_stream_chat_truncated_stream_yields_partial_tokens() -> None:
    client, _ = _sse_client(_chunk({"content": "Hi!"}))
    assert list(client.stream_chat([{"role": "user", "content": "hi"}])) == [
        StreamToken(stream="content", text="Hi!")
    ]


def test_stream_chat_sends_tools_only_when_provided() -> None:
    tool = {
        "type": "function",
        "function": {
            "name": "add",
            "description": "add ints",
            "parameters": {"type": "object", "properties": {}},
        },
    }
    client, seen = _sse_client("data: [DONE]\n\n")
    list(client.stream_chat([{"role": "user", "content": "hi"}], tools=[tool]))
    body = json.loads(seen[0].content)
    assert body["tools"] == [tool]
    assert body["tool_choice"] == "auto"
    empties: list[list[dict[str, Any]] | None] = [None, []]
    for omitted in empties:
        client, seen = _sse_client("data: [DONE]\n\n")
        list(client.stream_chat([{"role": "user", "content": "hi"}], tools=omitted))
        body = json.loads(seen[0].content)
        assert "tools" not in body
        assert "tool_choice" not in body


def test_stream_chat_accumulates_tool_calls_after_tokens() -> None:
    body = (
        _chunk({"reasoning": "Let me add. "})
        + _chunk({"tool_calls": [{"id": "call-1", "index": 0, "function": {"name": "add"}}]})
        + _chunk({"tool_calls": [{"index": 0, "function": {"arguments": '{"a": 2, '}}]})
        + _chunk({"tool_calls": [{"index": 0, "function": {"arguments": '"b": 2}'}}]})
        + _chunk({"content": "Adding. "})
        + "data: [DONE]\n\n"
    )
    client, _ = _sse_client(body)
    assert list(client.stream_chat([{"role": "user", "content": "hi"}])) == [
        StreamToken(stream="reasoning", text="Let me add. "),
        StreamToken(stream="content", text="Adding. "),
        ToolCall(id="call-1", name="add", arguments='{"a": 2, "b": 2}'),
    ]


def test_stream_chat_orders_tool_calls_by_index() -> None:
    body = (
        _chunk({"tool_calls": [{"id": "call-b", "index": 1, "function": {"name": "b"}}]})
        + _chunk({"tool_calls": [{"id": "call-a", "index": 0, "function": {"name": "a"}}]})
        + _chunk({"tool_calls": [{"index": 2, "function": {"name": "c"}}]})
        + _chunk({"tool_calls": [{"index": 2, "id": 7, "function": {"name": None}}]})
        + _chunk({"tool_calls": [{"index": 2, "function": {"arguments": 7}}]})
        + _chunk({"tool_calls": [{"index": 3, "function": {"arguments": "{}"}}]})
        + "data: [DONE]\n\n"
    )
    client, _ = _sse_client(body)
    assert list(client.stream_chat([{"role": "user", "content": "hi"}])) == [
        ToolCall(id="call-a", name="a", arguments=""),
        ToolCall(id="call-b", name="b", arguments=""),
        ToolCall(id="", name="c", arguments=""),
        ToolCall(id="", name="", arguments="{}"),
    ]


def test_stream_chat_malformed_tool_calls_raise() -> None:
    cases: list[tuple[dict[str, Any], str]] = [
        ({"tool_calls": {}}, "stream chunk tool_calls must be a list"),
        ({"tool_calls": [7]}, "stream chunk tool call must be an object"),
        (
            {"tool_calls": [{"function": {}}]},
            "stream chunk tool call needs an integer index",
        ),
        (
            {"tool_calls": [{"index": "0", "function": {}}]},
            "stream chunk tool call needs an integer index",
        ),
        ({"tool_calls": [{"index": 0}]}, "stream chunk tool call needs a function object"),
        (
            {"tool_calls": [{"index": 0, "function": []}]},
            "stream chunk tool call needs a function object",
        ),
    ]
    for delta, message in cases:
        client, _ = _sse_client(_chunk(delta))
        with pytest.raises(VllmResponseError) as exc_info:
            list(client.stream_chat([{"role": "user", "content": "hi"}]))
        assert str(exc_info.value) == message


def test_stream_chat_ignores_lines_after_done() -> None:
    body = _chunk({"content": "Hi!"}) + "data: [DONE]\n\n" + _chunk({"content": "Late!"})
    client, _ = _sse_client(body)
    assert list(client.stream_chat([{"role": "user", "content": "hi"}])) == [
        StreamToken(stream="content", text="Hi!")
    ]


def test_stream_chat_midstream_error_object_raises_request_error() -> None:
    error_line = "data: " + json.dumps(
        {
            "error": {
                "message": "Internal server error",
                "type": "InternalServerError",
                "param": None,
                "code": 500,
            }
        }
    )
    body = _chunk({"content": "Hi! "}) + error_line + "\n\n"
    client, _ = _sse_client(body)
    events = client.stream_chat([{"role": "user", "content": "hi"}])
    assert next(events) == StreamToken(stream="content", text="Hi! ")
    with pytest.raises(VllmRequestError) as exc_info:
        list(events)
    assert str(exc_info.value) == "server error during stream: Internal server error"


def test_stream_chat_unusable_error_objects_fall_through() -> None:
    for error in ("boom", 7, {}, {"message": 7}, {"message": ""}):
        body = "data: " + json.dumps({"error": error}) + "\n\n"
        client, _ = _sse_client(body)
        with pytest.raises(VllmResponseError) as exc_info:
            list(client.stream_chat([{"role": "user", "content": "hi"}]))
        assert str(exc_info.value) == "stream chunk has no choices"


REAL_DIFF = (
    "diff --git a/validators.py b/validators.py\n"
    "--- a/validators.py\n"
    "+++ b/validators.py\n"
    "@@ -1,2 +1,3 @@\n"
    "-def is_valid_email(addr):\n"
    "-    raise NotImplementedError\n"
    '+SEP = "@"\n'
    "+def is_valid_email(addr):\n"
    "+    return isinstance(addr, str) and addr.count(SEP) == 1\n"
)


def _decoder_admits(schema: dict[str, Any], instance: str) -> bool:
    """Validate the way the decoding backend does: `pattern` is a FULL match.

    JSON Schema defines `pattern` as an unanchored partial match, but
    xgrammar compiles it into a grammar that must match the whole string.
    `re.fullmatch` is that semantics, so this is the check that matters
    for anything saddle sends as `structured_outputs`.
    """
    pattern = schema["properties"]["diff"].get("pattern")
    return pattern is None or re.fullmatch(pattern, instance) is not None


def test_diff_schema_admits_a_real_diff() -> None:
    """Whatever constrains the diff field must still admit a working diff.

    The v3 T1 arm failed every attempt with `git apply: No valid patches
    in input`. The cause was `pattern = "^diff --git "`, which xgrammar
    compiled to a closed literal:

        root_prop_0 ::= (("\\"" "d" "i" "f" "f" " " ... "t" " " "\\""))

    a grammar for exactly one 11-character string. The token mask forced
    the string closed after the header, so no diff was representable --
    identical output at temperature 0.0 and 0.8, hence identical retries
    and replans.

    The previous test here asserted the pattern *existed* and called the
    class leak-free (F9). Existence was never the question: a constraint
    is only verified once a known-good instance is shown to satisfy it.
    """
    assert _decoder_admits({"properties": {"diff": {"type": "string"}}}, REAL_DIFF)
    # The diff no longer travels inside a JSON string at all: DIFF_GRAMMAR
    # constrains the output language itself, so a hunk is mandatory and
    # prose is unrepresentable. What that grammar admits and rejects is
    # decided by xgrammar, not here: tools/diff_grammar_check.py runs the
    # known-good/known-bad corpus against the serving container's copy.
    # The structural pin below says only which shape was sent there.


def _grammar_rules(grammar: str) -> dict[str, str]:
    """Split an EBNF text into {rule name: body}, joining continuation lines."""
    rules: dict[str, str] = {}
    name = ""
    for raw in grammar.splitlines():
        if "::=" in raw:
            head, _, body = raw.partition("::=")
            name = head.strip()
            rules[name] = body.strip()
        elif raw.strip() and name:
            rules[name] += " " + raw.strip()
    return rules


def test_diff_grammar_requires_the_file_lines_before_a_hunk() -> None:
    """A hunk may not follow `diff --git` (or `index`, or a mode line) directly.

    That shape is what `git apply` refuses with `patch fragment without
    header at line 3`, and it was legal here: `"--- "` and `"+++ "` sat in
    `meta_pfx`, which `section` takes zero or more of. `section` now
    requires `from to` between the metadata and the hunks.

    This is a structural pin, not an acceptance test, and it is deliberately
    the weaker half: xgrammar is the engine that enforces the grammar and it
    is not installed in this venv (it lives in the serving container), so
    nothing here can show a diff being admitted or refused. The corpus that
    can is tools/diff_grammar_check.py; the last assertion keeps the
    known-bad instance in its reject list, and pins where it has to die --
    at the `@@` itself, since a later rejection would mean a different rule
    had swallowed the header.
    """
    rules = _grammar_rules(DIFF_GRAMMAR)
    assert rules["section"].split() == ["header", "meta*", "from", "to", "hunk+"]
    assert rules["from"] == r'"--- " line "\n"'
    assert rules["to"] == r'"+++ " line "\n"'
    # ... and they are no longer reachable as optional metadata instead.
    assert '"--- "' not in rules["meta_pfx"]
    assert '"+++ "' not in rules["meta_pfx"]

    # Imported in-function: tools/ reaches the mutmut work copy only through
    # also_copy (pyproject), and test_mutmut_layout runs this test there to
    # prove it; a module-level import would fail at collection instead.
    from tools.diff_grammar_check import MUST_REJECT, REJECT_AT

    headerless = "diff --git a/x b/x\n@@ -1 +1 @@\n-a\n+b\n"
    named = [name for name, case in MUST_REJECT.items() if case == headerless]
    assert named, "the check tool no longer carries the headerless diff"
    assert REJECT_AT[named[0]] == headerless.index("@@")


def test_grammar_check_corpus_carries_the_grammar_it_checks() -> None:
    """`--emit` writes the grammar into the cases file; `--run` reads it there.

    The container has xgrammar and nothing else: `/tmp/check.py` has no
    `src/` beside it, so importing `saddle.vllm` there raised
    `ModuleNotFoundError` the first time the tool's own docstring lines
    were actually run (T3-18, 2026-09-19). The corpus is the only thing
    that crosses into the container, so the grammar travels inside it,
    byte-identical to this checkout's.
    """
    from tools.diff_grammar_check import build_cases

    cases = build_cases(commits=1)
    assert cases["grammar"] == DIFF_GRAMMAR
    # Every half the container checks travels too.
    assert {"admit", "reject", "reject_at", "not_stop", "stop"} <= cases.keys()
    assert "hunk without file lines" in cases["reject"]


def test_decoder_semantics_reject_a_prefix_only_pattern() -> None:
    """The guard above discriminates: it fails on the exact shipped bug.

    A whole-value pattern (`^REQ-\\d{3}$`) survives full-match compilation;
    a prefix pattern does not. That is the rule any future `pattern` on a
    free-form field has to clear before it can be shipped.
    """
    legacy = {"properties": {"diff": {"type": "string", "pattern": "^diff --git "}}}
    assert not _decoder_admits(legacy, REAL_DIFF)
    assert re.fullmatch(r"^REQ-\d{3}$", "REQ-001") is not None


def test_parse_diff_response_passes_prose_through_to_the_apply_backstop() -> None:
    """Prose must stay retryable, not abort the run.

    The header backstop lives in _apply_diff, not here: a fatal parse
    error would throw away a run that a fresh attempt could fix, which is
    the defect #52 is about. The parser only rejects a missing or blank
    field.
    """
    client, _ = _json_client(_ok_body(content="Sure! I will fix that."))
    assert client.propose_diff("Do x.").diff == "Sure! I will fix that."
