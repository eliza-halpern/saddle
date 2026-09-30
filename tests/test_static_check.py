"""`[tool.saddle] static-check`: the project's own static check, run by the audit.

Known-good: a project that declares one gets a `static-check` finding at tier
1, passing on a tree the check accepts; a project that declares none gets no
such finding, and every count reads as before. Known-bad: a tree the check
rejects fails the finding, quoting the checker's lines, so the finish is
refused; the setting is read from the baseline commit, so the audited tree
cannot drop its own check; an unusable value is refused by name.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest
from test_auditor import BASE_CODE, FIXED_CODE, TEST_BODY, _git, _init

from saddle.audit import AuditError
from saddle.auditor import STATIC_CHECK, Auditor
from saddle.evidence import CapturedRun, SuiteLimitError, static_check
from saddle.gates import TOOL_UNAVAILABLE, check_static

# A stand-in type checker: it fails on any .py file holding the word BADTYPE,
# naming the file and line the way mypy does.
CHECKER = (
    "import pathlib, sys\n"
    "bad = [f'{p}:{i}: error: BADTYPE here' for p in sorted(pathlib.Path('.').rglob('*.py'))\n"
    "       for i, line in enumerate(p.read_text().splitlines(), 1)\n"
    "       if 'BADTYPE' in line and p.name != 'checker.py']\n"
    "print('\\n'.join(bad)); sys.exit(1 if bad else 0)\n"
)


def project(tmp_path: Path, *, declared: bool) -> Path:
    tree = tmp_path / "tree"
    files = {"n.py": BASE_CODE, "checker.py": CHECKER}
    if declared:
        files["pyproject.toml"] = (
            f'[tool.saddle]\nstatic-check = ["{sys.executable}", "checker.py"]\n'
        )
    _init(tree, files)
    (tree / "n.py").write_text(FIXED_CODE)
    (tree / "test_n.py").write_text(TEST_BODY.format(value=2))
    return tree


def verdicts(tree: Path) -> dict[str, str]:
    return {f.gate: f.verdict for f in Auditor(tree).tier1().findings}


def test_a_declared_check_that_accepts_the_tree_passes(tmp_path: Path) -> None:
    assert verdicts(project(tmp_path, declared=True))[STATIC_CHECK] == "pass"


def test_a_declared_check_that_rejects_the_tree_fails_and_names_the_line(
    tmp_path: Path,
) -> None:
    tree = project(tmp_path, declared=True)
    (tree / "test_n.py").write_text(TEST_BODY.format(value=2) + "x = 1  # BADTYPE\n")
    found = {f.gate: f for f in Auditor(tree).tier1().findings}
    assert found[STATIC_CHECK].verdict == "fail"
    assert "test_n.py:6: error: BADTYPE here" in found[STATIC_CHECK].detail
    assert not Auditor(tree).tier1().passed


def test_no_declared_check_means_no_finding(tmp_path: Path) -> None:
    assert STATIC_CHECK not in verdicts(project(tmp_path, declared=False))


def test_the_audited_tree_cannot_drop_its_own_check(tmp_path: Path) -> None:
    """The setting is read at the baseline: a tree that deletes it and adds a
    violation is still checked, and still fails."""
    tree = project(tmp_path, declared=True)
    (tree / "pyproject.toml").write_text("[tool.saddle]\n")
    (tree / "n.py").write_text(FIXED_CODE + "y = 2  # BADTYPE\n")
    assert verdicts(tree)[STATIC_CHECK] == "fail"


@pytest.mark.parametrize("value", ['"mypy src"', "[]", '["mypy", ""]', "[1, 2]"])
def test_an_unusable_value_is_refused_by_name(tmp_path: Path, value: str) -> None:
    tree = tmp_path / "tree"
    _init(tree, {"n.py": BASE_CODE, "pyproject.toml": f"[tool.saddle]\nstatic-check = {value}\n"})
    with pytest.raises(SuiteLimitError, match="static-check"):
        static_check(tree, "HEAD")
    (tree / "n.py").write_text(FIXED_CODE)
    with pytest.raises(AuditError, match="static-check"):
        Auditor(tree).tier1()
    _git(tree, "status")


def run(code: int, out: str = "", *, timed_out: bool = False) -> CapturedRun:
    return CapturedRun(argv=("mypy",), exit_code=code, stdout=out, stderr="", timed_out=timed_out)


def test_check_static_reads_the_exit_and_quotes_the_errors() -> None:
    assert check_static(["mypy", "src"], run(0)).passed
    failed = check_static(["mypy", "src"], run(1, "a.py:3: error: X\nFound 1 error\n"))
    assert not failed.passed
    assert failed.detail == "mypy src exited 1:\na.py:3: error: X\nFound 1 error"
    many = check_static(["mypy"], run(1, "\n".join(f"e{i}" for i in range(20))))
    assert many.detail.endswith("(+8 more lines)")
    assert "could not be launched" in check_static(["mypy"], run(TOOL_UNAVAILABLE)).detail
    timed = check_static(["mypy"], run(0, timed_out=True))
    assert not timed.passed
    assert timed.detail == "mypy: timed out"
