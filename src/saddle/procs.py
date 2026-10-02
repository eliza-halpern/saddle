"""A chat session's process list: what its commands started and left running.

A command that starts a server, a GUI program or a test launch with `setsid`,
`nohup` or a trailing `&` can leave it running after the turn, the lane change
or the session. A record of PIDs misses exactly those: a process that calls
`setsid` leaves its parent's session and process group, and one that forks
twice is reparented to init, so neither is findable from the command's own PID.

What is robust on Linux is a cgroup. Every command saddle starts already runs
in a transient `systemd-run --user --scope` unit of its own (`memcap`), and a
process cannot leave its cgroup by forking, daemonising or calling `setsid`.
So the ledger remembers each command's unit, and the session's processes are
whatever the kernel lists in those units' `cgroup.procs`. Another subreaper
(`prctl(PR_SET_CHILD_SUBREAPER)`) was rejected: it would adopt the orphans of
everything saddle's server starts, not one session's, and it cannot tell one
session's orphan from another's.

Where no user systemd manager is reachable (`memcap.cgroup_problem`) the
ledger falls back to each command's process group, and says so
(`ProcessLedger.tracking`): a program that calls `setsid` is then not seen.

Only a process the ledger recorded can be listed or stopped, so a process of
the same name that something else started is never touched.
"""

from __future__ import annotations

import json
import os
import signal
import subprocess
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Final, Literal

from saddle import memcap

GRACE_S: Final = 1.0
"""How long a stopped process has to exit on SIGTERM before it gets SIGKILL."""

KILL_S: Final = 3.0
"""How long SIGKILL is repeated for a process that keeps appearing."""

MAX_COMMAND: Final = 200
"""Characters of a command line kept in a list entry."""

SHELL: Final = ("bash", "-lc")
"""How `Sandbox._argv` starts every command; systemd-run may spell `bash` by path."""

SCOPE_ROOT: Final = Path("/sys/fs/cgroup")


@dataclass
class Tracked:
    """One command's scope, as the ledger remembers it."""

    unit: str | None
    terminal: str
    command: str
    started: float
    pgid: int
    process: subprocess.Popen[str] | None = field(default=None, compare=False)
    """The command's own process while this saddle holds it; None once the
    ledger was read back from disk (a restart), when only the scope remains."""


@dataclass(frozen=True)
class Entry:
    """One process group still running from a session."""

    id: int
    """The process group: what `stop` takes."""
    pid: int
    command: str
    started: float
    """Epoch seconds the group's first process started."""
    terminal: str
    ran: str
    """The command the session ran that this process descends from."""
    processes: int

    def describe(self) -> str:
        when = time.strftime("%H:%M:%S", time.localtime(self.started))
        more = f" (+{self.processes - 1} more)" if self.processes > 1 else ""
        return f"group {self.id}: {self.command}{more}, started {when}, from `{self.ran}`"

    def as_json(self) -> dict[str, object]:
        return {
            "id": self.id,
            "pid": self.pid,
            "pgid": self.id,
            "command": self.command,
            "started": self.started,
            "terminal": self.terminal,
            "ran": self.ran,
            "processes": self.processes,
        }


def _clip(text: str) -> str:
    return text if len(text) <= MAX_COMMAND else text[: MAX_COMMAND - 1] + "…"


def _stat(pid: int) -> tuple[str, int, int] | None:
    """(state, process group, start ticks since boot) of `pid`, or None if gone."""
    try:
        raw = Path(f"/proc/{pid}/stat").read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None
    rest = raw[raw.rindex(")") + 2 :].split()  # past "pid (comm) "
    return rest[0], int(rest[2]), int(rest[19])


def _alive(pid: int) -> bool:
    """Whether `pid` exists and is not a zombie (a zombie holds no program)."""
    stat = _stat(pid)
    return stat is not None and stat[0] != "Z"


def _is_command_shell(cmdline: str) -> bool:
    """Whether `cmdline` is the `bash -lc` that saddle wraps each command in."""
    argv = cmdline.split(" ", 2)
    return (argv[0].rsplit("/", 1)[-1], *argv[1:2]) == SHELL


def _cmdline(pid: int) -> str:
    try:
        raw = Path(f"/proc/{pid}/cmdline").read_bytes()
    except OSError:
        return ""
    return raw.replace(b"\0", b" ").decode("utf-8", errors="replace").strip()


def _boot_time() -> float:
    for line in Path("/proc/stat").read_text(encoding="utf-8").splitlines():
        if line.startswith("btime "):
            return float(line.split()[1])
    return 0.0  # pragma: no cover - every Linux /proc/stat has btime


def _group_pids(pgid: int) -> list[int]:
    """Live processes of process group `pgid`, by scanning /proc."""
    found = []
    for name in os.listdir("/proc"):
        if name.isdigit():
            stat = _stat(int(name))
            if stat is not None and stat[1] == pgid and stat[0] != "Z":
                found.append(int(name))
    return found


class ProcessLedger:
    """The processes a session's commands started, found again on demand."""

    def __init__(self, path: Path | None = None) -> None:
        self.path = path
        self._lock = threading.RLock()
        self._tracked: list[Tracked] = []
        self._cgroups: dict[str, Path | None] = {}
        if path is not None and path.is_file():
            try:
                rows = json.loads(path.read_text(encoding="utf-8"))
                self._tracked = [
                    Tracked(
                        unit=row["unit"],
                        terminal=row["terminal"],
                        command=row["command"],
                        started=float(row["started"]),
                        pgid=int(row["pgid"]),
                    )
                    for row in rows
                ]
            except (OSError, ValueError, KeyError, TypeError):
                self._tracked = []  # a damaged file is an empty list, never a crash

    @property
    def tracking(self) -> Literal["cgroup", "process group"]:
        """How a detached program is found: by cgroup, or (no user systemd) not at all."""
        return "cgroup" if memcap.cgroup_problem() is None else "process group"

    def record(self, tracked: Tracked) -> None:
        with self._lock:
            self._tracked.append(tracked)
            self._save()

    def _save(self) -> None:
        if self.path is None:
            return
        rows = [
            {
                "unit": t.unit,
                "terminal": t.terminal,
                "command": t.command,
                "started": t.started,
                "pgid": t.pgid,
            }
            for t in self._tracked
        ]
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self.path.write_text(json.dumps(rows), encoding="utf-8")
        except OSError:
            pass  # an unwritten list costs a restart's memory, never a command

    def _scope_dir(self, unit: str) -> Path | None:
        """The unit's cgroup directory, asked of systemd once it exists."""
        if self._cgroups.get(unit) is None:
            shown = subprocess.run(
                [
                    "systemctl",
                    "--user",
                    "show",
                    "--property=ControlGroup",
                    "--value",
                    f"{unit}.scope",
                ],
                capture_output=True,
                text=True,
                check=False,
            )
            value = shown.stdout.strip()
            self._cgroups[unit] = SCOPE_ROOT / value.lstrip("/") if value else None
        return self._cgroups[unit]

    def _pids(self, tracked: Tracked) -> list[int]:
        if tracked.unit is None:
            return _group_pids(tracked.pgid)
        cgroup = self._scope_dir(tracked.unit)
        if cgroup is None:
            return []
        try:
            raw = (cgroup / "cgroup.procs").read_text(encoding="utf-8")
        except OSError:
            return []
        return [pid for pid in map(int, raw.split()) if _alive(pid)]

    def _live(self) -> list[tuple[Tracked, list[int]]]:
        """Each tracked command with its live PIDs; a finished command whose
        scope is empty is forgotten."""
        with self._lock:
            live = []
            kept = []
            for tracked in self._tracked:
                pids = self._pids(tracked)
                running = tracked.process is not None and tracked.process.poll() is None
                if pids or running:
                    kept.append(tracked)
                    live.append((tracked, pids))
                else:
                    self._cgroups.pop(tracked.unit or "", None)
            if len(kept) != len(self._tracked):
                self._tracked = kept
                self._save()
            return live

    def entries(self) -> list[Entry]:
        """One entry per process group still running, oldest first."""
        boot = _boot_time()
        ticks = os.sysconf("SC_CLK_TCK")
        found: dict[int, Entry] = {}
        for tracked, pids in self._live():
            groups: dict[int, list[tuple[int, int]]] = {}
            for pid in pids:
                stat = _stat(pid)
                if (
                    stat is not None
                    and _cmdline(pid).split(" ", 1)[0].rsplit("/", 1)[-1] != "bwrap"
                ):
                    groups.setdefault(stat[1], []).append((stat[2], pid))
            for pgid, members in groups.items():
                members.sort()
                first_tick, pid = members[0]
                shown = _cmdline(pid)
                # The shell saddle wraps a command in is not what the person
                # asked for; the command is.
                if _is_command_shell(shown):
                    shown = tracked.command
                found[pgid] = Entry(
                    id=pgid,
                    pid=pid,
                    command=_clip(shown or "(unknown)"),
                    started=boot + first_tick / ticks,
                    terminal=tracked.terminal,
                    ran=_clip(tracked.command),
                    processes=len(members),
                )
        return sorted(found.values(), key=lambda entry: entry.started)

    def stop(self, entry_id: int) -> Entry | None:
        """Stop one of this session's process groups; None when it is not one."""
        with self._lock:
            entry = next((e for e in self.entries() if e.id == entry_id), None)
            if entry is None:
                return None
            self._terminate(
                lambda: [p for _, pids in self._live() for p in pids if _group(p) == entry_id]
            )
            return entry

    def stop_all(self) -> list[Entry]:
        """Stop everything still running from this session; what it stopped."""
        with self._lock:
            entries = self.entries()
            self._terminate(lambda: [p for _, pids in self._live() for p in pids])
            return entries

    @staticmethod
    def _terminate(find: Callable[[], list[int]]) -> None:
        """SIGTERM what `find` returns until it returns nothing or `GRACE_S`
        passes, then SIGKILL the same way. `find` is asked again each time, so
        a process forked between two looks is found too."""
        for sig, grace in ((signal.SIGTERM, GRACE_S), (signal.SIGKILL, KILL_S)):
            deadline = time.monotonic() + grace
            while True:
                pids = find()
                if not pids:
                    return
                for pid in pids:
                    try:
                        os.kill(pid, sig)
                    except (ProcessLookupError, PermissionError):
                        pass
                if time.monotonic() >= deadline:
                    break
                time.sleep(0.05)


def _group(pid: int) -> int | None:
    stat = _stat(pid)
    return None if stat is None else stat[1]
