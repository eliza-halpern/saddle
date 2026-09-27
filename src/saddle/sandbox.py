"""Command execution with a real workdir boundary and background terminals.

Two things the old `run_command` did not do.

**The workdir is a boundary, not a default.** It used to set `cwd` and
nothing else, so `read_file("../../.ssh/id_rsa")` was a normal, working call.
Every path a tool touches is now resolved and required to stay inside the
root, symlinks included -- `Path.resolve()` first, then a containment check,
because a symlink out of the tree resolves out of the tree.

**A command can outlive the turn.** A build or a test suite takes minutes;
blocking the whole conversation on it is what makes an agent feel dead. A
command may be started in the background, its output read while it runs, and
waited on later -- which is what lets the model start work, say what it did,
and come back to the result.

**A command sees only what it needs.** Under `bwrap` the root is built up
from the system directories and the interpreter, read-only; HOME and /tmp
are empty; the workdir is the one writable place, and its `.git` is
read-only again, because hooks and config there are code the host runs
later. The environment is an allowlist, each command gets its own session
and process group, and a lane can take the network away. A `bwrap` that is
installed but cannot start is not trusted, and a lane that needs isolation
refuses to run without it. Where isolation is absent, commands run as the
invoking user and `Sandbox.isolation` says "none" rather than implying
protection that is not there.
"""

from __future__ import annotations

import functools
import os
import shutil
import signal
import subprocess
import sys
import threading
import uuid
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from time import monotonic
from typing import Final, Literal

from saddle import memcap

DEFAULT_TIMEOUT: Final = 120
MAX_CAPTURE: Final = 400_000
"""Per-terminal output cap. Beyond this the head and tail are kept: an
unbounded buffer is how a `yes` loop takes the whole session down."""


class OutsideRootError(ValueError):
    """A path escaped the workdir. Raised before anything is opened."""


class IsolationUnavailableError(RuntimeError):
    """A lane that needs isolation asked for it on a box that cannot give it."""


Network = Literal["host", "none"]

ENV_KEEP: Final = (
    "PATH",
    "HOME",
    "USER",
    "LOGNAME",
    "SHELL",
    "LANG",
    "LANGUAGE",
    "TZ",
    "VIRTUAL_ENV",
    "PYTHONPATH",
    "PYTHONDONTWRITEBYTECODE",
)
"""The only variables of saddle's own environment a command sees, plus any
`LC_*`. An allowlist rather than a `*KEY*`/`*TOKEN*` denylist: a key can be
named anything, and saddle's process holds the model key when it is exported."""


def command_env(extra: Mapping[str, str]) -> dict[str, str]:
    """The environment a command runs with: the allowlist, then `extra`."""
    kept = {
        name: value
        for name, value in os.environ.items()
        if name in ENV_KEEP or name.startswith("LC_")
    }
    return {**kept, "TERM": "dumb", "NO_COLOR": "1", **extra}


HOST_GIT_GUARD: Final = ("-c", "core.fsmonitor=", "-c", "core.hooksPath=/dev/null")
"""Options for every git command saddle itself runs in a tree a command
could have written: no fsmonitor, no hooks. Git runs both as programs, from
config, so a hook or an fsmonitor written into `.git` is code the host
executes the next time saddle stages or commits. Under bwrap a command cannot
write `.git` at all; this is the second layer, for a run without bwrap and a
repo that already carried such config. saddle's own commits, worktrees and
merges need no hook, so nothing a run relies on is lost."""


SYSTEM_DIRS: Final = (
    "/usr",
    "/bin",
    "/sbin",
    "/lib",
    "/lib32",
    "/lib64",
    "/libx32",
    "/etc",
    "/opt",
    "/sys",
)
"""What a command may read besides its workdir and the interpreter. Built up
from an empty root rather than `--ro-bind / /` minus a list: a deny list of
places misses the one nobody thought of (a sibling answer dir, /var/tmp,
another agent's scratch, /run/user sockets)."""

RESOLVER_DIR: Final = "/run/systemd/resolve"
"""Where /etc/resolv.conf points on systemd boxes; needed only with network."""


GATE_TOOLS: Final = ("python", "python3", "pytest", "ruff", "coverage", "mutmut")
"""Names a command runs bare that saddle's own gates also run. Where one of
them lives in a venv outside the system dirs (a project's `.venv`, a
`uv tool` or `pipx` install under HOME), that venv is shown read-only."""


def _venv_root(tool: Path) -> Path | None:
    """The venv holding `tool` (`<venv>/bin/<tool>`), if it is one."""
    root = tool.parent.parent
    return root if (root / "pyvenv.cfg").is_file() else None


def _base_prefix(venv: Path) -> Path | None:
    """Where the venv's interpreter comes from (`home` in `pyvenv.cfg`)."""
    for line in (venv / "pyvenv.cfg").read_text(encoding="utf-8", errors="replace").splitlines():
        key, sep, value = line.partition("=")
        if sep and key.strip() == "home" and value.strip():
            return Path(value.strip()).resolve().parent
    return None


def default_expose(env: Mapping[str, str]) -> tuple[tuple[Path, Path], ...]:
    """Read-only `(source, destination)` binds for the gate tools `env` finds.

    Minimal on purpose: each venv a gate tool (or `VIRTUAL_ENV`) lives in,
    the interpreter that venv was made from, and the tool's own PATH entry
    when that is a link into the venv. Nothing else under HOME: a sibling of
    the venv, the rest of `~/.local/bin`, and dotfiles all stay hidden."""
    binds: dict[Path, Path] = {}
    venvs: list[Path] = []
    if env.get("VIRTUAL_ENV"):
        venvs.append(Path(env["VIRTUAL_ENV"]))
    for name in GATE_TOOLS:
        found = shutil.which(name, path=env.get("PATH", ""))
        if found is None:
            continue
        spelled, landed = Path(found).absolute(), Path(found).resolve()
        for tool in (spelled, landed):
            venv = _venv_root(tool)
            if venv is not None:
                venvs.append(venv)
        if spelled != landed and _venv_root(landed) is not None:
            binds[spelled] = landed
    for venv in venvs:
        if not (venv / "pyvenv.cfg").is_file():
            continue
        real = venv.resolve()
        binds[real] = real
        base = _base_prefix(real)
        if base is not None:
            binds[base] = base
    return tuple((source, dest) for dest, source in sorted(binds.items()) if not _is_system(dest))


def _is_system(path: Path) -> bool:
    """Already visible: under one of `SYSTEM_DIRS`."""
    return any(path == Path(d) or Path(d) in path.parents for d in SYSTEM_DIRS)


@functools.cache
def bwrap_works(bwrap: str) -> str | None:
    """None when `bwrap` can build a sandbox here, else its error text.

    An installed bwrap is not a working one: where unprivileged user
    namespaces are restricted (Ubuntu 24.04's AppArmor default) it exists and
    fails on every call, and trusting `which` labels that "bwrap"."""
    try:
        done = subprocess.run(
            [bwrap, "--ro-bind", "/", "/", "--unshare-all", "true"],
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return str(exc)
    return None if done.returncode == 0 else (done.stderr.strip() or f"exit {done.returncode}")


def resolve_within(root: Path, candidate: str | Path) -> Path:
    """Resolve `candidate` under `root`, or raise `OutsideRoot`.

    Resolution happens first so that a symlink pointing out of the tree is
    caught by where it *lands*, not by how it is spelled.
    """
    base = root.resolve()
    target = (base / Path(candidate)).resolve()
    if target != base and base not in target.parents:
        msg = f"{candidate!r} resolves outside the working directory"
        raise OutsideRootError(msg)
    return target


@dataclass
class Terminal:
    """One command, possibly still running."""

    id: str
    command: str
    started: float
    process: subprocess.Popen[str] | None = None
    chunks: list[str] = field(default_factory=list)
    exit_code: int | None = None
    truncated: int = 0
    _lock: threading.Lock = field(default_factory=threading.Lock)

    @property
    def running(self) -> bool:
        return self.exit_code is None

    def output(self) -> str:
        with self._lock:
            text = "".join(self.chunks)
        if self.truncated:
            head = text[: MAX_CAPTURE // 2]
            tail = text[-MAX_CAPTURE // 2 :]
            return f"{head}\n[... {self.truncated} characters elided ...]\n{tail}"
        return text

    def _append(self, chunk: str) -> None:
        with self._lock:
            self.chunks.append(chunk)
            size = sum(len(c) for c in self.chunks)
            if size > MAX_CAPTURE:
                joined = "".join(self.chunks)
                self.truncated += len(joined) - MAX_CAPTURE
                self.chunks = [joined[: MAX_CAPTURE // 2], joined[-MAX_CAPTURE // 2 :]]


@dataclass
class Sandbox:
    """Command execution rooted at `root`.

    `on_output(terminal_id, chunk)` is called from the reader thread as
    output arrives, so a caller can stream it somewhere. A background
    command outlives the tool call that started it, so its output cannot
    be returned from that call -- it has to be pushed.
    """

    root: Path
    isolation: Literal["bwrap", "none"] = "none"
    terminals: dict[str, Terminal] = field(default_factory=dict)
    on_output: Callable[[str, str], None] | None = None
    env: Mapping[str, str] = field(default_factory=dict)
    """Extra environment for every command, over the scrubbed allowlist."""
    network: Network = "host"
    """"none" gives a command its own empty network namespace (loopback only).
    saddle talks to the model itself, so no command needs the host network to
    do the task; only installs do."""
    expose: tuple[tuple[Path, Path], ...] = ()
    """Read-only `(source, destination)` binds beyond the system dirs and the
    interpreter: the venvs the gate tools live in (`default_expose`)."""
    memory_max: int | None = None
    """Hard memory cap in bytes for each command and everything it starts
    (`memcap`); `for_workdir` sets it from `SADDLE_MEMORY_MAX`."""

    @classmethod
    def for_workdir(
        cls,
        root: Path,
        *,
        prefer_bwrap: bool = True,
        on_output: Callable[[str, str], None] | None = None,
        env: Mapping[str, str] | None = None,
        require_isolation: bool = False,
        network: Network = "host",
    ) -> Sandbox:
        """A sandbox for `root`; with `require_isolation`, never an unisolated one."""
        found = shutil.which("bwrap") if prefer_bwrap else None
        problem = "bwrap is not installed" if found is None else bwrap_works(found)
        if problem is not None and require_isolation:
            msg = f"this lane needs isolation and bwrap cannot provide it: {problem}"
            raise IsolationUnavailableError(msg)
        return cls(
            root=root.resolve(),
            isolation="bwrap" if problem is None else "none",
            on_output=on_output,
            env=dict(env or {}),
            network=network,
            expose=default_expose(command_env(env or {})),
            memory_max=memcap.memory_max(),
        )

    def _git_binds(self) -> list[str]:
        """Read-only binds that keep git usable and its metadata untouchable.

        A writable `.git` is code the host runs later: a hook, a
        `core.fsmonitor`, or a worktree's `.git` file pointed at a repo the
        command made. So `.git` is read-only, and a worktree's real gitdir
        (outside the workdir, hidden otherwise) is shown read-only too."""
        dot = self.root / ".git"
        if dot.is_dir():
            return ["--ro-bind", str(dot), str(dot)]
        if not dot.is_file():
            return []
        binds = ["--ro-bind", str(dot), str(dot)]
        text = dot.read_text(encoding="utf-8", errors="replace").strip()
        if text.startswith("gitdir:"):
            gitdir = (self.root / text.removeprefix("gitdir:").strip()).resolve()
            common = gitdir / "commondir"
            shown = [gitdir]
            if common.is_file():
                shown.append((gitdir / common.read_text(encoding="utf-8").strip()).resolve())
            for path in shown:
                binds += ["--ro-bind-try", str(path), str(path)]
        return binds

    def _argv(self, command: str) -> list[str]:
        """The argv actually executed, wrapped for isolation when available."""
        if self.isolation != "bwrap":
            return ["bash", "-lc", command]
        argv = ["bwrap"]
        for name in SYSTEM_DIRS:
            path = Path(name)
            if path.is_symlink():
                argv += ["--symlink", os.readlink(path), name]
            else:
                argv += ["--ro-bind-try", name, name]
        argv += ["--dev", "/dev", "--proc", "/proc", "--tmpfs", "/tmp"]
        argv += ["--tmpfs", str(Path.home())]
        if self.network == "host":
            argv += ["--ro-bind-try", RESOLVER_DIR, RESOLVER_DIR]
        for prefix in sorted({Path(sys.prefix).resolve(), Path(sys.base_prefix).resolve()}):
            argv += ["--ro-bind", str(prefix), str(prefix)]
        for source, dest in self.expose:
            argv += ["--ro-bind", str(source), str(dest)]
        # Order matters: later mounts shadow earlier ones, so the writable
        # workdir comes after the tmpfs mounts (a workdir under /tmp or HOME
        # would vanish behind them), and the read-only .git after the workdir.
        argv += ["--bind", str(self.root), str(self.root), *self._git_binds()]
        argv += ["--unshare-all"]
        if self.network == "host":
            argv += ["--share-net"]
        return [
            *argv,
            "--new-session",
            "--die-with-parent",
            "--chdir",
            str(self.root),
            "bash",
            "-lc",
            command,
        ]

    def run(self, command: str, *, timeout: int = DEFAULT_TIMEOUT) -> Terminal:
        """Run to completion (or timeout) and return the finished terminal."""
        terminal = self.start(command)
        self.wait(terminal.id, timeout=timeout)
        return terminal

    def start(self, command: str) -> Terminal:
        """Start a command in the background; returns immediately."""
        terminal = Terminal(id=uuid.uuid4().hex[:8], command=command, started=monotonic())
        cap = None if self.memory_max is None else memcap.cap(self.memory_max)
        argv, env = self._argv(command), command_env(self.env)
        if cap is not None:
            argv, env = cap.wrap(argv, env)
        try:
            process = subprocess.Popen(
                argv,
                cwd=str(self.root),
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                bufsize=1,
                env=env,
                start_new_session=True,
            )
        except OSError as exc:
            terminal.exit_code = 127
            terminal._append(f"error: could not start command: {exc}")
            self.terminals[terminal.id] = terminal
            return terminal
        terminal.process = process

        def pump() -> None:
            assert process.stdout is not None
            for line in process.stdout:
                terminal._append(line)
                if self.on_output is not None:
                    try:
                        self.on_output(terminal.id, line)
                    except Exception:  # a broken listener must not stop the command
                        self.on_output = None
            code = process.wait()
            _kill_group(process.pid)  # nothing it started outlives it
            if cap is not None and cap.oom_killed():
                terminal._append(f"\n{cap.reason()}\n")
            terminal.exit_code = code

        threading.Thread(target=pump, daemon=True).start()
        self.terminals[terminal.id] = terminal
        return terminal

    def wait(self, terminal_id: str, *, timeout: float = DEFAULT_TIMEOUT) -> Terminal:
        """Block until the terminal finishes or `timeout` elapses.

        A timeout is not a kill: the command keeps running and can be waited
        on again. That is what makes "start it, tell the user, come back"
        work instead of forcing every long job to finish inside one turn.
        """
        terminal = self.terminals[terminal_id]
        deadline = monotonic() + timeout
        while terminal.running and monotonic() < deadline:
            if terminal.process is not None:
                try:
                    terminal.process.wait(timeout=min(0.2, max(0.0, deadline - monotonic())))
                except subprocess.TimeoutExpired:
                    continue
        return terminal

    def kill(self, terminal_id: str) -> Terminal:
        terminal = self.terminals[terminal_id]
        if terminal.process is not None and terminal.running:
            _kill_group(terminal.process.pid)
            terminal.process.wait(timeout=5)
        return terminal


def _kill_group(pgid: int) -> None:
    """SIGKILL a command's whole process group (its own session: pgid == pid).

    Killing the shell alone leaves its children running; without bwrap's PID
    namespace nothing else reaps them. A child that calls setsid itself
    escapes this, which is one reason unisolated runs are labelled "none"."""
    try:
        os.killpg(pgid, signal.SIGKILL)
    except (ProcessLookupError, PermissionError):
        pass
