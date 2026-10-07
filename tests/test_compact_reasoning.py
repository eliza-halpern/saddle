"""Compaction clears old reasoning before it touches a file the model read (stage 0).

Compaction cut the oldest tool results first and then whole exchanges, while
every past round's reasoning stayed in the prompt. In a recorded dogfood run old
reasoning was a quarter to three fifths of the context at each of nine
compactions, and each one dropped the file the model was editing, which it then
read again. Keeping the newest reasoning scores like keeping all of it, and
dropping all of it costs rounds and wall time (saddle's keep-reasoning A/B).

Known-bad: with old reasoning and an old file read over the limit, the read is
truncated or dropped while the reasoning stays. Known-good: the newest rounds
keep their reasoning; when clearing is enough nothing else is touched; what is
cleared is handed to `archive` whole; the note says reasoning was cleared, and
keeps saying so on a later compaction; a context under the limit is untouched.
"""

from __future__ import annotations

import json
from typing import Any

from saddle import memory
from saddle.memory import ELIDED, REASONING_KEPT, REASONING_LINE, RESULT_HEAD, RESULT_TAIL, compact

FILE = "def f():\n    return 1\n" * ((RESULT_HEAD + RESULT_TAIL) // 20 + 40)
THOUGHT = "I think about the schema and the fixtures at length. " * 300


def exchange(n: int, reasoning: str = THOUGHT, result: str = "ok") -> list[dict[str, Any]]:
    call = {
        "id": f"c{n}",
        "type": "function",
        "function": {"name": "read_file", "arguments": json.dumps({"path": f"f{n}.py"})},
    }
    return [
        {
            "role": "assistant",
            "content": "",
            "reasoning_content": reasoning,
            "reasoning": reasoning,
            "tool_calls": [call],
        },
        {"role": "tool", "tool_call_id": f"c{n}", "content": result},
    ]


def history(rounds: int, *, file_at: int = 0) -> list[dict[str, Any]]:
    messages: list[dict[str, Any]] = [
        {"role": "system", "content": "sys"},
        {"role": "user", "content": "the task"},
    ]
    for n in range(rounds):
        messages += exchange(n, result=FILE if n == file_at else "ok")
    return messages


def reasoning_of(messages: list[dict[str, Any]]) -> list[str]:
    return [m.get("reasoning_content", "") for m in messages if m.get("role") == "assistant"]


def test_old_reasoning_goes_before_the_file_the_model_read() -> None:
    messages = history(10, file_at=1)
    before = memory.estimate_tokens(messages)
    dropped, summary = compact(
        messages,
        limit_tokens=before - 1,
        target_tokens=before // 2,
        pin="first",
        state=lambda: "state",
    )
    assert (dropped, summary) == (0, f"reasoning of {10 - REASONING_KEPT} earlier round(s) cleared")
    read = next(m for m in messages if m.get("tool_call_id") == "c1")
    assert read["content"] == FILE  # whole: no truncation, not dropped
    assert ELIDED not in json.dumps(messages)
    kept = reasoning_of(messages)
    assert kept[: 10 - REASONING_KEPT] == [""] * (10 - REASONING_KEPT)
    assert kept[10 - REASONING_KEPT :] == [THOUGHT] * REASONING_KEPT  # the newest keep theirs
    assert all(
        m.get("reasoning") == ""
        for m in messages
        if m.get("role") == "assistant" and not m.get("reasoning_content")
    )


def test_what_is_cleared_reaches_the_archive_whole() -> None:
    messages = history(6)
    archived: list[dict[str, Any]] = []
    before = memory.estimate_tokens(messages)
    compact(messages, limit_tokens=before - 1, target_tokens=before // 2, archive=archived.extend)
    # Every round's reasoning is either still in context or in the archive, once:
    # stage 0 hands over what it clears, and stage 2 an exchange it drops whole.
    in_context = sum(m.get("reasoning_content") == THOUGHT for m in messages)
    archived_too = sum(m.get("reasoning_content") == THOUGHT for m in archived)
    assert in_context + archived_too == 6
    assert archived_too >= 6 - REASONING_KEPT


def test_when_clearing_is_not_enough_the_note_says_reasoning_was_cleared_and_keeps_saying_so() -> (
    None
):
    messages = history(12, file_at=1)
    before = memory.estimate_tokens(messages)
    dropped, _ = compact(
        messages,
        limit_tokens=before // 4,
        target_tokens=before // 8,
        pin="first",
        state=lambda: "state",
    )
    assert dropped > 0  # stage 2 ran too
    (note,) = [m for m in messages if memory.is_note(m)]
    assert REASONING_LINE in note["content"]
    # A later compaction with nothing left to clear keeps the line.
    messages += exchange(99, reasoning="short", result=FILE)
    later = memory.estimate_tokens(messages)
    compact(
        messages,
        limit_tokens=later - 1,
        target_tokens=later // 2,
        pin="first",
        state=lambda: "state",
    )
    (note,) = [m for m in messages if memory.is_note(m)]
    assert REASONING_LINE in note["content"]


def test_a_context_under_the_limit_keeps_all_its_reasoning() -> None:
    messages = history(8)
    snapshot = json.dumps(messages)
    assert compact(messages, limit_tokens=memory.estimate_tokens(messages) + 1) == (0, "")
    assert json.dumps(messages) == snapshot


def test_old_results_are_cut_oldest_first_and_only_as_far_as_the_target() -> None:
    """Known-bad before: stage 1 cut every oversized old result, the file the
    model read last as well as the first, even when the first was enough."""
    messages = history(10, file_at=1)
    next(m for m in messages if m.get("tool_call_id") == "c2")["content"] = FILE  # a newer read
    for message in messages:  # no reasoning: stage 1 alone decides
        if message.get("role") == "assistant":
            message["reasoning_content"] = message["reasoning"] = ""
    before = memory.estimate_tokens(messages)
    cut_one = memory.estimate_tokens([{"role": "tool", "content": FILE}]) - memory.estimate_tokens(
        [{"role": "tool", "content": memory._truncate_result(FILE)}]
    )
    compact(
        messages,
        limit_tokens=before - 1,
        target_tokens=before - cut_one,
        pin="first",
        state=lambda: "state",
    )
    first = next(m for m in messages if m.get("tool_call_id") == "c1")
    second = next(m for m in messages if m.get("tool_call_id") == "c2")
    assert ELIDED in first["content"]
    assert second["content"] == FILE


def test_reasoning_under_one_key_alone_is_cleared_and_no_key_is_added() -> None:
    """Strata spells a round's reasoning `reasoning_content` alone: clearing it never
    adds the other spelling to the message."""
    messages = history(10, file_at=1)
    for message in messages:
        message.pop("reasoning", None)
    before = memory.estimate_tokens(messages)
    compact(messages, limit_tokens=before - 1, target_tokens=before - 1, pin="first")
    kept = reasoning_of(messages)
    assert kept == [""] * (10 - REASONING_KEPT) + [THOUGHT] * REASONING_KEPT
    assert not any("reasoning" in m for m in messages)
