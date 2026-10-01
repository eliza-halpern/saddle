"""The code block's copy button, from the server side.

The click happens in a browser, so the button lives in the static files,
and its behaviour (what a click copies, how a refused copy reads) is
pinned by tests/markdown.test.js, which the renderer suite's runner in
test_chat_ui runs under node with the rest of `pytest -q`. This file is
the pin the server can own: the files the page loads must carry the
button's wiring -- `app.copy_button_wiring` names any file that lost it,
so a change that strips the button reads as a failure here instead of a
pass.

Known-good: the served files carry the wiring. Known-bad: a file that
lost part of it is named for exactly what it lost, and a file that is
missing lost everything.
"""

from __future__ import annotations

from pathlib import Path

from saddle.web.app import STATIC


def test_the_page_carries_the_code_block_copy_button() -> None:
    # Every file the page loads carries the button's wiring: the CSS
    # rule, the renderer's copy machinery, the terminal row's, and the
    # packet card's.
    from saddle.web.app import copy_button_wiring

    assert copy_button_wiring(STATIC) == {}


def test_the_wiring_pin_names_a_file_that_lost_the_button(tmp_path: Path) -> None:
    # A file that keeps part of the wiring and loses the rest is named
    # for exactly what it lost; a file that is missing lost everything.
    (tmp_path / "markdown.js").write_text(
        "function copyText\nfunction copyButton\n",
        encoding="utf-8",
    )
    from saddle.web.app import copy_button_wiring

    assert copy_button_wiring(tmp_path) == {
        "app.css": [".code-copy"],
        "markdown.js": ["function attachCopy"],
        "app.js": ["attachCopy(", "body.output"],
        "tasks.js": ["attachCopy("],
    }
