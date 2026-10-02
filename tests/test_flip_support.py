"""Test support code (a shared fake DOM, a conftest, a helper, a data fixture) is
compared too: a pre-existing one that changed is a change named by its path.

The gap: `flips.detect` read only test bodies, so weakening a shared fake DOM or
a conftest fixture changed what every test leaning on it means, and no label was
asked for. Each case is a pair: a changed file is reported and an unchanged,
comment-only or new one is not. File names here are made up on purpose.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from saddle import flips as tc
from saddle.auditor import changed_tests, flip_finding
from saddle.flips import ChangedTest, detect, judge, language
from saddle.gates import is_test_code

SHIM = "tests/fixtures/fake_dom_sample.js"
CONF = "tests/conftest.py"
HELP = "tests/helpers.py"
MIXED_JS = "tests/widget.test.js"
MIXED_PY = "tests/test_widget.py"

SHIM_JS = """\
class Element {
  // children
  constructor() { this.childNodes = []; }
  get children() { return this.childNodes.filter((n) => n instanceof Element); }
}
module.exports = { Element };
"""
CONF_PY = "import pytest\n\n\n@pytest.fixture\ndef root(tmp_path):\n    return tmp_path\n"
HELP_PY = "def build(x):\n    return [x]\n"


def names(found: list[ChangedTest]) -> list[tuple[str, str]]:
    return [(c.name, c.kind) for c in found]


# -- the three probes ----------------------------------------------------------------


@pytest.mark.parametrize(
    ("path", "before", "after"),
    [
        (SHIM, SHIM_JS, SHIM_JS.replace("filter((n) => n instanceof Element)", "slice()")),
        (CONF, CONF_PY, CONF_PY.replace("return tmp_path", "return None")),
        (HELP, HELP_PY, HELP_PY.replace("[x]", "[]")),
    ],
)
def test_a_changed_support_file_is_a_change_named_by_its_path(
    path: str, before: str, after: str
) -> None:
    found = detect({path: before}, {path: after})
    assert names(found) == [(path, tc.SUPPORT_CHANGED)]
    assert found[0].detail.startswith("was `")


@pytest.mark.parametrize(
    ("path", "text", "edited"),
    [
        (
            SHIM,
            SHIM_JS,
            "// a new note\n" + SHIM_JS.replace("constructor() {", "constructor() {   "),
        ),
        (CONF, CONF_PY, "# a note\n" + CONF_PY.replace("\n\n\n@", "\n\n\n\n\n@")),
        (HELP, HELP_PY, HELP_PY.replace("def build", "# why\ndef build") + "\n\n"),
    ],
)
def test_a_comment_or_layout_edit_of_a_support_file_asks_for_no_label(
    path: str, text: str, edited: str
) -> None:
    assert edited != text
    assert detect({path: text}, {path: edited}) == []


def test_a_formatting_change_that_alters_code_tokens_is_a_change() -> None:
    assert names(detect({SHIM: "const a = 'x';\n"}, {SHIM: 'const a = "x";\n'})) == [
        (SHIM, tc.SUPPORT_CHANGED)
    ]
    # Python is read as a syntax tree, so quote style is not a token change there.
    assert detect({HELP: "x = 'a'\n"}, {HELP: 'x = "a"\n'}) == []


def test_a_new_support_file_is_never_a_change() -> None:
    assert detect({}, {SHIM: SHIM_JS, CONF: CONF_PY, "tests/fixtures/new.png": "x"}) == []


def test_a_deleted_support_file_is_a_change_but_a_deleted_test_module_says_it_once() -> None:
    found = detect({HELP: HELP_PY}, {})
    assert names(found) == [(HELP, tc.SUPPORT_CHANGED)]
    assert found[0].detail == "the file is deleted"
    module = "import os\n\n\ndef test_a():\n    assert os\n"
    assert names(detect({MIXED_PY: module}, {})) == [("test_a", tc.DELETED)]


def test_a_support_file_the_scan_cannot_read_is_unreadable_not_unchanged() -> None:
    assert names(detect({HELP: HELP_PY}, {HELP: "def build(:\n"})) == [(HELP, tc.UNREADABLE)]


def test_a_python_redefinition_added_later_shadows_the_old_one_and_is_a_change() -> None:
    assert names(
        detect({HELP: HELP_PY}, {HELP: HELP_PY + "\n\ndef build(x):\n    return 1\n"})
    ) == [(HELP, tc.SUPPORT_CHANGED)]


# -- a file that holds both tests and support code ------------------------------------


def test_code_outside_the_tests_of_a_mixed_js_file_is_named_so() -> None:
    base = SHIM_JS + 'test("works", () => { assert.ok(new Element()); });\n'
    head = base.replace("this.childNodes = []", "this.childNodes = {}")
    found = detect({MIXED_JS: base}, {MIXED_JS: head})
    assert names(found) == [(f"{MIXED_JS} (code outside the tests)", tc.SUPPORT_CHANGED)]


def test_code_outside_the_tests_of_a_mixed_python_file_is_named_so() -> None:
    base = HELP_PY + "\n\ndef test_it():\n    assert build(1)\n"
    head = base.replace("[x]", "[]")
    found = detect({MIXED_PY: base}, {MIXED_PY: head})
    assert names(found) == [(f"{MIXED_PY} (code outside the tests)", tc.SUPPORT_CHANGED)]


def test_a_python_assertion_only_addition_inside_a_test_is_still_free() -> None:
    base = HELP_PY + "\n\ndef test_it():\n    assert build(1)\n"
    assert detect({MIXED_PY: base}, {MIXED_PY: base + "    assert build(2)\n"}) == []


def test_an_async_test_is_not_support_code() -> None:
    base = HELP_PY + "\n\nasync def test_it():\n    assert build(1)\n"
    assert detect({MIXED_PY: base}, {MIXED_PY: base.replace("build(1)", "build(1) and 2")}) == [
        ChangedTest(MIXED_PY, "test_it", tc.ASSERTION_CHANGED, "no longer asserts: assert build(1)")
    ]


def test_adding_a_test_beside_unchanged_support_code_is_free() -> None:
    js = SHIM_JS + 'test("works", () => { assert.ok(1); });\n'
    more = js.replace(
        "module.exports = { Element };\n",
        "module.exports = { Element };\n",
    )
    assert (
        detect({MIXED_JS: js}, {MIXED_JS: more + 'test("more", () => { assert.ok(2); });\n'}) == []
    )


def test_the_label_for_a_mixed_file_is_its_stable_name() -> None:
    change = ChangedTest(MIXED_JS, f"{MIXED_JS} (code outside the tests)", tc.SUPPORT_CHANGED, "x")
    line = f"flip: {MIXED_JS} (code outside the tests) -- the shim now models a real child list"
    assert judge([change], line).verdict == "not-proven"  # type: ignore[union-attr]
    bare = judge([change], f"flip: {MIXED_JS} -- the shim now models a real child list")
    assert bare is not None
    assert bare.verdict == "fail"
    assert f"'{MIXED_JS} (code outside the tests)'" in bare.detail


# -- data fixtures and other languages ------------------------------------------------


def test_a_changed_data_fixture_is_a_change_and_an_untouched_one_is_not() -> None:
    path = "tests/fixtures/sample.json"
    assert detect({path: "[1]"}, {path: "[1]"}) == []
    assert names(detect({path: "[1]"}, {path: "[2]"})) == [(path, tc.FILE_CHANGED)]


# -- the one rule for what is test code -----------------------------------------------


@pytest.mark.parametrize(
    ("path", "expected"),
    [
        ("tests/conftest.py", "python"),
        ("conftest.py", "python"),
        ("tests/helpers.py", "python"),
        ("tests/fixtures/dom_shim_sample.js", "js"),
        ("tests/data.json", "other"),
        ("tests/fixtures/shot.png", "other"),
        ("tests/fixtures/test_names.txt", "other"),
        ("pkg/test/helpers.rb", "other"),
        ("src/calc.py", None),
        ("src/web/static/app.js", None),
        ("docs/guide.md", None),
        ("pkg/calc_test.go", "other"),
        ("lib/parse.spec.rb", "other"),
    ],
)
def test_the_answer_for_what_is_test_code_is_the_shared_rules_plus_named_languages(
    path: str, expected: str | None
) -> None:
    assert language(path) == expected


def test_every_path_the_shared_rule_calls_test_code_is_read_by_some_reading() -> None:
    for path in ("tests/a.py", "x/tests/y/z.bin", "a.test.js", "b/c.spec.ts", "d/test_e.py"):
        assert is_test_code(path)
        assert language(path) is not None
    for path in ("src/a.py", "web/app.js", "README.md"):
        assert not is_test_code(path)
        assert language(path) is None


# -- end to end through the audit -----------------------------------------------------


def git(root: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(root), "-c", "user.name=t", "-c", "user.email=t@t", *args],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    root = tmp_path / "repo"
    files = {
        SHIM: SHIM_JS,
        CONF: CONF_PY,
        HELP: HELP_PY,
        "src/app.py": "x = 1\n",
    }
    for rel, text in files.items():
        (root / rel).parent.mkdir(parents=True, exist_ok=True)
        (root / rel).write_text(text)
    (root / "tests/fixtures/shot.bin").write_bytes(b"\x89PNG\xff\xfe\x00")
    git(tmp_path, "init", "-q", "-b", "main", str(root))
    git(root, "add", "-A")
    git(root, "commit", "-q", "-m", "init")
    return root


WEAKENED = {
    SHIM: SHIM_JS.replace("filter((n) => n instanceof Element)", "slice()"),
    CONF: CONF_PY.replace("return tmp_path", "return None"),
    HELP: HELP_PY.replace("[x]", "[]"),
}
EVIDENCE = "a real run in which the old helper let a wrong value through: build(2) gave [2]"


@pytest.mark.parametrize("path", [SHIM, CONF, HELP])
def test_the_audit_refuses_a_weakened_support_file_and_accepts_a_labelled_one(
    repo: Path, path: str
) -> None:
    (repo / path).write_text(WEAKENED[path])
    found = flip_finding(repo, "HEAD", "")
    assert found is not None
    assert found.verdict == "fail"
    assert f"'{path}'" in found.detail
    labelled = flip_finding(repo, "HEAD", f"flip: {path} -- {EVIDENCE}")
    assert labelled is not None
    assert labelled.verdict == "not-proven"
    assert "needs a person's review" in labelled.detail
    circular = flip_finding(repo, "HEAD", f"flip: {path} -- the new code fails the old test")
    assert circular is not None
    assert circular.verdict == "fail"
    assert tc.CIRCULAR_SAID in circular.detail


def test_a_binary_data_fixture_is_compared_not_refused_and_a_new_one_is_free(repo: Path) -> None:
    assert flip_finding(repo, "HEAD", "") is None
    (repo / "tests/fixtures/shot.bin").write_bytes(b"\x89PNG\xff\xfe\x01")
    found = changed_tests(repo, "HEAD")
    assert names(found) == [("tests/fixtures/shot.bin", tc.FILE_CHANGED)]
    git(repo, "checkout", "--", "tests/fixtures/shot.bin")
    (repo / "tests/fixtures/added.bin").write_bytes(b"\xff\xfe")
    (repo / "tests/fixtures/added_support.js").write_text("module.exports = 1;\n")
    assert flip_finding(repo, "HEAD", "") is None
