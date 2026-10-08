"""The spared-definitions list in a coverage finding's header is capped (#101).

`check_changed_line_coverage` names every baseline definition it spared
(`basis` ends `spared-defs=<file>:<name>,...`) and the coverage header quoted
that list whole. In the recorded draw EAF-t5 s1 the header of the coverage
finding took 234 and 236 characters of the `feed.DETAIL_CHARS` a finding gets,
111 of them a four-name definition list the model cannot act on, ahead of the
per-function rows that say where its lines are unproven -- checkpoint 2 has 19
of those rows and the model read six. A longer list spends the whole budget:
40 names of the shape below are 2211 characters, so no row is read at all. The
model-facing header now names at most `coverage_text.SPARED_NAMES` of them and
counts the rest, "and N more", while the finding's cites, the sealed coverage
sidecar and the packet's Not proven rows keep every name.
"""

from __future__ import annotations

import json
from pathlib import Path

from saddle import coverage_text, feed
from saddle.audit import audit_node
from saddle.auditor import Auditor, AuditorConfig, Finding
from saddle.dag import Node
from saddle.evidence import run_argv
from saddle.gates import spared_definitions
from saddle.journal import attempt_sidecar_path, read_spans
from saddle.packet import compile_packet

# `spared-defs=<file>:<qualname>,...` as the gate spells it: long package paths
# and method names, the shape a baseline carrying several untouched public
# definitions produces.
SPARED = [f"package/subpackage/module_{i:02d}.py:Account.method_name_{i:02d}" for i in range(40)]
SOURCE = "def bare(boxes):\n    total = 0\n    for box in boxes:\n        total += box.size\n"


def spared_summary(
    names: list[str], detail: str = "no test runs t.py:3"
) -> coverage_text.CoverageSummary:
    """The summary of a finding that spared `names` baseline definitions."""
    return coverage_text.describe_coverage(
        {
            "detail": detail,
            "cites": [
                "saddle.gates.check_changed_line_coverage",
                f"changed-lines=400 compelled-lines=32 spared-defs={','.join(names)}",
            ],
        },
        {"t.py": SOURCE},
    )


def spared_head(names: list[str]) -> str:
    """The header a finding with `names` spared definitions shows the model."""
    return coverage_text.render_coverage(spared_summary(names), text=False).splitlines()[0]


def test_a_spared_list_within_the_cap_reads_exactly_as_the_record_spells_it() -> None:
    """Known-good (#101): one, two and three spared definitions render as they
    always did, every name quoted and nothing appended."""
    for names in (SPARED[:1], SPARED[:2], SPARED[:3]):
        head = spared_head(names)
        assert head.endswith(f"spared-defs={','.join(names)}]")
        assert " more]" not in head


def test_the_header_names_three_spared_definitions_and_counts_the_rest() -> None:
    """Known-bad (#101): the header quoted the gate's whole spared list, so a
    tree with many untouched baseline public definitions spent the finding's
    budget naming definitions the model cannot act on."""
    head = spared_head(SPARED)
    assert head == (
        "Not proven by any test: 1 changed lines no test runs "
        "[record: detail names 1 lines; changed-lines=400 compelled-lines=32 "
        f"spared-defs={','.join(SPARED[:3])} and 37 more]"
    )
    for dropped in SPARED[3:]:
        assert dropped not in head
    # The three it names are the three the record names first, in record order.
    assert all(name in head for name in SPARED[:3])


def test_the_header_stops_growing_once_the_list_passes_the_cap() -> None:
    """The bound holds whatever the tree holds: four spared definitions or
    forty name the same three, and the header's length moves only by how many
    the finding had to count."""
    heads = [spared_head(SPARED[:n]) for n in (4, 9, 40)]
    named = {head.split(" and ")[0].split("spared-defs=")[1] for head in heads}
    assert named == {",".join(SPARED[:3])}
    assert all(
        head.endswith(f"and {n - 3} more]") for head, n in zip(heads, (4, 9, 40), strict=True)
    )
    assert all(len(head) < len(f"spared-defs={','.join(SPARED)}") for head in heads)


def test_a_long_spared_list_does_not_push_the_rows_out_of_the_model_s_text() -> None:
    """Known-bad (#101): the 40-name field is 2211 characters and the heading
    built from it 2333, both longer than the whole `feed.DETAIL_CHARS` a
    finding gets, so the model read the head of a definition list cut mid-name
    and no row saying where its lines are unproven."""
    uncapped = f"spared-defs={','.join(SPARED)}"
    assert len(uncapped) > feed.DETAIL_CHARS
    summary = spared_summary(SPARED, detail="no test runs t.py:2, t.py:3, t.py:4")
    text = coverage_text.render_coverage(summary, text=False)
    cov = Finding("coverage", 1, "fail", "evidence-thin", "no test runs t.py:2", ())
    shown = feed._worded(feed.AuditResult("checkpoint 1", "t" * 40, (cov,), coverage=text), cov)
    assert "and 37 more" in shown
    assert "t.py bare: 3 changed lines never run -- nothing exercises bare [lines 2, 3, 4]" in shown
    assert len(shown) <= feed.DETAIL_CHARS + len(" ...")


def test_capping_the_header_loses_no_name_from_the_finding_cites() -> None:
    """The cap is the rendering's, not the record's: the finding's cites --
    what the auditor seals and the packet reads back -- still hold every
    spared definition, and the recap's header is capped the same way."""
    summary = spared_summary(SPARED)
    assert spared_definitions(summary.cites[-1]) == SPARED
    compact = coverage_text.render_coverage(summary, compact=True).splitlines()[0]
    assert compact == spared_head(SPARED)


# A baseline holding five untouched public definitions a change edits but may
# not cover: the same shape as the recorded draw, at the size where the header
# has to cap the list.
MANY_BASE = '''"""Ledger."""


def keep_one():
    return 1


def keep_two():
    return 2


class Account:
    def to_dict(self):
        return {}

    def from_dict(self):
        return {}

    def __repr__(self):
        return "Account()"
'''
MANY_AFTER = (
    MANY_BASE.replace("    return 1", "    return 3")
    .replace("    return 2", "    return 4")
    .replace("        return {}", "        return 0")
    .replace('return "Account()"', "return 'Account'")
)
MANY_UNCALLED_TEST = "def test_nothing():\n    assert True\n"
MANY_SPARED = [
    "n.py:Account.__repr__",
    "n.py:Account.from_dict",
    "n.py:Account.to_dict",
    "n.py:keep_one",
    "n.py:keep_two",
]


def _git(root: Path, *argv: str) -> None:
    assert run_argv(["git", *argv], root) == 0


def _many_tree(tmp_path: Path) -> Path:
    """Baseline `MANY_BASE` committed; `MANY_AFTER` in the working tree."""
    root = tmp_path / "tree"
    root.mkdir()
    for argv in (["init", "-q"], ["config", "user.email", "t@t"], ["config", "user.name", "t"]):
        _git(root, *argv)
    (root / "n.py").write_text(MANY_BASE, encoding="utf-8")
    (root / "test_n.py").write_text(MANY_UNCALLED_TEST, encoding="utf-8")
    _git(root, "add", "-A")
    _git(root, "commit", "-q", "-m", "base")
    (root / "n.py").write_text(MANY_AFTER, encoding="utf-8")
    return root


def _impl_node() -> Node:
    """The audit's node as an `impl` node: the one kind that may not edit tests."""
    return audit_node().model_copy(update={"kind": "impl"})


def test_a_long_spared_list_stays_whole_in_the_record_and_the_packet(tmp_path: Path) -> None:
    """Known-good (#101): the cap belongs to the model-facing header, not to the
    record. With five spared baseline definitions the finding's cites, the
    sealed coverage sidecar and the packet's Not proven rows each still name
    every one."""
    journal = tmp_path / "proofs.jsonl"
    config = AuditorConfig(journal=journal, node=_impl_node())
    found = Auditor(_many_tree(tmp_path), config=config).tier1()
    coverage = next(f for f in found.findings if f.gate == "coverage")
    assert coverage.verdict == "pass", coverage.detail
    assert spared_definitions(coverage.cites[-1]) == MANY_SPARED
    span = next(s for s in read_spans(journal) if s.name == "audit-tier1:coverage")
    sealed = json.loads(attempt_sidecar_path(journal, span.span_id).read_text())
    assert sealed["spared"] == MANY_SPARED
    rows = {row.key: row for row in compile_packet(journal).rows}
    items = [i for i in rows["not-proven"].items if "was not judged by coverage" in i]
    assert items == [
        f"{name} was not judged by coverage: a definition the baseline had, "
        "which the change may not delete and no test reaches."
        for name in MANY_SPARED
    ]
