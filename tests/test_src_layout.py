"""A gate's tests import the audited tree's package, never another copy.

The installed copy here is real: this suite's venv holds saddle as an
editable install of the checkout the suite runs from. The audited tree is a
`src`-layout project whose package is also named `saddle` and differs from
the installed one by a planted marker, so a test can tell the two apart.
Before the fix a confined run could not import the package at all (the
sandbox hides the checkout the editable install points at), and an
unconfined one imported the checkout instead of the tree.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

import saddle
from saddle.evidence import run_shell_capture, src_layout_env

MARKER_TEST = (
    "import saddle\n\n\ndef test_the_tree_is_what_runs():\n"
    "    assert getattr(saddle, 'MARKER', None) == 'audited'\n"
)


def _src_tree(root: Path, marker: str) -> Path:
    package = root / "src" / "saddle"
    package.mkdir(parents=True)
    (package / "__init__.py").write_text(f"MARKER = {marker!r}\n")
    (root / "test_marker.py").write_text(MARKER_TEST)
    return root


def test_the_installed_copy_is_a_different_one() -> None:
    """The premise: the installed saddle has no marker."""
    assert not hasattr(saddle, "MARKER")


def test_a_src_layout_trees_tests_import_the_tree(tmp_path: Path) -> None:
    """Known-good: the audited copy, with the marker the test expects, passes."""
    tree = _src_tree(tmp_path / "tree", "audited")
    run = run_shell_capture("python -m pytest -q -p no:cacheprovider", tree)
    assert run.exit_code == 0, run.stdout + run.stderr


def test_a_planted_difference_in_the_tree_is_seen(tmp_path: Path) -> None:
    """Known-bad: the audited copy's marker is wrong; the installed copy
    (no marker) would fail too, so the failure must name the tree's value."""
    tree = _src_tree(tmp_path / "tree", "planted")
    run = run_shell_capture("python -m pytest -q -p no:cacheprovider", tree)
    assert run.exit_code == 1
    assert "'planted' == 'audited'" in run.stdout


def test_src_goes_first_and_keeps_an_inherited_pythonpath(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (tmp_path / "src").mkdir()
    monkeypatch.setenv("PYTHONPATH", "/inherited")
    assert src_layout_env(tmp_path) == {
        "PYTHONPATH": os.pathsep.join([str((tmp_path / "src").resolve()), "/inherited"])
    }
    monkeypatch.delenv("PYTHONPATH")
    assert src_layout_env(tmp_path) == {"PYTHONPATH": str((tmp_path / "src").resolve())}


def test_a_tree_without_src_gets_no_pythonpath(tmp_path: Path) -> None:
    assert src_layout_env(tmp_path) == {}
