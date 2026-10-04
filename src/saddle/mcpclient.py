"""An MCP (Model Context Protocol) client for the Edit lane (#139).

saddle borrows maintained servers instead of writing its own tools: a person
names the servers, and the exact tools of each, in an allowlist file; saddle
starts them over stdio with the official SDK (`initialize`, `tools/list`,
`tools/call`) and offers only the named tools, one schema each, so a small
model never meets a server's whole tool list.

What holds, and where:

- **Only the person's allowlist.** `load_config` reads `mcp.json` from the
  saddle config directory (`CONFIG_ENV` overrides the path). A server has a
  command that names its pinned version, an `access` (`acting`: the session
  that edits files may call it; `reader`: only the quarantined reader may,
  #93), and the exact tools it exposes. A malformed file is an error naming
  the fault, never an empty list.
- **Descriptions are data, approved by the person.** A server's tool
  descriptions come from the server. They are shown to the person when the
  server is first allowed (`saddle mcp approve`), and the approval is bound
  to a fingerprint of the command and of each exposed tool's name,
  description and schema, so a server that changes a description after an
  update is refused until the person approves again.
- **Servers live in the session's process list.** A server starts the way a
  command does: in the session's sandbox (none for a full-access session),
  inside its own memory-capped scope, recorded in the session's
  `procs.ProcessLedger`. Ending access stops it, and the next call says so.
- **A failure is named.** A server that cannot start, crashes, hangs past its
  timeout or answers with an error is an `McpError` saying which and why,
  never an empty result.

Limits, stated: a full-access session can edit the allowlist and the
approvals like any other file in the person's home, so the allowlist
confines a sandboxed session, not a full-access one.
"""

from __future__ import annotations

import asyncio
import hashlib
import importlib
import json
import os
import re
import shlex
import shutil
import tempfile
import threading
import time
from collections.abc import Callable, Mapping
from concurrent.futures import Future
from contextlib import suppress
from dataclasses import dataclass, field, replace
from pathlib import Path
from types import ModuleType
from typing import Any, Final, Literal, TextIO

from saddle.procs import Tracked
from saddle.sandbox import Sandbox, also_exposing, command_env, default_expose

CONFIG_ENV: Final = "SADDLE_MCP_CONFIG"
"""Overrides where the allowlist is read from."""
APPROVALS_ENV: Final = "SADDLE_MCP_APPROVALS"
"""Overrides where the person's approvals are kept."""
DEFAULT_CONFIG: Final = Path("~/.config/saddle/mcp.json")
DEFAULT_APPROVALS: Final = Path("~/.config/saddle/mcp-approved.json")

PREFIX: Final = "mcp__"
"""A tool the model sees is `mcp__<server>__<tool>`."""

Access = Literal["acting", "reader"]
ACCESSES: Final = ("acting", "reader")

START_TIMEOUT_S: Final = 120.0
"""How long a server has to start and list its tools (an `npx` or `uvx` first
run downloads its package)."""
CALL_TIMEOUT_S: Final = 120.0
"""How long one tool call has to answer before the server is stopped."""
STDERR_TAIL: Final = 600
"""Characters of a server's stderr quoted in a failure."""

_NAME: Final = re.compile(r"[a-z][a-z0-9_-]{0,31}")
_TOOL: Final = re.compile(r"[A-Za-z0-9_-]{1,64}")
MAX_NAME: Final = 64
"""The longest function name a model is offered."""


class McpConfigError(ValueError):
    """The allowlist is unreadable or breaks a rule. Names the server and why."""


class McpError(RuntimeError):
    """A server could not be used: refused, not started, crashed, hung or
    answering with an error. The message is shown to the model and the person."""


@dataclass(frozen=True)
class ServerSpec:
    """One allowlisted server."""

    name: str
    command: tuple[str, ...]
    version: str
    access: Access
    tools: tuple[str, ...]
    expose: tuple[str, ...] = ()
    """Commands shown read-only inside the server's sandbox (as `SADDLE_EXPOSE`
    does for gates): the `node`, `npx` or `uvx` an installed server needs."""
    expose_paths: tuple[str, ...] = ()
    """Directories shown read-only at their own path inside the server's sandbox:
    where an installed server and its runtime live (`~/.hermes/node`, a
    `node_modules`), which a command name alone does not show."""


@dataclass(frozen=True)
class ToolInfo:
    """A tool as the server describes it. Data from the server, never trusted."""

    name: str
    description: str
    schema: dict[str, Any]


def config_path(environ: Mapping[str, str] | None = None) -> Path:
    env = os.environ if environ is None else environ
    return Path(env.get(CONFIG_ENV) or DEFAULT_CONFIG).expanduser()


def approvals_path(environ: Mapping[str, str] | None = None) -> Path:
    env = os.environ if environ is None else environ
    return Path(env.get(APPROVALS_ENV) or DEFAULT_APPROVALS).expanduser()


def _spec(name: str, raw: object) -> ServerSpec:
    if not _NAME.fullmatch(name):
        msg = (
            f"server name {name!r}: use a lowercase letter, then lowercase letters, "
            "digits, - or _ (32 at most)"
        )
        raise McpConfigError(msg)
    if not isinstance(raw, dict):
        msg = f"server {name!r}: expected an object with command, version, access and tools"
        raise McpConfigError(msg)
    unknown = sorted(set(raw) - {"command", "version", "access", "tools", "expose", "expose_paths"})
    if unknown:
        msg = f"server {name!r}: unknown key {', '.join(unknown)}"
        raise McpConfigError(msg)
    command = raw.get("command")
    if (
        not isinstance(command, list)
        or not command
        or not all(isinstance(part, str) and part for part in command)
    ):
        msg = f"server {name!r}: command must be a non-empty list of strings"
        raise McpConfigError(msg)
    version = raw.get("version")
    if not isinstance(version, str) or not version:
        msg = f"server {name!r}: version is required (the pinned version the command runs)"
        raise McpConfigError(msg)
    if not any(version in part for part in command):
        msg = (
            f"server {name!r}: the command does not name its pinned version {version!r}; "
            "pin it in the command itself (pkg==1.2.3, pkg@1.2.3)"
        )
        raise McpConfigError(msg)
    access = raw.get("access")
    if access not in ACCESSES:
        msg = (
            f'server {name!r}: access must be "acting" or "reader" '
            "(reader: only the web reader may call it)"
        )
        raise McpConfigError(msg)
    tools = raw.get("tools")
    if (
        not isinstance(tools, list)
        or not tools
        or not all(isinstance(tool, str) and _TOOL.fullmatch(tool) for tool in tools)
    ):
        msg = f"server {name!r}: tools must be a non-empty list of exact tool names (no wildcards)"
        raise McpConfigError(msg)
    if len(set(tools)) != len(tools):
        msg = f"server {name!r}: a tool is listed twice"
        raise McpConfigError(msg)
    too_long = [tool for tool in tools if len(f"{PREFIX}{name}__{tool}") > MAX_NAME]
    if too_long:
        msg = f"server {name!r}: {too_long[0]!r} makes a tool name over {MAX_NAME} characters"
        raise McpConfigError(msg)
    expose = raw.get("expose", [])
    if not isinstance(expose, list) or not all(
        isinstance(item, str) and re.fullmatch(r"[A-Za-z0-9._+-]+", item) for item in expose
    ):
        msg = f"server {name!r}: expose must be a list of command names (node, npx, uvx)"
        raise McpConfigError(msg)
    paths = raw.get("expose_paths", [])
    if not isinstance(paths, list) or not all(
        isinstance(item, str) and item.startswith(("/", "~")) for item in paths
    ):
        msg = f"server {name!r}: expose_paths must be a list of absolute paths (or ~/...)"
        raise McpConfigError(msg)
    return ServerSpec(
        name, tuple(command), version, access, tuple(tools), tuple(expose), tuple(paths)
    )


def load_config(path: Path | None = None) -> dict[str, ServerSpec]:
    """The allowlist; empty when the file does not exist, an error when it is
    present and wrong."""
    where = path if path is not None else config_path()
    try:
        text = where.read_text(encoding="utf-8")
    except FileNotFoundError:
        return {}
    except OSError as exc:
        msg = f"cannot read the MCP allowlist {where}: {exc}"
        raise McpConfigError(msg) from exc
    try:
        data = json.loads(text)
    except ValueError as exc:
        msg = f"the MCP allowlist {where} is not valid JSON: {exc}"
        raise McpConfigError(msg) from exc
    servers = data.get("servers") if isinstance(data, dict) else None
    if not isinstance(servers, dict):
        msg = f'the MCP allowlist {where} must be an object with a "servers" object'
        raise McpConfigError(msg)
    return {name: _spec(name, raw) for name, raw in servers.items()}


def fingerprint(spec: ServerSpec, tools: list[ToolInfo]) -> str:
    """What an approval is bound to: the command, version and each exposed
    tool's name, description and input schema, in a fixed order."""
    body = {
        "command": list(spec.command),
        "version": spec.version,
        "access": spec.access,
        "expose": list(spec.expose),
        "expose_paths": list(spec.expose_paths),
        "tools": sorted(
            ({"name": t.name, "description": t.description, "schema": t.schema} for t in tools),
            key=lambda row: str(row["name"]),
        ),
    }
    return hashlib.sha256(json.dumps(body, sort_keys=True).encode()).hexdigest()


class Approvals:
    """The person's approvals: server name -> the fingerprint they approved."""

    def __init__(self, path: Path | None = None) -> None:
        self.path = path if path is not None else approvals_path()

    def _read(self) -> dict[str, str]:
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}  # no approvals, or an unreadable file: nothing is approved
        if not isinstance(data, dict):
            return {}
        return {str(k): v for k, v in data.items() if isinstance(v, str)}

    def knows(self, name: str) -> bool:
        """Whether the person approved this server once, in some form."""
        return name in self._read()

    def is_approved(self, spec: ServerSpec, tools: list[ToolInfo]) -> bool:
        return self._read().get(spec.name) == fingerprint(spec, tools)

    def approve(self, spec: ServerSpec, tools: list[ToolInfo]) -> None:
        data = self._read()
        data[spec.name] = fingerprint(spec, tools)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps(data, indent=2, sort_keys=True), encoding="utf-8")


def render_review(spec: ServerSpec, tools: list[ToolInfo]) -> str:
    """What the person reads before approving: the command and, for each
    exposed tool, the description the server gave, verbatim."""
    lines = [
        f"server {spec.name!r} ({spec.access}): {shlex.join(spec.command)}",
        "These descriptions come from the server, not from saddle:",
    ]
    for tool in tools:
        lines.append(f"\n  {tool.name}\n    {tool.description or '(no description)'}")
        lines.append(f"    arguments: {json.dumps(tool.schema.get('properties', {}))}")
    return "\n".join(lines)


def tool_schema(spec: ServerSpec, tool: ToolInfo) -> dict[str, Any]:
    """The function schema the model is offered for one exposed tool."""
    schema = tool.schema if tool.schema.get("type") == "object" else {"type": "object"}
    return {
        "type": "function",
        "function": {
            "name": f"{PREFIX}{spec.name}__{tool.name}",
            "description": f"[MCP server {spec.name}] {tool.description}".strip(),
            "parameters": schema,
        },
    }


def clip_result(text: str, limit: int, count: Callable[[str], int | None] | None) -> str:
    """`text` cut to whole lines that fit `limit` tokens (the model's tokenizer
    when `count` is given, else `limit` // 4 lines), with what was left out said."""
    lines = text.splitlines(keepends=True)
    fits = len(lines)
    if count is not None and (whole := count(text)) is not None:
        if whole <= limit:
            return text
        low, high = 0, len(lines)
        while high - low > 1:
            mid = (low + high) // 2
            size = count("".join(lines[:mid]))
            if size is None or size <= limit:
                low = mid
            else:
                high = mid
        fits = low
    elif len(lines) > limit // 4:
        fits = limit // 4
    if fits >= len(lines):
        return text
    return (
        "".join(lines[:fits])
        + f"\n[{len(lines) - fits} more lines of the server's result were left out]"
    )


SDK_MISSING: Final = (
    "the MCP SDK is not installed: install saddle with its mcp extra "
    "(pip install 'saddle-harness[mcp]')"
)


def command_missing(spec: ServerSpec) -> str | None:
    """Why `spec`'s command cannot start here (its program is not installed), or None."""
    program = spec.command[0]
    if shutil.which(program) is None and not Path(program).expanduser().exists():
        return f"{program} is not installed"
    return None


def load_sdk() -> ModuleType:
    """`saddle.mcpsdk`, or `McpError` saying the extra is not installed."""
    try:
        return importlib.import_module("saddle.mcpsdk")
    except ImportError as exc:
        msg = f"{SDK_MISSING} ({exc})"
        raise McpError(msg) from exc


def sdk_problem() -> str | None:
    """None when the SDK is installed, else why MCP is unavailable."""
    try:
        load_sdk()
    except McpError as exc:
        return str(exc)
    return None


def _find_child(marker: str) -> int | None:
    """The pid of this process's child whose environment carries `marker`."""
    needle = f"SADDLE_MCP_ID={marker}".encode()
    me = os.getpid()
    for name in os.listdir("/proc"):
        if not name.isdigit():
            continue
        try:
            stat = Path(f"/proc/{name}/stat").read_text(encoding="utf-8", errors="replace")
            parent = int(stat[stat.rindex(")") + 2 :].split()[1])
            if parent == me and needle in Path(f"/proc/{name}/environ").read_bytes().split(b"\0"):
                return int(name)
        except (OSError, ValueError):
            continue
    return None


@dataclass
class _Conn:
    """One live server: the task that owns its client and the calls queued to it."""

    spec: ServerSpec
    tools: list[ToolInfo]
    queue: asyncio.Queue[tuple[str, dict[str, Any], float, Future[Any]] | None]
    task: asyncio.Task[None]
    errlog: TextIO


@dataclass
class McpHost:
    """A session's MCP servers: started on demand, called by name, stopped
    with the session's access."""

    config: Mapping[str, ServerSpec]
    approvals: Approvals
    box: Callable[[], Sandbox]
    """The session's sandbox when a server is started (`ToolContext.box`)."""
    ask: Callable[[str, list[str]], bool] | None = None
    """Asks the person (a title and the lines to read; their yes or no) when a
    server is not approved, so the page can show its descriptions and record the
    approval, as `saddle mcp approve` does. None asks nobody."""
    start_timeout: float = START_TIMEOUT_S
    call_timeout: float = CALL_TIMEOUT_S
    problems: dict[str, str] = field(default_factory=dict)
    """Servers that are allowlisted but unusable, and why."""
    _told: set[str] = field(default_factory=set, repr=False)
    _loop: asyncio.AbstractEventLoop | None = field(default=None, repr=False)
    _conns: dict[str, _Conn] = field(default_factory=dict, repr=False)
    _lock: threading.RLock = field(default_factory=threading.RLock, repr=False)
    _tasks: set[asyncio.Task[None]] = field(default_factory=set, repr=False)

    # -- the loop thread ----------------------------------------------------

    def _run(self, coroutine: Any, timeout: float) -> Any:
        with self._lock:
            if self._loop is None:
                loop = asyncio.new_event_loop()
                threading.Thread(target=loop.run_forever, daemon=True, name="mcp").start()
                self._loop = loop
            loop = self._loop
        return asyncio.run_coroutine_threadsafe(coroutine, loop).result(timeout)

    # -- starting a server ----------------------------------------------------

    async def _serve(
        self, spec: ServerSpec, ready: Future[_Conn], box: Sandbox, errlog: TextIO, sdk: ModuleType
    ) -> None:
        """The task that owns one server's client for its whole life."""
        marker = os.urandom(8).hex()
        argv, env, cap = box.stdio_launch("exec " + shlex.join(spec.command))
        queue: asyncio.Queue[Any] = asyncio.Queue()
        registrar = asyncio.create_task(self._register(spec, box, marker, cap.unit))
        try:
            async with asyncio.timeout(self.start_timeout):
                client_cm = sdk.client(
                    argv, {**env, "SADDLE_MCP_ID": marker}, str(box.root), errlog
                )
                await client_cm.__aenter__()
            try:
                task = asyncio.current_task()
                assert task is not None
                listed = [ToolInfo(*row) for row in await sdk.list_all(client_cm)]
                conn = _Conn(spec, listed, queue, task, errlog)
                ready.set_result(conn)
                while (item := await queue.get()) is not None:
                    name, arguments, timeout, answer = item
                    try:
                        async with asyncio.timeout(timeout):
                            answer.set_result(await client_cm.call_tool(name, arguments))
                    except BaseException as exc:
                        answer.set_exception(exc)
                        raise  # any failure here is the connection's: the server is done
            finally:
                await client_cm.__aexit__(None, None, None)
        except BaseException as exc:
            if not ready.done():
                ready.set_exception(exc)
        finally:
            registrar.cancel()

    async def _register(
        self, spec: ServerSpec, box: Sandbox, marker: str, unit: str | None
    ) -> None:
        """Enter the server in the session's process list once its process exists."""
        while True:  # ended by `_serve`: this task is cancelled with the server's
            pid = _find_child(marker)
            if pid is not None:
                if box.ledger is not None:
                    box.ledger.record(
                        Tracked(
                            unit=unit,
                            terminal=f"mcp:{spec.name}",
                            command=f"MCP server {spec.name}: {shlex.join(spec.command)}",
                            started=time.time(),
                            pgid=pid,
                            saddle=True,  # saddle's own: never the model's to stop (F41)
                        )
                    )
                return
            await asyncio.sleep(0.02)

    @staticmethod
    def _shown(spec: ServerSpec, box: Sandbox) -> Sandbox:
        """`box` with what the server's entry names shown read-only inside it: its
        commands (`expose`) and its directories (`expose_paths`). A directory that
        is not there is a named failure, before anything starts."""
        if not (spec.expose or spec.expose_paths):
            return box
        with also_exposing(spec.expose):
            shown = default_expose(command_env(box.env))
        where = [Path(item).expanduser() for item in spec.expose_paths]
        absent = [str(path) for path in where if not path.exists()]
        if absent:
            msg = (
                f"MCP server {spec.name!r} could not start: expose_paths names {absent[0]}, "
                "which does not exist"
            )
            raise McpError(msg)
        return replace(box, expose=(*shown, *((path.resolve(), path) for path in where)))

    def _connect(self, spec: ServerSpec) -> _Conn:
        with self._lock:
            conn = self._conns.get(spec.name)
            if conn is not None and not conn.task.done():
                return conn
            self._conns.pop(spec.name, None)
        sdk = load_sdk()
        box = self._shown(spec, self.box())
        errlog = tempfile.TemporaryFile("w+", encoding="utf-8")
        ready: Future[_Conn] = Future()

        async def start() -> None:
            self._tasks.add(asyncio.create_task(self._serve(spec, ready, box, errlog, sdk)))

        self._run(start(), 10)
        try:
            conn = ready.result(self.start_timeout + 5)
        except TimeoutError as exc:
            msg = f"MCP server {spec.name!r} did not start within {self.start_timeout:g}s"
            raise McpError(msg) from exc
        except Exception as exc:
            tail = _stderr(errlog)
            msg = f"MCP server {spec.name!r} could not start: {_describe(exc)}{tail}"
            raise McpError(msg) from exc
        with self._lock:
            self._conns[spec.name] = conn
        return conn

    # -- the person's view and the model's -----------------------------------

    def _exposed(self, spec: ServerSpec, conn: _Conn) -> list[ToolInfo]:
        by_name = {t.name: t for t in conn.tools}
        missing = [name for name in spec.tools if name not in by_name]
        if missing:
            msg = (
                f"MCP server {spec.name!r} does not offer {', '.join(missing)}, which the "
                "allowlist names; it was refused"
            )
            raise McpError(msg)
        return [by_name[name] for name in spec.tools]

    def review(self, name: str) -> tuple[ServerSpec, list[ToolInfo], bool]:
        """Start the server and return its spec, its exposed tools as the
        server describes them, and whether the person has approved exactly
        these. Used before the first approval and by `saddle mcp`."""
        spec = self.config.get(name)
        if spec is None:
            msg = f"{name!r} is not in the MCP allowlist"
            raise McpError(msg)
        tools = self._exposed(spec, self._connect(spec))
        return spec, tools, self.approvals.is_approved(spec, tools)

    def schemas(self, access: Access = "acting") -> list[dict[str, Any]]:
        """The schemas of every approved tool of every `access` server. A server
        that cannot be used contributes nothing and is named in `problems`."""
        offered: list[dict[str, Any]] = []
        for name, spec in self.config.items():
            if spec.access != access:
                continue
            try:
                _, tools, approved = self.review(name)
            except McpError as exc:
                self.problems[name] = str(exc)
                continue
            if not approved and self.ask is not None:
                title = (
                    f"MCP server {name} changed since you approved it"
                    if self.approvals.knows(name)
                    else f"Allow MCP server {name}?"
                )
                if self.ask(title, render_review(spec, tools).splitlines()):
                    self.approvals.approve(spec, tools)
                    approved = True
            if not approved:
                self.problems[name] = (
                    f"MCP server {name!r} is not approved (or changed since it was): "
                    f"run `saddle mcp approve {name}`"
                )
                self.stop(name)
                continue
            self.problems.pop(name, None)
            offered += [tool_schema(spec, tool) for tool in tools]
        return offered

    def unreported(self) -> list[str]:
        """The `problems` the person has not been told yet, each once, so a server
        that is allowlisted but not usable is said to them and not skipped
        silently."""
        fresh = [text for text in self.problems.values() if text not in self._told]
        self._told.update(fresh)
        return fresh

    def owns(self, tool_name: str, access: Access = "acting") -> bool:
        """Whether `tool_name` is `mcp__server__tool` of an allowlisted `access`
        server's exposed tools (by the config alone: it starts nothing)."""
        return self.split(tool_name, access) is not None

    def split(self, tool_name: str, access: Access) -> tuple[ServerSpec, str] | None:
        for spec in self.config.values():
            head = f"{PREFIX}{spec.name}__"
            if spec.access == access and tool_name.startswith(head):
                tool = tool_name[len(head) :]
                if tool in spec.tools:
                    return spec, tool
        return None

    # -- calling -------------------------------------------------------------

    def call(
        self,
        tool_name: str,
        arguments: dict[str, Any],
        *,
        access: Access,
        limit_tokens: int,
        count: Callable[[str], int | None] | None = None,
    ) -> str:
        """Call `mcp__server__tool`. Refused unless the server is allowlisted for
        `access`, the tool is one the allowlist exposes, and the person has
        approved exactly what the server describes now."""
        found = self.split(tool_name, access)
        if found is None:
            msg = f"{tool_name!r} is not an MCP tool this session may call"
            raise McpError(msg)
        spec, tool = found
        conn = self._connect(spec)
        exposed = self._exposed(spec, conn)
        if not self.approvals.is_approved(spec, exposed):
            self.stop(spec.name)
            msg = (
                f"MCP server {spec.name!r} is not approved, or its tool descriptions changed "
                f"since the person approved it: run `saddle mcp approve {spec.name}`"
            )
            raise McpError(msg)
        declared = next(t for t in exposed if t.name == tool).schema.get("properties")
        if isinstance(declared, dict) and (extra := sorted(set(arguments) - set(declared))):
            msg = (
                f"{tool} does not take {', '.join(extra)}; its arguments are "
                f"{', '.join(sorted(declared)) or 'none'}"
            )
            raise McpError(msg)
        answer: Future[Any] = Future()

        async def send() -> None:
            await conn.queue.put((tool, arguments, self.call_timeout, answer))

        self._run(send(), 10)
        try:
            result = answer.result(self.call_timeout + 10)
        except TimeoutError as exc:
            self.stop(spec.name)
            msg = (
                f"MCP server {spec.name!r} did not answer {tool!r} within "
                f"{self.call_timeout:g}s; the server was stopped"
            )
            raise McpError(msg) from exc
        except Exception as exc:
            tail = _stderr(conn.errlog)
            self.stop(spec.name)
            msg = (
                f"MCP server {spec.name!r} stopped while running {tool!r} "
                f"({_describe(exc)}){tail}; no result was returned"
            )
            raise McpError(msg) from exc
        return clip_result(load_sdk().render_result(tool_name, result), limit_tokens, count)

    # -- stopping ------------------------------------------------------------

    def stop(self, name: str) -> None:
        """Stop one server (it is started again on its next use)."""
        with self._lock:
            conn = self._conns.pop(name, None)
        if conn is None:
            return

        async def end() -> None:
            await conn.queue.put(None)
            await asyncio.wait({conn.task})

        with suppress(Exception):  # a server that will not stop is the ledger's to kill
            self._run(end(), 60)
        conn.errlog.close()

    def close(self) -> None:
        """Stop every server and the loop thread."""
        for name in list(self._conns):
            self.stop(name)
        with self._lock:
            loop, self._loop = self._loop, None
        if loop is not None:
            loop.call_soon_threadsafe(loop.stop)


def _describe(exc: BaseException) -> str:
    while isinstance(exc, BaseExceptionGroup) and len(exc.exceptions) == 1:
        exc = exc.exceptions[0]  # the SDK wraps a failed start in a task group
    return f"{type(exc).__name__}: {exc}" if str(exc) else type(exc).__name__


def _stderr(errlog: TextIO) -> str:
    try:
        errlog.flush()
        errlog.seek(0)
        tail = errlog.read().strip()[-STDERR_TAIL:]
    except (OSError, ValueError):
        return ""
    return f"; its last stderr: {tail}" if tail else ""
