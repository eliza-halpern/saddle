"""The chat UI's HTTP surface: sessions, uploads, and a live event stream.

One turn runs in a worker thread and pushes `events.Event` objects onto a
per-session queue; the browser drains that queue over server-sent events.
SSE rather than a websocket because the traffic is one-directional -- the
browser posts a message and then only listens -- and SSE reconnects by
itself, which matters when a turn runs for minutes.

State lives on disk (`sessions.SessionStore`), so a server restart loses
the turn in flight and nothing else.
"""

from __future__ import annotations

import asyncio
import json
import queue
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import FileResponse, JSONResponse, StreamingResponse
from starlette.routing import Mount, Route
from starlette.staticfiles import StaticFiles

from saddle.engine import TurnOptions, run_turn
from saddle.events import (
    ErrorEvent,
    Event,
    SessionInfo,
    SessionTitle,
    TerminalOutput,
)
from saddle.memory import estimate_tokens
from saddle.sessions import SessionStore, personas
from saddle.titles import title_for
from saddle.tools import ToolContext
from saddle.vllm import VllmClient

STATIC = Path(__file__).resolve().parent / "static"


@dataclass
class Live:
    """A session's in-memory half: subscribers, its tools, its turn counter.

    Events fan out to one queue **per connected client**. A single shared
    queue looked right and was not: `queue.get` hands each event to exactly
    one consumer, so a second tab -- or a reconnect that briefly overlaps the
    connection it replaces -- silently splits the stream between them and
    both render half a conversation.
    """

    subscribers: list[queue.Queue[Event | None]] = field(default_factory=list)
    context: ToolContext | None = None
    turn: int = 0
    parent: str | None = None
    busy: bool = False
    cancelled: bool = False
    lock: threading.Lock = field(default_factory=threading.Lock)

    def subscribe(self) -> queue.Queue[Event | None]:
        channel: queue.Queue[Event | None] = queue.Queue()
        with self.lock:
            self.subscribers.append(channel)
        return channel

    def unsubscribe(self, channel: queue.Queue[Event | None]) -> None:
        with self.lock:
            if channel in self.subscribers:
                self.subscribers.remove(channel)

    def publish(self, event: Event | None) -> None:
        with self.lock:
            channels = list(self.subscribers)
        for channel in channels:
            channel.put(event)


class ChatServer:
    def __init__(self, store: SessionStore, client_factory: Any, *, default_workdir: Path) -> None:
        self.store = store
        self.client_factory = client_factory
        self.default_workdir = default_workdir
        self.live: dict[str, Live] = {}
        self.window: int | None = None

    def _live(self, session_id: str) -> Live:
        return self.live.setdefault(session_id, Live())

    def _context_window(self, client: Any) -> int:
        """Ask the server once; fall back to a conservative default."""
        if self.window is None:
            try:
                self.window = client.max_model_len() or 120_000
            except Exception:  # a server that will not say gets the default
                self.window = 120_000
        return self.window

    def _name_session(
        self, session: Any, text: str, client: Any, live: Live
    ) -> None:
        """Name a new session after its opening message, once.

        Deliberately after the turn, not before it: the answer is already on
        screen by the time this costs anything, and a session that is never
        answered does not need a name. Failures are swallowed -- a session
        called "New session" is a cosmetic problem, and a turn that died
        because its title could not be written would not be.
        """
        if not session.auto_title or live.turn != 1:
            return
        try:
            title = title_for(client, text)
            if not title:
                return
            self.store.update(session.id, title=title, auto_title=False)
        except Exception:
            return
        live.publish(SessionTitle(session_id=session.id, title=title))

    # -- turn ------------------------------------------------------------

    def _run(self, session_id: str, text: str, images: list[str] | None = None) -> None:
        live = self._live(session_id)
        live.cancelled = False
        try:
            session = self.store.get(session_id)
            messages = self.store.load_messages(session_id)
            workdir = Path(session.workdir)
            if live.context is None or live.context.workdir != workdir:
                # Terminal output arrives on the reader thread, after the tool
                # call that started it has already returned, so it is pushed
                # to the session's subscribers rather than yielded by the turn.
                live.context = ToolContext(
                    workdir=workdir,
                    on_output=lambda tid, chunk: live.publish(
                        TerminalOutput(id=tid, chunk=chunk)
                    ),
                )
            live.turn += 1
            with self.client_factory() as client:
                options = TurnOptions(
                    workdir=workdir,
                    journal=self.store.journal_path(session_id),
                    system_prompt=session.prompt_text(),
                    reasoning_effort=session.reasoning_effort,
                    context_tokens=self._context_window(client),
                )
                for event in run_turn(
                    client,
                    messages,
                    text,
                    options,
                    turn=live.turn,
                    parent=live.parent,
                    context=live.context,
                    images=[Path(raw) for raw in (images or [])],
                    cancel=lambda: live.cancelled,
                ):
                    live.publish(event)
                    if event.kind == "turn.end":
                        live.parent = getattr(event, "proof", None)
                self._name_session(session, text, client, live)
            self.store.save_messages(session_id, messages)
        except Exception as exc:  # a dead turn must not take the server with it
            live.publish(ErrorEvent(message=f"{type(exc).__name__}: {exc}"))
        finally:
            with live.lock:
                live.busy = False
            live.publish(None)


def build_app(store: SessionStore, client_factory: Any, *, default_workdir: Path) -> Starlette:
    server = ChatServer(store, client_factory, default_workdir=default_workdir)

    async def index(_: Request) -> FileResponse:
        return FileResponse(STATIC / "index.html")

    async def list_sessions(_: Request) -> JSONResponse:
        return JSONResponse([s.__dict__ for s in store.list()])

    async def create_session(request: Request) -> JSONResponse:
        body = await request.json() if await request.body() else {}
        session = store.create(
            title=body.get("title") or "New session",
            workdir=body.get("workdir") or str(default_workdir),
            persona=body.get("persona") or "engineer",
        )
        return JSONResponse(session.__dict__)

    async def patch_session(request: Request) -> JSONResponse:
        body = await request.json()
        # A name the user typed is theirs; the model must not replace it.
        if "title" in body:
            body.setdefault("auto_title", False)
        session = store.update(request.path_params["sid"], **body)
        server.live.pop(session.id, None)  # workdir or persona may have moved
        return JSONResponse(session.__dict__)

    async def delete_session(request: Request) -> JSONResponse:
        store.delete(request.path_params["sid"])
        server.live.pop(request.path_params["sid"], None)
        return JSONResponse({"ok": True})

    async def get_messages(request: Request) -> JSONResponse:
        return JSONResponse(store.load_messages(request.path_params["sid"]))

    async def list_personas(_: Request) -> JSONResponse:
        return JSONResponse(personas())

    async def browse(request: Request) -> JSONResponse:
        """Directory listing for the folder picker."""
        raw = request.query_params.get("path") or str(Path.home())
        path = Path(raw).expanduser()
        if not path.is_dir():
            return JSONResponse({"error": f"not a directory: {raw}"}, status_code=400)
        entries = []
        for child in sorted(path.iterdir(), key=lambda p: p.name.lower()):
            if child.name.startswith(".") or not child.is_dir():
                continue
            entries.append({"name": child.name, "path": str(child)})
        return JSONResponse(
            {
                "path": str(path.resolve()),
                "parent": str(path.resolve().parent),
                "entries": entries[:400],
            }
        )

    async def upload(request: Request) -> JSONResponse:
        sid = request.path_params["sid"]
        form = await request.form()
        saved = []
        for item in form.getlist("files"):
            if not hasattr(item, "filename") or not item.filename:
                continue
            name = Path(str(item.filename)).name  # never trust a client path
            target = store.uploads_dir(sid) / name
            target.write_bytes(await item.read())
            saved.append({"name": name, "path": str(target), "size": target.stat().st_size})
        return JSONResponse({"saved": saved})

    async def post_message(request: Request) -> JSONResponse:
        sid = request.path_params["sid"]
        body = await request.json()
        text = str(body.get("text") or "").strip()
        if not text:
            return JSONResponse({"error": "empty message"}, status_code=400)
        # Only files this session actually uploaded may be attached, so a
        # crafted request cannot read an arbitrary path off the machine.
        allowed = store.uploads_dir(sid).resolve()
        images = [
            str(candidate)
            for raw in (body.get("images") or [])
            if (candidate := Path(str(raw)).resolve()).is_file()
            and allowed in candidate.parents
        ]
        live = server._live(sid)
        with live.lock:
            if live.busy:
                return JSONResponse({"error": "a turn is already running"}, status_code=409)
            live.busy = True
        threading.Thread(
            target=server._run, args=(sid, text, images), daemon=True
        ).start()
        return JSONResponse({"ok": True})

    async def stop_turn(request: Request) -> JSONResponse:
        """Ask the running turn to stop at its next safe point.

        Not a kill: the engine finishes the chunk or tool call it is in,
        then seals what it did. Work already done is real and stays in the
        record rather than being thrown away.
        """
        live = server._live(request.path_params["sid"])
        live.cancelled = True
        return JSONResponse({"stopping": live.busy})

    async def events(request: Request) -> StreamingResponse:
        sid = request.path_params["sid"]
        live = server._live(sid)
        session = store.get(sid)
        channel = live.subscribe()

        async def stream() -> Any:
            loop = asyncio.get_running_loop()
            try:
                info = SessionInfo(
                    session_id=sid,
                    title=session.title,
                    workdir=session.workdir,
                    persona=session.persona,
                    reasoning_effort=session.reasoning_effort,
                    context_used=estimate_tokens(store.load_messages(sid)),
                    context_limit=server.window or 175_000,
                    messages=store.load_messages(sid),
                )
                yield f"data: {json.dumps(info.payload())}\n\n"
                while True:
                    if await request.is_disconnected():
                        return
                    try:
                        event = await loop.run_in_executor(None, lambda: channel.get(timeout=1.0))
                    except queue.Empty:
                        # A comment frame keeps intermediaries from closing an
                        # idle stream, and costs nothing on the client.
                        yield ": keepalive\n\n"
                        continue
                    if event is None:
                        yield f"data: {json.dumps({'kind': 'idle'})}\n\n"
                        continue
                    yield f"data: {json.dumps(event.payload())}\n\n"
            finally:
                live.unsubscribe(channel)

        return StreamingResponse(
            stream(),
            media_type="text/event-stream",
            headers={
                "Cache-Control": "no-cache",
                "X-Accel-Buffering": "no",
            },
        )

    return Starlette(
        routes=[
            Route("/", index),
            Route("/api/personas", list_personas),
            Route("/api/browse", browse),
            Route("/api/sessions", list_sessions, methods=["GET"]),
            Route("/api/sessions", create_session, methods=["POST"]),
            Route("/api/sessions/{sid}", patch_session, methods=["PATCH"]),
            Route("/api/sessions/{sid}", delete_session, methods=["DELETE"]),
            Route("/api/sessions/{sid}/messages", get_messages),
            Route("/api/sessions/{sid}/upload", upload, methods=["POST"]),
            Route("/api/sessions/{sid}/message", post_message, methods=["POST"]),
        Route("/api/sessions/{sid}/stop", stop_turn, methods=["POST"]),
            Route("/api/sessions/{sid}/events", events),
            Mount("/static", StaticFiles(directory=str(STATIC)), name="static"),
        ]
    )


def serve(
    *,
    host: str,
    port: int,
    api_key: str,
    base_url: str,
    model: str,
    workdir: Path,
    sessions_root: Path | None = None,
) -> None:
    import uvicorn

    store = SessionStore(sessions_root)

    def factory() -> VllmClient:
        return VllmClient(api_key=api_key, base_url=base_url, model=model)

    app = build_app(store, factory, default_workdir=workdir)
    uvicorn.run(app, host=host, port=port, log_level="warning")
