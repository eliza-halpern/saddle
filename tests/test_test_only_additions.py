"""Tests for `gates.check_test_only_additions`: code only tests reach is not production code.

The real case is a run that changed only browser JavaScript, which the
mutation gate cannot see, and so added `copy_button_wiring` to a Python
module with no production caller plus a test that called it. Each rule is
tested by a pair of instances: one the check must pass, one it must refuse.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import replace

import pytest
from test_gates import _node, _passing_inputs

from saddle.gates import (
    GateCheck,
    Tier1Inputs,
    check_dead_additions,
    check_test_only_additions,
    run_tier1,
)

APP = "src/pkg/web/app.py"
WIRING = (
    "def copy_button_wiring():\n"
    '    """Pin the copy button\'s assets."""\n'
    '    return ("markdown.js", "app.js")\n'
)
SERVE = "def serve():\n    return 1\n"
CLI = "from pkg.web.app import serve\n\n\ndef main():\n    return serve()\n"
TEST_CALLS = (
    "from pkg.web.app import copy_button_wiring\n\n\n"
    "def test_wiring():\n    assert copy_button_wiring()\n"
)


def _lines(text: str, start: int = 1) -> set[int]:
    return set(range(start, start + text.count("\n")))


def _run(
    sources: Mapping[str, str],
    added: Mapping[str, set[int]],
    *,
    baseline: Mapping[str, str] | None = None,
    pyproject: str | None = None,
    imported: bool = True,
) -> GateCheck:
    """The check over `sources`, with `CLI` (which imports `APP`'s module) in the tree unless
    `imported` is False: a module production code does not import is not judged."""
    tree = {"src/pkg/cli.py": CLI, **sources} if imported else sources
    return check_test_only_additions(tree, added, baseline=baseline, pyproject=pyproject)


def _app_with(added_text: str, *, extra: Mapping[str, str] | None = None) -> GateCheck:
    """`APP` gains `added_text` after `SERVE`; `extra` are the other modules of the tree."""
    sources = {APP: SERVE + "\n\n" + added_text, **(extra or {})}
    start = SERVE.count("\n") + 3
    return _run(sources, {APP: _lines(added_text, start)}, baseline={APP: SERVE})


def test_the_copy_button_run_is_refused_and_the_symbol_named() -> None:
    check = _app_with(
        WIRING, extra={"tests/test_copy_button.py": TEST_CALLS, "src/pkg/cli.py": CLI}
    )
    assert not check.passed
    assert check.name == "dead-code"
    assert (
        f"{APP}: copy_button_wiring (referenced only by tests/test_copy_button.py)" in check.detail
    )
    assert "not production code" in check.detail
    assert "wire each into production code" in check.detail


def test_the_same_function_called_from_a_production_module_passes() -> None:
    caller = (
        "from pkg.web.app import copy_button_wiring\n\n\n"
        "def boot():\n    return copy_button_wiring()\n"
    )
    check = _app_with(
        WIRING, extra={"tests/test_copy_button.py": TEST_CALLS, "src/pkg/boot.py": caller}
    )
    assert check.passed, check.detail
    assert (
        check.detail == "every function, class and constant added is reached from production code"
    )


def test_a_function_nothing_references_is_refused() -> None:
    check = _app_with(WIRING)
    assert not check.passed
    assert f"{APP}: copy_button_wiring (referenced by nothing)" in check.detail


def test_a_function_reached_only_by_its_own_recursion_is_refused() -> None:
    check = _app_with("def walk(n):\n    return walk(n - 1) if n else 0\n")
    assert not check.passed
    assert f"{APP}: walk (referenced by nothing)" in check.detail


def test_a_call_from_elsewhere_in_the_same_module_is_production_use() -> None:
    check = _app_with(
        "def helper():\n    return 1\n\n\ndef run():\n    return helper()\n\n\nrun()\n"
    )
    assert check.passed, check.detail


def test_a_string_or_a_docstring_that_spells_the_name_is_not_a_caller() -> None:
    spelled = (
        "def wiring():\n"
        '    """Not called by `wiring_caller` or anyone."""\n'
        "    return 1\n\n\n"
        'NAMES = ["wiring"]\n'
    )
    check = _app_with(spelled, extra={"src/pkg/other.py": 'doc = "wiring"\n"""wiring"""\n'})
    assert not check.passed
    assert f"{APP}: wiring (referenced by nothing)" in check.detail


def test_a_decorator_that_registers_the_function_passes_and_a_transparent_one_does_not() -> None:
    registered = "@registry.register('copy')\ndef copy_button_wiring():\n    return 1\n"
    assert _app_with(registered, extra={"src/pkg/reg.py": "registry = {}\n"}).passed
    wrapped = "@functools.cache\ndef copy_button_wiring():\n    return 1\n"
    check = _app_with(wrapped, extra={"tests/test_copy_button.py": TEST_CALLS})
    assert not check.passed
    assert "copy_button_wiring" in check.detail
    bare = "@cache\ndef copy_button_wiring():\n    return 1\n"
    assert not _app_with(bare).passed
    called = "@registry\ndef copy_button_wiring():\n    return 1\n"
    assert _app_with(called).passed
    odd = "@handlers[0]\ndef copy_button_wiring():\n    return 1\n"
    assert _app_with(odd).passed


def test_an_entry_point_in_pyproject_passes() -> None:
    pyproject = '[project.scripts]\npkg-web = "pkg.web.app:copy_button_wiring"\n'
    other = '[project.scripts]\npkg-web = "pkg.web.app:serve"\n'
    package = (
        '[project.entry-points."pkg.plugins"]\nweb = "pkg.web.app:copy_button_wiring [extra]"\n'
    )
    gui = '[project.gui-scripts]\nweb = "pkg.web.app:copy_button_wiring"\n'
    sources = {APP: WIRING}
    added = {APP: _lines(WIRING)}
    assert _run(sources, added, pyproject=pyproject).passed
    assert _run(sources, added, pyproject=package).passed
    assert _run(sources, added, pyproject=gui).passed
    # Known-bad: an entry point for a different function of the module reaches nothing here.
    assert not _run(sources, added, pyproject=other).passed
    # ... and so does one in a module that is not this one, or a bare module with no function.
    elsewhere = '[project.scripts]\nweb = "pkg.cli:copy_button_wiring"\nrun = "pkg.web.app"\n'
    assert not _run(sources, added, pyproject=elsewhere).passed
    assert _run(sources, added, pyproject="[tool.x]\ny = 1\n").detail.startswith(f"{APP}: ")


def test_an_entry_point_names_a_module_with_or_without_the_src_prefix() -> None:
    plain = '[project.scripts]\nweb = "app:copy_button_wiring"\n'
    assert _run({"app.py": WIRING}, {"app.py": _lines(WIRING)}, pyproject=plain).passed
    init = '[project.scripts]\nweb = "pkg:copy_button_wiring"\n'
    assert _run(
        {"src/pkg/__init__.py": WIRING}, {"src/pkg/__init__.py": _lines(WIRING)}, pyproject=init
    ).passed


def test_an_all_entry_exports_the_name() -> None:
    exported = WIRING + '\n\n__all__ = ["copy_button_wiring"]\n'
    assert _app_with(exported).passed
    extended = WIRING + '\n\n__all__ += ("copy_button_wiring",)\n'
    assert _app_with(extended).passed
    annotated = WIRING + '\n\n__all__: list[str] = ["copy_button_wiring"]\n'
    assert _app_with(annotated).passed
    other = WIRING + '\n\n__all__ = ["serve"]\n'
    assert not _app_with(other).passed


def test_a_handler_passed_as_a_value_and_an_import_are_references() -> None:
    value = {"src/pkg/routes.py": "ROUTES = {'/copy': copy_button_wiring}\n"}
    assert _app_with(WIRING, extra=value).passed
    attribute = {
        "src/pkg/routes.py": "import pkg.web.app as app\n\nROUTES = [app.copy_button_wiring]\n"
    }
    assert _app_with(WIRING, extra=attribute).passed
    imported = {"src/pkg/__init__.py": "from pkg.web.app import copy_button_wiring\n"}
    assert _app_with(WIRING, extra=imported).passed
    # A store is not a read: assigning to the name elsewhere reaches nothing.
    stored = {"src/pkg/routes.py": "copy_button_wiring = None\nobj.copy_button_wiring = 1\n"}
    assert not _app_with(WIRING, extra=stored).passed


def test_classes_and_constants_are_judged_public_or_private() -> None:
    cls = _app_with(
        "class Wiring:\n    def go(self):\n        return Wiring()\n",
        extra={
            "tests/test_w.py": (
                "from pkg.web.app import Wiring\n\n\ndef test_w():\n    Wiring().go()\n"
            )
        },
    )
    assert not cls.passed
    assert f"{APP}: Wiring (referenced only by tests/test_w.py)" in cls.detail
    assert not _app_with("ASSET_NAMES = ('app.js',)\n").passed
    assert not _app_with("ASSET_COUNT: int = 2\n").passed
    assert not _app_with("A = B = 1\n").passed
    assert not _app_with("_private = 1\n").passed
    assert not _app_with("async def _fetch():\n    return 1\n").passed
    assert _app_with("ANNOTATION_ONLY: int\n").passed
    assert _app_with("__version__ = '1'\nobj.attr = 1\nfor i in (1,):\n    pass\n").passed


def test_a_name_used_only_by_a_dead_definition_is_dead_with_it() -> None:
    chain = (
        "BODY = 'x'\n\n\ndef helper():\n    return BODY\n\n\n"
        "def copy_button_wiring():\n    return helper()\n"
    )
    check = _app_with(chain, extra={"tests/test_copy_button.py": TEST_CALLS})
    assert not check.passed
    assert (
        f"{APP}: copy_button_wiring (referenced only by tests/test_copy_button.py)" in check.detail
    )
    assert "and what only these use: BODY, helper" in check.detail
    assert check.basis == "test-only-definitions=3 unreadable=0"
    # ... but the same chain with a production caller at the top is all live.
    caller = {
        "src/pkg/boot.py": "from pkg.web.app import copy_button_wiring\n\ncopy_button_wiring()\n"
    }
    assert _app_with(chain, extra=caller).passed


def test_a_cycle_of_dead_definitions_is_all_listed() -> None:
    cycle = "def ping():\n    return pong()\n\n\ndef pong():\n    return ping()\n"
    check = _app_with(cycle)
    assert not check.passed
    assert (
        f"{APP}: ping (used only by other definitions no production code reaches)" in check.detail
    )
    assert (
        f"{APP}: pong (used only by other definitions no production code reaches)" in check.detail
    )
    assert "and what only these use" not in check.detail


def test_dependents_beyond_eight_are_counted() -> None:
    names = [f"C{i}" for i in range(11)]
    text = (
        "".join(f"{n} = 1\n" for n in names) + "def top():\n    return (" + ", ".join(names) + ")\n"
    )
    check = _app_with(text)
    assert not check.passed
    assert "and what only these use: C0, C1, C2, C3, C4, C5, C6, C7 (+3 more)" in check.detail


def test_only_what_the_diff_adds_and_the_baseline_lacked_is_judged() -> None:
    old = "def untouched():\n    return 1\n"
    # Not an addition: no changed line, or the baseline module had the name already.
    assert _run({APP: old}, {APP: set()}, baseline={APP: old}).passed
    assert _run({APP: old}, {APP: _lines(old)}, baseline={APP: old}).passed
    # An edit that leaves a statement line of the definition untouched is not an addition.
    two = "def f():\n    x = 1\n    return x\n"
    assert _run({APP: two}, {APP: {3}}, baseline={APP: two}).passed
    # With no baseline copy at all (a new module) the same lines are an addition.
    assert not _run({APP: old}, {APP: _lines(old)}).passed
    # A baseline that does not parse says nothing about names; the definition is judged.
    assert not _run({APP: old}, {APP: _lines(old)}, baseline={APP: "def (:\n"}).passed


def test_test_code_is_never_a_candidate_and_never_a_caller() -> None:
    tests = {
        "tests/helpers.py": "def make():\n    return 1\n",
        "tests/conftest.py": "SEED = 1\n",
        "conftest.py": "SEED = 1\n",
        "pkg/test_x.py": "def test_x():\n    pass\n",
        "pkg/x_test.py": "def test_y():\n    pass\n",
    }
    added = {path: _lines(text) for path, text in tests.items()}
    assert _run(tests, added).passed
    assert _run(tests, added).detail == "no function, class or constant added to a non-test module"
    # A helper under `tests/` calling a production definition is no production caller.
    helper = {
        "tests/helpers.py": "from pkg.web.app import copy_button_wiring\ncopy_button_wiring()\n"
    }
    check = _app_with(WIRING, extra=helper)
    assert not check.passed
    assert "referenced only by tests/helpers.py" in check.detail
    stray = {"conftest.py": "from pkg.web.app import copy_button_wiring\n"}
    assert not _app_with(WIRING, extra=stray).passed
    # A directory merely named like a test module is not test code.
    assert _app_with(WIRING, extra={"tools/tests_data.py": "copy_button_wiring()\n"}).passed
    # A path missing from the sources is skipped, not an error.
    assert _run({}, {APP: {1}}).passed


def test_a_test_directory_of_any_spelling_is_test_code_in_both_directions() -> None:
    """`gates.is_test_code` also counts a `test/` or `__tests__/` directory at any depth: the
    old private predicate knew only `tests/`. What changed: such a module is no longer a
    candidate, and a read from it no longer makes a definition production-used."""
    for directory in ("test", "__tests__", "tests"):
        helper = f"src/pkg/{directory}/helper.py"
        # As a candidate: a function added there is test code, never judged.
        assert _run({helper: WIRING}, {helper: _lines(WIRING)}).detail == (
            "no function, class or constant added to a non-test module"
        )
        # As a reader: a call from there is no production caller.
        reader = {helper: "from pkg.web.app import copy_button_wiring\ncopy_button_wiring()\n"}
        check = _app_with(WIRING, extra=reader)
        assert not check.passed
        assert f"referenced only by {helper}" in check.detail
    # A directory that merely contains the word is production code.
    for directory in ("testing", "latest", "contest"):
        helper = f"src/pkg/{directory}/helper.py"
        uses = {"src/pkg/boot.py": f"from pkg.{directory} import helper\n"}
        refused = _run({helper: WIRING, **uses}, {helper: _lines(WIRING)})
        assert not refused.passed
        assert f"{helper}: copy_button_wiring" in refused.detail


def test_a_module_that_does_not_parse_is_reported_never_read_as_no_references() -> None:
    # An added module that does not parse.
    broken = _run({APP: "def (:\n"}, {APP: {1}})
    assert not broken.passed
    assert f"cannot tell whether code reaches it: {APP} does not parse" in broken.detail
    assert broken.basis == "test-only-definitions=0 unreadable=1"
    # A production module that does not parse and spells the name could be the caller.
    blind = _app_with(WIRING, extra={"src/pkg/boot.py": "def boot(:\n    copy_button_wiring()\n"})
    assert not blind.passed
    assert "src/pkg/boot.py does not parse and spells copy_button_wiring" in blind.detail
    assert "(referenced" not in blind.detail
    # One that does not parse and never spells it can say nothing about it.
    quiet = _app_with(WIRING, extra={"src/pkg/boot.py": "def boot(:\n    other()\n"})
    assert f"{APP}: copy_button_wiring (referenced by nothing)" in quiet.detail
    # A test that does not parse is not a production caller either way.
    assert not _app_with(
        WIRING, extra={"tests/test_x.py": "def test(:\n    copy_button_wiring()\n"}
    ).passed
    # The same finding is said once however many names it blocks.
    twice = _app_with(
        "def one():\n    return 1\n\n\ndef two():\n    return 2\n",
        extra={"src/pkg/boot.py": "def boot(:\n    one(); two()\n"},
    )
    assert twice.detail.count("src/pkg/boot.py does not parse and spells") == 2


def test_a_pyproject_that_does_not_parse_is_reported_while_a_name_is_unreached() -> None:
    check = _run({APP: WIRING}, {APP: _lines(WIRING)}, pyproject="[project\n")
    assert not check.passed
    assert "pyproject.toml does not parse" in check.detail
    assert "so no entry point of copy_button_wiring is known" in check.detail
    # Reached by a production caller, no entry point is needed and a bad file is not charged.
    caller = {
        "src/pkg/boot.py": "from pkg.web.app import copy_button_wiring\ncopy_button_wiring()\n"
    }
    sources = {APP: WIRING, **caller}
    assert _run(sources, {APP: _lines(WIRING)}, pyproject="[project\n").passed


def test_a_module_nothing_imports_is_not_judged_and_the_same_function_in_an_imported_one_is() -> (
    None
):
    """A function added to a standalone library module whose callers are its tests is the
    change itself; the identical shape in a module the app imports is the copy-button defect."""
    library = {"calc.py": "def add(a, b):\n    return a + b\n"}
    calls = {
        "test_calc.py": "from calc import add\n\n\ndef test_add():\n    assert add(1, 2) == 3\n"
    }
    quiet = _run({**library, **calls}, {"calc.py": {1, 2}}, imported=False)
    assert quiet.passed
    assert quiet.detail == (
        "no function, class or constant added to a module production code imports"
        "; not judged, nothing imports calc.py"
    )
    assert quiet.basis == "modules=1 unreached-modules=1"
    imported = {"src/pkg/cli.py": "import calc\n\n\ndef main():\n    return 1\n"}
    refused = _run({**library, **calls, **imported}, {"calc.py": {1, 2}}, imported=False)
    assert not refused.passed
    assert "calc.py: add (referenced only by test_calc.py)" in refused.detail


def test_a_module_is_reached_by_an_import_an_entry_point_a_script_guard_or_an_unreadable_file() -> (
    None
):
    mod = {"src/pkg/tool.py": "def helper():\n    return 1\n"}
    added = {"src/pkg/tool.py": {1, 2}}

    def judged(extra: Mapping[str, str], **kw: object) -> bool:
        check = _run({**mod, **extra}, added, imported=False, **kw)  # type: ignore[arg-type]
        return "not judged" not in check.detail

    assert not judged({})
    assert judged({"src/pkg/a.py": "from pkg import tool\n"})
    assert judged({"src/pkg/a.py": "from pkg.tool import helper\n"})
    assert judged({"src/pkg/a.py": "import pkg.tool\n"})
    assert judged({"src/pkg/a.py": "from . import tool\n"})
    assert judged({"src/pkg/a.py": "from .tool import helper\n"})
    assert not judged({"src/pkg/a.py": "import pkg.other\n"})
    # A test importing it is not production, and neither is the module importing itself.
    assert not judged({"tests/test_tool.py": "from pkg import tool\n"})
    assert not judged({"src/pkg/tool.py": "def helper():\n    return 1\n\n\nimport pkg.tool\n"})
    assert judged({}, pyproject='[project.scripts]\nt = "pkg.tool:other"\n')
    assert judged({"src/pkg/b.py": "def broken(:\n    tool\n"})
    guarded = {
        "src/pkg/tool.py": 'def helper():\n    return 1\n\n\nif __name__ == "__main__":\n    pass\n'
    }
    assert "not judged" not in _run(guarded, {"src/pkg/tool.py": {1, 2}}, imported=False).detail
    main = _run(
        {"src/pkg/__main__.py": "def helper():\n    return 1\n"},
        {"src/pkg/__main__.py": {1, 2}},
        imported=False,
    )
    assert "not judged" not in main.detail
    package = _run(
        {"src/pkg/__init__.py": "def helper():\n    return 1\n", "src/x.py": "import pkg\n"},
        {"src/pkg/__init__.py": {1, 2}},
        imported=False,
    )
    assert not package.passed


# -- the gate: `run_tier1` joins this to `dead-code` only when the audit asks -----------------


def _dead_code(inputs: Tier1Inputs) -> GateCheck:
    return next(c for c in run_tier1(_node(), inputs).checks if c.name == "dead-code")


def _wired(**changes: object) -> Tier1Inputs:
    sources = {APP: WIRING, "tests/test_copy_button.py": TEST_CALLS, "src/pkg/cli.py": CLI}
    return replace(
        _passing_inputs(),
        sources=sources,
        added_lines={APP: tuple(sorted(_lines(WIRING)))},
        baseline_sources={},
        **changes,  # type: ignore[arg-type]
    )


def test_run_tier1_asks_the_test_only_question_only_when_told_to() -> None:
    assert _dead_code(_wired()).passed
    asked = _dead_code(_wired(test_only_additions=True))
    assert not asked.passed
    assert "copy_button_wiring" in asked.detail


def test_run_tier1_reads_the_trees_entry_points() -> None:
    pyproject = '[project.scripts]\nweb = "pkg.web.app:copy_button_wiring"\n'
    assert _dead_code(_wired(test_only_additions=True, pyproject_text=pyproject)).passed


def test_run_tier1_leaves_the_private_definition_verdict_alone_when_the_new_check_passes() -> None:
    inputs = replace(
        _wired(test_only_additions=True),
        sources={APP: SERVE, "src/pkg/cli.py": CLI},
        added_lines={APP: tuple(sorted(_lines(SERVE)))},
    )
    check = _dead_code(inputs)
    assert check.passed
    assert check.detail == "every private definition added is mentioned elsewhere in the tree"


def test_run_tier1_keeps_a_failure_of_the_private_definition_check_when_the_new_one_passes() -> (
    None
):
    # A registry decorator is production use to the new check; the older one still finds a
    # private name nothing mentions, and its verdict stands.
    lone = "@register\ndef _lone():\n    return 1\n"
    inputs = replace(
        _wired(test_only_additions=True),
        sources={APP: lone},
        added_lines={APP: tuple(sorted(_lines(lone)))},
        dead_code_runner=lambda _edited: 0,
    )
    check = _dead_code(inputs)
    assert not check.passed
    assert "implement no requirement" in check.detail
    assert "; also " not in check.detail


def test_run_tier1_says_both_when_both_checks_refuse() -> None:
    both = "def _unused():\n    return 1\n"
    inputs = replace(
        _wired(test_only_additions=True),
        sources={APP: both, "src/pkg/cli.py": CLI},
        added_lines={APP: tuple(sorted(_lines(both)))},
        dead_code_runner=lambda _edited: 0,
    )
    check = _dead_code(inputs)
    assert not check.passed
    assert f"{APP}: _unused (referenced by nothing)" in check.detail
    assert "; also " in check.detail
    assert "implement no requirement" in check.detail
    assert check.basis == "test-only-definitions=1 unreadable=0 dead-definitions=1"


def test_the_older_check_alone_is_not_changed_by_the_new_one() -> None:
    check = check_dead_additions(
        {APP: WIRING}, {APP: _lines(WIRING)}, suite_passed=True, run_without=lambda _e: 0
    )
    assert check.passed


@pytest.mark.parametrize("kind", ["test", "impl"])
def test_a_spec_node_is_not_asked(kind: str) -> None:
    node = _node(kind=kind)
    inputs = _wired(test_only_additions=True)
    checks = {c.name: c for c in run_tier1(node, inputs).checks}
    assert checks["dead-code"].passed == (kind == "test")
