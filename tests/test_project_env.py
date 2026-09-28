"""A Task run uses the project's own virtualenv, for the gates and the model's
commands alike (#113).

A run works in a fresh worktree of tracked files only, so the project's
untracked `.venv` is not in it; `sandbox.project_env` finds it in the user's
folder instead. The venvs here are real (`python -m venv --without-pip`,
offline). Each one sees saddle's own gate tools through a `.pth` line, and
holds one package of its own, `onlyhere`, that saddle's environment lacks:
a test that imports it passes only on the project's interpreter.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import sysconfig
from pathlib import Path
from typing import Any, cast

import pytest
from test_auto import Scripted, call, finish, git

from saddle import feed, sandbox
from saddle.auditor import Finding, Findings
from saddle.auto import AutoError, AutoOptions, AutoResult, run_auto
from saddle.journal import read_spans
from saddle.packet import compile_packet
from saddle.tools import ToolContext
from saddle.vllm import VllmClient

# n.py line 2 is where the suite's mutmut stub locates its killed mutants.
BUGGY = "def f():\n    return 3\n"
FIXED = "def f():\n    return 2\n"
NEEDS_ONLYHERE = (
    "from onlyhere import TWO\n\nfrom n import f\n\n\ndef test_f():\n    assert f() == TWO\n"
)
CONSOLE = {
    "pytest": "from pytest import console_main as main",
    "coverage": "from coverage.cmdline import main",
}


def make_venv(where: Path, *, tools: tuple[str, ...] = ("pytest", "coverage", "mutmut")) -> Path:
    """A real venv at `where` holding `onlyhere` and the gate scripts named in
    `tools`. `mutmut` is a copy of the suite's PATH stub (conftest
    `_stub_mutmut`), so the mutation gate runs as everywhere else in the
    suite; a link would dangle inside the sandbox, where the stub's
    directory is hidden."""
    subprocess.run(
        [sys.executable, "-m", "venv", "--without-pip", str(where)],
        check=True,
        capture_output=True,
    )
    purelib = Path(sysconfig.get_path("purelib", vars={"base": str(where), "platbase": str(where)}))
    (purelib / "onlyhere").mkdir(parents=True)
    (purelib / "onlyhere" / "__init__.py").write_text("TWO = 2\n")
    (purelib / "saddle-tools.pth").write_text(sysconfig.get_path("purelib") + "\n")
    for name in tools:
        script = where / "bin" / name
        if name == "mutmut":
            stub = shutil.which("mutmut")
            assert stub is not None
            shutil.copy(stub, script)
            continue
        script.write_text(
            f"#!{where / 'bin' / 'python'}\nimport sys\n{CONSOLE[name]}\nsys.exit(main())\n"
        )
        script.chmod(0o755)
    return where.resolve()


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    """A repo whose test imports `onlyhere`, and whose `.venv` (untracked) has it."""
    root = tmp_path / "repo"
    (root / "tests").mkdir(parents=True)
    (root / "n.py").write_text(BUGGY)
    (root / "tests" / "test_n.py").write_text(NEEDS_ONLYHERE)
    (root / ".gitignore").write_text(".venv/\n")
    git(root, "init", "-q", "-b", "main")
    git(root, "add", "-A")
    git(root, "commit", "-q", "-m", "init")
    make_venv(root / ".venv")
    return root


def _start(result: AutoResult) -> Any:
    return next(s for s in read_spans(result.journal) if s.name == "auto:start")


def _run(repo: Path, client: Scripted, arm: str = "E+A+F", **kwargs: Any) -> AutoResult:
    options = AutoOptions(task="make add add", repo=repo, run_id="r1", arm=arm, **kwargs)  # type: ignore[arg-type]
    return run_auto(options, cast(VllmClient, client))


# -- finding the project's environment ----------------------------------------


def _cfg(root: Path) -> Path:
    root.mkdir(parents=True)
    (root / "pyvenv.cfg").write_text("home = /usr/bin\n")
    return root


def test_project_env_is_the_folders_dot_venv_then_venv(tmp_path: Path) -> None:
    folder = tmp_path / "p"
    _cfg(folder / "venv")
    assert sandbox.project_env(folder, {}) == (folder / "venv").resolve()
    _cfg(folder / ".venv")
    assert sandbox.project_env(folder, {}) == (folder / ".venv").resolve()


def test_a_directory_without_pyvenv_cfg_is_not_a_venv(tmp_path: Path) -> None:
    (tmp_path / ".venv" / "bin").mkdir(parents=True)
    assert sandbox.project_env(tmp_path, {}) is None


def test_an_active_virtual_env_counts_unless_it_is_saddles_own(tmp_path: Path) -> None:
    active = _cfg(tmp_path / "elsewhere")
    folder = tmp_path / "p"
    folder.mkdir()
    assert sandbox.project_env(folder, {"VIRTUAL_ENV": str(active)}) == active.resolve()
    assert sandbox.project_env(folder, {"VIRTUAL_ENV": sys.prefix}) is None
    assert sandbox.project_env(folder, {"VIRTUAL_ENV": str(tmp_path / "gone")}) is None
    # The folder's own venv wins over an activated one.
    _cfg(folder / ".venv")
    assert sandbox.project_env(folder, {"VIRTUAL_ENV": str(active)}) == (folder / ".venv").resolve()


def test_project_env_reads_saddles_environment_by_default(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    active = _cfg(tmp_path / "elsewhere")
    monkeypatch.setenv("VIRTUAL_ENV", str(active))
    assert sandbox.project_env(tmp_path) == active.resolve()


def test_a_venv_missing_a_gate_tool_is_a_setup_problem_naming_it(tmp_path: Path) -> None:
    venv = make_venv(tmp_path / "v", tools=("pytest",))
    problem = sandbox.project_env_problem(venv)
    assert problem is not None
    assert "has no coverage, mutmut; the auditor runs the tests on it" in problem
    assert f"{venv}/bin/python -m pip install coverage mutmut" in problem
    assert sandbox.project_env_problem(make_venv(tmp_path / "full")) is None
    (tmp_path / "empty").mkdir()
    assert "has no python in" in str(sandbox.project_env_problem(tmp_path / "empty"))


def test_gate_path_puts_the_project_bin_first_only_inside_the_block(tmp_path: Path) -> None:
    venv = tmp_path / "v"
    bin_dir = str(venv / "bin")
    beside = str(sandbox.tool_dir())
    with sandbox.using_project_env(venv):
        assert sandbox.gate_path(f"/a{os.pathsep}{bin_dir}").split(os.pathsep) == [
            bin_dir,
            "/a",
            beside,
        ]
        assert sandbox.gate_path("").split(os.pathsep) == [bin_dir, beside]
    assert sandbox.gate_path("/a").split(os.pathsep) == ["/a", beside]
    assert sandbox.project_command_env(None) == {}


# -- the gates: known-good and known-bad on the project's interpreter ----------


def test_a_task_run_accepts_a_correct_change_whose_tests_need_the_project_venv(
    repo: Path,
) -> None:
    """Known-good (#113). Red before: the gates ran `python` from PATH or
    saddle's own, neither has `onlyhere`, and finish was refused."""
    fix = call("write_file", "w1", path="n.py", content=FIXED)
    result = _run(repo, Scripted([[fix], finish()]), finish_refusal_cap=1)
    assert (result.outcome, result.reason) == ("finished", "finish called"), result.reason
    venv = (repo / ".venv").resolve()
    assert f"environment the project's virtualenv {venv}" in _start(result).detail
    # compile_packet runs check_packet: every cite, the start span's too, is in the ledger.
    packet = compile_packet(result.journal, run_id="r1")
    tests = next(r for r in packet.rows if r.key == "tests")
    assert tests.status == "proven"
    assert tests.text.endswith(f". Tests ran on the project's virtualenv {venv}.")
    assert _start(result).record_hash in tests.cites


def test_a_task_run_on_the_project_venv_still_refuses_a_wrong_change(repo: Path) -> None:
    """Known-bad: the right environment does not make a wrong change pass."""
    wrong = call("write_file", "w1", path="n.py", content="def f():\n    return 4\n")
    result = _run(repo, Scripted([[wrong], finish()]), finish_refusal_cap=1)
    assert result.outcome == "stopped"
    tests = next(r for r in compile_packet(result.journal, run_id="r1").rows if r.key == "tests")
    assert tests.status == "failed"


def test_a_venv_the_gates_cannot_run_on_stops_an_audited_run_before_it_starts(
    repo: Path,
) -> None:
    """A setup error, not a refusal: nothing is created."""
    (repo / ".venv" / "bin" / "mutmut").unlink()
    with pytest.raises(AutoError, match=r"has no mutmut; the auditor runs the tests on it"):
        _run(repo, Scripted([finish()]))
    assert not (repo / ".saddle").exists()
    # Arm E runs no gate, so it does not need them.
    assert _run(repo, Scripted([finish()]), arm="E").outcome == "finished"


def test_without_a_project_venv_the_start_record_names_the_gates_python(tmp_path: Path) -> None:
    root = tmp_path / "plain"
    root.mkdir()
    (root / "n.py").write_text(BUGGY)
    git(root, "init", "-q", "-b", "main")
    git(root, "add", "-A")
    git(root, "commit", "-q", "-m", "init")
    result = _run(root, Scripted([finish()]), arm="E")
    found = shutil.which("python", path=sandbox.gate_path(os.environ["PATH"]))
    said = (
        f"saddle's own interpreter {found}"
        if found is not None and Path(found).parent == sandbox.tool_dir()
        else f"{found} from PATH"
    )
    assert f"environment {said}" in _start(result).detail


# -- the model's commands ------------------------------------------------------


PROBE = "python -c 'import onlyhere, sys; print(sys.prefix)'"


def test_a_task_runs_commands_use_the_project_venv(repo: Path) -> None:
    """The model's `python` is the project's, read-only; red before: not found."""
    probe = call("run_command", "r1", command=PROBE)
    touch = call("run_command", "r2", command="touch .venv-probe $VIRTUAL_ENV/written")
    result = _run(repo, Scripted([[probe], [touch], finish()]), arm="E")
    spans = [s for s in read_spans(result.journal) if s.name == "run_command"]
    venv = (repo / ".venv").resolve()
    assert "exit 0" in spans[0].detail, spans[0].detail
    assert str(venv) in spans[0].detail
    assert "Read-only file system" in spans[1].detail, spans[1].detail
    assert not (venv / "written").exists()


def test_the_chat_lanes_sandbox_puts_the_folders_venv_first(repo: Path) -> None:
    box = ToolContext(workdir=repo).box()
    done = box.run(PROBE, timeout=60)
    assert (done.exit_code, done.output().strip()) == (0, str((repo / ".venv").resolve()))


# -- a checkpoint audit, on the feed's own thread, sees it too -----------------


class _PathAuditor:
    """Records the gate PATH each tier sees."""

    def __init__(self) -> None:
        self.paths: list[str] = []

    def _found(self, tier: int) -> Findings:
        self.paths.append(sandbox.gate_path(""))
        ok = Finding("tests", tier, "pass", "code-wrong", "1 passed", ("fake",))
        return Findings(tier=tier, key=f"k{tier}", findings=(ok,))

    def tier0(self, path: str, new_text: str) -> Findings:
        return self._found(0)

    def tier1(self, tree: Path | None = None) -> Findings:
        return self._found(1)

    def tier2(self, tree: Path | None = None) -> Findings:
        return self._found(2)


def test_every_audit_of_the_run_sees_the_project_venv_including_checkpoints(
    repo: Path,
) -> None:
    spy = _PathAuditor()
    edit = call("write_file", "w1", path="n.py", content=FIXED)
    look = call("run_command", "r1", command="true")
    options = AutoOptions(
        task="t", repo=repo, run_id="r1", arm="E+A+F", auditor_factory=lambda *a: spy
    )
    result = run_auto(options, cast(VllmClient, Scripted([[edit], [look], finish()])))
    assert result.outcome == "finished"
    names = [s.argv[1] for s in read_spans(result.journal) if s.name.startswith("audit:")]
    assert any(n.startswith("checkpoint") for n in names), names
    bin_dir = str((repo / ".venv" / "bin").resolve())
    assert len(spy.paths) >= 3
    assert all(p.split(os.pathsep)[0] == bin_dir for p in spy.paths), spy.paths


def test_the_feed_leaves_the_gates_alone_without_a_project_venv(repo: Path) -> None:
    spy = _PathAuditor()
    audit = feed.AuditFeed(
        worktree=repo, baseline="HEAD", journal=repo / "j.jsonl", run_span="s", auditor=spy
    )
    (repo / "n.py").write_text(FIXED)
    audit.final()
    bin_dir = str((repo / ".venv" / "bin").resolve())
    assert spy.paths
    assert all(p.split(os.pathsep)[0] != bin_dir for p in spy.paths)


def test_a_semicolon_in_the_venv_path_survives_the_sealed_record(tmp_path: Path) -> None:
    """`;` separates the start record's fields; the path is escaped there
    and read back whole in the packet."""
    odd = tmp_path / "a;b"
    odd.mkdir()
    root = odd / "repo"
    (root / "tests").mkdir(parents=True)
    (root / "n.py").write_text(BUGGY)
    (root / "tests" / "test_n.py").write_text(NEEDS_ONLYHERE)
    git(root, "init", "-q", "-b", "main")
    git(root, "add", "-A")
    git(root, "commit", "-q", "-m", "init")
    make_venv(root / ".venv")
    edit = call("write_file", "w1", path="n.py", content=FIXED)
    result = _run(root, Scripted([[edit], finish()]), finish_refusal_cap=1)
    assert result.outcome == "finished", result.reason
    venv = (root / ".venv").resolve()
    assert f"environment the project's virtualenv {str(venv).replace(';', '%3B')}" in (
        _start(result).detail
    )
    tests = next(r for r in compile_packet(result.journal, run_id="r1").rows if r.key == "tests")
    assert tests.text.endswith(f". Tests ran on the project's virtualenv {venv}.")
