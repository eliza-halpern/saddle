"""The page's process list (#136), in a real browser.

Known-good: while a session has programs running, the topbar shows a chip with
their count; its dialog lists each with its command and start time; "Stop" on
one stops that one, "Stop all" the rest; turning full access off stops what is
left and the transcript says what was stopped.

Known-bad: with nothing running the chip is not shown, and the dialog has no
"Stop all".

Set SADDLE_PROCESS_SHOTS to a directory to keep screenshots of the page.
"""

from __future__ import annotations

import os
import signal
import uuid
from pathlib import Path

import pytest
from chrome_page import drive_page
from test_chat_server import _server_of
from test_full_access import run
from test_memcap import needs_cgroup
from test_process_list import alive, detach, wait_for
from test_ui3_mode import NoModel, serving

from saddle.sessions import FULL_ACCESS_CONFIRM, SessionStore
from saddle.tools import ToolContext
from saddle.web.app import build_app


def sleep_marker() -> str:
    return f"{4000 + uuid.uuid4().int % 90000}.{uuid.uuid4().int % 1000:03d}"


@needs_cgroup
def test_the_page_lists_stops_one_stops_all_and_says_what_access_ending_stopped(
    tmp_path: Path,
) -> None:
    shots = os.environ.get("SADDLE_PROCESS_SHOTS", "")
    store = SessionStore(tmp_path / "s")
    app = build_app(store, NoModel, default_workdir=tmp_path)
    one, two, three = sleep_marker(), sleep_marker(), sleep_marker()
    pids: list[int] = []
    with serving(app) as base:
        sid = store.create(title="serve the site", workdir=str(tmp_path)).id
        store.update(sid, mode="edit")
        store.set_full_access(sid, True, confirm=FULL_ACCESS_CONFIRM)
        server = _server_of(app)
        ctx = ToolContext(workdir=tmp_path, full_access=True, processes=server.ledger(sid))
        server._live(sid).context = ctx
        try:
            for sleep in (one, two):
                run(ctx, "run_command", command=detach(sleep))
            first, second = wait_for(one, 1)[0], wait_for(two, 1)[0]
            pids += [first, second]
            got = drive_page(
                base,
                """
                await page.width(1100);
                await page.chat(args.sid);
                const text = (sel) => page.js((s) => document.querySelector(s).textContent, sel);
                await page.until(() => !document.querySelector("#procs-chip").hidden);
                const chip = await text("#procs-chip");
                await page.width(420);
                const narrow = await page.js(
                  () => document.documentElement.scrollWidth <= window.innerWidth);
                await page.shot("processes-narrow.png");
                await page.width(1100);
                await page.click("#procs-chip");
                await page.until(() => document.querySelectorAll("#procs-list .proc").length === 2);
                const rows = await page.js(() => [...document.querySelectorAll("#procs-list .proc")]
                  .map((r) => r.querySelector(".proc-command").textContent));
                const meta = await text("#procs-list .proc-meta");
                await page.shot("processes-dialog.png");
                await page.click("#procs-list .proc button");
                await page.until(() => document.querySelectorAll("#procs-list .proc").length === 1);
                const names = () => [...document.querySelectorAll("#procs-list .proc-command")]
                  .map((r) => r.textContent);
                const afterOne = await page.js(names);
                await page.click("#procs-stop-all");
                await page.until(() => document.querySelector("#procs-chip").hidden);
                const afterAll = await page.js(() => ({
                  chipHidden: document.querySelector("#procs-chip").hidden,
                  stopAll: document.querySelector("#procs-stop-all").hidden,
                  empty: !document.querySelector("#procs-empty").hidden,
                  notices: [...document.querySelectorAll(".notice")].map((n) => n.textContent),
                }));
                await page.shot("processes-empty.png");
                await page.click("#procs-close");
                return { chip, narrow, rows, meta, afterOne, afterAll };
                """,
                sid=sid,
                shots=shots,
            )
            assert got["chip"] == "2 running"
            assert got["narrow"] is True  # no sideways scroll on a phone
            assert sorted(got["rows"]) == sorted([f"sleep {one}", f"sleep {two}"])
            assert "started" in got["meta"]
            assert len(got["afterOne"]) == 1
            assert got["afterAll"]["chipHidden"] is True
            assert got["afterAll"]["stopAll"] is True  # known-bad: nothing to stop all of
            assert got["afterAll"]["empty"] is True
            assert any("Stopped 1 program" in n for n in got["afterAll"]["notices"])
            assert not alive(first)
            assert not alive(second)

            run(ctx, "run_command", command=detach(three))
            pids.append(wait_for(three, 1)[0])
            ended = drive_page(
                base,
                """
                await page.width(1100);
                await page.chat(args.sid);
                await page.until(() => !document.querySelector("#procs-chip").hidden);
                await page.click("#full-access-off");
                await page.until(() => document.querySelector("#procs-chip").hidden);
                await page.shot("processes-after-turn-off.png");
                return await page.js(() =>
                  [...document.querySelectorAll(".notice")].map((n) => n.textContent));
                """,
                sid=sid,
                shots=shots,
            )
            assert any(f"sleep {three}" in n and "Stopped 1 program" in n for n in ended), ended
            assert not alive(pids[-1])
        finally:
            ctx.stop_processes()
            for pid in pids:
                if alive(pid):
                    os.kill(pid, signal.SIGKILL)


def test_with_nothing_running_the_chip_is_not_shown(tmp_path: Path) -> None:
    store = SessionStore(tmp_path / "s")
    app = build_app(store, NoModel, default_workdir=tmp_path)
    with serving(app) as base:
        sid = store.create(title="quiet", workdir=str(tmp_path)).id
        got = drive_page(
            base,
            """
            await page.chat(args.sid);
            await page.js(() => new Promise((r) => setTimeout(r, 800)));
            return await page.js(() => document.querySelector("#procs-chip").hidden);
            """,
            sid=sid,
        )
    assert got is True


@needs_cgroup
def test_on_a_phone_the_chip_next_to_the_unsandboxed_banner_causes_no_sideways_scroll(
    tmp_path: Path,
) -> None:
    """With the chip, the full-access banner, the lane chip and the folder name
    all in the topbar at 420px the page must not scroll sideways (main, without
    the chip, does not). The chip shrinks to its dot and count."""
    shots = os.environ.get("SADDLE_PROCESS_SHOTS", "")
    store = SessionStore(tmp_path / "s")
    app = build_app(store, NoModel, default_workdir=tmp_path)
    with serving(app) as base:
        sid = store.create(title="serve the site", workdir=str(tmp_path)).id
        store.update(sid, mode="edit")
        store.set_full_access(sid, True, confirm=FULL_ACCESS_CONFIRM)
        server = _server_of(app)
        ctx = ToolContext(workdir=tmp_path, full_access=True, processes=server.ledger(sid))
        server._live(sid).context = ctx
        try:
            run(ctx, "run_command", command=f"sleep {sleep_marker()}", background=True)
            got = drive_page(
                base,
                """
                await page.width(420);
                await page.chat(args.sid);
                await page.until(() => !document.querySelector("#procs-chip").hidden);
                await page.shot("phone-chip.png");
                return await page.js(() => ({
                  wide: document.documentElement.scrollWidth - window.innerWidth,
                  topbar: document.querySelector("#topbar").scrollWidth
                    - document.querySelector("#topbar").clientWidth,
                  banner: !document.querySelector("#full-access-banner").hidden,
                  count: document.querySelector("#procs-count").textContent,
                }));
                """,
                sid=sid,
                shots=shots,
            )
        finally:
            ctx.stop_processes()
    assert got == {"wide": 0, "topbar": 0, "banner": True, "count": "1"}


@needs_cgroup
def test_on_a_phone_both_chips_and_the_banner_cause_no_sideways_scroll(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The two lanes' chips met in one topbar: running programs and outside
    changes, beside the full-access banner, at 420px. Each keeps only its
    count, the folder name gives way, and the page does not scroll sideways."""
    from saddle.sideeffects import SideEffects

    home = tmp_path / "home"
    home.mkdir()
    (home / "app.conf").write_text("theme=dark\n")
    monkeypatch.setenv("HOME", str(home))
    store = SessionStore(tmp_path / "s")
    app = build_app(store, NoModel, default_workdir=tmp_path)
    with serving(app) as base:
        sid = store.create(title="set up the launcher", workdir=str(tmp_path)).id
        store.update(sid, mode="edit")
        store.set_full_access(sid, True, confirm=FULL_ACCESS_CONFIRM)
        server = _server_of(app)
        ctx = ToolContext(
            workdir=tmp_path,
            full_access=True,
            processes=server.ledger(sid),
            effects=SideEffects(store.outside_dir(sid)),
        )
        server._live(sid).context = ctx
        try:
            run(ctx, "run_command", command=f"sed -i s/dark/light/ {home / 'app.conf'}")
            run(ctx, "run_command", command=f"sleep {sleep_marker()}", background=True)
            got = drive_page(
                base,
                """
                await page.width(420);
                await page.chat(args.sid);
                await page.until(() => !document.querySelector("#procs-chip").hidden
                  && !document.querySelector("#outside-chip").hidden);
                return await page.js(() => ({
                  wide: document.documentElement.scrollWidth - window.innerWidth,
                  topbar: document.querySelector("#topbar").scrollWidth
                    - document.querySelector("#topbar").clientWidth,
                  procs: document.querySelector("#procs-count").textContent,
                  outside: document.querySelector("#outside-count").textContent,
                }));
                """,
                sid=sid,
            )
        finally:
            ctx.stop_processes()
    assert got == {"wide": 0, "topbar": 0, "procs": "1", "outside": "1"}
