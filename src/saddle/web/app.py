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
import hmac
import ipaddress
import json
import mimetypes
import os
import queue
import secrets
import subprocess
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import urlencode

from starlette.applications import Starlette
from starlette.datastructures import UploadFile
from starlette.requests import Request
from starlette.responses import (
    FileResponse,
    HTMLResponse,
    JSONResponse,
    PlainTextResponse,
    RedirectResponse,
    Response,
    StreamingResponse,
)
from starlette.routing import Mount, Route
from starlette.staticfiles import StaticFiles
from starlette.types import ASGIApp, Receive, Scope, Send

from saddle.auto import DEFAULT_TIME_BUDGET_S, DEFAULT_TOKEN_BUDGET, AutoError, repo_root
from saddle.engine import TurnOptions
from saddle.engine import run_turn as run_turn  # an injection seam: the tests replace it
from saddle.events import (
    ErrorEvent,
    Event,
    SessionInfo,
    SessionTitle,
    TerminalOutput,
)
from saddle.feed import Arm, default_auditor
from saddle.feed import AuditorFactory as FeedAuditorFactory
from saddle.labels import label_for
from saddle.memory import estimate_tokens
from saddle.packet import Packet, compile_packet, render_packet_text
from saddle.sandbox import OutsideRootError, resolve_within
from saddle.sessions import BUILTIN_PERSONAS, SESSION_MODES, SessionStore
from saddle.titles import title_for
from saddle.tools import PREVIEWABLE, ToolContext, preview_for, tools_for_mode
from saddle.undo import UndoLog
from saddle.vllm import VllmClient
from saddle.web import branch_actions, tasks
from saddle.web.tasks import SMALL_LANE_TEST_EDITS, TaskRun

STATIC = Path(__file__).resolve().parent / "static"

TOKEN_FILE = "~/.config/saddle/chat-token"
"""Where a generated chat token is cached across restarts, when the caller
gives none and SADDLE_CHAT_TOKEN is not set. Distinct from `cli.KEY_FILE`,
which holds the vLLM key -- this file is never read for that purpose."""

TOKEN_COOKIE = "saddle_token"
TOKEN_COOKIE_MAX_AGE = 60 * 60 * 24 * 365  # one year


def git_branch(folder: Path) -> str:
    """The folder's checked-out branch for the header, or "" if it has none.

    Display only: a detached HEAD, a folder outside git, or git missing all
    show nothing rather than an error, since the header is not the place to
    report them."""
    try:
        out = subprocess.run(
            ["git", "-C", str(folder), "rev-parse", "--abbrev-ref", "HEAD"],
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return ""
    name = out.stdout.strip()
    return name if out.returncode == 0 and name != "HEAD" else ""


def needs_token(host: str) -> bool:
    """Whether a server bound to `host` must require a token.

    False only for `localhost` and an address `ipaddress` parses as
    loopback (127.0.0.0/8, ::1). Everything else -- `0.0.0.0`, `::`, an
    empty string, a tailnet IP, a MagicDNS name, garbage -- fails closed
    and answers True, because a bind address this function cannot make
    sense of is not one it can vouch for as local-only.
    """
    if host == "localhost":
        return False
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        return True
    return not address.is_loopback


def chat_token() -> str:
    """The token to require: env, then the cached file, then a fresh one.

    `SADDLE_CHAT_TOKEN` wins when set and non-empty. Otherwise `TOKEN_FILE`
    is read if it exists and is non-empty. Otherwise a random token is
    generated and the file is created with mode 0o600 before anything else
    can read it, mirroring `cli.KEY_FILE`'s fallback for the vLLM key --
    this constant is never used to read or print that key.

    An existing file is never overwritten. One that exists but is empty
    raises `FileExistsError` naming the path and how to recover, rather than
    regenerating into it.
    """
    env = os.environ.get("SADDLE_CHAT_TOKEN")
    if env:
        return env
    path = Path(TOKEN_FILE).expanduser()
    if path.exists():
        existing = path.read_text(encoding="utf-8").strip()
        if existing:
            return existing
        message = f"{path} exists but is empty; write a token into it or delete it"
        raise FileExistsError(message)
    token = secrets.token_urlsafe(32)
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor = os.open(str(path), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
        handle.write(token)
    return token


def _token_matches(provided: str | None, token: str) -> bool:
    """Constant-time comparison on bytes; a prefix or a near-miss must not pass."""
    if provided is None:
        return False
    return hmac.compare_digest(provided.encode("utf-8"), token.encode("utf-8"))


def _unauthorized() -> PlainTextResponse:
    # Never echo the token back, even implicitly -- the body is fixed text.
    return PlainTextResponse("saddle: token required", status_code=401)


class TokenGate:
    """Pure ASGI middleware: every `http` request must carry the token.

    Deliberately not `starlette.middleware.base.BaseHTTPMiddleware`, which
    buffers a handler's whole response before it can be forwarded -- that
    would break the SSE stream, which has to flush each event as it is
    published rather than after the connection closes. This wraps `scope`,
    `receive` and `send` directly, so an authorized request reaches the
    inner app, uploads and the static mount included, exactly as if the
    gate were not there.
    """

    def __init__(self, app: ASGIApp, token: str) -> None:
        self.app = app
        self.token = token

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        request = Request(scope, receive)
        query_token = request.query_params.get("token")
        if query_token is not None:
            if not _token_matches(query_token, self.token):
                await _unauthorized()(scope, receive, send)
                return
            kept = [(k, v) for k, v in request.query_params.multi_items() if k != "token"]
            location = request.url.path + (f"?{urlencode(kept)}" if kept else "")
            response = RedirectResponse(location, status_code=303)
            # HttpOnly so a page script cannot read the token back out of
            # document.cookie; Lax because every state-changing route is
            # POST/PATCH/DELETE and Lax never sends it cross-site for those,
            # while Strict would withhold it on the very first load after a
            # cross-site redirect (the phone-open-link path).
            response.set_cookie(
                TOKEN_COOKIE,
                self.token,
                max_age=TOKEN_COOKIE_MAX_AGE,
                httponly=True,
                samesite="lax",
                path="/",
            )
            await response(scope, receive, send)
            return
        header = request.headers.get("authorization") or ""
        scheme, _, value = header.partition(" ")
        bearer = value if scheme == "Bearer" else None
        cookie = request.cookies.get(TOKEN_COOKIE)
        if not (_token_matches(bearer, self.token) or _token_matches(cookie, self.token)):
            await _unauthorized()(scope, receive, send)
            return
        await self.app(scope, receive, send)


def history_for_display(
    messages: list[dict[str, Any]], workdir: Path, undo_root: Path | None = None
) -> list[dict[str, Any]]:
    """Stored messages, with each tool call carrying what the row showed.

    The transcript is rebuilt from this on every connect, and it used to keep
    only user and assistant text -- so a reconnect silently deleted every
    reasoning block and tool row the reader had just watched appear. The
    calls and their results were in the store the whole time; what was
    missing was the label and the outcome, which the renderer cannot derive
    because the tenses live in `labels`. So they are attached here, once,
    beside the call they belong to.
    """
    results = {
        message.get("tool_call_id"): str(message.get("content") or "")
        for message in messages
        if message.get("role") == "tool"
    }
    # Which stored version of a file each call produced, so an older
    # message keeps showing the picture it made rather than the newest one.
    versions = UndoLog(undo_root).versions() if undo_root is not None else {}
    shown: list[dict[str, Any]] = []
    for index, message in enumerate(messages):
        # The index travels with the message so the page can name one to
        # rewind to; it is re-checked server-side, since compaction moves them.
        message = {**message, "index": index}
        calls = message.get("tool_calls")
        if not calls:
            shown.append(message)
            continue
        rows = []
        for call in calls:
            function = call.get("function") or {}
            name = str(function.get("name") or "")
            arguments = str(function.get("arguments") or "")
            detail = results.get(call.get("id"), "")
            # A tool reports failure in its return value, not by raising, so
            # the outcome is recoverable from the stored result alone.
            ok = not detail.startswith("error: ")
            rows.append(
                {
                    "id": call.get("id"),
                    "name": name,
                    "label": label_for(name, arguments, ok=ok),
                    "ok": ok,
                    "detail": detail,
                    "preview": preview_for(name, arguments, workdir),
                    "version": versions.get(call.get("id")),
                }
            )
        shown.append({**message, "tools": rows})
    return shown


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
    def __init__(
        self,
        store: SessionStore,
        client_factory: Any,
        *,
        default_workdir: Path,
        auditor: tasks.AuditorFactory | None = None,
        arm: Arm = "E+A+F",
        feed_auditor: FeedAuditorFactory = default_auditor,
        allow_test_edits: bool = SMALL_LANE_TEST_EDITS,
    ) -> None:
        self.store = store
        self.client_factory = client_factory
        self.default_workdir = default_workdir
        self.live: dict[str, Live] = {}
        self.window: int | None = None
        self.tasks: dict[str, TaskRun] = {}
        self.auditor = auditor
        """The auditor seam: given a run, the hook `run_auto` consults after
        each tool call. None until the auditor lane lands; tests and the
        screenshot harness pass a scripted one."""
        self.arm: Arm = arm
        """The Phase 2 arm a chat-started run uses; `saddle auto`'s default."""
        self.feed_auditor = feed_auditor
        """Builds the feed's auditor (`feed.default_auditor`: the real one)."""
        self.allow_test_edits = allow_test_edits
        """The confirm strip's default for "Allow test edits"; each run may override it."""

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

    def _name_session(self, session: Any, text: str, client: Any, live: Live) -> None:
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

    def _run(self, session_id: str, text: str | None, images: list[str] | None = None) -> None:
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
                    on_output=lambda tid, chunk: live.publish(TerminalOutput(id=tid, chunk=chunk)),
                    undo=UndoLog(self.store.undo_dir(session_id)),
                )
            # Set every turn: the context outlives a lane change.
            tools = tools_for_mode(session.mode)
            live.context.allowed = tuple(t["function"]["name"] for t in tools)
            live.turn += 1
            with self.client_factory() as client:
                options = TurnOptions(
                    workdir=workdir,
                    journal=self.store.journal_path(session_id),
                    system_prompt=session.prompt_text(self.store.personas()),
                    reasoning_effort=session.reasoning_effort,
                    temperature=session.temperature,
                    context_tokens=self._context_window(client),
                    tools=tools,
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

    # -- task -----------------------------------------------------------

    def _run_task(self, session_id: str, run: TaskRun) -> None:
        """Run one chat-started task on `saddle auto`'s own path (T5-7)."""
        live = self._live(session_id)
        try:
            session = self.store.get(session_id)
            live.publish(run.state_event())
            audit = self.auditor(run) if self.auditor is not None else None
            with self.client_factory() as client:
                _verdict, recap = tasks.execute(
                    run,
                    workdir=Path(session.workdir),
                    client=client,
                    publish=live.publish,
                    chat_journal=self.store.journal_path(session_id),
                    reasoning_effort=session.reasoning_effort,
                    audit=audit,
                    arm=self.arm,
                    feed_auditor=self.feed_auditor,
                    allow_test_edits=run.allow_test_edits,
                )
            if recap is not None:
                messages = self.store.load_messages(session_id)
                messages.append(recap)
                self.store.save_messages(session_id, messages)
        except Exception as exc:  # a dead run must not take the server with it
            run.state = "failed"
            live.publish(run.state_event(f"{type(exc).__name__}: {exc}"))
        finally:
            with live.lock:
                live.busy = False
            live.publish(None)


def build_app(
    store: SessionStore,
    client_factory: Any,
    *,
    default_workdir: Path,
    token: str | None = None,
    auditor: tasks.AuditorFactory | None = None,
    arm: Arm = "E+A+F",
    feed_auditor: FeedAuditorFactory = default_auditor,
    allow_test_edits: bool = SMALL_LANE_TEST_EDITS,
) -> ASGIApp:
    server = ChatServer(
        store,
        client_factory,
        default_workdir=default_workdir,
        auditor=auditor,
        arm=arm,
        feed_auditor=feed_auditor,
        allow_test_edits=allow_test_edits,
    )

    async def index(_: Request) -> Response:
        """The page, with its asset URLs versioned by file mtime.

        StaticFiles sends an ETag but no Cache-Control, which leaves a
        browser free to cache app.js heuristically for hours and keep running
        an old UI against a new server. That is not hypothetical: a page was
        served by a build that defines `pastToolRows` and the function was
        undefined in it, so freshly added rows silently did not render and
        the server looked at fault. A changed file now has a different URL,
        and the page that names those URLs is never stored.
        """
        html = (STATIC / "index.html").read_text(encoding="utf-8")
        for name in ("app.css", "markdown.js", "tasks.js", "notify.js", "app.js"):
            try:
                version = int((STATIC / name).stat().st_mtime)
            except OSError:
                continue
            html = html.replace(f"/static/{name}", f"/static/{name}?v={version}")
        return HTMLResponse(html, headers={"Cache-Control": "no-store"})

    async def list_sessions(_: Request) -> JSONResponse:
        """Every session, with its latest run's state and task (None if none).

        The sidebar reads this so a run in a session that is not on screen --
        one waiting on you, above all -- is still visible. Runs are kept in
        start order, so the last one seen for a session is its latest.
        """
        latest: dict[str, TaskRun] = {}
        for run in list(server.tasks.values()):
            latest[run.session_id] = run
        rows = []
        for session in store.list():
            newest = latest.get(session.id)
            row = dict(session.__dict__)
            row["run_state"] = newest.state if newest is not None else None
            row["run_task"] = newest.task if newest is not None else None
            rows.append(row)
        return JSONResponse(rows)

    async def create_session(request: Request) -> JSONResponse:
        body = await request.json() if await request.body() else {}
        session = store.create(
            title=body.get("title") or "New session",
            workdir=body.get("workdir") or str(default_workdir),
            persona=body.get("persona"),
            reasoning_effort=body.get("reasoning_effort"),
            temperature=body.get("temperature"),
            # The sidebar's "+" asks for a session, not necessarily a new
            # one: an unwritten session is the same unwritten session, so
            # five impatient clicks leave one.
            reuse_unstarted=bool(body.get("reuse_unstarted", True)),
        )
        return JSONResponse(session.__dict__)

    async def get_settings(_: Request) -> JSONResponse:
        return JSONResponse(store.settings())

    async def task_policy(_: Request) -> JSONResponse:
        """What a chat-started run may do, so the confirm strip says it truly."""
        return JSONResponse({"test_edits": server.allow_test_edits})

    async def patch_settings(request: Request) -> JSONResponse:
        return JSONResponse(store.update_settings(**await request.json()))

    async def save_persona(request: Request) -> JSONResponse:
        name = request.path_params["name"]
        body = await request.json()
        try:
            table = store.save_persona(name, str(body.get("prompt") or ""))
        except ValueError as exc:
            return JSONResponse({"error": str(exc)}, status_code=400)
        return JSONResponse(table)

    async def delete_persona(request: Request) -> JSONResponse:
        try:
            table = store.delete_persona(request.path_params["name"])
        except KeyError as exc:
            # A builtin cannot be deleted, only shadowed and then restored.
            # `str(KeyError)` is the repr of its argument, quotes and all, so
            # the message is taken from args rather than stringified.
            return JSONResponse({"error": exc.args[0]}, status_code=404)
        return JSONResponse(table)

    async def patch_session(request: Request) -> JSONResponse:
        body = await request.json()
        # A name the user typed is theirs; the model must not replace it.
        if "title" in body:
            body.setdefault("auto_title", False)
        if "temperature" in body:
            body["temperature"] = store.clamp_temperature(body["temperature"])
        # `update` skips a None, so a bad mode must be refused here, before
        # any other field in the same body is written.
        if "mode" in body and body["mode"] not in SESSION_MODES:
            return JSONResponse(
                {"error": f"mode must be one of {', '.join(SESSION_MODES)}"}, status_code=400
            )
        session = store.update(request.path_params["sid"], **body)
        # The Live is deliberately kept. It used to be dropped here "because
        # the workdir or persona may have moved", but a Live is not a cache of
        # the session -- it holds the *subscriber queues* of every connected
        # browser. Dropping it orphaned the open EventSource: the next turn
        # published into a fresh Live that nobody was listening to, so the UI
        # sat on "working" forever with no reasoning and no reply.
        #
        # That is why changing persona looked like the persona was broken.
        # `engineer` needs no change to select, so it never hit this;
        # anything else did. The workdir case it was guarding is already
        # handled in `_run`, which rebuilds the tool context whenever the
        # session has moved.
        return JSONResponse(session.__dict__)

    async def delete_session(request: Request) -> JSONResponse:
        store.delete(request.path_params["sid"])
        server.live.pop(request.path_params["sid"], None)
        return JSONResponse({"ok": True})

    async def get_messages(request: Request) -> JSONResponse:
        return JSONResponse(store.load_messages(request.path_params["sid"]))

    async def list_personas(_: Request) -> JSONResponse:
        # The editor needs to know which names it may delete, so the builtin
        # set travels with the table rather than being guessed from it.
        return JSONResponse(
            {
                "personas": store.personas(),
                "builtin": sorted(BUILTIN_PERSONAS),
                "editable": sorted(store.custom_personas()),
            }
        )

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

    async def workdir_file(request: Request) -> Response:
        """Serve one image out of a session's own working directory.

        The same boundary the tools use, for the same reason: `path` arrives
        from the page, so it is resolved against the session's workdir and
        refused if it lands outside. Only image types are served, and only
        as attachments-in-place -- this is how a diagram the model just drew
        becomes visible without the reader leaving the conversation.
        """
        sid = request.path_params["sid"]
        session = store.get(sid)
        raw = request.query_params.get("path") or ""
        try:
            target = resolve_within(Path(session.workdir), raw)
        except OutsideRootError:
            return JSONResponse({"error": "outside the working folder"}, status_code=403)
        if target.suffix.lower() not in PREVIEWABLE:
            return JSONResponse({"error": f"no image at {raw}"}, status_code=404)
        # A message shows the version *it* produced. Without this an image
        # edited later in the conversation appeared, identically, in every
        # message that ever touched it.
        version = request.query_params.get("v")
        if version:
            if not version.isalnum():
                return JSONResponse({"error": "bad version"}, status_code=400)
            stored = UndoLog(store.undo_dir(sid)).blob_path(version)
            if stored.is_file():
                target = stored
        if not target.is_file():
            return JSONResponse({"error": f"no image at {raw}"}, status_code=404)
        # From the requested path: a stored version is a content-addressed
        # blob with no extension of its own.
        media = mimetypes.guess_type(raw)[0] or "application/octet-stream"
        return FileResponse(
            target,
            media_type=media,
            headers={
                # An SVG is a document, and a document served inline on this
                # origin can script against it. It is only ever needed here as
                # a picture, so say so and let the <img> tag render it.
                "Content-Security-Policy": "default-src 'none'; style-src 'unsafe-inline'",
                "X-Content-Type-Options": "nosniff",
            },
        )

    async def make_folder(request: Request) -> JSONResponse:
        """Create one folder inside an existing one.

        `name` is a single segment, checked rather than trusted: a slash or
        a `..` would turn "make a folder here" into "make one anywhere",
        which is not what the button says it does.
        """
        body = await request.json()
        parent = Path(str(body.get("path") or "")).expanduser()
        name = str(body.get("name") or "").strip()
        if not parent.is_dir():
            return JSONResponse({"error": f"not a directory: {parent}"}, status_code=400)
        if not name or name in (".", ".."):
            return JSONResponse({"error": "give the folder a name"}, status_code=400)
        if name != Path(name).name or any(sep in name for sep in ("/", "\\")):
            return JSONResponse({"error": "a name, not a path"}, status_code=400)
        target = parent / name
        if target.exists():
            return JSONResponse({"error": f"{name!r} is already there"}, status_code=409)
        try:
            target.mkdir()
        except OSError as exc:
            return JSONResponse({"error": str(exc)}, status_code=400)
        return JSONResponse({"path": str(target.resolve()), "name": name})

    async def upload(request: Request) -> JSONResponse:
        sid = request.path_params["sid"]
        form = await request.form()
        saved = []
        for item in form.getlist("files"):
            if not isinstance(item, UploadFile) or not item.filename:
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
            if (candidate := Path(str(raw)).resolve()).is_file() and allowed in candidate.parents
        ]
        live = server._live(sid)
        with live.lock:
            if live.busy:
                return JSONResponse({"error": "a turn is already running"}, status_code=409)
            live.busy = True
        threading.Thread(target=server._run, args=(sid, text, images), daemon=True).start()
        return JSONResponse({"ok": True})

    async def post_task(request: Request) -> JSONResponse:
        """Start `saddle auto` on this session's folder with the message as its task."""
        sid = request.path_params["sid"]
        body = await request.json()
        text = str(body.get("text") or "").strip()
        if not text:
            return JSONResponse({"error": "a task needs words"}, status_code=400)
        try:
            time_s = float(body.get("time_budget_s") or DEFAULT_TIME_BUDGET_S)
            token_budget = int(body.get("token_budget") or DEFAULT_TOKEN_BUDGET)
        except (TypeError, ValueError):
            return JSONResponse({"error": "budgets must be numbers"}, status_code=400)
        if time_s <= 0 or token_budget <= 0:
            return JSONResponse({"error": "budgets must be positive"}, status_code=400)
        allow_test_edits = body.get("allow_test_edits", server.allow_test_edits)
        if not isinstance(allow_test_edits, bool):
            return JSONResponse({"error": "allow_test_edits must be a boolean"}, status_code=400)
        store.get(sid)
        live = server._live(sid)
        with live.lock:
            if live.busy:
                return JSONResponse({"error": "a turn is already running"}, status_code=409)
            live.busy = True
        run = TaskRun(
            run_id=tasks.new_run_id(),
            session_id=sid,
            task=text,
            time_budget_s=time_s,
            token_budget=token_budget,
            allow_test_edits=allow_test_edits,
        )
        server.tasks[run.run_id] = run
        threading.Thread(target=server._run_task, args=(sid, run), daemon=True).start()
        return JSONResponse({"run_id": run.run_id})

    def _task(request: Request) -> TaskRun | None:
        return server.tasks.get(request.path_params["rid"])

    async def answer_task(request: Request) -> JSONResponse:
        """The user's answer to a run's question. The engine seals it in the ledger."""
        run = _task(request)
        if run is None:
            return JSONResponse({"error": "no such task"}, status_code=404)
        body = await request.json()
        text = str(body.get("text") or "").strip()
        if not text:
            return JSONResponse({"error": "an answer needs words"}, status_code=400)
        if run.state != "needs_you":
            return JSONResponse({"error": "this task is not waiting on you"}, status_code=409)
        run.answers.put(text)
        return JSONResponse({"ok": True})

    async def stop_task(request: Request) -> JSONResponse:
        """Stop a run at its next safe point; it ends `stopped`, sealed."""
        run = _task(request)
        if run is None:
            return JSONResponse({"error": "no such task"}, status_code=404)
        run.cancelled = True
        return JSONResponse({"stopping": run.state in ("running", "needs_you")})

    def _journal(sid: str, rid: str) -> Path | None:
        run = server.tasks.get(rid)
        return (
            run.journal
            if run is not None and run.session_id == sid
            else tasks.journal_for(store.journal_path(sid), rid)
        )

    async def task_packet(request: Request) -> JSONResponse:
        """The run's evidence packet, compiled from its ledger on every read."""
        sid, rid = request.path_params["sid"], request.path_params["rid"]
        journal = _journal(sid, rid)
        if journal is None:
            return JSONResponse({"error": "no such task in this session"}, status_code=404)
        # The run's repo, so the Reproduce row reports the branch anchor check
        # (FIX-4). A web run's ledger is `auto.ledger_path(repo, rid)`; a path
        # of any other shape names no repo, and the check is left out.
        parts = journal.parts
        shaped = len(parts) >= 4 and parts[-4:-2] == (".saddle", "runs")
        repo = Path(*parts[:-4]) if shaped else None
        return JSONResponse(compile_packet(journal, run_id=rid, anchor_repo=repo).payload())

    def _branch_context(request: Request) -> tuple[str, Path, Packet, str]:
        """The run id, checkout root, packet and run branch an action works on."""
        sid, rid = request.path_params["sid"], request.path_params["rid"]
        journal = _journal(sid, rid)
        if journal is None:
            msg = "no such task in this session"
            raise branch_actions.ActionRefusedError(msg, 404)
        packet = compile_packet(journal, run_id=rid)
        try:
            root = repo_root(Path(store.get(sid).workdir))
        except AutoError as exc:
            raise branch_actions.ActionRefusedError(str(exc), 404) from exc
        return rid, root, packet, branch_actions.run_branch(packet)

    def _refused(exc: branch_actions.ActionRefusedError) -> JSONResponse:
        return JSONResponse({"error": str(exc)}, status_code=exc.status)

    async def task_branch(request: Request) -> JSONResponse:
        """What the action row needs: branch, target, and whether merge is allowed."""
        try:
            _rid, root, packet, branch = _branch_context(request)
        except branch_actions.ActionRefusedError as exc:
            return _refused(exc)
        return JSONResponse(
            {
                "branch": branch,
                "exists": branch_actions.branch_exists(root, branch),
                "target": branch_actions.current_branch(root),
                "merge_refusal": branch_actions.merge_refusal(packet),
                "recap": render_packet_text(packet),
                "sealed": False,
                "log": "actions.log",
            }
        )

    async def task_diff(request: Request) -> JSONResponse:
        """The run's branch against its base, per file."""
        try:
            _rid, root, _packet, branch = _branch_context(request)
            files = branch_actions.diff(root, branch)
        except branch_actions.ActionRefusedError as exc:
            return _refused(exc)
        return JSONResponse(
            {"branch": branch, "files": [{"path": f.path, "patch": f.patch} for f in files]}
        )

    def _act(request: Request, action: str, body: dict[str, Any]) -> JSONResponse:
        sid = request.path_params["sid"]
        log = store.journal_path(sid).parent / "actions.log"
        confirm = str(body.get("confirm") or "")
        try:
            rid, root, packet, branch = _branch_context(request)
        except branch_actions.ActionRefusedError as exc:
            return _refused(exc)
        try:
            if action == "merge":
                said = branch_actions.merge(root, packet, branch, confirm)
            else:
                said = branch_actions.discard(root, branch, confirm)
        except branch_actions.ActionRefusedError as exc:
            branch_actions.log_action(log, rid, f"{action} refused", str(exc))
            return _refused(exc)
        branch_actions.log_action(log, rid, action, said)
        return JSONResponse({"ok": True, "output": said, "sealed": False})

    async def task_merge(request: Request) -> JSONResponse:
        return _act(request, "merge", await request.json())

    async def task_discard(request: Request) -> JSONResponse:
        return _act(request, "discard", await request.json())

    def _rewind_target(sid: str, index: int) -> tuple[list[dict[str, Any]], str] | None:
        """The stored messages and the question at `index`, if one is there.

        The index arrives from the page and may be stale -- compaction moves
        messages -- so it is checked against the store rather than trusted.
        """
        messages = store.load_messages(sid)
        if not 0 <= index < len(messages) or messages[index].get("role") != "user":
            return None
        content = messages[index].get("content")
        if isinstance(content, list):
            asked = next((p.get("text", "") for p in content if p.get("type") == "text"), "")
        else:
            asked = str(content or "")
        return messages, asked

    async def rewind_preview(request: Request) -> JSONResponse:
        """What rewinding here would change on disk, without changing it.

        A rewind edits the user's working directory. Doing that silently is
        not acceptable, so the page asks first -- and to ask it has to be
        able to name the files.
        """
        sid = request.path_params["sid"]
        try:
            index = int(request.query_params.get("index", ""))
        except ValueError:
            return JSONResponse({"error": "index must be a number"}, status_code=400)
        target = _rewind_target(sid, index)
        if target is None:
            return JSONResponse({"error": "no question there"}, status_code=404)
        pending = UndoLog(store.undo_dir(sid)).pending(index)
        return JSONResponse(
            {"text": target[1], "reverted": pending.reverted, "deleted": pending.deleted}
        )

    async def rewind(request: Request) -> JSONResponse:
        """Answer a question again: optionally reworded, always from a clean tree."""
        sid = request.path_params["sid"]
        body = await request.json()
        try:
            index = int(body.get("index"))
        except (TypeError, ValueError):
            return JSONResponse({"error": "index must be a number"}, status_code=400)
        live = server._live(sid)
        with live.lock:
            if live.busy:
                # While a turn is running the answer is 409 whatever the
                # index says, so this is checked before the index is.
                return JSONResponse({"error": "a turn is already running"}, status_code=409)
            live.busy = True

        target = _rewind_target(sid, index)
        if target is None:
            with live.lock:
                live.busy = False  # nothing was started, so nothing holds it
            return JSONResponse({"error": "no question there"}, status_code=404)
        messages, _asked = target

        restored = UndoLog(store.undo_dir(sid)).restore_to(index)
        text = body.get("text")
        edited = isinstance(text, str) and text.strip() != ""
        # An edit replaces the question, so the question goes too; a retry
        # keeps it and answers it again.
        store.save_messages(sid, messages[: index if edited else index + 1])
        threading.Thread(
            target=server._run,
            args=(sid, text.strip() if edited else None),
            daemon=True,
        ).start()
        return JSONResponse(
            {
                "ok": True,
                "reverted": restored.reverted,
                "deleted": restored.deleted,
                "failed": restored.failed,
            }
        )

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
                    temperature=session.temperature,
                    mode=session.mode,
                    branch=git_branch(Path(session.workdir)),
                    context_used=estimate_tokens(store.load_messages(sid)),
                    context_limit=server.window or 175_000,
                    messages=history_for_display(
                        store.load_messages(sid),
                        Path(session.workdir),
                        store.undo_dir(sid),
                    ),
                )
                yield f"data: {json.dumps(info.payload())}\n\n"
                # A run still going when the page (re)connects gets its card
                # back: its state and every session line so far.
                for run in list(server.tasks.values()):
                    if run.session_id != sid or run.state not in ("running", "needs_you"):
                        continue
                    yield f"data: {json.dumps(run.state_event().payload())}\n\n"
                    for line in list(run.lines):
                        yield f"data: {json.dumps(line.payload())}\n\n"
                    if run.progress is not None:
                        yield f"data: {json.dumps(run.progress.payload())}\n\n"
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

    app: ASGIApp = Starlette(
        routes=[
            Route("/", index),
            Route("/api/personas", list_personas),
            Route("/api/personas/{name}", save_persona, methods=["PUT"]),
            Route("/api/personas/{name}", delete_persona, methods=["DELETE"]),
            Route("/api/settings", get_settings),
            Route("/api/settings", patch_settings, methods=["PATCH"]),
            Route("/api/task-policy", task_policy),
            Route("/api/browse", browse),
            Route("/api/browse", make_folder, methods=["POST"]),
            Route("/api/sessions", list_sessions, methods=["GET"]),
            Route("/api/sessions", create_session, methods=["POST"]),
            Route("/api/sessions/{sid}", patch_session, methods=["PATCH"]),
            Route("/api/sessions/{sid}", delete_session, methods=["DELETE"]),
            Route("/api/sessions/{sid}/messages", get_messages),
            Route("/api/sessions/{sid}/file", workdir_file),
            Route("/api/sessions/{sid}/upload", upload, methods=["POST"]),
            Route("/api/sessions/{sid}/message", post_message, methods=["POST"]),
            Route("/api/sessions/{sid}/rewind", rewind_preview),
            Route("/api/sessions/{sid}/rewind", rewind, methods=["POST"]),
            Route("/api/sessions/{sid}/stop", stop_turn, methods=["POST"]),
            Route("/api/sessions/{sid}/task", post_task, methods=["POST"]),
            Route("/api/sessions/{sid}/tasks/{rid}/packet", task_packet),
            Route("/api/sessions/{sid}/tasks/{rid}/branch", task_branch),
            Route("/api/sessions/{sid}/tasks/{rid}/diff", task_diff),
            Route("/api/sessions/{sid}/tasks/{rid}/merge", task_merge, methods=["POST"]),
            Route("/api/sessions/{sid}/tasks/{rid}/discard", task_discard, methods=["POST"]),
            Route("/api/tasks/{rid}/answer", answer_task, methods=["POST"]),
            Route("/api/tasks/{rid}/stop", stop_task, methods=["POST"]),
            Route("/api/sessions/{sid}/events", events),
            Mount("/static", StaticFiles(directory=str(STATIC)), name="static"),
        ]
    )
    # A token wraps the whole app in the pure-ASGI gate, so the SSE stream
    # and the static mount are covered too -- not just the routes a
    # Starlette middleware would see. No token (a loopback bind) leaves the
    # app exactly as it always was.
    if token is not None:
        return TokenGate(app, token)
    return app


def serve(
    *,
    host: str,
    port: int,
    api_key: str,
    base_url: str,
    model: str,
    workdir: Path,
    sessions_root: Path | None = None,
    token: str | None = None,
) -> None:
    import uvicorn

    store = SessionStore(sessions_root)

    def factory() -> VllmClient:
        return VllmClient(api_key=api_key, base_url=base_url, model=model)

    # Fail closed: a non-loopback host requires a token even when the
    # caller passed none, so a direct call can never open the server by
    # omission the way the CLI's own resolution does deliberately.
    resolved = (token or chat_token()) if needs_token(host) else None
    app = build_app(store, factory, default_workdir=workdir, token=resolved)
    uvicorn.run(app, host=host, port=port, log_level="warning")
