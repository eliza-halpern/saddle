"""What a refused or repeated check tells the model, and when (#176).

A run argued "but the tree has changed since check 1" against a refusal that
named only the number, and read the same test-flip paragraph at every
checkpoint from 17 on although only its finish summary could clear it.

Contract: a refused check names the files it compared, and the comparison is
the one it states: a new file the worktree does not track is a change, a
gitignored one is not. A failing `test-changes` finding is shown in full the
first time with what clears it, then as one line naming where it was said;
its verdict never changes, and finish shows it in full.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from test_feed import BUGGY, TEST, FakeAuditor, git

from saddle.auditor import TEST_CHANGES
from saddle.feed import (
    CHECK_UNCHANGED,
    FLIP_CLEARED_AT_FINISH,
    FLIP_SAID,
    AuditFeed,
)

ZERO = "\n\ndef test_add_zero():\n    assert add(0, 0) == 0\n"


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    root = tmp_path / "repo"
    (root / "tests").mkdir(parents=True)
    (root / "calc.py").write_text(BUGGY.replace("a - b", "a + b"))
    (root / "tests" / "test_calc.py").write_text(TEST + ZERO)
    (root / ".gitignore").write_text(".coverage\n")
    git(root, "init", "-q", "-b", "main")
    git(root, "add", "-A")
    git(root, "commit", "-q", "-m", "init")
    return root


def _feed(repo: Path, tmp_path: Path) -> AuditFeed:
    return AuditFeed(repo, "HEAD", tmp_path / "j.jsonl", "s", auditor=FakeAuditor())


def test_a_gitignored_file_leaves_the_tree_unchanged_and_a_new_file_does_not(
    repo: Path, tmp_path: Path
) -> None:
    fed = _feed(repo, tmp_path)
    (repo / "calc.py").write_text((repo / "calc.py").read_text() + "# edited\n")
    assert not fed.check().startswith(CHECK_UNCHANGED)
    (repo / ".coverage").write_bytes(b"SQLite format 3\0")
    refused = fed.check()
    assert refused.startswith(CHECK_UNCHANGED + "1;")
    assert "gitignored files and anything outside it not" in refused
    (repo / "notes.txt").write_text("a new file the worktree does not track\n")
    assert not fed.check().startswith(CHECK_UNCHANGED)
    assert len(fed.checks) == 2


def _flip(repo: Path) -> None:
    test = repo / "tests" / "test_calc.py"
    test.write_text(test.read_text().replace("add(2, 2) == 4", "add(2, 3) == 5"))


def test_a_flip_is_said_in_full_once_then_in_one_line_and_in_full_at_finish(
    repo: Path, tmp_path: Path
) -> None:
    _flip(repo)
    fed = _feed(repo, tmp_path)
    first = fed.check()
    assert "test_add" in first
    assert FLIP_CLEARED_AT_FINISH in first
    (repo / "calc.py").write_text((repo / "calc.py").read_text() + "# edited\n")
    second = fed.check()
    assert (
        f"- {TEST_CHANGES} (tier 1): fail, evidence-thin: {FLIP_SAID.format(point='check 1')}"
        in second
    )
    assert "test_add" not in second
    assert FLIP_CLEARED_AT_FINISH not in second
    # the verdict is kept and the record holds the finding whole
    (kept,) = [f for f in fed.checks[-1].findings if f.gate == TEST_CHANGES]
    assert kept.verdict == "fail"
    assert "test_add" in kept.detail
    fed.tell_summary("done")
    accepted, text = fed.final()
    assert not accepted
    assert "test_add" in text
    assert FLIP_SAID.format(point="check 1") not in text


def test_a_different_flip_is_said_in_full_when_it_first_appears(repo: Path, tmp_path: Path) -> None:
    _flip(repo)
    fed = _feed(repo, tmp_path)
    fed.check()
    test = repo / "tests" / "test_calc.py"
    test.write_text(test.read_text().replace("add(0, 0) == 0", "add(0, 1) == 1"))
    again = fed.check()
    assert FLIP_CLEARED_AT_FINISH in again
    assert "test_add_zero" in again
    assert FLIP_SAID.format(point="check 1") not in again
