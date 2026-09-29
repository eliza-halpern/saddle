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
from typing import Final

import coverage
import pytest

from saddle import evidence
from saddle.audit import AuditError, audit_node, audit_tree, staged_copy
from saddle.auditor import Auditor, Finding, Findings
from saddle.evidence import (
    RAN_SERIALLY,
    SUITE_WORKERS_MAX,
    SuiteLimitError,
    SuiteRun,
    SuiteWorkers,
    covered_lines,
    run_argv,
    run_suite_capture,
    suite_limit,
    suite_run,
    suite_workers,
)
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
    """A project that set nothing pays for no probe."""
    root = _project(tmp_path / "p", _pyproject())
    journal = tmp_path / "spans.jsonl"
    recorder = SpanRecorder(path=journal, node_id="n")
    assert suite_run(root, COMMAND, 1, recorder=recorder) == SuiteRun()
    assert not journal.exists()
    assert suite_run(root, COMMAND, 3, recorder=recorder) == SuiteRun(workers=3)
    assert len(journal.read_text().splitlines()) == 1


def test_with_both_plugins_the_suite_runs_on_workers(tmp_path: Path) -> None:
    root = _project(tmp_path / "p", _pyproject())
    assert suite_run(root, COMMAND, 3) == SuiteRun(workers=3, project_cov=False)


def test_a_project_that_starts_pytest_cov_keeps_its_own_options(tmp_path: Path) -> None:
    root = _project(tmp_path / "p", _pyproject(addopts="--cov=pkg --cov-report=term-missing"))
    assert suite_run(root, COMMAND, 2) == SuiteRun(workers=2, project_cov=True)


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
    root = _project(tmp_path / "p", _pyproject())
    assert suite_run(root, command, 2) == SuiteRun(
        note=f"test-workers = 2 set but the test command does not start pytest: {RAN_SERIALLY}"
    )


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
    parallel = _record(root, SuiteRun(workers=2), ".coverage.parallel")
    assert parallel == serial
    lazy = str(root / "src/pkg/lazy.py")
    # `go`'s body runs only in a worker; `early_used` at conftest import.
    assert {(lazy, 8), (lazy, 9), (str(root / "src/pkg/early.py"), 7)} <= parallel
    assert (lazy, 10) not in parallel  # `return "nonpos"`: no test takes it


def test_a_project_starting_pytest_cov_records_what_its_serial_pytest_cov_records(
    tmp_path: Path,
) -> None:
    """With the project's own `--cov=pkg`, the gate's serial `coverage run`
    records nothing (pytest-cov displaces it), so the reference here is the
    project's own serial pytest-cov run: the parallel run records the same."""
    root = _project(tmp_path / "p", _pyproject(addopts="--cov=pkg"))
    serial = _record(root, SuiteRun(workers=1), ".coverage.gate")
    assert serial == set()
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
    parallel = _record(root, SuiteRun(workers=2, project_cov=True), ".coverage.parallel")
    assert parallel == expected


def test_a_coverage_file_left_in_the_tree_adds_no_line(tmp_path: Path) -> None:
    """Known-bad: a data file of the gate's name already in the tree (a
    parallel-run suffix file pytest-cov would combine) cannot mark a line
    no test runs as covered."""
    root = _project(tmp_path / "p", _pyproject())
    lazy = str(root / "src/pkg/lazy.py")
    for name in (".coverage.parallel", ".coverage.parallel.left.1.2"):
        planted = coverage.CoverageData(basename=str(root / name))
        planted.add_lines({lazy: [10]})
        planted.write()
    parallel = _record(root, SuiteRun(workers=2), ".coverage.parallel")
    assert (lazy, 9) in parallel
    assert (lazy, 10) not in parallel


# -- the gate: one tree, two runs, the same verdicts ----------------------------


def _gate_both_ways(root: Path) -> tuple[dict[str, tuple[bool, str, str | None]], ...]:
    """`run_node_gate` over one staged copy of `root`, serially and on two
    workers: each result's checks by name, then its uncovered lines."""
    results = []
    with staged_copy(root, "HEAD") as (copy, _staged, resolved):
        for workers in (1, 2):
            gated = run_node_gate(
                audit_node(), copy, baseline=resolved, tier2=False, test_workers=workers
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


def test_a_failing_test_fails_the_tests_check_on_workers(tmp_path: Path) -> None:
    root = _project(tmp_path / "p", _pyproject())
    (root / "src/pkg/calc.py").write_text(CALC.replace("return a - b", "return b - a"))
    serial, parallel = _gate_both_ways(root)
    assert serial["tests"] == (False, f"{COMMAND!r} exited 1", None)
    assert parallel["tests"] == (False, f"{COMMAND!r} exited 1", "test-workers=2")


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
    serial, parallel = _gate_both_ways(root)
    assert parallel["red-phase"] == serial["red-phase"]
    assert parallel["red-phase"][:2] == (True, "fail pre-change, pass post-change")
    (root / "tests/test_new.py").write_text(
        "from pkg.calc import mul\n\n\ndef test_mul_zero():\n    assert mul(3, 0) == 0\n"
    )
    serial, parallel = _gate_both_ways(root)
    assert parallel["red-phase"] == serial["red-phase"]
    assert parallel["red-phase"][:2] == (False, "tests pass pre-change; prove nothing")


def test_every_suite_run_of_the_gate_uses_the_workers(tmp_path: Path) -> None:
    """The red-phase sample and the dead-code rerun reach the same verdicts
    either way, so only the journal shows how they ran: each run of the
    suite is `-n 2`, the suite and the sample under pytest-cov (the probe,
    run once in the tree and once in the baseline copy, collects nothing)."""
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
            tier2=False,
            test_workers=2,
        )
    runs = [span.argv for span in read_spans(journal) if "pytest" in span.argv]
    probes = [argv for argv in runs if "saddle_workers_probe" in argv]
    suites = [argv for argv in runs if argv not in probes]
    assert len(probes) == 2
    covered = [*COMMAND.split(), "-n", "2", "--cov", "--cov-report=", "--cov-fail-under=0"]
    # the suite, the one red-phase sample, the dead-code rerun without `_spare`
    assert suites == [covered, covered, [*COMMAND.split(), "-n", "2"]]


# -- the audit: the setting at the baseline, the note in the finding --------------


def _tier1(root: Path) -> Findings:
    return Auditor(root).tier1()


def test_without_the_setting_the_tests_finding_is_unchanged(tmp_path: Path) -> None:
    """Byte for byte what it was: no note, no basis, no probe."""
    tests = _finding(_tier1(_project(tmp_path / "p", _pyproject())), "tests")
    assert (tests.verdict, tests.detail) == ("pass", f"{COMMAND!r} exited 0")
    assert tests.cites == ("saddle.gates.check_test_command",)


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
