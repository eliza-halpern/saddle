"""Tests for saddle.gates: one killer fixture per Tier-1 check."""

from __future__ import annotations

import inspect
from dataclasses import replace

from saddle.dag import DeterministicGate, Node
from saddle.evidence import MutationOutcome
from saddle.gates import (
    PYTEST_TESTS_FAILED,
    RED_PHASE_SAMPLES,
    SHELL_TIMEOUT,
    TOOL_UNAVAILABLE,
    GateCheck,
    Tier1Inputs,
    check_assertion_preservation,
    check_changed_line_coverage,
    check_mutation,
    check_node_scope,
    check_property_coverage,
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
            "kind": "refactor",
            "dependencies": [],
            "task_prompt": "Do n1.",
            "requirements": [{"id": "REQ-001", "statement": "REQ-001 holds."}],
            "execution_constraints": {
                "reasoning_budget": "low",
                "allowed_tools": ["read_file"],
                "max_context_tokens": 8000,
            },
            "deterministic_gate": {
                "test_command": "pytest tests/test_n1.py",
                "changed_line_coverage_min": 100.0,
                "red_phase_required": True,
                "mutation_sample": {
                    "scope": "changed-lines",
                    "max_mutants": 100,
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
        baseline_exits=(1,) * RED_PHASE_SAMPLES,
        baseline_output="",
        baseline_tests={},
        tests_changed=True,
        current_runner=lambda: 0,
        flipped_tests={"test_a": "def test_a():  # REQ-001\n    assert True\n"},
        mutation=MutationOutcome(killed=9, total=10, generated=10, survivors=("m1",)),
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


_STRONG = MutationOutcome(killed=10, total=10, generated=10, survivors=())
_PASSING_COVERAGE = GateCheck(name="coverage", passed=True, detail="100.0% >= 100.0%")


def _red(
    baseline: int,
    current: int,
    *,
    output: str = "",
    changed: tuple[str, ...] = (),
    tests_changed: bool = True,
    kind: str = "",
    coverage: GateCheck = _PASSING_COVERAGE,
    mutation: MutationOutcome = _STRONG,
) -> GateCheck:
    # Callers that predate the kind split describe themselves by whether
    # the node touched tests; a refactor is the no-test-change case.
    kind = kind or ("test" if tests_changed else "refactor")
    return check_red_phase(
        (baseline,) * RED_PHASE_SAMPLES,
        lambda: current,
        baseline_output=output,
        changed_files=changed,
        tests_changed=tests_changed,
        kind=kind,
        coverage=coverage,
        mutation=mutation,
    )


def test_red_phase_killer_fixture_pass_pre_change_fails() -> None:
    check = _red(0, 0)
    assert check.passed is False
    assert check.name == "red-phase"
    assert check.detail == "tests pass pre-change; prove nothing"


def test_red_phase_fail_post_change_fails() -> None:
    check = _red(1, 1)
    assert check.passed is False
    assert check.name == "red-phase"
    assert check.detail == "tests fail post-change"


def test_red_phase_flip_passes() -> None:
    flip = _red(1, 0)
    assert flip.passed is True
    assert flip.name == "red-phase"
    assert flip.detail == "fail pre-change, pass post-change"


def test_red_phase_cannot_be_waived() -> None:
    """The waiver is gone from the signature and unrepresentable on the wire.

    `const: true` is what makes guided decoding unable to emit a waiver,
    so the planner cannot opt a node out of the tautology killer.
    """
    assert "required" not in inspect.signature(check_red_phase).parameters
    schema = DeterministicGate.model_json_schema()
    assert schema["properties"]["red_phase_required"]["const"] is True


def test_red_phase_unrelated_baseline_error_fails() -> None:
    """Exit 2 blaming something the node never touched proves nothing."""
    check = _red(2, 0, output="ImportError: No module named 'yaml'", changed=("/w/n.py",))
    assert check.passed is False
    assert check.detail == "baseline collection error names no changed source; prove nothing"


def test_red_phase_greenfield_import_error_is_red() -> None:
    """Exit 2 naming a changed module is the greenfield red case."""
    check = _red(2, 0, output="ModuleNotFoundError: No module named 'n'", changed=("/w/n.py",))
    assert check.passed is True
    assert check.detail == "fail pre-change, pass post-change"


def test_red_phase_collection_header_names_changed_file() -> None:
    check = _red(2, 0, output="ERROR collecting n.py", changed=("/w/n.py",))
    assert check.passed is True


def test_red_phase_missing_file_exit_proves_nothing() -> None:
    """Exit 4 (file not found) was the old vacuous-red path."""
    check = _red(4, 0, output="file or directory not found", changed=("/w/n.py",))
    assert check.passed is False
    assert check.detail == "baseline exit 4: tests never ran; prove nothing"


def test_red_phase_no_tests_collected_proves_nothing() -> None:
    check = _red(5, 0, changed=("/w/n.py",))
    assert check.passed is False
    assert check.detail == "baseline exit 5: tests never ran; prove nothing"


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


def test_run_tier1_runs_exactly_the_ten_documented_checks_in_order() -> None:
    names = [check.name for check in run_tier1(_node(), _passing_inputs()).checks]
    assert names == [
        "syntax",
        "ruff",
        "tests",
        "coverage",
        "red-phase",
        "node-scope",
        "property-coverage",
        "assertion-preservation",
        "requirement-binding",
        "mutation",
    ]


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
        "node-scope",
        "property-coverage",
        "assertion-preservation",
        "requirement-binding",
        "mutation",
    ]
    by_name = {check.name: check for check in result.checks}
    assert by_name["coverage"].detail == "100.0% >= 100.0%"
    assert by_name["red-phase"].detail == "fail pre-change, pass post-change"
    assert by_name["mutation"].detail == "90.0% >= 85.0% over 10 mutant(s)"


def test_run_tier1_one_red_check_fails_but_all_run() -> None:
    bad = replace(_passing_inputs(), sources={"n1.py": "def broken(:\n"})
    result = run_tier1(_node(), bad)
    assert result.passed is False
    assert len(result.checks) == 10  # +assertion-preservation (#44)
    assert result.checks[0].passed is False
    assert all(check.passed for check in result.checks[1:])


def test_mutation_below_threshold_fails_with_survivors() -> None:
    outcome = MutationOutcome(
        killed=1, total=7, generated=7, survivors=("s6", "s5", "s4", "s3", "s2", "s1")
    )
    check = check_mutation(outcome, 85.0)
    assert check.name == "mutation"
    assert check.passed is False
    assert check.detail == "14.3% < 85.0%: survived s1, s2, s3, s4, s5"


def test_mutation_boundary_threshold_passes() -> None:
    outcome = MutationOutcome(killed=17, total=20, generated=20, survivors=("s1",))
    check = check_mutation(outcome, 85.0)
    assert check.name == "mutation"
    assert check.passed is True
    assert check.detail == "85.0% >= 85.0% over 20 mutant(s)"


def test_mutation_no_longer_passes_without_mutants() -> None:
    """Was `test_mutation_vacuous_passes_without_mutants`, asserting
    `passed is True` on an empty sample. The name said vacuous and the
    assertion pinned it; T7 shipped an infinite loop through this branch
    (F12). Inverted rather than deleted so the history stays legible."""
    check = check_mutation(MutationOutcome(killed=0, total=0, generated=0, survivors=()), 85.0)
    assert check.name == "mutation"
    assert check.passed is False
    assert check.detail == "no mutants on changed lines: mutation provided no evidence"


def test_mutation_undecided_fails_with_cause() -> None:
    bare = check_mutation(MutationOutcome(killed=0, total=0, generated=3, survivors=()), 85.0)
    assert bare.name == "mutation"
    assert bare.passed is False
    assert bare.detail == "no mutants decided"
    caused = check_mutation(
        MutationOutcome(
            killed=0, total=0, generated=0, survivors=("c6", "c5", "c4", "c3", "c2", "c1")
        ),
        85.0,
    )
    assert caused.name == "mutation"
    assert caused.passed is False
    assert caused.detail == "no mutants decided: c1, c2, c3, c4, c5"


def test_red_phase_behaviour_preserving_node_leans_on_coverage_and_mutation() -> None:
    """A node whose diff changes no test cannot have a pre-change failure."""
    check = _red(0, 0, tests_changed=False)
    assert check.passed is True
    assert check.detail == (
        "tests unchanged (behaviour preserved); coverage and mutation 100.0% carry the proof"
    )


def test_red_phase_behaviour_preserving_needs_coverage() -> None:
    failed = GateCheck(name="coverage", passed=False, detail="50.0% < 100.0%")
    check = _red(0, 0, tests_changed=False, coverage=failed)
    assert check.passed is False
    assert check.detail == "tests unchanged and coverage failed; nothing proves the change"


def test_red_phase_behaviour_preserving_needs_decided_mutants() -> None:
    none_decided = MutationOutcome(killed=0, total=0, generated=0, survivors=())
    check = _red(0, 0, tests_changed=False, mutation=none_decided)
    assert check.passed is False
    assert check.detail == "tests unchanged and no mutants decided; nothing proves the change"


def test_red_phase_behaviour_preserving_uses_fixed_floor_not_node_threshold() -> None:
    """The floor is fixed so a planner-chosen kill_threshold cannot waive it."""
    weak = MutationOutcome(killed=1, total=10, generated=10, survivors=("m1",))
    check = _red(0, 0, tests_changed=False, mutation=weak)
    assert check.passed is False
    assert check.detail == "tests unchanged and mutation 10.0% < 85.0%; nothing proves the change"


def test_tests_hanging_command_reports_hang_not_exit_code() -> None:
    """A hang is a distinct defect class, not an ordinary nonzero exit.

    Operationally a hang is worse than a failure: harnesses that parse
    pytest counts get partial or zero output from a suite that never
    terminates. The transcript must say so rather than burying it as
    'exited 124'.
    """
    check = check_test_command("pytest tests/test_x.py", lambda _cmd: SHELL_TIMEOUT)
    assert check.passed is False
    assert check.name == "tests"
    assert "hang" in check.detail.lower()
    assert "exited" not in check.detail


def test_red_phase_hanging_baseline_names_the_hang() -> None:
    """A baseline that never terminates is not 'tests never ran'.

    It fails the gate either way, but the transcript has to say which:
    exit 124 read as a generic nonzero exit sends recovery looking for a
    missing file instead of a loop.
    """
    check = check_red_phase(
        (SHELL_TIMEOUT,) * RED_PHASE_SAMPLES,
        lambda: 0,
        baseline_output="",
        changed_files=["orderedlist.py"],
        tests_changed=True,
        kind="test",
        coverage=GateCheck(name="coverage", passed=True),
        mutation=MutationOutcome(generated=1, total=1, killed=1, survivors=()),
    )
    assert check.passed is False
    assert "hang" in check.detail.lower()
    assert "never ran" not in check.detail


def test_red_phase_nondeterministic_baseline_proves_nothing() -> None:
    """A flaky baseline makes the differential meaningless.

    Red-phase is the only gate that reasons over *two* runs, so its
    evidence is exactly as good as the stability of the pre-change leg.
    A test that fails on one baseline run and passes on the next gives
    'fail pre-change, pass post-change' with no causal relation to the
    diff at all -- a vacuous red indistinguishable from a genuine one.
    2-16% of failures in large suites are flaky, and nothing in the
    harness would have noticed.
    """
    check = check_red_phase(
        (PYTEST_TESTS_FAILED, 0, PYTEST_TESTS_FAILED),
        lambda: 0,
        baseline_output="",
        changed_files=["n.py"],
        tests_changed=True,
        kind="test",
        coverage=GateCheck(name="coverage", passed=True),
        mutation=MutationOutcome(generated=1, total=1, killed=1, survivors=()),
    )
    assert check.passed is False
    assert "nondeterministic" in check.detail.lower()


def test_red_phase_unanimous_baseline_still_passes() -> None:
    """Sampling must not change the verdict on a stable baseline."""
    check = check_red_phase(
        (PYTEST_TESTS_FAILED,) * RED_PHASE_SAMPLES,
        lambda: 0,
        baseline_output="",
        changed_files=["n.py"],
        tests_changed=True,
        kind="test",
        coverage=GateCheck(name="coverage", passed=True),
        mutation=MutationOutcome(generated=1, total=1, killed=1, survivors=()),
    )
    assert check.passed is True


def test_tests_missing_tool_names_the_tool_not_an_exit_code() -> None:
    """`exited 127` sends recovery rewriting code over a broken venv."""
    check = check_test_command("pytest tests/test_x.py", lambda _cmd: TOOL_UNAVAILABLE)
    assert check.passed is False
    assert "unavailable" in check.detail.lower()
    assert "exited" not in check.detail


def test_ruff_missing_tool_names_the_tool() -> None:
    check = check_ruff(["n.py"], lambda _argv: TOOL_UNAVAILABLE)
    assert check.passed is False
    assert "unavailable" in check.detail.lower()


def test_requirement_binding_rejects_ids_the_node_never_declared() -> None:
    """A REQ ID in a test that no node declares is a hallucinated one.

    traceSDD's orphan rule: every ID cited in code is a verifiable claim,
    and one absent from the spec is automatically detectable. Without it
    the binding gate is satisfiable in both directions -- the worker can
    tag whatever it likes, and F5's circularity survives the statements.
    """
    check = check_requirement_binding(
        ["REQ-001"],
        {"tests/test_n.py": "def test_a():  # REQ-001\n    pass\n\n# REQ-742: invented\n"},
    )
    assert check.passed is False
    assert "REQ-742" in check.detail
    assert "undeclared" in check.detail.lower()


def test_requirement_binding_passes_when_every_cited_id_is_declared() -> None:
    check = check_requirement_binding(
        ["REQ-001", "REQ-002"],
        {"tests/test_n.py": "# REQ-001\n# REQ-002\n"},
    )
    assert check.passed is True


def test_mutation_zero_mutants_no_longer_passes() -> None:
    """Zero mutants is zero evidence, not a clean bill of health.

    F12: T7's node added a 218-line module, produced no changed-line
    mutants, and the gate reported `PASS (no mutants on changed lines)`
    having tested nothing. An infinite loop shipped past it. The
    fail-open was deliberate -- a change with no mutable surface cannot
    be under-tested -- but the premise is false: mutmut only mutates
    function bodies, so a module-scope `_RE = re.compile(...)` yields 0
    mutants where the same expression inline yields 7.
    """
    outcome = MutationOutcome(generated=0, total=0, killed=0, survivors=())
    check = check_mutation(outcome, 85.0)
    assert check.passed is False
    assert "no mutants" in check.detail.lower()


def test_mutation_small_sample_demands_every_mutant() -> None:
    """A percentage over a tiny sample is noise, so take no partial credit.

    F1: T1's four-line regex admitted 2 mutants, two shallow tests killed
    both, and the gate read 100%. The validator still accepted
    `.u@example.com` and `user@example..com`. The threshold cannot fix
    that, but it can refuse to call 3-of-4 adequate.
    """
    small = MutationOutcome(generated=4, total=4, killed=3, survivors=("m4",))
    check = check_mutation(small, 85.0)
    assert check.passed is False
    assert "small sample" in check.detail.lower()

    large = MutationOutcome(generated=20, total=20, killed=18, survivors=("m1", "m2"))
    assert check_mutation(large, 85.0).passed is True


def test_mutation_detail_always_reports_the_sample_size() -> None:
    """Weak evidence has to be visible in the transcript, not inferred."""
    outcome = MutationOutcome(generated=20, total=20, killed=20, survivors=())
    assert "20 mutant" in check_mutation(outcome, 85.0).detail


def test_impl_node_may_not_touch_test_files() -> None:
    """The circularity is one worker authoring both sides (F5, #44).

    Zylos: when one model writes the implementation and the tests, "a
    misreading of the contract doesn't get an independent second look; it
    gets encoded twice". T4's worker fixed the wrong module and rewrote
    the behaviour-pinning test to match; all seven gates passed. An impl
    node that cannot edit tests cannot do that.
    """
    check = check_node_scope("impl", ["orders.py", "tests/test_orders.py"])
    assert check.passed is False
    assert "tests/test_orders.py" in check.detail


def test_test_node_may_not_touch_source_files() -> None:
    """The other direction matters too: a test node that ships the
    implementation alongside its tests has authored both again."""
    check = check_node_scope("test", ["tests/test_orders.py", "orders.py"])
    assert check.passed is False
    assert "orders.py" in check.detail


def test_node_scope_accepts_a_node_that_stays_on_its_side() -> None:
    assert check_node_scope("impl", ["orders.py", "discounts.py"]).passed is True
    assert check_node_scope("test", ["tests/test_orders.py", "test_x.py"]).passed is True


def test_refactor_node_may_touch_both() -> None:
    """A behaviour-preserving refactor moves code and its tests together;
    splitting it across two nodes would leave the first one red."""
    check = check_node_scope("refactor", ["orders.py", "tests/test_orders.py"], added_files=[])
    assert check.passed is True


def test_refactor_node_may_not_create_a_file() -> None:
    """#65: a node picks `refactor` to dodge the impl/test split entirely.

    Editing both sides is the exemption's whole point, but creating a file
    is not editing -- it is the split done under the exempt name.
    """
    check = check_node_scope(
        "refactor", ["orders.py", "new_module.py"], added_files=["new_module.py"]
    )
    assert check.passed is False
    assert "new_module.py" in check.detail


def test_impl_node_takes_the_real_differential_not_the_refactor_branch() -> None:
    """An impl node changes no tests, but it is not behaviour-preserving.

    Its tests were written by the test node it depends on and already
    fail at its baseline. Routing it to _check_behaviour_preserved would
    grade a behaviour change on a refactor's evidence.
    """
    check = check_red_phase(
        (PYTEST_TESTS_FAILED,) * RED_PHASE_SAMPLES,
        lambda: 0,
        baseline_output="",
        changed_files=["orders.py"],
        tests_changed=False,
        kind="impl",
        coverage=GateCheck(name="coverage", passed=True),
        mutation=MutationOutcome(generated=10, total=10, killed=10, survivors=()),
    )
    assert check.passed is True
    assert "behaviour preserved" not in check.detail


def test_refactor_node_still_takes_the_behaviour_preserving_branch() -> None:
    check = check_red_phase(
        (0,) * RED_PHASE_SAMPLES,
        lambda: 0,
        baseline_output="",
        changed_files=["orders.py"],
        tests_changed=False,
        kind="refactor",
        coverage=GateCheck(name="coverage", passed=True),
        mutation=MutationOutcome(generated=10, total=10, killed=10, survivors=()),
    )
    assert "behaviour preserved" in check.detail


def test_test_node_must_contribute_a_property() -> None:
    """Two happy-path examples over an unbounded domain is the predicted
    output, not an anomaly (F1). T1 shipped a regex accepting
    `.u@example.com` and `user@example..com` with 7/7 gates green,
    because its two tests probed neither.
    """
    examples_only = {
        "tests/test_v.py": (
            "def test_valid():  # REQ-001\n    assert valid('a@b.co')\n\n"
            "def test_invalid():  # REQ-001\n    assert not valid('nope')\n"
        )
    }
    check = check_property_coverage("test", examples_only)
    assert check.passed is False
    assert "property" in check.detail.lower()


def test_hypothesis_given_counts_as_a_property() -> None:
    sources = {
        "tests/test_v.py": (
            "from hypothesis import given\n"
            "from hypothesis import strategies as st\n\n\n"
            "@given(st.from_regex(r'^[a-z]+@[a-z]+\\.[a-z]{2,}$'))\n"
            "def test_round_trip(address):  # REQ-001\n"
            "    assert valid(address)\n"
        )
    }
    assert check_property_coverage("test", sources).passed is True


def test_property_coverage_does_not_bind_impl_or_refactor_nodes() -> None:
    """An impl node writes no tests at all, and a refactor preserves the
    ones it moves; requiring a new property of either is unsatisfiable."""
    assert check_property_coverage("impl", {}).passed is True
    assert (
        check_property_coverage("refactor", {"tests/test_v.py": "def test_a():\n    pass\n"}).passed
        is True
    )


def test_property_detection_handles_unparseable_and_bare_decorators() -> None:
    """The syntax gate runs first, but check order is not a guarantee the
    property check may lean on; and a non-`given` decorator must not be
    mistaken for a property."""
    assert check_property_coverage("test", {"t.py": "def broken( :\n"}).passed is False

    bare = {
        "t.py": (
            "import pytest\n"
            "from hypothesis import given\n\n\n"
            "@pytest.mark.parametrize('x', [1])\n"
            "def test_example(x):  # REQ-001\n"
            "    assert x\n\n\n"
            "@given\n"
            "def test_property(value):  # REQ-001\n"
            "    assert value is not None\n"
        )
    }
    assert check_property_coverage("test", bare).passed is True


def test_parametrize_is_examples_not_a_property() -> None:
    """`@pytest.mark.parametrize` is a table of cases the author chose.

    That is precisely what F1 shows is insufficient -- the cases probed
    are the cases already in mind. Only generated inputs count.
    """
    table_only = {
        "t.py": (
            "import pytest\n\n\n"
            "@pytest.mark.parametrize('address', ['a@b.co', 'c@d.co'])\n"
            "def test_valid(address):  # REQ-001\n"
            "    assert valid(address)\n"
        )
    }
    check = check_property_coverage("test", table_only)
    assert check.passed is False
    assert "examples only" in check.detail


def test_refactor_may_not_rewrite_an_existing_assertion() -> None:
    """T4: the worker fixed the wrong module and rewrote the
    behaviour-pinning test to match -- 3.03 became 3.02 -- and all seven
    gates passed. #57 stops an impl node touching tests at all, but a
    refactor may move code and tests together, and "behaviour-preserving"
    means the assertions survive the move.
    """
    baseline = {"tests/t.py": "def test_total():  # REQ-001\n    assert total() == 3.03\n"}
    current = {"tests/t.py": "def test_total():  # REQ-001\n    assert total() == 3.02\n"}
    check = check_assertion_preservation("refactor", baseline, current)
    assert check.passed is False
    assert "test_total" in check.detail


def test_adding_assertions_to_an_existing_test_is_allowed() -> None:
    """Append-only: strengthening a test is the desired direction."""
    baseline = {"tests/t.py": "def test_total():  # REQ-001\n    assert total() == 3.03\n"}
    current = {
        "tests/t.py": (
            "def test_total():  # REQ-001\n    assert total() == 3.03\n    assert total() > 0\n"
        )
    }
    assert check_assertion_preservation("refactor", baseline, current).passed is True


def test_test_nodes_may_rewrite_assertions() -> None:
    """Repairing stale assertions is a test node's job (T2), and #57
    stops it shipping the implementation alongside."""
    baseline = {"tests/t.py": "def test_total():  # REQ-001\n    assert total() == 3.03\n"}
    current = {"tests/t.py": "def test_total():  # REQ-001\n    assert total() == 3.02\n"}
    assert check_assertion_preservation("test", baseline, current).passed is True


def test_moving_a_test_to_another_module_preserves_its_assertions() -> None:
    """A refactor relocating tests is fine while the asserts travel."""
    baseline = {"tests/old.py": "def test_total():  # REQ-001\n    assert total() == 3.03\n"}
    current = {"tests/new.py": "def test_total():  # REQ-001\n    assert total() == 3.03\n"}
    assert check_assertion_preservation("refactor", baseline, current).passed is True


def test_assertion_preservation_ignores_helpers_and_unparseable_sources() -> None:
    """Only `test*` functions carry the behaviour contract, and a source
    the syntax gate will reject must not crash this one."""
    baseline = {
        "tests/t.py": (
            "def _helper():\n    assert 1 == 1\n\n\n"
            "def test_real():  # REQ-001\n    assert total() == 3.03\n"
        ),
        "tests/broken.py": "def test_x( :\n",
    }
    # The helper's assertion vanishes; only the test function is contracted.
    current = {"tests/t.py": "def test_real():  # REQ-001\n    assert total() == 3.03\n"}
    assert check_assertion_preservation("refactor", baseline, current).passed is True


def test_same_test_name_in_two_modules_keeps_both_contracts() -> None:
    """Keying by name merges rather than overwrites.

    Two modules can each define `test_total` over different subjects. If
    the later one replaced the earlier, dropping the first module's
    assertion would go unnoticed -- the looser reading of a name
    collision, in a gate whose whole job is to be the stricter one.
    """
    baseline = {
        "tests/orders.py": "def test_total():  # REQ-001\n    assert order_total() == 3.03\n",
        "tests/invoices.py": "def test_total():  # REQ-002\n    assert invoice_total() == 3.03\n",
    }
    current = {
        "tests/orders.py": "def test_total():  # REQ-001\n    assert order_total() == 3.02\n",
        "tests/invoices.py": "def test_total():  # REQ-002\n    assert invoice_total() == 3.03\n",
    }
    check = check_assertion_preservation("refactor", baseline, current)
    assert check.passed is False
    assert "test_total" in check.detail
