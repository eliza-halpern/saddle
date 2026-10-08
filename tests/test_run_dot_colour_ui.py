"""A stopped run's dot in the Runs group is not drawn in the failed colour (#85 item 6).

The card, its verdict word and the session list's dot show a stopped or unchanged run in
--ask; the Runs group drew its dot in --bad, the failed colour, so a run the person ended
read as a failure there and nowhere else.

The rows are painted by `renderRuns` (runs.js) itself, in a real browser, in both themes.

Known-bad: a stopped run's dot resolves to --bad. Known-good: a stopped or unchanged run's
dot resolves to --ask, and a failed run's dot keeps --bad.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from browser_guard import BROWSER
from chrome_page import drive_page, served_chat

needs_chrome = pytest.mark.skipif(not BROWSER, reason="needs node and google-chrome")

STATES = ["stopped", "unchanged", "failed"]

BODY = """
await page.chat(args.sid);
return await page.js((theme, states) => {
  document.documentElement.dataset.theme = theme;
  const now = Date.now() / 1000;
  renderRuns({
    now,
    runs: states.map((state) => ({
      run_id: `run-${state}`, session_id: "s", session_title: "a session", task: `a ${state} task`,
      state, live: false, started: now - 60, ended: now - 30, state_since: now - 30, lane: "small",
    })),
  });
  const resolved = (name) => {
    const probe = document.createElement("span");
    probe.style.color = `var(${name})`;
    document.body.appendChild(probe);
    const colour = getComputedStyle(probe).color;
    probe.remove();
    return colour;
  };
  const dots = {};
  for (const state of states) {
    const dot = document.querySelector(`#run-list .run-state-dot.rs-${state}`);
    dots[state] = getComputedStyle(dot).color;
  }
  return { dots, ask: resolved("--ask"), bad: resolved("--bad") };
}, args.theme, args.states);
"""


def _read(tmp_path: Path, theme: str) -> dict[str, Any]:
    with served_chat(tmp_path) as site:
        out: dict[str, Any] = drive_page(site.base, BODY, sid=site.sid, theme=theme, states=STATES)
    assert out["ask"] != out["bad"], out  # the two colours this test tells apart differ
    return out


@pytest.mark.parametrize("theme", ["light", "dark"])
@needs_chrome
def test_a_stopped_or_unchanged_run_dot_is_ask_and_a_failed_one_is_bad(
    tmp_path: Path, theme: str
) -> None:
    out = _read(tmp_path, theme)
    assert out["dots"]["stopped"] == out["ask"], out
    assert out["dots"]["unchanged"] == out["ask"], out
    assert out["dots"]["failed"] == out["bad"], out
