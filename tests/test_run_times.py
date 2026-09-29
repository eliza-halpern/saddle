"""`saddle verify` says when a run started and finished.

A dogfood run's `saddle verify` transcript printed "Started: (unknown)" and
"Finished: (unknown)": an autonomous run sealed no wall-clock time at all
(none of its 119 records carried `started_at`), and the transcript printed
"(unknown)" whatever the journal held, a slice run's `run` span included.
"""

from __future__ import annotations

import re
from datetime import datetime
from pathlib import Path

from test_ledger_chain import index_of, lines, repo, run, verify_text, write

from saddle.journal import build_span
from saddle.transcript import render_journal_transcript

__all__ = ["repo"]

STAMP = re.compile(r"- Started: (\S+)\n- Finished: (\S+)\n")


def test_an_autonomous_runs_transcript_says_when_it_started_and_finished(repo: Path) -> None:
    before = datetime.now().astimezone()
    result = run(repo)
    after = datetime.now().astimezone()
    code, text = verify_text(result.journal)
    assert code == 0
    found = STAMP.search(text)
    assert found is not None, text
    started, finished = (datetime.fromisoformat(t) for t in found.groups())
    assert before <= started <= finished <= after


def test_a_run_still_in_flight_has_no_finish(repo: Path) -> None:
    result = run(repo)
    rows = lines(result.journal)
    write(result.journal, rows[: index_of(rows, "auto:finished")])
    _, text = verify_text(result.journal)
    assert "- Finished: (no outcome yet)\n" in text
    assert "- Started: (unknown)\n" not in text


def test_a_slice_runs_transcript_reads_its_run_span() -> None:
    span = build_span(
        node_id="",
        argv=["run"],
        duration_ms=61_500,
        exit_code=0,
        detail="1 proven, 0 failed, 0 undispatched",
        kind="agent",
        name="run",
        started_at="2026-09-16T00:00:00+00:00",
    )
    text = render_journal_transcript([], [span], "j")
    assert "- Started: 2026-09-16T00:00:00+00:00\n" in text
    assert "- Finished: 2026-09-16T00:01:01.500000+00:00\n" in text
    older = span.model_copy(update={"started_at": ""})
    text = render_journal_transcript([], [older], "j")
    assert "- Started: (unknown)\n- Finished: (unknown)\n" in text
