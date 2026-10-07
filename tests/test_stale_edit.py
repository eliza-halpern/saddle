"""An edit refused on a file a compaction dropped says so, and says to read it again (#189).

A compaction dropped `src/saddle/yamlcheck.py`, and its note said so and said to
read the file again before editing it (#177). In a dogfood run the worker
edited it from memory anyway. The refusal began "Its line 1 reads ...", which
read as the file's line 1: "The error says line 1 reads the same text... Odd."
The worker re-read the file two minutes after the compaction.

Known-bad: the edit of a dropped file, made from memory, is refused with the
reason and the way out. Known-good: once the file has been read again, a
mismatch reads as a plain one; either way the quoted line is named as the
snippet's.
"""

from __future__ import annotations

import json
from typing import Any

from test_chat_engine import FakeClient, content, options, run, tool

from saddle.engine import TurnOptions
from saddle.memory import (
    _NOTE_UNREAD,
    COMPACTION_ROLE,
    NOTE_HEAD,
    STALE_COPY,
    UNREAD_HOW,
    stale_files,
)

__all__ = ["options"]  # the fixture, shared with test_chat_engine

SOURCE = "def f():\n    return 1\n"


def note(*entries: str) -> dict[str, Any]:
    """A compaction note naming `entries` as files no longer in context."""
    return {
        "role": COMPACTION_ROLE,
        "content": f"{NOTE_HEAD}4 earlier message(s) dropped to fit the window.]\n"
        f"{_NOTE_UNREAD}{'; '.join(entries)}{UNREAD_HOW}",
    }


def call(name: str, call_id: str, **arguments: Any) -> dict[str, Any]:
    """An assistant message holding one tool call, as the engine records it."""
    function = {"name": name, "arguments": json.dumps(arguments)}
    return {
        "role": "assistant",
        "content": "",
        "tool_calls": [{"id": call_id, "type": "function", "function": function}],
    }


def edit_from_memory(options: TurnOptions, history: list[dict[str, Any]]) -> str:
    """What the worker is told when it edits `m.py` with a snippet the file lacks."""
    (options.workdir / "m.py").write_text(SOURCE)
    wrong = tool("edit_file", "e1", path="m.py", old="def f():\n    return 9\n", new="x = 1\n")
    run(FakeClient([[wrong], [content("done")]]), options, "change f", messages=history)
    (result,) = [m["content"] for m in history if m.get("tool_call_id") == "e1"]
    assert (options.workdir / "m.py").read_text() == SOURCE
    return str(result)


def test_an_edit_of_a_dropped_file_says_it_was_made_from_memory(options: TurnOptions) -> None:
    result = edit_from_memory(options, [note("m.py")])
    assert result.startswith("error: that snippet does not appear in 'm.py'.")
    assert "The snippet's line 2, '    return 9', is not in the file" in result
    assert result.endswith(STALE_COPY.format(path="m.py"))


def test_an_edit_of_a_file_read_again_since_is_a_plain_mismatch(options: TurnOptions) -> None:
    history = [
        note("m.py"),
        call("read_file", "r1", path="m.py"),
        {"role": "tool", "tool_call_id": "r1", "content": SOURCE},
    ]
    result = edit_from_memory(options, history)
    assert "The snippet's line 2, '    return 9', is not in the file" in result
    assert STALE_COPY.format(path="m.py") not in result


def test_the_stale_files_are_the_dropped_ones_not_brought_back() -> None:
    dropped = note("m.py (lines 1-40)", "./pkg/n.py")
    assert stale_files([]) == set()
    assert stale_files([dropped]) == {"m.py", "pkg/n.py"}
    written = call("write_file", "w1", path="pkg/n.py", content="x = 1\n")
    assert stale_files([dropped, written]) == {"m.py"}
