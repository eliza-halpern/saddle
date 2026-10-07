"""Test code is never mutated, and a change to test code alone reads "not proven" (#181).

The mutation scope once told test code from source by pytest's collection
patterns, so a helper under `tests/` that the tests import (a fixtures module)
was mutated as source. On a change to a test and its helper, the helper was the
only module mutated; the tests import it as `helper` and mutmut keys it
`tests.helper`, so `mutmut run` stopped ("use fully-qualified package imports")
and the finding read as a tool failure.

Known-good: a change to a test and a helper under `tests/` mutates nothing, and
its mutation finding is not proven with that reason, never a refusal. Known-bad:
a change that also edits a source function body still fails when mutation
generates nothing on it.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from test_auditor import _init
from test_evidence import _without_stubbed_mutmut

from saddle import runner
from saddle.auditor import MUTATION_TESTS_ONLY, Auditor, data_only_change, source_lines_changed
from saddle.evidence import MutationOutcome, mutation_sample

SOURCE = "def f():\n    return 1\n"
HELPER = "def make():\n    return 1\n"
TEST = "from helper import make\n\nfrom n import f\n\n\ndef test_f():\n    assert f() == make()\n"

NOTHING = MutationOutcome(killed=0, total=0, generated=0, survivors=())


def tree(tmp_path: Path) -> Path:
    root = tmp_path / "tree"
    (root / "tests").mkdir(parents=True)
    _init(root, {"n.py": SOURCE, "tests/helper.py": HELPER, "tests/test_n.py": TEST})
    return root


def sampled(outcome: MutationOutcome, monkeypatch: pytest.MonkeyPatch) -> None:
    def fake(*_args: Any, **_kwargs: Any) -> MutationOutcome:
        return outcome

    monkeypatch.setattr(runner, "mutation_sample", fake)


def mutation(root: Path) -> tuple[str, str]:
    (m,) = [f for f in Auditor(root).tier2().findings if f.gate == "mutation"]
    return m.verdict, m.detail


def test_a_change_to_a_test_and_its_helper_is_not_proven_and_does_not_refuse(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tree(tmp_path)
    (root / "tests" / "helper.py").write_text(HELPER.replace("return 1", "return 0 + 1"))
    (root / "tests" / "test_n.py").write_text(TEST + "    assert make() > 0\n")
    sampled(NOTHING, monkeypatch)
    assert mutation(root) == ("not-proven", MUTATION_TESTS_ONLY)
    assert not source_lines_changed(root, "HEAD")
    assert data_only_change(root, "HEAD") == []


def test_a_source_function_changed_beside_the_tests_still_fails_with_nothing_mutated(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tree(tmp_path)
    (root / "n.py").write_text(SOURCE.replace("return 1", "return 2 - 1"))
    (root / "tests" / "helper.py").write_text(HELPER.replace("return 1", "return 0 + 1"))
    sampled(NOTHING, monkeypatch)
    assert mutation(root) == (
        "fail",
        "no mutants on changed lines: mutation provided no evidence",
    )
    assert source_lines_changed(root, "HEAD")


def test_a_data_only_source_change_beside_a_helper_edit_is_still_data_only(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A helper's function body under `tests/` is test code, not source that is
    not data: the source change beside it is still read as data only."""
    root = tmp_path / "tree"
    (root / "tests").mkdir(parents=True)
    source = "NAMES = ('a',)\n\n\n" + SOURCE
    _init(root, {"n.py": source, "tests/helper.py": HELPER, "tests/test_n.py": TEST})
    (root / "n.py").write_text(source.replace("('a',)", "('a', 'b')"))
    (root / "tests" / "helper.py").write_text(HELPER.replace("return 1", "return 0 + 1"))
    (root / "tests" / "test_n.py").write_text(
        TEST.replace("from n import f", "from n import NAMES, f") + "    assert 'b' in NAMES\n"
    )
    sampled(NOTHING, monkeypatch)
    assert data_only_change(root, "HEAD") == ["n.py"]
    verdict, detail = mutation(root)
    assert verdict == "not-proven"
    assert detail.startswith("not proven: the source this change touches is module-level")


def test_mutation_sample_never_mutates_a_helper_module_under_tests(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Real mutmut. Pytest does not collect `tests/helper.py`, so it is not in
    `test_files`; it is test code all the same, so nothing is mutated and the
    outcome is empty. Known-bad (before #181): the helper was the only source
    path, and `mutmut run` exited 1 ("use fully-qualified package imports")."""
    _without_stubbed_mutmut(monkeypatch)
    workdir = tmp_path / "w"
    (workdir / "tests").mkdir(parents=True)
    (workdir / "tests" / "helper.py").write_text(HELPER)
    (workdir / "tests" / "test_h.py").write_text(
        "from helper import make\n\n\ndef test_make():\n    assert make() == 1\n"
    )
    changed = {
        (str(workdir / "tests" / "helper.py"), 2),
        (str(workdir / "tests" / "test_h.py"), 5),
    }
    outcome = mutation_sample(
        workdir, changed, 5, test_files={"tests/test_h.py"}, select_tests=("tests/test_h.py",)
    )
    assert outcome == NOTHING
