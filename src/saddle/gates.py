"""Tier-1 per-node gates (ARCHITECTURE.md §3 Phase 3 Tier 1).

Checks run in gate order: syntax, ruff, tests, coverage, red-phase,
node-scope, target-scope, property-coverage, assertion-preservation,
requirement-binding, mutation. Each check is a small pure function so
killer fixtures stay fast and deterministic; subprocess runners are
injected at the boundary, never embedded in the predicates.
"""

from __future__ import annotations

import ast
import re
from collections.abc import Callable, Collection, Mapping, Sequence
from dataclasses import dataclass
from fnmatch import fnmatch
from pathlib import PurePath
from typing import TYPE_CHECKING, Final

from saddle.dag import Node

if TYPE_CHECKING:
    from saddle.evidence import MutationOutcome

# pytest exit codes that carry red-phase evidence (see `check_red_phase`).
PYTEST_TESTS_FAILED: Final = 1
PYTEST_COLLECTION_ERROR: Final = 2
# Exit code for a command killed on timeout, following GNU `timeout(1)`.
# Pytest reserves 0-5, so this cannot collide with a real suite verdict.
SHELL_TIMEOUT: Final = 124
# Exit code for a gate tool that could not be launched at all, following
# the shell convention for "command not found". A tool that raises rather
# than returning bypasses recovery and leaves the run with no stated
# reason: T1 v2 failed with no gate lines in the transcript, the only
# evidence being a journal span reading "[Errno 2] ... 'coverage'".
TOOL_UNAVAILABLE: Final = 127
# Baseline runs sampled per red-phase check. Red-phase is the only gate
# that reasons over two runs, so its evidence is worth exactly what the
# stability of the pre-change leg is worth; one observation cannot tell a
# genuine failure from a flake.
RED_PHASE_SAMPLES: Final = 3
# Requirement IDs as they appear in test sources. Shape is pinned in the
# schema (dag.RequirementId), so anything matching here and absent from
# the node's declared set was invented by the worker.
REQUIREMENT_CITATION: Final = re.compile(r"REQ-\d{3}")
# Below this many decided mutants a kill percentage is noise: with 2
# mutants the only rates available are 0, 50 and 100. Demand all of them
# instead, so a thin sample is a stricter bar rather than a cheaper one.
MIN_SIGNIFICANT_MUTANTS: Final = 5
# pytest's own discovery rules, so "is this a test file" means the same
# thing to the gate as it does to the runner that will execute it.
TEST_FILE_PATTERNS: Final = ("test_*.py", "*_test.py")


def _is_test_file(path: str) -> bool:
    """True when pytest would collect `path` as a test module."""
    name = PurePath(path).name
    return any(fnmatch(name, pattern) for pattern in TEST_FILE_PATTERNS)


# Fixed floor for behaviour-preserving nodes; ARCHITECTURE.md's own gate
# example uses 85.0. Deliberately not the node's own kill_threshold.
REFACTOR_KILL_FLOOR: Final = 85.0


def _names_changed_source(baseline_output: str, changed_files: Collection[str]) -> bool:
    """True when a collection error blames a source file the node changed.

    Import failures name the module (`No module named 'n'`), collection
    headers name the file (`ERROR collecting n.py`), so both spellings
    are checked against every changed path.
    """
    return any(
        PurePath(path).name in baseline_output or f"'{PurePath(path).stem}'" in baseline_output
        for path in changed_files
    )


@dataclass(frozen=True)
class GateCheck:
    """Outcome of one Tier-1 check: `passed` plus human-readable `detail`.

    `basis` names the evidence the verdict rests on where a bare verdict
    would hide its weight: a mutation verdict over 0 mutants and one over
    5 both read "passed" (T7's 218-line module, CLAUDE.md), so the
    mutation check records how many mutants were sampled. Checks whose
    detail already carries the count leave it None.
    """

    name: str
    passed: bool
    detail: str = ""
    basis: str | None = None


def check_syntax(sources: Mapping[str, str]) -> GateCheck:
    """Parse each source; the first SyntaxError fails the gate."""
    for path, source in sources.items():
        try:
            ast.parse(source)
        except SyntaxError as exc:
            return GateCheck(name="syntax", passed=False, detail=f"{path}:{exc.lineno}: {exc.msg}")
    return GateCheck(name="syntax", passed=True, detail=f"{len(sources)} file(s) parsed")


def check_ruff(files: Collection[str], run: Callable[[list[str]], int]) -> GateCheck:
    """Ruff lint and format checks; either nonzero exit fails the gate."""
    ordered = sorted(files)
    if not ordered:
        return GateCheck(name="ruff", passed=True, detail="no files to lint")
    lint_code = run(["ruff", "check", *ordered])
    format_code = run(["ruff", "format", "--check", *ordered])
    if TOOL_UNAVAILABLE in (lint_code, format_code):
        return GateCheck(
            name="ruff",
            passed=False,
            detail="ruff unavailable: the gate tool could not be launched",
        )
    if lint_code != 0 or format_code != 0:
        return GateCheck(
            name="ruff",
            passed=False,
            detail=f"ruff check exited {lint_code}, format exited {format_code}",
        )
    return GateCheck(name="ruff", passed=True, detail=f"{len(ordered)} file(s) clean")


def _failing_test_count(output: str) -> int:
    """Failing tests as pytest's summary line counts them (`3 failed, 1 passed`)."""
    match = re.search(r"(\d+) failed", output)
    return int(match.group(1)) if match else 0


def _missing_module(output: str) -> str | None:
    """Top-level name of the module an import error says is absent, if any."""
    match = re.search(r"No module named '([\w.]+)'", output)
    return match.group(1).split(".")[0] if match else None


def _red_specification(
    test_command: str, exit_code: int, output: str, workdir_modules: Collection[str]
) -> GateCheck:
    """The `test`-kind tests verdict: the node's tests must fail now (T3-7a).

    A test node writes the specification the impl node depending on it
    must satisfy, so a suite that passes against the current code
    specified nothing. Exit 1 with at least one failing test is red. A
    collection error is red only when it names a module the worktree
    does not have -- the greenfield spec whose module the impl node will
    create; one naming an existing module is a broken test, not a
    specification. Any other exit never ran the tests.
    """
    if exit_code == 0:
        return GateCheck(
            name="tests",
            passed=False,
            detail=f"{test_command!r} exited 0: tests already pass, nothing specified",
        )
    if exit_code == PYTEST_TESTS_FAILED:
        failing = _failing_test_count(output)
        if failing:
            return GateCheck(
                name="tests",
                passed=True,
                detail=f"red specification: {failing} failing test(s)",
            )
        return GateCheck(
            name="tests",
            passed=False,
            detail=f"{test_command!r} exited 1 but its output counts no failing test",
        )
    if exit_code == PYTEST_COLLECTION_ERROR:
        missing = _missing_module(output)
        if missing is not None and missing not in workdir_modules:
            return GateCheck(
                name="tests",
                passed=True,
                detail=f"red specification: module {missing!r} does not exist yet",
            )
        blamed = f"names existing module {missing!r}" if missing else "names no missing module"
        return GateCheck(
            name="tests",
            passed=False,
            detail=f"{test_command!r} collection error {blamed}: broken, not a specification",
        )
    return GateCheck(
        name="tests",
        passed=False,
        detail=f"{test_command!r} exited {exit_code}: tests never ran",
    )


def check_test_command(
    test_command: str,
    run: Callable[[str], int],
    *,
    kind: str = "impl",
    output: str = "",
    workdir_modules: Collection[str] = (),
) -> GateCheck:
    """Run the node's declared pytest scope; nonzero exit fails the gate.

    A timeout is reported as a hang rather than as an exit code. The two
    are different defects: a failing assertion names the behaviour it
    disagrees with, while a suite that never terminates yields no verdict
    at all -- downstream harnesses that parse pytest counts read partial
    or zero results, so a hang scores worse than the failure it hides.

    A `test` node is graded the other way round (`_red_specification`):
    `output` is the run's captured text and `workdir_modules` the
    worktree's importable top-level names, both supplied by the runner so
    the predicate stays subprocess-free.
    """
    exit_code = run(test_command)
    if exit_code == TOOL_UNAVAILABLE:
        return GateCheck(
            name="tests",
            passed=False,
            detail=f"{test_command!r} unavailable: the gate tool could not be launched",
        )
    if exit_code == SHELL_TIMEOUT:
        return GateCheck(
            name="tests",
            passed=False,
            detail=f"{test_command!r} hangs: no verdict within the time limit",
        )
    if kind == "test":
        return _red_specification(test_command, exit_code, output, workdir_modules)
    if exit_code != 0:
        return GateCheck(
            name="tests",
            passed=False,
            detail=f"{test_command!r} exited {exit_code}",
        )
    return GateCheck(name="tests", passed=True, detail=f"{test_command!r} exited 0")


def check_changed_line_coverage(
    changed: set[tuple[str, int]],
    covered: set[tuple[str, int]],
    minimum: float,
) -> GateCheck:
    """Every changed line must be executed; `minimum` is the node threshold."""
    if not changed:
        return GateCheck(
            name="coverage", passed=True, detail="no changed lines", basis="changed-lines=0"
        )
    missing = sorted(changed - covered)
    percent = (len(changed) - len(missing)) / len(changed) * 100.0
    if percent < minimum:
        gaps = ", ".join(f"{path}:{line}" for path, line in missing)
        return GateCheck(
            name="coverage",
            passed=False,
            detail=f"{percent:.1f}% < {minimum:.1f}%: uncovered {gaps}",
            basis=f"changed-lines={len(changed)}",
        )
    return GateCheck(
        name="coverage",
        passed=True,
        detail=f"{percent:.1f}% >= {minimum:.1f}%",
        basis=f"changed-lines={len(changed)}",
    )


def _check_behaviour_preserved(coverage: GateCheck, mutation: MutationOutcome) -> GateCheck:
    """Red-phase stand-in for a node whose diff changes no test.

    Coverage alone would pass on tests that touch the changed lines
    without pinning them, so the mutation floor is fixed here rather than
    taken from the node: a planner-chosen `kill_threshold` of 0 would
    otherwise reopen the waiver this gate exists to close.
    """
    if not coverage.passed:
        return GateCheck(
            name="red-phase",
            passed=False,
            detail="tests unchanged and coverage failed; nothing proves the change",
        )
    if mutation.total == 0:
        return GateCheck(
            name="red-phase",
            passed=False,
            detail="tests unchanged and no mutants decided; nothing proves the change",
        )
    percent = 100.0 * mutation.killed / mutation.total
    if percent < REFACTOR_KILL_FLOOR:
        return GateCheck(
            name="red-phase",
            passed=False,
            detail=(
                f"tests unchanged and mutation {percent:.1f}% < "
                f"{REFACTOR_KILL_FLOOR:.1f}%; nothing proves the change"
            ),
        )
    return GateCheck(
        name="red-phase",
        passed=True,
        detail=(
            f"tests unchanged (behaviour preserved); coverage and "
            f"mutation {percent:.1f}% carry the proof"
        ),
    )


def check_red_phase(
    baseline_exits: Sequence[int],
    run_current: Callable[[], int],
    *,
    baseline_output: str,
    changed_files: Collection[str],
    tests_changed: bool,
    kind: str,
    coverage: GateCheck,
    mutation: MutationOutcome,
    red_spec: GateCheck | None = None,
) -> GateCheck:
    """New tests must fail pre-change for a reason the change explains.

    The baseline leg runs the node's own test sources against pre-change
    code, so exit 1 is a genuine assertion failure. A collection error
    (exit 2) counts only when it names a source file the node changed --
    the greenfield case, where the module under test does not exist yet.
    Any other nonzero exit (missing files, usage errors, no tests
    collected) says nothing about the new tests and fails the gate. A
    baseline that hangs is called out separately: it fails like the rest,
    but "tests never ran" would send recovery hunting a missing file
    instead of a loop.

    `baseline_exits` carries RED_PHASE_SAMPLES observations rather than
    one. They must agree: a test that fails on one pre-change run and
    passes on the next yields "fail pre-change, pass post-change" with no
    causal relation to the diff, which is a vacuous red that looks exactly
    like a genuine one.

    The planner cannot waive this: whether it binds is read off the diff.
    A node that leaves every test AST untouched preserved behaviour by
    construction, so no test can fail pre-change; there the proof falls to
    changed-line coverage plus a hard mutation floor, which a tautological
    refactor cannot clear either.
    """
    # A test node has no differential: its tests are the specification
    # and they must fail now, which the tests check already observed, so
    # red-phase mirrors that verdict (`red_spec`) and has no baseline leg
    # (T3-7a). The impl node that depends on it takes the real differential.
    if kind == "test":
        if red_spec is not None and red_spec.passed:
            return GateCheck(
                name="red-phase",
                passed=True,
                detail="red by construction: the specification fails now",
            )
        why = red_spec.detail if red_spec is not None else "no tests verdict to mirror"
        return GateCheck(name="red-phase", passed=False, detail=f"specification is not red: {why}")
    # Only a refactor is behaviour-preserving by construction. An impl
    # node also changes no tests, but its tests were written by the test
    # node it depends on and already fail at its baseline, so it takes the
    # real differential -- grading a behaviour change on a refactor's
    # evidence is how T4 passed while fixing the wrong module.
    if not tests_changed and kind == "refactor":
        return _check_behaviour_preserved(coverage, mutation)
    if len(set(baseline_exits)) > 1:
        seen = ", ".join(str(code) for code in baseline_exits)
        return GateCheck(
            name="red-phase",
            passed=False,
            detail=f"baseline nondeterministic across {len(baseline_exits)} runs "
            f"(exits {seen}); prove nothing",
        )
    baseline_exit = baseline_exits[0]
    if baseline_exit == 0:
        return GateCheck(
            name="red-phase", passed=False, detail="tests pass pre-change; prove nothing"
        )
    if baseline_exit == SHELL_TIMEOUT:
        return GateCheck(
            name="red-phase",
            passed=False,
            detail="baseline hangs: no pre-change verdict; prove nothing",
        )
    if baseline_exit == PYTEST_COLLECTION_ERROR:
        if not _names_changed_source(baseline_output, changed_files):
            return GateCheck(
                name="red-phase",
                passed=False,
                detail="baseline collection error names no changed source; prove nothing",
            )
    elif baseline_exit != PYTEST_TESTS_FAILED:
        return GateCheck(
            name="red-phase",
            passed=False,
            detail=f"baseline exit {baseline_exit}: tests never ran; prove nothing",
        )
    if run_current() != 0:
        return GateCheck(name="red-phase", passed=False, detail="tests fail post-change")
    return GateCheck(name="red-phase", passed=True, detail="fail pre-change, pass post-change")


def _has_property(source: str) -> bool:
    """True when a test module drives at least one hypothesis property."""
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return False
    for node in ast.walk(tree):
        if not isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef):
            continue
        for decorator in node.decorator_list:
            call = decorator.func if isinstance(decorator, ast.Call) else decorator
            name = call.attr if isinstance(call, ast.Attribute) else getattr(call, "id", "")
            if name == "given":
                return True
    return False


def check_target_files(target_files: Collection[str], touched_files: Collection[str]) -> GateCheck:
    """Opt-in localisation (#64): a node that names its files may not touch others.

    Empty `target_files` is unrestricted, so an emitting model that omits
    the field loses nothing; a declared list can only narrow the node's
    own scope, which is the one direction a planner-supplied value may
    move (CLAUDE.md: "A threshold the model itself supplies lets the model
    set its own bar" -- this one cannot lower it). T4's worker fixed the
    wrong module while passing every gate then in force; a node that had
    said which module would have been caught here.
    """
    if not target_files:
        return GateCheck(
            name="target-scope", passed=True, detail="unrestricted: no target_files declared"
        )
    allowed = set(target_files)
    stray = sorted(path for path in touched_files if path not in allowed)
    if stray:
        return GateCheck(
            name="target-scope",
            passed=False,
            detail=f"touched file(s) outside target_files: {', '.join(stray)}",
        )
    return GateCheck(
        name="target-scope",
        passed=True,
        detail=f"{len(touched_files)} touched file(s) within {len(target_files)} target(s)",
    )


def check_property_coverage(
    kind: str,
    test_sources: Mapping[str, str],
    *,
    oracle: MutationOutcome | None = None,
    targets: Collection[str] = (),
) -> GateCheck:
    """A test node must state a property; the impl node must show it bites.

    LLMs "generate ordinary programs following similar patterns seen in
    their massive training corpora, while fuzzing favors unusual inputs
    that cover edge cases". Two happy-path examples over an unbounded
    domain is the predicted output, and it is what T1 produced: a regex
    accepting `.u@example.com` and `user@example..com` behind 7/7 green
    gates (F1).

    Properties are invariants over generated inputs rather than pairs the
    author chose, so the cases they probe are not the cases the author
    already had in mind. A `test` node is bound by presence. An `impl`
    node is bound by the oracle (T3-3): `targets` are the property-bearing
    modules that import a changed module, and `oracle` is a mutation
    sample run with those modules alone as the test set; the property
    must kill at least one of the node's changed-line mutants, or it has
    no discriminating power over the code that implements it (F1's regex
    shipped behind a property that could not tell it from a correct one).
    No targets means no property claims this change and the check is not
    required; targets with no oracle means the runner did not run what it
    should have, which fails rather than passes. A refactor preserves the
    tests it moves and is not bound.
    """
    if kind == "impl":
        return _check_property_oracle(oracle, tuple(targets))
    if kind != "test":
        return GateCheck(name="property-coverage", passed=True, detail=f"{kind} node: not required")
    with_property = sorted(path for path, src in test_sources.items() if _has_property(src))
    if not with_property:
        return GateCheck(
            name="property-coverage",
            passed=False,
            detail="no hypothesis property in the node's tests: examples only",
        )
    return GateCheck(
        name="property-coverage",
        passed=True,
        detail=f"{len(with_property)} module(s) drive a property",
    )


def _check_property_oracle(oracle: MutationOutcome | None, targets: tuple[str, ...]) -> GateCheck:
    name = "property-coverage"
    if not targets:
        return GateCheck(
            name=name, passed=True, detail="not required: no property targets this change"
        )
    by = ", ".join(targets)
    if oracle is None:
        return GateCheck(name=name, passed=False, detail=f"property oracle did not run for {by}")
    if oracle.total == 0:
        return GateCheck(
            name=name, passed=False, detail=f"no mutants sampled for the property oracle ({by})"
        )
    if oracle.killed == 0:
        return GateCheck(
            name=name,
            passed=False,
            detail=f"property killed 0 of {oracle.total} mutant(s): no discriminating power ({by})",
        )
    return GateCheck(
        name=name,
        passed=True,
        detail=f"property killed {oracle.killed} of {oracle.total} mutant(s)",
        basis=f"oracle: killed {oracle.killed} of {oracle.total} mutant(s) by {by}",
    )


def _assertions_by_test(sources: Mapping[str, str]) -> dict[str, set[str]]:
    """Assertion ASTs per test function, keyed by function name.

    Keyed by name rather than by file so relocating a test during a
    refactor is not mistaken for rewriting it.
    """
    found: dict[str, set[str]] = {}
    for source in sources.values():
        try:
            tree = ast.parse(source)
        except SyntaxError:
            continue
        for node in ast.walk(tree):
            if not isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef):
                continue
            if not node.name.startswith("test"):
                continue
            asserts = {ast.dump(stmt) for stmt in ast.walk(node) if isinstance(stmt, ast.Assert)}
            found.setdefault(node.name, set()).update(asserts)
    return found


def check_assertion_preservation(
    kind: str, baseline_tests: Mapping[str, str], current_tests: Mapping[str, str]
) -> GateCheck:
    """Assertions in pre-existing tests are append-only, except for test nodes.

    T4's worker fixed the wrong module and rewrote the behaviour-pinning
    test to match -- `3.03` became `3.02` -- and every Tier-1 gate then in
    force passed (#44). #57 stops an impl node touching tests at all, but a refactor
    may carry code and tests together, and behaviour-preserving means the
    assertions survive the move.

    A `test` node is exempt because repairing stale assertions is its job
    (T2's task was exactly that), and #57 already stops it shipping the
    implementation alongside. That is the whole reason this needs no
    planner-set waiver: the exemption is structural, not chosen by the
    party being graded.
    """
    if kind == "test":
        return GateCheck(
            name="assertion-preservation", passed=True, detail="test node: may restate assertions"
        )
    before = _assertions_by_test(baseline_tests)
    after = _assertions_by_test(current_tests)
    dropped = sorted(name for name, asserts in before.items() if asserts - after.get(name, set()))
    if dropped:
        return GateCheck(
            name="assertion-preservation",
            passed=False,
            detail=f"{kind} node rewrote assertions in: {', '.join(dropped)}",
        )
    return GateCheck(
        name="assertion-preservation",
        passed=True,
        detail=f"{len(before)} pre-existing test(s) keep their assertions",
    )


def check_node_scope(
    kind: str,
    changed_files: Collection[str],
    added_files: Collection[str] = (),
    *,
    may_create: bool,
) -> GateCheck:
    """A node stays on its own side of the test/implementation split, and
    creates files only if it declared `write_file`.

    F5 and #44 are one defect: the worker authors the implementation and
    the tests, so a misreading of the contract is encoded twice and the
    suite it is graded by is the suite it just rewrote. T4's worker fixed
    the wrong module, rewrote the behaviour-pinning test to match, and
    passed every Tier-1 gate then in force.

    An `impl` node may not edit tests; a `test` node may not ship the
    implementation. `refactor` may edit both sides -- a behaviour-preserving
    move carries code and its tests together, and splitting it would leave
    the first node red with nothing able to fix it -- but it may not create
    a file: that is the `impl`/`test` split done under the exempt name (#65).

    `may_create` is the `write_file` binding (T3-4) and it binds every
    kind, ahead of the kind branches: `allowed_tools` was validated
    against the global allowlist and then consumed by nothing, so a plan
    that withheld `write_file` still got a node that could add whatever it
    liked. The probe is T2-2's staged adds, already collected; no new
    subprocess runs for this.
    """
    if added_files and not may_create:
        return GateCheck(
            name="node-scope",
            passed=False,
            detail=(
                "node may not create files: write_file not in allowed_tools; "
                f"added {', '.join(sorted(added_files))}"
            ),
        )
    if kind == "refactor":
        if added_files:
            return GateCheck(
                name="node-scope",
                passed=False,
                detail=f"refactor node added file(s): {', '.join(sorted(added_files))}",
            )
        return GateCheck(
            name="node-scope", passed=True, detail="refactor: edits both sides, adds nothing"
        )
    tests = sorted(path for path in changed_files if _is_test_file(path))
    sources = sorted(path for path in changed_files if not _is_test_file(path))
    stray = tests if kind == "impl" else sources
    if stray:
        other = "test" if kind == "impl" else "source"
        return GateCheck(
            name="node-scope",
            passed=False,
            detail=f"{kind} node changed {other} file(s): {', '.join(stray)}",
        )
    kept = sources if kind == "impl" else tests
    return GateCheck(
        name="node-scope", passed=True, detail=f"{kind} node changed {len(kept)} file(s) in scope"
    )


def check_requirement_binding(
    requirement_ids: Collection[str],
    flipped_tests: Mapping[str, str],
    *,
    planned_ids: Collection[str] = (),
) -> GateCheck:
    """Every declared requirement is cited, and every citation is planned.

    The second half is traceSDD's orphan rule: an ID cited in code that
    nobody declared is a hallucinated requirement, and detecting it is
    what stops the binding being satisfiable in both directions. A
    worker free to invent IDs can tag whatever it likes and the gate
    still reads green -- F5's circularity, which survives adding
    statements unless citations are constrained to the declared set.

    `planned_ids` widens that set to the ids other nodes of the same plan
    declare (T3-24): the test sources are read suite-wide, so a plan that
    gives its `test` node `REQ-001` and its `impl` node `REQ-002` cites
    both ids in one file, and with the node's own ids alone no node of
    such a plan can pass (session 20b). The first half is untouched: an
    id the node itself declares must be cited, whatever the plan holds.
    """
    unbound = sorted(
        req
        for req in requirement_ids
        if not any(req in source for source in flipped_tests.values())
    )
    if unbound:
        return GateCheck(
            name="requirement-binding",
            passed=False,
            detail=f"unbound requirements: {', '.join(unbound)}",
        )
    declared = set(requirement_ids)
    cited = {
        found for source in flipped_tests.values() for found in REQUIREMENT_CITATION.findall(source)
    }
    orphans = sorted(cited - declared - set(planned_ids))
    if orphans:
        return GateCheck(
            name="requirement-binding",
            passed=False,
            detail=f"undeclared requirements cited: {', '.join(orphans)}",
        )
    return GateCheck(
        name="requirement-binding",
        passed=True,
        detail=f"{len(list(requirement_ids))} requirement(s) bound",
    )


@dataclass(frozen=True)
class Tier1Inputs:
    """Collected evidence for one node: subprocess-free, runner-injected."""

    sources: Mapping[str, str]
    ruff_files: Collection[str]
    ruff_runner: Callable[[list[str]], int]
    test_runner: Callable[[str], int]
    changed: set[tuple[str, int]]
    covered: set[tuple[str, int]]
    baseline_exits: tuple[int, ...]
    baseline_output: str
    baseline_tests: Mapping[str, str]
    tests_changed: bool
    current_runner: Callable[[], int]
    flipped_tests: Mapping[str, str]
    mutation: MutationOutcome
    added_files: Collection[str] = ()
    # Repo-relative paths of every file the node changed or added (T3-2).
    touched_files: Collection[str] = ()
    # The current suite run's captured text and the worktree's importable
    # top-level names; read only by a `test` node's red-specification
    # verdict (T3-7a).
    test_output: str = ""
    workdir_modules: Collection[str] = ()
    # Every id some node of the plan declares; the orphan half of
    # requirement-binding subtracts these before rejecting a citation
    # (T3-24). Empty means the node's own ids are the whole plan.
    planned_requirements: tuple[str, ...] = ()
    # The property oracle (T3-3): `property_targets` are the property-bearing
    # test modules that import a changed module, and `property_oracle` is the
    # mutation sample run with those modules alone, or None when the runner
    # did not run it. Read only for `impl` nodes.
    property_oracle: MutationOutcome | None = None
    property_targets: tuple[str, ...] = ()


@dataclass(frozen=True)
class Tier1Result:
    """Aggregated Tier-1 verdict: every check runs, `passed` needs all green."""

    node_id: str
    passed: bool
    checks: tuple[GateCheck, ...]


def check_mutation(outcome: MutationOutcome, threshold: float) -> GateCheck:
    """Changed-line kill-rate must clear `threshold`; small samples take no
    partial credit, and an empty sample is no evidence rather than a pass.

    The old fail-open returned PASS when nothing was generated, reasoning
    that a change with no mutable surface cannot be under-tested. The
    premise is false: mutmut only mutates function bodies, so a
    module-scope `_RE = re.compile(...)` yields 0 mutants where the same
    expression inline yields 7. T7 added a 218-line module, generated
    nothing, and the gate passed an infinite loop (F12).

    The threshold cannot rescue a thin sample either. T1's four-line
    regex admitted 2 mutants, two shallow tests killed both, and the gate
    read 100% over a validator that accepts `user@example..com` (F1).
    Below MIN_SIGNIFICANT_MUTANTS a percentage is noise, so every mutant
    must die -- fewer mutants means a stricter bar, not a cheaper one.
    """
    if outcome.total == 0:
        if outcome.survivors:
            cause = ", ".join(sorted(outcome.survivors)[:5])
            # A tool that never ran is named as such (T3-20); "no mutants
            # decided" describes a run that happened.
            failed_tool = any(s.startswith("mutmut run exited") for s in outcome.survivors)
            detail = (
                f"mutation tool failed: {cause}" if failed_tool else f"no mutants decided: {cause}"
            )
            return GateCheck(name="mutation", passed=False, detail=detail, basis="sampled n=0")
        if outcome.generated == 0:
            return GateCheck(
                name="mutation",
                passed=False,
                detail="no mutants on changed lines: mutation provided no evidence",
                basis="sampled n=0",
            )
        return GateCheck(
            name="mutation", passed=False, detail="no mutants decided", basis="sampled n=0"
        )
    percent = 100.0 * outcome.killed / outcome.total
    small = outcome.total < MIN_SIGNIFICANT_MUTANTS
    required = 100.0 if small else threshold
    if percent < required:
        shown = ", ".join(sorted(outcome.survivors)[:5])
        note = f" (small sample: {outcome.total} mutant(s), all must die)" if small else ""
        return GateCheck(
            name="mutation",
            passed=False,
            detail=f"{percent:.1f}% < {required:.1f}%{note}: survived {shown}",
            basis=f"sampled n={outcome.total}",
        )
    return GateCheck(
        name="mutation",
        passed=True,
        detail=f"{percent:.1f}% >= {required:.1f}% over {outcome.total} mutant(s)",
        basis=f"sampled n={outcome.total}",
    )


def _not_required(name: str) -> GateCheck:
    """A `test` node changes no source, so source-only evidence is moot (T3-7a)."""
    return GateCheck(
        name=name, passed=True, detail="not required: no source changed", basis="test node"
    )


def run_tier1(node: Node, inputs: Tier1Inputs) -> Tier1Result:
    """Run all eleven Tier-1 checks against `node`'s gate spec and aggregate.

    A `test` node is a red specification (T3-7a): its tests check inverts,
    red-phase mirrors that verdict, and the two source-only checks
    (coverage, mutation) are substituted with "not required" so the
    order pin and the count stay the same for every kind.
    """
    gate = node.deterministic_gate
    sample = gate.mutation_sample
    is_spec = node.kind == "test"
    syntax = check_syntax(inputs.sources)
    ruff = check_ruff(inputs.ruff_files, inputs.ruff_runner)
    tests = check_test_command(
        gate.test_command,
        inputs.test_runner,
        kind=node.kind,
        output=inputs.test_output,
        workdir_modules=inputs.workdir_modules,
    )
    coverage = (
        _not_required("coverage")
        if is_spec
        else check_changed_line_coverage(
            inputs.changed, inputs.covered, gate.changed_line_coverage_min
        )
    )
    checks = (
        syntax,
        ruff,
        tests,
        coverage,
        check_red_phase(
            inputs.baseline_exits,
            inputs.current_runner,
            baseline_output=inputs.baseline_output,
            changed_files=sorted({path for path, _ in inputs.changed}),
            tests_changed=inputs.tests_changed,
            kind=node.kind,
            coverage=coverage,
            mutation=inputs.mutation,
            red_spec=tests,
        ),
        check_node_scope(
            node.kind,
            sorted({path for path, _ in inputs.changed}),
            inputs.added_files,
            may_create="write_file" in node.execution_constraints.allowed_tools,
        ),
        check_target_files(node.target_files, inputs.touched_files),
        check_property_coverage(
            node.kind,
            inputs.flipped_tests,
            oracle=inputs.property_oracle,
            targets=inputs.property_targets,
        ),
        check_assertion_preservation(node.kind, inputs.baseline_tests, inputs.flipped_tests),
        check_requirement_binding(
            node.requirement_ids, inputs.flipped_tests, planned_ids=inputs.planned_requirements
        ),
        _not_required("mutation")
        if is_spec
        else check_mutation(inputs.mutation, sample.kill_threshold),
    )
    return Tier1Result(node_id=node.id, passed=all(check.passed for check in checks), checks=checks)
