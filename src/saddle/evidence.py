"""Evidence collectors for Tier-1 gates (ARCHITECTURE.md §3 Phase 3 Tier 1).

Thin subprocess wrappers and pure parsers that turn a worktree into the
`Tier1Inputs` evidence bundle. Every collector is straight-line where
possible so fixtures stay fast and branch coverage stays cheap.
"""

from __future__ import annotations

import ast
import json
import os
import re
import shlex
import shutil
import subprocess
import tarfile
import tempfile
from collections.abc import Collection, Sequence
from dataclasses import dataclass
from io import BytesIO
from pathlib import Path
from time import perf_counter

import coverage

from saddle.journal import SpanRecorder

_HUNK = re.compile(r"^@@ -\d+(?:,\d+)? \+(\d+)(?:,(\d+))? @@")
_MUTANT_VERDICT = re.compile(r"^\s*(\S+): (killed|survived|timeout|not checked)\s*$")
_MUTATION_TIMEOUT_S = 600


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


@dataclass(frozen=True)
class CapturedRun:
    """One completed invocation with its output, for recovery prompts."""

    argv: tuple[str, ...]
    exit_code: int
    stdout: str
    stderr: str


def run_capture(
    argv: Sequence[str], cwd: Path, *, recorder: SpanRecorder | None = None
) -> CapturedRun:
    """Run `argv` in `cwd`; journal its span and return exit plus output."""
    start = perf_counter()
    proc = subprocess.run(argv, cwd=cwd, capture_output=True, text=True)
    _record(recorder, argv, start, proc)
    return CapturedRun(
        argv=tuple(argv), exit_code=proc.returncode, stdout=proc.stdout, stderr=proc.stderr
    )


def run_shell_capture(
    command: str, cwd: Path, *, recorder: SpanRecorder | None = None
) -> CapturedRun:
    """Run a `test_command` string via shlex splitting, capturing output."""
    return run_capture(shlex.split(command), cwd, recorder=recorder)


def drop_test_caches(root: Path) -> None:
    """Remove Python and pytest caches so gates evaluate current sources.

    Fix-forward retries rewrite test files seconds apart; an unchanged
    size within the same mtime second would otherwise validate a stale
    .pyc and run yesterday's tests. Targets materialize before removal
    so the tree never mutates mid-walk.
    """
    for cache in list(root.rglob("__pycache__")):
        shutil.rmtree(cache, ignore_errors=True)
    for cache in list(root.rglob(".pytest_cache")):
        shutil.rmtree(cache, ignore_errors=True)
    for stale in list(root.rglob("*.pyc")):
        # TOCTOU-only: rglob yields existing paths, so the flag never fires in tests.
        stale.unlink(missing_ok=True)  # pragma: no mutate


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


def git_ls_files(cwd: Path) -> list[str]:
    """Tracked worktree-relative paths at `cwd` for worker prompts."""
    proc = subprocess.run(
        ["git", "-C", str(cwd), "ls-files"],
        capture_output=True,
        text=True,
    )
    if proc.returncode != 0:
        msg = f"git ls-files failed: {proc.stderr.strip()}"
        raise RuntimeError(msg)
    return proc.stdout.splitlines()


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
    """Extract tracked files at git `ref` into `dest` (empty when the tree is empty)."""
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
    try:
        with tarfile.open(fileobj=BytesIO(proc.stdout)) as tar:
            tar.extractall(dest, filter="data")
    except tarfile.ReadError:
        # `git archive` of an empty tree is not a readable tar: nothing to extract.
        return


def under_coverage(test_command: str, data_file: str) -> str:
    """Wrap a pytest scope command so the run records into `data_file`."""
    return test_command.replace("pytest", f"coverage run --data-file={data_file} -m pytest", 1)


@dataclass(frozen=True)
class MutationOutcome:
    """Sampled kill-rate evidence over changed-line mutants."""

    killed: int
    total: int
    generated: int
    survivors: tuple[str, ...]


def _parse_mutant_verdicts(text: str) -> dict[str, str]:
    """Mutant name to verdict from `mutmut results --all True` output."""
    verdicts = {}
    for line in text.splitlines():
        match = _MUTANT_VERDICT.match(line)
        if match:
            verdicts[match.group(1)] = match.group(2)
    return verdicts


def _mutant_path(show_output: str) -> str | None:
    """Repo-relative path from `mutmut show`, else None."""
    for line in show_output.splitlines():
        if line.startswith("+++ "):
            return line[4:].strip().removeprefix("b/")
    return None


def _mutant_lines(show_output: str, source: str) -> set[int]:
    """File line numbers matching the removed (`-`) hunk lines.

    Hunk headers are function-relative, so the `-` excerpts themselves
    locate the mutant: exact matches against the scratch file.
    """
    removed = [
        line[1:]
        for line in show_output.splitlines()
        if line.startswith("-") and not line.startswith("--- ")
    ]
    if not removed:
        return set()
    numbered = list(enumerate(source.splitlines(), start=1))
    return {lineno for lineno, text in numbered for snippet in removed if text == snippet}


def _mutmut_scratch_config(sources: list[str]) -> str:
    """Minimal mutmut config: per-file sources (a `.` root nests mutants/)."""
    quoted = ", ".join(json.dumps(source) for source in sources)
    return (
        "[tool.mutmut]\n"
        f"source_paths = [{quoted}]\n"
        'pytest_add_cli_args = ["-q", "-x", "-p", "no:cacheprovider"]\n'
    )


def mutation_sample(
    workdir: Path,
    changed: Collection[tuple[str, int]],
    max_mutants: int,
    *,
    test_files: Collection[str],
    timeout_s: int = _MUTATION_TIMEOUT_S,
    recorder: SpanRecorder | None = None,
) -> MutationOutcome:
    """Kill-rate over changed-line mutants, sampled to `max_mutants` by name.

    Runs in a scratch copy (mutmut writes mutants/ into cwd) under a time
    budget; the verdict covers the deterministic name-sorted sample and
    degrades to decided mutants when the budget binds first. Test files
    are excluded from scope (mutating tests pollutes the rate), timeouts
    count as killed (behavior changed), and missing mutmut fails closed.
    """
    if not changed:
        return MutationOutcome(killed=0, total=0, generated=0, survivors=())
    if shutil.which("mutmut") is None:
        return MutationOutcome(killed=0, total=0, generated=0, survivors=("mutmut not on PATH",))
    root = os.path.realpath(workdir)
    by_line: dict[str, set[int]] = {}
    for path, line in changed:
        key = os.path.relpath(os.path.realpath(path), root)
        by_line.setdefault(key, set()).add(line)
    tests = set(test_files)
    with tempfile.TemporaryDirectory(prefix="saddle-mutation-") as tmp:
        scratch = Path(tmp)
        for source in sorted(workdir.rglob("*.py")):
            if "__pycache__" in source.parts:
                continue
            dest = scratch / source.relative_to(workdir)
            dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, dest)
        production = sorted(
            path.relative_to(scratch).as_posix()
            for path in scratch.rglob("*.py")
            if path.relative_to(scratch).as_posix() not in tests
        )
        if not production:
            return MutationOutcome(killed=0, total=0, generated=0, survivors=())
        (scratch / "pyproject.toml").write_text(_mutmut_scratch_config(production))
        run_capture(["timeout", str(timeout_s), "mutmut", "run"], scratch, recorder=recorder)
        results = run_capture(["mutmut", "results", "--all", "True"], scratch, recorder=recorder)
        verdicts = _parse_mutant_verdicts(results.stdout)
        scoped: list[tuple[str, str]] = []
        undecided = 0
        for name in sorted(verdicts):
            verdict = verdicts[name]
            if verdict == "not checked":
                undecided += 1
                continue
            shown = run_capture(["mutmut", "show", name], scratch, recorder=recorder)
            rel = _mutant_path(shown.stdout)
            if rel is None:
                continue
            posix_rel = rel.replace(os.sep, "/")
            if posix_rel in tests:
                continue
            key = os.path.relpath(os.path.realpath(scratch / rel), os.path.realpath(scratch))
            lines = by_line.get(key)
            if lines is None:
                continue
            target = scratch / rel
            if not target.is_file():
                continue
            if not _mutant_lines(shown.stdout, target.read_text()) & lines:
                continue
            scoped.append((name, verdict))
    sample = scoped[:max_mutants]
    killed = sum(1 for _, verdict in sample if verdict in ("killed", "timeout"))
    survivors = tuple(name for name, verdict in sample if verdict == "survived")
    return MutationOutcome(
        killed=killed, total=len(sample), generated=len(scoped) + undecided, survivors=survivors
    )


def covered_lines(data_file: str, files: Collection[str]) -> set[tuple[str, int]]:
    """Executed lines per file from a coverage data file (empty when unreadable).

    Data keys are absolute while callers may pass workdir-relative paths, so
    the join normalizes both sides; emitted tuples keep the caller's spelling.
    """
    cov = coverage.Coverage(data_file=data_file, config_file=False)
    try:
        cov.load()
    except coverage.CoverageException:
        return set()
    data = cov.get_data()
    by_realpath = {os.path.realpath(measured): measured for measured in data.measured_files()}
    covered: set[tuple[str, int]] = set()
    for filename in files:
        measured = by_realpath.get(os.path.realpath(filename))
        if measured is None:
            continue
        for number in data.lines(measured) or ():
            covered.add((filename, number))
    return covered


def _docstring_lines(tree: ast.Module) -> set[int]:
    """First lines of docstrings: leading strings of modules, classes, functions."""
    found = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
            first = node.body[0] if node.body else None
            if (
                isinstance(first, ast.Expr)
                and isinstance(first.value, ast.Constant)
                and isinstance(first.value.value, str)
            ):
                found.add(first.lineno)
    return found


def statement_lines(source: str) -> set[int]:
    """Executable first-lines of `source`; unparseable source yields none.

    Conservative approximation of what coverage can execute (blanks,
    comments, and docstrings are never statements; `case` lines carry
    no position of their own and stay exempt). A syntax error means the
    syntax gate fails the node anyway, so there is nothing coverable to
    require here.
    """
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return set()
    lines = {
        node.lineno for node in ast.walk(tree) if isinstance(node, (ast.stmt, ast.excepthandler))
    }
    return lines - _docstring_lines(tree)
