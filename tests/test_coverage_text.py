"""The coverage finding in English (coverage_text): function map, phrase, renderer.

The E-t5-s1 fixture is real: the sealed coverage finding from CALIB's
E-t5-s1 audit, money.py and store.py at the audited rev, and the diff from
baseline for those two files (internal CALIB run records, not public).
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

from saddle.coverage_text import (
    HEADING,
    MODULE_LEVEL,
    describe_coverage,
    gap_sentence,
    phrase,
    render_coverage,
    uncovered_lines,
)
from saddle.evidence import changed_statements
from saddle.mutant_text import describe_mutation

HERE = Path(__file__).parent / "fixtures"
T5 = HERE / "coverage_text" / "E-t5-s1"


def t5_finding() -> dict:
    return json.loads((T5 / "finding.json").read_text())


def t5_sources() -> dict[str, str]:
    # Stored as `.py.txt` so ruff does not lint a recorded tree.
    return {p.name.removesuffix(".txt"): p.read_text() for p in (T5 / "tree").iterdir()}


def t5_mutation():
    data = json.loads((HERE / "mutant_text" / "E-t5-s1.json").read_text())
    return describe_mutation(dict(data["outcome"], survivor_detail=data["survivor_detail"]))


@pytest.fixture
def t5_changed(tmp_path):
    # The caller's real spelling: `changed_statements` over the tree yields
    # absolute paths, not the detail's relative ones.
    for name, text in t5_sources().items():
        (tmp_path / name).write_text(text)
    return changed_statements(tmp_path, (T5 / "changes.diff").read_text())


def by_function(summary):
    return {(g.file, g.function): g for g in summary.gaps}


# -- known-good: the real E-t5-s1 finding --------------------------------------


def test_t5_groups_every_recorded_line_by_function(t5_changed):
    s = describe_coverage(t5_finding(), t5_sources(), t5_changed, t5_mutation())
    got = {k: g.uncovered for k, g in by_function(s).items()}
    assert got == {
        ("money.py", "_currency_error"): (42,),
        ("money.py", "check_currency"): (52,),
        ("money.py", "_coerce"): (66, 77, 78, 80),
        ("money.py", "convert"): tuple(range(131, 135)) + tuple(range(136, 142)),
        ("store.py", "_from_record_v1"): (43,),
        ("store.py", "load_accounts"): (83, 84),
    }
    assert sum(len(g.uncovered) for g in s.gaps) == s.detail_lines == 19
    assert s.unplaced == ()


def test_t5_renders_convert_and_version_1_loader(t5_changed):
    text = render_coverage(describe_coverage(t5_finding(), t5_sources(), t5_changed))
    assert text.splitlines()[0] == (
        "Not proven by any test: 19 changed lines no test runs "
        "[record: detail names 19 lines; changed-lines=155 compelled-lines=5]"
    )
    assert (
        "  - money.py convert: 10 of 16 changed lines never run -- nothing exercises convert "
        '("Convert a Decimal amount from source to target.") '
        "[lines 131, 132, 133, 134, 136, 137, 138, 139, 140, 141]"
    ) in text
    assert (
        "  - store.py _from_record_v1: 1 of 2 changed lines never run -- nothing exercises "
        '_from_record_v1 ("Rebuild one account from an old version-1 storage record,") '
        "[lines 43]"
    ) in text


def test_t5_mutation_link_only_where_a_behaviour_mutant_survived():
    s = describe_coverage(t5_finding(), t5_sources(), mutation=t5_mutation())
    assert all(g.mutant_survived for g in s.gaps)
    assert "; and a mutant there survived [lines 131" in render_coverage(s)
    # The same finding against a summary whose survivors sit elsewhere links nothing.
    other = describe_mutation({"killed": 0, "total": 0}, None)
    assert not any(
        g.mutant_survived
        for g in describe_coverage(t5_finding(), t5_sources(), mutation=other).gaps
    )


def test_never_rendered_as_failed(t5_changed):
    text = render_coverage(describe_coverage(t5_finding(), t5_sources(), t5_changed))
    assert text.startswith(HEADING)
    assert "fail" not in text.lower()


def test_every_sentence_cites_the_record(t5_changed):
    """Every bullet ends in a `[lines ...]` tag naming lines the finding
    names; a quoted source line (`      <n>: text`, PACKETHOOK) is not a
    sentence and carries no tag of its own, but its number must be one the
    bullet above it tagged."""
    s = describe_coverage(t5_finding(), t5_sources(), t5_changed, t5_mutation())
    lines = render_coverage(s).splitlines()
    assert re.search(r"\[record: [^\]]+\]$", lines[0])
    tagged: set[str] = set()
    quoted = 0
    for line in lines[1:]:
        q = re.fullmatch(r"      (\d+): .*", line)
        if q:
            assert q[1] in tagged, line
            quoted += 1
            continue
        m = re.search(r"\[lines ([\d, ]+)\]$", line)
        assert m, line
        file = line.split()[1]
        tagged = set(m[1].split(", "))
        for n in tagged:
            assert f"{file}:{n}" in t5_finding()["detail"]
    # every line the finding names is quoted exactly once (19 in E-t5-s1)
    assert quoted == len(re.findall(r"\w+\.py:\d+", t5_finding()["detail"])) == 19


# -- known-bad: module level, covered functions, no docstring -------------------

TOY = '''"""toy."""
RATE = 3
TABLE = {
    "JPY": 0,
}


def fee_for(currency):
    """Look up the fee for a currency."""
    return TABLE[currency]


def bare(x):
    return x + 1


class Box:
    @staticmethod
    def load(v):
        """Load a version-1 box."""
        return v
'''


def toy(detail: str, changed=None):
    return describe_coverage(
        {"detail": detail, "cites": ["x", "changed-lines=9"]}, {"t.py": TOY}, changed
    )


def test_line_outside_every_function_is_module_level():
    s = toy("no test runs t.py:2, t.py:4")
    assert [(g.function, g.uncovered) for g in s.gaps] == [(MODULE_LEVEL, (2, 4))]
    assert "nothing exercises the module-level code on these lines [lines 2, 4]" in render_coverage(
        s
    )


def test_covered_function_does_not_appear():
    s = toy("no test runs t.py:14")
    assert [g.function for g in s.gaps] == ["bare"]


def test_no_docstring_renders_the_name_and_nothing_else():
    (g,) = toy("no test runs t.py:14").gaps
    assert (
        gap_sentence(g)
        == "t.py bare: 1 changed lines never run -- nothing exercises bare [lines 14]"
    )
    assert phrase("bare", None) == "bare"


def test_boundaries_of_a_function_span():
    # First line (def), last line, decorator, and the lines just outside.
    assert toy("no test runs t.py:8").gaps[0].function == "fee_for"
    assert toy("no test runs t.py:10").gaps[0].function == "fee_for"
    assert toy("no test runs t.py:11").gaps[0].function == MODULE_LEVEL
    assert toy("no test runs t.py:7").gaps[0].function == MODULE_LEVEL
    assert toy("no test runs t.py:18").gaps[0].function == "Box.load"
    assert toy("no test runs t.py:17").gaps[0].function == MODULE_LEVEL
    (g,) = toy("no test runs t.py:21").gaps
    assert gap_sentence(g).endswith(
        'nothing exercises Box.load ("Load a version-1 box.") [lines 21]'
    )


def test_of_m_counts_changed_lines_in_the_same_function_and_file_only():
    changed = {
        ("/w/t.py", 9),
        ("/w/t.py", 10),
        ("/w/t.py", 14),
        ("/w/other.py", 10),
        ("/w/t.py", 2),
    }
    (g,) = toy("no test runs t.py:10", changed).gaps
    assert g.changed == 2
    (m,) = toy("no test runs t.py:2", changed).gaps
    assert m.changed == 1


def test_file_missing_from_tree_is_named_not_placed():
    s = toy("no test runs gone.py:5")
    assert s.gaps == ()
    assert s.unplaced == (("gone.py", 5),)
    assert (
        "gone.py:5: file not in the tree read; not placed [record: gone.py:5]" in render_coverage(s)
    )


def test_other_details_render_nothing():
    assert uncovered_lines("no changed lines") == []
    assert render_coverage(toy("every changed line is inside a definition")) == ""
    s = describe_coverage({"detail": "no test runs t.py:14"}, {"t.py": TOY})
    assert render_coverage(s).splitlines()[0].endswith("[record: detail names 1 lines]")


def test_blank_docstring_is_no_docstring():
    src = 'def f():\n    """ """\n    return 1\n'
    (g,) = describe_coverage({"detail": "no test runs a.py:3"}, {"a.py": src}).gaps
    assert g.doc is None


# -- the uncovered lines' own text under each bullet (PACKETHOOK) ---------------


def test_t5_convert_bullet_is_followed_by_the_text_of_its_uncovered_lines(t5_changed):
    """Known-good: exactly the lines the gate judged in `convert`, numbered,
    rstripped, read from the same money.py the function was placed in."""
    source = t5_sources()["money.py"].splitlines()
    text = render_coverage(describe_coverage(t5_finding(), t5_sources(), t5_changed))
    lines = text.splitlines()
    bullet = next(i for i, line in enumerate(lines) if line.startswith("  - money.py convert:"))
    wanted = [131, 132, 133, 134, 136, 137, 138, 139, 140, 141]
    got = lines[bullet + 1 : bullet + 1 + len(wanted)]
    assert got == [f"      {n}: {source[n - 1].rstrip()}" for n in wanted]
    assert got[0] == '      131:     if source == "USD":'
    assert got[-1] == '      141:     return usd * RATES["JPY"]'
    assert all(line.startswith("      ") and not line.startswith("      -") for line in got)
    # The line after the block is the next bullet, not more of convert.
    assert lines[bullet + 1 + len(wanted)].startswith("  - store.py")
    # Only judged lines: 135 was covered and is not printed.
    assert not any(line.startswith("      135:") for line in lines)


def test_line_text_is_an_option_and_off_prints_bullets_only(t5_changed):
    summary = describe_coverage(t5_finding(), t5_sources(), t5_changed)
    full = render_coverage(summary)
    plain = render_coverage(summary, text=False)
    assert plain.splitlines() == [x for x in full.splitlines() if not x.startswith("      ")]
    assert len(full.splitlines()) - len(plain.splitlines()) == 19  # one per uncovered line


def test_a_missing_source_is_not_placed_and_prints_no_line_text():
    """Known-bad: a file the tree did not hold keeps COVTEXT's "not placed"
    line and no text is invented for it."""
    s = describe_coverage({"detail": "no test runs gone.py:5, here.py:2", "cites": []},
                          {"here.py": "x = 1\ny = 2\n"})  # fmt: skip
    assert s.unplaced == (("gone.py", 5),)
    text = render_coverage(s)
    assert "  - gone.py:5: file not in the tree read; not placed [record: gone.py:5]" in text
    assert "      2: y = 2" in text
    assert "      5:" not in text
    assert [g.text for g in s.gaps] == [((2, "y = 2"),)]


# -- the compact rendering: the recap must not scroll (PACKETHOOK-3) ---------------


def test_compact_caps_functions_at_five_with_two_lines_each(t5_changed):
    s = describe_coverage(t5_finding(), t5_sources(), t5_changed)
    assert len(s.gaps) == 6
    text = render_coverage(s, compact=True)
    lines = text.splitlines()
    bullets = [x for x in lines if x.startswith("  - ")]
    assert len(bullets) == 5
    assert bullets[0].startswith("  - money.py convert: 10 of 16")  # most uncovered first
    assert lines[-1] == "  and 1 more functions in the packet"
    # The one left out is the last 1-line function in record order.
    assert "store.py _from_record_v1" not in text
    assert "store.py _from_record_v1" in render_coverage(s)
    convert = lines.index(bullets[0])
    assert lines[convert + 1 : convert + 4] == [
        '      131:     if source == "USD":',
        "      132:         usd = value",
        "      and 8 more lines",
    ]
    assert lines[convert + 4].startswith("  - ")
    assert len(lines) <= 20


def test_compact_with_few_functions_has_no_more_lines():
    s = toy("no test runs t.py:2, t.py:10")
    text = render_coverage(s, compact=True)
    assert "more functions" not in text
    assert "more lines" not in text
    assert [x for x in text.splitlines() if x.startswith("  - ")] == [
        x for x in render_coverage(s).splitlines() if x.startswith("  - ")
    ]


def test_compact_counts_unplaced_lines_on_one_line():
    s = describe_coverage({"detail": "no test runs gone.py:5, gone.py:6", "cites": []}, {})
    text = render_coverage(s, compact=True)
    assert text.splitlines()[-1] == "  2 lines not placed (file not in the tree read)"
    assert "not placed [record:" not in text
    assert render_coverage(s, compact=True, text=False) == text


def test_compact_without_text_quotes_no_line(t5_changed):
    s = describe_coverage(t5_finding(), t5_sources(), t5_changed)
    text = render_coverage(s, compact=True, text=False)
    assert "      131:" not in text
    assert "more lines" not in text
    assert [x for x in text.splitlines() if x.startswith("  - ")] == [
        x for x in render_coverage(s, compact=True).splitlines() if x.startswith("  - ")
    ]


def test_compact_on_a_passing_finding_is_empty():
    s = describe_coverage({"detail": "every changed line is run", "cites": []}, {"t.py": TOY})
    assert render_coverage(s, compact=True) == ""
    assert render_coverage(s) == ""
