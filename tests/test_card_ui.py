"""The task card in a real browser: its meters across a reload, and the
model-activity strip that is not evidence.

Model activity (#112). Contract: the strip reads the run's live events and
writes nothing a verdict, the ledger or the packet reads. Known-good: while
a reply streams, the strip's count grows and the ledger's line count does
not; it is folded by default, labelled "not evidence", the fold is
remembered per browser, and a card rebuilt after a reload says its counts
are "since this page opened" until the next reply. Known-bad: none of its
tail appears as a ledger line, and it sends nothing to the server.

Meters (#114). Contract: a card built after a reload shows the elapsed time
and tokens the run's own budget has spent, not a meter restarted at zero,
and an ended run's card -- live or rebuilt from its recap -- shows the
numbers its outcome sealed. Known-good: reloaded mid-reply, the time meter
is not behind where it was before the reload; after Stop and a reload the
card shows the stopped card's time and tokens. Known-bad: the meter that
restarted at the last progress report, and the recap card that read "0s"
and "—" under a packet that said otherwise.
"""

from __future__ import annotations

import json
import os
import subprocess
import time
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from browser_guard import BROWSER
from test_ui3_mode import _server_of, serving
from test_web_tasks import call, repo  # noqa: F401 -- the fixture

from saddle.journal import attempt_sidecar_path, read_spans
from saddle.sessions import SessionStore
from saddle.vllm import StreamToken
from saddle.web.app import build_app

CDP = Path(__file__).parent / "fixtures" / "card_cdp.mjs"
pytestmark = pytest.mark.skipif(not BROWSER, reason="needs node and google-chrome")

THOUGHT = (
    "The test expects add(2, 2) to be 4 and calc.add subtracts, so the operator is wrong. "
    "Only tests/test_calc.py imports it; the smallest change is the operator alone. "
)


class LongReply:
    """A first reply that streams reasoning for ten seconds and then reads a
    file, then a reply that streams until the run is stopped (at most a
    minute). A reload during the first reply comes before any run.progress:
    the snapshot is all the card has."""

    def __init__(self) -> None:
        self.turn = 0

    def __enter__(self) -> LongReply:
        return self

    def __exit__(self, *_: object) -> None:
        return None

    def max_model_len(self) -> int:
        return 200_000

    def stream_chat(self, messages: Any, **_: Any) -> Iterator[Any]:
        self.turn += 1
        first = self.turn == 1
        deadline = time.monotonic() + (10 if first else 60)
        while time.monotonic() < deadline:
            for word in THOUGHT.split(" "):
                time.sleep(0.02)
                yield StreamToken(stream="reasoning", text=word + " ")
        if first:
            yield call("read_file", "r1", path="calc.py")
        else:
            yield call("finish", "f", summary="gave up")


def test_the_cards_meters_survive_a_reload_and_the_strip_is_not_evidence(
    tmp_path: Path,
    repo: Path,  # noqa: F811
) -> None:
    store = SessionStore(tmp_path / "s")
    model = LongReply()
    app = build_app(store, lambda: model, default_workdir=repo, arm="E")
    server = _server_of(app)
    shots = os.environ.get("CARD_SHOTS", "")
    with serving(app) as base:
        sid = store.create(workdir=str(repo)).id
        out = subprocess.run(
            ["node", str(CDP), base, sid, "1280", "dark", shots, "card-"],
            capture_output=True,
            text=True,
            timeout=240,
            check=False,
        )
    assert out.returncode == 0, out.stderr
    got: dict[str, Any] = json.loads(out.stdout.strip().splitlines()[-1])

    # -- the strip: live, folded, labelled, and apart from the ledger ------------
    first, later, opened = got["streaming"]
    assert first["strip"]["shown"]
    assert not first["strip"]["open"]  # folded by default
    assert "not evidence" in first["strip"]["head"]
    assert first["strip"]["mode"] == "thinking"
    assert later["strip"]["chars"] > first["strip"]["chars"]  # the tail grows...
    assert later["lines"] == first["lines"]  # ...and the ledger does not
    assert opened["strip"]["open"]
    assert opened["strip"]["tailLabel"] == "latest reasoning, not evidence"
    assert opened["strip"]["tail"].strip()
    assert "since this page opened" not in opened["strip"]["streamed"]  # watched from its start
    assert opened["strip"]["tool"] == "none yet"
    assert not opened["strip"]["inLedger"]
    assert got["stripPosts"] == 0  # it asks the server for nothing
    reloaded, tool = got["afterReload"]["strip"], got["afterTool"]["strip"]
    assert reloaded["open"]  # the fold is remembered
    # Rebuilt after a reload, it joins the reply part-way and says so...
    assert "since this page opened" in reloaded["streamed"]
    assert reloaded["tool"] == "none since this page opened"
    # ...until the next tool call starts a reply it sees whole.
    assert tool["tool"] == "Read calc.py"
    assert "since this page opened" not in tool["streamed"]
    assert not tool["inLedger"]

    # -- the meters across a reload mid-reply ---------------------------------
    before, after = got["beforeReload"], got["afterReload"]
    assert before["shown"] > 2
    assert after["shown"] >= before["shown"] - 0.5  # not restarted
    # flip: was `after["tokens"] == before["tokens"]`. It pinned "no progress
    # event mid-reply", which the engine's partial progress (sent about once
    # a second while a reply streams) made false: the live meter advances
    # between the two reads (known-good below). The contract is that a
    # reload never steps the meter back; that is asserted on the first
    # paint after the reload, before any new progress event can repaint it.
    first, later = got["streaming"][0], got["streaming"][1]
    assert later["spent"] > first["spent"]  # the meter moves mid-reply
    right_after = got["firstAfterReload"]
    assert right_after["tokens"] != "—"
    assert right_after["spent"] >= before["spent"] > 0
    assert after["spent"] >= before["spent"]
    assert after["lines"] == before["lines"] == 1  # still the first reply

    # -- an ended run: live, then rebuilt from its recap ------------------------
    stopped, recap = got["stopped"], got["recap"]
    assert stopped["state"] == recap["state"] == "stopped"
    assert not stopped["strip"]["shown"]  # the packet is the story now
    assert recap["metersShown"]
    assert recap["time"] != "0s"
    assert recap["tokens"] != "—"
    assert (recap["time"], recap["tokens"]) == (stopped["time"], stopped["tokens"])
    run = next(iter(server.tasks.values()))
    assert run.journal is not None
    end = next(s for s in reversed(read_spans(run.journal)) if s.name.startswith("auto:"))
    sealed = json.loads(attempt_sidecar_path(run.journal, end.span_id).read_text())
    assert recap["shown"] == sealed["elapsed_s"]
    assert stopped["shown"] == sealed["elapsed_s"]
