"""uv commands the tests run from uv's cache, and online only when it falls short.

The tests pass `--offline` so a machine with a warm cache needs no network.
A fresh CI runner's cache holds only what `uv sync --frozen` fetched: the
locked wheels, and none of the index pages a resolve reads. There uv refuses
with the hint below, and the same command runs again online, as check.sh
itself runs it. Any other failure comes back as it is.
"""

from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Final

NETWORK_DISABLED: Final = "because the network was disabled"


def run_uv(argv: list[str], cwd: Path | None = None) -> subprocess.CompletedProcess[str]:
    """Run `argv`, a uv command carrying `--offline`; when uv says its cache was
    not enough, run it again without `--offline`."""
    assert "--offline" in argv, argv
    done = subprocess.run(argv, capture_output=True, text=True, cwd=cwd, check=False)
    if done.returncode != 0 and NETWORK_DISABLED in done.stderr:
        online = [arg for arg in argv if arg != "--offline"]
        done = subprocess.run(online, capture_output=True, text=True, cwd=cwd, check=False)
    return done
