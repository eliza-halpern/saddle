"""The verdicts on the node tests' own results (`gates.check_js_tests`,
`gates.check_js_red_phase`).

Contract: the node tests pass only if every test passed and the runner exited
0, with a run that found no test a failure; the new tests are red-phase proof
only if all pass on the change and at least one fails on the baseline.
Known-bad instances: a failed test, a runner that exited nonzero over passing
tests, no tests at all, a baseline on which everything passed or nothing ran.
"""

from __future__ import annotations

from saddle.gates import check_js_red_phase, check_js_tests

OK = ("tests/a.test.js", "adds", "pass")
BAD = ("tests/a.test.js", "clamps", "fail")
SKIP = ("tests/a.test.js", "later", "skipped")


def test_all_passing_tests_pass_and_skips_are_counted() -> None:
    check = check_js_tests([OK, OK, SKIP], 0)
    assert check.passed
    assert check.detail == "node --test: 2 passed, 0 failed, 1 skipped"


def test_a_failed_test_fails_the_check_and_is_named() -> None:
    check = check_js_tests([OK, BAD], 1)
    assert not check.passed
    assert check.detail == "node --test: 1 passed, 1 failed, 0 skipped; tests/a.test.js: clamps"


def test_only_the_first_failures_are_named_and_the_rest_counted() -> None:
    many = [("t.js", f"n{i}", "fail") for i in range(7)]
    detail = check_js_tests(many, 1).detail
    assert "t.js: n4" in detail
    assert "t.js: n5" not in detail
    assert detail.endswith("and 2 more")


def test_a_nonzero_exit_over_passing_tests_is_not_a_pass() -> None:
    check = check_js_tests([OK], 1)
    assert not check.passed
    assert check.detail.endswith("yet exit 1")


def test_a_run_that_found_no_test_is_not_a_pass() -> None:
    check = check_js_tests([], 0)
    assert not check.passed
    assert check.detail == "node --test ran no test (exit 0)"


def test_tests_red_on_the_baseline_and_green_on_the_change_are_proof() -> None:
    base = [("a.test.js", "clamps", "fail"), ("a.test.js", "adds", "pass")]
    head = [("a.test.js", "clamps", "pass"), ("a.test.js", "adds", "pass")]
    check = check_js_red_phase(head, base, 1)
    assert check.passed
    assert check.detail == (
        "1 of 2 node tests fail pre-change, pass post-change; "
        "green on the baseline: a.test.js: adds"
    )


def test_no_green_list_when_every_test_was_red() -> None:
    base = [("a.test.js", "clamps", "fail")]
    check = check_js_red_phase([("a.test.js", "clamps", "pass")], base, 1)
    assert check.passed
    assert check.detail == "1 of 1 node tests fail pre-change, pass post-change"


def test_tests_green_on_the_baseline_prove_nothing() -> None:
    green = [("a.test.js", "adds", "pass")]
    check = check_js_red_phase(green, green, 0)
    assert not check.passed
    assert check.detail == "node tests pass pre-change (exit 0); prove nothing"


def test_a_baseline_run_that_found_no_test_proves_nothing() -> None:
    check = check_js_red_phase([("a.test.js", "adds", "pass")], [], 0)
    assert not check.passed
    assert check.detail == "node tests ran no test pre-change (exit 0); prove nothing"


def test_a_test_failing_on_the_change_is_not_proof_whatever_the_baseline_did() -> None:
    base = [("a.test.js", "clamps", "fail")]
    check = check_js_red_phase([("a.test.js", "clamps", "fail")], base, 1)
    assert not check.passed
    assert check.detail == "tests fail post-change"
