"""Tier-1 node runner: worktree + node in, verdict out (ARCHITECTURE.md §3).

`run_node_gate` is straight-line glue over `saddle.evidence` collectors
and `saddle.gates` predicates: one coverage-wrapped suite run serves both
the tests check and the coverage check, plus one baseline run for
red-phase. Node outputs must be tracked (staged or committed) so the
baseline diff sees them.
"""

from __future__ import annotations

import tempfile
from pathlib import Path

from saddle.dag import Node
from saddle.evidence import (
    CapturedRun,
    changed_lines,
    covered_lines,
    drop_test_caches,
    git_diff,
    materialize_baseline,
    run_capture,
    run_shell,
    run_shell_capture,
    statement_lines,
    under_coverage,
)
from saddle.gates import Tier1Inputs, Tier1Result, run_tier1
from saddle.journal import SpanRecorder


def read_sources(root: Path, pattern: str) -> dict[str, str]:
    """Map workdir-relative posix paths to text for files matching `pattern`."""
    return {
        path.relative_to(root).as_posix(): path.read_text()
        for path in sorted(root.rglob(pattern))
        if path.is_file()
    }


def run_node_gate(
    node: Node,
    workdir: Path,
    *,
    baseline: str = "HEAD",
    recorder: SpanRecorder | None = None,
    capture: list[CapturedRun] | None = None,
) -> Tier1Result:
    """Gate `node` against the `workdir` worktree; `baseline` is the red ref.

    The current-tree suite runs once under coverage and its exit code
    serves both the tests check and the red-phase post leg; the baseline
    run must be nonzero (fail or error — a missing new test errors).
    Requirement witnesses are suite-granular: every discovered test
    source counts once the suite flips red-to-green. When `capture` is
    given, the suite and ruff invocations (exits plus output) append to
    it in run order for recovery prompts.
    """
    gate = node.deterministic_gate
    sources = read_sources(workdir, "*.py")
    statements = {
        (str(workdir / rel), number)
        for rel, source in sources.items()
        for number in statement_lines(source)
    }
    changed = {
        (str(workdir / path), line)
        for path, line in changed_lines(git_diff(workdir, baseline, recorder=recorder))
    } & statements
    changed_files = sorted({path for path, _ in changed})
    data_file = str(workdir / ".coverage.tier1")
    drop_test_caches(workdir)
    suite = run_shell_capture(
        under_coverage(gate.test_command, data_file), workdir, recorder=recorder
    )
    if capture is not None:
        capture.append(suite)
    current_exit = suite.exit_code
    covered = covered_lines(data_file, changed_files)
    with tempfile.TemporaryDirectory() as tmp:
        dest = Path(tmp)
        materialize_baseline(workdir, baseline, dest, recorder=recorder)
        baseline_exit = run_shell(gate.test_command, dest, recorder=recorder)
    test_sources = read_sources(workdir, "test_*.py") | read_sources(workdir, "*_test.py")

    def ruff_runner(argv: list[str]) -> int:
        run = run_capture(argv, workdir, recorder=recorder)
        if capture is not None:
            capture.append(run)
        return run.exit_code

    inputs = Tier1Inputs(
        sources=sources,
        ruff_files=[
            Path(path).relative_to(workdir).as_posix()
            for path in changed_files
            if path.endswith(".py")
        ],
        ruff_runner=ruff_runner,
        test_runner=lambda _command: current_exit,
        changed=changed,
        covered=covered,
        baseline_runner=lambda: baseline_exit,
        current_runner=lambda: current_exit,
        flipped_tests=test_sources,
    )
    return run_tier1(node, inputs)
