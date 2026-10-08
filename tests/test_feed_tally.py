"""The coverage finding the model reads names every file that carries its count.

The feed caps each finding at `feed.DETAIL_CHARS`. The coverage finding's
rows run in path order, so a file late in the alphabet can hold most of the
count and still fall past the cap. Benchmark draw EAF-t5 s1 did exactly
that: at checkpoint 2 the heading said 246 lines, 223 of them were in the
model's own `scratch_verify.py`, and the capped text named that file 0
times. The model saw the count jump with the same product rows visible and
spent 24 rounds writing its own coverage tracers.

The fixture is that draw's checkpoints 1 and 2, rebuilt from the sealed
audit sidecars: the findings, and the sources and changed-statement set
`auditor.coverage_evidence` sealed beside them. The coverage finding's
detail was cut at 4000 chars by the sidecar cap; it is rebuilt from the
sealed coverage text, which was whole, and the rebuild renders those rows
back exactly, under the recorded heading with its spared-definition list
capped for the model (`test_the_fixture_renders_the_rows_the_run_sealed`,
`test_the_recorded_header_names_three_spared_definitions_and_counts_the_fourth`,
#101).
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from saddle import coverage_text, feed
from saddle.auditor import Finding
from saddle.feed import AuditResult, render
from saddle.gates import spared_definitions

FIXTURE = Path(__file__).parent / "fixtures" / "eaf_t5_s1_checkpoints.json"

# The four baseline definitions that draw's coverage finding spared, and the
# heading the model is shown for each recorded checkpoint: the sealed one with
# the spared-definition list capped at `coverage_text.SPARED_NAMES` (#101). The
# fixture keeps the heading as the run sealed it -- it is what this cap
# replaces in the text a model reads -- and the rows below it still come from
# that text, whole.
SPARED_NAMES = (
    "accounts.py:Account.__eq__",
    "accounts.py:Account.__repr__",
    "accounts.py:Account.to_dict",
    "fees.py:fee_for",
)

# What the gate's `basis` cite puts before that list: `saddle.gates.SPARED_DEFS`.
SPARED_FIELD = "spared-defs="


def _capped_head(count: int, changed: int) -> str:
    """The recorded heading as the model now reads it: three of the four spared
    definitions named, the fourth counted (the three are literal here, so a
    change to `coverage_text.SPARED_NAMES` is a failure, not a follow-along)."""
    return (
        f"Not proven by any test: {count} changed lines no test runs "
        f"[record: detail names {count} lines; changed-lines={changed} "
        f"compelled-lines=6 spared-defs={','.join(SPARED_NAMES[:3])} and 1 more]"
    )


CP_HEADS = {"checkpoint 1": _capped_head(23, 139), "checkpoint 2": _capped_head(246, 362)}


def _findings(data: dict[str, Any]) -> tuple[Finding, ...]:
    return tuple(
        Finding(f["gate"], f["tier"], f["verdict"], f["reason"], f["detail"], tuple(f["cites"]))
        for f in data["findings"]
    )


def _checkpoint(name: str, monkeypatch: pytest.MonkeyPatch) -> tuple[AuditResult, dict[str, Any]]:
    """The checkpoint's audit as the feed builds it, from the sealed evidence."""
    data = json.loads(FIXTURE.read_text())[name]
    findings = _findings(data)
    sealed = {"sources": data["sources"], "changed": data["changed"]}
    monkeypatch.setattr(feed, "coverage_evidence", lambda tree, baseline, detail: sealed)
    words = feed._coverage_words(Path("snapshot"), "baseline", findings)
    return AuditResult(data["point"], data["tree"], findings, coverage=words), data


def _coverage(result: AuditResult) -> Finding:
    return next(f for f in result.findings if f.gate == "coverage")


def _rows(text: str) -> list[str]:
    return [line for line in text.splitlines() if line.startswith("  - ")]


def _whole_spared_list(head: str) -> list[str]:
    """The spared definitions a heading's record bracket names: read off a
    sealed heading, every definition the gate spared; read off a capped heading,
    only the ones it named (#101). A heading with no such field fails the split
    that reads it, which never reads as an empty list."""
    return spared_definitions(SPARED_FIELD + head.split(SPARED_FIELD, 1)[1].rstrip("]"))


# The rows whose function's whole body the finding lists: the only ones for
# which "nothing exercises" is what the record holds. The run sealed that
# phrase on every row; on each other row the function's own changed body
# statements are neither listed nor in a spared definition, so they ran
# (checkpoint 1's transfer: 110, 111, 113 and 114 ran, 112 did not).
NEVER_RAN = {
    "checkpoint 1": {"money.py _unsupported"},
    "checkpoint 2": {
        "money.py _unsupported",
        "scratch_verify.py module level",
        *(
            f"scratch_verify.py {f}"
            for f in (
                "check",
                "expect_raises",
                "r2_coercion",
                "r3_rounding",
                "r4_accounts",
                "r5_fees",
                "r6_report",
                "r7_store",
                "misc",
            )
        ),
    },
}


def _as_now(name: str, sealed: str) -> list[str]:
    """The sealed rows with the phrase the renderer now writes: a function
    that ran is one "no test reaches these lines of"."""
    return [
        row
        if row[4:].split(":")[0] in NEVER_RAN[name]
        else row.replace("-- nothing exercises ", "-- no test reaches these lines of ", 1)
        for row in _rows(sealed)
    ]


@pytest.mark.parametrize("name", ["checkpoint 1", "checkpoint 2"])
def test_the_fixture_renders_the_rows_the_run_sealed(
    name: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Fidelity: every row is the one the run showed, under the recorded
    heading with its spared-definition list capped for the model (#101)."""
    result, data = _checkpoint(name, monkeypatch)
    sealed = data["sealed_coverage_text"]
    assert result.coverage.splitlines()[0] == CP_HEADS[name]
    assert _rows(result.coverage) == _as_now(name, sealed)
    assert sum("-- nothing exercises " in r for r in _rows(result.coverage)) == len(NEVER_RAN[name])
    assert len(_rows(sealed)) > 7


@pytest.mark.parametrize("name", ["checkpoint 1", "checkpoint 2"])
def test_the_recorded_header_names_three_spared_definitions_and_counts_the_fourth(
    name: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Known-bad (#101): the recorded header quoted all four spared baseline
    definitions -- 234 and 236 characters, 111 of them a list of definition
    names the model cannot act on -- inside the `feed.DETAIL_CHARS` a finding
    gets, and its rows reached the model only six deep (of nine for checkpoint
    1, of nineteen for checkpoint 2). Known-good in the same test: the cap is
    the rendering's -- the finding's cites, which the auditor seals and the
    packet reads back, still name all four."""
    result, data = _checkpoint(name, monkeypatch)
    sealed_head = data["sealed_coverage_text"].splitlines()[0]
    shown = feed._worded(result, _coverage(result))
    head = shown.splitlines()[0]
    assert len(sealed_head) > 230, sealed_head
    assert len(head) < len(sealed_head)
    assert len(head) < feed.DETAIL_CHARS // 4, head
    assert len(head.split("spared-defs=")[1].rstrip("]").split(",")) == 3
    assert head.endswith(" and 1 more]")
    assert SPARED_NAMES[3] not in head
    assert spared_definitions(_coverage(result).cites[-1]) == list(SPARED_NAMES)
    assert len(shown) <= feed.DETAIL_CHARS + len(" ...")


@pytest.mark.parametrize("name", ["checkpoint 1", "checkpoint 2"])
def test_the_recorded_header_spares_its_definitions_of_the_judgement(
    name: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Known-good (#83, kept): the sealed finding's own `basis` cite reaches the
    heading unchanged, and the list it seals names every spared definition --
    all four. Capping is the model-facing rendering's business, not the
    record's: the cite the auditor sealed is the one from this run's tree."""
    result, data = _checkpoint(name, monkeypatch)
    sealed_head = data["sealed_coverage_text"].splitlines()[0]
    bracket = sealed_head.split("[record: ", 1)[1].rstrip("]")
    assert _coverage(result).cites[-1] == bracket.split("; ", 1)[1]
    assert all(def_name in sealed_head for def_name in SPARED_NAMES)
    assert _whole_spared_list(sealed_head) == list(SPARED_NAMES)


@pytest.mark.parametrize("name", ["checkpoint 1", "checkpoint 2"])
def test_the_recorded_heading_reaches_the_model_with_the_list_cut_to_three(
    name: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The whole of what moved in the recorded heading, spelled out (#101): the
    heading the model reads is the recorded heading with its spared-definition
    list cut to the first three names and the rest counted -- same heading, same
    counts, same bracket, nothing else changed. The three comparisons that read
    this fixture (`test_the_fixture_renders_the_rows_the_run_sealed`,
    `test_checkpoint_1_still_leads_with_its_heading_and_first_rows` and
    `test_checkpoint_2_reads_the_tally_under_its_heading_largest_first`) now
    expect that heading, and every row under it is still the sealed row."""
    result, data = _checkpoint(name, monkeypatch)
    sealed_head = data["sealed_coverage_text"].splitlines()[0]
    head = result.coverage.splitlines()[0]
    names = _whole_spared_list(sealed_head)
    assert names == list(SPARED_NAMES)
    assert head == sealed_head.replace(
        f"{SPARED_FIELD}{','.join(names)}",
        f"{SPARED_FIELD}{','.join(names[:3])} and {len(names) - 3} more",
    )
    assert head == CP_HEADS[name]
    assert _rows(result.coverage) == _as_now(name, data["sealed_coverage_text"])


@pytest.mark.parametrize(
    ("name", "rows", "readable"),
    [("checkpoint 1", 9, 6), ("checkpoint 2", 19, 6)],
)
def test_the_cap_bounds_the_header_and_not_the_whole_finding(
    name: str, rows: int, readable: int, monkeypatch: pytest.MonkeyPatch
) -> None:
    """What the cap does and does not do. It bounds the one part of the heading
    whose length grows with the tree; the recorded finding's own rows are long,
    so `feed`'s cap still leaves six of nine, or six of nineteen, in front of
    the model. Pinned so a later change to that budget reads as its own change
    rather than as this cap doing more than it does."""
    result = _checkpoint(name, monkeypatch)[0]
    shown = feed._worded(result, _coverage(result))
    assert len(_rows(result.coverage)) == rows
    assert sum(1 for line in shown.splitlines() if _rows(line)) == readable
    assert len(shown) >= feed.DETAIL_CHARS, shown


def test_checkpoint_2_names_the_file_that_carries_most_of_the_count(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Known-bad: the capped text named scratch_verify.py 0 times."""
    result, _ = _checkpoint("checkpoint 2", monkeypatch)
    shown = feed._worded(result, _coverage(result))
    assert shown.endswith(" ...")  # the cap is in force
    assert "scratch_verify.py" in shown
    assert "scratch_verify.py" in render(result)


def test_checkpoint_1_still_leads_with_its_heading_and_first_rows(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Known-good: product rows only; heading first, the first rows visible."""
    result, data = _checkpoint("checkpoint 1", monkeypatch)
    shown = feed._worded(result, _coverage(result))
    assert shown.startswith(CP_HEADS["checkpoint 1"] + "\n")
    for row in _as_now("checkpoint 1", data["sealed_coverage_text"])[:4]:
        assert row in shown
    assert "scratch" not in shown


def test_a_single_file_finding_reads_as_before(monkeypatch: pytest.MonkeyPatch) -> None:
    """Known-good: one file, so there is nothing to tally; the text is unchanged."""
    source = [
        "def g():",
        '    """Return the rate."""',
        "    return 7",
        "",
        "def h():",
        "    return 8",
    ]
    cov = Finding(
        "coverage", 1, "fail", "evidence-thin", "no test runs u.py:1, u.py:3, u.py:6", ("c",)
    )
    sealed = {"sources": {"u.py": source}, "changed": [["u.py", 1], ["u.py", 3], ["u.py", 6]]}
    monkeypatch.setattr(feed, "coverage_evidence", lambda tree, baseline, detail: sealed)
    words = feed._coverage_words(Path("snapshot"), "baseline", (cov,))
    summary = coverage_text.describe_coverage(
        {"detail": cov.detail, "cites": list(cov.cites)},
        {"u.py": "\n".join(source) + "\n"},
        [("u.py", 1), ("u.py", 3), ("u.py", 6)],
    )
    assert words == coverage_text.render_coverage(summary, text=False).rstrip("\n")
    assert len(_rows(words)) == 2


CP2_TALLY = (
    "  Lines per file, most first: scratch_verify.py 223, money.py 17, accounts.py 5, store.py 1"
)


def test_checkpoint_2_reads_the_tally_under_its_heading_largest_first(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    result, data = _checkpoint("checkpoint 2", monkeypatch)
    shown = feed._worded(result, _coverage(result))
    heading, tally, first_row = shown.splitlines()[:3]
    assert heading == CP_HEADS["checkpoint 2"]
    assert tally == CP2_TALLY
    assert first_row == _as_now("checkpoint 2", data["sealed_coverage_text"])[0]


def test_checkpoint_1_reads_the_same_tally_without_the_scratch_file(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    result, _ = _checkpoint("checkpoint 1", monkeypatch)
    assert result.coverage.splitlines()[1] == (
        "  Lines per file, most first: money.py 17, accounts.py 5, store.py 1"
    )


def _summary(counts: dict[str, int], unplaced: tuple[tuple[str, int], ...] = ()) -> Any:
    gaps = tuple(
        coverage_text.FunctionGap(f, "f", None, tuple(range(1, n + 1)), None)
        for f, n in counts.items()
    )
    total = sum(counts.values()) + len(unplaced)
    return coverage_text.CoverageSummary(total, (), gaps, unplaced)


def test_many_files_name_the_largest_and_count_the_rest() -> None:
    """The largest file is last in path order; the tally still leads with it."""
    counts = {f"a{i}.py": 1 for i in range(8)} | {"z_scratch.py": 90, "m.py": 2}
    tally = coverage_text.file_tally(_summary(counts))
    assert tally == (
        "  Lines per file, most first: z_scratch.py 90, m.py 2, a0.py 1, a1.py 1, a2.py 1,"
        " a3.py 1; and 4 more files (4 lines)"
    )
    one_more = coverage_text.file_tally(_summary(counts), top=9)
    assert one_more.endswith("a6.py 1; and 1 more file (1 line)")
    # realistic worst case: 40-character paths, the tally stays well inside the cap
    wide = {f"src/package/subpackage/module_{i:03d}.py": 10 + i for i in range(40)}
    assert len(coverage_text.file_tally(_summary(wide))) < feed.DETAIL_CHARS // 2


def test_unplaced_lines_count_under_their_file() -> None:
    tally = coverage_text.file_tally(_summary({"u.py": 1}, (("gone.py", 3), ("gone.py", 4))))
    assert tally == "  Lines per file, most first: gone.py 2, u.py 1"
    assert coverage_text.file_tally(_summary({"u.py": 5})) == ""
