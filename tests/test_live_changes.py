"""A running run's changes so far, read without touching its worktree (#85 item 11).

`branch_actions.live_diff` diffs a run's worktree against the commit it started from:
what it committed, what it edited and the files it created. It reads through a throwaway
index, because the worktree belongs to a run that is still writing to it. The page asks
`/api/sessions/{sid}/tasks/{rid}/changes` for it while the run is going.

Known-bad: a new file missing (a plain `git diff <base>` leaves untracked files out); the
run's own index or status changed by the read; another session's run, or a run with no
worktree yet, answered as if it were there; a diff git refuses answered as a server error.
Known-good: every changed file with its added lines, an ignored file left out, the worktree
byte for byte as it was, and git's refusal answered as a refusal in git's own words.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from packet_seed import git, make_repo
from starlette.testclient import TestClient
from test_ui3_mode import NoModel, _server_of

from saddle.sessions import SessionStore
from saddle.web import branch_actions
from saddle.web.app import build_app
from saddle.web.tasks import TaskRun


def _running(root: Path) -> tuple[Path, str]:
    """A repo at a run's mid-flight: one commit past its base, an edit, a new file, a log."""
    repo = make_repo(root)
    base = git(repo, "rev-parse", "HEAD").strip()
    (repo / ".gitignore").write_text("*.log\n")
    (repo / "calc.py").write_text("def add(a, b):\n    return a + b\n")
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", "the run's first commit")
    (repo / "README").write_text("calc, in progress\n")  # edited, not staged
    (repo / "live_change.py").write_text("LIVE_MARKER = 1\n")  # new, untracked
    (repo / "run.log").write_text("noise\n")  # ignored
    return repo, base


def _state(repo: Path) -> tuple[str, str]:
    return git(repo, "status", "--porcelain=v1"), git(repo, "ls-files", "-s")


def test_every_change_so_far_is_listed_and_the_worktree_is_untouched(tmp_path: Path) -> None:
    repo, base = _running(tmp_path / "repo")
    before = _state(repo)
    files = {f.path: f.patch for f in branch_actions.live_diff(repo, base)}
    assert set(files) == {".gitignore", "calc.py", "README", "live_change.py"}, sorted(files)
    assert "+LIVE_MARKER = 1" in files["live_change.py"], files["live_change.py"]
    assert "+    return a + b" in files["calc.py"], files["calc.py"]
    assert "+calc, in progress" in files["README"], files["README"]
    assert _state(repo) == before  # neither its index nor its status moved


def test_a_base_git_cannot_read_is_refused(tmp_path: Path) -> None:
    repo, _base = _running(tmp_path / "repo")
    with pytest.raises(branch_actions.ActionRefusedError):
        branch_actions.live_diff(repo, "no-such-commit")


def test_the_endpoint_answers_for_its_own_live_run_only(tmp_path: Path) -> None:
    repo, base = _running(tmp_path / "repo")
    store = SessionStore(tmp_path / "s")
    sid = store.create(title="t", workdir=str(repo)).id
    other = store.create(title="other", workdir=str(repo)).id
    app = build_app(store, NoModel, default_workdir=repo)
    run = TaskRun(run_id="r1", session_id=sid, task="t", time_budget_s=0, token_budget=0)
    _server_of(app).tasks["r1"] = run
    with TestClient(app) as client:
        early = client.get(f"/api/sessions/{sid}/tasks/r1/changes")
        run.worktree, run.base, run.branch = repo, base, "saddle/auto/r1"
        got = client.get(f"/api/sessions/{sid}/tasks/r1/changes")
        elsewhere = client.get(f"/api/sessions/{other}/tasks/r1/changes")
        unknown = client.get(f"/api/sessions/{sid}/tasks/nope/changes")
    assert early.status_code == 409, early.text  # the run has not made its worktree yet
    assert got.status_code == 200, got.text
    assert got.json()["branch"] == "saddle/auto/r1"
    assert "live_change.py" in {f["path"] for f in got.json()["files"]}
    assert elsewhere.status_code == 404, elsewhere.text
    assert unknown.status_code == 404, unknown.text


def test_a_live_diff_git_refuses_is_answered_as_a_refusal(tmp_path: Path) -> None:
    repo, _base = _running(tmp_path / "repo")
    store = SessionStore(tmp_path / "s")
    sid = store.create(title="t", workdir=str(repo)).id
    app = build_app(store, NoModel, default_workdir=repo)
    run = TaskRun(run_id="r1", session_id=sid, task="t", time_budget_s=0, token_budget=0)
    run.worktree, run.base, run.branch = repo, "no-such-commit", "saddle/auto/r1"
    _server_of(app).tasks["r1"] = run
    with TestClient(app) as client:
        refused = client.get(f"/api/sessions/{sid}/tasks/r1/changes")
    assert refused.status_code == 409, refused.text
    said = refused.json()["error"]
    assert "no-such-commit" in said, said  # git's own words, naming what it could not read
    assert "worktree yet" not in said, said
