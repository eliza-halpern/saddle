"""Tier 1 runs the project's suite once: it takes no red-phase baseline sample.

Contract: `runner.run_node_gate(tier2=False)`, the auditor's checkpoint tier,
runs the node's test command once, on the tree; no tier-1 finding reads
red-phase, so no baseline sample is taken, and `check_red_phase` returns a
placeholder that says so. Tier 2 samples the baseline exactly as before.

Why: one baseline sample is one more run of the whole suite. On saddle's own
repository a tier-1 audit took 4742 s in a 2-core slot, two runs of about
2300 s each, for a check the tier never reports.

Known-good: the tier-1 findings of a tree whose new test is red on the
baseline and of one whose new test is not are what they were (red-phase is
in neither), and tier 2 still tells them apart. Known-bad: a tier-1 run that
samples the baseline again shows a second suite run in the journal.
"""

from __future__ import annotations

from pathlib import Path
from typing import Final

from saddle.audit import audit_node, staged_copy
from saddle.auditor import TIER1, Auditor
from saddle.evidence import MutationOutcome, run_argv
from saddle.gates import RED_PHASE_NOT_SAMPLED, RED_PHASE_SAMPLES, GateCheck, check_red_phase
from saddle.journal import SpanRecorder, read_spans
from saddle.runner import run_node_gate

CODE: Final = "def neg(x):\n    return x\n"
FIXED: Final = "def neg(x):\n    return -x\n"
OLD_TEST: Final = "from n import neg\n\n\ndef test_zero():\n    assert neg(0) == 0\n"
RED_TEST: Final = OLD_TEST + "\n\ndef test_one():\n    assert neg(1) == -1\n"
"""A new test the baseline fails and the fix passes: red-phase passes."""
VACUOUS_TEST: Final = OLD_TEST + "\n\ndef test_zero_again():\n    assert neg(0) == 0\n"
"""A new test the baseline already passes: red-phase fails."""


def _tree(root: Path, test: str) -> Path:
    root.mkdir(parents=True)
    (root / "n.py").write_text(CODE)
    (root / "test_n.py").write_text(OLD_TEST)
    for argv in (
        ["init", "-q"],
        ["add", "-A"],
        ["-c", "user.email=t@e", "-c", "user.name=t", "commit", "-qm", "base"],
    ):
        assert run_argv(["git", *argv], root) == 0
    (root / "n.py").write_text(FIXED)
    (root / "test_n.py").write_text(test)
    return root


def _suite_runs(root: Path, *, tier2: bool, journal: Path) -> int:
    with staged_copy(root, "HEAD") as (copy, _staged, resolved):
        run_node_gate(
            audit_node(),
            copy,
            baseline=resolved,
            recorder=SpanRecorder(path=journal, node_id="n"),
            tier2=tier2,
        )
    return sum("pytest" in span.argv for span in read_spans(journal))


def test_tier_one_runs_the_suite_once_and_tier_two_samples_the_baseline(tmp_path: Path) -> None:
    root = _tree(tmp_path / "t", RED_TEST)
    assert _suite_runs(root, tier2=False, journal=tmp_path / "one.jsonl") == 1
    assert _suite_runs(root, tier2=True, journal=tmp_path / "two.jsonl") == 1 + RED_PHASE_SAMPLES


def test_tier_one_findings_do_not_need_the_sample_and_tier_two_still_reads_it(
    tmp_path: Path,
) -> None:
    red = Auditor(_tree(tmp_path / "red", RED_TEST))
    vacuous = Auditor(_tree(tmp_path / "vacuous", VACUOUS_TEST))
    for auditor in (red, vacuous):
        first = auditor.tier1()
        assert [f.gate for f in first.findings] == list(TIER1)
        assert {f.gate: f.verdict for f in first.findings}["tests"] == "pass"
    phases = [
        next(f for f in auditor.tier2().findings if f.gate == "red-phase")
        for auditor in (red, vacuous)
    ]
    assert [(f.verdict, f.detail) for f in phases] == [
        ("pass", "fail pre-change, pass post-change"),
        ("fail", "tests pass pre-change; prove nothing"),
    ]


def test_no_sample_is_a_placeholder_never_a_baseline_verdict() -> None:
    """Only a run that changed a test reads the samples, and with none it
    says so instead of reading a baseline it never ran."""
    tests = GateCheck(name="tests", passed=True)
    mutation = MutationOutcome(killed=0, total=0, generated=0, survivors=())
    for kind in ("impl", "refactor"):
        found = check_red_phase(
            (),
            lambda: 0,
            baseline_output="",
            changed_files=[],
            tests_changed=True,
            kind=kind,
            coverage=tests,
            mutation=mutation,
        )
        assert found == GateCheck(name="red-phase", passed=False, detail=RED_PHASE_NOT_SAMPLED)
    unchanged = check_red_phase(
        (),
        lambda: 0,
        baseline_output="",
        changed_files=[],
        tests_changed=False,
        kind="refactor",
        coverage=tests,
        mutation=mutation,
    )
    assert unchanged.detail == "tests unchanged and no mutants decided; nothing proves the change"
