"""A research call's full text is kept for the person, never for the acting model (#93).

The acting model gets a cited summary or a typed value. #93 asks for the full text in a
side panel for the person, and it was thrown away when the tool returned: the pages the
reader read and a summary it wrote before the cut existed only in memory. Each call with
an id now leaves one record beside the session's downloads (`research.RESEARCH_RECORDS`).

Known-bad: no record once the tool returns; a record named by an id that climbs out of
its directory. Known-good: one record per call, named by the call's id, holding the text
the acting model got, the reader's whole report (the uncut summary too) and every page it
read, with secret-shaped spans redacted; nothing kept for a call with no id or an id that
is more than a name; a record that cannot be written leaves the answer as it was.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from test_research import LONG, SUMMARY, Rig, Scripted, W, _summary_call, needs_bwrap, tool

from saddle.research import RESEARCH_RECORDS, ReaderGate, Report

INSTALL = "https://docs.example/install"


def _research(rig: Rig, rounds: list[Any], record_id: str | None, want: str = "summary") -> str:
    rig.researcher.client = Scripted(rounds)
    rig.researcher.person_text = [INSTALL]
    return rig.researcher.research("how do I install it?", want, record_id=record_id)


def _record(rig: Rig, record_id: str) -> dict[str, Any]:
    kept: dict[str, Any] = json.loads(
        (rig.researcher.records_dir / f"{record_id}.json").read_text()
    )
    return kept


@pytest.fixture
def rig(tmp_path: Path) -> Any:
    made = Rig(tmp_path)
    yield made
    made.close()


@needs_bwrap
def test_a_summary_call_keeps_its_report_and_every_page_the_reader_read(rig: Rig) -> None:
    fetch = [tool(W + "fetch", url=INSTALL)]
    answer = _research(rig, [fetch, _summary_call(SUMMARY)], "call_1")
    assert "Install guide" not in answer  # the acting model never gets the page
    record = _record(rig, "call_1")
    assert record["answer"] == answer
    assert record["question"] == "how do I install it?"
    assert record["untrusted"] is True
    assert record["report"]["kind"] == "summary"
    assert record["report"]["summary"] == SUMMARY
    assert record["report"]["full_summary"] is None  # nothing was cut
    assert record["visited"] == [INSTALL]
    assert any("Install guide" in page for page in record["read"]), record["read"]
    assert rig.researcher.records_dir == rig.downloads.parent / RESEARCH_RECORDS


@needs_bwrap
def test_an_over_long_summary_keeps_the_text_the_reader_wrote_before_the_cut(rig: Rig) -> None:
    fetch = [tool(W + "fetch", url=INSTALL)]
    long = _summary_call(LONG)
    answer = _research(rig, [fetch, long, long, long], "call_2")
    assert "Point 599" not in answer  # the acting model got the cut summary
    report = _record(rig, "call_2")["report"]
    assert report["shortened"] is True
    assert "Point 599" in report["full_summary"]
    assert "Point 599" not in report["summary"]


@pytest.mark.parametrize("record_id", [None, "", "../escape", "a/b", ".hidden", "x" * 129])
def test_a_call_with_no_id_or_more_than_a_name_keeps_nothing(
    rig: Rig, record_id: str | None
) -> None:
    rig.researcher._keep(record_id, "q", "summary", "answer", "nothing found", ReaderGate())
    assert not rig.researcher.records_dir.exists()
    assert not list(rig.tmp.rglob("escape*")), list(rig.tmp.rglob("escape*"))


def test_a_secret_on_a_page_is_redacted_in_the_record(rig: Rig) -> None:
    key = "sk-" + "a1B2" * 12  # built in pieces so the leak guard can scan this file
    gate = ReaderGate(corpus=[f"the page says api_key={key} and more"])
    report = Report("summary", summary=f"It quotes {key} [1].", sources=(INSTALL,))
    rig.researcher._keep("call_3", "q", "summary", f"summary: {key}", report, gate)
    text = (rig.researcher.records_dir / "call_3.json").read_text()
    assert key not in text, text
    assert "the page says" in text


def test_a_record_that_cannot_be_written_leaves_nothing_behind(rig: Rig) -> None:
    rig.researcher.records_dir.write_text("a file where the directory should be")
    rig.researcher._keep("call_4", "q", "summary", "answer", "nothing found", ReaderGate())
    assert rig.researcher.records_dir.read_text() == "a file where the directory should be"


def test_the_page_is_served_its_own_sessions_records_and_nothing_else(tmp_path: Path) -> None:
    from starlette.testclient import TestClient
    from test_ui3_mode import NoModel

    from saddle.sessions import SessionStore
    from saddle.web.app import build_app

    store = SessionStore(tmp_path / "s")
    sid = store.create(title="t", workdir=str(tmp_path)).id
    other = store.create(title="o", workdir=str(tmp_path)).id
    records = store.downloads_dir(sid).parent / RESEARCH_RECORDS
    records.mkdir(parents=True)
    (records / "call_9.json").write_text(json.dumps({"question": "q", "read": []}))
    (records / "broken.json").write_text("{not json")
    # A file the check must keep out of reach: ".hidden" is no record id.
    (records / ".hidden.json").write_text(json.dumps({"question": "never served"}))
    with TestClient(build_app(store, NoModel, default_workdir=tmp_path)) as client:
        got = client.get(f"/api/sessions/{sid}/research/call_9")
        elsewhere = client.get(f"/api/sessions/{other}/research/call_9")
        missing = client.get(f"/api/sessions/{sid}/research/call_0")
        dotted = client.get(f"/api/sessions/{sid}/research/.call_9")
        broken = client.get(f"/api/sessions/{sid}/research/broken")
        hidden = client.get(f"/api/sessions/{sid}/research/.hidden")
    assert got.status_code == 200, got.text
    assert got.json() == {"question": "q", "read": []}
    for answer in (elsewhere, missing, dotted, broken, hidden):
        assert answer.status_code == 404, answer.text
        assert answer.json() == {"error": "This call's full text was not kept."}
