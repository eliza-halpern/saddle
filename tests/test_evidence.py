"""Tests for saddle.evidence: collectors over tmp worktrees."""

from __future__ import annotations

import sys
import warnings
from pathlib import Path

import pytest

from saddle.evidence import (
    changed_lines,
    covered_lines,
    git_diff,
    materialize_baseline,
    run_argv,
    run_shell,
    run_stdin,
    statement_lines,
    under_coverage,
)


def _git_repo(root: Path) -> None:
    setup = (
        ["git", "init"],
        ["git", "config", "user.email", "test@example.com"],
        ["git", "config", "user.name", "test"],
        ["git", "commit", "--allow-empty", "-m", "base"],
    )
    for argv in setup:
        assert run_argv(argv, root) == 0


def test_run_argv_returns_exit_code(tmp_path: Path) -> None:
    assert run_argv([sys.executable, "-c", "pass"], tmp_path) == 0
    assert run_argv([sys.executable, "-c", "raise SystemExit(3)"], tmp_path) == 3


def test_run_argv_captures_child_output(tmp_path: Path, capfd: pytest.CaptureFixture[str]) -> None:
    assert run_argv([sys.executable, "-c", "print('hi')"], tmp_path) == 0
    assert capfd.readouterr().out == ""


def test_run_stdin_feeds_text(tmp_path: Path, capfd: pytest.CaptureFixture[str]) -> None:
    (tmp_path / "f").write_text("x\n")
    code = run_stdin(["sh", "-c", "cat f && grep -q hi"], tmp_path, "hi\n")
    assert code == 0
    assert capfd.readouterr().out == ""


def test_run_shell_splits_command_string(tmp_path: Path) -> None:
    assert run_shell(f"{sys.executable} -c pass", tmp_path) == 0
    assert run_shell(f"{sys.executable} -c 'raise SystemExit(2)'", tmp_path) == 2


def test_changed_lines_modified_file_hunk_range() -> None:
    diff = (
        "diff --git a/n.py b/n.py\n"
        "--- a/n.py\n"
        "+++ b/n.py\n"
        "@@ -1,2 +1,3 @@\n"
        " x = 1\n"
        "+y = 2\n"
        "+z = 3\n"
    )
    assert changed_lines(diff) == {("n.py", 1), ("n.py", 2), ("n.py", 3)}


def test_changed_lines_new_file_single_line_hunk() -> None:
    diff = "diff --git a/new.py b/new.py\n--- /dev/null\n+++ b/new.py\n@@ -0,0 +1 @@\n+x = 1\n"
    assert changed_lines(diff) == {("new.py", 1)}


def test_changed_lines_deleted_file_and_empty_diff_yield_none() -> None:
    deleted = (
        "diff --git a/gone.py b/gone.py\n--- a/gone.py\n+++ /dev/null\n@@ -1,2 +0,0 @@\n-x = 1\n"
    )
    assert changed_lines(deleted) == set()
    assert changed_lines("") == set()


def test_changed_lines_ignores_hunk_before_any_file() -> None:
    diff = "@@ -1 +1 @@\n+x = 1\n+++ b/n.py\n@@ -1 +1 @@\n+y = 2\n"
    assert changed_lines(diff) == {("n.py", 1)}


def test_changed_lines_ignores_additions_after_dev_null() -> None:
    diff = "+++ /dev/null\n@@ -1 +1 @@\n+x = 1\n"
    assert changed_lines(diff) == set()


def test_git_diff_shows_tracked_modification(tmp_path: Path) -> None:
    _git_repo(tmp_path)
    (tmp_path / "n.py").write_text("x = 1\n")
    assert run_argv(["git", "add", "n.py"], tmp_path) == 0
    assert run_argv(["git", "commit", "-m", "add n"], tmp_path) == 0
    (tmp_path / "n.py").write_text("x = 1\ny = 2\n")
    diff = git_diff(tmp_path, "HEAD")
    assert changed_lines(diff) == {("n.py", 2)}


def test_git_diff_unknown_ref_raises(tmp_path: Path) -> None:
    _git_repo(tmp_path)
    with pytest.raises(RuntimeError, match="no-such-ref"):
        git_diff(tmp_path, "no-such-ref")


def test_materialize_baseline_extracts_pre_change_tree(tmp_path: Path) -> None:
    _git_repo(tmp_path)
    (tmp_path / "n.py").write_text("x = 1\n")
    assert run_argv(["git", "add", "n.py"], tmp_path) == 0
    assert run_argv(["git", "commit", "-m", "add n"], tmp_path) == 0
    (tmp_path / "n.py").write_text("x = 2\n")
    dest = tmp_path / "nested" / "baseline"
    materialize_baseline(tmp_path, "HEAD", dest)
    assert (dest / "n.py").read_text() == "x = 1\n"
    materialize_baseline(tmp_path, "HEAD", dest)
    assert (dest / "n.py").read_text() == "x = 1\n"


def test_materialize_baseline_unknown_ref_raises(tmp_path: Path) -> None:
    _git_repo(tmp_path)
    with pytest.raises(RuntimeError, match="no-such-ref"):
        materialize_baseline(tmp_path, "no-such-ref", tmp_path / "baseline")


def test_materialize_baseline_extracts_without_warnings(tmp_path: Path) -> None:
    _git_repo(tmp_path)
    (tmp_path / "n.py").write_text("x = 1\n")
    assert run_argv(["git", "add", "n.py"], tmp_path) == 0
    assert run_argv(["git", "commit", "-m", "add n"], tmp_path) == 0
    with warnings.catch_warnings():
        warnings.simplefilter("error")
        materialize_baseline(tmp_path, "HEAD", tmp_path / "baseline")


def test_under_coverage_wraps_pytest_command() -> None:
    wrapped = under_coverage("pytest tests/test_n.py", "/tmp/cov.n")
    assert wrapped == "coverage run --data-file=/tmp/cov.n -m pytest tests/test_n.py"


def test_under_coverage_wraps_only_first_pytest() -> None:
    wrapped = under_coverage("pytest -p no:pytest_cache tests/", "/tmp/cov.n")
    assert wrapped == "coverage run --data-file=/tmp/cov.n -m pytest -p no:pytest_cache tests/"


def test_covered_lines_reads_subprocess_data_file(tmp_path: Path) -> None:
    target = tmp_path / "n.py"
    target.write_text("x = 1\ny = 2\n")
    data_file = str(tmp_path / ".coverage.node")
    prog = (
        "import coverage;"
        f"cov = coverage.Coverage(data_file={data_file!r}, config_file=False);"
        "cov.start();"
        f"exec(compile(open({str(target)!r}).read(), {str(target)!r}, 'exec'));"
        "cov.stop();cov.save()"
    )
    assert run_argv([sys.executable, "-c", prog], tmp_path) == 0
    name = str(target)
    assert covered_lines(data_file, [name]) == {(name, 1), (name, 2)}
    assert covered_lines(data_file, [str(tmp_path / "other.py")]) == set()
    assert covered_lines(data_file, []) == set()


def test_covered_lines_missing_data_file_yields_empty(tmp_path: Path) -> None:
    assert covered_lines(str(tmp_path / "nope.coverage"), ["n.py"]) == set()


def test_covered_lines_corrupt_data_file_yields_empty(tmp_path: Path) -> None:
    bad = tmp_path / "bad.coverage"
    bad.write_bytes(b"not a coverage file at all" * 10)
    assert covered_lines(str(bad), ["n.py"]) == set()


def test_covered_lines_ignores_local_config(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (tmp_path / ".coveragerc").write_text("[run\ninvalid [[[\n")
    monkeypatch.chdir(tmp_path)
    assert covered_lines(str(tmp_path / "nope.coverage"), ["n.py"]) == set()


def test_statement_lines_skips_blanks_and_comments() -> None:
    source = "# a comment\n\nx = 1\n\n\ndef f():\n    return x\n"
    assert statement_lines(source) == {3, 6, 7}


def test_statement_lines_unparseable_yields_none() -> None:
    assert statement_lines("def broken(:\n") == set()
