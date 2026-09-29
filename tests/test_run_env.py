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
    assert "/.saddle/worktrees/" in detail, detail
    assert "/src/pkg/__init__.py" in detail, detail


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
    assert "src/" in system
    assert "PYTHONPATH" in system


def test_the_prompt_says_so_when_there_is_no_project_venv_and_no_src(tmp_path: Path) -> None:
    repo = _repo(tmp_path / "repo", src=False)
    client = Scripted([finish()])
    auto(repo, client)
    system = _system(client)
    assert f"`{AUDIT_TEST_COMMAND}`" in system
    assert "project's own environment" not in system
    assert "no virtual environment of its own" in system
    assert "PYTHONPATH" not in system


def test_the_prompt_states_the_runs_budgets_as_given(tmp_path: Path) -> None:
    """Red before: the model was never told its time or token budget. Two
    budgets, two sentences: the numbers come from the run, not the text."""
    short = Scripted([finish()])
    auto(_repo(tmp_path / "a", src=False), short, time_budget_s=600.0, token_budget=5_000)
    long = Scripted([finish()])
    auto(_repo(tmp_path / "b", src=False), long, time_budget_s=5_400.0, token_budget=120_000)
    assert "This run has 10 minutes and 5,000 generated tokens" in _system(short)
    assert "This run has 90 minutes and 120,000 generated tokens" in _system(long)
    assert "run the test files that cover your change first" in _system(short)


def test_a_budget_under_a_minute_still_reads_as_one_minute(tmp_path: Path) -> None:
    repo = _repo(tmp_path / "repo", src=False)
    client = Scripted([finish()])
    auto(repo, client, time_budget_s=20.0)
    assert "This run has 1 minute and " in _system(client)


def test_the_prompt_warns_about_coverage_in_addopts_only_when_it_is_there(
    tmp_path: Path,
) -> None:
    """Red before: a one-file test run on a repo whose addopts carry --cov
    failed on package coverage, and nothing said why. Instances: pyproject
    addopts (string and list), pytest.ini addopts, and a repo without."""
    cases = {
        "toml": ("pyproject.toml", '[tool.pytest.ini_options]\naddopts = "--cov=pkg"\n'),
        "list": ("pyproject.toml", '[tool.pytest.ini_options]\naddopts = ["-q", "--cov=pkg"]\n'),
        "ini": ("pytest.ini", "[pytest]\naddopts = --cov=pkg\n"),
        "none": ("pyproject.toml", '[tool.pytest.ini_options]\naddopts = "-q"\n'),
        "broken": ("pyproject.toml", "[tool.pytest.ini_options\naddopts = --cov\n"),
    }
    said: dict[str, bool] = {}
    for name, (config, text) in cases.items():
        root = tmp_path / name
        root.mkdir()
        (root / config).write_text(text)
        (root / "tests").mkdir()
        (root / "n.py").write_text(FLAT)
        (root / "tests" / "test_x.py").write_text("def test_x():\n    assert True\n")
        git(root, "init", "-q", "-b", "main")
        git(root, "add", "-A")
        git(root, "commit", "-q", "-m", "init")
        client = Scripted([finish()])
        auto(root, client)
        said[name] = "--no-cov" in _system(client)
    assert said == {"toml": True, "list": True, "ini": True, "none": False, "broken": False}


def test_the_prompt_names_the_worktree_and_not_the_checkout_it_hides(tmp_path: Path) -> None:
    """Red before: the prompt printed the project venv's absolute path, inside
    the main checkout; a watched run `cd`'d there, found it hidden by the
    sandbox, and lost two rounds asking where it was."""
    repo = _repo(tmp_path / "repo", src=True)
    make_venv(repo / ".venv")
    client = Scripted([finish()])
    auto(repo, client)
    system = _system(client)
    worktree = system.split("Your working directory is ", 1)[1].split(",", 1)[0]
    assert "/.saddle/worktrees/" in worktree
    assert Path(worktree).is_absolute()
    assert str((repo / ".venv").resolve()) not in system
    assert "not visible" in system


def test_the_prompt_explains_audit_notes_only_when_the_run_delivers_them(
    tmp_path: Path,
) -> None:
    """Red before: arm E+A+F appends audit results to tool results and the
    model was never told; a watched run guessed they came from the repo."""
    said: dict[str, bool] = {}
    for arm in ("E+A+F", "E+A", "E"):
        client = Scripted([finish()])
        auto(_repo(tmp_path / arm.replace("+", "p"), src=False), client, arm=arm)
        said[arm] = "audit checkpoint N on tree <id>, then PASS or FAIL" in _system(client)
    assert said == {"E+A+F": True, "E+A": False, "E": False}
