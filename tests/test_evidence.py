"""Tests for saddle.evidence: collectors over tmp worktrees."""

from __future__ import annotations

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
    git_diff,
    git_ls_files,
    materialize_baseline,
    mutation_sample,
    run_argv,
    run_capture,
    run_shell,
    run_shell_capture,
    run_stdin,
    statement_lines,
    under_coverage,
)
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
