"""Tier-1 per-node gates (ARCHITECTURE.md §3 Phase 3 Tier 1).

Checks run in gate order: lint/syntax, tests, changed-line coverage,
red-phase, requirement binding, sampled mutation. Each check is a small
pure function so killer fixtures stay fast and deterministic; subprocess
runners are injected at the boundary, never embedded in the predicates.
"""

from __future__ import annotations

import ast
from collections.abc import Callable, Collection, Mapping, Sequence
from dataclasses import dataclass
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
# Baseline runs sampled per red-phase check. Red-phase is the only gate
# that reasons over two runs, so its evidence is worth exactly what the
# stability of the pre-change leg is worth; one observation cannot tell a
# genuine failure from a flake.
RED_PHASE_SAMPLES: Final = 3
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
    """Outcome of one Tier-1 check: `passed` plus human-readable `detail`."""

    name: str
    passed: bool
    detail: str = ""


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
    if lint_code != 0 or format_code != 0:
        return GateCheck(
            name="ruff",
            passed=False,
            detail=f"ruff check exited {lint_code}, format exited {format_code}",
        )
    return GateCheck(name="ruff", passed=True, detail=f"{len(ordered)} file(s) clean")


def check_test_command(test_command: str, run: Callable[[str], int]) -> GateCheck:
    """Run the node's declared pytest scope; nonzero exit fails the gate.

    A timeout is reported as a hang rather than as an exit code. The two
    are different defects: a failing assertion names the behaviour it
    disagrees with, while a suite that never terminates yields no verdict
    at all -- downstream harnesses that parse pytest counts read partial
    or zero results, so a hang scores worse than the failure it hides.
    """
    exit_code = run(test_command)
    if exit_code == SHELL_TIMEOUT:
        return GateCheck(
            name="tests",
            passed=False,
            detail=f"{test_command!r} hangs: no verdict within the time limit",
        )
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
        return GateCheck(name="coverage", passed=True, detail="no changed lines")
    missing = sorted(changed - covered)
    percent = (len(changed) - len(missing)) / len(changed) * 100.0
    if percent < minimum:
        gaps = ", ".join(f"{path}:{line}" for path, line in missing)
        return GateCheck(
            name="coverage",
            passed=False,
            detail=f"{percent:.1f}% < {minimum:.1f}%: uncovered {gaps}",
        )
    return GateCheck(name="coverage", passed=True, detail=f"{percent:.1f}% >= {minimum:.1f}%")


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
    coverage: GateCheck,
    mutation: MutationOutcome,
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
    if not tests_changed:
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


def check_requirement_binding(
    requirement_ids: Collection[str], flipped_tests: Mapping[str, str]
) -> GateCheck:
    """Each claimed requirement must name-check in ≥1 flipped test's source."""
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
    tests_changed: bool
    current_runner: Callable[[], int]
    flipped_tests: Mapping[str, str]
    mutation: MutationOutcome


@dataclass(frozen=True)
class Tier1Result:
    """Aggregated Tier-1 verdict: every check runs, `passed` needs all green."""

    node_id: str
    passed: bool
    checks: tuple[GateCheck, ...]


def check_mutation(outcome: MutationOutcome, threshold: float) -> GateCheck:
    """Sampled changed-line kill-rate must clear `threshold`; never waived."""
    if outcome.total == 0:
        if outcome.survivors:
            cause = ", ".join(sorted(outcome.survivors)[:5])
            return GateCheck(name="mutation", passed=False, detail=f"no mutants decided: {cause}")
        if outcome.generated == 0:
            return GateCheck(name="mutation", passed=True, detail="no mutants on changed lines")
        return GateCheck(name="mutation", passed=False, detail="no mutants decided")
    percent = 100.0 * outcome.killed / outcome.total
    if percent < threshold:
        shown = ", ".join(sorted(outcome.survivors)[:5])
        return GateCheck(
            name="mutation",
            passed=False,
            detail=f"{percent:.1f}% < {threshold:.1f}%: survived {shown}",
        )
    return GateCheck(name="mutation", passed=True, detail=f"{percent:.1f}% >= {threshold:.1f}%")


def run_tier1(node: Node, inputs: Tier1Inputs) -> Tier1Result:
    """Run all seven Tier-1 checks against `node`'s gate spec and aggregate."""
    gate = node.deterministic_gate
    sample = gate.mutation_sample
    coverage = check_changed_line_coverage(
        inputs.changed, inputs.covered, gate.changed_line_coverage_min
    )
    checks = (
        check_syntax(inputs.sources),
        check_ruff(inputs.ruff_files, inputs.ruff_runner),
        check_test_command(gate.test_command, inputs.test_runner),
        coverage,
        check_red_phase(
            inputs.baseline_exits,
            inputs.current_runner,
            baseline_output=inputs.baseline_output,
            changed_files=sorted({path for path, _ in inputs.changed}),
            tests_changed=inputs.tests_changed,
            coverage=coverage,
            mutation=inputs.mutation,
        ),
        check_requirement_binding(node.requirement_ids, inputs.flipped_tests),
        check_mutation(inputs.mutation, sample.kill_threshold),
    )
    return Tier1Result(node_id=node.id, passed=all(check.passed for check in checks), checks=checks)
