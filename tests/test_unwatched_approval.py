"""A question put to a session nobody has open is a no at once, and says why (F37).

Known-good: with a page subscribed to the session, `ask_approval` and
`ask_password` still wait for the person and return their answer, and a
declined held command says the person did not approve it.

Known-bad: with no page subscribed, a held command used to wait the full
`PASSWORD_WAIT_S` (five minutes) and then read as a plain "did not approve".
Now both questions return at once (under a second, nothing left open), and
the refusal the model reads says nobody had the session's page open.
"""

from __future__ import annotations

import json
import threading
import time
from pathlib import Path

import pytest

from saddle.mcpclient import Approvals
from saddle.research import NOBODY_WATCHING, Brought, Researcher
from saddle.sideeffects import SideEffects
from saddle.tools import ToolContext, execute_tool
from saddle.vllm import ToolCall
from saddle.web.app import Live


def asked_in_background(ask: object) -> tuple[threading.Thread, list[object]]:
    got: list[object] = []
    asker = threading.Thread(target=lambda: got.append(ask()), daemon=True)  # type: ignore[operator]
    asker.start()
    return asker, got


def wait_for(open_requests: dict[str, object]) -> str:
    deadline = time.monotonic() + 5
    while not open_requests:
        assert time.monotonic() < deadline, "the question never reached the page"
        time.sleep(0.01)
    (request_id,) = open_requests
    return request_id


def run(ctx: ToolContext, command: str) -> str:
    call = ToolCall(id="c", name="run_command", arguments=json.dumps({"command": command}))
    return execute_tool(call, workdir=ctx.workdir, context=ctx)


# --- the live session's questions --------------------------------------------


def test_with_no_page_open_an_approval_is_a_no_at_once() -> None:
    live = Live()
    started = time.monotonic()
    asker, got = asked_in_background(lambda: live.ask_approval("Run it?", ["command: x"]))
    asker.join(1)
    assert not asker.is_alive(), "ask_approval waited with nobody watching"
    assert got == [False]
    assert time.monotonic() - started < 1
    assert live.approvals == {}


def test_with_no_page_open_a_password_is_refused_at_once() -> None:
    live = Live()
    asker, got = asked_in_background(lambda: live.ask_password("[sudo] password:"))
    asker.join(1)
    assert not asker.is_alive(), "ask_password waited with nobody watching"
    assert got == [None]
    assert live.passwords == {}


@pytest.mark.parametrize("answer", [True, False])
def test_with_a_page_open_an_approval_waits_for_the_answer(answer: bool) -> None:
    live = Live()
    live.subscribe()
    asker, got = asked_in_background(lambda: live.ask_approval("Run it?", []))
    request_id = wait_for(live.approvals)  # type: ignore[arg-type]
    asker.join(0.3)
    assert asker.is_alive(), "ask_approval did not wait for the person"
    live.approvals[request_id].put(answer)
    asker.join(5)
    assert got == [answer]


def test_with_a_page_open_a_password_waits_for_the_answer() -> None:
    live = Live()
    live.subscribe()
    asker, got = asked_in_background(lambda: live.ask_password("pw:"))
    request_id = wait_for(live.passwords)  # type: ignore[arg-type]
    asker.join(0.3)
    assert asker.is_alive(), "ask_password did not wait for the person"
    live.passwords[request_id].put("typed")
    asker.join(5)
    assert got == ["typed"]


def test_a_page_that_closed_leaves_nobody_watching() -> None:
    live = Live()
    channel = live.subscribe()
    assert live.watched()
    live.unsubscribe(channel)
    assert not live.watched()


# --- what the model reads ----------------------------------------------------


@pytest.fixture
def held(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[Path, Path]:
    home = tmp_path / "home"
    (home / "hp1").mkdir(parents=True)
    (home / "hp1" / "a.txt").write_text("alpha")
    monkeypatch.setenv("HOME", str(home))
    folder = tmp_path / "folder"
    folder.mkdir()
    (folder / "path").write_text(str(home / "hp1"))
    return folder, home / "hp1"


def held_ctx(tmp_path: Path, folder: Path, live: Live) -> ToolContext:
    return ToolContext(
        workdir=folder,
        full_access=True,
        effects=SideEffects(tmp_path / "rec"),
        approve=live.ask_approval,
        watched=live.watched,
    )


def test_a_held_command_with_nobody_watching_is_refused_at_once_saying_so(
    tmp_path: Path, held: tuple[Path, Path]
) -> None:
    folder, tree = held
    live = Live()
    started = time.monotonic()
    out = run(held_ctx(tmp_path, folder, live), 'rm -rf "$(cat path)"')
    assert time.monotonic() - started < 1
    assert tree.exists()
    assert out.startswith("error: this command was not run")
    assert NOBODY_WATCHING in out
    assert "the person did not approve it" not in out


def test_a_held_command_the_person_declines_says_they_did_not_approve(
    tmp_path: Path, held: tuple[Path, Path]
) -> None:
    folder, tree = held
    live = Live()
    live.subscribe()
    ctx = held_ctx(tmp_path, folder, live)
    asker, got = asked_in_background(lambda: run(ctx, 'rm -rf "$(cat path)"'))
    live.approvals[wait_for(live.approvals)].put(False)  # type: ignore[arg-type]
    asker.join(10)
    (out,) = got
    assert tree.exists()
    assert "the person did not approve it" in str(out)
    assert NOBODY_WATCHING not in str(out)


def reader_ctx(tmp_path: Path, live: Live) -> ToolContext:
    researcher = Researcher(
        {},
        Approvals(tmp_path / "a.json"),
        tmp_path / "d",
        approve=live.ask_approval,
        watched=live.watched,
    )
    researcher.brought.append(Brought("https://docs.example/changelog", "cited"))
    return ToolContext(workdir=tmp_path, research=researcher, full_access=True)


def test_a_command_naming_what_the_reader_brought_with_nobody_watching_says_so(
    tmp_path: Path,
) -> None:
    live = Live()
    started = time.monotonic()
    out = run(reader_ctx(tmp_path, live), "curl https://docs.example/changelog")
    assert time.monotonic() - started < 1
    assert out.startswith("error: this command names")
    assert NOBODY_WATCHING in out


def test_a_command_naming_what_the_reader_brought_that_is_declined_does_not_blame_the_page(
    tmp_path: Path,
) -> None:
    live = Live()
    live.subscribe()
    ctx = reader_ctx(tmp_path, live)
    asker, got = asked_in_background(lambda: run(ctx, "curl https://docs.example/changelog"))
    live.approvals[wait_for(live.approvals)].put(False)  # type: ignore[arg-type]
    asker.join(10)
    (out,) = got
    assert str(out).startswith("error: this command names")
    assert NOBODY_WATCHING not in str(out)
