"""The mutation scratch is narrowed to the changed statements a test ran.

Contract: when `mutation_sample` is handed `only_covered`, the covering tests
(`select_tests`) and the lines some test ran (`covered`), it edits its scratch
copy so mutmut makes mutants only of the changed statements whose first line a
test ran, and mutmut runs no covered-lines pass of its own (on a large test
selection that pass alone outlasted the whole mutation budget). When that edit
cannot be made safely, or mutmut fails on the edited copy, mutmut's own pass
runs as before, on the tree as it was.
"""

from __future__ import annotations

import os
import re
import sys
import tokenize
from collections.abc import Sequence
from pathlib import Path
from typing import Any

import pytest
from test_evidence import _show_diff, _stub_mutmut, _without_stubbed_mutmut

import saddle.evidence as evidence_module
from saddle.evidence import (
    CapturedRun,
    MutationOutcome,
    _excluded_rows,
    _logical_lines,
    _mutate_only,
    _narrow,
    _narrowed_scratch,
    _parse_mutant_verdicts,
    covered_lines,
    mutation_sample,
    mutation_text,
    run_argv,
)

FUNCTIONS = (
    "def f(a):\n"  # 1
    "    x = a + 1\n"  # 2
    "    y = g(\n"  # 3
    "        a,\n"  # 4
    "        x,\n"  # 5
    "    )\n"  # 6
    "    return x + y\n"  # 7
    "\n"  # 8
    "\n"  # 9
    "def g(*args):\n"  # 10
    "    return sum(args)\n"  # 11
)
NOTHING: tuple[str, frozenset[str], frozenset[str]] = ("", frozenset(), frozenset())


def test_mutate_only_leaves_a_kept_statement_alone_and_marks_every_other_one() -> None:
    """Known-good instance. Line 2 is kept: it is untouched, every other logical
    line of `f` ends with the pragma (the multi-line call on its last row), and
    `g`, which holds no kept line, is skipped whole by a block pragma. The
    continuation rows of the unkept call are named for the regex, and the kept
    row's text is named so it can be kept out of it."""
    assert _mutate_only(FUNCTIONS, {2}) == (
        "def f(a):  # pragma: no mutate\n"
        "    x = a + 1\n"
        "    y = g(\n"
        "        a,\n"
        "        x,\n"
        "    )  # pragma: no mutate\n"
        "    return x + y  # pragma: no mutate\n"
        "\n"
        "\n"
        "def g(*args):  # pragma: no mutate block\n"
        "    return sum(args)\n",
        frozenset({"        a,", "        x,", "    )  # pragma: no mutate"}),
        frozenset({"    x = a + 1"}),
    )


def test_mutate_only_keeps_a_whole_multi_line_statement_a_kept_line_lies_in() -> None:
    """Known-bad for a narrower reading: a kept first line keeps its statement's
    continuation rows too, so the call's arguments are still mutated and none
    of them is named for the regex."""
    pruned, quiet, kept = _mutate_only(FUNCTIONS, {3}) or NOTHING
    assert "    y = g(\n        a,\n        x,\n    )\n" in pruned
    assert not {"        a,", "        x,"} & quiet
    assert {"    y = g(", "        a,", "        x,", "    )"} <= kept


def test_mutate_only_keeps_every_statement_of_a_logical_line_a_kept_line_shares() -> None:
    source = "def f(a):\n    if a: return 1\n    return 2\n"
    pruned, quiet, kept = _mutate_only(source, {2}) or NOTHING
    assert pruned == (
        "def f(a):  # pragma: no mutate\n    if a: return 1\n    return 2  # pragma: no mutate\n"
    )
    assert kept == {"    if a: return 1"}
    assert not quiet


def test_mutate_only_reads_a_static_method_like_a_function_and_skips_other_decorated_ones() -> None:
    """mutmut 3.8 mutates a function under `staticmethod` or `classmethod` and
    under no other decorator: the first is marked like any function, the rest
    are left exactly as they are (they make no mutants to narrow)."""
    source = (
        "import functools\n\n\n"
        "class K:\n"
        "    @staticmethod\n"
        "    def s(a):\n"
        "        return a + 1\n\n"
        "    @functools.cache\n"
        "    def c(self, a):\n"
        "        return a + 2\n\n"
        "    @staticmethod\n"
        "    @functools.cache\n"
        "    def both(a):\n"
        "        return a + 3\n"
    )
    pruned, _quiet, _kept = _mutate_only(source, {7}) or NOTHING
    assert pruned == source.replace("    def s(a):\n", "    def s(a):  # pragma: no mutate\n")


def test_mutate_only_keeps_the_files_own_line_endings_and_trailing_comments() -> None:
    source = "def f(a):  # noqa\r\n    return a\r\n"
    assert _mutate_only(source, set()) == (
        "def f(a):  # noqa  # pragma: no mutate block\r\n    return a\r\n",
        frozenset(),
        frozenset(),
    )


@pytest.mark.parametrize("source", ["def f(:\n    pass\n", "x = 1\x00\n"], ids=["syntax", "null"])
def test_mutate_only_gives_none_for_source_it_cannot_read(source: str) -> None:
    assert _mutate_only(source, {1}) is None


def test_mutate_only_refuses_an_edit_that_would_change_the_syntax_tree(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Known-bad: a comment appended inside a string literal is text, not a
    comment. The edit must change nothing but comments, so a row that lands
    inside a string (here by a tokenizer that misplaces a logical line's end)
    gives None and the caller keeps mutmut's own way."""
    source = 'def f():\n    x = """a\nb"""\n    return x\n'
    assert _mutate_only(source, {4}) is not None
    monkeypatch.setattr(evidence_module, "_logical_lines", lambda _s: [(1, 1), (2, 2), (4, 4)])
    assert _mutate_only(source, {4}) is None


def test_logical_lines_name_the_row_each_statement_ends_on() -> None:
    assert _logical_lines("x = (1,\n     2)  # c\nif x:\n    y = 3; z = 4\n") == [
        (1, 2),
        (3, 3),
        (4, 4),
    ]
    assert _logical_lines("x = 1") == [(1, 1)]
    with pytest.raises(tokenize.TokenError):
        _logical_lines("x = (1,\n")


def test_narrow_keeps_a_row_quiet_only_if_no_file_keeps_the_same_text() -> None:
    """Known-good and known-bad in one instance: the regex is one for the whole
    run, so a quiet row of `a.py` whose text a kept row of `b.py` also has must
    not be in it (it would silence the kept statement's mutants); a row nobody
    keeps is, and only as a whole line."""
    a = "def f(x):\n    n = 0\n    y = g(\n        1,\n        x,\n    )\n    return y\n"
    b = "def h(x):\n    z = k(\n        1,\n    )\n    return z\n"
    narrowed = _narrow({"a.py": a, "b.py": b}, {"a.py": {2}, "b.py": {2}})
    assert narrowed is not None
    assert dict(narrowed.keep) == {"a.py": {2}, "b.py": {2}}
    assert set(narrowed.sources) == {"a.py", "b.py"}
    assert re.search(narrowed.pattern, "        x,")
    assert re.search(narrowed.pattern, "    )  # pragma: no mutate")
    assert not re.search(narrowed.pattern, "        1,")
    assert not re.search(narrowed.pattern, "    y = g(        x,")


def test_narrow_drops_a_file_with_nothing_to_keep_and_is_none_without_any() -> None:
    a = "def f(x):\n    return x + 1\n"
    narrowed = _narrow({"a.py": a, "b.py": a}, {"a.py": {2}})
    assert narrowed is not None
    assert set(narrowed.sources) == {"a.py"}
    assert narrowed.pattern == ""
    assert _narrow({"a.py": a}, {"a.py": set()}) is None
    assert _narrow({"a.py": "def f(:\n"}, {"a.py": {1}}) is None


def test_excluded_rows_are_the_statements_coverage_excludes_over_their_whole_span(
    tmp_path: Path,
) -> None:
    source = (
        "def f(x):\n"
        "    if x:  # pragma: no cover\n"
        "        return 1\n"
        "    y = foo(\n"
        "        x,\n"
        "    )  # pragma: no cover\n"
        "    return y\n"
    )
    (tmp_path / "m.py").write_text(source)
    assert _excluded_rows(tmp_path, source, "m.py") == frozenset({2, 3, 4, 5, 6})


@pytest.mark.parametrize(
    ("name", "text", "known"),
    [
        (".coveragerc", "[report]\nexclude_lines = nope\n", False),
        ("setup.cfg", "[coverage:report]\nexclude_lines = nope\n", False),
        ("tox.ini", "[coverage:report]\nexclude_lines = nope\n", False),
        ("setup.cfg", "[metadata]\nname = x\n", True),
    ],
    ids=["coveragerc", "setup-cfg", "tox-ini", "setup-cfg-without-coverage"],
)
def test_excluded_rows_are_unknown_when_a_coverage_config_could_change_them(
    tmp_path: Path, name: str, text: str, known: bool
) -> None:
    """mutmut's own pass reads the scratch's coverage config; saddle reads none,
    so a project that has one keeps mutmut's own pass."""
    (tmp_path / "m.py").write_text("x = 1\n")
    (tmp_path / name).write_text(text)
    assert (_excluded_rows(tmp_path, "x = 1\n", "m.py") is not None) is known


def test_excluded_rows_are_unknown_for_a_file_that_does_not_analyse(tmp_path: Path) -> None:
    (tmp_path / "m.py").write_text("def f(:\n")
    assert _excluded_rows(tmp_path, "def f(:\n", "m.py") is None
    assert _excluded_rows(tmp_path, "x = 1\n", "gone.py") is None


def test_narrowed_scratch_drops_what_coverage_excludes_and_what_no_test_ran(tmp_path: Path) -> None:
    source = (
        "def f(x):\n"  # 1
        "    a = x + 1\n"  # 2 changed, ran
        "    b = x + 2\n"  # 3 changed, never ran
        "    if x:  # pragma: no cover\n"  # 4 changed, ran, excluded
        "        return 1\n"  # 5
        "    return a\n"  # 6 not changed
    )
    (tmp_path / "m.py").write_text(source)
    ran = {"m.py": {1, 2, 4, 5, 6}}
    narrowed = _narrowed_scratch(tmp_path, ["m.py"], {"m.py": {2, 3, 4}}, ran)
    assert narrowed is not None
    assert dict(narrowed.keep) == {"m.py": {2}}
    assert _narrowed_scratch(tmp_path, ["m.py"], {"m.py": {3}}, {"m.py": {1, 2}}) is None
    (tmp_path / "setup.cfg").write_text("[coverage:run]\nbranch = true\n")
    assert _narrowed_scratch(tmp_path, ["m.py"], {"m.py": {2}}, {"m.py": {2}}) is None


# --- mutmut itself --------------------------------------------------------------

GRADE = (
    "def label(a, b):\n"  # 1
    "    return f'{a}:{b}'\n"  # 2
    "\n"
    "\n"
    "def grade(score):\n"  # 5
    "    base = score * 2\n"  # 6 unchanged
    "    bonus = base + 5\n"  # 7 CHANGED, ran, asserted
    "    margin = bonus - 10\n"  # 8 CHANGED, ran, never asserted
    "    if bonus > 1000:\n"  # 9 unchanged
    "        return bonus - 1\n"  # 10 CHANGED, never ran
    "    text = label(\n"  # 11 unchanged, four rows
    "        bonus,\n"  # 12
    "        score,\n"  # 13
    "    )\n"  # 14
    "    return bonus, text\n"  # 15 unchanged
)
GRADE_TEST = "from n import grade\n\n\ndef test_grade():\n    assert grade(3)[0] == 11\n"
HANDED = ("tests/test_n.py::test_grade",)


def _engine_runs(monkeypatch: pytest.MonkeyPatch) -> list[dict[str, Any]]:
    """Every `mutmut run` `mutation_sample` makes, as it was handed to it: the
    scratch's config, the text of `n.py` there, and mutmut's listing after it."""
    runs: list[dict[str, Any]] = []
    real = evidence_module.run_capture

    def spy(argv: Sequence[str], cwd: Path, **kwargs: Any) -> CapturedRun:
        if list(argv[2:4]) == ["mutmut", "run"]:
            config = (cwd / "pyproject.toml").read_text()
            runs.append({"config": config, "source": (cwd / "n.py").read_text(), "argv": argv})
        done = real(argv, cwd, **kwargs)
        if list(argv[:2]) == ["mutmut", "results"]:
            runs[-1]["listing"] = _parse_mutant_verdicts(done.stdout)
        return done

    monkeypatch.setattr(evidence_module, "run_capture", spy)
    return runs


def test_real_mutmut_narrowed_run_scores_what_mutmuts_own_covered_pass_scored(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Known-good, on the enforcing engine: the changed statements a test ran
    (lines 7 and 8) are decided exactly as mutmut's own covered-lines pass
    decides them -- the same kills, the same survivors on the same lines. The
    changed statement no test ran (line 10) makes no mutant either way. The
    narrowed run made fewer mutants: none of `label`'s, none of the unchanged
    statements of `grade`, and ran no covered-lines pass."""
    _without_stubbed_mutmut(monkeypatch)
    runs = _engine_runs(monkeypatch)
    workdir = tmp_path / "work"
    (workdir / "tests").mkdir(parents=True)
    (workdir / "n.py").write_text(GRADE)
    (workdir / "tests" / "test_n.py").write_text(GRADE_TEST)
    data = tmp_path / "data"
    cover = ["-m", "coverage", "run", f"--data-file={data}", "-m", "pytest", "-q"]
    assert run_argv([sys.executable, *cover, "-p", "no:cacheprovider", "tests"], workdir) == 0
    covered = covered_lines(str(data), [str(workdir / "n.py")])
    assert {line for _, line in covered} >= {6, 7, 8, 9, 11, 15}
    assert 10 not in {line for _, line in covered}
    changed = {(str(workdir / "n.py"), line) for line in (7, 8, 10)}
    tests = {"tests/test_n.py"}
    own = mutation_sample(
        workdir, changed, 10, test_files=tests, select_tests=HANDED, only_covered=True
    )
    narrowed = mutation_sample(
        workdir,
        changed,
        10,
        test_files=tests,
        select_tests=HANDED,
        only_covered=True,
        covered=covered,
    )
    assert own.total > 0
    assert {line for _, line in narrowed.survivor_lines} == {8}
    assert (own.killed, own.total, own.survivor_lines, own.statuses) == (
        narrowed.killed,
        narrowed.total,
        narrowed.survivor_lines,
        narrowed.statuses,
    )
    own_run, narrowed_run = runs
    assert "mutate_only_covered_lines = true" in own_run["config"]
    assert "mutate_only_covered_lines" not in narrowed_run["config"]
    assert "do_not_mutate_patterns" in narrowed_run["config"]
    assert len(narrowed_run["listing"]) < len(own_run["listing"])
    assert not [name for name in narrowed_run["listing"] if ".x_label__" in name]
    assert narrowed_run["argv"][2:5] == ["mutmut", "run", "*.x_grade__mutmut_*"]


PHANTOM = (
    "def g(*args):\n"  # 1
    "    return len(args)\n"  # 2
    "\n"
    "\n"
    "def f(a, b):\n"  # 5
    "    x = g(\n"  # 6 CHANGED
    "        a,\n"  # 7
    "    )\n"  # 8
    "    y = g(\n"  # 9 unchanged
    "        a,\n"  # 10
    "        b,\n"  # 11
    "    )\n"  # 12
    "    return x, y\n"  # 13
)


def test_real_mutmut_narrowed_run_counts_no_mutant_of_a_statement_that_did_not_change(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Scope narrowed, shown on the enforcing engine. Only line 6 changed, so
    only the two mutants of `x = g(...)` are on a changed line. mutmut's own
    pass makes mutants of `y = g(...)` too, and the lines it removes for them
    (`        a,` and `    )`) read as lines of the changed statement, so the
    old count took four mutants of code nobody changed for mutants of the
    change: 6 mutants, 4 killed and 2 survived, where the change has 2, 1
    killed and 1 survived. The narrowed run makes no mutant of `y` at all,
    and so counts none of its killed or its surviving ones."""
    _without_stubbed_mutmut(monkeypatch)
    workdir = tmp_path / "work"
    (workdir / "tests").mkdir(parents=True)
    (workdir / "n.py").write_text(PHANTOM)
    (workdir / "tests" / "test_n.py").write_text(
        "from n import f\n\n\ndef test_f():\n    assert f(1, 2) == (1, 2)\n"
    )
    data = tmp_path / "data"
    cover = ["-m", "coverage", "run", f"--data-file={data}", "-m", "pytest", "-q"]
    assert run_argv([sys.executable, *cover, "-p", "no:cacheprovider", "tests"], workdir) == 0
    covered = covered_lines(str(data), [str(workdir / "n.py")])
    assert covered
    narrowed = mutation_sample(
        workdir,
        {(str(workdir / "n.py"), 6)},
        10,
        test_files={"tests/test_n.py"},
        select_tests=("tests/test_n.py::test_f",),
        only_covered=True,
        covered=covered,
    )
    assert (narrowed.killed, narrowed.total) == (1, 2)
    assert len(narrowed.mutant_detail) == 2
    assert all("y = " not in mutation_text(show) for _n, _s, show in narrowed.mutant_detail)
    assert {line for _, line in narrowed.survivor_lines} == {6}


# --- a failed narrowed run gives way to mutmut's own ---------------------------------


def _stub_engine(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, run_body: str) -> Path:
    """A tree of one function and a stub `mutmut` whose `run` is `run_body`."""
    workdir = tmp_path / "work"
    workdir.mkdir()
    (workdir / "n.py").write_text("def f(a):\n    x = a + 1\n    return x\n")
    stub_dir = tmp_path / "stub"
    stub_dir.mkdir()
    _stub_mutmut(
        stub_dir,
        "  n.x_f__mutmut_1: killed\n",
        {"n.x_f__mutmut_1": _show_diff("n.py", "    x = a + 1", "    x = a - 1")},
        run_body=run_body,
    )
    monkeypatch.setenv("PATH", f"{stub_dir}{os.pathsep}{os.environ['PATH']}")
    return workdir


def _sample(workdir: Path, covered: set[tuple[str, int]]) -> MutationOutcome:
    return mutation_sample(
        workdir,
        {(str(workdir / "n.py"), 2)},
        10,
        test_files=set(),
        select_tests=("test_n.py::test_f",),
        only_covered=True,
        covered=covered,
    )


def test_a_failed_narrowed_run_gives_way_to_mutmuts_own_pass_on_the_tree_as_it_was(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Known-bad for the edit: mutmut fails wherever the edited source is (here
    the stub exits 1 on a `no mutate` pragma). The run is then made again with
    mutmut's own covered-lines pass, on the original text, and its verdict
    stands; a failure of the narrowed run is not the answer."""
    body = 'if grep -q "pragma: no mutate" n.py; then echo broken >&2; exit 1; fi; exit 0'
    workdir = _stub_engine(tmp_path, monkeypatch, body)
    runs = _engine_runs(monkeypatch)
    outcome = _sample(workdir, {(str(workdir / "n.py"), 2)})
    assert (outcome.killed, outcome.total, outcome.survivors) == (1, 1, ())
    first, second = runs
    assert "pragma: no mutate" in first["source"]
    assert "mutate_only_covered_lines" not in first["config"]
    assert second["source"] == (workdir / "n.py").read_text()
    assert "mutate_only_covered_lines = true" in second["config"]
    assert "do_not_mutate_patterns" not in second["config"]


def test_a_narrowed_run_that_succeeds_is_not_repeated(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    workdir = _stub_engine(tmp_path, monkeypatch, "exit 0")
    runs = _engine_runs(monkeypatch)
    outcome = _sample(workdir, {(str(workdir / "n.py"), 2)})
    assert (outcome.killed, outcome.total) == (1, 1)
    assert len(runs) == 1


@pytest.mark.parametrize("ran", [(), (3,)], ids=["no-covered-lines-handed", "no-changed-line-ran"])
def test_without_a_changed_line_a_test_ran_mutmut_keeps_its_own_pass(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, ran: tuple[int, ...]
) -> None:
    """Known-bad: no covered lines handed, or none of them a changed line, leaves
    nothing to narrow to, and mutmut runs its own pass on the tree as it is (an
    empty `keep` would have skipped every function)."""
    workdir = _stub_engine(tmp_path, monkeypatch, "exit 0")
    runs = _engine_runs(monkeypatch)
    _sample(workdir, {(str(workdir / "n.py"), line) for line in ran})
    (only,) = runs
    assert "mutate_only_covered_lines = true" in only["config"]
    assert "pragma" not in only["source"]


def test_an_unparseable_file_keeps_mutmuts_own_pass(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    workdir = _stub_engine(tmp_path, monkeypatch, "exit 0")
    (workdir / "n.py").write_text("def f(a):\n    x = a +\n")
    runs = _engine_runs(monkeypatch)
    _sample(workdir, {(str(workdir / "n.py"), 2)})
    (only,) = runs
    assert "mutate_only_covered_lines = true" in only["config"]
