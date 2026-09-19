"""Tests for saddle.slice: vertical-slice driver and its transcript."""

from __future__ import annotations

import asyncio
import hashlib
import os
import re
import stat
import subprocess
from datetime import datetime
from pathlib import Path
from typing import Final

import pytest

import saddle.slice as slice_module
from saddle.dag import Dag, Node
from saddle.evidence import CapturedRun, run_argv, run_capture
from saddle.gates import MIN_SIGNIFICANT_MUTANTS, GateCheck, Tier1Result
from saddle.journal import (
    ProofRecord,
    SpanRecord,
    SpanRecorder,
    append_record,
    append_span,
    read_records,
    read_spans,
)
from saddle.slice import (
    MAX_RECOVERY_RETRIES,
    PROPOSAL_SAMPLES,
    RECOVERY_OUTPUT_CHARS,
    NodeGateFailedError,
    NodeUnappliableError,
    ReplanFailedError,
    SliceResult,
    _apply_diff,
    _HaltRecoveryError,
    _run_node,
    _schedulable_nodes,
    _utcnow,
    autofix,
    format_attempt_failure,
    format_replan_history,
    run_slice,
    splice_replan,
)
from saddle.vllm import DiffProposal, VllmResponseError


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


# Every tool name the global allowlist carries (T3-4). A node listing all
# four behaves exactly as it did before each name was bound to a harness
# behaviour, so this is the fixtures' known-good default; a test that pins
# one binding passes a shorter list.
ALL_TOOLS: Final[tuple[str, ...]] = ("read_file", "write_file", "run_tests", "lint")


def _node_dict(
    node_id: str, deps: list[str], *, tools: list[str] | None = None
) -> dict[str, object]:
    return {
        "id": node_id,
        "kind": "impl",
        "dependencies": deps,
        "task_prompt": f"Do {node_id}.",
        "requirements": [{"id": "REQ-001", "statement": "REQ-001 holds."}],
        "execution_constraints": {
            # All four by default (T3-4): these nodes create test files and
            # read the suite output back on a failed attempt, which is what
            # they did before each name was bound to a behaviour.
            "reasoning_budget": "low",
            "allowed_tools": tools if tools is not None else list(ALL_TOOLS),
            "max_context_tokens": 8000,
        },
        "deterministic_gate": {
            "test_command": "pytest test_n.py",
            "changed_line_coverage_min": 100.0,
            "red_phase_required": True,
            "mutation_sample": {
                "scope": "changed-lines",
                "max_mutants": 100,
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
    (root / "test_n.py").write_text(
        "from n import f\n\n\ndef test_f():  # REQ-001\n    assert f() == 2\n"
    )
    assert run_argv(["git", "add", "n.py", "test_n.py"], root) == 0
    assert run_argv(["git", "commit", "-m", "baseline"], root) == 0


GOOD_DIFF = (
    "diff --git a/n.py b/n.py\n"
    "--- a/n.py\n"
    "+++ b/n.py\n"
    "@@ -1,2 +1,2 @@\n"
    " def f():\n"
    "-    return 1\n"
    "+    return 2\n"
)

BAD_DIFF = GOOD_DIFF.replace("+    return 2\n", "+    return 3\n")

FIX_DIFF = (
    "diff --git a/n.py b/n.py\n"
    "--- a/n.py\n"
    "+++ b/n.py\n"
    "@@ -1,2 +1,2 @@\n"
    " def f():\n"
    "-    return 3\n"
    "+    return 2\n"
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

TRUNCATED = "completion truncated (finish_reason=length)"


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


def test_node_unappliable_carries_evidence() -> None:
    error = NodeUnappliableError("n1", "no proposed diff applied in 3 attempts", 3, "Attempt 3")
    assert str(error) == "node 'n1': no proposed diff applied in 3 attempts"
    assert error.node_id == "n1"
    assert error.attempts == 3
    assert error.failure == "Attempt 3"
    defaulted = NodeUnappliableError("n1", "detail")
    assert (defaulted.attempts, defaulted.failure) == (1, None)


def test_halt_recovery_carries_evidence() -> None:
    error = _HaltRecoveryError(None, 2, "Attempt 2 evidence")
    assert str(error) == "worker re-proposed an identical diff"
    assert error.result is None
    assert error.attempts == 2
    assert error.failure == "Attempt 2 evidence"


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
    """Content without a git header is named as such, not as a failed apply.

    It also short-circuits: running the whole tolerance ladder over prose
    costs four `git apply` invocations to learn something the first
    character already said.
    """
    _git_repo(tmp_path)
    expected = f"worker content is not a unified diff (no 'diff --git' header) in {str(tmp_path)!r}"
    with pytest.raises(RuntimeError, match=re.escape(expected)):
        _apply_diff(tmp_path, "not a diff\n")


def test_apply_diff_header_without_hunk_raises(tmp_path: Path) -> None:
    """A header with no '@@' hunk is schema-valid but applies nothing.

    The decoding grammar cannot be relied on to force a hunk; a degenerate
    completion that stops right after the header satisfies the grammar
    and would otherwise cost all four _APPLY_MODES a git-apply exit 128
    ("No valid patches in input") before failing with no useful detail.
    """
    _git_repo(tmp_path)
    expected = f"worker diff has a header but no hunk ('@@ ' marker) in {str(tmp_path)!r}"
    with pytest.raises(RuntimeError, match=re.escape(expected)):
        _apply_diff(tmp_path, "diff --git a/n.py b/n.py\n--- a/n.py\n+++ b/n.py\n")


def test_autofix_fixes_what_ruff_can_fix(tmp_path: Path) -> None:
    """Formatting never reaches the worker: ruff solves it exactly.

    A repair attempt spent on `ruff format --check` is one not spent on
    the correctness defect, which is how T7 finished with an infinite
    loop untouched (F13).
    """
    _git_repo(tmp_path)
    (tmp_path / "n.py").write_text("import os\nx     =    1\n")
    assert run_argv(["git", "add", "n.py"], tmp_path) == 0
    autofix(tmp_path)
    fixed = (tmp_path / "n.py").read_text()
    assert "x = 1\n" in fixed  # ruff format normalised the spacing
    assert "x     =    1" not in fixed
    assert "import os" not in fixed  # ruff check --fix removed the unused import
    # Both legs of check_ruff are now satisfied without spending an attempt.
    assert run_argv(["ruff", "format", "--check", "n.py"], tmp_path) == 0
    assert run_argv(["ruff", "check", "n.py"], tmp_path) == 0


def test_autofix_leaves_files_the_node_did_not_change(tmp_path: Path) -> None:
    """Scoped to changed files, or coverage would be owed on untouched lines.

    `changed_line_coverage_min` is 100.0 against `git diff <baseline>`.
    Formatting the whole tree would mark every pre-existing unformatted
    file changed and demand the node cover lines it never wrote.
    """
    _git_repo(tmp_path)
    (tmp_path / "untouched.py").write_text("import os\ny     =    2\n")
    assert run_argv(["git", "add", "untouched.py"], tmp_path) == 0
    assert run_argv(["git", "commit", "-m", "pre-existing mess"], tmp_path) == 0
    (tmp_path / "n.py").write_text("z     =    3\n")
    assert run_argv(["git", "add", "n.py"], tmp_path) == 0
    autofix(tmp_path)
    assert (tmp_path / "n.py").read_text() == "z = 3\n"
    assert (tmp_path / "untouched.py").read_text() == "import os\ny     =    2\n"


def test_autofix_no_python_changes_runs_no_tools(tmp_path: Path) -> None:
    """Nothing changed means no subprocess and no spans to explain."""
    _git_repo(tmp_path)
    journal = tmp_path / "spans.jsonl"
    recorder = SpanRecorder(path=journal, node_id="n1", parent_id="p1")
    autofix(tmp_path, recorder=recorder)
    names = [span.name for span in read_spans(journal)] if journal.exists() else []
    assert names == ["git"]  # the scope query only; no ruff, no re-stage


def test_autofix_ignores_changed_non_python_files(tmp_path: Path) -> None:
    """Only Python files reach ruff, even when the node changed others.

    Found by a surviving contract mutant: dropping the `.py` filter broke
    no test, because the pre-existing-file case uses a *committed* file,
    which `git diff` never reports whatever the filter does. A changed
    non-Python file is the case that discriminates.
    """
    _git_repo(tmp_path)
    (tmp_path / "notes.txt").write_text("not python  at  all\n")
    (tmp_path / "data.json").write_text('{"a":   1}\n')
    assert run_argv(["git", "add", "notes.txt", "data.json"], tmp_path) == 0
    journal = tmp_path / "spans.jsonl"
    recorder = SpanRecorder(path=journal, node_id="n1", parent_id="p1")
    autofix(tmp_path, recorder=recorder)
    names = [span.name for span in read_spans(journal)] if journal.exists() else []
    assert names == ["git"]  # scope query only: ruff was never invoked
    assert (tmp_path / "notes.txt").read_text() == "not python  at  all\n"
    assert (tmp_path / "data.json").read_text() == '{"a":   1}\n'


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
        # This test pins the exact tool-span sequence and mocks perf_counter
        # with four ticks; the merge-time suite (T2-3) is exercised on its own.
        merge_command=None,
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
    # Only n.py is in the diff -- test_n.py is unchanged from the impl
    # node's own baseline (T2-2a), so it is not in ruff's changed-file scope.
    assert "- Gate ruff: PASS (1 file(s) clean)\n" in result.transcript
    assert "- Gate tests: PASS ('pytest test_n.py' exited 0)\n" in result.transcript
    assert "- Gate coverage: PASS (100.0% >= 100.0%)\n" in result.transcript
    assert "- Gate red-phase: PASS (fail pre-change, pass post-change)\n" in result.transcript
    assert "- Gate requirement-binding: PASS (1 requirement(s) bound)\n" in result.transcript
    assert "- Gate mutation: PASS (100.0% >= 85.0% over 5 mutant(s))\n" in result.transcript
    assert f"- Proof: {result.proofs['n1']}\n" in result.transcript
    assert "- Issues: none (chain verifies)\n" in result.transcript
    assert read_records(journal)[0].thinking == "return two instead"
    assert "  - thought: return two instead\n" in result.transcript
    assert re.search(
        r"  - tool git: exit 0 in \d+ms: git apply --index --recount -\n",
        result.transcript,
    )
    # Red-phase baseline leg: coverage-wrapped like the current leg, failing
    # because the node's own test runs against pre-change code.
    assert re.search(
        r"  - tool coverage: exit [1-9]\d* in \d+ms: coverage run [^\n]*-m pytest test_n.py\n",
        result.transcript,
    )
    assert "worker:n1" not in result.transcript
    assert result.transcript.index("  - thought:") < result.transcript.index("  - tool ")
    spans = read_spans(journal)
    tools = [span for span in spans if span.kind == "tool"]
    assert [span.name for span in tools] == [
        # T3-8: the node's own baseline -- `add -u`, `write-tree`,
        # `commit-tree`, `update-ref` -- taken before any proposal is drawn,
        # so every ref below is this node's snapshot and not `HEAD`.
        *["git"] * 4,
        "git",
        # autofix (#66): scope to the node's changed files, apply ruff's
        # mechanical fixes, re-stage -- all before anything measures the
        # tree, and all journaled, because the sealed artifact now differs
        # from the diff the worker proposed.
        "git",
        "ruff",
        "ruff",
        "git",
        "git",
        # T2-2: the staged-adds probe behind node-scope's file-creation rule.
        "git",
        # T3-2: changed-files list for target-scope.
        "git",
        "coverage",
        "git",
        # One per red-phase baseline sample (#54): this node's test is
        # unchanged from its own baseline (T2-2a's honest impl fixture), so
        # `tests_changed` is False and only one sample is taken.
        *["coverage"] * 1,
        "timeout",
        # `results`, then one `show` per mutant in the sample (#49).
        "mutmut",
        *["mutmut"] * MIN_SIGNIFICANT_MUTANTS,
        "ruff",
        "ruff",
    ]
    assert all(span.node_id == "n1" for span in tools)
    (worker, run) = [span for span in spans if span.kind == "agent"]
    assert (worker.name, worker.exit_code) == ("worker:n1", 0)
    # The seal records sampling agreement: the correlation signal is
    # only useful if it is written down (#59). Known-bad-for-diversity: a
    # constant proposer still dedups to one distinct sample.
    assert worker.detail == f"1 distinct of {PROPOSAL_SAMPLES} sample(s)"
    assert worker.parent_id == run.span_id
    assert all(span.parent_id == worker.span_id for span in tools)
    assert (run.name, run.exit_code, run.parent_id, run.node_id) == ("run", 0, None, "")
    assert run.detail == "1 proven, 0 failed, 0 undispatched"
    assert (worker.duration_ms, run.duration_ms) == (1000, 3000)


def test_run_slice_pass_end_to_end_records_distinct_sample_count(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Known-good half of #59: three distinct, individually appliable
    # proposals are all drawn (the first two gate-fail, the third wins),
    # so the seal reports "3 distinct" rather than short-circuiting.
    _slice_repo(tmp_path)
    dag = Dag.model_validate({"nodes": [_node_dict("n1", [])]})
    journal = tmp_path / "proofs.jsonl"
    ticks = iter([0.0, 1.0, 2.0, 3.0])
    monkeypatch.setattr(slice_module, "perf_counter", lambda: next(ticks))
    bad2 = BAD_DIFF.replace("+    return 3\n", "+    return 4\n")
    proposals = iter(
        [
            DiffProposal(BAD_DIFF, "first guess"),
            DiffProposal(bad2, "second guess"),
            DiffProposal(GOOD_DIFF, "return two instead"),
        ]
    )
    result = run_slice(
        "Fix f.",
        dag,
        workdir=tmp_path,
        journal_path=journal,
        propose=lambda node, failure: next(proposals),
        merge_command=None,  # four perf_counter ticks, as above
        now=lambda: "2026-09-16T00:00:00+00:00",
    )
    assert result.passed is True
    spans = read_spans(journal)
    (worker, run) = [span for span in spans if span.kind == "agent"]
    assert (worker.name, worker.exit_code) == ("worker:n1", 0)
    assert worker.detail == f"3 distinct of {PROPOSAL_SAMPLES} sample(s)"
    assert run.detail == "1 proven, 0 failed, 0 undispatched"


def test_run_slice_merge_suite_gate_runs_once_and_is_journaled(tmp_path: Path) -> None:
    """Known-good (T2-3, #60): after every node is proven, the unscoped merge
    command runs once in the workdir and seals a `merge-suite` tool span
    under the run span; exit 0 keeps the run passing."""
    _slice_repo(tmp_path)
    dag = Dag.model_validate({"nodes": [_node_dict("n1", [])]})
    journal = tmp_path / "proofs.jsonl"
    result = run_slice(
        "Fix f.",
        dag,
        workdir=tmp_path,
        journal_path=journal,
        propose=lambda node, failure: DiffProposal(GOOD_DIFF, "return two instead"),
        merge_command="true",
        now=lambda: "2026-09-16T00:00:00+00:00",
    )
    assert result.passed is True
    assert list(result.proofs) == ["n1"]
    spans = read_spans(journal)
    (merge,) = [span for span in spans if span.name == "merge-suite"]
    run = next(span for span in spans if span.name == "run")
    assert (merge.kind, merge.node_id, merge.exit_code) == ("tool", "", 0)
    assert merge.argv == ["true"]
    assert merge.detail == "merge-time full suite: exit 0"
    assert merge.parent_id == run.span_id
    assert run.exit_code == 0
    assert run.detail == "1 proven, 0 failed, 0 undispatched, merge exit 0"
    # T2-4: the sealed record says what each verdict rested on.
    (record,) = read_records(journal)
    basis = {output.name: output.basis for output in record.gate_outputs}
    assert basis["mutation"] == "sampled n=5"
    assert basis["coverage"] == "changed-lines=1"
    assert basis["tests"] is None


def test_run_slice_merge_suite_failure_fails_the_run_not_the_node(tmp_path: Path) -> None:
    """Known-bad (T2-3, #60): a node can pass its own scoped gate while the
    union of diffs breaks the suite. The merge command's non-zero exit fails
    the run; the node's proof record stays, because its verdict was earned."""
    _slice_repo(tmp_path)
    dag = Dag.model_validate({"nodes": [_node_dict("n1", [])]})
    journal = tmp_path / "proofs.jsonl"
    result = run_slice(
        "Fix f.",
        dag,
        workdir=tmp_path,
        journal_path=journal,
        propose=lambda node, failure: DiffProposal(GOOD_DIFF, "return two instead"),
        merge_command="false",
        now=lambda: "2026-09-16T00:00:00+00:00",
    )
    assert result.passed is False
    assert list(result.proofs) == ["n1"]
    (record,) = read_records(journal)
    assert record.node_id == "n1"
    assert "- Verdict: FAIL\n" in result.transcript
    spans = read_spans(journal)
    (merge,) = [span for span in spans if span.name == "merge-suite"]
    run = next(span for span in spans if span.name == "run")
    assert merge.exit_code == 1
    assert merge.detail == "merge-time full suite: exit 1"
    assert run.exit_code == 1
    assert run.detail == "1 proven, 0 failed, 0 undispatched, merge exit 1"


def test_run_slice_merge_suite_is_skipped_when_nothing_was_proven(tmp_path: Path) -> None:
    """No proven node means no merged tree to test: the merge command does
    not run and the run detail carries no merge exit."""
    _slice_repo(tmp_path)
    dag = Dag.model_validate({"nodes": [_node_dict("n1", [])]})
    journal = tmp_path / "proofs.jsonl"
    result = run_slice(
        "Fix f.",
        dag,
        workdir=tmp_path,
        journal_path=journal,
        propose=lambda node, failure: DiffProposal(BAD_DIFF, ""),
        merge_command="true",
        now=lambda: "2026-09-16T00:00:00+00:00",
    )
    assert result.passed is False
    spans = read_spans(journal)
    assert [span for span in spans if span.name == "merge-suite"] == []
    run = next(span for span in spans if span.name == "run")
    assert "merge exit" not in run.detail


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
    # Attempt 1 draws PROPOSAL_SAMPLES unconditioned samples before any
    # recovery attempt (#59), so proposal counts no longer equal attempts.
    assert set(seen) == {"a"}
    # PROPOSAL_SAMPLES draws, then one recovery attempt that re-proposes
    # the same diff and halts. Identical samples are evaluated once, but
    # the worker is still asked PROPOSAL_SAMPLES times.
    assert len(seen) == PROPOSAL_SAMPLES + 1
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
    tools = [span for span in spans if span.kind == "tool"]
    assert tools
    assert {tool.parent_id for tool in tools} == {first.span_id}


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
    # No `git apply` line: prose is rejected on its missing header before
    # the tolerance ladder runs (#52).
    assert "git apply" not in result.transcript
    spans = read_spans(tmp_path / "proofs.jsonl")
    # One span per apply mode tried: the ladder journals every attempt, so
    # a diff that needed loosening -- or exhausted the ladder -- is visible.
    # Prose never reaches the ladder (#52): the header check rejects it
    # before the first `git apply`, so no apply span is recorded at all.
    assert [span for span in spans if "apply" in span.argv] == []
    # The node's baseline snapshot is the only `git` that runs, once, before
    # the first proposal is drawn (T3-8): four runs, named rather than
    # counted, so a fifth git run could not hide behind the total.
    git_runs = [span for span in spans if span.name == "git"]
    assert [span.argv[3] for span in git_runs] == ["add", "write-tree", "commit-tree", "update-ref"]
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
        phase = 0 if failure is None else sum(1 for seen in calls if seen is not None)
        return DiffProposal(diffs[min(phase, len(diffs) - 1)], "")

    result = run_slice(
        "Fix f.",
        dag,
        workdir=tmp_path,
        journal_path=journal,
        propose=propose,
        now=lambda: "2026-09-16T00:00:00+00:00",
    )
    assert result.passed is False
    # Attempt 1 draws PROPOSAL_SAMPLES unconditioned samples before any
    # recovery attempt (#59), so proposal counts no longer equal attempts.
    # PROPOSAL_SAMPLES draws on attempt 1 (none apply, so the last is
    # reused rather than paying for a fourth), then one proposal per
    # remaining attempt.
    assert len(calls) == PROPOSAL_SAMPLES + MAX_RECOVERY_RETRIES
    assert "- Gate " not in result.transcript
    assert "- Attempts: 3\n" in result.transcript
    spans = read_spans(journal)
    assert [span for span in spans if "apply" in span.argv] == []
    workers = [span for span in spans if span.name == "worker:n1"]
    assert len(workers) == 3
    assert all("diff did not apply" in span.detail for span in workers)
    git_runs = [span for span in spans if span.name == "git"]
    run = next(span for span in spans if span.name == "run")
    assert [span.exit_code for span in workers] == [1, 1, 1]
    assert [span.parent_id for span in workers] == [run.span_id] * 3
    # Prose short-circuits before the ladder, so the only git spans parented
    # to a worker attempt are the baseline snapshot's, taken once on attempt
    # 1 and reused by attempts 2..N (T3-8).
    assert [span.argv[3] for span in git_runs] == ["add", "write-tree", "commit-tree", "update-ref"]
    assert {span.parent_id for span in git_runs} == {workers[0].span_id}


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
    # Attempt 1 draws PROPOSAL_SAMPLES unconditioned samples before any
    # recovery attempt (#59), so proposal counts no longer equal attempts.
    assert len(seen_failures) == 4
    assert seen_failures[0] is None
    # Samples are unconditioned; the first recovery attempt carries the
    # gate evidence (#59).
    failure = next(entry for entry in seen_failures if entry is not None)
    assert failure is not None
    # BAD_DIFF's fault now lives in n.py (T2-2a), so the changed line no
    # longer matches the conftest mutmut stub's fixed "n.py:2 was return 2"
    # location; the mutation gate correctly finds no evidence there, on top
    # of tests and red-phase failing -- three gates, not two.
    assert "Attempt 1 of 3 failed 3 gate(s):" in failure
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
    assert agents[0].detail == "attempt 1/3: 3 gate(s) failed"
    assert agents[1].detail == "recovered after 2 attempts"


def test_run_slice_withholds_suite_output_from_a_node_without_run_tests(
    tmp_path: Path,
) -> None:
    """T3-4 end to end: the node's own `allowed_tools` reach the repair prompt.

    Known-good is `test_run_slice_retry_repairs_failing_tests` directly
    above, whose node lists all four and whose recovery prompt carries
    pytest's "FAILED" lines. Same repo, same diffs, one name removed: the
    gate verdicts survive and the captured suite output does not. A unit
    test of `format_attempt_failure` cannot see this -- the wiring from
    `node.execution_constraints` is the half that was decorative.
    """
    _slice_repo(tmp_path)
    node = _node_dict("n1", [], tools=["read_file", "write_file", "lint"])
    dag = Dag.model_validate({"nodes": [node]})
    journal = tmp_path / "proofs.jsonl"
    seen_failures: list[str | None] = []

    def propose(_node: Node, failure: str | None) -> DiffProposal:
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
    failure = next(entry for entry in seen_failures if entry is not None)
    assert failure is not None
    assert "Attempt 1 of 3 failed 3 gate(s):" in failure
    assert "- tests: 'pytest test_n.py' exited 1" in failure
    assert "FAILED" not in failure
    assert "coverage run" not in failure


def test_run_slice_exhausted_retries_fail_with_attempts(tmp_path: Path) -> None:
    _slice_repo(tmp_path)
    dag = Dag.model_validate({"nodes": [_node_dict("n1", [])]})
    journal = tmp_path / "proofs.jsonl"
    diffs = [BAD_DIFF, JUNK1_DIFF, JUNK2_DIFF]
    seen_failures: list[str | None] = []

    def propose(node: Node, failure: str | None) -> DiffProposal:
        seen_failures.append(failure)
        phase = 0 if failure is None else sum(1 for entry in seen_failures if entry is not None)
        return DiffProposal(diffs[min(phase, len(diffs) - 1)], "")

    result = run_slice(
        "Fix f.",
        dag,
        workdir=tmp_path,
        journal_path=journal,
        propose=propose,
        now=lambda: "2026-09-16T00:00:00+00:00",
    )
    assert result.passed is False
    # Attempt 1 draws PROPOSAL_SAMPLES unconditioned samples before any
    # recovery attempt (#59), so proposal counts no longer equal attempts.
    assert len(seen_failures) == PROPOSAL_SAMPLES + MAX_RECOVERY_RETRIES
    assert seen_failures[0] is None
    # Samples carry no failure; index the recovery attempts instead.
    recoveries = [entry for entry in seen_failures if entry is not None]
    assert "Attempt 1 of 3" in recoveries[0]
    assert "Attempt 2 of 3" in recoveries[1]
    assert "- Attempts: 3\n" in result.transcript
    assert "- Gate tests: FAIL" in result.transcript
    agents = [span for span in read_spans(journal) if span.kind == "agent"]
    # BAD_DIFF's fault now lives in n.py (T2-2a), which also breaks the
    # conftest mutmut stub's location match (see the retry test above), and
    # JUNK1_DIFF/JUNK2_DIFF's new files stay permanently uncovered on top of
    # that -- one more failing gate at every attempt than before.
    assert [span.detail for span in agents] == [
        "attempt 1/3: 3 gate(s) failed",
        "attempt 2/3: 4 gate(s) failed",
        "attempt 3/3: 4 gate(s) failed",
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
    assert agents[0].parent_id == agents[1].span_id


def test_run_node_identical_after_nonapply_reports_unappliable(tmp_path: Path) -> None:
    _slice_repo(tmp_path)
    node = Node.model_validate(_node_dict("n1", []))

    def propose(node: Node, failure: str | None) -> DiffProposal:
        return DiffProposal("not a diff\n", "")

    with pytest.raises(NodeUnappliableError) as caught:
        asyncio.run(_run_node(node, tmp_path, tmp_path / "proofs.jsonl", propose, {}, "run"))
    error = caught.value
    assert error.node_id == "n1"
    assert error.attempts == 2
    assert error.failure is not None
    assert error.failure.startswith("Attempt 1 of 3: diff did not apply: ")
    assert str(error) == "node 'n1': identical diff re-proposed after 2 non-applying attempt(s)"


def test_run_node_exhausted_nonapply_reports_unappliable(tmp_path: Path) -> None:
    _slice_repo(tmp_path)
    node = Node.model_validate(_node_dict("n1", []))
    diffs = ["garbage one\n", "garbage two\n", "garbage three\n"]
    calls: list[str | None] = []

    def propose(node: Node, failure: str | None) -> DiffProposal:
        calls.append(failure)
        phase = 0 if failure is None else sum(1 for seen in calls if seen is not None)
        return DiffProposal(diffs[min(phase, len(diffs) - 1)], "")

    with pytest.raises(NodeUnappliableError) as caught:
        asyncio.run(_run_node(node, tmp_path, tmp_path / "proofs.jsonl", propose, {}, "run"))
    error = caught.value
    assert error.node_id == "n1"
    assert error.attempts == 3
    assert error.failure is not None
    assert error.failure.startswith("Attempt 3 of 3: diff did not apply: ")
    assert str(error) == "node 'n1': no proposed diff applied in 3 attempts"


def test_run_node_truncated_worker_call_retries_instead_of_dying(tmp_path: Path) -> None:
    """A truncated completion spends an attempt; it does not kill the node.

    T5's worker hit `finish_reason=length` and the node failed with the
    worktree untouched, because the worker path let VllmResponseError
    escape while `_emit_valid_dag` retried the identical condition for
    the planner (#51).
    """
    _slice_repo(tmp_path)
    node = Node.model_validate(_node_dict("n1", []))
    calls: list[str | None] = []

    def propose(node: Node, failure: str | None) -> DiffProposal:
        calls.append(failure)
        if len(calls) <= PROPOSAL_SAMPLES + 1:
            raise VllmResponseError(TRUNCATED)
        return DiffProposal(GOOD_DIFF, "")

    asyncio.run(_run_node(node, tmp_path, tmp_path / "proofs.jsonl", propose, {}, "run"))
    assert (tmp_path / "n.py").read_text() == "def f():\n    return 2\n"
    # The first attempt's samples and its fallback draw all raised, so the
    # node recovered on a later attempt rather than raising.
    assert len(calls) > PROPOSAL_SAMPLES


def test_run_node_every_worker_call_truncated_fails_the_node(tmp_path: Path) -> None:
    """Retrying is not waiving: exhausting attempts still fails the node."""
    _slice_repo(tmp_path)
    node = Node.model_validate(_node_dict("n1", []))

    def propose(node: Node, failure: str | None) -> DiffProposal:
        raise VllmResponseError(TRUNCATED)

    with pytest.raises(NodeUnappliableError) as caught:
        asyncio.run(_run_node(node, tmp_path, tmp_path / "proofs.jsonl", propose, {}, "run"))
    error = caught.value
    assert error.attempts == 3
    assert error.failure is not None
    assert error.failure.startswith("Attempt 3 of 3: worker call failed: ")


def test_run_node_exhausted_gate_failures_reports_failure(tmp_path: Path) -> None:
    _slice_repo(tmp_path)
    node = Node.model_validate(_node_dict("n1", []))
    diffs = [BAD_DIFF, JUNK1_DIFF, JUNK2_DIFF]
    seen: list[str | None] = []

    def propose(node: Node, failure: str | None) -> DiffProposal:
        seen.append(failure)
        phase = 0 if failure is None else sum(1 for entry in seen if entry is not None)
        return DiffProposal(diffs[min(phase, len(diffs) - 1)], "")

    with pytest.raises(NodeGateFailedError) as caught:
        asyncio.run(_run_node(node, tmp_path, tmp_path / "proofs.jsonl", propose, {}, "run"))
    error = caught.value
    assert error.attempts == 3
    assert error.result.passed is False
    assert error.failure is not None
    assert "Attempt 3 of 3" in error.failure


def test_run_node_missing_proof_seals_attempt_with_tool_linkage(tmp_path: Path) -> None:
    _slice_repo(tmp_path)
    node = Node.model_validate(_node_dict("n1", ["ghost"]))
    journal = tmp_path / "proofs.jsonl"

    def propose(node: Node, failure: str | None) -> DiffProposal:
        return DiffProposal(GOOD_DIFF, "")

    with pytest.raises(KeyError):
        asyncio.run(_run_node(node, tmp_path, journal, propose, {}, "run-test"))
    spans = read_spans(journal)
    tools = [span for span in spans if span.kind == "tool"]
    workers = [span for span in spans if span.name == "worker:n1"]
    assert tools
    assert len(workers) == 1
    assert workers[0].detail == "'ghost'"
    assert workers[0].exit_code == 1
    assert workers[0].parent_id == "run-test"
    assert {tool.parent_id for tool in tools} == {workers[0].span_id}


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
    text = format_attempt_failure(_failed_result(), [], attempt=1, max_attempts=3, tools=ALL_TOOLS)
    assert text.startswith("Attempt 1 of 3 failed 1 gate(s):\n")
    assert "- tests: 'pytest test_n.py' exited 1\n" in text
    assert "syntax" not in text
    assert "---" not in text


def test_format_attempt_failure_skips_passing_runs() -> None:
    captured = [
        CapturedRun(argv=("pytest", "test_n.py"), exit_code=0, stdout="ok", stderr=""),
    ]
    text = format_attempt_failure(
        _failed_result(), captured, attempt=2, max_attempts=3, tools=ALL_TOOLS
    )
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
    text = format_attempt_failure(
        _failed_result(), captured, attempt=1, max_attempts=3, tools=ALL_TOOLS
    )
    assert "--- `pytest test_n.py` (exit 1) ---" in text
    assert "traceback here" in text
    assert "a warning" in text


def test_format_attempt_failure_truncates_long_output() -> None:
    captured = [
        CapturedRun(argv=("pytest",), exit_code=1, stdout="x" * 5000, stderr=""),
    ]
    text = format_attempt_failure(
        _failed_result(), captured, attempt=1, max_attempts=3, tools=ALL_TOOLS
    )
    assert "[earlier output truncated]\n" + "x" * 4000 in text
    assert "x" * 4001 not in text


def test_format_attempt_failure_marks_empty_output() -> None:
    captured = [CapturedRun(argv=("pytest",), exit_code=2, stdout="", stderr="")]
    text = format_attempt_failure(
        _failed_result(), captured, attempt=1, max_attempts=3, tools=ALL_TOOLS
    )
    assert text == (
        "Attempt 1 of 3 failed 1 gate(s):\n"
        "- tests: 'pytest test_n.py' exited 1\n"
        "--- `pytest` (exit 2) ---\n"
        "(no output)\n"
    )


def test_format_attempt_failure_renders_failing_run_after_passing_run() -> None:
    captured = [
        CapturedRun(argv=("pytest",), exit_code=0, stdout="ok", stderr=""),
        CapturedRun(argv=("pytest",), exit_code=1, stdout="traceback here", stderr=""),
    ]
    text = format_attempt_failure(
        _failed_result(), captured, attempt=1, max_attempts=3, tools=ALL_TOOLS
    )
    assert "--- `pytest` (exit 1) ---" in text
    assert "traceback here" in text


def test_format_attempt_failure_keeps_exact_boundary_output() -> None:
    captured = [
        CapturedRun(argv=("pytest",), exit_code=1, stdout="y" * RECOVERY_OUTPUT_CHARS, stderr="")
    ]
    text = format_attempt_failure(
        _failed_result(), captured, attempt=1, max_attempts=3, tools=ALL_TOOLS
    )
    assert "[earlier output truncated]" not in text
    assert "y" * RECOVERY_OUTPUT_CHARS in text


def test_repair_prompt_withholds_test_output_without_run_tests() -> None:
    """T3-4 known-bad: `run_tests` gates the suite output in the repair prompt.

    Before the binding, every non-zero captured run was inlined whatever
    the plan said, so `run_tests` cost nothing to omit and bought nothing
    to list. The gate verdict lines stay -- they are the node's own
    result -- so the worker still learns which gates failed.
    """
    captured = [
        CapturedRun(
            argv=("coverage", "run", "-m", "pytest"),
            exit_code=1,
            stdout="E   assert 1 == 2",
            stderr="",
        ),
        CapturedRun(argv=("ruff", "check", "n.py"), exit_code=1, stdout="n.py:1:1 F401", stderr=""),
    ]
    text = format_attempt_failure(
        _failed_result(), captured, attempt=1, max_attempts=3, tools=["lint"]
    )
    # Withheld: the suite's own output and its header.
    assert "assert 1 == 2" not in text
    assert "coverage run -m pytest" not in text
    # Kept: the lint output this node did ask for, and the gate verdicts.
    assert "--- `ruff check n.py` (exit 1) ---" in text
    assert "n.py:1:1 F401" in text
    assert "- tests: 'pytest test_n.py' exited 1" in text


def test_repair_prompt_withholds_lint_output_without_lint() -> None:
    """The other half: a `run_tests`-only node gets the suite and not ruff."""
    captured = [
        CapturedRun(
            argv=("coverage", "run", "-m", "pytest"),
            exit_code=1,
            stdout="E   assert 1 == 2",
            stderr="",
        ),
        CapturedRun(argv=("ruff", "check", "n.py"), exit_code=1, stdout="n.py:1:1 F401", stderr=""),
    ]
    text = format_attempt_failure(
        _failed_result(), captured, attempt=1, max_attempts=3, tools=["run_tests"]
    )
    assert "assert 1 == 2" in text
    assert "n.py:1:1 F401" not in text


@pytest.mark.parametrize(
    ("argv", "governing"),
    [
        (("pytest", "tests"), "run_tests"),
        (("coverage", "run", "-m", "pytest"), "run_tests"),
        (("python3", "-m", "pytest", "tests"), "run_tests"),
        (("/usr/bin/python3.12", "-m", "coverage", "run", "-m", "pytest"), "run_tests"),
        ((".venv/bin/pytest", "tests"), "run_tests"),
        (("python", "tests/run.py"), "run_tests"),
        (("ruff", "check", "n.py"), "lint"),
        (("python", "-m", "ruff", "check", "n.py"), "lint"),
        ((".venv/bin/ruff", "format", "--check"), "lint"),
        (("mutmut", "run"), None),
        (("git", "diff", "--stat"), None),
        ((), None),
    ],
)
def test_repair_prompt_binding_matches_how_a_plan_spells_the_command(
    argv: tuple[str, ...], governing: str | None
) -> None:
    """T3-4 follow-up: the binding governs what ran, not how argv[0] was spelled.

    `test_command` is a free string, so a plan could write `python3 -m
    pytest` and get the suite's output back without `run_tests`. Match on
    the basename, and on the module for `python -m X`. A run that resolves
    to no binding (mutmut, git) is kept whatever the node listed.
    """
    captured = [CapturedRun(argv=argv, exit_code=1, stdout="RUN-MARKER", stderr="")]

    def prompt(tools: list[str]) -> str:
        return format_attempt_failure(
            _failed_result(), captured, attempt=1, max_attempts=3, tools=tools
        )

    if governing is None:
        assert "RUN-MARKER" in prompt([])
        return
    other = "lint" if governing == "run_tests" else "run_tests"
    assert "RUN-MARKER" in prompt([governing])
    assert "RUN-MARKER" not in prompt([other])


def test_repair_prompt_keeps_both_when_the_node_declared_both() -> None:
    """T3-4 known-good: all four names behaves exactly as it did before."""
    captured = [
        CapturedRun(
            argv=("coverage", "run", "-m", "pytest"),
            exit_code=1,
            stdout="E   assert 1 == 2",
            stderr="",
        ),
        CapturedRun(argv=("ruff", "check", "n.py"), exit_code=1, stdout="n.py:1:1 F401", stderr=""),
    ]
    text = format_attempt_failure(
        _failed_result(), captured, attempt=1, max_attempts=3, tools=ALL_TOOLS
    )
    assert "assert 1 == 2" in text
    assert "n.py:1:1 F401" in text


def test_repair_prompt_keeps_a_run_no_binding_governs() -> None:
    """A capability the node declined is withheld; harness output nobody
    named is not. Dropping an unmapped run would remove evidence no plan
    asked to have withheld."""
    captured = [CapturedRun(argv=("git", "apply"), exit_code=1, stdout="corrupt patch", stderr="")]
    text = format_attempt_failure(_failed_result(), captured, attempt=1, max_attempts=3, tools=[])
    assert "corrupt patch" in text


def test_splice_replan_rewires_dependents_to_new_leaves() -> None:
    dag = Dag.model_validate(
        {
            "nodes": [
                _node_dict("z", []),
                _node_dict("a", ["z"]),
                _node_dict("b", ["a"]),
                _node_dict("c", []),
                _node_dict("d", ["a", "z"]),
            ]
        }
    )
    new = Dag.model_validate({"nodes": [_node_dict("m1", []), _node_dict("m2", ["m1"])]})
    merged, gen_ids = splice_replan(dag, "a", new)
    assert gen_ids == ["a.r1", "a.r2"]
    by_id = {node.id: node for node in merged.nodes}
    assert [node.id for node in merged.nodes] == ["z", "a", "b", "c", "d", "a.r1", "a.r2"]
    assert by_id["a"].dependencies == ["z"]
    assert by_id["a.r1"].dependencies == ["z"]
    assert by_id["a.r2"].dependencies == ["a.r1"]
    assert by_id["b"].dependencies == ["a.r2"]
    assert by_id["c"].dependencies == []
    assert by_id["d"].dependencies == ["a.r2", "z"]
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
    assert text == (
        "Node 'n1' failed Tier-1 after 2 attempt(s):\n"
        "- tests: 'pytest test_n.py' exited 1\n"
        "Last attempt evidence:\n"
        "Attempt 2 evidence\n"
        "\n"
    )


def test_format_replan_history_unappliable_without_evidence() -> None:
    error = NodeUnappliableError("n1", "no proposed diff applied in 3 attempts", 3)
    text = format_replan_history("n1", error)
    assert text == "Node 'n1' produced no applicable diff after 3 attempt(s).\n"


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
    assert "Node 'n1' failed Tier-1 after 2 attempt(s):" in replans[0][1]
    assert "Last attempt evidence:\nAttempt 1 of 3" in replans[0][1]
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


def test_run_slice_replan_continues_past_failed_emission(tmp_path: Path) -> None:
    _slice_repo(tmp_path)
    dag = Dag.model_validate({"nodes": [_node_dict("a", []), _node_dict("b", [])]})
    journal = tmp_path / "proofs.jsonl"
    calls: list[str] = []

    def propose(node: Node, failure: str | None) -> DiffProposal:
        if node.id in ("a", "b"):
            return DiffProposal(BAD_DIFF, "")
        return DiffProposal(FIX_DIFF, "")

    def replan(node: Node, history: str) -> Dag:
        calls.append(node.id)
        if node.id == "a":
            msg = "emission exhausted"
            raise ReplanFailedError(msg)
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
    assert calls == ["a", "b"]
    assert list(result.proofs) == ["b.r1"]
    assert result.passed is False


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


TIDY_DIFF = (
    "diff --git a/n.py b/n.py\n"
    "--- a/n.py\n"
    "+++ b/n.py\n"
    "@@ -1,2 +1,3 @@\n"
    " def f():\n"
    "     return 2\n"
    "+# tidy\n"
)

# A refactor with a statement to its name: the value `n1` proved, spelled as
# a sum. Behaviour preserved, the same test still pins it, and the changed
# line is one coverage can cover and mutation can mutate.
REFACTOR_DIFF = (
    "diff --git a/n.py b/n.py\n"
    "--- a/n.py\n"
    "+++ b/n.py\n"
    "@@ -1,2 +1,2 @@\n"
    " def f():\n"
    "-    return 2\n"
    "+    return 1 + 1\n"
)


def _first_run(tmp_path: Path) -> tuple[Path, SliceResult]:
    _slice_repo(tmp_path)
    dag = Dag.model_validate({"nodes": [_node_dict("n1", [])]})
    journal = tmp_path / "proofs.jsonl"
    first = run_slice(
        "Fix f.",
        dag,
        workdir=tmp_path,
        journal_path=journal,
        propose=lambda node, failure: DiffProposal(GOOD_DIFF, "return two instead"),
        now=lambda: "2026-09-16T00:00:00+00:00",
    )
    assert first.passed is True
    return journal, first


def test_run_slice_resumes_a_verified_journal_and_reuses_its_proofs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Known-good (T3-1): a verified journal seeds the proofs. The proven
    node is never re-proposed, its sealed hash is reused as the parent of
    the new node, and the run counts both as proven.

    `n2` is a refactor that rewrites the statement `n1` proved, so it
    carries its own evidence: coverage of its changed line and killed
    mutants on it. It used to be a comment-only edit, which passed only
    while `n2` was gated against `HEAD` and credited with `n1`'s work
    (T3-8); against its own baseline that diff has no statement line and
    proves nothing (the known-bad below).
    """
    journal, first = _first_run(tmp_path)
    _refactor_mutmut(tmp_path / "stub", monkeypatch)
    tidy = _node_dict("n2", ["n1"])
    tidy["kind"] = "refactor"
    dag = Dag.model_validate({"nodes": [_node_dict("n1", []), tidy]})
    proposed: list[str] = []

    def propose(node: Node, failure: str | None) -> DiffProposal:
        proposed.append(node.id)
        assert node.id != "n1", "a proven node must not be re-proposed"
        return DiffProposal(REFACTOR_DIFF, "same value, spelled as a sum")

    second = run_slice(
        "Fix f.",
        dag,
        workdir=tmp_path,
        journal_path=journal,
        propose=propose,
        now=lambda: "2026-09-16T00:00:00+00:00",
    )
    assert second.passed is True, second.transcript
    assert set(proposed) == {"n2"}
    assert second.proofs["n1"] == first.proofs["n1"]
    assert list(second.proofs) == ["n1", "n2"]
    records = read_records(journal)
    assert [record.node_id for record in records] == ["n1", "n2"]
    assert records[1].parent_proofs == [first.proofs["n1"]]
    run = [span for span in read_spans(journal) if span.name == "run"][-1]
    assert run.detail == "2 proven, 0 failed, 0 undispatched, merge exit 0"
    assert "## Node n1\n" in second.transcript
    assert f"- Proof: {first.proofs['n1']}\n" in second.transcript
    assert (tmp_path / "n.py").read_text() == "def f():\n    return 1 + 1\n"
    assert (
        "- Gate red-phase: PASS (tests unchanged (behaviour preserved); coverage and "
        "mutation 100.0% carry the proof)\n"
    ) in second.transcript


def test_run_slice_resumed_comment_only_refactor_proves_nothing(tmp_path: Path) -> None:
    """Known-bad (T3-8): the fixture the resume test used to run.

    A refactor whose diff is a comment has no changed statement line, so
    coverage has nothing to cover and mutation nothing to mutate; gated
    against its own baseline it fails red-phase for exactly that reason.
    Against `HEAD` it passed, credited with `n1`'s `return 2` -- which is
    how this fixture stayed green from T3-1 until T3-8 measured it.
    """
    journal, first = _first_run(tmp_path)
    tidy = _node_dict("n2", ["n1"])
    tidy["kind"] = "refactor"
    dag = Dag.model_validate({"nodes": [_node_dict("n1", []), tidy]})
    second = run_slice(
        "Fix f.",
        dag,
        workdir=tmp_path,
        journal_path=journal,
        propose=lambda node, failure: DiffProposal(TIDY_DIFF, "tidy"),
        now=lambda: "2026-09-16T00:00:00+00:00",
    )
    assert second.passed is False
    assert list(second.proofs) == ["n1"]
    assert second.proofs["n1"] == first.proofs["n1"]
    assert (
        "- Gate red-phase: FAIL (tests unchanged and no mutants decided; "
        "nothing proves the change)\n"
    ) in second.transcript
    assert (
        "- Gate mutation: FAIL (no mutants on changed lines: mutation provided no evidence)\n"
        in second.transcript
    )


def test_run_slice_resume_with_nothing_left_runs_no_worker(tmp_path: Path) -> None:
    journal, first = _first_run(tmp_path)
    dag = Dag.model_validate({"nodes": [_node_dict("n1", [])]})

    def propose(node: Node, failure: str | None) -> DiffProposal:
        msg = "nothing should be proposed"
        raise AssertionError(msg)

    second = run_slice(
        "Fix f.",
        dag,
        workdir=tmp_path,
        journal_path=journal,
        propose=propose,
        now=lambda: "2026-09-16T00:00:00+00:00",
    )
    assert second.passed is True
    assert second.proofs == first.proofs
    run = [span for span in read_spans(journal) if span.name == "run"][-1]
    assert run.detail == "1 proven, 0 failed, 0 undispatched, merge exit 0"


def test_run_slice_refuses_a_journal_that_does_not_verify(tmp_path: Path) -> None:
    """Known-bad (T3-1): a tampered record breaks its hash, and resuming
    onto it raises before any node runs."""
    journal, _first = _first_run(tmp_path)
    text = journal.read_text()
    assert text.count("return two instead") == 1
    journal.write_text(text.replace("return two instead", "return three instead"))
    dag = Dag.model_validate({"nodes": [_node_dict("n1", [])]})

    def propose(node: Node, failure: str | None) -> DiffProposal:
        msg = "nothing should be proposed"
        raise AssertionError(msg)

    with pytest.raises(ValueError, match=r"failed verification: bad-hash@line \d+"):
        run_slice(
            "Fix f.",
            dag,
            workdir=tmp_path,
            journal_path=journal,
            propose=propose,
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


def _git_repo_multiline(root: Path) -> None:
    """A file with enough lines that a hunk carries real context."""
    for argv in (
        ["git", "init"],
        ["git", "config", "user.email", "test@example.com"],
        ["git", "config", "user.name", "test"],
    ):
        assert run_argv(argv, root) == 0
    (root / "m.py").write_text("def f():\n    a = 1\n    b = 2\n    return a + b\n")
    assert run_argv(["git", "add", "m.py"], root) == 0
    assert run_argv(["git", "commit", "-m", "base"], root) == 0


def test_apply_diff_tolerates_whitespace_drift_in_context(tmp_path: Path) -> None:
    """Context reproduced with drifted indentation must still apply.

    T1 spent two of three worker calls on diffs that would not apply and
    T7 lost an entire run to three consecutive failures, one lint fix
    from passing (F4, F11, F13). The dominant cause is not a wrong edit:
    it is context the model reproduced from memory with whitespace that
    does not match byte-for-byte. The edit itself is unambiguous.
    """
    _git_repo_multiline(tmp_path)
    diff = (
        "diff --git a/m.py b/m.py\n"
        "--- a/m.py\n"
        "+++ b/m.py\n"
        "@@ -1,4 +1,4 @@\n"
        " def f():\n"
        "   a = 1\n"  # drifted: real file has four spaces
        "     b = 2\n"  # drifted: real file has four spaces
        "-    return a + b\n"
        "+    return a * b\n"
    )
    _apply_diff(tmp_path, diff)
    assert (tmp_path / "m.py").read_text() == "def f():\n    a = 1\n    b = 2\n    return a * b\n"


def test_apply_diff_reports_which_mode_applied(tmp_path: Path) -> None:
    """A diff that needed loosening must not read as an exact match.

    The ladder exists to stop unappliable diffs destroying runs, not to
    make sloppy ones invisible. An exact diff reports `strict`; a drifted
    one names the tolerance it required, so the journal distinguishes
    them.
    """
    _git_repo_multiline(tmp_path)
    exact = (
        "diff --git a/m.py b/m.py\n"
        "--- a/m.py\n"
        "+++ b/m.py\n"
        "@@ -1,4 +1,4 @@\n"
        " def f():\n"
        "     a = 1\n"
        "     b = 2\n"
        "-    return a + b\n"
        "+    return a - b\n"
    )
    assert _apply_diff(tmp_path, exact) == "strict"

    drifted = (
        "diff --git a/m.py b/m.py\n"
        "--- a/m.py\n"
        "+++ b/m.py\n"
        "@@ -1,4 +1,4 @@\n"
        " def f():\n"
        "   a = 1\n"
        "     b = 2\n"
        "-    return a - b\n"
        "+    return a * b\n"
    )
    assert _apply_diff(tmp_path, drifted) == "ignore-whitespace"


def test_first_attempt_draws_independent_samples_and_takes_the_best(tmp_path: Path) -> None:
    """Sequential retry optimises against whichever gate shouts loudest.

    On T7 recovery drove the node from four failing gates to one and then
    spent its whole budget on ruff while an infinite loop sat untouched
    (F13). SpecBench measures the same thing: "additional search steps
    did not reliably reduce gaps... longer search increases the severity
    of reward hacking".

    The first attempt now draws PROPOSAL_SAMPLES proposals with no
    failure conditioning -- independent samples, not a chain anchored on
    the last rejection -- and keeps the one that gates best.
    """
    _slice_repo(tmp_path)
    journal = tmp_path / "proofs.jsonl"
    dag = Dag.model_validate({"nodes": [_node_dict("n1", [])]})
    seen_failures: list[str | None] = []
    order = [BAD_DIFF, GOOD_DIFF, BAD_DIFF]

    def propose(node: Node, failure: str | None) -> DiffProposal:
        seen_failures.append(failure)
        return DiffProposal(order[(len(seen_failures) - 1) % len(order)], "")

    result = run_slice(
        "Fix f.",
        dag,
        workdir=tmp_path,
        journal_path=journal,
        propose=propose,
        now=lambda: "2026-09-16T00:00:00+00:00",
    )

    # Sampling stops as soon as one candidate gates clean: k is a budget,
    # not a quota, and model calls are the dominant cost (F8).
    assert 0 < len(seen_failures) <= PROPOSAL_SAMPLES
    # Independent: none of the samples was conditioned on a rejection.
    assert seen_failures == [None] * len(seen_failures)
    # The good sample is selected even though it was not drawn first.
    assert result.passed is True
    assert (tmp_path / "n.py").read_text() == "def f():\n    return 2\n"


# --- T3-8: a node is gated against its own baseline, so slices can be wide ---

M_DIFF = (
    "diff --git a/m.py b/m.py\n"
    "--- a/m.py\n"
    "+++ b/m.py\n"
    "@@ -1,2 +1,2 @@\n"
    " def g():\n"
    "-    return 1\n"
    "+    return 2\n"
)

# The `test` node's whole diff: one new file holding a failing specification
# with a hypothesis property, which is what a `test` node must ship (T3-7a).
SPEC_DIFF = (
    "diff --git a/test_n.py b/test_n.py\n"
    "new file mode 100644\n"
    "--- /dev/null\n"
    "+++ b/test_n.py\n"
    "@@ -0,0 +1,12 @@\n"
    "+from hypothesis import given\n"
    "+from hypothesis import strategies as st\n"
    "+\n"
    "+from n import f\n"
    "+\n"
    "+\n"
    "+def test_f_returns_two():  # REQ-001\n"
    "+    assert f() == 2\n"
    "+\n"
    "+\n"
    "+@given(st.integers())\n"
    "+def test_f_is_an_int(_value):  # REQ-001\n"
    "+    assert isinstance(f(), int)\n"
)


def _two_module_repo(root: Path) -> None:
    """`_slice_repo` plus a second module and its failing test, committed.

    `n2`'s work is `m.py` alone, so its gates have to read `n1`'s proven
    edit to `n.py` as history rather than as a file `n2` strayed into.
    """
    _slice_repo(root)
    (root / "m.py").write_text("def g():\n    return 1\n")
    (root / "test_m.py").write_text(
        "from m import g\n\n\ndef test_g():  # REQ-002\n    assert g() == 2\n"
    )
    assert run_argv(["git", "add", "m.py", "test_m.py"], root) == 0
    assert run_argv(["git", "commit", "-m", "second module"], root) == 0


def _spec_slice_repo(root: Path) -> None:
    """`_slice_repo` without `test_n.py`: the `test` node creates it."""
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


def _declares_both_requirements(node: dict[str, object]) -> dict[str, object]:
    """Both REQ ids on both nodes, because binding is suite-granular.

    `run_node_gate` reads citations from every discovered test source, not
    from the ones the node's scoped command runs, so `test_m.py`'s
    `REQ-002` tag counts as cited for `n1` too. A fixture that declared it
    on `n2` alone would fail `n1` with "undeclared requirements cited:
    REQ-002" -- the orphan rule doing its job, not a T3-8 defect.
    """
    node["requirements"] = [
        {"id": "REQ-001", "statement": "REQ-001 holds."},
        {"id": "REQ-002", "statement": "REQ-002 holds."},
    ]
    return node


def _mutmut_stub(
    stub_dir: Path, monkeypatch: pytest.MonkeyPatch, mutants: dict[str, tuple[str, str]]
) -> None:
    """A mutmut stub reporting every mutant in `mutants` killed.

    Each entry is `name: (file, removed line)`; the collector locates a
    mutant by matching the removed line's text against the file, so the
    text must be exactly the source line the fixture's diff leaves there.
    """
    stub_dir.mkdir(exist_ok=True)
    (stub_dir / "results.txt").write_text("".join(f"  {name}: killed\n" for name in mutants))
    for name, (rel, removed) in mutants.items():
        (stub_dir / f"show_{name}.txt").write_text(
            f"--- {rel}\n+++ {rel}\n@@ -2 +2 @@\n-{removed}\n+    return 3  # {name}\n"
        )
    script = stub_dir / "mutmut"
    script.write_text(
        "#!/bin/sh\n"
        f'STUB_DIR="{stub_dir}"\n'
        'case "$1" in\n'
        "  run) exit 0;;\n"
        '  results) cat "$STUB_DIR/results.txt";;\n'
        '  show) cat "$STUB_DIR/show_$2.txt";;\n'
        "esac\n"
    )
    script.chmod(script.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    monkeypatch.setenv("PATH", f"{stub_dir}{os.pathsep}{os.environ['PATH']}")


def _two_module_mutmut(stub_dir: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Killed mutants on *both* modules' changed lines.

    conftest's autouse stub locates every mutant at `n.py:2`, so the node
    whose changed line lives in `m.py` would find no mutants in scope and
    fail the mutation gate for a fixture reason rather than a T3-8 one.
    Each node still only counts the mutants that land on its own changed
    lines: the other module's are skipped before the file is read.
    """
    mutants = {
        f"n{index}": ("n.py", "    return 2") for index in range(1, 1 + MIN_SIGNIFICANT_MUTANTS)
    }
    mutants |= {
        f"g{index}": ("m.py", "    return 2") for index in range(1, 1 + MIN_SIGNIFICANT_MUTANTS)
    }
    _mutmut_stub(stub_dir, monkeypatch, mutants)


def _refactor_mutmut(stub_dir: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Killed mutants on the line `REFACTOR_DIFF` leaves behind.

    conftest's stub locates its mutants at `    return 2`; the resumed
    refactor rewrote that line, so its mutants have to sit on the new text.
    """
    _mutmut_stub(
        stub_dir,
        monkeypatch,
        {
            f"r{index}": ("n.py", "    return 1 + 1")
            for index in range(1, 1 + MIN_SIGNIFICANT_MUTANTS)
        },
    )


def test_run_slice_two_nodes_are_gated_against_their_own_baselines(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Known-good (T3-8): the second node's gates see the second node's diff.

    `_run_node` applies, autofixes, gates and seals but never commits, so
    when `n2` runs, `n1`'s proven edit to `n.py` is still staged. Gated
    against `HEAD` -- the default every caller took before this -- `n2`
    failed `target-scope` ("touched file(s) outside target_files: n.py"),
    `coverage` (0.0%, uncovered `n.py:2`) and `red-phase` on work that was
    already proven. Gated against a snapshot of the tree `n2` started
    from, its diff is `m.py` and nothing else.
    """
    _two_module_repo(tmp_path)
    _two_module_mutmut(tmp_path / "stub", monkeypatch)
    second = _declares_both_requirements(_node_dict("n2", ["n1"]))
    second["target_files"] = ["m.py"]
    gate = second["deterministic_gate"]
    assert isinstance(gate, dict)
    gate["test_command"] = "pytest test_m.py"
    first = _declares_both_requirements(_node_dict("n1", []))
    dag = Dag.model_validate({"nodes": [first, second]})
    journal = tmp_path / "proofs.jsonl"
    calls: list[str] = []

    def propose(node: Node, failure: str | None) -> DiffProposal:
        calls.append(node.id)
        return DiffProposal(GOOD_DIFF if node.id == "n1" else M_DIFF, "")

    result = run_slice(
        "Fix f and g.",
        dag,
        workdir=tmp_path,
        journal_path=journal,
        propose=propose,
        now=lambda: "2026-09-16T00:00:00+00:00",
    )
    assert result.passed is True, result.transcript
    assert list(result.proofs) == ["n1", "n2"]
    # One call per node: the first sample gated clean, so `_best_of_samples`
    # took its `failures == 0` exit instead of drawing all three. Against
    # `HEAD` the first sample scores `target-scope` red and `n2` costs three.
    assert calls == ["n1", "n2"]
    assert "- Gate target-scope: PASS (1 touched file(s) within 1 target(s))\n" in result.transcript
    assert "- Gate coverage: PASS (100.0% >= 100.0%)\n" in result.transcript
    spans = read_spans(journal)
    run = [span for span in spans if span.name == "run"][-1]
    assert run.detail == "2 proven, 0 failed, 0 undispatched, merge exit 0"
    # Every ref the harness diffs `n2` against -- autofix's scope and the
    # runner's changed-file lists alike -- is `n2`'s own snapshot, never
    # `HEAD`. Autofix against `HEAD` is idempotent on an earlier node's
    # ruff-clean files, so this is the only place the threading shows.
    name_only = [
        span
        for span in spans
        if span.node_id == "n2" and span.name == "git" and "--name-only" in span.argv
    ]
    assert name_only, "no `git diff --name-only` span for n2"
    assert all("refs/saddle/baseline/n2" in span.argv for span in name_only)
    assert not any("HEAD" in span.argv for span in name_only)
    refs = run_capture(["git", "for-each-ref", "--format=%(refname)", "refs/saddle/"], tmp_path)
    assert refs.stdout.split() == ["refs/saddle/baseline/n1", "refs/saddle/baseline/n2"]


# `n2`'s own stray: the same `m.py` edit plus a comment on `n1`'s file.
STRAY_DIFF = M_DIFF + (
    "diff --git a/n.py b/n.py\n"
    "--- a/n.py\n"
    "+++ b/n.py\n"
    "@@ -1,2 +1,3 @@\n"
    " def f():\n"
    "     return 2\n"
    "+# stray\n"
)


def test_run_slice_second_node_own_stray_still_fails_target_scope(tmp_path: Path) -> None:
    """Known-bad (T3-8): narrowing the ref does not narrow the check.

    `n2` declares `target_files=["m.py"]` and edits `m.py` *and* `n.py`.
    `n1`'s proven edit to `n.py` is no longer charged to `n2`, but `n2`'s own
    line on that file still is: `target-scope` names `n.py` and the node
    fails. The pass in the test above is not "target-scope stopped looking".
    """
    _two_module_repo(tmp_path)
    second = _declares_both_requirements(_node_dict("n2", ["n1"]))
    second["target_files"] = ["m.py"]
    gate = second["deterministic_gate"]
    assert isinstance(gate, dict)
    gate["test_command"] = "pytest test_m.py"
    first = _declares_both_requirements(_node_dict("n1", []))
    dag = Dag.model_validate({"nodes": [first, second]})
    journal = tmp_path / "proofs.jsonl"

    def propose(node: Node, failure: str | None) -> DiffProposal:
        return DiffProposal(GOOD_DIFF if node.id == "n1" else STRAY_DIFF, "")

    result = run_slice(
        "Fix f and g.",
        dag,
        workdir=tmp_path,
        journal_path=journal,
        propose=propose,
        now=lambda: "2026-09-16T00:00:00+00:00",
    )
    assert result.passed is False
    assert list(result.proofs) == ["n1"]
    assert (
        "- Gate target-scope: FAIL (touched file(s) outside target_files: n.py)\n"
        in result.transcript
    )


def test_run_slice_test_node_then_impl_node_both_prove(tmp_path: Path) -> None:
    """Deferred from T3-7a, unblocked by T3-8: a `test` node writes the
    specification, the `impl` node that depends on it makes it pass, and both
    prove. Against `HEAD` the impl node failed `node-scope` with "impl node
    changed test file(s): test_n.py", because the spec the test node had just
    created was staged and counted as the impl node's own edit.
    """
    _spec_slice_repo(tmp_path)
    spec = _node_dict("t1", [])
    spec["kind"] = "test"
    dag = Dag.model_validate({"nodes": [spec, _node_dict("n1", ["t1"])]})
    journal = tmp_path / "proofs.jsonl"
    calls: list[str] = []

    def propose(node: Node, failure: str | None) -> DiffProposal:
        calls.append(node.id)
        return DiffProposal(SPEC_DIFF if node.id == "t1" else GOOD_DIFF, "")

    result = run_slice(
        "Specify f, then fix it.",
        dag,
        workdir=tmp_path,
        journal_path=journal,
        propose=propose,
        now=lambda: "2026-09-16T00:00:00+00:00",
    )
    assert result.passed is True, result.transcript
    assert list(result.proofs) == ["t1", "n1"]
    assert calls == ["t1", "n1"]
    assert "- Gate node-scope: PASS (test node changed 1 file(s) in scope)\n" in result.transcript
    assert "- Gate node-scope: PASS (impl node changed 1 file(s) in scope)\n" in result.transcript
    assert "- Gate tests: PASS (red specification: 1 failing test(s))\n" in result.transcript
    run = [span for span in read_spans(journal) if span.name == "run"][-1]
    assert run.detail == "2 proven, 0 failed, 0 undispatched, merge exit 0"
