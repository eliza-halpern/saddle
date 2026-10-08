"""`run_uv` goes online only when uv says its offline cache fell short.

The first CI run failed four uv tests: a fresh runner's cache has no index
pages, so every offline resolve refused. Known-good: that refusal is retried
online. Known-bad: any other failure is not retried and comes back whole.

Where there is no network as well, five tests failed inside the audit's sandbox
on an unchanged tree once saddle's `sandbox-expose` showed uv (#196): HOME, where
the cache is, is hidden there, and the sandbox has no network. Known-good: there
the test skips, saying so, and a command the cache serves still runs offline.
Known-bad: going online with no network, or skipping a command the cache serves.
"""

from __future__ import annotations

import socket
import stat
import subprocess
import sys
from pathlib import Path

import pytest
import uv_offline
from uv_offline import NETWORK_DISABLED, NO_PACKAGE_SOURCE, network_reachable, run_uv

from saddle import sandbox

TESTS = Path(__file__).resolve().parent

needs_bwrap = pytest.mark.skipif(
    sandbox.isolation_problem() is not None, reason="needs a bwrap that can start"
)


def _fake_uv(tmp_path: Path, offline_error: str | None) -> tuple[Path, Path]:
    """A `uv` that fails with `offline_error` when given `--offline` (or, given
    None, serves it from its cache) and succeeds otherwise; each call's arguments
    are appended to the returned log."""
    log = tmp_path / "calls"
    uv = tmp_path / "uv"
    refuse = (
        ""
        if offline_error is None
        else f'case " $* " in *" --offline "*) echo \'hint: {offline_error}\' >&2; exit 2;; esac\n'
    )
    uv.write_text(f'#!/bin/sh\necho "$*" >> {log}\n{refuse}echo done\n')
    uv.chmod(uv.stat().st_mode | stat.S_IXUSR)
    return uv, log


def _network(monkeypatch: pytest.MonkeyPatch, reachable: bool) -> None:
    monkeypatch.setattr(uv_offline, "network_reachable", lambda host="": reachable)


def _ran(argv: list[str], cwd: Path) -> subprocess.CompletedProcess[str]:
    """`run_uv` where the command must run: a skip raised here would hide the test
    instead of failing it, so it fails."""
    try:
        return run_uv(argv, cwd=cwd)
    except pytest.skip.Exception as skipped:
        pytest.fail(f"skipped where the command must run: {skipped}")


def test_a_cache_without_the_index_pages_runs_the_command_online(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _network(monkeypatch, True)
    uv, log = _fake_uv(tmp_path, f"Packages were unavailable {NETWORK_DISABLED}.")
    done = _ran([str(uv), "lock", "--check", "--offline"], cwd=tmp_path)
    assert (done.returncode, done.stdout) == (0, "done\n")
    assert log.read_text().splitlines() == ["lock --check --offline", "lock --check"]


def test_any_other_failure_is_returned_and_not_retried(tmp_path: Path) -> None:
    uv, log = _fake_uv(tmp_path, "the lockfile needs to be updated")
    done = _ran([str(uv), "lock", "--check", "--offline"], cwd=tmp_path)
    assert done.returncode == 2
    assert "needs to be updated" in done.stderr
    assert log.read_text().splitlines() == ["lock --check --offline"]


def test_with_no_network_a_command_the_cache_cannot_serve_skips_saying_so(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _network(monkeypatch, False)
    uv, log = _fake_uv(tmp_path, f"Packages were unavailable {NETWORK_DISABLED}.")
    with pytest.raises(pytest.skip.Exception, match="no network here"):
        run_uv([str(uv), "lock", "--check", "--offline"], cwd=tmp_path)
    assert log.read_text().splitlines() == ["lock --check --offline"]  # never went online
    assert "check.sh runs it" in NO_PACKAGE_SOURCE


def test_a_command_the_cache_serves_runs_offline_with_no_network(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _network(monkeypatch, False)
    uv, log = _fake_uv(tmp_path, None)
    done = _ran([str(uv), "lock", "--check", "--offline"], cwd=tmp_path)
    assert (done.returncode, done.stdout) == (0, "done\n")
    assert log.read_text().splitlines() == ["lock --check --offline"]


def test_the_network_is_reachable_only_when_the_index_host_resolves(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    asked: list[object] = []

    def resolves(host: str, port: int) -> list[object]:
        asked.append((host, port))
        return [object()]

    def fails(host: str, port: int) -> list[object]:
        raise socket.gaierror(-2, "Name or service not known")

    monkeypatch.setattr(socket, "getaddrinfo", resolves)
    assert network_reachable() is True
    assert asked == [("pypi.org", 443)]
    monkeypatch.setattr(socket, "getaddrinfo", fails)
    assert network_reachable() is False


@needs_bwrap
def test_saddles_sandbox_has_no_network_to_go_online_on(tmp_path: Path) -> None:
    """The probe as the audit's sandbox answers it: `sandbox.confine`, the boundary
    every gate's subprocess (the whole suite included) runs inside. The sandbox
    hides HOME, this checkout with it, so the module goes in the tree it confines."""
    (tmp_path / "uv_offline.py").write_text((TESTS / "uv_offline.py").read_text())
    probe = "from uv_offline import network_reachable\nprint(network_reachable())\n"
    argv, env = sandbox.confine([sys.executable, "-c", probe], tmp_path)
    done = subprocess.run(argv, env=env, cwd=tmp_path, capture_output=True, text=True, check=False)
    assert (done.returncode, done.stdout) == (0, "False\n"), done.stderr
