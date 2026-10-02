"""The held-command dialog, full-access wording and outside chip, in a real browser (#124, #137).

Known-good: a full-access command held for an unresolvable target (`rm -rf "$(cat
path)"`) shows the person its command and why, in the approval dialog; Decline
leaves the files and the model's tool result says it was not run; Approve runs
it. The full-access dialog says what can be undone (attributed file changes)
and what cannot. The outside chip shows the count of the session on screen and
nothing for a session that changed nothing outside, also straight after switching
from one that did.

Known-bad: the dialog no longer claims that nothing a command does can be
undone; the chip never keeps the previous session's count.

Set SADDLE_HOLD_SHOTS to a directory to keep screenshots of the page.
"""

from __future__ import annotations

import json
import os
import threading
import time
from pathlib import Path

import pytest
from chrome_page import drive_page
from test_chat_server import _server_of
from test_ui3_mode import NoModel, serving

from saddle.sessions import FULL_ACCESS_CONFIRM, SessionStore
from saddle.sideeffects import SideEffects
from saddle.tools import ToolContext, execute_tool
from saddle.vllm import ToolCall
from saddle.web.app import build_app


@pytest.fixture
def home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    place = tmp_path / "home"
    place.mkdir()
    monkeypatch.setenv("HOME", str(place))
    return place


def run(ctx: ToolContext, command: str) -> str:
    call = ToolCall(id="c", name="run_command", arguments=json.dumps({"command": command}))
    return execute_tool(call, workdir=ctx.workdir, context=ctx)


def test_a_held_command_is_shown_with_its_reasons_and_runs_only_on_approval(
    tmp_path: Path, home: Path
) -> None:
    shots = os.environ.get("SADDLE_HOLD_SHOTS", "")
    folder = tmp_path / "folder"
    folder.mkdir()
    keep = home / "hp1"
    keep.mkdir()
    (keep / "a.txt").write_text("alpha")
    other = home / "hp2"
    other.mkdir()
    (folder / "where").write_text(str(keep))
    (folder / "where2").write_text(str(other))
    store = SessionStore(tmp_path / "s")
    app = build_app(store, NoModel, default_workdir=folder)
    with serving(app) as base:
        sid = store.create(title="set up", workdir=str(folder)).id
        live = _server_of(app)._live(sid)
        ctx = ToolContext(
            workdir=folder,
            full_access=True,
            effects=SideEffects(store.outside_dir(sid)),
            approve=live.ask_approval,
        )
        results: list[str] = []

        def commands() -> None:
            while not live.subscribers:
                time.sleep(0.02)
            results.append(run(ctx, 'rm -rf "$(cat where)"'))
            while live.approvals:
                time.sleep(0.02)
            results.append(run(ctx, 'rm -rf "$(cat where2)"'))

        worker = threading.Thread(target=commands)
        worker.start()
        got = drive_page(
            base,
            """
            await page.width(1100);
            await page.chat(args.sid);
            const open = () => document.querySelector("#approval-dialog").open;
            await page.until(() => document.querySelector("#approval-dialog").open);
            const shown = await page.js(() => ({
              title: document.querySelector("#approval-title").textContent,
              lines: document.querySelector("#approval-lines").textContent,
              overflow: document.querySelector("#approval-lines").scrollWidth
                > document.querySelector("#approval-dialog").clientWidth,
            }));
            await page.shot("hold-desktop.png");
            await page.width(400);
            const narrow = await page.js(
              () => document.documentElement.scrollWidth <= window.innerWidth);
            await page.shot("hold-narrow.png");
            await page.width(1100);
            const first = await page.js(() => state.approvalId);
            await page.click("#approval-decline");
            await page.until((id) => state.approvalId && state.approvalId !== id
              && document.querySelector("#approval-dialog").open, first);
            await page.click("#approval-approve");
            await page.until(() => !document.querySelector("#approval-dialog").open);
            return { shown, narrow };
            """,
            sid=sid,
            shots=shots,
        )
        worker.join(30)
    assert "can destroy" in got["shown"]["title"]
    assert got["shown"]["lines"].startswith('command: rm -rf "$(cat where)"')
    assert "held because:" in got["shown"]["lines"]
    assert "command substitution" in got["shown"]["lines"]
    assert got["narrow"] is True
    assert results[0].startswith("error: this command was not run")
    assert results[1].endswith("exit 0\n")
    assert (keep / "a.txt").read_text() == "alpha"  # declined: untouched
    assert not other.exists()  # approved: ran


def test_the_full_access_dialog_says_what_can_and_cannot_be_undone(tmp_path: Path) -> None:
    shots = os.environ.get("SADDLE_HOLD_SHOTS", "")
    store = SessionStore(tmp_path / "s")
    app = build_app(store, NoModel, default_workdir=tmp_path)
    with serving(app) as base:
        sid = store.create(title="t", workdir=str(tmp_path)).id
        store.update(sid, mode="edit")
        got = drive_page(
            base,
            """
            await page.width(1100);
            await page.chat(args.sid);
            await page.click("#full-access-open");
            await page.until(() => document.querySelector("#full-access-dialog").open);
            const text = await page.js(
              () => document.querySelector("#full-access-dialog .fa-grants").textContent);
            await page.shot("full-access-desktop.png");
            await page.width(400);
            const narrow = await page.js(
              () => document.documentElement.scrollWidth <= window.innerWidth);
            await page.shot("full-access-narrow.png");
            return { text, narrow };
            """,
            sid=sid,
            shots=shots,
        )
    flat = " ".join(got["text"].split())
    assert "backed up first and can be undone from the record" in flat
    assert "everything else cannot be undone" in flat
    assert "not tracked" in flat
    assert "nothing a command does can be undone" not in flat
    assert got["narrow"] is True


def test_the_outside_chip_shows_only_the_session_on_screen(tmp_path: Path, home: Path) -> None:
    shots = os.environ.get("SADDLE_HOLD_SHOTS", "")
    folder = tmp_path / "folder"
    folder.mkdir()
    (home / "conf.txt").write_text("a")
    store = SessionStore(tmp_path / "s")
    app = build_app(store, NoModel, default_workdir=folder)
    with serving(app) as base:
        busy = store.create(title="busy", workdir=str(folder)).id
        quiet = store.create(title="quiet", workdir=str(folder)).id
        lapsed = store.create(title="lapsed", workdir=str(folder)).id  # had full access, now Ask
        store.update(busy, mode="edit")
        store.set_full_access(busy, True, confirm=FULL_ACCESS_CONFIRM)
        ctx = ToolContext(
            workdir=folder, full_access=True, effects=SideEffects(store.outside_dir(busy))
        )
        run(ctx, f"echo b > {home}/conf.txt; echo c > {home}/new.txt")
        again = ToolContext(
            workdir=folder, full_access=True, effects=SideEffects(store.outside_dir(lapsed))
        )
        run(again, f"echo d > {home}/third.txt")
        got = drive_page(
            base,
            """
            await page.width(400);
            await page.chat(args.busy);
            const chip = () => page.js(() => ({
              hidden: document.querySelector("#outside-chip").hidden,
              count: document.querySelector("#outside-count").textContent }));
            await page.until(() => !document.querySelector("#outside-chip").hidden);
            const first = await chip();
            await page.shot("chip-busy-narrow.png");
            // The new session's record is slow to arrive: the old count must not wait with it.
            await page.js((sid) => {
              const real = window.fetch;
              window.hang = (url, options) => String(url).includes(`/${sid}/outside`)
                ? new Promise(() => {}) : real(url, options);
              window.realFetch = real;
              window.fetch = window.hang;
              select(sid);
            }, args.quiet);
            await page.until((sid) => state.sessionId === sid
              && document.querySelector("#title").value === "quiet", args.quiet);
            const waiting = await chip();
            await page.js(() => { window.fetch = window.realFetch; });
            await page.js((sid) => select(sid), args.busy);
            await page.until(() => !document.querySelector("#outside-chip").hidden);
            await page.js((sid) => select(sid), args.quiet);
            await page.until((sid) => state.sessionId === sid
              && document.querySelector("#title").value === "quiet", args.quiet);
            await page.js(() => new Promise((r) => setTimeout(r, 1200)));
            const switched = await chip();
            await page.shot("chip-quiet-narrow.png");
            await page.js((sid) => select(sid), args.busy);
            await page.until(() => !document.querySelector("#outside-chip").hidden);
            const back = await chip();
            await page.js((sid) => select(sid), args.lapsed);
            await page.until((sid) => state.sessionId === sid, args.lapsed);
            await page.until(() => !document.querySelector("#outside-chip").hidden);
            const lapsed = await chip();
            return { first, waiting, switched, back, lapsed };
            """,
            busy=busy,
            quiet=quiet,
            lapsed=lapsed,
            shots=shots,
        )
    assert got["first"] == {"hidden": False, "count": "2"}
    assert got["waiting"] == {"hidden": True, "count": "0"}
    assert got["switched"]["hidden"] is True
    assert got["switched"]["count"] == "0"
    assert got["back"] == {"hidden": False, "count": "2"}
    assert got["lapsed"] == {"hidden": False, "count": "1"}  # Ask lane, but its record is read
