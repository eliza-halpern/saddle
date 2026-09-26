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
from collections.abc import Iterator, Mapping, Sequence
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from time import perf_counter
from types import TracebackType
from typing import Any, Final

import httpx

from saddle.dag import dag_json_schema

DEFAULT_BASE_URL: Final = "http://127.0.0.1:18020/v1"
DEFAULT_MODEL: Final = "qwen3.8-27b"
# Non-streaming, so this is the whole-generation deadline. At 300s a 27B
# server silently capped completions around 6-12K tokens regardless of
# max_tokens, which read as a hang; it must exceed the slowest budget.
DEFAULT_TIMEOUT: Final = 1800.0
PREFLIGHT_TIMEOUT: Final = 10.0
DEFAULT_MAX_TOKENS: Final = 4096
DEFAULT_TEMPERATURE: Final = 0.0
DEFAULT_REASONING_EFFORT: Final = "medium"
REASONING_EFFORTS: Final[tuple[str, ...]] = ("none", "low", "medium", "xhigh")
# Constrain the output *language*, not a JSON-escaped copy of it. A regex
# over a diff inside a JSON string cannot work here in either direction:
# every pattern correct under the decoder's full-match semantics let the
# model emit a raw `"` and break its own packet, and every pattern that
# kept the packet intact excluded code containing a quote. A unified diff
# has a real grammar, so state that instead -- which is also what
# Agentless does by emitting plain fenced edits rather than JSON
# (arXiv:2407.01489), and what grammar-constrained decoding means
# (arXiv:2305.13971).
#
# Validated against the serving container's xgrammar before shipping, and
# `tools/diff_grammar_check.py` re-runs it: 44/44 real diffs from this
# repo's own history admitted, including rename, mode-change, delete and
# `\ No newline at end of file`; prose, markdown fences, a JSON wrapper
# and a `diff -u` header all rejected at the first offending byte. Both
# halves are required -- the pattern this replaces was only ever checked
# for existence, and it admitted exactly one 11-character string.
#
# ANY EDIT HERE MUST RE-RUN THAT CHECK. A construct left out of this
# grammar is a diff the worker cannot express, which is precisely the
# failure it exists to prevent.
#
# `--- ` and `+++ ` are required (`from to`), not optional `meta`: a hunk
# that follows the `diff --git` line directly is the shape behind every
# one of the smoke run's 16 `patch fragment without header at line 3`
# apply failures (WORKPLAN T3-18, smoke record S2). Hunk line counts stay
# unenforceable -- a CFG cannot count -- so this closes the header half of
# that class, not the count half. Re-run in the container for this
# tightening on 2026-09-19: 53/53 (44 admit, 6 reject with the headerless
# section refused at its `@@`, 2 must-not-stop, 1 must-stop).
# A section is a WRITE or a DELETE (T6-62/A1). A write carries the
# complete new contents of one file and nothing else: there is no
# original side to reproduce, no context to match, and the only hunk
# header the grammar admits is the one anchored at line 1. That is the
# whole point of the envelope. F21.38 measured what the diff envelope
# cost: the model writes its *intended output* onto the context lines --
# its reproduction of `accounts.py` ran 105 lines against the file's 73,
# similarity 0.652, every divergence an edit it meant to make -- and no
# git flag reaches that, because the context is not a transcription
# error. It was already emitting whole files and spelling them as diffs
# it could not get right (every failing diff in rounds 3h and 3i is one
# hunk per file anchored at line 1). So ask for what it is already
# writing. `@@ -0,0 +1` is a literal, not a count: "anchored at line 1"
# is unrepresentable otherwise, by construction rather than by check.
#
# The syntax is still git's, deliberately. The model is fluent in it, the
# grammar delta stays small, and `slice.whole_file_reconstruction` already
# parses exactly this shape. Nothing is applied: the new side IS the file.
#
# SCOPE NARROWED, not loosened: `new file mode` is no longer required on a
# write. It was required because `--- /dev/null` with no mode line let
# `git apply` read `/dev/null` as a path (round 3d probe, F21.14:
# `error: dev/null: No such file or directory`). `git apply` no longer
# sees worker output at all, so the hazard that rule existed for cannot
# occur; and under whole-file semantics every write is create-or-
# overwrite, which makes a "new file" marker on an overwrite a lie. A
# deletion still carries `deleted file mode`, which distinguishes the two
# branches on their first byte. `path` never starts with `/`, so
# `/dev/null` remains unrepresentable wherever a real path belongs.
#
# A delete carries no hunk: the content of a file being removed is not
# evidence of anything, and re-emitting it whole is tokens spent on a
# transcription the writer then discards.
DIFF_GRAMMAR: Final = r"""root ::= section+
section    ::= header (write | delete)
write      ::= meta* "--- /dev/null\n" to anchor body
delete     ::= meta* "deleted file mode " line "\n" meta* from "+++ /dev/null\n"
header     ::= "diff --git " line "\n"
anchor     ::= "@@ -0,0 +1" ("," digits)? " @@\n"
body       ::= bline+ noeol?
bline      ::= "+" line "\n"
noeol      ::= "\\ No newline at end of file\n"
meta       ::= meta_pfx line "\n"
meta_pfx   ::= "index " | "old mode " | "new mode " | "new file mode "
             | "similarity index " | "dissimilarity index "
             | "rename from " | "rename to " | "copy from " | "copy to "
from       ::= "--- " path "\n"
to         ::= "+++ " path "\n"
path       ::= [^/\n] [^\n]*
digits     ::= [0-9]+
line       ::= [^\n]*
"""


class VllmError(Exception):
    """Base error for guided-emission failures.

    `evidence` (T6-27) is what the failed call was: prompt, seed,
    temperature, start time and wall, attached by the method that made the
    call so the attempt's sidecar can hold them whatever the failure type.
    Round 3d's 2494 s timeout sealed nothing the client knew.
    """

    evidence: dict[str, Any]


class VllmAuthError(VllmError):
    """Server rejected the API key (HTTP 401/403)."""


class VllmRequestError(VllmError):
    """Transport failure or non-auth error status from the server."""


class VllmResponseError(VllmError):
    """Server replied 200 with a malformed or non-JSON envelope.

    A truncation carries what did arrive (T6-12): the partial reasoning
    and content, the usage the server reported, and the cap the call
    sent, so the attempt's sidecar can hold them instead of the journal
    losing the most expensive failure's only evidence.
    """

    def __init__(
        self,
        message: str,
        *,
        reasoning: str = "",
        content: str = "",
        usage: Mapping[str, int] | None = None,
        max_tokens: int | None = None,
        finish_reason: str = "",
    ) -> None:
        super().__init__(message)
        self.reasoning = reasoning
        self.content = content
        self.usage = dict(usage or {})
        self.max_tokens = max_tokens
        self.finish_reason = finish_reason


@dataclass(frozen=True)
class DagEmission:
    """One guided completion: parsed DAG object plus its reasoning trace."""

    dag: dict[str, Any]
    reasoning: str
    raw_content: str


@dataclass(frozen=True)
class DiffProposal:
    """One guided diff plus the worker reasoning that produced it.

    `usage` and `max_tokens` (T6-12) are the server's token accounting and
    the cap the call was sent; they ride along so the attempt's sidecar
    can record them. Equality on the two text fields is what callers and
    tests compare, so the extras are excluded from it.
    """

    diff: str
    reasoning: str
    usage: dict[str, int] = field(default_factory=dict, compare=False)
    max_tokens: int | None = field(default=None, compare=False)
    # The call that produced it (T6-27): enough to rebuild the request.
    prompt: str = field(default="", compare=False)
    seed: int | None = field(default=None, compare=False)
    temperature: float | None = field(default=None, compare=False)
    started_at: str = field(default="", compare=False)
    wall_s: float | None = field(default=None, compare=False)
    # T6-47. The effort is part of the request, not a detail of it: the
    # served chat template injects a different instruction sentence per
    # effort, so two draws at different efforts are different prompts.
    # Without this field a cell rebuilt from the record has to guess, and
    # F21.18 is what guessing cost -- a `low` attempt replayed at `xhigh`,
    # 12 prompt tokens apart, neither arm reproducing the draw.
    reasoning_effort: str = field(default="", compare=False)


@dataclass(frozen=True)
class StreamToken:
    """One streamed token: which stream it belongs to plus its text."""

    stream: str
    text: str


@dataclass(frozen=True)
class StreamUsage:
    """The server's token accounting for one streamed completion.

    Yielded last, and only when the server sent a usage object -- the
    request asks for one with `stream_options.include_usage`, but a server
    that ignores that option sends none, and the caller must then estimate.
    """

    prompt_tokens: int
    completion_tokens: int
    reasoning_tokens: int | None = None
    """`usage.completion_tokens_details.reasoning_tokens` when the server
    sent it, else None: the caller estimates from the reasoning text."""


@dataclass(frozen=True)
class ToolCall:
    """One complete tool call: id, function name, raw JSON arguments."""

    id: str
    name: str
    arguments: str


class _ToolCallAccumulator:
    """Merges streamed tool_call fragments (keyed by index) into calls."""

    def __init__(self) -> None:
        self._pending: dict[int, dict[str, str]] = {}

    def add(self, delta: Mapping[str, Any]) -> None:
        """Fold one delta's tool_calls fragments into the pending calls."""
        raw = delta.get("tool_calls")
        if raw is None:
            return
        if not isinstance(raw, list):
            msg = "stream chunk tool_calls must be a list"
            raise VllmResponseError(msg)
        for entry in raw:
            self._add_entry(entry)

    def _add_entry(self, entry: Any) -> None:
        """Fold one tool_call fragment into its indexed pending call."""
        if not isinstance(entry, dict):
            msg = "stream chunk tool call must be an object"
            raise VllmResponseError(msg)
        index = entry.get("index")
        if not isinstance(index, int):
            msg = "stream chunk tool call needs an integer index"
            raise VllmResponseError(msg)
        function = entry.get("function")
        if not isinstance(function, dict):
            msg = "stream chunk tool call needs a function object"
            raise VllmResponseError(msg)
        pending = self._pending.setdefault(index, {"id": "", "name": "", "arguments": ""})
        call_id = entry.get("id")
        if isinstance(call_id, str):
            pending["id"] = call_id
        name = function.get("name")
        if isinstance(name, str):
            pending["name"] = name
        arguments = function.get("arguments")
        if isinstance(arguments, str):
            pending["arguments"] += arguments

    def complete(self) -> list[ToolCall]:
        """Pending calls as complete ToolCalls, in index order."""
        return [
            ToolCall(
                id=self._pending[index]["id"],
                name=self._pending[index]["name"],
                arguments=self._pending[index]["arguments"],
            )
            for index in sorted(self._pending)
        ]


def _checked_effort(reasoning_effort: str) -> None:
    """Reject unknown reasoning efforts before spending a round-trip."""
    if reasoning_effort not in REASONING_EFFORTS:
        allowed = ", ".join(REASONING_EFFORTS)
        msg = f"reasoning_effort must be one of {allowed}; got {reasoning_effort!r}"
        raise ValueError(msg)


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
    *,
    model: str,
    prompt: str,
    max_tokens: int,
    temperature: float,
    reasoning_effort: str,
    seed: int | None = None,
    grammar: str = DIFF_GRAMMAR,
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "model": model,
        "messages": [{"role": "user", "content": prompt}],
        "temperature": temperature,
        "max_tokens": max_tokens,
        "reasoning_effort": reasoning_effort,
        "include_reasoning": True,
        "structured_outputs": {"grammar": grammar},
    }
    # Concurrent draws of one prompt are told apart by seed (T6-25); a
    # call without one leaves the key out and the server picks, as before.
    if seed is not None:
        payload["seed"] = seed
    return payload


def _build_text_payload(
    *, model: str, prompt: str, max_tokens: int, temperature: float, reasoning_effort: str
) -> dict[str, Any]:
    return {
        "model": model,
        "messages": [{"role": "user", "content": prompt}],
        "temperature": temperature,
        "max_tokens": max_tokens,
        "reasoning_effort": reasoning_effort,
        "include_reasoning": True,
    }


def _build_chat_payload(
    *,
    model: str,
    messages: Sequence[Mapping[str, Any]],
    max_tokens: int,
    temperature: float,
    reasoning_effort: str,
    tools: Sequence[Mapping[str, Any]] | None = None,
) -> dict[str, Any]:
    payload = {
        "model": model,
        "messages": [dict(message) for message in messages],
        "temperature": temperature,
        "max_tokens": max_tokens,
        "reasoning_effort": reasoning_effort,
        "include_reasoning": True,
        "stream": True,
        "stream_options": {"include_usage": True},
    }
    if tools:
        payload["tools"] = [dict(tool) for tool in tools]
        payload["tool_choice"] = "auto"
    return payload


def _stream_error(data: dict[str, Any]) -> str | None:
    """Server message from a mid-stream error object, else None."""
    error = data.get("error")
    if not isinstance(error, dict):
        return None
    message = error.get("message")
    if not isinstance(message, str) or not message:
        return None
    return message


def _chunk_delta(data: object) -> dict[str, Any]:
    """Validated delta mapping from one SSE chunk envelope."""
    if not isinstance(data, dict):
        msg = f"stream chunk must be an object, got {type(data).__name__}"
        raise VllmResponseError(msg)
    detail = _stream_error(data)
    if detail is not None:
        msg = f"server error during stream: {detail}"
        raise VllmRequestError(msg)
    choices = data.get("choices")
    if not isinstance(choices, list) or not choices:
        msg = "stream chunk has no choices"
        raise VllmResponseError(msg)
    first = choices[0]
    delta = first.get("delta") if isinstance(first, dict) else None
    if not isinstance(delta, dict):
        msg = "stream chunk has no delta"
        raise VllmResponseError(msg)
    return delta


def _delta_tokens(delta: Mapping[str, Any]) -> list[StreamToken]:
    """Reasoning then content tokens from one validated delta."""
    tokens: list[StreamToken] = []
    for stream in ("reasoning", "content"):
        text = delta.get(stream)
        if isinstance(text, str) and text:
            tokens.append(StreamToken(stream=stream, text=text))
    return tokens


def _text_or_empty(value: object) -> str:
    return value if isinstance(value, str) else ""


def _usage(data: Mapping[str, Any]) -> dict[str, int]:
    """The server's token accounting, integers only; absent fields are absent."""
    raw = data.get("usage")
    if not isinstance(raw, dict):
        return {}
    flat: dict[str, int] = {}
    for key, value in raw.items():
        if isinstance(value, int) and not isinstance(value, bool):
            flat[key] = value
        elif isinstance(value, dict):
            flat.update({k: v for k, v in value.items() if isinstance(v, int)})
    return flat


def _parse_message(data: object, *, max_tokens: int | None = None) -> tuple[str, str]:
    """Validated (content, reasoning) from a chat envelope; truncations raise.

    A truncation names the cap it hit when the caller passes it. It no
    longer advises "retry with more max_tokens": for a year no caller did,
    and a message that names a remedy nothing applies is a claim the
    mechanism cannot back (T6-14). Escalation is the caller's contract.
    """
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
        at = f" at {max_tokens} output tokens" if max_tokens is not None else ""
        msg = f"completion truncated{at} (finish_reason=length)"
        raw_partial = first.get("message")
        partial: dict[str, Any] = raw_partial if isinstance(raw_partial, dict) else {}
        raise VllmResponseError(
            msg,
            reasoning=_text_or_empty(partial.get("reasoning")),
            content=_text_or_empty(partial.get("content")),
            usage=_usage(data),
            max_tokens=max_tokens,
            finish_reason="length",
        )
    message = first.get("message")
    if not isinstance(message, dict):
        msg = "first choice has no message object"
        raise VllmResponseError(msg)
    content = message.get("content")
    if not isinstance(content, str) or not content.strip():
        # T6-18 (F21.10 b-s2): HTTP 200, `finish_reason: "stop"`, `content:
        # null`, 45k chars of reasoning cut mid-word. The think block ran
        # out and the turn ended with nothing emitted. Name it, and carry
        # the reasoning out so the attempt sidecar shows what happened.
        reasoning = _text_or_empty(message.get("reasoning"))
        raw_finish = first.get("finish_reason")
        finish = raw_finish if isinstance(raw_finish, str) else ""
        msg = (
            "message has no text content"
            f" (finish_reason={finish or 'unknown'}, {len(reasoning)} reasoning chars)"
        )
        raise VllmResponseError(
            msg,
            reasoning=reasoning,
            usage=_usage(data),
            max_tokens=max_tokens,
            finish_reason=finish,
        )
    # vLLM 0.28 surfaces the think block as `reasoning` (not `reasoning_content`).
    raw_reasoning = message.get("reasoning")
    reasoning = raw_reasoning if isinstance(raw_reasoning, str) else ""
    return content, reasoning


def _parse_response(data: object) -> DagEmission:
    content, reasoning = _parse_message(data)
    try:
        dag = json.loads(content)
    except json.JSONDecodeError as exc:
        msg = f"content is not valid JSON: {exc}"
        raise VllmResponseError(msg) from exc
    if not isinstance(dag, dict):
        msg = "content JSON must be an object"
        raise VllmResponseError(msg)
    return DagEmission(dag=dag, reasoning=reasoning, raw_content=content)


def _parse_text_response(data: object) -> str:
    """Free-text content from a chat envelope; prose needs no JSON parse."""
    content, _ = _parse_message(data)
    return content


def _parse_diff_response(data: object, *, max_tokens: int | None = None) -> DiffProposal:
    """The content IS the diff: DIFF_GRAMMAR constrains raw text, not JSON.

    Structural rejection stays in `slice._apply_diff` rather than here, so
    a bad packet remains a retryable attempt rather than throwing the run
    away. Blank content is already refused by `_parse_message`.
    """
    content, reasoning = _parse_message(data, max_tokens=max_tokens)
    usage = _usage(data) if isinstance(data, dict) else {}
    return DiffProposal(diff=content, reasoning=reasoning, usage=usage, max_tokens=max_tokens)


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


def _model_context(data: object, model: str) -> int | None:
    """`max_model_len` of the served `model` from a /models envelope; None if unreported."""
    if model not in _model_ids(data):
        return None
    for item in data["data"]:  # type: ignore[index]  # _model_ids validated the shape
        if item["id"] == model:
            value = item.get("max_model_len")
            return value if isinstance(value, int) and not isinstance(value, bool) else None
    return None  # pragma: no cover -- unreachable: the id was in _model_ids


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


API_ROOT_SEGMENT: Final = "v1"


def _sibling_url(base_url: httpx.URL, endpoint: str) -> httpx.URL:
    """An endpoint beside the API root rather than under it.

    vLLM mounts the OpenAI-compatible API at `/v1` but serves `/tokenize`
    and `/version` at the server root, so a URL built relative to the API
    root asks for `/v1/tokenize` and is answered 404. A deployment behind a
    prefix keeps it: `/inference/v1` -> `/inference/tokenize`.
    """
    segments = [part for part in base_url.path.split("/") if part]
    if segments and segments[-1] == API_ROOT_SEGMENT:
        segments.pop()
    segments.append(endpoint)
    return base_url.copy_with(path="/" + "/".join(segments), query=None, fragment=None)


def _version_url(base_url: httpx.URL) -> httpx.URL:
    """The version endpoint beside the API root, not under it (T6-45).

    vLLM serves its version at the server root while the OpenAI-compatible
    API is mounted under `/v1`, so a request built relative to the API root
    asks for `/v1/version` and is answered 404 -- which `served_version`
    then records as `unknown`, a refusal the server never made. A
    deployment mounted under a prefix keeps that prefix, so only the API
    segment itself is dropped: `/inference/v1` -> `/inference/version`.
    """
    segments = [part for part in base_url.path.split("/") if part]
    if segments and segments[-1] == API_ROOT_SEGMENT:
        segments.pop()
    segments.append("version")
    return base_url.copy_with(path="/" + "/".join(segments), query=None, fragment=None)


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
        _checked_effort(reasoning_effort)
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
        seed: int | None = None,
        grammar: str = DIFF_GRAMMAR,
    ) -> DiffProposal:
        """Propose an edit payload for *prompt*, guided by an EBNF grammar.

        The default is DIFF_GRAMMAR, which admits whole-file writes and
        deletes only. A caller that wants the smaller emission passes
        `edits.EDIT_GRAMMAR` instead; the transport is identical, so the
        choice lives with the caller that also writes the prompt.
        """
        if not prompt.strip():
            msg = "prompt must not be empty"
            raise ValueError(msg)
        _checked_effort(reasoning_effort)
        payload = _build_diff_payload(
            model=self._model,
            prompt=prompt,
            max_tokens=max_tokens,
            temperature=temperature,
            reasoning_effort=reasoning_effort,
            seed=seed,
            grammar=grammar,
        )
        started_at = datetime.now(UTC).isoformat()
        start = perf_counter()
        try:
            proposal = _parse_diff_response(self._post(payload), max_tokens=max_tokens)
        except VllmError as exc:
            exc.evidence = {
                "prompt": prompt,
                "seed": seed,
                "temperature": temperature,
                "reasoning_effort": payload["reasoning_effort"],
                "started_at": started_at,
                "wall_s": round(perf_counter() - start, 3),
            }
            raise
        return replace(
            proposal,
            prompt=prompt,
            seed=seed,
            temperature=temperature,
            reasoning_effort=payload["reasoning_effort"],
            started_at=started_at,
            wall_s=round(perf_counter() - start, 3),
        )

    def complete(
        self,
        prompt: str,
        *,
        max_tokens: int = DEFAULT_MAX_TOKENS,
        temperature: float = DEFAULT_TEMPERATURE,
        reasoning_effort: str = DEFAULT_REASONING_EFFORT,
    ) -> str:
        """One free-text completion for *prompt* (recovery planning).

        No guided schema: the plan is prose, so there is nothing to
        mis-parse — only envelope, truncation, and blank-content errors.
        """
        if not prompt.strip():
            msg = "prompt must not be empty"
            raise ValueError(msg)
        _checked_effort(reasoning_effort)
        payload = _build_text_payload(
            model=self._model,
            prompt=prompt,
            max_tokens=max_tokens,
            temperature=temperature,
            reasoning_effort=reasoning_effort,
        )
        return _parse_text_response(self._post(payload))

    def stream_chat(
        self,
        messages: Sequence[Mapping[str, Any]],
        *,
        max_tokens: int = DEFAULT_MAX_TOKENS,
        temperature: float = DEFAULT_TEMPERATURE,
        reasoning_effort: str = DEFAULT_REASONING_EFFORT,
        tools: Sequence[Mapping[str, Any]] | None = None,
    ) -> Iterator[StreamToken | ToolCall | StreamUsage]:
        """Stream one completion: tokens, then complete tool calls, then usage if sent."""
        if not messages:
            msg = "messages must not be empty"
            raise ValueError(msg)
        _checked_effort(reasoning_effort)
        payload = _build_chat_payload(
            model=self._model,
            messages=messages,
            max_tokens=max_tokens,
            temperature=temperature,
            reasoning_effort=reasoning_effort,
            tools=tools,
        )
        # httpx upper-cases the method, so case mutants ("post") are
        # behaviorally identical on the wire: unkillable, hence pragma.
        method = "POST"  # pragma: no mutate
        try:
            with self._client.stream(method, "/chat/completions", json=payload) as response:
                if response.status_code in (401, 403):
                    msg = f"server rejected the API key (HTTP {response.status_code})"
                    raise VllmAuthError(msg)
                if response.status_code >= 400:
                    response.read()
                    msg = f"server returned HTTP {response.status_code}: {response.text[:200]}"
                    raise VllmRequestError(msg)
                calls = _ToolCallAccumulator()
                usage: dict[str, int] = {}
                for line in response.iter_lines():
                    if not line.startswith("data: "):
                        continue
                    content = line[len("data: ") :]
                    if content == "[DONE]":
                        break
                    try:
                        data = json.loads(content)
                    except json.JSONDecodeError as exc:
                        msg = f"stream chunk is not valid JSON: {exc}"
                        raise VllmResponseError(msg) from exc
                    if isinstance(data, dict) and isinstance(data.get("usage"), dict):
                        usage = _usage(data)
                        if data.get("choices") == []:
                            continue  # the include_usage chunk carries no delta
                    delta = _chunk_delta(data)
                    yield from _delta_tokens(delta)
                    calls.add(delta)
                yield from calls.complete()
                if "completion_tokens" in usage:
                    yield StreamUsage(
                        prompt_tokens=usage.get("prompt_tokens", 0),
                        completion_tokens=usage["completion_tokens"],
                        reasoning_tokens=usage.get("reasoning_tokens"),
                    )
        except httpx.HTTPError as exc:
            msg = f"request failed: {exc}"
            raise VllmRequestError(msg) from exc

    def _models(self) -> Any:
        try:
            response = self._client.get("/models", timeout=PREFLIGHT_TIMEOUT)
        except httpx.HTTPError as exc:
            msg = f"request failed: {exc}"
            raise VllmRequestError(msg) from exc
        return _checked_json(response)

    def list_models(self) -> list[str]:
        """GET /models with a short timeout; return served model ids."""
        return _model_ids(self._models())

    def server_version(self) -> str | None:
        """The server's version string, or None when it has none (T6-27, T6-45)."""
        try:
            response = self._client.get(
                _version_url(self._client.base_url), timeout=PREFLIGHT_TIMEOUT
            )
        except httpx.HTTPError as exc:
            msg = f"request failed: {exc}"
            raise VllmRequestError(msg) from exc
        data = _checked_json(response)
        version = data.get("version") if isinstance(data, dict) else None
        return version if isinstance(version, str) and version else None

    def max_model_len(self) -> int | None:
        """The served model's context length as vLLM reports it, or None (T6-17)."""
        return _model_context(self._models(), self._model)

    def count_tokens(
        self,
        messages: Sequence[Mapping[str, Any]],
        *,
        tools: Sequence[Mapping[str, Any]] | None = None,
    ) -> int | None:
        """Exactly how many tokens this request's prompt will cost.

        The server owns the tokeniser and the chat template, so it is the
        only thing that can answer this. Counting characters over four is a
        guess that is wrong in both directions and wrong by a lot: measured
        here, 2,732 estimated against 4,100 charged, which is the difference
        between a reply and an HTTP 400.

        `tools` matters: the schemas are rendered into the prompt by the chat
        template, and on this server they are 874 of the 948 tokens a small
        request costs. Returns None if the server does not serve /tokenize,
        so the caller can fall back rather than fail.
        """
        payload: dict[str, Any] = {
            "model": self._model,
            "messages": [dict(message) for message in messages],
        }
        if tools:
            payload["tools"] = [dict(tool) for tool in tools]
        try:
            response = self._client.post(
                _sibling_url(self._client.base_url, "tokenize"),
                json=payload,
                timeout=PREFLIGHT_TIMEOUT,
            )
            if response.status_code != httpx.codes.OK:
                return None
            count = response.json().get("count")
        except (httpx.HTTPError, ValueError):
            return None
        return count if isinstance(count, int) else None

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
