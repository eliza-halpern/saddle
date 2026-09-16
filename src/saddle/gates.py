"""Tier-1 per-node gates (ARCHITECTURE.md §3 Phase 3 Tier 1).

Checks run in gate order: lint/syntax, tests, changed-line coverage,
red-phase, requirement binding. Each check is a small pure function so
killer fixtures stay fast and deterministic; subprocess runners are
injected at the boundary, never embedded in the predicates.
"""

from __future__ import annotations

import ast
from collections.abc import Callable, Collection, Mapping
from dataclasses import dataclass

from saddle.dag import Node


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
    """Run the node's declared pytest scope; nonzero exit fails the gate."""
    exit_code = run(test_command)
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


def check_red_phase(
    run_baseline: Callable[[], int], run_current: Callable[[], int], *, required: bool
) -> GateCheck:
    """New tests must fail pre-change (baseline) and pass post-change."""
    if not required:
        return GateCheck(name="red-phase", passed=True, detail="not required")
    if run_baseline() == 0:
        return GateCheck(
            name="red-phase", passed=False, detail="tests pass pre-change; prove nothing"
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
    baseline_runner: Callable[[], int]
    current_runner: Callable[[], int]
    flipped_tests: Mapping[str, str]


@dataclass(frozen=True)
class Tier1Result:
    """Aggregated Tier-1 verdict: every check runs, `passed` needs all green."""

    node_id: str
    passed: bool
    checks: tuple[GateCheck, ...]


def run_tier1(node: Node, inputs: Tier1Inputs) -> Tier1Result:
    """Run all five Tier-1 checks against `node`'s gate spec and aggregate."""
    gate = node.deterministic_gate
    checks = (
        check_syntax(inputs.sources),
        check_ruff(inputs.ruff_files, inputs.ruff_runner),
        check_test_command(gate.test_command, inputs.test_runner),
        check_changed_line_coverage(inputs.changed, inputs.covered, gate.changed_line_coverage_min),
        check_red_phase(
            inputs.baseline_runner, inputs.current_runner, required=gate.red_phase_required
        ),
        check_requirement_binding(node.requirement_ids, inputs.flipped_tests),
    )
    return Tier1Result(node_id=node.id, passed=all(check.passed for check in checks), checks=checks)
