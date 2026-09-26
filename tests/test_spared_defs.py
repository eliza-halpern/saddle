"""FEEDFIX item 3: coverage names each baseline definition it spared.

`check_changed_line_coverage` removes from its judgement the lines of a
public definition the baseline had, that `public-deletions` will not let
the change drop and that no test reaches (`compelled_lines`, T6-75). Its
`basis` said only how many lines ("compelled-lines=N"), so a pass that
judged nothing in `Account.to_dict` read the same as one that judged
everything. Now the basis names each spared definition, the auditor
seals the names beside the coverage finding whatever its verdict, and
the packet lists each under Not proven.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from saddle.auditor import Auditor, AuditorConfig
from saddle.evidence import run_argv
from saddle.gates import (
    check_changed_line_coverage,
    compelled_definitions,
    compelled_lines,
    spared_definitions,
)
from saddle.journal import attempt_sidecar_path, read_spans
from saddle.packet import compile_packet

BASE = "def f():\n    return 1\n\n\nclass A:\n    def keep(self):\n        return 1\n"
AFTER = BASE.replace("        return 1\n", "        return 2\n")
TWO = BASE + "\n    def other(self):\n        return 3\n"


def test_compelled_definitions_is_compelled_lines_per_definition() -> None:
    both = compelled_definitions({"n.py": TWO}, {"n.py": TWO}, "/w")
    assert sorted(both) == ["n.py:A.keep", "n.py:A.other", "n.py:f"]
    assert set().union(*both.values()) == compelled_lines({"n.py": TWO}, {"n.py": TWO}, "/w")
    assert both["n.py:A.keep"] == {("/w/n.py", 6), ("/w/n.py", 7)}


def test_the_basis_names_only_the_definitions_a_changed_line_was_spared_from() -> None:
    by_def = compelled_definitions({"n.py": TWO}, {"n.py": TWO})
    # known-bad for the old basis: a pass that judged nothing in A.keep
    check = check_changed_line_coverage({("n.py", 7)}, set(), 100.0, (), by_def)
    assert check.passed
    assert check.basis == "changed-lines=1 compelled-lines=1 spared-defs=n.py:A.keep"
    assert spared_definitions(check.basis) == ["n.py:A.keep"]
    # two spared, sorted; a compelled definition with no changed line is not named
    two = check_changed_line_coverage({("n.py", 7), ("n.py", 2)}, set(), 100.0, (), by_def)
    assert spared_definitions(two.basis) == ["n.py:A.keep", "n.py:f"]
    # the plain line set still spares, and names nothing
    lines = check_changed_line_coverage({("n.py", 7)}, set(), 100.0, (), set(by_def["n.py:A.keep"]))
    assert lines.passed
    assert lines.basis == "changed-lines=1 compelled-lines=1"
    assert spared_definitions(lines.basis) == []
    # nothing spared, nothing named: the line is judged and fails
    none = check_changed_line_coverage({("n.py", 4)}, set(), 100.0, (), by_def)
    assert not none.passed
    assert spared_definitions(none.basis) == []


def _git(root: Path, *argv: str) -> None:
    assert run_argv(["git", *argv], root) == 0


def _tree(tmp_path: Path, test: str) -> Path:
    root = tmp_path / "tree"
    root.mkdir()
    for argv in (["init", "-q"], ["config", "user.email", "t@t"], ["config", "user.name", "t"]):
        _git(root, *argv)
    (root / "n.py").write_text(BASE)
    (root / "test_n.py").write_text(test)
    _git(root, "add", "-A")
    _git(root, "commit", "-q", "-m", "base")
    (root / "n.py").write_text(AFTER)
    return root


UNCALLED = "from n import f\n\n\ndef test_f():\n    assert f() == 1\n"
CALLED = UNCALLED + "\n\ndef test_keep():\n    from n import A\n\n    assert A().keep()\n"


@pytest.mark.parametrize(
    ("test", "spared"),
    [(UNCALLED, ["n.py:A.keep"]), (CALLED, [])],
    ids=["uncalled", "called"],
)
def test_the_auditor_seals_and_the_packet_lists_each_spared_definition(
    tmp_path: Path, test: str, spared: list[str]
) -> None:
    journal = tmp_path / "proofs.jsonl"
    found = Auditor(_tree(tmp_path, test), config=AuditorConfig(journal=journal)).tier1()
    coverage = next(f for f in found.findings if f.gate == "coverage")
    assert coverage.verdict == "pass", coverage.detail
    assert spared_definitions(coverage.cites[-1]) == spared
    span = next(s for s in read_spans(journal) if s.name == "audit-tier1:coverage")
    sealed = attempt_sidecar_path(journal, span.span_id).read_text() if span.attempt_hash else ""
    assert ("n.py:A.keep" in sealed) is bool(spared)
    rows = {r.key: r for r in compile_packet(journal).rows}
    items = [i for i in rows["not-proven"].items if "was not judged by coverage" in i]
    assert items == [
        f"{n} was not judged by coverage: a definition the baseline had, which the change "
        "may not delete and no test reaches."
        for n in spared
    ]
    if spared:
        assert span.record_hash in rows["not-proven"].cites
