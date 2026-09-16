"""Evidence collectors for Tier-1 gates (ARCHITECTURE.md §3 Phase 3 Tier 1).

Thin subprocess wrappers and pure parsers that turn a worktree into the
`Tier1Inputs` evidence bundle. Every collector is straight-line where
possible so fixtures stay fast and branch coverage stays cheap.
"""

from __future__ import annotations

import ast
import re
import shlex
import subprocess
import tarfile
from collections.abc import Collection, Sequence
from io import BytesIO
from pathlib import Path

import coverage

_HUNK = re.compile(r"^@@ -\d+(?:,\d+)? \+(\d+)(?:,(\d+))? @@")


def run_argv(argv: Sequence[str], cwd: Path) -> int:
    """Run `argv` in `cwd`; return its exit code, capturing output."""
    proc = subprocess.run(argv, cwd=cwd, capture_output=True)
    return proc.returncode


def run_shell(command: str, cwd: Path) -> int:
    """Run a `test_command` string via shlex splitting (never a shell)."""
    return run_argv(shlex.split(command), cwd)


def changed_lines(diff: str) -> set[tuple[str, int]]:
    """Added-line identities from `git diff -U0` text; deletions add none."""
    changed: set[tuple[str, int]] = set()
    path: str | None = None
    for line in diff.splitlines():
        if line.startswith("+++ "):
            target = line.removeprefix("+++ ").strip()
            path = None if target == "/dev/null" else target.removeprefix("b/")
        elif path is not None and (match := _HUNK.match(line)):
            start = int(match.group(1))
            count = int(match.group(2)) if match.group(2) is not None else 1
            changed.update((path, number) for number in range(start, start + count))
    return changed


def git_diff(cwd: Path, ref: str) -> str:
    """Zero-context diff of the worktree at `cwd` against git `ref`."""
    proc = subprocess.run(
        ["git", "-C", str(cwd), "diff", "-U0", ref, "--", "."],
        capture_output=True,
        text=True,
    )
    if proc.returncode != 0:
        msg = f"git diff against {ref!r} failed: {proc.stderr.strip()}"
        raise RuntimeError(msg)
    return proc.stdout


def materialize_baseline(cwd: Path, ref: str, dest: Path) -> None:
    """Extract tracked files at git `ref` into `dest` for the red-phase run."""
    proc = subprocess.run(
        ["git", "-C", str(cwd), "archive", ref],
        capture_output=True,
    )
    if proc.returncode != 0:
        msg = f"git archive of {ref!r} failed: {proc.stderr.decode().strip()}"
        raise RuntimeError(msg)
    dest.mkdir(parents=True, exist_ok=True)
    with tarfile.open(fileobj=BytesIO(proc.stdout)) as tar:
        tar.extractall(dest, filter="data")


def under_coverage(test_command: str, data_file: str) -> str:
    """Wrap a pytest scope command so the run records into `data_file`."""
    return test_command.replace("pytest", f"coverage run --data-file={data_file} -m pytest", 1)


def covered_lines(data_file: str, files: Collection[str]) -> set[tuple[str, int]]:
    """Executed lines per file from a coverage data file (empty when unreadable)."""
    cov = coverage.Coverage(data_file=data_file, config_file=False)
    try:
        cov.load()
    except coverage.CoverageException:
        return set()
    data = cov.get_data()
    covered: set[tuple[str, int]] = set()
    for filename in files:
        for number in data.lines(filename) or ():
            covered.add((filename, number))
    return covered


def statement_lines(source: str) -> set[int]:
    """Executable first-lines of `source`; unparseable source yields none.

    Conservative approximation of what coverage can execute (blanks and
    comments are never statements; `case` lines carry no position of
    their own and stay exempt). A syntax error means the syntax gate
    fails the node anyway, so there is nothing coverable to require here.
    """
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return set()
    return {
        node.lineno for node in ast.walk(tree) if isinstance(node, (ast.stmt, ast.excepthandler))
    }
