"""What a task run's model is told about, and given as, its environment.

Dogfood run on saddle's own repo (src layout): the model spent seven rounds
finding a Python that could import the project, because its commands lacked
the `src/`-first `PYTHONPATH` the gates already use (`evidence.src_layout_env`)
and its prompt never said where it was or how the audit runs the tests.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from test_auto import Scripted, auto, call, finish, git
from test_project_env import make_venv

from saddle.audit import AUDIT_TEST_COMMAND
from saddle.journal import read_spans

PKG = "VALUE = 7\n"
FLAT = "def f():\n    return 2\n"
PROBE = 'python3 -c "import pkg; print(pkg.__file__)"'
PYPATH = "python3 -c \"import os; print('PP=' + os.environ.get('PYTHONPATH', ''))\""


def _repo(root: Path, *, src: bool) -> Path:
    (root / "tests").mkdir(parents=True)
    if src:
        (root / "src" / "pkg").mkdir(parents=True)
        (root / "src" / "pkg" / "__init__.py").write_text(PKG)
    else:
        (root / "n.py").write_text(FLAT)
    (root / "tests" / "test_x.py").write_text("def test_x():\n    assert True\n")
    (root / ".gitignore").write_text(".venv/\n")
    git(root, "init", "-q", "-b", "main")
    git(root, "add", "-A")
    git(root, "commit", "-q", "-m", "init")
    return root


def _command_details(result: Any) -> list[str]:
    return [s.detail for s in read_spans(result.journal) if s.name == "run_command"]


def _system(client: Scripted) -> str:
    return str(client.asked[0]["messages"][0]["content"])


def test_the_models_commands_import_the_src_layout_package_from_the_worktree(
    tmp_path: Path,
) -> None:
    """Red before: `import pkg` failed (ModuleNotFoundError) in the model's
    commands, though the gates import it from the same worktree."""
    repo = _repo(tmp_path / "repo", src=True)
    result = auto(repo, Scripted([[call("run_command", "r1", command=PROBE)], finish()]))
    detail = _command_details(result)[0]
    assert "exit 0" in detail, detail
    assert "/.saddle/worktrees/" in detail and "/src/pkg/__init__.py" in detail, detail


def test_a_flat_project_gets_no_src_entry_on_its_import_path(tmp_path: Path) -> None:
    """Known-good half: without `src/` nothing is added."""
    repo = _repo(tmp_path / "repo", src=False)
    result = auto(repo, Scripted([[call("run_command", "r1", command=PYPATH)], finish()]))
    detail = _command_details(result)[0]
    assert "exit 0" in detail, detail
    assert "/src" not in detail.split("PP=", 1)[1], detail


def test_the_prompt_states_the_worktree_the_python_and_the_audits_test_command(
    tmp_path: Path,
) -> None:
    """Red before: the system prompt said none of these."""
    repo = _repo(tmp_path / "repo", src=True)
    make_venv(repo / ".venv")
    client = Scripted([finish()])
    auto(repo, client)
    system = _system(client)
    assert "worktree" in system
    assert f"`{AUDIT_TEST_COMMAND}`" in system
    assert "project's own environment" in system
    assert "src/" in system and "PYTHONPATH" in system


def test_the_prompt_says_so_when_there_is_no_project_venv_and_no_src(tmp_path: Path) -> None:
    repo = _repo(tmp_path / "repo", src=False)
    client = Scripted([finish()])
    auto(repo, client)
    system = _system(client)
    assert f"`{AUDIT_TEST_COMMAND}`" in system
    assert "project's own environment" not in system
    assert "no virtual environment of its own" in system
    assert "PYTHONPATH" not in system
