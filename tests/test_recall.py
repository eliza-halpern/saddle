"""`saddle.recall`: what compaction took, archived and searchable by meaning.

Contract: compaction hands its archive the full text of every tool result it
shortens and every message it drops, each result labelled with its call, and
never a result's shortened text; `recall` returns the nearest archived pieces
in full and always says how many it searched and how many are not embedded
yet; nothing archived, or a server that is down, never reads as "no matches".
The tool is offered only with the `embeddings` switch on, the server
answering and something archived, and a chat's compaction note names it then.
"""

from __future__ import annotations

import math
from pathlib import Path
from typing import Any, cast

import pytest
from test_auto import Scripted, call
from test_compactfix import BIG, BIG_LINES, CTX, _reads

from saddle import engine, recall, tools
from saddle.embed import EmbedError
from saddle.engine import RECALL_HINT, TurnOptions, run_turn
from saddle.memory import CHARS_PER_TOKEN, ELIDED, RESULT_HEAD, RESULT_TAIL, compact, is_note
from saddle.recall import PIECE_LINES, Piece, Recall, pieces, render
from saddle.tools import (
    CODE_SEARCH_TOOL,
    RECALL_TOOL,
    ToolContext,
    execute_tool,
    offer_code_search,
)
from saddle.vllm import ToolCall, VllmClient

WORDS = ("traceback", "config", "helper")
DOWN = "no embeddings server at http://e"


class FakeEmbed:
    """Vectors from which of `WORDS` a text names."""

    up = True

    def embed(self, items: list[Any], dimensions: int | None = None) -> list[list[float]]:
        if not FakeEmbed.up:
            raise EmbedError(DOWN)
        vectors = []
        for item in items:
            raw = [float(str(item).count(w)) for w in WORDS] + [0.1]
            norm = math.sqrt(sum(x * x for x in raw))
            vectors.append([x / norm for x in raw])
        return vectors


@pytest.fixture(autouse=True)
def _up() -> None:
    FakeEmbed.up = True


def _recall() -> Recall:
    return Recall(FakeEmbed, background=False)


def _call(id_: str, name: str, args: str) -> dict[str, Any]:
    return {"id": id_, "type": "function", "function": {"name": name, "arguments": args}}


def test_a_long_text_is_cut_into_numbered_pieces_and_blank_ones_are_left_out() -> None:
    assert pieces("user", "hello") == [Piece("user", "hello")]
    assert pieces("user", "\n\n") == []
    long = "\n".join(f"line {i}" for i in range(PIECE_LINES + 1))
    cut = pieces("tool", long)
    assert [p.label for p in cut] == ["tool (part 1 of 2)", "tool (part 2 of 2)"]
    assert cut[1].text == f"line {PIECE_LINES}"
    wide = pieces("tool", "x" * (recall.PIECE_TOKENS * CHARS_PER_TOKEN + 1))
    assert [len(p.text) for p in wide] == [recall.PIECE_TOKENS * CHARS_PER_TOKEN, 1]


def test_each_message_is_labelled_with_who_said_it_or_the_call_it_answers() -> None:
    assert render({"role": "user", "content": "hi"}) == ("user", "hi")
    assert render({"role": "tool", "call": "run_command({})", "content": "ok"}) == (
        "result of run_command({})",
        "ok",
    )
    asked = {"role": "assistant", "content": "", "tool_calls": [_call("1", "read_file", "{}")]}
    assert render(asked) == ("assistant called read_file({})", "")
    parts = [{"type": "text", "text": "look"}, {"type": "image_url", "image_url": {}}]
    assert render({"role": "user", "content": parts}) == ("user", "look")


def test_compaction_hands_over_the_full_text_it_shortens_and_drops() -> None:
    big = "traceback line\n" * ((RESULT_HEAD + RESULT_TAIL) // 10)
    messages: list[dict[str, Any]] = [
        {"role": "system", "content": "s"},
        {"role": "user", "content": "the task"},
        {"role": "assistant", "content": "", "tool_calls": [_call("c1", "run_command", "{}")]},
        {"role": "tool", "tool_call_id": "c1", "content": big},
        {"role": "user", "content": "old config talk " + "q" * 8000},
        *({"role": "assistant", "content": f"recent {i}"} for i in range(6)),
    ]
    lost: list[dict[str, Any]] = []
    compact(messages, limit_tokens=600, pin="first", archive=lost.extend)
    results = [m for m in lost if m["role"] == "tool"]
    assert results[0] == {
        "role": "tool",
        "tool_call_id": "c1",
        "content": big,
        "call": "run_command({})",
    }
    assert all(ELIDED not in str(m["content"]) for m in lost)  # never the shortened text
    assert any(str(m["content"]).startswith("old config talk") for m in lost)
    assert not any(m["content"] == "s" for m in lost)


def test_recall_returns_the_nearest_pieces_in_full_and_says_how_much_it_searched() -> None:
    archive = _recall()
    assert (
        archive.add(
            [
                {"role": "tool", "call": "pytest()", "content": "traceback traceback in test_x"},
                {"role": "user", "content": "the config lives in setup.cfg"},
                {"role": "assistant", "content": "a helper"},
            ]
        )
        == 3
    )
    assert archive.add([{"role": "user", "content": "the config lives in setup.cfg"}]) == 0
    found = archive.search("traceback", top=1).splitlines()
    assert found == [
        "Searched 3 of 3 archived pieces. Nearest first (similarity 0 to 1; compare them, "
        "not their size):",
        "--- result of pytest() (1.00)",
        "traceback traceback in test_x",
    ]


def test_nothing_archived_or_nothing_embedded_never_reads_as_no_matches() -> None:
    archive = _recall()
    assert archive.search("x").startswith("Nothing has been compacted away")
    FakeEmbed.up = False
    archive.add([{"role": "user", "content": "config"}])
    assert archive.error == DOWN
    assert archive.search("config") == f"error: recall could not search: {DOWN}"
    FakeEmbed.up = True
    archive._background = True  # the search retries what is waiting, in the background
    assert archive.search("config").startswith(
        f"error: Searched 0 of 1 archived pieces; 1 not embedded yet ({DOWN}), so nothing"
    )
    assert archive._worker is not None
    archive._worker.join(5)
    assert archive.search("config").startswith("Searched 1 of 1 archived pieces.")


def test_pieces_are_embedded_off_the_compaction_thread() -> None:
    archive = Recall(FakeEmbed)
    archive.add([{"role": "user", "content": "config"}])
    assert archive._worker is not None
    archive._worker.join(5)
    assert archive.search("config").startswith("Searched 1 of 1 archived pieces.")
    pending = [{"role": "user", "content": "still embedding"}]
    archive._pieces.append(Piece("user", "late"))
    archive.add(pending)
    archive._worker.join(5)
    assert all(p.vector is not None for p in archive._pieces)


def test_recall_is_offered_only_once_something_is_archived(tmp_path: Path) -> None:
    ctx = ToolContext(workdir=tmp_path, embeddings=True, recall=_recall())
    named = [t["function"]["name"] for t in offer_code_search([], ctx, lambda: True)]
    assert named == [CODE_SEARCH_TOOL]
    assert ctx.recall is not None
    ctx.recall.add([{"role": "user", "content": "config"}])
    named = [t["function"]["name"] for t in offer_code_search([], ctx, lambda: True)]
    assert named == [CODE_SEARCH_TOOL, RECALL_TOOL]
    assert offer_code_search([], ctx, lambda: False) == []
    found = execute_tool(
        ToolCall("1", RECALL_TOOL, '{"query": "config"}'), workdir=tmp_path, context=ctx
    )
    assert found.startswith("Searched 1 of 1 archived pieces.")
    off = ToolContext(workdir=tmp_path)
    assert execute_tool(
        ToolCall("1", RECALL_TOOL, '{"query": "x"}'), workdir=tmp_path, context=off
    ) == ("error: recall needs the embeddings capability, which is off.")


def test_a_chat_archives_what_compaction_drops_and_its_note_names_recall(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (tmp_path / "big.py").write_text(BIG)
    history: list[dict[str, Any]] = [{"role": "system", "content": "You are a persona."}]
    for i in range(3):
        history += [
            {"role": "user", "content": f"old config question {i} " + "q" * 8000},
            {"role": "assistant", "content": f"old answer {i}"},
        ]
    client = Scripted(
        [*_reads(6), [call("read_file", "z", path="big.py", limit=BIG_LINES)]], tail=[]
    )
    options = TurnOptions(
        workdir=tmp_path,
        journal=tmp_path / "j.jsonl",
        system_prompt="You are a persona.",
        context_tokens=CTX,
    )
    monkeypatch.setattr(engine, "embeddings_answer", lambda: True)
    monkeypatch.setattr(tools, "embeddings_answer", lambda: True)
    ctx = ToolContext(workdir=tmp_path, embeddings=True, recall=_recall())
    list(run_turn(cast(VllmClient, client), history, "which helper?", options, turn=9, context=ctx))
    assert ctx.recall is not None
    assert "old config question 0" in ctx.recall.search("config")
    late = client.asked[-1]
    note = next(m for m in late["messages"] if is_note(m))
    assert RECALL_HINT.strip() in note["content"]
    assert RECALL_TOOL in [t["function"]["name"] for t in late["tools"]]
    monkeypatch.setattr(engine, "embeddings_answer", lambda: False)
    monkeypatch.setattr(tools, "embeddings_answer", lambda: False)
    quiet = Scripted(
        [*_reads(6), [call("read_file", "z", path="big.py", limit=BIG_LINES)]], tail=[]
    )
    list(run_turn(cast(VllmClient, quiet), history[:7], "again?", options, turn=10, context=ctx))
    note = next(m for m in quiet.asked[-1]["messages"] if is_note(m))
    assert "recall" not in note["content"]  # the server is down: the note never offers it
