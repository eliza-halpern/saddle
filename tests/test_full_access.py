"""A session's full access (#124): the Edit lane's commands run outside the
sandbox, only when a person turned it on with its confirmation.

Known-good: off by default; on with `FULL_ACCESS_CONFIRM` word for word, a
command reaches past the folder and its result says UNSANDBOXED; off again at
once, its commands stopped and the next one sandboxed.

Known-bad: `update`, a PATCH or a near-miss confirmation never turns it on; a
new session is never handed back with it; a lane that requires isolation can
never be given it; the CLI starts nothing unless the answer is y or yes.
"""

from __future__ import annotations

import io
import json
import re
from pathlib import Path
from typing import Any, cast

import httpx
import pytest
from starlette.testclient import TestClient
from test_chat_server import ScriptedClient, _server_of, engine_app
from test_cli import _chat_recorder, _FakeClient
from test_ui3_mode import _info, serving

from saddle.chat import ChatOptions, run_chat
from saddle.cli import FULL_ACCESS_PROMPT, chat_console, confirm_full_access, main
from saddle.journal import read_spans
from saddle.sandbox import Sandbox, isolation_problem
from saddle.sessions import FULL_ACCESS_CONFIRM, FullAccessRefusedError, SessionStore
from saddle.tools import UNSANDBOXED, ToolContext, execute_tool
from saddle.vllm import StreamToken, ToolCall, VllmClient
from saddle.web.app import build_app

BWRAP = isolation_problem() is None


def call(name: str, **arguments: Any) -> ToolCall:
    return ToolCall(id="c1", name=name, arguments=json.dumps(arguments))


def run(ctx: ToolContext, name: str, **arguments: Any) -> str:
    return execute_tool(call(name, **arguments), workdir=ctx.workdir, context=ctx)


def started_id(result: str) -> str:
    match = re.search(r"started terminal (\S+) ", result)
    assert match is not None, result
    return match.group(1)


# -- the setting: off by default, on only with its confirmation -------------------


def test_a_new_session_starts_without_full_access_and_update_cannot_turn_it_on(
    tmp_path: Path,
) -> None:
    store = SessionStore(tmp_path / "s")
    session = store.create(workdir=str(tmp_path))
    assert session.full_access is False
    with pytest.raises(FullAccessRefusedError):
        store.update(session.id, full_access=True)
    assert store.get(session.id).full_access is False


@pytest.mark.parametrize(
    "confirm",
    ["", "yes", FULL_ACCESS_CONFIRM.upper(), f"{FULL_ACCESS_CONFIRM} ", FULL_ACCESS_CONFIRM[:-1]],
    ids=["none", "yes", "upper", "trailing-space", "cut-short"],
)
def test_full_access_turns_on_only_with_its_confirmation_word_for_word(
    tmp_path: Path, confirm: str
) -> None:
    store = SessionStore(tmp_path / "s")
    sid = store.create(workdir=str(tmp_path)).id
    store.update(sid, mode="edit")
    with pytest.raises(FullAccessRefusedError):
        store.set_full_access(sid, True, confirm=confirm)
    assert store.get(sid).full_access is False
    store.set_full_access(sid, True, confirm=FULL_ACCESS_CONFIRM)
    # A new store over the same root is what a restarted server sees.
    assert SessionStore(tmp_path / "s").get(sid).full_access is True
    store.set_full_access(sid, False)  # off needs nothing
    assert SessionStore(tmp_path / "s").get(sid).full_access is False


def test_a_new_session_is_never_handed_back_with_full_access(tmp_path: Path) -> None:
    store = SessionStore(tmp_path / "s")
    first = store.create(workdir=str(tmp_path))
    # Known-good control: an untouched session is reused rather than duplicated.
    assert store.create(workdir=str(tmp_path), reuse_unstarted=True).id == first.id
    store.update(first.id, mode="edit")
    store.set_full_access(first.id, True, confirm=FULL_ACCESS_CONFIRM)
    fresh = store.create(workdir=str(tmp_path), reuse_unstarted=True)
    assert fresh.id != first.id
    assert fresh.full_access is False


@pytest.mark.parametrize("lane", ["ask", "task"])
def test_full_access_exists_only_in_the_edit_lane(tmp_path: Path, lane: str) -> None:
    """Granted only in Edit; anything else written to the session keeps it;
    leaving Edit ends it, and coming back does not bring it back."""
    store = SessionStore(tmp_path / "s")
    sid = store.create(workdir=str(tmp_path)).id
    with pytest.raises(FullAccessRefusedError, match="Edit lane"):
        store.set_full_access(sid, True, confirm=FULL_ACCESS_CONFIRM)  # in Ask
    store.update(sid, mode="edit")
    store.set_full_access(sid, True, confirm=FULL_ACCESS_CONFIRM)
    store.update(sid, title="renamed", mode="edit")  # staying in Edit keeps it
    assert store.get(sid).full_access is True
    store.update(sid, mode=lane)
    assert SessionStore(tmp_path / "s").get(sid).full_access is False
    store.update(sid, mode="edit")
    assert store.get(sid).full_access is False


def test_a_lane_that_requires_isolation_can_never_run_unsandboxed(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="requires isolation"):
        Sandbox.for_workdir(tmp_path, require_isolation=True, unsandboxed=True)


# -- the commands: outside the sandbox, and labelled every time -------------------


def test_with_full_access_a_command_reaches_past_the_folder_and_says_so(tmp_path: Path) -> None:
    folder = tmp_path / "folder"
    folder.mkdir()
    (tmp_path / "outside.txt").write_text("past-the-folder\n")
    ctx = ToolContext(workdir=folder, full_access=True)
    result = run(ctx, "run_command", command=f"cat {tmp_path / 'outside.txt'}")
    assert ctx.box().isolation == "none"
    assert result.startswith(f"{UNSANDBOXED}\nexit 0\n")
    assert "past-the-folder" in result


@pytest.mark.skipif(not BWRAP, reason="needs a bwrap that can start")
def test_without_full_access_the_same_command_cannot_see_past_the_folder(tmp_path: Path) -> None:
    folder = tmp_path / "folder"
    folder.mkdir()
    (tmp_path / "outside.txt").write_text("past-the-folder\n")
    ctx = ToolContext(workdir=folder)
    result = run(ctx, "run_command", command=f"cat {tmp_path / 'outside.txt'}")
    assert ctx.box().isolation == "bwrap"
    assert "past-the-folder" not in result
    assert UNSANDBOXED not in result


@pytest.mark.parametrize("full", [True, False], ids=["full-access", "sandboxed"])
def test_every_command_result_is_labelled_exactly_while_full_access_is_on(
    tmp_path: Path, full: bool
) -> None:
    ctx = ToolContext(workdir=tmp_path, full_access=full)
    slow = started_id(run(ctx, "run_command", command="sleep 20", background=True))
    quick = started_id(run(ctx, "run_command", command="echo done", background=True))
    results = {
        "ran": run(ctx, "run_command", command="echo hi"),
        "still running": run(ctx, "run_command", command="sleep 20", timeout=1),
        "started": run(ctx, "run_command", command="true", background=True),
        "read": run(ctx, "read_terminal", id=slow),
        "waited, still running": run(ctx, "wait_for_terminal", id=slow, timeout=1),
        "waited, exited": run(ctx, "wait_for_terminal", id=quick, timeout=10),
    }
    for terminal_id, terminal in list(ctx.box().terminals.items()):
        if terminal.running:
            ctx.box().kill(terminal_id)
    labelled = {name: text.startswith(f"{UNSANDBOXED}\n") for name, text in results.items()}
    assert labelled == dict.fromkeys(results, full), results


def test_turning_full_access_off_stops_its_commands_and_sandboxes_the_next(
    tmp_path: Path,
) -> None:
    ctx = ToolContext(workdir=tmp_path, full_access=True)
    run(ctx, "run_command", command="true")  # a finished terminal is left as it is
    slow = started_id(run(ctx, "run_command", command="sleep 30", background=True))
    box = ctx.box()
    ctx.revoke_full_access()
    process = box.terminals[slow].process
    assert process is not None
    assert process.poll() is not None  # stopped, not left running unsandboxed
    assert (ctx.full_access, ctx.sandbox) == (False, None)
    after = run(ctx, "run_command", command="echo hi")
    assert not after.startswith(UNSANDBOXED)
    assert ctx.box().isolation == ("bwrap" if BWRAP else "none")
    ctx.revoke_full_access()  # again: harmless
    ToolContext(workdir=tmp_path, full_access=True).revoke_full_access()  # no box yet


# -- the web API: a dedicated, confirmed endpoint -----------------------------------


def test_a_patch_can_never_turn_full_access_on(tmp_path: Path) -> None:
    store = SessionStore(tmp_path / "s")
    app = build_app(store, ScriptedClient, default_workdir=tmp_path)
    with TestClient(app) as client:
        sid = client.post("/api/sessions", json={}).json()["id"]
        reply = client.patch(f"/api/sessions/{sid}", json={"full_access": True, "mode": "edit"})
        assert reply.status_code == 400
        assert "/full-access" in reply.json()["error"]
    assert (store.get(sid).full_access, store.get(sid).mode) == (False, "ask")


def test_the_full_access_endpoint_turns_it_on_only_when_confirmed(tmp_path: Path) -> None:
    """Known-bad bodies leave it off; the confirmation turns it on, and the page
    learns it from `session.info` on its next connect; off needs nothing."""
    store = SessionStore(tmp_path / "s")
    app = build_app(store, ScriptedClient, default_workdir=tmp_path)
    with serving(app) as base, httpx.Client(base_url=base, timeout=15) as client:
        sid = client.post("/api/sessions", json={}).json()["id"]
        url = f"/api/sessions/{sid}/full-access"
        in_ask = client.post(url, json={"on": True, "confirm": FULL_ACCESS_CONFIRM})
        assert (in_ask.status_code, _info(client, sid)["full_access"]) == (400, False)
        client.patch(f"/api/sessions/{sid}", json={"mode": "edit"})
        for body in ({"on": True}, {"on": True, "confirm": "yes"}, {"on": True, "confirm": 1}):
            assert client.post(url, json=body).status_code == 400
            assert _info(client, sid)["full_access"] is False
        on = client.post(url, json={"on": True, "confirm": FULL_ACCESS_CONFIRM})
        assert (on.status_code, on.json()["full_access"]) == (200, True)
        assert _info(client, sid)["full_access"] is True
        off = client.post(url, json={"on": False})
        assert (off.status_code, off.json()["full_access"]) == (200, False)
        assert _info(client, sid)["full_access"] is False


def _commands(text: str) -> list[list[Any]]:
    return [
        [ToolCall(id="r", name="run_command", arguments=json.dumps({"command": text}))],
        [StreamToken(stream="content", text="ran it")],
    ]


def _last_tool_result(store: SessionStore, sid: str) -> str:
    tools = [m for m in store.load_messages(sid) if m.get("role") == "tool"]
    return str(tools[-1]["content"])


def test_a_chat_turn_runs_its_commands_as_the_session_says(tmp_path: Path) -> None:
    """Through the server: full access on, the turn's command is labelled; off
    again, the next turn's command is sandboxed. Off also reaches a context the
    server is already holding, so it never runs on with the old sandbox."""
    store = SessionStore(tmp_path / "s")
    # Each turn builds its own client, which replays these rounds from the start.
    with engine_app(store, tmp_path, _commands("echo one")) as (client, app):
        sid = client.post("/api/sessions", json={"workdir": str(tmp_path)}).json()["id"]
        client.patch(f"/api/sessions/{sid}", json={"mode": "edit"})
        server = _server_of(app)
        server._run(sid, "sandboxed first")
        assert not _last_tool_result(store, sid).startswith(UNSANDBOXED)
        url = f"/api/sessions/{sid}/full-access"
        client.post(url, json={"on": True, "confirm": FULL_ACCESS_CONFIRM})
        server._run(sid, "now with full access")
        assert _last_tool_result(store, sid).startswith(f"{UNSANDBOXED}\nexit 0\none")
        held = server.live[sid].context
        assert held is not None
        assert held.full_access is True
        client.post(url, json={"on": False})
        assert held.full_access is False  # the held context, at once
        server._run(sid, "sandboxed again")
        assert not _last_tool_result(store, sid).startswith(UNSANDBOXED)


def test_leaving_edit_turns_full_access_off_and_stops_its_commands(tmp_path: Path) -> None:
    """A background command started with full access is stopped the moment the
    session leaves Edit, not at its next turn; back in Edit, it stays off."""
    store = SessionStore(tmp_path / "s")
    started: list[list[Any]] = [
        [
            ToolCall(
                id="bg",
                name="run_command",
                arguments=json.dumps({"command": "sleep 30", "background": True}),
            )
        ],
        [StreamToken(stream="content", text="started it")],
    ]
    with engine_app(store, tmp_path, started) as (client, app):
        sid = client.post("/api/sessions", json={"workdir": str(tmp_path)}).json()["id"]
        session_url = f"/api/sessions/{sid}"
        client.patch(session_url, json={"mode": "edit"})
        client.post(f"{session_url}/full-access", json={"on": True, "confirm": FULL_ACCESS_CONFIRM})
        server = _server_of(app)
        server._run(sid, "start something")
        held = server.live[sid].context
        assert held is not None
        box = held.box()
        (terminal,) = box.terminals.values()
        assert terminal.process is not None
        assert terminal.process.poll() is None  # running, outside the sandbox
        title_only = client.patch(session_url, json={"title": "still in edit"})
        assert title_only.json()["full_access"] is True  # nothing else ends it
        assert terminal.process.poll() is None
        left = client.patch(session_url, json={"mode": "ask"})
        assert left.json()["full_access"] is False
        assert terminal.process.poll() is not None  # stopped at once
        assert held.full_access is False
        back = client.patch(session_url, json={"mode": "edit"})
        assert back.json()["full_access"] is False


# -- the CLI: `saddle up --mode edit --full-access` asks first ---------------------


@pytest.mark.parametrize(
    ("answer", "granted"),
    [("y\n", True), ("YES\n", True), (" yes \n", True), ("n\n", False), ("\n", False), ("", False)],
    ids=["y", "YES", "spaced", "n", "enter", "eof"],
)
def test_the_cli_turns_full_access_on_only_for_y_or_yes(answer: str, granted: bool) -> None:
    out = io.StringIO()
    assert confirm_full_access(io.StringIO(answer), out) is granted
    said = out.getvalue()
    assert said.startswith(FULL_ACCESS_PROMPT)
    assert (UNSANDBOXED in said) is granted


def test_saddle_up_full_access_needs_edit_and_a_yes_before_anything_starts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    seen: list[dict[str, Any]] = []
    monkeypatch.setenv("SADDLE_VLLM_API_KEY", "k1")
    monkeypatch.setattr("saddle.cli.VllmClient", _FakeClient)
    monkeypatch.setattr("saddle.cli.run_chat", _chat_recorder(seen))
    _FakeClient.made.clear()
    assert main(["up", "--full-access", "--workdir", str(tmp_path)]) == 2
    assert "--mode edit only" in capsys.readouterr().err
    edit = ["up", "--mode", "edit", "--full-access", "--workdir", str(tmp_path)]
    assert main(edit, stdin=io.StringIO("n\n"), stdout=io.StringIO()) == 1
    assert (seen, _FakeClient.made) == ([], [])  # nothing started
    assert main(edit, stdin=io.StringIO("y\n/quit\n"), stdout=io.StringIO()) == 0
    assert [s["options"].full_access for s in seen] == [True]


@pytest.mark.parametrize("full", [True, False], ids=["full-access", "sandboxed"])
def test_a_terminal_chat_journals_each_command_as_it_ran(tmp_path: Path, full: bool) -> None:
    """`saddle up`'s session context carries the choice: the command's span in
    the journal starts UNSANDBOXED exactly when full access is on."""
    ScriptedClient.rounds = _commands("echo hi")
    journal = tmp_path / "j.jsonl"
    options = ChatOptions(workdir=tmp_path, journal=journal, mode="edit", full_access=full)
    with ScriptedClient() as client:
        code = run_chat(
            options,
            cast(VllmClient, client),
            stdin=io.StringIO("run it\n"),
            console=chat_console(io.StringIO()),
        )
    assert code == 0
    (span,) = [s for s in read_spans(journal) if s.argv[:1] == ["run_command"]]
    assert span.detail.startswith(UNSANDBOXED) is full
