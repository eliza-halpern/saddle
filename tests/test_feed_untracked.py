"""What the audit's copy of the worktree holds besides the tracked files (#173).

The hypothesis in #173: the worker's own `.coverage` and `.pytest_cache`,
left by its whole-suite run, reached the audit's snapshot and failed tests
the change did not break. Refuted for those two: the project's `.gitignore`
names them, and the snapshot copies nothing git ignores. What does reach it
is a pytest-cov worker data file, `.coverage.<host>.pid<n>.X<rand>x.<rand>`:
a pytest-xdist worker that runs out of tests saves one at the top of the
tree and the controller combines it into `.coverage` only when the last
worker ends, so a suite still running leaves files that `.coverage` in a
`.gitignore` does not match. The snapshot stages them as the run's new
files, as it must (`auto` commits the worktree as left, `git add -A`).

Contract: a failing audit names, in the text the worker reads, every file
its copy holds that the worktree does not track; a clean worktree, and a
passing audit, add no such line.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from file_checks import REGISTRY, key_for
from test_feed import BUGGY, TEST, FakeAuditor, git

from saddle import impact
from saddle.feed import UNTRACKED_HEAD, AuditFeed, AuditResult, render, snapshot

WORKER_DATA = ".coverage.host.pid4242.Xabcdefx.abcdefgh"
"""A pytest-cov xdist worker's data file, spelled as pytest-cov 7 names one."""


@pytest.fixture
def worktree(tmp_path: Path) -> Path:
    """A linked worktree, as `auto` makes a run's, of a repo whose `.gitignore`
    names the two artifacts #173 suspected, as this repository's own does."""
    repo = tmp_path / "repo"
    (repo / "tests").mkdir(parents=True)
    (repo / "calc.py").write_text(BUGGY)
    (repo / "tests" / "test_calc.py").write_text(TEST)
    (repo / ".gitignore").write_text(".coverage\n.pytest_cache/\n")
    git(repo, "init", "-q", "-b", "main")
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", "init")
    tree = tmp_path / "worktree"
    git(repo, "worktree", "add", "-q", "-b", "run", str(tree), "HEAD")
    return tree


def _listed(worktree: Path, into: Path) -> list[str]:
    tree = snapshot(worktree, worktree, into)
    return git(into, "ls-tree", "-r", "--name-only", tree).splitlines()


def test_ignored_artifacts_never_reach_the_snapshot_and_a_worker_data_file_does(
    worktree: Path, tmp_path: Path
) -> None:
    """Reproduction, both halves. `.coverage` and `.pytest_cache` change
    nothing the audit sees: the snapshot is the same tree with and without
    them. A worker data file is copied and staged as a new file. Its key has
    no entry in this repository's file-type registry, so it fails
    `test_the_tracked_tree_is_fully_registered`, one of the tests checkpoints
    7 and 8 of the dogfood run failed; and a non-Python file nothing names
    sends the impact selection to the whole suite, which those two
    checkpoints ran where the ones around them ran 47 of 227 files."""
    test = worktree / "tests" / "test_calc.py"
    test.write_text(test.read_text() + "# the change: one test file\n")
    clean = _listed(worktree, tmp_path / "clean")
    (worktree / ".coverage").write_bytes(b"SQLite format 3\0")
    (worktree / ".pytest_cache" / "v" / "cache").mkdir(parents=True)
    (worktree / ".pytest_cache" / "v" / "cache" / "nodeids").write_text("[]")
    assert _listed(worktree, tmp_path / "ignored") == clean
    (worktree / WORKER_DATA).write_bytes(b"SQLite format 3\0")
    stray = _listed(worktree, tmp_path / "stray")
    assert sorted(stray) == sorted([*clean, WORKER_DATA])
    assert key_for(WORKER_DATA) not in REGISTRY
    base = git(worktree, "rev-parse", "HEAD")
    assert impact.select(tmp_path / "clean", base, {}) == ("tests/test_calc.py",)
    assert impact.select(tmp_path / "stray", base, {}) is None


def _final(worktree: Path, tmp_path: Path) -> tuple[bool, str, AuditResult]:
    f = AuditFeed(
        worktree=worktree,
        baseline=git(worktree, "rev-parse", "HEAD"),
        journal=tmp_path / "j.jsonl",
        run_span="s",
        auditor=FakeAuditor(),
    )
    ok, text = f.final()
    f.close()
    return ok, text, f.results[-1]


def test_a_failing_audit_names_the_untracked_files_its_copy_holds(
    worktree: Path, tmp_path: Path
) -> None:
    """Known-bad: the worker data file reached the copy and the finding said
    nothing of it, so the model guessed for 27 rounds what the copy held.
    Known-good in the same test: the worker's own new test file is named too,
    and a gitignored artifact is not, since the copy does not hold it."""
    (worktree / "tests" / "test_more.py").write_text("def test_more():\n    pass\n")
    (worktree / WORKER_DATA).write_bytes(b"SQLite format 3\0")
    (worktree / ".coverage").write_bytes(b"SQLite format 3\0")
    ok, text, result = _final(worktree, tmp_path)
    assert not ok  # calc.py still subtracts: the fake fails `tests`
    named = next(line for line in text.splitlines() if line.startswith(UNTRACKED_HEAD))
    assert WORKER_DATA in named
    assert "tests/test_more.py" in named
    assert ".coverage," not in named
    assert ".coverage." not in named.replace(WORKER_DATA, "")
    assert result.untracked == (WORKER_DATA, "tests/test_more.py")
    assert result.to_dict()["untracked"] == [WORKER_DATA, "tests/test_more.py"]


def test_a_clean_worktree_or_a_passing_audit_adds_no_untracked_line(
    worktree: Path, tmp_path: Path
) -> None:
    ok, text, result = _final(worktree, tmp_path)
    assert not ok
    assert UNTRACKED_HEAD not in text
    assert result.untracked == ()
    assert "untracked" not in result.to_dict()
    passed = AuditResult("finish", "t" * 40, (), untracked=(WORKER_DATA,))
    assert passed.passed
    assert UNTRACKED_HEAD not in render(passed)


def test_a_long_untracked_list_is_cut_and_counted() -> None:
    from saddle.auditor import Finding

    bad = Finding("tests", 1, "fail", "code-wrong", "1 failing", ("x",))
    names = tuple(f"f{i:02}.txt" for i in range(15))
    line = render(AuditResult("checkpoint 1", "t" * 40, (bad,), untracked=names)).splitlines()[-1]
    assert line.startswith(UNTRACKED_HEAD)
    assert "f09.txt" in line
    assert "f10.txt" not in line
    assert " f09.txt and 5 more. " in line
