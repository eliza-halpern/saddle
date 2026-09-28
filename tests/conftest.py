"""Shared pytest fixtures: hermetic cwd for every test.

Mutants that drop the `cwd` argument (e.g. `_git_ok`'s `run_argv(argv,
None)`) make git inherit pytest's cwd. Running each test from a
disposable directory keeps those side effects out of the checkout.
"""

from __future__ import annotations

import os
import re
import subprocess
import sys
from pathlib import Path

import pytest

from saddle import evidence
from saddle.journal import SpanRecorder

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

_PRODUCTION_SHOW_ALL = evidence.show_all_mutants
"""The real `show_all_mutants`, saved before `_stub_mutmut` patches it out.

`test_evidence._without_stubbed_mutmut` restores this so tests that run the
real mutmut engine exercise the real lookup, not the PATH-stub replay below.
"""


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

    The server URL and model resolve the same way, so an exported
    SADDLE_BASE_URL or SADDLE_MODEL on the developer's machine is cleared
    too: a test that expects the built-in default must not see theirs.
    """
    from saddle import cli

    monkeypatch.setattr(cli, "KEY_FILE", "/nonexistent/saddle-test-env")
    monkeypatch.delenv(cli.BASE_URL_ENV, raising=False)
    monkeypatch.delenv(cli.MODEL_ENV, raising=False)


def _replay_show_all_mutants(
    scratch: Path, *, recorder: SpanRecorder | None = None
) -> dict[str, str]:
    """Stand-in for `evidence.show_all_mutants` under a PATH-stub `mutmut`.

    None of the ten PATH-stub `mutmut` scripts across the suite write a
    `mutants/` directory, so the production lookup subprocess -- which reads
    mutmut's own meta files -- would find nothing under them. This replays
    the old per-name loop instead: `mutmut results --all True`, then
    `mutmut show NAME` for every name the results line, through whichever
    `mutmut` is first on PATH. `subprocess.run` is used directly, not
    `evidence.run_capture`, so a test that patches `run_capture` never
    observes these calls.
    """
    results = subprocess.run(
        ["mutmut", "results", "--all", "True"], cwd=scratch, capture_output=True, text=True
    )
    names = re.findall(r"^\s*(\S+): ", results.stdout, re.MULTILINE)
    mapping: dict[str, str] = {}
    for name in names:
        shown = subprocess.run(
            ["mutmut", "show", name], cwd=scratch, capture_output=True, text=True
        )
        mapping[name] = shown.stdout
    return mapping


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

    `show_all_mutants` is patched to `_replay_show_all_mutants`: the
    production lookup shells out to a real mutmut installation's meta files,
    which this stub (and the other nine PATH stubs across the suite) never
    creates.
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
    monkeypatch.setattr(evidence, "show_all_mutants", _replay_show_all_mutants)
