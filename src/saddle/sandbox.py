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

Isolation beyond the path boundary is best-effort and honestly labelled:
when `bwrap` is present it is used for a read-only view of the system with
the workdir writable; when it is not, commands run as the invoking user and
`Sandbox.isolation` says so rather than implying protection that is absent.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import threading
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from time import monotonic
from typing import Final, Literal

DEFAULT_TIMEOUT: Final = 120
MAX_CAPTURE: Final = 400_000
"""Per-terminal output cap. Beyond this the head and tail are kept: an
unbounded buffer is how a `yes` loop takes the whole session down."""


class OutsideRootError(ValueError):
    """A path escaped the workdir. Raised before anything is opened."""


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

    @classmethod
    def for_workdir(
        cls, root: Path, *, prefer_bwrap: bool = True,
        on_output: Callable[[str, str], None] | None = None,
    ) -> Sandbox:
        available = prefer_bwrap and shutil.which("bwrap") is not None
        return cls(
            root=root.resolve(),
            isolation="bwrap" if available else "none",
            on_output=on_output,
        )

    def _argv(self, command: str) -> list[str]:
        """The argv actually executed, wrapped for isolation when available."""
        if self.isolation != "bwrap":
            return ["bash", "-lc", command]
        # Order matters: later mounts shadow earlier ones, so the writable
        # workdir bind must come AFTER --tmpfs /tmp or a workdir under /tmp
        # disappears behind the tmpfs and --chdir fails.
        return [
            "bwrap",
            "--ro-bind",
            "/",
            "/",
            "--dev",
            "/dev",
            "--proc",
            "/proc",
            "--tmpfs",
            "/tmp",
            "--bind",
            str(self.root),
            str(self.root),
            "--unshare-all",
            "--share-net",
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
        try:
            process = subprocess.Popen(
                self._argv(command),
                cwd=str(self.root),
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                bufsize=1,
                env={**os.environ, "TERM": "dumb", "NO_COLOR": "1"},
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
            terminal.exit_code = process.wait()

        threading.Thread(target=pump, daemon=True).start()
        self.terminals[terminal.id] = terminal
        return terminal

    def wait(self, terminal_id: str, *, timeout: int = DEFAULT_TIMEOUT) -> Terminal:
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
            terminal.process.kill()
            terminal.process.wait(timeout=5)
        return terminal
