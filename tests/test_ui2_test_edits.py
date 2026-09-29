"""UI2: the chat's Run strip chooses whether a run may edit tests.

Contract: the Small lane defaults to test edits allowed; the strip's choice
reaches `run_auto` as `allow_test_edits`; the card and packet say which it
was; and a run that stopped "audit unresolved" with tests read-only, on a
finding a new test closes, is offered again with test edits allowed --
never when tests were already editable.

Known-good: a stubborn read-only run stopped on coverage is offered.
Known-bad: the same stop with tests editable, a finish, and a stop on a
finding no test closes are not offered; a non-boolean choice is refused.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, cast

import pytest
from starlette.testclient import TestClient
from test_evidence import _without_stubbed_mutmut
from test_integ import BASE, TEST
from test_integ import Scripted as Stubborn
from test_web_tasks import FIX, app_for, git, idle, wait_for
from test_web_tasks import TEST as WEB_TEST

import saddle.auto
from saddle.auto import AutoOptions, run_auto
from saddle.journal import append_span, build_span, write_attempt_sidecar
from saddle.packet import _needs_a_test, _test_edits, compile_packet
from saddle.sessions import SessionStore
from saddle.vllm import VllmClient
from saddle.web import tasks
from saddle.web.app import build_app
from saddle.web.tasks import TaskRun


def _commit(root: Path, files: dict[str, str]) -> Path:
    for name, text in files.items():
        (root / name).parent.mkdir(parents=True, exist_ok=True)
        (root / name).write_text(text)
    git(root, "init", "-q", "-b", "main")
    git(root, "add", "-A")
    git(root, "commit", "-q", "-m", "init")
    return root


@pytest.fixture
def store(tmp_path: Path) -> SessionStore:
    return SessionStore(tmp_path / "sessions")


@pytest.fixture
def web_repo(tmp_path: Path) -> Path:
    """test_web_tasks' calc repo: `add` subtracts, tests under tests/."""
    return _commit(
        tmp_path / "repo",
        {
            "calc.py": "def add(a, b):\n    return a - b\n",
            "tests/test_calc.py": WEB_TEST,
            "pyproject.toml": '[tool.pytest.ini_options]\ntestpaths = ["tests"]\n'
            'pythonpath = ["."]\n',
            ".gitignore": "__pycache__/\n.pytest_cache/\n",
        },
    )


@pytest.fixture
def repo(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """test_integ's repo, with the real mutmut so tier 2 reports."""
    _without_stubbed_mutmut(monkeypatch)
    return _commit(
        tmp_path / "repo",
        {"n.py": BASE, "test_n.py": TEST, ".gitignore": "__pycache__/\n.saddle/\n"},
    )


def test_the_small_lane_defaults_to_test_edits_allowed(tmp_path: Path) -> None:
    app = build_app(SessionStore(tmp_path), object, default_workdir=tmp_path)
    with TestClient(app) as client:
        assert client.get("/api/task-policy").json() == {
            "test_edits": True,
            "task_text_check": False,
        }
    assert TaskRun("r", "s", "t", 1.0, 1).allow_test_edits is True
    html = (Path(tasks.__file__).parent / "static" / "index.html").read_text()
    assert 'id="tc-test-edits" checked' in html


def _started(
    store: SessionStore, root: Path, monkeypatch: pytest.MonkeyPatch, body: dict[str, Any]
) -> tuple[list[AutoOptions], Any]:
    seen: list[AutoOptions] = []
    real = saddle.auto.run_auto

    def spy(*args: Any, **kwargs: Any) -> Any:
        seen.append(args[0])
        return real(*args, **kwargs)

    monkeypatch.setattr(tasks, "run_auto", spy)
    with app_for(store, root, FIX) as (client, server):
        sid = client.post("/api/sessions").json()["id"]
        rid = client.post(f"/api/sessions/{sid}/task", json={"text": "t", **body}).json()["run_id"]
        wait_for(lambda: idle(server, sid))
        packet = client.get(f"/api/sessions/{sid}/tasks/{rid}/packet").json()
        return seen, (server.tasks[rid], packet)


@pytest.mark.parametrize(
    ("body", "expected", "word"),
    [
        ({}, True, "tests editable"),
        ({"allow_test_edits": True}, True, "tests editable"),
        ({"allow_test_edits": False}, False, "tests read-only"),
    ],
)
def test_the_strips_choice_reaches_run_auto_and_the_card(
    store: SessionStore,
    web_repo: Path,
    monkeypatch: pytest.MonkeyPatch,
    body: dict[str, Any],
    expected: bool,
    word: str,
) -> None:
    seen, (run, packet) = _started(store, web_repo, monkeypatch, body)
    assert [o.allow_test_edits for o in seen] == [expected]
    assert run.state_event().test_edits is expected
    assert packet["test_edits"] is expected
    assert word in packet["header"]
    scope = next(r for r in packet["rows"] if r["key"] == "scope")
    assert scope["text"].endswith("Tests were editable." if expected else "Tests were read-only.")
    assert packet["offer_test_edits"] is False  # finished: nothing to offer


def test_a_choice_that_is_not_a_boolean_is_refused(
    store: SessionStore,
    web_repo: Path,
) -> None:
    with app_for(store, web_repo, FIX) as (client, _server):
        sid = client.post("/api/sessions").json()["id"]
        reply = client.post(
            f"/api/sessions/{sid}/task", json={"text": "t", "allow_test_edits": "no"}
        )
    assert reply.status_code == 400


def _stubborn(repo: Path, *, allow: bool) -> Any:
    run_id = f"stubborn-{allow}"
    journal = repo / ".saddle" / "runs" / run_id / "proofs.jsonl"
    client = Stubborn(journal, learns=False)
    options = AutoOptions(
        task="f(None) should be 0", repo=repo, run_id=run_id, allow_test_edits=allow
    )
    return run_auto(options, cast(VllmClient, client))


@pytest.mark.parametrize(("allow", "offered"), [(False, True), (True, False)])
def test_the_offer_is_made_only_when_tests_were_read_only(
    repo: Path,
    allow: bool,
    offered: bool,
) -> None:
    result = _stubborn(repo, allow=allow)
    assert (result.outcome, result.reason) == ("stopped", "audit unresolved")
    packet = compile_packet(result.journal)
    assert "coverage (evidence-thin)" in packet.verdict_text
    assert packet.test_edits is allow
    assert packet.offer_test_edits is offered
    assert packet.payload()["offer_test_edits"] is offered


@pytest.mark.parametrize(
    ("findings", "needs"),
    [
        ([{"gate": "coverage", "reason": "evidence-thin"}], True),
        (
            [
                {"gate": "mutation", "reason": "unknown"},
                {"gate": "tests", "reason": "evidence-thin"},
            ],
            True,
        ),
        ([{"gate": "coverage", "reason": "fail"}], True),
        ([{"gate": "mutation", "reason": "unknown"}], False),
        (["junk"], False),
        ("junk", False),
    ],
)
def test_only_a_finding_a_test_closes_earns_the_offer(findings: Any, needs: bool) -> None:
    assert _needs_a_test({"unresolved_findings": findings}) is needs


def test_an_unrecorded_policy_is_none_not_read_only(tmp_path: Path) -> None:
    assert _test_edits(None) is None
    # A ledger from before the start span named its test policy: no claim either way.
    journal = tmp_path / "old" / "proofs.jsonl"
    journal.parent.mkdir()
    append_span(
        journal,
        build_span(
            node_id="chat#1",
            argv=["auto:start", "t"],
            duration_ms=0,
            exit_code=0,
            detail="arm E; branch b",
            kind="agent",
        ),
    )
    evidence = {"files_changed": [], "unresolved_findings": [{"gate": "coverage"}]}
    digest = write_attempt_sidecar(journal, "fin", evidence)
    append_span(
        journal,
        build_span(
            node_id="chat#1",
            argv=["auto:stopped"],
            duration_ms=0,
            exit_code=3,
            detail="stopped: audit unresolved",
            kind="agent",
            name="auto:stopped",
            span_id="fin",
            attempt_hash=digest,
        ),
    )
    packet = compile_packet(journal)
    assert packet.test_edits is None
    assert packet.offer_test_edits is False
    scope = {r.key: r for r in packet.rows}["scope"]
    assert scope.status == "observed"
    assert "Tests were" not in scope.text
    assert not any(h.startswith("tests ") for h in packet.header)
