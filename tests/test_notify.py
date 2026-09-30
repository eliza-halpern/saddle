"""A run finds you: tab title, favicon, notification, sidebar state, live region.

Known-good: `/api/sessions` carries each session's latest run state and
task; in a real browser with the tab hidden, a run reaching needs_you
prefixes the title, swaps the favicon, announces it in the live region and
(permission granted) fires one Notification; a run in a session that is
not on screen still shows its state in the sidebar.

Known-bad: no Notification while the tab is visible or without permission;
the title prefix does not outlive the user looking at the finished run.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path
from typing import Any

import pytest
from starlette.testclient import TestClient
from test_ui3_mode import NoModel, _server_of, serving

from saddle.sessions import SessionStore
from saddle.web.app import build_app
from saddle.web.tasks import TaskRun

HERE = Path(__file__).parent
CDP = HERE / "fixtures" / "notify_cdp.mjs"


def _run(sid: str, rid: str, task: str, st: str) -> TaskRun:
    return TaskRun(
        run_id=rid, session_id=sid, task=task, time_budget_s=60, token_budget=1000, state=st
    )


def test_sessions_carry_their_latest_run_state(tmp_path: Path) -> None:
    store = SessionStore(tmp_path / "s")
    app = build_app(store, NoModel, default_workdir=tmp_path)
    server = _server_of(app)
    a = store.create(title="a", workdir=str(tmp_path)).id
    b = store.create(title="b", workdir=str(tmp_path)).id
    c = store.create(title="c", workdir=str(tmp_path)).id
    server.tasks["r1"] = _run(a, "r1", "old", "finished")
    server.tasks["r2"] = _run(b, "r2", "ask me", "needs_you")
    server.tasks["r3"] = _run(a, "r3", "newer", "running")
    with TestClient(app) as client:
        rows = {row["id"]: row for row in client.get("/api/sessions").json()}
    assert (rows[a]["run_state"], rows[a]["run_task"]) == ("running", "newer")
    assert (rows[b]["run_state"], rows[b]["run_task"]) == ("needs_you", "ask me")
    assert (rows[c]["run_state"], rows[c]["run_task"]) == (None, None)
    assert rows[a]["title"] == "a"


# -- the browser --------------------------------------------------------------


def _scripted(app: Any) -> None:
    """Runs go running -> needs_you -> (answer) finished; no model, no worktree."""
    import time

    from saddle.events import Question

    server = _server_of(app)

    def run_task(sid: str, run: TaskRun) -> None:
        live = server._live(sid)
        live.publish(run.state_event())
        time.sleep(0.6)
        run.question = Question(id="q1", text="Which schema?", options=["v1", "v2"])
        run.state = "needs_you"
        live.publish(run.state_event())
        try:
            run.answers.get(timeout=20)
            run.state = "finished"
        except Exception:  # pragma: no cover - only when a browser step hangs
            run.state = "stopped"
        run.question = None
        live.publish(run.state_event())
        with live.lock:
            live.busy = False
        live.publish(None)

    server._run_task = run_task  # type: ignore[method-assign,assignment]


def _browser(
    base: str, sid: str, step: str, other: str = "-", perm: str = "granted"
) -> dict[str, Any]:
    args = ["node", str(CDP), base, sid, step, other, perm]
    shots = __import__("os").environ.get("NOTIFY_SHOTS")
    if shots:
        args.append(shots)
    out = subprocess.run(args, capture_output=True, text=True, timeout=90, check=False)
    assert out.returncode == 0, out.stderr
    got: dict[str, Any] = json.loads(out.stdout.strip().splitlines()[-1])
    return got


BROWSER = shutil.which("node") and shutil.which("google-chrome")
needs_browser = pytest.mark.skipif(not BROWSER, reason="needs node and google-chrome")


def _page(tmp_path: Path, step: str, perm: str = "granted") -> dict[str, Any]:
    store = SessionStore(tmp_path / "s")
    app = build_app(store, NoModel, default_workdir=tmp_path)
    _scripted(app)
    sid = store.create(title="parser work", workdir=str(tmp_path)).id
    other = store.create(title="schema work", workdir=str(tmp_path)).id
    with serving(app) as base:
        got = _browser(base, sid, step, other, perm)
    got["sid"], got["other"] = sid, other
    return got


@needs_browser
def test_a_hidden_tab_is_told_when_a_run_needs_you_and_when_it_ends(tmp_path: Path) -> None:
    got = _page(tmp_path, "hidden")
    idle, needs, done, back = got["idle"], got["needs"], got["finished"], got["back"]
    assert idle["title"] == "parser work"
    assert needs["title"] == "? needs you · fix the parser"
    assert done["title"] == "✓ finished · fix the parser"
    # Looking at the tab again is what ends the finished prefix.
    assert back["title"] == "parser work"
    icons = {idle["icon"], needs["icon"], done["icon"]}
    assert len(icons) == 3
    assert all(icon.startswith("data:image/svg+xml,") for icon in icons)
    assert back["icon"] == idle["icon"]
    assert needs["liveAttr"] == "polite"
    assert needs["live"] == "Task needs you: fix the parser"
    assert done["live"] == "Task finished: fix the parser"
    assert [n["title"] for n in done["notes"]] == [
        "? needs you · parser work",
        "✓ finished · parser work",
    ]
    assert done["notes"][0]["body"] == "fix the parser"
    assert needs["dots"][got["sid"]] == "needs_you"
    assert idle["requests"] == 0  # never asked for permission unprompted


@needs_browser
def test_a_visible_tab_gets_the_title_but_no_notification(tmp_path: Path) -> None:
    got = _page(tmp_path, "visible")
    assert got["needs"]["title"] == "? needs you · fix the parser"
    assert got["finished"]["notes"] == []


@needs_browser
def test_no_notification_without_permission(tmp_path: Path) -> None:
    got = _page(tmp_path, "hidden", perm="default")
    assert got["needs"]["title"] == "? needs you · fix the parser"
    assert got["finished"]["notes"] == []
    assert got["finished"]["requests"] == 0


@needs_browser
def test_a_run_in_another_session_shows_in_the_sidebar(tmp_path: Path) -> None:
    got = _page(tmp_path, "other")
    sid, other = got["sid"], got["other"]
    seen = got["otherNeeds"]
    assert seen["dots"] == {sid: None, other: "needs_you"}
    # The tab belongs to the session on screen, which has no run.
    assert seen["title"] == "parser work"
    assert seen["live"] == "Task needs you in schema work: ask about the schema"
    assert [n["title"] for n in seen["notes"]] == ["? needs you · schema work"]
    assert got["mixed"]["dots"] == {sid: "finished", other: "needs_you"}


@needs_browser
@pytest.mark.parametrize(
    ("perm", "label", "pref", "second"),
    [
        ("granted", "Notifying when hidden", "on", "Notify me"),
        ("denied", "Notifications blocked", "off", "Notifications blocked"),
    ],
)
def test_the_notify_control_asks_once_and_remembers(
    tmp_path: Path, perm: str, label: str, pref: str, second: str
) -> None:
    got = _page(tmp_path, "control", perm=perm)
    assert got["idle"]["requests"] == 0  # nothing asked on load
    assert got["idle"]["control"] == "Notify me"
    assert got["afterClick"]["requests"] == 1
    assert got["afterClick"]["control"] == label
    assert got["pref"] == pref
    # A second click toggles the remembered choice; it never asks again.
    assert got["afterSecond"]["requests"] == 1
    assert got["afterSecond"]["control"] == second


@needs_browser
def test_keyboard_focus_is_visible_on_every_input(tmp_path: Path) -> None:
    rings = _page(tmp_path, "focus")["rings"]
    assert set(rings) == {"#title", "#input", "#temp", "#notify-toggle"}
    for sel, ring in rings.items():
        assert ring["style"] != "none", sel
        assert ring["width"] >= 2, sel
