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
        index = next(
            (i for i in range(len(messages)) if not _protected(messages, i)), None
        )
        if index is None:
            break  # nothing left that may be dropped; report what we managed
        message = messages.pop(index)
        dropped += 1
        if message.get("role") == "user":
            text = (message.get("content") or "").strip().splitlines()
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


def _selftest() -> int:
    ok = True

    def check(cond: bool, label: str, detail: str = "") -> None:
        nonlocal ok
        print(f"{'ok  ' if cond else 'FAIL'}  {label}{(' -- ' + detail) if detail else ''}")
        ok = ok and cond

    small = [{"role": "user", "content": "hi"}, {"role": "assistant", "content": "hello"}]
    before = list(small)
    dropped, summary = compact(small, limit_tokens=1000)
    check((dropped, summary) == (0, "") and small == before,
          "known-good: a short conversation is untouched")

    big = [{"role": "system", "content": "you are saddle"}]
    for i in range(30):
        big.append({"role": "user", "content": f"question {i} " + "x" * 200})
        big.append({"role": "assistant", "content": f"answer {i} " + "y" * 200})
    dropped, summary = compact(big, limit_tokens=500)
    check(dropped > 0 and estimate_tokens(big) <= 500 + 100,
          "known-good: an oversized conversation is brought under the limit",
          f"dropped {dropped}, now ~{estimate_tokens(big)} tokens")
    check(big[0].get("content") == "you are saddle",
          "known-bad: the system prompt is never dropped")
    check(any("compacted" in (m.get("content") or "") for m in big),
          "known-bad: compaction is announced in-band, never silent")
    check(big[-1]["content"].startswith("answer 29"),
          "known-bad: the most recent exchange survives", big[-1]["content"][:20])

    fat = [{"role": "system", "content": "s"}]
    fat.append({"role": "tool", "tool_call_id": "1", "content": "Z" * 40_000})
    for i in range(KEEP_RECENT):
        fat.append({"role": "user", "content": f"recent {i}"})
    dropped, summary = compact(fat, limit_tokens=2000)
    body = fat[1]["content"]
    check("elided by compaction" in body and len(body) < 3000,
          "known-good: a huge tool result is elided head-and-tail before anything is dropped",
          f"len {len(body)}")
    check(body.startswith("Z") and body.endswith("Z"),
          "known-good: the elision keeps both ends, where the signal is")

    only_protected = [{"role": "system", "content": "S" * 100_000}]
    dropped, _ = compact(only_protected, limit_tokens=10)
    check(dropped == 0 and len(only_protected) == 1,
          "known-bad: when only protected messages remain it stops, never empties the list")

    print("\nMEMORY SELFTEST PASS" if ok else "\nMEMORY SELFTEST FAIL")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(_selftest())
