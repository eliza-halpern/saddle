"""A run's row in `runs.json` says where the run lives and which commits are its own.

A finished run once sat in its session's index as six fields (id, task, state,
lane and times). Two review commits were then made by hand on top of its
branch, and nothing in the session said where the run's worktree was, which
branch it used, or which commit it ended on.

Known-good: a run started through the web path ends with a row carrying the
worktree, branch, base commit and final commit, and those are the real ones
(the directory exists, the branch resolves to the commit). Known-bad: a row
written before the fields existed still loads and lists, a run still going
has no `commit` yet, and the packet the run is judged by never prints the
worktree's absolute path.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

from starlette.testclient import TestClient
from test_ui3_mode import NoModel, _server_of
from test_web_tasks import FIX, READ, app_for, idle, repo, store, wait_for

from saddle.sessions import SessionStore
from saddle.web.app import build_app
from saddle.web.tasks import TaskRun

__all__ = ["repo", "store"]  # fixtures re-exported for pytest


def rev(repo_path: Path, ref: str) -> str:
    done = subprocess.run(
        ["git", "-C", str(repo_path), "rev-parse", ref], capture_output=True, text=True, check=True
    )
    return done.stdout.strip()


def test_a_web_run_ends_with_worktree_branch_base_and_commit(
    store: SessionStore, repo: Path
) -> None:
    started_from = rev(repo, "HEAD")
    with app_for(store, repo, FIX) as (client, server):
        sid = client.post("/api/sessions").json()["id"]
        rid = client.post(f"/api/sessions/{sid}/task", json={"text": "make add add"}).json()[
            "run_id"
        ]
        wait_for(lambda: idle(server, sid))
        listed = client.get("/api/runs").json()["runs"]
    [row] = store.runs(sid)
    assert row["state"] == "finished"
    worktree = Path(row["worktree"])
    assert worktree.is_absolute()
    assert (worktree / "calc.py").is_file()  # the directory is the run's real worktree
    assert row["branch"] == f"saddle/auto/{rid}"
    assert row["base"] == started_from  # a sha, not the name `main`
    assert row["commit"] == rev(repo, row["branch"])
    assert row["commit"] != row["base"]
    served = next(r for r in listed if r["run_id"] == rid)
    fields = ("worktree", "branch", "base", "commit")
    assert {k: row[k] for k in fields} == {k: served[k] for k in fields}


def test_a_run_still_going_says_where_it_is_but_has_no_commit_yet(
    store: SessionStore, repo: Path
) -> None:
    with app_for(store, repo, [], tail=READ) as (client, server):
        sid = client.post("/api/sessions").json()["id"]
        rid = client.post(f"/api/sessions/{sid}/task", json={"text": "t"}).json()["run_id"]
        wait_for(lambda: bool(store.runs(sid)) and "base" in store.runs(sid)[0])
        mid = store.runs(sid)[0]
        client.post(f"/api/tasks/{rid}/stop")
        wait_for(lambda: idle(server, sid))
    assert mid["state"] == "running"
    assert mid["branch"] == f"saddle/auto/{rid}"
    assert Path(mid["worktree"]).is_dir()
    assert "commit" not in mid  # nothing has been committed while the run goes
    assert store.runs(sid)[0]["commit"] == rev(repo, mid["branch"])  # and the stop commits one


def test_a_row_written_before_the_fields_existed_still_loads_and_lists(tmp_path: Path) -> None:
    store = SessionStore(tmp_path / "s")
    sid = store.create(workdir=str(tmp_path)).id
    legacy = {
        "run_id": "old1",
        "task": "an old task",
        "state": "finished",
        "lane": "small",
        "started": 1.0,
        "ended": 2.0,
        "state_since": 2.0,
    }
    (tmp_path / "s" / sid / "runs.json").write_text(json.dumps([legacy]), encoding="utf-8")
    assert store.runs(sid) == [legacy]
    app = build_app(store, NoModel, default_workdir=tmp_path)
    live = TaskRun(run_id="new1", session_id=sid, task="live", time_budget_s=1, token_budget=1)
    live.worktree, live.branch, live.base = tmp_path / "w", "saddle/auto/new1", "a" * 40
    _server_of(app).tasks["new1"] = live
    with TestClient(app) as client:
        rows = {r["run_id"]: r for r in client.get("/api/runs").json()["runs"]}
    assert rows["old1"]["task"] == "an old task"
    assert not {"worktree", "branch", "base", "commit"} & set(rows["old1"])
    assert rows["new1"]["branch"] == "saddle/auto/new1"
    assert "commit" not in rows["new1"]  # an unset field is left out, not written empty
    # A later write merges over the old row: it adds fields and blanks none.
    store.record_run(sid, {"run_id": "old1", "branch": "b"})
    [row] = store.runs(sid)
    assert row["task"] == "an old task"
    assert row["branch"] == "b"


def test_the_packet_does_not_print_the_worktrees_absolute_path(
    store: SessionStore, repo: Path
) -> None:
    with app_for(store, repo, FIX) as (client, server):
        sid = client.post("/api/sessions").json()["id"]
        rid = client.post(f"/api/sessions/{sid}/task", json={"text": "make add add"}).json()[
            "run_id"
        ]
        wait_for(lambda: idle(server, sid))
        packet = client.get(f"/api/sessions/{sid}/tasks/{rid}/packet").json()
    shown = json.dumps(packet) + server.tasks[rid].state_event().task
    assert str(store.runs(sid)[0]["worktree"]) not in shown
    assert str(repo) not in shown
