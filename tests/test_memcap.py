"""A hard memory and task cap on every command that runs a tree's code.

Both sides of each constraint (CONTRIBUTING.md: a constraint is verified by
instances, never by its presence):

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
from collections.abc import Iterator
from pathlib import Path
from types import ModuleType

import pytest

from saddle import evidence, gates
from saddle import sandbox as sandbox_module
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


STORM = """
import subprocess
held = []
try:
    for _ in range(100):
        held.append(subprocess.Popen(["sleep", "3"]))
except OSError as exc:
    print("storm stopped:", exc)
for p in held:
    p.kill()
"""
"""100 processes at once, far past a 32-task cap; stops at the first refusal."""


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
    assert run.exit_code != 0, run.stdout + run.stderr
    assert "memory cap" in run.stderr


@needs_cgroup
def test_a_gate_run_cannot_start_a_process_storm(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(memcap(), "TASKS_MAX", 32)
    run = evidence.run_capture([sys.executable, "-c", STORM], tmp_path, memory_limit=256 * MIB)
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
    for box in boxes(tmp_path):
        terminal = box.run(f"{PY} -c {shlex.quote(STORM)}", timeout=60)
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


# -- the probe and the wrapper, directly ----------------------------------------


@pytest.fixture
def fresh_probe() -> Iterator[None]:
    memcap().cgroup_problem.cache_clear()
    yield
    memcap().cgroup_problem.cache_clear()


def test_no_systemd_run_means_no_cgroup(monkeypatch: pytest.MonkeyPatch, fresh_probe: None) -> None:
    monkeypatch.setattr(memcap().shutil, "which", lambda name: None)
    assert memcap().cgroup_problem() == "systemd-run is not installed"
    assert memcap().cap(256 * MIB).kind == "rlimit"


def test_a_systemd_run_that_cannot_reach_a_manager_is_not_trusted(
    monkeypatch: pytest.MonkeyPatch, fresh_probe: None
) -> None:
    monkeypatch.setattr(memcap().shutil, "which", lambda name: "/bin/false")
    assert memcap().cgroup_problem() == "exit 1"


def test_a_systemd_run_that_will_not_start_is_not_trusted(
    monkeypatch: pytest.MonkeyPatch, fresh_probe: None
) -> None:
    monkeypatch.setattr(memcap().shutil, "which", lambda name: "/nonexistent/systemd-run")
    assert "nonexistent" in (memcap().cgroup_problem() or "")


def test_a_missing_program_under_the_cap_is_unavailable_not_a_failure(tmp_path: Path) -> None:
    run = evidence.run_capture(["no-such-program-here"], tmp_path, memory_limit=256 * MIB)
    assert run.exit_code == gates.TOOL_UNAVAILABLE


def test_the_bus_is_lent_only_when_the_command_env_lacks_it(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(memcap(), "cgroup_problem", lambda: None)
    monkeypatch.setenv("XDG_RUNTIME_DIR", "/run/user/1")
    monkeypatch.delenv("DBUS_SESSION_BUS_ADDRESS", raising=False)
    cap = memcap().cap(256 * MIB)
    argv, env = cap.wrap(["true"], {"PATH": "/usr/bin"})
    assert argv[-5:] == ["env", "-u", "XDG_RUNTIME_DIR", "--", "true"]
    assert env == {"PATH": "/usr/bin", "XDG_RUNTIME_DIR": "/run/user/1"}
    argv, env = cap.wrap(["true"], {"XDG_RUNTIME_DIR": "/own"})
    assert argv[-2:] == ["--", "true"]
    assert "env" not in argv
    assert env == {"XDG_RUNTIME_DIR": "/own"}
    assert cap.wrap(["true"], None) == ([*cap.prefix, "true"], None)


def _failed_units(listing: str) -> list[str]:
    """The units whose ACTIVE column reads `failed` in `list-units --plain` rows.

    The DESCRIPTION column is a scope's whole command line, so a word anywhere
    in it (another test's tmp path, say) is not a unit's state (#170)."""
    rows = (line.split(maxsplit=4) for line in listing.splitlines())
    return [row[0] for row in rows if len(row) >= 3 and row[2] == "failed"]


def _no_failed_scope_left() -> None:
    left = subprocess.run(
        ["systemctl", "--user", "list-units", "--all", "--no-legend", "--plain", "saddle-cmd-*"],
        capture_output=True,
        text=True,
        check=False,
    ).stdout
    assert _failed_units(left) == [], left


def test_a_failed_word_in_a_scope_command_line_is_not_a_failed_scope() -> None:
    # The listing that tripped #170: a running scope whose command line held
    # another test's tmp path, test_mutation_sample_failed_ru0.
    running = (
        "saddle-cmd-1.scope loaded active running /usr/bin/env -- bash -lc"
        " true --chdir /tmp/p/popen-gw3/test_mutation_sample_failed_ru0"
    )
    assert _failed_units(running) == []


def test_a_scope_whose_state_is_failed_is_found() -> None:
    listing = (
        "saddle-cmd-1.scope loaded active running bash -lc true\n"
        "saddle-cmd-2.scope loaded failed failed bash -lc false\n"
    )
    assert _failed_units(listing) == ["saddle-cmd-2.scope"]


@needs_cgroup
def test_an_oom_kill_of_a_child_is_named_and_its_scope_cleared(
    tmp_path: Path, capped: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The hog is the shell's child, not the command itself, and the shell
    # ignores the SIGTERM systemd sends the rest of the scope after the kill:
    # it outlives the child and exits 0. The run still fails, with the
    # reason, and no failed scope is left behind in the user manager.
    # Unconfined (no working bwrap) so the shell is the scope's top process;
    # the confined case is the next test.
    monkeypatch.setattr(sandbox_module, "isolation_problem", lambda: "unconfined in this test")
    shell = f"trap '' TERM; {hog(512)}; echo after-the-child"
    run = evidence.run_capture(["bash", "-c", shell], tmp_path, memory_limit=256 * MIB)
    assert run.exit_code == memcap().OOM_KILLED_EXIT, run.stderr
    assert "after-the-child" in run.stdout
    assert "allocated" not in run.stdout
    assert "memory cap" in run.stderr
    _no_failed_scope_left()


@needs_cgroup
@pytest.mark.skipif(not HAS_BWRAP, reason="bwrap is not installed")
def test_a_confined_command_whose_child_is_oom_killed_fails_with_the_reason(
    tmp_path: Path, capped: None
) -> None:
    # Confined (#104), bwrap is the scope's top process and does not ignore
    # the SIGTERM systemd sends after the kill, so a parent that would have
    # outlived its child is stopped too. What this admits is recorded here:
    # the command fails where it once exited 0, and the reason still names
    # the cap.
    shell = f"trap '' TERM; {hog(512)}; echo after-the-child"
    run = evidence.run_capture(["bash", "-c", shell], tmp_path, memory_limit=256 * MIB)
    assert run.exit_code != 0, run.stdout
    assert "allocated" not in run.stdout
    assert "memory cap" in run.stderr
    _no_failed_scope_left()


def test_a_detected_oom_kill_fails_the_run_whatever_the_command_exited(
    tmp_path: Path, capped: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The order a busy user manager produces: the kernel kills inside the
    # scope, the command finishes with 0 before systemd stops what is left,
    # and only then does saddle ask the scope why. Forced here, so the
    # outcome never rests on which side of that race the box lands.
    from saddle import memcap as live

    monkeypatch.setattr(live.Cap, "oom_killed", lambda self: True)
    journal = tmp_path / "proofs.jsonl"
    run = evidence.run_capture(
        ["true"], tmp_path, memory_limit=256 * MIB, recorder=SpanRecorder(journal, "n1")
    )
    assert run.exit_code != 0, run.stderr
    assert "memory cap" in run.stderr
    (span,) = read_spans(journal)
    assert span.exit_code == run.exit_code
    # a command that already failed keeps its own exit code
    assert evidence.run_capture(["false"], tmp_path, memory_limit=256 * MIB).exit_code == 1


# -- every gate launch that runs the tree's code reads the configured cap -------


def test_every_gate_launch_reads_the_configured_cap(
    tmp_path: Path, capped: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    from test_evidence import _mutation_workdir
    from test_sanction_red import OLD_TESTS, SANCTIONED, _tree

    from saddle import auditor, rule_d_run
    from saddle.evidence import CapturedRun, mutation_sample

    want = 256 * MIB
    limits: dict[str, list[int | None]] = {}
    real = evidence.run_capture

    def spy(site: str, *, delegate: bool = True) -> object:
        def run(argv: list[str], cwd: Path, **kw: object) -> CapturedRun:
            limits.setdefault(site, []).append(kw.get("memory_limit"))  # type: ignore[arg-type]
            if not delegate:
                return CapturedRun(tuple(argv), 0, "", "")
            return real(argv, cwd, **kw)  # type: ignore[arg-type]

        return run

    # the tests gate, the coverage run and the red-phase baseline all use this
    monkeypatch.setattr(evidence, "run_capture", spy("shell"))
    evidence.run_shell_capture(f"{PY} -c pass", tmp_path)
    # mutmut run
    monkeypatch.setattr(evidence, "run_capture", spy("mutmut", delegate=False))
    monkeypatch.setattr(shutil, "which", lambda name, *a, **k: f"/fake/{name}")
    work = _mutation_workdir(tmp_path)
    mutation_sample(work, {(str(work / "a.py"), 1)}, 10, test_files=set())
    monkeypatch.undo()
    monkeypatch.setenv("SADDLE_MEMORY_MAX", TEST_CAP)
    # the auditor's baseline collection and run of sanctioned rewrites
    monkeypatch.setattr(auditor, "run_capture", spy("auditor"))
    tests = OLD_TESTS.replace("== 9", "== 8").replace("== 19", "== 18")
    (tmp_path / "sanction").mkdir()
    auditor.Auditor(_tree(tmp_path / "sanction", tests), config=SANCTIONED).tier1()
    # rule D's per-input runs
    seen: dict[str, object] = {}

    def rule_d_spy(argv: list[str], cwd: Path, **kw: object) -> CapturedRun:
        seen.update(kw)
        return CapturedRun(tuple(argv), 1, "", "")

    rule_d_run.tree_answers(tmp_path, "n.py", "f", ["a"], runner=rule_d_spy)

    assert limits["shell"] == [want]
    assert want in limits["mutmut"]
    assert limits["auditor"].count(want) == 2  # collect-only, then the named tests
    assert seen["memory_limit"] == want


@needs_cgroup
def test_a_command_whose_child_was_killed_for_memory_says_so(tmp_path: Path, capped: None) -> None:
    box = Sandbox.for_workdir(tmp_path, prefer_bwrap=False)
    terminal = box.run(f"trap '' TERM; {hog(512)}; echo after-the-child", timeout=60)
    assert terminal.exit_code == 0, terminal.output()
    assert "after-the-child" in terminal.output()
    assert "memory cap" in terminal.output()


def test_a_sandbox_built_directly_is_capped_at_the_default(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # for_workdir is not the only way to build one; a caller that skips it
    # must still get the documented cap, never an uncapped command.
    from saddle import memcap as live

    limits: list[int] = []
    real = live.cap

    def spy(limit: int) -> live.Cap:
        limits.append(limit)
        return real(limit)

    monkeypatch.setattr(live, "cap", spy)
    terminal = Sandbox(root=tmp_path).run("echo ran", timeout=60)
    assert terminal.exit_code == 0, terminal.output()
    assert "ran" in terminal.output()
    assert limits == [live.DEFAULT_MEMORY_MAX]
