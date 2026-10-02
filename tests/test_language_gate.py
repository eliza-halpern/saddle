"""A finding is shown only when the change touches the language it is about.

Contract: the diff's languages are decided once per audit (`languages.classify`);
a Python-only change carries no `js-*` finding and no JavaScript stage in the
`Gate:` line, a JavaScript-only change carries no Python coverage or dead-code
finding, a change touching both carries both, and a finding that refuses is never
hidden: a JavaScript change that breaks the Python suite is still refused.
Instances are real audits of a tiny project; the units are the lookups.
"""

from __future__ import annotations

import shutil
from pathlib import Path
from typing import Any

import pytest
from test_auditor import _init

from saddle import runner
from saddle.auditor import Auditor, Findings
from saddle.evidence import MutationOutcome, SuiteLimitError, gate_stage_languages
from saddle.languages import classify, stage_visible, touches, visible

needs_node = pytest.mark.skipif(shutil.which("node") is None, reason="node is not installed")

BASE_JS = "function add(a, b) {\n  return a + b;\n}\nmodule.exports = { add };\n"
HEAD_JS = BASE_JS.replace("a + b", "b + a")
JS_TEST = (
    "const test = require('node:test');\nconst assert = require('node:assert');\n"
    "const { add } = require('../static/a.js');\n"
    "test('add', () => { assert.strictEqual(add(1, 2), 3); });\n"
)
PY_TEST = (
    "from pathlib import Path\n\nfrom n import f\n\n\n"
    "def test_f():\n    assert f() == 2\n\n\n"
    "def test_js_still_exports_add():\n"
    "    assert 'module.exports' in (Path(__file__).parent / 'static/a.js').read_text()\n"
)
SADDLE = (
    "[tool.saddle]\n"
    'sandbox-expose = ["node"]\n'
    'gate-checks = [["sh", "py_stage.sh"], ["sh", "js_stage.sh"]]\n'
    'gate-stage-languages = {"sh py_stage.sh" = ["python"], "sh js_stage.sh" = ["javascript"]}\n'
)
FILES = {
    "n.py": "def f():\n    return 1\n",
    "test_n.py": PY_TEST.replace("== 2", "== 1"),
    "static/a.js": BASE_JS,
    "tests/a.test.js": JS_TEST,
    "py_stage.sh": "exit 0\n",
    "js_stage.sh": "exit 0\n",
    "pyproject.toml": SADDLE,
}
NOTHING = MutationOutcome(killed=0, total=0, generated=0, survivors=())


@pytest.fixture(autouse=True)
def python_mutation_is_not_under_test(monkeypatch: pytest.MonkeyPatch) -> None:
    def fake(*_args: Any, **_kwargs: Any) -> MutationOutcome:
        return NOTHING

    monkeypatch.setattr(runner, "mutation_sample", fake)


def project(tmp_path: Path, **head: str | None) -> Path:
    """A committed project; `head` rewrites (or, None, deletes) files in the working tree."""
    root = tmp_path / "tree"
    (root / "static").mkdir(parents=True)
    (root / "tests").mkdir()
    _init(root, FILES)
    for rel, text in head.items():
        path = root / rel.replace("__", "/")
        if text is None:
            path.unlink()
        else:
            path.write_text(text)
    return root


def gates(found: Findings) -> set[str]:
    return {f.gate for f in found.findings}


def gate_line(found: Findings) -> str:
    detail = next(f.detail for f in found.findings if f.gate == "project-gate")
    return detail.splitlines()[0]


PY_CHANGE = {"n.py": "def f():\n    return 2\n", "test_n.py": PY_TEST}
JS_CHANGE = {"static__a.js": HEAD_JS}


def test_a_python_only_change_has_no_js_findings_and_no_js_stage(tmp_path: Path) -> None:
    found = Auditor(project(tmp_path, **PY_CHANGE)).tier1()
    assert {"tests", "coverage"} <= gates(found)
    assert not {g for g in gates(found) if g.startswith("js-")}
    assert gate_line(found) == "Gate: base ✓, head ✓ (1 stage)"


@needs_node
def test_a_js_only_change_has_no_python_coverage_or_tests_finding(tmp_path: Path) -> None:
    root = project(tmp_path, **JS_CHANGE)
    found = Auditor(root).tier1()
    assert "js-tests" in gates(found)
    assert not gates(found) & {"tests", "coverage", "public-deletions", "assertion-preservation"}
    assert gate_line(found) == "Gate: base ✓, head ✓ (1 stage)"
    second = Auditor(root).tier2()
    assert not gates(second) & {"property-coverage", "red-phase", "mutation", "full-suite"}


@needs_node
def test_a_change_touching_both_shows_both(tmp_path: Path) -> None:
    found = Auditor(project(tmp_path, **PY_CHANGE, **JS_CHANGE)).tier1()
    assert {"tests", "coverage", "js-tests"} <= gates(found)
    assert gate_line(found) == "Gate: base ✓, head ✓ (2 stages)"


@needs_node
def test_a_deleted_js_file_counts_as_touching_javascript(tmp_path: Path) -> None:
    found = Auditor(project(tmp_path, **PY_CHANGE, **{"static__a.js": None})).tier1()
    assert gate_line(found) == "Gate: base ✓, head ✓ (2 stages)"


def test_a_config_file_in_the_change_hides_nothing(tmp_path: Path) -> None:
    config = {**PY_CHANGE, "pyproject.toml": SADDLE + "# edited\n"}
    found = Auditor(project(tmp_path, **config)).tier1()
    assert gate_line(found) == "Gate: base ✓, head ✓ (2 stages)"


@needs_node
def test_a_js_change_that_breaks_the_python_suite_is_still_refused(tmp_path: Path) -> None:
    root = project(tmp_path, **{"static__a.js": "function add() {}\n"})
    found = Auditor(root).tier1()
    refused = next(f for f in found.findings if f.gate == "tests")
    assert refused.verdict == "fail"
    assert not found.passed


def test_unit_lookups() -> None:
    assert classify(["a.py", "b/c.PYI"]) == {"python"}
    assert classify(["a.mjs", "x.cjs", "y.js"]) == {"javascript"}
    assert classify(["Makefile"]) == {"other"}
    assert classify([]) == {"other"}
    assert not touches({"python"}, {"javascript"})
    assert touches({"config"}, {"javascript"})
    assert visible("js-tests", "pass", {"python"}) is False
    assert visible("js-tests", "fail", {"python"}) is True
    assert visible("coverage", "pass", {"javascript"}) is False
    assert visible("task-requirements", "pass", {"python"}) is True
    # mutation is about JavaScript only where its tool measures it
    assert visible("mutation", "not-proven", {"javascript"}, ("javascript",)) is False
    assert visible("mutation", "pass", {"javascript"}) is True
    declared = {"ruff": ("python",), "sh js.sh": ("javascript",)}
    assert stage_visible("ruff check", {"javascript"}, declared) is False
    assert stage_visible("ruff check", {"python"}, declared) is True
    assert stage_visible("sh js.sh", {"python"}, declared) is False
    assert stage_visible("uv lock", {"python"}, declared) is True


def test_a_bad_stage_language_table_names_the_commit(tmp_path: Path) -> None:
    root = tmp_path / "tree"
    _init(root, {"pyproject.toml": '[tool.saddle]\ngate-stage-languages = {ruff = ["cobol"]}\n'})
    with pytest.raises(SuiteLimitError, match="gate stage languages"):
        gate_stage_languages(root, "HEAD")
    _init(tmp_path / "t2", {"pyproject.toml": "[tool.saddle]\n"})
    assert gate_stage_languages(tmp_path / "t2", "HEAD") == {}
    _init(tmp_path / "t3", {"n.py": "x = 1\n"})
    assert gate_stage_languages(tmp_path / "t3", "HEAD") == {}


@needs_node
def test_a_python_static_check_is_not_run_for_a_js_only_change(tmp_path: Path) -> None:
    table = SADDLE.replace("gate-checks", 'static-check = ["sh", "py_stage.sh"]\ngate-checks')
    root = tmp_path / "tree"
    (root / "static").mkdir(parents=True)
    (root / "tests").mkdir()
    _init(root, {**FILES, "pyproject.toml": table})
    (root / "static/a.js").write_text(HEAD_JS)
    assert "static-check" not in gates(Auditor(root).tier1())
    (root / "n.py").write_text("def f():\n    return 1\n# x\n")
    assert "static-check" in gates(Auditor(root).tier1())
