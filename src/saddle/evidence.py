"""Evidence collectors for Tier-1 gates (ARCHITECTURE.md §3 Phase 3 Tier 1).

Thin subprocess wrappers and pure parsers that turn a worktree into the
`Tier1Inputs` evidence bundle. Every collector is straight-line where
possible so fixtures stay fast and branch coverage stays cheap.
"""

from __future__ import annotations

import ast
import hashlib
import io
import json
import os
import re
import shlex
import shutil
import subprocess
import sys
import tarfile
import tempfile
import tokenize
from collections.abc import Collection, Mapping, Sequence
from dataclasses import dataclass
from io import BytesIO
from pathlib import Path, PurePath
from time import perf_counter
from typing import Any, Final

import coverage

from saddle.gates import SHELL_TIMEOUT, TOOL_UNAVAILABLE, RuffFinding
from saddle.journal import SpanRecorder

_HUNK = re.compile(r"^@@ -\d+(?:,\d+)? \+(\d+)(?:,(\d+))? @@")
_MUTANT_VERDICT = re.compile(r"^\s*(\S+): (killed|survived|timeout|not checked)\s*$")
_MUTANT_NAME = re.compile(
    r"^(?:\w+\.)+x(?:_(?P<func>.+?)|ǁ(?P<cls>\w+)ǁ(?P<method>.+?))__mutmut_\d+$"
)
"""mutmut's two mangled-name shapes (P0-2), parsed locally so saddle never
imports mutmut into its own process: `<dotted.module>.x_<func>__mutmut_<n>`
for a function, `<dotted.module>.xǁ<Class>ǁ<method>__mutmut_<n>` for a
method (ǁ is U+01C1). Checked against mutmut's own
`orig_function_and_class_names_from_key` as the test oracle
(test_mutant_name_matches_mutmut_names_from_key), not used at runtime. A
name that does not match this shape is a hand-built test stub and takes
`_mutant_lines`'s old whole-file fallback."""
_MUTATION_TIMEOUT_S = 600

DEFAULT_TEST_TIMEOUT_S: Final = 300.0
"""Wall-clock ceiling for a declared test command. Suites in scope run
in seconds; this bounds non-termination without failing slow-but-sound
runs."""


def _record(
    recorder: SpanRecorder | None,
    argv: Sequence[str],
    start: float,
    proc: subprocess.CompletedProcess[str] | subprocess.CompletedProcess[bytes],
    *,
    name: str | None = None,
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
        name=name,
    )


def _partial(stream: str | bytes | None) -> str:
    """Decode whatever a killed process managed to emit before dying."""
    if stream is None:
        return ""
    return stream.decode(errors="replace") if isinstance(stream, bytes) else stream


def _record_timeout(
    recorder: SpanRecorder | None,
    argv: Sequence[str],
    start: float,
    expired: subprocess.TimeoutExpired,
) -> None:
    """Journal a killed invocation; the span must not silently vanish."""
    if recorder is None:
        return
    recorder.record(
        argv=list(argv),
        duration_ms=int((perf_counter() - start) * 1000),
        exit_code=SHELL_TIMEOUT,
        detail=f"timed out after {expired.timeout}s: {_partial(expired.stderr)}",
    )


def _record_unavailable(
    recorder: SpanRecorder | None, argv: Sequence[str], start: float, exc: OSError
) -> None:
    """Journal a tool that never launched; the reason must reach the run."""
    if recorder is None:
        return
    recorder.record(
        argv=list(argv),
        duration_ms=int((perf_counter() - start) * 1000),
        exit_code=TOOL_UNAVAILABLE,
        detail=str(exc),
    )


def run_argv(
    argv: Sequence[str],
    cwd: Path,
    *,
    recorder: SpanRecorder | None = None,
    timeout: float | None = None,
) -> int:
    """Run `argv` in `cwd`; return its exit code, capturing output.

    A `timeout` bounds the run: exceeding it yields `SHELL_TIMEOUT`
    rather than blocking the gate runner forever.
    """
    start = perf_counter()
    try:
        proc = subprocess.run(argv, cwd=cwd, capture_output=True, timeout=timeout)
    except subprocess.TimeoutExpired as expired:
        _record_timeout(recorder, argv, start, expired)
        return SHELL_TIMEOUT
    except OSError as exc:
        _record_unavailable(recorder, argv, start, exc)
        return TOOL_UNAVAILABLE
    _record(recorder, argv, start, proc)
    return proc.returncode


# The lint rules the ruff gate enforces (T6-37): defects, not style. Round
# 3e (F21.16) was gated by ruff 0.16.7's *default* rule set -- no config
# existed in the workdir or above it -- and UP031 failed a node for the
# `%`-formatting its own baseline uses in 4 of 5 modules; a `pip install
# -U ruff` would have changed the verdict. Every ruff call runs
# `--isolated`, so neither the graded repo's config nor the machine's
# reaches the gate, and `ruff check` selects exactly these: pyflakes (F),
# the E4/E7/E9 subset ruff itself ships by default, and bugbear (B).
RUFF_RULES: Final = ("F", "E4", "E7", "E9", "B")


def ruff_argv(command: str, *args: str) -> list[str]:
    """`ruff <command>` as the gate runs it: isolated, and for `check`, saddle's rules."""
    argv = ["ruff", command, "--isolated"]
    if command == "check":
        argv.extend(["--select", ",".join(RUFF_RULES)])
    argv.extend(args)
    return argv


def ruff_version() -> str:
    """The installed ruff's version (`ruff --version`), or "unavailable"."""
    try:
        proc = subprocess.run(["ruff", "--version"], capture_output=True, text=True, check=False)
    except OSError:
        return "unavailable"
    words = proc.stdout.split()
    return words[1] if proc.returncode == 0 and len(words) >= 2 else "unavailable"


def ruff_findings(
    workdir: Path, files: Sequence[str], *, recorder: SpanRecorder | None = None
) -> tuple[CapturedRun, list[RuffFinding]]:
    """Run `ruff check --output-format json` on `files` in `workdir` (T6-3).

    Returns the run (exit code as ruff gave it; stdout replaced by one
    human line per finding, `path:row:col: CODE message`, so a repair
    brief reads findings rather than JSON) and the parsed findings, each
    carrying the stripped source line it sits on. Unparseable output
    yields no findings and leaves the exit code to say the tool failed.
    """
    argv = ruff_argv("check", "--output-format", "json", *files)
    run = run_capture(argv, workdir, recorder=recorder)
    findings: list[RuffFinding] = []
    try:
        raw = json.loads(run.stdout) if run.stdout.strip() else []
    except ValueError:
        raw = []
    root = os.path.realpath(workdir)
    for item in raw if isinstance(raw, list) else []:
        if not isinstance(item, dict):
            continue
        raw_location = item.get("location")
        location: dict[str, Any] = raw_location if isinstance(raw_location, dict) else {}
        row = int(location.get("row", 0) or 0)
        path = os.path.relpath(os.path.realpath(str(item.get("filename", ""))), root)
        line = _source_line(workdir / path, row)
        findings.append(
            RuffFinding(
                code=str(item.get("code") or "?"),
                path=path,
                row=row,
                message=str(item.get("message") or ""),
                line=line,
                column=int(location.get("column", 0) or 0),
            )
        )
    rendered = "\n".join(f"{f.path}:{f.row}:{f.column}: {f.code} {f.message}" for f in findings)
    shown = CapturedRun(
        argv=("ruff", "check", *files),
        exit_code=run.exit_code,
        stdout=rendered,
        stderr=run.stderr,
        timed_out=run.timed_out,
    )
    return shown, findings


def _source_line(path: Path, row: int) -> str:
    """The stripped text of line `row` (1-based) of `path`, or empty."""
    try:
        lines = path.read_text().splitlines()
    except (OSError, UnicodeDecodeError):
        return ""
    return lines[row - 1].strip() if 0 < row <= len(lines) else ""


def run_stdin(
    argv: Sequence[str], cwd: Path, text: str, *, recorder: SpanRecorder | None = None
) -> int:
    """Run `argv` with `text` on stdin; return its exit code."""
    return run_stdin_capture(argv, cwd, text, recorder=recorder)[0]


def run_stdin_capture(
    argv: Sequence[str], cwd: Path, text: str, *, recorder: SpanRecorder | None = None
) -> tuple[int, str]:
    """Run `argv` with `text` on stdin; return its exit code and stderr (T6-27)."""
    start = perf_counter()
    proc = subprocess.run(argv, input=text, cwd=cwd, capture_output=True, text=True)
    _record(recorder, argv, start, proc)
    return proc.returncode, proc.stderr


def run_shell(
    command: str,
    cwd: Path,
    *,
    recorder: SpanRecorder | None = None,
    timeout: float | None = DEFAULT_TEST_TIMEOUT_S,
) -> int:
    """Run a `test_command` string via shlex splitting (never a shell)."""
    return run_argv(shlex.split(command), cwd, recorder=recorder, timeout=timeout)


@dataclass(frozen=True)
class CapturedRun:
    """One completed invocation with its output, for recovery prompts."""

    argv: tuple[str, ...]
    exit_code: int
    stdout: str
    stderr: str
    timed_out: bool = False


def run_capture(
    argv: Sequence[str],
    cwd: Path,
    *,
    recorder: SpanRecorder | None = None,
    timeout: float | None = None,
) -> CapturedRun:
    """Run `argv` in `cwd`; journal its span and return exit plus output.

    On timeout the partial output is preserved and `timed_out` is set, so
    recovery prompts still see how far the run got before it stalled.
    """
    start = perf_counter()
    try:
        proc = subprocess.run(argv, cwd=cwd, capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired as expired:
        _record_timeout(recorder, argv, start, expired)
        return CapturedRun(
            argv=tuple(argv),
            exit_code=SHELL_TIMEOUT,
            stdout=_partial(expired.stdout),
            stderr=_partial(expired.stderr),
            timed_out=True,
        )
    except OSError as exc:
        _record_unavailable(recorder, argv, start, exc)
        return CapturedRun(argv=tuple(argv), exit_code=TOOL_UNAVAILABLE, stdout="", stderr=str(exc))
    _record(recorder, argv, start, proc)
    return CapturedRun(
        argv=tuple(argv), exit_code=proc.returncode, stdout=proc.stdout, stderr=proc.stderr
    )


def run_shell_capture(
    command: str,
    cwd: Path,
    *,
    recorder: SpanRecorder | None = None,
    timeout: float | None = DEFAULT_TEST_TIMEOUT_S,
) -> CapturedRun:
    """Run a `test_command` string via shlex splitting, capturing output."""
    return run_capture(shlex.split(command), cwd, recorder=recorder, timeout=timeout)


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


def git_changed_files(cwd: Path, ref: str, *, recorder: SpanRecorder | None = None) -> list[str]:
    """Worktree-relative paths differing from `ref`, for scoping autofixes.

    `--no-renames` (T3-14): with git's default rename detection a staged
    `git mv n.py m.py` prints only `m.py`, so the path a node deleted
    never reached target-scope and a `target_files` list naming the new
    path alone let the rename through. Both paths are the node's.
    """
    argv = ["git", "-C", str(cwd), "diff", "--no-renames", "--name-only", ref, "--", "."]
    start = perf_counter()
    proc = subprocess.run(argv, capture_output=True, text=True)
    _record(recorder, argv, start, proc)
    if proc.returncode != 0:
        msg = f"git diff --name-only against {ref!r} failed: {proc.stderr.strip()}"
        raise RuntimeError(msg)
    return proc.stdout.splitlines()


def git_added_files(cwd: Path, ref: str, *, recorder: SpanRecorder | None = None) -> list[str]:
    """Worktree-relative paths that do not exist at `ref` (renames count as added).

    Only staged adds count. A worker introduces files through its diff
    alone, and `_apply_diff` applies with `--index`, so every file a node
    creates is staged. Untracked files in the worktree are the harness's
    own artefacts -- `.saddle/proofs.jsonl`, `.coverage.tier1`, bytecode --
    and listing them would fail every `refactor` node on its first retry.
    """
    argv = [
        "git",
        "-C",
        str(cwd),
        "diff",
        "--no-renames",
        "--diff-filter=A",
        "--name-only",
        ref,
        "--",
        ".",
    ]
    start = perf_counter()
    proc = subprocess.run(argv, capture_output=True, text=True)
    _record(recorder, argv, start, proc)
    if proc.returncode != 0:
        msg = f"git diff --diff-filter=A against {ref!r} failed: {proc.stderr.strip()}"
        raise RuntimeError(msg)
    return proc.stdout.splitlines()


BASELINE_REF_PREFIX: Final = "refs/saddle/baseline/"
"""Namespace for per-node baseline commits. Outside `refs/heads` and
`refs/tags`, so writing one moves no branch, no tag and not `HEAD`."""


PROVEN_REF_PREFIX: Final = "refs/saddle/proven/"
"""Namespace for the tree each proof was sealed against (T3-10). Same
properties as `BASELINE_REF_PREFIX`: writing one moves no branch, no tag
and not `HEAD`."""


ATTEMPT_REF_PREFIX: Final = "refs/saddle/attempt/"
"""Namespace for the tree each attempt was graded on (T6-34). Same
properties as `BASELINE_REF_PREFIX`: writing one moves no branch, no tag
and not `HEAD`."""


def _ref_slug(node_id: str) -> str:
    """`node_id` as a git-legal ref component, else its sha256 prefix.

    Node ids come from the planner and nothing in the DAG schema makes
    them ref-safe: a space, a `..`, a trailing `.lock` would fail
    `update-ref` and take the node down for a naming reason. git's own
    parser decides -- `check-ref-format` is the one `update-ref` uses --
    and the digest fallback is a pure function of the id, so attempts
    2..N of the same node resolve the same ref.
    """
    proc = subprocess.run(
        ["git", "check-ref-format", f"{BASELINE_REF_PREFIX}{node_id}"],
        capture_output=True,
        text=True,
    )
    if proc.returncode == 0:
        return node_id
    return hashlib.sha256(node_id.encode()).hexdigest()[:16]


def proven_ref(node_id: str) -> str:
    """The ref holding the tree `node_id`'s proof was sealed against (T3-10).

    Same slug rule as the baseline refs: `check-ref-format` decides a ref
    component by component, so an id git accepts under one
    `refs/saddle/<word>/` prefix it accepts under the other.
    """
    return f"{PROVEN_REF_PREFIX}{_ref_slug(node_id)}"


def attempt_ref(node_id: str, attempt: int) -> str:
    """The ref holding the tree attempt `attempt` of `node_id` was graded on.

    A failed attempt used to leave no ref at all: the baseline ref is the
    pre-node tree and a proven ref exists only for a pass, so `git gc`
    pruned the only copy of what the gates actually judged. Session 41
    had to rebuild round 3d's attempt trees out of `lost-found` dangling
    blobs to re-score them (F21.15), and two of those blobs differed from
    each other only by autofix.

    The attempt number is its own ref component, so attempts 1..N of one
    node are N refs and not one that the last attempt overwrites. The
    slug rule is `proven_ref`'s: `check-ref-format` decides a ref
    component at a time, and a decimal attempt number is legal in every
    component position.
    """
    return f"{ATTEMPT_REF_PREFIX}{_ref_slug(node_id)}/{attempt}"


# The identity every commit saddle creates is authored under (T6-56).
# `commit-tree` takes no identity of its own, so it falls back to git's
# auto-derived `user@host` -- which is not a fallback at all when the host
# has no domain: `unable to auto-detect email address (got
# 'eliza@pop-os.(none)')`, exit 128. Round 3h died on that at its first
# node, 16 ms into the slice, and the transcript reported a node with no
# proof and no gate naming it, because no gate ran. `_ensure_repo` already
# committed the baseline under this name; the snapshots T6-34 added did
# not, so a run's survival depended on the operator's git config and on
# DNS. Nothing here is a user identity: a run's refs are saddle's own.
SADDLE_COMMIT_IDENTITY: Final = (
    "-c",
    "user.name=saddle",
    "-c",
    "user.email=saddle@local",
)


def snapshot_tree(cwd: Path, ref: str | None, *, recorder: SpanRecorder | None = None) -> str:
    """Stage the tracked files at `cwd`; return their `git write-tree` id.

    `git add -u` stages tracked files only (no pathspec: git 2.45 and later
    reject `-- .` when nothing is tracked yet, as after `commit
    --allow-empty`; 2.43 did not, which is why CI was red while check.sh was
    green), so `.coverage.tier1`,
    `.saddle/` and bytecode stay out of the tree, while a file `git apply
    --index` added is already tracked and does go in. The id is a pure
    function of that content and nothing else: the same tracked tree
    hashes the same before and after it is committed, which is what lets
    a resume compare the worktree it was handed against the tree a proof
    was sealed on (`ref=None`: stage and hash, publish nothing).

    With a `ref`, the tree is also named -- `commit-tree -p HEAD` then
    `update-ref`, outside `refs/heads` and `refs/tags`, so `HEAD`, every
    branch and the set of paths the index tracks are unchanged and the
    worker's worktree is the worktree it had. `git diff`, `git diff
    --diff-filter=A` and `git archive` take the result exactly as they
    take `HEAD`.
    """

    def git(*args: str) -> str:
        argv = ["git", "-C", str(cwd), *args]
        start = perf_counter()
        proc = subprocess.run(argv, capture_output=True, text=True)
        _record(recorder, argv, start, proc)
        if proc.returncode != 0:
            msg = f"{' '.join(argv)} failed: {proc.stderr.strip()}"
            raise RuntimeError(msg)
        return proc.stdout.strip()

    git("add", "-u")
    tree = git("write-tree")
    if ref is not None:
        commit = git(
            *SADDLE_COMMIT_IDENTITY,
            "commit-tree",
            tree,
            "-p",
            "HEAD",
            "-m",
            f"saddle snapshot {ref}",
        )
        git("update-ref", ref, commit)
    return tree


def snapshot_baseline(cwd: Path, node_id: str, *, recorder: SpanRecorder | None = None) -> str:
    """Freeze the worktree at `cwd` as a commit; return the ref naming it.

    A node's gates must diff the node's own work, and `HEAD` is not that
    ref once a slice has more than one node: `_run_node` applies,
    autofixes, gates and seals but never commits, so every earlier node's
    proven edit is still staged when the next node runs. Against `HEAD`
    the second node's `target-scope` names the first node's files, its
    `coverage` demands lines it never wrote, and `red-phase` compares
    against a tree two nodes old.

    `snapshot_tree` does the work and states what it leaves untouched;
    this one only decides the ref.
    """
    ref = f"{BASELINE_REF_PREFIX}{_ref_slug(node_id)}"
    snapshot_tree(cwd, ref, recorder=recorder)
    return ref


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


def restore_baseline(cwd: Path, ref: str, *, recorder: SpanRecorder | None = None) -> None:
    """Put the worktree and index at `cwd` back to git `ref` (T3-23).

    A node that gave up leaves its applied diffs staged; a replacement
    node would snapshot them as its own baseline and the merge suite
    would run over them. One `git restore --source <ref> --staged
    --worktree -- .` drops edits and staged adds alike (probed: a file
    `git apply --index` added since the ref is gone from disk after it).
    The span is named `restore-baseline` so the journal shows the tree
    was reset. Raises `RuntimeError` naming the ref if anything still
    differs from it afterwards; `HEAD` and every branch stand.
    """
    argv = ["git", "-C", str(cwd), "restore", "--source", ref, "--staged", "--worktree", "--", "."]
    start = perf_counter()
    proc = subprocess.run(argv, capture_output=True, text=True)
    _record(recorder, argv, start, proc, name="restore-baseline")
    if proc.returncode != 0:
        msg = f"git restore to {ref!r} failed: {proc.stderr.strip()}"
        raise RuntimeError(msg)
    leftover = git_changed_files(cwd, ref, recorder=recorder) + git_added_files(
        cwd, ref, recorder=recorder
    )
    if leftover:
        paths = ", ".join(sorted(set(leftover)))
        msg = f"worktree still differs from {ref!r} after restore: {paths}"
        raise RuntimeError(msg)


def under_coverage(test_command: str, data_file: str) -> str:
    """Wrap a pytest scope command so the run records into `data_file`."""
    return test_command.replace("pytest", f"coverage run --data-file={data_file} -m pytest", 1)


def pytest_scope(test_command: str) -> tuple[str, ...]:
    """The arguments a declared `test_command` hands to pytest, in order.

    Everything after the first `pytest` token, so the mutation gate's
    engine collects the same tests the tests gate ran (F21.12a). A
    command that never names pytest yields nothing, and the engine runs
    its whole tree as before.
    """
    argv = shlex.split(test_command)
    if "pytest" not in argv:
        return ()
    return tuple(argv[argv.index("pytest") + 1 :])


def scoped_targets(targets: Collection[str], scope: Collection[str]) -> tuple[str, ...]:
    """`targets` the declared pytest `scope` would itself collect (F21.65).

    The property oracle runs its targets ALONE against the node's
    mutants, and mutmut baselines by running that selection, so one
    module the node's scope excludes decides the whole oracle. In a TDD
    plan such a module is red by construction -- the test node writes
    every module's specification up front and the impl node that greens
    it has not landed -- and the oracle then reports "no mutants
    sampled", which no diff the node can write will change.

    An empty `scope` is pytest's whole tree, so nothing is filtered.
    Flags are not selectors and are ignored; a `path::name` selector
    scopes by its path. What this admits, stated plainly: a node whose
    only property-bearing module lies outside its scope gets no oracle
    at all, the same way the mutation gate already runs only the node's
    declared tests.
    """
    roots = tuple(
        PurePath(item.split("::", 1)[0]).as_posix().rstrip("/")
        for item in scope
        if not item.startswith("-")
    )
    if not roots:
        return tuple(targets)
    kept = []
    for target in targets:
        path = PurePath(target).as_posix()
        if any(path == root or path.startswith(root + "/") for root in roots):
            kept.append(target)
    return tuple(kept)


@dataclass(frozen=True)
class MutationOutcome:
    """Sampled kill-rate evidence over changed-line mutants."""

    killed: int
    total: int
    generated: int
    survivors: tuple[str, ...]
    # Mutants whose only change is inside string literals (T6-33): no test
    # derived from a requirement can kill one without pinning wording, so
    # they leave the population and are counted here instead.
    text_only: int = 0
    # Where each survivor sits (T6-29c): the changed lines its removed hunk
    # lines matched, spelled as the caller spelled `changed`, so a recovery
    # can name the enclosing function without re-running the engine.
    survivor_lines: tuple[tuple[str, int], ...] = ()


def _is_given(decorator: ast.expr) -> bool:
    node = decorator.func if isinstance(decorator, ast.Call) else decorator
    name = (
        node.id
        if isinstance(node, ast.Name)
        else node.attr
        if isinstance(node, ast.Attribute)
        else ""
    )
    return name == "given"


def _imported_names(tree: ast.AST) -> set[str]:
    """Dotted names a module imports: `a.b` and, for `from a import b`, `a` and `a.b`."""
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            module = node.module or ""
            if module:
                names.add(module)
            names.update(f"{module}.{alias.name}" if module else alias.name for alias in node.names)
    return names


def property_modules(
    test_sources: Mapping[str, str], changed_files: Collection[str]
) -> dict[str, str]:
    """Test modules that drive a `@given` property over a changed module.

    A module counts when its AST carries a `given` decorator and one of
    its imports names a changed file: the dotted name, as a path, equals
    the changed file's path without `.py` (a package's `__init__.py` is
    its directory) or ends it at a `/` boundary. These are the modules
    the property oracle runs alone against the node's mutants (T3-3).
    """
    targets: set[str] = set()
    for path in changed_files:
        posix = PurePath(path).as_posix().removesuffix(".py")
        targets.add(posix.removesuffix("/__init__") if posix.endswith("/__init__") else posix)
    matched: dict[str, str] = {}
    for path in sorted(test_sources):
        source = test_sources[path]
        try:
            tree = ast.parse(source)
        except SyntaxError:
            continue
        functions = (
            n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef | ast.AsyncFunctionDef)
        )
        if not any(_is_given(d) for f in functions for d in f.decorator_list):
            continue
        for name in _imported_names(tree):
            as_path = name.replace(".", "/")
            if any(t == as_path or t.endswith("/" + as_path) for t in targets):
                matched[path] = source
                break
    return matched


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


_FSTRING_TEXT: Final = frozenset(
    getattr(tokenize, n) for n in ("FSTRING_MIDDLE",) if hasattr(tokenize, n)
)


def _tokens_modulo_strings(line: str) -> list[tuple[int, str]] | None:
    """A line's tokens with every string literal's text blanked, or None when
    the line does not tokenize on its own (a multi-line construct)."""
    try:
        tokens = list(tokenize.generate_tokens(io.StringIO(line + "\n").readline))
    except (tokenize.TokenError, SyntaxError):
        return None
    out: list[tuple[int, str]] = []
    for tok in tokens:
        if tok.type in (tokenize.NL, tokenize.NEWLINE, tokenize.ENDMARKER, tokenize.COMMENT):
            continue
        if tok.type in _FSTRING_TEXT:
            # An f-string's literal text arrives as a variable number of
            # middle tokens (3.12+); it is string content, so it is dropped
            # rather than blanked, and the expressions between stay.
            continue
        out.append((tok.type, "" if tok.type == tokenize.STRING else tok.string))
    return out


def text_only_mutant(show_output: str) -> bool:
    """Whether a `mutmut show` diff changes nothing but string-literal text (T6-33).

    Round 3d's n2 was asked to kill 66 survivors of which 34 edited only a
    message (`"cannot convert"` to `"XXcannot convertXX"`, F21.14). Pairwise:
    the removed and added lines must tokenize identically once string
    contents are blanked, and at least one string must differ. Anything that
    does not tokenize line by line stays in the population (fail closed).

    This is a shape test, not a semantic one, and it cannot be more: whether
    a string's contents are constrained is a property of the task, not of
    the code. t5's rule 1 requires the `ValueError` message to name all
    three currencies, and a string can equally be a currency code or a
    `Decimal` exponent -- round 3h classified `currency == "XXJPYXX"` and
    `Decimal("XX1XX")` as text-only, and the suite killed both (F21.32).
    So `mutation_sample` consults this only for SURVIVORS, where an
    exclusion costs nothing it could have learned; a killed mutant of any
    shape is evidence the suite discriminates and stays in the population.
    """
    removed = [
        line[1:]
        for line in show_output.splitlines()
        if line.startswith("-") and not line.startswith("--- ")
    ]
    added = [
        line[1:]
        for line in show_output.splitlines()
        if line.startswith("+") and not line.startswith("+++ ")
    ]
    if not removed or len(removed) != len(added):
        return False
    differs = False
    for old, new in zip(removed, added, strict=True):
        if old == new:
            continue
        a, b = _tokens_modulo_strings(old), _tokens_modulo_strings(new)
        if a is None or b is None or a != b:
            return False
        differs = True
    return differs


def _mutant_def(
    tree: ast.Module, match: re.Match[str]
) -> ast.FunctionDef | ast.AsyncFunctionDef | None:
    """The `def` mutmut mutated, from `_MUTANT_NAME`'s parse of `mutant_name`.

    A method resolves only inside its own top-level `ClassDef`'s direct
    children (P0-2's M-L5: name alone is not enough -- two classes, or a
    module-level function sharing a method's name, must not collide). A
    function resolves only among top-level `def`s, matching the spec's
    "the top-level def named <func>".
    """
    cls_name = match.group("cls")
    func_name = match.group("func") or match.group("method")
    scope: list[ast.stmt] = tree.body
    if cls_name is not None:
        class_node = next(
            (n for n in tree.body if isinstance(n, ast.ClassDef) and n.name == cls_name),
            None,
        )
        if class_node is None:
            return None
        scope = class_node.body
    for node in scope:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == func_name:
            return node
    return None


def _statement_start(tree: ast.Module, line: int) -> int | None:
    """First line of the innermost `ast.stmt`/`ast.excepthandler` in `tree`
    whose `[lineno, end_lineno]` contains `line`; `None` if none does.

    This is the coordinate system `statement_lines` (and so `changed`)
    uses, built by taking the enclosing statement with the smallest span --
    a nested statement's range is always a subset of its parents', so the
    smallest one containing `line` is the innermost. Walking the whole
    module rather than just the resolved def's own subtree is deliberate
    (P0-2's M-L1): the def's own search range is what keeps a duplicate
    line elsewhere in the file out of `matched` in the first place, and
    this function must not independently re-derive that scoping, or a
    mutant that widens the range to the whole file stops being
    observable -- it would land back on a real statement (the duplicate's
    own) instead of the resolved def's, silently passing.
    """
    best: ast.stmt | ast.excepthandler | None = None
    best_span = -1
    for child in ast.walk(tree):
        if not isinstance(child, (ast.stmt, ast.excepthandler)):
            continue
        end = child.end_lineno or child.lineno
        if not (child.lineno <= line <= end):
            continue
        span = end - child.lineno
        if best is None or span < best_span:
            best, best_span = child, span
    return best.lineno if best is not None else None


def _mutant_lines(show_output: str, source: str, mutant_name: str) -> set[int]:
    """Statement-start line numbers the mutant's removed (`-`) hunk lines locate to.

    Scoped to the function mutmut actually mutated (P0-2's contract): a
    method mutant on a changed line enters the population, a mutant on a
    continuation line of a changed statement enters it, and a mutant whose
    text merely repeats a changed line elsewhere in the file does not.

    `mutant_name`'s two production shapes (`_MUTANT_NAME`) resolve a `def`
    with `ast` (`_mutant_def`). Only lines inside that def's own range --
    from its first decorator line (or the `def` line) to `end_lineno` --
    can match, either at the def's own indentation added back (mutmut
    renders the extracted function at column 0, so every line including a
    continuation loses that one level of dedent -- verified against a
    real method mutant on a continuation line, see the report) or
    unreindented (defensive: the spec calls for both forms). Restricting
    `matched` to the def's own range is the only thing standing between a
    duplicate line elsewhere in the file and a wrong attribution (P0-2's
    M-L1): `_statement_start` looks up the innermost statement over the
    *whole* module, not just this def's subtree, so a matched line is
    trusted to belong to this def only because the range already
    confined it there. A matched line the whole module covers by no
    statement at all -- a decorator, since `ast.FunctionDef.lineno` is
    the `def` line and never the decorator's -- falls back to the def's
    own line, `statement_lines`'s coordinate for the whole decorated
    function.

    A name that does not match either shape is a hand-built test stub
    (`m1`, `m_hit1`, ...): production names always parse, so this keeps
    the old whole-file exact match, with no statement mapping, exactly as
    it was before this function took `mutant_name`.
    """
    removed = [
        line[1:]
        for line in show_output.splitlines()
        if line.startswith("-") and not line.startswith("--- ")
    ]
    if not removed:
        return set()
    match = _MUTANT_NAME.match(mutant_name)
    if match is None:
        numbered = list(enumerate(source.splitlines(), start=1))
        return {lineno for lineno, text in numbered for snippet in removed if text == snippet}
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return set()
    node = _mutant_def(tree, match)
    if node is None:
        return set()
    lines = source.splitlines()
    start = node.decorator_list[0].lineno if node.decorator_list else node.lineno
    end = min(node.end_lineno or node.lineno, len(lines))
    indent_match = re.match(r"[ \t]*", lines[node.lineno - 1])
    indent = indent_match.group() if indent_match else ""
    matched = {
        lineno
        for lineno in range(start, end + 1)
        for snippet in removed
        if lines[lineno - 1] == indent + snippet or lines[lineno - 1] == snippet
    }
    return {(_statement_start(tree, lineno) or node.lineno) for lineno in matched}


def _mutmut_scratch_config(sources: list[str], run_tests: Collection[str] = ()) -> str:
    """Minimal mutmut config: per-file sources (a `.` root nests mutants/).

    `run_tests` are pytest arguments appended after the fixed flags, so
    only what they collect runs against each mutant; empty means the
    whole scratch tree, as before. A kill scored by a narrowed set
    belongs to that set (T3-3), which the unrestricted run cannot say.
    A sequence keeps its order (a declared scope's `-k expr` must stay a
    pair, F21.12a); an unordered collection is sorted for a stable file.
    """
    quoted = ", ".join(json.dumps(source) for source in sources)
    ordered = list(run_tests) if isinstance(run_tests, Sequence) else sorted(run_tests)
    args = ["-q", "-x", "-p", "no:cacheprovider", *ordered]
    joined = ", ".join(json.dumps(arg) for arg in args)
    return f"[tool.mutmut]\nsource_paths = [{quoted}]\npytest_add_cli_args = [{joined}]\n"


class MutantLookupError(RuntimeError):
    """The batched mutant-lookup subprocess failed or gave unparseable output.

    A lookup failure must be named, never silently read as "no mutants"
    (T3-20's rule for `mutmut run`, extended to this lookup).
    """


# First line is the marker the journal is grepped for (P0-1): argv for this
# call is `[sys.executable, "-c", _MUTANT_LOOKUP_SCRIPT]`, so the marker
# lands in the recorded span's argv and a census can count "one lookup
# subprocess" instead of one `mutmut show` per mutant. Reads every mutant's
# diff in one process: `SourceFileMutationData` and `get_diff_for_mutant`
# are the same functions `mutmut show NAME` calls
# (`mutmut/__main__.py:show`, `mutmut/mutation/diff_apply.py`), so walking
# `walk_mutatable_files()` once and loading each file's meta once
# reproduces `mutmut show`'s stdout byte for byte (measured 2026-09-22,
# `lookup_equiv.py`: 480/480, 535/535, 541/541 across three sealed trees).
# The first file to name a key wins, matching `find_mutant`. A name whose
# diff raises keeps only the header line, exactly what `show` printed to
# stdout before its traceback -- today's silent per-name skip, preserved.
_MUTANT_LOOKUP_SCRIPT: Final = r"""# saddle-mutant-lookup
import json
import sys

from mutmut.mutation.data import SourceFileMutationData
from mutmut.mutation.diff_apply import get_diff_for_mutant
from mutmut.stats import status_by_exit_code
from mutmut.utils.file_utils import walk_mutatable_files

mapping: dict[str, str] = {}
for path in walk_mutatable_files():
    data = SourceFileMutationData(path=path)
    data.load()
    for name, exit_code in data.exit_code_by_key.items():
        if name in mapping:
            continue
        header = f"# {name}: {status_by_exit_code[exit_code]}\n"
        try:
            diff = get_diff_for_mutant(name, path=data.path)
        except Exception:
            mapping[name] = header
        else:
            mapping[name] = header + diff + "\n"
json.dump(mapping, sys.stdout)
"""


def _lookup_failure(run: CapturedRun) -> MutantLookupError:
    """One line: the lookup subprocess's exit code and its last stderr line."""
    lines = run.stderr.strip().splitlines()
    last = lines[-1] if lines else "no output"
    msg = f"exit {run.exit_code}: {last}"
    return MutantLookupError(msg)


def show_all_mutants(scratch: Path, *, recorder: SpanRecorder | None = None) -> dict[str, str]:
    """`mutmut show NAME`'s stdout for every mutant, in one subprocess (P0-1).

    Runs `_MUTANT_LOOKUP_SCRIPT` with `cwd=scratch`: mutmut's `config()` is a
    process-global cache read from `./pyproject.toml` and
    `read_mutants_module` opens `Path("mutants") / path` relative to the
    working directory, so the lookup must run as a subprocess rooted at
    `scratch` rather than inside saddle's own process. Raises
    `MutantLookupError` on a non-zero exit or unparseable stdout.
    """
    run = run_capture([sys.executable, "-c", _MUTANT_LOOKUP_SCRIPT], scratch, recorder=recorder)
    if run.exit_code != 0:
        raise _lookup_failure(run)
    try:
        mapping = json.loads(run.stdout)
    except ValueError:
        raise _lookup_failure(run) from None
    if not isinstance(mapping, dict) or not all(isinstance(v, str) for v in mapping.values()):
        raise _lookup_failure(run)
    return mapping


def mutation_sample(
    workdir: Path,
    changed: Collection[tuple[str, int]],
    max_mutants: int,
    *,
    test_files: Collection[str],
    run_tests: Collection[str] = (),
    suite_passed: bool = True,
    timeout_s: int = _MUTATION_TIMEOUT_S,
    recorder: SpanRecorder | None = None,
) -> MutationOutcome:
    """Kill-rate over every decided mutant on a changed line.

    Runs in a scratch copy (mutmut writes mutants/ into cwd) under a time
    budget; the verdict covers every changed-line mutant mutmut decided
    and degrades to decided mutants when the budget binds first (a real
    timeout, not a truncation: see `max_mutants` below). `max_mutants` is
    kept as a parameter only because the plan schema and its callers
    still pass it; it no longer samples or truncates the population
    (P0-8, T6-61), so no verdict depends on which mutants sort first by
    name. `test_files` are excluded from mutation scope (mutating tests
    pollutes the rate);
    `run_tests` restricts which tests pytest collects against each mutant
    and leaves the scope alone -- the two are different sets (T3-3: a
    session read the first as the second and built a vacuous oracle).
    Text-only mutants are excluded only when they SURVIVED (F21.32).
    Timeouts count as killed (behavior changed), and missing mutmut
    fails closed.
    """
    if not changed:
        return MutationOutcome(killed=0, total=0, generated=0, survivors=())
    if shutil.which("mutmut") is None:
        return MutationOutcome(killed=0, total=0, generated=0, survivors=("mutmut not on PATH",))
    root = os.path.realpath(workdir)
    by_line: dict[str, set[int]] = {}
    spelled: dict[str, str] = {}
    for path, line in changed:
        key = os.path.relpath(os.path.realpath(path), root)
        by_line.setdefault(key, set()).add(line)
        spelled.setdefault(key, path)
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
        (scratch / "pyproject.toml").write_text(_mutmut_scratch_config(production, run_tests))
        ran = run_capture(["timeout", str(timeout_s), "mutmut", "run"], scratch, recorder=recorder)
        # `mutmut run` exits 0 even when mutants survive, so any other exit
        # is the tool failing, not a verdict (T3-20): the smoke run's mutmut
        # 3.8 refused a package named `src` and exited 1 in 658 ms, and the
        # gate read "no mutants decided" -- the absence of a verdict, not
        # the tool. SHELL_TIMEOUT is the budget binding and keeps its path.
        if ran.exit_code not in (0, SHELL_TIMEOUT):
            output = (ran.stderr.strip() or ran.stdout.strip()).splitlines()
            last = output[-1].strip() if output else "no output"
            # The same exit covers two causes and only the caller can tell
            # them apart (T6-63): mutmut baselines by running the suite, so
            # a red suite fails collection exactly as a broken engine does.
            # `suite_passed` is the tests gate's own verdict on this tree;
            # when it is False the suite is the cause and the engine is not.
            cause = f"mutmut run exited {ran.exit_code}: {last}"
            return MutationOutcome(
                killed=0,
                total=0,
                generated=0,
                survivors=(cause if suite_passed else f"suite is red: {cause}",),
            )
        results = run_capture(["mutmut", "results", "--all", "True"], scratch, recorder=recorder)
        verdicts = _parse_mutant_verdicts(results.stdout)
        try:
            shows = show_all_mutants(scratch, recorder=recorder)
        except MutantLookupError as exc:
            # A lookup failure is named, never read as "no mutants" (T3-20's
            # rule for `mutmut run`, extended to the batched lookup).
            msg = f"mutant lookup failed: {exc}"
            return MutationOutcome(killed=0, total=0, generated=0, survivors=(msg,))
        scoped: list[tuple[str, str, str, set[int]]] = []
        undecided = 0
        text_only = 0
        for name in sorted(verdicts):
            verdict = verdicts[name]
            if verdict == "not checked":
                undecided += 1
                continue
            shown_stdout = shows.get(name, "")
            rel = _mutant_path(shown_stdout)
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
            hit = _mutant_lines(shown_stdout, target.read_text(), name) & lines
            if not hit:
                continue
            # F21.32: only a SURVIVING text-only mutant is excluded. The
            # tokenizer cannot tell a message from a currency code or a
            # `Decimal` exponent, so round 3h dropped `currency == "XXJPYXX"`
            # and `Decimal("XX1XX")` -- real behaviour changes the suite
            # killed -- from both sides of the ratio. A kill is evidence the
            # suite discriminates; the verdict decides, not the shape.
            if verdict == "survived" and text_only_mutant(shown_stdout):
                text_only += 1
                continue
            scoped.append((name, verdict, key, hit))
    sample = scoped
    killed = sum(1 for _, verdict, _, _ in sample if verdict in ("killed", "timeout"))
    survivors = tuple(name for name, verdict, _, _ in sample if verdict == "survived")
    survivor_lines = sorted(
        {
            (spelled[key], line)
            for _, verdict, key, hit in sample
            if verdict == "survived"
            for line in hit
        }
    )
    return MutationOutcome(
        killed=killed,
        total=len(sample),
        generated=len(scoped) + undecided,
        survivors=survivors,
        text_only=text_only,
        survivor_lines=tuple(survivor_lines),
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
