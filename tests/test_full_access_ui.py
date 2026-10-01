"""The page's full-access control (#124), in a real browser.

Known-good: in the Edit lane a "Full access…" button opens a dialog whose
focus starts on "Keep sandboxed"; only "Turn on full access" turns it on; the
topbar banner then shows, survives a reload, and "Turn off" ends it, as does
leaving Edit (coming back does not restore it); a past command that ran
unsandboxed carries a badge on its folded row.

Known-bad: Enter (the default) and Escape leave it off on the server; a past
command that ran sandboxed carries no badge.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path
from typing import Any

import pytest
from test_ui3_mode import BROWSER, NoModel, serving

from saddle.sessions import SessionStore
from saddle.tools import UNSANDBOXED
from saddle.web.app import build_app

CDP = Path(__file__).parent / "fixtures" / "full_access_cdp.mjs"


def _history() -> list[dict[str, Any]]:
    """Two past commands: one that ran with full access, one sandboxed."""

    def ran(call_id: str, command: str, result: str) -> list[dict[str, Any]]:
        arguments = json.dumps({"command": command})
        return [
            {
                "role": "assistant",
                "content": "",
                "tool_calls": [
                    {
                        "id": call_id,
                        "type": "function",
                        "function": {"name": "run_command", "arguments": arguments},
                    }
                ],
            },
            {"role": "tool", "tool_call_id": call_id, "content": result},
        ]

    return [
        {"role": "user", "content": "run two things"},
        *ran("a", "echo outside", f"{UNSANDBOXED}\nexit 0\noutside\n"),
        *ran("b", "echo inside", "exit 0\ninside\n"),
        {"role": "assistant", "content": "done"},
    ]


@pytest.mark.skipif(not BROWSER, reason="needs node and google-chrome")
def test_full_access_is_turned_on_only_by_its_button_and_shown_until_turned_off(
    tmp_path: Path,
) -> None:
    store = SessionStore(tmp_path / "s")
    app = build_app(store, NoModel, default_workdir=tmp_path)
    with serving(app) as base:
        sid = store.create(title="t", workdir=str(tmp_path)).id
        store.update(sid, mode="edit")
        store.save_messages(sid, _history())
        out = subprocess.run(
            ["node", str(CDP), base, sid], capture_output=True, text=True, timeout=90, check=False
        )
        assert out.returncode == 0, out.stderr
        got: dict[str, Any] = json.loads(out.stdout.strip().splitlines()[-1])
        final = store.get(sid).full_access
    assert got["before"]["banner"] is False
    assert got["before"]["openButton"] is True
    # The badge records how a past command ran, whatever the setting is now.
    assert got["before"]["badges"] == [True, False]
    assert got["opened"]["dialogOpen"] is True
    assert got["opened"]["focus"] == "fa-keep"  # the default answer is No
    for refused in ("afterEnter", "afterEscape"):
        assert got[refused]["dialogOpen"] is False, refused
        assert got[refused]["banner"] is False, refused
        assert got[refused]["posts"] == 0, refused  # nothing asked of the server
    assert got["afterGrant"]["posts"] == 1
    assert got["afterGrant"]["banner"] is True
    assert got["afterGrant"]["openButton"] is False
    assert got["afterReload"]["banner"] is True
    assert got["afterReload"]["badges"] == [True, False]  # only the unsandboxed one
    assert got["afterReload"]["overflow"] is False
    assert got["afterOff"]["banner"] is False
    assert got["afterOff"]["openButton"] is True
    assert got["afterRegrant"]["banner"] is True
    # Leaving Edit ends it: no banner, and no way to turn it on outside Edit.
    assert (got["afterAsk"]["banner"], got["afterAsk"]["openButton"]) == (False, False)
    assert got["afterAsk"]["badges"] == [True, False]  # history keeps how they ran
    assert (got["afterBack"]["banner"], got["afterBack"]["openButton"]) == (False, True)
    assert final is False
