"""A full-access command's `sudo` asks the person for a password (#125).

Known-good: the password reaches the command (through `SUDO_ASKPASS`, and
through the `sudo` wrapper that runs the real one with `-A`).

Known-bad: it appears in nothing else -- no message, journal span or event the
page receives; cancel, or no answer, makes the command fail; without full
access there is no askpass at all.
"""

from __future__ import annotations

import json
import os
import queue
import socket
import subprocess
import threading
import time
from pathlib import Path
from typing import Any

import pytest
from test_chat_server import _server_of, engine_app

from saddle import askpass as askpass_module
from saddle.askpass import Askpass
from saddle.sessions import FULL_ACCESS_CONFIRM, SessionStore
from saddle.tools import ToolContext, execute_tool
from saddle.vllm import StreamToken, ToolCall
from saddle.web import app as app_module

SECRET = "s3cret-pass-7731"
# Compared with a file, so the password is in no command the transcript shows.
CHECK = 'test "$("$SUDO_ASKPASS" "pw:")" = "$(cat .expected)" && echo matched || echo refused'


def run(ctx: ToolContext, command: str) -> str:
    call = ToolCall(id="c", name="run_command", arguments=json.dumps({"command": command}))
    return execute_tool(call, workdir=ctx.workdir, context=ctx)


# -- the helper and the socket ---------------------------------------------------


def test_the_helper_hands_over_the_answer_and_exits_1_when_refused(tmp_path: Path) -> None:
    answers = {"yes:": SECRET, "no:": None}
    asked: list[str] = []

    def ask(prompt: str) -> str | None:
        asked.append(prompt)
        return answers.get(prompt)

    helper = Askpass(ask)
    env = {**os.environ, **helper.env(os.environ.get("PATH", ""))}
    try:
        given = subprocess.run(
            [env["SUDO_ASKPASS"], "yes:"], env=env, capture_output=True, text=True, check=False
        )
        assert (given.returncode, given.stdout) == (0, f"{SECRET}\n")
        refused = subprocess.run(
            [env["SUDO_ASKPASS"], "no:"], env=env, capture_output=True, text=True, check=False
        )
        assert (refused.returncode, refused.stdout) == (1, "")
        # A request that is not JSON, or ends without a newline, asks with no prompt.
        for raw in (b"not json\n", b"{}"):
            with socket.socket(socket.AF_UNIX) as conn:
                conn.connect(str(helper.socket_path))
                conn.sendall(raw)
                conn.shutdown(socket.SHUT_WR)
                assert json.loads(conn.recv(4096)) == {"cancel": True}
        assert asked == ["yes:", "no:", "", ""]
        assert oct(helper.dir.stat().st_mode & 0o777) == "0o700"  # nobody else's to reach
    finally:
        helper.close()
    assert not helper.dir.exists()
    assert not helper.thread.is_alive()  # the serving thread ended, not left blocked
    helper.close()  # closing twice is harmless


def test_the_sudo_wrapper_runs_the_real_sudo_with_dash_a(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake = tmp_path / "real-sudo"
    fake.write_text(
        '#!/bin/sh\n[ "$1" = "-A" ] || exit 9\nshift\n'
        f'[ "$("$SUDO_ASKPASS" "[sudo] password:")" = {SECRET} ] || exit 1\n'
        'echo "ran $* as root"\n'
    )
    fake.chmod(0o755)
    monkeypatch.setattr(askpass_module, "real_sudo", lambda: str(fake))
    asked: list[str] = []

    def ask(prompt: str) -> str:
        asked.append(prompt)
        return SECRET

    ctx = ToolContext(workdir=tmp_path, full_access=True, ask_password=ask)
    result = run(ctx, "sudo whoami")
    assert "ran whoami as root" in result
    assert SECRET not in result
    assert asked == ["[sudo] password:"]
    ctx.revoke_full_access()
    assert ctx.askpass is None


def test_with_no_sudo_on_the_system_there_is_no_wrapper(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(askpass_module, "real_sudo", lambda: None)
    helper = Askpass(lambda _prompt: None)
    try:
        assert list(helper.bin.iterdir()) == []
    finally:
        helper.close()


@pytest.mark.parametrize("full", [False, True], ids=["sandboxed", "nobody-to-ask"])
def test_without_full_access_and_someone_to_ask_there_is_no_askpass(
    tmp_path: Path, full: bool
) -> None:
    asked: list[str] = []

    def answer(prompt: str) -> str:
        asked.append(prompt)
        return SECRET

    ask = answer if not full else None
    ctx = ToolContext(workdir=tmp_path, full_access=full, ask_password=ask)
    assert "none" in run(ctx, 'echo "${SUDO_ASKPASS:-none}"')
    assert (ctx.askpass, asked) == (None, [])


# -- through the server: to the command, and nowhere else ------------------------


def _check_round() -> list[list[Any]]:
    return [
        [ToolCall(id="pw", name="run_command", arguments=json.dumps({"command": CHECK}))],
        [StreamToken(stream="content", text="checked")],
    ]


@pytest.mark.parametrize(("answer", "outcome"), [(SECRET, "matched"), (None, "refused")])
def test_the_password_reaches_the_command_and_nothing_else(
    tmp_path: Path, answer: str | None, outcome: str
) -> None:
    store = SessionStore(tmp_path / "s")
    (tmp_path / ".expected").write_text(SECRET)
    with engine_app(store, tmp_path, _check_round()) as (client, app):
        sid = client.post("/api/sessions", json={"workdir": str(tmp_path)}).json()["id"]
        url = f"/api/sessions/{sid}"
        client.patch(url, json={"mode": "edit"})
        client.post(f"{url}/full-access", json={"on": True, "confirm": FULL_ACCESS_CONFIRM})
        server = _server_of(app)
        live = server._live(sid)
        seen = live.subscribe()

        def reply() -> None:
            deadline = time.monotonic() + 20
            while not live.passwords and time.monotonic() < deadline:
                time.sleep(0.02)
            (request_id,) = list(live.passwords)
            body = {"id": request_id, "cancel": True} if answer is None else {"id": request_id}
            if answer is not None:
                body["password"] = answer
            assert client.post(f"{url}/password", json=body).json() == {"ok": True}

        person = threading.Thread(target=reply)
        person.start()
        server._run(sid, "check the password")
        person.join(timeout=30)
        events: list[str] = []
        while True:
            try:
                event = seen.get_nowait()
            except queue.Empty:
                break
            events.append(json.dumps(event.__dict__ if event is not None else None))
    tools = [m for m in store.load_messages(sid) if m.get("role") == "tool"]
    assert outcome in str(tools[-1]["content"])
    kinds = [json.loads(e)["kind"] for e in events if e != "null"]
    assert "password.request" in kinds
    assert "password.done" in kinds
    everything = "\n".join(
        [*events, json.dumps(store.load_messages(sid)), store.journal_path(sid).read_text()]
    )
    assert SECRET not in everything
    assert live.passwords == {}


def test_an_answer_to_no_request_is_refused(tmp_path: Path) -> None:
    store = SessionStore(tmp_path / "s")
    with engine_app(store, tmp_path, []) as (client, app):
        sid = client.post("/api/sessions", json={"workdir": str(tmp_path)}).json()["id"]
        url = f"/api/sessions/{sid}/password"
        assert client.post(url, json={"id": "x", "password": "p"}).status_code == 404  # no live
        _server_of(app)._live(sid)
        assert client.post(url, json={"id": "x", "password": "p"}).status_code == 404


def test_nobody_answering_refuses_the_password(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(app_module, "PASSWORD_WAIT_S", 0.05)
    live = app_module.Live()
    seen = live.subscribe()
    assert live.ask_password("pw:") is None
    assert live.passwords == {}
    kinds = [seen.get_nowait().kind for _ in range(2)]  # type: ignore[union-attr]
    assert kinds == ["password.request", "password.done"]
