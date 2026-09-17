"""Tests for saddle.runner: source reading plus end-to-end node gating."""

from __future__ import annotations

import os
from pathlib import Path

from saddle.dag import Node
from saddle.evidence import CapturedRun, run_argv
from saddle.journal import SpanRecorder, read_spans
from saddle.runner import read_sources, run_node_gate


def _node(test_command: str = "pytest test_n.py") -> Node:
    return Node.model_validate(
        {
            "id": "n1",
            "dependencies": [],
            "task_prompt": "Fix f.",
            "requirement_ids": ["REQ-001"],
            "execution_constraints": {
                "reasoning_budget": "low",
                "allowed_tools": ["read_file"],
                "max_context_tokens": 5000,
            },
            "deterministic_gate": {
                "test_command": test_command,
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


def _worktree(
    root: Path,
    test_body: str,
    *,
    test_name: str = "test_n.py",
    baseline_code: str = "def f():\n    return 1\n",
    fixed_code: str = "def f():\n    return 2\n",
    baseline_test: str | None = None,
) -> None:
    setup = (
        ["git", "init"],
        ["git", "config", "user.email", "test@example.com"],
        ["git", "config", "user.name", "test"],
    )
    for argv in setup:
        assert run_argv(argv, root) == 0
    (root / "n.py").write_text(baseline_code)
    assert run_argv(["git", "add", "n.py"], root) == 0
    if baseline_test is not None:
        (root / test_name).write_text(baseline_test)
        assert run_argv(["git", "add", test_name], root) == 0
    assert run_argv(["git", "commit", "-m", "baseline"], root) == 0
    (root / "n.py").write_text(fixed_code)
    (root / test_name).write_text(test_body)
    assert run_argv(["git", "add", "-A"], root) == 0


def test_read_sources_maps_relative_paths(tmp_path: Path) -> None:
    (tmp_path / "n.py").write_text("x = 1\n")
    sub = tmp_path / "pkg"
    sub.mkdir()
    (sub / "m.py").write_text("y = 2\n")
    (sub / "note.txt").write_text("ignored\n")
    assert read_sources(tmp_path, "*.py") == {"n.py": "x = 1\n", "pkg/m.py": "y = 2\n"}


def test_run_node_gate_end_to_end_pass(tmp_path: Path) -> None:
    test_body = (
        "from n import f\n\n\ndef test_f_returns_fixed_value():  # REQ-001\n    assert f() == 2\n"
    )
    _worktree(tmp_path, test_body)
    result = run_node_gate(_node(), tmp_path)
    assert result.node_id == "n1"
    assert result.passed is True
    assert all(check.passed for check in result.checks)
    assert (tmp_path / ".coverage.tier1").is_file()


def test_run_node_gate_records_tool_spans(tmp_path: Path) -> None:
    test_body = (
        "from n import f\n\n\ndef test_f_returns_fixed_value():  # REQ-001\n    assert f() == 2\n"
    )
    _worktree(tmp_path, test_body)
    journal = tmp_path / "proofs.jsonl"
    result = run_node_gate(_node(), tmp_path, recorder=SpanRecorder(path=journal, node_id="n1"))
    assert result.passed is True
    spans = read_spans(journal)
    assert [span.name for span in spans] == ["git", "coverage", "git", "pytest", "ruff", "ruff"]
    assert all(span.node_id == "n1" for span in spans)
    assert [span.exit_code for span in spans] == [0, 0, 0, 4, 0, 0]


def test_run_node_gate_capture_collects_suite_and_ruff_runs(tmp_path: Path) -> None:
    test_body = (
        "from n import f\n\n\ndef test_f_returns_fixed_value():  # REQ-001\n    assert f() == 2\n"
    )
    _worktree(tmp_path, test_body)
    captured: list[CapturedRun] = []
    result = run_node_gate(_node(), tmp_path, capture=captured)
    assert result.passed is True
    assert [run.argv[0] for run in captured] == ["coverage", "ruff", "ruff"]
    assert all(run.exit_code == 0 for run in captured)
    assert "1 passed" in captured[0].stdout


def test_run_node_gate_ignores_stale_bytecode(tmp_path: Path) -> None:
    passing = (
        "from n import f\n\n\ndef test_f_returns_fixed_value():  # REQ-001\n    assert f() == 2\n"
    )
    _worktree(tmp_path, passing)
    assert run_node_gate(_node(), tmp_path).passed is True
    assert list(tmp_path.rglob("__pycache__")), "expected pytest to mint bytecode caches"
    failing = passing.replace("assert f() == 2", "assert f() == 3")
    assert len(failing) == len(passing)
    target = tmp_path / "test_n.py"
    mtime = target.stat().st_mtime
    target.write_text(failing)
    os.utime(target, (mtime, mtime))
    assert run_argv(["git", "add", "-A"], tmp_path) == 0
    result = run_node_gate(_node(), tmp_path)
    assert result.passed is False
    assert [check.name for check in result.checks if not check.passed] == [
        "tests",
        "red-phase",
    ]


def test_run_node_gate_unbound_requirement_fails(tmp_path: Path) -> None:
    test_body = "from n import f\n\n\ndef test_f_returns_fixed_value():\n    assert f() == 2\n"
    _worktree(tmp_path, test_body)
    result = run_node_gate(_node(), tmp_path)
    assert result.passed is False
    binding = next(check for check in result.checks if check.name == "requirement-binding")
    assert binding.passed is False
    assert "REQ-001" in binding.detail


def test_run_node_gate_uncovered_line_fails(tmp_path: Path) -> None:
    fixed = "def f():\n    return 2\n\n\ndef unused():\n    return 3\n"
    test_body = "from n import f\n\n\ndef test_f():  # REQ-001\n    assert f() == 2\n"
    _worktree(tmp_path, test_body, fixed_code=fixed)
    result = run_node_gate(_node(), tmp_path)
    assert result.passed is False
    coverage = next(check for check in result.checks if check.name == "coverage")
    assert coverage.passed is False
    assert "n.py:6" in coverage.detail


def test_run_node_gate_pass_pre_change_fails(tmp_path: Path) -> None:
    test_body = "from n import f\n\n\ndef test_f():  # REQ-001\n    assert f() in (1, 2)\n"
    _worktree(tmp_path, test_body, baseline_test=test_body)
    result = run_node_gate(_node(), tmp_path)
    assert result.passed is False
    red = next(check for check in result.checks if check.name == "red-phase")
    assert red.passed is False


def test_run_node_gate_lint_dirty_fails(tmp_path: Path) -> None:
    fixed = "import os\n\n\ndef f():\n    return 2\n"
    test_body = "from n import f\n\n\ndef test_f():  # REQ-001\n    assert f() == 2\n"
    _worktree(tmp_path, test_body, fixed_code=fixed)
    result = run_node_gate(_node(), tmp_path)
    assert result.passed is False
    ruff = next(check for check in result.checks if check.name == "ruff")
    assert ruff.passed is False


def test_run_node_gate_suffix_style_test_binds(tmp_path: Path) -> None:
    test_body = "from n import f\n\n\ndef test_f():  # REQ-001\n    assert f() == 2\n"
    _worktree(tmp_path, test_body, test_name="n_test.py")
    result = run_node_gate(_node("pytest n_test.py"), tmp_path)
    assert result.passed is True
