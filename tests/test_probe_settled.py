"""A probe that did not finish is never remembered as its answer (#182).

`sandbox.bwrap_works`, `memcap.cgroup_problem` and `sandbox._runs_gates` ask
the box once and keep the answer for as long as saddle runs. Each used to keep
a timeout (or an `OSError`) as if it were the answer, so one slow start under
load left every later sandbox in the process without isolation, every later
command on rlimit caps, or the gates without their interpreter, until saddle
restarted, on a box where all three work.

Known-good: a probe that finished is kept, whatever it said, and is not asked
again. Known-bad: a probe that timed out or could not start answers that call
only; once the box answers, the next call returns the real answer.
"""

from __future__ import annotations

import shutil
import subprocess
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from saddle import memcap, sandbox
from saddle.sandbox import Sandbox

FAKE = "/fake-probe"
"""Where the fake programs live: only argv under it is answered by `Box`."""

MIB = 1024**2


class Box:
    """`subprocess.run` for the probes. A call whose program lives under `FAKE`
    takes the next answer: an exception to raise, or an exit status and its
    stderr. Every other call runs for real."""

    def __init__(self, *answers: BaseException | tuple[int, str]) -> None:
        self.answers = list(answers)
        self.calls = 0
        self.real = subprocess.run

    def run(self, argv: Any, *args: Any, **kwargs: Any) -> Any:
        if not str(argv[0]).startswith(FAKE):
            return self.real(argv, *args, **kwargs)
        self.calls += 1
        answer = self.answers.pop(0)
        if isinstance(answer, BaseException):
            raise answer
        code, err = answer
        return subprocess.CompletedProcess(argv, code, "", err)


@pytest.fixture(autouse=True)
def nothing_settled() -> Iterator[None]:
    """Each test starts with no answer kept, and leaves none of its own."""
    memos: tuple[dict[str, Any], ...] = (
        sandbox.BWRAP_SETTLED,
        sandbox.GATES_SETTLED,
        memcap.CGROUP_SETTLED,
    )
    for memo in memos:
        memo.clear()
    yield
    for memo in memos:
        memo.clear()


def answering(monkeypatch: pytest.MonkeyPatch, *answers: BaseException | tuple[int, str]) -> Box:
    box = Box(*answers)
    monkeypatch.setattr(subprocess, "run", box.run)
    return box


def found_as(monkeypatch: pytest.MonkeyPatch, name: str) -> str:
    """`shutil.which` finds `name` under `FAKE`; every other name as before."""
    real = shutil.which
    fake = f"{FAKE}/{name}"

    def which(program: str, *args: Any, **kwargs: Any) -> str | None:
        return fake if program == name else real(program, *args, **kwargs)

    monkeypatch.setattr(shutil, "which", which)
    return fake


def slow(program: str) -> subprocess.TimeoutExpired:
    return subprocess.TimeoutExpired([program], 10)


# -- bwrap ------------------------------------------------------------------------


def test_a_bwrap_probe_that_timed_out_is_asked_again(monkeypatch: pytest.MonkeyPatch) -> None:
    bwrap = f"{FAKE}/bwrap"
    box = answering(monkeypatch, slow(bwrap), (0, ""))
    assert "timed out after 10 seconds" in (sandbox.bwrap_works(bwrap) or "")
    assert sandbox.bwrap_works(bwrap) is None
    assert sandbox.bwrap_works(bwrap) is None
    assert box.calls == 2  # the answer it finished with is kept


def test_a_bwrap_that_could_not_be_started_is_asked_again(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    bwrap = f"{FAKE}/bwrap"
    answering(monkeypatch, OSError(11, "Resource temporarily unavailable"), (0, ""))
    assert "temporarily unavailable" in (sandbox.bwrap_works(bwrap) or "")
    assert sandbox.bwrap_works(bwrap) is None


def test_a_bwrap_probe_that_finished_failing_is_kept(monkeypatch: pytest.MonkeyPatch) -> None:
    bwrap = f"{FAKE}/bwrap"
    box = answering(monkeypatch, (1, "bwrap: setting up uid map: Permission denied"))
    assert sandbox.bwrap_works(bwrap) == "bwrap: setting up uid map: Permission denied"
    assert sandbox.bwrap_works(bwrap) == "bwrap: setting up uid map: Permission denied"
    assert box.calls == 1


def test_after_a_slow_start_the_next_sandbox_is_isolated_again(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The consequence that mattered: on main the second sandbox here was
    `isolation="none"` too, and so was every one after it."""
    bwrap = found_as(monkeypatch, "bwrap")
    answering(monkeypatch, slow(bwrap), (0, ""))
    assert Sandbox.for_workdir(tmp_path).isolation == "none"
    assert Sandbox.for_workdir(tmp_path).isolation == "bwrap"
    assert sandbox.isolation_problem() is None


# -- the user systemd manager -------------------------------------------------------


def test_a_cgroup_probe_that_timed_out_is_asked_again(monkeypatch: pytest.MonkeyPatch) -> None:
    exe = found_as(monkeypatch, "systemd-run")
    box = answering(monkeypatch, slow(exe), (0, ""))
    assert memcap.cap(256 * MIB).kind == "rlimit"
    assert memcap.cap(256 * MIB).kind == "cgroup"
    assert memcap.cgroup_problem() is None
    assert box.calls == 2


def test_a_cgroup_probe_that_finished_failing_is_kept(monkeypatch: pytest.MonkeyPatch) -> None:
    found_as(monkeypatch, "systemd-run")
    box = answering(monkeypatch, (1, "Failed to connect to bus: No such file or directory"))
    assert memcap.cgroup_problem() == "Failed to connect to bus: No such file or directory"
    assert memcap.cgroup_problem() == "Failed to connect to bus: No such file or directory"
    assert box.calls == 1


# -- the gates' interpreter -----------------------------------------------------------


def test_a_gate_interpreter_probe_that_timed_out_is_asked_again(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(sandbox, "isolation_problem", lambda: "unconfined in this test")
    python3 = f"{FAKE}/python3"
    box = answering(monkeypatch, slow(python3), (0, ""))
    assert sandbox._runs_gates(python3) is False
    assert sandbox._runs_gates(python3) is True
    assert sandbox._runs_gates(python3) is True
    assert box.calls == 2


def test_a_gate_interpreter_that_cannot_import_the_gates_is_kept(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(sandbox, "isolation_problem", lambda: "unconfined in this test")
    python3 = f"{FAKE}/python3"
    box = answering(monkeypatch, (1, "ModuleNotFoundError: No module named 'mutmut'"))
    assert sandbox._runs_gates(python3) is False
    assert sandbox._runs_gates(python3) is False
    assert box.calls == 1
