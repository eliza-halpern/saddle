"""Context compaction: keep a long session inside the window, visibly.

A chat that runs for hours outgrows any context window. The failure mode to
avoid is the silent one -- a harness that quietly drops the middle of a
conversation and then answers as though it still knew it.

So compaction here is **deterministic, staged, and reported**. No model call
is involved: a summariser that is itself a model call adds latency, a
failure mode, and a second thing to distrust. The stages remove the bulkiest
and least re-derivable material first, and what was removed is always
announced as a `Compaction` event.

Stage 1  truncate oversized tool results in older turns, keeping a head and
         a tail plus a marker (a 40 KB file dump is the usual offender, and
         its first and last lines carry most of what mattered)
Stage 2  drop whole older exchanges, oldest first, replacing them with one
         note recording how many went and what they were about

Never removed: the system prompt, the most recent exchange, and the user
message currently being answered.
"""

from __future__ import annotations

import json
from typing import Any, Final

CHARS_PER_TOKEN: Final = 4
"""Deliberately crude. An exact tokeniser would tie compaction to one model,
and the decision this feeds is "is there room", not "how many exactly"."""

IMAGE_TOKENS: Final = 1200
"""What one image part is charged against the window."""

KEEP_RECENT: Final = 6
"""Messages at the tail that are never touched, whatever the pressure."""

RESULT_HEAD: Final = 1200
RESULT_TAIL: Final = 400
COMPACTION_ROLE: Final = "user"
"""Not "system": the served Qwen3.8 template raises on a system message after
index 0, and in an autonomous run a user-role note also keeps a user query in
the request once older turns are gone."""
SUMMARY_PREFIXES: Final = ("FAILED ", "ERROR ")
SUMMARY_LINES: Final = 50
NOTE_MARK: Final = "\n\n[Earlier conversation compacted"
EDIT_TOOLS: Final = ("edit_file", "write_file")


def estimate_tokens(messages: list[dict[str, Any]]) -> int:
    """Rough token count for a message list."""
    total = 0
    for message in messages:
        content = message.get("content") or ""
        if isinstance(content, list):
            # Multimodal parts. Image data is base64 and enormous, so it is
            # counted at a flat rate rather than by length: charging a photo
            # 200,000 tokens would evict the whole conversation around it.
            for part in content:
                if part.get("type") == "text":
                    total += len(part.get("text") or "") // CHARS_PER_TOKEN
                else:
                    total += IMAGE_TOKENS
            continue
        total += len(content) // CHARS_PER_TOKEN
        total += len(message.get("reasoning_content") or "") // CHARS_PER_TOKEN
        for call in message.get("tool_calls") or []:
            total += len(str(call)) // CHARS_PER_TOKEN
    return total


def _truncate_result(text: str) -> str:
    if len(text) <= RESULT_HEAD + RESULT_TAIL:
        return text
    dropped = len(text) - RESULT_HEAD - RESULT_TAIL
    middle = text[RESULT_HEAD:-RESULT_TAIL]
    kept = [ln for ln in middle.splitlines() if ln.startswith(SUMMARY_PREFIXES)][:SUMMARY_LINES]
    kept_text = ("\n".join(kept) + "\n") if kept else ""
    return (
        text[:RESULT_HEAD]
        + f"\n\n[... {dropped} characters elided by compaction ...]\n"
        + kept_text
        + "\n"
        + text[-RESULT_TAIL:]
    )


def _content_text(content: Any) -> str:
    """A message's content as plain text, whichever shape it is stored in.

    A string is the common case. An uploaded image makes it a list of
    content parts instead (`engine._user_message` builds
    `[{"type": "text", ...}, {"type": "image_url", ...}]`), so this takes
    the text of the first text part -- the same rule `engine._last_asked`
    applies to a retry, kept in sync by hand since `memory` must not import
    `engine`.
    """
    if isinstance(content, list):
        return next((p.get("text", "") for p in content if p.get("type") == "text"), "")
    return str(content or "")


def _protected(messages: list[dict[str, Any]], index: int, pin_task: bool = False) -> bool:
    """System prompts, the recent tail and (autonomous runs) the task are never compacted."""
    if messages[index].get("role") == "system":
        return True
    if pin_task and index == next(
        (i for i, m in enumerate(messages) if m.get("role") == "user"), -1
    ):
        return True
    return index >= len(messages) - KEEP_RECENT


ASK_USER: Final = "Ask the user if you need detail that is no longer in context."
REREAD: Final = (
    "Re-read files and re-run commands with the tools if you need detail "
    "that is no longer in context."
)


def compact(
    messages: list[dict[str, Any]],
    *,
    limit_tokens: int,
    pin_task: bool = False,
    ask_hint: str = ASK_USER,
) -> tuple[int, str]:
    """Bring `messages` under `limit_tokens` in place.

    Returns (messages dropped, human summary). `(0, "")` means nothing was
    needed, which is the common case and must stay cheap.
    """
    if estimate_tokens(messages) <= limit_tokens:
        return 0, ""

    # Stage 1: shrink old tool results.
    for index, message in enumerate(messages):
        if _protected(messages, index, pin_task) or message.get("role") != "tool":
            continue
        content = message.get("content") or ""
        if len(content) > RESULT_HEAD + RESULT_TAIL:
            message["content"] = _truncate_result(content)
    if estimate_tokens(messages) <= limit_tokens:
        return 0, "large tool results elided"

    # Stage 2: drop the oldest exchanges, a tool call always with its results.
    dropped = 0
    topics: list[str] = []
    edited: list[str] = []
    audits: list[str] = []
    while estimate_tokens(messages) > limit_tokens:
        oldest = next(
            (i for i in range(len(messages)) if not _protected(messages, i, pin_task)), None
        )
        if oldest is None:
            break  # nothing left that may be dropped; report what we managed
        end = oldest + 1
        while end < len(messages) and messages[end].get("role") == "tool":
            end += 1
        if any(_protected(messages, i, pin_task) for i in range(oldest, end)):
            break  # the call's results are in the protected tail; never split them
        for message in messages[oldest:end]:
            dropped += 1
            for c in message.get("tool_calls") or []:
                fn = c.get("function") or {}
                if fn.get("name") in EDIT_TOOLS:
                    try:
                        path = json.loads(fn.get("arguments") or "{}").get("path")
                    except (ValueError, AttributeError):
                        path = None
                    if isinstance(path, str) and path not in edited:
                        edited.append(path)
            text = _content_text(message.get("content"))
            audits += [ln for ln in text.splitlines() if ln.startswith("[audit")]
            if message.get("role") == "user":
                lines = text.strip().splitlines()
                if lines:
                    topics.append(lines[0][:60])
        del messages[oldest:end]
    # Stage 3: still over (the protected tail itself is too big): shrink tool
    # results in the tail too, all but the latest round's.
    if estimate_tokens(messages) > limit_tokens:
        last = max((i for i, m in enumerate(messages) if m.get("role") == "assistant"), default=-1)
        for message in messages[:last]:
            content = message.get("content") or ""
            if message.get("role") == "tool" and len(content) > RESULT_HEAD + RESULT_TAIL:
                message["content"] = _truncate_result(content)
    if not dropped:
        return 0, "large tool results elided"

    summary = f"{dropped} earlier message(s) compacted"
    if topics:
        summary += ": " + "; ".join(topics[:5])
    parts = [summary]
    if edited:
        parts.append("files edited in the dropped part: " + ", ".join(edited))
    if audits:
        parts.append("latest audit finding in the dropped part: " + audits[-1])
    parts.append(ask_hint)
    note = {"role": COMPACTION_ROLE, "content": NOTE_MARK.strip() + ": " + ". ".join(parts) + "]"}
    insert_at = 1 if messages and messages[0].get("role") == "system" else 0
    messages.insert(insert_at, note)
    return dropped, summary
