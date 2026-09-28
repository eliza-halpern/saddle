"""UI3: Chat and Task are modes a session is in, not a button beside Send.

Known-good: a session starts in ask (it used to start in chat); PATCH mode=task persists and comes
back on reconnect in `session.info`; in a real browser, Enter in Task mode
opens the confirm strip and starts nothing, and a second Enter starts the
run; the description under the composer names the selected mode.

Known-bad: a mode other than ask/edit/task is refused with 400 and the stored
mode is unchanged; a Task-mode Enter never sends a chat message.
"""

from __future__ import annotations

import json
import shutil
import subprocess
import threading
import time
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

import pytest
from starlette.testclient import TestClient

from saddle.sessions import SESSION_MODES, SessionStore
from saddle.web.app import ChatServer, build_app
from saddle.web.tasks import TaskRun

HERE = Path(__file__).parent
CDP = HERE / "fixtures" / "ui3_mode_cdp.mjs"


class NoModel:
    def __enter__(self) -> NoModel:
        return self

    def __exit__(self, *_: object) -> None:
        return None

    def max_model_len(self) -> int:
        return 200_000


def _server_of(app: Any) -> ChatServer:
    return next(
        cell.cell_contents
        for route in app.routes
        for cell in (getattr(getattr(route, "endpoint", None), "__closure__", None) or ())
        if isinstance(cell.cell_contents, ChatServer)
    )


@contextmanager
def serving(app: Any) -> Iterator[str]:
    import uvicorn

    config = uvicorn.Config(app, host="127.0.0.1", port=0, log_level="error")
    server = uvicorn.Server(config)
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    try:
        deadline = time.monotonic() + 10
        while not server.started and time.monotonic() < deadline:
            time.sleep(0.01)
        assert server.started, "uvicorn did not start"
        yield f"http://127.0.0.1:{server.servers[0].sockets[0].getsockname()[1]}"
    finally:
        server.should_exit = True
        thread.join(timeout=10)


def _info(client: Any, sid: str) -> dict[str, Any]:
    with client.stream("GET", f"/api/sessions/{sid}/events") as response:
        for line in response.iter_lines():
            if line.startswith("data: "):
                frame = json.loads(line[6:])
                if frame["kind"] == "session.info":
                    info: dict[str, Any] = frame
                    return info
    message = "no session.info"
    raise AssertionError(message)  # pragma: no cover


def test_the_modes_are_exactly_ask_edit_and_task() -> None:
    assert SESSION_MODES == ("ask", "edit", "task")


def test_a_session_starts_in_ask_and_its_mode_persists(tmp_path: Path) -> None:
    store = SessionStore(tmp_path / "s")
    app = build_app(store, NoModel, default_workdir=tmp_path)
    with TestClient(app) as client:
        sid = client.post("/api/sessions", json={}).json()["id"]
        assert store.get(sid).mode == "ask"
        reply = client.patch(f"/api/sessions/{sid}", json={"mode": "task"})
        assert reply.status_code == 200
        assert reply.json()["mode"] == "task"
    # A new store over the same root is what a restarted server sees.
    assert SessionStore(tmp_path / "s").get(sid).mode == "task"


def test_the_mode_comes_back_in_session_info_on_reconnect(tmp_path: Path) -> None:
    import httpx

    store = SessionStore(tmp_path / "s")
    app = build_app(store, NoModel, default_workdir=tmp_path)
    with serving(app) as base, httpx.Client(base_url=base, timeout=15) as client:
        sid = client.post("/api/sessions", json={}).json()["id"]
        assert _info(client, sid)["mode"] == "ask"
        client.patch(f"/api/sessions/{sid}", json={"mode": "task"})
        assert _info(client, sid)["mode"] == "task"


@pytest.mark.parametrize("bad", ["auto", "", "TASK", "chat", 1, None, ["task"]])
def test_a_mode_that_is_not_a_lane_is_refused(tmp_path: Path, bad: Any) -> None:
    store = SessionStore(tmp_path / "s")
    app = build_app(store, NoModel, default_workdir=tmp_path)
    with TestClient(app) as client:
        sid = client.post("/api/sessions", json={}).json()["id"]
        client.patch(f"/api/sessions/{sid}", json={"mode": "task"})
        reply = client.patch(f"/api/sessions/{sid}", json={"mode": bad, "title": "x"})
        assert reply.status_code == 400
        assert store.get(sid).mode == "task"
        assert store.get(sid).title != "x"


def test_a_session_saved_before_modes_existed_reads_as_ask(tmp_path: Path) -> None:
    store = SessionStore(tmp_path / "s")
    sid = store.create(title="old", workdir=str(tmp_path)).id
    meta = tmp_path / "s" / sid / "session.json"
    data = json.loads(meta.read_text())
    del data["mode"]
    meta.write_text(json.dumps(data))
    assert store.get(sid).mode == "ask"


# -- the browser --------------------------------------------------------------


@contextmanager
def _recording(app: Any) -> Iterator[tuple[ChatServer, list[tuple[str, Any]], list[str]]]:
    """Record run starts and chat turns instead of doing either."""
    server = _server_of(app)
    runs: list[tuple[str, Any]] = []
    chats: list[str] = []

    def run_task(session_id: str, run: TaskRun) -> None:
        runs.append((session_id, run))
        with server._live(session_id).lock:
            server._live(session_id).busy = False

    server._run_task = run_task  # type: ignore[method-assign]
    import saddle.web.app as module

    original = module.run_turn

    def fake_turn(_c: Any, messages: Any, text: str, _o: Any, **_kw: Any) -> Any:
        chats.append(text)
        messages.append({"role": "user", "content": text})
        return iter(())

    module.run_turn = fake_turn  # type: ignore[assignment]
    try:
        yield server, runs, chats
    finally:
        module.run_turn = original


def _browser(base: str, sid: str, step: str) -> dict[str, Any]:
    out = subprocess.run(
        ["node", str(CDP), base, sid, step],
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    assert out.returncode == 0, out.stderr
    result: dict[str, Any] = json.loads(out.stdout.strip().splitlines()[-1])
    return result


BROWSER = shutil.which("node") and shutil.which("google-chrome")


@pytest.mark.skipif(not BROWSER, reason="needs node and google-chrome")
def test_in_task_mode_enter_opens_the_strip_and_a_second_enter_starts(tmp_path: Path) -> None:
    store = SessionStore(tmp_path / "s")
    app = build_app(store, NoModel, default_workdir=tmp_path)
    with _recording(app) as (_server, runs, chats), serving(app) as base:
        sid = store.create(title="t", workdir=str(tmp_path)).id
        store.update(sid, mode="task")
        got = _browser(base, sid, "task")
        deadline = time.monotonic() + 5
        while not runs and time.monotonic() < deadline:
            time.sleep(0.05)
    first, second = got["afterFirstEnter"], got["afterSecondEnter"]
    assert first["stripVisible"] is True
    assert first["focus"] == "tc-start"
    assert first["taskPosts"] == 0
    assert first["desc"].startswith("Task · Small:")
    assert first["chip"] == "task"
    assert second["taskPosts"] == 1
    assert second["stripVisible"] is False
    assert [run.task for _sid, run in runs] == ["make add add"]
    assert chats == []
    assert got["afterEscape"]["stripVisible"] is False
    assert got["afterEscape"]["focus"] == "input"


@pytest.mark.skipif(not BROWSER, reason="needs node and google-chrome")
def test_in_edit_mode_enter_sends_and_the_switch_persists(tmp_path: Path) -> None:
    store = SessionStore(tmp_path / "s")
    app = build_app(store, NoModel, default_workdir=tmp_path)
    with _recording(app) as (_server, runs, chats), serving(app) as base:
        sid = store.create(title="t", workdir=str(tmp_path)).id
        store.update(sid, mode="edit")
        got = _browser(base, sid, "chat")
        deadline = time.monotonic() + 5
        while not chats and time.monotonic() < deadline:
            time.sleep(0.05)
        mode_after = store.get(sid).mode
    assert got["before"]["desc"].startswith("Edit: unaudited, edits your folder.")
    assert got["before"]["chip"] == "edit"
    assert got["afterEnter"]["stripVisible"] is False
    assert got["afterEnter"]["taskPosts"] == 0
    assert chats == ["hello there"]
    assert runs == []
    assert got["afterSwitch"]["desc"].startswith("Task · Small:")
    assert got["afterSwitch"]["chip"] == "task"
    assert got["afterReload"]["chip"] == "task"
    assert got["afterReload"]["desc"].startswith("Task · Small:")
    assert mode_after == "task"


@pytest.mark.skipif(not BROWSER, reason="needs node and google-chrome")
def test_task_mode_with_no_folder_says_so_instead_of_starting(tmp_path: Path) -> None:
    store = SessionStore(tmp_path / "s")
    app = build_app(store, NoModel, default_workdir=tmp_path)
    with _recording(app) as (_server, runs, _chats), serving(app) as base:
        sid = store.create(title="t", workdir=str(tmp_path)).id
        store.update(sid, mode="task")
        got = _browser(base, sid, "nofolder")
    assert got["stripVisible"] is False
    assert "folder" in got["note"].lower()
    assert got["taskPosts"] == 0
    assert runs == []
