"""A reply cut at a limit leaves what it streamed on the record, labelled partial.

In a dogfood run, one reply streamed reasoning for 1,736 s (an estimated
69,166 tokens) with no content and no tool call, and the time limit cancelled
it. Its spend record said only that it was cut; the turn's proof record kept
the first 4,000 characters of the whole turn's reasoning, not tied to that
round, and the rest (274,213 characters) was gone, so what the reply was doing
when it was cut could not be read back. Contract: when a reply is cut (at the
time limit, or at a token cap), its spend record carries a sidecar with what
it streamed, bounded, with its lengths, labelled partial, redacted.
"""

from __future__ import annotations

import json
import time
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from test_reply_budget import FOREVER, Server, auto, fast_poll, repo

from saddle import engine
from saddle.engine import PARTIAL_ENDS
from saddle.journal import (
    attempt_sidecar_path,
    read_spans,
    redact_secrets,
    verify_journal,
)
from saddle.vllm import StreamToken

__all__ = ["fast_poll", "repo"]

SECRET = "pass" + "word=" + "hunter2"  # built from fragments for the leak guard


class Runaway:
    """A reply that never ends: a sentence of content, then numbered lines of
    reasoning going round in a circle, one of them secret-shaped."""

    def __init__(self) -> None:
        self.reasoning: list[str] = []

    def stream_chat(self, messages: Any, **_: Any) -> Iterator[Any]:
        yield StreamToken(stream="content", text="I will check the docstring. ")
        n = 0
        while True:
            if n >= 400:  # a burst first, so the text is long on any machine, then a trickle
                time.sleep(0.001)
            text = f"line {n}: hmm, maybe it means the whole docstring. "
            if n == 1:
                text += f"{SECRET} "
            self.reasoning.append(text)
            yield StreamToken(stream="reasoning", text=text)
            n += 1


def cut_spends(journal: Path) -> list[tuple[dict[str, Any], dict[str, Any] | None]]:
    """Each spend record's fields, and the sidecar sealed beside it (None if none)."""
    found = []
    for span in read_spans(journal):
        if span.name != "auto:spend":
            continue
        path = attempt_sidecar_path(journal, span.span_id)
        sealed = json.loads(path.read_text()) if span.attempt_hash else None
        found.append((json.loads(span.argv[1]), sealed))
    return found


@pytest.mark.usefixtures("fast_poll")
def test_a_reply_cut_at_the_time_limit_leaves_its_partial_text(repo: Path) -> None:
    server = Runaway()
    result = auto(repo, server, time_budget_s=0.4)
    assert result.reason.endswith(f"; {engine.CUT_AT_TIME}")
    assert verify_journal(result.journal) == []
    [(spend, sealed)] = cut_spends(result.journal)
    assert spend["cut"] == engine.CUT_AT_TIME
    assert sealed is not None
    assert (sealed["partial"], sealed["cut"]) == (True, engine.CUT_AT_TIME)
    reasoning = sealed["reasoning"]
    streamed = "".join(server.reasoning)[: reasoning["chars"]]
    assert reasoning["chars"] > 2 * PARTIAL_ENDS  # long enough that the middle is dropped
    shown = redact_secrets(streamed)
    assert reasoning["head"] == shown[:PARTIAL_ENDS]
    assert reasoning["tail"] == shown[-PARTIAL_ENDS:]
    assert reasoning["head"].startswith("line 0: hmm")
    assert SECRET not in json.dumps(sealed)
    assert "password=***" in reasoning["head"]
    assert sealed["content"] == {
        "chars": len("I will check the docstring. "),
        "head": "I will check the docstring. ",
        "tail": "",
    }


def test_a_reply_cut_at_the_token_cap_leaves_its_partial_text(repo: Path) -> None:
    server = Server([104, FOREVER, 0])
    result = auto(repo, server, token_budget=1_000)
    records = cut_spends(result.journal)
    ended, _ = records[0]
    assert "cut" not in ended
    assert records[0][1] is None  # a reply that ended by itself leaves no partial text
    cut = [(spend, sealed) for spend, sealed in records if "cut" in spend]
    assert cut
    for spend, sealed in cut:
        assert sealed is not None
        assert (sealed["partial"], sealed["cut"]) == (True, spend["cut"])
        assert sealed["reasoning"] == {"chars": 16, "head": "x" * 16, "tail": ""}
    assert verify_journal(result.journal) == []


@pytest.mark.parametrize(
    ("length", "head", "tail"),
    [
        (10, 10, 0),
        (PARTIAL_ENDS, PARTIAL_ENDS, 0),
        (PARTIAL_ENDS + 5, PARTIAL_ENDS, 5),
        (2 * PARTIAL_ENDS, PARTIAL_ENDS, PARTIAL_ENDS),
        (2 * PARTIAL_ENDS + 7, PARTIAL_ENDS, PARTIAL_ENDS),
    ],
)
def test_the_partial_text_keeps_both_ends_and_never_repeats(
    length: int, head: int, tail: int
) -> None:
    text = "".join(chr(ord("a") + i % 26) for i in range(length))
    sealed = engine.partial_reply("", text, engine.CUT_AT_TIME)["reasoning"]
    assert (sealed["chars"], len(sealed["head"]), len(sealed["tail"])) == (length, head, tail)
    assert text.startswith(sealed["head"])
    assert text.endswith(sealed["tail"])


def test_a_secret_across_the_two_ends_is_redacted() -> None:
    """The ends are cut from redacted text: a secret straddling the cut is
    redacted whole, never left as two harmless-looking halves."""
    text = "y" * (PARTIAL_ENDS - 6) + SECRET + " and on"
    assert "hunter2" in text[PARTIAL_ENDS:]  # the cut falls inside the secret
    sealed = engine.partial_reply("", text, engine.CUT_AT_TIME)["reasoning"]
    assert "hunter2" not in sealed["head"] + sealed["tail"]
    assert (sealed["head"] + sealed["tail"]).endswith("password=*** and on")
