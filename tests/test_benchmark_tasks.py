"""Benchmark tasks are whole, graded by tests that can fail, and kept out of lint.

Contract: every directory under benchmark/tasks has a task statement, hidden
tests and a reference solution; the hidden tests fail on the task's starting
code and pass on the reference; each behaviour the task's design says is
perturbed is caught by a hidden test; and the lint, type and collection tools
skip benchmark/tasks, because it holds planted bugs and adapted third-party
code on purpose.

The task layout is derived from the one tracked task and from how the grading
is described in docs/BENCHMARK.md and the task's own DESIGN.md: the arm gets
`SPEC.md`; grading copies `hidden_tests/` next to the arm's tree and runs
`python -m pytest`; `reference/` is the known-good tree that proves the hidden
suite can pass. A task's starting code is its optional `baseline/` directory;
without one the arm starts from an empty repository (task t7 says "Start from
the empty repo").

The hidden suites run here in a temporary copy, with the repository's pytest
configuration out of reach (`-o addopts=`, `rootdir` is the temp directory), so
a result cannot come from this repo's options, coverage gate or conftest.
"""

from __future__ import annotations

import re
import shutil
import subprocess
import sys
import tomllib
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
TASKS = ROOT / "benchmark" / "tasks"

# What a task directory must hold. DESIGN.md is where the adaptation's
# provenance and perturbations live; SPEC.md is the statement the arm is given.
REQUIRED_FILES = ("SPEC.md", "DESIGN.md")


def task_dirs(tasks: Path = TASKS) -> list[Path]:
    """Every task directory, found by listing (a dot-directory is a task too)."""
    return sorted(path for path in tasks.iterdir() if path.is_dir() and path.name != "__pycache__")


def missing_parts(task: Path) -> list[str]:
    """What `task` lacks, in words; empty when it is whole."""
    missing = [name for name in REQUIRED_FILES if not (task / name).is_file()]
    hidden = task / "hidden_tests"
    if not any(hidden.glob("test_*.py")) if hidden.is_dir() else True:
        missing.append("hidden_tests/test_*.py")
    reference = task / "reference"
    if not any(reference.glob("*.py")) if reference.is_dir() else True:
        missing.append("reference/*.py")
    return missing


TASK_DIRS = task_dirs()
TASK_IDS = [path.name for path in TASK_DIRS]


def _copy_tree(source: Path, dest: Path) -> None:
    shutil.copytree(source, dest, ignore=shutil.ignore_patterns("__pycache__"), dirs_exist_ok=True)


def run_hidden(
    task: Path, work: Path, *, solution: Path | None
) -> subprocess.CompletedProcess[str]:
    """Run `task`'s hidden tests in a copy holding `solution` (None: its starting code).

    The starting code is `baseline/` when the task has one, else nothing.
    """
    work.mkdir(parents=True)
    if solution is not None:
        _copy_tree(solution, work)
    elif (task / "baseline").is_dir():
        _copy_tree(task / "baseline", work)
    _copy_tree(task / "hidden_tests", work / "hidden_tests")
    return subprocess.run(
        [
            sys.executable,
            "-m",
            "pytest",
            "-p",
            "no:cacheprovider",
            "-o",
            "addopts=",
            "-q",
            "hidden_tests",
        ],
        cwd=work,
        capture_output=True,
        text=True,
        timeout=120,
    )


def _counts(output: str) -> dict[str, int]:
    """`{"passed": 63}` from pytest's summary line."""
    return {
        word: int(n)
        for n, word in re.findall(r"(\d+) (passed|failed|error|errors|skipped)", output)
    }


def test_there_are_tasks_to_check() -> None:
    """The parametrisation below covers exactly these; none would mean it covers nothing."""
    assert TASK_IDS, "no directories under benchmark/tasks"
    assert "t7" in TASK_IDS


@pytest.mark.parametrize("task", TASK_DIRS, ids=TASK_IDS)
def test_a_task_directory_holds_every_required_part(task: Path) -> None:
    assert missing_parts(task) == []


def test_a_task_missing_a_part_is_named(tmp_path: Path) -> None:
    """Known-bad: each absent part is reported, and a whole task is not."""
    whole = tmp_path / "whole"
    (whole / "hidden_tests").mkdir(parents=True)
    (whole / "reference").mkdir()
    for name in REQUIRED_FILES:
        (whole / name).write_text("x\n")
    (whole / "hidden_tests" / "test_h.py").write_text("def test_h(): ...\n")
    (whole / "reference" / "m.py").write_text("x = 1\n")
    assert missing_parts(whole) == []

    for part, expected in [
        ("SPEC.md", "SPEC.md"),
        ("DESIGN.md", "DESIGN.md"),
        ("hidden_tests/test_h.py", "hidden_tests/test_*.py"),
        ("reference/m.py", "reference/*.py"),
    ]:
        broken = tmp_path / f"broken-{expected.replace('/', '-')}"
        shutil.copytree(whole, broken)
        (broken / part).unlink()
        assert missing_parts(broken) == [expected]
    # A directory that is not there at all reads as missing, not as fine.
    bare = tmp_path / "bare"
    bare.mkdir()
    assert missing_parts(bare) == [*REQUIRED_FILES, "hidden_tests/test_*.py", "reference/*.py"]


@pytest.fixture(scope="module")
def graded(
    tmp_path_factory: pytest.TempPathFactory,
) -> dict[str, dict[str, subprocess.CompletedProcess[str]]]:
    """Each task's hidden suite run once on its starting code and once on its reference."""
    base = tmp_path_factory.mktemp("graded")
    runs: dict[str, dict[str, subprocess.CompletedProcess[str]]] = {}
    for task in TASK_DIRS:
        runs[task.name] = {
            "start": run_hidden(task, base / task.name / "start", solution=None),
            "reference": run_hidden(
                task, base / task.name / "reference", solution=task / "reference"
            ),
        }
    return runs


@pytest.mark.parametrize("task", TASK_IDS)
def test_the_hidden_tests_fail_on_the_starting_code_for_want_of_the_solution(
    task: str, graded: dict[str, dict[str, subprocess.CompletedProcess[str]]]
) -> None:
    """Fails, and fails because nothing is implemented (the import), not for some other reason."""
    proc = graded[task]["start"]
    output = proc.stdout + proc.stderr
    assert proc.returncode != 0, output[-1500:]
    counts = _counts(output)
    assert counts.get("passed", 0) == 0, output[-1500:]
    assert re.search(r"ModuleNotFoundError|ImportError|AssertionError|NotImplementedError", output)


@pytest.mark.parametrize("task", TASK_IDS)
def test_the_hidden_tests_pass_on_the_reference_solution(
    task: str, graded: dict[str, dict[str, subprocess.CompletedProcess[str]]]
) -> None:
    proc = graded[task]["reference"]
    output = proc.stdout + proc.stderr
    assert proc.returncode == 0, output[-1500:]
    counts = _counts(output)
    assert counts.get("passed", 0) > 0
    # Nothing skipped, failed or errored: a skipped hidden test grades nothing.
    assert set(counts) == {"passed"}, output[-1500:]


def test_the_reference_passes_the_number_of_hidden_tests_the_docs_state(
    graded: dict[str, dict[str, subprocess.CompletedProcess[str]]],
) -> None:
    """docs/BENCHMARK.md says task T7 grades on N hidden tests; the suite holds N."""
    docs = (ROOT / "docs" / "BENCHMARK.md").read_text(encoding="utf-8")
    stated = re.search(r"\*\*T7\b.*?Grading:\s*(\d+)\s+hidden\s+tests", docs, re.DOTALL)
    assert stated, "docs/BENCHMARK.md no longer states T7's hidden-test count"
    output = graded["t7"]["reference"].stdout
    assert _counts(output) == {"passed": int(stated.group(1))}


# Each perturbation the t7 design lists (DESIGN.md "Perturbations") as a
# one-line edit of the reference, with the text that must be present to edit.
# A mutant memorised upstream code would contain; the hidden suite must refuse it.
T7_PERTURBATIONS = {
    "P1 insert returns the index": (
        "        self._list.insert(pos, value)\n        return pos\n",
        "        self._list.insert(pos, value)\n",
    ),
    "P2 remove removes all and returns the count": (
        "        del self._list[start:stop]\n        return count\n",
        "        del self._list[start]\n",
    ),
    "P3 discard returns a bool": (
        "            del self._list[pos]\n            return True\n        return False\n",
        "            del self._list[pos]\n",
    ),
}


@pytest.mark.parametrize("perturbation", sorted(T7_PERTURBATIONS))
def test_t7_hidden_tests_refuse_a_reference_that_loses_a_perturbation(
    perturbation: str, tmp_path: Path
) -> None:
    """A suite that passes the reference and also passes upstream's behaviour grades nothing.

    Known-bad instance per perturbation: the reference with that one behaviour
    reverted must fail, and fail in the test file that names the behaviour.
    """
    task = TASKS / "t7"
    before, after = T7_PERTURBATIONS[perturbation]
    source = (task / "reference" / "orderedlist.py").read_text(encoding="utf-8")
    assert source.count(before) == 1, f"mutation target not found: {perturbation}"
    mutant = tmp_path / "mutant"
    _copy_tree(task / "reference", mutant)
    (mutant / "orderedlist.py").write_text(source.replace(before, after), encoding="utf-8")
    assert (mutant / "orderedlist.py").read_text(encoding="utf-8") != source
    proc = run_hidden(task, tmp_path / "work", solution=mutant)
    output = proc.stdout + proc.stderr
    assert proc.returncode != 0, output[-1500:]
    counts = _counts(output)
    assert counts.get("failed", 0) >= 1, output[-1500:]
    assert counts.get("passed", 0) > 0, output[-1500:]
    assert "test_hidden_perturb.py" in output or "test_hidden_orderedlist.py" in output


# -- kept out of lint ---------------------------------------------------------


def _ruff_files(cwd: Path) -> list[str]:
    proc = subprocess.run(
        ["ruff", "check", "--show-files", "--no-cache"],
        cwd=cwd,
        capture_output=True,
        text=True,
        check=True,
    )
    return [
        Path(line).resolve().relative_to(cwd.resolve()).as_posix()
        for line in proc.stdout.split("\n")
        if line
    ]


def _pyproject() -> dict[str, object]:
    with open(ROOT / "pyproject.toml", "rb") as fh:
        return tomllib.load(fh)


def test_ruff_skips_the_benchmark_tasks_and_still_checks_the_rest() -> None:
    files = _ruff_files(ROOT)
    assert "benchmark/stall_check.py" in files, "ruff lists nothing near the tasks: vacuous"
    assert len(files) > 100
    assert [f for f in files if f.startswith("benchmark/tasks/")] == []


def test_ruff_would_lint_a_task_file_without_the_exclusion(tmp_path: Path) -> None:
    """Known-bad: the same configuration minus its `exclude` lists the task's files."""
    config = (ROOT / "pyproject.toml").read_text(encoding="utf-8")
    stripped = re.sub(r"(?m)^exclude = \[[^\]]*\]\n", "", config, count=1)
    assert stripped != config, "no `exclude = [...]` line found in pyproject.toml"
    (tmp_path / "pyproject.toml").write_text(stripped, encoding="utf-8")
    task = tmp_path / "benchmark" / "tasks" / "t0"
    task.mkdir(parents=True)
    (task / "planted.py").write_text("x = 1\n", encoding="utf-8")
    assert "benchmark/tasks/t0/planted.py" in _ruff_files(tmp_path)
    # And with the real configuration the same file is skipped.
    (tmp_path / "pyproject.toml").write_text(config, encoding="utf-8")
    assert "benchmark/tasks/t0/planted.py" not in _ruff_files(tmp_path)


def _collected(args: list[str]) -> str:
    """pytest's collection report for `args`, run at the repo root with its real config."""
    proc = subprocess.run(
        [
            *(sys.executable, "-m", "pytest", "--collect-only", "-q", "-p", "no:cacheprovider"),
            *("-p", "no:cov", "-o", "addopts="),
            *args,
        ],
        cwd=ROOT,
        capture_output=True,
        text=True,
    )
    return proc.stdout + proc.stderr


def test_pytest_does_not_collect_the_benchmark_tasks() -> None:
    """Asked for the whole benchmark directory, pytest still skips the tasks' grading tests."""
    assert "benchmark/tasks" not in _collected(["benchmark"])


def test_pytest_would_collect_the_hidden_tests_without_the_exclusion() -> None:
    """Known-bad: with `norecursedirs` overridden the grading tests appear in the report."""
    assert "benchmark/tasks/t7/hidden_tests" in _collected(
        ["-o", "norecursedirs=none", "benchmark"]
    )


def _target_contains_tasks(target: str) -> bool:
    path = (ROOT / target).resolve()
    return path == TASKS or path in TASKS.parents or TASKS in path.parents


def test_mypy_is_never_pointed_at_the_benchmark_tasks() -> None:
    """mypy has no skip list here (`[tool.mypy]` sets no `files` or `exclude`): the
    tasks stay out because no target contains them. The targets are the
    `static-check` command the audit runs and the one `check.sh` runs."""
    config = _pyproject()
    tool = config["tool"]
    assert isinstance(tool, dict)
    assert "files" not in tool["mypy"], "mypy `files` would add targets this test does not read"
    audit = tool["saddle"]["static-check"]
    gate = re.search(r"(?m)^uv run mypy (.+)$", (ROOT / "check.sh").read_text(encoding="utf-8"))
    assert gate, "check.sh no longer runs mypy by that command"
    assert audit[0] == "mypy"
    targets = [*audit[1:], *gate.group(1).split()]
    assert len(targets) >= 4
    assert not any(_target_contains_tasks(t) for t in targets), targets


def test_a_mypy_target_that_contains_the_tasks_is_caught() -> None:
    """Known-bad: the root, `benchmark` and the tasks directory all contain them."""
    assert _target_contains_tasks(".")
    assert _target_contains_tasks("benchmark")
    assert _target_contains_tasks("benchmark/tasks")
    assert _target_contains_tasks("benchmark/tasks/t7")
    assert not _target_contains_tasks("src")
    assert not _target_contains_tasks("tests")
    assert not _target_contains_tasks("benchmark/stall_check.py")
