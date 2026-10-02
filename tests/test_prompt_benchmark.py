"""`[tool.saddle] prompt-benchmark`: a changed prompt is measured or listed as not proven.

Known-good: a change that edits a prompt constant gets a tier-2 `prompt-effect`
finding. With no benchmark configured it is not proven and names the constant;
with one, its score (and the baseline's) is in the finding and in the ledger,
the finding stays not proven unless the project set a floor or a margin at its
baseline, and a head within that bar passes. A change that only re-wraps a
prompt, or edits a constant that is not a prompt, gets no such finding.
Known-bad: a benchmark that exits non-zero, times out, or prints something
other than a JSON score on its last line is not proven and is never a pass;
a head below the floor, or too far below the baseline, fails; the audited tree
cannot lower the floor it is judged by; an unusable setting is refused by name.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

import pytest
from test_auditor import _init
from test_skipped_tests import ledger

from saddle import runner
from saddle.audit import AuditError, staged_copy
from saddle.auditor import (
    PROMPT_EFFECT_GATE,
    Auditor,
    AuditorConfig,
    Findings,
    finding_body,
    prompt_effect,
)
from saddle.evidence import (
    MutationOutcome,
    SuiteLimitError,
    prompt_benchmark,
    run_capture,
)
from saddle.journal import read_spans
from saddle.packet import compile_packet
from saddle.prompt_changes import Measured, Score

NOTHING = MutationOutcome(killed=0, total=0, generated=0, survivors=())

OLD_PROMPT = 'SYSTEM_PROMPT = "You are careful. Read the code first."\n'
NEW_PROMPT = 'SYSTEM_PROMPT = "You are careful. Read the code twice."\n'

# A stand-in benchmark whose score reads the tree it runs in: a prompt with
# "twice" scores 0.9, one with "sloppy" scores 0.5, any other 0.7. Its log line
# comes first; only the last line is the contract.
SCORER = (
    "import json, pathlib\n"
    "text = pathlib.Path('prompts.py').read_text()\n"
    "score = 0.9 if 'twice' in text else 0.5 if 'sloppy' in text else 0.7\n"
    "print('running 12 cases')\n"
    "print(json.dumps({'score': score, 'n': 12, 'label': 'stub'}))\n"
)

TESTS = "import prompts\nfrom n import f\n\n\ndef test_f():\n    assert f() == 1 and prompts\n"


def settings(*lines: str, script: str = "bench.py") -> str:
    command = f'prompt-benchmark = ["{sys.executable}", "{script}"]\n'
    return "[tool.saddle]\n" + command + "".join(f"{line}\n" for line in lines)


def project(
    tmp_path: Path,
    head_prompt: str | None = NEW_PROMPT,
    *,
    pyproject: str | None = None,
    bench: str = SCORER,
) -> Path:
    """A baseline holding the prompt, a stand-in benchmark and (when `pyproject`)
    its settings; the head replaces the prompt with `head_prompt`."""
    root = tmp_path / "tree"
    files = {
        "n.py": "def f():\n    return 1\n",
        "prompts.py": OLD_PROMPT,
        "bench.py": bench,
        "test_n.py": TESTS,
    }
    if pyproject is not None:
        files["pyproject.toml"] = pyproject
    _init(root, files)
    if head_prompt is not None:
        (root / "prompts.py").write_text(head_prompt)
    return root


@pytest.fixture(autouse=True)
def no_mutants(monkeypatch: pytest.MonkeyPatch) -> None:
    def fake(*_args: Any, **_kwargs: Any) -> MutationOutcome:
        return NOTHING

    monkeypatch.setattr(runner, "mutation_sample", fake)


def effect(root: Path) -> tuple[str, str]:
    found = Auditor(root).tier2()
    return verdict_and_detail(found)


def verdict_and_detail(found: Findings) -> tuple[str, str]:
    return next((f.verdict, f.detail) for f in found.findings if f.gate == PROMPT_EFFECT_GATE)


def gates(found: Findings) -> set[str]:
    return {f.gate for f in found.findings}


# -- no benchmark configured -----------------------------------------------------


def test_an_edited_prompt_with_no_benchmark_is_not_proven_and_named(tmp_path: Path) -> None:
    found = Auditor(project(tmp_path)).tier2()
    verdict, detail = verdict_and_detail(found)
    assert verdict == "not-proven"
    assert detail.startswith("prompt text changed: prompts.py: SYSTEM_PROMPT;")
    assert "no test, coverage or mutation result measures a prompt's effect" in detail
    assert "this project configures no prompt benchmark" in detail
    finding = next(f for f in found.findings if f.gate == PROMPT_EFFECT_GATE)
    assert (finding.tier, finding.reason) == (2, "evidence-thin")


def test_a_rewrapped_prompt_and_a_non_prompt_constant_get_no_finding(tmp_path: Path) -> None:
    rewrapped = (
        'SYSTEM_PROMPT = (\n    "You are careful. "\n    "Read the code first."\n)  # same text\n'
    )
    assert PROMPT_EFFECT_GATE not in gates(Auditor(project(tmp_path, rewrapped)).tier2())
    other = tmp_path / "other"
    other.mkdir()
    changed = OLD_PROMPT + 'COLOR_STYLE = "red"\n'
    root = project(other, changed)
    assert PROMPT_EFFECT_GATE not in gates(Auditor(root).tier2())


def test_a_prompt_named_constant_in_a_test_file_is_not_a_prompt_change(tmp_path: Path) -> None:
    root = project(tmp_path, None)
    (root / "test_n.py").write_text(TESTS + 'FIXTURE_PROMPT = "Say this exact text."\n')
    assert PROMPT_EFFECT_GATE not in gates(Auditor(root).tier2())
    # the same edit to a source file is one
    (root / "prompts.py").write_text(OLD_PROMPT + 'FIXTURE_PROMPT = "Say this exact text."\n')
    assert effect(root)[1].startswith("prompt text changed: prompts.py: FIXTURE_PROMPT;")


def test_a_change_that_touches_no_python_source_gets_no_finding(tmp_path: Path) -> None:
    root = project(tmp_path, None)
    (root / "notes.txt").write_text("SYSTEM_PROMPT = something new\n")
    assert PROMPT_EFFECT_GATE not in gates(Auditor(root).tier2())


@pytest.mark.parametrize("configured", [False, True])
def test_a_baseline_file_that_does_not_parse_is_reported_not_skipped(
    tmp_path: Path, configured: bool
) -> None:
    # Only a baseline file can be unreadable here (deleted by the change): a
    # head file that does not parse already fails tier 1, which blocks tier 2.
    root = project(tmp_path, None, pyproject=settings() if configured else None)
    (root / "extra.py").write_text("def broken(:\n")
    run_capture(["git", "add", "-A"], root)
    run_capture(["git", "commit", "-q", "-m", "a broken file"], root)
    (root / "extra.py").unlink()
    found = Auditor(root).tier2()
    assert PROMPT_EFFECT_GATE not in gates(Auditor(root).tier1())  # tier 2 only
    verdict, detail = verdict_and_detail(found)
    assert verdict == "not-proven"
    assert detail.startswith("could not tell whether prompt text changed in extra.py")
    if configured:
        # No prompt text is known to have changed, so the benchmark is not run.
        assert detail.endswith("Not proven: a person reads the files.")
        assert "head" not in detail
    else:
        assert "this project configures no prompt benchmark" in detail


def test_the_note_and_the_data_only_note_do_not_contradict_each_other(tmp_path: Path) -> None:
    found = Auditor(project(tmp_path)).tier2()
    mutation = next(f for f in found.findings if f.gate == "mutation")
    assert mutation.verdict == "not-proven"
    assert "module-level constants only (prompts.py)" in mutation.detail
    journal = ledger(tmp_path, [finding_body(f) for f in found.findings])
    row = {r.key: r for r in compile_packet(journal, run_id="r").rows}["not-proven"]
    assert row.text == "What this packet cannot vouch for:"
    listed = " ".join(row.items)
    assert "prompt-effect (tier 2): prompt text changed: prompts.py: SYSTEM_PROMPT" in listed
    assert "mutation (tier 2): not proven: the source this change touches" in listed
    assert "Nothing is left unproven" not in row.text


# -- a benchmark configured ------------------------------------------------------


def test_the_score_is_in_the_finding_and_sealed_in_the_ledger(tmp_path: Path) -> None:
    root = project(tmp_path, pyproject=settings())
    journal = tmp_path / "ledger" / "proofs.jsonl"
    found = Auditor(root, "HEAD", AuditorConfig(journal=journal)).tier2()
    verdict, detail = verdict_and_detail(found)
    assert verdict == "not-proven"
    assert "prompt benchmark base 0.7 (n=12) [stub] -> head 0.9 (n=12) [stub]." in detail
    assert "Not proven: the project sets no prompt-benchmark-floor" in detail
    (span,) = [s for s in read_spans(journal) if s.name == f"audit-tier2:{PROMPT_EFFECT_GATE}"]
    sidecar = next((journal.parent / "attempts").glob(f"{span.span_id}*")).read_text()
    record = json.loads(sidecar)
    assert (record["head"]["score"], record["base"]["score"]) == (0.9, 0.7)
    assert record["names"] == ["prompts.py: SYSTEM_PROMPT"]


def test_the_score_can_vary_with_the_prompt(tmp_path: Path) -> None:
    """The count the mechanism reports must be able to differ: the same stub,
    run on a worse prompt, reports a lower head score."""
    root = project(
        tmp_path, 'SYSTEM_PROMPT = "You are sloppy. Skim the code."\n', pyproject=settings()
    )
    _, detail = effect(root)
    assert "base 0.7 (n=12) [stub] -> head 0.5 (n=12) [stub]" in detail


def test_the_baseline_score_is_measured_once_per_run(tmp_path: Path) -> None:
    root = project(tmp_path, pyproject=settings())
    auditor = Auditor(root)
    resolved = run_capture(["git", "rev-parse", "HEAD"], root).stdout.strip()
    command = (sys.executable, "bench.py")
    auditor._bench_seen[(resolved, command)] = Measured(Score(0.55, 3, "remembered"))
    _, detail = verdict_and_detail(auditor.tier2())
    assert "base 0.55 (n=3) [remembered] -> head 0.9" in detail


def test_a_floor_met_passes_and_a_head_below_it_fails(tmp_path: Path) -> None:
    met = project(tmp_path, pyproject=settings("prompt-benchmark-floor = 0.8"))
    assert effect(met)[0] == "pass"
    low = tmp_path / "low"
    low.mkdir()
    root = project(
        low,
        'SYSTEM_PROMPT = "You are sloppy. Skim the code."\n',
        pyproject=settings("prompt-benchmark-floor = 0.8"),
    )
    found = Auditor(root).tier2()
    verdict, detail = verdict_and_detail(found)
    assert verdict == "fail"
    assert "head is below the floor 0.8 (prompt-benchmark-floor, read at the baseline)" in detail
    assert next(f for f in found.findings if f.gate == PROMPT_EFFECT_GATE).reason == "code-wrong"
    assert not found.passed


def test_the_tree_cannot_lower_its_own_floor(tmp_path: Path) -> None:
    root = project(
        tmp_path,
        'SYSTEM_PROMPT = "You are sloppy. Skim the code."\n',
        pyproject=settings("prompt-benchmark-floor = 0.8"),
    )
    (root / "pyproject.toml").write_text(settings("prompt-benchmark-floor = 0.1"))
    assert effect(root)[0] == "fail"
    (root / "pyproject.toml").write_text("[tool.saddle]\n")  # or drop the benchmark outright
    verdict, detail = effect(root)
    assert verdict == "fail"
    assert "head 0.5" in detail


def test_a_drop_beyond_the_margin_fails_and_a_small_one_passes(tmp_path: Path) -> None:
    worse = 'SYSTEM_PROMPT = "You are sloppy. Skim the code."\n'  # 0.7 -> 0.5
    big = project(tmp_path, worse, pyproject=settings("prompt-benchmark-margin = 0.1"))
    verdict, detail = effect(big)
    assert verdict == "fail"
    assert "head is more than 0.1 below base (prompt-benchmark-margin" in detail
    small = tmp_path / "small"
    small.mkdir()
    assert (
        effect(project(small, worse, pyproject=settings("prompt-benchmark-margin = 0.3")))[0]
        == "pass"
    )


def test_a_margin_with_no_baseline_score_is_not_proven(tmp_path: Path) -> None:
    # The benchmark needs a file the baseline lacks, so it runs only on the head.
    script = (
        "import json, pathlib\n"
        "if not pathlib.Path('fresh.txt').exists():\n    raise SystemExit(4)\n"
        "print(json.dumps({'score': 0.9}))\n"
    )
    root = project(tmp_path, pyproject=settings("prompt-benchmark-margin = 0.1"), bench=script)
    (root / "fresh.txt").write_text("here\n")
    verdict, detail = effect(root)
    assert verdict == "not-proven"
    assert "base: the benchmark failed to run (exit 4); head 0.9" in detail
    assert "a margin is set but the baseline has no score" in detail


# -- a benchmark that gives no score ----------------------------------------------


@pytest.mark.parametrize(
    ("script", "said"),
    [
        ("print('all good')\n", "ran but produced no readable score"),
        ("import json\nprint(json.dumps({'score': 'high'}))\nprint('done')\n", "no readable score"),
        ("raise SystemExit(3)\n", "failed to run (exit 3)"),
        ("import time\ntime.sleep(60)\n", "timed out"),
    ],
    ids=["garbage", "score-not-last", "exit-nonzero", "timeout"],
)
def test_a_benchmark_with_no_score_is_not_proven_never_a_pass(
    tmp_path: Path, script: str, said: str
) -> None:
    pyproject = settings("prompt-benchmark-floor = 0.8").replace(
        "[tool.saddle]", "[tool.saddle]\ntest-timeout = 20"
    )
    root = project(tmp_path, pyproject=pyproject, bench=script)
    verdict, detail = effect(root)
    assert verdict == "not-proven"  # even with a floor set: no score is no verdict on the prompt
    assert f"the prompt benchmark {said}" in detail or said in detail
    assert "so the prompt's effect is not proven" in detail


def test_a_missing_command_is_reported_as_not_launched(tmp_path: Path) -> None:
    pyproject = '[tool.saddle]\nprompt-benchmark = ["saddle-no-such-benchmark-tool"]\n'
    verdict, detail = effect(project(tmp_path, pyproject=pyproject))
    assert verdict == "not-proven"
    assert "the prompt benchmark could not be launched" in detail


# -- the setting ---------------------------------------------------------------------


@pytest.mark.parametrize(
    "lines",
    [
        'prompt-benchmark = "python bench.py"',
        "prompt-benchmark = []",
        'prompt-benchmark = ["python", ""]',
        'prompt-benchmark = ["python"]\nprompt-benchmark-floor = "high"',
        'prompt-benchmark = ["python"]\nprompt-benchmark-floor = true',
        'prompt-benchmark = ["python"]\nprompt-benchmark-floor = nan',
        'prompt-benchmark = ["python"]\nprompt-benchmark-margin = -0.1',
        "prompt-benchmark-floor = 0.5",
        "prompt-benchmark-margin = 0.5",
    ],
)
def test_an_unusable_setting_is_refused_by_name(tmp_path: Path, lines: str) -> None:
    root = tmp_path / "tree"
    _init(root, {"n.py": "x = 1\n", "pyproject.toml": f"[tool.saddle]\n{lines}\n"})
    with pytest.raises(SuiteLimitError, match="prompt benchmark"):
        prompt_benchmark(root, "HEAD")
    (root / "n.py").write_text("x = 2\n")
    with pytest.raises(AuditError, match="prompt benchmark"):
        Auditor(root).tier2()


def test_a_typo_of_a_key_is_refused_not_read_as_unset(tmp_path: Path) -> None:
    root = tmp_path / "tree"
    _init(root, {"pyproject.toml": '[tool.saddle]\nprompt-bench = ["x"]\n'})
    with pytest.raises(SuiteLimitError, match="prompt-bench"):
        prompt_benchmark(root, "HEAD")


def test_the_settings_read_at_the_baseline_are_returned_whole(tmp_path: Path) -> None:
    root = tmp_path / "tree"
    _init(
        root,
        {"pyproject.toml": settings("prompt-benchmark-floor = 1", "prompt-benchmark-margin = 0")},
    )
    got = prompt_benchmark(root, "HEAD")
    assert got is not None
    assert got.argv == (sys.executable, "bench.py")
    assert (got.floor, got.margin) == (1.0, 0.0)
    plain = tmp_path / "plain"
    _init(plain, {"pyproject.toml": "[tool.saddle]\n"})
    assert prompt_benchmark(plain, "HEAD") is None
    bare = tmp_path / "bare"
    _init(bare, {"n.py": "x = 1\n"})
    assert prompt_benchmark(bare, "HEAD") is None


def test_prompt_effect_is_none_when_nothing_prompt_changed(tmp_path: Path) -> None:
    root = project(tmp_path, None)
    (root / "n.py").write_text("def f():\n    return 2\n")
    with staged_copy(root, "HEAD") as (copy, _staged, resolved):
        assert prompt_effect(copy, resolved, 10.0, None, {}) is None
