"""Drive the chat page in a real Chrome from a test, in a few lines.

A whole browser test::

    from chrome_page import drive_page, served_chat

    def test_the_title_field_shows_the_session_title(tmp_path: Path) -> None:
        with served_chat(tmp_path, title="parser work") as site:
            got = drive_page(
                site.base,
                '''
                await page.chat(args.sid);
                return await page.js(() => document.querySelector("#title").value);
                ''',
                sid=site.sid,
            )
        assert got == "parser work"

`drive_page` runs the body -- the inside of an async JavaScript function of
`page` and `args` -- on a fresh headless Chrome page (tests/fixtures/page_cdp.mjs)
and returns what it returns, as JSON. Keyword arguments become `args`. The page
API (`goto`, `chat`, `js`, `until`, `click`, `type`, `key`, `width`, `shot`,
`send`) is listed at the top of tests/fixtures/cdp_page.mjs. A function given to
`page.js` or `page.until` runs inside the page; values passed after it reach it
as JSON, so it reads nothing from the test's own scope.

Without node and Chrome the test is skipped; with SADDLE_REQUIRE_BROWSER set,
importing this module fails instead (browser_guard). A body that throws, or a
`page.until` that times out, fails the test with the page's message.

A module that drives a page must be listed under `chrome_tests` in
tests/fixtures/js_coverage_scope.json: the audit reads page-script coverage only
from the modules listed there (tests/test_chrome_audit.py checks every module
that imports this one is).
"""

from __future__ import annotations

import json
import subprocess
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest
from browser_guard import BROWSER
from test_ui3_mode import NoModel, serving

from saddle.sessions import SessionStore
from saddle.web.app import build_app

PAGE_CDP = Path(__file__).resolve().parent / "fixtures" / "page_cdp.mjs"


@dataclass(frozen=True)
class ChatSite:
    """A served chat page with one session: `base` is its URL, `sid` the session."""

    base: str
    store: SessionStore
    sid: str


@contextmanager
def served_chat(tmp_path: Path, *, title: str = "t", **app_options: Any) -> Iterator[ChatSite]:
    """The chat app on a free local port, with one session titled `title`; no model.
    `app_options` reach `build_app` (a `capability_probe`, say)."""
    store = SessionStore(tmp_path / "sessions")
    app = build_app(store, NoModel, default_workdir=tmp_path, **app_options)
    with serving(app) as base:
        sid = store.create(title=title, workdir=str(tmp_path)).id
        yield ChatSite(base, store, sid)


def drive_page(
    base: str,
    body: str,
    *,
    width: int = 1280,
    scheme: str = "light",
    shots: str = "",
    timeout: float = 60,
    **args: Any,
) -> Any:
    """Run `body` on a fresh Chrome page opened against `base`; return its result.

    `args` reach the body as `args`. Skips without node and Chrome."""
    if not BROWSER:
        pytest.skip("needs node and google-chrome")
    request = {"body": body, "args": args, "width": width, "scheme": scheme, "shots": shots}
    ran = subprocess.run(
        ["node", str(PAGE_CDP), base],
        input=json.dumps(request),
        capture_output=True,
        text=True,
        timeout=timeout,
        check=False,
    )
    if ran.returncode != 0:
        pytest.fail(f"the page script failed:\n{ran.stderr.strip()}", pytrace=False)
    return json.loads(ran.stdout.strip().splitlines()[-1])
