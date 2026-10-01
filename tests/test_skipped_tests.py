"""Every skipped test is listed as not proven, and a packet never says "nothing
is left unproven" while any finding is not proven.

Known-good: a suite that skips one test and expects another to fail gives the
audit a `skipped-tests` finding naming both ids with their reasons, which
refuses nothing; a suite that skips nothing gives none, and a packet over a
run whose findings all passed still reads "Nothing is left unproven."
Known-bad: a skip report that cannot be read is "could not be determined",
never zero; a packet with a not-proven finding of any gate never prints the
sentence.
"""

from __future__ import annotations

import json
import uuid
from pathlib import Path
from typing import Any

import pytest
from test_auditor import _init

from saddle import auditor as auditor_mod
from saddle.auditor import SKIPPED_GATE, Auditor, AuditorConfig, skipped_detail
from saddle.evidence import (
    SKIP_REPORT_NAME,
    SkippedTests,
    read_skip_report,
    skip_report_options,
    skip_reportable,
)
from saddle.impact import ImpactMemo
from saddle.journal import append_span, build_span, write_attempt_sidecar
from saddle.packet import compile_packet

BASE = "def f():\n    return 1\n"
SKIPS = (
    "import pytest\n\n\n"
    '@pytest.mark.skip(reason="needs a browser")\n'
    "def test_browser(): pass\n\n\n"
    '@pytest.mark.xfail(reason="known bug in the parser")\n'
    "def test_known(): assert False\n\n\n"
    "class TestGroup:\n"
    '    @pytest.mark.skipif(True, reason="no display")\n'
    "    def test_inside(self): pass\n"
)


def project(tmp_path: Path, *, skips: bool, conftest: str = "") -> Path:
    root = tmp_path / "tree"
    _init(
        root,
        {"n.py": BASE, "test_n.py": "from n import f\n\n\ndef test_f():\n    assert f() == 1\n"},
    )
    (root / "n.py").write_text("def f():\n    return 2\n")
    (root / "test_n.py").write_text("from n import f\n\n\ndef test_f():\n    assert f() == 2\n")
    if skips:
        (root / "test_skips.py").write_text(SKIPS)
    if conftest:
        (root / "conftest.py").write_text(conftest)
    return root


def skipped_finding(root: Path, config: AuditorConfig | None = None) -> Any:
    found = Auditor(root, "HEAD", config or AuditorConfig()).tier2()
    return found, next((f for f in found.findings if f.gate == SKIPPED_GATE), None)


# -- the audit ----------------------------------------------------------------


def test_a_skipped_and_an_xfailed_test_are_named_and_refuse_nothing(tmp_path: Path) -> None:
    found, finding = skipped_finding(project(tmp_path, skips=True))
    assert finding is not None
    assert (finding.verdict, finding.tier) == ("not-proven", 2)
    assert "2 skipped, 1 expected to fail (xfail) in the suite run" in finding.detail
    assert "test_skips.py::test_browser (needs a browser)" in finding.detail
    assert "test_skips.py::TestGroup::test_inside (no display)" in finding.detail
    assert "test_skips.py::test_known [xfail] (known bug in the parser)" in finding.detail
    assert found.passed


def test_a_suite_that_skips_nothing_adds_no_finding(tmp_path: Path) -> None:
    found, finding = skipped_finding(project(tmp_path, skips=False))
    assert finding is None
    assert found.passed


def test_an_unreadable_report_is_could_not_be_determined_not_zero(tmp_path: Path) -> None:
    # The project's own conftest removes pytest's report as pytest shuts down,
    # as a run cut short would leave none.
    conftest = (
        "import os\n\n\n"
        "def pytest_unconfigure(config):\n"
        f"    if os.path.exists({SKIP_REPORT_NAME!r}):\n"
        f"        os.remove({SKIP_REPORT_NAME!r})\n"
    )
    found, finding = skipped_finding(project(tmp_path, skips=False, conftest=conftest))
    assert finding is not None
    assert finding.verdict == "not-proven"
    assert "could not be determined" in finding.detail
    assert "pytest wrote no report" in finding.detail
    assert "not as none skipped" in finding.detail
    assert found.passed


def test_an_impact_scoped_run_says_which_run_it_reports(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(auditor_mod, "_selection", lambda *_a: ("test_n.py", "test_skips.py"))
    _, finding = skipped_finding(project(tmp_path, skips=True), AuditorConfig(impact=ImpactMemo()))
    assert finding is not None
    assert "in the impact-scoped run (2 test file(s) the change can reach;" in finding.detail
    assert "2 skipped, 1 expected to fail (xfail)" in finding.detail


def test_the_whole_list_is_sealed_with_the_finding(tmp_path: Path) -> None:
    journal = tmp_path / "ledger" / "proofs.jsonl"
    root = project(tmp_path, skips=True)
    skipped_finding(root, AuditorConfig(journal=journal))
    from saddle.journal import read_spans

    (span,) = [s for s in read_spans(journal) if s.name == f"audit-tier2:{SKIPPED_GATE}"]
    assert span.exit_code == 0  # not-proven journals as a pass does, and is read by verdict
    sidecar = json.loads(
        next(
            p for p in (journal.parent / "attempts").iterdir() if p.stem == span.span_id
        ).read_text()
    )
    assert sorted(i for i, _ in sidecar["skipped"]) == [
        "test_skips.py::TestGroup::test_inside",
        "test_skips.py::test_browser",
    ]
    assert sidecar["xfailed"] == [["test_skips.py::test_known", "known bug in the parser"]]


# -- the wording and the cap ---------------------------------------------------


def test_a_long_list_names_six_and_counts_the_rest() -> None:
    many = tuple((f"tests/t.py::test_{i}", "no browser") for i in range(15))
    said = skipped_detail(SkippedTests(known=True, skipped=many), "the suite run")
    assert said is not None
    assert "15 skipped in the suite run" in said
    assert "tests/t.py::test_5 (no browser), ... and 9 more." in said
    assert "test_6" not in said


def test_a_long_reason_is_cut_to_sixteen_words() -> None:
    reason = " ".join(f"w{i}" for i in range(30))
    said = skipped_detail(SkippedTests(known=True, skipped=(("t.py::t", reason),)), "the suite run")
    assert said is not None
    assert "w15 ...)" in said
    assert "w16" not in said


def test_no_skip_is_no_detail_and_an_unknown_one_is_still_a_detail() -> None:
    assert skipped_detail(SkippedTests(known=True), "the suite run") is None
    assert skipped_detail(SkippedTests(known=False, why="x"), "the suite run") is not None


# -- the report ---------------------------------------------------------------

REPORT = (
    "<testsuites><testsuite>"
    '<testcase classname="tests.test_a" name="test_ok" file="tests/test_a.py"/>'
    '<testcase classname="tests.test_a" name="test_p[1]" file="tests/test_a.py">'
    '<skipped type="pytest.skip" message="par &lt;skip&gt; &amp; q">x</skipped></testcase>'
    '<testcase classname="tests.test_a.TestC" name="test_in" file="tests/test_a.py">'
    '<skipped type="pytest.skip" message="dyn"/></testcase>'
    '<testcase classname="tests.test_a" name="test_xf" file="tests/test_a.py">'
    '<skipped type="pytest.xfail" message="known"/></testcase>'
    '<testcase classname="mod" name="test_nofile"><skipped type="pytest.skip" message=""/>'
    "</testcase>"
    '<testcase name="bare"><skipped type="pytest.skip" message="m"/></testcase>'
    "</testsuite></testsuites>"
)


def test_the_report_gives_ids_and_reasons_and_keeps_xfail_apart(tmp_path: Path) -> None:
    path = tmp_path / "r.xml"
    path.write_text(REPORT)
    got = read_skip_report(path)
    assert got.known
    assert got.skipped == (
        ("tests/test_a.py::test_p[1]", "par <skip> & q"),
        ("tests/test_a.py::TestC::test_in", "dyn"),
        ("mod.test_nofile", ""),
        ("bare", "m"),
    )
    assert got.xfailed == (("tests/test_a.py::test_xf", "known"),)


def test_a_missing_or_broken_report_is_known_false_with_a_reason(tmp_path: Path) -> None:
    missing = read_skip_report(tmp_path / "none.xml")
    assert (missing.known, missing.why) == (False, "pytest wrote no report")
    (tmp_path / "bad.xml").write_text("<testsuites><oops")
    broken = read_skip_report(tmp_path / "bad.xml")
    assert (broken.known, broken.why) == (False, "its report did not parse")
    assert not missing.skipped
    assert not broken.skipped


def test_only_a_pytest_command_without_its_own_junit_report_is_reportable(tmp_path: Path) -> None:
    assert skip_reportable("python -m pytest -q", tmp_path)
    assert skip_reportable("pytest tests", tmp_path)
    assert not skip_reportable("python -m unittest", tmp_path)
    assert not skip_reportable("python -m pytest --junitxml=out.xml", tmp_path)
    (tmp_path / "pytest.ini").write_text("[pytest]\naddopts = -q\n")
    assert skip_reportable("python -m pytest", tmp_path)
    (tmp_path / "pytest.ini").write_text("[pytest]\naddopts = -p no:junitxml\n")
    assert not skip_reportable("python -m pytest", tmp_path)
    assert skip_report_options(tmp_path / "r.xml")[-1] == f"--junitxml={tmp_path / 'r.xml'}"


# -- the packet's "Not proven" row ----------------------------------------------


def ledger(tmp_path: Path, findings: list[dict[str, Any]]) -> Path:
    """A finished run's ledger holding `findings` as the auditor seals them."""
    journal = tmp_path / "run" / "proofs.jsonl"
    start = build_span(
        node_id="chat#1",
        argv=["auto:start", "task"],
        duration_ms=0,
        exit_code=0,
        detail="arm E+A+F; branch b; test edits allowed",
        kind="agent",
    )
    append_span(journal, start)
    for finding in findings:
        sealed = finding.pop("sidecar", None)
        span_id = uuid.uuid4().hex
        append_span(
            journal,
            build_span(
                node_id="chat#1",
                argv=["saddle-audit", f"tier{finding['tier']}", finding["gate"], "k"],
                duration_ms=0,
                exit_code=1 if finding["verdict"] == "fail" else 0,
                detail=json.dumps(finding, sort_keys=True),
                name=f"audit-tier{finding['tier']}:{finding['gate']}",
                span_id=span_id,
                attempt_hash=write_attempt_sidecar(journal, span_id, sealed) if sealed else "",
            ),
        )
    span_id = uuid.uuid4().hex
    evidence = {
        "outcome": "finished",
        "reason": "finish called",
        "narrative": "done",
        "files_changed": ["n.py"],
        "rounds": 1,
        "tool_span_hashes": [],
        "tokens_spent": 10,
        "token_source": "usage",
        "token_budget": 0,
        "elapsed_s": 1.0,
        "time_budget_s": 0,
        "unresolved_findings": [],
    }
    append_span(
        journal,
        build_span(
            node_id="chat#1",
            argv=["auto:finished"],
            duration_ms=1000,
            exit_code=0,
            detail="finished: finish called; arm E+A+F",
            kind="agent",
            parent_id=start.span_id,
            span_id=span_id,
            attempt_hash=write_attempt_sidecar(journal, span_id, evidence),
        ),
    )
    return journal


def finding(gate: str, verdict: str, detail: str, tier: int = 2) -> dict[str, Any]:
    return {
        "gate": gate,
        "tier": tier,
        "verdict": verdict,
        "reason": "evidence-thin",
        "detail": detail,
        "cites": [],
    }


MUTATED = finding("mutation", "pass", "killed 8 of 9 changed-line mutants (88.9% >= 85.0%)")
TESTED = finding("tests", "pass", "'python -m pytest -q' exited 0", tier=1)


def not_proven_row(tmp_path: Path, findings: list[dict[str, Any]]) -> Any:
    journal = ledger(tmp_path, findings)
    return {r.key: r for r in compile_packet(journal, run_id="r").rows}["not-proven"]


def test_every_finding_proven_still_reads_nothing_is_left_unproven(tmp_path: Path) -> None:
    row = not_proven_row(tmp_path, [TESTED, MUTATED])
    assert row.text == "Nothing is left unproven."
    assert row.items == ()


def test_a_skipped_tests_finding_is_named_in_the_row(tmp_path: Path) -> None:
    said = "2 skipped in the suite run: t.py::a (no browser), t.py::b (no browser). Not proven."
    row = not_proven_row(tmp_path, [TESTED, MUTATED, finding(SKIPPED_GATE, "not-proven", said)])
    assert row.text == "What this packet cannot vouch for:"
    assert row.items == (f"{SKIPPED_GATE} (tier 2): {said}",)


@pytest.mark.parametrize("gate", ["coverage", "mutation", "skipped-tests", "a-gate-added-later"])
def test_a_not_proven_finding_of_any_gate_removes_the_sentence(tmp_path: Path, gate: str) -> None:
    tier = 1 if gate == "coverage" else 2
    others = [f for f in (TESTED, MUTATED) if f["gate"] != gate]
    row = not_proven_row(tmp_path, [*others, finding(gate, "not-proven", "x\nsecond line", tier)])
    assert row.text != "Nothing is left unproven."
    assert row.items == (f"{gate} (tier {tier}): x",)


def test_a_finding_the_row_already_itemises_is_not_named_twice(tmp_path: Path) -> None:
    asked = finding("task-requirements", "not-proven", "2 of 5 units not judged", tier=1)
    asked["sidecar"] = {"unjudged": ["unit one", "unit two"]}
    row = not_proven_row(tmp_path, [TESTED, MUTATED, asked])
    assert row.items == (
        "Not judged by the task-text check: unit one.",
        "Not judged by the task-text check: unit two.",
    )


def test_the_suite_run_writes_the_report_only_for_a_pytest_command(tmp_path: Path) -> None:
    from saddle.evidence import SuiteRun, run_suite_capture

    (tmp_path / "test_x.py").write_text("def test_x(): pass\n")
    report = tmp_path / SKIP_REPORT_NAME
    done = run_suite_capture(
        SuiteRun(),
        "python -c pass",
        tmp_path,
        str(tmp_path / ".cov"),
        timeout=60,
        skip_report=report,
    )
    assert done.exit_code == 0
    assert not report.exists()
    done = run_suite_capture(
        SuiteRun(),
        "python -m pytest -q -p no:cacheprovider",
        tmp_path,
        str(tmp_path / ".cov"),
        timeout=60,
        skip_report=report,
    )
    assert done.exit_code == 0
    assert read_skip_report(report) == SkippedTests(known=True)
