"""A checkpoint delivered after the tree changed says so, and names what changed (#188).

A checkpoint audits the tree as a burst of edits left it and arrives with a later
tool result. In one run the worker fixed a `ruff format` failure with a command,
then read the checkpoint that still reported it, under a header naming the tree
only by hash, and spent a minute working out that the audit was of the tree
before its fix. A command that changes files is no edit to the feed, so counting
edits cannot see it; the files' stat can.

Known-bad: a file changed by a command after the checkpoint was taken is named
when the checkpoint is delivered. Known-good: a checkpoint of the tree as it
still is reads as before; a tree state that cannot be read says so and is never
read as unchanged.
"""

from __future__ import annotations

import threading
from pathlib import Path

import pytest
from test_feed import FakeAuditor, git, repo

from saddle import feed
from saddle.audit import AuditError
from saddle.feed import STALE_NAMED, AuditFeed

__all__ = ["repo"]  # the fixture, shared with test_feed


def held(repo: Path, tmp_path: Path) -> tuple[AuditFeed, threading.Event]:
    """A feed whose first checkpoint has started and waits for the latch."""
    latch = threading.Event()
    unit = AuditFeed(
        worktree=repo,
        baseline=git(repo, "rev-parse", "HEAD"),
        journal=tmp_path / "j.jsonl",
        run_span="s",
        auditor=FakeAuditor(latch),
    )
    unit.after_tool("edit_file", ok=True)
    unit.before_tool("run_command")  # the burst ends: checkpoint 1 starts
    assert unit.checkpoints == 1
    return unit, latch


def delivered(unit: AuditFeed, latch: threading.Event) -> str:
    latch.set()
    unit._await()
    text = unit.collect()
    unit.close()
    assert text.startswith("[audit checkpoint 1 on tree ")
    return text


def test_a_file_a_command_changed_since_the_checkpoint_is_named(repo: Path, tmp_path: Path) -> None:
    # As in the run: the burst edited calc.py, the checkpoint froze it, and a command
    # (`ruff format`, say) then rewrote the same file. Same path both times; the
    # file itself is what changed.
    (repo / "calc.py").write_text("def add(a,b):\n    return a - b\n")
    unit, latch = held(repo, tmp_path)
    (repo / "calc.py").write_text("def add(a, b):\n    return a + b\n")
    text = delivered(unit, latch)
    stale = feed.STALE_CHECKPOINT.format(point="checkpoint 1", files="calc.py")
    assert text.endswith("\n" + stale)


def test_a_checkpoint_of_the_tree_as_it_still_is_says_nothing_more(
    repo: Path, tmp_path: Path
) -> None:
    unit, latch = held(repo, tmp_path)
    text = delivered(unit, latch)
    assert "[This checkpoint" not in text
    assert "could not be read" not in text


def test_many_changed_files_are_named_up_to_a_bound_and_counted(repo: Path, tmp_path: Path) -> None:
    unit, latch = held(repo, tmp_path)
    for n in range(STALE_NAMED + 2):
        (repo / f"new_{n}.py").write_text(f"N = {n}\n")
    text = delivered(unit, latch)
    named = ", ".join(f"new_{n}.py" for n in range(STALE_NAMED))
    assert f"changes to {named} and 2 more:" in text


@pytest.mark.parametrize("fails", ["when taken", "when delivered"])
def test_a_tree_state_that_cannot_be_read_is_never_read_as_unchanged(
    repo: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, fails: str
) -> None:
    real = feed.tree_state

    def broken(worktree: Path) -> dict[str, tuple[int, int]]:
        message = "git status failed: no index"
        raise AuditError(message)

    if fails == "when taken":
        monkeypatch.setattr(feed, "tree_state", broken)
    unit, latch = held(repo, tmp_path)
    monkeypatch.setattr(feed, "tree_state", broken if fails == "when delivered" else real)
    text = delivered(unit, latch)
    assert text.endswith(
        "\n[Whether the tree changed since this checkpoint 1 could not be read: "
        "git status failed: no index]"
    )


def test_a_file_a_command_deleted_since_the_checkpoint_is_named(repo: Path, tmp_path: Path) -> None:
    """Git still lists a deleted file and its stat fails: it is read as gone, never
    skipped, or a file deleted after a clean checkpoint would change nothing."""
    unit, latch = held(repo, tmp_path)
    (repo / "calc.py").unlink()
    text = delivered(unit, latch)
    stale = feed.STALE_CHECKPOINT.format(point="checkpoint 1", files="calc.py")
    assert text.endswith("\n" + stale)


def test_a_tree_git_cannot_read_raises_and_says_why(tmp_path: Path) -> None:
    plain = tmp_path / "plain"
    plain.mkdir()
    with pytest.raises(AuditError, match=r"^git status failed: \S"):
        feed.tree_state(plain)
