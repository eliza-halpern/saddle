"""ASK: the run's own questions reach the card and are answered through it.

Known-good: a chat-started run with tests read-only is refused at finish on
an uncovered line, the card goes "needs you" with the test-edit question,
the answer posted through `/api/tasks/<id>/answer` (the existing seam) lets
the run write the test and finish, and the served packet's Contract row
lists the question and answer, citing both ledger records. The same path
in headless Chrome, answering by clicking the option on the card.

Known-bad: "Keep read-only" posted the same way ends the run stopped, with
the test edit refused.
"""

from __future__ import annotations

import json
import os
import subprocess
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

import pytest
from browser_guard import BROWSER
from starlette.testclient import TestClient
from test_ask_test_edits import Learner, repo  # noqa: F401 -- the fixture
from test_ui3_mode import _server_of, serving
from test_web_tasks import idle, wait_for

from saddle.journal import read_spans
from saddle.sessions import SessionStore
from saddle.web.app import ChatServer, build_app

CDP = Path(__file__).parent / "fixtures" / "ask_cdp.mjs"


@pytest.fixture
def store(tmp_path: Path) -> SessionStore:
    return SessionStore(tmp_path / "sessions")


@contextmanager
def app_for(
    store: SessionStore, root: Path, client: Any
) -> Iterator[tuple[TestClient, ChatServer]]:
    app = build_app(store, lambda: client, default_workdir=root, arm="E+A+F")
    with TestClient(app) as http:
        yield http, _server_of(app)


def start(http: TestClient, **body: Any) -> tuple[str, str]:
    sid = http.post("/api/sessions").json()["id"]
    body = {"text": "f(None) should be 0", "allow_test_edits": False, **body}
    return sid, http.post(f"/api/sessions/{sid}/task", json=body).json()["run_id"]


@pytest.mark.parametrize(
    ("reply", "verdict"), [("Allow", "finished"), ("Keep read-only", "stopped")]
)
def test_the_test_edit_question_is_answered_through_the_web_api(
    store: SessionStore,
    repo: Path,  # noqa: F811
    reply: str,
    verdict: str,
) -> None:
    with app_for(store, repo, Learner()) as (http, server):
        sid, rid = start(http)
        wait_for(lambda: server.tasks[rid].state == "needs_you", timeout=120)
        run = server.tasks[rid]
        question = run.state_event().question
        assert question is not None
        assert question["id"] == "test-edits"
        assert question["options"] == ["Allow", "Keep read-only"]
        assert "covers n.py:3" in question["text"]
        assert http.post(f"/api/tasks/{rid}/answer", json={"text": reply}).json() == {"ok": True}
        wait_for(lambda: idle(server, sid), timeout=180)
        packet = http.get(f"/api/sessions/{sid}/tasks/{rid}/packet").json()
    assert run.state == verdict
    assert packet["verdict"] == verdict
    assert run.journal is not None
    spans = read_spans(run.journal)
    [asked] = [s for s in spans if s.name == "question"]
    [answered] = [s for s in spans if s.name == "answer"]
    assert answered.parent_id == asked.span_id
    contract = next(r for r in packet["rows"] if r["key"] == "contract")
    assert contract["items"] == [f"You were asked: {asked.detail} → you answered: {reply}"]
    assert contract["cites"] == [asked.record_hash, answered.record_hash]
    refused = [s for s in spans if s.name.startswith("refused:")]
    assert bool(refused) is (reply != "Allow")


@pytest.mark.skipif(not BROWSER, reason="needs node and google-chrome")
def test_the_card_answers_the_test_edit_question(tmp_path: Path, repo: Path) -> None:  # noqa: F811
    store = SessionStore(tmp_path / "s")
    app = build_app(store, lambda: Learner(), default_workdir=repo, arm="E+A+F")
    shots = os.environ.get("ASK_SHOTS", "")
    with serving(app) as base:
        sid = store.create(title="t", workdir=str(repo)).id
        store.update(sid, mode="task")
        out = subprocess.run(
            ["node", str(CDP), base, sid, "Allow", "100", shots, "test-edits"],
            capture_output=True,
            text=True,
            timeout=300,
            check=False,
        )
    assert out.returncode == 0, out.stderr
    got = json.loads(out.stdout.strip().splitlines()[-1])
    assert got["question"].startswith("The auditor needs a test that covers n.py:3.")
    assert got["options"] == ["Allow", "Keep read-only"]
    assert got["typed"] == "Allow"
    assert got["verdict"] == "v-finished"
    assert got["askHidden"] is True
    assert got["contract"]["status"] == "s-observed"
    assert got["contract"]["items"] == [f"You were asked: {got['question']} → you answered: Allow"]
    assert got["contract"]["cites"] == 2
