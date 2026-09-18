"""Mutant work-copy layout must support test collection.

mutmut runs pytest from a `mutants/` copy containing only source_paths,
also_copy, and tests. Any test-module import outside that set (e.g. the
benchmark harness) breaks collection for every mutant run. This test
rebuilds that layout from pyproject and proves collection succeeds.
"""

from __future__ import annotations

import shutil
import subprocess
import sys
import tomllib
from pathlib import Path


def test_mutant_layout_collects(tmp_path: Path) -> None:
    root = Path(__file__).resolve().parents[1]
    with open(root / "pyproject.toml", "rb") as fh:
        mutmut_cfg = tomllib.load(fh)["tool"]["mutmut"]
    layout = tmp_path / "mutants"
    copied = [*mutmut_cfg["source_paths"], *mutmut_cfg.get("also_copy", []), "tests"]
    for rel in copied:
        dest = layout / rel
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copytree(root / rel, dest, ignore=shutil.ignore_patterns("__pycache__"))
    proc = subprocess.run(
        [
            sys.executable,
            "-m",
            "pytest",
            "--collect-only",
            "-p",
            "no:cacheprovider",
            *mutmut_cfg.get("pytest_add_cli_args", []),
        ],
        cwd=layout,
        capture_output=True,
        text=True,
    )
    assert proc.returncode == 0, (proc.stdout + proc.stderr)[-2000:]
