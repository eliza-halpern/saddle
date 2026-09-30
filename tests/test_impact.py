"""Test impact: a later audit of a run runs only the test files its change can reach.

Contract: under an `ImpactMemo` the first audit runs the whole suite with
per-test contexts and `impact.build` records which test files ran each
function's body; every later audit runs `impact.select`'s test files, which
are the tests that ran a changed function, the tests of whatever names a
changed binding (followed across modules), and every changed test file, or
the whole suite whenever `select` cannot say (None).

Why (loosened, scope narrowed: the finish no longer runs every test): a
watched run spent most of each checkpoint and of its finish running a
3,300-test suite for edits that reached a few files.

Known-good: a changed function's own tests are selected, and a break there
still fails the tests check. Known-bad: a constant read in another module
selects that module's tests too (a file-level map misses it), and a
conftest, config, deleted file, broad fixture or code run at import time
takes the whole suite. The cost, on the record: a test that reaches a
changed constant only through a name built at run time is not selected.
"""

from __future__ import annotations

from pathlib import Path
from typing import Final

import coverage
import pytest

from saddle import auditor as auditor_module
from saddle import evidence
from saddle.auditor import Auditor, AuditorConfig, Finding, Findings
from saddle.impact import FileImpact, ImpactMap, ImpactMemo, build, select

CALC: Final = '''"""Arithmetic."""

LIMIT = 10


def add(a, b):
    return a + b


def capped(a):
    return min(a, LIMIT)


class Box:
    size = 3

    def area(self):
        return self.size * self.size
'''

ADD_TEST: Final = "from pkg.calc import add\n\n\ndef test_add():\n    assert add(1, 2) == 3\n"

FILES: Final = {
    "pyproject.toml": '[project]\nname = "pkg"\n',
    "src/pkg/__init__.py": "",
    "src/pkg/calc.py": CALC,
    "src/pkg/use.py": "from pkg.calc import LIMIT\n\n\ndef double_limit():\n    return LIMIT * 2\n",
    "src/pkg/load.py": 'def load():\n    return open("table.json").read()\n',
    "table.json": "{}\n",
    "docs/GUIDE.md": "# Guide\n",
    "docs/OTHER.md": "# Other\n",
    "tests/conftest.py": "",
    "tests/test_add.py": ADD_TEST,
    "tests/test_capped.py": (
        "from pkg.calc import capped\n\n\ndef test_capped():\n    assert capped(99) == 10\n"
    ),
    "tests/test_use.py": (
        "from pkg.use import double_limit\n\n\ndef test_use():\n    assert double_limit() == 20\n"
    ),
    "tests/test_box.py": (
        "from pkg.calc import Box\n\n\ndef test_box():\n    assert Box().area() == 9\n"
    ),
    "tests/test_load.py": "from pkg.load import load\n\n\ndef test_load():\n    assert load()\n",
    "tests/test_docs.py": (
        "from pathlib import Path\n\n\ndef test_guide():\n"
        '    assert Path("docs/GUIDE.md").read_text()\n'
    ),
    "tests/test_dyn.py": (
        "import importlib\n\n\ndef test_limit():\n"
        '    assert getattr(importlib.import_module("pkg.calc"), "LIMIT") == 10\n'
    ),
}

KNOWN: Final[ImpactMap] = {
    "src/pkg/calc.py": FileImpact(
        tests=frozenset({"tests/test_add.py", "tests/test_capped.py", "tests/test_box.py"}),
        functions={
            "add": frozenset({"tests/test_add.py"}),
            "capped": frozenset({"tests/test_capped.py"}),
            "Box.area": frozenset({"tests/test_box.py"}),
        },
        outside=frozenset(),
    ),
    "src/pkg/use.py": FileImpact(
        tests=frozenset({"tests/test_use.py"}),
        functions={"double_limit": frozenset({"tests/test_use.py"})},
        outside=frozenset(),
    ),
    "src/pkg/load.py": FileImpact(
        tests=frozenset({"tests/test_load.py"}),
        functions={"load": frozenset({"tests/test_load.py"})},
        outside=frozenset(),
    ),
}


def _git(root: Path, *argv: str) -> None:
    assert evidence.run_argv(["git", *argv], root) == 0


@pytest.fixture
def tree(tmp_path: Path) -> Path:
    _git(tmp_path, "init", "-q")
    _git(tmp_path, "config", "user.email", "test@example.com")
    _git(tmp_path, "config", "user.name", "test")
    for name, text in FILES.items():
        (tmp_path / name).parent.mkdir(parents=True, exist_ok=True)
        (tmp_path / name).write_text(text)
    _git(tmp_path, "add", "-A")
    _git(tmp_path, "commit", "-q", "-m", "baseline")
    return tmp_path


def _edit(tree: Path, name: str, old: str, new: str) -> None:
    text = (tree / name).read_text()
    assert old in text
    (tree / name).write_text(text.replace(old, new, 1))
    _git(tree, "add", "-A")


# -- select ---------------------------------------------------------------------


def test_a_changed_function_selects_the_tests_that_ran_it(tree: Path) -> None:
    _edit(tree, "src/pkg/calc.py", "return a + b", "return b + a")
    assert select(tree, "HEAD", KNOWN) == ("tests/test_add.py",)


def test_a_changed_constant_selects_the_tests_of_every_function_that_reads_it(
    tree: Path,
) -> None:
    _edit(tree, "src/pkg/calc.py", "LIMIT = 10", "LIMIT = 11")
    selected = select(tree, "HEAD", KNOWN)
    # use.py reads LIMIT through an import and runs none of calc.py's lines.
    assert selected == ("tests/test_capped.py", "tests/test_use.py")
    # The cost, on the record: a name built at run time is not followed.
    assert "tests/test_dyn.py" not in selected


def test_a_changed_class_attribute_selects_its_methods_and_its_users(tree: Path) -> None:
    _edit(tree, "src/pkg/calc.py", "size = 3", "size = 4")
    assert select(tree, "HEAD", KNOWN) == ("tests/test_box.py",)


def test_a_new_function_reaches_the_tests_of_its_changed_callers(tree: Path) -> None:
    _edit(
        tree,
        "src/pkg/calc.py",
        "def add(a, b):\n    return a + b\n",
        "def _sum(a, b):\n    return a + b\n\n\ndef add(a, b):\n    return _sum(a, b)\n",
    )
    assert select(tree, "HEAD", KNOWN) == ("tests/test_add.py",)


def test_a_changed_test_file_is_selected(tree: Path) -> None:
    _edit(tree, "tests/test_add.py", "== 3", "== 1 + 2")
    assert select(tree, "HEAD", KNOWN) == ("tests/test_add.py",)


def test_a_comment_only_edit_runs_every_test_of_its_file(tree: Path) -> None:
    _edit(tree, "src/pkg/calc.py", "LIMIT = 10", "LIMIT = 10  # the cap")
    assert select(tree, "HEAD", KNOWN) == (
        "tests/test_add.py",
        "tests/test_box.py",
        "tests/test_capped.py",
    )


def test_a_file_that_is_not_python_selects_what_names_it(tree: Path) -> None:
    _edit(tree, "docs/GUIDE.md", "# Guide", "# The guide")
    assert select(tree, "HEAD", KNOWN) == ("tests/test_docs.py",)
    _git(tree, "reset", "-q", "--hard")
    _edit(tree, "table.json", "{}", '{"a": 1}')
    assert select(tree, "HEAD", KNOWN) == ("tests/test_load.py",)


@pytest.mark.parametrize(
    ("name", "old", "new"),
    [
        ("tests/conftest.py", "", "import os\n"),
        ("pyproject.toml", 'name = "pkg"', 'name = "pkg2"'),
        ("docs/OTHER.md", "# Other", "# Nothing names this"),
        ("src/pkg/calc.py", "return a + b", "return a +"),
        (
            "tests/test_add.py",
            "\n\ndef test_add",
            '\n\n@pytest.fixture(scope="session")\ndef s():\n    return 1\n\n\ndef test_add',
        ),
    ],
    ids=["conftest", "config", "unnamed-doc", "unparseable", "session-fixture"],
)
def test_what_select_cannot_see_takes_the_whole_suite(
    tree: Path, name: str, old: str, new: str
) -> None:
    (tree / name).write_text((tree / name).read_text().replace(old, new, 1) if old else new)
    _git(tree, "add", "-A")
    assert select(tree, "HEAD", KNOWN) is None


def test_a_deleted_module_takes_the_whole_suite(tree: Path) -> None:
    _git(tree, "rm", "-q", "src/pkg/load.py")
    assert select(tree, "HEAD", KNOWN) is None


def test_a_function_that_also_ran_outside_every_test_takes_the_whole_suite(tree: Path) -> None:
    _edit(tree, "src/pkg/calc.py", "return a + b", "return b + a")
    ran_at_import = dict(KNOWN)
    ran_at_import["src/pkg/calc.py"] = FileImpact(
        KNOWN["src/pkg/calc.py"].tests,
        KNOWN["src/pkg/calc.py"].functions,
        frozenset({"add"}),
    )
    assert select(tree, "HEAD", KNOWN) == ("tests/test_add.py",)
    assert select(tree, "HEAD", ran_at_import) is None


# -- build ----------------------------------------------------------------------


def _data(tmp_path: Path, runs: dict[str, dict[str, list[int]]]) -> str:
    data_file = str(tmp_path / ".coverage.ctx")
    data = coverage.CoverageData(basename=data_file)
    for context, lines in runs.items():
        data.set_context(context)
        data.add_lines(lines)
    data.write()
    return data_file


def test_build_charges_a_body_to_the_tests_that_ran_it_and_a_def_line_to_none(
    tmp_path: Path,
) -> None:
    source = tmp_path / "m.py"
    source.write_text("def f():\n    return 1\n\n\ndef g():\n    return 2\n")
    data_file = _data(
        tmp_path,
        {
            "": {str(source): [1, 5, 6]},  # def lines at import; g's body too
            "tests/test_m.py::test_f|run": {str(source): [2]},
        },
    )
    drawn = build(data_file, tmp_path)
    assert drawn is not None
    assert drawn["m.py"].functions == {"f": frozenset({"tests/test_m.py"})}
    assert drawn["m.py"].tests == frozenset({"tests/test_m.py"})
    assert drawn["m.py"].outside == frozenset({"g"})


def test_a_run_with_no_test_context_draws_no_map(tmp_path: Path) -> None:
    source = tmp_path / "m.py"
    source.write_text("def f():\n    return 1\n")
    assert build(_data(tmp_path, {"": {str(source): [1, 2]}}), tmp_path) is None
    assert build(str(tmp_path / "missing"), tmp_path) is None


# -- the audit: the first run draws the map, later ones run what the change reaches ----

PROJECT: Final = {
    "pyproject.toml": '[project]\nname = "pkg"\n\n[tool.saddle]\ntest-workers = 2\n',
    "src/pkg/__init__.py": "",
    "src/pkg/calc.py": "def add(a, b):\n    return a + b\n\n\ndef neg(a):\n    return -a\n",
    "tests/test_add.py": ADD_TEST,
    "tests/test_neg.py": "from pkg.calc import neg\n\n\ndef test_neg():\n    assert neg(2) == -2\n",
}


def _tests(result: Findings) -> Finding:
    found: Finding = next(f for f in result.findings if f.gate == "tests")
    return found


def test_the_second_audit_runs_only_what_the_change_reaches(tmp_path: Path) -> None:
    _git(tmp_path, "init", "-q")
    _git(tmp_path, "config", "user.email", "test@example.com")
    _git(tmp_path, "config", "user.name", "test")
    for name, text in PROJECT.items():
        (tmp_path / name).parent.mkdir(parents=True, exist_ok=True)
        (tmp_path / name).write_text(text)
    _git(tmp_path, "add", "-A")
    _git(tmp_path, "commit", "-q", "-m", "baseline")
    memo = ImpactMemo()
    auditor = Auditor(
        tmp_path, "HEAD", AuditorConfig(test_command="python -m pytest -q", impact=memo)
    )
    calc = tmp_path / "src/pkg/calc.py"
    calc.write_text(calc.read_text().replace("return a + b", "return b + a"))
    first = _tests(auditor.tier1())
    assert first.verdict == "pass", first.detail
    assert "impact:" not in first.detail
    assert memo.tests is not None
    assert memo.tests["src/pkg/calc.py"].functions["add"] == frozenset({"tests/test_add.py"})
    # A break in a function the change reaches still fails the tests check.
    calc.write_text(calc.read_text().replace("return b + a", "return b - a"))
    second = _tests(auditor.tier1())
    assert second.verdict == "fail"
    assert "impact: 1 of 2 test files ran" in second.detail


def _project(root: Path) -> Path:
    root.mkdir(parents=True)
    _git(root, "init", "-q")
    _git(root, "config", "user.email", "test@example.com")
    _git(root, "config", "user.name", "test")
    for name, text in PROJECT.items():
        (root / name).parent.mkdir(parents=True, exist_ok=True)
        (root / name).write_text(text)
    _git(root, "add", "-A")
    _git(root, "commit", "-q", "-m", "baseline")
    return root


def _auditor(root: Path, memo: ImpactMemo) -> Auditor:
    return Auditor(root, "HEAD", AuditorConfig(test_command="python -m pytest -q", impact=memo))


def test_a_map_drawn_at_the_start_makes_the_first_audit_selective(tmp_path: Path) -> None:
    """Red before: the first audit of a run drew the map, running the whole
    suite itself; a watched run's finish audit paid seven minutes for it."""
    root = _project(tmp_path / "p")
    memo = ImpactMemo()
    auditor = _auditor(root, memo)
    assert auditor.draw_map() == "map drawn over 4 files"
    calc = root / "src/pkg/calc.py"
    calc.write_text(calc.read_text().replace("return a + b", "return b + a"))
    first = _tests(auditor.tier1())
    assert first.verdict == "pass", first.detail
    assert "impact: 1 of 2 test files ran" in first.detail


def test_a_drawn_map_is_read_back_from_the_cache_for_the_same_tree(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = _project(tmp_path / "p")
    cache = tmp_path / "cache"
    drawn = ImpactMemo(cache=cache)
    assert _auditor(root, drawn).draw_map().startswith("map drawn")
    (entry,) = cache.iterdir()

    def no_suite(*_args: object, **_kwargs: object) -> None:
        msg = "a cached map ran the suite"
        raise AssertionError(msg)

    monkeypatch.setattr(auditor_module, "run_suite_capture", no_suite)
    again = ImpactMemo(cache=cache)
    assert _auditor(root, again).draw_map() == f"map read from {entry.name}"
    assert again.tests == drawn.tests


@pytest.mark.parametrize("change", ["tree", "corrupt"])
def test_a_cached_map_of_another_tree_or_unreadable_is_drawn_again(
    tmp_path: Path, change: str
) -> None:
    root = _project(tmp_path / "p")
    cache = tmp_path / "cache"
    assert _auditor(root, ImpactMemo(cache=cache)).draw_map().startswith("map drawn")
    (entry,) = cache.iterdir()
    if change == "tree":
        (root / "tests/test_neg.py").write_text(PROJECT["tests/test_neg.py"] + "\n# edited\n")
    else:
        entry.write_text("{not json")
    assert _auditor(root, ImpactMemo(cache=cache)).draw_map().startswith("map drawn")
