"""A change that is only module-level data reads mutation "not proven", not "fail".

Known-good: when mutmut generates nothing and every changed source line is a
module-level literal constant (a new entry in a tuple of names, as on #94), the
mutation finding is not proven with a named reason, and the tier passes.
Known-bad: module-level code that is not a literal (a call) still fails -- the
case the fail exists for, a module mutmut cannot reach -- and so does an engine
that failed, even on a data-only change.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from test_auditor import _init

from saddle import runner
from saddle.auditor import Auditor, data_only_change
from saddle.evidence import MutationOutcome

BASE = "NAMES = ('a',)\n\n\ndef f():\n    return 1\n"
TEST = (
    "from n import NAMES, f\n\n\n"
    "def test_b_is_named():\n    assert 'b' in NAMES\n    assert f() == 1\n"
)


def tree(tmp_path: Path, names_line: str) -> Path:
    root = tmp_path / "tree"
    _init(
        root,
        {"n.py": BASE, "test_n.py": "from n import f\n\n\ndef test_f():\n    assert f() == 1\n"},
    )
    (root / "n.py").write_text(BASE.replace("NAMES = ('a',)", names_line))
    (root / "test_n.py").write_text(TEST)
    return root


def sampled(outcome: MutationOutcome, monkeypatch: pytest.MonkeyPatch) -> None:
    def fake(*_args: Any, **_kwargs: Any) -> MutationOutcome:
        return outcome

    monkeypatch.setattr(runner, "mutation_sample", fake)


NOTHING = MutationOutcome(killed=0, total=0, generated=0, survivors=())


def mutation(root: Path) -> tuple[str, str, bool]:
    found = Auditor(root).tier2()
    (m,) = [f for f in found.findings if f.gate == "mutation"]
    return m.verdict, m.detail, found.passed


def test_a_data_only_change_is_not_proven_and_does_not_refuse(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    sampled(NOTHING, monkeypatch)
    verdict, detail, passed = mutation(tree(tmp_path, "NAMES = ('a', 'b')"))
    assert verdict == "not-proven"
    assert detail.startswith("not proven: the source this change touches is module-level")
    assert "(n.py)" in detail
    assert passed


def test_module_level_code_that_is_not_a_literal_still_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    sampled(NOTHING, monkeypatch)
    verdict, detail, passed = mutation(tree(tmp_path, "NAMES = tuple(sorted({'b', 'a'}))"))
    assert verdict == "fail"
    assert detail == "no mutants on changed lines: mutation provided no evidence"
    assert not passed


def test_an_engine_that_failed_is_named_not_read_as_data_only(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A failed engine is never relabelled as the data-only case: the detail
    names the tool, and the verdict is not-proven (#169), never a pass."""
    broken = MutationOutcome(killed=0, total=0, generated=0, survivors=("mutmut run exited 2",))
    sampled(broken, monkeypatch)
    verdict, detail, _ = mutation(tree(tmp_path, "NAMES = ('a', 'b')"))
    assert verdict == "not-proven"
    assert detail.startswith("mutation tool failed")
    assert "module-level" not in detail


def test_data_only_change_reads_every_changed_source_line(tmp_path: Path) -> None:
    root = tree(tmp_path, "NAMES = ('a', 'b')")
    assert data_only_change(root, "HEAD") == ["n.py"]
    # A changed function body anywhere in the source makes it not data-only.
    (root / "n.py").write_text(
        BASE.replace("NAMES = ('a',)", "NAMES = ('a', 'b')").replace("return 1", "return 2")
    )
    assert data_only_change(root, "HEAD") == []
    # Only a test changed: no source data to name.
    (root / "n.py").write_text(BASE)
    assert data_only_change(root, "HEAD") == []


# -- data built from names: #132a r1 ------------------------------------------------

TABLE_BASE = (
    "from typing import Final\n\n"
    'CSS: Final = "css"\n'
    "SUFFIX: Final = {\n"
    '    ".css": CSS,\n'
    "}\n"
    "NAMES: Final = frozenset({CSS})\n"
    '"""The names a project may use."""\n\n\n'
    "def classify(suffix):\n    return SUFFIX.get(suffix, 'other')\n"
)
TABLE_HEAD = (
    "from typing import Final\n\n"
    'CSS: Final = "css"\n'
    'SQL: Final = "sql"\n'
    "SUFFIX: Final = {\n"
    '    ".css": CSS,\n'
    '    ".sql": SQL,\n'
    "}\n"
    "NAMES: Final = frozenset({CSS, SQL})\n"
    '"""The names a project may use, sql among them."""\n\n\n'
    "def classify(suffix):\n    return SUFFIX.get(suffix, 'other')\n"
)
TABLE_TEST = (
    "from n import NAMES, classify\n\n\n"
    "def test_sql():\n    assert classify('.sql') == 'sql'\n    assert 'sql' in NAMES\n"
)


def table_tree(tmp_path: Path, head: str) -> Path:
    root = tmp_path / "table"
    _init(root, {"n.py": TABLE_BASE, "test_n.py": "def test_x():\n    assert True\n"})
    (root / "n.py").write_text(head)
    (root / "test_n.py").write_text(TABLE_TEST)
    return root


def test_a_table_of_named_constants_is_data_and_its_change_is_not_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """#132a r1's change, in miniature: a constant, a dict entry naming it, a
    frozenset of names and the docstring under it. Correct and data only; the old
    rule (literals only) refused it "no mutants on changed lines"."""
    sampled(NOTHING, monkeypatch)
    root = table_tree(tmp_path, TABLE_HEAD)
    assert data_only_change(root, "HEAD") == ["n.py"]
    verdict, detail, passed = mutation(root)
    assert verdict == "not-proven"
    assert detail.startswith("not proven: the source this change touches is module-level")
    assert passed


@pytest.mark.parametrize(
    "value",
    [
        "frozenset({CSS}) | {SQL}",
        "(*NAMES, SQL)",
        "{**{'.css': CSS}, '.sql': SQL}",
        "-1",
        "(CSS, SQL, languages.PYTHON)",  # a constant another module names
    ],
)
def test_operators_and_unpacking_over_data_are_data(tmp_path: Path, value: str) -> None:
    head = TABLE_BASE.replace("NAMES: Final = frozenset({CSS})", f"NAMES: Final = {value}")
    head = head.replace('CSS: Final = "css"\n', 'CSS: Final = "css"\nSQL: Final = "sql"\n')
    assert data_only_change(table_tree(tmp_path, head), "HEAD") == ["n.py"]


@pytest.mark.parametrize(
    "value",
    [
        "frozenset(sorted({CSS}))",  # a call that is not a container's constructor
        "load_names()",
        "{n for n in (CSS,)}",
        "SUFFIX['.css']",
        "frozenset(name=load_names())",
    ],
)
def test_module_level_code_that_computes_is_not_data(tmp_path: Path, value: str) -> None:
    """Still refused as before (test_module_level_code_that_is_not_a_literal_still_fails
    pins the finding for a tree whose tests pass)."""
    head = TABLE_BASE.replace("NAMES: Final = frozenset({CSS})", f"NAMES: Final = {value}")
    assert data_only_change(table_tree(tmp_path, head), "HEAD") == []


def test_a_changed_module_level_statement_that_is_not_an_assignment_is_not_data(
    tmp_path: Path,
) -> None:
    head = TABLE_BASE.replace('"""The names a project may use."""', "for _ in ():\n    pass")
    assert data_only_change(table_tree(tmp_path, head), "HEAD") == []
