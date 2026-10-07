"""What the image probe learns is kept per server, never under a reused `id()` (#186).

`server_accepts_images`, `known_verdict`, `image_limit` and `learn_image_limit`
kept their facts under `client.server_key`, falling back to `id(client)`.
Python reuses an `id()` once its object is gone, so a client without a
`server_key` (the suite's scripted clients) could start with a dead client's
verdict or image limit, and a cached answer depended on garbage collection.

Known-bad: a keyless client with a probed one's `id()` starts unknown and is
asked. Known-good: a keyless client is asked
once while it lives; clients of one server share what it said; a client that
cannot be held weakly is asked each time, never answered from a stranger.
"""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any

import pytest

from saddle import vision
from saddle.vision import (
    PROBE_TEXT,
    image_limit,
    known_verdict,
    learn_image_limit,
    reset_cache,
    server_accepts_images,
)
from saddle.vllm import StreamToken

REFUSAL = "At most 2 image(s) may be provided in one prompt"


class Keyless:
    """A client with no `server_key`, as the suite's scripted clients are: it
    answers the probe with `answer` and counts the times it was asked."""

    def __init__(self, answer: str) -> None:
        self.answer = answer
        self.asked = 0

    def stream_chat(self, *_a: Any, **_k: Any) -> Iterator[StreamToken]:
        self.asked += 1
        yield StreamToken(stream="content", text=self.answer)


class Keyed(Keyless):
    server_key = "http://127.0.0.1:9/v1|m"


class Slotted:
    """A keyless client that cannot be held weakly."""

    __slots__ = ("asked",)

    def __init__(self) -> None:
        self.asked = 0

    def stream_chat(self, *_a: Any, **_k: Any) -> Iterator[StreamToken]:
        self.asked += 1
        yield StreamToken(stream="content", text=PROBE_TEXT)


@pytest.fixture(autouse=True)
def nothing_known() -> Iterator[None]:
    reset_cache()
    yield
    reset_cache()


def test_a_client_with_a_probed_clients_id_starts_unknown(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Python may give a new client a freed one's `id()`, when its allocator
    reuses the address; here every client gets the same one, so the collision
    is certain rather than up to the allocator."""
    monkeypatch.setattr(vision, "id", lambda _client: 7, raising=False)
    first = Keyless(PROBE_TEXT)
    assert server_accepts_images(first) is True
    assert learn_image_limit(first, REFUSAL) == 2
    second = Keyless("none")
    assert known_verdict(second) is None
    assert image_limit(second) is None
    assert server_accepts_images(second) is False
    assert second.asked == 1


def test_a_keyless_client_is_asked_once_while_it_lives() -> None:
    client = Keyless(PROBE_TEXT)
    assert server_accepts_images(client) is True
    assert server_accepts_images(client) is True
    assert known_verdict(client) is True
    assert client.asked == 1


def test_clients_of_one_server_share_what_it_said() -> None:
    first = Keyed(PROBE_TEXT)
    assert server_accepts_images(first) is True
    learn_image_limit(first, REFUSAL)
    second = Keyed("none")
    assert server_accepts_images(second) is True  # the server's answer, not asked again
    assert second.asked == 0
    assert image_limit(second) == 2


def test_a_client_that_cannot_be_held_weakly_is_asked_each_time() -> None:
    client = Slotted()
    assert server_accepts_images(client) is True
    assert server_accepts_images(client) is True
    assert client.asked == 2
    assert known_verdict(client) is None
    assert learn_image_limit(client, REFUSAL) == 2
    assert image_limit(client) is None
