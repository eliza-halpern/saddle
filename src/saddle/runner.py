"""Tier-1 node runner: worktree + node in, verdict out (ARCHITECTURE.md §3).

`run_node_gate` is straight-line glue over `saddle.evidence` collectors
and `saddle.gates` predicates: one coverage-wrapped suite run serves both
the tests check and the coverage check, plus one baseline run for
red-phase. Node outputs must be tracked (staged or committed) so the
baseline diff sees them.
"""

from __future__ import annotations

import ast
import tempfile
from pathlib import Path, PurePath

from saddle.dag import Node
from saddle.evidence import (
    CapturedRun,
    MutationOutcome,
    changed_lines,
    covered_lines,
    drop_test_caches,
    git_added_files,
    git_changed_files,
    git_diff,
    materialize_baseline,
    mutation_sample,
    property_modules,
    run_capture,
    run_shell_capture,
    statement_lines,
    under_coverage,
)
from saddle.gates import RED_PHASE_SAMPLES, Tier1Inputs, Tier1Result, run_tier1
from saddle.journal import SpanRecorder


def read_sources(root: Path, pattern: str) -> dict[str, str]:
    """Map workdir-relative posix paths to text for files matching `pattern`."""
    return {
        path.relative_to(root).as_posix(): path.read_text()
        for path in sorted(root.rglob(pattern))
        if path.is_file()
    }


class _Stubber(ast.NodeTransformer):
    """Replace every function body with `raise NotImplementedError`."""

    def _empty(self, node: ast.FunctionDef | ast.AsyncFunctionDef) -> ast.AST:
        self.generic_visit(node)
        node.body = [ast.Raise(exc=ast.Name(id="NotImplementedError", ctx=ast.Load()), cause=None)]
        return node

    def visit_FunctionDef(self, node: ast.FunctionDef) -> ast.AST:
        return self._empty(node)

    def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> ast.AST:
        return self._empty(node)


def _stub_module(source: str) -> str:
    """A signature-preserving stub of `source`: same API, no behaviour.

    Red-phase's greenfield branch accepts a baseline collection error,
    because a module the node creates cannot be imported before it
    exists. That is clearable on demand: any new code in a new module
    with a new test produces an import error naming a changed source, so
    the gate passes whatever the test asserts (F2).

    Materializing a stub instead gives the pre-change run something to
    import. A test that exercises the new code then fails for a real
    reason, and a tautological one passes pre-change and is rejected --
    which is what the gate is for.
    """
    tree = _Stubber().visit(ast.parse(source))
    ast.fix_missing_locations(tree)
    return ast.unparse(tree)


def _test_signatures(sources: dict[str, str]) -> dict[str, str]:
    """Map each test module to a comment- and layout-insensitive signature.

    Whether red-phase binds is read off the diff, so the comparison must
    ignore edits that cannot change an outcome: adding a `# REQ-001` tag
    to satisfy requirement-binding must not, by itself, make a
    behaviour-preserving node claim a red-phase flip. Unparseable sources
    fall back to their text, which simply counts as changed.
    """
    signatures = {}
    for rel, text in sources.items():
        try:
            signatures[rel] = ast.dump(ast.parse(text))
        except SyntaxError:  # pragma: no cover - current tree already parsed by check_syntax
            signatures[rel] = text
    return signatures


def run_node_gate(
    node: Node,
    workdir: Path,
    *,
    baseline: str = "HEAD",
    recorder: SpanRecorder | None = None,
    capture: list[CapturedRun] | None = None,
    planned_requirements: tuple[str, ...] = (),
) -> Tier1Result:
    """Gate `node` against the `workdir` worktree; `baseline` is the red ref.

    `planned_requirements` is every id the node's plan declares, which the
    binding gate's orphan half subtracts before rejecting a citation
    (T3-24); a single node gated on its own leaves it empty.

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
    added = git_added_files(workdir, baseline, recorder=recorder)
    # Every file the diff names (git decides, so deletions and non-Python
    # files count, and a staged new file is already among them -- tracked-
    # ness comes from the index), for the opt-in target-scope check (T3-2).
    touched = sorted(git_changed_files(workdir, baseline, recorder=recorder))
    data_file = str(workdir / ".coverage.tier1")
    drop_test_caches(workdir)
    suite = run_shell_capture(
        under_coverage(gate.test_command, data_file), workdir, recorder=recorder
    )
    if capture is not None:
        capture.append(suite)
    current_exit = suite.exit_code
    covered = covered_lines(data_file, changed_files)
    test_sources = read_sources(workdir, "test_*.py") | read_sources(workdir, "*_test.py")
    with tempfile.TemporaryDirectory() as tmp:
        dest = Path(tmp)
        materialize_baseline(workdir, baseline, dest, recorder=recorder)
        baseline_tests = read_sources(dest, "test_*.py") | read_sources(dest, "*_test.py")
        tests_changed = _test_signatures(baseline_tests) != _test_signatures(test_sources)
        # Red-phase means the node's own tests against pre-change sources.
        # Without this copy the probe runs a suite that never contained the
        # new tests, so "file not found" scored as red for every new file.
        # Modules the node creates do not exist at baseline, so the
        # pre-change run cannot import them and red-phase falls back to
        # accepting a collection error -- clearable on demand, whatever
        # the test asserts (F2, #53). A signature-preserving stub gives
        # the run something to import, so a test that exercises the new
        # code fails for a real reason and a tautological one passes
        # pre-change and is rejected.
        for rel, source in sources.items():
            if rel in test_sources or (dest / rel).exists():
                continue
            target = dest / rel
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(_stub_module(source))
        for rel, source in test_sources.items():
            target = dest / rel
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(source)
        # Sampled, not observed once: red-phase is the only gate that
        # reasons over two runs, so a flaky pre-change leg yields "fail
        # before, pass after" with no causal relation to the diff. Caches
        # are dropped between samples so each run stands alone. Skipped
        # entirely when no test changed -- that path never reads the
        # exits, and three extra suite runs per refactor node is real
        # wall-clock for evidence nothing consumes.
        # A test node has no baseline leg: its red-phase mirrors the tests
        # verdict (T3-7a), so the samples would be evidence nothing reads.
        samples = 0 if node.kind == "test" else (RED_PHASE_SAMPLES if tests_changed else 1)
        baseline_exits: list[int] = []
        baseline_output = ""
        for sample_index in range(samples):
            drop_test_caches(dest)
            baseline_run = run_shell_capture(
                under_coverage(gate.test_command, str(dest / ".coverage.red")),
                dest,
                recorder=recorder,
            )
            baseline_exits.append(baseline_run.exit_code)
            if sample_index == 0:
                baseline_output = baseline_run.stdout + baseline_run.stderr

    def ruff_runner(argv: list[str]) -> int:
        run = run_capture(argv, workdir, recorder=recorder)
        if capture is not None:
            capture.append(run)
        return run.exit_code

    sample = gate.mutation_sample
    # A test node changes no source, so there is nothing to mutate and the
    # check is substituted with "not required" (T3-7a): skip the mutmut run.
    mutation = (
        MutationOutcome(killed=0, total=0, generated=0, survivors=())
        if node.kind == "test"
        else mutation_sample(
            workdir, changed, sample.max_mutants, test_files=test_sources, recorder=recorder
        )
    )
    # The property oracle (T3-3), `impl` nodes only: the property-bearing
    # test modules that import a changed module run alone against the same
    # changed-line mutants, with the same exclusion set; `run_tests` narrows
    # what pytest collects, which `test_files` never did. `None` when no
    # module qualifies, so the check can tell "no targets" from "not run".
    property_targets = tuple(property_modules(test_sources, changed_files))
    property_oracle = (
        mutation_sample(
            workdir,
            changed,
            sample.max_mutants,
            test_files=test_sources,
            run_tests=property_targets,
            recorder=recorder,
        )
        if node.kind == "impl" and property_targets
        else None
    )
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
        baseline_exits=tuple(baseline_exits),
        baseline_output=baseline_output,
        baseline_tests=baseline_tests,
        tests_changed=tests_changed,
        current_runner=lambda: current_exit,
        flipped_tests=test_sources,
        mutation=mutation,
        added_files=[str(workdir / p) for p in added],
        touched_files=touched,
        test_output=suite.stdout + suite.stderr,
        workdir_modules=sorted({PurePath(rel).parts[0].removesuffix(".py") for rel in sources}),
        planned_requirements=planned_requirements,
        property_oracle=property_oracle,
        property_targets=property_targets,
    )
    return run_tier1(node, inputs)
