"""The audit's changed-line coverage reaches `.js` files, through c8.

Contract: a changed code line of a `.js` file the coverage scope lists as
measured that no node test runs is a `js-coverage` failure naming `file:line`;
a changed line of a file the scope lists as not measured, or lists nowhere, and
a run without c8, read `not-proven`, never a pass. Blank and comment lines are
not code and never count. Known-good and known-bad instances are real audits
with real node and c8; the failure branches of the c8 run are driven through
`run_capture`.
"""

from __future__ import annotations

import json
import shutil
from pathlib import Path
from typing import Any

import pytest
from test_auditor import _init
from test_js_audit import BASE_JS, FILES, HEAD_JS, HEAD_TEST, NOTHING, finding

from saddle import jsevidence, runner
from saddle.auditor import JS_COVERAGE_GATE, Auditor, AuditorConfig, not_measurable_detail
from saddle.evidence import CapturedRun
from saddle.gates import SHELL_TIMEOUT, TOOL_UNAVAILABLE, check_js_coverage
from saddle.jsevidence import measure_coverage, parse_lcov, read_coverage_scope

REPO = Path(__file__).resolve().parents[1]
needs_c8 = pytest.mark.skipif(
    shutil.which("node") is None or not (REPO / "node_modules/c8/bin/c8.js").is_file(),
    reason="node or c8 is not installed",
)
SCOPE = "tests/fixtures/js_coverage_scope.json"
FULL_TEST = HEAD_TEST + "test('clamp high', () => { assert.strictEqual(clamp(9, 2, 5), 5); });\n"
FULL_TEST += "test('clamp mid', () => { assert.strictEqual(clamp(3, 2, 5), 3); });\n"


@pytest.fixture(autouse=True)
def python_mutation_is_not_under_test(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(runner, "mutation_sample", lambda *_a, **_k: NOTHING)


def tree(
    tmp_path: Path,
    *,
    test: str = HEAD_TEST,
    scope: dict[str, Any] | None = None,
    c8: bool = True,
    head: str = HEAD_JS,
) -> Path:
    root = tmp_path / "tree"
    (root / "static").mkdir(parents=True)
    (root / "tests/fixtures").mkdir(parents=True)
    extra = {} if scope is None else {SCOPE: json.dumps(scope)}
    _init(root, {**FILES, **extra})
    (root / "static/a.js").write_text(head)
    (root / "tests/a.test.js").write_text(test)
    if c8:
        (root / "node_modules").symlink_to(REPO / "node_modules")
    return root


MEASURED = {"measured": ["static/a.js"], "not_measured": {}}


@needs_c8
def test_a_changed_line_a_test_runs_passes(tmp_path: Path) -> None:
    found = Auditor(tree(tmp_path, test=FULL_TEST, scope=MEASURED), "HEAD").tier2()
    covered = finding(found, JS_COVERAGE_GATE)
    assert (covered.verdict, covered.detail) == ("pass", "every executable changed line runs (6)")


@needs_c8
def test_a_changed_line_no_test_runs_fails_naming_it(tmp_path: Path) -> None:
    root = tree(tmp_path, scope=MEASURED)
    found = Auditor(root, "HEAD").tier2()
    covered = finding(found, JS_COVERAGE_GATE)
    assert covered.verdict == "fail"
    assert covered.detail == "no test runs static/a.js:6, static/a.js:7"
    assert not found.passed
    assert not (root / "coverage").exists()
    assert not (root / ".c8").exists()


@needs_c8
def test_a_comment_or_blank_line_in_an_unrun_function_is_not_counted(tmp_path: Path) -> None:
    head = HEAD_JS.replace("  return x;", "\n  // the fallthrough\n  return x;")
    found = Auditor(tree(tmp_path, test=FULL_TEST, scope=MEASURED, head=head), "HEAD").tier2()
    assert finding(found, JS_COVERAGE_GATE).verdict == "pass"
    # the same file with the new branch unrun: only code lines are named
    found = Auditor(tree(tmp_path / "b", scope=MEASURED, head=head), "HEAD").tier2()
    assert finding(found, JS_COVERAGE_GATE).detail == "no test runs static/a.js:6, static/a.js:9"


@needs_c8
def test_a_file_the_scope_does_not_measure_is_not_proven_never_covered(tmp_path: Path) -> None:
    scope = {"measured": [], "not_measured": {"static/a.js": "page wiring"}}
    covered = finding(Auditor(tree(tmp_path, scope=scope), "HEAD").tier2(), JS_COVERAGE_GATE)
    assert covered.verdict == "not-proven"
    assert covered.detail == "not proven: not line-measured: static/a.js (page wiring)"


@needs_c8
def test_a_file_in_neither_list_is_not_proven_and_so_is_a_tree_with_no_scope(
    tmp_path: Path,
) -> None:
    listed: dict[str, Any] = {"measured": [], "not_measured": {}}
    one = finding(Auditor(tree(tmp_path / "a", scope=listed), "HEAD").tier2(), JS_COVERAGE_GATE)
    two = finding(Auditor(tree(tmp_path / "b"), "HEAD").tier2(), JS_COVERAGE_GATE)
    for covered in (one, two):
        assert covered.verdict == "not-proven"
        assert "not line-measured: static/a.js (in no coverage list)" in covered.detail


def test_without_c8_a_measured_file_is_not_proven_naming_the_tool(tmp_path: Path) -> None:
    covered = finding(
        Auditor(tree(tmp_path, scope=MEASURED, c8=False), "HEAD").tier2(), JS_COVERAGE_GATE
    )
    assert covered.verdict == "not-proven"
    assert "c8 was not found in node_modules" in covered.detail


@needs_c8
def test_a_gap_in_a_measured_file_outranks_an_unmeasured_file(tmp_path: Path) -> None:
    scope = {"measured": ["static/a.js"], "not_measured": {"static/b.js": "page wiring"}}
    root = tree(tmp_path, scope=scope)
    (root / "static/b.js").write_text("let x = 1;\n")
    found = Auditor(root, "HEAD").tier2()
    covered = finding(found, JS_COVERAGE_GATE)
    assert covered.verdict == "fail"
    assert covered.detail.endswith("; also not line-measured: static/b.js (page wiring)")


def test_a_change_with_no_js_code_line_gets_no_coverage_finding(tmp_path: Path) -> None:
    root = tree(tmp_path, scope=MEASURED, head=BASE_JS + "// a note\n")
    assert finding(Auditor(root, "HEAD").tier2(), JS_COVERAGE_GATE) is None


def test_a_malformed_scope_is_a_named_problem_not_a_silent_pass(tmp_path: Path) -> None:
    root = tree(tmp_path, c8=False)
    (root / SCOPE).parent.mkdir(parents=True, exist_ok=True)
    (root / SCOPE).write_text("{not json")
    covered = finding(Auditor(root, "HEAD").tier2(), JS_COVERAGE_GATE)
    assert covered.verdict == "not-proven"
    assert f"{SCOPE} could not be read" in covered.detail


def test_the_shortlist_reads_a_coverage_gap_as_not_proven(tmp_path: Path) -> None:
    if not (REPO / "node_modules/c8/bin/c8.js").is_file() or shutil.which("node") is None:
        pytest.skip("node or c8 is not installed")
    config = AuditorConfig(tier2="shortlist")
    covered = finding(
        Auditor(tree(tmp_path, scope=MEASURED), "HEAD", config).tier2(), JS_COVERAGE_GATE
    )
    assert covered.verdict == "not-proven"
    assert covered.detail.startswith("no test runs static/a.js:6")


def test_the_gate_names_unrun_lines_and_skips_unreported_ones() -> None:
    hits = {"a.js": {1: 3, 2: 0, 4: 0}}
    bad = check_js_coverage({"a.js": [1, 2, 3, 4]}, hits)
    assert (bad.passed, bad.detail) == (False, "no test runs a.js:2, a.js:4")
    good = check_js_coverage({"a.js": [1, 3]}, hits)
    assert (good.passed, good.detail) == (True, "every executable changed line runs (1)")


def test_lcov_lines_are_read_per_file() -> None:
    text = "TN:\nSF:src/a.js\nFN:1,f\nDA:1,2\nDA:2,0\nend_of_record\nSF:/r/b.js\nDA:5,1\n"
    assert parse_lcov(text, Path("/r")) == {"src/a.js": {1: 2, 2: 0}, "b.js": {5: 1}}
    for broken in ("DA:1,1\n", "SF:a.js\nDA:x,1\n"):
        with pytest.raises(ValueError, match=r"DA|invalid"):
            parse_lcov(broken, Path("/r"))


def test_the_scope_file_is_read_as_measured_and_not_measured(tmp_path: Path) -> None:
    assert read_coverage_scope(tmp_path).measured == frozenset()
    (tmp_path / "tests/fixtures").mkdir(parents=True)
    (tmp_path / SCOPE).write_text(json.dumps({"measured": ["a.js"], "not_measured": {"b.js": "r"}}))
    scope = read_coverage_scope(tmp_path)
    assert (scope.measured, dict(scope.not_measured), scope.problem) == (
        frozenset({"a.js"}),
        {"b.js": "r"},
        "",
    )
    (tmp_path / SCOPE).write_text(json.dumps({"measured": 3}))
    assert "could not be read" in read_coverage_scope(tmp_path).problem


def captured(code: int, stderr: str = "") -> CapturedRun:
    return CapturedRun(argv=("c8",), exit_code=code, stdout="", stderr=stderr)


@pytest.mark.parametrize(
    ("code", "stderr", "words"),
    [
        (TOOL_UNAVAILABLE, "no node", "c8 could not be launched: no node"),
        (SHELL_TIMEOUT, "", "c8 timed out"),
        (1, "boom\nlast", "exited 1: last"),
        (0, "", "c8 wrote no lcov report"),
    ],
)
def test_a_c8_run_that_did_not_deliver_a_report_names_why(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, code: int, stderr: str, words: str
) -> None:
    (tmp_path / "node_modules/c8/bin").mkdir(parents=True)
    (tmp_path / "node_modules/c8/bin/c8.js").write_text("")
    monkeypatch.setattr(jsevidence, "run_capture", lambda *_a, **_k: captured(code, stderr))
    got = measure_coverage(tmp_path, ["a.js"], ["t.test.js"])
    assert words in got.problem
    assert not got.lines


def test_an_unparseable_report_is_a_problem_not_zero_lines(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (tmp_path / "node_modules/c8/bin").mkdir(parents=True)
    (tmp_path / "node_modules/c8/bin/c8.js").write_text("")

    def fake(argv: list[str], *_a: Any, **_k: Any) -> CapturedRun:
        report = Path(next(a for a in argv if a.startswith("--reports-dir=")).split("=", 1)[1])
        report.mkdir(parents=True)
        (report / "lcov.info").write_text("DA:1,1\n")
        return captured(0)

    monkeypatch.setattr(jsevidence, "run_capture", fake)
    assert "did not parse" in measure_coverage(tmp_path, ["a.js"], ["t.test.js"]).problem


def test_the_not_measurable_wording_does_not_claim_every_js_file() -> None:
    words = not_measurable_detail(["a.css"])
    assert "measure JavaScript and Python only" in words
    assert "changed-line coverage only in the files" in words
    assert "js_coverage_scope.json" in words
    assert "js-coverage" in words
    assert "JavaScript" not in not_measurable_detail(["a.css"], {".py": "Python"})


def test_a_measured_js_file_with_no_node_test_is_not_proven(tmp_path: Path) -> None:
    root = tree(tmp_path, scope=MEASURED)
    (root / "tests/a.test.js").unlink()
    covered = finding(Auditor(root, "HEAD").tier2(), JS_COVERAGE_GATE)
    assert covered.verdict == "not-proven"
    assert "no node test file" in covered.detail


@needs_c8
def test_a_covered_measured_file_beside_an_unmeasured_one_is_still_not_proven(
    tmp_path: Path,
) -> None:
    scope = {"measured": ["static/a.js"], "not_measured": {"static/b.js": "page wiring"}}
    root = tree(tmp_path, test=FULL_TEST, scope=scope)
    (root / "static/b.js").write_text("let x = 1;\n")
    covered = finding(Auditor(root, "HEAD").tier2(), JS_COVERAGE_GATE)
    assert covered.verdict == "not-proven"
    assert covered.detail == (
        "not proven: not line-measured: static/b.js (page wiring) "
        "(every executable changed line runs (6) in the measured files)"
    )


def test_a_report_without_the_measured_file_is_not_proven_never_clean(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from saddle import auditor as auditor_mod
    from saddle.jsevidence import JsCoverage

    monkeypatch.setattr(auditor_mod, "measure_coverage", lambda *_a, **_k: JsCoverage())
    covered = finding(Auditor(tree(tmp_path, scope=MEASURED), "HEAD").tier2(), JS_COVERAGE_GATE)
    assert covered.verdict == "not-proven"
    assert "c8 reported no lines for static/a.js" in covered.detail
