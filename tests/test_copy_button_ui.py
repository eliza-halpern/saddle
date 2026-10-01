"""The code-block copy button, clicked in a real Chrome on both clipboard paths.

Known-good: on a loopback address (a secure context, so the async clipboard
exists) a click on a fenced block's button leaves exactly the code on the
system clipboard, the button says Copied and settles back to Copy; the same on
a plain-http non-loopback address, where `navigator.clipboard` is absent, goes
through a selected, read-only field and `execCommand("copy")`, removes the
field afterwards and gives focus back to the button. A tool row's button sits
in its summary, copies the text the tool returned, and a click on it does not
fold or unfold the row.

Known-bad: the node tests of the button run on a fake DOM whose `children` is
an Array; in a real page it is an HTMLCollection with no `find`, and the
button's wiring for rows, diffs and results threw, which took every tool row
and the packet card's panels down. The case that opens the page on each
origin asserts the origin really is (or is not) a secure context, so it cannot
silently test the other path.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path
from typing import Any

import pytest
from browser_guard import BROWSER
from test_ui3_mode import NoModel, serving

from saddle.sessions import SessionStore
from saddle.web.app import build_app

CDP = Path(__file__).parent / "fixtures" / "copy_cdp.mjs"
pytestmark = pytest.mark.skipif(not BROWSER, reason="needs node and google-chrome")

# Quotes, angle brackets, a blank line, an indent and a trailing newline-free end:
# anything a DOM round-trip or a label glued on would change.
CODE = 'def greet(name):\n    print("hi, <" + name + ">")\n\n    return \'x\' + "\\n"'
OUTPUT = "exit 0\nbuild ok\nsecond line  \n"


def _history() -> list[dict[str, Any]]:
    call = {
        "id": "a",
        "type": "function",
        "function": {"name": "run_command", "arguments": json.dumps({"command": "make"})},
    }
    return [
        {"role": "user", "content": "show me code and run make"},
        {"role": "assistant", "content": f"Here it is:\n\n```python\n{CODE}\n```\n"},
        {"role": "assistant", "content": "", "tool_calls": [call]},
        {"role": "tool", "tool_call_id": "a", "content": OUTPUT},
        {"role": "assistant", "content": "done"},
    ]


def drive(tmp_path: Path, host: str) -> dict[str, Any]:
    store = SessionStore(tmp_path / "s")
    app = build_app(store, NoModel, default_workdir=tmp_path)
    with serving(app) as base:
        sid = store.create(title="t", workdir=str(tmp_path)).id
        store.save_messages(sid, _history())
        url = base.replace("127.0.0.1", host)
        out = subprocess.run(
            ["node", str(CDP), url, sid], capture_output=True, text=True, timeout=120, check=False
        )
    assert out.returncode == 0, out.stderr
    got: dict[str, Any] = json.loads(out.stdout.strip().splitlines()[-1])
    return got


def test_on_a_loopback_address_the_async_clipboard_holds_exactly_the_code(tmp_path: Path) -> None:
    got = drive(tmp_path, "127.0.0.1")
    assert got["env"]["secure"] is True
    assert got["env"]["hasClipboard"] is True
    assert got["granted"] is True
    block = got["block"]
    assert block["clipboard"] == CODE  # no label, no fence, no trailing junk
    assert (block["before"], block["shown"], block["after"]) == ("Copy", "Copied", "Copy")
    assert block["copies"] == []  # the fallback was not needed
    assert got["blockTitle"] == "Copy code (python)"


def test_on_a_plain_http_address_the_page_falls_back_to_exec_command(tmp_path: Path) -> None:
    got = drive(tmp_path, "saddle-ui.test")
    # The case is only meaningful if this origin is not a secure context.
    assert got["env"]["origin"].startswith("http://saddle-ui.test:")
    assert got["env"]["secure"] is False
    assert got["env"]["hasClipboard"] is False
    block = got["block"]
    assert block["clipboard"] is None
    assert block["copies"] == [{"name": "copy", "ok": True, "field": "textarea", "selected": CODE}]
    assert (block["before"], block["shown"], block["after"]) == ("Copy", "Copied", "Copy")
    assert block["newFields"] == 0  # the temporary field is gone
    assert block["focusOnButton"] is True  # focus went back to where it was


@pytest.mark.parametrize("host", ["127.0.0.1", "saddle-ui.test"], ids=["async", "fallback"])
def test_a_tool_rows_button_sits_in_its_summary_and_does_not_fold_the_row(
    tmp_path: Path, host: str
) -> None:
    got = drive(tmp_path, host)
    shape = got["rowShape"]
    assert (shape["inSummary"], shape["inDetail"]) == (1, 0)
    assert shape["detailText"] == OUTPUT  # the detail holds the output and nothing else
    assert got["rowOpenAfter"] == got["rowOpenBefore"]  # the click did not toggle the row
    row = got["row"]
    assert (row["before"], row["shown"], row["after"]) == ("Copy", "Copied", "Copy")
    if host == "127.0.0.1":
        assert row["clipboard"] == OUTPUT
    else:
        assert row["copies"] == [
            {"name": "copy", "ok": True, "field": "textarea", "selected": OUTPUT}
        ]
        assert row["newFields"] == 0
        assert row["focusOnButton"] is True
