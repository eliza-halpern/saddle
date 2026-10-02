"""The audit's staged copy sees the checkout's dependency directories, as its gate does.

Contract: a suite that reads the project's git-ignored `node_modules` passes in
the audit when it passes in the checkout; the directory is linked into the
staged copy once and shown read-only to every confined run, and a copy that
already holds one is left alone. Why: the staged copy leaves out what git
ignores, so 80 of saddle's own tests failed in its audit and passed in
check.sh, and the audit refused every change to the repository.

Known-good: a test that reads `node_modules/pkg/data.txt` passes in a tier-1
audit; a run inside the copy cannot write there. Known-bad: without the link
the same audit fails `tests` naming that test (the mutant below is the red
half); a checkout with no `node_modules` links nothing.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from test_auditor import _init

from saddle import sandbox
from saddle.auditor import Auditor, AuditorConfig, link_dependencies

needs_sandbox = pytest.mark.skipif(
    sandbox.isolation_problem() is not None, reason="needs a bwrap that can start"
)

READS_IT = (
    "from pathlib import Path\n\n\n"
    "def test_the_package_is_there() -> None:\n"
    '    assert Path("node_modules/pkg/data.txt").read_text() == "ok"\n\n\n'
    "def test_it_cannot_be_written() -> None:\n"
    "    try:\n"
    '        Path("node_modules/pkg/new.txt").write_text("x")\n'
    "    except OSError:\n"
    "        return\n"
    '    raise AssertionError("node_modules was writable from the audit")\n'
)


def tree(tmp_path: Path) -> Path:
    root = tmp_path / "tree"
    _init(
        root,
        {
            ".gitignore": "node_modules/\n",
            "n.py": "def f():\n    return 1\n",
            "test_n.py": "from n import f\n\n\ndef test_f():\n    assert f() == 1\n",
            "test_deps.py": READS_IT,
        },
    )
    (root / "node_modules/pkg").mkdir(parents=True)
    (root / "node_modules/pkg/data.txt").write_text("ok")
    (root / "n.py").write_text("def f():\n    return 1  # changed\n")
    return root


@needs_sandbox
def test_a_suite_that_reads_node_modules_passes_in_the_audit(tmp_path: Path) -> None:
    root = tree(tmp_path)
    found = {f.gate: f for f in Auditor(root, "HEAD").tier1().findings}
    assert found["tests"].verdict == "pass", found["tests"].detail
    assert not (root / "node_modules/pkg/new.txt").exists()


def test_link_dependencies_links_what_the_copy_lacks_and_nothing_else(tmp_path: Path) -> None:
    checkout, copy = tmp_path / "checkout", tmp_path / "copy"
    (checkout / "node_modules").mkdir(parents=True)
    copy.mkdir()
    assert link_dependencies(copy, checkout) == ((checkout / "node_modules").resolve(),)
    assert (copy / "node_modules").resolve() == (checkout / "node_modules").resolve()
    # Asked again (the stage runner's own check), nothing more is linked.
    assert link_dependencies(copy, checkout) == ()
    held = tmp_path / "held"
    (held / "node_modules").mkdir(parents=True)
    assert link_dependencies(held, checkout) == ()
    assert not (held / "node_modules").is_symlink()
    assert link_dependencies(tmp_path / "bare", tmp_path / "none") == ()


@needs_sandbox
def test_a_run_audits_its_worktree_with_the_checkouts_packages(tmp_path: Path) -> None:
    """An autonomous run audits its worktree, whose `node_modules` is only an empty
    mount point (`Sandbox.mounted`). Its finish audit linked that empty directory,
    found no c8, no typescript and no npx tools, and refused a correct change on
    17 tests that needed them; `dependencies` names where the packages are."""
    checkout = tmp_path / "checkout"
    (checkout / "node_modules" / "pkg").mkdir(parents=True)
    (checkout / "node_modules" / "pkg" / "data.txt").write_text("ok")
    worktree = tree(tmp_path / "worktree")
    for leftover in (worktree / "node_modules" / "pkg").iterdir():
        leftover.unlink()
    (worktree / "node_modules" / "pkg").rmdir()
    assert list((worktree / "node_modules").iterdir()) == []  # the mount point
    given = Auditor(worktree, "HEAD", AuditorConfig(dependencies=checkout)).tier1()
    assert {f.gate: f for f in given.findings}["tests"].verdict == "pass"
    bare = Auditor(worktree, "HEAD").tier1()
    assert {f.gate: f for f in bare.findings}["tests"].verdict == "fail"


def test_mutmut_finds_the_packages_where_it_runs_the_tests(tmp_path: Path) -> None:
    """mutmut runs the tests in `mutants/`, holding only its sources and `also_copy`:
    a test that read node_modules failed its stats run ("failed to collect stats")."""
    from saddle.evidence import _link_into_mutants

    workdir, scratch = tmp_path / "work", tmp_path / "scratch"
    (workdir / "node_modules" / "pkg").mkdir(parents=True)
    scratch.mkdir()
    _link_into_mutants(workdir, scratch)
    assert (scratch / "mutants" / "node_modules" / "pkg").is_dir()
    assert (scratch / "mutants" / "node_modules").resolve() == (workdir / "node_modules").resolve()
    _link_into_mutants(workdir, scratch)  # again: the link stands, nothing raises
    bare = tmp_path / "bare"
    bare.mkdir()
    _link_into_mutants(bare, tmp_path / "scratch2")
    assert not (tmp_path / "scratch2" / "mutants" / "node_modules").exists()
