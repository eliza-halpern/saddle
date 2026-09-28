"""A gate uses the tools on the user's PATH, falls back to the ones installed
beside saddle only when PATH has none, and reports a tool found in neither
as a setup error, never a verdict.

"Beside saddle" is `sandbox.tool_dir()`, the directory of the running
interpreter: in this suite, its own venv's `bin`. A decoy is a script that
prints a marker line and exits 3, put first on PATH to see which copy a gate
starts. tests/test_packaging.py runs the same contract on an installed wheel.
"""

from __future__ import annotations

import importlib.metadata
import os
import shutil
import sys
from pathlib import Path

import pytest

from saddle import cli, evidence, sandbox
from saddle.audit import GateSetupError, check_gate_commands
from saddle.evidence import ruff_version, run_argv, run_capture
from saddle.gates import TOOL_UNAVAILABLE

DECOY_EXIT = 3
BESIDE = Path(sys.executable).parent
SYSTEM_PATH = f"/usr/bin{os.pathsep}/bin"


def _decoys(root: Path, *names: str, exit_code: int = DECOY_EXIT) -> Path:
    """A directory of scripts named `names` that each print a decoy line and
    exit `exit_code`."""
    root.mkdir(parents=True, exist_ok=True)
    for name in names:
        script = root / name
        script.write_text(f"#!/bin/sh\necho '{name} 0.0.0-decoy'\nexit {exit_code}\n")
        script.chmod(0o755)
    return root


@pytest.fixture
def decoy_path(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """PATH = decoys of every gate tool, then the system dirs; nothing of this venv."""
    decoys = _decoys(tmp_path / "decoys", "coverage", "mutmut", "pytest", "python")
    # A version probe reads only a zero exit, so the ruff decoy answers like the real one.
    _decoys(decoys, "ruff", exit_code=0)
    monkeypatch.setenv("PATH", f"{decoys}{os.pathsep}{SYSTEM_PATH}")
    return decoys


@pytest.fixture
def system_path(monkeypatch: pytest.MonkeyPatch) -> None:
    """PATH = the system dirs only: the `uv tool install` shape, where no
    gate tool is on PATH and saddle's own are the only copies."""
    for name in ("coverage", "ruff", "mutmut"):
        assert shutil.which(name, path=SYSTEM_PATH) is None, f"this host has {name} in /usr/bin"
    monkeypatch.setenv("PATH", SYSTEM_PATH)


# -- the lookup order ---------------------------------------------------------


def test_tool_dir_is_the_directory_of_the_running_interpreter() -> None:
    assert sandbox.tool_dir() == BESIDE


def test_tool_dir_is_none_when_python_cannot_name_its_interpreter(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(sys, "executable", "")
    assert sandbox.tool_dir() is None
    assert sandbox.gate_path(SYSTEM_PATH) == SYSTEM_PATH


def test_gate_path_appends_the_tool_dir_after_every_path_entry() -> None:
    assert sandbox.gate_path(f"/a{os.pathsep}/b") == os.pathsep.join(["/a", "/b", str(BESIDE)])
    assert sandbox.gate_path("") == str(BESIDE)


def test_gate_path_leaves_a_path_that_already_has_the_tool_dir_alone() -> None:
    """The user placed it; its position is theirs."""
    placed = os.pathsep.join([str(BESIDE), "/a"])
    assert sandbox.gate_path(placed) == placed


# -- PATH first: the user's tools win (known-bad for "prepend") ----------------


def test_a_confined_gate_run_uses_the_tool_on_path(tmp_path: Path, decoy_path: Path) -> None:
    """A different `coverage` first on PATH is the one the gate starts, not
    saddle's: the project's environment decides what its tests run on."""
    work = tmp_path / "work"
    work.mkdir()
    run = run_capture(["coverage", "--version"], work, memory_limit=evidence.tree_memory_limit())
    assert (run.exit_code, run.stdout.strip()) == (DECOY_EXIT, "coverage 0.0.0-decoy")


def test_a_confined_python_test_command_uses_the_python_on_path(
    tmp_path: Path, decoy_path: Path
) -> None:
    """The default test command's `python`, the same way."""
    work = tmp_path / "work"
    work.mkdir()
    run = evidence.run_shell_capture("python -m pytest -q", work)
    assert (run.exit_code, run.stdout.strip()) == (DECOY_EXIT, "python 0.0.0-decoy")


def test_unconfined_gate_runs_use_the_tool_on_path(tmp_path: Path, decoy_path: Path) -> None:
    assert run_capture(["coverage", "--version"], tmp_path).exit_code == DECOY_EXIT
    assert run_argv(["coverage", "--version"], tmp_path) == DECOY_EXIT
    assert ruff_version() == "0.0.0-decoy"


# -- the fallback: a tool PATH lacks is found beside saddle ---------------------


def test_a_confined_gate_run_finds_a_tool_that_is_only_beside_saddle(
    tmp_path: Path, system_path: None
) -> None:
    """Found by the launch check and inside the sandbox alike."""
    work = tmp_path / "work"
    work.mkdir()
    run = run_capture(["coverage", "--version"], work, memory_limit=evidence.tree_memory_limit())
    assert (run.exit_code, "Coverage.py" in run.stdout) == (0, True), run


def test_unconfined_gate_runs_find_a_tool_that_is_only_beside_saddle(
    tmp_path: Path, system_path: None
) -> None:
    assert run_capture(["coverage", "--version"], tmp_path).exit_code == 0
    assert run_argv(["coverage", "--version"], tmp_path) == 0
    assert ruff_version() == importlib.metadata.version("ruff")


class _StoppedError(Exception):
    """Raised instead of starting mutmut: the lookup is what is under test."""


def _refuse_to_run(*_args: object, **_kwargs: object) -> None:
    raise _StoppedError


def test_the_mutation_gate_finds_mutmut_that_is_only_beside_saddle(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, system_path: None
) -> None:
    """Found, the gate goes on to start it (intercepted here); a failed
    lookup returns "mutmut not on PATH" without starting anything."""
    work = tmp_path / "w"
    work.mkdir()
    (work / "n.py").write_text("def f():\n    return 2\n")
    monkeypatch.setattr(evidence, "run_capture", _refuse_to_run)
    with pytest.raises(_StoppedError):
        evidence.mutation_sample(work, {(str(work / "n.py"), 2)}, 10, test_files=())


# -- the model's commands never see saddle's directory -------------------------


def test_the_model_command_env_does_not_gain_saddles_bin(system_path: None) -> None:
    """`command_env` (every `Sandbox` command the model runs) keeps PATH as
    saddle was given it; only the gates add the fallback."""
    entries = sandbox.command_env({})["PATH"].split(os.pathsep)
    assert str(BESIDE) not in entries
    assert str(BESIDE) in sandbox.confine(["true"], Path.cwd())[1]["PATH"].split(os.pathsep)


def test_a_sandbox_command_does_not_find_a_tool_that_is_only_beside_saddle(
    tmp_path: Path, system_path: None
) -> None:
    box = sandbox.Sandbox.for_workdir(tmp_path)
    done = box.run("command -v coverage || echo absent", timeout=30)
    assert done.output().strip() == "absent"


# -- found in neither: a setup error ------------------------------------------


def test_a_tool_found_nowhere_does_not_launch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """With saddle's directory lacking it too, the confined run reports the
    tool unavailable (127), never an exit a test suite could produce."""
    monkeypatch.setattr(sandbox, "tool_dir", lambda: tmp_path / "empty")
    monkeypatch.setenv("PATH", str(tmp_path / "empty"))
    run = run_capture(["coverage", "--version"], tmp_path, memory_limit=2**30)
    assert run.exit_code == TOOL_UNAVAILABLE


def test_every_gate_command_is_found_through_the_fallback(system_path: None) -> None:
    """Known-good: PATH without this venv, and the preflight still passes."""
    check_gate_commands()


def test_a_gate_command_found_nowhere_is_a_setup_error_naming_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Known-bad: no tool beside saddle (an empty tool dir) and some on PATH."""
    empty = tmp_path / "empty"
    empty.mkdir()
    monkeypatch.setattr(sandbox, "tool_dir", lambda: empty)
    monkeypatch.setenv("PATH", str(_decoys(tmp_path / "some", "python", "pytest")))
    with pytest.raises(GateSetupError, match=r"on PATH: coverage, ruff, mutmut; reinstall"):
        check_gate_commands()


def _git_only(root: Path) -> Path:
    """A PATH directory holding git and nothing else."""
    root.mkdir()
    real = shutil.which("git")
    assert real is not None
    (root / "git").symlink_to(real)
    return root


def test_saddle_audit_with_a_tool_found_nowhere_is_an_error_not_a_refusal(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Without the preflight, the tests and coverage gates fail on the
    missing tool and the audit prints `verdict: refuse` for a correct change."""
    tree = tmp_path / "tree"
    tree.mkdir()
    for argv in (["init", "-q"], ["commit", "-q", "--allow-empty", "-m", "base"]):
        assert run_argv(["git", "-c", "user.name=t", "-c", "user.email=t@t", *argv], tree) == 0
    (tree / "n.py").write_text("def f():\n    return 2\n")
    (tree / "test_n.py").write_text("from n import f\n\n\ndef test_f():\n    assert f() == 2\n")
    empty = tmp_path / "empty"
    empty.mkdir()
    monkeypatch.setattr(sandbox, "tool_dir", lambda: empty)
    monkeypatch.setenv("PATH", str(_git_only(tmp_path / "gitonly")))
    for tiered in ([], ["--tiered"]):
        code = cli.main(["audit", *tiered, "--repo", str(tree), "--no-cache"])
        out, err = capsys.readouterr()
        assert code == cli.AUDIT_COULD_NOT_AUDIT, (tiered, out, err)
        assert "setup: gate tools not found" in err
        assert "verdict" not in out
