"""Shared pytest fixtures: hermetic cwd for every test.

Mutants that drop the `cwd` argument (e.g. `_git_ok`'s `run_argv(argv,
None)`) make git inherit pytest's cwd. Running each test from a
disposable directory keeps those side effects out of the checkout.
"""

from __future__ import annotations

import pytest


@pytest.fixture(autouse=True)
def _empty_cwd(tmp_path_factory: pytest.TempPathFactory, monkeypatch: pytest.MonkeyPatch) -> None:
    """Run each test with cwd in a dedicated empty tmp dir."""
    monkeypatch.chdir(tmp_path_factory.mktemp("cwd"))
