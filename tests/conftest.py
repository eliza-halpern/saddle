"""Shared pytest fixtures: hermetic cwd for every test.

Mutants that drop the `cwd` argument (e.g. `_git_ok`'s `run_argv(argv,
None)`) make git inherit pytest's cwd. Running each test from a
disposable directory keeps those side effects out of the checkout.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


@pytest.fixture(autouse=True)
def _empty_cwd(tmp_path_factory: pytest.TempPathFactory, monkeypatch: pytest.MonkeyPatch) -> None:
    """Run each test with cwd in a dedicated empty tmp dir."""
    monkeypatch.chdir(tmp_path_factory.mktemp("cwd"))


@pytest.fixture(autouse=True)
def _stub_mutmut(tmp_path_factory: pytest.TempPathFactory, monkeypatch: pytest.MonkeyPatch) -> None:
    """Hermetic mutmut: e2e tests exercise the collector without real runs.

    The stub reports one killed mutant with unlocatable output, so the
    verdict is vacuous-pass; tests needing real verdicts override PATH.
    """
    stub_dir = tmp_path_factory.mktemp("mutmut-stub")
    script = stub_dir / "mutmut"
    script.write_text(
        '#!/bin/sh\ncase "$1" in\n  run) exit 0;;\n  results) echo "  m1: killed";;\n'
        '  show) echo "unparseable";;\nesac\n'
    )
    script.chmod(0o755)
    monkeypatch.setenv("PATH", f"{stub_dir}{os.pathsep}{os.environ['PATH']}")
