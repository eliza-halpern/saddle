"""A hard memory and task cap on every command that runs a tree's code.

Both sides of each constraint (CLAUDE.md: a constraint is verified by a
known-good instance, never by its existence):

- known-bad: a runaway allocation, one spread over several processes, and a
  process storm are stopped, recorded as a failure with a reason, and saddle
  carries on; the same holds for the agent's `run_command` and for the gate
  runners (`run_capture` with a `memory_limit`, which is how pytest, coverage
  and mutmut are launched);
- known-good: a normal test run passes under the cap, and a program that
  reserves more address space than the cap but touches little of it is not
  refused (the address-space ceiling refused it for no reason).

Allocations are a few hundred MiB against a 256 MiB test cap, so a failing
cap costs the box nothing.
"""

from __future__ import annotations

import importlib
import os
import shlex
import shutil
import subprocess
import sys
from pathlib import Path
from types import ModuleType

import pytest

from saddle import evidence
from saddle.journal import SpanRecorder, read_spans
from saddle.sandbox import Sandbox

PY = shlex.quote(sys.executable)
MIB = 1024**2
TEST_CAP = "256M"


def _cgroup_available() -> bool:
    """Probed here rather than through saddle, so the file stays importable."""
    exe = shutil.which("systemd-run")
    if exe is None:  # pragma: no cover - the dev box has it
        return False
    done = subprocess.run(
        [exe, "--user", "--scope", "--quiet", "--collect", "--", "true"],
        capture_output=True,
        check=False,
    )
    return done.returncode == 0


needs_cgroup = pytest.mark.skipif(
    not _cgroup_available(), reason="no user systemd manager to hold a scope"
)
HAS_BWRAP = shutil.which("bwrap") is not None


def memcap() -> ModuleType:
    return importlib.import_module("saddle.memcap")


def hog(mib: int) -> str:
    """Python that touches `mib` MiB and then says so."""
    return f"{PY} -c " + shlex.quote(f"b = b'x' * ({mib} * {MIB}); print('allocated')")


def spread_hog(workers: int, mib: int) -> list[str]:
    """`workers` processes of `mib` MiB each, alive together; exit 1 if any died."""
    each = f"b = b'x' * ({mib} * {MIB}); import time; time.sleep(2)"
    script = (
        "pids=''; "
        f'for i in $(seq {workers}); do {PY} -c {shlex.quote(each)} & pids="$pids $!"; done; '
        "rc=0; for p in $pids; do wait $p || rc=1; done; echo spread-done; exit $rc"
    )
    return ["bash", "-c", script]


@pytest.fixture
def capped(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SADDLE_MEMORY_MAX", TEST_CAP)


# -- configuration -------------------------------------------------------------


def test_the_cap_defaults_to_six_gib(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("SADDLE_MEMORY_MAX", raising=False)
    assert memcap().memory_max() == 6 * 1024**3


@pytest.mark.parametrize(
    ("raw", "expected"),
    [("1073741824", 1024**3), ("512M", 512 * MIB), ("2g", 2 * 1024**3), ("64K", 64 * 1024)],
)
def test_the_cap_reads_saddle_memory_max(
    monkeypatch: pytest.MonkeyPatch, raw: str, expected: int
) -> None:
    monkeypatch.setenv("SADDLE_MEMORY_MAX", raw)
    assert memcap().memory_max() == expected


@pytest.mark.parametrize("raw", ["lots", "0", "1.5G", "-1G", "12X"])
def test_a_cap_that_does_not_parse_is_an_error_not_the_default(
    monkeypatch: pytest.MonkeyPatch, raw: str
) -> None:
    monkeypatch.setenv("SADDLE_MEMORY_MAX", raw)
    with pytest.raises(ValueError, match="SADDLE_MEMORY_MAX"):
        memcap().memory_max()


# -- gate runners: run_capture with a memory_limit ------------------------------


@needs_cgroup
def test_a_gate_run_that_allocates_past_the_cap_is_killed_and_recorded(
    tmp_path: Path, capped: None
) -> None:
    journal = tmp_path / "proofs.jsonl"
    run = evidence.run_shell_capture(hog(512), tmp_path, recorder=SpanRecorder(journal, "n1"))
    assert run.exit_code != 0
    assert not run.timed_out
    assert "allocated" not in run.stdout
    assert "memory cap" in run.stderr
    (span,) = read_spans(journal)
    assert span.exit_code == run.exit_code
    assert "memory cap" in span.detail


@needs_cgroup
def test_the_gate_cap_covers_every_process_a_run_starts(tmp_path: Path) -> None:
    # Four 120 MiB workers: each under the 256 MiB cap alone, twice it together.
    # A per-process ceiling lets all four through; the run's own cgroup does not.
    argv = spread_hog(4, 120)
    run = evidence.run_capture(argv, tmp_path, memory_limit=256 * MIB)
    assert "spread-done" in run.stdout  # the shell itself lived to report
    assert run.exit_code != 0, run.stdout + run.stderr


@needs_cgroup
def test_a_gate_run_cannot_start_a_process_storm(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(memcap(), "TASKS_MAX", 32)
    storm = "for i in $(seq 100); do sleep 3 & done 2>&1; wait; echo storm-done"
    run = evidence.run_capture(["bash", "-c", storm], tmp_path, memory_limit=256 * MIB)
    assert "Resource temporarily unavailable" in run.stdout + run.stderr


@needs_cgroup
def test_a_normal_suite_passes_under_the_cap(tmp_path: Path, capped: None) -> None:
    (tmp_path / "calc.py").write_text("def add(a, b):\n    return a + b\n")
    (tmp_path / "test_calc.py").write_text(
        "from calc import add\n\n\ndef test_add():\n    assert add(2, 2) == 4\n"
    )
    run = evidence.run_shell_capture(f"{PY} -m pytest -q -p no:cacheprovider", tmp_path)
    assert run.exit_code == 0, run.stdout + run.stderr
    assert "1 passed" in run.stdout


@needs_cgroup
def test_reserving_address_space_past_the_cap_is_not_a_failure(tmp_path: Path) -> None:
    # 8 GiB reserved, one page touched: resident memory stays tiny. Runtimes
    # that reserve up front (Go, the JVM, sanitizers) look like this, and an
    # address-space ceiling of any size below the reservation refuses them.
    reserve = "import mmap; m = mmap.mmap(-1, 8 * 1024**3); m[0] = 1; print('reserved')"
    run = evidence.run_capture(
        [sys.executable, "-c", reserve], tmp_path, memory_limit=evidence.TEST_MEMORY_LIMIT_BYTES
    )
    assert run.exit_code == 0, run.stderr
    assert "reserved" in run.stdout


def test_without_a_user_manager_the_cap_falls_back_to_an_address_space_ceiling(
    tmp_path: Path, capped: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(memcap(), "cgroup_problem", lambda: "no user manager")
    run = evidence.run_shell_capture(hog(512), tmp_path)
    assert run.exit_code != 0
    assert "MemoryError" in run.stdout + run.stderr


# -- the agent's run_command ----------------------------------------------------


def boxes(work: Path) -> list[Sandbox]:
    found = [Sandbox.for_workdir(work, prefer_bwrap=False)]
    if HAS_BWRAP:
        found.append(Sandbox.for_workdir(work))
    return found


@needs_cgroup
def test_a_command_that_allocates_past_the_cap_is_killed_and_says_why(
    tmp_path: Path, capped: None
) -> None:
    for box in boxes(tmp_path):
        terminal = box.run(hog(512), timeout=60)
        assert terminal.exit_code not in (0, None), (box.isolation, terminal.output())
        assert "allocated" not in terminal.output()
        assert "memory cap" in terminal.output(), box.isolation


@needs_cgroup
def test_a_command_under_the_cap_runs_normally(tmp_path: Path, capped: None) -> None:
    for box in boxes(tmp_path):
        terminal = box.run(hog(64), timeout=60)
        assert terminal.exit_code == 0, (box.isolation, terminal.output())
        assert "allocated" in terminal.output()
        assert "memory cap" not in terminal.output()


@needs_cgroup
def test_a_command_cannot_start_a_process_storm(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(memcap(), "TASKS_MAX", 32)
    storm = "for i in $(seq 100); do sleep 3 & done; wait; echo storm-done"
    for box in boxes(tmp_path):
        terminal = box.run(storm, timeout=60)
        assert "Resource temporarily unavailable" in terminal.output(), box.isolation


@needs_cgroup
def test_the_cap_does_not_hand_a_command_the_session_bus(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # systemd-run needs the user bus to make the scope; the command must not
    # inherit it (the allowlisted environment has neither variable).
    monkeypatch.setenv("XDG_RUNTIME_DIR", os.environ.get("XDG_RUNTIME_DIR", "/run/user/0"))
    for box in boxes(tmp_path):
        terminal = box.run('echo "[${XDG_RUNTIME_DIR:-unset}][${DBUS_SESSION_BUS_ADDRESS:-unset}]"')
        assert "[unset][unset]" in terminal.output(), box.isolation
