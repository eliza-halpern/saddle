"""The chat server's HTTP and SSE layer.

This layer had no tests at all until now, and it is where two real defects
already shipped and were caught by hand rather than by anything in the repo:
a single shared queue that split one event stream between two tabs, and a
multimodal message that rendered as `[object Object]`. Both are pinned here.

The security-shaped properties get an accepted instance *and* a refused one,
because a boundary tested only against the happy path is a boundary nobody
has checked: an upload may not write outside its session, and a message may
not attach a file the session never uploaded.
"""

from __future__ import annotations

import json
import queue
import threading
from contextlib import contextmanager
from pathlib import Path
from typing import Any

import pytest
from starlette.testclient import TestClient

from saddle.events import ContentDelta, ErrorEvent, TurnEnd
from saddle.sessions import SessionStore
from saddle.web.app import ChatServer, Live, build_app


class FakeClient:
    """Stands in for VllmClient: records the turn, emits scripted events."""

    def __init__(self, script: list[Any] | None = None, window: int | None = 200_000) -> None:
        self.script = script if script is not None else [ContentDelta(text="hi")]
        self.window = window
        self.closed = False

    def max_model_len(self) -> int | None:
        if isinstance(self.window, Exception):
            raise self.window
        return self.window

    def __enter__(self) -> FakeClient:
        return self

    def __exit__(self, *_: object) -> None:
        self.closed = True


@pytest.fixture
def store(tmp_path: Path) -> SessionStore:
    return SessionStore(tmp_path / "sessions")


@contextmanager
def app_for(store: SessionStore, tmp_path: Path, script: list[Any] | None = None) -> Any:
    """A live app whose turns are scripted rather than generated."""
    events = script if script is not None else [ContentDelta(text="hello")]

    def fake_run_turn(_client: Any, messages: list[dict[str, Any]], text: str,
                      _options: Any, **_kw: Any) -> Any:
        messages.append({"role": "user", "content": text})
        yield from events

    import saddle.web.app as module

    original = module.run_turn
    module.run_turn = fake_run_turn  # type: ignore[assignment]
    try:
        app = build_app(store, FakeClient, default_workdir=tmp_path)
        with TestClient(app) as client:
            yield client, app
    finally:
        module.run_turn = original  # type: ignore[assignment]


# -- Live: the fan-out that was once a single shared queue --------------------

def test_an_event_reaches_every_subscriber_not_just_the_first() -> None:
    live = Live()
    first, second = live.subscribe(), live.subscribe()
    live.publish(ContentDelta(text="x"))
    assert first.get_nowait().text == "x"
    assert second.get_nowait().text == "x"


def test_an_unsubscribed_client_stops_receiving() -> None:
    live = Live()
    channel = live.subscribe()
    live.unsubscribe(channel)
    live.publish(ContentDelta(text="x"))
    with pytest.raises(queue.Empty):
        channel.get_nowait()


def test_unsubscribing_twice_is_not_an_error() -> None:
    live = Live()
    channel = live.subscribe()
    live.unsubscribe(channel)
    live.unsubscribe(channel)
    assert live.subscribers == []


def test_publishing_to_nobody_is_not_an_error() -> None:
    Live().publish(ContentDelta(text="x"))


# -- the context window -------------------------------------------------------

def test_the_window_is_asked_for_once_and_then_remembered(tmp_path: Path) -> None:
    calls = []

    class Counting(FakeClient):
        def max_model_len(self) -> int | None:
            calls.append(1)
            return 150_000

    server = ChatServer(SessionStore(tmp_path), Counting, default_workdir=tmp_path)
    client = Counting()
    assert server._context_window(client) == 150_000
    assert server._context_window(client) == 150_000
    assert len(calls) == 1


def test_a_server_that_will_not_say_gets_a_conservative_default(tmp_path: Path) -> None:
    server = ChatServer(SessionStore(tmp_path), FakeClient, default_workdir=tmp_path)
    assert server._context_window(FakeClient(window=RuntimeError("no"))) == 120_000


def test_a_zero_window_is_treated_as_no_answer(tmp_path: Path) -> None:
    server = ChatServer(SessionStore(tmp_path), FakeClient, default_workdir=tmp_path)
    assert server._context_window(FakeClient(window=0)) == 120_000


# -- sessions -----------------------------------------------------------------

def test_a_session_is_created_listed_patched_and_deleted(
    store: SessionStore, tmp_path: Path
) -> None:
    with app_for(store, tmp_path) as (client, _app):
        made = client.post("/api/sessions", json={"title": "T"}).json()
        assert made["title"] == "T"
        assert [s["id"] for s in client.get("/api/sessions").json()] == [made["id"]]

        patched = client.patch(f"/api/sessions/{made['id']}", json={"title": "U"}).json()
        assert patched["title"] == "U"

        assert client.delete(f"/api/sessions/{made['id']}").json() == {"ok": True}
        assert client.get("/api/sessions").json() == []


def test_creating_a_session_with_no_body_uses_the_defaults(
    store: SessionStore, tmp_path: Path
) -> None:
    with app_for(store, tmp_path) as (client, _app):
        made = client.post("/api/sessions").json()
        assert made["title"] == "New session"
        assert made["persona"] == "engineer"
        assert made["workdir"] == str(tmp_path)


def test_moving_a_session_discards_its_cached_tool_context(
    store: SessionStore, tmp_path: Path
) -> None:
    # The context holds the workdir and its terminals; keeping it across a
    # move would run the next command in the old directory.
    with app_for(store, tmp_path) as (client, app):
        sid = client.post("/api/sessions").json()["id"]
        server = _server_of(app)
        server._live(sid).turn = 7
        client.patch(f"/api/sessions/{sid}", json={"workdir": str(tmp_path)})
        assert sid not in server.live


def test_deleting_a_session_discards_its_live_state(
    store: SessionStore, tmp_path: Path
) -> None:
    with app_for(store, tmp_path) as (client, app):
        sid = client.post("/api/sessions").json()["id"]
        server = _server_of(app)
        server._live(sid)
        client.delete(f"/api/sessions/{sid}")
        assert sid not in server.live


def test_messages_round_trip_over_http(store: SessionStore, tmp_path: Path) -> None:
    with app_for(store, tmp_path) as (client, _app):
        sid = client.post("/api/sessions").json()["id"]
        assert client.get(f"/api/sessions/{sid}/messages").json() == []
        store.save_messages(sid, [{"role": "user", "content": "x"}])
        assert client.get(f"/api/sessions/{sid}/messages").json()[0]["content"] == "x"


def test_the_persona_list_is_served(store: SessionStore, tmp_path: Path) -> None:
    with app_for(store, tmp_path) as (client, _app):
        assert "engineer" in client.get("/api/personas").json()


def test_the_index_page_is_served(store: SessionStore, tmp_path: Path) -> None:
    with app_for(store, tmp_path) as (client, _app):
        body = client.get("/").text
        assert "<title>" in body
        assert "markdown.js" in body


def _server_of(app: Any) -> ChatServer:
    """The ChatServer closed over by the route handlers."""
    for route in app.routes:
        handler = getattr(route, "endpoint", None)
        for cell in getattr(handler, "__closure__", None) or ():
            if isinstance(cell.cell_contents, ChatServer):
                return cell.cell_contents
    message = "no ChatServer found on the app"
    raise AssertionError(message)


# -- uploads: a client filename is never a path -------------------------------

def test_an_upload_lands_in_the_session_and_reports_its_size(
    store: SessionStore, tmp_path: Path
) -> None:
    with app_for(store, tmp_path) as (client, _app):
        sid = client.post("/api/sessions").json()["id"]
        reply = client.post(
            f"/api/sessions/{sid}/upload", files={"files": ("note.txt", b"hello")}
        ).json()
        assert reply["saved"] == [
            {
                "name": "note.txt",
                "path": str(store.uploads_dir(sid) / "note.txt"),
                "size": 5,
            }
        ]
        assert (store.uploads_dir(sid) / "note.txt").read_bytes() == b"hello"


def test_a_traversing_upload_filename_is_flattened_not_honoured(
    store: SessionStore, tmp_path: Path
) -> None:
    marker = tmp_path / "victim.txt"
    marker.write_text("original")
    with app_for(store, tmp_path) as (client, _app):
        sid = client.post("/api/sessions").json()["id"]
        reply = client.post(
            f"/api/sessions/{sid}/upload",
            files={"files": ("../../victim.txt", b"overwritten")},
        ).json()
        assert reply["saved"][0]["name"] == "victim.txt"
        assert Path(reply["saved"][0]["path"]).parent == store.uploads_dir(sid)
        assert marker.read_text() == "original"


def test_a_form_field_that_is_not_a_file_is_skipped(
    store: SessionStore, tmp_path: Path
) -> None:
    with app_for(store, tmp_path) as (client, _app):
        sid = client.post("/api/sessions").json()["id"]
        reply = client.post(f"/api/sessions/{sid}/upload", data={"files": "not-a-file"})
        assert reply.json() == {"saved": []}


# -- post_message -------------------------------------------------------------

def test_an_empty_message_is_refused(store: SessionStore, tmp_path: Path) -> None:
    with app_for(store, tmp_path) as (client, _app):
        sid = client.post("/api/sessions").json()["id"]
        reply = client.post(f"/api/sessions/{sid}/message", json={"text": "   "})
        assert reply.status_code == 400
        assert reply.json() == {"error": "empty message"}


def test_a_second_turn_is_refused_while_one_is_running(
    store: SessionStore, tmp_path: Path
) -> None:
    with app_for(store, tmp_path) as (client, app):
        sid = client.post("/api/sessions").json()["id"]
        live = _server_of(app)._live(sid)
        live.busy = True
        reply = client.post(f"/api/sessions/{sid}/message", json={"text": "hi"})
        assert reply.status_code == 409
        assert reply.json() == {"error": "a turn is already running"}


def test_an_image_this_session_uploaded_is_attached(
    store: SessionStore, tmp_path: Path
) -> None:
    seen: list[list[Path]] = []

    def capture(_c: Any, messages: list[dict[str, Any]], text: str, _o: Any,
                **kw: Any) -> Any:
        seen.append(list(kw.get("images") or ()))
        messages.append({"role": "user", "content": text})
        return iter(())

    import saddle.web.app as module

    with app_for(store, tmp_path) as (client, _app):
        sid = client.post("/api/sessions").json()["id"]
        good = store.uploads_dir(sid) / "shot.png"
        good.write_bytes(b"\x89PNG")
        module.run_turn = capture  # type: ignore[assignment]
        client.post(
            f"/api/sessions/{sid}/message", json={"text": "look", "images": [str(good)]}
        )
        _settle(lambda: bool(seen))
    assert seen[0] == [good]


def test_a_file_the_session_never_uploaded_is_not_attached(
    store: SessionStore, tmp_path: Path
) -> None:
    # The refusal half: a crafted request must not read an arbitrary path.
    secret = tmp_path / "id_rsa"
    secret.write_text("PRIVATE KEY")
    seen: list[list[Path]] = []

    def capture(_c: Any, messages: list[dict[str, Any]], text: str, _o: Any,
                **kw: Any) -> Any:
        seen.append(list(kw.get("images") or ()))
        messages.append({"role": "user", "content": text})
        return iter(())

    import saddle.web.app as module

    with app_for(store, tmp_path) as (client, _app):
        sid = client.post("/api/sessions").json()["id"]
        module.run_turn = capture  # type: ignore[assignment]
        client.post(
            f"/api/sessions/{sid}/message",
            json={"text": "read this", "images": [str(secret)]},
        )
        _settle(lambda: bool(seen))
    assert seen[0] == []


def test_a_missing_image_path_is_dropped_rather_than_raising(
    store: SessionStore, tmp_path: Path
) -> None:
    seen: list[list[Path]] = []

    def capture(_c: Any, messages: list[dict[str, Any]], text: str, _o: Any,
                **kw: Any) -> Any:
        seen.append(list(kw.get("images") or ()))
        messages.append({"role": "user", "content": text})
        return iter(())

    import saddle.web.app as module

    with app_for(store, tmp_path) as (client, _app):
        sid = client.post("/api/sessions").json()["id"]
        module.run_turn = capture  # type: ignore[assignment]
        client.post(
            f"/api/sessions/{sid}/message",
            json={"text": "x", "images": [str(store.uploads_dir(sid) / "gone.png")]},
        )
        _settle(lambda: bool(seen))
    assert seen[0] == []


# -- stop ---------------------------------------------------------------------

def test_stopping_an_idle_session_reports_that_nothing_was_running(
    store: SessionStore, tmp_path: Path
) -> None:
    with app_for(store, tmp_path) as (client, app):
        sid = client.post("/api/sessions").json()["id"]
        assert client.post(f"/api/sessions/{sid}/stop").json() == {"stopping": False}
        assert _server_of(app)._live(sid).cancelled is True


def test_stopping_a_running_session_reports_that_it_is_stopping(
    store: SessionStore, tmp_path: Path
) -> None:
    with app_for(store, tmp_path) as (client, app):
        sid = client.post("/api/sessions").json()["id"]
        _server_of(app)._live(sid).busy = True
        assert client.post(f"/api/sessions/{sid}/stop").json() == {"stopping": True}


def _settle(done: Any, timeout: float = 5.0) -> None:
    """Wait for a daemon turn thread to reach an observable state."""
    import time

    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if done():
            return
        time.sleep(0.01)
    message = "the turn never reached the expected state"
    raise AssertionError(message)


# -- the event stream ---------------------------------------------------------

# Starlette's TestClient collects a whole response before handing it back, so
# it can never read an endpoint that streams until the client goes away: it
# blocks in handle_request forever. These tests run the app under uvicorn on
# an ephemeral port and talk to it over real HTTP, which is also the only way
# `request.is_disconnected()` -- the stream's exit condition -- is exercised
# at all.

@contextmanager
def serving(app: Any) -> Any:
    import time

    import uvicorn

    config = uvicorn.Config(app, host="127.0.0.1", port=0, log_level="error")
    server = uvicorn.Server(config)
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    try:
        deadline = time.monotonic() + 10
        while not server.started and time.monotonic() < deadline:
            time.sleep(0.01)
        if not server.started:
            message = "uvicorn did not start"
            raise AssertionError(message)
        port = server.servers[0].sockets[0].getsockname()[1]
        yield f"http://127.0.0.1:{port}"
    finally:
        server.should_exit = True
        thread.join(timeout=10)


@contextmanager
def live_app(store: SessionStore, tmp_path: Path, script: list[Any] | None = None) -> Any:
    """A real server, plus a plain HTTP client pointed at it."""
    import httpx

    with app_for(store, tmp_path, script) as (_test_client, app):
        with serving(app) as base, httpx.Client(base_url=base, timeout=15) as client:
            yield client, app


def _frames(client: Any, sid: str, want: int, *, prime: Any = None) -> list[dict[str, Any]]:
    """Open the stream, collect `want` data frames, then disconnect."""
    got: list[dict[str, Any]] = []
    with client.stream("GET", f"/api/sessions/{sid}/events") as response:
        assert response.status_code == 200
        assert response.headers["content-type"].startswith("text/event-stream")
        assert response.headers["cache-control"] == "no-cache"
        assert response.headers["x-accel-buffering"] == "no"
        if prime is not None:
            prime()
        for line in response.iter_lines():
            if line.startswith("data: "):
                got.append(json.loads(line[6:]))
                if len(got) >= want:
                    return got
    return got


def test_the_stream_opens_with_everything_a_reloading_client_needs(
    store: SessionStore, tmp_path: Path
) -> None:
    with live_app(store, tmp_path) as (client, _app):
        sid = client.post("/api/sessions", json={"title": "Resumed"}).json()["id"]
        store.update(sid, reasoning_effort="low")
        store.save_messages(sid, [{"role": "user", "content": "earlier"}])
        info = _frames(client, sid, 1)[0]

    assert info["kind"] == "session.info"
    assert info["title"] == "Resumed"
    # The effort was declared on SessionInfo but never set, so a reload always
    # showed "xhigh" whatever the session was actually using.
    assert info["reasoning_effort"] == "low"
    # The meter stayed blank until the first turn ended.
    assert info["context_used"] > 0
    assert info["context_limit"] > info["context_used"]
    assert info["messages"][0]["content"] == "earlier"


def test_two_clients_see_the_same_stream_not_half_each(
    store: SessionStore, tmp_path: Path
) -> None:
    # The defect this pins: one shared queue hands each event to exactly one
    # consumer, so a second tab splits the conversation with the first.
    import httpx

    with live_app(store, tmp_path) as (client, app):
        sid = client.post("/api/sessions").json()["id"]
        live = _server_of(app)._live(sid)
        collected: dict[int, list[dict[str, Any]]] = {}
        base = str(client.base_url)

        def reader(index: int) -> None:
            with httpx.Client(base_url=base, timeout=15) as own:
                collected[index] = _frames(own, sid, 4)

        threads = [threading.Thread(target=reader, args=(i,)) for i in (0, 1)]
        for thread in threads:
            thread.start()
        _settle(lambda: len(live.subscribers) == 2)
        for text in ("alpha", "beta", "gamma"):
            live.publish(ContentDelta(text=text))
        for thread in threads:
            thread.join(timeout=15)

    assert [f["text"] for f in collected[0][1:]] == ["alpha", "beta", "gamma"]
    assert [f["text"] for f in collected[1][1:]] == ["alpha", "beta", "gamma"]


def test_the_end_of_a_turn_is_announced_as_idle(
    store: SessionStore, tmp_path: Path
) -> None:
    with live_app(store, tmp_path) as (client, app):
        sid = client.post("/api/sessions").json()["id"]
        live = _server_of(app)._live(sid)

        def prime() -> None:
            _settle(lambda: bool(live.subscribers))
            live.publish(None)

        frames = _frames(client, sid, 2, prime=prime)
    assert frames[1] == {"kind": "idle"}


def test_an_idle_stream_is_kept_alive_rather_than_left_silent(
    store: SessionStore, tmp_path: Path
) -> None:
    with live_app(store, tmp_path) as (client, _app):
        sid = client.post("/api/sessions").json()["id"]
        with client.stream("GET", f"/api/sessions/{sid}/events") as response:
            seen = []
            for line in response.iter_lines():
                seen.append(line)
                if line.startswith(":"):
                    break
    assert seen[-1] == ": keepalive"


def test_a_client_that_goes_away_stops_being_a_subscriber(
    store: SessionStore, tmp_path: Path
) -> None:
    with live_app(store, tmp_path) as (client, app):
        sid = client.post("/api/sessions").json()["id"]
        live = _server_of(app)._live(sid)
        _frames(client, sid, 1)
        _settle(lambda: live.subscribers == [], timeout=10)
    assert live.subscribers == []


def test_a_turn_streams_to_a_connected_browser_end_to_end(
    store: SessionStore, tmp_path: Path
) -> None:
    with live_app(store, tmp_path, script=[ContentDelta(text="streamed")]) as (client, app):
        sid = client.post("/api/sessions").json()["id"]
        live = _server_of(app)._live(sid)

        def prime() -> None:
            _settle(lambda: bool(live.subscribers))
            client.post(f"/api/sessions/{sid}/message", json={"text": "go"})

        frames = _frames(client, sid, 3, prime=prime)
    assert [f["kind"] for f in frames] == ["session.info", "content.delta", "idle"]
    assert frames[1]["text"] == "streamed"


# -- the turn runner ----------------------------------------------------------

def test_a_turn_publishes_its_events_saves_its_messages_and_signals_the_end(
    store: SessionStore, tmp_path: Path
) -> None:
    with app_for(store, tmp_path, script=[ContentDelta(text="done")]) as (client, app):
        sid = client.post("/api/sessions").json()["id"]
        server = _server_of(app)
        live = server._live(sid)
        channel = live.subscribe()
        server._run(sid, "ping")

        assert channel.get_nowait().text == "done"
        assert channel.get_nowait() is None          # the turn is over
        assert live.busy is False
        assert store.load_messages(sid)[-1]["content"] == "ping"


def test_a_turn_that_raises_reports_the_error_instead_of_killing_the_server(
    store: SessionStore, tmp_path: Path
) -> None:
    def explode(*_a: Any, **_k: Any) -> Any:
        message = "model went away"
        raise RuntimeError(message)

    import saddle.web.app as module

    with app_for(store, tmp_path) as (client, app):
        sid = client.post("/api/sessions").json()["id"]
        server = _server_of(app)
        channel = server._live(sid).subscribe()
        module.run_turn = explode  # type: ignore[assignment]
        server._run(sid, "ping")

        first = channel.get_nowait()
        assert isinstance(first, ErrorEvent)
        assert first.message == "RuntimeError: model went away"
        assert channel.get_nowait() is None          # and the end is still signalled
        assert server._live(sid).busy is False


def test_a_sealed_turn_becomes_the_parent_of_the_next(
    store: SessionStore, tmp_path: Path
) -> None:
    with app_for(store, tmp_path, script=[TurnEnd(turn=1, proof="proof-abc")]) as (
        client,
        app,
    ):
        sid = client.post("/api/sessions").json()["id"]
        server = _server_of(app)
        server._run(sid, "ping")
        assert server._live(sid).parent == "proof-abc"


def test_the_turn_counter_advances_per_session(
    store: SessionStore, tmp_path: Path
) -> None:
    with app_for(store, tmp_path) as (client, app):
        sid = client.post("/api/sessions").json()["id"]
        server = _server_of(app)
        server._run(sid, "one")
        server._run(sid, "two")
        assert server._live(sid).turn == 2


def test_a_stale_cancellation_does_not_kill_the_next_turn(
    store: SessionStore, tmp_path: Path
) -> None:
    with app_for(store, tmp_path) as (client, app):
        sid = client.post("/api/sessions").json()["id"]
        server = _server_of(app)
        server._live(sid).cancelled = True           # a previous turn was stopped
        server._run(sid, "ping")
        assert server._live(sid).cancelled is False


def test_a_tool_context_is_reused_across_turns_but_not_across_a_move(
    store: SessionStore, tmp_path: Path
) -> None:
    other = tmp_path / "elsewhere"
    other.mkdir()
    with app_for(store, tmp_path) as (client, app):
        sid = client.post("/api/sessions").json()["id"]
        server = _server_of(app)
        server._run(sid, "one")
        first = server._live(sid).context
        server._run(sid, "two")
        assert server._live(sid).context is first    # terminals survive a turn

        store.update(sid, workdir=str(other))
        server._run(sid, "three")
        moved = server._live(sid).context
        assert moved is not first
        assert moved is not None
        assert moved.workdir == other


def test_terminal_output_reaches_subscribers_after_its_tool_call_returned(
    store: SessionStore, tmp_path: Path
) -> None:
    # Background output arrives on a reader thread, long after the tool call
    # that started it has already been sealed, so it cannot be yielded by the
    # turn and has to be published to the session instead.
    with app_for(store, tmp_path) as (client, app):
        sid = client.post("/api/sessions").json()["id"]
        server = _server_of(app)
        server._run(sid, "one")
        live = server._live(sid)
        channel = live.subscribe()
        assert live.context is not None
        assert live.context.on_output is not None
        live.context.on_output("term-1", "tick")

        pushed = channel.get_nowait()
        assert pushed.kind == "terminal.output"
        assert (pushed.id, pushed.chunk) == ("term-1", "tick")


# -- the folder picker --------------------------------------------------------

def test_browsing_lists_only_subdirectories(store: SessionStore, tmp_path: Path) -> None:
    root = tmp_path / "root"
    (root / "beta").mkdir(parents=True)
    (root / "Alpha").mkdir()
    (root / ".hidden").mkdir()
    (root / "a-file.txt").write_text("x")
    with app_for(store, tmp_path) as (client, _app):
        body = client.get("/api/browse", params={"path": str(root)}).json()

    assert body["path"] == str(root.resolve())
    assert body["parent"] == str(root.resolve().parent)
    # Sorted case-insensitively; files and dotfiles are not places to work.
    assert [e["name"] for e in body["entries"]] == ["Alpha", "beta"]
    assert body["entries"][0]["path"] == str(root / "Alpha")


def test_browsing_a_file_is_an_error_rather_than_an_empty_listing(
    store: SessionStore, tmp_path: Path
) -> None:
    target = tmp_path / "not-a-dir.txt"
    target.write_text("x")
    with app_for(store, tmp_path) as (client, _app):
        reply = client.get("/api/browse", params={"path": str(target)})
    assert reply.status_code == 400
    assert reply.json() == {"error": f"not a directory: {target}"}


def test_browsing_with_no_path_starts_at_home(store: SessionStore, tmp_path: Path) -> None:
    with app_for(store, tmp_path) as (client, _app):
        body = client.get("/api/browse").json()
    assert body["path"] == str(Path.home().resolve())


def test_a_tilde_is_expanded(store: SessionStore, tmp_path: Path) -> None:
    with app_for(store, tmp_path) as (client, _app):
        body = client.get("/api/browse", params={"path": "~"}).json()
    assert body["path"] == str(Path.home().resolve())


def test_a_huge_directory_is_truncated_rather_than_streamed_whole(
    store: SessionStore, tmp_path: Path
) -> None:
    root = tmp_path / "many"
    root.mkdir()
    for index in range(420):
        (root / f"d{index:04d}").mkdir()
    with app_for(store, tmp_path) as (client, _app):
        body = client.get("/api/browse", params={"path": str(root)}).json()
    assert len(body["entries"]) == 400


# -- the entry point ----------------------------------------------------------

def test_serve_builds_an_app_and_hands_it_to_uvicorn(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import uvicorn

    from saddle.web import app as module

    handed: dict[str, Any] = {}

    def fake_run(app: Any, **kwargs: Any) -> None:
        handed["app"] = app
        handed["kwargs"] = kwargs

    monkeypatch.setattr(uvicorn, "run", fake_run)
    module.serve(
        host="127.0.0.1",
        port=9999,
        api_key="unused-in-this-test",
        base_url="http://example.invalid/v1",
        model="a-model",
        workdir=tmp_path,
        sessions_root=tmp_path / "sessions",
    )

    assert handed["kwargs"]["host"] == "127.0.0.1"
    assert handed["kwargs"]["port"] == 9999
    # The client is built per turn, so serve must not have connected to the
    # model just to start the server.
    assert _server_of(handed["app"]).default_workdir == tmp_path


# -- disconnect ---------------------------------------------------------------

def _endpoint(app: Any, path: str) -> Any:
    for route in app.routes:
        if getattr(route, "path", None) == path:
            return route.endpoint
    message = f"no route for {path}"
    raise AssertionError(message)


def test_a_disconnected_client_ends_its_stream_and_frees_its_queue(
    store: SessionStore, tmp_path: Path
) -> None:
    # Over real HTTP the disconnect is a race; driven directly it is exact.
    # The contract: the loop notices, stops, and drops the subscriber, so a
    # closed tab does not leave a queue filling forever behind it.
    import asyncio

    from starlette.requests import Request

    with app_for(store, tmp_path) as (client, app):
        sid = client.post("/api/sessions").json()["id"]
        live = _server_of(app)._live(sid)
        events = _endpoint(app, "/api/sessions/{sid}/events")

        async def receive() -> dict[str, str]:
            return {"type": "http.disconnect"}

        async def drive() -> list[str]:
            request = Request(
                {
                    "type": "http",
                    "method": "GET",
                    "path": f"/api/sessions/{sid}/events",
                    "headers": [],
                    "query_string": b"",
                    "path_params": {"sid": sid},
                },
                receive,
            )
            response = await events(request)
            return [chunk async for chunk in response.body_iterator]

        chunks = asyncio.run(drive())

    assert len(chunks) == 1                       # the opening frame, then it stops
    assert json.loads(chunks[0][6:])["kind"] == "session.info"
    assert live.subscribers == []                 # and nothing is left listening


def test_the_client_factory_builds_a_real_client_per_turn(tmp_path: Path) -> None:
    import uvicorn

    from saddle.vllm import VllmClient
    from saddle.web import app as module

    handed: dict[str, Any] = {}
    original = uvicorn.run
    uvicorn.run = lambda app, **kw: handed.update(app=app)  # type: ignore[assignment]
    try:
        module.serve(
            host="127.0.0.1",
            port=9999,
            api_key="unused-in-this-test",
            base_url="http://example.invalid/v1",
            model="a-model",
            workdir=tmp_path,
            sessions_root=tmp_path / "sessions",
        )
    finally:
        uvicorn.run = original  # type: ignore[assignment]

    built = _server_of(handed["app"]).client_factory()
    assert isinstance(built, VllmClient)
