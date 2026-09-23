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
COMPACTION_ROLE: Final = "system"


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
        for call in message.get("tool_calls") or []:
            total += len(str(call)) // CHARS_PER_TOKEN
    return total


def _truncate_result(text: str) -> str:
    if len(text) <= RESULT_HEAD + RESULT_TAIL:
        return text
    dropped = len(text) - RESULT_HEAD - RESULT_TAIL
    return (
        text[:RESULT_HEAD]
        + f"\n\n[... {dropped} characters elided by compaction ...]\n\n"
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


def _protected(messages: list[dict[str, Any]], index: int) -> bool:
    """System prompts and the recent tail are never compacted."""
    if messages[index].get("role") == "system":
        return True
    return index >= len(messages) - KEEP_RECENT


def compact(messages: list[dict[str, Any]], *, limit_tokens: int) -> tuple[int, str]:
    """Bring `messages` under `limit_tokens` in place.

    Returns (messages dropped, human summary). `(0, "")` means nothing was
    needed, which is the common case and must stay cheap.
    """
    if estimate_tokens(messages) <= limit_tokens:
        return 0, ""

    # Stage 1: shrink old tool results.
    for index, message in enumerate(messages):
        if _protected(messages, index) or message.get("role") != "tool":
            continue
        content = message.get("content") or ""
        if len(content) > RESULT_HEAD + RESULT_TAIL:
            message["content"] = _truncate_result(content)
    if estimate_tokens(messages) <= limit_tokens:
        return 0, "large tool results elided"

    # Stage 2: drop the oldest exchanges.
    dropped = 0
    topics: list[str] = []
    while estimate_tokens(messages) > limit_tokens:
        oldest = next((i for i in range(len(messages)) if not _protected(messages, i)), None)
        if oldest is None:
            break  # nothing left that may be dropped; report what we managed
        message = messages.pop(oldest)
        dropped += 1
        if message.get("role") == "user":
            text = _content_text(message.get("content")).strip().splitlines()
            if text:
                topics.append(text[0][:60])
    if not dropped:
        return 0, "large tool results elided"

    summary = f"{dropped} earlier message(s) compacted"
    if topics:
        summary += ": " + "; ".join(topics[:5])
    note = {
        "role": COMPACTION_ROLE,
        "content": (
            f"[Earlier conversation compacted: {summary}. "
            "Ask the user if you need detail that is no longer in context.]"
        ),
    }
    insert_at = 1 if messages and messages[0].get("role") == "system" else 0
    messages.insert(insert_at, note)
    return dropped, summary
