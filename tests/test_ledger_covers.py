"""A run's ledger says which commits it covers, and which came after it.

A finished run's branch once had two review commits made on top by hand. The
ledger, the session and the packet could not tell the run's commit from the
ones after it. The ledger now seals the run's branch, base, commit and tree
once the run has committed (`auto:committed`); `saddle verify` and the packet
state the covered range and name the commits that sit on top of it.

Known-good: a finished run covers `<base>..<commit>` and its branch has no
later commit; after two commits on top, both are named, oldest first, as not
covered. Known-bad: a lookup that cannot be made (not a repository, branch
gone, history rewritten, git failing) says so and never reads as "none"; a
ledger sealed before the field existed says "not recorded" and names no
commit; a run still going covers nothing yet.
"""

from __future__ import annotations

import io
import shutil
import subprocess
from pathlib import Path
from typing import Any

import pytest
from test_ledger_anchor import git, lines, repo, reseal, run, write

from saddle import covers
from saddle.auto import AutoResult
from saddle.cli import run_verify
from saddle.covers import SHORT, Later, later_commits
from saddle.journal import AUTO_COMMITTED, coverage_path, read_spans, verify_journal
from saddle.packet import compile_packet

__all__ = ["repo"]  # the fixture, re-exported for pytest


def short(sha: str) -> str:
    return sha[:SHORT]


def verify(journal: Path, anchor: Path | None) -> str:
    out = io.StringIO()
    code = run_verify(journal, stdout=out, anchor=anchor)
    assert code == 0, out.getvalue()
    return out.getvalue()


def commit_on(result: AutoResult, name: str, subject: str) -> str:
    """A commit made by hand on top of the run's branch, in the run's worktree."""
    (result.worktree / name).write_text(subject + "\n")
    git(result.worktree, "add", name)
    git(result.worktree, "commit", "-q", "-m", subject)
    return git(result.worktree, "rev-parse", "HEAD").strip()


def reproduce_text(journal: Path, anchor: Path | None) -> str:
    packet = compile_packet(journal, run_id="r1", anchor_repo=anchor)
    return next(row.text for row in packet.rows if row.key == "reproduce")


def test_the_ledger_seals_the_branch_base_commit_and_tree(repo: Path) -> None:
    started_from = git(repo, "rev-parse", "HEAD").strip()
    result = run(repo)
    spans = read_spans(result.journal)
    [committed] = read_spans(coverage_path(result.journal))
    outcome = next(s for s in spans if s.name == "auto:finished")
    assert committed.name == AUTO_COMMITTED
    # bound to this ledger by its outcome, which the commit's own trailer also holds
    assert committed.argv == [AUTO_COMMITTED, result.commit, outcome.record_hash]
    assert spans[-1] is outcome  # nothing is appended to the ledger after its outcome
    fields = dict(part.strip().split(" ", 1) for part in committed.detail.split(";"))
    assert fields == {
        "branch": result.branch,
        "base": started_from,
        "commit": result.commit,
        "tree": git(repo, "rev-parse", f"{result.commit}^{{tree}}").strip(),
    }
    assert result.base == started_from
    assert verify_journal(result.journal) == []


def test_verify_states_the_covered_range_and_names_the_later_commits(repo: Path) -> None:
    started_from = git(repo, "rev-parse", "HEAD").strip()
    result = run(repo)
    tree = git(repo, "rev-parse", f"{result.commit}^{{tree}}").strip()
    covered = f"covers: {short(started_from)}..{short(result.commit)} on {result.branch}"
    before = verify(result.journal, repo)
    assert f"{covered} (tree {short(tree)})\n" in before
    assert f"later commits on {result.branch}: none\n" in before

    first = commit_on(result, "r1.txt", "review fix one")
    second = commit_on(result, "r2.txt", "review fix two")
    after = verify(result.journal, repo)
    assert f"{covered} (tree {short(tree)})\n" in after  # the ledger still covers the run's commit
    assert (
        f"not covered by this ledger: 2 later commit(s): {short(first)} review fix one; "
        f"{short(second)} review fix two\n"
    ) in after
    assert f"later commits on {result.branch}: none" not in after


def test_more_later_commits_than_are_listed_say_how_many_more(repo: Path) -> None:
    result = run(repo)
    for n in range(covers.LISTED_LATER + 2):
        commit_on(result, f"f{n}.txt", f"later {n}")
    text = verify(result.journal, repo)
    assert f"{covers.LISTED_LATER + 2} later commit(s): " in text
    assert "later 0" in text
    assert f"later {covers.LISTED_LATER}" not in text
    assert text.count("; and 2 more\n") == 1


@pytest.mark.parametrize("problem", ["not a repo", "branch gone", "rewritten"])
def test_a_lookup_that_cannot_be_made_says_so_and_never_reads_as_none(
    repo: Path, tmp_path: Path, problem: str
) -> None:
    result = run(repo)
    journal = result.journal
    if problem == "not a repo":
        # a copy of the ledger far from any checkout: the default repository is not one
        journal = tmp_path / "copy" / ".saddle" / "runs" / "r1" / "proofs.jsonl"
        shutil.copytree(result.journal.parent, journal.parent)
    elif problem == "branch gone":
        git(repo, "worktree", "remove", "--force", str(result.worktree))
        git(repo, "branch", "-D", result.branch)
    else:
        # the branch now holds other history: the covered commit is not on it
        git(repo, "worktree", "remove", "--force", str(result.worktree))
        git(repo, "branch", "-f", result.branch, "main")
        git(repo, "checkout", "-q", "--detach")
        (repo / "other.txt").write_text("x\n")
        git(repo, "add", "other.txt")
        git(repo, "commit", "-q", "-m", "elsewhere")
        git(repo, "branch", "-f", result.branch, "HEAD")
    text = verify(journal, None)
    [line] = [x for x in text.splitlines() if x.startswith("later commits on the branch")]
    assert line.startswith("later commits on the branch: not checked (")
    assert {
        "not a repo": "is not a git repository",
        "branch gone": f"branch {result.branch} is not in that repository",
        "rewritten": "history was rewritten",
    }[problem] in line
    assert "none" not in line


def test_git_failing_or_missing_is_a_problem_not_an_empty_list(
    repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    result = run(repo)
    head = result.commit
    assert later_commits(repo, result.branch, head) == Later()  # a real lookup: nothing after
    assert later_commits(repo, result.branch, "0" * 40).problem.endswith(
        "is not in that repository"
    )

    real = covers._git

    def failing(step: str) -> Any:
        def fake(repo_: Path, *args: str) -> tuple[int, str]:
            return (128, "boom") if args[0] == step else real(repo_, *args)

        return fake

    for step, said in (("merge-base", "git merge-base failed: boom"), ("log", "git log failed")):
        monkeypatch.setattr(covers, "_git", failing(step))
        assert later_commits(repo, result.branch, head).problem.startswith(said)
    monkeypatch.undo()

    def no_git(*_: Any, **__: Any) -> Any:
        msg = "git"
        raise FileNotFoundError(msg)

    monkeypatch.setattr(subprocess, "run", no_git)
    assert later_commits(repo, result.branch, head).problem == "git could not run: git"


def test_a_ledger_sealed_before_the_field_says_not_recorded_and_invents_nothing(
    repo: Path,
) -> None:
    result = run(repo)
    coverage_path(result.journal).unlink()  # what an older version left: no coverage file
    assert verify_journal(result.journal) == []
    text = verify(result.journal, repo)
    assert "covers: not recorded (ledger predates this field" in text
    assert result.commit[:SHORT] not in text
    assert "later commits" not in text
    assert "Covers: not recorded" in reproduce_text(result.journal, repo)


def test_a_run_that_has_not_ended_covers_nothing_yet(repo: Path) -> None:
    result = run(repo)
    write(result.journal, lines(result.journal)[:1])  # in flight: a start, no outcome
    coverage_path(result.journal).unlink()
    assert "covers: nothing yet (the run has not ended)" in verify(result.journal, None)


def test_the_packet_states_the_same_coverage(repo: Path) -> None:
    started_from = git(repo, "rev-parse", "HEAD").strip()
    result = run(repo)
    first = commit_on(result, "r1.txt", "review fix one")
    second = commit_on(result, "r2.txt", "review fix two")
    checked = reproduce_text(result.journal, repo)
    assert f"Covers: {short(started_from)}..{short(result.commit)} on {result.branch} (tree " in (
        checked
    )
    assert (
        f"Not covered by this ledger: 2 later commit(s): {short(first)} review fix one; "
        f"{short(second)} review fix two. "
    ) in checked
    unchecked = reproduce_text(result.journal, None)
    assert "Later commits on the branch: not checked here. " in unchecked
    assert "Not covered" not in unchecked
    # No absolute path of the user's machine goes into text a packet shares.
    assert str(repo.parent) not in checked + unchecked


def test_a_ledger_with_no_run_covers_nothing_and_prints_nothing(tmp_path: Path) -> None:
    journal = tmp_path / "chat.jsonl"
    journal.write_text("")
    assert covers.read_coverage(journal, []) == []
    assert covers.verify_lines(journal, [], tmp_path) == []
    assert covers.packet_text(journal, [], None) == ""


def codes(journal: Path) -> list[str]:
    return [issue.code for issue in verify_journal(journal)]


def test_a_tampered_or_foreign_commit_record_fails_verification(repo: Path) -> None:
    result = run(repo)
    cfile = coverage_path(result.journal)
    [row] = lines(cfile)
    assert codes(result.journal) == []
    # known-bad 1: the sealed commit is edited without resealing the record
    write(cfile, [{**row, "detail": row["detail"].replace(result.commit, "f" * 40)}])
    assert codes(result.journal) == ["bad-hash"]
    # known-bad 2: resealed, but it names an outcome this ledger does not hold: it
    # verifies, is stated as foreign, and covers nothing
    write(cfile, [reseal({**row, "argv": [AUTO_COMMITTED, result.commit, "0" * 64]})])
    assert codes(result.journal) == []
    said = verify(result.journal, None)
    assert "coverage file: 1 commit record(s) name an outcome this ledger does not hold" in said
    assert "covers: not recorded" in said
    assert result.commit[:SHORT] not in said
    # known-bad 3: the same record twice
    write(cfile, [row, row])
    assert codes(result.journal) == ["committed-duplicate"]
    # known-good again: the honest file
    write(cfile, [row])
    assert codes(result.journal) == []
