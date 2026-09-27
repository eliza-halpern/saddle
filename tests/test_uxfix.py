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
import os
import subprocess
from pathlib import Path

import pytest
from starlette.testclient import TestClient
from test_ask_budget import Reader, repo  # noqa: F401 -- the fixture
from test_ask_web import BROWSER, app_for, start
from test_ui3_mode import _server_of, serving
from test_web_tasks import idle, wait_for

from saddle.journal import attempt_sidecar_path, read_spans
from saddle.sessions import SessionStore
from saddle.web.app import build_app

CDP = Path(__file__).parent / "fixtures" / "uxfix_cdp.mjs"


def cdp(base: str, *args: str) -> dict:
    shots = [os.environ["UXFIX_SHOTS"]] if os.environ.get("UXFIX_SHOTS") else []
    out = subprocess.run(
        ["node", str(CDP), base, *args, *shots],
        capture_output=True,
        text=True,
        timeout=300,
        check=False,
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


# F3 -- the "↓ newest" pill sat on the composer. Contract: when shown, the
# pill is wholly above the composer and inside the viewport, at 400 px and
# at desktop width. It was placed at a fixed `bottom: 96px` of <main>, and
# the composer is taller than that (shots/04-question-card-400.png: the pill
# covers the placeholder beside the send button).


@pytest.mark.skipif(not BROWSER, reason="needs node and google-chrome")
def test_the_newest_pill_sits_above_the_composer(tmp_path: Path) -> None:
    store = SessionStore(tmp_path / "s")
    sid = store.create(title="long", workdir=str(tmp_path)).id
    words = "a line of an answer long enough to wrap on a phone screen. " * 6
    messages = []
    for i in range(20):
        messages.append({"role": "user", "content": f"question {i}"})
        messages.append({"role": "assistant", "content": f"{words}\n\n{words}"})
    store.save_messages(sid, messages)
    app = build_app(store, lambda: Reader(13), default_workdir=tmp_path)
    with serving(app) as base:
        got = cdp(base, "jump", sid)
    for width in ("400", "1280"):
        pill = got[width]
        assert pill["hidden"] is False, width
        assert pill["jumpBottom"] <= pill["composerTop"], (width, pill)
        assert pill["jumpTop"] >= 0, (width, pill)
        assert pill["jumpRight"] <= pill["viewport"], (width, pill)


# Q1 (N4) -- the sidebar forgot every run when the server restarted, because
# `app.list_sessions` read only `server.tasks` (memory). Contract: a session's
# latest ended run reads the same after a restart over the same store as it
# did before -- state and task in `/api/sessions`, verdict and budget in the
# packet, branch from the branch endpoint -- because the chat journal's
# `run-ref` span names it (shots/10-sidebar-after-restart-desktop.png).
# Known-bad half: a session whose journal has no run-ref shows no run.


def test_the_sidebar_rebuilds_a_runs_state_after_a_restart(
    tmp_path: Path,
    repo: Path,  # noqa: F811
) -> None:
    from test_web_tasks import FIX
    from test_web_tasks import app_for as tasks_app

    store = SessionStore(tmp_path / "s")
    with tasks_app(store, repo, FIX) as (http, server):
        sid = http.post("/api/sessions").json()["id"]
        blank = http.post("/api/sessions", json={"reuse_unstarted": False}).json()["id"]
        rid = http.post(f"/api/sessions/{sid}/task", json={"text": "make add add"}).json()["run_id"]
        wait_for(lambda: idle(server, sid))
        before = _seen(http, sid, rid)
        assert before["state"] == "finished"
    # A new server over the same store: nothing in memory.
    app = build_app(store, lambda: None, default_workdir=repo)
    assert _server_of(app).tasks == {}
    with TestClient(app) as http:
        after = _seen(http, sid, rid)
        rows = {row["id"]: row for row in http.get("/api/sessions").json()}
    assert after == before
    assert (rows[blank]["run_state"], rows[blank]["run_task"]) == (None, None)


def _seen(http: TestClient, sid: str, rid: str) -> dict:
    row = next(r for r in http.get("/api/sessions").json() if r["id"] == sid)
    packet = http.get(f"/api/sessions/{sid}/tasks/{rid}/packet").json()
    cost = next(r for r in packet["rows"] if r["key"] == "cost")
    return {
        "state": row["run_state"],
        "task": row["run_task"],
        "verdict": packet["verdict"],
        "budget": cost["text"],
        "branch": http.get(f"/api/sessions/{sid}/tasks/{rid}/branch").json()["branch"],
    }


# Q3 (N5) -- on a phone, nothing outside the closed drawer said that another
# session needs you: `.status` was display:none at 420 px and below, and ☰
# had no badge (shots/13-needs-you-from-another-session-400.png). Contract,
# at 400 px: on the run's own session the header pill "needs you" is
# rendered and on screen; on any other session the ☰ button carries a
# visible dot (a ::after with a width) and says so in its label, with the
# drawer closed. Known-bad half: with no run needing you, ☰ has no dot.


@pytest.mark.skipif(not BROWSER, reason="needs node and google-chrome")
def test_a_phone_shows_needs_you_outside_the_drawer(
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
        got = cdp(base, "phone", a, b)
        [rid] = list(server.tasks)
        server.tasks[rid].answers.put("Stop at limit")
        wait_for(lambda: idle(server, a), timeout=60)
    on_a, on_b = got["onA"], got["onB"]
    assert on_a["pill"]["text"] == "needs you"
    assert on_a["pill"]["display"] != "none"
    assert on_a["pill"]["onScreen"] is True
    assert on_a["menu"]["needs"] is False, "the dot is for another session's run"
    assert on_b["menu"]["drawerOpen"] is False
    assert on_b["menu"]["needs"] is True
    assert on_b["menu"]["after"] not in ("none", "normal", "")
    assert on_b["menu"]["afterWidth"] >= 6, on_b["menu"]
    assert "needs you" in (on_b["menu"]["label"] or "")
    assert on_b["pill"]["text"] == "idle"


# Q4 (N6) -- a finished run with no mutation record said "Mutation ○ No
# mutation record" and, two lines under it, "Not proven ✓ Nothing is left
# unproven" (shots/19-merge-unproven-desktop.png). Contract: when an auditor
# ran and no mutation record exists, Not proven carries an item saying the
# changed lines were not mutation-tested. Known-good: a mutation record
# present keeps "Nothing is left unproven."


@pytest.mark.parametrize(
    ("kind", "text", "item"),
    [
        ("mutated", "Nothing is left unproven.", None),
        (
            "audited",
            "What this packet cannot vouch for:",
            "Changed lines were not mutation-tested: no mutation record.",
        ),
    ],
)
def test_not_proven_names_a_missing_mutation_record(
    tmp_path: Path, kind: str, text: str, item: str | None
) -> None:
    from packet_seed import make_repo, seed

    from saddle.auto import ledger_path
    from saddle.packet import compile_packet

    calc = make_repo(tmp_path / "repo")
    store = SessionStore(tmp_path / "s")
    _sid, rid, _branch = seed(store, calc, kind)  # type: ignore[arg-type]
    rows = {row.key: row for row in compile_packet(ledger_path(calc, rid), run_id=rid).rows}
    assert rows["not-proven"].text == text
    assert list(rows["not-proven"].items) == ([item] if item else [])
    assert rows["mutation"].status == ("proven" if kind == "mutated" else "absent")


# Q10 -- "Download full report": GET .../tasks/<rid>/packet.md serves the full
# packet as one markdown attachment, rendered fresh from the sealed ledger
# through render_packet_text and written beside the ledger as packet.md, and
# the click is logged in the session's actions.log. Known-bad: an unknown
# run is a 404 and writes no file.


def test_the_report_download_is_the_full_packet_text_written_beside_the_ledger(
    tmp_path: Path,
) -> None:
    from packet_seed import make_repo, seed
    from test_ui3_mode import NoModel

    from saddle.auto import ledger_path
    from saddle.packet import compile_packet, render_packet_text

    calc = make_repo(tmp_path / "repo")
    store = SessionStore(tmp_path / "s")
    sid, rid, _branch = seed(store, calc, "audited")
    journal = ledger_path(calc, rid)
    expected = render_packet_text(compile_packet(journal, run_id=rid))
    app = build_app(store, NoModel, default_workdir=calc)
    with TestClient(app) as http:
        got = http.get(f"/api/sessions/{sid}/tasks/{rid}/packet.md")
        missing = http.get(f"/api/sessions/{sid}/tasks/{'0' * 12}/packet.md")
        no_branch = http.get(f"/api/sessions/{sid}/tasks/{'0' * 12}/branch")
    assert got.status_code == 200
    assert got.content == expected.encode()
    assert (
        got.headers["content-disposition"] == f'attachment; filename="saddle-packet-{rid[:8]}.md"'
    )
    assert got.headers["content-type"].startswith("text/markdown")
    assert (journal.parent / "packet.md").read_text(encoding="utf-8") == expected
    log = (store.journal_path(sid).parent / "actions.log").read_text(encoding="utf-8")
    assert f"\t{rid}\tdownload\t" in log
    assert missing.status_code == no_branch.status_code == 404
    assert not list((calc / ".saddle" / "runs").glob("000000000000*"))
    assert [p.name for p in (calc / ".saddle" / "runs").rglob("packet.md")] == ["packet.md"]


# Q9/Q10 end to end -- the Ask lane opens the report the pre-fill names.
# A harness test, not a model-behaviour measurement: the "model" is the
# fake server (tests/fixtures/fake_model_server.py, from the internal M3
# dry run) replaying scripted tool calls. What it proves: a turn started
# from the pre-filled composer can read_file the exact path on the
# pre-fill's last line and gets the full packet bytes back (known-good);
# a read_file of a path outside the session's folder is refused by the
# Ask lane's scoping and returns none of that file (known-bad).

FAKE_SERVER = Path(__file__).parent / "fixtures" / "fake_model_server.py"


@pytest.mark.skipif(not BROWSER, reason="needs node and google-chrome")
def test_the_ask_lane_reads_the_report_the_prefill_names_and_nothing_outside(
    tmp_path: Path,
) -> None:
    import socket
    import sys

    from packet_seed import make_repo, seed

    from saddle.auto import ledger_path
    from saddle.packet import compile_packet, render_packet_text
    from saddle.vllm import VllmClient

    calc = make_repo(tmp_path / "repo")
    store = SessionStore(tmp_path / "s")
    sid, rid, _branch = seed(store, calc, "audited")
    store.update(sid, mode="task")
    report = ledger_path(calc, rid).parent / "packet.md"
    outside = tmp_path / "outside.txt"  # beside the repo, not inside it
    outside.write_text("SECRET-OUTSIDE-THE-FOLDER\n")
    # One key, scripted in order: the good session's read, reply and the
    # title request app._name_session makes after turn 1 (an empty turn);
    # then the same three for the bad session.
    scripts = {
        "ux": [
            [["read_file", {"path": str(report)}]],
            [],
            [],
            [["read_file", {"path": str(outside)}]],
            [],
            [],
        ]
    }
    (tmp_path / "scripts.json").write_text(json.dumps(scripts))
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
    fake = subprocess.Popen(
        [
            sys.executable,
            str(FAKE_SERVER),
            "--port",
            str(port),
            "--scripts",
            str(tmp_path / "scripts.json"),
            "--log",
            str(tmp_path / "requests.jsonl"),
        ],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    try:
        wait_for(lambda: _open(port), timeout=20)

        def factory() -> VllmClient:
            return VllmClient(
                api_key="fake-local", base_url=f"http://127.0.0.1:{port}/ux/v1", model="fake"
            )

        app = build_app(store, factory, default_workdir=calc)
        server = _server_of(app)
        with serving(app) as base:
            args = ["node", str(Path(__file__).parent / "fixtures" / "packet_cdp.mjs")]
            args += [base, sid, "chat-send", "", "", "1200"]
            out = subprocess.run(args, capture_output=True, text=True, timeout=300, check=False)
            assert out.returncode == 0, out.stderr
            got = json.loads(out.stdout.strip().splitlines()[-1])
            wait_for(lambda: idle(server, sid), timeout=60)
            bad = store.create(title="bad", workdir=str(calc)).id  # Ask is the default lane
            with TestClient(app) as http:
                http.post(f"/api/sessions/{bad}/message", json={"text": "read the outside file"})
                wait_for(lambda: idle(server, bad), timeout=60)
    finally:
        fake.terminate()
        fake.wait(timeout=10)
    # Known-good: the pre-fill's last line names the report; the turn's
    # read_file asked for exactly that path and got the full packet back.
    named = got["input"].strip().splitlines()[-1].removeprefix("Full report: ")
    assert Path(named) == report
    full = render_packet_text(compile_packet(ledger_path(calc, rid), run_id=rid))
    calls, results = _tool_calls(store.load_messages(sid))
    assert [(c["name"], json.loads(c["arguments"])["path"]) for c in calls] == [
        ("read_file", named)
    ]
    assert results == [full]
    assert report.read_text(encoding="utf-8") == full
    # Known-bad: the outside path is refused by the Ask lane's scoping.
    calls, results = _tool_calls(store.load_messages(bad))
    assert [c["name"] for c in calls] == ["read_file"]
    assert len(results) == 1
    assert "resolves outside the working directory" in results[0]
    assert "SECRET-OUTSIDE-THE-FOLDER" not in results[0]
    # Every streamed turn offered Ask's read-only tools and nothing else
    # (the title requests offer none).
    seen = [json.loads(line) for line in (tmp_path / "requests.jsonl").read_text().splitlines()]
    assert [r["tools"] for r in seen if r["stream"]] == [["list_dir", "read_file", "search"]] * 4
    e2e = os.environ.get("UXFIX_E2E_DIR")
    if e2e:  # a transcript for the lane's report, when asked for
        for name, messages in (
            ("good", store.load_messages(sid)),
            ("bad", store.load_messages(bad)),
        ):
            (Path(e2e) / f"{name}-messages.json").write_text(json.dumps(messages, indent=1))
        (Path(e2e) / "page-transcript.txt").write_text(got["transcript"])
        (Path(e2e) / "requests.jsonl").write_text((tmp_path / "requests.jsonl").read_text())


def _open(port: int) -> bool:
    import socket

    with socket.socket() as s:
        return s.connect_ex(("127.0.0.1", port)) == 0


def _tool_calls(messages: list[dict]) -> tuple[list[dict], list[str]]:
    calls = [
        call["function"]
        for m in messages
        if m.get("role") == "assistant"
        for call in (m.get("tool_calls") or [])
    ]
    results = [str(m.get("content")) for m in messages if m.get("role") == "tool"]
    return calls, results
