"""In a task session, the sidebar dims the controls a task run does not read (#85 item 10).

`ChatServer._run_task` hands a run the session's thinking effort and nothing else of it:
no persona and no temperature. Temperature was already dimmed in task mode; Persona was
not, so it looked as if it shaped the run.

The lane is switched with the page's own `showMode`, in a real browser.

Known-bad: Persona at full strength in a task session. Known-good: Persona and Temperature
dimmed there, Thinking at full strength; in a chat session all three at full strength.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from browser_guard import BROWSER
from chrome_page import drive_page, served_chat

needs_chrome = pytest.mark.skipif(not BROWSER, reason="needs node and google-chrome")

CONTROLS = ["persona", "temp", "effort"]

BODY = """
await page.chat(args.sid);
return await page.js((lanes, controls) => {
  const seen = {};
  for (const lane of lanes) {
    showMode(lane);
    seen[lane] = {};
    for (const id of controls) {
      const label = document.querySelector(`.side-foot label:has(#${id})`);
      const style = getComputedStyle(label);
      seen[lane][id] = { opacity: Number(style.opacity), shown: style.display !== "none" };
    }
  }
  return seen;
}, args.lanes, args.controls);
"""


def _seen(tmp_path: Path) -> dict[str, Any]:
    with served_chat(tmp_path) as site:
        out: dict[str, Any] = drive_page(
            site.base, BODY, sid=site.sid, lanes=["task", "ask"], controls=CONTROLS
        )
    return out


@needs_chrome
def test_a_task_session_dims_persona_and_temperature_and_keeps_thinking(tmp_path: Path) -> None:
    task = _seen(tmp_path)["task"]
    assert all(task[c]["shown"] for c in CONTROLS), task
    assert task["persona"]["opacity"] < 1, task
    assert task["temp"]["opacity"] < 1, task
    assert task["effort"]["opacity"] == 1, task


@needs_chrome
def test_a_chat_session_keeps_every_control_at_full_strength(tmp_path: Path) -> None:
    ask = _seen(tmp_path)["ask"]
    assert {c: ask[c] for c in CONTROLS} == {c: {"opacity": 1, "shown": True} for c in CONTROLS}
