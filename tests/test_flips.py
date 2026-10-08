"""`saddle.flips`: pre-existing tests the tree changed, and the `flip:` rule.

Each detection case is a pair: the tree that changed a test is reported, and
the tree that did not (or only added) is silent. A constraint is checked by
instances, never by its text: the circular-evidence predicate rejects known-bad
lines and accepts known-good ones.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from saddle import feed
from saddle import flips as tc
from saddle.flips import ChangedTest, detect, judge, language

FIXTURES = Path(__file__).parent / "fixtures"
JS = "tests/markdown.test.js"
PY = "tests/test_calc.py"


def kinds(found: list[ChangedTest]) -> list[tuple[str, str]]:
    return [(c.name, c.kind) for c in found]


# -- which files are test code ------------------------------------------------------


@pytest.mark.parametrize(
    ("path", "expected"),
    [
        ("tests/test_calc.py", "python"),
        ("pkg/calc_test.py", "python"),
        ("tests/conftest.py", "python"),
        ("src/calc.py", None),
        ("tests/markdown.test.js", "js"),
        ("web/app.spec.ts", "js"),
        ("tests/helpers.mjs", "js"),
        ("src/web/static/app.js", None),
        ("tests/fixtures/page_driver.mjs", "js"),
        ("pkg/calc_test.go", "other"),
        ("tests/data.json", "other"),
        ("tests/fixtures/test_names.txt", "other"),
    ],
)
def test_language_reads_the_name_and_the_directory(path: str, expected: str | None) -> None:
    assert language(path) == expected


# -- Python ---------------------------------------------------------------------------

BASE_PY = """\
import pytest


def test_add():
    assert add(2, 2) == 4
    assert add(0, 0) == 0


def test_parse():
    with pytest.raises(ValueError):
        parse("x")


def test_other():
    assert other() == 1
"""


def py(text: str) -> list[ChangedTest]:
    return detect({PY: BASE_PY}, {PY: text})


def test_an_untouched_python_file_reports_nothing() -> None:
    assert py(BASE_PY) == []


def test_a_changed_assertion_is_reported_and_names_the_old_one() -> None:
    found = py(BASE_PY.replace("add(2, 2) == 4", "add(2, 2) == 5"))
    assert kinds(found) == [("test_add", tc.ASSERTION_CHANGED)]
    assert "add(2, 2) == 4" in found[0].detail


def test_assertions_only_added_are_not_a_flip() -> None:
    more = BASE_PY.replace(
        "assert add(0, 0) == 0\n", "assert add(0, 0) == 0\n    assert add(1, 1) == 2\n"
    )
    assert more != BASE_PY
    assert py(more) == []


def test_a_removed_assertion_is_reported() -> None:
    found = py(BASE_PY.replace("    assert add(0, 0) == 0\n", ""))
    assert kinds(found) == [("test_add", tc.ASSERTION_CHANGED)]


def test_a_raises_block_turned_into_a_plain_call_is_reported() -> None:
    loosened = BASE_PY.replace(
        '    with pytest.raises(ValueError):\n        parse("x")\n', '    parse("x")\n'
    )
    assert kinds(py(loosened)) == [("test_parse", tc.RAISES_REMOVED)]


def test_a_raises_block_with_another_exception_is_reported() -> None:
    changed = BASE_PY.replace("pytest.raises(ValueError)", "pytest.raises(Exception)")
    assert kinds(py(changed)) == [("test_parse", tc.RAISES_REMOVED)]


def test_a_deleted_test_is_reported() -> None:
    found = py(BASE_PY.replace("def test_other():\n    assert other() == 1\n", ""))
    assert kinds(found) == [("test_other", tc.DELETED)]


def test_a_renamed_test_is_reported_as_a_rename_with_the_new_name() -> None:
    found = py(BASE_PY.replace("test_other", "test_the_other_thing"))
    assert kinds(found) == [("test_other", tc.RENAMED)]
    assert "test_the_other_thing" in found[0].detail
    assert "unchanged" in found[0].detail


def test_a_renamed_test_whose_body_also_changed_says_so() -> None:
    changed = BASE_PY.replace("test_other", "test_renamed").replace("other() == 1", "other() == 2")
    found = py(changed)
    assert kinds(found) == [("test_other", tc.RENAMED)]
    assert "also changed" in found[0].detail


def test_a_test_moved_to_another_file_is_not_lost() -> None:
    moved = BASE_PY.replace("def test_other():\n    assert other() == 1\n", "")
    found = detect(
        {PY: BASE_PY},
        {PY: moved, "tests/test_more.py": "def test_other():\n    assert other() == 1\n"},
    )
    assert found == []


def test_a_test_moved_with_a_weaker_assertion_is_reported() -> None:
    moved = BASE_PY.replace("def test_other():\n    assert other() == 1\n", "")
    found = detect(
        {PY: BASE_PY},
        {PY: moved, "tests/test_more.py": "def test_other():\n    assert other()\n"},
    )
    assert kinds(found) == [("test_other", tc.ASSERTION_CHANGED)]


def test_a_deleted_file_reports_its_tests() -> None:
    found = detect({PY: BASE_PY}, {})
    assert sorted(c.name for c in found) == ["test_add", "test_other", "test_parse"]
    assert {c.kind for c in found} == {tc.DELETED}


def test_a_skip_or_xfail_added_to_a_test_is_reported() -> None:
    for mark in (
        "@pytest.mark.skip(reason='x')",
        "@pytest.mark.xfail",
        "@pytest.mark.skipif(True)",
    ):
        marked = BASE_PY.replace("def test_other", f"{mark}\ndef test_other")
        assert kinds(py(marked)) == [("test_other", tc.MARKED)], mark


def test_a_skip_call_in_the_body_and_a_class_or_module_mark_are_reported() -> None:
    called = BASE_PY.replace(
        "    assert other() == 1", "    pytest.skip('later')\n    assert other() == 1"
    )
    assert kinds(py(called)) == [("test_other", tc.MARKED)]
    cls = BASE_PY + "\n\nclass TestGroup:\n    def test_inner(self):\n        assert 1\n"
    base = {PY: cls}
    on_class = cls.replace("class TestGroup", "@pytest.mark.skip\nclass TestGroup")
    # the class's own decorator is code outside the tests, so it is reported as well
    assert kinds(detect(base, {PY: on_class})) == [
        ("test_inner", tc.MARKED),
        (f"{PY} (code outside the tests)", tc.SUPPORT_CHANGED),
    ]
    module = cls.replace("import pytest\n", "import pytest\n\npytestmark = pytest.mark.skip\n")
    assert {c.name for c in detect(base, {PY: module})} == {
        f"{PY} (code outside the tests)",
        "test_add",
        "test_parse",
        "test_other",
        "test_inner",
    }


def test_unittest_style_assertions_are_read_too() -> None:
    base = "class T:\n    def test_a(self):\n        self.assertEqual(f(), 1)\n"
    changed = base.replace("1", "2")
    assert kinds(detect({PY: base}, {PY: changed})) == [("test_a", tc.ASSERTION_CHANGED)]
    assert detect({PY: base}, {PY: base + "\n# note\n"}) == []


def test_an_unparsable_python_file_is_its_own_finding_never_nothing() -> None:
    found = py("def test_add(:\n")
    assert kinds(found) == [(PY, tc.UNREADABLE)]
    assert detect({PY: "def test_add(:\n"}, {PY: BASE_PY})[0].detail.startswith("baseline")


def test_a_file_in_another_language_is_compared_whole() -> None:
    path = "pkg/calc_test.go"
    assert detect({path: "func TestA() {}"}, {path: "func TestA() {}"}) == []
    changed = detect({path: "func TestA() {}"}, {path: "func TestA() { skip() }"})
    assert kinds(changed) == [(path, tc.FILE_CHANGED)]
    assert "deleted" in detect({path: "x"}, {})[0].detail


# -- JavaScript -----------------------------------------------------------------------

BASE_JS = """\
const { test, describe } = require("node:test");
const assert = require("node:assert");

test("adds", () => {
  assert.strictEqual(add(2, 2), 4);
});

describe("parsing", () => {
  test("rejects x", () => {
    assert.throws(() => parse("x"));
  });
  test("accepts y", () => {
    assert.ok(parse("y"));
  });
});
"""


def js(text: str) -> list[ChangedTest]:
    return detect({JS: BASE_JS}, {JS: text})


def test_the_recorded_rewrite_of_a_pre_existing_test_is_found() -> None:
    base = (FIXTURES / "renderer_suite_base.txt").read_text()
    head = (FIXTURES / "renderer_suite_head.txt").read_text()
    found = detect({JS: base}, {JS: head})
    # the recorded rewrite also extended the fake DOM above the tests
    assert [c.name for c in found] == [
        "output that is not a diff is left alone",
        f"{JS} (code outside the tests)",
    ]
    assert found[0].kind == tc.RENAMED
    assert "keeps its text and gains a copy button" in found[0].detail


def test_the_recorded_suite_read_against_itself_reports_nothing() -> None:
    head = (FIXTURES / "renderer_suite_head.txt").read_text()
    assert detect({JS: head}, {JS: head + "\n// trailing note\n"}) == []


def test_formatting_and_comments_are_not_a_change() -> None:
    spaced = BASE_JS.replace(
        "assert.strictEqual(add(2, 2), 4);",
        "// why\n  assert.strictEqual(\n    add(2, 2),\n    4\n  ); /* x */",
    )
    assert js(spaced) == []


def test_a_changed_expectation_is_reported_with_the_old_and_new_text() -> None:
    found = js(BASE_JS.replace("add(2, 2), 4", "add(2, 2), 5"))
    assert kinds(found) == [("adds", tc.BODY_CHANGED)]
    assert "4" in found[0].detail
    assert "5" in found[0].detail


def test_a_changed_string_is_a_change_even_when_only_its_spacing_differs() -> None:
    base = 'test("pads", () => { assert.strictEqual(pad(), "a  b"); });\n'
    changed = base.replace("a  b", "a b")
    assert kinds(detect({JS: base}, {JS: changed})) == [("pads", tc.BODY_CHANGED)]


def test_a_test_added_beside_the_old_ones_is_not_a_flip() -> None:
    assert js(BASE_JS + 'test("new one", () => { assert.ok(1); });\n') == []


def test_a_deleted_js_test_is_reported() -> None:
    gone = BASE_JS.replace('  test("accepts y", () => {\n    assert.ok(parse("y"));\n  });\n', "")
    assert kinds(js(gone)) == [("accepts y", tc.DELETED)]


def test_a_renamed_js_test_is_reported_with_its_new_title() -> None:
    found = js(BASE_JS.replace('"adds"', '"adds two numbers"'))
    assert kinds(found) == [("adds", tc.RENAMED)]
    assert "adds two numbers" in found[0].detail


def test_a_js_test_moved_to_another_file_is_not_lost() -> None:
    moved = BASE_JS.replace('test("adds", () => {\n  assert.strictEqual(add(2, 2), 4);\n});\n', "")
    other = 'test("adds", () => {\n  assert.strictEqual(add(2, 2), 4);\n});\n'
    assert detect({JS: BASE_JS}, {JS: moved, "tests/more.test.js": other}) == []


def test_skipping_a_test_or_its_group_is_a_change() -> None:
    assert kinds(js(BASE_JS.replace('test("adds"', 'test.skip("adds"'))) == [
        ("adds", tc.BODY_CHANGED)
    ]
    grouped = js(BASE_JS.replace('describe("parsing"', 'describe.skip("parsing"'))
    assert kinds(grouped) == [("parsing", tc.BODY_CHANGED)]


def test_a_group_is_judged_on_its_own_code_not_on_its_tests() -> None:
    hooked = BASE_JS.replace(
        'describe("parsing", () => {\n',
        'describe("parsing", () => {\n  beforeEach(() => reset());\n',
    )
    assert kinds(js(hooked)) == [("parsing", tc.BODY_CHANGED)]
    inner = BASE_JS.replace('assert.ok(parse("y"))', "assert.ok(1)")
    assert kinds(js(inner)) == [("accepts y", tc.BODY_CHANGED)]


def test_a_computed_title_is_compared_by_its_body() -> None:
    base = "for (const n of [1]) { test(`case ${n}`, () => { assert.ok(n); }); }\n"
    assert detect({JS: base}, {JS: base}) == []
    changed = base.replace("assert.ok(n)", "assert.ok(1)")
    assert kinds(detect({JS: base}, {JS: changed})) == [("case ${n}", tc.BODY_CHANGED)]
    dynamic = "test(name, () => { assert.ok(1); });\n"
    assert kinds(detect({JS: dynamic}, {JS: dynamic.replace("1", "2")})) == [
        ("<computed title>", tc.BODY_CHANGED)
    ]


def test_a_function_named_test_is_not_a_test_call() -> None:
    base = "function test(a) { return a; }\n"
    found = detect({JS: base}, {JS: base.replace("return a", "return 1")})
    assert [(c.name, c.kind) for c in found] == [(JS, tc.SUPPORT_CHANGED)]  # code, not a test


def test_quotes_brackets_regexes_and_templates_inside_a_test_do_not_confuse_the_scan() -> None:
    body = (
        'test("odd", () => {\n'
        '  const a = /["\')}]+/.test("x"); // a ) in a comment\n'
        '  const b = `t ${ {k: "}"}.k } )`;\n'
        "  const c = 4 / 2 / 1;\n"
        "  const d = 'it\\'s (';\n"
        "  assert.ok(a, b, c, d);\n"
        "});\n"
    )
    after = 'test("next", () => { assert.ok(1); });\n'
    base = body + after
    assert detect({JS: base}, {JS: base}) == []
    changed = base.replace("assert.ok(1)", "assert.ok(2)")
    assert kinds(detect({JS: base}, {JS: changed})) == [("next", tc.BODY_CHANGED)]


@pytest.mark.parametrize(
    "broken",
    [
        'test("a", () => { assert.ok(1);\n',
        'test("a", () => { assert.ok(1); });})\n',
        'test("a", () => { const s = "open;\n});\n',
        'test("a", () => { /* never closed\n',
        'test("a", () => { const t = `open ${ 1 \n});\n',
        'test("a", () => { const t = `open;\n});\n',
        'test("a", () => { const r = /open;\n});\n',
        'test("a", () => { const t = `${ "x };\n',
        'test("a", () => { f(]); });\n',
        'test("a", () => { const s = \'open',
        'test("a", () => { const t = `open',
        'test("a", () => { const t = `${ 1',
        'test("a", () => { const r = /open',
    ],
)
def test_a_js_file_the_scan_cannot_read_is_its_own_finding_never_nothing(broken: str) -> None:
    found = detect({JS: BASE_JS}, {JS: broken})
    assert kinds(found) == [(JS, tc.UNREADABLE)]
    assert found[0].detail.startswith("tree: ")
    again = detect({JS: broken}, {JS: BASE_JS})
    assert kinds(again) == [(JS, tc.UNREADABLE)]
    assert again[0].detail.startswith("baseline: ")


def test_nested_templates_and_a_broken_new_file_are_handled() -> None:
    nested = 'test("n", () => { const t = `a \\` ${ `b ${ 1 }` } c`; assert.ok(t); });\n'
    assert detect({JS: nested}, {JS: nested}) == []
    changed = nested.replace("assert.ok(t)", "assert.ok(1)")
    assert kinds(detect({JS: nested}, {JS: changed})) == [("n", tc.BODY_CHANGED)]
    # a new file the scan cannot read is not a pre-existing test: nothing to compare
    assert detect({JS: nested}, {JS: nested, "tests/new.test.js": "test("}) == []
    assert detect({PY: BASE_PY}, {PY: BASE_PY, "tests/test_new.py": "def test_x(:"}) == []


def test_decorators_and_helpers_that_are_not_tests_are_not_read_as_changes() -> None:
    base = BASE_PY + "\n\n@pytest.mark.parametrize('n', [1])\ndef test_p(n):\n    assert n\n"
    base += "\n\ndef helper():\n    return 1\n"
    found = detect({PY: base}, {PY: base.replace("return 1", "return 2")})
    assert [(c.name, c.kind) for c in found] == [
        (f"{PY} (code outside the tests)", tc.SUPPORT_CHANGED)
    ]


def test_a_rename_against_huge_bodies_falls_back_to_deleted(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(tc, "_SIMILARITY_LIMIT", 10)
    found = py(
        BASE_PY.replace("test_other", "test_renamed").replace("other() == 1", "other() == 2")
    )
    assert kinds(found) == [("test_other", tc.DELETED)]
    monkeypatch.setattr(tc, "_SIMILARITY_LIMIT", 400)
    bulky = BASE_PY.replace("test_other", "test_bulky").replace(
        "other() == 1", "other() == 1" + " + 0" * 100
    )
    assert kinds(py(bulky)) == [("test_other", tc.DELETED)]
    monkeypatch.setattr(tc, "_SIMILARITY_LIMIT", 20_000)
    found = detect(
        {PY: BASE_PY},
        {PY: BASE_PY.replace("test_other", "test_renamed") + "def test_big():\n    assert 1\n" * 1},
    )
    assert kinds(found) == [("test_other", tc.RENAMED)]


# -- the label ------------------------------------------------------------------------


def test_a_flip_line_names_a_test_by_its_exact_name() -> None:
    message = "Summary\n\nflip: adds -- the old 4 was a typo: 2 + 2 is 4, the old input was 3 + 2\n"
    assert tc.parse_flips(message, {"adds"}) == {
        "adds": "the old 4 was a typo: 2 + 2 is 4, the old input was 3 + 2"
    }
    assert tc.parse_flips(message, {"adds two"}) == {}


def test_a_flip_line_may_quote_the_name_and_wrap_its_evidence() -> None:
    message = (
        'flip: "output that is not a diff is left alone": evidence line one\n'
        "continued here\n"
        "\n"
        "not evidence\n"
        "flip: second -- b\n"
    )
    found = tc.parse_flips(message, {"output that is not a diff is left alone", "second"})
    assert found == {
        "output that is not a diff is left alone": "evidence line one continued here",
        "second": "b",
    }


def test_the_longer_name_answers_a_line_that_two_names_fit() -> None:
    found = tc.parse_flips("flip: parses a path -- x\n", {"parses", "parses a path"})
    assert found == {"parses a path": "x"}
    assert tc.parse_flips("flip: parsesabc -- x\n", {"parses"}) == {}


def test_a_flip_line_may_be_an_item_of_a_markdown_list() -> None:
    message = (
        "Rewritten pre-existing tests:\n"
        "- flip: adds -- the old 4 was a typo\n"
        "* flip: second -- b\n"
        "  + flip: third -- c\n"
    )
    found = tc.parse_flips(message, {"adds", "second", "third"})
    assert found == {"adds": "the old 4 was a typo", "second": "b", "third": "c"}
    for unlabelled in ("- see flip: adds -- x\n", "-flip: adds -- x\n", "1. flip: adds -- x\n"):
        assert tc.parse_flips(unlabelled, {"adds"}) == {}


def test_a_flip_line_may_name_a_test_by_its_pytest_node_id() -> None:
    """#80a1 r2 labelled all nine of its changed tests this way, each with evidence."""
    message = (
        "flip: tests/test_feed.py::test_x -- the old row lacked `tests`\n"
        "flip: tests/t.py::TestK::test_y -- a class part too\n"
        'flip: "tests/t.py::test_z" -- quoted\n'
    )
    found = tc.parse_flips(message, {"test_x", "test_y", "test_z"})
    assert found == {
        "test_x": "the old row lacked `tests`",
        "test_y": "a class part too",
        "test_z": "quoted",
    }
    # the name after the path still has to be the test's own
    assert tc.parse_flips("flip: tests/t.py::test_other -- x\n", {"test_x"}) == {}
    assert tc.parse_flips("flip: tests/t.py::test_xy -- x\n", {"test_x"}) == {}


def test_a_flip_line_that_labels_nothing_is_named_in_the_finding() -> None:
    got = judge([CHANGE], "flip: tests/x.py::nope -- 2 + 3 is 5\nflip: elsewhere -- y\n")
    assert got is not None
    assert got.verdict == "fail"
    assert (
        "flip line(s) labelling no changed test here: 'tests/x.py::nope', 'elsewhere'" in got.detail
    )
    assert "[no flip line]" in got.detail  # the test it owes is still marked
    labelled = judge([CHANGE], "flip: adds -- 2 + 3 is 5 and the old assertion said 4")
    assert labelled is not None
    assert "labelling no changed test" not in labelled.detail
    # a node-id line that labels its test is never listed as a stray, even when the
    # finish fails for another test
    other = judge([CHANGE, OTHER], "flip: tests/x.py::adds -- 2 + 3 is 5, the old said 4")
    assert other is not None
    assert other.verdict == "fail"
    assert "labelling no changed test" not in other.detail, other.detail


@pytest.mark.parametrize("dash", [" \u2014 ", "\u2014", " \u2013 ", ": "])
def test_a_stray_flip_line_is_quoted_as_the_label_it_wrote_whatever_its_separator(
    dash: str,
) -> None:
    """#201: #80a1 r3 wrote its flip lines with an em dash. A stray one was quoted
    as its first 80 characters, a name it never gave."""
    long = (
        "tests/test_evidence.py::"
        "test_real_mutmut_runs_each_mutant_against_only_the_tests_that_ran_its_function"
    )
    got = judge([CHANGE], f"flip: {long}{dash}unpack widened from 3 to 4 fields\n")
    assert got is not None
    assert f"flip line(s) labelling no changed test here: {long!r}." in got.detail


def test_eighty_a1_r2s_labels_by_pytest_node_id_are_accepted() -> None:
    """The shape #80a1 r2's first finish used for each of its changed tests."""
    changes = [
        ChangedTest("tests/test_feed.py", "test_record_rows", tc.BODY_CHANGED, "x"),
        ChangedTest("tests/test_evidence.py", "test_seals_each_row", tc.BODY_CHANGED, "y"),
    ]
    message = (
        "Summary.\n\n"
        "flip: tests/test_feed.py::test_record_rows -- the sealed row now carries `tests`; "
        "the old literal row omitted it\n"
        "flip: tests/test_evidence.py::test_seals_each_row -- a mutant's row with no stats "
        "record now reads `tests: []`, the old literal had no key\n"
    )
    got = judge(changes, message)
    assert got is not None
    assert got.verdict == "not-proven", got.detail


def test_a_flip_line_with_no_evidence_maps_to_empty() -> None:
    assert tc.parse_flips("FLIP: adds\n", {"adds"}) == {"adds": ""}


@pytest.mark.parametrize(
    "evidence",
    [
        "",
        " -- ",
        "the new code fails the old test",
        "The old test fails with the new code.",
        "the old assertion no longer passes",
        "the existing test breaks; the new code makes it fail",
        "old test is red now",
        "the new output does not match the old assertion",
        "the old test failed, and the old expectation broke",
        "fails against the new behaviour",
    ],
)
def test_circular_or_missing_evidence_is_refused(evidence: str) -> None:
    assert tc.circular_evidence(evidence) in (tc.CIRCULAR_SAID, tc.NO_EVIDENCE_SAID)


@pytest.mark.parametrize(
    "evidence",
    [
        "the input '+1' is a valid number; the old assertion rejected it",
        "ran the CLI on a.txt: the old assertion accepted the wrong output 'Copy' twice",
        "the old test failed, but only because it pinned 3.03 where the spec says 3.02",
        "the old assertion let a rounding bug through on 2.675 (real run, tree abc123)",
    ],
)
def test_evidence_that_says_something_else_is_accepted(evidence: str) -> None:
    assert tc.circular_evidence(evidence) is None


def test_the_reason_a_line_was_refused_is_the_right_one() -> None:
    assert tc.circular_evidence("") == tc.NO_EVIDENCE_SAID
    assert tc.circular_evidence("the old test fails") == tc.CIRCULAR_SAID


# -- the verdict ----------------------------------------------------------------------

CHANGE = ChangedTest(JS, "adds", tc.BODY_CHANGED, "was `4`, now `5`")
OTHER = ChangedTest(JS, "subtracts", tc.DELETED, "no test named 'subtracts' remains")


def test_nothing_changed_has_no_verdict() -> None:
    assert judge([], "flip: adds -- x") is None


def test_a_changed_test_without_a_flip_line_fails_with_the_rule_and_the_name() -> None:
    got = judge([CHANGE], "no label here")
    assert got is not None
    assert got.verdict == "fail"
    assert "'adds'" in got.detail
    assert "[no flip line]" in got.detail
    assert "flip: <exact test name> -- <evidence>" in got.detail
    assert "known-good instance the old assertion rejected" in got.detail


def test_a_circular_flip_line_fails_and_says_why() -> None:
    got = judge([CHANGE], "flip: adds -- the new code fails the old test")
    assert got is not None
    assert got.verdict == "fail"
    assert "flip line refused" in got.detail
    assert tc.CIRCULAR_SAID in got.detail


def test_one_unlabelled_test_among_labelled_ones_fails_the_whole() -> None:
    got = judge([CHANGE, OTHER], "flip: adds -- 2 + 3 is 5 and the old assertion said 4")
    assert got is not None
    assert got.verdict == "fail"
    assert got.detail.count("[no flip line]") == 1
    assert "'subtracts'" in got.detail.split("[no flip line]")[0].splitlines()[-1]


def test_the_tests_still_owed_a_line_lead_the_refusal_before_any_long_detail() -> None:
    long = ChangedTest(JS, "adds", tc.BODY_CHANGED, "was `" + "x" * 900 + "`, now `5`")
    labelled = "flip: adds -- 2 + 3 is 5 and the old assertion said 4"
    got = judge([long, OTHER], labelled)
    assert got is not None
    entries = [line for line in got.detail.splitlines() if line.startswith("- ")]
    assert entries[0].startswith(f"- {JS}: 'subtracts' [no flip line] -- ")
    assert entries[1].startswith(f"- {JS}: 'adds' -- ")
    seen = got.detail[: feed.DETAIL_CHARS]
    assert "'subtracts' [no flip line]" in seen


def test_a_good_flip_line_is_accepted_as_not_proven_never_as_a_pass() -> None:
    message = "flip: adds -- 2 + 3 is 5 and the old assertion said 4"
    got = judge([CHANGE], message)
    assert got is not None
    assert got.verdict == "not-proven"
    assert "flipped test(s): adds" in got.detail
    assert "needs a person's review" in got.detail
    assert "2 + 3 is 5" in got.detail
