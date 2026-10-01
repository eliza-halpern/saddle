"""The one guard every browser test module takes: node and Chrome, or a skip, or an error.

The browser tests drive a real Chrome through a `tests/fixtures/*_cdp.mjs`
node script. Each module used to guard itself with its own `shutil.which`
pair and skip when either tool was missing, so a machine or a sandbox without
them passed the suite having run none of the browser tests, and nothing said
so. This is the single definition: `BROWSER` is the Chrome path when node and
Chrome are both found and None when not, and with `SADDLE_REQUIRE_BROWSER=1`
a missing tool raises `BrowserRequiredError` at import, naming what is
missing, so CI fails instead of skipping. `tests/test_browser_guard.py`
checks that every module that runs a driver takes `BROWSER` from here.
"""

from __future__ import annotations

import os
import shutil
from collections.abc import Mapping

REQUIRE_ENV = "SADDLE_REQUIRE_BROWSER"
"""Set to `1` where a skipped browser test must be a failure (CI)."""

TOOLS = ("node", "google-chrome")


class BrowserRequiredError(RuntimeError):
    """`REQUIRE_ENV` is set and node or Chrome is not on PATH."""


def resolve(environ: Mapping[str, str] | None = None, path: str | None = None) -> str | None:
    """The Chrome path when node and Chrome are both on `path` (default: PATH).

    None when either is missing and `REQUIRE_ENV` is unset, empty or `0`: the
    callers skip. With `REQUIRE_ENV` set otherwise, a missing tool raises
    `BrowserRequiredError` naming each one that is missing. `environ` defaults
    to the process environment."""
    env = os.environ if environ is None else environ
    found = {tool: shutil.which(tool, path=path) for tool in TOOLS}
    missing = [tool for tool, where in found.items() if where is None]
    if not missing:
        return found["google-chrome"]
    if env.get(REQUIRE_ENV, "") not in ("", "0"):
        msg = (
            f"{REQUIRE_ENV} is set, so the browser tests may not skip, "
            f"and {' and '.join(missing)} is not on PATH"
        )
        raise BrowserRequiredError(msg)
    return None


BROWSER = resolve()
"""What every browser test module guards on: `skipif(not BROWSER, ...)`."""
