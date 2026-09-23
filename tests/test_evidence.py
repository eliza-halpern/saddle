"""Tests for saddle.evidence: collectors over tmp worktrees."""

from __future__ import annotations

import ast
import hashlib
import json
import os
import re
import shutil
import stat
import subprocess
import sys
import warnings
from collections.abc import Sequence
from pathlib import Path

import conftest
import pytest

import saddle.evidence as evidence_module
from saddle.evidence import (
    _MUTATION_TIMEOUT_S,
    SADDLE_COMMIT_IDENTITY,
    CapturedRun,
    MutantLookupError,
    MutationOutcome,
    _mutant_lines,
    _mutmut_scratch_config,
    _statement_start,
    attempt_ref,
    changed_lines,
    covered_lines,
    drop_test_caches,
    git_added_files,
    git_changed_files,
    git_diff,
    git_ls_files,
    materialize_baseline,
    mutation_sample,
    property_modules,
    pytest_scope,
    restore_baseline,
    ruff_argv,
    ruff_findings,
    ruff_version,
    run_argv,
    run_capture,
    run_shell,
    run_shell_capture,
    run_stdin,
    scoped_targets,
    show_all_mutants,
    snapshot_baseline,
    statement_lines,
    text_only_mutant,
    under_coverage,
)
from saddle.gates import SHELL_TIMEOUT, TOOL_UNAVAILABLE
from saddle.journal import SpanRecorder, read_spans

_ALL_MUTANT_NAMES = re.compile(r"^\s*(\S+): ", re.MULTILINE)
"""Every name `mutmut results --all True` lines, whatever its verdict --
broader than `_parse_mutant_verdicts`, which only recognizes killed,
survived, timeout and not-checked and silently drops e.g. "no tests"."""


def _git_subcommand(argv: Sequence[str]) -> str:
    """The subcommand in a `git -C <dir> [-c k=v ...] <sub> ...` argv.

    Position stopped naming it when T6-56 put saddle's identity on the
    commit: two `-c` pairs now sit between `-C <dir>` and the subcommand.
    Reading it by shape rather than by index keeps the assertion pinned to
    which git runs, which is what it was ever about.
    """
    rest = iter(argv[3:])
    for token in rest:
        if token == "-c":
            next(rest)
            continue
        return token
    msg = f"no git subcommand in {list(argv)}"
    raise AssertionError(msg)


def _git_repo(root: Path) -> None:
    setup = (
        ["git", "init"],
        ["git", "config", "user.email", "test@example.com"],
        ["git", "config", "user.name", "test"],
        ["git", "commit", "--allow-empty", "-m", "base"],
    )
    for argv in setup:
        assert run_argv(argv, root) == 0


def test_run_argv_returns_exit_code(tmp_path: Path) -> None:
    assert run_argv([sys.executable, "-c", "pass"], tmp_path) == 0
    assert run_argv([sys.executable, "-c", "raise SystemExit(3)"], tmp_path) == 3


def test_run_argv_captures_child_output(tmp_path: Path, capfd: pytest.CaptureFixture[str]) -> None:
    assert run_argv([sys.executable, "-c", "print('hi')"], tmp_path) == 0
    assert capfd.readouterr().out == ""


def test_run_stdin_feeds_text(tmp_path: Path, capfd: pytest.CaptureFixture[str]) -> None:
    (tmp_path / "f").write_text("x\n")
    code = run_stdin(["sh", "-c", "cat f && grep -q hi"], tmp_path, "hi\n")
    assert code == 0
    assert capfd.readouterr().out == ""


def test_run_shell_splits_command_string(tmp_path: Path) -> None:
    assert run_shell(f"{sys.executable} -c pass", tmp_path) == 0
    assert run_shell(f"{sys.executable} -c 'raise SystemExit(2)'", tmp_path) == 2


def test_run_capture_returns_exit_and_output(tmp_path: Path) -> None:
    run = run_capture(
        [sys.executable, "-c", "import sys; print('out'); print('err', file=sys.stderr)"],
        tmp_path,
    )
    assert run.exit_code == 0
    assert run.stdout == "out\n"
    assert run.stderr == "err\n"
    assert run.argv[0] == sys.executable
    failed = run_capture([sys.executable, "-c", "raise SystemExit(3)"], tmp_path)
    assert failed.exit_code == 3


def test_run_capture_records_span_like_run_argv(tmp_path: Path) -> None:
    journal = tmp_path / "proofs.jsonl"
    recorder = SpanRecorder(path=journal, node_id="n1")
    run = run_capture([sys.executable, "-c", "pass"], tmp_path, recorder=recorder)
    assert run.exit_code == 0
    (span,) = read_spans(journal)
    assert (span.name, span.exit_code, span.node_id) == ("python", 0, "n1")


def test_run_shell_capture_splits_and_returns_output(tmp_path: Path) -> None:
    run = run_shell_capture(f"{sys.executable} -c \"print('hi')\"", tmp_path)
    assert (run.exit_code, run.stdout) == (0, "hi\n")


def test_drop_test_caches_removes_caches_but_keeps_sources(tmp_path: Path) -> None:
    drop_test_caches(tmp_path)
    cache = tmp_path / "pkg" / "__pycache__"
    cache.mkdir(parents=True)
    (cache / "m.pyc").write_text("x")
    (tmp_path / "stray.pyc").write_text("x")
    (tmp_path / ".pytest_cache").mkdir()
    (tmp_path / "keep.py").write_text("x = 1\n")
    drop_test_caches(tmp_path)
    assert not cache.exists()
    assert not (tmp_path / "stray.pyc").exists()
    assert not (tmp_path / ".pytest_cache").exists()
    assert (tmp_path / "keep.py").is_file()


def test_drop_test_caches_tolerates_unremovable_caches(tmp_path: Path) -> None:
    locked = [tmp_path / "pkg" / "__pycache__", tmp_path / ".pytest_cache"]
    try:
        for cache in locked:
            cache.mkdir(parents=True, exist_ok=True)
            (cache / "x.bin").write_text("x")
            cache.chmod(0o555)
        drop_test_caches(tmp_path)
    finally:
        for cache in locked:
            cache.chmod(0o755)


def test_runners_record_spans_when_given_recorder(tmp_path: Path) -> None:
    journal = tmp_path / "proofs.jsonl"
    recorder = SpanRecorder(path=journal, node_id="n1")
    assert run_argv([sys.executable, "-c", "pass"], tmp_path, recorder=recorder) == 0
    (tmp_path / "f").write_text("x\n")
    assert run_stdin(["sh", "-c", "cat f"], tmp_path, "hi\n", recorder=recorder) == 0
    assert run_shell(f"{sys.executable} -c pass", tmp_path, recorder=recorder) == 0
    spans = read_spans(journal)
    assert [span.name for span in spans] == ["python", "sh", "python"]
    assert all(span.node_id == "n1" for span in spans)
    assert all(span.exit_code == 0 for span in spans)
    assert all(span.duration_ms >= 0 for span in spans)


def test_runners_write_no_spans_without_recorder(tmp_path: Path) -> None:
    assert run_argv([sys.executable, "-c", "pass"], tmp_path) == 0
    assert list(tmp_path.iterdir()) == []


def test_record_duration_uses_perf_counter_delta(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    journal = tmp_path / "proofs.jsonl"
    recorder = SpanRecorder(path=journal, node_id="n1")
    ticks = iter([100.0, 110.5])
    monkeypatch.setattr(evidence_module, "perf_counter", lambda: next(ticks))
    run_argv([sys.executable, "-c", "pass"], tmp_path, recorder=recorder)
    (span,) = read_spans(journal)
    assert span.duration_ms == 10500


def test_record_replaces_undecodable_stderr(tmp_path: Path) -> None:
    journal = tmp_path / "proofs.jsonl"
    recorder = SpanRecorder(path=journal, node_id="n1")
    run_argv(
        [sys.executable, "-c", "import sys; sys.stderr.buffer.write(b'\\xff\\xfe')"],
        tmp_path,
        recorder=recorder,
    )
    (span,) = read_spans(journal)
    assert span.detail == "��"


def test_changed_lines_modified_file_hunk_range() -> None:
    diff = (
        "diff --git a/n.py b/n.py\n"
        "--- a/n.py\n"
        "+++ b/n.py\n"
        "@@ -1,2 +1,3 @@\n"
        " x = 1\n"
        "+y = 2\n"
        "+z = 3\n"
    )
    assert changed_lines(diff) == {("n.py", 1), ("n.py", 2), ("n.py", 3)}


def test_changed_lines_new_file_single_line_hunk() -> None:
    diff = "diff --git a/new.py b/new.py\n--- /dev/null\n+++ b/new.py\n@@ -0,0 +1 @@\n+x = 1\n"
    assert changed_lines(diff) == {("new.py", 1)}


def test_changed_lines_deleted_file_and_empty_diff_yield_none() -> None:
    deleted = (
        "diff --git a/gone.py b/gone.py\n--- a/gone.py\n+++ /dev/null\n@@ -1,2 +0,0 @@\n-x = 1\n"
    )
    assert changed_lines(deleted) == set()
    assert changed_lines("") == set()


def test_changed_lines_ignores_hunk_before_any_file() -> None:
    diff = "@@ -1 +1 @@\n+x = 1\n+++ b/n.py\n@@ -1 +1 @@\n+y = 2\n"
    assert changed_lines(diff) == {("n.py", 1)}


def test_changed_lines_ignores_additions_after_dev_null() -> None:
    diff = "+++ /dev/null\n@@ -1 +1 @@\n+x = 1\n"
    assert changed_lines(diff) == set()


def test_git_diff_shows_tracked_modification(tmp_path: Path) -> None:
    _git_repo(tmp_path)
    (tmp_path / "n.py").write_text("x = 1\n")
    assert run_argv(["git", "add", "n.py"], tmp_path) == 0
    assert run_argv(["git", "commit", "-m", "add n"], tmp_path) == 0
    (tmp_path / "n.py").write_text("x = 1\ny = 2\n")
    diff = git_diff(tmp_path, "HEAD")
    assert changed_lines(diff) == {("n.py", 2)}


def test_git_ls_files_lists_tracked_paths(tmp_path: Path) -> None:
    _git_repo(tmp_path)
    (tmp_path / "n.py").write_text("x = 1\n")
    assert run_argv(["git", "add", "n.py"], tmp_path) == 0
    assert git_ls_files(tmp_path) == ["n.py"]


def test_git_ls_files_outside_repo_raises(tmp_path: Path) -> None:
    with pytest.raises(RuntimeError, match="git ls-files failed"):
        git_ls_files(tmp_path)


def test_git_diff_unknown_ref_raises(tmp_path: Path) -> None:
    _git_repo(tmp_path)
    with pytest.raises(RuntimeError, match="no-such-ref"):
        git_diff(tmp_path, "no-such-ref")


def test_git_changed_files_lists_modified_paths(tmp_path: Path) -> None:
    _git_repo(tmp_path)
    (tmp_path / "n.py").write_text("x = 1\n")
    (tmp_path / "notes.txt").write_text("hello\n")
    assert run_argv(["git", "add", "n.py", "notes.txt"], tmp_path) == 0
    assert run_argv(["git", "commit", "-m", "add files"], tmp_path) == 0
    (tmp_path / "n.py").write_text("x = 2\n")
    assert git_changed_files(tmp_path, "HEAD") == ["n.py"]


def test_git_changed_files_unknown_ref_raises(tmp_path: Path) -> None:
    """Fails loudly: a silent empty list would skip the autofix entirely."""
    _git_repo(tmp_path)
    with pytest.raises(RuntimeError, match="git diff --name-only against 'no-such-ref' failed"):
        git_changed_files(tmp_path, "no-such-ref")


def test_git_added_files_lists_staged_adds_and_ignores_untracked(tmp_path: Path) -> None:
    """A staged new file is the node's; an untracked one is the harness's
    (journal, coverage data, bytecode) and must not fail node-scope (#65, T2-2)."""
    _git_repo(tmp_path)
    (tmp_path / "b.py").write_text("x = 1\n")
    assert run_argv(["git", "add", "b.py"], tmp_path) == 0
    assert git_added_files(tmp_path, "HEAD") == ["b.py"]
    assert git_changed_files(tmp_path, "HEAD") == ["b.py"]
    (tmp_path / ".coverage.tier1").write_text("")
    (tmp_path / "c.py").write_text("y = 2\n")
    assert git_added_files(tmp_path, "HEAD") == ["b.py"]


def test_git_diff_helpers_see_a_staged_rename_as_both_paths(tmp_path: Path) -> None:
    """Known-good (T3-14): a staged `git mv n.py m.py` is an add of `m.py`
    (so a `refactor` node fails node-scope) and touches both paths (so a
    `target_files` list must name the old file too). Git's default rename
    detection would fold it into one `R100` entry: `--diff-filter=A` then
    prints nothing and `--name-only` prints only the new name -- the
    known-bad, reached by dropping either `--no-renames`."""
    _git_repo(tmp_path)
    (tmp_path / "n.py").write_text("x = 1\n")
    assert run_argv(["git", "add", "n.py"], tmp_path) == 0
    assert run_argv(["git", "commit", "-q", "-m", "n"], tmp_path) == 0
    assert run_argv(["git", "mv", "n.py", "m.py"], tmp_path) == 0
    assert git_added_files(tmp_path, "HEAD") == ["m.py"]
    assert git_changed_files(tmp_path, "HEAD") == ["m.py", "n.py"]


def test_git_added_files_unknown_ref_raises(tmp_path: Path) -> None:
    _git_repo(tmp_path)
    with pytest.raises(RuntimeError, match="git diff --diff-filter=A against 'no-such-ref'"):
        git_added_files(tmp_path, "no-such-ref")


def test_snapshot_baseline_freezes_the_worktree_without_moving_head(tmp_path: Path) -> None:
    """Known-good (T3-8): the ref carries the worktree as the node found it --
    a staged add and an unstaged edit alike -- so nothing the earlier nodes
    left behind is a change against it, and `HEAD` and the branches stand."""
    _git_repo(tmp_path)
    (tmp_path / "a.py").write_text("x = 1\n")
    assert run_argv(["git", "add", "a.py"], tmp_path) == 0
    assert run_argv(["git", "commit", "-m", "add a"], tmp_path) == 0
    head = run_capture(["git", "rev-parse", "HEAD"], tmp_path).stdout
    branches = run_capture(["git", "branch", "-a"], tmp_path).stdout
    (tmp_path / "a.py").write_text("x = 2\n")
    (tmp_path / "b.py").write_text("y = 1\n")
    assert run_argv(["git", "add", "b.py"], tmp_path) == 0
    ref = snapshot_baseline(tmp_path, "n2")
    assert ref == "refs/saddle/baseline/n2"
    assert git_changed_files(tmp_path, ref) == []
    assert git_added_files(tmp_path, ref) == []
    dest = tmp_path / "tree"
    materialize_baseline(tmp_path, ref, dest)
    assert (dest / "a.py").read_text() == "x = 2\n"
    assert (dest / "b.py").read_text() == "y = 1\n"
    assert run_capture(["git", "rev-parse", "HEAD"], tmp_path).stdout == head
    assert run_capture(["git", "branch", "-a"], tmp_path).stdout == branches


def test_snapshot_baseline_excludes_untracked_harness_artefacts(tmp_path: Path) -> None:
    """Known-bad (T3-8): `.coverage.tier1` and stray bytecode are the
    harness's, not the node's baseline. `add -u` stages tracked files only,
    so an untracked file cannot enter the tree -- and a node that creates
    that same path later is still charged with creating it."""
    _git_repo(tmp_path)
    (tmp_path / "a.py").write_text("x = 1\n")
    assert run_argv(["git", "add", "a.py"], tmp_path) == 0
    assert run_argv(["git", "commit", "-m", "add a"], tmp_path) == 0
    (tmp_path / ".coverage.tier1").write_text("")
    (tmp_path / "junk.py").write_text("z = 1\n")
    ref = snapshot_baseline(tmp_path, "n1")
    dest = tmp_path / "tree"
    materialize_baseline(tmp_path, ref, dest)
    assert sorted(entry.name for entry in dest.iterdir()) == ["a.py"]


def test_snapshot_baseline_lists_a_later_staged_add_against_its_ref(tmp_path: Path) -> None:
    """Tracked-ness comes from the index, not from the ref: a file the node
    stages after its snapshot is still an add against it, so node-scope and
    target-scope still see the node's own file creation (T2-2, T3-2)."""
    _git_repo(tmp_path)
    (tmp_path / "a.py").write_text("x = 1\n")
    assert run_argv(["git", "add", "a.py"], tmp_path) == 0
    assert run_argv(["git", "commit", "-m", "add a"], tmp_path) == 0
    ref = snapshot_baseline(tmp_path, "n1")
    (tmp_path / "new.py").write_text("y = 1\n")
    assert run_argv(["git", "add", "new.py"], tmp_path) == 0
    assert git_added_files(tmp_path, ref) == ["new.py"]
    assert git_changed_files(tmp_path, ref) == ["new.py"]


def test_snapshot_baseline_names_an_illegal_node_id_by_digest(tmp_path: Path) -> None:
    """Known-good (T3-8): an id git's ref parser rejects still gets a
    resolvable ref. Nothing in the DAG schema makes a node id ref-safe, and
    a naming failure must not be how a node dies."""
    _git_repo(tmp_path)
    ref = snapshot_baseline(tmp_path, "a b")
    assert ref == f"refs/saddle/baseline/{hashlib.sha256(b'a b').hexdigest()[:16]}"
    assert run_argv(["git", "rev-parse", "--verify", ref], tmp_path) == 0
    assert git_changed_files(tmp_path, ref) == []


def test_attempt_ref_names_the_node_and_the_attempt_separately() -> None:
    """T6-34 known-good: the attempt number is its own ref component, so N
    attempts of one node are N refs. Known-bad: a ref without it is one ref
    the last attempt overwrites, which is the failure this item exists to
    fix -- round 3d's attempt-1 trees were recoverable only from
    `lost-found` (F21.15). The digest fallback is `proven_ref`'s, because
    `check-ref-format` decides a component at a time.
    """
    assert attempt_ref("n2", 1) == "refs/saddle/attempt/n2/1"
    assert attempt_ref("n2", 2) == "refs/saddle/attempt/n2/2"
    digest = hashlib.sha256(b"a b").hexdigest()[:16]
    assert attempt_ref("a b", 3) == f"refs/saddle/attempt/{digest}/3"


def test_snapshot_baseline_outside_a_repo_raises(tmp_path: Path) -> None:
    """Known-bad (T3-8): no silent ref. A caller handed `HEAD` back for a
    failed snapshot would gate the node against the wrong tree and say
    nothing; the argv that failed is named so the transcript can explain it."""
    with pytest.raises(RuntimeError, match=r"git -C .* add -u failed: fatal: not a git"):
        snapshot_baseline(tmp_path, "n1")


def test_snapshot_baseline_records_one_span_per_git_run(tmp_path: Path) -> None:
    """Four subprocess runs, four spans: an unjournaled git run writes the
    ref the whole node is gated against and leaves the chain unable to say
    where it came from."""
    _git_repo(tmp_path)
    journal = tmp_path / "proofs.jsonl"
    recorder = SpanRecorder(path=journal, node_id="n1")
    snapshot_baseline(tmp_path, "n1", recorder=recorder)
    spans = read_spans(journal)
    assert [span.name for span in spans] == ["git"] * 4
    assert [span.exit_code for span in spans] == [0, 0, 0, 0]
    assert all(span.node_id == "n1" for span in spans)
    assert [_git_subcommand(span.argv) for span in spans] == [
        "add",
        "write-tree",
        "commit-tree",
        "update-ref",
    ]
    # And the identity rides on the commit alone (T6-56): a snapshot that
    # only stages and hashes needs none, and a run that carried it
    # everywhere would still read as four named git runs here.
    assert [tuple(span.argv[3:7]) == SADDLE_COMMIT_IDENTITY for span in spans] == [
        False,
        False,
        True,
        False,
    ]


def test_restore_baseline_drops_a_staged_edit_and_a_staged_add(tmp_path: Path) -> None:
    """Known-good (T3-23): a staged edit and a staged new file since the
    ref are both gone from worktree and index; `HEAD` and the branches stand."""
    _git_repo(tmp_path)
    (tmp_path / "a.py").write_text("x = 1\n")
    assert run_argv(["git", "add", "a.py"], tmp_path) == 0
    assert run_argv(["git", "commit", "-m", "add a"], tmp_path) == 0
    head = run_capture(["git", "rev-parse", "HEAD"], tmp_path).stdout
    branches = run_capture(["git", "branch", "-a"], tmp_path).stdout
    ref = snapshot_baseline(tmp_path, "n1")
    (tmp_path / "a.py").write_text("x = 2\n")
    (tmp_path / "new.py").write_text("y = 1\n")
    assert run_argv(["git", "add", "a.py", "new.py"], tmp_path) == 0
    assert git_added_files(tmp_path, ref) == ["new.py"]
    journal = tmp_path / "proofs.jsonl"
    recorder = SpanRecorder(path=journal, node_id="n1")
    restore_baseline(tmp_path, ref, recorder=recorder)
    assert (tmp_path / "a.py").read_text() == "x = 1\n"
    assert not (tmp_path / "new.py").exists()
    assert run_capture(["git", "diff", ref], tmp_path).stdout == ""
    assert run_capture(["git", "diff", "--cached", ref], tmp_path).stdout == ""
    assert git_changed_files(tmp_path, ref) == []
    assert git_added_files(tmp_path, ref) == []
    assert run_capture(["git", "rev-parse", "HEAD"], tmp_path).stdout == head
    assert run_capture(["git", "branch", "-a"], tmp_path).stdout == branches
    spans = read_spans(journal)
    assert [span.name for span in spans] == ["restore-baseline", "git", "git"]
    assert [span.exit_code for span in spans] == [0, 0, 0]
    assert spans[0].argv[3:] == ["restore", "--source", ref, "--staged", "--worktree", "--", "."]


def test_restore_baseline_is_a_recorded_no_op_on_a_tree_already_at_the_ref(
    tmp_path: Path,
) -> None:
    _git_repo(tmp_path)
    (tmp_path / "a.py").write_text("x = 1\n")
    assert run_argv(["git", "add", "a.py"], tmp_path) == 0
    assert run_argv(["git", "commit", "-m", "add a"], tmp_path) == 0
    ref = snapshot_baseline(tmp_path, "n1")
    journal = tmp_path / "proofs.jsonl"
    recorder = SpanRecorder(path=journal, node_id="n1")
    restore_baseline(tmp_path, ref, recorder=recorder)
    assert (tmp_path / "a.py").read_text() == "x = 1\n"
    assert run_capture(["git", "diff", ref], tmp_path).stdout == ""
    assert run_capture(["git", "diff", "--cached", ref], tmp_path).stdout == ""
    restore = next(span for span in read_spans(journal) if span.name == "restore-baseline")
    assert restore.exit_code == 0


def test_restore_baseline_unknown_ref_raises_and_records(tmp_path: Path) -> None:
    _git_repo(tmp_path)
    journal = tmp_path / "proofs.jsonl"
    recorder = SpanRecorder(path=journal, node_id="n1")
    with pytest.raises(RuntimeError, match="no-such-ref"):
        restore_baseline(tmp_path, "no-such-ref", recorder=recorder)
    (span,) = read_spans(journal)
    assert (span.name, span.exit_code != 0) == ("restore-baseline", True)


def test_restore_baseline_refuses_a_tree_that_still_differs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The post-condition is asserted, not assumed: a restore that leaves a
    difference behind names the ref and the paths instead of returning."""
    _git_repo(tmp_path)
    (tmp_path / "a.py").write_text("x = 1\n")
    assert run_argv(["git", "add", "a.py"], tmp_path) == 0
    ref = snapshot_baseline(tmp_path, "n1")
    monkeypatch.setattr(evidence_module, "git_changed_files", lambda *_a, **_k: ["a.py"])
    with pytest.raises(RuntimeError, match=f"still differs from {ref!r} after restore: a.py"):
        restore_baseline(tmp_path, ref)


def test_git_collectors_record_spans_including_failures(tmp_path: Path) -> None:
    _git_repo(tmp_path)
    (tmp_path / "n.py").write_text("x = 1\n")
    assert run_argv(["git", "add", "n.py"], tmp_path) == 0
    assert run_argv(["git", "commit", "-m", "add n"], tmp_path) == 0
    journal = tmp_path / "proofs.jsonl"
    recorder = SpanRecorder(path=journal, node_id="n1")
    git_diff(tmp_path, "HEAD", recorder=recorder)
    materialize_baseline(tmp_path, "HEAD", tmp_path / "base", recorder=recorder)
    with pytest.raises(RuntimeError, match="no-such-ref"):
        git_diff(tmp_path, "no-such-ref", recorder=recorder)
    spans = read_spans(journal)
    assert [span.name for span in spans] == ["git", "git", "git"]
    assert [span.exit_code for span in spans] == [0, 0, 128]
    assert all(span.node_id == "n1" for span in spans)


def test_materialize_baseline_extracts_pre_change_tree(tmp_path: Path) -> None:
    _git_repo(tmp_path)
    (tmp_path / "n.py").write_text("x = 1\n")
    assert run_argv(["git", "add", "n.py"], tmp_path) == 0
    assert run_argv(["git", "commit", "-m", "add n"], tmp_path) == 0
    (tmp_path / "n.py").write_text("x = 2\n")
    dest = tmp_path / "nested" / "baseline"
    materialize_baseline(tmp_path, "HEAD", dest)
    assert (dest / "n.py").read_text() == "x = 1\n"
    materialize_baseline(tmp_path, "HEAD", dest)
    assert (dest / "n.py").read_text() == "x = 1\n"


def test_materialize_baseline_unknown_ref_raises(tmp_path: Path) -> None:
    _git_repo(tmp_path)
    with pytest.raises(RuntimeError, match="no-such-ref"):
        materialize_baseline(tmp_path, "no-such-ref", tmp_path / "baseline")


def test_materialize_baseline_extracts_without_warnings(tmp_path: Path) -> None:
    _git_repo(tmp_path)
    (tmp_path / "n.py").write_text("x = 1\n")
    assert run_argv(["git", "add", "n.py"], tmp_path) == 0
    assert run_argv(["git", "commit", "-m", "add n"], tmp_path) == 0
    with warnings.catch_warnings():
        warnings.simplefilter("error")
        materialize_baseline(tmp_path, "HEAD", tmp_path / "baseline")


def test_materialize_baseline_empty_tree_extracts_nothing(tmp_path: Path) -> None:
    _git_repo(tmp_path)
    dest = tmp_path / "baseline"
    materialize_baseline(tmp_path, "HEAD", dest)
    assert dest.is_dir()
    assert list(dest.iterdir()) == []


def test_under_coverage_wraps_pytest_command() -> None:
    wrapped = under_coverage("pytest tests/test_n.py", "/tmp/cov.n")
    assert wrapped == "coverage run --data-file=/tmp/cov.n -m pytest tests/test_n.py"


def test_under_coverage_wraps_only_first_pytest() -> None:
    wrapped = under_coverage("pytest -p no:pytest_cache tests/", "/tmp/cov.n")
    assert wrapped == "coverage run --data-file=/tmp/cov.n -m pytest -p no:pytest_cache tests/"


def test_covered_lines_reads_subprocess_data_file(tmp_path: Path) -> None:
    target = tmp_path / "n.py"
    target.write_text("x = 1\ny = 2\n")
    data_file = str(tmp_path / ".coverage.node")
    prog = (
        "import coverage;"
        f"cov = coverage.Coverage(data_file={data_file!r}, config_file=False);"
        "cov.start();"
        f"exec(compile(open({str(target)!r}).read(), {str(target)!r}, 'exec'));"
        "cov.stop();cov.save()"
    )
    assert run_argv([sys.executable, "-c", prog], tmp_path) == 0
    name = str(target)
    assert covered_lines(data_file, [name]) == {(name, 1), (name, 2)}
    assert covered_lines(data_file, [str(tmp_path / "other.py")]) == set()
    assert covered_lines(data_file, []) == set()


def test_covered_lines_matches_relative_query_to_absolute_data(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    target = tmp_path / "n.py"
    target.write_text("x = 1\ny = 2\n")
    data_file = str(tmp_path / ".coverage.node")
    prog = (
        "import coverage;"
        f"cov = coverage.Coverage(data_file={data_file!r}, config_file=False);"
        "cov.start();"
        f"exec(compile(open({str(target)!r}).read(), {str(target)!r}, 'exec'));"
        "cov.stop();cov.save()"
    )
    assert run_argv([sys.executable, "-c", prog], tmp_path) == 0
    monkeypatch.chdir(tmp_path)
    assert covered_lines(data_file, ["n.py"]) == {("n.py", 1), ("n.py", 2)}
    assert covered_lines(data_file, ["missing.py", "n.py"]) == {("n.py", 1), ("n.py", 2)}


def test_covered_lines_missing_data_file_yields_empty(tmp_path: Path) -> None:
    assert covered_lines(str(tmp_path / "nope.coverage"), ["n.py"]) == set()


def test_covered_lines_corrupt_data_file_yields_empty(tmp_path: Path) -> None:
    bad = tmp_path / "bad.coverage"
    bad.write_bytes(b"not a coverage file at all" * 10)
    assert covered_lines(str(bad), ["n.py"]) == set()


def test_covered_lines_ignores_local_config(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (tmp_path / ".coveragerc").write_text("[run\ninvalid [[[\n")
    monkeypatch.chdir(tmp_path)
    assert covered_lines(str(tmp_path / "nope.coverage"), ["n.py"]) == set()


def _stub_mutmut(
    stub_dir: Path, results: str, shows: dict[str, str], run_body: str = "exit 0"
) -> None:
    """Executable `mutmut` stub with canned results and per-name show output."""
    (stub_dir / "results.txt").write_text(results)
    for name, diff in shows.items():
        (stub_dir / f"show_{name}.txt").write_text(diff)
    script = stub_dir / "mutmut"
    script.write_text(
        "#!/bin/sh\n"
        f'STUB_DIR="{stub_dir}"\n'
        'case "$1" in\n'
        f"  run) {run_body};;\n"
        '  results) cat "$STUB_DIR/results.txt";;\n'
        '  show) cat "$STUB_DIR/show_$2.txt" 2>/dev/null || echo "unparseable";;\n'
        "esac\n"
    )
    script.chmod(script.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)


def _show_diff(path: str, old: str, new: str = "x = 2") -> str:
    return f"--- {path}\n+++ {path}\n@@ -1 +1 @@\n-{old}\n+{new}\n"


def _mutation_workdir(root: Path) -> Path:
    workdir = root / "work"
    (workdir / "tests").mkdir(parents=True)
    # Line 3 ("-- a.py") is a sentinel: it equals the [1:] slice of the
    # "--- a.py" header, so any mutant that admits header lines into the
    # removed-line set flips a miss to a hit. Never imported; text only.
    (workdir / "a.py").write_text("x = 1\ny = 2\n-- a.py\n")
    (workdir / "b.py").write_text("z = 3\n")
    (workdir / "tests" / "test_a.py").write_text("def test_a():\n    assert True\n")
    deep = workdir / "src" / "deep" / "nested"
    deep.mkdir(parents=True)
    # Two missing levels: mkdir(parents=False) mutants fail materializing this.
    (deep / "deep.py").write_text("x = 1\n")
    cache = workdir / "__pycache__"
    cache.mkdir()
    (cache / "q.py").write_text("# cached bytecode source stays out of the scratch copy\n")
    return workdir


def test_mutation_sample_filters_scopes_and_counts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    workdir = _mutation_workdir(tmp_path)
    stub_dir = tmp_path / "stub"
    stub_dir.mkdir()
    _stub_mutmut(
        stub_dir,
        "\n".join(
            [
                "",
                "  m_apending: not checked",
                "  m_file: killed",
                "  m_ghost: killed",
                "  m_hit1: killed",
                "  m_hit2: survived",
                "  m_hit3: timeout",
                "  m_line: killed",
                "  m_noop: killed",
                "  m_noshow: killed",
                "  m_zpending: not checked",
                "  m_zpref: killed",
                "",
            ]
        ),
        {
            "m_file": _show_diff("b.py", "z = 3"),
            "m_ghost": _show_diff("ghost.py", "x = 1"),
            "m_hit1": _show_diff("a.py", "x = 1"),
            "m_hit2": _show_diff("a.py", "x = 1"),
            "m_hit3": _show_diff("a.py", "x = 1"),
            "m_line": _show_diff("a.py", "y = 2"),
            "m_noop": "--- a.py\n+++ a.py\n@@ -1 +1 @@\n context only\n",
            "m_zpref": "--- b/a.py\n+++ b/a.py\n@@ -1 +1 @@\n-x = 1\n+x = 2\n",
        },
    )
    monkeypatch.setenv("PATH", f"{stub_dir}{os.pathsep}{os.environ['PATH']}")
    changed = {
        (str(workdir / "a.py"), 1),
        (str(workdir / "a.py"), 3),
        (str(workdir / "ghost.py"), 1),
    }
    outcome = mutation_sample(workdir, changed, 10, test_files={"tests/test_a.py"})
    assert outcome == MutationOutcome(
        killed=3,
        total=4,
        generated=6,
        survivors=("m_hit2",),
        # T6-29c: the survivor is located in the caller's own spelling.
        survivor_lines=((str(workdir / "a.py"), 1),),
    )
    sampled = mutation_sample(workdir, changed, 2, test_files={"tests/test_a.py"})
    assert sampled == MutationOutcome(
        killed=1,
        total=2,
        generated=6,
        survivors=("m_hit2",),
        survivor_lines=((str(workdir / "a.py"), 1),),
    )


def _text_only_workdir(root: Path) -> Path:
    """Two string-literal lines and one numeric line, all mutable."""
    workdir = root / "work"
    (workdir / "tests").mkdir(parents=True)
    (workdir / "a.py").write_text('m = "hello"\nn = "world"\nq = 5\n')
    (workdir / "tests" / "test_a.py").write_text("def test_a():\n    assert True\n")
    return workdir


def _text_only_outcome(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> MutationOutcome:
    """One killed text-only mutant, one survived, one real change."""
    workdir = _text_only_workdir(tmp_path)
    stub_dir = tmp_path / "stub"
    stub_dir.mkdir()
    _stub_mutmut(
        stub_dir,
        "\n".join(["", "  m_killtext: killed", "  m_real: killed", "  m_survtext: survived", ""]),
        {
            "m_killtext": _show_diff("a.py", 'm = "hello"', 'm = "XXhelloXX"'),
            "m_survtext": _show_diff("a.py", 'n = "world"', 'n = "XXworldXX"'),
            "m_real": _show_diff("a.py", "q = 5", "q = 6"),
        },
    )
    monkeypatch.setenv("PATH", f"{stub_dir}{os.pathsep}{os.environ['PATH']}")
    changed = {(str(workdir / "a.py"), line) for line in (1, 2, 3)}
    return mutation_sample(workdir, changed, 10, test_files={"tests/test_a.py"})


def test_mutation_sample_counts_a_text_only_mutant_the_suite_killed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """F21.32: a killed text-only mutant is evidence the suite discriminates.

    `text_only_mutant` cannot tell a message from a currency code or a
    `Decimal` exponent -- round 3h excluded `currency == "XXJPYXX"` and
    `Decimal("XX1XX")`, both real behaviour changes the suite killed.
    Dropping them removed a kill from both sides of the ratio and cost
    the node the gate. The suite's verdict decides, not the shape.
    """
    outcome = _text_only_outcome(tmp_path, monkeypatch)
    assert outcome.killed == 2
    assert outcome.total == 2


def test_mutation_sample_still_excludes_a_text_only_mutant_that_survived(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """T6-33's purpose, kept: round 3d's 34 message survivors stay out.

    A survivor whose only change is string text cannot be killed by a
    spec-derived test, so it is not a missing test and does not count
    against the node.
    """
    outcome = _text_only_outcome(tmp_path, monkeypatch)
    assert outcome.survivors == ()
    assert "m_survtext" not in outcome.survivors


def test_mutmut_scratch_config_exact() -> None:
    assert _mutmut_scratch_config(["a.py", "tests/x.py"]) == (
        "[tool.mutmut]\n"
        'source_paths = ["a.py", "tests/x.py"]\n'
        'pytest_add_cli_args = ["-q", "-x", "-p", "no:cacheprovider"]\n'
    )


def test_mutmut_scratch_config_appends_run_tests_to_pytest_args() -> None:
    """`run_tests` are collection paths after the fixed flags, sorted (T3-3)."""
    assert _mutmut_scratch_config(["a.py"], run_tests={"test_z.py", "test_a.py"}) == (
        "[tool.mutmut]\n"
        'source_paths = ["a.py"]\n'
        'pytest_add_cli_args = ["-q", "-x", "-p", "no:cacheprovider", "test_a.py", "test_z.py"]\n'
    )


def test_mutmut_scratch_config_keeps_a_declared_scope_in_order() -> None:
    """F21.12a: a declared scope is a sequence and its `-k expr` pair
    survives; sorting would put `-k` before the path and split the pair."""
    assert _mutmut_scratch_config(["a.py"], run_tests=("tests/test_a.py", "-k", "slow")) == (
        "[tool.mutmut]\n"
        'source_paths = ["a.py"]\n'
        'pytest_add_cli_args = ["-q", "-x", "-p", "no:cacheprovider", '
        '"tests/test_a.py", "-k", "slow"]\n'
    )


def _without_stubbed_mutmut(monkeypatch: pytest.MonkeyPatch) -> None:
    """Drop the conftest `mutmut` stub from PATH so the real engine runs.

    Also restores the production `show_all_mutants` (P0-1): the autouse
    `_stub_mutmut` fixture points it at a PATH-stub replay that never
    touches a real mutmut installation's meta files, so a test using the
    real engine must undo that too, or the lookup finds nothing.
    """
    kept = [p for p in os.environ["PATH"].split(os.pathsep) if "mutmut-stub" not in p]
    monkeypatch.setenv("PATH", os.pathsep.join(kept))
    monkeypatch.setattr(evidence_module, "show_all_mutants", conftest._PRODUCTION_SHOW_ALL)
    assert shutil.which("mutmut") is not None, "the venv must be on PATH (CLAUDE.md)"


def test_mutation_sample_run_tests_restricts_which_tests_the_engine_runs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Real mutmut, the enforcing engine (CLAUDE.md): `test_files` does not
    choose what runs, `run_tests` does. Session 21's probe: a property with
    no discriminating power beside an example that has it, mutant
    `return 2 -> return 3`. With the example alone collected the mutant is
    killed; with the property alone it survives. Before T3-3 both calls
    reported the kill, because pytest always collected the whole tree.
    """
    _without_stubbed_mutmut(monkeypatch)
    workdir = tmp_path / "work"
    workdir.mkdir()
    (workdir / "n.py").write_text("def f():\n    return 2\n")
    (workdir / "test_prop.py").write_text(
        "from hypothesis import given, strategies as st\n"
        "from n import f\n\n\n"
        "@given(st.integers())\n"
        "def test_prop(_x):\n"
        "    assert isinstance(f(), int)\n"
    )
    (workdir / "test_ex.py").write_text("from n import f\n\n\ndef test_f():\n    assert f() == 2\n")
    changed = {(str(workdir / "n.py"), 2)}
    tests = {"test_prop.py", "test_ex.py"}
    example = mutation_sample(workdir, changed, 5, test_files=tests, run_tests={"test_ex.py"})
    assert example == MutationOutcome(killed=1, total=1, generated=1, survivors=())
    prop = mutation_sample(workdir, changed, 5, test_files=tests, run_tests={"test_prop.py"})
    assert prop == MutationOutcome(
        killed=0,
        total=1,
        generated=1,
        survivors=("n.x_f__mutmut_1",),
        survivor_lines=((str(workdir / "n.py"), 2),),
    )


def test_mutation_sample_scoped_run_baselines_past_a_red_sibling_specification(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """F21.12a against the real engine, round 3c's arms A and B. Known-bad:
    with no `run_tests` mutmut's stats run collects the red sibling
    specification and the tool fails before any mutant is judged.
    Known-good: the declared scope alone baselines and kills.
    """
    _without_stubbed_mutmut(monkeypatch)
    workdir = tmp_path / "w"
    workdir.mkdir()
    (workdir / "n.py").write_text("def f():\n    return 2\n")
    (workdir / "test_n.py").write_text("from n import f\n\n\ndef test_f():\n    assert f() == 2\n")
    (workdir / "test_later.py").write_text("def test_later_module():\n    assert False\n")
    changed = {(str(workdir / "n.py"), 2)}
    tests = {"test_n.py", "test_later.py"}
    unscoped = mutation_sample(workdir, changed, 5, test_files=tests)
    assert unscoped.total == 0
    assert unscoped.survivors == (
        "mutmut run exited 1: failed to collect stats. runner returned 1",
    )
    scoped = mutation_sample(workdir, changed, 5, test_files=tests, run_tests=("test_n.py",))
    assert scoped == MutationOutcome(killed=1, total=1, generated=1, survivors=())


def test_mutation_sample_names_a_red_suite_instead_of_blaming_the_tool(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """T6-63. mutmut exits 1 both when the engine is broken and when the
    node's own suite is red, and every red suite was read as a broken
    engine: round 3c's three impl attempts all died on "failed to collect
    stats" (runner.py says so at the call site) and the brief handed the
    worker a tool failure it could not act on.

    Known-bad: the suite the tests gate just ran was red, so the outcome
    names the suite and not the engine. Known-good: the same mutmut exit
    with a suite that passed still names the engine -- T3-20's contract,
    which the red-sibling test above exercises unchanged.
    """
    _without_stubbed_mutmut(monkeypatch)
    workdir = tmp_path / "w"
    workdir.mkdir()
    (workdir / "n.py").write_text("def f():\n    return 2\n")
    (workdir / "test_n.py").write_text("from n import f\n\n\ndef test_f():\n    assert f() == 3\n")
    changed = {(str(workdir / "n.py"), 2)}
    tests = {"test_n.py"}
    tool = "mutmut run exited 1: failed to collect stats. runner returned 1"
    red = mutation_sample(workdir, changed, 5, test_files=tests, suite_passed=False)
    assert red.total == 0
    assert red.survivors == (f"suite is red: {tool}",)
    blamed = mutation_sample(workdir, changed, 5, test_files=tests, suite_passed=True)
    assert blamed.survivors == (tool,)


def test_property_modules_selects_property_bearing_modules_that_import_a_change() -> None:
    """Known-good: a `@given` module importing the changed module, by stem,
    by dotted path, or by package `__init__`. Known-bad: a property over
    an unrelated import, examples only over the changed module, and an
    unparseable file.
    """
    given = "from hypothesis import given\nfrom hypothesis import strategies as st\n"
    sources = {
        "test_n.py": given
        + "from n import f\n@given(st.integers())\ndef test_p(x):\n    assert f()\n",
        "tests/test_pkg.py": given
        + "import pkg.m\n@given(st.integers())\ndef test_q(x):\n    assert pkg.m.g()\n",
        "test_init.py": given
        + "from pkg import g\n@given(st.text())\ndef test_r(x):\n    assert g()\n",
        "test_os.py": given + "import os\n@given(st.integers())\ndef test_s(x):\n    assert os\n",
        "test_rel.py": given
        + "from . import n\n@given(st.integers())\ndef test_u(x):\n    assert n\n",
        "test_examples.py": "from n import f\ndef test_t():\n    assert f() == 2\n",
        "test_broken.py": "def broken( :\n",
    }
    changed = ["/w/n.py", "/w/pkg/m.py"]
    # `test_rel.py` reaches `n` through a relative import (no module name).
    assert list(property_modules(sources, changed)) == [
        "test_n.py",
        "test_rel.py",
        "tests/test_pkg.py",
    ]
    assert property_modules(sources, changed)["test_n.py"] == sources["test_n.py"]
    assert list(property_modules(sources, ["/w/pkg/__init__.py"])) == ["test_init.py"]
    # `import os` matches only a changed `os.py`; a stem inside a longer name does not.
    assert property_modules(sources, ["/w/os.py"]) == {"test_os.py": sources["test_os.py"]}
    assert property_modules(sources, ["/w/xn.py", "/w/pkgm.py"]) == {}
    assert property_modules({}, changed) == {}


def test_pytest_scope_is_the_arguments_after_pytest() -> None:
    """F21.12a. Known-good: the declared command's arguments after `pytest`,
    in order, whatever precedes the module (`coverage run -m pytest`).
    Known-bad: a command that never names pytest yields nothing, so the
    engine runs its whole tree as before, and `pytest` itself is never
    an argument.
    """
    assert pytest_scope("pytest tests/test_accounts.py tests/test_fees.py") == (
        "tests/test_accounts.py",
        "tests/test_fees.py",
    )
    assert pytest_scope("coverage run -m pytest -q 'tests/te st.py'") == ("-q", "tests/te st.py")
    assert pytest_scope("pytest") == ()
    assert pytest_scope("python -m nose tests") == ()


def test_scoped_targets_keeps_only_what_the_declared_scope_collects() -> None:
    """F21.65. Known-good: a target the node's own pytest arguments would
    collect survives -- by exact path, and by directory prefix, since a
    directory argument collects everything under it. Known-bad: a target
    outside every argument is dropped, which is the whole point (the
    oracle ran a later node's red specification and could never baseline).

    Two shapes that must NOT filter, because neither narrows what pytest
    collects: no arguments at all (pytest's whole tree, which is what a
    command not naming pytest yields), and arguments that are only flags.
    A `path::name` selector scopes by its path -- the module is collected
    either way, and the oracle's own selection decides the rest.
    """
    targets = ("tests/test_accounts.py", "tests/test_fees.py", "tests/test_store.py")
    assert scoped_targets(targets, ("tests/test_accounts.py", "tests/test_fees.py")) == (
        "tests/test_accounts.py",
        "tests/test_fees.py",
    )
    assert scoped_targets(targets, ("tests/",)) == targets
    assert scoped_targets(targets, ("tests",)) == targets
    assert scoped_targets(targets, ()) == targets
    assert scoped_targets(targets, ("-q", "--no-cov")) == targets
    assert scoped_targets(targets, ("tests/test_fees.py::test_apply_fee",)) == (
        "tests/test_fees.py",
    )
    assert scoped_targets(targets, ("tests/test_report.py",)) == ()
    # A prefix that is not a path boundary is not a parent directory.
    assert scoped_targets(("tests_extra/test_x.py",), ("tests",)) == ()


def test_mutation_sample_invocation_shape(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The collector invokes mutmut with exact argv, scratch cwd, and recorder.

    A recording fake (not the shell stub) observes the invocation shape the
    stub cannot see: exact argv per call, a saddle-mutation- scratch cwd
    carrying the config and deep tree but no __pycache__, and recorder
    pass-through on every call. Updated pin (P0-1), not a flip: the third
    call used to be a per-name `mutmut show`; this task removes that call
    and replaces it with the one batched lookup subprocess, so the pin now
    names that call instead -- `sys.executable -c <script carrying the
    saddle-mutant-lookup marker>` -- while `show_all_mutants` itself is
    restored to production so the fake `run_capture` actually sees it.
    """
    workdir = _mutation_workdir(tmp_path)
    monkeypatch.setattr(evidence_module, "show_all_mutants", conftest._PRODUCTION_SHOW_ALL)
    calls: list[tuple[tuple[str, ...], Path | None, SpanRecorder | None]] = []

    def fake(
        argv: Sequence[str], cwd: Path | None, *, recorder: SpanRecorder | None = None
    ) -> CapturedRun:
        assert cwd is not None
        assert cwd.name.startswith("saddle-mutation-")
        assert (cwd / "pyproject.toml").is_file()
        assert not (cwd / "__pycache__").exists()
        assert (cwd / "src" / "deep" / "nested" / "deep.py").is_file()
        calls.append((tuple(argv), cwd, recorder))
        if list(argv[:2]) == ["mutmut", "results"]:
            return CapturedRun(argv=tuple(argv), exit_code=0, stdout="  m1: killed\n", stderr="")
        if argv[0] == sys.executable and any("saddle-mutant-lookup" in part for part in argv):
            return CapturedRun(
                argv=tuple(argv),
                exit_code=0,
                stdout=json.dumps({"m1": _show_diff("a.py", "x = 1")}),
                stderr="",
            )
        return CapturedRun(argv=tuple(argv), exit_code=0, stdout="", stderr="")

    monkeypatch.setattr(evidence_module, "run_capture", fake)
    monkeypatch.setattr(shutil, "which", lambda name: f"/fake/{name}")
    rec = SpanRecorder(path=tmp_path / "spans.jsonl", node_id="n1")
    outcome = mutation_sample(
        workdir, {(str(workdir / "a.py"), 1)}, 10, test_files=set(), recorder=rec
    )
    assert outcome == MutationOutcome(killed=1, total=1, generated=1, survivors=())
    calls_argv = [argv for argv, _, _ in calls]
    assert calls_argv[:2] == [
        ("timeout", str(_MUTATION_TIMEOUT_S), "mutmut", "run"),
        ("mutmut", "results", "--all", "True"),
    ]
    assert len(calls_argv) == 3
    lookup_argv = calls_argv[2]
    assert lookup_argv[0] == sys.executable
    assert any("saddle-mutant-lookup" in part for part in lookup_argv)
    cwds = [cwd for _, cwd, _ in calls]
    assert cwds[0] is not None
    assert all(cwd == cwds[0] for cwd in cwds)
    assert [item is rec for _, _, item in calls] == [True, True, True]


def test_mutation_sample_missing_tool_fails_closed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    workdir = _mutation_workdir(tmp_path)
    monkeypatch.setenv("PATH", str(tmp_path / "empty"))
    outcome = mutation_sample(workdir, {(str(workdir / "a.py"), 1)}, 10, test_files=set())
    assert outcome == MutationOutcome(
        killed=0, total=0, generated=0, survivors=("mutmut not on PATH",)
    )


def test_mutation_sample_timeout_yields_undecided(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    workdir = _mutation_workdir(tmp_path)
    stub_dir = tmp_path / "stub"
    stub_dir.mkdir()
    _stub_mutmut(stub_dir, "  m1: not checked\n", {}, run_body="sleep 5")
    monkeypatch.setenv("PATH", f"{stub_dir}{os.pathsep}{os.environ['PATH']}")
    outcome = mutation_sample(
        workdir, {(str(workdir / "a.py"), 1)}, 10, test_files=set(), timeout_s=1
    )
    assert outcome == MutationOutcome(killed=0, total=0, generated=1, survivors=())


def test_mutation_sample_vacuous_without_changes(tmp_path: Path) -> None:
    workdir = _mutation_workdir(tmp_path)
    assert mutation_sample(workdir, set(), 10, test_files=set()) == MutationOutcome(
        killed=0, total=0, generated=0, survivors=()
    )


def test_mutation_sample_vacuous_without_production(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    workdir = tmp_path / "work"
    (workdir / "tests").mkdir(parents=True)
    (workdir / "tests" / "test_a.py").write_text("def test_a():\n    assert True\n")
    stub_dir = tmp_path / "stub"
    stub_dir.mkdir()
    _stub_mutmut(stub_dir, "", {}, run_body=f"touch {stub_dir}/ran && exit 0")
    monkeypatch.setenv("PATH", f"{stub_dir}{os.pathsep}{os.environ['PATH']}")
    changed = {(str(workdir / "tests" / "test_a.py"), 1)}
    outcome = mutation_sample(workdir, changed, 10, test_files={"tests/test_a.py"})
    assert outcome == MutationOutcome(killed=0, total=0, generated=0, survivors=())
    assert not (stub_dir / "ran").exists()


def test_mutation_sample_vacuous_when_no_mutants(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    workdir = _mutation_workdir(tmp_path)
    stub_dir = tmp_path / "stub"
    stub_dir.mkdir()
    _stub_mutmut(stub_dir, "\n", {})
    monkeypatch.setenv("PATH", f"{stub_dir}{os.pathsep}{os.environ['PATH']}")
    outcome = mutation_sample(workdir, {(str(workdir / "a.py"), 1)}, 10, test_files=set())
    assert outcome == MutationOutcome(killed=0, total=0, generated=0, survivors=())


def test_mutation_sample_skips_test_file_mutants(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    workdir = _mutation_workdir(tmp_path)
    stub_dir = tmp_path / "stub"
    stub_dir.mkdir()
    _stub_mutmut(
        stub_dir,
        "  m_atest: killed\n  m_prod: killed\n",
        {
            "m_atest": _show_diff("tests/test_a.py", "def test_a():"),
            "m_prod": _show_diff("a.py", "x = 1"),
        },
    )
    monkeypatch.setenv("PATH", f"{stub_dir}{os.pathsep}{os.environ['PATH']}")
    changed = {(str(workdir / "a.py"), 1), (str(workdir / "tests" / "test_a.py"), 1)}
    outcome = mutation_sample(workdir, changed, 10, test_files={"tests/test_a.py"})
    assert outcome == MutationOutcome(killed=1, total=1, generated=1, survivors=())


# --- P0-1: batch the mutant lookup ------------------------------------------
#
# `mutation_sample` used to run one `mutmut show NAME` subprocess per decided
# mutant; it now runs one `show_all_mutants` lookup subprocess per call. The
# tests below are the three known-good/known-bad pairs the task names, plus
# direct coverage of `show_all_mutants`'s own failure branches.


def _mutant_shapes_workdir(root: Path) -> Path:
    """A tiny real tree with each mutant shape P0-1 names: a module-level
    function, a method, a string-literal-only mutant, and a second module
    ("c.py") no test imports (its mutants come back "no tests")."""
    workdir = root / "work"
    workdir.mkdir()
    (workdir / "a.py").write_text(
        "def f():\n    return 2\n\n\nclass C:\n    def m(self):\n        return 3\n"
    )
    (workdir / "b.py").write_text('def g():\n    return "hello"\n')
    (workdir / "c.py").write_text("def h():\n    return 5\n")
    (workdir / "test_a.py").write_text(
        "from a import f, C\n"
        "\n"
        "\n"
        "def test_f():\n"
        "    assert f() == 2\n"
        "\n"
        "\n"
        "def test_m():\n"
        "    assert C().m() == 3\n"
    )
    (workdir / "test_b.py").write_text(
        'from b import g\n\n\ndef test_g():\n    assert g() == "hello"\n'
    )
    return workdir


def _build_real_scratch(workdir: Path, scratch: Path, tests: set[str]) -> None:
    """Copy `workdir`'s .py files into `scratch` and run real `mutmut run`,
    exactly the shape `mutation_sample` builds internally."""
    production = []
    for source in sorted(workdir.rglob("*.py")):
        dest = scratch / source.relative_to(workdir)
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, dest)
        rel = source.relative_to(workdir).as_posix()
        if rel not in tests:
            production.append(rel)
    (scratch / "pyproject.toml").write_text(_mutmut_scratch_config(production))
    ran = subprocess.run(["mutmut", "run"], cwd=scratch, capture_output=True, text=True)
    assert ran.returncode == 0, ran.stdout + ran.stderr


def test_show_all_mutants_matches_mutmut_show_byte_for_byte(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Known-good (P0-1), real engine (CLAUDE.md): `show_all_mutants` is
    byte-identical to `mutmut show NAME` for every name `mutmut results
    --all True` lists, across a module-level function, a method, a
    string-literal mutant and a module no test imports ("no tests").
    """
    _without_stubbed_mutmut(monkeypatch)
    workdir = _mutant_shapes_workdir(tmp_path)
    tests = {"test_a.py", "test_b.py"}
    scratch = tmp_path / "scratch"
    scratch.mkdir()
    _build_real_scratch(workdir, scratch, tests)

    results = subprocess.run(
        ["mutmut", "results", "--all", "True"], cwd=scratch, capture_output=True, text=True
    )
    names = sorted(set(_ALL_MUTANT_NAMES.findall(results.stdout)))
    assert names, "fixture produced no mutants to compare"

    mapping = show_all_mutants(scratch)
    assert set(mapping) == set(names)
    for name in names:
        expected = subprocess.run(
            ["mutmut", "show", name], cwd=scratch, capture_output=True, text=True
        ).stdout
        assert mapping[name] == expected


def test_mutation_sample_matches_the_per_name_replay_at_unit_scale(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Known-good (P0-1), real engine: `mutation_sample` gives an identical
    `MutationOutcome` whether the lookup is the production batched
    subprocess or the old per-name `mutmut show` loop -- "same verdicts"
    at the level `mutation_sample` itself operates.
    """
    _without_stubbed_mutmut(monkeypatch)
    workdir = _mutant_shapes_workdir(tmp_path)
    tests = {"test_a.py", "test_b.py"}
    changed = {
        (str(workdir / "a.py"), 2),
        (str(workdir / "a.py"), 7),
        (str(workdir / "b.py"), 2),
        (str(workdir / "c.py"), 2),
    }
    production = mutation_sample(workdir, changed, 10, test_files=tests)
    monkeypatch.setattr(evidence_module, "show_all_mutants", conftest._replay_show_all_mutants)
    replayed = mutation_sample(workdir, changed, 10, test_files=tests)
    assert production == replayed
    assert production.total > 0


def test_mutation_sample_uses_one_lookup_subprocess_not_one_per_mutant(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Known-bad (P0-1), real engine: a per-mutant `mutmut show` subprocess
    must not reappear. A recording spy wraps the real `run_capture` (not a
    fake) so this exercises the actual mutmut subprocess protocol. Must
    fail on d04ede8, on the `mutmut show` calls (red-first, recorded in
    the report).
    """
    _without_stubbed_mutmut(monkeypatch)
    workdir = _mutant_shapes_workdir(tmp_path)
    tests = {"test_a.py", "test_b.py"}
    changed = {(str(workdir / "a.py"), 2), (str(workdir / "a.py"), 7)}

    calls: list[tuple[str, ...]] = []
    real_run_capture = evidence_module.run_capture

    def spy(
        argv: Sequence[str],
        cwd: Path,
        *,
        recorder: SpanRecorder | None = None,
        timeout: float | None = None,
    ) -> CapturedRun:
        calls.append(tuple(argv))
        return real_run_capture(argv, cwd, recorder=recorder, timeout=timeout)

    monkeypatch.setattr(evidence_module, "run_capture", spy)
    journal = tmp_path / "spans.jsonl"
    rec = SpanRecorder(path=journal, node_id="n1")
    outcome = mutation_sample(workdir, changed, 10, test_files=tests, recorder=rec)
    assert outcome.total > 0

    show_calls = [c for c in calls if c[:2] == ("mutmut", "show")]
    assert show_calls == []
    lookup_calls = [c for c in calls if any("saddle-mutant-lookup" in part for part in c)]
    assert len(lookup_calls) == 1

    spans = read_spans(journal)
    lookup_spans = [s for s in spans if any("saddle-mutant-lookup" in part for part in s.argv)]
    assert len(lookup_spans) == 1


def test_mutation_sample_names_a_failed_lookup_instead_of_reading_no_mutants(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Known-bad (P0-1), real engine: a batched-lookup subprocess failure is
    named, never silently read as "no mutants" (CLAUDE.md: a lookup
    failure must never read as absence, per T3-20's rule for `mutmut
    run`). `sys.executable` is the seam `show_all_mutants` shells out
    through for the lookup; breaking it is harmless to `mutmut run` and
    `mutmut results`, both spawned by bare name via PATH and each its own
    OS process with its own interpreter, so the same test also runs
    unmodified on d04ede8 -- there it fails this assertion instead (the
    old code never touches `sys.executable`), which is this pair's
    red-first record.
    """
    _without_stubbed_mutmut(monkeypatch)
    workdir = tmp_path / "work"
    workdir.mkdir()
    (workdir / "n.py").write_text("def f():\n    return 2\n")
    (workdir / "test_n.py").write_text("from n import f\n\n\ndef test_f():\n    assert f() == 2\n")
    changed = {(str(workdir / "n.py"), 2)}
    monkeypatch.setattr(sys, "executable", "/bin/false")
    outcome = mutation_sample(workdir, changed, 5, test_files={"test_n.py"})
    assert outcome.total == 0
    assert len(outcome.survivors) == 1
    assert outcome.survivors[0].startswith("mutant lookup failed: ")


def test_show_all_mutants_raises_on_a_nonzero_lookup_exit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`show_all_mutants` names a lookup subprocess failure instead of
    returning a mapping it never got."""
    monkeypatch.setattr(
        evidence_module,
        "run_capture",
        lambda *a, **k: CapturedRun(argv=("x",), exit_code=1, stdout="", stderr="boom\nlast line"),
    )
    with pytest.raises(MutantLookupError, match=r"exit 1: last line"):
        show_all_mutants(tmp_path)


def test_show_all_mutants_raises_on_unparseable_stdout(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        evidence_module,
        "run_capture",
        lambda *a, **k: CapturedRun(argv=("x",), exit_code=0, stdout="not json", stderr=""),
    )
    with pytest.raises(MutantLookupError, match=r"exit 0: no output"):
        show_all_mutants(tmp_path)


def test_show_all_mutants_raises_when_stdout_is_not_a_string_mapping(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        evidence_module,
        "run_capture",
        lambda *a, **k: CapturedRun(argv=("x",), exit_code=0, stdout="[1, 2]", stderr=""),
    )
    with pytest.raises(MutantLookupError, match=r"exit 0: no output"):
        show_all_mutants(tmp_path)


# P0-2: `_mutant_lines` used to match a removed hunk line against the
# *whole file*. A method's own line never matched (mutmut renders the
# extracted def at column 0, so the line is dedented one level relative to
# the file); an identical line anywhere else in the file matched instead,
# so `survivor_lines` could name the wrong function; and a continuation
# line of a multi-line statement could never intersect `changed`, which
# only ever holds first lines (`statement_lines`). The three pairs below
# are the spec's own minimal reproduction (P0-2-locator.md), against the
# real engine (CLAUDE.md): a module-level `f`/`g` sharing a body line, a
# method `K.m`, and a multi-line `return sum([a, b + 1])` inside `total`.


def _locator_workdir(root: Path) -> Path:
    """`a.py`/`tests/test_a.py`, byte-for-byte the spec's tree (line numbers
    2, 11, 15 and 16 are named in the spec and must not move)."""
    workdir = root / "work"
    (workdir / "tests").mkdir(parents=True)
    (workdir / "a.py").write_text(
        "def f(x):\n"
        "    return x + 1\n"
        "\n"
        "\n"
        "def g(x):\n"
        "    return x + 1\n"
        "\n"
        "\n"
        "class K:\n"
        "    def m(self, y):\n"
        "        return y * 3\n"
        "\n"
        "\n"
        "def total(a, b):\n"
        "    return sum(\n"
        "        [a, b + 1]\n"
        "    )\n"
    )
    (workdir / "tests" / "test_a.py").write_text(
        "from a import K, f, g, total\n"
        "\n"
        "\n"
        "def test_f():\n"
        "    assert f(1) == 2\n"
        "\n"
        "\n"
        "def test_g_runs():\n"
        "    g(1)  # runs g, checks nothing: g's mutants survive\n"
        "\n"
        "\n"
        "def test_m():\n"
        "    assert K().m(2) == 6\n"
        "\n"
        "\n"
        "def test_total():\n"
        "    assert total(1, 2) == 4\n"
    )
    return workdir


def test_mutation_sample_excludes_a_survivor_located_only_via_duplicate_text(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Known-bad pair 1 (P0-2-locator.md): `changed` names only `f`'s line
    (2), and `g` has an identical body line (6) but no discriminating
    test. Before this task the whole-file match let `g`'s survivors in,
    located on `f`'s line -- must fail on be99efe (red-first, recorded in
    the report). After: `g`'s mutants stay out and `survivor_lines` holds
    only `f`'s own line.
    """
    _without_stubbed_mutmut(monkeypatch)
    workdir = _locator_workdir(tmp_path)
    changed = {(str(workdir / "a.py"), 2)}
    outcome = mutation_sample(workdir, changed, 100, test_files={"tests/test_a.py"})
    assert not any(name.startswith("a.x_g__mutmut_") for name in outcome.survivors)
    assert all(line == 2 for _, line in outcome.survivor_lines)
    assert outcome.total == 2
    assert outcome.killed == 2


def test_mutation_sample_admits_a_method_mutant_on_its_own_changed_line(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Known-good pair 2: `changed` names `K.m`'s own body line (11). Before
    this task a method mutant never matched its own dedented line, so
    `total == 0`; every real `K.xǁKǁm__mutmut_N` mutant now enters the
    population and `test_m` kills all of them.
    """
    _without_stubbed_mutmut(monkeypatch)
    workdir = _locator_workdir(tmp_path)
    changed = {(str(workdir / "a.py"), 11)}
    outcome = mutation_sample(workdir, changed, 100, test_files={"tests/test_a.py"})
    assert outcome.total > 0
    assert outcome.killed == outcome.total
    assert outcome.survivors == ()


def test_mutation_sample_admits_a_continuation_line_mutant_via_its_statement(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Known-good pair 3: `changed` names line 15, `return sum(`'s own first
    line -- `statement_lines`'s coordinate for the whole multi-line
    return. Before this task a mutant on line 16 (`[a, b + 1]`, a
    continuation line) could never intersect `changed`, so `total == 0`;
    it now maps to line 15 and enters the population.
    """
    _without_stubbed_mutmut(monkeypatch)
    workdir = _locator_workdir(tmp_path)
    changed = {(str(workdir / "a.py"), 15)}
    outcome = mutation_sample(workdir, changed, 100, test_files={"tests/test_a.py"})
    assert outcome.total > 0
    assert outcome.killed == outcome.total
    assert outcome.survivors == ()


def test_mutant_name_parses_like_mutmuts_own_orig_names_from_key() -> None:
    """`_MUTANT_NAME`'s local parse (P0-2: no import of mutmut into
    saddle's process at runtime) agrees with mutmut's own
    `orig_function_and_class_names_from_key`, used here only as the test
    oracle, across both production shapes: a function, a method, a
    private-looking function name (`_to_decimal`), a dunder method
    (`__eq__`/`__init__`), and a dotted package path.
    """
    from mutmut.utils.format_utils import orig_function_and_class_names_from_key

    names = [
        "a.x_f__mutmut_1",
        "a.x_g__mutmut_2",
        "a.x__to_decimal__mutmut_9",
        "a.xǁKǁm__mutmut_1",
        "accounts.xǁAccountǁ__init____mutmut_10",
        "accounts.xǁAccountǁ__eq____mutmut_2",
        "pkg.sub.x_helper__mutmut_3",
        "pkg.sub.xǁCǁ_private__mutmut_1",
    ]
    for name in names:
        expected_func, expected_cls = orig_function_and_class_names_from_key(name)
        match = evidence_module._MUTANT_NAME.match(name)
        assert match is not None, name
        func = match.group("func") or match.group("method")
        assert (func, match.group("cls")) == (expected_func, expected_cls), name


def test_mutant_lines_a_stub_name_keeps_the_old_whole_file_match(tmp_path: Path) -> None:
    """A name that does not match `_MUTANT_NAME` (a hand-built test stub,
    e.g. `m1`) is not a production mutmut name and takes the old
    whole-file exact match, byte for byte, with no statement mapping --
    the conftest autouse stub's tests all use names of this shape and
    must not move.
    """
    source = "x = 1\ny = 1\n"
    show = "--- a.py\n+++ a.py\n@@ -1 +1 @@\n-x = 1\n+x = 2\n"
    assert _mutant_lines(show, source, "m1") == {1}


def test_mutant_lines_a_method_locates_inside_its_own_class_never_the_function() -> None:
    """M-L5: a method and a same-named module-level function must not
    collide -- the class scopes the lookup, not the name alone. The two
    names below share the exact same `show` text (mutmut renders both
    defs' single-line bodies identically once dedented to column 0);
    only the name's class tag picks which `def` it locates inside.
    """
    source = (
        "def f(x):\n    return x + 1\n\n\nclass K:\n    def f(self, x):\n        return x + 1\n"
    )
    show = (
        "--- a.py\n+++ a.py\n@@ -1,2 +1,2 @@\n"
        " def f(self, x):\n-    return x + 1\n+    return x + 2\n"
    )
    assert _mutant_lines(show, source, "a.x_f__mutmut_1") == {2}
    assert _mutant_lines(show, source, "a.xǁKǁf__mutmut_1") == {7}


def test_mutant_lines_reindents_two_space_class_bodies() -> None:
    """A non-default (2-space) indentation still locates correctly: the
    dedent mutmut applies is the `def` line's own leading whitespace, not
    a fixed 4 spaces."""
    source = "class K:\n  def m(self):\n    return 1\n"
    show = "--- a.py\n+++ a.py\n@@ -1,2 +1,2 @@\n def m(self):\n-  return 1\n+  return 2\n"
    assert _mutant_lines(show, source, "a.xǁKǁm__mutmut_1") == {3}


def test_mutant_lines_a_decorated_functions_range_starts_at_the_decorator() -> None:
    """The search range runs from the first decorator line, not the `def`
    line (P0-2-locator.md), so a mutant on the decorator itself (mutmut
    can mutate a decorator call's own arguments) still matches -- and
    maps to the `def` line, `statement_lines`'s own coordinate for the
    whole decorated function, since no nested statement covers a
    decorator's line (`ast.FunctionDef.lineno` is the `def` line).
    """
    source = "@deco(1)\ndef f():\n    return 1\n"
    show = "--- a.py\n+++ a.py\n@@ -1,3 +1,3 @@\n-@deco(1)\n+@deco(2)\n def f():\n     return 1\n"
    assert _mutant_lines(show, source, "a.x_f__mutmut_1") == {2}


def test_mutant_lines_unknown_class_in_the_name_returns_empty() -> None:
    """`_mutant_def`'s class lookup can miss even though `mutant_name`
    parses: a name shaped like a production method mutant but naming a
    class absent from `source` (a stale name against a rewritten file)
    fails closed to no lines, never the whole-file fallback -- that
    fallback is reserved for a name `_MUTANT_NAME` cannot parse at all.
    """
    source = "class K:\n    def m(self):\n        return 1\n"
    show = "--- a.py\n+++ a.py\n@@ -1 +1 @@\n-        return 1\n+        return 2\n"
    assert _mutant_lines(show, source, "a.xǁZǁm__mutmut_1") == set()


def test_mutant_lines_unknown_function_in_the_name_returns_empty() -> None:
    """`_mutant_def`'s function-name lookup can also miss: a name naming a
    function absent from `source`'s top level fails closed the same way.
    """
    source = "def f():\n    return 1\n"
    show = "--- a.py\n+++ a.py\n@@ -1 +1 @@\n-    return 1\n+    return 2\n"
    assert _mutant_lines(show, source, "a.x_missing__mutmut_1") == set()


def test_mutant_lines_unparseable_source_with_a_parsed_name_returns_empty() -> None:
    """`ast.parse` can fail even though `mutant_name` parsed fine -- a
    stale or rewritten source. Fails closed, the same as an unresolved
    `def`."""
    source = "def f(:\n"
    show = "--- a.py\n+++ a.py\n@@ -1 +1 @@\n-    return 1\n+    return 2\n"
    assert _mutant_lines(show, source, "a.x_f__mutmut_1") == set()


def test_statement_start_keeps_the_first_best_on_a_tied_span() -> None:
    """`ast.walk` visits a parent before its child, so a one-line
    `if True: pass` gives the `If` and its `pass` the same span (0): the
    second candidate must not overwrite the first (branch coverage for
    the `best is None or span < best_span` guard's false arm).
    """
    tree = ast.parse("if True: pass\n")
    assert _statement_start(tree, 1) == 1


def test_statement_lines_skips_blanks_and_comments() -> None:
    source = "# a comment\n\nx = 1\n\n\ndef f():\n    return x\n"
    assert statement_lines(source) == {3, 6, 7}


def test_statement_lines_unparseable_yields_none() -> None:
    assert statement_lines("def broken(:\n") == set()


def test_statement_lines_exempts_docstrings_but_keeps_stray_strings() -> None:
    source = (
        '"""Module docstring."""\n'
        "\n"
        "x = 1\n"
        "\n"
        "\n"
        "def f():\n"
        '    """Function docstring."""\n'
        '    "not a docstring"\n'
        "    return x\n"
        "\n"
        "\n"
        "class C:\n"
        '    """Class docstring."""\n'
        "\n"
        "    def m(self):\n"
        '        """Method docstring."""\n'
        "        return 1\n"
        "\n"
        "\n"
        "async def g():\n"
        '    """Async docstring."""\n'
        "    return 2\n"
        "\n"
        "\n"
        "def numbers():\n"
        "    123\n"
        "    return 3\n"
        "\n"
        "\n"
        "def called():\n"
        '    print("hi")\n'
        "    return 4\n"
    )
    assert statement_lines(source) == {3, 6, 8, 9, 12, 15, 17, 20, 22, 25, 26, 27, 30, 31, 32}


def test_statement_lines_empty_source_yields_none() -> None:
    assert statement_lines("") == set()


def test_run_capture_hanging_command_times_out(tmp_path: Path) -> None:
    """A command that never returns must not hang the gate runner.

    T7's `test_op_add` did not fail, it looped forever: `x += x` called
    `extend(self)`, which iterated the backing list while inserting into
    it. Six of seven gates passed on that code because nothing in the
    stack could observe a test that simply never came back.
    """
    argv = [sys.executable, "-c", "import time; time.sleep(30)"]
    run = run_capture(argv, tmp_path, timeout=1.0)
    assert run.exit_code == SHELL_TIMEOUT
    assert run.timed_out is True


def test_run_argv_timeout_journals_the_killed_span(tmp_path: Path) -> None:
    """A killed run must leave a span; a hang that vanishes is worse than one
    that is recorded, because the transcript then shows no reason at all."""
    journal = tmp_path / "spans.jsonl"
    recorder = SpanRecorder(path=journal, node_id="n1")
    argv = [
        sys.executable,
        "-c",
        "import sys; sys.stderr.write('partial'); sys.stderr.flush()\nimport time; time.sleep(30)",
    ]

    assert run_argv(argv, tmp_path, recorder=recorder, timeout=1.0) == SHELL_TIMEOUT

    spans = read_spans(journal)
    assert len(spans) == 1
    assert spans[0].exit_code == SHELL_TIMEOUT
    assert "timed out after 1.0s" in spans[0].detail
    assert "partial" in spans[0].detail


def test_run_shell_paths_bound_the_command(tmp_path: Path) -> None:
    """The shell wrappers are what the runner actually calls for test
    commands; a timeout that stops at `run_capture` protects nothing."""
    hang = f"{sys.executable} -c 'import time; time.sleep(30)'"

    captured = run_shell_capture(hang, tmp_path, timeout=1.0)
    assert captured.exit_code == SHELL_TIMEOUT
    assert captured.timed_out is True

    assert run_shell(hang, tmp_path, timeout=1.0) == SHELL_TIMEOUT


def test_run_capture_missing_tool_does_not_raise(tmp_path: Path) -> None:
    """A missing gate binary must be a verdict, not an exception.

    T1 v2 failed with `Verdict: FAIL` and NO gate lines in the transcript;
    the only evidence anywhere was a journal span reading
    `[Errno 2] No such file or directory: 'coverage'`. A gate tool that
    raises instead of returning bypasses recovery and leaves the run with
    no stated reason (#34).
    """
    run = run_capture(["definitely-not-a-real-binary-xyz"], tmp_path)
    assert run.exit_code == TOOL_UNAVAILABLE
    assert "definitely-not-a-real-binary-xyz" in run.stderr
    assert run.timed_out is False


def test_run_argv_missing_tool_journals_the_reason(tmp_path: Path) -> None:
    """The reason a tool never launched has to reach the journal.

    #34's complaint was that the only trace of a missing `coverage` was a
    span detail; the fix is not to drop the span, it is to make the gate
    report it too. The span still has to carry the OS error.
    """
    journal = tmp_path / "spans.jsonl"
    recorder = SpanRecorder(path=journal, node_id="n1")

    assert run_argv(["no-such-binary-abc"], tmp_path, recorder=recorder) == TOOL_UNAVAILABLE

    spans = read_spans(journal)
    assert len(spans) == 1
    assert spans[0].exit_code == TOOL_UNAVAILABLE
    assert "no-such-binary-abc" in spans[0].detail


def test_mutation_sample_failed_run_names_the_tool(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """T3-20 known-bad: `mutmut run` exiting non-zero is the tool failing.

    The smoke run's mutmut 3.8 refused a package named `src` and exited 1
    in 658 ms; the outcome read "no mutants decided" because the run's exit
    was discarded and `results` (exit 0, empty) was trusted instead.
    """
    workdir = _mutation_workdir(tmp_path)
    stub_dir = tmp_path / "stub"
    stub_dir.mkdir()
    _stub_mutmut(
        stub_dir,
        "",
        {},
        run_body=(
            'echo "Running stats"; echo "AssertionError: Module name starts with src." >&2; exit 1'
        ),
    )
    monkeypatch.setenv("PATH", f"{stub_dir}{os.pathsep}{os.environ['PATH']}")
    outcome = mutation_sample(workdir, {(str(workdir / "a.py"), 1)}, 10, test_files=set())
    assert outcome == MutationOutcome(
        killed=0,
        total=0,
        generated=0,
        survivors=("mutmut run exited 1: AssertionError: Module name starts with src.",),
    )


def test_ruff_findings_parse_the_engine_and_render_human_lines(tmp_path: Path) -> None:
    """T6-3 against the real engine: the JSON run yields findings keyed by
    source line and a captured run whose stdout is one human line each;
    a clean file yields none and exit 0."""
    (tmp_path / "m.py").write_text("import os\n\nx = 1\n")
    run, findings = ruff_findings(tmp_path, ["m.py"])
    assert run.exit_code == 1
    assert run.argv == ("ruff", "check", "m.py")
    assert [(f.code, f.path, f.row, f.line) for f in findings] == [("F401", "m.py", 1, "import os")]
    assert run.stdout.startswith("m.py:1:8: F401 ")
    (tmp_path / "c.py").write_text("x = 1\n")
    run, findings = ruff_findings(tmp_path, ["c.py"])
    assert (run.exit_code, findings, run.stdout) == (0, [], "")


def test_ruff_gate_runs_isolated_on_saddle_rules_not_the_installed_default(tmp_path: Path) -> None:
    """T6-37 known-bad from round 3e (F21.16): `%`-formatting, the idiom the
    task's own baseline uses, failed the ruff gate under ruff 0.16.7's
    default rule set (UP031) although no config existed anywhere; a
    workdir config selecting everything must not reach the gate either.
    Known-good: an undefined name (F821) and a mutable default (B006) are
    still findings, and the argv spells the isolation and the selection."""
    (tmp_path / "pyproject.toml").write_text('[tool.ruff.lint]\nselect = ["ALL"]\n')
    (tmp_path / "m.py").write_text(
        'def show(owner, balance):\n    return "Account(owner=%r, balance=%r)" % (owner, balance)\n'
    )
    run, findings = ruff_findings(tmp_path, ["m.py"])
    assert (run.exit_code, findings) == (0, [])
    (tmp_path / "d.py").write_text("def f(items=[]):\n    return missing + items\n")
    _, findings = ruff_findings(tmp_path, ["d.py"])
    assert sorted(f.code for f in findings) == ["B006", "F821"]
    assert ruff_argv("check", "--output-format", "json", "d.py") == [
        "ruff",
        "check",
        "--isolated",
        "--select",
        "F,E4,E7,E9,B",
        "--output-format",
        "json",
        "d.py",
    ]
    assert ruff_argv("format", "--check", "d.py") == [
        "ruff",
        "format",
        "--isolated",
        "--check",
        "d.py",
    ]


def test_ruff_version_reads_the_engine_and_says_when_it_cannot(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    version = ruff_version()
    assert version.count(".") == 2, version
    engine = subprocess.run(["ruff", "--version"], capture_output=True, text=True, check=False)
    assert version == engine.stdout.split()[1]

    def missing(*_args: object, **_kwargs: object) -> object:
        msg = "ruff"
        raise FileNotFoundError(msg)

    monkeypatch.setattr(subprocess, "run", missing)
    assert ruff_version() == "unavailable"

    class Odd:
        returncode = 0
        stdout = "ruff"

    monkeypatch.setattr(subprocess, "run", lambda *_a, **_k: Odd())
    assert ruff_version() == "unavailable"


def test_ruff_findings_tolerates_bad_json_and_unreadable_sources(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """T6-3 edges: output that is not JSON yields no findings (the exit code
    still says the tool failed); a non-object entry is skipped; a finding
    whose file cannot be read carries an empty source line."""
    import saddle.evidence as evidence_module

    def fake_run(argv: list[str], cwd: Path, *, recorder: object = None) -> CapturedRun:
        return CapturedRun(argv=tuple(argv), exit_code=2, stdout=fake_run.stdout, stderr="boom")  # type: ignore[attr-defined]

    monkeypatch.setattr(evidence_module, "run_capture", fake_run)
    fake_run.stdout = "not json"  # type: ignore[attr-defined]
    run, findings = ruff_findings(Path("/w"), ["m.py"])
    assert (run.exit_code, findings, run.stdout, run.stderr) == (2, [], "", "boom")
    fake_run.stdout = (  # type: ignore[attr-defined]
        '[1, {"code": "E999", "filename": "/w/gone.py", "message": "m", "location": {"row": 3}}]'
    )
    run, findings = ruff_findings(Path("/w"), ["gone.py"])
    assert [(f.code, f.path, f.row, f.line, f.column) for f in findings] == [
        ("E999", "gone.py", 3, "", 0)
    ]
    assert run.stdout == "gone.py:3:0: E999 m"


def test_text_only_mutant_classifies_string_edits_and_nothing_else() -> None:
    """T6-33 known-good: a mutant that changes only the text inside a string
    literal (a message, an f-string body) is text-only. Known-bad: a value
    change, an operator change, a string that gains an operand, a line that
    does not tokenize on its own, an empty or unbalanced diff."""

    def show(old: str, new: str) -> str:
        return f"--- a/m.py\n+++ b/m.py\n@@ -1 +1 @@\n-{old}\n+{new}\n"

    assert text_only_mutant(
        show('raise ValueError("cannot convert")', 'raise ValueError("XXcannot convertXX")')
    )
    assert text_only_mutant(show('x = "a"', "x = 'b'"))
    assert text_only_mutant(show('msg = f"bad {value}"', 'msg = f"XXbad {value}XX"'))
    assert not text_only_mutant(show("x = 1", "x = 2"))
    assert not text_only_mutant(show("return a + b", "return a - b"))
    assert not text_only_mutant(show('x = "a"', 'x = "a" + y'))
    assert not text_only_mutant(show('x = "a"', "x = None"))
    assert not text_only_mutant(show('s = """start', 's = """other'))
    assert not text_only_mutant("--- a/m.py\n+++ b/m.py\n@@ -1 +1 @@\n-x = 1\n")
    assert not text_only_mutant(show("x = 1", "x = 1"))


def test_mutation_sample_leaves_text_only_mutants_out_of_the_population(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """T6-33 end to end on the stub engine: two survivors, one a message edit,
    one a value change. The text-only one leaves the population (total
    and survivors exclude it) and is counted; the behavioural one stays."""
    workdir = _mutation_workdir(tmp_path)
    (workdir / "a.py").write_text('x = 1\nmsg = "bad"\n')
    stub_dir = tmp_path / "stub"
    stub_dir.mkdir()
    _stub_mutmut(
        stub_dir,
        "\n".join(["", "  m_text: survived", "  m_value: survived", "  m_kill: killed", ""]),
        {
            "m_text": _show_diff("a.py", 'msg = "bad"', 'msg = "XXbadXX"'),
            "m_value": _show_diff("a.py", "x = 1", "x = 2"),
            "m_kill": _show_diff("a.py", "x = 1", "x = 3"),
        },
    )
    monkeypatch.setenv("PATH", f"{stub_dir}{os.pathsep}{os.environ['PATH']}")
    changed = {(str(workdir / "a.py"), 1), (str(workdir / "a.py"), 2)}
    outcome = mutation_sample(workdir, changed, 10, test_files=set())
    assert outcome == MutationOutcome(
        killed=1,
        total=2,
        generated=2,
        survivors=("m_value",),
        text_only=1,
        survivor_lines=((str(workdir / "a.py"), 1),),
    )


def _git_repo_that_refuses_to_guess(root: Path) -> None:
    """A repo git will not invent an author identity for.

    Round 3h died here 16 ms into its slice: the host's DNS domain had gone
    away, so git's guess was `eliza@pop-os.(none)`, which is not an address,
    and it refused. `user.useConfigOnly` makes git refuse the guess
    everywhere instead of only on a host that happens to be misconfigured.
    The seed commit carries its own identity so the refusal is the
    snapshot's alone.
    """
    setup = (
        ["git", "init"],
        ["git", "config", "user.useConfigOnly", "true"],
        [
            "git",
            "-c",
            "user.name=seed",
            "-c",
            "user.email=seed@example.com",
            "commit",
            "--allow-empty",
            "-m",
            "base",
        ],
    )
    for argv in setup:
        assert run_argv(argv, root) == 0


def _author_of(root: Path, ref: str) -> str:
    return run_capture(["git", "log", "-1", "--format=%an <%ae>", ref], root).stdout.strip()


def test_snapshot_baseline_commits_where_git_will_not_guess_an_identity(tmp_path: Path) -> None:
    """Known-bad (T6-56): the host supplies no identity and will not invent
    one. `commit-tree` takes no `--author`, so without an identity of its own
    the snapshot exits 128 -- and it is the first thing a node does, so the
    run dies before any gate can name what went wrong."""
    _git_repo_that_refuses_to_guess(tmp_path)
    (tmp_path / "a.py").write_text("x = 1\n")
    assert run_argv(["git", "add", "a.py"], tmp_path) == 0
    ref = snapshot_baseline(tmp_path, "n1")
    assert _author_of(tmp_path, ref) == "saddle <saddle@local>"


def test_snapshot_baseline_does_not_borrow_the_operators_identity(tmp_path: Path) -> None:
    """Known-good (T6-56): the host does supply an identity, and the snapshot
    still is not signed with it. A saddle ref is saddle's own bookkeeping; the
    operator did not author it, and a run that reads the same either way does
    not depend on git config it never set."""
    _git_repo(tmp_path)
    (tmp_path / "a.py").write_text("x = 1\n")
    assert run_argv(["git", "add", "a.py"], tmp_path) == 0
    ref = snapshot_baseline(tmp_path, "n1")
    assert _author_of(tmp_path, "HEAD") == "test <test@example.com>"
    assert _author_of(tmp_path, ref) == "saddle <saddle@local>"
