"""Tests for saddle.gates: one killer fixture per Tier-1 check."""

from __future__ import annotations

from dataclasses import replace

from saddle.dag import Node
from saddle.gates import (
    Tier1Inputs,
    check_changed_line_coverage,
    check_red_phase,
    check_requirement_binding,
    check_ruff,
    check_syntax,
    check_test_command,
    run_tier1,
)


def _node() -> Node:
    return Node.model_validate(
        {
            "id": "n1",
            "dependencies": [],
            "task_prompt": "Do n1.",
            "requirement_ids": ["REQ-001"],
            "execution_constraints": {
                "reasoning_budget": "low",
                "allowed_tools": ["read_file"],
                "max_context_tokens": 5000,
            },
            "deterministic_gate": {
                "test_command": "pytest tests/test_n1.py",
                "changed_line_coverage_min": 100.0,
                "red_phase_required": True,
                "mutation_sample": {
                    "scope": "changed-lines",
                    "max_mutants": 10,
                    "kill_threshold": 85.0,
                },
            },
        }
    )


def _passing_inputs() -> Tier1Inputs:
    return Tier1Inputs(
        sources={"n1.py": "x = 1\n"},
        ruff_files=["n1.py"],
        ruff_runner=lambda _argv: 0,
        test_runner=lambda _cmd: 0,
        changed={("n1.py", 1)},
        covered={("n1.py", 1)},
        baseline_runner=lambda: 1,
        current_runner=lambda: 0,
        flipped_tests={"test_a": "def test_a():  # REQ-001\n    assert True\n"},
    )


def test_syntax_killer_fixture_invalid_syntax_fails() -> None:
    check = check_syntax({"node.py": "def broken(:\n    pass\n"})
    assert check.passed is False
    assert check.name == "syntax"
    assert "node.py" in check.detail


def test_ruff_killer_fixture_lint_failure_fails() -> None:
    def run(argv: list[str]) -> int:
        return 1 if argv[1] == "check" else 0

    check = check_ruff(["n.py"], run)
    assert check.passed is False
    assert check.name == "ruff"
    assert "check exited 1" in check.detail


def test_ruff_format_failure_fails() -> None:
    def run(argv: list[str]) -> int:
        return 0 if argv[1] == "check" else 2

    check = check_ruff(["n.py"], run)
    assert check.passed is False
    assert "format exited 2" in check.detail


def test_ruff_clean_passes() -> None:
    seen: list[list[str]] = []

    def run(argv: list[str]) -> int:
        seen.append(argv)
        return 0

    check = check_ruff(["b.py", "a.py"], run)
    assert check.passed is True
    assert check.detail == "2 file(s) clean"
    assert seen == [
        ["ruff", "check", "a.py", "b.py"],
        ["ruff", "format", "--check", "a.py", "b.py"],
    ]


def test_ruff_no_files_passes_without_running() -> None:
    seen: list[list[str]] = []

    def run(argv: list[str]) -> int:
        seen.append(argv)
        return 0

    check = check_ruff([], run)
    assert check.passed is True
    assert check.name == "ruff"
    assert check.detail == "no files to lint"
    assert seen == []


def test_syntax_valid_sources_pass() -> None:
    check = check_syntax({"node.py": "def ok():\n    return 1\n"})
    assert check.passed is True
    assert check.detail == "1 file(s) parsed"


def test_tests_killer_fixture_failing_command_fails() -> None:
    check = check_test_command("pytest tests/test_x.py", lambda _cmd: 1)
    assert check.passed is False
    assert check.name == "tests"
    assert "exited 1" in check.detail


def test_tests_passing_command_passes() -> None:
    seen: list[str] = []

    def run(cmd: str) -> int:
        seen.append(cmd)
        return 0

    check = check_test_command("pytest tests/test_x.py", run)
    assert check.passed is True
    assert check.detail == "'pytest tests/test_x.py' exited 0"
    assert seen == ["pytest tests/test_x.py"]


def test_coverage_killer_fixture_uncovered_line_fails() -> None:
    check = check_changed_line_coverage({("node.py", 3), ("node.py", 4)}, set(), 100.0)
    assert check.passed is False
    assert check.name == "coverage"
    assert check.detail == "0.0% < 100.0%: uncovered node.py:3, node.py:4"


def test_coverage_full_cover_and_empty_diff_pass() -> None:
    full = check_changed_line_coverage({("node.py", 3)}, {("node.py", 3)}, 100.0)
    assert full.passed is True
    assert full.name == "coverage"
    assert full.detail == "100.0% >= 100.0%"
    empty = check_changed_line_coverage(set(), set(), 100.0)
    assert empty.passed is True
    assert empty.name == "coverage"
    assert empty.detail == "no changed lines"


def test_coverage_partial_percent_compared_to_minimum() -> None:
    changed = {("n.py", 1), ("n.py", 2), ("n.py", 3)}
    check = check_changed_line_coverage(changed, {("n.py", 1), ("n.py", 2)}, 67.0)
    assert check.passed is False
    assert check.detail.startswith("66.7% < 67.0%")


def test_red_phase_killer_fixture_pass_pre_change_fails() -> None:
    check = check_red_phase(lambda: 0, lambda: 0, required=True)
    assert check.passed is False
    assert check.name == "red-phase"
    assert check.detail == "tests pass pre-change; prove nothing"


def test_red_phase_fail_post_change_fails() -> None:
    check = check_red_phase(lambda: 1, lambda: 1, required=True)
    assert check.passed is False
    assert check.name == "red-phase"
    assert check.detail == "tests fail post-change"


def test_red_phase_flip_and_opt_out_pass() -> None:
    flip = check_red_phase(lambda: 1, lambda: 0, required=True)
    assert flip.passed is True
    assert flip.name == "red-phase"
    assert flip.detail == "fail pre-change, pass post-change"
    skipped = check_red_phase(lambda: 0, lambda: 0, required=False)
    assert skipped.passed is True
    assert skipped.name == "red-phase"
    assert skipped.detail == "not required"


def test_binding_killer_fixture_unbound_requirement_fails() -> None:
    check = check_requirement_binding(
        ["REQ-001", "REQ-002"], {"test_a": "def test_a():\n    assert True\n"}
    )
    assert check.passed is False
    assert check.name == "requirement-binding"
    assert check.detail == "unbound requirements: REQ-001, REQ-002"


def test_binding_all_bound_passes() -> None:
    check = check_requirement_binding(
        ["REQ-001"], {"test_a": "def test_a():  # REQ-001\n    assert True\n"}
    )
    assert check.passed is True
    assert check.detail == "1 requirement(s) bound"


def test_run_tier1_all_green_passes() -> None:
    seen: list[str] = []

    def run(cmd: str) -> int:
        seen.append(cmd)
        return 0

    result = run_tier1(_node(), replace(_passing_inputs(), test_runner=run))
    assert result.node_id == "n1"
    assert result.passed is True
    assert seen == ["pytest tests/test_n1.py"]
    assert [check.name for check in result.checks] == [
        "syntax",
        "ruff",
        "tests",
        "coverage",
        "red-phase",
        "requirement-binding",
    ]
    by_name = {check.name: check for check in result.checks}
    assert by_name["coverage"].detail == "100.0% >= 100.0%"
    assert by_name["red-phase"].detail == "fail pre-change, pass post-change"


def test_run_tier1_one_red_check_fails_but_all_run() -> None:
    bad = replace(_passing_inputs(), sources={"n1.py": "def broken(:\n"})
    result = run_tier1(_node(), bad)
    assert result.passed is False
    assert len(result.checks) == 6
    assert result.checks[0].passed is False
    assert all(check.passed for check in result.checks[1:])
