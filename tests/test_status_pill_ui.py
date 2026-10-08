"""A task session's status pill, in a real browser (#85 item 3).

The run's own card carries its state -- "● running", "? needs you", "■ stopped"
-- and, in a session in the task lane, the topbar's pill read that state beside
it: the same thing stated twice on one screen. The rule the page now follows is
`PILL_STATES` (tasks.js) and `statusShown` (app.js): the pill keeps only what
that card leaves unsaid once it has scrolled off the screen. Each run state is
read twice in the same pass -- the card's own pill and the topbar's -- so what
the page shows is on the record.

Known-good: a task session whose run needs you, ended stopped, or ended with no
outcome shows the pill, with the face and the name that state has, at a phone's
width as well as at a wide one -- on a phone the topbar is the only place left
once the card scrolls away.

Known-bad, the defect as reported: while a run was going the topbar read "task
running" beside the card's own "● running", and it read "finished" and
"unchanged" the same way. The other halves are a hidden pill that still fills a
box, and a kept state that renders as nothing at a phone's width.

A session switched to Ask is a chat session and keeps its pill for every state,
shown at every width.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from browser_guard import BROWSER
from chrome_page import drive_page, served_chat

needs_chrome = pytest.mark.skipif(not BROWSER, reason="needs node and google-chrome")

RUN = "pill-ui-run"

# Every state a Task run reports, and what the topbar's pill does with it: None
# where the run's own card reads that state, so the pill holds nothing; else the
# word the pill reads and the name a screen reader is given (#85 item 3).
RUN_STATES: dict[str, tuple[str, str] | None] = {
    "running": None,
    "loading": None,
    "finished": None,
    "unchanged": None,
    "needs_you": ("needs you", "needs"),
    "asked": ("needs you", "needs"),
    "stopped": ("stopped", "stopped"),
    "failed": ("no outcome", "failed"),
    # This page's Stop button asked for it; no run state spells it out.
    "stopping": ("stopping…", "working"),
}
# The states the run's own card spells out for itself: there the pill holds
# nothing, because the card beside it already says the state.
CARD_OWN = {state for state, rule in RUN_STATES.items() if rule is None}
# The states the owner asked the pill to keep, spelled as the topbar says them.
KEPT = {
    "needs_you": "needs you",
    "asked": "needs you",
    "stopped": "stopped",
    "failed": "no outcome",
    "stopping": "stopping…",
}
# The chat's own states, which this change leaves exactly as they were.
CHAT_STATES = {"idle": "idle", "working": "task running", "needs": "needs", "error": "error"}

BODY = """
await page.chat(args.sid);
return await page.js(async (run, lane, states, chat, stopping, thenAsk) => {
  showMode(lane);
  const read = () => {
    const node = document.querySelector("#status");
    const box = node.getBoundingClientRect();
    const card = document.querySelector(".task-pill");
    return {
      lane: document.body.dataset.mode,
      hidden: node.hidden,
      rendered: box.width > 0 && box.height > 0,
      face: node.className,
      words: node.textContent,
      name: node.getAttribute("aria-label"),
      card: card ? card.textContent : null,
    };
  };
  const seen = [];
  for (const state of states) {
    handleTask({ kind: "task.state", run_id: run, task: "close the duplicate pill", state });
    seen.push([state, read()]);
    if (stopping && state === "running") {
      // The Stop button this page offers, before the run's own state arrives.
      await stopTask(run);
      seen.push(["stopping", read()]);
    }
  }
  for (const kind of chat) {
    setStatus(kind, kind === "working" ? "task running" : undefined);
    seen.push(["chat-" + kind, read()]);
  }
  if (thenAsk) {
    // Out of the task lane: a session switched to Ask is a chat session.
    showMode("ask");
    seen.push(["switched-to-ask", read()]);
  }
  return { width: window.innerWidth, seen };
}, args.run, args.lane, args.states, args.chat, args.stopping, args.thenAsk);
"""


def _page(
    tmp_path: Path,
    lane: str,
    width: int,
    *,
    states: list[str] | None = None,
    chat: list[str] | None = None,
    stopping: bool = False,
    then_ask: bool = False,
) -> dict[str, dict[str, Any]]:
    """Each state the page was left in, with the card's words and the topbar's."""
    with served_chat(tmp_path) as site:
        out = drive_page(
            site.base,
            BODY,
            sid=site.sid,
            lane=lane,
            width=width,
            run=RUN,
            states=list(RUN_STATES) if states is None else states,
            chat=list(CHAT_STATES) if chat is None else chat,
            stopping=stopping,
            thenAsk=then_ask,
        )
    assert out["width"] == width, f"Chrome did not hold the page at {width} px: {out['width']}"
    rows = dict(out["seen"])
    lane_seen = {pill["lane"] for pill in rows.values()}
    wanted = {lane} if not then_ask else {lane, "ask"}
    assert lane_seen == wanted, f"the page left its lane: {lane_seen}"
    return rows


def _assert_shown(pill: dict[str, Any], words: str, name: str, width: int) -> None:
    assert pill["hidden"] is False, f"the topbar dropped a state its card hides: {pill}"
    assert pill["rendered"] is True, f"kept, but nothing renders it at {width} px: {pill}"
    assert pill["words"] == words, f"the pill reads {pill['words']!r}, not {words!r}"
    assert pill["name"] == name, f"a reader is given {pill['name']!r}, not {name!r}"


def _assert_hidden(pill: dict[str, Any], card: str) -> None:
    assert pill["hidden"] is True, f"the topbar reads {pill['words']!r} beside the card's {card!r}"
    assert pill["rendered"] is False, f"a pill with nothing to say still fills a box: {pill}"


@pytest.mark.parametrize("width", [400, 1100])
@needs_chrome
def test_a_task_session_shows_the_pill_for_exactly_the_states_its_card_leaves_unsaid(
    tmp_path: Path, width: int
) -> None:
    rows = _page(tmp_path, "task", width, chat=[], stopping=True)
    for state, expected in RUN_STATES.items():
        pill = rows[state]
        assert pill["card"], f"a run in state {state} painted no card pill to read against: {pill}"
        if expected is None:
            _assert_hidden(pill, pill["card"])
            continue
        words, name = expected
        _assert_shown(pill, words, name, width)


@needs_chrome
def test_the_task_lanes_pill_repeats_no_state_the_run_card_already_reads(tmp_path: Path) -> None:
    """The defect, named: one screen saying the same state twice.

    The words the topbar is allowed to keep are the ones the owner asked it to
    keep -- needs you, stopped, no outcome, and the stopping this page asked for
    -- and they are the states the phone has nowhere else to read. Every other
    state the run reported reaches the topbar only if it says something the card
    did not.
    """
    rows = _page(tmp_path, "task", 1600, chat=[], stopping=True)
    shown = {state: pill["words"] for state, pill in rows.items() if not pill["hidden"]}
    cards = {state: pill["card"] for state, pill in rows.items()}
    assert shown == KEPT, f"the topbar read {shown} beside the card's {cards}"
    # Of the states it shows, none is one the card spells for itself.
    repeated = {state: shown[state] for state in shown if state in CARD_OWN}
    assert repeated == {}, f"the topbar read its own run card a second time: {repeated}"
    # Which is also the way the chat's own word for a live turn goes away there.
    assert "task running" not in shown.values(), f"the task lane read the chat's words: {shown}"


@needs_chrome
def test_a_session_switched_to_ask_is_a_chat_session_and_keeps_its_pill(tmp_path: Path) -> None:
    rows = _page(tmp_path, "task", 1100, states=["running"], chat=[], then_ask=True)
    task = rows["running"]
    assert task["card"] == "● running", f"the run card read {task['card']!r}"
    _assert_hidden(task, task["card"])
    chat = rows["switched-to-ask"]
    assert chat["lane"] == "ask", f"Ask did not make it a chat session: {chat}"
    _assert_shown(chat, "task running", "working", 1100)


@needs_chrome
def test_a_chat_session_keeps_its_status_pill_for_every_state(tmp_path: Path) -> None:
    rows = _page(tmp_path, "ask", 1100, states=["running", "stopped"], stopping=True)
    for row in rows.values():
        assert row["hidden"] is False, f"the Ask lane hid the chat's status pill: {row}"
        assert row["rendered"] is True, f"the Ask lane renders no status pill: {row}"
    for kind, words in CHAT_STATES.items():
        pill = rows[f"chat-{kind}"]
        assert pill["words"] == words, f"the pill reads {pill['words']!r}, not {words!r}"
        assert pill["name"] == kind, f"a reader is given {pill['name']!r}, not {kind!r}"
