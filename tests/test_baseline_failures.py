"""A failing test that fails on the starting commit too is named as such (#174).

A run could not tell a failure it caused from one the project already had: in
dogfood runs workers spent their time on tests their change never touched. The
audit now reruns, alone and on the untouched baseline tree, the failing tests that
still fail alone, and names those that fail there too.

Known-bad: a test that already failed before the change reads as the change's.
Known-good: a test the change broke (it passes on the baseline) is never named as
failing before; a test the change added has no baseline file and is never asked
about; a baseline file that cannot run names
nothing about its tests; and the verdict is unchanged either way.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, cast

from test_suite_workers import _commit, _pyproject

from saddle import runner as runner_module
from saddle.audit import audit_node, staged_copy
from saddle.evidence import SuiteRun
from saddle.runner import BASELINE_FAILS, RERUN_ALONE_MAX, _failing_at_baseline, run_node_gate

CALC = "def add(a, b):\n    return a + b\n\n\ndef neg(a):\n    return -a\n"
TEST_ADD = "from pkg.calc import add\n\n\ndef test_add():\n    assert add(2, 2) == 4\n"
TEST_OLD = "from pkg.calc import neg\n\n\ndef test_old():\n    assert neg(1) == 1\n"
"""Fails on the baseline already: the project had it before the change."""
TEST_NEW = "from pkg.calc import neg\n\n\ndef test_new():\n    assert neg(2) == 2\n"
"""Added by the change, failing: it has no baseline file."""


def _tests_detail(root: Path, baseline_old: str = TEST_OLD) -> str:
    _commit(
        root,
        {
            "pyproject.toml": _pyproject(),
            "src/pkg/__init__.py": "",
            "src/pkg/calc.py": CALC,
            "tests/test_add.py": TEST_ADD,
            "tests/test_old.py": baseline_old,
        },
        "baseline",
    )
    (root / "src/pkg/calc.py").write_text(CALC.replace("a + b", "a - b"))  # breaks test_add
    (root / "tests/test_old.py").write_text(TEST_OLD)
    (root / "tests/test_new.py").write_text(TEST_NEW)
    with staged_copy(root, "HEAD") as (copy, _staged, resolved):
        gated = run_node_gate(audit_node(), copy, baseline=resolved, test_workers=1)
    tests = next(c for c in gated.checks if c.name == "tests")
    assert not tests.passed  # the verdict stands either way
    return tests.detail


def test_a_test_that_failed_before_the_change_is_named_and_the_changes_own_are_not(
    tmp_path: Path,
) -> None:
    detail = _tests_detail(tmp_path / "repo")
    said = BASELINE_FAILS.format(n=1, names="tests/test_old.py::test_old")
    assert said in detail, detail
    before = detail.split(said)[0]
    assert "tests/test_add.py::test_add" in before  # still named among the failing
    assert "tests/test_new.py::test_new" in before
    assert said.count("test_add") == said.count("test_new") == 0


def test_a_baseline_file_that_cannot_run_names_none_of_its_tests(tmp_path: Path) -> None:
    """The baseline's test_old does not even import (a collection error): the rerun
    there never shows test_old failing, so it is never read as "fails there too"."""
    detail = _tests_detail(tmp_path / "repo", baseline_old="def test_old(:\n")
    assert "fail on the starting commit too" not in detail
    assert "tests/test_old.py::test_old" in detail  # the head failure is still named


def test_a_test_node_or_too_many_failures_asks_the_baseline_nothing(tmp_path: Path) -> None:
    node = audit_node()
    nothing_runs = cast(SuiteRun, object())  # never asked: each case returns before it
    test_node = node.model_copy(update={"kind": "test"})
    common: dict[str, Any] = {"recorder": None, "timeout": None}
    assert _failing_at_baseline(nothing_runs, test_node, tmp_path, ["t.py::a"], **common) == []
    many = [f"t.py::t{i}" for i in range(RERUN_ALONE_MAX + 1)]
    assert _failing_at_baseline(nothing_runs, node, tmp_path, many, **common) == []
    assert _failing_at_baseline(nothing_runs, node, tmp_path, [], **common) == []
    assert runner_module.RERUN_ALONE_MAX == RERUN_ALONE_MAX
