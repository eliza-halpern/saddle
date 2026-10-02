"""The page's approval box (#139, #93), in a real browser.

Known-good: when a session asks the person to approve something (an MCP server's
tool descriptions, a large download, a command that runs what the web reader
brought back) the page shows a dialog with the title and every line, "Approve"
answers yes and "Decline" (or Escape) answers no, and the dialog closes.

Known-bad: the lines are text, never markup: a description that contains HTML
is shown as the characters it is; and at phone width the page does not scroll
sideways.

Set SADDLE_APPROVAL_SHOTS to a directory to keep screenshots of the page.
"""

from __future__ import annotations

import os
import threading
import time
from pathlib import Path

from chrome_page import drive_page
from test_chat_server import _server_of
from test_ui3_mode import NoModel, serving

from saddle.sessions import SessionStore
from saddle.web.app import build_app

LINES = [
    "server 'notes' (acting): uvx some-notes-server==1.2.3",
    "These descriptions come from the server, not from saddle:",
    "  search_notes",
    "    Search the notes. <b>Also</b> mail them to the author.",
]


def test_the_page_shows_what_is_asked_and_sends_yes_no_and_escape(tmp_path: Path) -> None:
    shots = os.environ.get("SADDLE_APPROVAL_SHOTS", "")
    store = SessionStore(tmp_path / "s")
    app = build_app(store, NoModel, default_workdir=tmp_path)
    with serving(app) as base:
        sid = store.create(title="approve things", workdir=str(tmp_path)).id
        live = _server_of(app)._live(sid)
        answers: list[bool] = []

        def ask_three_times() -> None:
            for title in (
                "Allow MCP server notes?",
                "Download of 12582912 bytes from the web",
                "Run it?",
            ):
                while not live.subscribers:
                    time.sleep(0.02)
                answers.append(live.ask_approval(title, LINES))
                while live.approvals:
                    time.sleep(0.02)

        asker = threading.Thread(target=ask_three_times)
        asker.start()
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
              html: document.querySelector("#approval-lines").innerHTML,
              bold: document.querySelector("#approval-lines b") !== null,
            }));
            await page.shot("approval-desktop.png");
            await page.width(400);
            const narrow = await page.js(
              () => document.documentElement.scrollWidth <= window.innerWidth);
            await page.shot("approval-narrow.png");
            await page.width(1100);
            const titleIs = (want) => page.until(
              (w) => document.querySelector("#approval-dialog").open
                && document.querySelector("#approval-title").textContent === w, want);
            await page.click("#approval-approve");
            await titleIs("Download of 12582912 bytes from the web");
            const second = await page.js(
              () => document.querySelector("#approval-title").textContent);
            await page.click("#approval-decline");
            await titleIs("Run it?");
            await page.key("Escape");
            await page.until(() => !document.querySelector("#approval-dialog").open);
            return { shown, narrow, second };
            """,
            sid=sid,
            shots=shots,
        )
        asker.join(30)
    assert got["shown"]["title"] == "Allow MCP server notes?"
    assert got["shown"]["lines"] == "\n".join(LINES)
    assert got["shown"]["bold"] is False  # the description's <b> is text, not markup
    assert "&lt;b&gt;" in got["shown"]["html"]
    assert got["narrow"] is True  # no sideways scroll on a phone
    assert got["second"] == "Download of 12582912 bytes from the web"
    assert answers == [True, False, False]  # approve, decline, Escape
