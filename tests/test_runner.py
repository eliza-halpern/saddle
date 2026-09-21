"""Tests for saddle.runner: source reading plus end-to-end node gating."""

from __future__ import annotations

import ast
import os
import stat
from pathlib import Path
from typing import Final

import pytest

from saddle.dag import Node
from saddle.evidence import CapturedRun, run_argv
from saddle.gates import MIN_SIGNIFICANT_MUTANTS, RED_PHASE_SAMPLES
from saddle.journal import SpanRecorder, read_spans
from saddle.runner import _stub_module, read_sources, run_node_gate

# Every tool name the global allowlist carries (T3-4). A node listing all
# four behaves exactly as it did before each name was bound to a harness
# behaviour, so this is the fixtures' known-good default; a test that pins
# one binding passes a shorter list.
ALL_TOOLS: Final[tuple[str, ...]] = ("read_file", "write_file", "run_tests", "lint")


def _node(
    test_command: str = "pytest test_n.py",
    kill_threshold: float = 85.0,
    max_mutants: int = 100,
    kind: str = "impl",
    target_files: list[str] | None = None,
    tools: list[str] | None = None,
) -> Node:
    return Node.model_validate(
        {
            "id": "n1",
            "kind": kind,
            "target_files": target_files or [],
            "dependencies": [],
            "task_prompt": "Fix f.",
            "requirements": [
                {"id": "REQ-001", "statement": "REQ-001 holds.", "accepts": ["2"], "rejects": ["3"]}
            ],
            "execution_constraints": {
                "reasoning_budget": "low",
                # All four by default (T3-4). Several tests below create a
                # test file at baseline; without `write_file` node-scope
                # would fail them for a reason they do not assert on, and
                # the extra red would sit silent behind the check they do.
                "allowed_tools": tools if tools is not None else list(ALL_TOOLS),
                "max_context_tokens": 8000,
            },
            "deterministic_gate": {
                "test_command": test_command,
                "changed_line_coverage_min": 100.0,
                "red_phase_required": True,
                "mutation_sample": {
                    "scope": "changed-lines",
                    "max_mutants": max_mutants,
                    "kill_threshold": kill_threshold,
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
    # Any test file already in `root` is earlier sealed work: it goes in
    # the baseline commit, not the node's diff (F21.12a fixture).
    for earlier in sorted(root.glob("test_*.py")):
        assert run_argv(["git", "add", earlier.name], root) == 0
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
    _worktree(tmp_path, test_body, baseline_test=test_body)
    result = run_node_gate(_node(), tmp_path)
    assert result.node_id == "n1"
    assert result.passed is True
    assert all(check.passed for check in result.checks)
    assert (tmp_path / ".coverage.tier1").is_file()


def test_run_node_gate_full_sample_catches_what_a_small_cap_hid(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    test_body = (
        "from n import f\n\n\ndef test_f_returns_fixed_value():  # REQ-001\n    assert f() == 2\n"
    )
    _worktree(tmp_path, test_body, baseline_test=test_body)
    stub_dir = tmp_path / "stub"
    stub_dir.mkdir()
    (stub_dir / "results.txt").write_text("  m1: killed\n  m2: survived\n")
    (stub_dir / "show_m1.txt").write_text(
        "--- n.py\n+++ n.py\n@@ -2 +2 @@\n-    return 2\n+    return 3\n"
    )
    (stub_dir / "show_m2.txt").write_text(
        "--- n.py\n+++ n.py\n@@ -2 +2 @@\n-    return 2\n+    return 4\n"
    )
    script = stub_dir / "mutmut"
    script.write_text(
        "#!/bin/sh\n"
        f'STUB_DIR="{stub_dir}"\n'
        'case "$1" in\n'
        "  run) exit 0;;\n"
        '  results) cat "$STUB_DIR/results.txt";;\n'
        '  show) cat "$STUB_DIR/show_$2.txt";;\n'
        "esac\n"
    )
    script.chmod(script.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    monkeypatch.setenv("PATH", f"{stub_dir}{os.pathsep}{os.environ['PATH']}")
    # This test used to pass `max_mutants=1` and assert `100.0% >= 85.0%`
    # -- a clean PASS drawn from a one-mutant sample while `m2` survived
    # unexamined. That was the exploit the schema floor now closes, and
    # the suite had it recorded as expected behaviour. With the cap pinned
    # at ARCHITECTURE.md's 100, both mutants are sampled and the survivor
    # fails the node.
    result = run_node_gate(_node(kill_threshold=85.0), tmp_path)
    assert result.passed is False
    by_name = {check.name: check for check in result.checks}
    assert by_name["mutation"].detail == (
        "50.0% < 100.0% (small sample: 2 mutant(s), all must die): survived 1: m2"
    )


def test_run_node_gate_records_tool_spans(tmp_path: Path) -> None:
    test_body = (
        "from n import f\n\n\ndef test_f_returns_fixed_value():  # REQ-001\n    assert f() == 2\n"
    )
    # This pins the RED_PHASE_SAMPLES multi-sampling span shape, which only
    # fires when the node's test signature differs from baseline. An `impl`
    # node's test is unchanged at its own baseline, so this is a `refactor`
    # node that edits both sides: the test exists at baseline with a weaker
    # assertion and the node appends the binding one (append-only, so
    # assertion-preservation holds; no file is created, so node-scope holds).
    baseline_test = test_body.replace("    assert f() == 2\n", "    assert f() is not None\n")
    test_body = test_body.replace(
        "    assert f() == 2\n", "    assert f() is not None\n    assert f() == 2\n"
    )
    _worktree(tmp_path, test_body, baseline_test=baseline_test)
    journal = tmp_path / "proofs.jsonl"
    result = run_node_gate(
        _node(kind="refactor"), tmp_path, recorder=SpanRecorder(path=journal, node_id="n1")
    )
    assert result.passed is True
    spans = read_spans(journal)
    assert [span.name for span in spans] == [
        "git",
        # T2-2: the staged-adds probe behind node-scope's file-creation rule.
        "git",
        # T3-2: the changed-files list behind target-scope.
        "git",
        "coverage",
        "git",
        # T6-3: the ruff baseline leg on the snapshot, before red-phase
        # writes stubs and tests into it.
        "ruff",
        # One coverage span per red-phase baseline sample: the pre-change
        # leg is observed RED_PHASE_SAMPLES times so a flaky failure
        # cannot pass as a genuine red.
        *["coverage"] * RED_PHASE_SAMPLES,
        # T6-3: both ruff legs on the current tree run in the runner, before
        # the mutation sample, not inside the gate predicate.
        "ruff",
        "ruff",
        "timeout",
        # `results`, then one `show` per mutant in the sample.
        "mutmut",
        *["mutmut"] * MIN_SIGNIFICANT_MUTANTS,
    ]
    assert all(span.node_id == "n1" for span in spans)
    # Spans 4..6 are the red-phase baseline samples: same coverage-wrapped
    # command as the current leg, exiting 1 because the node's own test
    # genuinely fails against pre-change code. It used to be a raw pytest
    # exiting 4 -- file not found, because the new test was never copied in.
    # The baseline samples all exit 1: unanimous, which is what a stable
    # pre-change leg looks like. Disagreement here is what fails the gate.
    assert [span.exit_code for span in spans] == [
        0,
        0,
        0,
        0,
        0,
        0,
        *[1] * RED_PHASE_SAMPLES,
        *[0] * (2 + MIN_SIGNIFICANT_MUTANTS + 2),
    ]


def test_run_node_gate_capture_collects_suite_and_ruff_runs(tmp_path: Path) -> None:
    test_body = (
        "from n import f\n\n\ndef test_f_returns_fixed_value():  # REQ-001\n    assert f() == 2\n"
    )
    _worktree(tmp_path, test_body, baseline_test=test_body)
    captured: list[CapturedRun] = []
    result = run_node_gate(_node(), tmp_path, capture=captured)
    assert result.passed is True
    assert [run.argv[0] for run in captured] == ["coverage", "ruff", "ruff"]
    assert all(run.exit_code == 0 for run in captured)
    assert "1 passed" in captured[0].stdout


def test_run_node_gate_ignores_stale_bytecode(tmp_path: Path) -> None:
    # This test edits test_n.py mid-test to force a stale-bytecode mtime
    # collision, which an honest `impl` node's own diff never does (it may
    # not touch tests at all); kept a `refactor` node so that deliberate
    # test edit does not trip node-scope on top of what this test means to
    # exercise. The test exists at baseline: a refactor may edit it but
    # (T2-2) may not create it.
    passing = (
        "from n import f\n\n\ndef test_f_returns_fixed_value():  # REQ-001\n    assert f() == 2\n"
    )
    _worktree(tmp_path, passing, baseline_test=passing)
    assert run_node_gate(_node(kind="refactor"), tmp_path).passed is True
    assert list(tmp_path.rglob("__pycache__")), "expected pytest to mint bytecode caches"
    failing = passing.replace("assert f() == 2", "assert f() == 3")
    assert len(failing) == len(passing)
    target = tmp_path / "test_n.py"
    mtime = target.stat().st_mtime
    target.write_text(failing)
    os.utime(target, (mtime, mtime))
    # Stage the edited test only: `git add -A` would also stage the first
    # run's .coverage.tier1 and bytecode as new files, which node-scope
    # now rightly rejects for a refactor node (T2-2) but is not the point.
    assert run_argv(["git", "add", "test_n.py"], tmp_path) == 0
    result = run_node_gate(_node(kind="refactor"), tmp_path)
    assert result.passed is False
    # assertion-preservation joins the list because the test now exists at
    # baseline and the rewrite drops its assertion; stale bytecode would
    # have hidden the tests/red-phase failures, and does not.
    assert [check.name for check in result.checks if not check.passed] == [
        "tests",
        "red-phase",
        "assertion-preservation",
    ]


def test_run_node_gate_fails_a_node_whose_addition_nothing_depends_on(tmp_path: Path) -> None:
    """T6-41 end to end: the private helper is executed and still carries nothing.

    `_touched()` runs at import, so every one of its lines is covered and
    the coverage gate is satisfied; it admits no mutant, so the mutation
    population never sees it. Removing it changes no test's verdict, and
    that is the whole of what the gate asks.
    """
    test_body = "from n import f\n\n\ndef test_f():  # REQ-001\n    assert f() == 2\n"
    honest = "def f():\n    return 2\n"
    with_dead = honest + "\n\ndef _touched():\n    pass\n\n\n_touched()\n"
    _worktree(tmp_path, test_body, baseline_test=test_body, fixed_code=with_dead)
    result = run_node_gate(_node(), tmp_path)
    assert [check.name for check in result.checks if not check.passed] == ["dead-code"]
    dead = {check.name: check for check in result.checks}["dead-code"]
    assert dead.detail == (
        "n.py adds _touched, which nothing else in the tree mentions; the suite still "
        "passes with them removed, so they implement no requirement"
    )
    assert dead.basis == "dead-definitions=1"
    # The worktree the other gates measured is untouched: the question was
    # asked in a copy.
    assert (tmp_path / "n.py").read_text() == with_dead


def test_run_node_gate_keeps_a_private_helper_its_tests_need(tmp_path: Path) -> None:
    """The known-good at the same boundary: same shape, load-bearing.

    `_double` is private and named by nothing outside the module either,
    so it is the same candidate; the suite goes red without it and the
    node seals.
    """
    test_body = "from n import f\n\n\ndef test_f():  # REQ-001\n    assert f() == 2\n"
    with_helper = "def _double(value):\n    return value * 2\n\n\ndef f():\n    return _double(1)\n"
    # The baseline shares no line with the rewrite, so every line of the
    # module is the node's own and `_double` is a real candidate: a helper
    # whose only caller the node also wrote is the case that must not fail.
    _worktree(
        tmp_path,
        test_body,
        baseline_test=test_body,
        baseline_code="x = 0\n",
        fixed_code=with_helper,
    )
    result = run_node_gate(_node(), tmp_path)
    dead = {check.name: check for check in result.checks}["dead-code"]
    assert dead.passed is True
    assert dead.detail == (
        "n.py adds _double; the suite fails without them, so they carry the work"
    )
    assert dead.basis == "dead-candidates=1"


def test_run_node_gate_target_files_binds_end_to_end(tmp_path: Path) -> None:
    """T3-2, #64: the same honest impl node passes when it names the file
    it changes and fails, naming the stray, when it names a different one.
    Paths are repo-relative, whatever the runner's absolute convention."""
    test_body = "from n import f\n\n\ndef test_f():  # REQ-001\n    assert f() == 2\n"
    _worktree(tmp_path, test_body, baseline_test=test_body)
    scoped = run_node_gate(_node(target_files=["n.py"]), tmp_path)
    assert scoped.passed is True
    by_name = {check.name: check for check in scoped.checks}
    assert by_name["target-scope"].detail == "1 touched file(s) within 1 target(s)"
    wrong = run_node_gate(_node(target_files=["other.py"]), tmp_path)
    assert wrong.passed is False
    assert [check.name for check in wrong.checks if not check.passed] == ["target-scope"]
    failed = {check.name: check for check in wrong.checks}["target-scope"]
    assert failed.detail == "touched file(s) outside target_files: n.py"


def test_run_node_gate_target_files_names_a_staged_new_file(tmp_path: Path) -> None:
    """T3-16(a): `git diff --name-only <ref>` already lists a staged new
    file (tracked-ness comes from the index), so target-scope must name a
    stray file added outside target_files with no separate union needed."""
    test_body = "from n import f\n\n\ndef test_f():  # REQ-001\n    assert f() == 2\n"
    _worktree(tmp_path, test_body, baseline_test=test_body)
    (tmp_path / "extra.py").write_text("")
    assert run_argv(["git", "add", "extra.py"], tmp_path) == 0
    result = run_node_gate(_node(target_files=["n.py"]), tmp_path)
    assert result.passed is False
    failed = {check.name: check for check in result.checks}["target-scope"]
    assert failed.detail == "touched file(s) outside target_files: extra.py"


def test_run_node_gate_unbound_requirement_fails(tmp_path: Path) -> None:
    test_body = "from n import f\n\n\ndef test_f_returns_fixed_value():\n    assert f() == 2\n"
    _worktree(tmp_path, test_body, baseline_test=test_body)
    result = run_node_gate(_node(), tmp_path)
    assert result.passed is False
    binding = next(check for check in result.checks if check.name == "requirement-binding")
    assert binding.passed is False
    assert "REQ-001" in binding.detail


def test_run_node_gate_planned_requirements_reach_the_binding_gate(tmp_path: Path) -> None:
    """End to end (T3-24): a suite citing an id another node of the plan
    declares fails the node gated alone and passes once the plan's ids are
    handed in; the node's own REQ-001 is still the one counted as bound."""
    test_body = "from n import f\n\n\ndef test_f():  # REQ-001\n    assert f() == 2  # REQ-002\n"
    _worktree(tmp_path, test_body, baseline_test=test_body)
    alone = run_node_gate(_node(), tmp_path)
    binding = next(check for check in alone.checks if check.name == "requirement-binding")
    assert binding.detail == "undeclared requirements cited: REQ-002"
    planned = run_node_gate(_node(), tmp_path, planned_requirements=("REQ-001", "REQ-002"))
    binding = next(check for check in planned.checks if check.name == "requirement-binding")
    assert binding.passed is True
    assert binding.detail == "1 requirement(s) bound"


def test_run_node_gate_uncovered_line_fails(tmp_path: Path) -> None:
    fixed = "def f():\n    return 2\n\n\ndef unused():\n    return 3\n"
    test_body = "from n import f\n\n\ndef test_f():  # REQ-001\n    assert f() == 2\n"
    _worktree(tmp_path, test_body, fixed_code=fixed, baseline_test=test_body)
    result = run_node_gate(_node(), tmp_path)
    assert result.passed is False
    coverage = next(check for check in result.checks if check.name == "coverage")
    assert coverage.passed is False
    assert "n.py:6" in coverage.detail


def test_run_node_gate_pass_pre_change_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`assert f() in (1, 2)` passes before and after, so it proves nothing.

    The node leaves every test untouched, which routes red-phase to the
    behaviour-preserving branch (only reachable for a `refactor` node) and
    leans the proof on mutation. The shared stub reports a healthy kill for
    every node; this test needs a survivor, because a test that accepts
    both the old and new return value is exactly what fails to kill one.
    """
    test_body = "from n import f\n\n\ndef test_f():  # REQ-001\n    assert f() in (1, 2)\n"
    _worktree(tmp_path, test_body, baseline_test=test_body)
    _surviving_mutmut(tmp_path / "stub", monkeypatch)
    result = run_node_gate(_node(kind="refactor"), tmp_path)
    assert result.passed is False
    red = next(check for check in result.checks if check.name == "red-phase")
    assert red.passed is False


def test_run_node_gate_lint_dirty_fails(tmp_path: Path) -> None:
    fixed = "import os\n\n\ndef f():\n    return 2\n"
    test_body = "from n import f\n\n\ndef test_f():  # REQ-001\n    assert f() == 2\n"
    _worktree(tmp_path, test_body, fixed_code=fixed, baseline_test=test_body)
    result = run_node_gate(_node(), tmp_path)
    assert result.passed is False
    ruff = next(check for check in result.checks if check.name == "ruff")
    assert ruff.passed is False
    # T6-3: the detail names the rule, file and line.
    assert ruff.detail.startswith("introduced 1 finding(s): n.py:1 F401 ")


def test_run_node_gate_inherited_lint_does_not_fail_and_a_shift_is_still_inherited(
    tmp_path: Path,
) -> None:
    """T6-3 known-good: the baseline ships `n.py` with an unused import; the
    node adds a clean function below it (the finding moves down) and
    passes with `inherited: 1`. Known-bad: a diff that adds its own
    unused import to the same file fails naming that finding only."""
    dirty = "import os\n\n\ndef f():\n    return 1\n"
    clean_add = "import os\n\n\ndef g():\n    return 0\n\n\ndef f():\n    return 2\n"
    test_body = "from n import f\n\n\ndef test_f():  # REQ-001\n    assert f() == 2\n"
    _worktree(
        tmp_path, test_body, baseline_code=dirty, fixed_code=clean_add, baseline_test=test_body
    )
    captured: list[CapturedRun] = []
    result = run_node_gate(_node(), tmp_path, capture=captured)
    ruff = next(check for check in result.checks if check.name == "ruff")
    assert ruff.passed is True, ruff.detail
    assert ruff.detail == "1 file(s) clean; inherited: 1"
    assert [run.argv[:2] for run in captured] == [
        ("coverage", "run"),
        ("ruff", "check"),
        ("ruff", "format"),
    ]
    also_dirty = "import os\nimport sys\n\n\ndef f():\n    return 2\n"
    (tmp_path / "n.py").write_text(also_dirty)
    assert run_argv(["git", "add", "n.py"], tmp_path) == 0
    result = run_node_gate(_node(), tmp_path)
    ruff = next(check for check in result.checks if check.name == "ruff")
    assert ruff.passed is False
    assert ruff.detail.startswith("introduced 1 finding(s): n.py:2 F401 ")
    assert ruff.detail.endswith("; inherited: 1")


def test_run_node_gate_suffix_style_test_binds(tmp_path: Path) -> None:
    test_body = "from n import f\n\n\ndef test_f():  # REQ-001\n    assert f() == 2\n"
    _worktree(tmp_path, test_body, test_name="n_test.py", baseline_test=test_body)
    result = run_node_gate(_node("pytest n_test.py"), tmp_path)
    assert result.passed is True


def test_run_node_gate_new_test_passing_pre_change_is_not_red(tmp_path: Path) -> None:
    """A brand-new test that also passes against baseline code is not red.

    Regression: the baseline leg ran the gate command against a tree that
    never contained the new test file, so "file not found" counted as red
    and every greenfield node cleared red-phase vacuously.

    test_n.py is new-at-baseline on purpose (that is what the regression
    needs), so this stays a `refactor` node -- an honest `impl` node's test
    already exists, unchanged, at its own baseline.
    """
    test_body = "from n import f\n\n\ndef test_f():  # REQ-001\n    assert f() in (1, 2)\n"
    _worktree(tmp_path, test_body)
    result = run_node_gate(_node(kind="refactor"), tmp_path)
    red = next(check for check in result.checks if check.name == "red-phase")
    assert red.passed is False, f"vacuous red: {red.detail}"


def _surviving_mutmut(stub_dir: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A mutmut stub whose changed-line mutant survives the node's tests."""
    stub_dir.mkdir(exist_ok=True)
    (stub_dir / "results.txt").write_text("  m1: survived\n")
    (stub_dir / "show_m1.txt").write_text(
        "--- n.py\n+++ n.py\n@@ -2 +2 @@\n-    return 2\n+    return 3\n"
    )
    script = stub_dir / "mutmut"
    script.write_text(
        "#!/bin/sh\n"
        f'STUB_DIR="{stub_dir}"\n'
        'case "$1" in\n'
        "  run) exit 0;;\n"
        '  results) cat "$STUB_DIR/results.txt";;\n'
        '  show) cat "$STUB_DIR/show_$2.txt";;\n'
        "esac\n"
    )
    script.chmod(script.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    monkeypatch.setenv("PATH", f"{stub_dir}{os.pathsep}{os.environ['PATH']}")


def _locatable_mutmut(
    stub_dir: Path, monkeypatch: pytest.MonkeyPatch, *, removed: str, line: int
) -> None:
    """Install a mutmut stub whose one mutant locates to a changed line.

    The autouse stub in conftest reports unparseable output on purpose, so
    every mutant is undecided; the behaviour-preserving red-phase branch
    needs a real kill to lean on.
    """
    stub_dir.mkdir(exist_ok=True)
    (stub_dir / "results.txt").write_text("  m1: killed\n")
    (stub_dir / "show_m1.txt").write_text(
        f"--- n.py\n+++ n.py\n@@ -{line} +{line} @@\n-{removed}\n+    pass\n"
    )
    script = stub_dir / "mutmut"
    script.write_text(
        "#!/bin/sh\n"
        f'STUB_DIR="{stub_dir}"\n'
        'case "$1" in\n'
        "  run) exit 0;;\n"
        '  results) cat "$STUB_DIR/results.txt";;\n'
        '  show) cat "$STUB_DIR/show_$2.txt";;\n'
        "esac\n"
    )
    script.chmod(script.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    monkeypatch.setenv("PATH", f"{stub_dir}{os.pathsep}{os.environ['PATH']}")


def test_run_node_gate_comment_only_test_edit_stays_behaviour_preserving(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Tagging a test with a REQ id must not fake a red-phase flip.

    Requirement-binding forces the worker to name REQ ids in test source,
    so a comment-only edit would otherwise let a behaviour-preserving node
    claim its tests changed and take the red-phase path. The shortcut this
    pins is only reachable for a `refactor` node (an `impl` node's
    unchanged-test case takes the real differential instead, per
    check_red_phase), so kept explicit rather than the new impl default.
    """
    baseline_test = "from n import f\n\n\ndef test_f():\n    assert f() == 1\n"
    tagged = "from n import f\n\n\ndef test_f():  # REQ-001\n    assert f() == 1\n"
    _worktree(
        tmp_path,
        tagged,
        baseline_code="def f():\n    return 1\n",
        # Behaviour-preserving rewrite: changed lines exist to mutate, but
        # no test can fail pre-change because the result is identical.
        fixed_code="def f():\n    value = 1\n    return value\n",
        baseline_test=baseline_test,
    )
    _locatable_mutmut(tmp_path / "stub", monkeypatch, removed="    value = 1", line=2)
    result = run_node_gate(_node(max_mutants=100, kind="refactor"), tmp_path)
    red = next(check for check in result.checks if check.name == "red-phase")
    assert red.passed is True
    assert red.detail.startswith("tests unchanged (behaviour preserved)")


def test_run_node_gate_changed_assertion_takes_the_red_phase_path(tmp_path: Path) -> None:
    """Changing what a test asserts binds the gate to a real pre-change run.

    Both the source and the test change here, which only a `refactor` node
    may do honestly (an `impl` node may not touch tests at all).
    """
    baseline_test = "from n import f\n\n\ndef test_f():  # REQ-001\n    assert f() == 1\n"
    changed = "from n import f\n\n\ndef test_f():  # REQ-001\n    assert f() == 2\n"
    _worktree(tmp_path, changed, baseline_test=baseline_test)
    result = run_node_gate(_node(kind="refactor"), tmp_path)
    red = next(check for check in result.checks if check.name == "red-phase")
    assert red.passed is True
    assert red.detail == "fail pre-change, pass post-change"


def test_run_node_gate_flaky_baseline_is_caught_end_to_end(tmp_path: Path) -> None:
    """A genuinely nondeterministic pre-change leg must fail the gate.

    Pins the *behaviour*, not the constant: a single observation cannot
    distinguish a flake from a genuine red, so RED_PHASE_SAMPLES must be
    greater than one for this to be detectable at all. The counter file
    survives drop_test_caches, so the test alternates across samples.

    test_n.py is new-at-baseline on purpose (the node must be judged on its
    own new test), so this stays a `refactor` node.
    """
    flaky = (
        "import pathlib\n\n"
        "_COUNTER = pathlib.Path(__file__).parent / '.flake_count'\n\n\n"
        "def test_f_flaky():  # REQ-001\n"
        "    n = int(_COUNTER.read_text()) if _COUNTER.exists() else 0\n"
        "    _COUNTER.write_text(str(n + 1))\n"
        "    assert n % 2 == 0\n"
    )
    _worktree(tmp_path, flaky)
    result = run_node_gate(_node(kind="refactor"), tmp_path)

    red = next(check for check in result.checks if check.name == "red-phase")
    assert red.passed is False
    assert "nondeterministic" in red.detail


def test_stub_module_keeps_the_api_and_empties_the_bodies() -> None:
    """Greenfield red-phase needs a baseline the tests can actually run.

    F2: a new module cannot be imported at baseline, so red-phase accepts
    a collection error naming a changed source. That is reachable on
    demand -- any new code in a new module with a new test clears it
    regardless of what the test asserts. Stubbing the module instead
    turns the import error into a real assertion failure, so a test that
    exercises the new code fails pre-change and a tautological one
    passes and is rejected.
    """
    source = (
        "import re\n\n"
        "_RE = re.compile(r'x')\n\n\n"
        "class Holder:\n"
        "    def take(self, value: int) -> int:\n"
        "        return value * 2\n\n\n"
        "def top(a: int) -> int:\n"
        "    return a + 1\n"
    )
    stub = _stub_module(source)
    tree = ast.parse(stub)

    names = {node.name for node in ast.walk(tree) if isinstance(node, ast.FunctionDef)}
    assert names == {"take", "top"}
    assert "class Holder" in stub
    assert "NotImplementedError" in stub
    assert "return value * 2" not in stub
    assert "return a + 1" not in stub


def test_stub_module_empties_async_bodies_too() -> None:
    stub = _stub_module("async def fetch(url: str) -> bytes:\n    return b'real'\n")
    assert "NotImplementedError" in stub
    assert b"real".decode() not in stub


def test_run_node_gate_greenfield_tautology_no_longer_clears_red_phase(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The greenfield path was clearable by any new module (F2, #53).

    The node creates `helper.py` and a test that never calls it. Before
    stubbing, the baseline run failed to *import* the module, red-phase
    read that collection error as a genuine red, and the node passed
    while its test asserted nothing about the new code. With a stub in
    the baseline the test imports fine, passes pre-change, and the gate
    rejects it.
    """
    for argv in (
        ["git", "init"],
        ["git", "config", "user.email", "test@example.com"],
        ["git", "config", "user.name", "test"],
    ):
        assert run_argv(argv, tmp_path) == 0
    (tmp_path / "keep.py").write_text("x = 1\n")
    assert run_argv(["git", "add", "keep.py"], tmp_path) == 0
    assert run_argv(["git", "commit", "-m", "baseline"], tmp_path) == 0

    (tmp_path / "helper.py").write_text(
        "def normalize(value: str) -> str:\n    return value.strip()\n"
    )
    (tmp_path / "test_n.py").write_text(
        "import helper\n\n\ndef test_helper_exists():  # REQ-001\n    assert helper is not None\n"
    )
    assert run_argv(["git", "add", "-A"], tmp_path) == 0

    result = run_node_gate(_node(), tmp_path)
    red = next(check for check in result.checks if check.name == "red-phase")
    assert red.passed is False
    assert "pass pre-change" in red.detail


# --- T3-7a: the first `test`-kind nodes that can pass Tier-1 -----------------

_SPEC_CODE: Final = "def f():\n    return 1\n"
SPEC_TEST: Final = (
    "from hypothesis import given\n"
    "from hypothesis import strategies as st\n\n"
    "from n import f\n\n\n"
    "def test_f_returns_two():  # REQ-001\n"
    "    assert f() == 2\n\n\n"
    "@given(st.integers())\n"
    "def test_f_is_an_int(_value):  # REQ-001\n"
    "    assert isinstance(f(), int)\n\n\n"
    "@given(st.integers())\n"
    "def test_f_is_never_three(_value):  # REQ-001\n"
    "    assert (f() == 3) is False\n"
)


def _spec_worktree(root: Path, test_body: str) -> None:
    """Source committed and untouched; the node's whole diff is one new test file."""
    _worktree(root, test_body, baseline_code=_SPEC_CODE, fixed_code=_SPEC_CODE)


def test_run_node_gate_test_node_passes_as_a_red_specification(tmp_path: Path) -> None:
    """T3-7a known-good, end to end: before it, no test node could pass (T3-7)."""
    _spec_worktree(tmp_path, SPEC_TEST)
    recorder = SpanRecorder(path=tmp_path / "proofs.jsonl", node_id="n1")
    result = run_node_gate(_node(kind="test"), tmp_path, recorder=recorder)
    assert result.passed is True, [check for check in result.checks if not check.passed]
    by_name = {check.name: check for check in result.checks}
    assert by_name["tests"].detail == "red specification: 1 failing test(s)"
    assert by_name["red-phase"].detail == "red by construction: the specification fails now"
    assert by_name["coverage"].basis == "test node"
    assert by_name["mutation"].basis == "test node"
    # No baseline samples and no mutmut: the suite ran exactly once.
    names = [span.name for span in read_spans(tmp_path / "proofs.jsonl") if span.kind == "tool"]
    assert names.count("coverage") == 1
    assert "mutmut" not in names
    assert "timeout" not in names


def test_run_node_gate_test_node_greenfield_import_is_red(tmp_path: Path) -> None:
    """A spec importing the module the impl node will create is red, not broken."""
    # `m` does not exist, so isort files it as third-party: no blank line.
    body = SPEC_TEST.replace(
        "from hypothesis import strategies as st\n\nfrom n import f\n",
        "from hypothesis import strategies as st\nfrom m import f\n",
    )
    _spec_worktree(tmp_path, body)
    result = run_node_gate(_node(kind="test"), tmp_path)
    assert result.passed is True, [check for check in result.checks if not check.passed]
    by_name = {check.name: check for check in result.checks}
    assert by_name["tests"].detail == "red specification: module 'm' does not exist yet"


def test_run_node_gate_test_node_whose_tests_pass_specifies_nothing(tmp_path: Path) -> None:
    """T3-7a known-bad: the tautological spec fails tests and red-phase, nothing else."""
    # The examples stay asserted on (T6-4): the only defect is that it passes.
    _spec_worktree(tmp_path, SPEC_TEST.replace("assert f() == 2", "assert f() in (1, 2)"))
    result = run_node_gate(_node(kind="test"), tmp_path)
    assert result.passed is False
    failed = {check.name: check.detail for check in result.checks if not check.passed}
    assert list(failed) == ["tests", "red-phase"]
    assert failed["tests"] == "'pytest test_n.py' exited 0: tests already pass, nothing specified"


def test_run_node_gate_impl_node_property_oracle_passes_with_basis(tmp_path: Path) -> None:
    """T3-3 known-good, end to end: `n1` implements the specification `t1`
    wrote (`SPEC_TEST`: an example and a property over `n`). The conftest
    stub kills every mutant on every call, so the oracle -- a second
    `mutmut run` -- passes and the record carries its basis.
    """
    _worktree(tmp_path, SPEC_TEST, baseline_test=SPEC_TEST)
    recorder = SpanRecorder(path=tmp_path / "proofs.jsonl", node_id="n1")
    result = run_node_gate(_node(), tmp_path, recorder=recorder)
    assert result.passed is True, [check for check in result.checks if not check.passed]
    by_name = {check.name: check for check in result.checks}
    assert by_name["property-coverage"].detail == "property killed 5 of 5 mutant(s)"
    assert by_name["property-coverage"].basis == "oracle: killed 5 of 5 mutant(s) by test_n.py"
    names = [span.name for span in read_spans(tmp_path / "proofs.jsonl") if span.kind == "tool"]
    assert names.count("mutmut") >= 2, names


def _oracle_aware_mutmut(stub_dir: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A mutmut stub that can answer the two calls differently (T3-3).

    The gate's run comes first and the oracle's second (`runner.py`
    calls `mutation_sample` in that order); every call gets a fresh
    scratch copy, so the marker lives beside the stub: `run` sets it on
    its second call and `results` then reports survivors; the first call
    reports kills. Since F21.12a both runs carry the node's declared
    scope, so the two `pyproject.toml`s no longer tell the calls apart
    (here the scope and the property target are the same file); the
    conftest stub never could, so a fixture built on it could never fail
    this gate.
    """
    stub_dir.mkdir(exist_ok=True)
    show = "--- n.py\n+++ n.py\n@@ -2 +2 @@\n-    return 2\n+    return 3\n"
    script = stub_dir / "mutmut"
    script.write_text(
        "#!/bin/sh\n"
        'case "$1" in\n'
        '  run) d=$(dirname "$0"); if [ -f "$d/.gate-run" ]; then touch "$d/.narrowed";'
        ' else touch "$d/.gate-run"; fi; exit 0;;\n'
        '  results) if [ -f "$(dirname "$0")/.narrowed" ]; then v=survived; else v=killed; fi;'
        ' for i in 1 2 3 4 5; do echo "  m$i: $v"; done;;\n'
        f"  show) printf '%s' '{show}';;\n"
        "esac\n"
    )
    script.chmod(script.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    monkeypatch.setenv("PATH", f"{stub_dir}{os.pathsep}{os.environ['PATH']}")


def test_run_node_gate_impl_node_property_that_cannot_discriminate_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """T3-3 known-bad, end to end: the same fixture, with a stub whose
    narrowed run reports every mutant survived. The main mutation gate
    still passes on the unrestricted run, so the failing set is exactly
    the property oracle -- the verdict F1 never received.
    """
    _worktree(tmp_path, SPEC_TEST, baseline_test=SPEC_TEST)
    _oracle_aware_mutmut(tmp_path / "stub", monkeypatch)
    result = run_node_gate(_node(), tmp_path)
    assert result.passed is False
    failed = {check.name: check.detail for check in result.checks if not check.passed}
    assert failed == {
        "property-coverage": (
            "property killed 0 of 5 mutant(s): no discriminating power (test_n.py)"
        )
    }


def _scope_checking_mutmut(stub_dir: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A mutmut stub shaped like round 3c's arms A and B (F21.12a).

    `run` reads the scratch `pyproject.toml`: unless `pytest_add_cli_args`
    names the node's declared scope (`test_n.py`) it behaves as real
    mutmut did against the red whole suite -- exits 1 with the stats
    failure -- and otherwise reports kills.
    """
    stub_dir.mkdir(exist_ok=True)
    show = "--- n.py\n+++ n.py\n@@ -2 +2 @@\n-    return 2\n+    return 3\n"
    script = stub_dir / "mutmut"
    script.write_text(
        "#!/bin/sh\n"
        'case "$1" in\n'
        "  run) grep -q '\"test_n.py\"' pyproject.toml && exit 0;"
        ' echo "failed to collect stats. runner returned 1" >&2; exit 1;;\n'
        '  results) for i in 1 2 3 4 5; do echo "  m$i: killed"; done;;\n'
        f"  show) printf '%s' '{show}';;\n"
        "esac\n"
    )
    script.chmod(script.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    monkeypatch.setenv("PATH", f"{stub_dir}{os.pathsep}{os.environ['PATH']}")


def test_run_node_gate_mutation_runs_the_declared_scope_not_the_red_suite(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """F21.12a, end to end. Known-good: an impl node whose declared scope
    is green while a sibling specification (`test_other.py`, written red
    by an earlier test node) still fails. The tests gate already honours
    the scope; the mutation gate must baseline against the same scope,
    or no impl node on a TDD plan can seal until the last one lands.
    Known-bad in the stub: an unscoped run is the red suite and fails.
    """
    test_body = "from n import f\n\n\ndef test_f():  # REQ-001\n    assert f() == 2\n"
    # The sibling specification is the earlier test node's sealed work, so
    # it sits at baseline, outside this node's diff and coverage scope.
    (tmp_path / "test_other.py").write_text("def test_later_module():\n    assert False\n")
    _worktree(tmp_path, test_body, baseline_test=test_body)
    _scope_checking_mutmut(tmp_path / "stub", monkeypatch)
    result = run_node_gate(_node(test_command="pytest test_n.py"), tmp_path)
    by_name = {check.name: check for check in result.checks}
    assert by_name["tests"].passed is True
    assert by_name["mutation"].passed is True, by_name["mutation"].detail
    assert [(c.name, c.detail) for c in result.checks if not c.passed] == []


def test_run_node_gate_failed_mutmut_run_is_named_end_to_end(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """T3-20: the gate detail carries the tool's last line, so recovery
    reads a broken tool rather than a missing test."""
    test_body = (
        "from n import f\n\n\ndef test_f_returns_fixed_value():  # REQ-001\n    assert f() == 2\n"
    )
    _worktree(tmp_path, test_body, baseline_test=test_body)
    stub_dir = tmp_path / "stub"
    stub_dir.mkdir()
    script = stub_dir / "mutmut"
    script.write_text(
        "#!/bin/sh\n"
        'case "$1" in\n'
        '  run) echo "Failed trampoline hit. Module name starts with src." >&2; exit 1;;\n'
        "  results) exit 0;;\n"
        "esac\n"
    )
    script.chmod(script.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    monkeypatch.setenv("PATH", f"{stub_dir}{os.pathsep}{os.environ['PATH']}")
    result = run_node_gate(_node(), tmp_path)
    by_name = {check.name: check for check in result.checks}
    assert by_name["mutation"].passed is False
    assert by_name["mutation"].detail == (
        "mutation tool failed: mutmut run exited 1: "
        "Failed trampoline hit. Module name starts with src."
    )
