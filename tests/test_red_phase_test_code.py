"""Red-phase runs a change's tests with all of its test code, and does not judge a
change to test code alone (#183).

The pre-change tree took the change's `test_*.py` files and nothing else, so a
helper under `tests/`, a `conftest.py` or a file the tests read stayed at the
baseline. 156a-t added tests pinning behaviour 156a had shipped, plus three
entries in `tests/yaml_check_fixtures.py`. Pre-change the new tests failed only
on a `KeyError` for those entries, and red-phase passed ("fail pre-change, pass
post-change"); with the helper carried, all 120 pass there. A test that pins
behaviour the code already has can never fail pre-change, so the same change
without the helper edit was refused ("tests pass pre-change").

Known-bad: a source change whose new test fails pre-change only on a new helper
entry, behind an assertion the old code meets too, is refused. Known-good: a
source change whose new test, reading a new helper entry, fails pre-change on
the old behaviour still passes; a change to test code alone is not applicable,
never a refusal, and runs no baseline sample.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from test_auditor import _init
from test_mutation_tests_only import HELPER, NOTHING, SOURCE, TEST, sampled
from test_runner import _node

from saddle import evidence
from saddle import runner as runner_module
from saddle.auditor import RED_PHASE_ONLY_TESTS, Auditor
from saddle.evidence import run_argv
from saddle.gates import RED_PHASE_TESTS_ONLY
from saddle.runner import carry_test_code, run_node_gate

COMMAND = "python -m pytest tests -q"
BASE = {"n.py": "def f():\n    return 1\n", "tests/helper.py": "VALUES: dict[str, int] = {}\n"}


def reads(key: str) -> str:
    """A new test that checks `f` against a helper entry the baseline lacks."""
    return (
        "from helper import VALUES\n\nfrom n import f\n\n\n"
        f"def test_f():  # REQ-001\n    assert f() == VALUES[{key!r}]\n"
    )


def changed(tmp_path: Path, files: dict[str, str]) -> Path:
    """`BASE` committed, then `files` written and staged, as a node leaves its work."""
    root = tmp_path / "tree"
    (root / "tests").mkdir(parents=True)
    _init(root, BASE)
    for name, text in files.items():
        (root / name).write_text(text)
    assert run_argv(["git", "add", "-A"], root) == 0
    return root


def red_phase(root: Path) -> tuple[bool, str]:
    result = run_node_gate(_node(COMMAND, kind="refactor"), root)
    red = next(check for check in result.checks if check.name == "red-phase")
    return red.passed, red.detail


def test_a_new_test_failing_pre_change_only_on_a_new_helper_entry_is_refused(
    tmp_path: Path,
) -> None:
    root = changed(
        tmp_path,
        {
            "n.py": "def f():\n    return 0 + 1\n",  # changed, and still 1
            "tests/helper.py": 'VALUES: dict[str, int] = {"one": 1}\n',
            "tests/test_n.py": reads("one"),
        },
    )
    assert red_phase(root) == (False, "tests pass pre-change; prove nothing")


def test_a_new_test_reading_a_new_helper_entry_still_fails_on_the_old_behaviour(
    tmp_path: Path,
) -> None:
    root = changed(
        tmp_path,
        {
            "n.py": "def f():\n    return 2\n",
            "tests/helper.py": 'VALUES: dict[str, int] = {"want": 2}\n',
            "tests/test_n.py": reads("want"),
        },
    )
    assert red_phase(root) == (True, "fail pre-change, pass post-change")


def test_a_change_to_test_code_alone_runs_no_baseline_sample(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = changed(
        tmp_path,
        {
            "tests/helper.py": 'VALUES: dict[str, int] = {"one": 1}\n',
            "tests/test_n.py": reads("one"),
        },
    )
    dropped: list[Path] = []
    real = evidence.drop_test_caches

    def watched(tree: Path) -> None:
        dropped.append(Path(tree))
        real(tree)

    monkeypatch.setattr(runner_module, "drop_test_caches", watched)
    assert red_phase(root) == (False, RED_PHASE_TESTS_ONLY)
    assert [tree for tree in dropped if tree != root] == []  # no pre-change sample ran


def test_a_change_to_test_code_alone_is_not_applicable_in_the_audit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "tree"
    (root / "tests").mkdir(parents=True)
    _init(root, {"n.py": SOURCE, "tests/helper.py": HELPER, "tests/test_n.py": TEST})
    (root / "tests" / "helper.py").write_text(HELPER.replace("return 1", "return 0 + 1"))
    (root / "tests" / "test_n.py").write_text(TEST + "    assert make() > 0\n")
    sampled(NOTHING, monkeypatch)
    (red,) = [f for f in Auditor(root).tier2().findings if f.gate == "red-phase"]
    assert (red.verdict, red.detail) == ("not-applicable", RED_PHASE_ONLY_TESTS)


def test_the_pre_change_tree_takes_the_changes_test_code_and_no_source(tmp_path: Path) -> None:
    work, dest = tmp_path / "work", tmp_path / "dest"
    for tree, text in ((work, "after"), (dest, "before")):
        (tree / "tests").mkdir(parents=True)
        (tree / "tests" / "helper.py").write_text(text)
        (tree / "n.py").write_text(text)
    (work / "tests" / "data.json").write_text("{}")
    (dest / "tests" / "gone.py").write_text("before")
    touched = ["tests/helper.py", "tests/data.json", "tests/gone.py", "tests/never.py", "n.py"]
    carry_test_code(work, dest, touched)
    assert (dest / "tests" / "helper.py").read_text() == "after"
    assert (dest / "tests" / "data.json").read_text() == "{}"
    assert not (dest / "tests" / "gone.py").exists()  # the change deleted it
    assert not (dest / "tests" / "never.py").exists()
    assert (dest / "n.py").read_text() == "before"  # source stays at the baseline
