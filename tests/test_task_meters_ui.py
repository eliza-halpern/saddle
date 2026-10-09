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

import time
from pathlib import Path
from typing import Any

import pytest
from browser_guard import BROWSER
from chrome_page import drive_page, served_chat
from packet_seed import make_repo, seed

from saddle.packet import Packet, compile_packet

needs_chrome = pytest.mark.skipif(not BROWSER, reason="needs node and google-chrome")

BODY = """
await page.chat(args.sid);
// A new card paints its meters "0s" at once and keeps them hidden; the packet's numbers
// arrive in paintSpend, which unhides them. Shown, not merely filled, is the packet's.
await page.until(() => {
  const values = [...document.querySelectorAll(".task-card .tmeter-value")];
  const shown = (v) => v.textContent !== "—" && !v.closest("[hidden]");
  return values.length === 2 && values.every(shown);
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


@needs_chrome
def test_the_meters_are_read_once_the_packet_is_painted_however_slow_it_is(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Both tests above flaked on 2026-10-08, in a run's audit and in a full gate. Each read
    `0s` and `~0 tokens`, the numbers a card holds from the moment it is made. The packet's
    numbers reach a card in `paintSpend`, which also unhides its meters; until then its
    meters read `0s`, hidden. A slow packet route makes that window wide every time."""

    def slow(*args: Any, **kwargs: Any) -> Packet:
        time.sleep(1.5)
        return compile_packet(*args, **kwargs)

    monkeypatch.setattr("saddle.web.app.compile_packet", slow)  # the name the route calls
    meters = _meters(tmp_path)
    assert meters["sealed"]["time"] == {"value": "42s of 10m 00s", "track": True}, meters
    assert meters["unbudgeted"]["time"] == {"value": "42s", "track": False}, meters
