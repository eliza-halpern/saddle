"""With no `python` on PATH, the gates run the `python3` there, when it can
run them, before falling back to saddle's own interpreter (#113).

Ubuntu ships `python3` and no `python` unless python-is-python3 is
installed. The test command is `python -m pytest`, so before this the gates
ran saddle's own interpreter, which has none of the project's packages. The
`python3` here is a real venv (`python -m venv --without-pip`) with its
`python` removed; `onlyhere` is a package only it has.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest
from test_project_env import make_venv

from saddle import cli, evidence, sandbox
from saddle.evidence import run_argv

SYSTEM = f"/usr/bin{os.pathsep}/bin"
SADDLES_PYTHON = str(Path(sys.executable).parent / "python")


@pytest.fixture(autouse=True)
def _no_system_python() -> None:
    """A host whose system dirs have a `python` cannot show this shape."""
    if shutil.which("python", path=SYSTEM) is not None:
        pytest.skip("this host has `python` in /usr/bin or /bin")


def python3_only(where: Path, **kwargs: object) -> Path:
    """`make_venv(where)` with `bin/python` removed; returns its bin."""
    venv = make_venv(where, **kwargs)  # type: ignore[arg-type]
    (venv / "bin" / "python").unlink()
    return venv / "bin"


def _python(path: str) -> str | None:
    return shutil.which("python", path=sandbox.gate_path(path))


def test_a_python3_that_runs_the_gates_is_used_before_saddles_own(tmp_path: Path) -> None:
    bin_dir = python3_only(tmp_path / "v")
    path = f"{bin_dir}{os.pathsep}{SYSTEM}"
    entries = sandbox.gate_path(path).split(os.pathsep)
    shim = Path(entries[-2])
    assert entries[:-2] == [str(bin_dir), *SYSTEM.split(os.pathsep)]
    assert entries[-1] == str(sandbox.tool_dir())
    assert _python(path) == str(shim / "python")
    # Nothing on the user's system was written: the shim is saddle's own dir.
    assert not (bin_dir / "python").exists()


def test_the_shimmed_python_is_the_venvs_python3_inside_the_sandbox(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    bin_dir = python3_only(tmp_path / "v")
    monkeypatch.setenv("PATH", f"{bin_dir}{os.pathsep}{SYSTEM}")
    work = tmp_path / "work"
    work.mkdir()
    run = evidence.run_capture(
        ["python", "-c", "import onlyhere, sys; print(sys.prefix)"],
        work,
        memory_limit=evidence.tree_memory_limit(),
    )
    assert (run.exit_code, run.stdout.strip()) == (0, str(bin_dir.parent)), run.stderr


def test_a_python3_without_the_gate_tools_leaves_saddles_own_in_place(tmp_path: Path) -> None:
    """Known-bad for "always python3": one without pytest would fail every
    test run. The probe runs confined, so saddle's tools are not borrowed."""
    bare = tmp_path / "bare"
    subprocess.run([sys.executable, "-m", "venv", "--without-pip", str(bare)], check=True)
    (bare / "bin" / "python").unlink()
    stub = shutil.which("mutmut")
    assert stub is not None
    shutil.copy(stub, bare / "bin" / "mutmut")
    path = f"{bare / 'bin'}{os.pathsep}{SYSTEM}"
    assert _python(path) == SADDLES_PYTHON


def test_a_python3_without_mutmut_on_path_leaves_saddles_own_in_place(tmp_path: Path) -> None:
    """mutmut runs the tests in its own process: without one beside this
    python3, the mutation gate would run saddle's interpreter anyway."""
    bin_dir = python3_only(tmp_path / "v", tools=("pytest", "coverage"))
    assert _python(f"{bin_dir}{os.pathsep}{SYSTEM}") == SADDLES_PYTHON


def test_a_python_on_path_is_used_as_before(tmp_path: Path) -> None:
    venv = make_venv(tmp_path / "v")
    path = f"{venv / 'bin'}{os.pathsep}{SYSTEM}"
    assert _python(path) == str(venv / "bin" / "python")


def test_a_probe_that_hangs_counts_as_cannot(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    slow = tmp_path / "slow"
    slow.mkdir()
    for name, body in (("python3", "sleep 5"), ("mutmut", "exit 0")):
        (slow / name).write_text(f"#!/bin/sh\n{body}\n")
        (slow / name).chmod(0o755)
    monkeypatch.setattr(sandbox, "PROBE_TIMEOUT_S", 0.5)
    assert _python(f"{slow}{os.pathsep}{SYSTEM}") == SADDLES_PYTHON


def test_the_start_record_label_names_the_python3(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    bin_dir = python3_only(tmp_path / "v")
    monkeypatch.setenv("PATH", f"{bin_dir}{os.pathsep}{SYSTEM}")
    assert sandbox.gate_environment() == (
        f"{bin_dir / 'python3'} from PATH (there is no `python` on PATH)"
    )


def _tree(root: Path) -> Path:
    root.mkdir()
    (root / "n.py").write_text("def f():\n    return 3\n")
    (root / "test_n.py").write_text(
        "from onlyhere import TWO\n\nfrom n import f\n\n\ndef test_f():\n    assert f() == TWO\n"
    )
    ident = ["git", "-c", "user.name=t", "-c", "user.email=t@t"]
    assert run_argv([*ident, "init", "-q"], root) == 0
    assert run_argv([*ident, "add", "-A"], root) == 0
    assert run_argv([*ident, "commit", "-q", "-m", "base"], root) == 0
    (root / "n.py").write_text("def f():\n    return 2\n")
    return root


def test_saddle_audit_accepts_a_correct_change_on_the_python3_on_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Known-good, end to end. Red before: the tests ran on saddle's own
    interpreter, `import onlyhere` failed, and the audit refused."""
    bin_dir = python3_only(tmp_path / "v")
    git = shutil.which("git")
    assert git is not None
    monkeypatch.setenv("PATH", os.pathsep.join([str(bin_dir), str(Path(git).parent), SYSTEM]))
    tree = _tree(tmp_path / "tree")
    code = cli.main(["audit", "--repo", str(tree), "--no-cache"])
    out = capsys.readouterr().out
    assert "verdict: accept" in out, out
    assert code == 0
