"""A person's audit of a project that has no tests: not proven, never a refusal.

pytest exits 5 when it collects nothing, and the `tests` check read that as a
failing suite: every change to a project without tests was refused as
code-wrong, for a reason that is not true, and no edit to the change could
clear it (#129).

The contract: when the audited project has no tests at the baseline and the
head collects none, an audit of a person's commits (`AuditorConfig(no_tests=
"not-proven")`, which `saddle audit --tiered` sets) reports every check that
needs a test as not proven, evidence-thin, saying nothing was run. A Task
run's audit (`no_tests="refuse"`, the default the feed keeps) still refuses:
an agent can write the tests.

Known-good: a correct change in a project with no tests is accepted with the
not-proven rows. Known-bad: a change that deletes every test the baseline had
is still refused, and so is the same no-tests project audited for a Task run.
"""

from __future__ import annotations

from pathlib import Path

from saddle.auditor import NO_TESTS_DETAIL, Auditor, AuditorConfig, Findings
from saddle.cli import _tiered_audit
from saddle.evidence import run_argv
from saddle.gates import NO_TESTS_COLLECTED, PYTEST_NO_TESTS, check_test_command

PERSON = AuditorConfig(no_tests="not-proven")
NEEDS_A_TEST = ("tests", "coverage")
NEEDS_A_TEST_TIER2 = ("full-suite", "mutation", "red-phase")


def _git(root: Path, *argv: str) -> None:
    assert run_argv(["git", *argv], root) == 0


def _repo(tmp_path: Path, *, base_tests: bool, head_tests: bool) -> Path:
    root = tmp_path / "tree"
    root.mkdir()
    _git(root, "init")
    _git(root, "config", "user.email", "t@t")
    _git(root, "config", "user.name", "t")
    (root / "app.py").write_text("def run():\n    return 1\n")
    if base_tests:
        (root / "test_app.py").write_text(
            "from app import run\n\n\ndef test_run():\n    assert run() in (1, 2)\n"
        )
    _git(root, "add", "-A")
    _git(root, "commit", "-m", "base")
    (root / "app.py").write_text("def run():\n    return 2\n")
    if base_tests and not head_tests:
        (root / "test_app.py").unlink()
    return root


def _rows(found: Findings) -> dict[str, tuple[str, str, str]]:
    return {f.gate: (f.verdict, f.reason, f.detail) for f in found.findings}


def test_pytest_collecting_nothing_is_named_in_the_tests_detail() -> None:
    check = check_test_command("python -m pytest -q", lambda _: PYTEST_NO_TESTS)
    assert not check.passed
    assert NO_TESTS_COLLECTED in check.detail
    other = check_test_command("python -m pytest -q", lambda _: 1)
    assert NO_TESTS_COLLECTED not in other.detail


def test_a_persons_change_to_a_project_with_no_tests_is_not_proven(tmp_path: Path) -> None:
    root = _repo(tmp_path, base_tests=False, head_tests=False)
    auditor = Auditor(root, config=PERSON)
    first = auditor.tier1()
    rows = _rows(first)
    for gate in NEEDS_A_TEST:
        assert rows[gate] == ("not-proven", "evidence-thin", NO_TESTS_DETAIL), gate
    assert first.passed
    second = _rows(auditor.tier2())
    for gate in NEEDS_A_TEST_TIER2:
        assert second[gate] == ("not-proven", "evidence-thin", NO_TESTS_DETAIL), gate


def test_a_change_that_deletes_every_test_is_still_refused(tmp_path: Path) -> None:
    root = _repo(tmp_path, base_tests=True, head_tests=False)
    found = Auditor(root, config=PERSON).tier1()
    verdict, reason, detail = _rows(found)["tests"]
    assert (verdict, reason) == ("fail", "code-wrong")
    assert NO_TESTS_COLLECTED in detail
    assert not found.passed


def test_a_task_runs_audit_of_a_project_with_no_tests_still_refuses(tmp_path: Path) -> None:
    root = _repo(tmp_path, base_tests=False, head_tests=False)
    found = Auditor(root, config=AuditorConfig()).tier1()
    assert _rows(found)["tests"][:2] == ("fail", "code-wrong")
    assert not found.passed


def test_saddle_audit_tiered_audits_a_persons_commits(tmp_path: Path) -> None:
    root = _repo(tmp_path, base_tests=False, head_tests=False)
    _git(root, "commit", "-am", "change")
    results = _tiered_audit(root, "HEAD~1", test_command="python -m pytest -q", cache=None)
    assert isinstance(results, tuple)
    tests = next(f for r in results for f in r.findings if f.gate == "tests")
    assert (tests.verdict, tests.detail) == ("not-proven", NO_TESTS_DETAIL)
    assert all(r.passed for r in results)
