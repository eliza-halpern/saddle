"""The audit runs a changed `.js` tree's node tests itself, and counts StrykerJS mutants.

Contract: when a change touches a `.js` file, the tier-2 audit records one
result per node test (`js-tests`), runs the new or changed node tests on the
baseline (`js-red-phase`), and, where StrykerJS is installed, counts the
mutants of the changed `.js` lines in the `mutation` check beside the Python
ones. Known-bad instances are real runs: a node test that fails, a new test
that already passes on the baseline, a tree with no node test, a mutant no test
kills. Python-only changes get none of the two findings.
"""

from __future__ import annotations

import json
import shutil
from pathlib import Path
from typing import Any

import pytest
from test_auditor import _init

from saddle import runner
from saddle.auditor import JS_RED_PHASE_GATE, JS_TESTS_GATE, Auditor, AuditorConfig, Findings
from saddle.evidence import MutationOutcome
from saddle.journal import read_spans

REPO = Path(__file__).resolve().parents[1]
needs_node = pytest.mark.skipif(shutil.which("node") is None, reason="node is not installed")
needs_stryker = pytest.mark.skipif(
    shutil.which("node") is None or not (REPO / "node_modules/@stryker-mutator/core").is_dir(),
    reason="StrykerJS is not installed",
)

BASE_JS = "function add(a, b) {\n  return a + b;\n}\nmodule.exports = { add };\n"
HEAD_JS = (
    BASE_JS.split("module.exports")[0]
    + "function clamp(x, lo, hi) {\n  if (x < lo) return lo;\n  if (x > hi) return hi;\n"
    "  return x;\n}\nmodule.exports = { add, clamp };\n"
)
HEAD_TEST = (
    "const test = require('node:test');\nconst assert = require('node:assert');\n"
    "const { clamp, add } = require('../static/a.js');\n"
    "test('clamp low', () => { assert.strictEqual(clamp(1, 2, 5), 2); });\n"
    "test('add', () => { assert.strictEqual(add(1, 2), 3); });\n"
)
FILES = {
    "n.py": "def f():\n    return 1\n",
    "test_n.py": "from n import f\n\n\ndef test_f():\n    assert f() == 1\n",
    "static/a.js": BASE_JS,
    "pyproject.toml": '[tool.saddle]\nsandbox-expose = ["node"]\n',
    ".gitignore": "node_modules\n",
}
NOTHING = MutationOutcome(killed=0, total=0, generated=0, survivors=())


def project(tmp_path: Path, *, test: str | None = HEAD_TEST, stryker: bool = False) -> Path:
    root = tmp_path / "tree"
    (root / "static").mkdir(parents=True)
    _init(root, FILES)
    (root / "static/a.js").write_text(HEAD_JS)
    if test is not None:
        (root / "tests").mkdir()
        (root / "tests/a.test.js").write_text(test)
    if stryker:
        (root / "node_modules").symlink_to(REPO / "node_modules")
    return root


@pytest.fixture(autouse=True)
def python_mutation_is_not_under_test(monkeypatch: pytest.MonkeyPatch) -> None:
    def fake(*_args: Any, **_kwargs: Any) -> MutationOutcome:
        return NOTHING

    monkeypatch.setattr(runner, "mutation_sample", fake)


def finding(found: Findings, gate: str) -> Any:
    return next((f for f in found.findings if f.gate == gate), None)


@needs_node
def test_new_node_tests_are_run_per_test_and_red_on_the_baseline(tmp_path: Path) -> None:
    journal = tmp_path / "ledger" / "proofs.jsonl"
    found = Auditor(project(tmp_path), "HEAD", AuditorConfig(journal=journal)).tier2()
    tests = finding(found, JS_TESTS_GATE)
    assert (tests.verdict, tests.detail) == ("pass", "node --test: 2 passed, 0 failed, 0 skipped")
    red = finding(found, JS_RED_PHASE_GATE)
    assert red.verdict == "pass"
    assert red.detail.startswith("1 of 2 node tests fail pre-change, pass post-change")
    assert "green on the baseline: tests/a.test.js: add" in red.detail
    (span,) = [s for s in read_spans(journal) if s.name == f"audit-tier2:{JS_TESTS_GATE}"]
    sidecar = json.loads(next((journal.parent / "attempts").glob(f"{span.span_id}*")).read_text())
    assert sidecar["results"] == [
        ["tests/a.test.js", "clamp low", "pass"],
        ["tests/a.test.js", "add", "pass"],
    ]
    (span,) = [s for s in read_spans(journal) if s.name == f"audit-tier2:{JS_RED_PHASE_GATE}"]
    red_sidecar = json.loads(
        next((journal.parent / "attempts").glob(f"{span.span_id}*")).read_text()
    )
    assert red_sidecar["base"] == [
        ["tests/a.test.js", "clamp low", "fail"],
        ["tests/a.test.js", "add", "pass"],
    ]
    assert red_sidecar["base_exit_code"] == 1


@needs_node
def test_a_failing_node_test_refuses_the_change(tmp_path: Path) -> None:
    failing = HEAD_TEST.replace("strictEqual(add(1, 2), 3)", "strictEqual(add(1, 2), 4)")
    found = Auditor(project(tmp_path, test=failing)).tier2()
    tests = finding(found, JS_TESTS_GATE)
    assert tests.verdict == "fail"
    assert "tests/a.test.js: add" in tests.detail
    assert not found.passed
    assert finding(found, JS_RED_PHASE_GATE).detail == "tests fail post-change"


@needs_node
def test_a_new_test_that_passes_on_the_baseline_proves_nothing(tmp_path: Path) -> None:
    only_add = HEAD_TEST.split("test('clamp low'")[0] + HEAD_TEST.split("\n")[-2] + "\n"
    found = Auditor(project(tmp_path, test=only_add)).tier2()
    red = finding(found, JS_RED_PHASE_GATE)
    assert red.verdict == "fail"
    assert red.detail == "node tests pass pre-change (exit 0); prove nothing"
    assert not found.passed


@needs_node
def test_a_js_change_with_no_node_test_is_not_proven_never_a_pass(tmp_path: Path) -> None:
    found = Auditor(project(tmp_path, test=None)).tier2()
    tests = finding(found, JS_TESTS_GATE)
    assert tests.verdict == "not-proven"
    assert "no node test file in the tree" in tests.detail
    assert finding(found, JS_RED_PHASE_GATE) is None


def test_a_change_with_no_js_file_gets_neither_finding(tmp_path: Path) -> None:
    root = tmp_path / "tree"
    (root / "static").mkdir(parents=True)
    _init(root, FILES)
    (root / "n.py").write_text("def f():\n    return 2\n")
    (root / "test_n.py").write_text(FILES["test_n.py"].replace("== 1", "== 2"))
    found = Auditor(root).tier2()
    assert finding(found, JS_TESTS_GATE) is None
    assert finding(found, JS_RED_PHASE_GATE) is None


@needs_stryker
def test_changed_js_lines_get_mutants_that_the_mutation_check_counts(tmp_path: Path) -> None:
    found = Auditor(project(tmp_path, stryker=True)).tier2()
    mutation = finding(found, "mutation")
    assert mutation.verdict == "fail"
    assert "static/a.js:" in mutation.detail
    assert finding(found, "not-measurable") is None


@needs_node
def test_without_stryker_the_js_file_is_listed_as_not_measurable_as_before(
    tmp_path: Path,
) -> None:
    found = Auditor(project(tmp_path)).tier2()
    listed = finding(found, "not-measurable")
    assert listed is not None
    assert "static/a.js" in listed.detail
    assert "measure Python only" in listed.detail


@needs_node
def test_a_source_only_js_change_runs_the_tests_and_has_no_red_phase(tmp_path: Path) -> None:
    root = tmp_path / "tree"
    (root / "static").mkdir(parents=True)
    (root / "tests").mkdir()
    _init(root, {**FILES, "tests/a.test.js": HEAD_TEST.replace("clamp low", "add twice")})
    (root / "static/a.js").write_text(HEAD_JS)
    found = Auditor(root).tier2()
    assert finding(found, JS_TESTS_GATE).verdict == "pass"
    assert finding(found, JS_RED_PHASE_GATE) is None


@needs_node
def test_a_baseline_run_that_could_not_run_is_not_proven_never_red(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from saddle import auditor as auditor_mod
    from saddle.jsevidence import JsRedPhase, JsTestRun

    def timed_out(*_args: Any, **_kwargs: Any) -> JsRedPhase:
        return JsRedPhase(("tests/a.test.js",), (), JsTestRun(124, problem="node --test timed out"))

    monkeypatch.setattr(auditor_mod, "red_phase", timed_out)
    found = Auditor(project(tmp_path)).tier2()
    red = finding(found, JS_RED_PHASE_GATE)
    assert (red.verdict, red.detail) == ("not-proven", "node --test timed out")
    assert found.passed
