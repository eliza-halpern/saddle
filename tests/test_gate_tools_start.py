"""Every tool of saddle's own gate stages starts inside the audit's sandbox (#167).

`uv lock --check` is one of saddle's gate stages, but `uv` lives under HOME, which
the audit's sandbox hides, and saddle's `sandbox-expose` named only `node` and
`google-chrome`. The stage could never start on the base or the head, so every
packet of a run on saddle's own tree read "uv lock: not proven, uv could not be
launched here", and a change that broke the lock file could not be caught.

The stages run the way the audit runs them: `run_static_check` in the gate's
sandbox (no network), the npx stages as `node <script>` (`auditor.node_stage`),
judged by the gate's own classifier (`gates._state`).

Known-good: with saddle's `sandbox-expose`, every stage's tool starts, and
`uv lock --check` passes on saddle's own lock file. Known-bad: with nothing
exposed, the uv stage reads as missing, never as passing.
"""

from __future__ import annotations

import shutil
import tomllib
from pathlib import Path
from typing import Any

from test_sandbox_reach import needs_bwrap
from test_shell_scripts import ROOT, needs_checkout

from saddle import sandbox
from saddle.auditor import node_stage
from saddle.evidence import run_static_check
from saddle.gates import _state

UV_LOCK = ("uv", "lock", "--check")


def saddle_table() -> dict[str, Any]:
    table: dict[str, Any] = tomllib.loads((ROOT / "pyproject.toml").read_text())["tool"]["saddle"]
    return table


@needs_bwrap
@needs_checkout
def test_every_gate_stage_tool_starts_in_the_sandbox_with_saddles_expose_list(
    tmp_path: Path,
) -> None:
    table = saddle_table()
    started: dict[str, str] = {}
    with sandbox.also_exposing(table["sandbox-expose"]):
        for stage in table["gate-checks"]:
            probe = (node_stage(stage, ROOT)[0], "--version")
            run = run_static_check(probe, tmp_path, timeout=60)
            started[" ".join(stage[:3])] = _state(probe, run)
    assert set(started.values()) == {"ok"}, started
    assert "uv lock --check" in started  # the stage this was about is in the list


@needs_bwrap
@needs_checkout
def test_the_uv_lock_stage_passes_with_uv_shown_and_is_missing_without(tmp_path: Path) -> None:
    for name in ("pyproject.toml", "uv.lock"):
        shutil.copy(ROOT / name, tmp_path / name)
    with sandbox.also_exposing(saddle_table()["sandbox-expose"]):
        shown = run_static_check(UV_LOCK, tmp_path, timeout=60)
    assert _state(UV_LOCK, shown) == "ok", shown.stdout + shown.stderr
    with sandbox.also_exposing(()):
        hidden = run_static_check(UV_LOCK, tmp_path, timeout=60)
    assert _state(UV_LOCK, hidden) == "missing", hidden.stdout + hidden.stderr
