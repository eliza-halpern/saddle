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
def _no_real_api_key(monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep the suite away from the operator's actual key.

    `_api_key` falls back to ~/.config/saddle/env when nothing is exported,
    which is what makes `saddle chat` work without sourcing anything. It also
    means a test that deletes the environment variables still finds a live
    credential on a developer's machine. That is not hypothetical: the five
    "missing key" tests began passing a real key to `main`, and
    test_main_doctor_missing_key_reports reached an actual vLLM server and
    returned 0 instead of 1.

    So every test reads the key from a path that cannot exist, unless it
    points KEY_FILE somewhere itself. Tests must never depend on -- or
    spend -- a real credential.
    """
    from saddle import cli

    monkeypatch.setattr(cli, "KEY_FILE", "/nonexistent/saddle-test-env")


@pytest.fixture(autouse=True)
def _stub_mutmut(tmp_path_factory: pytest.TempPathFactory, monkeypatch: pytest.MonkeyPatch) -> None:
    """Hermetic mutmut: e2e tests exercise the collector without real runs.

    Reports MIN_SIGNIFICANT_MUTANTS killed mutants that locate to the
    line every worktree fixture changes (n.py:2), so a node with healthy
    tests passes the mutation gate for the stated reason.

    This stub used to emit one mutant with unparseable `show` output, so
    nothing was decided and the gate returned its fail-open "no mutants
    on changed lines" PASS -- the fixture's own docstring called the
    verdict vacuous. Roughly thirty tests across the suite depended on
    that path, which is how load-bearing the fail-open had become (#49).
    Tests needing other verdicts still override PATH.
    """
    stub_dir = tmp_path_factory.mktemp("mutmut-stub")
    script = stub_dir / "mutmut"
    results = "\n".join(f"  m{index}: killed" for index in range(1, 6))
    show = "--- n.py\n+++ n.py\n@@ -2 +2 @@\n-    return 2\n+    return 3\n"
    script.write_text(
        "#!/bin/sh\n"
        'case "$1" in\n'
        "  run) exit 0;;\n"
        f"  results) printf '%s\\n' '{results}';;\n"
        f"  show) printf '%s' '{show}';;\n"
        "esac\n"
    )
    script.chmod(0o755)
    monkeypatch.setenv("PATH", f"{stub_dir}{os.pathsep}{os.environ['PATH']}")
