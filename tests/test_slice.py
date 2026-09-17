"""Tests for saddle.slice: vertical-slice driver and its transcript."""

from __future__ import annotations

import hashlib
import re
import subprocess
from datetime import datetime
from pathlib import Path

import pytest

import saddle.slice as slice_module
from saddle.dag import Dag, Node
from saddle.evidence import CapturedRun, run_argv
from saddle.gates import GateCheck, Tier1Result
from saddle.journal import (
    ProofRecord,
    SpanRecord,
    append_record,
    append_span,
    read_records,
    read_spans,
)
from saddle.slice import (
    NodeGateFailedError,
    NodeUnappliableError,
    ReplanFailedError,
    _apply_diff,
    _schedulable_nodes,
    _utcnow,
    format_attempt_failure,
    format_replan_history,
    run_slice,
    splice_replan,
)
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

BAD_DIFF = GOOD_DIFF.replace("+    assert f() == 2\n", "+    assert f() == 3\n")

FIX_DIFF = (
    "diff --git a/test_n.py b/test_n.py\n"
    "--- a/test_n.py\n"
    "+++ b/test_n.py\n"
    "@@ -4,2 +4,2 @@\n"
    " def test_f():  # REQ-001\n"
    "-    assert f() == 3\n"
    "+    assert f() == 2\n"
)

JUNK1_DIFF = (
    "diff --git a/junk1.py b/junk1.py\n"
    "new file mode 100644\n"
    "--- /dev/null\n"
    "+++ b/junk1.py\n"
    "@@ -0,0 +1 @@\n"
    "+X = 1\n"
)

JUNK2_DIFF = JUNK1_DIFF.replace("junk1.py", "junk2.py")


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
    assert error.attempts == 1
    assert NodeGateFailedError(result, attempts=3).attempts == 3


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


def test_run_slice_pass_end_to_end(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _slice_repo(tmp_path)
    dag = Dag.model_validate({"nodes": [_node_dict("n1", [])]})
    journal = tmp_path / "proofs.jsonl"
    ticks = iter([0.0, 1.0, 2.0, 3.0])
    monkeypatch.setattr(slice_module, "perf_counter", lambda: next(ticks))
    result = run_slice(
        "Fix f.",
        dag,
        workdir=tmp_path,
        journal_path=journal,
        propose=lambda node, failure: DiffProposal(GOOD_DIFF, "return two instead"),
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
    assert "  - thought: return two instead\n" in result.transcript
    assert re.search(
        r"  - tool git: exit 0 in \d+ms: git apply --index --recount -\n",
        result.transcript,
    )
    assert re.search(
        r"  - tool pytest: exit [1-9]\d* in \d+ms: pytest test_n.py\n", result.transcript
    )
    assert "worker:n1" not in result.transcript
    assert result.transcript.index("  - thought:") < result.transcript.index("  - tool ")
    spans = read_spans(journal)
    tools = [span for span in spans if span.kind == "tool"]
    assert [span.name for span in tools] == [
        "git",
        "git",
        "coverage",
        "git",
        "pytest",
        "ruff",
        "ruff",
    ]
    assert all(span.node_id == "n1" for span in tools)
    (worker, run) = [span for span in spans if span.kind == "agent"]
    assert (worker.name, worker.exit_code, worker.detail) == ("worker:n1", 0, "")
    assert worker.parent_id == run.span_id
    assert all(span.parent_id == worker.span_id for span in tools)
    assert (run.name, run.exit_code, run.parent_id, run.node_id) == ("run", 0, None, "")
    assert run.detail == "1 proven, 0 failed, 0 undispatched"
    assert (worker.duration_ms, run.duration_ms) == (1000, 3000)


def test_run_slice_gate_fail_leaves_dependent_undispatched(tmp_path: Path) -> None:
    _slice_repo(tmp_path)
    dag = Dag.model_validate({"nodes": [_node_dict("a", []), _node_dict("b", ["a"])]})
    bad_diff = GOOD_DIFF.replace(
        "+    return 2\n",
        "+    return 2\n+\n+\n+def unused():\n+    return 3\n",
    ).replace("@@ -1,2 +1,2 @@", "@@ -1,2 +1,6 @@")
    seen: list[str] = []

    def propose(node: Node, failure: str | None) -> DiffProposal:
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
    assert seen == ["a", "a"]
    assert "- Verdict: FAIL\n" in result.transcript
    assert "## Node a\n" in result.transcript
    assert "## Node b\n" in result.transcript
    assert "- Attempts: 2\n" in result.transcript
    assert "- Gate coverage: FAIL" in result.transcript
    assert "- Timeline:\n" in result.transcript
    assert "thought:" not in result.transcript
    assert re.search(
        r"  - tool git: exit 0 in \d+ms: git apply --index --recount -\n",
        result.transcript,
    )
    journal = tmp_path / "proofs.jsonl"
    spans = read_spans(journal)
    agents = [span for span in spans if span.kind == "agent"]
    assert [span.name for span in agents] == ["worker:a", "worker:a", "run"]
    (first, second, run) = agents
    assert (first.exit_code, first.detail) == (1, "attempt 1/3: 1 gate(s) failed")
    assert (second.exit_code, second.detail) == (
        1,
        "attempt 2/3: worker re-proposed an identical diff; stopping recovery",
    )
    assert first.parent_id == run.span_id
    assert second.parent_id == run.span_id
    assert run.exit_code == 1
    assert run.detail == "0 proven, 1 failed, 1 undispatched"


def test_run_slice_unappliable_diff_fails_without_checks(tmp_path: Path) -> None:
    _slice_repo(tmp_path)
    dag = Dag.model_validate({"nodes": [_node_dict("n1", [])]})
    result = run_slice(
        "Fix f.",
        dag,
        workdir=tmp_path,
        journal_path=tmp_path / "proofs.jsonl",
        propose=lambda node, failure: DiffProposal("not a diff\n", ""),
        now=lambda: "2026-09-16T00:00:00+00:00",
    )
    assert result.passed is False
    assert "- Gate " not in result.transcript
    assert "- Proof: none\n" in result.transcript
    assert re.search(
        r"  - tool git: exit [1-9]\d* in \d+ms: git apply --index --recount -\n",
        result.transcript,
    )
    spans = read_spans(tmp_path / "proofs.jsonl")
    assert len([span for span in spans if span.name == "git"]) == 1
    assert len([span for span in spans if span.kind == "agent"]) == 3
    assert "- Attempts: 2\n" in result.transcript


def test_run_slice_distinct_unappliable_diffs_exhaust_attempts(tmp_path: Path) -> None:
    _slice_repo(tmp_path)
    dag = Dag.model_validate({"nodes": [_node_dict("n1", [])]})
    journal = tmp_path / "proofs.jsonl"
    diffs = ["garbage one\n", "garbage two\n", "garbage three\n"]
    calls: list[str | None] = []

    def propose(node: Node, failure: str | None) -> DiffProposal:
        calls.append(failure)
        return DiffProposal(diffs[len(calls) - 1], "")

    result = run_slice(
        "Fix f.",
        dag,
        workdir=tmp_path,
        journal_path=journal,
        propose=propose,
        now=lambda: "2026-09-16T00:00:00+00:00",
    )
    assert result.passed is False
    assert len(calls) == 3
    assert "- Gate " not in result.transcript
    assert "- Attempts: 3\n" in result.transcript
    spans = read_spans(journal)
    assert len([span for span in spans if span.name == "git"]) == 3
    workers = [span for span in spans if span.name == "worker:n1"]
    assert len(workers) == 3
    assert all("diff did not apply" in span.detail for span in workers)


def test_run_slice_retry_repairs_failing_tests(tmp_path: Path) -> None:
    _slice_repo(tmp_path)
    dag = Dag.model_validate({"nodes": [_node_dict("n1", [])]})
    journal = tmp_path / "proofs.jsonl"
    seen_failures: list[str | None] = []

    def propose(node: Node, failure: str | None) -> DiffProposal:
        seen_failures.append(failure)
        return DiffProposal(BAD_DIFF if failure is None else FIX_DIFF, "fix it")

    result = run_slice(
        "Fix f.",
        dag,
        workdir=tmp_path,
        journal_path=journal,
        propose=propose,
        now=lambda: "2026-09-16T00:00:00+00:00",
    )
    assert result.passed is True
    assert len(seen_failures) == 2
    assert seen_failures[0] is None
    failure = seen_failures[1]
    assert failure is not None
    assert "Attempt 1 of 3 failed 2 gate(s):" in failure
    assert "- tests: 'pytest test_n.py' exited 1" in failure
    assert "FAILED" in failure
    assert "- Attempts: 2\n" in result.transcript
    (record,) = read_records(journal)
    assert record.attempts == 2
    assert record.evidence_id == "n1#2"
    joined = BAD_DIFF + "\n" + FIX_DIFF
    assert record.diff_hash == hashlib.sha256(joined.encode()).hexdigest()
    agents = [span for span in read_spans(journal) if span.kind == "agent"]
    assert [(span.name, span.exit_code) for span in agents] == [
        ("worker:n1", 1),
        ("worker:n1", 0),
        ("run", 0),
    ]
    assert agents[0].detail == "attempt 1/3: 2 gate(s) failed"
    assert agents[1].detail == "recovered after 2 attempts"


def test_run_slice_exhausted_retries_fail_with_attempts(tmp_path: Path) -> None:
    _slice_repo(tmp_path)
    dag = Dag.model_validate({"nodes": [_node_dict("n1", [])]})
    journal = tmp_path / "proofs.jsonl"
    diffs = [BAD_DIFF, JUNK1_DIFF, JUNK2_DIFF]
    seen_failures: list[str | None] = []

    def propose(node: Node, failure: str | None) -> DiffProposal:
        seen_failures.append(failure)
        return DiffProposal(diffs[len(seen_failures) - 1], "")

    result = run_slice(
        "Fix f.",
        dag,
        workdir=tmp_path,
        journal_path=journal,
        propose=propose,
        now=lambda: "2026-09-16T00:00:00+00:00",
    )
    assert result.passed is False
    assert len(seen_failures) == 3
    assert seen_failures[0] is None
    assert "Attempt 1 of 3" in (seen_failures[1] or "")
    assert "Attempt 2 of 3" in (seen_failures[2] or "")
    assert "- Attempts: 3\n" in result.transcript
    assert "- Gate tests: FAIL" in result.transcript
    agents = [span for span in read_spans(journal) if span.kind == "agent"]
    assert [span.detail for span in agents] == [
        "attempt 1/3: 2 gate(s) failed",
        "attempt 2/3: 3 gate(s) failed",
        "attempt 3/3: 3 gate(s) failed",
        "0 proven, 1 failed, 0 undispatched",
    ]


def test_run_slice_propose_error_seals_worker_span(tmp_path: Path) -> None:
    _slice_repo(tmp_path)
    dag = Dag.model_validate({"nodes": [_node_dict("n1", [])]})
    journal = tmp_path / "proofs.jsonl"

    def propose(node: Node, failure: str | None) -> DiffProposal:
        msg = "boom"
        raise RuntimeError(msg)

    calls: list[str] = []

    def replan(node: Node, history: str) -> Dag:
        calls.append(node.id)
        return dag

    result = run_slice(
        "Fix f.",
        dag,
        workdir=tmp_path,
        journal_path=journal,
        propose=propose,
        replan=replan,
        now=lambda: "2026-09-16T00:00:00+00:00",
    )
    assert result.passed is False
    assert "- Attempts:" not in result.transcript
    assert calls == []
    agents = [span for span in read_spans(journal) if span.kind == "agent"]
    assert [(span.name, span.exit_code, span.detail) for span in agents] == [
        ("worker:n1", 1, "boom"),
        ("run", 1, "0 proven, 1 failed, 0 undispatched"),
    ]


def _failed_result() -> Tier1Result:
    return Tier1Result(
        node_id="n1",
        passed=False,
        checks=(
            GateCheck(name="tests", passed=False, detail="'pytest test_n.py' exited 1"),
            GateCheck(name="syntax", passed=True, detail="2 file(s) parsed"),
        ),
    )


def test_format_attempt_failure_lists_failed_gates_only() -> None:
    text = format_attempt_failure(_failed_result(), [], attempt=1, max_attempts=3)
    assert text.startswith("Attempt 1 of 3 failed 1 gate(s):\n")
    assert "- tests: 'pytest test_n.py' exited 1\n" in text
    assert "syntax" not in text
    assert "---" not in text


def test_format_attempt_failure_skips_passing_runs() -> None:
    captured = [
        CapturedRun(argv=("pytest", "test_n.py"), exit_code=0, stdout="ok", stderr=""),
    ]
    text = format_attempt_failure(_failed_result(), captured, attempt=2, max_attempts=3)
    assert text.startswith("Attempt 2 of 3 failed 1 gate(s):\n")
    assert "---" not in text


def test_format_attempt_failure_includes_failing_output() -> None:
    captured = [
        CapturedRun(
            argv=("pytest", "test_n.py"),
            exit_code=1,
            stdout="traceback here",
            stderr="a warning",
        ),
    ]
    text = format_attempt_failure(_failed_result(), captured, attempt=1, max_attempts=3)
    assert "--- `pytest test_n.py` (exit 1) ---" in text
    assert "traceback here" in text
    assert "a warning" in text


def test_format_attempt_failure_truncates_long_output() -> None:
    captured = [
        CapturedRun(argv=("pytest",), exit_code=1, stdout="x" * 5000, stderr=""),
    ]
    text = format_attempt_failure(_failed_result(), captured, attempt=1, max_attempts=3)
    assert "[earlier output truncated]\n" + "x" * 4000 in text
    assert "x" * 4001 not in text


def test_format_attempt_failure_marks_empty_output() -> None:
    captured = [CapturedRun(argv=("pytest",), exit_code=2, stdout="", stderr="")]
    text = format_attempt_failure(_failed_result(), captured, attempt=1, max_attempts=3)
    assert "(no output)" in text


def test_splice_replan_rewires_dependents_to_new_leaves() -> None:
    dag = Dag.model_validate(
        {
            "nodes": [
                _node_dict("z", []),
                _node_dict("a", ["z"]),
                _node_dict("b", ["a"]),
                _node_dict("c", []),
            ]
        }
    )
    new = Dag.model_validate({"nodes": [_node_dict("m1", []), _node_dict("m2", ["m1"])]})
    merged, gen_ids = splice_replan(dag, "a", new)
    assert gen_ids == ["a.r1", "a.r2"]
    by_id = {node.id: node for node in merged.nodes}
    assert [node.id for node in merged.nodes] == ["z", "a", "b", "c", "a.r1", "a.r2"]
    assert by_id["a"].dependencies == ["z"]
    assert by_id["a.r1"].dependencies == ["z"]
    assert by_id["a.r2"].dependencies == ["a.r1"]
    assert by_id["b"].dependencies == ["a.r2"]
    assert by_id["c"].dependencies == []
    assert by_id["z"].dependencies == []


def test_splice_replan_rejects_collision() -> None:
    dag = Dag.model_validate({"nodes": [_node_dict("a", []), _node_dict("a.r1", [])]})
    new = Dag.model_validate({"nodes": [_node_dict("m1", [])]})
    with pytest.raises(ReplanFailedError, match="collides"):
        splice_replan(dag, "a", new)


def test_splice_replan_rejects_unknown_dependency() -> None:
    dag = Dag.model_validate({"nodes": [_node_dict("a", [])]})
    new = Dag.model_validate({"nodes": [_node_dict("m1", ["ghost"])]})
    with pytest.raises(ReplanFailedError, match="unknown node"):
        splice_replan(dag, "a", new)


def test_splice_replan_rejects_leafless_replan() -> None:
    dag = Dag.model_validate({"nodes": [_node_dict("a", [])]})
    new = Dag.model_validate({"nodes": [_node_dict("m1", ["m1"])]})
    with pytest.raises(ReplanFailedError, match="no leaf nodes"):
        splice_replan(dag, "a", new)


def test_format_replan_history_gate_failure() -> None:
    error = NodeGateFailedError(_failed_result(), 2, "Attempt 2 evidence\n")
    text = format_replan_history("n1", error)
    assert "Node 'n1' failed Tier-1 after 2 attempt(s):" in text
    assert "- tests: 'pytest test_n.py' exited 1" in text
    assert "Last attempt evidence:\nAttempt 2 evidence\n" in text


def test_format_replan_history_unappliable_without_evidence() -> None:
    error = NodeUnappliableError("n1", "no proposed diff applied in 3 attempts", 3)
    text = format_replan_history("n1", error)
    assert "Node 'n1' produced no applicable diff after 3 attempt(s)." in text
    assert "Last attempt" not in text


def test_schedulable_nodes_filters_proven_failed_blocked() -> None:
    dag = Dag.model_validate(
        {
            "nodes": [
                _node_dict("p", []),
                _node_dict("f", []),
                _node_dict("b", ["f"]),
                _node_dict("n", ["p"]),
            ]
        }
    )
    msg = "boom"
    ready = _schedulable_nodes(dag, {"p": "h"}, {"f": RuntimeError(msg)})
    assert [(node.id, node.dependencies) for node in ready] == [("n", [])]


def test_run_slice_replan_recovers_failed_node(tmp_path: Path) -> None:
    _slice_repo(tmp_path)
    dag = Dag.model_validate({"nodes": [_node_dict("n1", [])]})
    journal = tmp_path / "proofs.jsonl"
    replans: list[tuple[str, str]] = []

    def propose(node: Node, failure: str | None) -> DiffProposal:
        return DiffProposal(BAD_DIFF if node.id == "n1" else FIX_DIFF, "")

    def replan(node: Node, history: str) -> Dag:
        replans.append((node.id, history))
        return Dag.model_validate({"nodes": [_node_dict("m1", [])]})

    result = run_slice(
        "Fix f.",
        dag,
        workdir=tmp_path,
        journal_path=journal,
        propose=propose,
        replan=replan,
        now=lambda: "2026-09-16T00:00:00+00:00",
    )
    assert result.passed is True
    assert len(replans) == 1
    assert replans[0][0] == "n1"
    assert "failed Tier-1 after 2 attempt(s)" in replans[0][1]
    assert list(result.proofs) == ["n1.r1"]
    assert "## Node n1\n" in result.transcript
    assert "## Node n1.r1\n" in result.transcript
    assert result.transcript.count("- Attempts: 2\n") == 1
    (record,) = read_records(journal)
    assert (record.node_id, record.attempts) == ("n1.r1", 1)


def test_run_slice_replanned_node_failure_stays_failed(tmp_path: Path) -> None:
    _slice_repo(tmp_path)
    dag = Dag.model_validate({"nodes": [_node_dict("n1", [])]})
    journal = tmp_path / "proofs.jsonl"
    calls: list[str] = []

    def propose(node: Node, failure: str | None) -> DiffProposal:
        calls.append(node.id)
        return DiffProposal(BAD_DIFF, "")

    def replan(node: Node, history: str) -> Dag:
        calls.append(f"replan:{node.id}")
        return Dag.model_validate({"nodes": [_node_dict("m1", [])]})

    result = run_slice(
        "Fix f.",
        dag,
        workdir=tmp_path,
        journal_path=journal,
        propose=propose,
        replan=replan,
        now=lambda: "2026-09-16T00:00:00+00:00",
    )
    assert result.passed is False
    assert calls.count("replan:n1") == 1
    assert "replan:n1.r1" not in calls
    assert result.transcript.count("- Attempts: 2\n") == 2
    run = next(span for span in read_spans(journal) if span.name == "run")
    assert run.detail == "0 proven, 1 failed, 0 undispatched"


def test_run_slice_replan_failure_keeps_node_failed(tmp_path: Path) -> None:
    _slice_repo(tmp_path)
    dag = Dag.model_validate({"nodes": [_node_dict("n1", [])]})
    journal = tmp_path / "proofs.jsonl"

    def propose(node: Node, failure: str | None) -> DiffProposal:
        return DiffProposal(BAD_DIFF, "")

    def replan(node: Node, history: str) -> Dag:
        msg = "emission exhausted"
        raise ReplanFailedError(msg)

    result = run_slice(
        "Fix f.",
        dag,
        workdir=tmp_path,
        journal_path=journal,
        propose=propose,
        replan=replan,
        now=lambda: "2026-09-16T00:00:00+00:00",
    )
    assert result.passed is False
    assert "- Attempts: 2\n" in result.transcript
    run = next(span for span in read_spans(journal) if span.name == "run")
    assert run.detail == "0 proven, 1 failed, 0 undispatched"


def test_run_slice_pass_with_replan_callback_unused(tmp_path: Path) -> None:
    _slice_repo(tmp_path)
    dag = Dag.model_validate({"nodes": [_node_dict("n1", [])]})
    calls: list[str] = []

    def replan(node: Node, history: str) -> Dag:
        calls.append(node.id)
        return dag

    result = run_slice(
        "Fix f.",
        dag,
        workdir=tmp_path,
        journal_path=tmp_path / "proofs.jsonl",
        propose=lambda node, failure: DiffProposal(GOOD_DIFF, ""),
        replan=replan,
        now=lambda: "2026-09-16T00:00:00+00:00",
    )
    assert result.passed is True
    assert calls == []


def test_run_slice_refuses_stale_journal(tmp_path: Path) -> None:
    _slice_repo(tmp_path)
    dag = Dag.model_validate({"nodes": [_node_dict("n1", [])]})
    journal = tmp_path / "proofs.jsonl"
    first = run_slice(
        "Fix f.",
        dag,
        workdir=tmp_path,
        journal_path=journal,
        propose=lambda node, failure: DiffProposal(GOOD_DIFF, ""),
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
            propose=lambda node, failure: DiffProposal(GOOD_DIFF, ""),
        )


def test_run_slice_post_write_corruption_raises(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _slice_repo(tmp_path)
    dag = Dag.model_validate({"nodes": [_node_dict("n1", [])]})
    journal = tmp_path / "proofs.jsonl"

    def sabotage(path: Path, span: SpanRecord) -> None:
        append_span(path, span)
        if span.name == "run":
            with path.open("a", encoding="utf-8") as handle:
                handle.write('{"half": ')

    monkeypatch.setattr(slice_module, "append_span", sabotage)
    expected = f"journal {str(journal)!r} has a torn tail after our own writes"
    with pytest.raises(RuntimeError, match=re.escape(expected)):
        run_slice(
            "Fix f.",
            dag,
            workdir=tmp_path,
            journal_path=journal,
            propose=lambda node, failure: DiffProposal(GOOD_DIFF, ""),
            now=lambda: "2026-09-16T00:00:00+00:00",
        )


def test_run_slice_mid_run_corruption_raises(
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
    with pytest.raises(ValueError, match="failed verification: unparseable-line"):
        run_slice(
            "Fix f.",
            dag,
            workdir=tmp_path,
            journal_path=journal,
            propose=lambda node, failure: DiffProposal(GOOD_DIFF, ""),
            now=lambda: "2026-09-16T00:00:00+00:00",
        )
