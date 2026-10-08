"""Counting a prompt's tokens with the model's own tokenizer when the server counts none.

The server owns the tokenizer and the chat template, so `VllmClient.count_tokens`
asks it first (`/tokenize`). Strata serves no /tokenize, and every limit kept in
tokens then had no count: `tools._fit_output` fell back to lines, so one command's
output of a few very long lines (417,831 characters: a grep through node_modules'
source maps) went whole into a run's context, and the next request overflowed a
131,072-token window (#80a1 r1).

`SADDLE_TOKENIZER` names the served model's tokenizer as a Hugging Face
`tokenizer.json`. With it, a count is made here, with that tokenizer, over the
messages framed as the model's chat template frames them: `<|im_start|>role`,
each content trimmed as the template trims it, reasoning in `<think>`, tool calls
and tool results in their tags, and the tool schemas as JSON. Each content is
counted exactly as the server counts it; the framing is the template's own markers,
so a whole prompt's count can differ from the server's by the few tokens the
template adds around them. A message holding an image is not counted here at all
(None): its cost depends on the server's vision encoder.

The `tokenizers` package is optional (`saddle[tokenizer]`). A name that is set but
cannot be loaded stops saddle at the start, never falls back to an uncounted run.
"""

from __future__ import annotations

import json
import os
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Final

TOKENIZER_ENV: Final = "SADDLE_TOKENIZER"
"""The environment variable naming the served model's `tokenizer.json`."""


class TokenizerError(RuntimeError):
    """`SADDLE_TOKENIZER` is set, but its tokenizer cannot be used."""


def _text(content: object) -> str | None:
    """A message content as text, or None when it is or holds something that is not text."""
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        texts = [
            str(part.get("text", ""))
            for part in content
            if isinstance(part, Mapping) and part.get("type") == "text"
        ]
        return "".join(texts) if len(texts) == len(content) else None
    return None


def framed(messages: Sequence[Mapping[str, Any]]) -> str | None:
    """`messages` as the chat template frames them, or None when one holds an image."""
    parts: list[str] = []
    for message in messages:
        body = _text(message.get("content"))
        if body is None:
            return None
        role = str(message.get("role") or "user")
        body = body.strip()
        reasoning = message.get("reasoning_content") or message.get("reasoning")
        if role == "assistant" and isinstance(reasoning, str) and reasoning.strip():
            body = f"<think>\n{reasoning.strip()}\n</think>\n\n{body}"
        for call in message.get("tool_calls") or []:
            body += f"\n<tool_call>\n{json.dumps(call, ensure_ascii=False)}\n</tool_call>"
        if role == "tool":
            role, body = "user", f"<tool_response>\n{body}\n</tool_response>"
        parts.append(f"<|im_start|>{role}\n{body}<|im_end|>\n")
    return "".join(parts)


@dataclass(frozen=True)
class LocalTokenizer:
    """The served model's tokenizer, loaded from a Hugging Face `tokenizer.json`."""

    path: Path
    encode: Callable[[str], int]
    """How many tokens a text is, special tokens such as `<|im_start|>` included."""

    @classmethod
    def load(cls, path: Path) -> LocalTokenizer:
        try:
            import tokenizers  # type: ignore[import-untyped]  # optional: saddle[tokenizer]
        except ImportError as exc:
            msg = (
                f"{TOKENIZER_ENV} names {path}, but the tokenizers package is not "
                "installed: install saddle[tokenizer], or unset it"
            )
            raise TokenizerError(msg) from exc
        try:
            tokenizer = tokenizers.Tokenizer.from_file(str(path))
        except Exception as exc:  # the library raises a bare Exception for a bad file
            msg = f"{TOKENIZER_ENV} names {path}, which is not a readable tokenizer: {exc}"
            raise TokenizerError(msg) from exc
        return cls(path, lambda text: len(tokenizer.encode(text, add_special_tokens=False).ids))

    @classmethod
    def from_env(cls, environ: Mapping[str, str] | None = None) -> LocalTokenizer | None:
        """The tokenizer `SADDLE_TOKENIZER` names, or None when it is unset."""
        named = (os.environ if environ is None else environ).get(TOKENIZER_ENV, "").strip()
        return cls.load(Path(named).expanduser()) if named else None

    def count(
        self,
        messages: Sequence[Mapping[str, Any]],
        *,
        tools: Sequence[Mapping[str, Any]] | None = None,
    ) -> int | None:
        """The tokens of `messages` and `tools`, or None when a message holds an image."""
        text = framed(messages)
        if text is None:
            return None
        if tools:
            text += "\n".join(json.dumps(tool, ensure_ascii=False) for tool in tools)
        return self.encode(text)
