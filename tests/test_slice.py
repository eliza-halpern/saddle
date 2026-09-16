"""Tests for saddle.slice: vertical-slice driver and its transcript."""

from __future__ import annotations

import re
import subprocess
from datetime import datetime
from pathlib import Path

import pytest

import saddle.slice as slice_module
from saddle.dag import Dag, Node
from saddle.evidence import run_argv
from saddle.gates import GateCheck, Tier1Result
from saddle.journal import ProofRecord, append_record, read_records
from saddle.slice import NodeGateFailedError, _apply_diff, _utcnow, run_slice
from saddle.vllm import DiffProposal


def _git_repo(root: Path) -> None:
    setup = (
        ["git", "init"],
        ["git", "config", "user.email", "test@example.com"],
        ["git", "config", "user.name", "test"],
    )
    for argv in setup:
        assert run_argv(argv, root) == 0
    (root / "n.py").write_text("x = 1\n")
    assert run_argv(["git", "add", "n.py"], root) == 0
    assert run_argv(["git", "commit", "-m", "base"], root) == 0


def _node_dict(node_id: str, deps: list[str]) -> dict[str, object]:
    return {
        "id": node_id,
        "dependencies": deps,
        "task_prompt": f"Do {node_id}.",
        "requirement_ids": ["REQ-001"],
        "execution_constraints": {
            "reasoning_budget": "low",
            "allowed_tools": ["read_file"],
            "max_context_tokens": 5000,
        },
        "deterministic_gate": {
            "test_command": "pytest test_n.py",
            "changed_line_coverage_min": 100.0,
            "red_phase_required": True,
            "mutation_sample": {
                "scope": "changed-lines",
                "max_mutants": 10,
                "kill_threshold": 85.0,
            },
        },
    }


def _slice_repo(root: Path) -> None:
    setup = (
        ["git", "init"],
        ["git", "config", "user.email", "test@example.com"],
        ["git", "config", "user.name", "test"],
    )
    for argv in setup:
        assert run_argv(argv, root) == 0
    (root / "n.py").write_text("def f():\n    return 1\n")
    assert run_argv(["git", "add", "n.py"], root) == 0
    assert run_argv(["git", "commit", "-m", "baseline"], root) == 0


GOOD_DIFF = (
    "diff --git a/n.py b/n.py\n"
    "--- a/n.py\n"
    "+++ b/n.py\n"
    "@@ -1,2 +1,2 @@\n"
    " def f():\n"
    "-    return 1\n"
    "+    return 2\n"
    "diff --git a/test_n.py b/test_n.py\n"
    "new file mode 100644\n"
    "--- /dev/null\n"
    "+++ b/test_n.py\n"
    "@@ -0,0 +1,5 @@\n"
    "+from n import f\n"
    "+\n"
    "+\n"
    "+def test_f():  # REQ-001\n"
    "+    assert f() == 2\n"
)


def test_utcnow_returns_timezone_aware_iso() -> None:
    stamp = _utcnow()
    assert datetime.fromisoformat(stamp).utcoffset() is not None
    assert stamp.endswith("+00:00")


def test_node_gate_failed_carries_verdict() -> None:
    result = Tier1Result(
        node_id="n9",
        passed=False,
        checks=(GateCheck(name="tests", passed=False, detail="boom"),),
    )
    error = NodeGateFailedError(result)
    assert str(error) == "node 'n9' failed its Tier-1 gate"
    assert error.result is result


def test_apply_diff_applies_and_stages(tmp_path: Path) -> None:
    _git_repo(tmp_path)
    diff = "diff --git a/n.py b/n.py\n--- a/n.py\n+++ b/n.py\n@@ -1 +1 @@\n-x = 1\n+x = 2\n"
    _apply_diff(tmp_path, diff)
    assert (tmp_path / "n.py").read_text() == "x = 2\n"
    staged = subprocess.run(
        ["git", "-C", str(tmp_path), "diff", "--cached", "--name-only"],
        capture_output=True,
        text=True,
    )
    assert staged.stdout.split() == ["n.py"]


def test_apply_diff_recounts_wrong_hunk_headers(tmp_path: Path) -> None:
    _git_repo(tmp_path)
    diff = "diff --git a/n.py b/n.py\n--- a/n.py\n+++ b/n.py\n@@ -1,3 +1,3 @@\n-x = 1\n+x = 2\n"
    _apply_diff(tmp_path, diff)
    assert (tmp_path / "n.py").read_text() == "x = 2\n"


def test_apply_diff_garbage_raises(tmp_path: Path) -> None:
    _git_repo(tmp_path)
    expected = f"worker diff did not apply cleanly in {str(tmp_path)!r}"
    with pytest.raises(RuntimeError, match=re.escape(expected)):
        _apply_diff(tmp_path, "not a diff\n")


def test_run_slice_pass_end_to_end(tmp_path: Path) -> None:
    _slice_repo(tmp_path)
    dag = Dag.model_validate({"nodes": [_node_dict("n1", [])]})
    journal = tmp_path / "proofs.jsonl"
    result = run_slice(
        "Fix f.",
        dag,
        workdir=tmp_path,
        journal_path=journal,
        propose=lambda node: DiffProposal(GOOD_DIFF, "return two instead"),
        now=lambda: "2026-09-16T00:00:00+00:00",
    )
    assert result.passed is True
    assert list(result.proofs) == ["n1"]
    assert "- Task: Fix f.\n" in result.transcript
    assert "- Started: 2026-09-16T00:00:00+00:00\n" in result.transcript
    assert "- Finished: 2026-09-16T00:00:00+00:00\n" in result.transcript
    assert f"- Path: {journal}\n" in result.transcript
    assert "- Verdict: PASS\n" in result.transcript
    assert "## Node n1\n" in result.transcript
    assert "- Gate syntax: PASS (2 file(s) parsed)\n" in result.transcript
    assert "- Gate ruff: PASS (2 file(s) clean)\n" in result.transcript
    assert "- Gate tests: PASS ('pytest test_n.py' exited 0)\n" in result.transcript
    assert "- Gate coverage: PASS (100.0% >= 100.0%)\n" in result.transcript
    assert "- Gate red-phase: PASS (fail pre-change, pass post-change)\n" in result.transcript
    assert "- Gate requirement-binding: PASS (1 requirement(s) bound)\n" in result.transcript
    assert f"- Proof: {result.proofs['n1']}\n" in result.transcript
    assert "- Issues: none (chain verifies)\n" in result.transcript
    assert read_records(journal)[0].thinking == "return two instead"


def test_run_slice_gate_fail_leaves_dependent_undispatched(tmp_path: Path) -> None:
    _slice_repo(tmp_path)
    dag = Dag.model_validate({"nodes": [_node_dict("a", []), _node_dict("b", ["a"])]})
    bad_diff = GOOD_DIFF.replace(
        "+    return 2\n",
        "+    return 2\n+\n+\n+def unused():\n+    return 3\n",
    ).replace("@@ -1,2 +1,2 @@", "@@ -1,2 +1,6 @@")
    seen: list[str] = []

    def propose(node: Node) -> DiffProposal:
        seen.append(node.id)
        return DiffProposal(bad_diff, "")

    result = run_slice(
        "Fix f.",
        dag,
        workdir=tmp_path,
        journal_path=tmp_path / "proofs.jsonl",
        propose=propose,
        now=lambda: "2026-09-16T00:00:00+00:00",
    )
    assert result.passed is False
    assert result.proofs == {}
    assert seen == ["a"]
    assert "- Verdict: FAIL\n" in result.transcript
    assert "## Node a\n" in result.transcript
    assert "## Node b\n" in result.transcript
    assert "- Gate coverage: FAIL" in result.transcript


def test_run_slice_unappliable_diff_fails_without_checks(tmp_path: Path) -> None:
    _slice_repo(tmp_path)
    dag = Dag.model_validate({"nodes": [_node_dict("n1", [])]})
    result = run_slice(
        "Fix f.",
        dag,
        workdir=tmp_path,
        journal_path=tmp_path / "proofs.jsonl",
        propose=lambda node: DiffProposal("not a diff\n", ""),
        now=lambda: "2026-09-16T00:00:00+00:00",
    )
    assert result.passed is False
    assert "- Gate " not in result.transcript
    assert "- Proof: none\n" in result.transcript


def test_run_slice_refuses_stale_journal(tmp_path: Path) -> None:
    _slice_repo(tmp_path)
    dag = Dag.model_validate({"nodes": [_node_dict("n1", [])]})
    journal = tmp_path / "proofs.jsonl"
    first = run_slice(
        "Fix f.",
        dag,
        workdir=tmp_path,
        journal_path=journal,
        propose=lambda node: DiffProposal(GOOD_DIFF, ""),
        now=lambda: "2026-09-16T00:00:00+00:00",
    )
    assert first.passed is True
    expected = f"journal {str(journal)!r} is not fresh; resume is not supported"
    with pytest.raises(ValueError, match=re.escape(expected)):
        run_slice(
            "Fix f.",
            dag,
            workdir=tmp_path,
            journal_path=journal,
            propose=lambda node: DiffProposal(GOOD_DIFF, ""),
        )


def test_run_slice_post_write_corruption_raises(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _slice_repo(tmp_path)
    dag = Dag.model_validate({"nodes": [_node_dict("n1", [])]})
    journal = tmp_path / "proofs.jsonl"

    def sabotage(path: Path, record: ProofRecord) -> None:
        append_record(path, record)
        with path.open("a", encoding="utf-8") as handle:
            handle.write('{"half": ')

    monkeypatch.setattr(slice_module, "append_record", sabotage)
    expected = f"journal {str(journal)!r} has a torn tail after our own writes"
    with pytest.raises(RuntimeError, match=re.escape(expected)):
        run_slice(
            "Fix f.",
            dag,
            workdir=tmp_path,
            journal_path=journal,
            propose=lambda node: DiffProposal(GOOD_DIFF, ""),
            now=lambda: "2026-09-16T00:00:00+00:00",
        )
