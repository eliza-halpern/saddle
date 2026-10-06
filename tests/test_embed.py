"""`saddle.embed`: vectors from the local embeddings server, and the `embeddings` row.

Contract: queries and documents carry the model's task prefixes and images go
in bare; `embed` returns one vector per input in order, batching long inputs;
a server that cannot be reached, errs, or returns the wrong count raises
`EmbedError` and never reads as an empty result. The `embeddings` status row
names the server's model when it answers and why not when it does not, and
asks only the embeddings server.
"""

from __future__ import annotations

import base64
import json
from typing import Any

import httpx
import pytest

from saddle import embed
from saddle.capabilities import Switches, status
from saddle.embed import EmbedClient, EmbedError, cosine, document, image, query


def _server(
    health: dict[str, Any] | None = None, *, broken: bool = False, short: bool = False
) -> tuple[httpx.Client, list[dict[str, Any]]]:
    seen: list[dict[str, Any]] = []

    def handle(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/health":
            if health is None:
                return httpx.Response(503)
            return httpx.Response(200, json=health)
        body = json.loads(request.content)
        seen.append(body)
        if broken:
            return httpx.Response(500, json={"error": "boom"})
        items = body["input"][: -1 if short else None]
        return httpx.Response(
            200,
            json={"data": [{"index": i, "embedding": [float(i), 1.0]} for i in range(len(items))]},
        )

    return httpx.Client(transport=httpx.MockTransport(handle)), seen


def test_prefixes_follow_the_model_card_and_images_go_in_bare() -> None:
    assert query("code retrieval", "where is x") == "task: code retrieval | query: where is x"
    assert document("def x(): ...", "a.py") == "title: a.py | text: def x(): ..."
    assert document("text") == "title: none | text: text"
    assert image(b"\x89PNG") == {"image": base64.b64encode(b"\x89PNG").decode()}


def test_embed_returns_one_vector_per_input_in_order_across_batches(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(embed, "BATCH", 2)
    http, seen = _server()
    vectors = EmbedClient("http://e", http).embed(["a", "b", "c"], dimensions=256)
    assert vectors == [[0.0, 1.0], [1.0, 1.0], [0.0, 1.0]]
    assert [body["input"] for body in seen] == [["a", "b"], ["c"]]
    assert all(body["dimensions"] == 256 for body in seen)
    EmbedClient("http://e", http).embed(["d"])
    assert "dimensions" not in seen[-1]


@pytest.mark.parametrize("server", [{"broken": True}, {"short": True}])
def test_a_failing_or_short_reply_is_an_error_never_an_empty_result(
    server: dict[str, bool],
) -> None:
    http, _ = _server(broken=server.get("broken", False), short=server.get("short", False))
    with pytest.raises(EmbedError):
        EmbedClient("http://e", http).embed(["a", "b"])


def test_health_names_the_model_or_says_why_there_is_none() -> None:
    http, _ = _server({"model": "gemma", "dim": 768, "images": True})
    assert EmbedClient("http://e", http).health()["dim"] == 768
    for health in (None, {"model": "gemma"}):
        with pytest.raises(EmbedError):
            EmbedClient("http://e", _server(health)[0]).health()


def test_cosine_of_normalised_vectors() -> None:
    assert cosine([1.0, 0.0], [1.0, 0.0]) == 1.0
    assert cosine([1.0, 0.0], [0.0, 1.0]) == 0.0
    with pytest.raises(ValueError, match="longer"):
        cosine([1.0], [1.0, 0.0])


def test_the_server_url_comes_from_the_environment_or_the_default() -> None:
    assert embed.server_url({}) == embed.DEFAULT_URL
    assert embed.server_url({embed.URL_ENV: "http://h:1/"}) == "http://h:1"


def test_the_embeddings_row_names_the_model_or_why_it_is_unavailable() -> None:
    on = Switches(embeddings=True)
    up, _ = _server({"model": "gemma", "dim": 768, "images": True})
    row = next(r for r in status(on, up) if r.name == "embeddings")
    assert (row.state, row.reason) == ("on", "gemma, 768d, text and images")
    text_only, _ = _server({"model": "g", "dim": 256})
    assert next(r for r in status(on, text_only) if r.name == "embeddings").reason == (
        "g, 256d, text only"
    )
    down = next(r for r in status(on, _server()[0]) if r.name == "embeddings")
    assert down.state == "unavailable"
    assert "start tools/embed_sidecar.py" in down.reason
    off = next(r for r in status(Switches(), up) if r.name == "embeddings")
    assert off.state == "off"
