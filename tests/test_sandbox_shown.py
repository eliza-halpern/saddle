"""`shown`: directories a confined run may read and not write.

Contract: a directory passed as `shown` to `sandbox.confine` (and so to
`evidence.run_capture`) can be read from inside the confined run, cannot be
written, and a directory not passed is not there. Why: StrykerJS lives in the
project's `node_modules`, which the staged copy leaves out, and the confined
run on the copy could not otherwise see it.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from saddle import sandbox
from saddle.evidence import run_capture, tree_memory_limit

needs_sandbox = pytest.mark.skipif(
    sandbox.isolation_problem() is not None, reason="needs a bwrap that can start"
)


def _tools(tmp_path: Path) -> tuple[Path, Path]:
    tools = tmp_path / "tools"
    tools.mkdir()
    (tools / "note.txt").write_text("from the tools")
    work = tmp_path / "work"
    work.mkdir()
    return tools, work


@needs_sandbox
def test_a_shown_directory_is_readable_and_not_writable(tmp_path: Path) -> None:
    tools, work = _tools(tmp_path)
    script = f"cat {tools}/note.txt; touch {tools}/x 2>&1; test ! -e {tools}/x"
    argv, env = sandbox.confine(["sh", "-c", script], work, shown=[tools])
    ran = subprocess.run(argv, env=env, capture_output=True, text=True, check=False)
    assert ran.returncode == 0, ran.stdout + ran.stderr
    assert "from the tools" in ran.stdout
    assert "Read-only file system" in ran.stdout


@needs_sandbox
def test_a_directory_not_shown_is_not_there(tmp_path: Path) -> None:
    tools, work = _tools(tmp_path)
    argv, env = sandbox.confine(["sh", "-c", f"test ! -e {tools}/note.txt"], work)
    assert subprocess.run(argv, env=env, check=False).returncode == 0
    argv, env = sandbox.confine(["sh", "-c", f"test -e {tools}/note.txt"], work, shown=[tools])
    assert subprocess.run(argv, env=env, check=False).returncode == 0


@needs_sandbox
def test_run_capture_passes_shown_to_the_confined_run(tmp_path: Path) -> None:
    tools, work = _tools(tmp_path)
    shut = run_capture(
        ["cat", str(tools / "note.txt")], work, memory_limit=tree_memory_limit(), shown=[tools]
    )
    assert (shut.exit_code, shut.stdout) == (0, "from the tools")
    hidden = run_capture(["cat", str(tools / "note.txt")], work, memory_limit=tree_memory_limit())
    assert hidden.exit_code != 0
