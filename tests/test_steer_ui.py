"""Writing to the model while its turn runs (F40), in a real browser.

Known-good: during a running chat turn the composer still sends. Enter posts
the message, the box empties, and the message shows in the turn as the
person's, marked as waiting. When the turn takes it at its next step it loses
the mark and moves below what the model had already done, so the reply that
answers it reads after it. The turn was not stopped.

Known-bad: before F40 the page dropped a message typed during a turn (`send`
returned while busy) and the server refused it with 409, so nothing waited
and nothing reached the model.

Set SADDLE_STEER_SHOTS to a directory to keep screenshots of the page.
"""

from __future__ import annotations

import os
import threading
from pathlib import Path
from typing import Any

from chrome_page import drive_page
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import PlainTextResponse
from starlette.routing import Route
from test_ui3_mode import NoModel, serving

import saddle.web.app as module
from saddle.events import ContentDelta, MessageDelivered, TurnEnd, TurnStart
from saddle.sessions import SessionStore
from saddle.web.app import build_app

NOTE = "an error dialog says the disk is full"


def test_a_message_typed_during_a_turn_waits_then_joins_it(tmp_path: Path) -> None:
    shots = os.environ.get("SADDLE_STEER_SHOTS", "")
    store = SessionStore(tmp_path / "s")
    app = build_app(store, NoModel, default_workdir=tmp_path)
    release = threading.Event()
    steered: list[list[str]] = []
    stopped: list[bool] = []

    def held_turn(
        _c: Any, messages: list[dict[str, Any]], text: str | None, _o: Any, **kw: Any
    ) -> Any:
        messages.append({"role": "user", "content": text})
        yield TurnStart(turn=1, prompt=text or "")
        yield ContentDelta(text="working on it")
        release.wait(30)
        taken = [said for said, _images in kw["steer"]()]
        steered.append(taken)
        for said in taken:
            yield MessageDelivered(text=said)
        yield ContentDelta(text="got it")
        stopped.append(kw["cancel"]())
        yield TurnEnd(turn=1, proof="p")

    async def release_turn(_request: Request) -> PlainTextResponse:
        release.set()  # the page says it has shown the message as waiting
        return PlainTextResponse("ok")

    assert isinstance(app, Starlette)  # no token on this test server, so no wrapper
    app.router.routes.append(Route("/test-release", release_turn))
    original = module.run_turn
    module.run_turn = held_turn  # type: ignore[assignment]
    try:
        with serving(app) as base:
            sid = store.create(title="steer", workdir=str(tmp_path)).id
            got = drive_page(
                base,
                """
                const said = () => [...document.querySelectorAll(".turn > div")].map(
                  (n) => [n.className, n.textContent]);
                await page.chat(args.sid);
                await page.type("#input", "fix the build");
                await page.key("Enter");
                const shows = (text) => [...document.querySelectorAll(".turn .assistant")]
                  .some((n) => n.textContent.includes(text));
                await page.until(`(${shows})("working on it")`);
                const before = await page.js(() => document.querySelectorAll(".turn").length);
                await page.type("#input", args.note);
                await page.key("Enter");
                await page.until(() => document.querySelector(".user.queued") !== null);
                const waiting = await page.js(() => {
                  const node = document.querySelector(".user.queued");
                  return {
                    text: node.textContent,
                    title: node.title,
                    input: document.querySelector("#input").value,
                    turns: document.querySelectorAll(".turn").length,
                  };
                });
                await page.shot("steer-waiting.png");
                await page.js(() => fetch("/test-release"));
                await page.until(() => document.querySelector(".user.queued") === null
                  && [...document.querySelectorAll(".turn .assistant")]
                    .some((n) => n.textContent.includes("got it")));
                await page.shot("steer-delivered.png");
                return { waiting, before, order: await page.js(said) };
                """,
                sid=sid,
                note=NOTE,
                shots=shots,
            )
    finally:
        module.run_turn = original
    assert got["waiting"]["text"] == NOTE
    assert got["waiting"]["title"] != ""  # says why it is marked
    assert got["waiting"]["input"] == ""  # the box emptied: it was sent
    assert got["waiting"]["turns"] == got["before"]  # joined the running turn, no new one
    # Taken at the turn's next step, which was not stopped to do so.
    assert steered == [[NOTE]]
    assert stopped == [False]
    # The message sits below what the model had done, and the answer below it.
    texts = [text for _cls, text in got["order"]]
    assert texts.index("working on it") < texts.index(NOTE) < texts.index("got it")
    assert [cls for cls, text in got["order"] if text == NOTE] == ["user"]
