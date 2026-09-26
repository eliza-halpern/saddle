"""The packet's action row: view the run's diff, merge it, discard it.

Known-good: a finished, audited run merges into a clean checkout
(fast-forward, and cherry-pick when the checkout moved on); the diff lists
the run's files and nothing the checkout changed since; discard deletes the
branch and its run worktree; every attempt is logged to the session.

Known-bad: merge is refused on a dirty checkout, on an honest stop, on an
unaudited finish, on a failed audit, and without the confirm naming the
branch; discard is refused for the checkout's current branch and without
the confirm. A git failure comes back verbatim and leaves no half-merge.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from packet_seed import git, make_repo, seed
from starlette.testclient import TestClient
from test_ui3_mode import NoModel

from saddle.packet import Packet, Row
from saddle.sessions import SessionStore
from saddle.web import branch_actions
from saddle.web.app import build_app


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    return make_repo(tmp_path / "repo")


@pytest.fixture
def store(tmp_path: Path) -> SessionStore:
    return SessionStore(tmp_path / "s")


def client_for(store: SessionStore, repo: Path) -> TestClient:
    return TestClient(build_app(store, NoModel, default_workdir=repo))


def url(sid: str, rid: str, what: str) -> str:
    return f"/api/sessions/{sid}/tasks/{rid}/{what}"


def head(repo: Path) -> str:
    return git(repo, "rev-parse", "HEAD").strip()


# -- known-good ---------------------------------------------------------------


def test_a_finished_audited_run_fast_forwards_into_a_clean_checkout(
    store: SessionStore, repo: Path
) -> None:
    sid, rid, branch = seed(store, repo, "audited")
    with client_for(store, repo) as client:
        info = client.get(url(sid, rid, "branch")).json()
        done = client.post(url(sid, rid, "merge"), json={"confirm": branch})
    assert info["branch"] == branch
    assert info["target"] == "main"
    assert info["merge_refusal"] == ""
    assert info["exists"] is True
    assert info["sealed"] is False
    assert info["recap"].startswith("verdict: finished")
    assert done.status_code == 200, done.text
    assert done.json()["output"].startswith(f"Fast-forwarded main to {branch}")
    assert head(repo) == git(repo, "rev-parse", branch).strip()
    assert "a + b" in (repo / "calc.py").read_text()
    log = (store.journal_path(sid).parent / "actions.log").read_text()
    assert f"\t{rid}\tmerge\tFast-forwarded main" in log


def test_a_checkout_that_moved_on_gets_the_runs_commits_cherry_picked(
    store: SessionStore, repo: Path
) -> None:
    sid, rid, branch = seed(store, repo, "audited")
    (repo / "README").write_text("calc, moved on\n")
    git(repo, "commit", "-qam", "meanwhile")
    before = head(repo)
    with client_for(store, repo) as client:
        done = client.post(url(sid, rid, "merge"), json={"confirm": branch})
    assert done.status_code == 200, done.text
    assert done.json()["output"].startswith("Cherry-picked")
    assert git(repo, "rev-parse", "HEAD~1").strip() == before
    assert "a + b" in (repo / "calc.py").read_text()
    assert (repo / "README").read_text() == "calc, moved on\n"


def test_the_diff_shows_the_runs_files_and_nothing_else(store: SessionStore, repo: Path) -> None:
    sid, rid, branch = seed(store, repo, "audited")
    # The checkout moves on after the run started: not the run's change.
    (repo / "README").write_text("calc, moved on\n")
    git(repo, "commit", "-qam", "meanwhile")
    with client_for(store, repo) as client:
        got = client.get(url(sid, rid, "diff")).json()
    assert got["branch"] == branch
    assert [f["path"] for f in got["files"]] == ["calc.py", "tests/test_zero.py"]
    calc = got["files"][0]["patch"]
    assert "-    return a - b" in calc
    assert "+    return a + b" in calc
    assert "test_zero" not in calc  # per file, not the whole diff each time


def test_discard_deletes_the_branch_and_its_run_worktree(store: SessionStore, repo: Path) -> None:
    sid, rid, branch = seed(store, repo, "stopped")
    worktree = repo / ".saddle" / "worktrees" / rid
    assert worktree.is_dir()
    with client_for(store, repo) as client:
        done = client.post(url(sid, rid, "discard"), json={"confirm": branch})
        again = client.get(url(sid, rid, "branch")).json()
        packet = client.get(url(sid, rid, "packet")).json()
    assert done.status_code == 200, done.text
    assert "Deleted branch" in done.json()["output"]
    assert not branch_actions.branch_exists(repo, branch)
    assert not worktree.exists()
    assert again["exists"] is False
    assert packet["verdict"] == "stopped"  # the ledger outlives the branch


# -- known-bad ----------------------------------------------------------------


@pytest.mark.parametrize("dirt", ["modified", "untracked", "staged"])
def test_merge_is_refused_on_a_dirty_checkout(store: SessionStore, repo: Path, dirt: str) -> None:
    sid, rid, branch = seed(store, repo, "audited")
    if dirt == "untracked":
        (repo / "scratch.txt").write_text("mine\n")
    else:
        (repo / "README").write_text("edited, not committed\n")
        if dirt == "staged":
            git(repo, "add", "README")
    before = head(repo)
    with client_for(store, repo) as client:
        done = client.post(url(sid, rid, "merge"), json={"confirm": branch})
    assert done.status_code == 409
    assert done.json()["error"].startswith("The checkout has uncommitted changes")
    assert head(repo) == before
    log = (store.journal_path(sid).parent / "actions.log").read_text()
    assert f"\t{rid}\tmerge refused\tThe checkout has uncommitted changes" in log


@pytest.mark.parametrize(
    ("kind", "why"),
    [
        ("stopped", "The run is stopped, not finished"),
        ("unaudited", "No auditor verdict covers the change"),
        ("failed", "Failed on the record: Tests"),
    ],
)
def test_merge_is_refused_unless_finished_and_audited(
    store: SessionStore, repo: Path, kind: str, why: str
) -> None:
    sid, rid, branch = seed(store, repo, kind)  # type: ignore[arg-type]
    before = head(repo)
    with client_for(store, repo) as client:
        info = client.get(url(sid, rid, "branch")).json()
        done = client.post(url(sid, rid, "merge"), json={"confirm": branch})
    assert info["merge_refusal"].startswith(why)
    assert done.status_code == 409
    assert done.json()["error"].startswith(why)
    assert head(repo) == before


@pytest.mark.parametrize("action", ["merge", "discard"])
@pytest.mark.parametrize("confirm", [None, "", "main", "saddle/auto/000000000000"])
def test_nothing_happens_without_a_confirm_naming_the_branch(
    store: SessionStore, repo: Path, action: str, confirm: str | None
) -> None:
    sid, rid, branch = seed(store, repo, "audited")
    before = head(repo)
    body = {} if confirm is None else {"confirm": confirm}
    with client_for(store, repo) as client:
        done = client.post(url(sid, rid, action), json=body)
    assert done.status_code == 400
    assert done.json()["error"].startswith("Not confirmed")
    assert head(repo) == before
    assert branch_actions.branch_exists(repo, branch)


def test_discard_is_refused_for_the_current_branch(store: SessionStore, repo: Path) -> None:
    sid, rid, branch = seed(store, repo, "audited")
    git(repo, "worktree", "remove", "--force", str(repo / ".saddle" / "worktrees" / rid))
    git(repo, "checkout", "-q", branch)
    with client_for(store, repo) as client:
        done = client.post(url(sid, rid, "discard"), json={"confirm": branch})
    assert done.status_code == 409
    assert "is the checkout's current branch" in done.json()["error"]
    assert branch_actions.branch_exists(repo, branch)


def test_discard_twice_says_the_branch_is_gone(store: SessionStore, repo: Path) -> None:
    sid, rid, branch = seed(store, repo, "audited")
    with client_for(store, repo) as client:
        client.post(url(sid, rid, "discard"), json={"confirm": branch})
        again = client.post(url(sid, rid, "discard"), json={"confirm": branch})
    assert again.status_code == 404
    assert "already discarded" in again.json()["error"]


def test_discard_leaves_a_branch_held_outside_the_run_worktrees(
    store: SessionStore, repo: Path, tmp_path: Path
) -> None:
    sid, rid, branch = seed(store, repo, "audited")
    git(repo, "worktree", "remove", "--force", str(repo / ".saddle" / "worktrees" / rid))
    mine = tmp_path / "mine"
    git(repo, "worktree", "add", "-q", str(mine), branch)
    with client_for(store, repo) as client:
        done = client.post(url(sid, rid, "discard"), json={"confirm": branch})
    assert done.status_code == 409
    assert "not a run worktree" in done.json()["error"]
    assert mine.is_dir()


def test_a_conflicting_cherry_pick_is_reported_verbatim_and_aborted(
    store: SessionStore, repo: Path
) -> None:
    sid, rid, branch = seed(store, repo, "audited")
    (repo / "calc.py").write_text("def add(a, b):\n    return b + a  # mine\n")
    git(repo, "commit", "-qam", "conflicting")
    before = head(repo)
    with client_for(store, repo) as client:
        done = client.post(url(sid, rid, "merge"), json={"confirm": branch})
    assert done.status_code == 409
    assert "CONFLICT" in done.json()["error"]
    assert head(repo) == before
    assert git(repo, "status", "--porcelain") == ""


def test_merge_is_refused_on_a_detached_head(store: SessionStore, repo: Path) -> None:
    sid, rid, branch = seed(store, repo, "audited")
    git(repo, "checkout", "-q", "--detach")
    with client_for(store, repo) as client:
        done = client.post(url(sid, rid, "merge"), json={"confirm": branch})
    assert done.status_code == 409
    assert "detached HEAD" in done.json()["error"]


def test_unknown_runs_and_folders_that_are_not_repos_are_404(
    store: SessionStore, repo: Path, tmp_path: Path
) -> None:
    sid, rid, _branch = seed(store, repo, "audited")
    with client_for(store, repo) as client:
        assert client.get(url(sid, "nope", "branch")).status_code == 404
        assert client.get(url(sid, "nope", "diff")).status_code == 404
        assert client.post(url(sid, "nope", "merge"), json={}).status_code == 404
        store.update(sid, workdir=str(tmp_path))
        gone = client.get(url(sid, rid, "diff"))
    assert gone.status_code == 404


def test_a_packet_without_a_run_branch_has_no_actions() -> None:
    packet = Packet("r", "t", "unrecorded", "", ("branch main",), ())
    with pytest.raises(branch_actions.ActionRefusedError, match="names no branch"):
        branch_actions.run_branch(packet)


def test_a_git_failure_is_reported_as_git_wrote_it(tmp_path: Path) -> None:
    with pytest.raises(branch_actions.ActionRefusedError, match="not a git repository"):
        branch_actions.dirty(tmp_path)


def test_merge_refusal_reads_rows_not_words() -> None:
    def packet(verdict: str, *rows: Row) -> Packet:
        return Packet("r", "t", verdict, "", (), rows)

    proven = Row("audit", "Audit", "proven", "1 of 1 finding passed.", ("h",))
    observed = Row("tests", "Tests", "observed", "ran pytest", ("h",))
    assert branch_actions.merge_refusal(packet("finished", proven)) == ""
    assert branch_actions.merge_refusal(packet("finished", observed)).startswith("No auditor")
    assert branch_actions.merge_refusal(packet("needs_you", proven)).startswith("The run is")


def test_a_real_scripted_run_merges_end_to_end(tmp_path: Path) -> None:
    """The same, on a branch and ledger `run_auto` made (scripted model, no GPU)."""
    from test_web_tasks import FIX, app_for, idle, wait_for

    from saddle.events import AuditFinding, Event
    from saddle.web.tasks import TaskRun

    repo = make_repo(tmp_path / "live")
    (repo / "pyproject.toml").write_text(
        '[tool.pytest.ini_options]\ntestpaths = ["tests"]\npythonpath = ["."]\n'
    )
    (repo / ".gitignore").write_text("__pycache__/\n.pytest_cache/\n")
    git(repo, "add", "-A")
    git(repo, "commit", "-qm", "config")
    store = SessionStore(tmp_path / "live-s")

    def auditor(_run: TaskRun):  # type: ignore[no-untyped-def]
        def audit(name: str, _arguments: str, result: str) -> list[Event]:
            if name == "run_command" and result.startswith("exit 0"):
                return [AuditFinding(gate="changed-line-coverage", ok=True, detail="covered")]
            return []

        return audit

    with app_for(store, repo, FIX, auditor=auditor) as (client, server):
        sid = client.post("/api/sessions").json()["id"]
        rid = client.post(f"/api/sessions/{sid}/task", json={"text": "make add add"}).json()[
            "run_id"
        ]
        wait_for(lambda: idle(server, sid))
        info = client.get(url(sid, rid, "branch")).json()
        files = [f["path"] for f in client.get(url(sid, rid, "diff")).json()["files"]]
        done = client.post(url(sid, rid, "merge"), json={"confirm": info["branch"]})
    assert server.tasks[rid].state == "finished"
    assert info["merge_refusal"] == ""
    assert files == ["calc.py"]
    assert done.status_code == 200, done.text
    assert "(a + b)" in (repo / "calc.py").read_text()


def test_discard_works_when_the_run_worktree_is_already_gone(
    store: SessionStore, repo: Path
) -> None:
    sid, rid, branch = seed(store, repo, "audited")
    git(repo, "worktree", "remove", "--force", str(repo / ".saddle" / "worktrees" / rid))
    with client_for(store, repo) as client:
        done = client.post(url(sid, rid, "discard"), json={"confirm": branch})
    assert done.status_code == 200, done.text
    assert not branch_actions.branch_exists(repo, branch)
