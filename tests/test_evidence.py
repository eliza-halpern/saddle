"""Tests for saddle.evidence: collectors over tmp worktrees."""

from __future__ import annotations

import hashlib
import os
import shutil
import stat
import sys
import warnings
from collections.abc import Sequence
from pathlib import Path

import pytest

import saddle.evidence as evidence_module
from saddle.evidence import (
    _MUTATION_TIMEOUT_S,
    CapturedRun,
    MutationOutcome,
    _mutmut_scratch_config,
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
    restore_baseline,
    run_argv,
    run_capture,
    run_shell,
    run_shell_capture,
    run_stdin,
    snapshot_baseline,
    statement_lines,
    under_coverage,
)
from saddle.gates import SHELL_TIMEOUT, TOOL_UNAVAILABLE
from saddle.journal import SpanRecorder, read_spans


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
    assert [span.argv[3] for span in spans] == ["add", "write-tree", "commit-tree", "update-ref"]


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
    assert outcome == MutationOutcome(killed=3, total=4, generated=6, survivors=("m_hit2",))
    sampled = mutation_sample(workdir, changed, 2, test_files={"tests/test_a.py"})
    assert sampled == MutationOutcome(killed=1, total=2, generated=6, survivors=("m_hit2",))


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


def _without_stubbed_mutmut(monkeypatch: pytest.MonkeyPatch) -> None:
    """Drop the conftest `mutmut` stub from PATH so the real engine runs."""
    kept = [p for p in os.environ["PATH"].split(os.pathsep) if "mutmut-stub" not in p]
    monkeypatch.setenv("PATH", os.pathsep.join(kept))
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
    assert prop == MutationOutcome(killed=0, total=1, generated=1, survivors=("n.x_f__mutmut_1",))


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


def test_mutation_sample_invocation_shape(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The collector invokes mutmut with exact argv, scratch cwd, and recorder.

    A recording fake (not the shell stub) observes the invocation shape the
    stub cannot see: exact argv per call, a saddle-mutation- scratch cwd
    carrying the config and deep tree but no __pycache__, and recorder
    pass-through on every call.
    """
    workdir = _mutation_workdir(tmp_path)
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
        if list(argv[:2]) == ["mutmut", "show"]:
            return CapturedRun(
                argv=tuple(argv),
                exit_code=0,
                stdout=_show_diff("a.py", "x = 1"),
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
    assert [argv for argv, _, _ in calls] == [
        ("timeout", str(_MUTATION_TIMEOUT_S), "mutmut", "run"),
        ("mutmut", "results", "--all", "True"),
        ("mutmut", "show", "m1"),
    ]
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
