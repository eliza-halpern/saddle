"""uv commands the tests run from uv's cache, and online only when it falls short.

The tests pass `--offline` so a machine with a warm cache needs no network.
A fresh CI runner's cache holds only what `uv sync --frozen` fetched: the
locked wheels, and none of the index pages a resolve reads. There uv refuses
with the hint below, and the same command runs again online, as check.sh
itself runs it. Any other failure comes back as it is.

Where there is no network either, the test skips and says so (`NO_PACKAGE_SOURCE`).
saddle's sandboxes, the audit's and a command's, hide HOME, where uv's cache lives,
and have no network: since saddle's own `sandbox-expose` shows uv (#167), a command
the cache cannot serve could only fail there, on the environment and not on the
change (#196). A command the cache can serve still runs offline there.
"""

from __future__ import annotations

import socket
import subprocess
from pathlib import Path
from typing import Final

import pytest

NETWORK_DISABLED: Final = "because the network was disabled"

INDEX_HOST: Final = "pypi.org"
"""Where uv's default index is: the host a command run online would reach."""

NO_PACKAGE_SOURCE: Final = (
    "uv's cache cannot serve this offline and there is no network here"
    " (saddle's sandbox hides HOME, where the cache is): check.sh runs it"
)


def network_reachable(host: str = INDEX_HOST) -> bool:
    """Whether `host` resolves. Inside saddle's sandbox, which has no network, the
    lookup fails at once."""
    try:
        socket.getaddrinfo(host, 443)
    except OSError:
        return False
    return True


def run_uv(argv: list[str], cwd: Path | None = None) -> subprocess.CompletedProcess[str]:
    """Run `argv`, a uv command carrying `--offline`; when uv says its cache was
    not enough, run it again without `--offline`, or, with no network to run it on,
    skip the test (`NO_PACKAGE_SOURCE`)."""
    assert "--offline" in argv, argv
    done = subprocess.run(argv, capture_output=True, text=True, cwd=cwd, check=False)
    if done.returncode != 0 and NETWORK_DISABLED in done.stderr:
        if not network_reachable():
            pytest.skip(NO_PACKAGE_SOURCE)
        online = [arg for arg in argv if arg != "--offline"]
        done = subprocess.run(online, capture_output=True, text=True, cwd=cwd, check=False)
    return done
