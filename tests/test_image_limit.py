"""A server that shows at most N images per request gets at most N.

Live (a watched setup trial, vLLM serving with `--limit-mm-per-prompt
{"image": {"count": 1}}`): the second screenshot made every request carry
two or more images, the server refused each with HTTP 400 "At most 1
image(s) may be provided in one prompt", and the turn ended there.
`trim_screenshots` lets up to SCREENSHOTS_KEPT + SCREENSHOTS_SLACK pile up,
which suits a server with no such limit and not this one.

Contract: on that refusal the engine remembers the server's limit, elides
the oldest tool images until no more than the limit remain (the person's
own pictures stay), and sends the round again once; later requests to that
server respect the limit from the start. A refusal it cannot satisfy by
eliding, or any other refusal, still ends the turn with the server's error.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from pathlib import Path
from typing import Any, cast

import pytest

from saddle.engine import TurnOptions, run_turn
from saddle.events import ErrorEvent, Event
from saddle.memory import IMAGE_ELIDED, trim_screenshots
from saddle.tools import ToolContext
from saddle.vision import (
    PROBE_QUESTION,
    PROBE_TEXT,
    data_url,
    images_message,
    learn_image_limit,
    reset_cache,
    solid_png,
)
from saddle.vllm import StreamToken, ToolCall, VllmClient, VllmRequestError

RED = solid_png(16, 16, (255, 0, 0))
GREEN = solid_png(16, 16, (0, 255, 0))
BLUE = solid_png(16, 16, (0, 0, 255))
# vLLM's own words, as the client reports them (`server returned HTTP ...`).
REFUSAL = (
    'server returned HTTP 400: {"error":{"message":"At most {n} image(s) may be '
    'provided in one prompt. (parameter=image)","type":"BadRequestError",'
    '"param":"image","code":400}}'
)


@pytest.fixture(autouse=True)
def _fresh_cache() -> Iterator[None]:
    reset_cache()
    yield
    reset_cache()


def images_in(messages: list[dict[str, Any]]) -> list[str]:
    return [
        p["image_url"]["url"]
        for m in messages
        if isinstance(m.get("content"), list)
        for p in m["content"]
        if p.get("type") == "image_url"
    ]


def call(cid: str, path: str) -> ToolCall:
    return ToolCall(id=cid, name="read_file", arguments=json.dumps({"path": path}))


class LimitedServer:
    """Refuses, as vLLM does, any request carrying more than `limit` images.
    Answers the image probe as a model that reads would."""

    def __init__(self, rounds: list[list[Any]], *, limit: int, refusal: str = REFUSAL) -> None:
        self.rounds = list(rounds)
        self.limit = limit
        self.refusal = refusal
        self.server_key = "limited"
        self.sent: list[list[dict[str, Any]]] = []
        self.refused = 0

    def stream_chat(self, messages: Any, **kwargs: Any) -> Any:
        content = messages[0]["content"]
        if isinstance(content, list) and content[0].get("text") == PROBE_QUESTION:
            return iter([StreamToken(stream="content", text=f"It reads {PROBE_TEXT}.")])
        if len(images_in(messages)) > self.limit:
            self.refused += 1
            raise VllmRequestError(self.refusal.replace("{n}", str(self.limit)))
        self.sent.append([dict(m) for m in messages])
        return iter(self.rounds.pop(0) if self.rounds else [StreamToken("content", "done")])


def turn(server: LimitedServer, workdir: Path, text: str = "look") -> list[Event]:
    options = TurnOptions(workdir=workdir, journal=workdir / "j.jsonl")
    context = ToolContext(workdir=workdir, images=True)
    messages: list[dict[str, Any]] = []
    return list(
        run_turn(cast(VllmClient, server), messages, text, options, turn=1, context=context)
    )


def three_looks(tmp_path: Path) -> list[list[Any]]:
    for name, png in (("a.png", RED), ("b.png", GREEN), ("c.png", BLUE)):
        (tmp_path / name).write_bytes(png)
    return [[call("c1", "a.png")], [call("c2", "b.png")], [call("c3", "c.png")]]


def test_a_one_image_server_sees_only_the_newest_screenshot_and_the_turn_goes_on(
    tmp_path: Path,
) -> None:
    server = LimitedServer(three_looks(tmp_path), limit=1)
    events = turn(server, tmp_path)
    assert not [e for e in events if isinstance(e, ErrorEvent)]
    assert all(len(images_in(sent)) <= 1 for sent in server.sent)
    last = server.sent[-1]
    assert images_in(last) == [data_url("image/png", BLUE)]
    elided = [
        p["text"]
        for m in last
        if isinstance(m.get("content"), list)
        for p in m["content"]
        if p.get("text") == IMAGE_ELIDED
    ]
    assert len(elided) == 2


def test_the_limit_is_learned_once_and_kept_for_the_server(tmp_path: Path) -> None:
    server = LimitedServer(three_looks(tmp_path), limit=1)
    turn(server, tmp_path)
    assert server.refused == 1


def test_a_two_image_server_keeps_the_newest_two(tmp_path: Path) -> None:
    server = LimitedServer(three_looks(tmp_path), limit=2)
    turn(server, tmp_path)
    assert images_in(server.sent[-1]) == [data_url("image/png", GREEN), data_url("image/png", BLUE)]


def test_a_refusal_eliding_cannot_fix_ends_the_turn_with_the_server_s_words(
    tmp_path: Path,
) -> None:
    # The person's own two pictures are never elided, so nothing can bring it to one.
    shots = [tmp_path / "p1.png", tmp_path / "p2.png"]
    shots[0].write_bytes(RED)
    shots[1].write_bytes(BLUE)
    server = LimitedServer([], limit=1)
    options = TurnOptions(workdir=tmp_path, journal=tmp_path / "j.jsonl")
    context = ToolContext(workdir=tmp_path, images=True)
    events = list(
        run_turn(
            cast(VllmClient, server),
            [],
            "these two",
            options,
            turn=1,
            context=context,
            images=shots,
        )
    )
    errors = [e.message for e in events if isinstance(e, ErrorEvent)]
    assert errors
    assert "At most 1 image(s)" in errors[0]
    assert server.refused == 1


def test_any_other_refusal_is_not_retried(tmp_path: Path) -> None:
    other = 'server returned HTTP 400: {"error":{"message":"bad request"}}'
    server = LimitedServer(three_looks(tmp_path), limit=1, refusal=other)
    events = turn(server, tmp_path)
    errors = [e.message for e in events if isinstance(e, ErrorEvent)]
    assert errors == [other]
    assert server.refused == 1


def test_learning_reads_only_the_servers_own_sentence() -> None:
    class Client:
        server_key = "k"

    assert learn_image_limit(Client(), REFUSAL.replace("{n}", "3")) == 3
    assert learn_image_limit(Client(), "server returned HTTP 400: bad request") is None
    assert learn_image_limit(Client(), REFUSAL.replace("{n}", "0")) == 0


def test_trimming_to_a_limit_keeps_the_newest_and_the_person_s_pictures() -> None:
    person = {
        "role": "user",
        "content": [
            {"type": "text", "text": "mine"},
            {"type": "image_url", "image_url": {"url": "data:p"}},
        ],
    }
    shots = [images_message([(f"c{n}", f"s{n}.png", f"data:s{n}")]) for n in range(3)]
    messages: list[dict[str, Any]] = [person, *shots]
    assert trim_screenshots(messages, limit=2) == 2
    assert images_in(messages) == ["data:p", "data:s2"]
    assert trim_screenshots(messages, limit=2) == 0


def test_without_a_limit_trimming_is_what_it_was() -> None:
    shots = [images_message([(f"c{n}", f"s{n}.png", f"data:s{n}")]) for n in range(3)]
    assert trim_screenshots(shots) == 0
    assert len(images_in(shots)) == 3


def test_two_images_from_one_round_are_trimmed_within_their_message() -> None:
    # Two reads in one round arrive in one follow-up; the limit counts images, not messages.
    pair = images_message([("c1", "a.png", "data:a"), ("c2", "b.png", "data:b")])
    messages: list[dict[str, Any]] = [pair]
    assert trim_screenshots(messages, limit=1) == 1
    assert images_in(messages) == ["data:b"]
