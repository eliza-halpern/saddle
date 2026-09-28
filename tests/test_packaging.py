"""What a user installs: the sdist and the wheel, built from this tree.

The rest of the suite runs inside the development environment, where every
tool is on PATH and every file is on disk, so it cannot see a release that
leaves a file or a dependency behind. These tests build the distributions
and look at them from the outside.
"""

from __future__ import annotations

import shutil
import subprocess
import tarfile
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]

pytestmark = pytest.mark.packaging


def _uv() -> str:
    found = shutil.which("uv")
    if found is None:
        pytest.skip("uv is not on PATH; the packaging tests build with it")
    return found


def _tracked() -> set[str]:
    if not (REPO / ".git").exists():
        pytest.skip("not a git checkout: nothing to compare the sdist with")
    listed = subprocess.run(
        ["git", "ls-files", "-z"], cwd=REPO, capture_output=True, text=True, check=True
    )
    return {name for name in listed.stdout.split("\0") if name}


def test_the_sdist_carries_every_tracked_file(tmp_path: Path) -> None:
    """Contract: the sdist holds every file git tracks. A tracked fixture a
    test reads, dropped by an ignore pattern, fails that test from the sdist."""
    tracked = _tracked()
    subprocess.run(
        [_uv(), "build", "--sdist", "--offline", "--out-dir", str(tmp_path), str(REPO)],
        capture_output=True,
        check=True,
    )
    (sdist,) = tmp_path.glob("*.tar.gz")
    with tarfile.open(sdist) as archive:
        members = {m.name.partition("/")[2] for m in archive.getmembers() if m.isfile()}
    assert sorted(tracked - members) == []
