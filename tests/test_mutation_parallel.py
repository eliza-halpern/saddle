"""mutmut's stats pass runs on the project's pytest-xdist workers.

Contract: when `mutation_sample` is handed `workers` of 2 or more, a green
suite and at least `PARALLEL_TESTS_PER_WORKER` selected tests per worker, the
scratch tells mutmut to load a plugin that runs its stats pass -- the pass
that records which tests reach which function and how long each takes -- on
those workers; mutmut's own tables come out as its serial pass makes them (the
same tests per function), and every verdict is the serial run's. Otherwise,
or when mutmut fails with the plugin, mutmut runs as it always did.
"""

from __future__ import annotations

import json
import os
import re
from collections.abc import Sequence
from pathlib import Path
from typing import Any

import pytest
from test_evidence import _show_diff, _stub_mutmut, _without_stubbed_mutmut

import saddle.evidence as evidence_module
from saddle.evidence import (
    _PARALLEL_MODULE,
    PARALLEL_MIN_SERIAL_S,
    PARALLEL_TESTS_PER_WORKER,
    CapturedRun,
    _mutmut_scratch_config,
    _parallel_plugin,
    mutation_sample,
    suite_test_seconds,
)

CALC = (
    "def add(a, b):\n"
    "    return a + b\n"
    "\n"
    "\n"
    "def mul(a, b):\n"
    "    total = 0\n"
    "    for _ in range(b):\n"
    "        total += a\n"
    "    return total\n"
    "\n"
    "\n"
    "def clamp(x, lo, hi):\n"
    "    if x < lo:\n"
    "        return lo\n"
    "    if x > hi:\n"
    "        return hi\n"
    "    return x\n"
)
CALC_TESTS = (
    "import time\n\n"
    "import pytest\n\n"
    "from calc import add, clamp, mul\n\n\n"
    "@pytest.fixture(autouse=True)\n"
    "def _elsewhere(tmp_path, monkeypatch):\n"
    "    monkeypatch.chdir(tmp_path)\n\n\n"
    '@pytest.mark.parametrize("i", range(5))\n'
    "def test_add(i):\n"
    "    time.sleep(0.05)\n"
    "    assert add(i, 1) == i + 1\n\n\n"
    '@pytest.mark.parametrize("i", range(5))\n'
    "def test_mul(i):\n"
    "    assert mul(i, 3) == 3 * i\n\n\n"
    "def test_clamp_low():\n"
    "    assert clamp(-5, 0, 10) == 0\n\n\n"
    "def test_clamp_mid():\n"
    "    assert clamp(5, 0, 10) == 5\n"
)
SELECTED = tuple(
    [f"test_calc.py::test_add[{i}]" for i in range(5)]
    + [f"test_calc.py::test_mul[{i}]" for i in range(5)]
    + ["test_calc.py::test_clamp_low", "test_calc.py::test_clamp_mid"]
)
"""Twelve tests: three to a worker on four workers, four on three, six on two."""


def _calc_workdir(root: Path) -> Path:
    workdir = root / "work"
    workdir.mkdir()
    (workdir / "calc.py").write_text(CALC)
    (workdir / "test_calc.py").write_text(CALC_TESTS)
    return workdir


def _runs(monkeypatch: pytest.MonkeyPatch) -> list[dict[str, Any]]:
    """Every `mutmut run` made: the scratch's config, the plugin file if any,
    and the stats mutmut saved after it."""
    runs: list[dict[str, Any]] = []
    real = evidence_module.run_capture

    def spy(argv: Sequence[str], cwd: Path, **kwargs: Any) -> CapturedRun:
        run = list(argv[2:4]) == ["mutmut", "run"]
        if run:
            plugin = cwd / f"{_PARALLEL_MODULE}.py"
            runs.append(
                {
                    "config": (cwd / "pyproject.toml").read_text(),
                    "plugin": plugin.read_text() if plugin.exists() else None,
                }
            )
        done = real(argv, cwd, **kwargs)
        if run:
            stats = cwd / "mutants" / "mutmut-stats.json"
            runs[-1]["stats"] = json.loads(stats.read_text()) if stats.exists() else None
            runs[-1]["exit"] = done.exit_code
        return done

    monkeypatch.setattr(evidence_module, "run_capture", spy)
    return runs


def _sample(workdir: Path, workers: int, **kwargs: Any) -> Any:
    return mutation_sample(
        workdir,
        {(str(workdir / "calc.py"), line) for line in (2, 6, 7, 8, 9, 13, 14, 15, 16, 17)},
        10,
        test_files={"test_calc.py"},
        select_tests=SELECTED,
        workers=workers,
        test_seconds=2 * PARALLEL_MIN_SERIAL_S / len(SELECTED),
        **kwargs,
    )


def test_plugin_text_carries_the_workers_and_the_phases() -> None:
    text = _parallel_plugin(6)
    assert "WORKERS = 6\n" in text
    assert "PHASES = ('stats',)\n" in text
    compile(text, _PARALLEL_MODULE, "exec")


def test_scratch_config_loads_the_plugin_in_every_run() -> None:
    config = _mutmut_scratch_config(["a.py"], plugins=("p",))
    assert '"-p", "no:cacheprovider", "-p", "p"' in config
    assert '"p"' not in _mutmut_scratch_config(["a.py"])


def test_real_mutmut_stats_on_workers_are_the_serial_stats_and_the_verdicts_the_serial_run_gave(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Known-good, on the enforcing engine: with two workers the plugin is
    loaded, mutmut's stats pass is the plugin's, and what it saved -- the
    tests that reach each function, a duration for every selected test -- is
    what its own serial pass saved, so every mutant gets the same verdict. Every
    test here changes directory first, as saddle's own do, so the workers must
    have read mutmut's config before the first test."""
    _without_stubbed_mutmut(monkeypatch)
    runs = _runs(monkeypatch)
    workdir = _calc_workdir(tmp_path)
    serial = _sample(workdir, 1)
    parallel = _sample(workdir, 2)
    assert serial.total >= 8
    assert {line for _, line in serial.survivor_lines}, "the tests leave `clamp` mutants alive"
    assert (parallel.killed, parallel.total, parallel.survivor_lines, parallel.statuses) == (
        serial.killed,
        serial.total,
        serial.survivor_lines,
        serial.statuses,
    )
    assert len(runs) == 2, "the plugin's run was not repeated without it"
    one, two = runs
    assert (one["exit"], two["exit"]) == (0, 0)
    assert one["plugin"] is None
    assert _PARALLEL_MODULE not in one["config"]
    assert two["plugin"] is not None
    assert f'"-p", "{_PARALLEL_MODULE}"' in two["config"]
    assert f'"{_PARALLEL_MODULE}.py"' in two["config"]
    serial_stats, parallel_stats = one["stats"], two["stats"]
    assert parallel_stats["tests_by_mangled_function_name"].keys() == (
        serial_stats["tests_by_mangled_function_name"].keys()
    )
    for name, tests in serial_stats["tests_by_mangled_function_name"].items():
        assert sorted(parallel_stats["tests_by_mangled_function_name"][name]) == sorted(tests), name
    assert parallel_stats["duration_by_test"].keys() == serial_stats["duration_by_test"].keys()
    assert set(parallel_stats["duration_by_test"]) == set(SELECTED)
    slept = [t for t in SELECTED if "test_add" in t]
    assert all(parallel_stats["duration_by_test"][t] >= 0.04 for t in slept)


QUICK = PARALLEL_MIN_SERIAL_S / 100
"""A test's core-seconds for which 100 tests are estimated just short of `PARALLEL_MIN_SERIAL_S`."""
SLOW = 1.0


@pytest.mark.parametrize(
    ("workers", "suite_passed", "tests", "seconds"),
    [
        (1, True, 100, SLOW),
        (0, True, 100, SLOW),
        (2, False, 100, SLOW),
        (4, True, 4 * PARALLEL_TESTS_PER_WORKER - 1, 100.0),
        (4, True, 100, None),
        (4, True, 99, QUICK),
    ],
    ids=[
        "serial-project",
        "no-workers",
        "red-suite",
        "too-few-tests-per-worker",
        "no-estimate",
        "quick-tests",
    ],
)
def test_the_plugin_is_loaded_only_for_workers_a_green_suite_enough_tests_and_slow_ones(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    workers: int,
    suite_passed: bool,
    tests: int,
    seconds: float | None,
) -> None:
    """Known-bad cases, each alone: one worker (or none), a suite the tests gate
    found red, fewer than `PARALLEL_TESTS_PER_WORKER` tests per worker, no
    estimate of what a test costs, and tests so quick that starting workers
    would cost more than the serial pass (a few seconds against under
    `PARALLEL_MIN_SERIAL_S`) keep mutmut's own serial pass. The same tree with
    enough of each loads the plugin (next test)."""
    workdir, runs = _stubbed(tmp_path, monkeypatch)
    _stub_sample(workdir, workers, suite_passed, tests, seconds)
    (only,) = runs
    assert only["plugin"] is None
    assert _PARALLEL_MODULE not in only["config"]


def test_the_plugin_is_loaded_when_workers_tests_and_cost_are_enough(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    workdir, runs = _stubbed(tmp_path, monkeypatch)
    _stub_sample(workdir, 4, True, 100, PARALLEL_MIN_SERIAL_S / 100)
    (only,) = runs
    assert re.search(r"WORKERS = 4\n", only["plugin"])
    assert _PARALLEL_MODULE in only["config"]


@pytest.mark.parametrize(
    ("output", "workers", "expected"),
    [
        ("746 passed, 1 skipped, 16 warnings in 67.61s (0:01:07)", 8, 67.61 * 8 / 746),
        ("3 passed in 0.50s", 1, 0.5 / 3),
        ("1 failed, 4 passed in 2.00s", 2, 2.0 * 2 / 4),
        ("collected 5 items\n5 passed in 12s\n", 4, 12 * 4 / 5),
        ("===== 1 passed in 1.0s =====\n===== 9 passed in 3.0s =====", 1, 3.0 / 9),
        ("no tests ran in 0.01s", 4, None),
        ("0 passed in 0.01s", 4, None),
        ("", 4, None),
    ],
    ids=[
        "workers",
        "serial",
        "some-failed",
        "whole-seconds",
        "last-line-wins",
        "none-ran",
        "zero",
        "empty",
    ],
)
def test_suite_test_seconds_reads_a_tests_core_seconds_off_pytests_last_line(
    output: str, workers: int, expected: float | None
) -> None:
    got = suite_test_seconds(output, workers)
    assert got == pytest.approx(expected) if expected is not None else got is None


def _stubbed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, body: str = "exit 0"
) -> tuple[Path, list[dict[str, Any]]]:
    """A tree of one function, a stub `mutmut` whose `run` is `body`, and the
    record of every run made."""
    workdir = tmp_path / "work"
    workdir.mkdir()
    (workdir / "n.py").write_text("def f(a):\n    x = a + 1\n    return x\n")
    stub = tmp_path / "stub"
    stub.mkdir()
    _stub_mutmut(
        stub,
        "  n.x_f__mutmut_1: killed\n",
        {"n.x_f__mutmut_1": _show_diff("n.py", "    x = a + 1", "    x = a - 1")},
        run_body=body,
    )
    monkeypatch.setenv("PATH", f"{stub}{os.pathsep}{os.environ['PATH']}")
    return workdir, _runs(monkeypatch)


def _stub_sample(
    workdir: Path, workers: int, suite_passed: bool, tests: int, seconds: float | None
) -> Any:
    return mutation_sample(
        workdir,
        {(str(workdir / "n.py"), 2)},
        10,
        test_files=set(),
        select_tests=tuple(f"test_n.py::test_{i}" for i in range(tests)),
        workers=workers,
        suite_passed=suite_passed,
        test_seconds=seconds,
    )


def test_a_failed_run_with_the_plugin_gives_way_to_mutmuts_own_serial_pass(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Known-bad for the plugin: mutmut fails wherever it is loaded (the stub
    exits 1 when it finds the plugin in the config). The run is made again
    without it, and that run's verdict stands."""
    body = f"if grep -q {_PARALLEL_MODULE} pyproject.toml; then echo broken >&2; exit 1; fi; exit 0"
    workdir, runs = _stubbed(tmp_path, monkeypatch, body)
    outcome = _stub_sample(workdir, 2, True, 100, SLOW)
    assert (outcome.killed, outcome.total, outcome.survivors) == (1, 1, ())
    first, second = runs
    assert first["plugin"] is not None
    assert second["plugin"] is None
    assert _PARALLEL_MODULE not in second["config"]
    assert first["exit"] == 1
    assert second["exit"] == 0
