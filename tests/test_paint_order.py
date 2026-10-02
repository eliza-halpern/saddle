"""A streamed reply is painted once, whichever comes first: its frame or the turn's end.

The page paints a streamed delta on the next animation frame (`schedulePaint`)
and paints at once when the turn ends, cancelling a frame still pending
(`flushPaint`). Which of the two came first was left to timing, so each path
ran on some runs only: the page-script coverage of `app.js` changed from run to
run with no code change. The driver (`tests/fixtures/paint_cdp.mjs`) calls the
page's own event handler in each order on purpose.

Known-good: in every round the delta is not painted before its frame or the
turn's end, is painted exactly once by either, and leaves no frame pending.
Known-bad: if the turn's end cancels the pending frame but leaves its id behind,
the end-first round reports a frame left, and the next round's `schedulePaint`
returns early, so that reply is never painted. (The frame callback's own reset
is redundant: the `flushPaint` it calls resets a pending id itself, so removing
it changes nothing a test can see.)
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest
from browser_guard import BROWSER
from test_ui3_mode import NoModel, serving

from saddle.sessions import SessionStore
from saddle.web.app import build_app

CDP = Path(__file__).resolve().parent / "fixtures" / "paint_cdp.mjs"


@pytest.mark.skipif(not BROWSER, reason="needs node and google-chrome")
def test_a_streamed_reply_is_painted_once_whichever_comes_first(tmp_path: Path) -> None:
    store = SessionStore(tmp_path / "s")
    app = build_app(store, NoModel, default_workdir=tmp_path)
    with serving(app) as base:
        sid = store.create(title="t", workdir=str(tmp_path)).id
        out = subprocess.run(
            ["node", str(CDP), base, sid],
            capture_output=True,
            text=True,
            timeout=60,
            check=False,
        )
    assert out.returncode == 0, out.stderr
    rounds = json.loads(out.stdout.strip().splitlines()[-1])["rounds"]
    once = {"pending": True, "beforePaint": 0, "painted": 1, "frameLeft": 0}
    assert rounds == [
        {"order": "frame-first", **once, "atEnd": 1},
        {"order": "frame-first", **once, "atEnd": 1},
        {"order": "end-first", **once, "afterFrames": 1},
        {"order": "frame-first", **once, "atEnd": 1},
    ]
