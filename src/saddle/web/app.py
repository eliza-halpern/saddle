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
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Final
from urllib.parse import urlencode

from starlette.applications import Starlette
from starlette.concurrency import run_in_threadpool
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

from saddle import capabilities
from saddle.auto import DEFAULT_TIME_BUDGET_S, DEFAULT_TOKEN_BUDGET, AutoError, repo_root
from saddle.engine import TurnOptions, _user_message
from saddle.engine import run_turn as run_turn  # an injection seam: the tests replace it
from saddle.events import (
    ApprovalRequest,
    ApprovalSettled,
    ErrorEvent,
    Event,
    MessageDelivered,
    MessageQueued,
    PasswordRequest,
    PasswordSettled,
    SessionInfo,
    SessionTitle,
    TaskState,
    TerminalOutput,
)
from saddle.feed import Arm, default_auditor
from saddle.feed import AuditorFactory as FeedAuditorFactory
from saddle.installs import WheelFolder
from saddle.journal import ProofRecord, SpanRecord, append_span, build_span, read_entries
from saddle.labels import label_for
from saddle.memory import estimate_tokens
from saddle.packet import Packet, compile_packet, display_record, render_packet_text
from saddle.procs import ProcessLedger
from saddle.sandbox import OutsideRootError, resolve_within
from saddle.sessions import (
    BUILTIN_PERSONAS,
    DEFAULT_TITLE,
    SESSION_MODES,
    FullAccessRefusedError,
    SessionStore,
)
from saddle.sideeffects import SideEffects
from saddle.titles import title_for, words_title
from saddle.tools import PREVIEWABLE, ToolContext, attach_mcp, preview_for, scope_turn
from saddle.undo import UndoLog
from saddle.vision import is_image_followup
from saddle.vllm import VllmClient
from saddle.web import branch_actions, tasks
from saddle.web.tasks import SMALL_LANE_TEST_EDITS, TaskRun

MAX_RUN_ROWS = 20
"""The Runs group lists this many, newest first."""

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
        if is_image_followup(message):
            continue  # what read_file showed the model, not something the person said
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


PASSWORD_WAIT_S: Final = 300
"""How long a `sudo` password request waits for the person before the command
is refused, as if they had cancelled."""


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
    chatting: bool = False
    """Busy with a chat turn, not a Task run: only a chat turn takes messages
    written while it runs."""
    cancelled: bool = False
    lock: threading.Lock = field(default_factory=threading.Lock)
    waiting: list[tuple[str, list[str]]] = field(default_factory=list)
    """Messages written during the running chat turn, oldest first, as (text,
    image paths): the turn takes them before its next model request, and one
    it never took starts the next turn."""
    processes: ProcessLedger | None = None
    """The session's process list (`ChatServer.ledger`)."""
    passwords: dict[str, queue.Queue[str | None]] = field(default_factory=dict)
    """Open `sudo` password requests (#125), by id, each waiting on the page."""
    approvals: dict[str, queue.Queue[bool]] = field(default_factory=dict)
    """Open approval requests (#139, #93), by id, each waiting on the page."""

    def ask_password(self, prompt: str) -> str | None:
        """Ask the page for a full-access command's `sudo` password; None if
        the person cancels or nobody answers within `PASSWORD_WAIT_S`. The
        password only passes through here: it is returned to the helper and
        never published, stored or logged."""
        request_id = uuid.uuid4().hex[:12]
        answer: queue.Queue[str | None] = queue.Queue(maxsize=1)
        self.passwords[request_id] = answer
        self.publish(PasswordRequest(id=request_id, prompt=prompt))
        try:
            return answer.get(timeout=PASSWORD_WAIT_S)
        except queue.Empty:
            return None
        finally:
            self.passwords.pop(request_id, None)
            self.publish(PasswordSettled(id=request_id))

    def ask_approval(self, title: str, lines: list[str]) -> bool:
        """Ask the page for a yes or no (an MCP server's descriptions, a large
        download, a command running what the web reader brought back); no if the
        person declines or nobody answers within `PASSWORD_WAIT_S`."""
        request_id = uuid.uuid4().hex[:12]
        answer: queue.Queue[bool] = queue.Queue(maxsize=1)
        self.approvals[request_id] = answer
        self.publish(ApprovalRequest(id=request_id, title=title, lines=tuple(lines)))
        try:
            return answer.get(timeout=PASSWORD_WAIT_S)
        except queue.Empty:
            return False
        finally:
            self.approvals.pop(request_id, None)
            self.publish(ApprovalSettled(id=request_id))

    def offer(self, text: str, images: list[str]) -> str:
        """Take a message: "start" (the session was idle and is now busy with a
        chat turn), "queued" (a chat turn is running and will take it), or
        "refused" (a Task run holds the session). Queued under the same lock
        the turn's end reads, so a message is never left with no turn to take
        it; the page hears it is waiting before the turn can say it was taken."""
        with self.lock:
            if not self.busy:
                self.busy = self.chatting = True
                return "start"
            if not self.chatting:
                return "refused"
            self.waiting.append((text, images))
            for channel in self.subscribers:  # unbounded queues: never blocks
                channel.put(MessageQueued(text=text))
            return "queued"

    def take_waiting(self) -> list[tuple[str, list[str]]]:
        """Every message waiting for the running turn, oldest first."""
        with self.lock:
            taken, self.waiting = self.waiting, []
        return taken

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
        keep_reasoning: bool = True,
        wheels: WheelFolder | None = None,
        extract_requirements: bool = False,
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
        self.keep_reasoning = keep_reasoning
        """Whether a chat-started run keeps its reasoning (`AutoOptions.keep_reasoning`)."""
        self.wheels = wheels
        """`saddle web --allow-installs`: the wheel folder every chat-started run
        may install from, with the user's approval (`AutoOptions.wheels`)."""
        self.extract_requirements = extract_requirements
        """`saddle web --extract-requirements` (or its setting): every chat-started
        run extracts the task text's examples beside the worker and runs P1 at
        tier 1, at question strength (`AutoOptions.extract_requirements`)."""
        self.index_lock = threading.Lock()
        self.indexed: dict[str, str] = {}
        """run id -> the state last written to the run index."""

    def _live(self, session_id: str) -> Live:
        return self.live.setdefault(session_id, Live())

    def end_processes(self, session_id: str, *, revoke: bool) -> list[dict[str, object]]:
        """Stop everything still running from the session; what it stopped, as
        JSON. `revoke` also ends full access on the live context."""
        live = self._live(session_id)
        ledger = self.ledger(session_id)
        if live.context is None:
            entries = ledger.stop_all()
        else:
            entries = live.context.stop_processes()
            if revoke:
                live.context.revoke_full_access()
        return [entry.as_json() for entry in entries]

    def ledger(self, session_id: str) -> ProcessLedger:
        """The session's process list, kept on disk so a restart still finds
        what its commands left running."""
        live = self._live(session_id)
        if live.processes is None:
            live.processes = ProcessLedger(self.store.processes_path(session_id))
        return live.processes

    def _context_window(self, client: Any) -> int:
        """Ask the server once; fall back to a conservative default."""
        if self.window is None:
            try:
                self.window = client.max_model_len() or 120_000
            except Exception:  # a server that will not say gets the default
                self.window = 120_000
        return self.window

    def _name_session(self, session: Any, text: str | None, client: Any, live: Live) -> None:
        """Name a new session after its opening message, once.

        Deliberately after the turn, not before it: the answer is already on
        screen by the time this costs anything, and a session that is never
        answered does not need a name. Failures are swallowed -- a session
        called "New session" is a cosmetic problem, and a turn that died
        because its title could not be written would not be.
        """
        # A retry (text None) asks nothing new to name the session after.
        if text is None or not session.auto_title or live.turn != 1:
            return
        try:
            title = title_for(client, text)
            if not title:
                return
            self.store.update(session.id, title=title, auto_title=False)
        except Exception:
            return
        live.publish(SessionTitle(session_id=session.id, title=title))

    def _name_from_task(self, session_id: str, text: str) -> None:
        """Name a still-unnamed session after the first task started in it.

        Same rule as a chat turn's title: never over a name the user chose
        (`auto_title` is false once they rename), and only once.
        """
        session = self.store.get(session_id)
        # A session made with a name already has one; only the default is replaced.
        if not session.auto_title or session.title != DEFAULT_TITLE:
            return
        title = words_title(text)
        if not title:
            return
        self.store.update(session_id, title=title, auto_title=False)
        self._live(session_id).publish(SessionTitle(session_id=session_id, title=title))

    def _note_run(self, run: TaskRun) -> None:
        """Write the run's row to its session's run index (survives a restart)."""
        with self.index_lock:
            last = self.indexed.get(run.run_id)
            if last != run.state:
                run.state_since = time.time()
                if run.state in tasks.ENDED_STATES:
                    run.ended = run.state_since
                self.indexed[run.run_id] = run.state
            try:
                self.store.record_run(run.session_id, run.index_row())
            except (OSError, ValueError):
                pass  # a lost index row costs a sidebar entry, never the run

    def _publisher(self, run: TaskRun, live: Live) -> Any:
        def publish(event: Event | None) -> None:
            if isinstance(event, TaskState):
                self._note_run(run)
            live.publish(event)

        return publish

    # -- turn ------------------------------------------------------------

    def _run(
        self,
        session_id: str,
        text: str | None,
        images: list[str] | None = None,
        *,
        queued: bool = False,
    ) -> None:
        """One chat turn. `queued`: `text` was written during the turn before
        this one, which ended without taking it."""
        live = self._live(session_id)
        live.cancelled = False
        messages: list[dict[str, Any]] | None = None
        begun = False
        try:
            session = self.store.get(session_id)
            messages = self.store.load_messages(session_id)
            workdir = Path(session.workdir)
            if (
                live.context is None
                or live.context.workdir != workdir
                or live.context.full_access != session.full_access
            ):
                # Terminal output arrives on the reader thread, after the tool
                # call that started it has already returned, so it is pushed
                # to the session's subscribers rather than yielded by the turn.
                # Rebuilt when full access changes, so its sandbox follows it.
                if live.context is not None and live.context.mcp is not None:
                    live.context.mcp.close()  # the old context's servers end with it
                live.context = ToolContext(
                    workdir=workdir,
                    on_output=lambda tid, chunk: live.publish(TerminalOutput(id=tid, chunk=chunk)),
                    undo=UndoLog(self.store.undo_dir(session_id)),
                    full_access=session.full_access,
                    effects=SideEffects(self.store.outside_dir(session_id)),
                    ask_password=live.ask_password,
                    approve=live.ask_approval,
                    processes=self.ledger(session_id),
                    images=True,
                )
            # Every turn, so a capability the person switched on or off applies now.
            attach_mcp(live.context, self.store.downloads_dir(session_id))
            tools = scope_turn(live.context, session.mode)
            for problem in live.context.mcp.unreported() if live.context.mcp is not None else []:
                live.publish(ErrorEvent(message=problem))  # allowlisted, but not in use
            live.turn += 1
            with self.client_factory() as client:
                begun = True
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
                    steer=lambda: [
                        (said, [Path(raw) for raw in shown]) for said, shown in live.take_waiting()
                    ],
                ):
                    live.publish(event)
                    if queued and text is not None and event.kind == "turn.start":
                        live.publish(MessageDelivered(text=text))
                    if event.kind == "turn.end":
                        live.parent = getattr(event, "proof", None)
                self._name_session(session, text, client, live)
            self.store.save_messages(session_id, messages)
        except Exception as exc:  # a dead turn must not take the server with it
            reason = f"{type(exc).__name__}: {exc}"
            live.publish(ErrorEvent(message=f"the turn stopped: {reason}"))
            self._keep_dead_turn(session_id, live.turn, text, images, messages, begun, reason)
        finally:
            with live.lock:
                following = live.waiting.pop(0) if live.waiting else None
                if following is None:
                    live.busy = live.chatting = False
            live.publish(None)
            if following is not None:
                # Written during this turn and never taken: it is the next
                # turn's question, still busy, so nothing can start between.
                threading.Thread(
                    target=self._run,
                    args=(session_id, *following),
                    kwargs={"queued": True},
                    daemon=True,
                ).start()

    def _keep_dead_turn(
        self,
        session_id: str,
        turn: int,
        text: str | None,
        images: list[str] | None,
        messages: list[dict[str, Any]] | None,
        begun: bool,
        reason: str,
    ) -> None:
        """What a turn did before it died is real work: seal it in the journal
        and keep its messages, so a reload shows it and the next turn starts
        from it. Each step guards itself, so none can hide the others or the
        error already published."""
        try:
            append_span(
                self.store.journal_path(session_id),
                build_span(
                    node_id=f"chat#{turn}",
                    argv=["turn", "failed"],
                    duration_ms=0,
                    exit_code=1,
                    detail=reason,
                    name="turn_failed",
                ),
            )
        except Exception:  # the journal failing must not hide the rest
            pass
        if messages is None:
            return
        try:
            if not begun and text is not None:  # died before the engine took the question
                messages.append(_user_message(text, [Path(raw) for raw in (images or [])]))
            answered = {m.get("tool_call_id") for m in messages if m.get("role") == "tool"}
            last = next((m for m in reversed(messages) if m.get("role") == "assistant"), None)
            for call in (last or {}).get("tool_calls") or []:
                if call["id"] not in answered:  # a request with no result is rejected by the server
                    messages.append(
                        {
                            "role": "tool",
                            "tool_call_id": call["id"],
                            "content": f"error: the turn stopped before this ran ({reason})",
                        }
                    )
            self.store.save_messages(session_id, messages)
        except Exception:
            pass

    # -- task -----------------------------------------------------------

    def _run_task(self, session_id: str, run: TaskRun) -> None:
        """Run one chat-started task on `saddle auto`'s own path."""
        live = self._live(session_id)
        publish = self._publisher(run, live)
        try:
            session = self.store.get(session_id)
            publish(run.state_event())
            audit = self.auditor(run) if self.auditor is not None else None
            with self.client_factory() as client:
                _verdict, recap = tasks.execute(
                    run,
                    workdir=Path(session.workdir),
                    client=client,
                    publish=publish,
                    chat_journal=self.store.journal_path(session_id),
                    reasoning_effort=session.reasoning_effort,
                    audit=audit,
                    arm=self.arm,
                    feed_auditor=self.feed_auditor,
                    allow_test_edits=run.allow_test_edits,
                    keep_reasoning=self.keep_reasoning,
                    wheels=self.wheels,
                    extract_requirements=self.extract_requirements,
                )
            if recap is not None:
                messages = self.store.load_messages(session_id)
                messages.append(recap)
                self.store.save_messages(session_id, messages)
        except Exception as exc:  # a dead run must not take the server with it
            run.state = "failed"
            run.end_spend()
            publish(run.state_event(f"{type(exc).__name__}: {exc}"))
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
    keep_reasoning: bool = True,
    wheels: WheelFolder | None = None,
    extract_requirements: bool = False,
) -> ASGIApp:
    server = ChatServer(
        store,
        client_factory,
        default_workdir=default_workdir,
        auditor=auditor,
        arm=arm,
        feed_auditor=feed_auditor,
        allow_test_edits=allow_test_edits,
        keep_reasoning=keep_reasoning,
        wheels=wheels,
        extract_requirements=extract_requirements,
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
        for name in ("app.css", "markdown.js", "tasks.js", "notify.js", "runs.js", "app.js"):
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
        store.purge_expired()
        latest: dict[str, TaskRun] = {}
        for run in list(server.tasks.values()):
            latest[run.session_id] = run
        rows = []
        for session in store.list():
            newest = latest.get(session.id)
            row = dict(session.__dict__)
            if newest is not None:
                shown: tuple[str, str] | None = (newest.state, newest.task)
            else:
                # Nothing in memory (a restart, above all): the session's own
                # chat journal still names its latest ended run.
                shown = tasks.latest_run_ref(store.journal_path(session.id))
            row["run_state"] = shown[0] if shown is not None else None
            row["run_task"] = shown[1] if shown is not None else None
            rows.append(row)
        return JSONResponse(rows)

    async def list_runs(_: Request) -> JSONResponse:
        """Every run in every session, newest first: the sidebar's Runs group.

        Read from each session's run index on disk, so runs outlive a server
        restart, with this server's live runs laid over it. A run the index
        says was still going but this server does not hold was cut off by a
        restart: `live` is false and the page says so.
        """
        titles = {s.id: s.title for s in store.list()}
        rows: dict[str, dict[str, Any]] = {}
        for sid in titles:
            for row in store.runs(sid):
                if "run_id" in row:
                    rows[str(row["run_id"])] = {**row, "session_id": sid, "live": False}
        for run in list(server.tasks.values()):
            if run.session_id in titles:
                rows[run.run_id] = {
                    **run.index_row(),
                    "session_id": run.session_id,
                    "live": True,
                    "phase": run.phase,
                }
        out = []
        for row in rows.values():
            row["session_title"] = titles[row["session_id"]]
            out.append(row)
        out.sort(key=lambda r: float(r.get("started") or 0), reverse=True)
        return JSONResponse({"now": time.time(), "runs": out[:MAX_RUN_ROWS]})

    async def create_session(request: Request) -> JSONResponse:
        body = await request.json() if await request.body() else {}
        session = store.create(
            title=body.get("title") or DEFAULT_TITLE,
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
        return JSONResponse(
            {
                "test_edits": server.allow_test_edits,
                "task_text_check": server.extract_requirements,
            }
        )

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
        if "full_access" in body:
            return JSONResponse(
                {"error": "full access is set at /full-access, with its confirmation"},
                status_code=400,
            )
        sid = request.path_params["sid"]
        before = store.get(sid)
        session = store.update(sid, **body)
        stopped: list[dict[str, object]] = []
        if before.mode == "edit" and session.mode != "edit":
            # Leaving Edit ends full access (`SessionStore.update`) and every
            # command still running from the session: stop them now, not at
            # the next turn, and say what was stopped.
            stopped = server.end_processes(sid, revoke=before.full_access)
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
        return JSONResponse({**session.__dict__, "stopped": stopped})

    async def full_access(request: Request) -> JSONResponse:
        """Turn the session's full access on (only with `FULL_ACCESS_CONFIRM`)
        or off. Off takes effect at once: commands still running outside the
        sandbox are stopped and the next command is sandboxed again."""
        sid = request.path_params["sid"]
        body = await request.json()
        on = body.get("on") is True
        try:
            session = store.set_full_access(sid, on, confirm=str(body.get("confirm", "")))
        except FullAccessRefusedError as exc:
            return JSONResponse({"error": str(exc)}, status_code=400)
        stopped = [] if on else server.end_processes(sid, revoke=True)
        return JSONResponse({**session.__dict__, "stopped": stopped})

    async def processes(request: Request) -> JSONResponse:
        """The session's process list: what its commands started and left running."""
        sid = request.path_params["sid"]
        store.get(sid)
        ledger = server.ledger(sid)
        return JSONResponse(
            {"tracking": ledger.tracking, "processes": [e.as_json() for e in ledger.entries()]}
        )

    async def stop_processes(request: Request) -> JSONResponse:
        """Stop one process group of the session's list ({"id"}) or all of it
        ({"all": true}). An id that is not in the list is refused: only the
        session's own processes can be stopped here."""
        sid = request.path_params["sid"]
        store.get(sid)
        body = await request.json()
        ledger = server.ledger(sid)
        if body.get("all") is True:
            return JSONResponse({"stopped": server.end_processes(sid, revoke=False)})
        target = body.get("id")
        entry = (
            ledger.stop(target)
            if isinstance(target, int) and not isinstance(target, bool)
            else None
        )
        if entry is None:
            return JSONResponse(
                {"error": "not one of this session's processes; nothing was stopped"},
                status_code=404,
            )
        return JSONResponse({"stopped": [entry.as_json()]})

    async def outside(request: Request) -> JSONResponse:
        """The session's side-effect record (#137): what its full-access commands
        and file tools changed outside the folder, what was fetched or installed,
        what could not be tracked, and the programs still running (the process
        list's entries, so the page can link the two)."""
        sid = request.path_params["sid"]
        store.get(sid)
        view = SideEffects(store.outside_dir(sid)).view()
        view["processes"] = [e.as_json() for e in server.ledger(sid).entries()]
        return JSONResponse(view)

    async def outside_undo(request: Request) -> JSONResponse:
        """Put back what the record has a backup of. Removing the files the
        session created needs `{"delete_created": true, "confirm": true}`: the
        page asks first, and the server refuses the one without the other."""
        sid = request.path_params["sid"]
        store.get(sid)
        body = await request.json()
        delete = body.get("delete_created") is True
        if delete and body.get("confirm") is not True:
            return JSONResponse(
                {"error": "removing created files needs the person's confirmation"},
                status_code=400,
            )
        record = SideEffects(store.outside_dir(sid))
        done = record.undo(delete_created=delete)
        return JSONResponse({**done, "record": record.view()})

    async def password(request: Request) -> JSONResponse:
        """The person's answer to a `sudo` password request: {"id", "password"}
        or {"id", "cancel": true}. Handed to the waiting command and dropped;
        the reply never repeats it."""
        live = server.live.get(request.path_params["sid"])
        body = await request.json()
        waiting = live.passwords.get(str(body.get("id"))) if live is not None else None
        if waiting is None:
            return JSONResponse({"error": "no such password request"}, status_code=404)
        given = body.get("password")
        waiting.put(None if body.get("cancel") or not isinstance(given, str) else given)
        return JSONResponse({"ok": True})

    async def capabilities_status(_: Request) -> JSONResponse:
        """Which opt-in capabilities are on and working: the page's later panel
        reads this. Asks the search backend, so it runs off the event loop."""
        try:
            rows = await run_in_threadpool(capabilities.status)
        except capabilities.CapabilityError as exc:
            return JSONResponse({"error": str(exc)}, status_code=500)
        return JSONResponse({"capabilities": [row.as_json() for row in rows]})

    async def approval(request: Request) -> JSONResponse:
        """The person's answer to an approval request: {"id", "approve": bool}."""
        live = server.live.get(request.path_params["sid"])
        body = await request.json()
        waiting = live.approvals.get(str(body.get("id"))) if live is not None else None
        if waiting is None:
            return JSONResponse({"error": "no such approval request"}, status_code=404)
        waiting.put(body.get("approve") is True)
        return JSONResponse({"ok": True})

    async def delete_session(request: Request) -> JSONResponse:
        """Hide the session; it is removed once the undo window has passed.

        `?now=1` removes a session already hidden, which is what the page
        sends when its undo toast runs out. A session that was never hidden
        is never removed in one step.
        """
        sid = request.path_params["sid"]
        if request.query_params.get("now") == "1":
            if store.get(sid).deleted_at is None:
                return JSONResponse({"error": "restore or delete it first"}, status_code=409)
            stopped = server.end_processes(sid, revoke=True)
            store.delete(sid)
            server.live.pop(sid, None)
            return JSONResponse({"ok": True, **({"stopped": stopped} if stopped else {})})
        stopped = server.end_processes(sid, revoke=True)
        store.trash(sid)
        server.live.pop(sid, None)
        return JSONResponse({"ok": True, **({"stopped": stopped} if stopped else {})})

    async def restore_session(request: Request) -> JSONResponse:
        return JSONResponse(store.restore(request.path_params["sid"]).__dict__)

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
        # While a chat turn runs, the message joins it at its next request to
        # the model rather than being refused (F40): what the person sees on
        # screen is often what the model needs, and Stop would lose the turn.
        taken = server._live(sid).offer(text, images)
        if taken == "refused":
            return JSONResponse({"error": "a turn is already running"}, status_code=409)
        if taken == "queued":
            return JSONResponse({"ok": True, "queued": True})
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
        # 0 or less is "no limit" (engine.NO_LIMIT), the default; clamp so the
        # sealed record and the meters read a clean 0, never a negative.
        time_s = max(0.0, time_s)
        token_budget = max(0, token_budget)
        allow_test_edits = body.get("allow_test_edits", server.allow_test_edits)
        premise_check = body.get("premise_check", False)
        stall_check = body.get("stall_check", False)
        if not all(isinstance(b, bool) for b in (allow_test_edits, premise_check, stall_check)):
            return JSONResponse(
                {"error": "allow_test_edits, premise_check and stall_check must be booleans"},
                status_code=400,
            )
        session = store.get(sid)
        # A Task run works in a git worktree, so it cannot start where there is
        # no repository. Refuse up front with a clear message rather than start
        # a run that dies in setup and leaves a failed card.
        try:
            repo_root(Path(session.workdir))
        except AutoError:
            return JSONResponse(
                {
                    "error": (
                        f"{session.workdir} is not a git repository, so a Task run "
                        "cannot start here. Pick a repo folder, or run `git init` in it."
                    )
                },
                status_code=400,
            )
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
            premise_check=premise_check,
            stall_check=stall_check,
        )
        server.tasks[run.run_id] = run
        server._note_run(run)
        server._name_from_task(sid, text)
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
        # for a run in flight too. A web run's ledger is `auto.ledger_path(repo, rid)`; a path
        # of any other shape names no repo, and the check is left out.
        parts = journal.parts
        shaped = len(parts) >= 4 and parts[-4:-2] == (".saddle", "runs")
        repo = Path(*parts[:-4]) if shaped else None
        return JSONResponse(compile_packet(journal, run_id=rid, anchor_repo=repo).payload())

    def write_report(journal: Path, rid: str) -> tuple[Path, str]:
        """The full packet as text, written beside the run's ledger.

        `packet.md` sits in `.saddle/runs/<id>/` next to `proofs.jsonl`: inside
        the session's folder, so the Ask lane's read_file can open it when
        asked, and rewritten on every read here so it never lags the ledger.
        The bytes are `render_packet_text` of the packet, unchanged.
        """
        text = render_packet_text(compile_packet(journal, run_id=rid))
        path = journal.parent / "packet.md"
        path.write_text(text, encoding="utf-8")
        return path, text

    async def task_report(request: Request) -> Response:
        """`packet.md` as a download: the full packet, fresh from the ledger."""
        sid, rid = request.path_params["sid"], request.path_params["rid"]
        journal = _journal(sid, rid)
        if journal is None:
            return JSONResponse({"error": "no such task in this session"}, status_code=404)
        path, text = write_report(journal, rid)
        log = store.journal_path(sid).parent / "actions.log"
        branch_actions.log_action(log, rid, "download", str(path))
        return PlainTextResponse(
            text,
            media_type="text/markdown",
            headers={"Content-Disposition": f'attachment; filename="saddle-packet-{rid[:8]}.md"'},
        )

    async def task_record(request: Request) -> JSONResponse:
        """One sealed ledger record of a run, by its hash, so a session line can
        open the evidence it cites. Reads only a run this session recorded, and
        only a hash that run's own journal holds; a miss is a failure, never an
        empty record."""
        sid, rid = request.path_params["sid"], request.path_params["rid"]
        rhash = request.path_params["hash"]
        journal = _journal(sid, rid)
        if journal is None:
            return JSONResponse({"error": "no such task in this session"}, status_code=404)
        for entry in read_entries(journal):
            if entry.record_hash == rhash and isinstance(entry, ProofRecord | SpanRecord):
                return JSONResponse(display_record(entry))
        return JSONResponse({"error": "no such record in this run"}, status_code=404)

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
        journal = _journal(request.path_params["sid"], _rid)
        assert journal is not None  # _branch_context found it
        report, _text = write_report(journal, _rid)
        return JSONResponse(
            {
                "branch": branch,
                "exists": branch_actions.branch_exists(root, branch),
                "target": branch_actions.current_branch(root),
                "merge_refusal": branch_actions.merge_refusal(packet),
                "upstream": branch_actions.upstream_name(root),
                "push_refusal": branch_actions.push_refusal(root),
                "guarded_paths": list(packet.guarded_paths),
                "approve_refusal": branch_actions.approve_refusal(packet),
                "push_branch": {
                    "branch": branch_actions.current_branch(root),
                    "remote": branch_actions.branch_remote(root),
                    "refusal": branch_actions.push_branch_refusal(root),
                    "on_main": branch_actions.on_main_branch(root),
                },
                "recap": render_packet_text(packet),
                "report": str(report),
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
            elif action == "approve":
                paths = str(body.get("paths") or "")
                said = branch_actions.approve_merge(root, packet, branch, confirm, paths)
            elif action == "push-branch":
                said = branch_actions.push_branch(root, confirm)
            elif action == "merge-push":
                said = branch_actions.merge_and_push(root, packet, branch, confirm)
            else:
                said = branch_actions.discard(root, branch, confirm)
        except branch_actions.ActionRefusedError as exc:
            branch_actions.log_action(log, rid, f"{action} refused", str(exc))
            return _refused(exc)
        branch_actions.log_action(log, rid, action, said)
        return JSONResponse({"ok": True, "output": said, "sealed": False})

    async def task_merge(request: Request) -> JSONResponse:
        return _act(request, "merge", await request.json())

    async def task_approve(request: Request) -> JSONResponse:
        return _act(request, "approve", await request.json())

    async def task_push_branch(request: Request) -> JSONResponse:
        return _act(request, "push-branch", await request.json())

    async def task_merge_push(request: Request) -> JSONResponse:
        return _act(request, "merge-push", await request.json())

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
        with live.lock:
            live.chatting = True  # a turn will run: from here a message joins it
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
                    full_access=session.full_access,
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
                    yield f"data: {json.dumps(run.phase_event().payload())}\n\n"
                    for line in list(run.lines):
                        yield f"data: {json.dumps(line.payload())}\n\n"
                    progress = run.progress_event()
                    if progress is not None:
                        yield f"data: {json.dumps(progress.payload())}\n\n"
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
            Route("/api/sessions/{sid}/restore", restore_session, methods=["POST"]),
            Route("/api/sessions/{sid}/full-access", full_access, methods=["POST"]),
            Route("/api/sessions/{sid}/processes", processes, methods=["GET"]),
            Route("/api/sessions/{sid}/processes/stop", stop_processes, methods=["POST"]),
            Route("/api/sessions/{sid}/outside", outside, methods=["GET"]),
            Route("/api/sessions/{sid}/outside/undo", outside_undo, methods=["POST"]),
            Route("/api/sessions/{sid}/password", password, methods=["POST"]),
            Route("/api/sessions/{sid}/approval", approval, methods=["POST"]),
            Route("/api/capabilities", capabilities_status, methods=["GET"]),
            Route("/api/runs", list_runs),
            Route("/api/sessions/{sid}/messages", get_messages),
            Route("/api/sessions/{sid}/file", workdir_file),
            Route("/api/sessions/{sid}/upload", upload, methods=["POST"]),
            Route("/api/sessions/{sid}/message", post_message, methods=["POST"]),
            Route("/api/sessions/{sid}/rewind", rewind_preview),
            Route("/api/sessions/{sid}/rewind", rewind, methods=["POST"]),
            Route("/api/sessions/{sid}/stop", stop_turn, methods=["POST"]),
            Route("/api/sessions/{sid}/task", post_task, methods=["POST"]),
            Route("/api/sessions/{sid}/tasks/{rid}/packet", task_packet),
            Route("/api/sessions/{sid}/tasks/{rid}/packet.md", task_report),
            Route("/api/sessions/{sid}/tasks/{rid}/record/{hash}", task_record),
            Route("/api/sessions/{sid}/tasks/{rid}/branch", task_branch),
            Route("/api/sessions/{sid}/tasks/{rid}/diff", task_diff),
            Route("/api/sessions/{sid}/tasks/{rid}/merge", task_merge, methods=["POST"]),
            Route("/api/sessions/{sid}/tasks/{rid}/approve", task_approve, methods=["POST"]),
            Route(
                "/api/sessions/{sid}/tasks/{rid}/push-branch", task_push_branch, methods=["POST"]
            ),
            Route("/api/sessions/{sid}/tasks/{rid}/merge-push", task_merge_push, methods=["POST"]),
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
    keep_reasoning: bool = True,
    wheels: WheelFolder | None = None,
    extract_requirements: bool = False,
) -> None:
    import uvicorn

    store = SessionStore(sessions_root)

    def factory() -> VllmClient:
        return VllmClient(api_key=api_key, base_url=base_url, model=model)

    # Fail closed: a non-loopback host requires a token even when the
    # caller passed none, so a direct call can never open the server by
    # omission the way the CLI's own resolution does deliberately.
    resolved = (token or chat_token()) if needs_token(host) else None
    app = build_app(
        store,
        factory,
        default_workdir=workdir,
        token=resolved,
        keep_reasoning=keep_reasoning,
        wheels=wheels,
        extract_requirements=extract_requirements,
    )
    uvicorn.run(app, host=host, port=port, log_level="warning")
