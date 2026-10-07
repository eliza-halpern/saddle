"""A sanctioned test rewrite counts only if it is red on the baseline.

`auditor.sanction` reclasses an assertion-preservation failure by test name
alone (a constructed probe rewrote all four T5 tests to `assert True`
and was accepted). The contract now: the auditor runs the
rewritten tests against the baseline's sources, and a rewrite that passes
there did not assert the redefined behaviour, so it is not sanctioned.

Known-good: a rewrite asserting the new value (red on the baseline), as
EAF-t5-s2's four rewrites do (`apply_fee(10.0) == Decimal("9.70")`).
Known-bad: `assert True`, and a rewrite loose enough to pass on both trees.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from saddle.auditor import (
    GREEN_ON_BASELINE,
    REWRITE_QUESTION,
    Auditor,
    AuditorConfig,
    Finding,
    sanction,
)
from saddle.evidence import run_argv

BASE = "def fee(x):\n    return x - 1\n"
NEW = "def fee(x):\n    return x - 2\n"
OLD_TESTS = (
    "from fees import fee\n\n\n"
    "def test_fee_basic():\n    assert fee(10) == 9\n\n\n"
    "def test_fee_other():\n    assert fee(20) == 19\n"
)


def _git(root: Path, *argv: str) -> None:
    assert run_argv(["git", *argv], root) == 0


def _tree(tmp_path: Path, tests: str, source: str = NEW) -> Path:
    root = tmp_path / "tree"
    root.mkdir()
    _git(root, "init")
    _git(root, "config", "user.email", "t@t")
    _git(root, "config", "user.name", "t")
    (root / "fees.py").write_text(BASE)
    (root / "test_fees.py").write_text(OLD_TESTS)
    _git(root, "add", "-A")
    _git(root, "commit", "-m", "base")
    (root / "fees.py").write_text(source)
    (root / "test_fees.py").write_text(tests)
    return root


SANCTIONED = AuditorConfig(sanctioned_test_rewrites=("test_fee_basic", "test_fee_other"))


def _preservation(root: Path) -> Finding:
    found = Auditor(root, config=SANCTIONED).tier1()
    return next(f for f in found.findings if f.gate == "assertion-preservation")


def test_a_rewrite_that_asserts_the_new_behaviour_is_sanctioned(tmp_path: Path) -> None:
    """Known-good: both rewrites fail on the baseline (9 != 8, 19 != 18)."""
    tests = OLD_TESTS.replace("== 9", "== 8").replace("== 19", "== 18")
    finding = _preservation(_tree(tmp_path, tests))
    assert finding.reason == "sanctioned", finding.detail
    assert GREEN_ON_BASELINE not in finding.detail


def test_a_vacuous_rewrite_is_not_sanctioned(tmp_path: Path) -> None:
    """Known-bad (the vacuous-rewrite probe): `assert True` passes anywhere."""
    tests = OLD_TESTS.replace("assert fee(10) == 9", "assert True").replace("== 19", "== 18")
    finding = _preservation(_tree(tmp_path, tests))
    assert finding.verdict == "fail"
    assert finding.reason == "evidence-thin"
    assert finding.detail.endswith(f"{GREEN_ON_BASELINE}test_fee_basic")
    # The feed re-applies `sanction`; the marker keeps it refused there too.
    assert sanction(finding, SANCTIONED.sanctioned_test_rewrites) == finding


def test_a_rewrite_that_passes_on_both_trees_is_not_sanctioned(tmp_path: Path) -> None:
    """Known-bad: a loosened assertion (the old value still satisfies it)."""
    tests = OLD_TESTS.replace("== 9", ">= 8").replace("== 19", ">= 18")
    finding = _preservation(_tree(tmp_path, tests))
    assert finding.reason == "evidence-thin"
    assert finding.detail.endswith(f"{GREEN_ON_BASELINE}test_fee_basic, test_fee_other")


def test_a_rewrite_that_cannot_even_collect_on_the_baseline_is_red(tmp_path: Path) -> None:
    """A rewritten test importing a module the change added errors on the
    baseline: red, so sanctioned (the added source is removed there)."""
    tests = (
        OLD_TESTS.replace("from fees import fee", "from fees import fee\nfrom rates import RATE")
        .replace("== 9", "== 10 - RATE")
        .replace("== 19", "== 20 - RATE")
    )
    root = _tree(tmp_path, tests)
    (root / "rates.py").write_text("RATE = 2\n")
    finding = _preservation(root)
    assert finding.reason == "sanctioned", finding.detail


@pytest.mark.parametrize("path", ["tests/helpers.py", "test_x.py", "pkg/x_test.py", "conftest.py"])
def test_the_test_side_is_kept_and_everything_else_goes_back(path: str) -> None:
    from saddle.gates import is_test_code

    assert is_test_code(path)
    assert not is_test_code("src/pkg/fees.py")


def _asked(root: Path) -> Finding:
    """The assertion-preservation finding when no rewrite was sanctioned."""
    found = Auditor(root, config=AuditorConfig()).tier1()
    return next(f for f in found.findings if f.gate == "assertion-preservation")


def test_an_unapproved_rewrite_that_asserts_the_new_behaviour_is_put_to_a_person(
    tmp_path: Path,
) -> None:
    """Loosened, fail to question (the maintainer's call): with nothing
    sanctioned at the start, a rewrite red on the baseline is asked about,
    so the run ends needing a person instead of refusing work the task may
    require. Red before: it failed, and the model could not comply."""
    tests = OLD_TESTS.replace("== 9", "== 8").replace("== 19", "== 18")
    finding = _asked(_tree(tmp_path, tests))
    assert finding.verdict == "question", finding.detail
    assert finding.detail.endswith(REWRITE_QUESTION)


def test_an_unapproved_vacuous_rewrite_is_still_refused(tmp_path: Path) -> None:
    """Known-bad, unchanged: a rewrite that passes on the original code
    asserts nothing new, so it is refused, never asked about."""
    tests = OLD_TESTS.replace("assert fee(10) == 9", "assert True").replace("== 19", "== 18")
    finding = _asked(_tree(tmp_path, tests))
    assert finding.verdict == "fail"
    assert finding.detail.endswith(f"{GREEN_ON_BASELINE}test_fee_basic")
