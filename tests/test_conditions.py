"""What a chat session runs with: said when it starts, and loud when it changes.

Three times a run began with an opt-in capability silently off and nobody saw it
until the run was under way. Two contracts are pinned here (the page's strip is
pinned in tests/test_conditions_ui.py and tests/classic_scripts.test.js):

1. **Start report.** `saddle chat` and `saddle up` print the model, reasoning on
   or off, the effort, whether reasoning is kept, and one line per capability: a
   capability off that would work says so and how to turn it on; one switched on
   that cannot work is an ERROR line, and the chat still starts. Known-bad: an
   off-but-available capability printed as a plain `off`, an unavailable one
   printed without ERROR, a report that never reaches the output.
2. **Changes are recorded and published.** A change of the effort, of reasoning
   on or off, of keeping reasoning or of a capability's state is recorded in the
   session's conditions record (which survives a reload and is never sent to the
   model) and published to the page at the point it was seen; the first look
   records the start and publishes nothing. Known-bad: a change that is not
   recorded, recorded but not published, a capability toggle hidden by the
   cached look, a turn that starts before the change is marked.
"""

from __future__ import annotations

import asyncio
import io
import json
import shutil
from pathlib import Path
from typing import Any

import httpx
import pytest
from starlette.requests import Request
from starlette.testclient import TestClient
from test_chat_server import FakeClient, _endpoint, _server_of

from saddle import capabilities, cli, conditions
from saddle.capabilities import READS_UNKNOWN, Switches
from saddle.events import ConditionsChanged, ContentDelta, Event
from saddle.sessions import SessionStore
from saddle.web import app as web_app

# -- the rows -----------------------------------------------------------------


def refused() -> httpx.Client:
    """A search backend and embeddings server that both refuse: nothing is asked."""

    def handle(request: httpx.Request) -> httpx.Response:
        message = "refused"
        raise httpx.ConnectError(message, request=request)

    return httpx.Client(transport=httpx.MockTransport(handle))


def no_tesseract(monkeypatch: pytest.MonkeyPatch) -> None:
    """tesseract is missing; ImageMagick's programs are there."""
    monkeypatch.setattr(
        shutil, "which", lambda name: None if name == "tesseract" else f"/bin/{name}"
    )


def by_name(rows: list[conditions.Row]) -> dict[str, tuple[str, Any, str]]:
    return {row["name"]: (row["state"], row["available"], row["reason"]) for row in rows}


def test_an_off_capability_says_whether_switching_it_on_would_work(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    no_tesseract(monkeypatch)
    rows = by_name(conditions.capability_rows(Switches(), http=refused(), reads=lambda: None))
    assert [row["name"] for row in conditions.capability_rows(Switches(), http=refused())] == list(
        capabilities.NAMES
    )
    # Known-good: images and imagediff would work, so they are off *and available*.
    assert rows["images"] == ("off", True, "")
    assert rows["imagediff"] == ("off", True, "")
    # Known-bad: ocr would not (tesseract is missing), and says why.
    assert rows["ocr"] == ("off", False, "not installed: tesseract")
    assert rows["mcp"] == ("off", False, "no `access: acting` server is in the MCP allowlist")
    assert all(state == "off" for state, _, _ in rows.values())


def test_a_switched_on_row_keeps_its_state_and_is_never_marked_available(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    no_tesseract(monkeypatch)
    rows = by_name(
        conditions.capability_rows(Switches(ocr=True, images=True), http=refused(), reads=None)
    )
    assert rows["ocr"] == ("unavailable", None, "not installed: tesseract")
    assert rows["images"] == ("on", None, READS_UNKNOWN)
    assert rows["imagediff"] == ("off", True, "")


def test_without_availability_nothing_more_is_asked(monkeypatch: pytest.MonkeyPatch) -> None:
    asked: list[Switches] = []
    real = capabilities.status

    def status(on: Switches, *rest: Any) -> list[capabilities.Status]:
        asked.append(on)
        return real(on, *rest)

    monkeypatch.setattr(capabilities, "status", status)
    rows = conditions.capability_rows(Switches(images=True), http=refused(), availability=False)
    assert asked == [Switches(images=True)]
    assert {row["available"] for row in rows} == {None}
    # With availability, the off ones are looked at again, switched on.
    conditions.capability_rows(Switches(images=True), http=refused())
    assert asked[1:] == [Switches(images=True), Switches(**dict.fromkeys(capabilities.NAMES, True))]


def test_the_rows_read_the_switches_when_none_are_given(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(capabilities.OVERRIDE_ENV, "images")
    rows = by_name(conditions.capability_rows(availability=False))
    assert rows["images"][0] == "on"
    assert rows["ocr"][0] == "off"


# -- snapshots and changes ----------------------------------------------------


def rows_of(**states: str) -> list[dict[str, Any]]:
    return [{"name": n, "state": states.get(n, "off")} for n in capabilities.NAMES]


def test_a_snapshot_says_reasoning_is_off_exactly_when_the_effort_is_none() -> None:
    off = conditions.snapshot(rows_of(images="on"), "none", keep=False)
    assert off == {
        "reasoning": "off",
        "effort": "none",
        "keep_reasoning": "off",
        "capabilities": {**dict.fromkeys(capabilities.NAMES, "off"), "images": "on"},
    }
    on = conditions.snapshot(rows_of(), "low", keep=True)
    assert (on["reasoning"], on["keep_reasoning"]) == ("on", "on")


def test_each_changed_item_is_named_with_its_before_and_after() -> None:
    start = conditions.snapshot(rows_of(), "medium", keep=True)
    assert conditions.changes(start, start) == []
    assert conditions.changes(start, conditions.snapshot(rows_of(), "xhigh", keep=True)) == [
        {"item": "effort", "before": "medium", "after": "xhigh"}
    ]
    assert conditions.changes(start, conditions.snapshot(rows_of(), "none", keep=False)) == [
        {"item": "reasoning", "before": "on", "after": "off"},
        {"item": "effort", "before": "medium", "after": "none"},
        {"item": "keep_reasoning", "before": "on", "after": "off"},
    ]
    later = conditions.snapshot(rows_of(ocr="unavailable", browser="on"), "medium", keep=True)
    assert conditions.changes(start, later) == [
        {"item": "browser", "before": "off", "after": "on"},
        {"item": "ocr", "before": "off", "after": "unavailable"},
    ]


def test_a_capability_on_one_side_only_reads_as_absent_on_the_other() -> None:
    start = conditions.snapshot(rows_of(), "low", keep=True)
    grown = {**start, "capabilities": {**start["capabilities"], "zzz": "on", "aaa": "off"}}
    assert conditions.changes(start, grown) == [
        {"item": "aaa", "before": "absent", "after": "off"},
        {"item": "zzz", "before": "absent", "after": "on"},
    ]
    assert conditions.changes(grown, start)[0] == {
        "item": "aaa",
        "before": "off",
        "after": "absent",
    }
    assert conditions.changes({}, {}) == []


def test_a_change_reads_as_one_line() -> None:
    assert (
        conditions.change_text(
            [
                {"item": "effort", "before": "medium", "after": "xhigh"},
                {"item": "images", "before": "off", "after": "on"},
            ]
        )
        == "effort medium → xhigh; images off → on"
    )


# -- contract 1: the start report ---------------------------------------------


REPORT_ROWS: list[conditions.Row] = [
    {"name": "mcp", "state": "off", "reason": "no server", "available": False},
    {"name": "browser", "state": "off", "reason": "", "available": True},
    {"name": "images", "state": "on", "reason": READS_UNKNOWN, "available": None},
    {
        "name": "ocr",
        "state": "unavailable",
        "reason": "not installed: tesseract",
        "available": None,
    },
    {"name": "imagediff", "state": "on", "reason": "", "available": None},
    {"name": "search", "state": "off", "reason": "", "available": None},
]

REPORT_CAPABILITIES = [
    "      mcp        off",
    "!     browser    off -- available: turn it on with `saddle capabilities enable browser` "
    "(or $SADDLE_CAPABILITIES=browser for one process)",
    f"      images     on ({READS_UNKNOWN})",
    "ERROR ocr        unavailable: not installed: tesseract (it is switched on)",
    "      imagediff  on",
    "      search     off",
]


def test_the_start_report_states_every_condition_and_marks_the_two_that_need_the_person() -> None:
    lines = conditions.startup_lines(model="m-1", effort="medium", keep=True, rows=REPORT_ROWS)
    assert lines == [
        "model: m-1",
        "reasoning: on, effort medium",
        "keep reasoning: on (each round's reasoning is sent back to the model)",
        "capabilities:",
        *REPORT_CAPABILITIES,
    ]
    off = conditions.startup_lines(model="m", effort="none", keep=False, rows=[], effort_note="n")
    assert off == [
        "model: m",
        "reasoning: off, effort none (n)",
        "keep reasoning: off (past reasoning is not sent back to the model)",
        "capabilities:",
    ]


@pytest.fixture
def looked(monkeypatch: pytest.MonkeyPatch) -> list[dict[str, Any]]:
    """`conditions.capability_rows` answers REPORT_ROWS and records how it was asked."""
    calls: list[dict[str, Any]] = []

    def look(*args: Any, **kwargs: Any) -> list[conditions.Row]:
        calls.append(kwargs)
        return [dict(row) for row in REPORT_ROWS]

    monkeypatch.setattr(conditions, "capability_rows", look)
    return calls


def _serve(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, **extra: Any) -> tuple[str, Any]:
    import uvicorn

    handed: dict[str, Any] = {}
    monkeypatch.setattr(uvicorn, "run", lambda app, **_kw: handed.update(app=app))
    out = io.StringIO()
    web_app.serve(
        host="127.0.0.1",
        port=9999,
        api_key="unused",
        base_url="http://example.invalid/v1",
        model="served-model",
        workdir=tmp_path,
        sessions_root=tmp_path / "sessions",
        report=out,
        **extra,
    )
    return out.getvalue(), handed["app"]


def test_the_web_chat_prints_its_start_report_before_it_serves(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, looked: list[dict[str, Any]]
) -> None:
    printed, app = _serve(tmp_path, monkeypatch, keep_reasoning=False)
    assert printed.splitlines() == [
        "model: served-model",
        "reasoning: on, effort xhigh (new sessions start at this; each session has its own)",
        "keep reasoning: off (past reasoning is not sent back to the model)",
        "capabilities:",
        *REPORT_CAPABILITIES,
    ]
    # It looked with the served model's known reading (not yet asked: None),
    # and the page's strip looks the same way.
    assert looked[0]["reads"]() is None
    assert _server_of(app).capability_probe() == REPORT_ROWS
    assert looked[1]["reads"]() is None


def test_a_broken_switch_file_is_an_error_line_and_the_web_chat_still_starts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(capabilities.OVERRIDE_ENV, "nonsense")
    printed, app = _serve(tmp_path, monkeypatch)
    assert printed.splitlines()[0].startswith(
        "ERROR the capability switches cannot be read: $SADDLE_CAPABILITIES: 'nonsense'"
    )
    assert printed.splitlines()[-1] == "capabilities:"
    assert _server_of(app) is not None


def test_the_web_chat_prints_to_stdout_by_default(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    looked: list[dict[str, Any]],
    capsys: pytest.CaptureFixture[str],
) -> None:
    import uvicorn

    monkeypatch.setattr(uvicorn, "run", lambda app, **_kw: None)
    web_app.serve(
        host="127.0.0.1",
        port=9999,
        api_key="unused",
        base_url="http://example.invalid/v1",
        model="served-model",
        workdir=tmp_path,
        sessions_root=tmp_path / "sessions",
    )
    assert "ERROR ocr        unavailable" in capsys.readouterr().out


class _UpClient(FakeClient):
    def __init__(self, **_kw: Any) -> None:
        super().__init__()


def test_the_terminal_chat_prints_its_start_report_before_the_first_prompt(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, looked: list[dict[str, Any]]
) -> None:
    order: list[str] = []
    monkeypatch.setenv("SADDLE_VLLM_API_KEY", "k")
    monkeypatch.setattr(cli, "VllmClient", _UpClient)
    monkeypatch.setattr(cli, "check_server", lambda *_a, **_k: None)
    out = io.StringIO()

    def run_chat(*_a: Any, **_k: Any) -> int:
        order.append(out.getvalue())
        return 0

    monkeypatch.setattr(cli, "run_chat", run_chat)
    argv = ["up", "--model", "m-2", "--reasoning-effort", "low", "--no-keep-reasoning"]
    assert cli.main([*argv, "--journal", str(tmp_path / "j.jsonl")], stdout=out) == 0
    assert order[0].splitlines() == [
        "model: m-2",
        "reasoning: on, effort low",
        "keep reasoning: off (past reasoning is not sent back to the model)",
        "capabilities:",
        *REPORT_CAPABILITIES,
    ]
    assert looked[0]["reads"]() is None


def test_a_broken_switch_file_is_an_error_line_in_the_terminal_report() -> None:
    out = io.StringIO()
    cli.print_conditions(model="m", effort="low", keep=True, reads=None, stdout=out)
    assert "ERROR" not in out.getvalue()  # known-good: the suite's switches read fine
    with pytest.MonkeyPatch.context() as patch:
        patch.setenv(capabilities.OVERRIDE_ENV, "nonsense")
        out = io.StringIO()
        cli.print_conditions(model="m", effort="low", keep=True, reads=None, stdout=out)
    lines = out.getvalue().splitlines()
    assert lines[0].startswith("ERROR the capability switches cannot be read:")
    assert lines[1:] == [
        "model: m",
        "reasoning: on, effort low",
        "keep reasoning: on (each round's reasoning is sent back to the model)",
        "capabilities:",
    ]


# -- contract 3: a change is recorded, published and kept ----------------------


def switch_probe(calls: list[int]) -> conditions.Probe:
    """Rows that follow the switches, as the real look does, counting the looks."""

    def probe() -> list[conditions.Row]:
        calls.append(1)
        on = capabilities.load()
        return [
            {"name": n, "state": "on" if on.get(n) else "off", "reason": "", "available": True}
            for n in capabilities.NAMES
        ]

    return probe


@pytest.fixture
def server(tmp_path: Path) -> ChatServerAndLooks:
    calls: list[int] = []
    store = SessionStore(tmp_path / "sessions")
    made = web_app.ChatServer(
        store, FakeClient, default_workdir=tmp_path, capability_probe=switch_probe(calls)
    )
    return ChatServerAndLooks(made, calls)


class ChatServerAndLooks:
    def __init__(self, server: web_app.ChatServer, looks: list[int]) -> None:
        self.server = server
        self.looks = looks
        self.store = server.store


def drain(channel: Any) -> list[Event]:
    seen: list[Event] = []
    while not channel.empty():
        event = channel.get_nowait()
        if event is not None:
            seen.append(event)
    return seen


def test_the_first_look_records_the_start_and_publishes_nothing(
    server: ChatServerAndLooks, tmp_path: Path
) -> None:
    sid = server.store.create(title="t", workdir=str(tmp_path), reasoning_effort="medium").id
    channel = server.server._live(sid).subscribe()
    view = server.server.check_conditions(sid)
    assert view["start"] == {k: v for k, v in view["current"].items() if k != "rows"}
    assert view["changes"] == []
    assert view["error"] == ""
    assert view["start"]["effort"] == "medium"
    assert view["current"]["rows"][0] == {
        "name": "mcp",
        "state": "off",
        "reason": "",
        "available": True,
    }
    assert drain(channel) == []
    assert len(server.store.conditions(sid)) == 1
    # Looking again with nothing changed records nothing.
    server.server.check_conditions(sid)
    assert len(server.store.conditions(sid)) == 1


def test_an_effort_change_is_recorded_published_and_survives_a_reload(
    server: ChatServerAndLooks, tmp_path: Path
) -> None:
    sid = server.store.create(title="t", workdir=str(tmp_path), reasoning_effort="medium").id
    server.server.check_conditions(sid)
    channel = server.server._live(sid).subscribe()
    server.store.update(sid, reasoning_effort="none")
    view = server.server.check_conditions(sid)
    changed = [
        {"item": "reasoning", "before": "on", "after": "off"},
        {"item": "effort", "before": "medium", "after": "none"},
    ]
    (event,) = drain(channel)
    assert isinstance(event, ConditionsChanged)
    assert (event.changes, event.at, event.view) == (changed, 0, view)
    assert view["changes"] == [{"at": 0, "time": event.time, "changes": changed}]
    assert view["current"]["effort"] == "none"
    assert view["start"]["effort"] == "medium"
    # Kept beside the transcript, not in it: the model is never sent these rows.
    assert server.store.load_messages(sid) == []
    again = web_app.ChatServer(server.store, FakeClient, default_workdir=tmp_path)
    assert again.check_conditions(sid)["changes"] == view["changes"]


def test_a_capability_switched_on_is_seen_at_once_despite_the_cached_look(
    server: ChatServerAndLooks, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    sid = server.store.create(title="t", workdir=str(tmp_path)).id
    server.server.check_conditions(sid)
    server.server.check_conditions(sid)
    assert server.looks == [1]  # known-good: unchanged switches reuse the look
    monkeypatch.setenv(capabilities.OVERRIDE_ENV, "ocr")
    view = server.server.check_conditions(sid)
    assert server.looks == [1, 1]
    assert view["changes"][0]["changes"] == [{"item": "ocr", "before": "off", "after": "on"}]


def test_a_look_older_than_its_lifetime_is_taken_again(server: ChatServerAndLooks) -> None:
    server.server.capability_rows()
    switches, when, rows = server.server.looked or (Switches(), 0.0, [])
    server.server.looked = (switches, when - web_app.CONDITIONS_TTL_S - 1, rows)
    server.server.capability_rows()
    assert server.looks == [1, 1]


def test_switches_that_cannot_be_read_show_as_an_error_and_record_nothing(
    server: ChatServerAndLooks, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    sid = server.store.create(title="t", workdir=str(tmp_path)).id
    monkeypatch.setenv(capabilities.OVERRIDE_ENV, "nonsense")
    view = server.server.check_conditions(sid)
    assert view["current"] is None
    assert view["start"] is None
    assert view["error"].startswith("$SADDLE_CAPABILITIES: 'nonsense'")
    assert server.store.conditions(sid) == []


@pytest.fixture
def scripted_turns(monkeypatch: pytest.MonkeyPatch) -> None:
    def fake_run_turn(
        _client: Any, messages: list[dict[str, Any]], text: str, _options: Any, **_kw: Any
    ) -> Any:
        messages.append({"role": "user", "content": text})
        yield ContentDelta(text="ok")

    monkeypatch.setattr(web_app, "run_turn", fake_run_turn)


@pytest.mark.usefixtures("scripted_turns")
def test_a_turn_marks_a_change_before_it_starts(
    server: ChatServerAndLooks, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    sid = server.store.create(title="t", workdir=str(tmp_path)).id
    server.server._run(sid, "first")
    assert len(server.store.conditions(sid)) == 1  # the start, seen by the first turn
    monkeypatch.setenv(capabilities.OVERRIDE_ENV, "images")
    channel = server.server._live(sid).subscribe()
    server.server._run(sid, "second")
    seen = drain(channel)
    assert isinstance(seen[0], ConditionsChanged), seen
    assert seen[0].at == 1  # after the first turn's one message, before the second's
    assert any(isinstance(event, ContentDelta) for event in seen[1:])


def test_the_page_reads_the_conditions_and_an_effort_patch_marks_its_change(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = SessionStore(tmp_path / "sessions")
    app = web_app.build_app(store, FakeClient, default_workdir=tmp_path)
    with TestClient(app) as client:
        sid = client.post("/api/sessions", json={"reasoning_effort": "medium"}).json()["id"]
        first = client.get(f"/api/sessions/{sid}/conditions").json()
        assert first["changes"] == []
        # Without a full look, an off capability's availability is not claimed.
        assert {row["available"] for row in first["current"]["rows"]} == {None}
        client.patch(f"/api/sessions/{sid}", json={"title": "renamed"})
        assert len(store.conditions(sid)) == 1  # a title is not a condition
        client.patch(f"/api/sessions/{sid}", json={"reasoning_effort": "xhigh"})
        assert store.conditions(sid)[-1]["changes"] == [
            {"item": "effort", "before": "medium", "after": "xhigh"}
        ]
        after = client.get(f"/api/sessions/{sid}/conditions").json()
        assert after["current"]["effort"] == "xhigh"
        assert len(after["changes"]) == 1


def test_a_reconnecting_page_gets_the_conditions_with_the_session(tmp_path: Path) -> None:
    store = SessionStore(tmp_path / "sessions")
    sid = store.create(title="t", workdir=str(tmp_path), reasoning_effort="low").id
    app = web_app.build_app(store, FakeClient, default_workdir=tmp_path)
    server = _server_of(app)
    server.check_conditions(sid)
    store.update(sid, reasoning_effort="medium")
    server.check_conditions(sid)
    events = _endpoint(app, "/api/sessions/{sid}/events")

    async def receive() -> dict[str, str]:
        return {"type": "http.disconnect"}

    async def drive() -> list[str]:
        request = Request(
            {
                "type": "http",
                "method": "GET",
                "path": f"/api/sessions/{sid}/events",
                "headers": [],
                "query_string": b"",
                "path_params": {"sid": sid},
            },
            receive,
        )
        response = await events(request)
        return [chunk async for chunk in response.body_iterator]

    info = json.loads(asyncio.run(drive())[0][6:])
    assert info["kind"] == "session.info"
    assert info["conditions"]["start"]["effort"] == "low"
    assert info["conditions"]["current"]["effort"] == "medium"
    assert info["conditions"]["changes"][0]["changes"] == [
        {"item": "effort", "before": "low", "after": "medium"}
    ]


# -- the record ---------------------------------------------------------------


def test_the_record_skips_a_torn_line_and_is_not_written_for_a_purged_session(
    tmp_path: Path,
) -> None:
    store = SessionStore(tmp_path / "sessions")
    sid = store.create(title="t", workdir=str(tmp_path)).id
    assert store.conditions(sid) == []
    store.record_conditions(sid, {"at": 0})
    with store.conditions_path(sid).open("a", encoding="utf-8") as out:
        out.write('[1]\n{"at": 1}\n{"at": 2')
    assert store.conditions(sid) == [{"at": 0}, {"at": 1}]
    shutil.rmtree(store.conditions_path(sid).parent)
    store.record_conditions(sid, {"at": 3})
    assert not store.conditions_path(sid).exists()
