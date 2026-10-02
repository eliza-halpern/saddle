"""`[tool.saddle] gate-checks` through the auditor: the project's gate stages on head and base.

Known-good: a project whose stages the head passes gets a passing
`project-gate` finding and a packet line `Gate: base ✓, head ✓`. Known-bad: a
stage the head broke after the base passed it fails the finding and names the
stage; a stage red at the base too, or whose tool is missing, is not proven;
the audited tree cannot weaken the list it is judged by; the baseline's runs
are asked once per baseline.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest
from test_auditor import BASE_CODE, FIXED_CODE, TEST_BODY, _init

import saddle.auditor as auditor_module
from saddle.audit import AuditError
from saddle.auditor import PROJECT_GATE, STATIC_CHECK, Auditor, AuditorConfig, Finding, Findings
from saddle.evidence import materialize_baseline
from saddle.journal import read_spans
from saddle.packet import compile_packet, render_packet_text

# A stand-in project linter: `checker.py WORD` fails on any .py file holding WORD,
# naming the file and line the way a linter does.
CHECKER = (
    "import pathlib, sys\n"
    "word = sys.argv[1]\n"
    "bad = [f'{p}:{i}: {word} found' for p in sorted(pathlib.Path('.').rglob('*.py'))\n"
    "       for i, line in enumerate(p.read_text().splitlines(), 1)\n"
    "       if word in line and p.name != 'checker.py']\n"
    "print('\\n'.join(bad)); sys.exit(1 if bad else 0)\n"
)
LINT = f'["{sys.executable}", "lintcheck.py", "BADLINT"]'
FMT = f'["{sys.executable}", "fmtcheck.py", "BADFMT"]'
STAGES = f"[{LINT}, {FMT}]"


def project(tmp_path: Path, *, base_extra: str = "", saddle: str | None = None) -> Path:
    """A committed project declaring two stages; the working tree fixes `f`."""
    tree = tmp_path / "tree"
    table = f"[tool.saddle]\ngate-checks = {STAGES}\n" if saddle is None else saddle
    files = {"lintcheck.py": CHECKER, "fmtcheck.py": CHECKER, "checker.py": CHECKER}
    _init(tree, {"n.py": BASE_CODE + base_extra, **files, "pyproject.toml": table})
    (tree / "n.py").write_text(FIXED_CODE + base_extra)
    (tree / "test_n.py").write_text(TEST_BODY.format(value=2))
    return tree


def gate(tree: Path, config: AuditorConfig | None = None) -> Finding:
    found = {f.gate: f for f in Auditor(tree, config=config).tier1().findings}
    return found[PROJECT_GATE]


def test_stages_the_head_passes_give_a_passing_finding_and_the_gate_line(tmp_path: Path) -> None:
    found = gate(project(tmp_path))
    assert found.verdict == "pass"
    assert found.reason == "code-wrong"
    assert found.tier == 1
    assert found.detail.splitlines()[0] == "Gate: base ✓, head ✓ (2 stages)"
    assert "coverage total is judged by the project's own gate, not here" in found.detail


def test_a_stage_the_head_broke_fails_the_finding_and_names_it(tmp_path: Path) -> None:
    tree = project(tmp_path)
    (tree / "test_n.py").write_text(TEST_BODY.format(value=2) + "x = 1  # BADFMT\n")
    found = gate(tree)
    assert found.verdict == "fail"
    assert found.detail.splitlines()[0].startswith("Gate: base ✓, head ✗ (")
    assert "python fmtcheck.py: the change broke it, it passed at the base" in found.detail
    assert "test_n.py:6: BADFMT found" in found.detail
    assert "BADLINT" not in found.detail
    assert not Auditor(tree).tier1().passed


def test_a_stage_red_at_the_base_and_the_head_is_not_proven_and_never_refuses(
    tmp_path: Path,
) -> None:
    tree = project(tmp_path, base_extra="y = 2  # BADFMT\n")
    found = gate(tree)
    assert found.verdict == "not-proven"
    assert (
        "Gate: base ✗ (python fmtcheck.py), head ✗ (python fmtcheck.py) (2 stages)" in found.detail
    )
    assert "was failing at the base, so the change is not shown to have broken it" in found.detail
    assert "n.py:3: BADFMT found" not in found.detail
    assert Auditor(tree).tier1().passed


def test_a_stage_whose_tool_is_missing_is_not_proven_and_names_the_tool(tmp_path: Path) -> None:
    saddle = '[tool.saddle]\ngate-checks = [["no-such-gate-tool-xyz", "src"]]\n'
    found = gate(project(tmp_path, saddle=saddle))
    assert found.verdict == "not-proven"
    assert "no-such-gate-tool-xyz could not be launched here" in found.detail
    assert found.detail.splitlines()[0] == (
        "Gate: base ✗ (no-such-gate-tool-xyz src), head ✗ (no-such-gate-tool-xyz src) (1 stage)"
    )


def test_the_audited_tree_cannot_weaken_the_list_it_is_judged_by(tmp_path: Path) -> None:
    """The list is read at the baseline: a head that empties it and adds a violation
    is still judged by the stages the baseline names, and fails."""
    tree = project(tmp_path)
    (tree / "pyproject.toml").write_text("[tool.saddle]\ngate-checks = []\n")
    (tree / "n.py").write_text(FIXED_CODE + "y = 2  # BADLINT\n")
    found = gate(tree)
    assert found.verdict == "fail"
    assert "(2 stages)" in found.detail


def test_a_project_with_no_stages_gets_no_finding(tmp_path: Path) -> None:
    tree = project(tmp_path, saddle="[tool.saddle]\ntest-workers = 1\n")
    assert PROJECT_GATE not in {f.gate for f in Auditor(tree).tier1().findings}


def test_the_static_check_counts_as_one_more_stage_and_is_not_run_twice(tmp_path: Path) -> None:
    check = f'["{sys.executable}", "checker.py", "BADTYPE"]'
    both = f"[tool.saddle]\ngate-checks = {STAGES}\nstatic-check = {check}\n"
    tree = project(tmp_path, saddle=both)
    (tree / "n.py").write_text(FIXED_CODE + "y = 2  # BADTYPE\n")
    found = {f.gate: f for f in Auditor(tree).tier1().findings}
    assert found[STATIC_CHECK].verdict == "fail"
    assert found[PROJECT_GATE].verdict == "fail"
    assert "(3 stages)" in found[PROJECT_GATE].detail
    only = f"[tool.saddle]\nstatic-check = {check}\n"
    alone = gate(project(tmp_path / "alone", saddle=only))
    assert alone.detail.splitlines()[0] == "Gate: base ✓, head ✓ (1 stage)"
    listed = f"[tool.saddle]\ngate-checks = [{check}]\nstatic-check = {check}\n"
    once = gate(project(tmp_path / "once", saddle=listed))
    assert "(1 stage)" in once.detail


def test_an_unusable_value_is_refused_by_name(tmp_path: Path) -> None:
    tree = project(tmp_path, saddle='[tool.saddle]\ngate-checks = "ruff check ."\n')
    with pytest.raises(AuditError, match="gate-checks"):
        Auditor(tree).tier1()


class Counter:
    """Counts how many times the baseline was checked out for the gate stages."""

    def __init__(self, monkeypatch: pytest.MonkeyPatch) -> None:
        self.calls = 0
        real = materialize_baseline

        def counted(*args: object, **kwargs: object) -> None:
            self.calls += 1
            real(*args, **kwargs)  # type: ignore[arg-type]

        monkeypatch.setattr(auditor_module, "materialize_baseline", counted)


def test_the_baseline_runs_are_asked_once_per_baseline(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    counter = Counter(monkeypatch)
    tree = project(tmp_path)
    config = AuditorConfig(cache_dir=tmp_path / "cache")
    auditor = Auditor(tree, config=config)
    auditor.tier1()
    (tree / "test_n.py").write_text(TEST_BODY.format(value=2) + "x = 1  # BADFMT\n")
    assert auditor.tier1().findings  # a second tree, the same baseline
    assert counter.calls == 1
    # A new auditor over the same cache directory reads them from disk.
    (tree / "test_n.py").write_text(TEST_BODY.format(value=2) + "z = 1  # BADLINT\n")
    Auditor(tree, config=config).tier1()
    assert counter.calls == 1
    # A cache file that cannot be read is a miss, never an empty result.
    (cached,) = (tmp_path / "cache").glob("gate-*.json")
    cached.write_text("{not json")
    (tree / "test_n.py").write_text(TEST_BODY.format(value=2) + "w = 1  # BADLINT\n")
    assert Auditor(tree, config=config).tier1().findings
    assert counter.calls == 2
    cached.write_text(json.dumps([[0, False]]))  # one entry for two stages
    (tree / "test_n.py").write_text(TEST_BODY.format(value=2) + "v = 1  # BADFMT\n")
    Auditor(tree, config=config).tier1()
    assert counter.calls == 3


def test_the_baseline_runs_are_kept_in_memory_when_there_is_no_cache_directory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    counter = Counter(monkeypatch)
    tree = project(tmp_path)
    auditor = Auditor(tree)
    auditor.tier1()
    (tree / "test_n.py").write_text(TEST_BODY.format(value=2) + "x = 1  # BADFMT\n")
    auditor.tier1()
    assert counter.calls == 1


def test_a_tree_holding_the_gate_finding_is_never_reused_for_a_format_only_edit() -> None:
    """A format stage judges exactly what a format-only edit changes."""
    gate_finding = Finding(PROJECT_GATE, 1, "pass", "code-wrong", "Gate: ...", ())
    other = Finding("syntax", 1, "pass", "code-wrong", "ok", ())
    reusable = auditor_module._reusable
    assert reusable(Findings(tier=1, key="k", findings=(other,)))
    assert not reusable(Findings(tier=1, key="k", findings=(other, gate_finding)))


def test_the_packet_carries_the_gate_line_and_lists_a_stage_not_proven(tmp_path: Path) -> None:
    tree = project(tmp_path, base_extra="y = 2  # BADFMT\n")
    journal = tmp_path / "proofs.jsonl"
    Auditor(tree, config=AuditorConfig(journal=journal)).tier1()
    assert any(PROJECT_GATE in s.name for s in read_spans(journal))
    text = render_packet_text(compile_packet(journal))
    assert "Gate: base ✗ (python fmtcheck.py), head ✗ (python fmtcheck.py) (2 stages)" in text
    assert "Not proven" in text
    green = tmp_path / "green"
    clean = project(green)
    ledger = tmp_path / "green.jsonl"
    Auditor(clean, config=AuditorConfig(journal=ledger)).tier1()
    assert "Gate: base ✓, head ✓ (2 stages)" in render_packet_text(compile_packet(ledger))
