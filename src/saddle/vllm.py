"""vLLM guided-emission client (OpenAI-compatible chat completions).

Sends ``structured_outputs`` carrying the DAG schema (owned by ``saddle.dag``,
derived from the Pydantic models) so the server's XGrammar backend constrains
the completion to schema-valid JSON once freeform reasoning ends (vLLM 0.28
request API; the pre-0.28 ``guided_json`` field is ignored). Thinking is
requested via first-class ``reasoning_effort`` (none/low/medium/xhigh — the
model's template rejects anything else), never template backdoors, so the
effort level stays explicit and server defaults can't silently change
the contract.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from types import TracebackType
from typing import Any, Final

import httpx

from saddle.dag import dag_json_schema

DEFAULT_BASE_URL: Final = "http://127.0.0.1:18020/v1"
DEFAULT_MODEL: Final = "qwen3.8-27b"
DEFAULT_TIMEOUT: Final = 300.0
PREFLIGHT_TIMEOUT: Final = 10.0
DEFAULT_MAX_TOKENS: Final = 4096
DEFAULT_TEMPERATURE: Final = 0.0
DEFAULT_REASONING_EFFORT: Final = "medium"
REASONING_EFFORTS: Final[tuple[str, ...]] = ("none", "low", "medium", "xhigh")
DIFF_SCHEMA: Final[dict[str, Any]] = {
    "type": "object",
    "properties": {"diff": {"type": "string"}},
    "required": ["diff"],
    "additionalProperties": False,
}


class VllmError(Exception):
    """Base error for guided-emission failures."""


class VllmAuthError(VllmError):
    """Server rejected the API key (HTTP 401/403)."""


class VllmRequestError(VllmError):
    """Transport failure or non-auth error status from the server."""


class VllmResponseError(VllmError):
    """Server replied 200 with a malformed or non-JSON envelope."""


@dataclass(frozen=True)
class DagEmission:
    """One guided completion: parsed DAG object plus its reasoning trace."""

    dag: dict[str, Any]
    reasoning: str
    raw_content: str


@dataclass(frozen=True)
class DiffProposal:
    """One guided diff plus the worker reasoning that produced it."""

    diff: str
    reasoning: str


def _build_payload(
    *, model: str, prompt: str, max_tokens: int, temperature: float, reasoning_effort: str
) -> dict[str, Any]:
    return {
        "model": model,
        "messages": [{"role": "user", "content": prompt}],
        "temperature": temperature,
        "max_tokens": max_tokens,
        "reasoning_effort": reasoning_effort,
        "include_reasoning": True,
        "structured_outputs": {"json": dag_json_schema()},
    }


def _build_diff_payload(
    *, model: str, prompt: str, max_tokens: int, temperature: float, reasoning_effort: str
) -> dict[str, Any]:
    return {
        "model": model,
        "messages": [{"role": "user", "content": prompt}],
        "temperature": temperature,
        "max_tokens": max_tokens,
        "reasoning_effort": reasoning_effort,
        "include_reasoning": True,
        "structured_outputs": {"json": DIFF_SCHEMA},
    }


def _parse_response(data: object) -> DagEmission:
    if not isinstance(data, dict):
        msg = f"expected a JSON object envelope, got {type(data).__name__}"
        raise VllmResponseError(msg)
    choices = data.get("choices")
    if not isinstance(choices, list) or not choices:
        msg = "response envelope has no choices"
        raise VllmResponseError(msg)
    first = choices[0]
    if not isinstance(first, dict):
        msg = "first choice is not an object"
        raise VllmResponseError(msg)
    if first.get("finish_reason") == "length":
        msg = "completion truncated (finish_reason=length); retry with more max_tokens"
        raise VllmResponseError(msg)
    message = first.get("message")
    if not isinstance(message, dict):
        msg = "first choice has no message object"
        raise VllmResponseError(msg)
    content = message.get("content")
    if not isinstance(content, str) or not content.strip():
        msg = "message has no text content"
        raise VllmResponseError(msg)
    try:
        dag = json.loads(content)
    except json.JSONDecodeError as exc:
        msg = f"content is not valid JSON: {exc}"
        raise VllmResponseError(msg) from exc
    if not isinstance(dag, dict):
        msg = "content JSON must be an object"
        raise VllmResponseError(msg)
    # vLLM 0.28 surfaces the think block as `reasoning` (not `reasoning_content`).
    raw_reasoning = message.get("reasoning")
    reasoning = raw_reasoning if isinstance(raw_reasoning, str) else ""
    return DagEmission(dag=dag, reasoning=reasoning, raw_content=content)


def _parse_diff_response(data: object) -> DiffProposal:
    emission = _parse_response(data)
    diff = emission.dag.get("diff")
    if not isinstance(diff, str) or not diff.strip():
        msg = "content has no diff string"
        raise VllmResponseError(msg)
    return DiffProposal(diff=diff, reasoning=emission.reasoning)


def _model_ids(data: object) -> list[str]:
    """Served ids from a /models envelope; malformed envelopes raise."""
    if not isinstance(data, dict):
        msg = "models envelope must be an object"
        raise VllmResponseError(msg)
    items = data.get("data")
    if not isinstance(items, list):
        msg = "models envelope has no data list"
        raise VllmResponseError(msg)
    ids: list[str] = []
    for item in items:
        if not isinstance(item, dict) or not isinstance(item.get("id"), str):
            msg = "models entry has no string id"
            raise VllmResponseError(msg)
        ids.append(item["id"])
    return ids


def _checked_json(response: httpx.Response) -> Any:
    """Map error statuses to errors; parse the JSON body otherwise."""
    if response.status_code in (401, 403):
        msg = f"server rejected the API key (HTTP {response.status_code})"
        raise VllmAuthError(msg)
    if response.status_code >= 400:
        msg = f"server returned HTTP {response.status_code}: {response.text[:200]}"
        raise VllmRequestError(msg)
    try:
        return response.json()
    except ValueError as exc:
        msg = f"response is not valid JSON: {exc}"
        raise VllmResponseError(msg) from exc


class VllmClient:
    """Sync httpx client for guided DAG emission."""

    def __init__(
        self,
        *,
        api_key: str,
        base_url: str = DEFAULT_BASE_URL,
        model: str = DEFAULT_MODEL,
        timeout: float = DEFAULT_TIMEOUT,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        if not api_key:
            msg = "api_key must not be empty"
            raise ValueError(msg)
        self._model = model
        self._client = httpx.Client(
            base_url=base_url,
            headers={"Authorization": f"Bearer {api_key}"},
            timeout=timeout,
            transport=transport,
        )

    def _post(self, payload: dict[str, Any]) -> Any:
        """POST one chat payload; map transport and status failures to errors."""
        try:
            response = self._client.post("/chat/completions", json=payload)
        except httpx.HTTPError as exc:
            msg = f"request failed: {exc}"
            raise VllmRequestError(msg) from exc
        return _checked_json(response)

    def emit_dag(
        self,
        prompt: str,
        *,
        max_tokens: int = DEFAULT_MAX_TOKENS,
        temperature: float = DEFAULT_TEMPERATURE,
        reasoning_effort: str = DEFAULT_REASONING_EFFORT,
    ) -> DagEmission:
        """Emit one schema-constrained DAG plan for *prompt*.

        ``reasoning_effort`` must be one of none/low/medium/xhigh: the
        model's chat template renders anything else into an HTTP 400, so
        anything else is rejected here instead of wasting a round-trip.
        """
        if not prompt.strip():
            msg = "prompt must not be empty"
            raise ValueError(msg)
        if reasoning_effort not in REASONING_EFFORTS:
            allowed = ", ".join(REASONING_EFFORTS)
            msg = f"reasoning_effort must be one of {allowed}; got {reasoning_effort!r}"
            raise ValueError(msg)
        payload = _build_payload(
            model=self._model,
            prompt=prompt,
            max_tokens=max_tokens,
            temperature=temperature,
            reasoning_effort=reasoning_effort,
        )
        data = self._post(payload)
        return _parse_response(data)

    def propose_diff(
        self,
        prompt: str,
        *,
        max_tokens: int = DEFAULT_MAX_TOKENS,
        temperature: float = DEFAULT_TEMPERATURE,
        reasoning_effort: str = DEFAULT_REASONING_EFFORT,
    ) -> DiffProposal:
        """Propose a unified diff for *prompt*, guided to one JSON string field."""
        if not prompt.strip():
            msg = "prompt must not be empty"
            raise ValueError(msg)
        if reasoning_effort not in REASONING_EFFORTS:
            allowed = ", ".join(REASONING_EFFORTS)
            msg = f"reasoning_effort must be one of {allowed}; got {reasoning_effort!r}"
            raise ValueError(msg)
        payload = _build_diff_payload(
            model=self._model,
            prompt=prompt,
            max_tokens=max_tokens,
            temperature=temperature,
            reasoning_effort=reasoning_effort,
        )
        return _parse_diff_response(self._post(payload))

    def list_models(self) -> list[str]:
        """GET /models with a short timeout; return served model ids."""
        try:
            response = self._client.get("/models", timeout=PREFLIGHT_TIMEOUT)
        except httpx.HTTPError as exc:
            msg = f"request failed: {exc}"
            raise VllmRequestError(msg) from exc
        return _model_ids(_checked_json(response))

    def close(self) -> None:
        self._client.close()

    def __enter__(self) -> VllmClient:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        self.close()
