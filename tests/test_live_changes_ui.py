"""A running task card shows its changes so far, while the run keeps going (#85 item 11).

The run is a stand-in for `tasks.execute` that makes its worktree on its own branch, as a
real run does, writes one uncommitted new file, and holds until it is stopped. The page
starts it the way a person does, then uses the card's own control.

Known-bad: a running card that offers only Stop, or a control that shows nothing of the
file. Known-good: "Changes so far" beside Stop while the run is going; one click draws
the new file and its added line, the run is still running and Stop is still offered; a
second click folds them away; an ended card offers no live changes.
"""

from __future__ import annotations

import threading
import time
from pathlib import Path
from typing import Any

import pytest
from browser_guard import BROWSER
from chrome_page import drive_page, served_chat
from packet_seed import git, make_repo

from saddle.web import tasks

needs_chrome = pytest.mark.skipif(not BROWSER, reason="needs node and google-chrome")

LIVE_FILE = "live_change.py"
LIVE_TEXT = "LIVE_MARKER = 1"

BODY = """
await page.chat(args.sid);
await page.js((sid) => fetch(`/api/sessions/${sid}/task`, {
  method: "POST", headers: { "Content-Type": "application/json" },
  body: JSON.stringify({ text: "hold this run open while we look" }),
}).then((r) => r.status), args.sid);
await page.until(() => !!document.querySelector(".task-card[data-state=running] .task-changes"));
await page.until(async (sid) => {
  const rid = document.querySelector(".task-card").dataset.run;
  const got = await fetch(`/api/sessions/${sid}/tasks/${rid}/changes`);
  return got.ok && (await got.json()).files.length > 0;
}, args.sid);
const offered = await page.js(() => [...document.querySelectorAll(".task-card button")]
  .filter((b) => !b.closest("[hidden]") && b.getClientRects().length > 0)
  .map((b) => b.textContent.trim()));
await page.js(() => document.querySelector(".task-card .task-changes").click());
await page.until(() => !!document.querySelector(".task-card .task-live .diff-file"));
const going = await page.js((offered) => {
  const card = document.querySelector(".task-card");
  return { offered, state: card.dataset.state, live: card.querySelector(".task-live").innerText };
}, offered);
await page.js(() => document.querySelector(".task-card .task-changes").click());
await page.until(() => document.querySelector(".task-card .task-live").hidden);  // folded away
await page.js(() => document.querySelector(".task-card .task-stop").click());
await page.until(() => document.querySelector(".task-card").dataset.state === "stopped");
const ended = await page.js(() => {
  const card = document.querySelector(".task-card");
  return {
    changes: !card.querySelector(".task-changes").hidden,
    live: !card.querySelector(".task-live").hidden,
  };
});
return { ...going, ended };
"""


@needs_chrome
def test_a_running_card_shows_its_changes_so_far_and_keeps_running(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    held: list[tasks.TaskRun] = []

    def hold(run: tasks.TaskRun, *, publish: Any, workdir: Path, **_: Any) -> tuple[str, None]:
        base = git(workdir, "rev-parse", "HEAD").strip()
        branch = f"saddle/auto/live{run.run_id[:8]}"
        worktree = workdir.parent / f"wt-{run.run_id[:8]}"
        git(workdir, "worktree", "add", "-q", "-b", branch, str(worktree), base)
        (worktree / LIVE_FILE).write_text(LIVE_TEXT + "\n")
        run.worktree, run.branch, run.base = worktree, branch, base
        held.append(run)
        publish(run.state_event())
        deadline = time.monotonic() + 60
        while time.monotonic() < deadline and not run.cancelled:
            time.sleep(0.05)
        run.state = "stopped"
        publish(run.state_event("done"))
        return run.state, None

    monkeypatch.setattr(tasks, "execute", hold)
    with served_chat(tmp_path) as site:
        repo = make_repo(tmp_path / "repo")
        sid = site.store.create(title="calc work", workdir=str(repo)).id
        try:
            out: dict[str, Any] = drive_page(site.base, BODY, sid=sid)
        finally:
            for run in held:
                run.cancelled = True
            for thread in threading.enumerate():
                if thread is not threading.current_thread() and thread.daemon:
                    thread.join(timeout=0.5)
    assert "Changes so far" in out["offered"], out["offered"]
    assert "Stop" in out["offered"], out["offered"]
    assert out["state"] == "running", out
    assert LIVE_FILE in out["live"], out["live"]
    assert LIVE_TEXT in out["live"], out["live"]
    assert out["ended"] == {"changes": False, "live": False}, out["ended"]  # its packet's now
