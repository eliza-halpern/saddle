"""A change the mutation and coverage checks cannot measure is listed as not
proven, never refused and never padded.

Known-good: a change that touches only a browser script and a stylesheet is
accepted with a `not-measurable` finding naming both files, the packet's Not
proven row names them, and no finding refuses it for lacking Python. A change
that also touches Python keeps its measured verdict, worded as covering the
Python lines only. A change of Python alone lists nothing and the packet still
reads "Nothing is left unproven."
Known-bad (what the rule must not let through): Python source that changed and
that mutation could not reach still fails beside a stylesheet, and so does an
engine that failed on a browser-only change.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from test_auditor import _init
from test_skipped_tests import ledger

from saddle import auditor as auditor_mod
from saddle import runner
from saddle.auditor import (
    NOT_MEASURABLE_GATE,
    Auditor,
    AuditorConfig,
    Findings,
    finding_body,
    not_measurable_detail,
    unmeasurable_files,
)
from saddle.evidence import MutationOutcome
from saddle.packet import compile_packet

NOTHING = MutationOutcome(killed=0, total=0, generated=0, survivors=())
EIGHT_OF_NINE = MutationOutcome(killed=8, total=9, generated=9, survivors=("m",))

FILES = {
    "n.py": "def f():\n    return 1\n",
    "test_n.py": "from n import f\n\n\ndef test_f():\n    assert f() == 1\n",
    "static/markdown.js": "export const copy = 1;\n",
    "static/app.css": ".copy { color: red; }\n",
}


def tree(tmp_path: Path, *, python: bool = False, browser: bool = True) -> Path:
    """The baseline above with its browser files changed, and `n.py` too when `python`."""
    root = tmp_path / "tree"
    (root / "static").mkdir(parents=True)
    _init(root, FILES)
    if browser:
        (root / "static/markdown.js").write_text("export const copy = 2;\n")
        (root / "static/app.css").write_text(".copy { color: blue; }\n")
    if python:
        (root / "n.py").write_text("def f():\n    return 2\n")
        (root / "test_n.py").write_text(FILES["test_n.py"].replace("== 1", "== 2"))
    return root


def sampled(outcome: MutationOutcome, monkeypatch: pytest.MonkeyPatch) -> None:
    def fake(*_args: Any, **_kwargs: Any) -> MutationOutcome:
        return outcome

    monkeypatch.setattr(runner, "mutation_sample", fake)


def verdicts(found: Findings) -> dict[str, str]:
    return {f.gate: f.verdict for f in found.findings}


def detail(found: Findings, gate: str) -> str:
    return next(f.detail for f in found.findings if f.gate == gate)


# -- a browser-only change -----------------------------------------------------


def test_a_browser_only_change_is_accepted_with_its_files_listed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    sampled(NOTHING, monkeypatch)
    found = Auditor(tree(tmp_path)).tier2()
    got = verdicts(found)
    # The two refusals the real run met, "no mutants on changed lines" and
    # "tests unchanged and no mutants decided", are gone: no Python changed, so
    # Python's mutation and red-phase findings are not shown (languages.visible).
    # What the change cannot vouch for is still said, by not-measurable.
    assert "mutation" not in got
    assert "red-phase" not in got
    assert got[NOT_MEASURABLE_GATE] == "not-proven"
    assert "fail" not in got.values()
    assert found.passed
    listed = detail(found, NOT_MEASURABLE_GATE)
    assert listed.startswith(
        "not mutation-measurable: static/app.css, static/markdown.js (2 files;"
    )
    assert "measure Python only" in listed
    assert "is a defect" in listed


def test_a_browser_change_with_only_a_python_test_edit_says_no_source_line_changed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # A Python test file is Python, so the Python findings are shown; with no
    # Python source changed they say why they measured nothing, never refuse.
    sampled(NOTHING, monkeypatch)
    root = tree(tmp_path)
    (root / "test_n.py").write_text("# f is the module's one function\n" + FILES["test_n.py"])
    found = Auditor(root).tier2()
    got = verdicts(found)
    assert (got["mutation"], got["red-phase"]) == ("not-proven", "not-proven")
    assert "fail" not in got.values()
    assert "no Python source line changed" in detail(found, "mutation")
    assert NOT_MEASURABLE_GATE in detail(found, "red-phase")


def test_the_packet_names_the_files_and_never_says_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    sampled(NOTHING, monkeypatch)
    found = Auditor(tree(tmp_path)).tier2()
    journal = ledger(tmp_path, [finding_body(f) for f in found.findings])
    rows = {r.key: r for r in compile_packet(journal, run_id="r").rows}
    row = rows["not-proven"]
    assert row.text == "What this packet cannot vouch for:"
    assert any("static/app.css, static/markdown.js" in item for item in row.items)
    assert not any(r.status == "failed" for r in rows.values())


def test_the_whole_list_is_sealed_and_the_display_is_capped(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    sampled(NOTHING, monkeypatch)
    root = tree(tmp_path)
    for index in range(9):
        (root / "static" / f"extra{index}.html").write_text("<p></p>\n")
    from saddle.journal import read_spans

    journal = tmp_path / "ledger" / "proofs.jsonl"
    found = Auditor(root, "HEAD", AuditorConfig(journal=journal)).tier2()
    shown = detail(found, NOT_MEASURABLE_GATE)
    assert "(11 files;" in shown
    assert ", ... and 5 more (11 files" in shown
    assert "extra8.html" not in shown
    (span,) = [s for s in read_spans(journal) if s.name == f"audit-tier2:{NOT_MEASURABLE_GATE}"]
    sidecar = next((journal.parent / "attempts").glob(f"{span.span_id}*")).read_text()
    assert "extra8.html" in sidecar


def test_an_engine_that_failed_on_a_browser_only_change_is_never_a_pass(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A Python engine that crashed on a change with no Python in it measured
    none of the change's lines: under #169 it is not proven, which
    `languages.visible` shows only where Python changed, so it is not listed;
    it never reads as a mutation pass, and not-measurable still says what is
    unproven. Before #169 the crash refused the tree."""
    broken = MutationOutcome(killed=0, total=0, generated=0, survivors=("mutmut run exited 2",))
    sampled(broken, monkeypatch)
    found = Auditor(tree(tmp_path)).tier2()
    got = verdicts(found)
    assert "mutation" not in got
    assert got[NOT_MEASURABLE_GATE] == "not-proven"
    assert "fail" not in got.values()


def test_a_crashed_engine_is_not_proven_and_seals_its_output(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """#169: a correct tree (9 of 9 on its sealed oracle) was refused on a
    mutmut crash no edit could clear. A crashed engine is not proven, never a
    refusal and never a pass; the detail names the crash and says it is not
    the change's; the engine's output is sealed with the finding."""
    from saddle.journal import read_spans

    crash = MutationOutcome(
        killed=0,
        total=0,
        generated=0,
        survivors=("mutmut run exited 1: KeyError: 10062",),
        tool_output=("Traceback (most recent call last):", '  File "m.py", line 3', "KeyError"),
    )
    sampled(crash, monkeypatch)
    root = tree(tmp_path, python=True, browser=False)
    journal = tmp_path / "ledger" / "proofs.jsonl"
    found = Auditor(root, "HEAD", AuditorConfig(journal=journal)).tier2()
    assert verdicts(found)["mutation"] == "not-proven"
    shown = detail(found, "mutation")
    assert shown.startswith("mutation tool failed: mutmut run exited 1: KeyError: 10062")
    assert "not a finding against the change" in shown
    (span,) = [s for s in read_spans(journal) if s.name == "audit-tier2:mutation"]
    sealed = json.loads(next((journal.parent / "attempts").glob(f"{span.span_id}*")).read_text())
    assert sealed["tool_output"] == list(crash.tool_output)


def test_a_red_suite_under_the_engine_still_refuses(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The known-bad case #169 must not admit: mutmut failing because the
    suite is red is the change's to fix, so it stays a refusal."""
    red = MutationOutcome(
        killed=0,
        total=0,
        generated=0,
        survivors=("suite is red: mutmut run exited 1: failed to collect stats",),
    )
    sampled(red, monkeypatch)
    found = Auditor(tree(tmp_path, python=True, browser=False)).tier2()
    assert verdicts(found)["mutation"] == "fail"
    assert detail(found, "mutation").startswith("mutation not measured: suite is red")


# -- a change that mixes Python and other files ----------------------------------


def test_a_mixed_change_keeps_its_measured_verdict_scoped_to_the_python_lines(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    sampled(EIGHT_OF_NINE, monkeypatch)
    found = Auditor(tree(tmp_path, python=True)).tier2()
    said = detail(found, "mutation")
    assert said.startswith("killed 8 of 9 changed-line mutants (88.9% >= 85.0%)")
    assert said.endswith(
        "; Python lines only, 2 changed non-Python file(s) not measurable "
        f"(listed under {NOT_MEASURABLE_GATE})"
    )
    assert verdicts(found)["mutation"] == "pass"
    listed = detail(found, NOT_MEASURABLE_GATE)
    assert "static/app.css, static/markdown.js (2 files;" in listed
    assert "n.py" not in listed
    assert found.passed


def test_python_source_mutation_could_not_reach_still_fails_beside_a_stylesheet(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    sampled(NOTHING, monkeypatch)
    found = Auditor(tree(tmp_path, python=True)).tier2()
    got = verdicts(found)
    assert got["mutation"] == "fail"
    assert detail(found, "mutation").startswith(
        "no mutants on changed lines: mutation provided no evidence; Python lines only"
    )
    assert got[NOT_MEASURABLE_GATE] == "not-proven"
    assert not found.passed


# -- a change of Python alone: the good case stays good --------------------------


def test_a_python_only_change_lists_nothing_and_the_packet_still_says_nothing_is_left(
    tmp_path: Path,
) -> None:
    found = Auditor(tree(tmp_path, python=True, browser=False)).tier2()
    assert NOT_MEASURABLE_GATE not in verdicts(found)
    assert "Python lines only" not in detail(found, "mutation")
    assert found.passed
    journal = ledger(tmp_path, [finding_body(f) for f in found.findings])
    row = {r.key: r for r in compile_packet(journal, run_id="r").rows}["not-proven"]
    assert row.text == "Nothing is left unproven."
    assert row.items == ()


# -- the lookup ------------------------------------------------------------------


def test_the_measurable_suffixes_are_one_lookup(monkeypatch: pytest.MonkeyPatch) -> None:
    files = ["a.py", "B.PY", "web/app.js", "web/app.css", "check.sh", "LICENSE", "x/y.html"]
    without_js = ["LICENSE", "check.sh", "web/app.css", "web/app.js", "x/y.html"]
    # flip: StrykerJS measures `.js`, so the default lookup no longer lists it; the
    # old expectation is kept as the case of a tree without the tool and as the
    # lookup narrowed to Python.
    assert unmeasurable_files(files) == ["LICENSE", "check.sh", "web/app.css", "x/y.html"]
    assert unmeasurable_files(files, {".py": "Python"}) == without_js
    assert unmeasurable_files([]) == []
    assert "measure JavaScript and Python only" in not_measurable_detail(["web/app.css"])
    assert "measure Python only" in not_measurable_detail(["web/app.css"], {".py": "Python"})
    assert "(1 file;" in not_measurable_detail(["web/app.css"])
    monkeypatch.setattr(auditor_mod, "MEASURABLE_SUFFIXES", {".py": "Python"})
    assert unmeasurable_files(files) == without_js


def test_the_model_reads_that_padding_the_check_is_a_defect(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from saddle.feed import AuditResult, render

    sampled(NOTHING, monkeypatch)
    found = Auditor(tree(tmp_path)).tier2()
    text = render(AuditResult("finish", "t" * 12, found.findings))
    assert text.startswith("[audit finish on tree tttttttttttt: PASS]")
    (line,) = [ln for ln in text.splitlines() if f" {NOT_MEASURABLE_GATE} " in ln]
    assert line.startswith("(not proven, does not refuse) not-measurable (tier 2): ")
    assert "Recorded as not proven and not refused" in line
    assert "code added only to give those checks something to measure is a defect" in line


def test_a_new_python_test_that_passes_before_the_change_still_fails_red_phase(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Only the "tests unchanged and no mutants decided" refusal is lifted: a test
    # that proves nothing about the change is still refused.
    sampled(NOTHING, monkeypatch)
    root = tree(tmp_path)
    (root / "test_new.py").write_text("def test_new():\n    assert 1 + 1 == 2\n")
    found = Auditor(root).tier2()
    got = verdicts(found)
    assert got["mutation"] == "not-proven"
    assert got["red-phase"] == "fail"
    assert detail(found, "red-phase") == "tests pass pre-change; prove nothing"
    assert not found.passed
