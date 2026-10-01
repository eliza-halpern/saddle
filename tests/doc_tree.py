"""The repository's tracked files, for the tests that read docs and metadata.

These tests read files outside `src/` and `tests/` (README.md, pyproject.toml,
the images under docs/). The mutation work copy holds only `src`, `tests` and
the `also_copy` directories, so there they cannot run, and a test that fails
for lack of its inputs would kill every mutant for the wrong reason. A work
copy is recognised by its missing `pyproject.toml`, which `[tool.mutmut]`
documents as never copied; in a real checkout a missing input is a failure,
never a skip.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


def require_checkout() -> None:
    """Skip in the mutation work copy; fail everywhere else if git is unusable."""
    if not (ROOT / "pyproject.toml").exists():
        pytest.skip("mutation work copy: the top-level files these tests read are not copied")
    if not (ROOT / ".git").exists():
        pytest.skip("not a git checkout: there is no tracked-file list to check")


def git_ls_files(root: Path, *args: str) -> list[str]:
    """`git ls-files -z <args>` in `root`; a git failure raises, never reads as empty."""
    proc = subprocess.run(
        ["git", "ls-files", "-z", *args],
        cwd=root,
        capture_output=True,
        check=False,
    )
    if proc.returncode != 0:
        message = f"git ls-files {' '.join(args)} failed: {proc.stderr.decode()}"
        raise RuntimeError(message)
    return [name for name in proc.stdout.decode().split("\0") if name]


def tracked(*patterns: str) -> list[str]:
    """Tracked paths of the real repository matching the pathspecs, sorted."""
    require_checkout()
    return sorted(git_ls_files(ROOT, *patterns))
