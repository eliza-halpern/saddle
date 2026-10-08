"""A stopped packet offers "Run again", which starts the same task again beside it (#85 item 6).

Decided for the owner on 2026-10-06: a stop is usually the person's own Stop or a stop
that needs them, and running the task again meant typing it again. The offer is a plain
"Run again" (budgets are off by default, so there is no "more budget" variant), on a
stopped packet only: a finished run's next step is to land it, a failed run's to read why.

The packets are seeded, sealed runs the page loads itself; the run a click starts goes to
a stand-in for `tasks.execute` that records what it was handed.

Known-bad: a stopped packet with no "Run again", or one that starts nothing or another
task. Known-good: one click starts one run of the same task with the budgets and the test
edits the packet records, and the stopped packet stays; a finished packet offers none.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from browser_guard import BROWSER
from chrome_page import drive_page, served_chat
from packet_seed import make_repo, seed

from saddle.auto import ledger_path
from saddle.packet import compile_packet
from saddle.web import tasks

needs_chrome = pytest.mark.skipif(not BROWSER, reason="needs node and google-chrome")

TASK = "make add add (stopped seed)"

BODY = """
await page.chat(args.sid);
await page.until(() => !!document.querySelector(".task-card .act-row"));
const offers = await page.js(() => [...document.querySelectorAll(".task-card button")]
  .filter((b) => !b.closest("[hidden]") && b.getClientRects().length > 0)
  .map((b) => b.textContent.trim()));
if (!args.click || !offers.includes("Run again")) return { offers, cards: [] };
await page.js(() => [...document.querySelectorAll(".task-card button")]
  .find((b) => b.textContent.trim() === "Run again").click());
await page.until(() => document.querySelectorAll(".task-card").length >= 2);
const cards = await page.js(() => [...document.querySelectorAll(".task-card")]
  .map((c) => c.dataset.state));
return { offers, cards };
"""


def _page(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, kind: str, *, click: bool
) -> tuple[dict[str, Any], list[dict[str, Any]], Any]:
    started: list[dict[str, Any]] = []

    def stand_in(run: tasks.TaskRun, *, publish: Any, **kw: Any) -> tuple[str, None]:
        started.append(
            {
                "task": run.task,
                "time_budget_s": run.time_budget_s,
                "token_budget": run.token_budget,
                "allow_test_edits": kw.get("allow_test_edits"),
            }
        )
        run.state = "finished"
        publish(run.state_event("done"))
        return run.state, None

    monkeypatch.setattr(tasks, "execute", stand_in)
    with served_chat(tmp_path) as site:
        repo = make_repo(tmp_path / "repo")
        sid = site.store.create(title="calc work", workdir=str(repo)).id
        _sid, rid, _branch = seed(site.store, repo, kind, sid=sid, task=TASK)  # type: ignore[arg-type]
        out: dict[str, Any] = drive_page(site.base, BODY, sid=sid, click=click)
    return out, started, compile_packet(ledger_path(repo, rid))


@needs_chrome
def test_run_again_starts_the_same_task_and_settings_and_the_stop_stays(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    out, started, packet = _page(tmp_path, monkeypatch, "stopped", click=True)
    assert packet.verdict == "stopped", packet.verdict
    assert out["offers"].count("Run again") == 1, out["offers"]
    assert packet.spend is not None
    assert started == [
        {
            "task": TASK,
            "time_budget_s": packet.spend["time_budget_s"],
            "token_budget": packet.spend["token_budget"],
            "allow_test_edits": packet.test_edits,
        }
    ], started
    assert out["cards"].count("stopped") == 1, out["cards"]  # the stopped packet stays
    assert len(out["cards"]) == 2, out["cards"]


@needs_chrome
def test_a_finished_packet_offers_no_run_again(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    out, started, packet = _page(tmp_path, monkeypatch, "audited", click=False)
    assert packet.verdict == "finished", packet.verdict
    assert "Run again" not in out["offers"], out["offers"]
    assert started == []
