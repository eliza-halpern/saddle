"""`[tool.saddle] gate-checks`: the project's own gate stages, judged on head and base.

Known-good: a stage that passes on the head passes; a stage the head broke and
the base passed fails, quoting its output. Known-bad readings: a stage red at
the base too is not proven (never a pass, never a refusal); a stage whose tool
cannot start is not proven and names the tool; an unusable value is refused by
name. The audit-level behaviour is in `test_project_gate_audit`.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from test_auditor import BASE_CODE, _git, _init

from saddle.evidence import CapturedRun, SuiteLimitError, gate_checks
from saddle.gates import (
    GATE_STAGE_LINES,
    TOOL_UNAVAILABLE,
    check_project_gate,
    gate_stage_name,
)


def run(code: int, out: str = "", *, timed_out: bool = False) -> CapturedRun:
    return CapturedRun(argv=("t",), exit_code=code, stdout=out, stderr="", timed_out=timed_out)


OK = run(0)
RED = run(1, "a.py:3: E501 too long\n")
RUFF = ("ruff", "check", ".")
ESLINT = ("npx", "--no-install", "eslint", ".")


def test_a_stage_the_head_passes_passes_whatever_the_base_did() -> None:
    for base in (OK, RED, run(TOOL_UNAVAILABLE)):
        gate = check_project_gate([(RUFF, OK, base)])
        assert gate.verdict == "pass"
    assert gate.detail.splitlines()[0] == "Gate: base ✗ (ruff check), head ✓ (1 stage)"


def test_all_green_reads_base_and_head_clean_and_says_what_it_does_not_judge() -> None:
    gate = check_project_gate([(RUFF, OK, OK), (ESLINT, OK, OK)])
    assert gate.verdict == "pass"
    lines = gate.detail.splitlines()
    assert lines[0] == "Gate: base ✓, head ✓ (2 stages)"
    assert "coverage total is judged by the project's own gate, not here" in lines[-1]


def test_a_stage_the_head_broke_and_the_base_passed_fails_and_quotes_its_output() -> None:
    gate = check_project_gate([(RUFF, OK, OK), (ESLINT, RED, OK)])
    assert gate.verdict == "fail"
    assert gate.detail.splitlines()[0] == "Gate: base ✓, head ✗ (eslint) (2 stages)"
    assert "eslint: the change broke it, it passed at the base" in gate.detail
    assert "a.py:3: E501 too long" in gate.detail


def test_a_long_failure_is_cut_to_the_named_lines() -> None:
    many = run(1, "\n".join(f"e{i}" for i in range(20)))
    detail = check_project_gate([(RUFF, many, OK)]).detail
    assert f"e{GATE_STAGE_LINES - 1}" in detail
    assert f"e{GATE_STAGE_LINES}\n" not in detail
    assert f"(+{20 - GATE_STAGE_LINES} more lines)" in detail


def test_a_stage_that_timed_out_on_the_head_and_passed_on_the_base_fails() -> None:
    gate = check_project_gate([(RUFF, run(0, timed_out=True), OK)])
    assert gate.verdict == "fail"
    assert "timed out" in gate.detail


def test_a_stage_red_at_the_base_too_is_not_proven_and_says_it_was_already_failing() -> None:
    gate = check_project_gate([(RUFF, RED, RED)])
    assert gate.verdict == "not-proven"
    assert gate.detail.splitlines()[0] == "Gate: base ✗ (ruff check), head ✗ (ruff check) (1 stage)"
    assert "ruff check: not proven, it was failing at the base" in gate.detail
    assert "a.py:3" not in gate.detail


@pytest.mark.parametrize(
    ("base", "said"),
    [
        (run(0, timed_out=True), "timed out at the base"),
        (run(TOOL_UNAVAILABLE), "could not be launched at the base"),
    ],
)
def test_a_base_that_cannot_be_judged_leaves_a_red_head_not_proven(
    base: CapturedRun, said: str
) -> None:
    gate = check_project_gate([(RUFF, RED, base)])
    assert gate.verdict == "not-proven"
    assert said in gate.detail


def test_a_stage_whose_tool_cannot_start_is_not_proven_and_names_the_tool() -> None:
    gate = check_project_gate([(ESLINT, run(TOOL_UNAVAILABLE), OK), (RUFF, OK, OK)])
    assert gate.verdict == "not-proven"
    assert "npx could not be launched here" in gate.detail
    assert gate.detail.splitlines()[0] == "Gate: base ✓, head ✗ (eslint) (2 stages)"


def test_a_regression_outranks_a_not_proven_stage() -> None:
    gate = check_project_gate([(ESLINT, run(TOOL_UNAVAILABLE), OK), (RUFF, RED, OK)])
    assert gate.verdict == "fail"
    assert "eslint: not proven" in gate.detail


def test_stage_names_skip_launchers_and_options_and_never_collide() -> None:
    assert gate_stage_name(["uv", "run", "ruff", "format", "--check", "."]) == "ruff format"
    assert gate_stage_name(ESLINT) == "eslint"
    assert gate_stage_name(["uv", "lock", "--check"]) == "uv lock"
    assert gate_stage_name(RUFF, taken=["ruff check"]) == "ruff check ."
    assert gate_stage_name(["npx", "--no-install"]) == "npx --no-install"
    gate = check_project_gate(
        [(("ruff", "check", "a"), RED, RED), (("ruff", "check", "b"), RED, RED)]
    )
    assert gate.detail.splitlines()[0].startswith(
        "Gate: base ✗ (ruff check, ruff check b), head ✗ (ruff check, ruff check b)"
    )


def project(tmp_path: Path, value: str) -> Path:
    tree = tmp_path / "tree"
    _init(tree, {"n.py": BASE_CODE, "pyproject.toml": f"[tool.saddle]\ngate-checks = {value}\n"})
    return tree


def test_the_list_is_read_from_the_baseline_commit_not_the_working_tree(tmp_path: Path) -> None:
    tree = project(tmp_path, '[["ruff", "check", "."], ["shellcheck", "a.sh"]]')
    (tree / "pyproject.toml").write_text('[tool.saddle]\ngate-checks = [["true"]]\n')
    _git(tree, "add", "-A")
    assert gate_checks(tree, "HEAD") == (("ruff", "check", "."), ("shellcheck", "a.sh"))


def test_no_key_and_an_empty_list_both_mean_no_stages(tmp_path: Path) -> None:
    assert gate_checks(project(tmp_path, "[]"), "HEAD") == ()
    bare = tmp_path / "bare"
    _init(bare, {"n.py": BASE_CODE, "pyproject.toml": "[tool.saddle]\ntest-workers = 1\n"})
    assert gate_checks(bare, "HEAD") == ()
    nothing = tmp_path / "nothing"
    _init(nothing, {"n.py": BASE_CODE})
    assert gate_checks(nothing, "HEAD") == ()


@pytest.mark.parametrize(
    "value", ['"ruff check"', '["ruff"]', "[[]]", '[["ruff", ""]]', "[[1]]", '[["a"], "b"]']
)
def test_an_unusable_value_is_refused_by_name(tmp_path: Path, value: str) -> None:
    with pytest.raises(SuiteLimitError, match="gate-checks"):
        gate_checks(project(tmp_path, value), "HEAD")
