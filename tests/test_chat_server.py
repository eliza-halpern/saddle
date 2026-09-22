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

from saddle.events import (
    ContentDelta,
    ErrorEvent,
    Event,
    SessionTitle,
    TerminalOutput,
    TurnEnd,
)
from saddle.sessions import SessionStore
from saddle.web.app import ChatServer, Live, build_app


def one[E: Event](event: object, cls: type[E]) -> E:
    """Narrow one published event, asserting that is what it is.

    A channel carries `Event | None` -- None is the end-of-turn signal --
    so reading a subclass field off what comes out needs the narrowing, and
    stating the expected type is a stronger assertion than reading `.kind`.
    """
    assert isinstance(event, cls), f"expected {cls.__name__}, got {type(event).__name__}"
    return event


class FakeClient:
    """Stands in for VllmClient: records the turn, emits scripted events."""

    def __init__(
        self, script: list[Any] | None = None, window: int | BaseException | None = 200_000
    ) -> None:
        self.script = script if script is not None else [ContentDelta(text="hi")]
        self.window = window
        self.closed = False

    def max_model_len(self) -> int | None:
        if isinstance(self.window, BaseException):
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

    def fake_run_turn(
        _client: Any, messages: list[dict[str, Any]], text: str, _options: Any, **_kw: Any
    ) -> Any:
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
        module.run_turn = original


# -- Live: the fan-out that was once a single shared queue --------------------


def test_an_event_reaches_every_subscriber_not_just_the_first() -> None:
    live = Live()
    first, second = live.subscribe(), live.subscribe()
    live.publish(ContentDelta(text="x"))
    assert one(first.get_nowait(), ContentDelta).text == "x"
    assert one(second.get_nowait(), ContentDelta).text == "x"


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


def test_deleting_a_session_discards_its_live_state(store: SessionStore, tmp_path: Path) -> None:
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


def test_the_persona_list_says_which_ones_may_be_edited(
    store: SessionStore, tmp_path: Path
) -> None:
    # The editor has to distinguish "yours, delete it" from "built-in,
    # reset it", and it cannot infer that from the table alone.
    with app_for(store, tmp_path) as (client, _app):
        body = client.get("/api/personas").json()
        assert "engineer" in body["personas"]
        assert "engineer" in body["builtin"]
        assert body["editable"] == []

        client.put("/api/personas/pirate", json={"prompt": "Arr."})
        body = client.get("/api/personas").json()
        assert body["personas"]["pirate"] == "Arr."
        assert body["editable"] == ["pirate"]
        assert "pirate" not in body["builtin"]


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


def test_a_form_field_that_is_not_a_file_is_skipped(store: SessionStore, tmp_path: Path) -> None:
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


def test_a_second_turn_is_refused_while_one_is_running(store: SessionStore, tmp_path: Path) -> None:
    with app_for(store, tmp_path) as (client, app):
        sid = client.post("/api/sessions").json()["id"]
        live = _server_of(app)._live(sid)
        live.busy = True
        reply = client.post(f"/api/sessions/{sid}/message", json={"text": "hi"})
        assert reply.status_code == 409
        assert reply.json() == {"error": "a turn is already running"}


def test_an_image_this_session_uploaded_is_attached(store: SessionStore, tmp_path: Path) -> None:
    seen: list[list[Path]] = []

    def capture(_c: Any, messages: list[dict[str, Any]], text: str, _o: Any, **kw: Any) -> Any:
        seen.append(list(kw.get("images") or ()))
        messages.append({"role": "user", "content": text})
        return iter(())

    import saddle.web.app as module

    with app_for(store, tmp_path) as (client, _app):
        sid = client.post("/api/sessions").json()["id"]
        good = store.uploads_dir(sid) / "shot.png"
        good.write_bytes(b"\x89PNG")
        module.run_turn = capture  # type: ignore[assignment]
        client.post(f"/api/sessions/{sid}/message", json={"text": "look", "images": [str(good)]})
        _settle(lambda: bool(seen))
    assert seen[0] == [good]


def test_a_file_the_session_never_uploaded_is_not_attached(
    store: SessionStore, tmp_path: Path
) -> None:
    # The refusal half: a crafted request must not read an arbitrary path.
    secret = tmp_path / "id_rsa"
    secret.write_text("PRIVATE KEY")
    seen: list[list[Path]] = []

    def capture(_c: Any, messages: list[dict[str, Any]], text: str, _o: Any, **kw: Any) -> Any:
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

    def capture(_c: Any, messages: list[dict[str, Any]], text: str, _o: Any, **kw: Any) -> Any:
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


def test_two_clients_see_the_same_stream_not_half_each(store: SessionStore, tmp_path: Path) -> None:
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


def test_the_end_of_a_turn_is_announced_as_idle(store: SessionStore, tmp_path: Path) -> None:
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

        frames = _frames(client, sid, 4, prime=prime)
    assert [f["kind"] for f in frames] == ["session.info", "content.delta", "session.title", "idle"]
    assert frames[1]["text"] == "streamed"
    assert frames[2]["title"] == "go"


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

        assert one(channel.get_nowait(), ContentDelta).text == "done"
        # The session names itself after the answer is already on screen, and
        # before the end signal. FakeClient cannot be asked, so the name falls
        # back to the user's own words.
        named = one(channel.get_nowait(), SessionTitle)
        assert named.title == "ping"
        assert channel.get_nowait() is None  # the turn is over
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
        module.run_turn = explode
        server._run(sid, "ping")

        first = channel.get_nowait()
        assert isinstance(first, ErrorEvent)
        assert first.message == "RuntimeError: model went away"
        assert channel.get_nowait() is None  # and the end is still signalled
        assert server._live(sid).busy is False


def test_a_sealed_turn_becomes_the_parent_of_the_next(store: SessionStore, tmp_path: Path) -> None:
    with app_for(store, tmp_path, script=[TurnEnd(turn=1, proof="proof-abc")]) as (
        client,
        app,
    ):
        sid = client.post("/api/sessions").json()["id"]
        server = _server_of(app)
        server._run(sid, "ping")
        assert server._live(sid).parent == "proof-abc"


def test_the_turn_counter_advances_per_session(store: SessionStore, tmp_path: Path) -> None:
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
        server._live(sid).cancelled = True  # a previous turn was stopped
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
        assert server._live(sid).context is first  # terminals survive a turn

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

        pushed = one(channel.get_nowait(), TerminalOutput)
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

    assert len(chunks) == 1  # the opening frame, then it stops
    assert json.loads(chunks[0][6:])["kind"] == "session.info"
    assert live.subscribers == []  # and nothing is left listening


def test_the_client_factory_builds_a_real_client_per_turn(tmp_path: Path) -> None:
    import uvicorn

    from saddle.vllm import VllmClient
    from saddle.web import app as module

    handed: dict[str, Any] = {}
    original = uvicorn.run
    uvicorn.run = lambda app, **kw: handed.update(app=app)
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
        uvicorn.run = original

    built = _server_of(handed["app"]).client_factory()
    assert isinstance(built, VllmClient)


# -- naming a session ---------------------------------------------------------


class Naming(FakeClient):
    """A client that also answers the titling call."""

    answer: Any = "Slow Rust Build"

    def complete(self, _prompt: str, /, **_kw: Any) -> str:
        if isinstance(Naming.answer, BaseException):
            raise Naming.answer
        return str(Naming.answer)


@contextmanager
def naming_app(store: SessionStore, tmp_path: Path, answer: Any = "Slow Rust Build") -> Any:
    previous = Naming.answer
    Naming.answer = answer
    import saddle.web.app as module

    original = module.run_turn

    def fake_run_turn(
        _c: Any, messages: list[dict[str, Any]], text: str, _o: Any, **_kw: Any
    ) -> Any:
        messages.append({"role": "user", "content": text})
        yield ContentDelta(text="answered")

    module.run_turn = fake_run_turn  # type: ignore[assignment]
    try:
        app = build_app(store, Naming, default_workdir=tmp_path)
        with TestClient(app) as client:
            yield client, app
    finally:
        module.run_turn = original
        Naming.answer = previous


def test_the_first_turn_names_its_session_and_tells_every_tab(
    store: SessionStore, tmp_path: Path
) -> None:
    with naming_app(store, tmp_path) as (client, app):
        sid = client.post("/api/sessions").json()["id"]
        assert store.get(sid).title == "New session"
        channel = _server_of(app)._live(sid).subscribe()
        _server_of(app)._run(sid, "why is my rust build taking 4 minutes")

        assert store.get(sid).title == "Slow Rust Build"
        published = [channel.get_nowait() for _ in range(channel.qsize())]
        titled = next(e for e in published if isinstance(e, SessionTitle))
        assert titled.title == "Slow Rust Build"
        assert titled.session_id == sid


def test_a_later_turn_does_not_rename_the_session(store: SessionStore, tmp_path: Path) -> None:
    with naming_app(store, tmp_path) as (client, app):
        sid = client.post("/api/sessions").json()["id"]
        server = _server_of(app)
        server._run(sid, "first question")
        Naming.answer = "A Completely Different Name"
        server._run(sid, "second question")
        assert store.get(sid).title == "Slow Rust Build"


def test_a_name_the_user_chose_is_never_overwritten(store: SessionStore, tmp_path: Path) -> None:
    with naming_app(store, tmp_path) as (client, app):
        sid = client.post("/api/sessions").json()["id"]
        client.patch(f"/api/sessions/{sid}", json={"title": "My own name"})
        assert store.get(sid).auto_title is False
        _server_of(app)._run(sid, "why is my rust build slow")
        assert store.get(sid).title == "My own name"


def test_a_titling_failure_leaves_the_users_words_and_does_not_break_the_turn(
    store: SessionStore, tmp_path: Path
) -> None:
    with naming_app(store, tmp_path, answer=RuntimeError("model is down")) as (client, app):
        sid = client.post("/api/sessions").json()["id"]
        channel = _server_of(app)._live(sid).subscribe()
        _server_of(app)._run(sid, "why is my rust build slow")

        assert store.get(sid).title == "why is my rust build slow"
        published = [channel.get_nowait() for _ in range(channel.qsize())]
        assert not any(e is not None and e.kind == "error" for e in published)
        assert any(e is not None and e.kind == "content.delta" for e in published)


def test_a_turn_that_raises_is_not_titled(store: SessionStore, tmp_path: Path) -> None:
    # Naming happens after a turn that worked; a turn that died has nothing
    # to name and the error is what matters.
    import saddle.web.app as module

    def explode(*_a: Any, **_k: Any) -> Any:
        message = "the model went away"
        raise RuntimeError(message)

    with naming_app(store, tmp_path) as (client, app):
        sid = client.post("/api/sessions").json()["id"]
        module.run_turn = explode
        _server_of(app)._run(sid, "why is my rust build slow")
        assert store.get(sid).title == "New session"


def test_a_renamed_session_reports_its_own_name_on_reconnect(
    store: SessionStore, tmp_path: Path
) -> None:
    with naming_app(store, tmp_path) as (client, app):
        sid = client.post("/api/sessions").json()["id"]
        _server_of(app)._run(sid, "why is my rust build slow")
        assert client.get("/api/sessions").json()[0]["title"] == "Slow Rust Build"


def test_a_session_is_offered_a_name_once_not_on_every_later_turn(
    store: SessionStore, tmp_path: Path
) -> None:
    # Turn 1 has nothing to summarise, so no name sticks and auto_title stays
    # true. The guard that matters is the turn number: without it, every
    # later turn would spend a model call retrying a name for a session that
    # already declined one.
    asked: list[str] = []

    class Counting(Naming):
        def complete(self, prompt: str, /, **_kw: Any) -> str:
            asked.append(prompt)
            return "A Late Name"

    import saddle.web.app as module

    original = module.run_turn

    def fake_run_turn(
        _c: Any, messages: list[dict[str, Any]], text: str, _o: Any, **_kw: Any
    ) -> Any:
        messages.append({"role": "user", "content": text})
        yield ContentDelta(text="answered")

    module.run_turn = fake_run_turn  # type: ignore[assignment]
    try:
        app = build_app(store, Counting, default_workdir=tmp_path)
        with TestClient(app) as client:
            sid = client.post("/api/sessions").json()["id"]
            server = _server_of(app)
            server._run(sid, "   ")  # nothing to summarise
            assert store.get(sid).auto_title is True
            assert asked == []  # and no call was spent
            server._run(sid, "a real question this time")
    finally:
        module.run_turn = original

    assert asked == []
    assert store.get(sid).title == "New session"


def test_a_store_that_cannot_write_the_name_still_lets_the_turn_finish(
    store: SessionStore, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # title_for swallows the model's failures; this is the other half -- the
    # model answered but the name could not be saved. A read-only disk may
    # not turn a working answer into a failed turn.
    def refuse(*_a: Any, **_k: Any) -> None:
        message = "read-only file system"
        raise OSError(message)

    with naming_app(store, tmp_path) as (client, app):
        sid = client.post("/api/sessions").json()["id"]
        channel = _server_of(app)._live(sid).subscribe()
        monkeypatch.setattr(store, "update", refuse)
        _server_of(app)._run(sid, "why is my rust build slow")

        published = [channel.get_nowait() for _ in range(channel.qsize())]
        assert any(e is not None and e.kind == "content.delta" for e in published)
        assert not any(e is not None and e.kind == "error" for e in published)
        # No name was published, because no name was stored: the event and
        # the record may not disagree.
        assert not any(e is not None and e.kind == "session.title" for e in published)
        assert published[-1] is None  # and the turn still ended


# -- history that survives a reconnect ----------------------------------------


def test_stored_tool_calls_come_back_labelled_so_a_reconnect_keeps_them(
    store: SessionStore, tmp_path: Path
) -> None:
    # An EventSource reconnects on its own and every connect re-sends
    # session.info. History used to carry only user and assistant text, so
    # each reconnect silently deleted the tool rows the reader had just
    # watched appear. The calls were stored all along; the labels were not.
    from saddle.web.app import history_for_display

    (tmp_path / "note.txt").write_text("x")
    messages = [
        {"role": "user", "content": "read it"},
        {
            "role": "assistant",
            "content": "",
            "tool_calls": [
                {
                    "id": "a",
                    "type": "function",
                    "function": {"name": "read_file", "arguments": '{"path": "note.txt"}'},
                },
                {
                    "id": "b",
                    "type": "function",
                    "function": {"name": "read_file", "arguments": '{"path": "gone.txt"}'},
                },
            ],
        },
        {"role": "tool", "tool_call_id": "a", "content": "x"},
        {"role": "tool", "tool_call_id": "b", "content": "error: cannot read 'gone.txt'"},
        {"role": "assistant", "content": "done"},
    ]
    shown = history_for_display(messages, tmp_path)
    rows = shown[1]["tools"]

    assert [r["label"] for r in rows] == ["Read note.txt", "Failed to read gone.txt"]
    assert [r["ok"] for r in rows] == [True, False]
    assert rows[0]["detail"] == "x"
    # Messages with no calls are passed through untouched.
    assert "tools" not in shown[0]
    assert shown[-1]["content"] == "done"


def test_a_call_whose_result_is_missing_is_not_reported_as_a_failure(
    store: SessionStore, tmp_path: Path
) -> None:
    # A turn stopped between the call and its result leaves no tool message.
    from saddle.web.app import history_for_display

    messages = [
        {
            "role": "assistant",
            "content": "",
            "tool_calls": [
                {
                    "id": "a",
                    "type": "function",
                    "function": {"name": "read_file", "arguments": '{"path": "n.txt"}'},
                },
            ],
        },
    ]
    row = history_for_display(messages, tmp_path)[0]["tools"][0]
    assert row["ok"] is True
    assert row["detail"] == ""


# -- previews -----------------------------------------------------------------


def test_an_image_a_tool_wrote_is_offered_for_display(tmp_path: Path) -> None:
    from saddle.tools import preview_for

    (tmp_path / "chart.png").write_bytes(b"\x89PNG")
    (tmp_path / "notes.txt").write_text("x")
    assert preview_for("write_file", '{"path": "chart.png"}', tmp_path) == "chart.png"
    assert preview_for("edit_file", '{"path": "chart.png"}', tmp_path) == "chart.png"
    # Not an image, not a writing tool, not on disk, not parseable, outside.
    assert preview_for("write_file", '{"path": "notes.txt"}', tmp_path) is None
    assert preview_for("read_file", '{"path": "chart.png"}', tmp_path) is None
    assert preview_for("write_file", '{"path": "absent.png"}', tmp_path) is None
    assert preview_for("write_file", "{not json", tmp_path) is None
    assert preview_for("write_file", '{"path": 7}', tmp_path) is None
    assert preview_for("write_file", '{"path": "../escape.png"}', tmp_path) is None


def test_the_file_route_serves_an_image_and_refuses_everything_else(
    store: SessionStore, tmp_path: Path
) -> None:
    work = tmp_path / "work"
    work.mkdir()
    (work / "logo.svg").write_text("<svg xmlns='http://www.w3.org/2000/svg'/>")
    (work / "secrets.txt").write_text("not a picture")
    (tmp_path / "outside.png").write_bytes(b"\x89PNG")

    with app_for(store, tmp_path) as (client, _app):
        sid = client.post("/api/sessions", json={"workdir": str(work)}).json()["id"]

        served = client.get(f"/api/sessions/{sid}/file", params={"path": "logo.svg"})
        assert served.status_code == 200
        assert served.headers["content-type"].startswith("image/svg+xml")
        # An SVG is a document; served on this origin it must not be able to
        # script, so it is locked down and its type is not sniffable.
        assert served.headers["x-content-type-options"] == "nosniff"
        assert "default-src 'none'" in served.headers["content-security-policy"]

        for path, status in (
            ("../outside.png", 403),  # escape by traversal
            (str(tmp_path / "outside.png"), 403),  # escape by absolute path
            ("secrets.txt", 404),  # inside, but not an image
            ("absent.png", 404),
            ("", 404),
        ):
            assert (
                client.get(f"/api/sessions/{sid}/file", params={"path": path}).status_code == status
            ), path


# -- the page itself ----------------------------------------------------------


def test_the_page_versions_its_assets_and_is_never_stored(
    store: SessionStore, tmp_path: Path
) -> None:
    # StaticFiles sends an ETag but no Cache-Control, so a browser may cache
    # app.js heuristically and run an old UI against a new server. That is
    # what happened: a page served by a build defining pastToolRows had it
    # undefined, and freshly added rows silently did not render.
    with app_for(store, tmp_path) as (client, _app):
        page = client.get("/")
        assert page.headers["cache-control"] == "no-store"
        body = page.text
        for name in ("app.css", "markdown.js", "app.js"):
            assert f"/static/{name}?v=" in body, name
        assert 'src="/static/app.js"' not in body  # the unversioned form is gone


# -- one unstarted session ----------------------------------------------------

def test_asking_for_a_new_session_twice_gives_the_same_unwritten_one(
    store: SessionStore, tmp_path: Path
) -> None:
    # Five impatient clicks should not leave five identical empty sessions.
    with app_for(store, tmp_path) as (client, _app):
        ids = {client.post("/api/sessions", json={}).json()["id"] for _ in range(5)}
        assert len(ids) == 1
        assert len(client.get("/api/sessions").json()) == 1


def test_choosing_a_new_persona_is_not_swallowed_by_a_leftover_session(
    store: SessionStore, tmp_path: Path
) -> None:
    """The regression that made a new persona look broken.

    Reuse handed back any unwritten session, so with one left over at the
    old persona, asking for a new session returned that -- and the persona
    just chosen was silently ignored. It looked like a new session and
    answered as the old one, which is exactly what "the tsundere persona
    doesn't work, the built-in ones do" is: the leftover was already on the
    built-in.
    """
    with app_for(store, tmp_path) as (client, _app):
        client.put("/api/personas/tsundere", json={"prompt": "Hmph."})
        stale = client.post(
            "/api/sessions", json={"reuse_unstarted": False, "persona": "engineer"}
        ).json()

        client.patch("/api/settings", json={"persona": "tsundere"})
        made = client.post("/api/sessions", json={}).json()

        assert made["persona"] == "tsundere"
        assert made["id"] != stale["id"]


def test_a_session_you_have_configured_is_not_handed_back_as_a_new_one(
    store: SessionStore, tmp_path: Path
) -> None:
    # Setting a persona on an empty session is setting it up, not leaving it
    # lying around. Asking for a new one must not return your setup.
    with app_for(store, tmp_path) as (client, _app):
        mine = client.post("/api/sessions", json={}).json()
        client.patch(f"/api/sessions/{mine['id']}", json={"persona": "reviewer"})
        assert client.post("/api/sessions", json={}).json()["id"] != mine["id"]


def test_an_explicit_persona_is_honoured_even_when_one_is_lying_around(
    store: SessionStore, tmp_path: Path
) -> None:
    with app_for(store, tmp_path) as (client, _app):
        lying_around = client.post("/api/sessions", json={}).json()
        asked = client.post("/api/sessions", json={"persona": "explainer"}).json()
        assert asked["persona"] == "explainer"
        assert asked["id"] != lying_around["id"]


def test_two_untouched_sessions_are_still_one_session(
    store: SessionStore, tmp_path: Path
) -> None:
    # The original complaint must stay fixed: repeated clicks on a pristine
    # session still return it.
    with app_for(store, tmp_path) as (client, _app):
        ids = {client.post("/api/sessions", json={}).json()["id"] for _ in range(5)}
        assert len(ids) == 1


def test_a_session_that_has_been_written_in_is_not_reused(
    store: SessionStore, tmp_path: Path
) -> None:
    with app_for(store, tmp_path) as (client, _app):
        first = client.post("/api/sessions", json={}).json()["id"]
        store.save_messages(first, [{"role": "user", "content": "started"}])
        second = client.post("/api/sessions", json={}).json()["id"]
        assert second != first
        assert len(client.get("/api/sessions").json()) == 2


def test_reuse_can_be_declined_for_a_caller_that_wants_a_fresh_one(
    store: SessionStore, tmp_path: Path
) -> None:
    with app_for(store, tmp_path) as (client, _app):
        first = client.post("/api/sessions", json={}).json()["id"]
        second = client.post(
            "/api/sessions", json={"reuse_unstarted": False}
        ).json()["id"]
        assert second != first


def test_reusing_an_unstarted_session_still_honours_a_new_folder(
    store: SessionStore, tmp_path: Path
) -> None:
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    with app_for(store, tmp_path) as (client, _app):
        first = client.post("/api/sessions", json={}).json()
        again = client.post("/api/sessions", json={"workdir": str(elsewhere)}).json()
        assert again["id"] == first["id"]
        assert again["workdir"] == str(elsewhere)


def test_concurrent_creates_do_not_race(store: SessionStore, tmp_path: Path) -> None:
    """Reuse is a read then a write, so it needs a lock.

    Driven against the store, not through TestClient: TestClient runs every
    request through one portal, so a "concurrent" test written against it is
    serialised and cannot see the race at all. The first version of this test
    was exactly that, and a mutant that deleted the lock survived it.

    `unstarted()` reads a file per session, so the window between "no
    unstarted session" and "created one" is real rather than theoretical.
    """
    made: list[str] = []
    failed: list[BaseException] = []
    threads_count = 8
    barrier = threading.Barrier(threads_count)

    def ask() -> None:
        try:
            barrier.wait(timeout=10)
            made.append(store.create(workdir=str(tmp_path), reuse_unstarted=True).id)
        except BaseException as exc:
            failed.append(exc)

    threads = [threading.Thread(target=ask) for _ in range(threads_count)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=15)

    assert failed == []
    assert len(made) == threads_count
    assert len(set(made)) == 1, f"{len(set(made))} distinct sessions from one burst"
    assert len(store.list()) == 1


# -- defaults for a new session -----------------------------------------------

def test_a_new_session_starts_at_the_stated_defaults_not_a_reset(
    store: SessionStore, tmp_path: Path
) -> None:
    with app_for(store, tmp_path) as (client, _app):
        assert client.get("/api/settings").json() == {
            "persona": "engineer", "reasoning_effort": "xhigh",
        }
        client.patch("/api/settings", json={"persona": "reviewer",
                                            "reasoning_effort": "low"})
        made = client.post("/api/sessions", json={"reuse_unstarted": False}).json()
        assert made["persona"] == "reviewer"
        assert made["reasoning_effort"] == "low"


def test_a_sessions_own_persona_does_not_move_the_default(
    store: SessionStore, tmp_path: Path
) -> None:
    with app_for(store, tmp_path) as (client, _app):
        sid = client.post("/api/sessions", json={}).json()["id"]
        client.patch(f"/api/sessions/{sid}", json={"persona": "explainer"})
        assert client.get("/api/settings").json()["persona"] == "engineer"


def test_an_explicit_persona_beats_the_default(
    store: SessionStore, tmp_path: Path
) -> None:
    with app_for(store, tmp_path) as (client, _app):
        client.patch("/api/settings", json={"persona": "reviewer"})
        made = client.post(
            "/api/sessions", json={"persona": "plain", "reuse_unstarted": False}
        ).json()
        assert made["persona"] == "plain"


def test_an_unknown_setting_is_ignored_rather_than_stored(
    store: SessionStore, tmp_path: Path
) -> None:
    with app_for(store, tmp_path) as (client, _app):
        body = client.patch("/api/settings", json={"nonsense": "x",
                                                   "persona": "plain"}).json()
        assert body == {"persona": "plain", "reasoning_effort": "xhigh"}


# -- writing and editing personas ---------------------------------------------

def test_a_persona_can_be_written_and_reaches_the_model(
    store: SessionStore, tmp_path: Path
) -> None:
    seen: list[str] = []

    def capture(_c: Any, messages: list[dict[str, Any]], text: str, options: Any,
                **_kw: Any) -> Any:
        seen.append(options.system_prompt)
        messages.append({"role": "user", "content": text})
        return iter(())

    import saddle.web.app as module

    with app_for(store, tmp_path) as (client, app):
        client.put("/api/personas/pirate", json={"prompt": "Arr. Ye be terse."})
        sid = client.post("/api/sessions", json={"persona": "pirate"}).json()["id"]
        module.run_turn = capture  # type: ignore[assignment]
        _server_of(app)._run(sid, "hello")

    assert seen == ["Arr. Ye be terse."]


def test_editing_a_builtin_shadows_it_and_deleting_restores_it(
    store: SessionStore, tmp_path: Path
) -> None:
    from saddle.sessions import BUILTIN_PERSONAS

    with app_for(store, tmp_path) as (client, _app):
        client.put("/api/personas/engineer", json={"prompt": "Mine now."})
        assert client.get("/api/personas").json()["personas"]["engineer"] == "Mine now."

        assert client.delete("/api/personas/engineer").status_code == 200
        table = client.get("/api/personas").json()
        assert table["personas"]["engineer"] == BUILTIN_PERSONAS["engineer"]
        assert table["editable"] == []


def test_a_builtin_that_was_never_edited_cannot_be_deleted(
    store: SessionStore, tmp_path: Path
) -> None:
    with app_for(store, tmp_path) as (client, _app):
        reply = client.delete("/api/personas/engineer")
        assert reply.status_code == 404
        # The message is the contract: it has to say *why* -- that there is
        # nothing of yours under that name -- not just fail.
        assert reply.json()["error"] == "no editable persona named 'engineer'"


def test_a_persona_needs_a_name(store: SessionStore, tmp_path: Path) -> None:
    with app_for(store, tmp_path) as (client, _app):
        reply = client.put("/api/personas/%20%20", json={"prompt": "x"})
        assert reply.status_code == 400
        assert reply.json()["error"] == "a persona needs a name"


def test_an_overlong_persona_name_is_refused(
    store: SessionStore, tmp_path: Path
) -> None:
    from saddle.sessions import MAX_PERSONA_NAME

    with app_for(store, tmp_path) as (client, _app):
        reply = client.put("/api/personas/" + "x" * (MAX_PERSONA_NAME + 1),
                           json={"prompt": "x"})
        assert reply.status_code == 400
        assert str(MAX_PERSONA_NAME) in reply.json()["error"]


def test_an_empty_persona_prompt_is_allowed(
    store: SessionStore, tmp_path: Path
) -> None:
    # "plain" ships empty; a user may want the same.
    with app_for(store, tmp_path) as (client, _app):
        client.put("/api/personas/bare", json={"prompt": ""})
        assert client.get("/api/personas").json()["personas"]["bare"] == ""


def test_a_corrupt_persona_file_falls_back_to_the_builtins(
    store: SessionStore, tmp_path: Path
) -> None:
    (store.root / "personas.json").write_text("{not json")
    assert "engineer" in store.personas()
    assert store.custom_personas() == {}
