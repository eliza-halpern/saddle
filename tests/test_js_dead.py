"""A JavaScript definition only tests call, or nothing calls, is not production code.

Contract: the audit's `dead-code` check judges the top-level functions, classes and
constants a change adds to a `.js` file that something loads (a page `<script>`, an import,
a package entry): one that only tests reach, or that nothing reaches, is found and named
`file:line: name`; it is refused where the change shows padding (a private name, or other
non-test source touched) and otherwise listed `not proven`; a name another page script, an
inline handler or an import uses is production use. Node or TypeScript missing is `not
proven`, never a pass. Known-good and known-bad instances are real runs of real node.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from collections.abc import Iterator
from pathlib import Path

import pytest
from test_auditor import _dead_code, _init

from saddle import jsdead, sandbox
from saddle.auditor import Auditor
from saddle.evidence import CapturedRun, git_diff
from saddle.gates import (
    SHELL_TIMEOUT,
    TEST_ONLY_UNPROVEN,
    TOOL_UNAVAILABLE,
    JsDeadFinding,
    JsDeadReport,
    check_js_test_only_additions,
)

REPO = Path(__file__).resolve().parents[1]
STATIC = "src/saddle/web/static"
needs_node = pytest.mark.skipif(shutil.which("node") is None, reason="node is not installed")
needs_typescript = pytest.mark.skipif(
    shutil.which("node") is None or not (REPO / "node_modules/typescript").is_dir(),
    reason="typescript is not installed",
)

PAGE = (
    '<html><body><button onclick="clicked()">go</button>\n'
    '<script src="/static/lib.js"></script>\n<script src="/static/app.js"></script>\n'
    "</body></html>\n"
)
LIB_BASE = (
    "function used() {\n  return 1;\n}\n"
    "if (typeof module !== 'undefined') module.exports = { used };\n"
)
APP = "const total = used() + 1;\nconsole.log(total);\n"
TEST_BASE = (
    "const test = require('node:test');\nconst assert = require('node:assert');\n"
    "const { used } = require('../static/lib.js');\n"
    "test('used', () => { assert.strictEqual(used(), 1); });\n"
)
BASE = {
    "index.html": PAGE,
    "static/lib.js": LIB_BASE,
    "static/app.js": APP,
    "pyproject.toml": '[tool.saddle]\nsandbox-expose = ["node"]\n',
}


@pytest.fixture(autouse=True)
def node_is_shown() -> Iterator[None]:
    with sandbox.also_exposing(["node"]):
        yield


def build(tmp_path: Path, head: dict[str, str]) -> Path:
    """A repo whose baseline is `BASE` (and its test) and whose worktree adds `head`."""
    root = tmp_path / "tree"
    (root / "static").mkdir(parents=True)
    (root / "tests").mkdir()
    (root / "tests/lib.test.js").write_text(TEST_BASE)
    _init(root, BASE)
    (root / ".gitignore").write_text("node_modules\n")
    (root / "node_modules").symlink_to(REPO / "node_modules")
    for name, text in head.items():
        (root / name).parent.mkdir(parents=True, exist_ok=True)
        (root / name).write_text(text)
    subprocess.run(["git", "-C", str(root), "add", "-A"], check=True, capture_output=True)
    return root


def judged(root: Path) -> JsDeadReport:
    report = jsdead.analyse(root, git_diff(root, "HEAD"))
    assert report is not None
    return report


def lib(extra: str, exports: str = "used") -> str:
    head = LIB_BASE.rsplit("if (typeof", 1)[0]
    return f"{head}{extra}\nif (typeof module !== 'undefined') module.exports = {{ {exports} }};\n"


def only_tests_call(name: str = "onlyTested") -> dict[str, str]:
    body = f"function {name}() {{\n  return 7;\n}}\n"
    return {
        "static/lib.js": lib(body, f"used, {name}"),
        "tests/lib.test.js": TEST_BASE.replace("{ used }", f"{{ used, {name} }}")
        + f"test('{name}', () => {{ assert.strictEqual({name}(), 7); }});\n",
    }


@needs_typescript
def test_a_new_export_only_a_test_calls_is_found_and_named(tmp_path: Path) -> None:
    report = judged(build(tmp_path, only_tests_call()))
    assert [(f.file, f.name, f.kind, f.callers) for f in report.findings] == [
        ("static/lib.js", "onlyTested", "test-only", ("tests/lib.test.js",))
    ]
    assert report.findings[0].line == 4
    assert not report.unresolved


@needs_typescript
def test_a_function_nothing_calls_is_found_even_when_it_is_exported(tmp_path: Path) -> None:
    head = {"static/lib.js": lib("function orphan() {\n  return 2;\n}\n", "used, orphan")}
    report = judged(build(tmp_path, head))
    assert [(f.name, f.kind, f.callers) for f in report.findings] == [("orphan", "nothing", ())]


@needs_typescript
def test_a_name_another_page_script_uses_is_production_use(tmp_path: Path) -> None:
    head = {
        "static/lib.js": lib("function shared() {\n  return 3;\n}\n", "used, shared"),
        "static/app.js": APP + "console.log(shared());\n",
    }
    assert judged(build(tmp_path, head)).findings == ()


@needs_typescript
def test_a_name_an_inline_handler_calls_is_production_use(tmp_path: Path) -> None:
    head = {"static/lib.js": lib("function clicked() {\n  return 3;\n}\n", "used")}
    assert judged(build(tmp_path, head)).findings == ()


@needs_typescript
def test_an_import_in_production_code_is_production_use(tmp_path: Path) -> None:
    head = {
        "static/lib.js": lib("function helper() {\n  return 3;\n}\n", "used, helper"),
        "static/main.mjs": "const { helper } = require('./lib.js');\nconsole.log(helper());\n",
    }
    assert judged(build(tmp_path, head)).findings == ()


@needs_typescript
def test_only_a_dead_definition_using_a_name_does_not_make_it_live(tmp_path: Path) -> None:
    body = "function inner() {\n  return 3;\n}\nfunction outer() {\n  return inner();\n}\n"
    report = judged(build(tmp_path, {"static/lib.js": lib(body, "used, outer")}))
    assert [(f.name, f.kind) for f in report.findings] == [("outer", "nothing")]
    assert report.also == ("inner",)


@needs_typescript
def test_a_definition_that_calls_itself_is_not_its_own_caller(tmp_path: Path) -> None:
    body = "function again(n) {\n  return n ? again(n - 1) : 0;\n}\n"
    report = judged(build(tmp_path, {"static/lib.js": lib(body, "used, again")}))
    assert [(f.name, f.kind) for f in report.findings] == [("again", "nothing")]


@needs_typescript
def test_a_module_nothing_loads_is_not_judged(tmp_path: Path) -> None:
    head = {"static/spare.js": "function spare() {\n  return 1;\n}\nmodule.exports = { spare };\n"}
    report = judged(build(tmp_path, head))
    assert report.findings == ()


@needs_typescript
def test_a_file_that_does_not_parse_and_spells_the_name_is_unresolved(tmp_path: Path) -> None:
    head = {**only_tests_call(), "static/app.js": APP + "// onlyTested\n)))\n"}
    report = judged(build(tmp_path, head))
    assert report.findings == ()
    assert [(n, why) for _, _, n, why in report.unresolved] == [
        ("onlyTested", "static/app.js does not parse and spells onlyTested")
    ]


@needs_typescript
def test_a_global_looked_up_by_a_computed_name_leaves_every_finding_unresolved(
    tmp_path: Path,
) -> None:
    head = {**only_tests_call(), "static/app.js": APP + "console.log(window[process.argv[2]]);\n"}
    report = judged(build(tmp_path, head))
    assert report.findings == ()
    (unplaced,) = report.unresolved
    assert unplaced[2] == "onlyTested"
    assert "static/app.js:3" in unplaced[3]


@needs_typescript
def test_a_change_with_no_script_is_not_asked(tmp_path: Path) -> None:
    root = build(tmp_path, {"index.html": PAGE + "<!-- changed -->\n"})
    assert jsdead.analyse(root, git_diff(root, "HEAD")) is None


@needs_node
def test_without_typescript_the_analysis_is_a_named_problem_never_empty(tmp_path: Path) -> None:
    root = build(tmp_path, only_tests_call())
    (root / "node_modules").unlink()
    report = judged(root)
    assert report.findings == ()
    assert report.problem == "typescript was not found in node_modules"


@pytest.mark.parametrize(
    ("ran", "problem"),
    [
        (
            CapturedRun(("node",), TOOL_UNAVAILABLE, "", "no node"),
            "node could not be launched: no node",
        ),
        (CapturedRun(("node",), SHELL_TIMEOUT, "", ""), "the analysis timed out"),
        (CapturedRun(("node",), 1, "", "boom"), "its output did not parse (exit 1: boom)"),
        (CapturedRun(("node",), 0, "not json", ""), "its output did not parse (exit 0: no output)"),
        (CapturedRun(("node",), 0, json.dumps({"unavailable": "no parser"}), ""), "no parser"),
    ],
)
def test_a_run_that_could_not_happen_names_why(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, ran: CapturedRun, problem: str
) -> None:
    monkeypatch.setattr(jsdead, "run_capture", lambda *_a, **_k: ran)
    report = jsdead.reach(tmp_path, {"a.js": [[1, 1]]})
    assert report.problem == problem
    assert report.findings == ()


def test_changed_script_ranges_join_lines_and_leave_tests_and_other_files_out(
    tmp_path: Path,
) -> None:
    for name in ("a.js", "b.test.js", "tests/x.js", "c.py"):
        (tmp_path / name).parent.mkdir(exist_ok=True)
        (tmp_path / name).write_text("x\n" * 9)
    diff = (
        "".join(
            f"+++ b/{name}\n@@ -0,0 +{lines} @@\n"
            for name, lines in (
                ("a.js", "2,2"),
                ("b.test.js", "1"),
                ("tests/x.js", "1"),
                ("c.py", "1"),
                ("gone.js", "1"),
            )
        )
        + "+++ b/a.js\n@@ -0,0 +6,1 @@\n"
    )
    assert jsdead.changed_script_ranges(tmp_path, diff) == {"a.js": [[2, 3], [6, 6]]}


def finding(kind: str, name: str = "pad", file: str = "static/lib.js") -> JsDeadFinding:
    return JsDeadFinding(file, 4, name, kind, ("tests/lib.test.js",) if kind == "test-only" else ())


def verdict(
    *found: JsDeadFinding, touched: tuple[str, ...] = (), task: str | None = None
) -> tuple[bool, str]:
    check = check_js_test_only_additions(JsDeadReport(found), task_text=task, touched=touched)
    return check.passed, check.detail


def test_a_public_test_only_name_is_refused_beside_other_source_and_listed_alone() -> None:
    padded = verdict(finding("test-only"), touched=("static/lib.js", "static/app.css"))
    assert padded[0] is False
    assert "static/lib.js:4: pad (referenced only by tests/lib.test.js)" in padded[1]
    alone = verdict(finding("test-only"), touched=("static/lib.js", "tests/lib.test.js"))
    assert alone[0] is True
    assert alone[1].startswith(TEST_ONLY_UNPROVEN)
    assert "pad" in alone[1]


def test_a_private_or_unreached_name_is_refused_whatever_the_change_touches() -> None:
    assert verdict(finding("test-only", "_pad"), touched=("static/lib.js",))[0] is False
    assert verdict(finding("nothing"), touched=("static/lib.js",))[0] is False
    assert verdict(finding("chain"), touched=("static/lib.js",))[0] is False


def test_the_task_naming_a_public_name_excuses_it_and_never_a_private_one() -> None:
    touched = ("static/lib.js", "static/app.css")
    assert verdict(finding("test-only"), touched=touched, task="add pad to lib")[0] is True
    assert verdict(finding("nothing"), touched=touched, task="add pad to lib")[0] is True
    assert verdict(finding("test-only", "_pad"), touched=touched, task="add _pad")[0] is False
    assert verdict(finding("test-only"), touched=touched, task="a padding rule")[0] is False


def test_a_report_that_could_not_run_or_place_a_name_is_not_proven_never_a_pass() -> None:
    down = check_js_test_only_additions(JsDeadReport(problem="no parser"))
    assert down.passed
    assert down.detail.startswith(TEST_ONLY_UNPROVEN)
    assert "no parser" in down.detail
    unplaced = check_js_test_only_additions(
        JsDeadReport(unresolved=(("a.js", 3, "pad", "a file does not parse"),))
    )
    assert unplaced.passed
    assert unplaced.detail.startswith(TEST_ONLY_UNPROVEN)
    clean = check_js_test_only_additions(JsDeadReport())
    assert clean.passed
    assert not clean.detail.startswith(TEST_ONLY_UNPROVEN)


def test_what_only_a_refused_definition_uses_is_named_with_it() -> None:
    check = check_js_test_only_additions(
        JsDeadReport((finding("nothing", "outer"),), also=("inner",))
    )
    assert not check.passed
    assert "and what only these use: inner" in check.detail


@needs_typescript
def test_the_real_pages_scripts_have_no_false_finding(tmp_path: Path) -> None:
    """Every top-level name of the shipped scripts read as added: the page's own
    cross-script uses (`markdown.js` helpers `app.js` calls, inline handlers) are production
    use. The one name found, `MODES` in `app.js`, is real: nothing reads it (a comment says
    older code does, and a search of the tree finds none)."""
    copy = tmp_path / "repo"
    shutil.copytree(REPO / "src/saddle/web/static", copy / STATIC)
    shutil.copytree(
        REPO / "tests", copy / "tests", ignore=shutil.ignore_patterns("*.py", "fixtures")
    )
    (copy / "node_modules").symlink_to(REPO / "node_modules")
    scripts = {f"{STATIC}/{p.name}": [[1, 100000]] for p in (copy / STATIC).glob("*.js")}
    report = jsdead.reach(copy, scripts)
    assert report.problem == ""
    assert {f.name for f in report.findings} == {"MODES"}
    assert report.unresolved == ()


@needs_typescript
def test_the_audit_refuses_padding_in_javascript_and_passes_the_used_function(
    tmp_path: Path,
) -> None:
    """Through the real auditor with real node: a function only `lib.test.js` calls, beside a
    change to another source file, is refused naming `file:line: name`; the same change
    with the function used by the page passes."""
    head = {**only_tests_call(), "static/app.css": "body { color: black; }\n"}
    result = Auditor(build(tmp_path / "bad", head), "HEAD").tier1()
    dead = _dead_code(result)
    assert dead.verdict == "fail"
    assert "static/lib.js:4: onlyTested (referenced only by tests/lib.test.js)" in dead.detail
    used = {**only_tests_call(), "static/app.js": APP + "onlyTested();\n"}
    good = Auditor(build(tmp_path / "good", used), "HEAD").tier1()
    assert _dead_code(good).verdict == "pass", _dead_code(good).detail


@needs_typescript
def test_the_analysis_never_writes_into_the_tree_it_reads(tmp_path: Path) -> None:
    # The audit runs other tools over the same tree at the same time: eslint once
    # crashed (ENOENT scandir '.saddle') because this analysis made and removed a
    # scratch config there mid-walk. A read-only root makes any such write fail.
    root = build(tmp_path, only_tests_call())
    before = sorted(p.name for p in root.iterdir())
    root.chmod(0o555)
    try:
        report = judged(root)
    finally:
        root.chmod(0o755)
    assert not report.problem, report.problem
    assert [f.name for f in report.findings] == ["onlyTested"]
    assert sorted(p.name for p in root.iterdir()) == before
