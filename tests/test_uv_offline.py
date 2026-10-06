"""`run_uv` goes online only when uv says its offline cache fell short.

The first CI run failed four uv tests: a fresh runner's cache has no index
pages, so every offline resolve refused. Known-good: that refusal is retried
online. Known-bad: any other failure is not retried and comes back whole.
"""

from __future__ import annotations

import stat
from pathlib import Path

from uv_offline import NETWORK_DISABLED, run_uv


def _fake_uv(tmp_path: Path, offline_error: str) -> tuple[Path, Path]:
    """A `uv` that fails with `offline_error` when given `--offline` and succeeds
    otherwise; each call's arguments are appended to the returned log."""
    log = tmp_path / "calls"
    uv = tmp_path / "uv"
    uv.write_text(
        "#!/bin/sh\n"
        f'echo "$*" >> {log}\n'
        'case " $* " in *" --offline "*) '
        f"echo 'hint: {offline_error}' >&2; exit 2;; esac\n"
        "echo done\n"
    )
    uv.chmod(uv.stat().st_mode | stat.S_IXUSR)
    return uv, log


def test_a_cache_without_the_index_pages_runs_the_command_online(tmp_path: Path) -> None:
    uv, log = _fake_uv(tmp_path, f"Packages were unavailable {NETWORK_DISABLED}.")
    done = run_uv([str(uv), "lock", "--check", "--offline"], cwd=tmp_path)
    assert (done.returncode, done.stdout) == (0, "done\n")
    assert log.read_text().splitlines() == ["lock --check --offline", "lock --check"]


def test_any_other_failure_is_returned_and_not_retried(tmp_path: Path) -> None:
    uv, log = _fake_uv(tmp_path, "the lockfile needs to be updated")
    done = run_uv([str(uv), "lock", "--check", "--offline"], cwd=tmp_path)
    assert done.returncode == 2
    assert "needs to be updated" in done.stderr
    assert log.read_text().splitlines() == ["lock --check --offline"]
