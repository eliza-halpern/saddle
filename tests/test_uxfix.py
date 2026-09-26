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
from pathlib import Path

import pytest
from test_ask_budget import Reader, repo  # noqa: F401 -- the fixture
from test_ask_web import app_for, start
from test_web_tasks import idle, wait_for

from saddle.journal import attempt_sidecar_path, read_spans
from saddle.sessions import SessionStore


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
