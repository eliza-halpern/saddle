"""UXREVIEW2: defects found reviewing the merged chat UI from screenshots.

F1 -- the run card's budget after Extend. Contract: every state event a run
publishes carries the budget the run is working to, which is the budget its
outcome sidecar seals. Extend doubles the budget mid-run
(`engine._offer_budget`); the card read "~1.0k of 1.0k" at the end while the
packet's Cost row, compiled from the sealed sidecar, said "of 2.0k"
(out/UXREVIEW2/shots/05-run-card-after-extend-*.png, 08-packet-cost-*.png).

Known-good: Extend -> the final state event says 2000, as sealed. Known-bad
(the other half): Stop at limit -> 1000, as sealed; the state event must not
report a budget the run never had.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest
from test_ask_budget import Reader, repo  # noqa: F401 -- the fixture
from test_ask_web import BROWSER, app_for, start
from test_ui3_mode import _server_of, serving
from test_web_tasks import idle, wait_for

from saddle.journal import attempt_sidecar_path, read_spans
from saddle.sessions import SessionStore
from saddle.web.app import build_app

CDP = Path(__file__).parent / "fixtures" / "uxfix_cdp.mjs"


def cdp(base: str, *args: str) -> dict:
    out = subprocess.run(
        ["node", str(CDP), base, *args], capture_output=True, text=True, timeout=300, check=False
    )
    assert out.returncode == 0, out.stderr
    return json.loads(out.stdout.strip().splitlines()[-1])


@pytest.mark.parametrize(("reply", "budget"), [("Extend", 2000), ("Stop at limit", 1000)])
def test_the_runs_state_event_carries_the_budget_its_outcome_sealed(
    tmp_path: Path,
    repo: Path,  # noqa: F811
    reply: str,
    budget: int,
) -> None:
    store = SessionStore(tmp_path / "s")
    with app_for(store, repo, Reader(13)) as (http, server):
        server.arm = "E"
        sid, rid = start(http, token_budget=1000)
        wait_for(lambda: server.tasks[rid].state == "needs_you", timeout=60)
        assert server.tasks[rid].state_event().token_budget == 1000
        http.post(f"/api/tasks/{rid}/answer", json={"text": reply})
        wait_for(lambda: idle(server, sid), timeout=60)
    run = server.tasks[rid]
    assert run.journal is not None
    end = next(s for s in reversed(read_spans(run.journal)) if s.name.startswith("auto:"))
    sealed = json.loads(attempt_sidecar_path(run.journal, end.span_id).read_text())
    assert sealed["token_budget"] == budget
    final = run.state_event()
    assert final.state in ("finished", "stopped")
    assert final.token_budget == sealed["token_budget"]
    assert final.time_budget_s == sealed["time_budget_s"]


# F2 -- a session switch carried the last session's run state along.
# Contract: the header pill, the send button and what submitting does belong
# to the session on screen. Seen in shots/13-needs-you-from-another-session:
# a session with no run showed "? needs you" and a stop square, and the
# square, pressed there, stopped the other session's run (state.activeTask).
# Known-good half: back on the run's own session, "needs you" is replayed.


@pytest.mark.skipif(not BROWSER, reason="needs node and google-chrome")
def test_switching_sessions_leaves_the_other_runs_state_behind(
    tmp_path: Path,
    repo: Path,  # noqa: F811
) -> None:
    store = SessionStore(tmp_path / "s")
    app = build_app(store, lambda: Reader(13), default_workdir=repo, arm="E")
    server = _server_of(app)
    with serving(app) as base:
        a = store.create(title="a", workdir=str(repo)).id
        store.update(a, mode="task")
        b = store.create(title="b", workdir=str(repo)).id
        got = cdp(base, "switch", a, b)
        [rid] = list(server.tasks)
        run = server.tasks[rid]
        still_asking = run.state == "needs_you" and not run.cancelled
        run.answers.put("Stop at limit")
        wait_for(lambda: idle(server, a), timeout=60)
    assert got["onA"]["status"] == "needs you"
    assert got["onB"] == {"status": "idle", "send": "↑", "busy": False, "sid": b}
    assert still_asking, "submitting in session b reached session a's run"
    assert got["backOnA"]["status"] == "needs you"
    assert got["backOnA"]["send"] == "■"
