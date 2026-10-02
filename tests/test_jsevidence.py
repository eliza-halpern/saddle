"""JavaScript evidence: node's per-test results, the red phase and StrykerJS mutants.

Contract: the harness runs `node --test` itself and returns one result per
test (`run_node_tests`); runs the new or changed `.js` tests again on the
baseline's sources (`red_phase`); and makes StrykerJS mutants of the changed
`.js` lines whose kills and survivors count as the audit counts mutmut's
(`mutation_sample`, `merge_outcomes`). Why: a browser file's tests were one
pytest wrapper pass or fail, its new tests were never run on the baseline and
no mutant was ever made of a changed `.js` line.

Known-good and known-bad instances are real runs of node and StrykerJS over a
small git project; the tool-failure and report-shape branches use the shapes
the tools print.
"""

from __future__ import annotations

import shutil
import subprocess
from collections.abc import Iterator
from pathlib import Path

import pytest

from saddle import jsevidence as js
from saddle import sandbox
from saddle.evidence import CapturedRun, MutationOutcome, git_diff
from saddle.gates import SHELL_TIMEOUT, TOOL_UNAVAILABLE

REPO = Path(__file__).resolve().parents[1]
needs_node = pytest.mark.skipif(shutil.which("node") is None, reason="node is not installed")
needs_stryker = pytest.mark.skipif(
    not (REPO / js.STRYKER_PACKAGE).is_file() or shutil.which("node") is None,
    reason="StrykerJS is not installed",
)

BASE_SRC = "function add(a, b) {\n  return a + b;\n}\nmodule.exports = { add };\n"
HEAD_SRC = (
    "function add(a, b) {\n  return a + b;\n}\n"
    "function clamp(x, lo, hi) {\n  if (x < lo) return lo;\n  if (x > hi) return hi;\n"
    "  return x;\n}\nmodule.exports = { add, clamp };\n"
)
TEST_HEAD = (
    "const test = require('node:test');\nconst assert = require('node:assert');\n"
    "const { clamp, add } = require('../static/a.js');\n"
    "test('clamp low', () => { assert.strictEqual(clamp(1, 2, 5), 2); });\n"
    "test('clamp mid', () => { assert.strictEqual(clamp(3, 2, 5), 3); });\n"
    "test('add', () => { assert.strictEqual(add(1, 2), 3); });\n"
)


@pytest.fixture(autouse=True)
def node_is_shown() -> Iterator[None]:
    """The sandbox hides HOME, where a developer's node may live."""
    with sandbox.also_exposing(["node"]):
        yield


def git(root: Path, *argv: str) -> None:
    subprocess.run(["git", "-C", str(root), *argv], check=True, capture_output=True)


@pytest.fixture
def project(tmp_path: Path) -> Path:
    """A project at a baseline commit with `add`; the working tree adds `clamp`
    and a test file for both, staged."""
    root = tmp_path / "proj"
    (root / "static").mkdir(parents=True)
    (root / "tests").mkdir()
    git(root, "init", "-q")
    git(root, "config", "user.email", "t@example.com")
    git(root, "config", "user.name", "t")
    (root / "static" / "a.js").write_text(BASE_SRC)
    git(root, "add", "-A")
    git(root, "commit", "-qm", "baseline")
    (root / "static" / "a.js").write_text(HEAD_SRC)
    (root / "tests" / "a.test.js").write_text(TEST_HEAD)
    git(root, "add", "-A")
    return root


# -- names ----------------------------------------------------------------------------


def test_test_files_are_known_by_node_s_own_names_and_nothing_else() -> None:
    for good in ("tests/a.test.js", "x/b-test.js", "c_test.js", "test-d.js"):
        assert js.is_js_test_file(good), good
    for bad in ("static/a.js", "tests/fixtures/dom_shim.js", "static/test.js", "a.test.mjs"):
        assert not js.is_js_test_file(bad), bad


def test_test_side_code_is_every_js_under_a_tests_directory_and_test_files() -> None:
    for good in ("tests/fixtures/dom_shim.js", "a/test/helper.js", "static/a.test.js"):
        assert js.is_js_test_side(good), good
    for bad in ("static/a.js", "src/saddle/web/static/markdown.js", "contests/a.js"):
        assert not js.is_js_test_side(bad), bad


def test_the_census_reads_hidden_directories_and_skips_node_modules(tmp_path: Path) -> None:
    for rel in (".hidden/a.js", "node_modules/m/b.js", "tests/c.test.js", "d.txt", "e.js"):
        (tmp_path / rel).parent.mkdir(parents=True, exist_ok=True)
        (tmp_path / rel).write_text("")
    assert js.js_files(tmp_path) == [".hidden/a.js", "e.js", "tests/c.test.js"]
    assert js.js_test_files(tmp_path) == ["tests/c.test.js"]


# -- B5: one result per test -----------------------------------------------------------


@needs_node
def test_every_test_has_its_own_result_with_its_describe_names(tmp_path: Path) -> None:
    (tmp_path / "d.test.js").write_text(
        "const { describe, it } = require('node:test');\n"
        "describe('outer', () => { describe('inner', () => {\n"
        "  it('fails', () => { throw new Error('x'); });\n"
        "  it('skips', { skip: true }, () => {});\n"
        "  it('ok', () => {});\n"
        "}); });\n"
    )
    ran = js.run_node_tests(tmp_path, ["d.test.js"])
    assert ran.problem == ""
    assert ran.exit_code == 1
    assert [r.row() for r in ran.results] == [
        ("d.test.js", "outer > inner > fails", "fail"),
        ("d.test.js", "outer > inner > skips", "skipped"),
        ("d.test.js", "outer > inner > ok", "pass"),
    ]


@needs_node
def test_a_file_that_cannot_load_is_one_failed_result_named_by_the_file(tmp_path: Path) -> None:
    (tmp_path / "l.test.js").write_text("throw new Error('load');\n")
    ran = js.run_node_tests(tmp_path, ["l.test.js"])
    assert [r.row() for r in ran.results] == [("l.test.js", "l.test.js", "fail")]


def test_no_files_is_no_run_and_no_problem(tmp_path: Path) -> None:
    assert js.run_node_tests(tmp_path, []) == js.JsTestRun(exit_code=0)


def _run(code: int, out: str = "", err: str = "") -> CapturedRun:
    return CapturedRun(argv=("node",), exit_code=code, stdout=out, stderr=err)


@pytest.mark.parametrize(
    ("ran", "words"),
    [
        (_run(TOOL_UNAVAILABLE, err="bwrap: execvp node: No such file"), "could not be launched"),
        (_run(SHELL_TIMEOUT), "timed out"),
        (_run(1, out="not xml", err="boom\nlast line"), "did not parse"),
    ],
)
def test_a_run_that_gave_no_report_names_why_and_has_no_results(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, ran: CapturedRun, words: str
) -> None:
    monkeypatch.setattr(js, "run_capture", lambda *_a, **_k: ran)
    out = js.run_node_tests(tmp_path, ["a.test.js"])
    assert words in out.problem
    assert out.results == ()
    assert out.exit_code == ran.exit_code


def test_a_report_with_no_output_at_all_names_that(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(js, "run_capture", lambda *_a, **_k: _run(1))
    assert "no output" in js.run_node_tests(tmp_path, ["a.test.js"]).problem


def test_elements_that_are_not_tests_or_outcomes_are_ignored(tmp_path: Path) -> None:
    report = (
        f'<testsuites><properties/><testcase name="t" file="{tmp_path}/a.test.js">'
        "<system-out>noise</system-out></testcase></testsuites>"
    )
    assert [r.row() for r in js.parse_junit(report, tmp_path)] == [("a.test.js", "t", "pass")]


def test_a_test_with_no_file_is_a_problem_not_a_pass(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="names no file"):
        js.parse_junit('<testsuites><testcase name="t"/></testsuites>', tmp_path)


# -- B6: the new tests on the baseline -------------------------------------------------


@needs_node
def test_new_tests_run_on_the_baseline_and_the_ones_that_need_the_change_fail_there(
    project: Path,
) -> None:
    head = js.run_node_tests(project, js.js_test_files(project))
    assert [r.status for r in head.results] == ["pass", "pass", "pass"]
    red = js.red_phase(project, "HEAD", head.results)
    assert red is not None
    assert red.files == ("tests/a.test.js",)
    assert [(r.name, r.status) for r in red.base.results] == [
        ("clamp low", "fail"),
        ("clamp mid", "fail"),
        ("add", "pass"),
    ]
    assert [r.status for r in red.head] == ["pass", "pass", "pass"]


@needs_node
def test_a_test_that_never_needed_the_change_is_green_on_the_baseline(project: Path) -> None:
    (project / "tests" / "a.test.js").write_text(
        TEST_HEAD.split("test('clamp low'")[0]
        + "test('add', () => { assert.strictEqual(add(1, 2), 3); });\n"
    )
    head = js.run_node_tests(project, js.js_test_files(project))
    red = js.red_phase(project, "HEAD", head.results)
    assert red is not None
    assert [r.status for r in red.base.results] == ["pass"]


@needs_node
def test_a_changed_fixture_is_written_over_the_baseline_so_only_the_sources_are_old(
    project: Path,
) -> None:
    (project / "tests" / "fixtures").mkdir()
    (project / "tests" / "fixtures" / "h.js").write_text("module.exports = { n: 4 };\n")
    (project / "tests" / "b.test.js").write_text(
        "const test = require('node:test');\nconst assert = require('node:assert');\n"
        "const { n } = require('./fixtures/h.js');\n"
        "test('uses the fixture', () => { assert.strictEqual(n, 4); });\n"
    )
    git(project, "add", "-A")
    head = js.run_node_tests(project, js.js_test_files(project))
    red = js.red_phase(project, "HEAD", head.results)
    assert red is not None
    mine = [r for r in red.base.results if r.file == "tests/b.test.js"]
    assert [r.status for r in mine] == ["pass"]  # the fixture was there; nothing else was old


@needs_node
def test_no_changed_js_test_is_no_red_phase(project: Path) -> None:
    git(project, "commit", "-qm", "head")
    assert js.red_phase(project, "HEAD", ()) is None


# -- changed lines ---------------------------------------------------------------------


def test_changed_js_lines_are_code_lines_of_non_test_js_files(project: Path) -> None:
    (project / "static" / "a.js").write_text(HEAD_SRC + "// a comment\n\n/* block */\n * more\n")
    (project / "static" / "notes.txt").write_text("x\n")
    (project / "static" / "gone.js").write_text("x\n")
    git(project, "add", "-N", "static/notes.txt", "static/gone.js")
    diff = git_diff(project, "HEAD")
    (project / "static" / "gone.js").unlink()
    found = js.changed_js_lines(project, diff)
    mine = str(project / "static" / "a.js")
    assert sorted(found) == [(mine, n) for n in range(4, 10)]


# -- B4: mutants -----------------------------------------------------------------------


def test_stryker_is_found_in_the_tree_or_in_the_checkout_the_tree_was_staged_from(
    tmp_path: Path,
) -> None:
    staged, checkout = tmp_path / "staged", tmp_path / "checkout"
    (checkout / js.STRYKER_PACKAGE).parent.mkdir(parents=True)
    (checkout / js.STRYKER_PACKAGE).write_text("")
    staged.mkdir()
    assert js.stryker_entry(staged) is None
    assert js.stryker_entry(staged, checkout) == checkout / js.STRYKER_PACKAGE
    (staged / js.STRYKER_PACKAGE).parent.mkdir(parents=True)
    (staged / js.STRYKER_PACKAGE).write_text("")
    assert js.stryker_entry(staged, checkout) == staged / js.STRYKER_PACKAGE


def test_nothing_changed_is_an_empty_outcome(tmp_path: Path) -> None:
    out = js.mutation_sample(tmp_path, [])
    assert (out.total, out.generated, out.survivors) == (0, 0, ())


def test_a_missing_stryker_is_a_named_survivor_never_an_empty_population(project: Path) -> None:
    out = js.mutation_sample(project, [(str(project / "static" / "a.js"), 5)])
    assert (out.total, out.survivors) == (0, ("stryker not found in node_modules",))


@needs_stryker
def test_stryker_kills_what_the_tests_pin_and_names_what_they_do_not(project: Path) -> None:
    diff = git_diff(project, "HEAD")
    changed = js.changed_js_lines(project, diff)
    out = js.mutation_sample(project, changed, tools=REPO)
    assert out.survivors != ()
    assert out.killed > 0
    assert out.total == out.killed + len(out.survivors)
    # Nothing runs `clamp` above `hi`, so `if (x > hi)` -> `if (true)` is unpinned.
    assert any("6:7 ConditionalExpression" in name for name in out.survivors)
    detail = {d[0]: d for d in out.survivor_details}["static/a.js:5:7 EqualityOperator"]
    assert (detail[1], detail[3]) == ("Survived", 5)
    assert detail[4] == "-  if (x < lo) return lo;\n+  if (x <= lo) return lo;"
    assert all(line in range(4, 10) for _, line in out.survivor_lines)
    assert dict(out.statuses)["Killed"] == out.killed
    assert {n for n, _, _ in out.mutant_detail} >= set(out.survivors)


@needs_stryker
def test_a_test_that_runs_the_branch_kills_that_mutant(project: Path) -> None:
    (project / "tests" / "a.test.js").write_text(
        TEST_HEAD + "test('clamp high', () => { assert.strictEqual(clamp(9, 2, 5), 5); });\n"
    )
    changed = js.changed_js_lines(project, git_diff(project, "HEAD"))
    out = js.mutation_sample(project, changed, tools=REPO)
    assert not any("6:7 ConditionalExpression" in name for name in out.survivors)


@needs_stryker
def test_a_red_suite_is_a_tool_failure_with_its_cause_not_an_empty_population(
    project: Path,
) -> None:
    (project / "tests" / "a.test.js").write_text(TEST_HEAD.replace("2)", "9)", 1))
    changed = js.changed_js_lines(project, git_diff(project, "HEAD"))
    out = js.mutation_sample(project, changed, tools=REPO)
    assert out.total == 0
    assert out.survivors[0].startswith("stryker run exited")


def test_a_tree_with_no_node_test_file_says_so(project: Path, tmp_path: Path) -> None:
    (project / "tests" / "a.test.js").unlink()
    git(project, "add", "-A")
    tools = tmp_path / "tools"
    (tools / js.STRYKER_PACKAGE).parent.mkdir(parents=True)
    (tools / js.STRYKER_PACKAGE).write_text("")
    out = js.mutation_sample(project, [(str(project / "static" / "a.js"), 5)], tools=tools)
    assert out.survivors == ("no node test file in the tree",)


def test_a_stryker_run_that_timed_out_spent_its_budget(
    monkeypatch: pytest.MonkeyPatch, project: Path, tmp_path: Path
) -> None:
    tools = tmp_path / "tools"
    (tools / js.STRYKER_PACKAGE).parent.mkdir(parents=True)
    (tools / js.STRYKER_PACKAGE).write_text("")
    monkeypatch.setattr(js, "run_capture", lambda *_a, **_k: _run(SHELL_TIMEOUT))
    out = js.mutation_sample(project, [(str(project / "static" / "a.js"), 5)], tools=tools)
    assert out.budget_spent
    assert out.total == 0
    assert out.survivors[0].startswith("stryker run exited 124")


def test_a_failed_stryker_with_no_output_names_that(
    monkeypatch: pytest.MonkeyPatch, project: Path, tmp_path: Path
) -> None:
    tools = tmp_path / "tools"
    (tools / js.STRYKER_PACKAGE).parent.mkdir(parents=True)
    (tools / js.STRYKER_PACKAGE).write_text("")
    monkeypatch.setattr(js, "run_capture", lambda *_a, **_k: _run(1))
    out = js.mutation_sample(project, [(str(project / "static" / "a.js"), 5)], tools=tools)
    assert out.survivors == ("stryker run exited 1: no output",)


def _mutant(status: str, line: int, name: str = "BooleanLiteral") -> dict[str, object]:
    where = {"start": {"line": line, "column": 3}, "end": {"line": line, "column": 7}}
    return {
        "id": "1",
        "mutatorName": name,
        "replacement": "false",
        "status": status,
        "location": where,
    }


def test_only_decided_mutants_on_changed_lines_are_counted(tmp_path: Path) -> None:
    (tmp_path / "a.js").write_text("\n".join(["let a = 1;", "if (true) {}", "if (true) {}"]) + "\n")
    report = {
        "files": {
            "a.js": {
                "mutants": [
                    _mutant("Killed", 2),
                    _mutant("Timeout", 2),
                    _mutant("Survived", 2),
                    _mutant("NoCoverage", 2),
                    _mutant("RuntimeError", 2),
                    _mutant("CompileError", 2),
                    _mutant("Ignored", 2),
                    _mutant("Survived", 3),  # a line the change did not touch
                ]
            },
            "other.js": {"mutants": [_mutant("Survived", 2)]},  # a file it did not touch
        }
    }
    out = js._outcome(report, tmp_path, {"a.js": {2}}, {"a.js": "/abs/a.js"}, _run(0))
    assert (out.killed, out.total, out.generated) == (2, 5, 7)
    assert [s.split(" ")[0] for s in out.survivors] == ["a.js:2:3"] * 3
    assert dict(out.statuses) == {
        "Killed": 1,
        "NoCoverage": 1,
        "RuntimeError": 1,
        "Survived": 1,
        "Timeout": 1,
    }
    assert out.untested == 1
    assert out.survivor_lines == (("/abs/a.js", 2),)
    assert not out.budget_spent
    assert js._outcome(
        report, tmp_path, {"a.js": {2}}, {"a.js": "/a"}, _run(SHELL_TIMEOUT)
    ).budget_spent


def test_a_multi_line_mutant_is_shown_as_the_lines_it_replaces(tmp_path: Path) -> None:
    source = ["function f() {", "  return 1;", "}"]
    mutant = _mutant("Survived", 1, "BlockStatement")
    mutant["location"] = {"start": {"line": 1, "column": 14}, "end": {"line": 3, "column": 2}}
    mutant["replacement"] = "{}"
    assert js._shown("a.js", source, mutant) == (
        "--- a/a.js\n+++ b/a.js\n-function f() {\n-  return 1;\n-}\n+function f() {}"
    )


def test_the_two_runs_merge_into_one_population_and_a_failed_run_stays_visible() -> None:
    py = MutationOutcome(
        killed=3,
        total=4,
        generated=5,
        survivors=("m.x_f__mutmut_1",),
        statuses=(("killed", 3), ("survived", 1)),
        budget_spent=False,
    )
    other = MutationOutcome(
        killed=1,
        total=2,
        generated=2,
        survivors=("a.js:2:3 EqualityOperator",),
        statuses=(("Killed", 1), ("Survived", 1)),
        budget_spent=True,
    )
    both = js.merge_outcomes(py, other)
    assert (both.killed, both.total, both.generated) == (4, 6, 7)
    assert both.survivors == ("m.x_f__mutmut_1", "a.js:2:3 EqualityOperator")
    assert both.budget_spent
    assert dict(both.statuses) == {"killed": 3, "survived": 1, "Killed": 1, "Survived": 1}
    failed = MutationOutcome(killed=0, total=0, generated=0, survivors=("stryker run exited 1: x",))
    assert "stryker run exited 1: x" in js.merge_outcomes(py, failed).survivors
