"""K2 §3.3: each changed `raise` no test enters is named under Not proven.

The row is read from the coverage record the gate already made (the lines
its detail names, the definitions its `basis` spared) and placed in the tree
by `coverage_text.unreached_raises`. It is a report: no verdict changes,
which a test below shows by auditing the same tree with the row removed.
A raise inside a baseline definition the gate spared is named too, marked
"spared, not judged" (K2-8), where before no row said anything.
"""

from __future__ import annotations

import dataclasses
from pathlib import Path

import pytest
from test_p1_raises import PORT_UNITS, VALUE_ERROR, example, port_requirements

from saddle import auditor
from saddle.audit import audit_node
from saddle.auditor import Auditor, AuditorConfig
from saddle.coverage_text import MODULE_LEVEL, RaiseGap, raise_row, unreached_raises
from saddle.evidence import run_argv
from saddle.gates import check_task_requirements
from saddle.journal import read_spans
from saddle.packet import compile_packet
from saddle.task_examples import Outcome, TreeOutcome

SOURCE = """\
def f(x):
    if x < 0:
        raise ValueError(x)
    if x > 9:
        raise KeyError(x)
    return x


class A:
    def keep(self, x):
        if x:
            raise TypeError(x)
        return x


raise SystemExit
"""


def test_a_named_changed_raise_is_a_row_and_an_unchanged_or_run_one_is_not() -> None:
    changed = {("m.py", 3), ("m.py", 5), ("m.py", 16)}
    # line 3 uncovered and changed: named; line 5 changed but run: not named;
    # line 12 uncovered but unchanged: not named; module level is its own scope
    gaps = unreached_raises({"m.py": SOURCE}, changed, [("m.py", 3), ("m.py", 12), ("m.py", 16)])
    assert gaps == [
        RaiseGap("m.py", 3, "f", False, None),
        RaiseGap("m.py", 16, MODULE_LEVEL, False, None),
    ]


def test_a_raise_in_a_spared_definition_is_named_spared_not_judged() -> None:
    # the gate takes spared lines out of its judgement: the detail never names them
    gaps = unreached_raises({"m.py": SOURCE}, {("m.py", 12)}, [], ["m.py:A.keep"])
    assert gaps == [RaiseGap("m.py", 12, "A.keep", True, None)]
    # known-bad half: the same line, spared in another file's definition, is not
    assert unreached_raises({"m.py": SOURCE}, {("m.py", 12)}, [], ["o.py:A.keep"]) == []


def test_whether_a_p1_raise_example_entered_the_function_is_said_both_ways() -> None:
    def p1(entered: list[tuple[str, int]]) -> bool | None:
        (gap,) = unreached_raises({"m.py": SOURCE}, {("m.py", 3)}, [("m.py", 3)], (), entered)
        return gap.p1

    assert p1([("m.py", 2)]) is True
    assert p1([("m.py", 11), ("o.py", 2)]) is False
    module = unreached_raises({"m.py": SOURCE}, {("m.py", 16)}, [("m.py", 16)], (), [])
    assert [g.p1 for g in module] == [None]


def test_a_file_that_does_not_parse_yields_no_row() -> None:
    assert unreached_raises({"m.py": "def (:\n"}, {("m.py", 1)}, [("m.py", 1)]) == []


def test_the_row_text() -> None:
    plain = RaiseGap("n.py", 4, "A.keep", False)
    assert raise_row(plain) == "raise in `A.keep` (`n.py:4`): no test enters this branch"
    both = dataclasses.replace(plain, spared=True, p1=True)
    assert raise_row(both) == (
        "raise in `A.keep` (`n.py:4`): no test enters this branch; spared, not judged; "
        "a task-text raise example entered `A.keep`"
    )
    assert raise_row(dataclasses.replace(plain, p1=False)).endswith(
        "; no task-text raise example entered `A.keep`"
    )


def test_the_gate_records_what_raise_examples_ran_and_none_without_one() -> None:
    raising = example("parse_port('0')", VALUE_ERROR)
    missing = example("parse_port('-1')", VALUE_ERROR, eid="E-002")
    valued = example("parse_port('80')", Outcome.of("value", "80"), eid="E-003")
    ran = ("ports.py:2 (parse_port)", "ports.py:4 (parse_port)", "my dir/p.py:7 (g)")
    results = {
        "E-001": TreeOutcome("raises", raises=("ValueError",), ran=ran),
        "E-003": TreeOutcome("value", ran=("ports.py:9 (other)",)),
    }
    check = check_task_requirements(results, PORT_UNITS, [raising, missing, valued])
    assert check.raise_ran == (("my dir/p.py", 7), ("ports.py", 2), ("ports.py", 4))
    assert check_task_requirements(results, PORT_UNITS, [valued]).raise_ran is None


# -- end to end: the auditor seals the rows, the packet prints them ----------


def _git(root: Path, *argv: str) -> None:
    assert run_argv(["git", *argv], root) == 0


def _tree(tmp_path: Path, files: dict[str, str], after: dict[str, str]) -> Path:
    root = tmp_path / "tree"
    root.mkdir()
    for argv in (["init", "-q"], ["config", "user.email", "t@t"], ["config", "user.name", "t"]):
        _git(root, *argv)
    for name, text in files.items():
        (root / name).write_text(text)
    _git(root, "add", "-A")
    _git(root, "commit", "-q", "-m", "base")
    for name, text in after.items():
        (root / name).write_text(text)
    return root


KEEP = "class A:\n    def keep(self, x):\n        return x\n"
KEEP_RAISES = KEEP.replace(
    "        return x\n", "        if x < 0:\n            raise ValueError(x)\n        return x\n"
)
UNCALLED = "from n import A\n\n\ndef test_a():\n    assert A\n"
CALLED = (
    "import pytest\n\nfrom n import A\n\n\ndef test_a():\n"
    "    with pytest.raises(ValueError):\n        A().keep(-1)\n    assert A().keep(1) == 1\n"
)
ROW = "raise in `A.keep` (`n.py:4`): no test enters this branch; spared, not judged."


def _rows(journal: Path) -> list[str]:
    rows = {r.key: r for r in compile_packet(journal).rows}
    return [i for i in rows["not-proven"].items if i.startswith("raise in ")]


@pytest.mark.parametrize(("test", "rows"), [(UNCALLED, [ROW]), (CALLED, [])], ids=["k2-8", "run"])
def test_k2_8_a_raise_in_a_spared_definition_is_named_and_changes_no_verdict(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, test: str, rows: list[str]
) -> None:
    root = _tree(tmp_path, {"n.py": KEEP, "test_n.py": test}, {"n.py": KEEP_RAISES})
    node = audit_node().model_copy(update={"kind": "impl"})
    journal = tmp_path / "proofs.jsonl"
    found = Auditor(root, config=AuditorConfig(journal=journal, node=node)).tier1()
    coverage = next(f for f in found.findings if f.gate == "coverage")
    assert coverage.verdict == "pass", coverage.detail
    assert _rows(journal) == rows
    # the row is a report: the same tree audited without it reads the same
    monkeypatch.setattr(auditor, "raise_gaps", lambda *a, **k: [])
    bare = tmp_path / "bare.jsonl"
    again = Auditor(root, config=AuditorConfig(journal=bare, node=node)).tier1()
    assert _rows(bare) == []
    assert [(f.gate, f.verdict, f.detail) for f in again.findings] == [
        (f.gate, f.verdict, f.detail) for f in found.findings
    ]


PORT_BASE = "def parse_port(s):\n    return int(s)\n"
PORT_TREE = """\
def parse_port(s):
    n = int(s)
    if not 1 <= n <= 65535:
        raise ValueError(s)
    return n
"""
PORT_TEST = (
    'from ports import parse_port\n\n\ndef test_port():\n    assert parse_port("80") == 80\n'
)


def test_an_uncovered_raise_says_whether_a_p1_raise_example_entered_it(tmp_path: Path) -> None:
    root = _tree(
        tmp_path, {"ports.py": PORT_BASE, "test_ports.py": PORT_TEST}, {"ports.py": PORT_TREE}
    )
    requirements = tmp_path / "p1"
    requirements.mkdir()
    path = port_requirements(requirements, ['"0"'], ["below 1"])
    journal = tmp_path / "proofs.jsonl"
    config = AuditorConfig(journal=journal, task_requirements=path)
    found = Auditor(root, config=config).tier1()
    coverage = next(f for f in found.findings if f.gate == "coverage")
    assert coverage.verdict == "fail", coverage.detail
    assert "ports.py:4" in coverage.detail
    p1 = next(f for f in found.findings if f.gate == "task-requirements")
    assert p1.verdict == "pass", p1.detail
    assert _rows(journal) == [
        "raise in `parse_port` (`ports.py:4`): no test enters this branch; "
        "a task-text raise example entered `parse_port`."
    ]
    span = next(s for s in read_spans(journal) if s.name == "audit-tier1:coverage")
    assert span.attempt_hash


def test_a_named_file_the_tree_cannot_read_is_left_out(tmp_path: Path) -> None:
    root = _tree(tmp_path, {"n.py": KEEP}, {"n.py": KEEP_RAISES})
    # known-good half: the readable file's raise is named
    assert [g["line"] for g in auditor.raise_gaps(root, "HEAD", "no test runs n.py:4", "")] == [4]
    assert auditor.raise_gaps(root, "HEAD", "no test runs gone.py:4, n.py:4", "") == [
        dataclasses.asdict(RaiseGap("n.py", 4, "A.keep", False))
    ]
    assert auditor.raise_gaps(root, "HEAD", "every changed line runs", "changed-lines=3") == []
