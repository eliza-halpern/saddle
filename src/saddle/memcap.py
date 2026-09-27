"""A hard memory and task cap for every command that runs a tree's code.

A test suite can allocate without bound, and a fork bomb is one line
of shell. Either one, uncapped, is charged to the whole machine: the box
swaps, then the kernel picks a victim, and the victim can be saddle itself
or anything else the user is running. A command that ran away is a failing
command, and saddle records it as one.

The cap is a cgroup of the command's own: `systemd-run --user --scope`
puts the command (and everything it starts) in a transient scope with
`MemoryMax`, no swap, and `TasksMax`. The kernel then kills inside that
scope and nowhere else, and systemd remembers why (`Result=oom-kill`), so
the kill can be told apart from any other SIGKILL and named.

Where no user systemd manager is reachable (a container, a CI runner) the
fallback is `prlimit --as`, an address-space ceiling per process. It is
weaker in two ways, which is why it is only the fallback: it caps each
process rather than the tree a command starts, and it counts address space
reserved rather than memory used, so a program that reserves more than it
touches fails under it for no reason. `Cap.kind` says which one applied.

The value comes from `SADDLE_MEMORY_MAX` (bytes, or a number with a K, M,
G or T suffix, powers of 1024) and defaults to 6 GiB.
"""

from __future__ import annotations

import functools
import os
import re
import shutil
import subprocess
import uuid
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Final, Literal

DEFAULT_MEMORY_MAX: Final = 6 * 1024**3
"""Six GiB: what a real test suite in scope needs with room to spare, and
well under what a workstation can lose without falling over."""

TASKS_MAX: Final = 4096
"""Processes and threads one command may hold at once. A parallel test run
uses tens; a fork bomb reaches thousands within a second."""

BUS_VARS: Final = ("XDG_RUNTIME_DIR", "DBUS_SESSION_BUS_ADDRESS")
"""What `systemd-run` needs to reach the user manager, and what the command
it starts must not inherit when its own environment did not have them: the
bus is how a process asks the user manager to start anything at all."""

MEMORY_MAX_ENV: Final = "SADDLE_MEMORY_MAX"
_SIZE: Final = re.compile(r"([1-9][0-9]*)([KMGT]?)", re.IGNORECASE)


def memory_max(default: int = DEFAULT_MEMORY_MAX) -> int:
    """The configured cap in bytes: `SADDLE_MEMORY_MAX`, else `default`.

    A value that does not parse is an error, not the default: a typo in a
    safety limit must not silently become a different limit."""
    raw = os.environ.get(MEMORY_MAX_ENV, "").strip()
    if not raw:
        return default
    match = _SIZE.fullmatch(raw)
    if match is None:
        msg = f"{MEMORY_MAX_ENV}={raw!r}: expected bytes or a number with K, M, G or T"
        raise ValueError(msg)
    return int(match.group(1)) * 1024 ** "_KMGT".index(match.group(2).upper() or "_")


@functools.cache
def cgroup_problem() -> str | None:
    """None when this box can give one command a capped scope, else why not."""
    exe = shutil.which("systemd-run")
    if exe is None:
        return "systemd-run is not installed"
    try:
        done = subprocess.run(
            [exe, "--user", "--scope", "--quiet", "--collect", "-p", "MemoryMax=64M", "--", "true"],
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return str(exc)
    return None if done.returncode == 0 else (done.stderr.strip() or f"exit {done.returncode}")


@dataclass(frozen=True)
class Cap:
    """How one command is capped: the argv prefix, and its scope if any."""

    prefix: tuple[str, ...]
    kind: Literal["cgroup", "rlimit"]
    limit: int
    unit: str | None = None

    def wrap(
        self, argv: Sequence[str], env: Mapping[str, str] | None
    ) -> tuple[list[str], dict[str, str] | None]:
        """`argv` and `env` to launch so the command runs under this cap.

        `env=None` means the caller's own environment, which already holds
        whatever `systemd-run` needs. A scrubbed `env` gets the bus variables
        for `systemd-run` and loses them again, through `env -u`, before the
        command starts."""
        if self.kind == "rlimit" or env is None:
            return [*self.prefix, *argv], None if env is None else dict(env)
        lent = {k: os.environ[k] for k in BUS_VARS if k in os.environ and k not in env}
        unset = [flag for name in lent for flag in ("-u", name)]
        middle = ["env", *unset, "--"] if unset else []
        return [*self.prefix, *middle, *argv], {**env, **lent}

    def oom_killed(self) -> bool:
        """Whether the kernel killed anything in the command's scope for
        exceeding the cap: the command itself, or a child it survived.

        Only a cgroup cap can say: an rlimit shows up as the program's own
        `MemoryError`. systemd keeps a scope that saw an OOM kill, in the
        failed state, until asked (`--collect` would drop the answer with
        it); this asks, then clears it. Called after every capped command,
        whatever its exit, so no failed scope is left behind."""
        if self.unit is None:
            return False
        scope = f"{self.unit}.scope"
        shown = subprocess.run(
            ["systemctl", "--user", "show", "--property=Result", "--value", scope],
            capture_output=True,
            text=True,
            check=False,
        )
        subprocess.run(
            ["systemctl", "--user", "reset-failed", scope], capture_output=True, check=False
        )
        return shown.stdout.strip() == "oom-kill"

    def reason(self) -> str:
        """The line a killed command's record carries."""
        return (
            f"killed: the command exceeded saddle's memory cap of {self.limit} bytes"
            f" ({MEMORY_MAX_ENV}); recorded as a failure"
        )


def cap(limit: int) -> Cap:
    """The cap for one command: its own scope when possible, else an rlimit."""
    if cgroup_problem() is None:
        unit = f"saddle-cmd-{uuid.uuid4().hex[:16]}"
        prefix = (
            "systemd-run",
            "--user",
            "--scope",
            "--quiet",
            f"--unit={unit}",
            "-p",
            f"MemoryMax={limit}",
            "-p",
            "MemorySwapMax=0",
            "-p",
            f"TasksMax={TASKS_MAX}",
            "--",
        )
        return Cap(prefix=prefix, kind="cgroup", limit=limit, unit=unit)
    return Cap(prefix=("prlimit", f"--as={limit}", "--"), kind="rlimit", limit=limit)
