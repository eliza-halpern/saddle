"""The audit runs a project's suite on pytest-xdist workers when the project asks.

Contract: when the `pyproject.toml` committed at the audit's baseline sets
`[tool.saddle] test-workers = N` (N >= 2) and pytest, started as the test
command starts it in the gate's sandbox, loads pytest-xdist and pytest-cov,
the audit runs the suite as `pytest -n N` under pytest-cov, recorded into
the one data file the `tests` and `coverage` checks read. Its verdicts and
its covered lines equal the serial run's on the same tree. Otherwise the
run is the serial one, unchanged, and with N >= 2 the `tests` finding says
why ("test-workers = N set but pytest-xdist is not installed: ran
serially"). The setting is never read from the tree audited.

Why (scope narrowed to projects that opt in): saddle's own suite takes about
twenty minutes serially and about six on eight workers, and every gate run
of it waited the twenty.

Known-good: the serial and parallel runs of one tree record the same lines
over every file of a project whose modules are imported at conftest time,
at collection and inside a test body. Known-bad: a changed line no test
runs is still named under workers, a failing test still fails the tests
check, a coverage file left in the tree adds no line, a missing or disabled
plugin never reaches a parallel run, and a tree that sets the key itself
changes nothing.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path
from typing import Any, Final

import coverage
import pytest

from saddle import evidence
from saddle import runner as runner_module
from saddle.audit import AuditError, audit_node, audit_tree, staged_copy
from saddle.auditor import Auditor, Finding, Findings
from saddle.dag import Node
from saddle.evidence import (
    RAN_SERIALLY,
    SUITE_WORKERS_MAX,
    SuiteLimitError,
    SuiteRun,
    SuiteWorkers,
    covered_lines,
    covering_tests,
    run_argv,
    run_suite_capture,
    suite_limit,
    suite_run,
    suite_workers,
)
from saddle.gates import RED_PHASE_SAMPLES
from saddle.journal import SpanRecorder, read_spans
from saddle.runner import run_node_gate

COMMAND: Final = "python -m pytest -q"


def _git(root: Path, *argv: str) -> None:
    assert run_argv(["git", *argv], root) == 0


def _commit(root: Path, files: dict[str, str], message: str = "commit") -> str:
    """Write `files`, commit everything, return the commit's sha."""
    if not (root / ".git").exists():
        root.mkdir(parents=True, exist_ok=True)
        _git(root, "init", "-q")
        _git(root, "config", "user.email", "test@example.com")
        _git(root, "config", "user.name", "test")
    for name, text in files.items():
        (root / name).parent.mkdir(parents=True, exist_ok=True)
        (root / name).write_text(text)
    _git(root, "add", "-A")
    _git(root, "commit", "-q", "--allow-empty", "-m", message)
    return evidence.run_capture(["git", "rev-parse", "HEAD"], root).stdout.strip()


def _pyproject(table: str = "", addopts: str | None = None) -> str:
    text = '[project]\nname = "pkg"\n'
    if addopts is not None:
        text += f'\n[tool.pytest.ini_options]\naddopts = "{addopts}"\n'
    if table:
        text += f"\n[tool.saddle]\n{table}"
    return text


def _workers(count: int) -> str:
    return f"test-workers = {count}\n"


# -- the project: modules imported at conftest time, at collection, and lazily --

EARLY: Final = '''"""Imported by conftest.py at module level."""

EARLY = [n * 2 for n in range(3)]


def early_used():
    return EARLY[1]


def early_unused():
    return "never"
'''

LAZY: Final = '''"""Imported only inside a test body."""


class Thing:
    kind = "thing"

    def go(self, x):
        if x > 0:
            return "pos"
        return "nonpos"
'''

CALC: Final = """def add(a, b):
    return a + b


def sub(a, b):
    return a - b


def mul(a, b):
    total = 0
    for _ in range(b):
        total += a
    return total
"""

CALC_CHANGED: Final = CALC.replace(
    "    return a + b\n", "    total = a\n    total += b\n    return total\n"
)
"""A behaviour-preserving edit: three changed statements the tests run."""

UNREACHED: Final = "\n\ndef unreached():\n    return 1\n"
"""A public function nothing calls: its changed lines no test runs."""

CONFTEST: Final = "from pkg.early import early_used\n\nBASE = early_used()\n"


def _part(i: int) -> str:
    return (
        "from pkg.calc import add, sub\n\n\n"
        f"def test_add_{i}():\n    assert add({i}, 1) == {i} + 1\n\n\n"
        f"def test_sub_{i}():\n    assert sub({i}, 1) == {i} - 1\n"
    )


LAZY_TEST: Final = (
    'def test_lazy():\n    from pkg.lazy import Thing\n\n    assert Thing().go(1) == "pos"\n'
)


def _files(pyproject: str) -> dict[str, str]:
    files = {
        "pyproject.toml": pyproject,
        "src/pkg/__init__.py": '"""pkg."""\n',
        "src/pkg/early.py": EARLY,
        "src/pkg/lazy.py": LAZY,
        "src/pkg/calc.py": CALC,
        "tests/conftest.py": CONFTEST,
        "tests/test_lazy.py": LAZY_TEST,
    }
    files.update({f"tests/test_part{i}.py": _part(i) for i in range(1, 5)})
    return files


def _project(root: Path, pyproject: str) -> Path:
    """The project committed, then the working-tree edit the audit judges."""
    _commit(root, _files(pyproject), "baseline")
    (root / "src/pkg/calc.py").write_text(CALC_CHANGED)
    (root / "src/pkg/lazy.py").write_text(LAZY + UNREACHED)
    return root


def _python_files(root: Path) -> list[str]:
    """Every .py file under `root`, dot directories included (os.walk, not glob)."""
    found = []
    for folder, dirs, names in os.walk(root):
        dirs[:] = [d for d in dirs if d != "__pycache__"]
        found += [os.path.join(folder, n) for n in names if n.endswith(".py")]
    return sorted(found)


def _own_data(root: Path) -> str:
    """The data file pytest-cov records into for a project with no coverage config."""
    return str(root.resolve() / ".coverage")


def _env_without(root: Path, monkeypatch: pytest.MonkeyPatch, *prefixes: str) -> None:
    """Put first on PATH a virtualenv holding this one's packages except the
    site-packages entries starting with one of `prefixes`: a test environment
    without that plugin, made from the packages already installed."""
    venv = root / "venv"
    subprocess.run([sys.executable, "-m", "venv", "--without-pip", str(venv)], check=True)
    version = f"python{sys.version_info.major}.{sys.version_info.minor}"
    there = venv / "lib" / version / "site-packages"
    here = Path(coverage.__file__).resolve().parent.parent
    for entry in here.iterdir():
        if not entry.name.startswith(prefixes):
            (there / entry.name).symlink_to(entry)
    monkeypatch.setenv("PATH", os.pathsep.join([str(venv / "bin"), os.environ["PATH"]]))


NO_XDIST: Final = ("xdist", "pytest_xdist")
NO_COV: Final = ("pytest_cov",)


def _finding(result: Findings, gate: str) -> Finding:
    found: Finding = next(f for f in result.findings if f.gate == gate)
    return found


# -- the setting: read where the task started, like test-timeout -----------------


@pytest.mark.parametrize(
    "files",
    [
        {},
        {"pyproject.toml": '[project]\nname = "p"\n'},
        {"pyproject.toml": "[tool.saddle]\n"},
        {"pyproject.toml": "[tool.saddle]\ntest-timeout = 60\n"},
    ],
    ids=["no-file", "no-table", "empty-table", "timeout-only"],
)
def test_without_a_setting_the_suite_runs_serially(tmp_path: Path, files: dict[str, str]) -> None:
    sha = _commit(tmp_path, {"n.py": "x = 1\n", **files})
    found = suite_workers(tmp_path, "HEAD")
    assert found.count == 1
    assert found.source.startswith("serial (no ")
    assert sha[:12] in found.source


@pytest.mark.parametrize("count", [1, 2, 8, SUITE_WORKERS_MAX])
def test_the_setting_committed_at_the_rev_is_the_worker_count(tmp_path: Path, count: int) -> None:
    sha = _commit(tmp_path, {"pyproject.toml": _pyproject(_workers(count))})
    assert suite_workers(tmp_path, sha) == SuiteWorkers(
        count, f"[tool.saddle] test-workers in pyproject.toml at {sha[:12]}"
    )


@pytest.mark.parametrize(
    "written",
    ["0", "-2", f"{SUITE_WORKERS_MAX + 1}", "2.5", "8.0", '"8"', '"auto"', "true", "[8]"],
)
def test_a_value_that_is_not_a_worker_count_is_refused_by_name(
    tmp_path: Path, written: str
) -> None:
    """A bool (TOML's `true` is not one worker), a float, a string and a list
    are refused, as are none and more than the cap: never read as serial."""
    sha = _commit(tmp_path, {"pyproject.toml": _pyproject(f"test-workers = {written}\n")})
    with pytest.raises(SuiteLimitError) as caught:
        suite_workers(tmp_path, "HEAD")
    message = str(caught.value)
    assert message.startswith("cannot read the test worker count: ")
    assert f"pyproject.toml at {sha[:12]}: test-workers = " in message
    assert f"is not a whole number of workers from 1 to {SUITE_WORKERS_MAX}" in message


def test_the_worker_count_is_read_beside_the_time_limit(tmp_path: Path) -> None:
    """Known-good for the shared reader: each key is read, neither refuses the other."""
    _commit(tmp_path, {"pyproject.toml": _pyproject("test-timeout = 60\ntest-workers = 4\n")})
    assert suite_limit(tmp_path, "HEAD").seconds == 60.0
    assert suite_workers(tmp_path, "HEAD").count == 4


@pytest.mark.parametrize("key", ["test-worker", "test_workers", "workers", "test_timeout"])
def test_a_key_saddle_does_not_read_is_refused_by_both_readers(tmp_path: Path, key: str) -> None:
    """Known-bad: a typo of either key must not read as "not set"."""
    _commit(tmp_path, {"pyproject.toml": _pyproject(f"{key} = 8\n")})
    reads = "the keys saddle reads there are test-timeout and test-workers"
    for read in (suite_limit, suite_workers):
        with pytest.raises(SuiteLimitError, match=reads) as caught:
            read(tmp_path, "HEAD")
        assert f"[tool.saddle] holds {key};" in str(caught.value)


def test_edits_after_the_rev_change_nothing(tmp_path: Path) -> None:
    """The tree choosing its own run mode, unstaged, staged or committed on
    top, never reaches the count read at the rev."""
    base = _commit(tmp_path, {"pyproject.toml": _pyproject()})
    (tmp_path / "pyproject.toml").write_text(_pyproject(_workers(8)))
    assert suite_workers(tmp_path, base).count == 1
    assert suite_workers(tmp_path, "HEAD").count == 1
    _git(tmp_path, "add", "-A")
    assert suite_workers(tmp_path, "HEAD").count == 1
    later = _commit(tmp_path, {}, "ask for workers")
    assert suite_workers(tmp_path, base).count == 1
    assert suite_workers(tmp_path, later).count == 8


# -- the probe: pytest's own view of its plugins ----------------------------------


def test_one_worker_asks_pytest_nothing(tmp_path: Path) -> None:
    """A serial run of a project whose options cannot start pytest-cov pays
    for no probe: nothing in its test command or pytest config says `--cov`."""
    root = _project(tmp_path / "p", _pyproject())
    (root / "tox.ini").mkdir()  # unreadable as a file: skipped, not an error
    journal = tmp_path / "spans.jsonl"
    recorder = SpanRecorder(path=journal, node_id="n")
    assert suite_run(root, COMMAND, 1, recorder=recorder) == SuiteRun()
    assert not journal.exists()
    assert suite_run(root, COMMAND, 3, recorder=recorder) == SuiteRun(
        workers=3, data_file=_own_data(root)
    )
    assert len(journal.read_text().splitlines()) == 1


@pytest.mark.parametrize(
    ("config", "command", "project_cov"),
    [
        ({"pyproject.toml": _pyproject(addopts="--cov=pkg")}, COMMAND, True),
        ({"setup.cfg": "[tool:pytest]\naddopts = --cov=pkg\n"}, COMMAND, True),
        ({"pytest.ini": "[pytest]\naddopts = --cov=pkg --no-cov\n"}, COMMAND, False),
        ({"tox.ini": "# --cov is only mentioned here\n"}, COMMAND, False),
        ({}, f"{COMMAND} --cov=pkg", True),
    ],
    ids=["pyproject", "setup-cfg", "no-cov-wins", "comment-only", "command"],
)
def test_a_serial_run_asks_pytest_whenever_the_project_may_start_pytest_cov(
    tmp_path: Path, config: dict[str, str], command: str, project_cov: bool
) -> None:
    """A mention of `--cov` only makes the gate ask; pytest's answer decides."""
    root = _project(tmp_path / "p", _pyproject())
    for name, text in config.items():
        (root / name).write_text(text)
    journal = tmp_path / "spans.jsonl"
    found = suite_run(root, command, 1, recorder=SpanRecorder(path=journal, node_id="n"))
    own = _own_data(root) if project_cov else ""
    assert found == SuiteRun(project_cov=project_cov, data_file=own)
    assert len(journal.read_text().splitlines()) == 1


def test_with_both_plugins_the_suite_runs_on_workers(tmp_path: Path) -> None:
    root = _project(tmp_path / "p", _pyproject())
    assert suite_run(root, COMMAND, 3) == SuiteRun(workers=3, data_file=_own_data(root))


def test_a_project_that_starts_pytest_cov_keeps_its_own_options(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = _project(tmp_path / "p", _pyproject(addopts="--cov=pkg --cov-report=term-missing"))
    own = _own_data(root)
    assert suite_run(root, COMMAND, 2) == SuiteRun(workers=2, project_cov=True, data_file=own)
    assert suite_run(root, COMMAND, 1) == SuiteRun(project_cov=True, data_file=own)
    _env_without(tmp_path, monkeypatch, *NO_XDIST)
    assert suite_run(root, COMMAND, 2) == SuiteRun(
        project_cov=True,
        note=f"test-workers = 2 set but pytest-xdist is not installed: {RAN_SERIALLY}",
        data_file=own,
    )


@pytest.mark.parametrize(
    ("missing", "addopts", "why"),
    [
        (NO_XDIST, None, "pytest-xdist is not installed"),
        (NO_COV, None, "pytest-cov is not installed"),
        (
            NO_XDIST + NO_COV,
            None,
            "pytest-xdist is not installed and pytest-cov is not installed",
        ),
        ((), "-p no:xdist", "the project's pytest options disable pytest-xdist"),
        ((), "-p no:cov", "the project's pytest options disable pytest-cov"),
        ((), "--no-cov", "the project's pytest options disable pytest-cov (--no-cov)"),
    ],
    ids=["no-xdist", "no-cov", "neither", "xdist-disabled", "cov-disabled", "no-cov-flag"],
)
def test_each_way_a_plugin_is_missing_is_named(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    missing: tuple[str, ...],
    addopts: str | None,
    why: str,
) -> None:
    """Known-bad: an environment the parallel run would fail in (`-n` or
    `--cov` a usage error, or no coverage recorded) never gets one."""
    if missing:
        _env_without(tmp_path, monkeypatch, *missing)
    root = _project(tmp_path / "p", _pyproject(addopts=addopts))
    assert suite_run(root, COMMAND, 2) == SuiteRun(
        note=f"test-workers = 2 set but {why}: {RAN_SERIALLY}"
    )


@pytest.mark.parametrize("command", ["make test", "", "coverage run -m pytest", "tox -e py"])
def test_a_command_that_does_not_start_pytest_runs_serially(tmp_path: Path, command: str) -> None:
    root = _project(tmp_path / "p", _pyproject(addopts="--cov=pkg"))
    assert suite_run(root, command, 2) == SuiteRun(
        note=f"test-workers = 2 set but the test command does not start pytest: {RAN_SERIALLY}"
    )
    assert suite_run(root, command, 1) == SuiteRun()


@pytest.mark.parametrize(
    "command", ["pytest", "py.test -q", "python3 -m pytest tests", "/usr/bin/python3.12 -m pytest"]
)
def test_each_way_of_starting_pytest_is_probed(command: str) -> None:
    assert evidence._starts_pytest(command.split())


BOGUS_MARK: Final = "import atexit\n\natexit.register(print, 'saddle-workers-probe {not json')\n"
"""A conftest that prints the probe's marker after the probe has answered."""


@pytest.mark.parametrize(
    ("conftest", "why"),
    [
        ("raise RuntimeError('broken conftest')\n", "exit 4"),
        (BOGUS_MARK, "exit 0"),
        ("import time\n\ntime.sleep(60)\n", "timed out"),
    ],
    ids=["conftest-raises", "bogus-report", "hangs"],
)
def test_a_probe_that_does_not_answer_is_named_never_read_as_missing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, conftest: str, why: str
) -> None:
    """A lookup that fails is reported as a failure, with its exit, not as
    "no plugins"; a tree printing the probe's marker cannot crash the audit."""
    monkeypatch.setattr(evidence, "WORKERS_PROBE_TIMEOUT_S", 5.0)
    root = _project(tmp_path / "p", _pyproject())
    (root / "tests/conftest.py").write_text(conftest)
    found = suite_run(root, COMMAND, 2)
    assert found == SuiteRun(
        note=f"test-workers = 2 set but pytest did not say which plugins it loads ({why}): "
        f"{RAN_SERIALLY}"
    )
    (root / "pytest.ini").write_text("[pytest]\naddopts = --cov=pkg\n")
    assert suite_run(root, COMMAND, 1) == SuiteRun()  # as before: `coverage run`


# -- the run: the same lines, from the controller and every worker ----------------


def _record(root: Path, run: SuiteRun, name: str) -> set[tuple[str, int]]:
    data_file = str(root / name)
    done = run_suite_capture(run, COMMAND, root, data_file, timeout=120)
    assert done.exit_code == 0, done.stdout + done.stderr
    return covered_lines(data_file, _python_files(root))


def test_serial_and_parallel_runs_record_the_same_lines(tmp_path: Path) -> None:
    """Known-good over every file: modules run at conftest import (in the
    controller), at collection and inside a test body (in the workers)."""
    root = _project(tmp_path / "p", _pyproject())
    assert len(_python_files(root)) == 10  # 4 source modules, 6 test modules
    serial = _record(root, SuiteRun(), ".coverage.serial")
    run = suite_run(root, COMMAND, 2)
    assert run.parallel
    parallel = _record(root, run, ".coverage.parallel")
    assert parallel == serial
    lazy = str(root / "src/pkg/lazy.py")
    # `go`'s body runs only in a worker; `early_used` at conftest import.
    assert {(lazy, 8), (lazy, 9), (str(root / "src/pkg/early.py"), 7)} <= parallel
    assert (lazy, 10) not in parallel  # `return "nonpos"`: no test takes it


def test_a_project_starting_pytest_cov_records_what_its_serial_pytest_cov_records(
    tmp_path: Path,
) -> None:
    """With the project's own `--cov=pkg`, `coverage run` records nothing
    (pytest-cov displaces its tracer), so the gate records such a suite by
    the project's pytest-cov, serially and on workers; the reference is the
    project's own serial pytest-cov run."""
    root = _project(tmp_path / "p", _pyproject(addopts="--cov=pkg"))
    blind = _record(root, SuiteRun(), ".coverage.gate")
    assert blind == set()
    reference = str(root / ".coverage.reference")
    env = {**os.environ, "COVERAGE_FILE": reference, "PYTHONPATH": str(root / "src")}
    subprocess.run(
        [sys.executable, "-m", "pytest", "-q", "-p", "no:xdist"],
        cwd=root,
        env=env,
        check=True,
        capture_output=True,
    )
    expected = covered_lines(reference, _python_files(root))
    assert expected
    # flip: was `== expected`. The gate now measures the whole tree (`--cov=.`)
    # beside the project's own sources, so it records every line the
    # project's run records AND the test files that run (which the project's
    # `--cov=pkg` leaves out); see
    # test_changed_test_lines_are_measured_whatever_the_projects_coverage_source.
    for recorded in (
        _record(root, suite_run(root, COMMAND, 1), ".coverage.serial"),
        _record(root, suite_run(root, COMMAND, 2), ".coverage.parallel"),
    ):
        assert expected <= recorded
        extra = {path for path, _ in recorded - expected}
        assert extra
        assert all("/tests/" in path for path in extra), extra


@pytest.mark.parametrize(
    ("addopts", "workers"),
    [(None, 2), ("--cov-append", 2), ("--cov=pkg --cov-append", 2), ("--cov=pkg --cov-append", 1)],
    ids=["parallel", "parallel-append", "project-cov-append", "serial-project-cov-append"],
)
def test_a_coverage_file_left_in_the_tree_adds_no_line(
    tmp_path: Path, addopts: str | None, workers: int
) -> None:
    """Known-bad: a data file of the gate's name already in the tree (the
    file itself, or a suffix file pytest-cov would combine) cannot mark a line
    no test runs as covered, even under a project's `--cov-append`, which
    keeps old data."""
    root = _project(tmp_path / "p", _pyproject(addopts=addopts))
    lazy = str(root / "src/pkg/lazy.py")
    for name in (".coverage", ".coverage.left.1.2", ".coverage.gate", ".coverage.gate.left.1.2"):
        planted = coverage.CoverageData(basename=str(root / name))
        planted.add_lines({lazy: [10]})
        planted.write()
    run = suite_run(root, COMMAND, workers)
    assert run.by_pytest_cov
    recorded = _record(root, run, ".coverage.gate")
    assert (lazy, 9) in recorded
    assert (lazy, 10) not in recorded


NESTED_TEST: Final = """import subprocess
import sys


def test_runs_its_own_coverage(tmp_path):
    (tmp_path / "m.py").write_text("x = 1\\n")
    (tmp_path / "test_m.py").write_text("import m\\n\\n\\ndef test_m():\\n    assert m.x == 1\\n")
    done = subprocess.run(
        [sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider", "--cov=m"],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        check=False,
    )
    assert done.returncode == 0, done.stdout
"""
"""A test that runs pytest with coverage on a project of its own, in its own
directory, with no data file named: statement data, and the default name."""


@pytest.mark.parametrize(
    ("addopts", "workers"),
    [(None, 2), ("--cov=pkg", 1), ("--cov=pkg", 2)],
    ids=["parallel", "serial-project-cov", "parallel-project-cov"],
)
def test_a_test_that_runs_its_own_coverage_leaves_the_gates_data_alone(
    tmp_path: Path, addopts: str | None, workers: int
) -> None:
    """Known-bad for a data file named through the environment: the nested
    run inherited it, wrote statement data beside the gate's branch data, and
    pytest-cov's combine failed ("Can't combine branch coverage data with
    statement data", exit 3) on saddle's own suite. The gate's lines are
    still the suite's."""
    pyproject = _pyproject(addopts=addopts) + "\n[tool.coverage.run]\nbranch = true\n"
    root = _project(tmp_path / "p", pyproject)
    (root / "tests/test_nested.py").write_text(NESTED_TEST)
    run = suite_run(root, COMMAND, workers)
    assert run.by_pytest_cov
    recorded = _record(root, run, ".coverage.gate")
    lazy = str(root / "src/pkg/lazy.py")
    assert (lazy, 9) in recorded
    assert (lazy, 10) not in recorded


def test_the_projects_own_data_file_is_where_the_lines_are_read(tmp_path: Path) -> None:
    """Known-good: a coverage config naming its own data file in a subfolder
    is where pytest-cov records, and the gate reads the same lines from it."""
    pyproject = _pyproject() + '\n[tool.coverage.run]\ndata_file = "cov/data"\n'
    root = _project(tmp_path / "p", pyproject)
    (root / "cov").mkdir()
    run = suite_run(root, COMMAND, 2)
    assert run == SuiteRun(workers=2, data_file=str(root.resolve() / "cov/data"))
    assert _record(root, run, ".coverage.gate") == _record(root, SuiteRun(), ".coverage.serial")


def test_a_data_file_outside_the_tree_runs_serially(tmp_path: Path) -> None:
    """Known-bad: pytest-cov would record where the sandbox lets no run write."""
    elsewhere = tmp_path / "elsewhere" / ".coverage"
    pyproject = (
        _pyproject(addopts="--cov=pkg") + f'\n[tool.coverage.run]\ndata_file = "{elsewhere}"\n'
    )
    root = _project(tmp_path / "p", pyproject)
    why = f"pytest-cov records outside the tree ({elsewhere})"
    assert suite_run(root, COMMAND, 2) == SuiteRun(
        note=f"test-workers = 2 set but {why}: {RAN_SERIALLY}"
    )
    assert suite_run(root, COMMAND, 1) == SuiteRun()


# -- the schedule: an idle worker takes queued tests; a project's own mode stays --

QUICK: Final = 40

BEHIND_A_SLOW_TEST: Final = (
    "import time\nfrom pathlib import Path\n\nROOT = Path(__file__).resolve().parents[1]\n\n\n"
    "def test_slow():\n"
    "    deadline = time.monotonic() + 30\n"
    f"    while len(list(ROOT.glob('*.ran'))) < {QUICK - 1} and time.monotonic() < deadline:\n"
    "        time.sleep(0.05)\n"
    "    (ROOT / 'seen').write_text(str(len(list(ROOT.glob('*.ran')))))\n"
    + "".join(
        f"\n\ndef test_quick_{i:02}():\n    (ROOT / '{i:02}.ran').touch()\n" for i in range(QUICK)
    )
)
"""`test_slow` first in the file, then forty quick tests. `test_slow` waits
(30 s at most) until all but one of them have run, and writes how many had.
Its worker holds one quick test as the next item, which nothing can take;
every other quick test sent to it must be taken by the idle worker."""


def _schedule_project(root: Path, tests: str, addopts: str | None = None) -> Path:
    root.mkdir(parents=True)
    (root / "pyproject.toml").write_text(_pyproject(addopts=addopts))
    (root / "tests").mkdir()
    (root / "tests/test_schedule.py").write_text(tests)
    return root


def test_an_idle_worker_takes_the_tests_queued_behind_a_slow_one(tmp_path: Path) -> None:
    """`--dist worksteal`: 39 of the 40 quick tests run while `test_slow`
    waits. Under xdist's default `load` its worker keeps the first chunk it
    was sent (`test_slow` and four quick tests) and nothing else can run
    them: 36, after the whole 30 s wait."""
    root = _schedule_project(tmp_path / "p", BEHIND_A_SLOW_TEST)
    run = suite_run(root, COMMAND, 2)
    assert (run.workers, run.dist) == (2, "")
    done = run_suite_capture(run, COMMAND, root, str(root / ".coverage.gate"), timeout=120)
    assert done.exit_code == 0, done.stdout + done.stderr
    assert (root / "seen").read_text() == str(QUICK - 1)
    assert len(list(root.glob("*.ran"))) == QUICK


ONE_FILE: Final = (
    "import os\nfrom pathlib import Path\n\nROOT = Path(__file__).resolve().parents[1]\n"
    "WORKER = os.environ['PYTEST_XDIST_WORKER']\n"
    + "".join(
        f"\n\ndef test_{i:02}():\n    (ROOT / f'{{WORKER}}.{i:02}').touch()\n" for i in range(QUICK)
    )
)
"""Forty tests in one file, each leaving a file named for its worker."""


@pytest.mark.parametrize(
    ("addopts", "workers"), [(None, 2), ("--dist loadfile", 1)], ids=["worksteal", "loadfile"]
)
def test_a_distribution_mode_the_project_chooses_is_kept(
    tmp_path: Path, addopts: str | None, workers: int
) -> None:
    """Known-good: a project whose options say `--dist loadfile` (its tests
    must share a worker per file) runs every test of the file on one worker.
    Known-bad: the same file without it is split between the two workers,
    as `worksteal` splits it; a `--dist worksteal` added over the project's
    own mode split it too."""
    root = _schedule_project(tmp_path / "p", ONE_FILE, addopts)
    run = suite_run(root, COMMAND, 2)
    done = run_suite_capture(run, COMMAND, root, str(root / ".coverage.gate"), timeout=120)
    assert done.exit_code == 0, done.stdout + done.stderr
    ran = [path.name.split(".")[0] for path in root.iterdir() if path.name.startswith("gw")]
    assert len(ran) == QUICK
    assert len(set(ran)) == workers


# -- the gate: one tree, two runs, the same verdicts ----------------------------


def _gate_both_ways(
    root: Path, *, tier2: bool = False
) -> tuple[dict[str, tuple[bool, str, str | None]], ...]:
    """`run_node_gate` over one staged copy of `root`, serially and on two
    workers: each result's checks by name, then its uncovered lines."""
    results = []
    with staged_copy(root, "HEAD") as (copy, _staged, resolved):
        for workers in (1, 2):
            gated = run_node_gate(
                audit_node(), copy, baseline=resolved, tier2=tier2, test_workers=workers
            )
            checks = {c.name: (c.passed, c.detail, c.basis) for c in gated.checks}
            results.append((checks, gated.gaps))
    (serial, serial_gaps), (parallel, parallel_gaps) = results
    assert parallel_gaps == serial_gaps
    return serial, parallel


def test_the_gate_reads_the_same_lines_and_verdicts_on_workers(tmp_path: Path) -> None:
    """Known-good and known-bad in one tree: the changed lines the tests run
    are covered, and `unreached`'s, which none runs, are named, on both."""
    root = _project(tmp_path / "p", _pyproject())
    serial, parallel = _gate_both_ways(root)
    assert serial["tests"] == (True, f"{COMMAND!r} exited 0", None)
    assert parallel["tests"] == (True, f"{COMMAND!r} exited 0", "test-workers=2")
    assert parallel["coverage"] == serial["coverage"]
    passed, detail, _basis = parallel["coverage"]
    assert not passed
    assert detail.startswith("no test runs ")
    assert detail.endswith("/src/pkg/lazy.py:14")
    assert "calc.py" not in detail
    for name in ("dead-code", "public-deletions", "assertion-preservation", "red-phase"):
        assert parallel[name] == serial[name], name


@pytest.mark.parametrize(
    "addopts",
    ["--cov=pkg", "--cov=pkg --cov-report=term-missing --cov-fail-under=50"],
    ids=["cov", "cov-report-fail-under"],
)
def test_a_project_whose_options_start_pytest_cov_is_measured_serially_and_on_workers(
    tmp_path: Path, addopts: str
) -> None:
    """Instances for the displaced `coverage run`: the gate names the same
    unrun line (`unreached`'s `return 1`) and clears the same run ones as for
    a project without pytest-cov in its options, serially and on workers."""
    reference, _ = _gate_both_ways(_project(tmp_path / "a", _pyproject()))
    serial, parallel = _gate_both_ways(_project(tmp_path / "b", _pyproject(addopts=addopts)))
    assert serial["coverage"] == parallel["coverage"]
    passed, detail, basis = serial["coverage"]
    assert (passed, basis) == (False, reference["coverage"][2])
    assert detail.startswith("no test runs ")
    assert detail.endswith("/src/pkg/lazy.py:14")
    assert serial["tests"] == (True, f"{COMMAND!r} exited 0", None)


TOTAL: Final = "--cov=pkg --cov-report=term-missing --cov-fail-under=100"
"""A project's own options that start pytest-cov and enforce a total this
fixture's suite does not reach (`early_unused` and the `nonpos` branch never
run), as saddle's own enforce one its suite cannot reach in the sandbox."""


def test_a_projects_coverage_total_never_decides_the_tests_check(tmp_path: Path) -> None:
    """Known-good: a passing suite below its project's own total passes the
    tests check, serially and on workers; the coverage check still names the
    changed line no test runs, and only that one."""
    serial, parallel = _gate_both_ways(_project(tmp_path / "p", _pyproject(addopts=TOTAL)))
    assert serial["tests"] == (True, f"{COMMAND!r} exited 0", None)
    assert parallel["tests"] == (True, f"{COMMAND!r} exited 0", "test-workers=2")
    assert serial["coverage"] == parallel["coverage"]
    assert serial["coverage"][1].endswith("/src/pkg/lazy.py:14")


def test_dead_code_is_found_in_a_project_that_enforces_a_total(tmp_path: Path) -> None:
    """Known-bad for the rerun: its run failing on the total read as "the
    suite fails without them", which passed the dead helper."""
    root = _project(tmp_path / "p", _pyproject(addopts=TOTAL))
    (root / "src/pkg/calc.py").write_text(CALC_CHANGED + "\n\ndef _spare():\n    return 3\n")
    serial, parallel = _gate_both_ways(root)
    assert serial["dead-code"] == parallel["dead-code"]
    assert not serial["dead-code"][0]
    assert "_spare" in serial["dead-code"][1]


API: Final = "def api(x):\n    return helper(x)\n\n\ndef helper(x):\n    return x + 1\n"
API_TEST: Final = "from api import api\n\n\ndef test_api():\n    assert api(1) == 2\n"


def test_what_the_gate_admits_a_total_that_falls_on_unchanged_lines(tmp_path: Path) -> None:
    """The cost, exhibited: `api` stops calling `helper`. The changed line
    runs and nothing was added or deleted, so the audit accepts; only the
    project's own 100% total fell (`helper` is unchanged and no longer runs),
    which the gate no longer reads. The project's own check still does."""
    root = tmp_path / "p"
    _commit(
        root,
        {
            "pyproject.toml": _pyproject(addopts="--cov=api --cov-fail-under=100"),
            "api.py": API,
            "tests/test_api.py": API_TEST,
        },
    )
    (root / "api.py").write_text(API.replace("return helper(x)", "return x + 1"))
    serial, parallel = _gate_both_ways(root)
    for checks in (serial, parallel):
        assert checks["tests"][:2] == (True, f"{COMMAND!r} exited 0")
        assert checks["coverage"][:2] == (True, "every changed line runs")
        assert checks["dead-code"][0]
        assert checks["public-deletions"][0]
    own = subprocess.run(
        [sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider"],
        cwd=root,
        capture_output=True,
        text=True,
        check=False,
    )
    assert own.returncode == 1
    assert "FAIL Required test coverage of 100% not reached" in own.stdout


def test_a_failing_test_fails_the_tests_check_on_workers(tmp_path: Path) -> None:
    root = _project(tmp_path / "p", _pyproject())
    (root / "src/pkg/calc.py").write_text(CALC.replace("return a - b", "return b - a"))
    serial, parallel = _gate_both_ways(root)
    named = ", ".join(f"tests/test_part{i}.py::test_sub_{i}" for i in (2, 3, 4))
    detail = f"{COMMAND!r} exited 1: 3 failing: {named}"
    assert serial["tests"] == (False, detail, None)
    assert parallel["tests"] == (False, detail, "test-workers=2")


def test_dead_code_is_rerun_on_workers(tmp_path: Path) -> None:
    """The dead-code rerun asks the same question on workers: a private
    helper nothing mentions is found, one the suite needs is kept."""
    root = _project(tmp_path / "p", _pyproject())
    (root / "src/pkg/calc.py").write_text(CALC_CHANGED + "\n\ndef _spare():\n    return 3\n")
    serial, parallel = _gate_both_ways(root)
    assert parallel["dead-code"] == serial["dead-code"]
    assert not parallel["dead-code"][0]
    assert "_spare" in parallel["dead-code"][1]


def test_a_new_test_red_on_the_baseline_is_red_on_workers(tmp_path: Path) -> None:
    """The red-phase sample runs on the baseline copy on workers too: a new
    test of the change fails there and passes on the tree, as serially; a
    new test the baseline already passes proves nothing, as serially."""
    root = _project(tmp_path / "p", _pyproject())
    negative = "    if b < 0:\n        return -mul(a, -b)\n    total = 0\n"
    (root / "src/pkg/calc.py").write_text(CALC.replace("    total = 0\n", negative))
    (root / "tests/test_new.py").write_text(
        "from pkg.calc import mul\n\n\ndef test_mul_negative():\n    assert mul(3, -1) == -3\n"
    )
    serial, parallel = _gate_both_ways(root, tier2=True)
    assert parallel["red-phase"] == serial["red-phase"]
    assert parallel["red-phase"][:2] == (True, "fail pre-change, pass post-change")
    (root / "tests/test_new.py").write_text(
        "from pkg.calc import mul\n\n\ndef test_mul_zero():\n    assert mul(3, 0) == 0\n"
    )
    serial, parallel = _gate_both_ways(root, tier2=True)
    assert parallel["red-phase"] == serial["red-phase"]
    assert parallel["red-phase"][:2] == (False, "tests pass pre-change; prove nothing")


def test_every_suite_run_of_the_gate_uses_the_workers(tmp_path: Path) -> None:
    """The red-phase samples and the dead-code rerun reach the same verdicts
    either way, so only the journal shows how they ran: each run of the
    suite is `-n 2 --dist worksteal`, the suite under pytest-cov, the samples
    and the rerun with no coverage asked for (the probe, run once in the tree
    and once in the baseline copy, collects nothing)."""
    root = _project(tmp_path / "p", _pyproject())
    (root / "src/pkg/calc.py").write_text(CALC_CHANGED + "\n\ndef _spare():\n    return 3\n")
    (root / "tests/test_new.py").write_text(
        "from pkg.calc import mul\n\n\ndef test_mul_zero():\n    assert mul(3, 0) == 0\n"
    )
    journal = tmp_path / "spans.jsonl"
    with staged_copy(root, "HEAD") as (copy, _staged, resolved):
        run_node_gate(
            audit_node(),
            copy,
            baseline=resolved,
            recorder=SpanRecorder(path=journal, node_id="n"),
            test_workers=2,
        )
    runs = [span.argv for span in read_spans(journal) if "pytest" in span.argv]
    probes = [argv for argv in runs if "saddle_workers_probe" in argv]
    suites = [argv for argv in runs if argv not in probes]
    assert len(probes) == 2
    workers = ["-n", "2", "--dist", "worksteal"]
    covered = [*COMMAND.split(), *workers, "--cov-report=", "--cov=.", "--cov-fail-under=0"]
    # the suite, the red-phase samples, the dead-code rerun without `_spare`;
    # a sample runs only the changed test file, every other one ignored
    ignored = sorted(
        f"--ignore={path.relative_to(root).as_posix()}"
        for path in (root / "tests").glob("test_*.py")
        if path.name != "test_new.py"
    )
    # one sample: `test_mul_zero` passes on the original code, which ends the sampling
    samples = [[*COMMAND.split(), *ignored, *workers]]
    # the tree's own suite also records which test ran each line (tier 2)
    current = [*covered, "--cov-context=test"]
    assert suites == [current, *samples, [*COMMAND.split(), *workers]]


TRACED: Final = (
    "import os\n\nimport coverage\n\n\n"
    "def pytest_sessionstart(session):\n"
    "    if coverage.Coverage.current() is not None:\n"
    "        os.write(2, b'TRACED\\n')\n"
)
"""A root conftest that says on stderr, which the run's span keeps, when
coverage is tracing the session: under `coverage run` and pytest-cov, in the
controller and every worker. At session start pytest is not capturing, as it
is while it imports the conftest."""


@pytest.mark.parametrize(
    ("addopts", "workers", "sample_traced"),
    [(None, 2, False), ("--cov=pkg", 1, False), ("--cov=pkg", 2, False), (None, 1, True)],
    ids=["parallel", "serial-project-cov", "parallel-project-cov", "serial-coverage-run"],
)
def test_a_red_phase_sample_records_no_coverage_where_pytest_cov_would(
    tmp_path: Path, addopts: str | None, workers: int, sample_traced: bool
) -> None:
    """Red-phase reads a sample's exit and output, never its coverage. Where
    pytest-cov would record a sample (on workers, or under the project's own
    `--cov`), no tracer runs in it; on saddle's own test files tracing was
    about a third of each sample's wall. The tree's own suite is still
    traced, and the verdict is the same. A serial sample stays under
    `coverage run` (`test_a_serial_red_phase_sample_keeps_the_tree_root_importable`
    in test_runner.py says why)."""
    root = tmp_path / "p"
    _commit(root, {**_files(_pyproject(addopts=addopts)), "conftest.py": TRACED}, "baseline")
    negative = "    if b < 0:\n        return -mul(a, -b)\n    total = 0\n"
    (root / "src/pkg/calc.py").write_text(CALC.replace("    total = 0\n", negative))
    (root / "tests/test_new.py").write_text(
        "from pkg.calc import mul\n\n\ndef test_mul_negative():\n    assert mul(3, -1) == -3\n"
    )
    journal = tmp_path / "spans.jsonl"
    with staged_copy(root, "HEAD") as (copy, _staged, resolved):
        gated = run_node_gate(
            audit_node(),
            copy,
            baseline=resolved,
            recorder=SpanRecorder(path=journal, node_id="n"),
            test_workers=workers,
        )
    red = next(check for check in gated.checks if check.name == "red-phase")
    assert (red.passed, red.detail) == (True, "fail pre-change, pass post-change")
    runs = [
        span
        for span in read_spans(journal)
        if "pytest" in span.argv and "saddle_workers_probe" not in span.argv
    ]
    # a sample runs only the new test file, every other one ignored
    samples = [span for span in runs if any(a.startswith("--ignore=") for a in span.argv)]
    assert len(samples) == RED_PHASE_SAMPLES
    assert "TRACED" in runs[0].detail  # the tree's own suite
    assert ["TRACED" in span.detail for span in samples] == [sample_traced] * RED_PHASE_SAMPLES


def test_what_an_untraced_sample_admits_a_new_test_that_asserts_the_tracer(
    tmp_path: Path,
) -> None:
    """The cost, exhibited: a new test asserting only that coverage traces it
    fails on the untraced baseline sample and passes in the traced suite, so
    red-phase reads it as red. Traced, as before, the sample passed it and
    red-phase refused it ("tests pass pre-change")."""
    root = _project(tmp_path / "p", _pyproject())
    (root / "tests/test_new.py").write_text(
        "import coverage\n\n\n"
        "def test_traced():\n    assert coverage.Coverage.current() is not None\n"
    )
    with staged_copy(root, "HEAD") as (copy, _staged, resolved):
        gated = run_node_gate(audit_node(), copy, baseline=resolved, test_workers=2)
    red = next(check for check in gated.checks if check.name == "red-phase")
    assert (red.passed, red.detail) == (True, "fail pre-change, pass post-change")


def _spec_node() -> Node:
    """A `test` node: its tests are a red specification, read off the run's output."""
    node = audit_node().model_dump()
    node.update(id="spec", kind="test")
    return Node.model_validate(node)


def test_a_red_specification_is_read_off_a_serial_run(tmp_path: Path) -> None:
    """A greenfield spec's collection error is exit 2 serially and exit 1
    under xdist; the verdict reads the exit, so a `test` node never runs on
    workers: with them asked for, it gets the serial verdict."""
    root = tmp_path / "p"
    _commit(root, {"pyproject.toml": _pyproject(), "src/pkg/__init__.py": ""})
    (root / "tests").mkdir()
    (root / "tests/test_spec.py").write_text(
        "from newmod import thing\n\n\ndef test_thing():\n    assert thing() == 1\n"
    )
    with staged_copy(root, "HEAD") as (copy, _staged, resolved):
        verdicts = [
            next(
                (c.passed, c.detail, c.basis)
                for c in run_node_gate(
                    _spec_node(), copy, baseline=resolved, tier2=False, test_workers=workers
                ).checks
                if c.name == "tests"
            )
            for workers in (1, 2)
        ]
    assert verdicts == [(True, "red specification: module 'newmod' does not exist yet", None)] * 2


# -- the audit: the setting at the baseline, the note in the finding --------------


def _tier1(root: Path) -> Findings:
    return Auditor(root).tier1()


def test_without_the_setting_the_tests_finding_is_unchanged(tmp_path: Path) -> None:
    """Byte for byte what it was: no note, no basis, no probe."""
    tests = _finding(_tier1(_project(tmp_path / "p", _pyproject())), "tests")
    assert (tests.verdict, tests.detail) == ("pass", f"{COMMAND!r} exited 0")
    assert tests.cites == ("saddle.gates.check_test_command",)


def test_a_serial_audit_of_a_project_starting_pytest_cov_measures(tmp_path: Path) -> None:
    """Without the setting too: the same unrun line is named, not passed as
    unreached and not reported for every line."""
    result = _tier1(_project(tmp_path / "p", _pyproject(addopts="--cov=pkg")))
    tests = _finding(result, "tests")
    assert (tests.verdict, tests.detail) == ("pass", f"{COMMAND!r} exited 0")
    assert tests.cites == ("saddle.gates.check_test_command",)
    covered = _finding(result, "coverage")
    assert (covered.verdict, covered.detail) == ("fail", "no test runs src/pkg/lazy.py:14")


def test_the_audit_runs_on_the_workers_its_baseline_asks_for(tmp_path: Path) -> None:
    result = _tier1(_project(tmp_path / "p", _pyproject(_workers(2))))
    tests = _finding(result, "tests")
    assert (tests.verdict, tests.detail) == ("pass", f"{COMMAND!r} exited 0")
    assert tests.cites == ("saddle.gates.check_test_command", "test-workers=2")
    covered = _finding(result, "coverage")
    assert (covered.verdict, covered.detail) == ("fail", "no test runs src/pkg/lazy.py:14")


@pytest.mark.parametrize(
    ("baseline", "tree", "basis"),
    [
        (None, _workers(2), None),
        (_workers(2), None, "test-workers=2"),
        (_workers(2), _workers(1), "test-workers=2"),
    ],
    ids=["tree-asks", "tree-drops", "tree-lowers"],
)
def test_the_tree_audited_cannot_choose_its_own_run(
    tmp_path: Path, baseline: str | None, tree: str | None, basis: str | None
) -> None:
    """Known-bad at the source: only the baseline's setting counts."""
    root = _project(tmp_path / "p", _pyproject(baseline or ""))
    (root / "pyproject.toml").write_text(_pyproject(tree or ""))
    tests = _finding(_tier1(root), "tests")
    assert tests.verdict == "pass"
    assert tests.cites == ("saddle.gates.check_test_command", *filter(None, [basis]))


@pytest.mark.parametrize(
    ("missing", "why"),
    [(NO_XDIST, "pytest-xdist is not installed"), (NO_COV, "pytest-cov is not installed")],
    ids=["no-xdist", "no-cov"],
)
def test_a_missing_plugin_runs_the_audit_serially_and_says_so(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, missing: tuple[str, ...], why: str
) -> None:
    """The verdicts are the serial run's; the finding carries the reason."""
    root = _project(tmp_path / "p", _pyproject(_workers(2)))
    parallel = _tier1(root)
    _env_without(tmp_path, monkeypatch, *missing)
    serial = _tier1(root)
    tests = _finding(serial, "tests")
    assert (tests.verdict, tests.detail) == (
        "pass",
        f"{COMMAND!r} exited 0; test-workers = 2 set but {why}: {RAN_SERIALLY}",
    )
    assert tests.cites == ("saddle.gates.check_test_command",)
    for gate in ("coverage", "dead-code", "public-deletions"):
        assert _finding(serial, gate) == _finding(parallel, gate), gate


def test_a_worker_count_the_project_cannot_use_stops_the_audit(tmp_path: Path) -> None:
    root = _project(tmp_path / "p", _pyproject('test-workers = "8"\n'))
    with pytest.raises(AuditError, match="test-workers = '8' is not a whole number of workers"):
        _tier1(root)
    with pytest.raises(AuditError, match="test-workers = '8' is not a whole number of workers"):
        audit_tree(root)


def test_the_flat_audit_runs_on_the_workers_its_baseline_asks_for(tmp_path: Path) -> None:
    """`saddle audit`'s one-shot battery takes the same setting."""
    root = _project(tmp_path / "p", _pyproject(_workers(2)))
    checks = {c.name: c for c in audit_tree(root).checks}
    assert (checks["tests"].status, checks["tests"].basis) == ("pass", "test-workers=2")
    assert (checks["coverage"].status, checks["coverage"].detail) == (
        "fail",
        "no test runs src/pkg/lazy.py:14",
    )


NEW_TESTS: Final = (
    "\n\ndef test_new():\n    assert add(2, 2) == 4\n"
    "\n\ndef test_never():\n    if False:\n        unused = 1\n"
)


@pytest.mark.parametrize("workers", [1, 2])
def test_changed_test_lines_are_measured_whatever_the_projects_coverage_source(
    tmp_path: Path, workers: int
) -> None:
    """Red before: with the project's coverage `source` set to its package (as
    saddle's is) a pytest-cov run left test files unmeasured, and the coverage
    check refused every changed test line. Known-bad half: a test line that
    never runs is still named."""
    source = '\n[tool.coverage.run]\nsource = ["pkg"]\nbranch = true\n'
    table = _workers(workers) if workers > 1 else ""
    root = _project(tmp_path / "p", _pyproject(table, addopts="--cov=pkg") + source)
    part = root / "tests" / "test_part1.py"
    text = part.read_text() + NEW_TESTS
    part.write_text(text)
    lines = text.splitlines()
    ran = [lines.index("def test_new():") + 1, lines.index("    assert add(2, 2) == 4") + 1]
    never = lines.index("        unused = 1") + 1
    covered = _finding(_tier1(root), "coverage")
    assert covered.verdict == "fail"
    assert f"tests/test_part1.py:{never}" in covered.detail
    for line in ran:
        assert f"tests/test_part1.py:{line}" not in covered.detail, covered.detail


ADD_TESTS: Final = tuple(sorted(f"tests/test_part{i}.py::test_add_{i}" for i in range(1, 5)))
"""The tests that run `add`, whose body CALC_CHANGED edits (calc.py lines 2-4)."""


def test_covering_tests_names_only_the_tests_that_ran_a_changed_line(tmp_path: Path) -> None:
    """Instances from a real pytest-cov run with per-test contexts: the add
    tests ran the changed lines; the sub and lazy tests did not; a line no
    test ran names nobody, and a run recorded without contexts names nobody."""
    root = _project(tmp_path / "p", _pyproject())
    run = suite_run(root, COMMAND, 2)
    assert run.by_pytest_cov
    calc = str(root / "src/pkg/calc.py")
    changed = {(calc, 2), (calc, 3), (calc, 4)}
    data = str(tmp_path / "contexts.data")
    assert run_suite_capture(run, COMMAND, root, data, timeout=300, contexts=True).exit_code == 0
    assert covering_tests(data, changed) == ADD_TESTS
    assert covering_tests(data, {(calc, 99)}) == ()
    plain = str(tmp_path / "plain.data")
    assert run_suite_capture(run, COMMAND, root, plain, timeout=300).exit_code == 0
    assert covering_tests(plain, changed) == ()


def test_the_gates_mutation_run_is_handed_the_tests_that_ran_a_changed_line(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Red before: mutmut got the whole scope, so its baseline and stats
    passes ran every test on one core, and on saddle's own repository that
    outlasted the mutation budget before a mutant was tried."""
    root = _project(tmp_path / "p", _pyproject())
    handed: list[object] = []
    real = evidence.mutation_sample

    def spy(*args: Any, **kwargs: Any) -> evidence.MutationOutcome:
        handed.append((kwargs.get("run_tests"), kwargs.get("only_covered")))
        return real(*args, **kwargs)

    monkeypatch.setattr(runner_module, "mutation_sample", spy)
    with staged_copy(root, "HEAD") as (copy, _staged, resolved):
        run_node_gate(audit_node(), copy, baseline=resolved, test_workers=2)
    # and test_lazy: it imports lazy.py inside its body, which runs the added
    # `def unreached` line; the sub tests ran no changed line
    # and only the lines they run are mutated
    assert handed[0] == (tuple(sorted([*ADD_TESTS, "tests/test_lazy.py::test_lazy"])), True)


def test_an_unreadable_data_file_names_no_covering_test(tmp_path: Path) -> None:
    """A data file coverage cannot read hands mutmut nothing narrower: the
    gate then keeps its whole scope, the run as it was before contexts."""
    broken = tmp_path / "broken.data"
    broken.write_bytes(b"not a coverage database")
    assert covering_tests(str(broken), {(str(tmp_path / "a.py"), 1)}) == ()
