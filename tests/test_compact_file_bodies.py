"""A compaction that takes a file's contents says so, by file and lines (#177).

Right after a compaction a dogfood worker wrote "I don't remember the original
body precisely" and re-read the file it was in the middle of editing: the note
kept neither the file nor where the edit stood.

Contract: the note names every file whose contents (a `read_file` result or a
`write_file` body) the context showed and no longer holds whole, with the lines
read, and says to read it again before editing; a file still shown whole is
not named, and a later note keeps an entry until the file is read again.
"""

from __future__ import annotations

import json
from typing import Any

from saddle.memory import ELIDED, REREAD, UNREAD_HOW, compact, estimate_tokens, is_note

LIMIT = 600
"""Tight enough that every old exchange goes, not only shortened results."""
BODY = "\n".join(f"line {i}: " + "x" * 60 for i in range(400))


def _call(call_id: str, name: str, **args: Any) -> dict[str, Any]:
    return {
        "role": "assistant",
        "content": "",
        "tool_calls": [
            {
                "id": call_id,
                "type": "function",
                "function": {"name": name, "arguments": json.dumps(args)},
            }
        ],
    }


def _result(call_id: str, text: str) -> dict[str, Any]:
    return {"role": "tool", "tool_call_id": call_id, "content": text}


def _padding(tag: str, n: int = 8) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for i in range(n):
        out += [_call(f"{tag}{i}", "run_command", command="true"), _result(f"{tag}{i}", "ok")]
    return out


def _compacted(messages: list[dict[str, Any]]) -> str:
    compact(
        messages,
        limit_tokens=LIMIT,
        pin="first",
        hint=REREAD,
        state=lambda: "Run state, rebuilt from the run's records (not model-written):",
    )
    (note,) = [m for m in messages if is_note(m)]
    return str(note["content"])


def _unread(note: str) -> str:
    lines = [ln for ln in note.splitlines() if ln.endswith(UNREAD_HOW)]
    return lines[0] if lines else ""


def _run() -> list[dict[str, Any]]:
    return [
        {"role": "system", "content": "instructions"},
        {"role": "user", "content": "fix calc"},
        _call("r1", "read_file", path="calc.py", offset=1, limit=40),
        _result("r1", BODY),
        _call("r2", "read_file", path="big.py", offset=200),
        _result("r2", BODY),
        _call("r3", "read_file", path="whole.py"),
        _result("r3", BODY),
        _call("w1", "write_file", path="new.py", content=BODY),
        _result("w1", "wrote new.py"),
        _call("e1", "edit_file", path="only_edited.py", old="a", new="b"),
        _result("e1", "edited"),
        _call("r4", "read_file", path="kept.py"),
        _result("r4", BODY),
        *_padding("p"),
        _call("r5", "read_file", path="kept.py"),
        _result("r5", "short body, still whole"),
    ]


def test_a_note_names_each_file_whose_contents_it_took_with_the_lines_read() -> None:
    unread = _unread(_compacted(_run()))
    assert unread, "known-bad: the note names no file whose contents went"
    for entry in ("calc.py (lines 1-40)", "big.py (from line 200)", "whole.py", "new.py"):
        assert entry in unread.split(": ", 1)[1].removesuffix(UNREAD_HOW).split("; "), unread
    assert "kept.py" not in unread  # known-good: re-read, and that read is still whole
    assert "only_edited.py" not in unread  # an edit shows a fragment, never the body


def test_a_later_note_keeps_an_entry_until_the_file_is_read_again() -> None:
    messages = _run()
    _compacted(messages)
    messages += _padding("q", 12)
    assert "calc.py (lines 1-40)" in _unread(_compacted(messages))
    messages += [_call("r9", "read_file", path="calc.py"), _result("r9", "def add(a, b): ...")]
    messages += _padding("s", 12)
    messages += [_call("r10", "read_file", path="calc.py"), _result("r10", "def add(a, b): ...")]
    again = _unread(_compacted(messages))
    assert "calc.py" not in again
    assert "new.py" in again


def test_a_read_shortened_in_place_counts_as_gone() -> None:
    """The read stays, cut to its head and tail (stage 1): its body is gone
    all the same. An earlier note is rebuilt, so the line reaches the model."""
    messages = _run()
    _compacted(messages)
    messages += [_call("r7", "read_file", path="late.py"), _result("r7", BODY)]
    messages += _padding("t", 4)
    before = estimate_tokens(messages)
    compact(messages, limit_tokens=before - 100, pin="first", hint=REREAD, state=lambda: "")
    shortened = next(m for m in messages if m.get("tool_call_id") == "r7")
    assert ELIDED in shortened["content"]  # the read is still there, shortened
    (note,) = [m for m in messages if is_note(m)]
    assert "late.py" in _unread(str(note["content"]))
