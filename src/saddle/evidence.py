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
from time import perf_counter

import coverage

from saddle.journal import SpanRecorder

_HUNK = re.compile(r"^@@ -\d+(?:,\d+)? \+(\d+)(?:,(\d+))? @@")


def _record(
    recorder: SpanRecorder | None,
    argv: Sequence[str],
    start: float,
    proc: subprocess.CompletedProcess[str] | subprocess.CompletedProcess[bytes],
) -> None:
    """Journal one completed invocation; no recorder means no span."""
    if recorder is None:
        return
    err = proc.stderr
    detail = err.decode(errors="replace") if isinstance(err, bytes) else err
    recorder.record(
        argv=list(argv),
        duration_ms=int((perf_counter() - start) * 1000),
        exit_code=proc.returncode,
        detail=detail,
    )


def run_argv(argv: Sequence[str], cwd: Path, *, recorder: SpanRecorder | None = None) -> int:
    """Run `argv` in `cwd`; return its exit code, capturing output."""
    start = perf_counter()
    proc = subprocess.run(argv, cwd=cwd, capture_output=True)
    _record(recorder, argv, start, proc)
    return proc.returncode


def run_stdin(
    argv: Sequence[str], cwd: Path, text: str, *, recorder: SpanRecorder | None = None
) -> int:
    """Run `argv` with `text` on stdin; return its exit code."""
    start = perf_counter()
    proc = subprocess.run(argv, input=text, cwd=cwd, capture_output=True, text=True)
    _record(recorder, argv, start, proc)
    return proc.returncode


def run_shell(command: str, cwd: Path, *, recorder: SpanRecorder | None = None) -> int:
    """Run a `test_command` string via shlex splitting (never a shell)."""
    return run_argv(shlex.split(command), cwd, recorder=recorder)


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


def git_diff(cwd: Path, ref: str, *, recorder: SpanRecorder | None = None) -> str:
    """Zero-context diff of the worktree at `cwd` against git `ref`."""
    argv = ["git", "-C", str(cwd), "diff", "-U0", ref, "--", "."]
    start = perf_counter()
    proc = subprocess.run(
        argv,
        capture_output=True,
        text=True,
    )
    _record(recorder, argv, start, proc)
    if proc.returncode != 0:
        msg = f"git diff against {ref!r} failed: {proc.stderr.strip()}"
        raise RuntimeError(msg)
    return proc.stdout


def materialize_baseline(
    cwd: Path, ref: str, dest: Path, *, recorder: SpanRecorder | None = None
) -> None:
    """Extract tracked files at git `ref` into `dest` for the red-phase run."""
    argv = ["git", "-C", str(cwd), "archive", ref]
    start = perf_counter()
    proc = subprocess.run(
        argv,
        capture_output=True,
    )
    _record(recorder, argv, start, proc)
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
