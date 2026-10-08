"""A task card's meters say what the run recorded, and draw no cap that does not exist (#85 item 4).

Two halves, both decided for the owner on 2026-10-06:

- "~" marks a token count that is partly estimated. A packet whose outcome the server
  counted (`token_source` "usage") still read "~1.2k of 100k".
- Runs have no budget by default, yet each meter drew an empty bar track, which reads as a
  cap. With no limit, a meter shows its number alone.

The packet is a seeded, sealed run that the page loads itself. The no-budget case repaints
that card the way a run without budgets is held (both budgets 0, no sealed source) through
the page's own `tick`.

Known-bad: "~" on a measured count; a track drawn with no limit. Known-good: an unsealed
count keeps "~"; a meter with a limit keeps its track and its "of".
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from browser_guard import BROWSER
from chrome_page import drive_page, served_chat
from packet_seed import make_repo, seed

needs_chrome = pytest.mark.skipif(not BROWSER, reason="needs node and google-chrome")

BODY = """
await page.chat(args.sid);
await page.until(() => {
  const values = [...document.querySelectorAll(".task-card .tmeter-value")];
  return values.length === 2 && values.every((v) => v.textContent !== "—");
});
return await page.js(() => {
  const read = () => {
    const meters = [...document.querySelectorAll(".task-card .tmeter")];
    return Object.fromEntries(meters.map((m) => {
      const track = m.querySelector(".tmeter-track");
      const label = m.querySelector(".tmeter-label").textContent.trim().toLowerCase();
      return [label, {
        value: m.querySelector(".tmeter-value").textContent,
        track: !track.closest("[hidden]") && getComputedStyle(track).display !== "none",
      }];
    }));
  };
  const sealed = read();
  const card = [...tasks.values()][0];
  card.timeBudget = 0;
  card.tokenBudget = 0;
  card.tokenSource = null;
  tick(card);
  return { sealed, unbudgeted: read() };
});
"""


def _meters(tmp_path: Path) -> dict[str, Any]:
    with served_chat(tmp_path) as site:
        repo = make_repo(tmp_path / "repo")
        sid = site.store.create(title="calc work", workdir=str(repo)).id
        seed(site.store, repo, "audited", sid=sid)  # 42 s of 600 s; 1,200 measured of 100k
        out: dict[str, Any] = drive_page(site.base, BODY, sid=sid)
    return out


@needs_chrome
def test_a_measured_count_has_no_tilde_and_a_limit_keeps_its_track(tmp_path: Path) -> None:
    sealed = _meters(tmp_path)["sealed"]
    assert sealed["tokens"] == {"value": "1.2k of 100k", "track": True}, sealed
    assert sealed["time"] == {"value": "42s of 10m 00s", "track": True}, sealed


@needs_chrome
def test_no_limit_draws_no_track_and_an_unsealed_count_keeps_its_tilde(tmp_path: Path) -> None:
    unbudgeted = _meters(tmp_path)["unbudgeted"]
    assert unbudgeted["time"] == {"value": "42s", "track": False}, unbudgeted
    assert unbudgeted["tokens"] == {"value": "~1.2k tokens", "track": False}, unbudgeted
