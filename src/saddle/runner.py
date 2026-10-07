"""Tier-1 node runner: worktree + node in, verdict out (ARCHITECTURE.md §3).

`run_node_gate` is straight-line glue over `saddle.evidence` collectors
and `saddle.gates` predicates: one coverage-wrapped suite run serves both
the tests check and the coverage check, plus one baseline run for
red-phase. Node outputs must be tracked (staged or committed) so the
baseline diff sees them.
"""

from __future__ import annotations

import ast
import shlex
import shutil
import tempfile
from collections.abc import Callable, Collection, Mapping, Sequence
from dataclasses import replace
from pathlib import Path, PurePath
from typing import Final

from saddle.dag import Node
from saddle.evidence import (
    DEFAULT_TEST_TIMEOUT_S,
    RUFF_TIMEOUT_S,
    WORKERS_BASIS,
    CapturedRun,
    MutationOutcome,
    SuiteRun,
    changed_statements,
    covered_lines,
    covering_tests,
    drop_test_caches,
    format_overrides,
    git_added_files,
    git_changed_files,
    git_diff,
    materialize_baseline,
    mutation_sample,
    property_modules,
    pytest_scope,
    ruff_argv,
    ruff_configured,
    ruff_findings,
    run_capture,
    run_shell_capture,
    run_suite_capture,
    scoped_targets,
    suite_run,
    suite_test_seconds,
)
from saddle.gates import (
    PYTEST_TESTS_FAILED,
    RED_PHASE_SAMPLES,
    GateCheck,
    Tier1Inputs,
    Tier1Result,
    failing_tests,
    introduced_findings,
    run_tier1,
)
from saddle.journal import SpanRecorder
from saddle.jsdead import analyse as analyse_js_dead
from saddle.jsevidence import changed_js_lines, merge_outcomes, stryker_entry
from saddle.jsevidence import mutation_sample as js_mutation_sample

# The placeholder survivor a `tier2=False` gate run carries in place of a
# mutation sample: never a verdict, only a marker that nothing was measured.
NOT_MEASURED_AT_TIER1 = "not measured: tier-1 checkpoint"


IMPACT_RAN: Final = (
    "impact: {ran} of {of} test files ran, the ones this change can reach "
    "(the whole suite ran at the first audit of this run)"
)
"""Appended to the `tests` check when `run_node_gate` ran a selection."""


def red_phase_command(test_command: str, ignored: Collection[str]) -> str:
    """`test_command` with every file in `ignored` passed to pytest as `--ignore`.

    Red-phase asks whether the node's own new or changed tests fail
    pre-change, so its baseline leg ignores each test file the node left
    as it was (or deleted), and pytest collects the rest by the suite's own
    rules (`testpaths`, `norecursedirs`). A command that is not a plain
    pytest invocation, or nothing to ignore, comes back unchanged: the
    whole suite, as before.
    """
    argv = shlex.split(test_command)
    if not ignored or "pytest" not in argv or any(c in test_command for c in ";&|"):
        return test_command
    return shlex.join([*argv, *(f"--ignore={rel}" for rel in sorted(ignored))])


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
    the gate passes whatever the test asserts.

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


def _with_suite_run(check: GateCheck, run: SuiteRun) -> GateCheck:
    """The `tests` check, saying how its suite ran when the project asked for workers.

    A parallel run adds `test-workers=N` to `basis`; a serial run the
    project asked to parallelize appends `run.note` to `detail`, so the
    reason is in the finding. A project that set nothing gets the check
    unchanged, byte for byte."""
    if run.parallel:
        basis = f"{WORKERS_BASIS}{run.workers}"
        return replace(check, basis=f"{check.basis} {basis}" if check.basis else basis)
    if run.note:
        return replace(check, detail=f"{check.detail}; {run.note}")
    return check


RERUN_ALONE_MAX: Final = 20
"""The most failing tests a parallel suite run reruns alone (`_passed_alone`).
More than that is a change that broke the suite, not a test that needs quiet."""

LOAD_ONLY: Final = (
    "; {n} of them passed when rerun alone on the same tree, in one serial "
    "process: {names}. They fail only inside the full run, so look at what they "
    "share with it: a test that ran before them on the same worker and left "
    "something behind (a patched module, a cache, a file), or what the {workers} "
    "workers share (a port, a display, a time limit). The change itself may be "
    "what leaves it; the verdict stands until the suite passes"
)
"""What the `tests` finding adds for tests that fail in the suite and pass
rerun alone (#174): three dogfood runs spent 17 minutes finding out that a
failure their change did not cause passed in isolation. A rerun alone cannot
tell load from a test that leaked state into the next: the six tests this was
first written for failed because one test left the chat server's turn patched
for the rest of its worker, not from load. So the line names both, and does not
steer away from the change, which can be the test that leaks. A test that also
fails alone is never named here, so a test the change broke still reads as the
change's."""


def _passed_alone(
    mode: SuiteRun,
    node: Node,
    workdir: Path,
    output: str,
    *,
    recorder: SpanRecorder | None,
    timeout: float | None,
) -> list[str]:
    """The failing tests of a parallel suite run that pass rerun alone.

    Nothing when the suite ran serially, the node is a test node (its
    tests are meant to fail), no test or too many failed, or the rerun
    ended other than passing or failing tests (a hang, a usage error)."""
    failing = failing_tests(output)
    if not mode.parallel or node.kind == "test" or not 0 < len(failing) <= RERUN_ALONE_MAX:
        return []
    command = mode.alone(node.deterministic_gate.test_command, failing)
    rerun = run_shell_capture(command, workdir, recorder=recorder, timeout=timeout)
    if rerun.exit_code not in (0, PYTEST_TESTS_FAILED):
        return []
    still = set(failing_tests(rerun.stdout + rerun.stderr))
    return [t for t in failing if t not in still]


def _with_load_only(check: GateCheck, passed_alone: Sequence[str], run: SuiteRun) -> GateCheck:
    if not passed_alone:
        return check
    names = ", ".join(passed_alone)
    said = LOAD_ONLY.format(n=len(passed_alone), names=names, workers=run.workers)
    return replace(check, detail=f"{check.detail}{said}")


def run_node_gate(
    node: Node,
    workdir: Path,
    *,
    baseline: str = "HEAD",
    recorder: SpanRecorder | None = None,
    capture: list[CapturedRun] | None = None,
    planned_requirements: tuple[str, ...] = (),
    owed_tests: tuple[str, ...] = (),
    tier2: bool = True,
    test_timeout: float = DEFAULT_TEST_TIMEOUT_S,
    test_workers: int = 1,
    test_selection: Collection[str] | None = None,
    on_suite: Callable[[str, CapturedRun], None] | None = None,
    skip_report: Path | None = None,
    test_only_additions: bool = False,
    task_text: str | None = None,
    js_tools: Path | None = None,
) -> Tier1Result:
    """Gate `node` against the `workdir` worktree; `baseline` is the red ref.

    `planned_requirements` is every id the node's plan declares, which the
    binding gate's orphan half subtracts before rejecting a citation;
    a single node gated on its own leaves it empty.
    `owed_tests` is the nodes the plan still expects tests from, which
    defers an uncovered changed line rather than failing the node for a
    question no node has yet been able to answer; empty defers nothing.

    `tier2=False` (the auditor's checkpoint tier, `saddle.auditor`) skips
    the three evidence legs only tier 2 reads -- the mutation run, the
    property oracle and every red-phase baseline sample -- so the
    `mutation`, `property-coverage` and `red-phase` checks in the result
    are placeholders and must not be read. The suite then runs once. The
    default is the full battery, unchanged.

    The current-tree suite runs once under coverage and its exit code
    serves both the tests check and the red-phase post leg; the baseline
    run must be nonzero (fail or error — a missing new test errors).
    Requirement witnesses are suite-granular: every discovered test
    source counts once the suite flips red-to-green. When `capture` is
    given, the suite and ruff invocations (exits plus output) append to
    it in run order for recovery prompts.

    `test_timeout` bounds every run of the node's test command here: the
    suite, each red-phase sample and each dead-code rerun. Callers pass the
    project's `evidence.suite_limit`, read where the task started -- never
    at `baseline`, which in `slice` is a per-node snapshot that holds the
    edits of the nodes before it.

    `test_workers` is the project's `evidence.suite_workers`, read where
    the task started (the audit passes it; `slice` does not). From 2 up,
    and when `evidence.suite_run` finds pytest-xdist and pytest-cov in the
    tree's test environment, those same runs are `pytest -n test_workers`:
    the suite under pytest-cov, recorded into the one data file both the
    tests and the coverage checks read, and each red-phase sample with
    nothing recorded (`SuiteRun.red_sample`; asked again of the baseline
    copy, whose pytest options are the baseline's). The `tests` check's
    `basis` then says `test-workers=N`.
    Otherwise every run is serial, as before, and the `tests` check's
    detail ends with why. A `test` node always runs serially: its verdict
    is read off the run's output.

    `test_selection` (`saddle.impact.select`) is the test files the change
    can reach: the suite run and every dead-code rerun then pass every other
    test file to pytest as `--ignore`, keeping the command's own scope and
    options, and the `tests` check's detail says how many ran. None, or a
    command that is not a plain pytest invocation, runs the whole suite.
    `on_suite` is called with the data file and the suite's run while the
    tree is still there; the suite then records per-test contexts, which
    `saddle.impact.build` reads. `skip_report` is where the current-tree
    suite run also leaves pytest's report of the tests it skipped
    (`evidence.run_suite_capture`); the caller reads it.

    `test_only_additions` (the audit sets it) adds `gates.check_test_only_additions`
    to the `dead-code` check: a function, class or constant the change adds
    that only tests reach is refused. A plan's nodes leave it off, since an
    `impl` node may be gated before the node that uses what it writes.
    `task_text` is the task the audit was given, if any: a public name it spells
    was asked for (`gates.check_test_only_additions`). The same question is asked of
    the `.js` files the change added to (`jsdead.analyse`, in `js_tools`' or the
    tree's `node_modules`), and node or TypeScript missing is `not proven`.

    A changed `.js` line is mutated too, at tier 2, when StrykerJS is installed
    in `workdir` or in `js_tools` (the checkout a staged `workdir` was copied
    from, which holds the ignored `node_modules`): its mutants join the Python
    ones in the `mutation` check (`jsevidence.merge_outcomes`).
    """
    gate = node.deterministic_gate
    workers = 1 if node.kind == "test" else test_workers
    sources = read_sources(workdir, "*.py")
    diff = git_diff(workdir, baseline, recorder=recorder)
    changed = changed_statements(workdir, diff)
    js_changed = changed_js_lines(workdir, diff) if stryker_entry(workdir, js_tools) else set()
    js_dead = (
        analyse_js_dead(workdir, diff, tools=js_tools, recorder=recorder)
        if test_only_additions
        else None
    )
    changed_files = sorted({path for path, _ in changed})
    added = git_added_files(workdir, baseline, recorder=recorder)
    # Every file the diff names (git decides, so deletions and non-Python
    # files count, and a staged new file is already among them -- tracked-
    # ness comes from the index), for the opt-in target-scope check.
    touched = sorted(git_changed_files(workdir, baseline, recorder=recorder))
    data_file = str(workdir / ".coverage.tier1")
    drop_test_caches(workdir)
    test_sources = read_sources(workdir, "test_*.py") | read_sources(workdir, "*_test.py")
    # Only the test files the change can reach, when the caller knows them:
    # every other one is ignored, so the command's own scope still decides.
    skipped = sorted(set(test_sources) - set(test_selection)) if test_selection is not None else []
    command = red_phase_command(gate.test_command, skipped)
    mode = suite_run(workdir, gate.test_command, workers, recorder=recorder)
    suite = run_suite_capture(
        mode,
        command,
        workdir,
        data_file,
        recorder=recorder,
        timeout=test_timeout,
        contexts=tier2 or on_suite is not None,
        skip_report=skip_report,
    )
    if capture is not None:
        capture.append(suite)
    if on_suite is not None:
        on_suite(data_file, suite)
    current_exit = suite.exit_code
    passed_alone = (
        _passed_alone(
            mode,
            node,
            workdir,
            suite.stdout + suite.stderr,
            recorder=recorder,
            timeout=test_timeout,
        )
        if current_exit != 0
        else []
    )
    covered = covered_lines(data_file, changed_files)
    # The tests that ran a changed line are all a changed-line mutant can
    # meet, so mutmut runs those, not the whole scope (tier 2 only).
    covering = covering_tests(data_file, changed) if tier2 else ()
    ruff_files = [
        Path(path).relative_to(workdir).as_posix() for path in changed_files if path.endswith(".py")
    ]
    with tempfile.TemporaryDirectory() as tmp:
        dest = Path(tmp)
        materialize_baseline(workdir, baseline, dest, recorder=recorder)
        # The ruff baseline leg, on the untouched baseline tree
        # before red-phase writes stubs and tests into it: findings the
        # node inherited are reported, not charged to it.
        at_baseline = [rel for rel in ruff_files if (dest / rel).exists()]
        baseline_findings = (
            ruff_findings(dest, at_baseline, recorder=recorder)[1] if at_baseline else []
        )
        baseline_tests = read_sources(dest, "test_*.py") | read_sources(dest, "*_test.py")
        # Captured before the two loops below write into `dest`: the check
        # that a repair deleted nothing it measured asks what the baseline
        # defined, and after those loops `dest` also holds stubs of modules
        # the node created and the node's own new test files -- neither of
        # which the baseline had.
        baseline_modules = {
            rel: text
            for rel, text in read_sources(dest, "*.py").items()
            if rel not in baseline_tests
        }
        tests_changed = _test_signatures(baseline_tests) != _test_signatures(test_sources)
        # Red-phase means the node's own tests against pre-change sources.
        # Without this copy the probe runs a suite that never contained the
        # new tests, so "file not found" scored as red for every new file.
        # Modules the node creates do not exist at baseline, so the
        # pre-change run cannot import them and red-phase falls back to
        # accepting a collection error -- clearable on demand, whatever
        # the test asserts (#53). A signature-preserving stub gives
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
        # verdict, so the samples would be evidence nothing reads. Nor does
        # tier 1 (`tier2=False`): the auditor's checkpoint tier reports no
        # red-phase, and one sample is one more run of the whole suite. Nor
        # does a refactor that changed no test: `check_red_phase` judges it
        # on coverage and mutation and never reads a sample, and on saddle
        # that one unread sample was six minutes of every source-only audit.
        unread = node.kind == "test" or (node.kind == "refactor" and not tests_changed)
        samples = 0 if unread or not tier2 else (RED_PHASE_SAMPLES if tests_changed else 1)
        baseline_exits: list[int] = []
        baseline_output = ""
        baseline_mode = (
            suite_run(dest, gate.test_command, workers, recorder=recorder)
            if samples
            else SuiteRun()
        )
        # Only the node's own new or changed test files run pre-change: each
        # sample used to be the whole suite, most of a finish audit's wall on
        # a large project, and an unchanged test failing on the original code
        # stood in for a new test that proves nothing. With no test changed,
        # the one sample stays the whole suite (an impl node's tests are its
        # baseline's).
        red_command = (
            red_phase_command(
                gate.test_command,
                [
                    rel
                    for rel, text in baseline_tests.items()
                    if test_sources.get(rel) in (text, None)
                ],
            )
            if tests_changed
            else gate.test_command
        )
        # Red-phase reads a sample's exit and output, never its coverage, so
        # a sample pytest-cov would record records nothing (see `red_sample`
        # for why a serial `coverage run` sample stays as it is).
        for sample_index in range(samples):
            drop_test_caches(dest)
            baseline_run = run_shell_capture(
                baseline_mode.red_sample(red_command, str(dest / ".coverage.red")),
                dest,
                recorder=recorder,
                timeout=test_timeout,
            )
            baseline_exits.append(baseline_run.exit_code)
            if sample_index == 0:
                baseline_output = baseline_run.stdout + baseline_run.stderr
            # A first sample that passes decides red-phase: samples that all
            # pass and samples that disagree are both refused, so whatever
            # the rest would say, it is a refusal ("tests pass pre-change").
            # The rest run only after a first failure, where their agreement
            # is what tells a genuine red from a flake.
            if baseline_exits == [0]:
                break

    if ruff_files:
        lint_run, current_findings = ruff_findings(workdir, ruff_files, recorder=recorder)
        # Formatting is judged only where the project chose ruff (#130).
        format_checked = ruff_configured(workdir, baseline)
        format_run = (
            run_capture(
                ruff_argv("format", "--check", *format_overrides(workdir, baseline), *ruff_files),
                workdir,
                recorder=recorder,
                timeout=RUFF_TIMEOUT_S,
            )
            if format_checked
            else None
        )
        if capture is not None:
            capture.extend((lint_run,) if format_run is None else (lint_run, format_run))
        lint_exit = lint_run.exit_code
        format_exit = 0 if format_run is None else format_run.exit_code
    else:
        current_findings, lint_exit, format_exit, format_checked = [], 0, 0, True
    introduced, inherited = introduced_findings(current_findings, baseline_findings)

    sample = gate.mutation_sample
    # A test node changes no source, so there is nothing to mutate and the
    # check is substituted with "not required": skip the mutmut run.
    # The engine runs the node's declared scope, the same tests the tests
    # gate ran above: a TDD plan's test node writes every module's
    # specification red up front, so the whole suite is red until the last
    # impl node lands, and mutmut cannot baseline against a red suite --
    # round 3c's three impl attempts all died on `failed to collect stats`
    # and no impl node could seal.
    mutation = (
        MutationOutcome(killed=0, total=0, generated=0, survivors=())
        if node.kind == "test"
        else MutationOutcome(killed=0, total=0, generated=0, survivors=(NOT_MEASURED_AT_TIER1,))
        if not tier2
        else mutation_sample(
            workdir,
            changed,
            sample.max_mutants,
            test_files=test_sources,
            # The covering tests decide what mutmut's stats run collects; each
            # mutant then runs only the ones that ran its function. As
            # arguments to every run, each mutant ran all of them.
            run_tests=() if covering else pytest_scope(gate.test_command),
            select_tests=covering,
            suite_passed=current_exit == 0,
            recorder=recorder,
            # Only the lines those tests run are mutated: a changed line none
            # runs is the coverage check's refusal, and mutmut generating a
            # large module's every mutant alone could spend the budget.
            only_covered=bool(covering),
            covered=covered,
            # The workers the suite ran on, which mutmut's stats pass may use.
            workers=mode.workers if mode.parallel else 1,
            test_seconds=suite_test_seconds(suite.stdout, mode.workers if mode.parallel else 1),
        )
    )
    if tier2 and node.kind != "test" and js_changed:
        mutation = merge_outcomes(
            mutation, js_mutation_sample(workdir, js_changed, tools=js_tools, recorder=recorder)
        )
    # The property oracle, `impl` nodes only: the property-bearing
    # test modules that import a changed module run alone against the same
    # changed-line mutants, with the same exclusion set; `run_tests` narrows
    # what pytest collects, which `test_files` never did. `None` when no
    # module qualifies, so the check can tell "no targets" from "not run".
    # Scoped to the node's own tests, exactly as the mutation gate above
    # is. Unscoped, g1-79cd848's node-2 drew test_store.py --
    # node-4's specification, red because node-4 had not run -- and the
    # oracle reported "no mutants sampled" on all three attempts. The
    # same tree scoped: killed 85 of 100.
    property_candidates = tuple(property_modules(test_sources, changed_files))
    property_targets = scoped_targets(property_candidates, pytest_scope(gate.test_command))
    property_out_of_scope = tuple(p for p in property_candidates if p not in property_targets)
    property_oracle = (
        mutation_sample(
            workdir,
            changed,
            sample.max_mutants,
            test_files=test_sources,
            run_tests=property_targets,
            suite_passed=current_exit == 0,
            recorder=recorder,
        )
        if tier2 and node.kind == "impl" and property_targets
        else None
    )
    added_lines: dict[str, list[int]] = {}
    for absolute, line in changed:
        rel = Path(absolute).relative_to(workdir).as_posix()
        if rel in test_sources:
            continue
        added_lines.setdefault(rel, []).append(line)

    def suite_without(edited: Mapping[str, str]) -> int:
        """The node's own test command over the tree minus `edited`'s losses.

        A copy, so the gate that asks the question cannot answer it by
        changing the tree every later gate measures.
        """
        with tempfile.TemporaryDirectory(prefix="saddle-dead-code-") as tmp:
            sandbox = Path(tmp) / "tree"
            shutil.copytree(workdir, sandbox, ignore=shutil.ignore_patterns("__pycache__", ".git"))
            for rel, text in edited.items():
                (sandbox / rel).write_text(text)
            return run_shell_capture(
                mode.plain(command), sandbox, recorder=recorder, timeout=test_timeout
            ).exit_code

    inputs = Tier1Inputs(
        sources=sources,
        ruff_files=ruff_files,
        ruff_introduced=tuple(introduced),
        ruff_inherited=inherited,
        ruff_lint_exit=lint_exit,
        ruff_format_exit=format_exit,
        ruff_format_checked=format_checked,
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
        added_lines={rel: tuple(sorted(lines)) for rel, lines in sorted(added_lines.items())},
        dead_code_runner=suite_without,
        baseline_sources=baseline_modules,
        workdir=str(workdir),
        owed_tests=owed_tests,
        added_files=[str(workdir / p) for p in added],
        touched_files=touched,
        test_output=suite.stdout + suite.stderr,
        workdir_modules=sorted({PurePath(rel).parts[0].removesuffix(".py") for rel in sources}),
        planned_requirements=planned_requirements,
        property_oracle=property_oracle,
        property_targets=property_targets,
        property_out_of_scope=property_out_of_scope,
        test_only_additions=test_only_additions,
        task_text=task_text,
        js_dead=js_dead,
        pyproject_text=(
            (workdir / "pyproject.toml").read_text()
            if test_only_additions and (workdir / "pyproject.toml").is_file()
            else None
        ),
    )
    result = run_tier1(node, inputs)
    ran = IMPACT_RAN.format(ran=len(test_sources) - len(skipped), of=len(test_sources))
    checks = tuple(
        _with_suite_run(
            _with_load_only(
                replace(check, detail=f"{check.detail}; {ran}")
                if command != gate.test_command
                else check,
                passed_alone,
                mode,
            ),
            mode,
        )
        if check.name == "tests"
        else check
        for check in result.checks
    )
    return replace(result, checks=checks)
