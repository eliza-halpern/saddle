"""Every ruff run saddle starts stops at RUFF_TIMEOUT_S (#168).

A deadlocked `ruff check` once held a dogfood run for 31 minutes, and runs have
no time cap. `ruff_findings` and the node gate's `format --check` have their own
tests (test_evidence.py, test_runner.py); these cover the other call sites the
contract names. Each module binds the constant at import, so each is patched in
the module that reads it, and a fake `ruff` that never exits stands in for a hung
one: a call site without the ceiling would block for the fake's 40 seconds.
"""

from __future__ import annotations

import os
import stat
import subprocess
import time
from pathlib import Path

import pytest

from saddle import auditor as auditor_module
from saddle import auto as auto_module
from saddle import evidence as evidence_module
from saddle import slice as slice_module
from saddle.auditor import Auditor, AuditorConfig
from saddle.auto import format_changed
from saddle.slice import autofix

HUNG_FOR_S = 40
"""How long the fake ruff blocks: well past the bound each test asserts."""

BOUND_S = 30
"""What a call site with the ceiling (patched to 1 s) must finish under."""


def _git(root: Path, *args: str) -> None:
    subprocess.run(
        ["git", "-C", str(root), "-c", "user.name=t", "-c", "user.email=t@t", *args],
        check=True,
        capture_output=True,
    )


@pytest.fixture
def hung_ruff(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A `ruff` on PATH that never exits, and a 1 s ceiling in every module."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    fake = bin_dir / "ruff"
    fake.write_text(f"#!/bin/sh\nexec sleep {HUNG_FOR_S}\n")
    fake.chmod(fake.stat().st_mode | stat.S_IXUSR)
    monkeypatch.setenv("PATH", f"{bin_dir}{os.pathsep}{os.environ['PATH']}")
    for module in (evidence_module, auditor_module, auto_module, slice_module):
        monkeypatch.setattr(module, "RUFF_TIMEOUT_S", 1.0)


def _repo(root: Path) -> Path:
    """A repo that chose ruff, with one changed Python file."""
    root.mkdir()
    (root / "ruff.toml").write_text("line-length = 88\n")
    (root / "m.py").write_text("x = 1\n")
    _git(root, "init", "-q")
    _git(root, "add", "-A")
    _git(root, "commit", "-q", "-m", "base")
    (root / "m.py").write_text("x = 2\n")
    return root


@pytest.mark.usefixtures("hung_ruff")
def test_the_auditors_tier0_format_diff_stops_at_the_timeout(tmp_path: Path) -> None:
    repo = _repo(tmp_path / "repo")
    started = time.monotonic()
    Auditor(repo, "HEAD", AuditorConfig()).tier0("m.py", "x = 2\n")
    assert time.monotonic() - started < BOUND_S


@pytest.mark.usefixtures("hung_ruff")
def test_format_at_finish_stops_at_the_timeout(tmp_path: Path) -> None:
    repo = _repo(tmp_path / "repo")
    started = time.monotonic()
    format_changed(repo, "HEAD")
    assert time.monotonic() - started < BOUND_S


@pytest.mark.usefixtures("hung_ruff")
def test_the_slice_autofix_stops_both_its_ruff_runs_at_the_timeout(tmp_path: Path) -> None:
    repo = _repo(tmp_path / "repo")
    started = time.monotonic()
    autofix(repo)
    # two ruff runs, check --fix then format: each must stop at the ceiling
    assert time.monotonic() - started < BOUND_S
