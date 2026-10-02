"""The page's side-effect record (#137), in a real browser.

Known-good: after a full-access session edited a config file, downloaded an
archive and ran a script, the topbar chip counts the changes; its dialog lists
the file as changed, the download with its host and size, and the script under
"not tracked"; Restore puts the config back byte for byte; removing created
files asks first and Keep removes nothing.

Known-bad: a session that changed nothing outside has no chip.

Set SADDLE_OUTSIDE_SHOTS to a directory to keep screenshots of the page.
"""

from __future__ import annotations

import functools
import http.server
import json
import os
import shutil
import threading
from collections.abc import Iterator
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
def archive_server(tmp_path: Path) -> Iterator[str]:
    root = tmp_path / "www"
    root.mkdir()
    (root / "pkg.tgz").write_bytes(b"z" * 4096)
    handler = functools.partial(http.server.SimpleHTTPRequestHandler, directory=str(root))
    httpd = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{httpd.server_address[1]}"
    httpd.shutdown()
    httpd.server_close()


def run(ctx: ToolContext, command: str) -> None:
    call = ToolCall(id="c", name="run_command", arguments=json.dumps({"command": command}))
    execute_tool(call, workdir=ctx.workdir, context=ctx)


@pytest.mark.skipif(shutil.which("curl") is None, reason="needs curl")
def test_the_page_shows_the_record_restores_and_asks_before_removing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, archive_server: str
) -> None:
    shots = os.environ.get("SADDLE_OUTSIDE_SHOTS", "")
    home = tmp_path / "home"
    (home / ".config").mkdir(parents=True)
    monkeypatch.setenv("HOME", str(home))
    config = home / ".config" / "app.conf"
    original = b"theme=dark\r\nkeep=\xff\n"
    config.write_bytes(original)
    folder = tmp_path / "folder"
    folder.mkdir()
    (folder / "install.sh").write_text(f"#!/bin/sh\necho hi > {home}/ghost.txt\n")
    store = SessionStore(tmp_path / "s")
    app = build_app(store, NoModel, default_workdir=folder)
    with serving(app) as base:
        sid = store.create(title="set up the launcher", workdir=str(folder)).id
        store.update(sid, mode="edit")
        store.set_full_access(sid, True, confirm=FULL_ACCESS_CONFIRM)
        server = _server_of(app)
        ctx = ToolContext(
            workdir=folder,
            full_access=True,
            effects=SideEffects(store.outside_dir(sid)),
            processes=server.ledger(sid),
        )
        server._live(sid).context = ctx
        run(ctx, f"sed -i s/dark/light/ {config}")
        target = f"{home}/Downloads/pkg.tgz"
        run(ctx, f"mkdir -p {home}/Downloads && curl -s -o {target} {archive_server}/pkg.tgz")
        run(ctx, "bash install.sh")
        got = drive_page(
            base,
            """
            await page.width(1100);
            await page.chat(args.sid);
            const text = (sel) => page.js((s) => document.querySelector(s).textContent, sel);
            await page.until(() => !document.querySelector("#outside-chip").hidden);
            const chip = await text("#outside-chip");
            await page.width(400);
            const narrow = await page.js(
              () => document.documentElement.scrollWidth <= window.innerWidth);
            await page.shot("outside-narrow.png");
            await page.click("#outside-chip");
            await page.until(
              () => document.querySelectorAll("#outside-files .outside-row").length > 0);
            await page.shot("outside-dialog-narrow.png");
            await page.width(1100);
            const rows = (sel) => page.js((s) => [...document.querySelectorAll(s + " .outside-row")]
              .map((r) => r.textContent), sel);
            const files = await rows("#outside-files");
            const downloads = await rows("#outside-downloads");
            const untracked = await rows("#outside-untracked");
            const limits = await text("#outside-limits");
            await page.shot("outside-dialog.png");
            await page.click("#outside-remove");
            const asked = await page.js(() => !document.querySelector("#outside-confirm").hidden);
            await page.shot("outside-confirm.png");
            await page.click("#outside-confirm-no");
            const stillThere = await page.js(
              () => document.querySelector("#outside-confirm").hidden);
            await page.click("#outside-restore");
            await page.until(() => [...document.querySelectorAll(".notice")]
              .some((n) => n.textContent.startsWith("Undo: 1 restored")));
            await page.click("#outside-remove");
            await page.click("#outside-confirm-yes");
            await page.until(() => [...document.querySelectorAll(".notice")]
              .some((n) => n.textContent.includes("removed")));
            const after = await rows("#outside-files");
            await page.shot("outside-after-undo.png");
            return { chip, narrow, files, downloads, untracked, limits, asked, stillThere, after };
            """,
            sid=sid,
            shots=shots,
        )
        assert got["chip"].endswith("outside changes")
        assert got["narrow"] is True
        joined = "\n".join(got["files"])
        assert "app.conf" in joined
        assert "changed" in joined
        assert "backed up" in joined
        assert any("pkg.tgz" in row and "4.0 KB" in row for row in got["downloads"])
        assert any("127.0.0.1" in row for row in got["downloads"])
        assert len(got["untracked"]) == 1
        assert "bash install.sh" in got["untracked"][0]
        assert "not tracked" in got["untracked"][0]
        assert "paths it names" in got["limits"]
        assert got["asked"] is True
        assert got["stillThere"] is True
        assert config.read_bytes() == original
        assert not (home / "Downloads").exists()
        assert (home / "ghost.txt").exists()  # the script's file was never claimed
        assert all("app.conf" not in row for row in got["after"])


def test_a_session_that_changed_nothing_outside_has_no_chip(tmp_path: Path) -> None:
    store = SessionStore(tmp_path / "s")
    app = build_app(store, NoModel, default_workdir=tmp_path)
    with serving(app) as base:
        sid = store.create(title="quiet", workdir=str(tmp_path)).id
        store.update(sid, mode="edit")
        got = drive_page(
            base,
            """
            await page.chat(args.sid);
            await page.js(() => new Promise((r) => setTimeout(r, 800)));
            return await page.js(() => document.querySelector("#outside-chip").hidden);
            """,
            sid=sid,
        )
    assert got is True
