"""vLLM guided-emission client (OpenAI-compatible chat completions).

Sends ``structured_outputs`` carrying the DAG schema so the server's XGrammar
backend constrains the completion to schema-valid JSON once freeform reasoning
ends (vLLM 0.28 request API; the pre-0.28 ``guided_json`` field is ignored).
Thinking is requested via first-class ``reasoning_effort``, never template
backdoors, so the effort level stays explicit and server defaults can't
silently change the contract.
"""

from __future__ import annotations

import copy
import json
from dataclasses import dataclass
from types import TracebackType
from typing import Any, Final

import httpx

DEFAULT_BASE_URL: Final = "http://127.0.0.1:18020/v1"
DEFAULT_MODEL: Final = "qwen3.8-27b"
DEFAULT_TIMEOUT: Final = 300.0
DEFAULT_MAX_TOKENS: Final = 4096
DEFAULT_TEMPERATURE: Final = 0.0
DEFAULT_REASONING_EFFORT: Final = "medium"

# Wire schema for guided DAG emission (ARCHITECTURE.md §3 Phase 1 node shape).
# Semantic validation (acyclicity, allowlists, ceilings) belongs to the DAG
# validator; the client only guarantees "parsed JSON object".
DAG_JSON_SCHEMA: Final[dict[str, Any]] = {
    "type": "object",
    "properties": {
        "nodes": {
            "type": "array",
            "minItems": 1,
            "maxItems": 32,
            "items": {
                "type": "object",
                "properties": {
                    "id": {"type": "string", "minLength": 1, "maxLength": 64},
                    "dependencies": {
                        "type": "array",
                        "items": {"type": "string", "minLength": 1},
                    },
                    "task_prompt": {"type": "string", "minLength": 1},
                    "requirement_ids": {
                        "type": "array",
                        "items": {"type": "string", "minLength": 1},
                        "minItems": 1,
                    },
                    "execution_constraints": {
                        "type": "object",
                        "properties": {
                            "reasoning_budget": {
                                "type": "string",
                                "enum": ["zero", "low", "medium", "high", "xhigh"],
                            },
                            "allowed_tools": {
                                "type": "array",
                                "items": {"type": "string", "minLength": 1},
                                "minItems": 1,
                            },
                            "max_context_tokens": {
                                "type": "integer",
                                "minimum": 1000,
                                "maximum": 30000,
                            },
                        },
                        "required": [
                            "reasoning_budget",
                            "allowed_tools",
                            "max_context_tokens",
                        ],
                        "additionalProperties": False,
                    },
                    "deterministic_gate": {
                        "type": "object",
                        "properties": {
                            "test_command": {"type": "string", "minLength": 1},
                            "changed_line_coverage_min": {
                                "type": "number",
                                "minimum": 0,
                                "maximum": 100,
                            },
                            "red_phase_required": {"type": "boolean"},
                            "mutation_sample": {
                                "type": "object",
                                "properties": {
                                    "scope": {"type": "string", "enum": ["changed-lines"]},
                                    "max_mutants": {
                                        "type": "integer",
                                        "minimum": 1,
                                        "maximum": 1000,
                                    },
                                    "kill_threshold": {
                                        "type": "number",
                                        "minimum": 0,
                                        "maximum": 100,
                                    },
                                },
                                "required": ["scope", "max_mutants", "kill_threshold"],
                                "additionalProperties": False,
                            },
                        },
                        "required": [
                            "test_command",
                            "changed_line_coverage_min",
                            "red_phase_required",
                            "mutation_sample",
                        ],
                        "additionalProperties": False,
                    },
                },
                "required": [
                    "id",
                    "dependencies",
                    "task_prompt",
                    "requirement_ids",
                    "execution_constraints",
                    "deterministic_gate",
                ],
                "additionalProperties": False,
            },
        }
    },
    "required": ["nodes"],
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


def dag_schema() -> dict[str, Any]:
    """Return an independent copy of the guided DAG schema."""
    return copy.deepcopy(DAG_JSON_SCHEMA)


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
        "structured_outputs": {"json": dag_schema()},
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

    def emit_dag(
        self,
        prompt: str,
        *,
        max_tokens: int = DEFAULT_MAX_TOKENS,
        temperature: float = DEFAULT_TEMPERATURE,
        reasoning_effort: str = DEFAULT_REASONING_EFFORT,
    ) -> DagEmission:
        """Emit one schema-constrained DAG plan for *prompt*.

        ``reasoning_effort`` is passed through to the server (which validates
        it); only ``"none"`` is rejected here because it would disable the
        reasoning this client's contract requires.
        """
        if not prompt.strip():
            msg = "prompt must not be empty"
            raise ValueError(msg)
        if reasoning_effort == "none":
            msg = 'reasoning_effort "none" disables thinking; guided DAG emission requires it'
            raise ValueError(msg)
        payload = _build_payload(
            model=self._model,
            prompt=prompt,
            max_tokens=max_tokens,
            temperature=temperature,
            reasoning_effort=reasoning_effort,
        )
        try:
            response = self._client.post("/chat/completions", json=payload)
        except httpx.HTTPError as exc:
            msg = f"request failed: {exc}"
            raise VllmRequestError(msg) from exc
        if response.status_code in (401, 403):
            msg = f"server rejected the API key (HTTP {response.status_code})"
            raise VllmAuthError(msg)
        if response.status_code >= 400:
            msg = f"server returned HTTP {response.status_code}: {response.text[:200]}"
            raise VllmRequestError(msg)
        try:
            data = response.json()
        except ValueError as exc:
            msg = f"response is not valid JSON: {exc}"
            raise VllmResponseError(msg) from exc
        return _parse_response(data)

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
