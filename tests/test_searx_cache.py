"""One reader session answers a repeated SearXNG query from its own cache.

Contract: within one reader session (one `ReaderGate`), a query identical to an
earlier one once case and whitespace are ignored is answered without a request
to SearXNG, labelled `(cached)` as Brave's cached results are. A different
query, a new session, or a query whose first attempt failed asks SearXNG.
"""

from __future__ import annotations

from pathlib import Path

import httpx

from saddle.mcpclient import Approvals
from saddle.research import ReaderGate, Researcher

ROW = {"title": "Wine install", "url": "https://wiki.example/wine", "content": "install wine"}


class Backend:
    """A SearXNG that counts the queries it is asked."""

    def __init__(self, *statuses: int) -> None:
        self.queries: list[str] = []
        self.statuses = list(statuses)

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.queries.append(request.url.params["q"])
        status = self.statuses.pop(0) if self.statuses else 200
        return httpx.Response(status, json={"results": [ROW]})


def reader(tmp_path: Path, backend: Backend) -> Researcher:
    http = httpx.Client(transport=httpx.MockTransport(backend))
    return Researcher({}, Approvals(tmp_path / "a.json"), tmp_path, http=http)


def test_a_repeated_query_is_answered_from_the_cache_without_a_request(tmp_path: Path) -> None:
    backend = Backend()
    searcher, gate = reader(tmp_path, backend), ReaderGate()
    first = searcher._search("install wine linux", gate, None)
    again = searcher._search("  Install   WINE linux ", gate, None)
    assert backend.queries == ["install wine linux"]  # known-bad: a second request
    assert not first.startswith("(cached)")
    assert again == f"(cached)\n{first}"
    assert gate.check("fetch", {"url": ROW["url"]}) is None


def test_a_different_query_asks_searxng(tmp_path: Path) -> None:
    backend = Backend()
    searcher, gate = reader(tmp_path, backend), ReaderGate()
    searcher._search("install wine linux", gate, None)
    other = searcher._search("install wine macos", gate, None)
    assert backend.queries == ["install wine linux", "install wine macos"]
    assert not other.startswith("(cached)")


def test_a_new_reader_session_starts_with_an_empty_cache(tmp_path: Path) -> None:
    backend = Backend()
    searcher = reader(tmp_path, backend)
    searcher._search("install wine linux", ReaderGate(), None)
    fresh = searcher._search("install wine linux", ReaderGate(), None)
    assert len(backend.queries) == 2
    assert not fresh.startswith("(cached)")


def test_a_failed_search_is_not_cached(tmp_path: Path) -> None:
    backend = Backend(500)
    searcher, gate = reader(tmp_path, backend), ReaderGate()
    assert searcher._search("install wine linux", gate, None).startswith("error: ")
    retried = searcher._search("install wine linux", gate, None)
    assert len(backend.queries) == 2
    assert "https://wiki.example/wine" in retried
    assert not retried.startswith("(cached)")


def test_a_repeated_query_with_no_results_is_also_cached(tmp_path: Path) -> None:
    queries: list[str] = []

    def empty(request: httpx.Request) -> httpx.Response:
        queries.append(request.url.params["q"])
        return httpx.Response(200, json={"results": []})

    http = httpx.Client(transport=httpx.MockTransport(empty))
    searcher = Researcher({}, Approvals(tmp_path / "a.json"), tmp_path, http=http)
    gate = ReaderGate()
    searcher._search("obscure thing", gate, None)
    assert searcher._search("obscure thing", gate, None) == (
        "(cached)\n(the search returned no results)"
    )
    assert queries == ["obscure thing"]
