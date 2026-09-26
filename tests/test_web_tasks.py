"""Tasks started from the chat: `saddle auto`'s own path, watched from a card.

Known-good: a scripted client drives a task from the web endpoint to
finished; the web path calls `saddle.auto.run_auto` (the function the CLI
calls) and no loop of its own; the chat journal gets exactly one run
reference; every cite in the served packet is a ledger record; a question
round-trips and its answer is sealed in the ledger as a child of it.

Known-bad: an answer to a task that is not waiting is refused; a run with no
outcome record is never reported finished; a crafted run id cannot read
another session's ledger.
"""

from __future__ import annotations

import json
import subprocess
import threading
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

import pytest
from starlette.testclient import TestClient

import saddle.auto
from saddle.events import AuditFinding, Event, Question, TaskState
from saddle.journal import read_entries, read_spans
from saddle.sessions import SessionStore
from saddle.vllm import ToolCall
from saddle.web import tasks
from saddle.web.app import ChatServer, build_app
from saddle.web.tasks import RECAP_PREFIX, RUN_REF, TaskRun, execute, journal_for

TEST = "from calc import add\n\n\ndef test_add():\n    assert add(2, 2) == 4\n"


def git(repo: Path, *args: str) -> None:
    subprocess.run(
        ["git", "-C", str(repo), "-c", "user.name=t", "-c", "user.email=t@t", *args],
        check=True,
        capture_output=True,
    )


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    root = tmp_path / "repo"
    (root / "tests").mkdir(parents=True)
    (root / "calc.py").write_text("def add(a, b):\n    return a - b\n")
    (root / "tests" / "test_calc.py").write_text(TEST)
    (root / "pyproject.toml").write_text(
        '[tool.pytest.ini_options]\ntestpaths = ["tests"]\npythonpath = ["."]\n'
    )
    (root / ".gitignore").write_text("__pycache__/\n.pytest_cache/\n")
    git(root, "init", "-q", "-b", "main")
    git(root, "add", "-A")
    git(root, "commit", "-q", "-m", "init")
    return root


def call(name: str, cid: str, **arguments: Any) -> ToolCall:
    return ToolCall(id=cid, name=name, arguments=json.dumps(arguments))


FIX = [
    [call("edit_file", "c1", path="calc.py", old="a - b", new="(a + b)")],
    [call("run_command", "c2", command="python -m pytest -q")],
    [call("finish", "c3", summary="add adds now.")],
]
READ = [call("read_file", "r", path="calc.py")]


class Scripted:
    def __init__(self, rounds: list[list[Any]], tail: list[Any] | None = None) -> None:
        self.rounds = [list(r) for r in rounds]
        self.tail = tail

    def __enter__(self) -> Scripted:
        return self

    def __exit__(self, *_: object) -> None:
        return None

    def max_model_len(self) -> int:
        return 200_000

    def stream_chat(self, messages: Any, **_: Any) -> Iterator[Any]:
        if self.rounds:
            return iter(self.rounds.pop(0))
        if self.tail is not None:
            time.sleep(0.02)
            return iter(list(self.tail))
        return iter([call("finish", "z", summary="done")])


def asking(run: TaskRun) -> Callable[[str, str, str], list[Event]]:
    def audit(name: str, arguments: str, result: str) -> list[Event]:
        if name == "edit_file":
            return [Question(id="q1", text="Should add(0, 0) be 0?", options=["yes", "no"])]
        if name == "run_command" and result.startswith("exit 0"):
            return [AuditFinding(gate="changed-line-coverage", ok=True, detail="covered")]
        return []

    return audit


@contextmanager
def app_for(
    store: SessionStore,
    repo: Path,
    rounds: list[list[Any]],
    *,
    tail: list[Any] | None = None,
    auditor: Any = None,
) -> Iterator[tuple[TestClient, ChatServer]]:
    app = build_app(
        store, lambda: Scripted(rounds, tail), default_workdir=repo, auditor=auditor, arm="E"
    )
    server = next(
        cell.cell_contents
        for route in app.routes  # type: ignore[attr-defined]
        for cell in (getattr(getattr(route, "endpoint", None), "__closure__", None) or ())
        if isinstance(cell.cell_contents, ChatServer)
    )
    with TestClient(app) as client:
        yield client, server


def wait_for(predicate: Callable[[], bool], timeout: float = 30) -> None:
    deadline = time.monotonic() + timeout
    while not predicate():
        if time.monotonic() > deadline:
            msg = "timed out"
            raise AssertionError(msg)
        time.sleep(0.02)


def idle(server: ChatServer, sid: str) -> bool:
    return not server._live(sid).busy


@pytest.fixture
def store(tmp_path: Path) -> SessionStore:
    return SessionStore(tmp_path / "sessions")


# -- known-good ---------------------------------------------------------------


def test_a_task_runs_to_finished_on_the_clis_own_path(
    store: SessionStore, repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[Any] = []
    real = saddle.auto.run_auto

    def spy(*args: Any, **kwargs: Any) -> Any:
        calls.append(args[0])
        return real(*args, **kwargs)

    monkeypatch.setattr(tasks, "run_auto", spy)
    with app_for(store, repo, FIX) as (client, server):
        sid = client.post("/api/sessions").json()["id"]
        rid = client.post(f"/api/sessions/{sid}/task", json={"text": "make add add"}).json()[
            "run_id"
        ]
        wait_for(lambda: idle(server, sid))
        run = server.tasks[rid]
        packet = client.get(f"/api/sessions/{sid}/tasks/{rid}/packet").json()

    assert len(calls) == 1
    assert calls[0].task == "make add add"
    assert calls[0].run_id == rid
    assert run.state == "finished"
    assert packet["verdict"] == "finished"
    assert run.journal is not None
    ledger = {e.record_hash for e in read_entries(run.journal)}
    cites = {c for row in packet["rows"] for c in row["cites"]}
    assert cites
    assert cites <= ledger
    assert all(line.cite in ledger for line in run.lines)
    refs = [s for s in read_spans(store.journal_path(sid)) if s.name == RUN_REF]
    assert len(refs) == 1
    assert refs[0].argv[1] == rid
    assert refs[0].exit_code == 0
    messages = store.load_messages(sid)
    assert messages[-1]["role"] == "user"
    assert messages[-1]["content"].startswith(f"{RECAP_PREFIX}{rid}]")
    assert "add adds now" not in messages[-1]["content"]  # the recap, not the narrative


def test_a_question_round_trips_and_the_answer_is_sealed(store: SessionStore, repo: Path) -> None:
    with app_for(store, repo, FIX, auditor=asking) as (client, server):
        sid = client.post("/api/sessions").json()["id"]
        rid = client.post(f"/api/sessions/{sid}/task", json={"text": "t"}).json()["run_id"]
        wait_for(lambda: rid in server.tasks and server.tasks[rid].state == "needs_you")
        run = server.tasks[rid]
        assert run.state_event().question == {
            "id": "q1",
            "text": "Should add(0, 0) be 0?",
            "options": ["yes", "no"],
        }
        assert client.post(f"/api/tasks/{rid}/answer", json={"text": " "}).status_code == 400
        assert client.post(f"/api/tasks/{rid}/answer", json={"text": "yes"}).json() == {"ok": True}
        wait_for(lambda: idle(server, sid))
        late = client.post(f"/api/tasks/{rid}/answer", json={"text": "no"})

    assert late.status_code == 409  # known-bad: not waiting on you any more
    assert run.state == "finished"
    assert run.journal is not None
    spans = read_spans(run.journal)
    question = next(s for s in spans if s.name == "question")
    answer = next(s for s in spans if s.name == "answer")
    assert answer.parent_id == question.span_id
    assert answer.detail == "yes"
    assert [s.name for s in spans].count("answer") == 1


def test_stopping_a_task_ends_it_stopped_with_a_record(store: SessionStore, repo: Path) -> None:
    with app_for(store, repo, [], tail=READ) as (client, server):
        sid = client.post("/api/sessions").json()["id"]
        rid = client.post(f"/api/sessions/{sid}/task", json={"text": "t"}).json()["run_id"]
        busy = client.post(f"/api/sessions/{sid}/task", json={"text": "again"})
        wait_for(lambda: len(server.tasks[rid].lines) >= 2)
        assert client.post(f"/api/tasks/{rid}/stop").json() == {"stopping": True}
        wait_for(lambda: idle(server, sid))
        after = client.post(f"/api/tasks/{rid}/stop").json()

    assert busy.status_code == 409
    assert after == {"stopping": False}
    assert server.tasks[rid].state == "stopped"


def test_a_stopped_question_is_a_stop_not_a_finish(store: SessionStore, repo: Path) -> None:
    with app_for(store, repo, FIX, auditor=asking) as (client, server):
        sid = client.post("/api/sessions").json()["id"]
        rid = client.post(f"/api/sessions/{sid}/task", json={"text": "t"}).json()["run_id"]
        wait_for(lambda: rid in server.tasks and server.tasks[rid].state == "needs_you")
        client.post(f"/api/tasks/{rid}/stop")
        wait_for(lambda: idle(server, sid))
        packet = client.get(f"/api/sessions/{sid}/tasks/{rid}/packet").json()
    assert server.tasks[rid].state == "stopped"
    assert packet["verdict"] == "stopped"


def test_a_restarted_server_serves_the_packet_from_the_chat_journal_only(
    store: SessionStore, repo: Path
) -> None:
    with app_for(store, repo, FIX) as (client, server):
        sid = client.post("/api/sessions").json()["id"]
        other = client.post("/api/sessions", json={"reuse_unstarted": False}).json()["id"]
        rid = client.post(f"/api/sessions/{sid}/task", json={"text": "t"}).json()["run_id"]
        wait_for(lambda: idle(server, sid))
    with app_for(store, repo, []) as (client, _server):
        mine = client.get(f"/api/sessions/{sid}/tasks/{rid}/packet")
        theirs = client.get(f"/api/sessions/{other}/tasks/{rid}/packet")
    assert mine.json()["verdict"] == "finished"
    assert theirs.status_code == 404  # known-bad: a run id is not a path


# -- the request boundary -------------------------------------------------------


@pytest.mark.parametrize(
    ("body", "status"),
    [
        ({"text": ""}, 400),
        ({"text": "t", "time_budget_s": "soon"}, 400),
        ({"text": "t", "token_budget": -1}, 400),
    ],
)
def test_a_bad_task_request_is_refused(
    store: SessionStore, repo: Path, body: dict[str, Any], status: int
) -> None:
    with app_for(store, repo, []) as (client, _server):
        sid = client.post("/api/sessions").json()["id"]
        assert client.post(f"/api/sessions/{sid}/task", json=body).status_code == status


def test_unknown_tasks_are_404(store: SessionStore, repo: Path) -> None:
    with app_for(store, repo, []) as (client, _server):
        assert client.post("/api/tasks/nope/answer", json={"text": "x"}).status_code == 404
        assert client.post("/api/tasks/nope/stop").status_code == 404


# -- failure paths: never a finished card without a finish record ---------------


def test_a_folder_that_is_not_a_repo_fails_the_task(store: SessionStore, tmp_path: Path) -> None:
    plain = tmp_path / "plain"
    plain.mkdir()
    with app_for(store, plain, FIX) as (client, server):
        sid = client.post("/api/sessions").json()["id"]
        rid = client.post(f"/api/sessions/{sid}/task", json={"text": "t"}).json()["run_id"]
        wait_for(lambda: idle(server, sid))
    assert server.tasks[rid].state == "failed"
    assert (
        not any(s.name == RUN_REF for s in read_spans(store.journal_path(sid)))
        if (store.journal_path(sid).is_file())
        else True
    )


def test_an_auto_error_fails_the_task(
    repo: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def refuse(*_a: Any, **_k: Any) -> Any:
        msg = "worktree exists"
        raise saddle.auto.AutoError(msg)

    monkeypatch.setattr(tasks, "run_auto", refuse)
    published: list[Any] = []
    run = TaskRun(run_id="r", session_id="s", task="t", time_budget_s=60, token_budget=100)
    verdict, recap = execute(
        run,
        workdir=repo,
        client=None,
        publish=published.append,
        chat_journal=tmp_path / "chat.jsonl",
        reasoning_effort="low",
        audit=None,
    )
    assert (verdict, recap, run.state) == ("failed", None, "failed")
    assert isinstance(published[-1], TaskState)
    assert "worktree exists" in published[-1].detail


def test_a_run_that_leaves_no_outcome_record_is_failed_not_finished(
    repo: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(tasks, "run_auto", lambda *_a, **_k: None)  # writes nothing
    run = TaskRun(run_id="r", session_id="s", task="t", time_budget_s=60, token_budget=100)
    verdict, recap = execute(
        run,
        workdir=repo,
        client=None,
        publish=lambda _e: None,
        chat_journal=tmp_path / "chat.jsonl",
        reasoning_effort="low",
        audit=None,
    )
    assert verdict == "unrecorded"
    assert run.state == "failed"
    assert recap is not None
    assert "verdict: unrecorded" in recap["content"]
    ref = read_spans(tmp_path / "chat.jsonl")[0]
    assert ref.exit_code == 1
    assert ref.argv[4] == ""


def test_a_crash_inside_a_task_fails_the_card_and_frees_the_session(
    store: SessionStore, repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def boom(*_a: Any, **_k: Any) -> Any:
        msg = "kaput"
        raise RuntimeError(msg)

    monkeypatch.setattr(tasks, "execute", boom)
    with app_for(store, repo, []) as (client, server):
        sid = client.post("/api/sessions").json()["id"]
        rid = client.post(f"/api/sessions/{sid}/task", json={"text": "t"}).json()["run_id"]
        wait_for(lambda: idle(server, sid))
    assert server.tasks[rid].state == "failed"


def test_journal_for_reads_only_run_refs(tmp_path: Path) -> None:
    assert journal_for(tmp_path / "missing.jsonl", "r") is None


def test_wait_for_answer_gives_up_when_stopped() -> None:
    run = TaskRun(run_id="r", session_id="s", task="t", time_budget_s=1, token_budget=1)
    threading.Timer(0.3, lambda: setattr(run, "cancelled", True)).start()
    assert run.wait_for_answer(Question(id="q", text="?")) is None
    run.answers.put("yes")
    run.cancelled = False
    assert run.wait_for_answer(Question(id="q", text="?")) == "yes"


def test_new_lines_is_empty_before_the_ledger_exists(tmp_path: Path) -> None:
    run = TaskRun(run_id="r", session_id="s", task="t", time_budget_s=1, token_budget=1)
    assert run.new_lines() == []
    run.journal = tmp_path / "none.jsonl"
    assert run.new_lines() == []


def test_journal_for_skips_other_spans_and_other_runs(tmp_path: Path) -> None:
    from saddle.journal import append_span, build_span

    chat = tmp_path / "chat.jsonl"
    append_span(
        chat,
        build_span(
            node_id="c",
            argv=["x"],
            duration_ms=0,
            exit_code=0,
            detail="",
            kind="agent",
            name="other",
        ),
    )
    run = TaskRun(run_id="r1", session_id="s", task="t", time_budget_s=1, token_budget=1)
    run.journal = tmp_path / "r1.jsonl"
    append_span(chat, tasks.run_ref_span(run, "finished", "ok", ""))
    assert journal_for(chat, "r2") is None
    assert journal_for(chat, "r1") == tmp_path / "r1.jsonl"


def test_lines_written_after_the_last_event_still_reach_the_card(
    repo: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from saddle.journal import append_plan, append_span, build_plan, build_span

    def silent(options: Any, _client: Any, **_k: Any) -> None:
        journal = saddle.auto.ledger_path(repo, options.run_id)
        journal.parent.mkdir(parents=True)
        append_plan(journal, build_plan([], task_hash="t"))  # a plan draws no line
        append_span(
            journal,
            build_span(
                node_id="auto",
                argv=["auto:finished"],
                duration_ms=0,
                exit_code=0,
                detail="done",
                kind="agent",
                name="auto:finished",
            ),
        )

    monkeypatch.setattr(tasks, "run_auto", silent)
    published: list[Any] = []
    run = TaskRun(run_id="r", session_id="s", task="t", time_budget_s=60, token_budget=100)
    execute(
        run,
        workdir=repo,
        client=None,
        publish=published.append,
        chat_journal=tmp_path / "chat.jsonl",
        reasoning_effort="low",
        audit=None,
    )
    lines = [e for e in published if e.kind == "task.line"]
    assert [line.text for line in lines] == ["finished · done"]


# -- a page that reconnects mid-run gets its card back -------------------------


@contextmanager
def serving(app: Any) -> Iterator[str]:
    import uvicorn

    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=0, log_level="error"))
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    try:
        wait_for(lambda: server.started, 10)
        yield f"http://127.0.0.1:{server.servers[0].sockets[0].getsockname()[1]}"
    finally:
        server.should_exit = True
        thread.join(timeout=10)


def test_a_reconnecting_page_gets_the_running_cards_state_lines_and_spend(
    store: SessionStore, repo: Path
) -> None:
    import httpx

    app = build_app(store, lambda: Scripted(FIX), default_workdir=repo, auditor=asking, arm="E")
    with serving(app) as base, httpx.Client(base_url=base, timeout=15) as client:
        sid = client.post("/api/sessions").json()["id"]
        other = client.post("/api/sessions", json={"reuse_unstarted": False}).json()["id"]
        rid = client.post(f"/api/sessions/{sid}/task", json={"text": "t"}).json()["run_id"]
        packet_url = f"/api/sessions/{sid}/tasks/{rid}/packet"
        wait_for(lambda: client.get(packet_url).json().get("verdict") == "needs_you")
        server = next(
            cell.cell_contents
            for route in app.routes  # type: ignore[attr-defined]
            for cell in (getattr(getattr(route, "endpoint", None), "__closure__", None) or ())
            if isinstance(cell.cell_contents, ChatServer)
        )
        quiet = TaskRun(run_id="quiet", session_id=sid, task="q", time_budget_s=1, token_budget=1)
        server.tasks["quiet"] = quiet  # running, but nothing spent or sealed yet
        frames: list[dict[str, Any]] = []
        with client.stream("GET", f"/api/sessions/{sid}/events") as response:
            for line in response.iter_lines():
                if line.startswith("data: "):
                    frames.append(json.loads(line[6:]))
                    if frames[-1].get("run_id") == "quiet":
                        break
        with client.stream("GET", f"/api/sessions/{other}/events") as response:
            first = next(json.loads(x[6:]) for x in response.iter_lines() if x.startswith("data: "))
        quiet.state = "stopped"
        client.post(f"/api/tasks/{rid}/stop")
        wait_for(lambda: client.get(packet_url).json().get("verdict") == "stopped")

    kinds = [f["kind"] for f in frames]
    assert kinds[0] == "session.info"
    assert kinds[1] == "task.state"
    assert frames[1]["state"] == "needs_you"
    assert frames[1]["question"]["text"] == "Should add(0, 0) be 0?"
    assert "task.line" in kinds
    assert frames[-2]["event"]["kind"] == "run.progress"
    assert frames[-1]["kind"] == "task.state"
    assert frames[-1]["state"] == "running"
    assert first["kind"] == "session.info"  # another session's card is not replayed
