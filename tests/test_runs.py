"""The sidebar's Runs group, session names, delete-with-undo and the card's phase line.

REVIEW gaps 5, 6, 7, 32. Known-good and known-bad halves:

- a run's row is written to its session's run index and read back by a
  *new* server over the same store; a run the index says was going, that no
  live server holds, is reported not live (the page says "interrupted");
- `/api/runs` and the page list runs in every session, not only the one on
  screen;
- the first task names an unnamed session; a name the user chose survives
  a later task;
- deleting hides a session and keeps it on disk until the undo window has
  passed; it is gone after that, and never in one step;
- the phase follows the run's events and ledger, never a fixed word.
"""

from __future__ import annotations

import json
import os
import queue
import shutil
import subprocess
import time
from pathlib import Path
from typing import Any

import pytest
from starlette.testclient import TestClient
from test_ui3_mode import NoModel, _server_of, serving

from saddle import events
from saddle.auto import AutoError
from saddle.sessions import UNDO_DELETE_S, SessionStore
from saddle.titles import words_title
from saddle.web import tasks
from saddle.web.app import build_app
from saddle.web.tasks import TaskRun

HERE = Path(__file__).parent
CDP = HERE / "fixtures" / "runs_cdp.mjs"


def _fake_execute(hold: float = 0.0) -> Any:
    """`tasks.execute` without a model: phases, an optional question, then finished."""

    def execute(run: TaskRun, *, publish: Any, **_: Any) -> tuple[str, None]:
        run.phase, run.round = tasks.EDITING, 2
        publish(run.phase_event())
        if run.task.startswith("ask"):
            run.state = "needs_you"
            run.question = events.Question(id="q1", text="Which schema?", options=["v1"])
            publish(run.state_event())
            try:
                run.answers.get(timeout=30)
            except queue.Empty:  # pragma: no cover - only when a browser step hangs
                pass
            run.question = None
            run.state = "running"
            publish(run.state_event())
        deadline = time.monotonic() + hold
        while time.monotonic() < deadline and not run.cancelled:
            time.sleep(0.05)
        run.state = "stopped" if run.cancelled else "finished"
        publish(run.state_event("done"))
        return run.state, None

    return execute


def _wait(cond: Any, s: float = 5.0) -> None:
    deadline = time.monotonic() + s
    while not cond():
        assert time.monotonic() < deadline, "timed out"
        time.sleep(0.02)


def _start(client: TestClient, sid: str, text: str) -> str:
    rid: str = client.post(f"/api/sessions/{sid}/task", json={"text": text}).json()["run_id"]
    return rid


# -- run index ----------------------------------------------------------------


def test_the_run_index_survives_a_server_restart(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(tasks, "execute", _fake_execute())
    root = tmp_path / "s"
    store = SessionStore(root)
    sid = store.create(workdir=str(tmp_path)).id
    app = build_app(store, NoModel, default_workdir=tmp_path)
    with TestClient(app) as client:
        rid = _start(client, sid, "fix the parser")
        _wait(lambda: _server_of(app).tasks[rid].state == "finished")
        _wait(lambda: store.runs(sid) and store.runs(sid)[0]["state"] == "finished")
    # A run cut off mid-flight: the index says running, no server holds it.
    store.record_run(
        sid,
        {
            "run_id": "cut0ff",
            "task": "long job",
            "state": "running",
            "lane": "small",
            "started": 1.0,
            "ended": None,
            "state_since": 1.0,
        },
    )

    restarted = build_app(SessionStore(root), NoModel, default_workdir=tmp_path)
    with TestClient(restarted) as client:
        runs = {r["run_id"]: r for r in client.get("/api/runs").json()["runs"]}
    assert set(runs) == {rid, "cut0ff"}
    done = runs[rid]
    assert (done["state"], done["task"], done["session_id"], done["live"]) == (
        "finished",
        "fix the parser",
        sid,
        False,
    )
    assert done["ended"] is not None
    assert done["ended"] >= done["started"]
    assert done["lane"] == "small"
    assert (runs["cut0ff"]["state"], runs["cut0ff"]["live"]) == ("running", False)


def test_runs_in_every_session_are_listed_newest_first(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(tasks, "execute", _fake_execute())
    store = SessionStore(tmp_path / "s")
    a = store.create(title="a", workdir=str(tmp_path)).id
    b = store.create(title="b", workdir=str(tmp_path)).id
    app = build_app(store, NoModel, default_workdir=tmp_path)
    with TestClient(app) as client:
        r_b = _start(client, b, "ask which schema")
        _wait(lambda: _server_of(app).tasks[r_b].state == "needs_you")
        r_a = _start(client, a, "tidy the imports")
        _wait(lambda: _server_of(app).tasks[r_a].state == "finished")
        got = client.get("/api/runs").json()["runs"]
        client.post(f"/api/tasks/{r_b}/answer", json={"text": "v1"})
        _wait(lambda: _server_of(app).tasks[r_b].state == "finished")
    assert [(r["run_id"], r["session_id"], r["state"], r["live"]) for r in got] == [
        (r_a, a, "finished", True),
        (r_b, b, "needs_you", True),
    ]
    assert got[1]["phase"] == tasks.WAITING_FOR_YOU or got[1]["phase"] == tasks.EDITING
    assert got[1]["state_since"] >= got[1]["started"]


def test_a_trashed_sessions_runs_are_not_listed(tmp_path: Path) -> None:
    store = SessionStore(tmp_path / "s")
    sid = store.create(workdir=str(tmp_path)).id
    store.record_run(sid, {"run_id": "r1", "task": "t", "state": "finished", "started": 1.0})
    store.record_run(sid, {"run_id": "r0"})
    rows = store.runs(sid)
    rows.append({"task": "a row with no id is skipped"})
    (tmp_path / "s" / sid / "runs.json").write_text(json.dumps(rows), encoding="utf-8")
    app = build_app(store, NoModel, default_workdir=tmp_path)
    _server_of(app).tasks["r2"] = TaskRun(
        run_id="r2", session_id=sid, task="live", time_budget_s=1, token_budget=1
    )
    with TestClient(app) as client:
        assert [r["run_id"] for r in client.get("/api/runs").json()["runs"]] == ["r2", "r1", "r0"]
        client.delete(f"/api/sessions/{sid}")
        assert client.get("/api/runs").json()["runs"] == []


def test_a_row_for_a_purged_session_is_dropped_not_raised(tmp_path: Path) -> None:
    store = SessionStore(tmp_path / "s")
    sid = store.create(workdir=str(tmp_path)).id
    store.delete(sid)
    store.record_run(sid, {"run_id": "r1", "task": "t", "state": "running"})
    assert not (tmp_path / "s" / sid).exists()
    (tmp_path / "s" / sid).mkdir()
    (tmp_path / "s" / sid / "runs.json").write_text("{not json", encoding="utf-8")
    assert store.runs(sid) == []
    (tmp_path / "s" / sid / "runs.json").write_text('{"a": 1}', encoding="utf-8")
    assert store.runs(sid) == []


def test_a_failed_index_write_does_not_kill_the_run(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(tasks, "execute", _fake_execute())
    store = SessionStore(tmp_path / "s")
    sid = store.create(workdir=str(tmp_path)).id

    def broken(*_: Any) -> None:
        msg = "disk full"
        raise OSError(msg)

    monkeypatch.setattr(store, "record_run", broken)
    app = build_app(store, NoModel, default_workdir=tmp_path)
    with TestClient(app) as client:
        rid = _start(client, sid, "fix it")
        _wait(lambda: _server_of(app).tasks[rid].state == "finished")


# -- names --------------------------------------------------------------------


def test_words_title() -> None:
    assert words_title("fix the parser so it accepts nested lists please") == (
        "fix the parser so it accepts…"
    )
    assert words_title("  tidy imports.  ") == "tidy imports"
    assert words_title("\n\n") == ""


def test_a_finished_run_names_its_session(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(tasks, "execute", _fake_execute())
    store = SessionStore(tmp_path / "s")
    sid = store.create(workdir=str(tmp_path)).id
    app = build_app(store, NoModel, default_workdir=tmp_path)
    with TestClient(app) as client:
        assert store.get(sid).title == "New session"
        rid = _start(client, sid, "make the score function return zero for None input")
        _wait(lambda: _server_of(app).tasks[rid].state == "finished")
        rows = {r["id"]: r for r in client.get("/api/sessions").json()}
        # A second task does not rename it again.
        rid2 = _start(client, sid, "and now something else entirely different here")
        _wait(lambda: _server_of(app).tasks[rid2].state == "finished")
    assert rows[sid]["title"] == "make the score function return zero…"
    assert rows[sid]["title"] != "New session"
    assert store.get(sid).title == "make the score function return zero…"
    assert store.get(sid).auto_title is False


def test_a_name_the_user_chose_is_not_overwritten_by_a_task(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(tasks, "execute", _fake_execute())
    store = SessionStore(tmp_path / "s")
    sid = store.create(workdir=str(tmp_path)).id
    app = build_app(store, NoModel, default_workdir=tmp_path)
    with TestClient(app) as client:
        client.patch(f"/api/sessions/{sid}", json={"title": "Parser work"})
        rid = _start(client, sid, "fix the parser")
        _wait(lambda: _server_of(app).tasks[rid].state == "finished")
    assert store.get(sid).title == "Parser work"


def test_a_session_made_with_a_name_keeps_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(tasks, "execute", _fake_execute())
    store = SessionStore(tmp_path / "s")
    sid = store.create(title="Named at birth", workdir=str(tmp_path)).id
    app = build_app(store, NoModel, default_workdir=tmp_path)
    with TestClient(app) as client:
        rid = _start(client, sid, "fix the parser")
        _wait(lambda: _server_of(app).tasks[rid].state == "finished")
    assert store.get(sid).title == "Named at birth"


def test_a_task_with_no_words_is_refused_and_names_nothing(tmp_path: Path) -> None:
    store = SessionStore(tmp_path / "s")
    sid = store.create(workdir=str(tmp_path)).id
    app = build_app(store, NoModel, default_workdir=tmp_path)
    server = _server_of(app)
    server._name_from_task(sid, "   ")
    assert store.get(sid).title == "New session"
    assert store.get(sid).auto_title is True


# -- delete with undo ---------------------------------------------------------


def test_delete_hides_then_purges_only_after_the_undo_window(tmp_path: Path) -> None:
    store = SessionStore(tmp_path / "s")
    sid = store.create(title="keep me", workdir=str(tmp_path)).id
    app = build_app(store, NoModel, default_workdir=tmp_path)
    with TestClient(app) as client:
        assert client.delete(f"/api/sessions/{sid}").json() == {"ok": True}
        # Hidden from the list, still on disk.
        assert sid not in {r["id"] for r in client.get("/api/sessions").json()}
        assert (tmp_path / "s" / sid / "session.json").is_file()
        # Undo brings it back whole.
        assert client.post(f"/api/sessions/{sid}/restore").json()["title"] == "keep me"
        assert sid in {r["id"] for r in client.get("/api/sessions").json()}
        # A session that was never hidden cannot be removed in one step.
        assert client.delete(f"/api/sessions/{sid}?now=1").status_code == 409
        assert (tmp_path / "s" / sid).is_dir()
        # Hidden, then the page's toast runs out: gone.
        client.delete(f"/api/sessions/{sid}")
        assert client.delete(f"/api/sessions/{sid}?now=1").json() == {"ok": True}
    assert not (tmp_path / "s" / sid).exists()


def test_purge_expired_keeps_a_session_inside_the_window(tmp_path: Path) -> None:
    store = SessionStore(tmp_path / "s")
    sid = store.create(workdir=str(tmp_path)).id
    keep = store.create(workdir=str(tmp_path)).id
    store.update(keep, title="kept")
    at = store.trash(sid, now=1000.0).deleted_at
    assert at == 1000.0
    assert store.purge_expired(now=1000.0 + UNDO_DELETE_S - 0.5) == set()
    assert (tmp_path / "s" / sid).is_dir()
    assert store.purge_expired(now=1000.0 + UNDO_DELETE_S) == {sid}
    assert not (tmp_path / "s" / sid).exists()
    assert [s.id for s in store.list()] == [keep]


# -- phase --------------------------------------------------------------------


@pytest.mark.parametrize(
    ("name", "kind", "argv", "code", "want"),
    [
        ("question", "agent", [], 0, "waiting for you"),
        ("answer", "agent", [], 0, "working"),
        ("refused:write_file", "tool", ["write_file", "{}"], 1, "audit refused"),
        ("audit-tier1:coverage", "agent", [], 1, "audit refused"),
        ("audit-tier1:coverage", "agent", [], 0, "working"),
        ("audit:delivered", "agent", [], 0, "working"),
        ("edit_file", "tool", ["edit_file", "{}"], 0, "waiting for audit"),
        ("run_command", "tool", ["run_command", "{}"], 0, "working"),
        ("auto:start", "agent", [], 0, None),
    ],
)
def test_phase_for_a_sealed_entry(
    name: str, kind: str, argv: list[str], code: int, want: str | None
) -> None:
    assert tasks.phase_for(name, kind, argv, code) == want


@pytest.mark.parametrize(
    ("tool", "args", "want"),
    [
        ("edit_file", '{"path": "a.py"}', "editing"),
        ("write_file", '{"path": "a.py"}', "editing"),
        ("run_command", '{"command": "python -m pytest -q"}', "running tests"),
        ("run_command", '{"command": "ls"}', "working"),
        ("read_file", '{"path": "a.py"}', "working"),
    ],
)
def test_tool_phase(tool: str, args: str, want: str) -> None:
    assert tasks.tool_phase(tool, args) == want


def test_execute_publishes_the_phase_its_events_put_the_run_in(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    run = TaskRun(run_id="r1", session_id="s", task="t", time_budget_s=60, token_budget=10)
    progress = events.RunProgress(elapsed_s=1, time_budget_s=60, tokens=5, token_budget=10)

    def fake_auto(_options: Any, _client: Any, *, on_event: Any, **_: Any) -> None:
        on_event(events.ReasoningDelta(text="hmm"))
        on_event(events.ToolStart(id="1", name="edit_file", arguments="{}", present="Editing"))
        on_event(progress)
        on_event(
            events.ToolStart(
                id="2", name="run_command", arguments='{"command": "pytest"}', present="Running"
            )
        )
        on_event(events.Question(id="q", text="?", options=[]))
        msg = "stop here"
        raise AutoError(msg)

    monkeypatch.setattr(tasks, "run_auto", fake_auto)
    published: list[Any] = []
    tasks.execute(
        run,
        workdir=repo,
        client=None,
        publish=published.append,
        chat_journal=tmp_path / "chat.jsonl",
        reasoning_effort="none",
        audit=None,
    )
    phases = [(e.phase, e.round) for e in published if isinstance(e, events.TaskPhase)]
    assert phases == [
        ("editing", 1),
        ("editing", 2),
        ("running tests", 2),
        ("waiting for you", 2),
    ]


def test_the_ledger_moves_the_phase(tmp_path: Path) -> None:
    from saddle.journal import append_span, build_span

    journal = tmp_path / "proofs.jsonl"
    run = TaskRun(
        run_id="r1", session_id="s", task="t", time_budget_s=60, token_budget=10, journal=journal
    )
    append_span(
        journal,
        build_span(
            node_id="n",
            argv=["edit_file", "{}"],
            duration_ms=1,
            exit_code=0,
            detail="ok",
            kind="tool",
            name="edit_file",
        ),
    )
    run.new_lines()
    assert run.phase == "waiting for audit"
    append_span(
        journal,
        build_span(
            node_id="n",
            argv=["audit"],
            duration_ms=1,
            exit_code=1,
            detail="x",
            kind="agent",
            name="audit:coverage",
        ),
    )
    run.new_lines()
    assert run.phase == "audit refused"


# -- the browser --------------------------------------------------------------

BROWSER = shutil.which("node") and shutil.which("google-chrome")
needs_browser = pytest.mark.skipif(not BROWSER, reason="needs node and google-chrome")


def _page(tmp_path: Path, step: str, monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    monkeypatch.setattr(tasks, "execute", _fake_execute(hold=8.0))
    store = SessionStore(tmp_path / "s")
    app = build_app(store, NoModel, default_workdir=tmp_path)
    a = store.create(title="parser work", workdir=str(tmp_path)).id
    b = store.create(title="schema work", workdir=str(tmp_path)).id
    store.update(a, auto_title=False)
    store.update(b, auto_title=False)
    args = ["node", str(CDP), "BASE", a, step, b]
    shots = os.environ.get("RUNS_SHOTS")
    with serving(app) as base:
        args[2] = base
        if shots:
            args.append(shots)
        out = subprocess.run(args, capture_output=True, text=True, timeout=120, check=False)
        for run in _server_of(app).tasks.values():
            run.cancelled = True
            run.answers.put("done")
    assert out.returncode == 0, out.stderr
    got: dict[str, Any] = json.loads(out.stdout.strip().splitlines()[-1])
    got["a"], got["b"], got["root"] = a, b, tmp_path / "s"
    return got


@needs_browser
def test_a_run_in_another_session_is_in_the_runs_group(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    got = _page(tmp_path, "runs", monkeypatch)
    a, b = got["a"], got["b"]
    rows = got["rows"]
    assert got["active"] == a
    assert [(r["sid"], r["state"]) for r in rows] == [(a, "running"), (b, "needs_you")]
    assert rows[1]["task"] == "ask which schema the loader should…"
    assert rows[1]["time"].startswith("waiting ")
    assert rows[0]["lane"] == "small"
    # A click goes to the other session and to its card.
    assert got["jumped"] == {"active": b, "card": True}


@needs_browser
def test_the_running_card_says_its_phase_and_ages_on_its_own(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    got = _page(tmp_path, "phase", monkeypatch)
    first, later = got["first"], got["later"]
    assert first.startswith("editing · round 2 · last event ")
    assert later.startswith("editing · round 2 · last event ")

    def age(text: str) -> int:
        return int(text.rsplit("event ", 1)[1].split("s", 1)[0])

    assert age(later) >= age(first) + 2
    assert got["fetches"] == 0
    assert "thinking" not in later


@needs_browser
def test_delete_offers_undo_and_undo_keeps_the_session(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    got = _page(tmp_path, "undo", monkeypatch)
    b = got["b"]
    assert got["toast"]["shown"] is True
    assert got["toast"]["focus"] == "Undo"
    assert b not in got["toast"]["listed"]
    assert b in got["afterUndo"]["listed"]
    assert (got["root"] / b / "session.json").is_file()
    assert json.loads((got["root"] / b / "session.json").read_text())["deleted_at"] is None


@needs_browser
def test_delete_is_final_once_the_toast_runs_out(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    got = _page(tmp_path, "expire", monkeypatch)
    assert got["toastGone"] is True
    assert not (got["root"] / got["b"]).exists()
