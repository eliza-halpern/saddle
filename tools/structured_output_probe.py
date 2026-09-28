#!/usr/bin/env python3
"""Probe what vLLM 0.28 constrains, and when (settles a question before D14).

Three things the workflow could not settle by reading:

1. With ``structured_outputs={"grammar": DIFF_GRAMMAR}`` and reasoning on,
   is the ``<think>`` block free text and only the visible completion
   constrained -- the alternation ARCHITECTURE.md §1 and D14 require?
2. Does the server honour an ``enable_in_reasoning``-style flag, or accept
   an unknown key and silently ignore it?
3. Does ``structural_tag`` exist on this build, under ``structured_outputs``
   or under ``response_format``?

The prompt is deliberately a *prose* question, so "the completion is
constrained" cannot rest on the model's obedience: with the grammar on the
content must be a diff even though a sentence was asked for, and the
`control` arm (same prompt, no grammar) must be a sentence. Without that
pair the `grammar` arm shows only that a model asked for a diff produced
one.

Every arm's raw envelope is written to ``<outdir>/<arm>.json``; rejections
are recorded with the server's own message, which is the finding for arms
3-5. Reads the key from ``SADDLE_VLLM_API_KEY``; never prints it.

    SADDLE_VLLM_API_KEY=... .venv/bin/python tools/structured_output_probe.py OUTDIR
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from typing import Any

import httpx

from saddle.vllm import DEFAULT_BASE_URL, DEFAULT_MODEL, DIFF_GRAMMAR

PROMPT = "In one sentence, what does the Python expression `sum(range(4))` evaluate to?"
TRIGGER_PROMPT = (
    "In one sentence, what does the Python expression `sum(range(4))` evaluate to? "
    "Then emit the literal tag <diff> followed by your answer again and the tag </diff>."
)
TIMEOUT = 600.0
DIFF_HEAD = "diff --git "
# The `unknown_key` arm's whole value is that nothing implements this name, so
# the probe refuses to run if it ever leaks into another arm: renamed to
# something real, that arm silently stops discriminating "the server
# recognises the flag" from "the server drops what it does not know", and the
# console output does not change one character.
UNIMPLEMENTED_KEY = "saddle_not_a_real_key"
DIFF_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {"diff": {"type": "string"}},
    "required": ["diff"],
}
# Four candidate structural_tag shapes. The server names the key in its
# rejection ("Invalid structured_outputs structural_tag specification"), so
# the key exists and only the shape is in question; each arm below is one
# published spelling of it.
TAG_GRAMMAR: dict[str, Any] = {
    "type": "structural_tag",
    "structures": [{"begin": "<diff>", "end": "</diff>", "grammar": DIFF_GRAMMAR}],
    "triggers": ["<diff>"],
}
TAG_SCHEMA: dict[str, Any] = {
    "type": "structural_tag",
    "structures": [{"begin": "<diff>", "end": "</diff>", "schema": DIFF_SCHEMA}],
    "triggers": ["<diff>"],
}
TAG_TRIGGERED: dict[str, Any] = {
    "type": "structural_tag",
    "format": {
        "type": "triggered_tags",
        "triggers": ["<diff>"],
        "tags": [
            {
                "begin": "<diff>",
                "end": "</diff>",
                "content": {"type": "grammar", "syntax": "ebnf", "definition": DIFF_GRAMMAR},
            }
        ],
    },
}
TAG_BARE: dict[str, Any] = {
    "structures": [{"begin": "<diff>", "end": "</diff>", "schema": DIFF_SCHEMA}],
    "triggers": ["<diff>"],
}


def _base(extra: dict[str, Any], prompt: str = PROMPT) -> dict[str, Any]:
    """One chat payload: the shared shape plus this arm's extra keys."""
    return {
        "model": DEFAULT_MODEL,
        "messages": [{"role": "user", "content": prompt}],
        "temperature": 0.0,
        "max_tokens": 1024,
        "reasoning_effort": "low",
        "include_reasoning": True,
        **extra,
    }


ARMS: dict[str, dict[str, Any]] = {
    # The item's request, and the control that makes its verdict falsifiable.
    "grammar": _base({"structured_outputs": {"grammar": DIFF_GRAMMAR}}),
    "control": _base({}),
    # Is an enable_in_reasoning-style flag honoured, ignored, or refused? The
    # `unknown_key` arm is the vacuity check on the answer: if a name nobody
    # implements is also accepted, a 200 on `enable_in_reasoning` means only
    # that extra keys are dropped, not that the flag is recognised.
    "enable_in_reasoning": _base(
        {"structured_outputs": {"grammar": DIFF_GRAMMAR, "enable_in_reasoning": True}}
    ),
    "enable_in_reasoning_top": _base(
        {"structured_outputs": {"grammar": DIFF_GRAMMAR}, "enable_in_reasoning": True}
    ),
    "unknown_key": _base(
        {"structured_outputs": {"grammar": DIFF_GRAMMAR, UNIMPLEMENTED_KEY: True}}
    ),
    # Does structural_tag exist here, and under which field and shape?
    "structural_tag_so": _base({"structured_outputs": {"structural_tag": TAG_GRAMMAR}}),
    "structural_tag_rf": _base({"response_format": TAG_GRAMMAR}),
    "structural_tag_schema": _base({"structured_outputs": {"structural_tag": TAG_SCHEMA}}),
    "structural_tag_triggered": _base({"structured_outputs": {"structural_tag": TAG_TRIGGERED}}),
    "structural_tag_bare": _base({"structured_outputs": {"structural_tag": TAG_BARE}}),
    # TAG_SCHEMA is field-for-field the server's own LegacyStructuralTagResponseFormat
    # and TAG_TRIGGERED its StructuralTagResponseFormat (see openapi_structural_tag.json,
    # written below): these two arms send each documented shape through the field the
    # spec attaches it to, so a rejection here is the server refusing its own schema.
    "structural_tag_rf_schema": _base({"response_format": TAG_SCHEMA}),
    "structural_tag_rf_triggered": _base({"response_format": TAG_TRIGGERED}),
    # An accepted structural tag says nothing about what it accepts *inside*
    # the tag until the trigger actually fires, so this arm asks for the tag.
    # Known-good: prose before `<diff>`, and the span between the delimiters
    # constrained to TAG_SCHEMA's JSON object rather than to free text.
    "structural_tag_fires": _base({"response_format": TAG_SCHEMA}, TRIGGER_PROMPT),
}

# Which structural_tag shapes this build will even name. `format` is typed `Any`
# in the spec, so the spec bounds the outer shape and llguidance owns the inner
# one -- worth saving verbatim next to the rejections it explains.
TAG_SCHEMA_NAMES = (
    "StructuralTagResponseFormat",
    "LegacyStructuralTag",
    "LegacyStructuralTagResponseFormat",
)


def _shape(body: Any) -> dict[str, Any]:
    """The response shape this probe exists to record, from a 200 envelope."""
    choice = body["choices"][0] if isinstance(body, dict) and body.get("choices") else {}
    message = choice.get("message") or {}
    reasoning = message.get("reasoning") or ""
    content = message.get("content") or ""
    return {
        "envelope_keys": sorted(body) if isinstance(body, dict) else None,
        "choice_keys": sorted(choice),
        "message_keys": sorted(message),
        "finish_reason": choice.get("finish_reason"),
        "reasoning_len": len(reasoning),
        "content_len": len(content),
        "reasoning_is_diff_head": reasoning.startswith(DIFF_HEAD),
        "content_is_diff_head": content.startswith(DIFF_HEAD),
        "reasoning_head": reasoning[:400],
        "content_head": content[:400],
        "tag_span": _tag_span(content),
    }


def _tag_span(content: str) -> dict[str, Any] | None:
    """What a fired `<diff>` trigger enclosed, and what preceded it."""
    begin, end = "<diff>", "</diff>"
    if begin not in content:
        return None
    before, _, rest = content.partition(begin)
    inside = rest.partition(end)[0]
    try:
        parsed: Any = json.loads(inside)
    except ValueError:
        parsed = None
    return {
        "before_tag": before,
        "inside_tag": inside,
        "inside_is_json_object": isinstance(parsed, dict),
        "inside_json_keys": sorted(parsed) if isinstance(parsed, dict) else None,
    }


def main(argv: list[str]) -> int:
    """Run every arm against the live server, writing one JSON per arm."""
    key = os.environ.get("SADDLE_VLLM_API_KEY") or os.environ.get("VLLM_API_KEY")
    if not key:
        print("error: SADDLE_VLLM_API_KEY is not set", file=sys.stderr)
        return 2
    leaked = sorted(
        arm
        for arm, payload in ARMS.items()
        if arm != "unknown_key" and UNIMPLEMENTED_KEY in json.dumps(payload)
    )
    if leaked or UNIMPLEMENTED_KEY not in json.dumps(ARMS["unknown_key"]):
        print(f"error: the unknown_key arm no longer discriminates ({leaked=})", file=sys.stderr)
        return 3
    outdir = Path(argv[1] if len(argv) > 1 else ".")
    outdir.mkdir(parents=True, exist_ok=True)
    with httpx.Client(
        base_url=DEFAULT_BASE_URL,
        headers={"Authorization": f"Bearer {key}"},
        timeout=TIMEOUT,
    ) as client:
        for arm, payload in ARMS.items():
            response = client.post("/chat/completions", json=payload)
            try:
                body = response.json()
            except ValueError:
                body = {"non_json_body": response.text[:2000]}
            record = {
                "arm": arm,
                "status": response.status_code,
                "request_extra": {k: v for k, v in payload.items() if k not in _base({})},
                "body": body,
            }
            if response.status_code == 200:
                record["shape"] = _shape(body)
            (outdir / f"{arm}.json").write_text(json.dumps(record, indent=2) + "\n")
            print(f"{arm}: HTTP {response.status_code} -> {outdir / f'{arm}.json'}")
            if response.status_code == 200:
                shape = record["shape"]
                print(
                    f"  finish={shape['finish_reason']}"
                    f" reasoning_len={shape['reasoning_len']}"
                    f" reasoning_is_diff_head={shape['reasoning_is_diff_head']}"
                    f" content_len={shape['content_len']}"
                    f" content_is_diff_head={shape['content_is_diff_head']}"
                )
            else:
                print(f"  rejected: {json.dumps(body)[:400]}")
        _save_tag_schemas(client, outdir)
    return 0


def _save_tag_schemas(client: httpx.Client, outdir: Path) -> None:
    """Save the build's own structural_tag models, which bound the shapes above."""
    spec = client.get("../openapi.json").json()
    schemas = spec.get("components", {}).get("schemas", {})
    found = {name: schemas[name] for name in TAG_SCHEMA_NAMES if name in schemas}
    (outdir / "openapi_structural_tag.json").write_text(
        json.dumps({"version": spec.get("info", {}).get("version"), "schemas": found}, indent=2)
        + "\n"
    )
    print(f"structural_tag models named by this build: {sorted(found)}")


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
