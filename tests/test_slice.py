"""Tests for saddle.slice: vertical-slice driver and its transcript."""

from __future__ import annotations

import asyncio
import dataclasses
import hashlib
import json
import os
import re
import stat
import subprocess
import threading
import time
from collections.abc import Sequence
from datetime import datetime
from pathlib import Path
from typing import Any, Final

import pytest

import saddle.slice as slice_module
from saddle.dag import Dag, Node
from saddle.evidence import CapturedRun, attempt_ref, run_argv, run_capture
from saddle.gates import MIN_SIGNIFICANT_MUTANTS, GateCheck, Tier1Result
from saddle.journal import (
    GateOutput,
    PlanRecord,
    ProofRecord,
    SpanRecord,
    SpanRecorder,
    append_record,
    append_span,
    attempt_sidecar_path,
    build_record,
    hash_node,
    read_entries,
    read_plans,
    read_records,
    read_spans,
    verify_journal,
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
    _payload_sections,
    _reconstruction_evidence,
    _run_node,
    _schedulable_nodes,
    _unwrapped,
    _utcnow,
    _write_files,
    autofix,
    format_attempt_failure,
    format_replan_history,
    run_slice,
    splice_replan,
    whole_file_reconstruction,
)
from saddle.transcript import is_run_end
from saddle.vllm import DiffProposal, VllmAuthError, VllmRequestError, VllmResponseError


def _git_subcommand(argv: Sequence[str]) -> str:
    """The subcommand in a `git -C <dir> [-c k=v ...] <sub> ...` argv.

    Position stopped naming it when T6-56 put saddle's identity on the
    commit: two `-c` pairs now sit between `-C <dir>` and the subcommand.
    Reading it by shape rather than by index keeps the assertion pinned to
    which git runs, which is what it was ever about.
    """
    rest = iter(argv[3:])
    for token in rest:
        if token == "-c":
            next(rest)
            continue
        return token
    msg = f"no git subcommand in {list(argv)}"
    raise AssertionError(msg)


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
        "requirements": [
            {"id": "REQ-001", "statement": "REQ-001 holds.", "accepts": ["2"], "rejects": ["3"]}
        ],
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


def whole_file(path: str, *lines: str) -> str:
    """One write section: the envelope the worker grammar admits (T6-62/A1).

    The inverse of `slice._payload_sections`. Tests spell payloads through
    this rather than by hand so that a change to the envelope breaks in
    one place instead of ninety.
    """
    body = "".join(f"+{line}\n" for line in lines)
    return (
        f"diff --git a/{path} b/{path}\n"
        f"--- /dev/null\n+++ b/{path}\n"
        f"@@ -0,0 +1,{len(lines)} @@\n{body}"
    )


GOOD_DIFF = whole_file("n.py", "def f():", "    return 2")

BAD_DIFF = GOOD_DIFF.replace("+    return 2\n", "+    return 3\n")

# A repair writes the file as it should end up, so the fix for BAD_DIFF is
# byte-identical to GOOD_DIFF: under this envelope "what changed" is not
# part of the payload.
FIX_DIFF = whole_file("n.py", "def f():", "    return 2")

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
TIMED_OUT = "request failed: timed out"
KEY_REJECTED = "server rejected the API key (HTTP 401)"


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


def test_apply_diff_that_does_not_match_the_tree_raises(tmp_path: Path) -> None:
    """A well-formed diff git rejects in every tolerance mode is named as a
    failed apply. Until T3-23 this path was covered only by accident: a
    replacement node's diff written against the tree its failed
    predecessor left behind, which the restore now makes apply."""
    _git_repo(tmp_path)
    diff = "diff --git a/n.py b/n.py\n--- a/n.py\n+++ b/n.py\n@@ -1 +1 @@\n-x = 9\n+x = 2\n"
    expected = f"worker diff did not apply cleanly in {str(tmp_path)!r} (last rung three-way: "
    # T6-27: the failure names the last rung and git's last stderr line, so
    # the attempt's record says which line git rejected, not only that it did.
    with pytest.raises(RuntimeError, match=re.escape(expected) + r"error: .*") as caught:
        _apply_diff(tmp_path, diff)
    assert "\n" not in str(caught.value)
    assert (tmp_path / "n.py").read_text() == "x = 1\n"


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


# --- T6-62/A1: the whole-file envelope ---------------------------------------


def _staged(root: Path) -> list[str]:
    done = subprocess.run(
        ["git", "-C", str(root), "diff", "--cached", "--name-only"],
        capture_output=True,
        text=True,
    )
    return done.stdout.split()


def test_write_files_replaces_the_whole_file_and_stages_it(tmp_path: Path) -> None:
    """Known-good. The new side IS the file, so what the payload leaves out
    is gone. That is the entire difference from a patch, and the reason
    there is nothing to match against the tree."""
    _git_repo(tmp_path)
    (tmp_path / "n.py").write_text("x = 1\ny = 2\nz = 3\n")
    _write_files(tmp_path, whole_file("n.py", "x = 9"))
    assert (tmp_path / "n.py").read_text() == "x = 9\n"
    assert _staged(tmp_path) == ["n.py"]


def test_write_files_applies_an_edit_payload_and_stages_it(tmp_path: Path) -> None:
    """One apply path carries either envelope, told apart by the first word.

    Known-good: an edit block reaches `apply_edits`, changes the file and
    is staged the way a whole-file section is. Falling through to the
    envelope check below would raise "not a file payload" instead.
    """
    _git_repo(tmp_path)
    _write_files(tmp_path, "edit n.py\n-x = 1\n=======\n+x = 2\n>>>>>>>\n")
    assert (tmp_path / "n.py").read_text() == "x = 2\n"
    assert _staged(tmp_path) == ["n.py"]


def test_write_files_names_the_worktree_when_an_edit_does_not_apply(tmp_path: Path) -> None:
    """Known-bad half: an edit naming no site is a spent attempt, reported
    like any other failed apply -- not an EditError escaping the layer."""
    _git_repo(tmp_path)
    payload = "edit n.py\n-y = 9\n=======\n+y = 8\n>>>>>>>\n"
    expected = (
        f"worker edits did not apply in {str(tmp_path)!r}: "
        "n.py: the search block does not appear in the file"
    )
    with pytest.raises(RuntimeError, match=re.escape(expected)):
        _write_files(tmp_path, payload)
    assert (tmp_path / "n.py").read_text() == "x = 1\n"


def test_write_files_stages_only_the_paths_an_edit_payload_named(tmp_path: Path) -> None:
    """Known-bad for the pathspec. A draw is scored on a `copytree` of a
    live worktree, so a bare `git add -A` would stage whatever else was
    lying in it and the gate would read a tree the payload never wrote.
    The whole-file arm names its paths; this one names the same list
    `apply_edits` reports back.
    """
    _git_repo(tmp_path)
    (tmp_path / "stray.py").write_text("junk = 1\n")
    _write_files(tmp_path, "edit n.py\n-x = 1\n=======\n+x = 2\n>>>>>>>\n")
    assert _staged(tmp_path) == ["n.py"]


def test_write_files_creates_a_file_and_its_parent_directories(tmp_path: Path) -> None:
    """Known-good. A creation is spelled exactly like an overwrite, so a
    node adding a module needs no separate form."""
    _git_repo(tmp_path)
    _write_files(tmp_path, whole_file("pkg/sub/m.py", "A = 1"))
    assert (tmp_path / "pkg" / "sub" / "m.py").read_text() == "A = 1\n"
    assert _staged(tmp_path) == ["pkg/sub/m.py"]


def test_write_files_deletes_what_a_delete_section_names(tmp_path: Path) -> None:
    """Known-good for the other branch: a delete carries no body, because
    re-emitting a file in order to remove it is tokens spent on a
    transcription the writer discards."""
    _git_repo(tmp_path)
    (tmp_path / "gone.py").write_text("x = 1\n")
    assert run_argv(["git", "add", "gone.py"], tmp_path) == 0
    payload = (
        "diff --git a/gone.py b/gone.py\ndeleted file mode 100644\n--- a/gone.py\n+++ /dev/null\n"
    )
    _write_files(tmp_path, payload)
    assert not (tmp_path / "gone.py").exists()


def test_write_files_forgives_one_enclosing_markdown_fence(tmp_path: Path) -> None:
    """T6-48's packaging rule still holds: the wrapper comes off before
    anything judges the content, so both paths see the same bytes."""
    _git_repo(tmp_path)
    fenced = "\n\n```diff\n" + whole_file("n.py", "x = 9").rstrip("\n") + "\n```"
    _write_files(tmp_path, fenced)
    assert (tmp_path / "n.py").read_text() == "x = 9\n"


def test_write_files_refuses_a_context_line(tmp_path: Path) -> None:
    """Known-bad, and the one that matters most. Silently keeping only the
    `+` lines of a patch would write a file holding just the additions --
    destroying content the model never saw, which is exactly the hazard
    T6-62 named when it priced a whole-file rung."""
    _git_repo(tmp_path)
    patchy = (
        "diff --git a/n.py b/n.py\n--- /dev/null\n+++ b/n.py\n"
        "@@ -0,0 +1,2 @@\n+def f():\n     return 1\n"
    )
    with pytest.raises(RuntimeError, match=re.escape("carries a context line in 'n.py'")):
        _write_files(tmp_path, patchy)
    # Refused before anything was written, not half-applied.
    assert (tmp_path / "n.py").read_text() == "x = 1\n"


def test_write_files_refuses_a_removal_line(tmp_path: Path) -> None:
    """Known-bad, the other half of the same contract."""
    _git_repo(tmp_path)
    patchy = (
        "diff --git a/n.py b/n.py\n--- /dev/null\n+++ b/n.py\n"
        "@@ -0,0 +1,2 @@\n+def f():\n-    return 1\n"
    )
    with pytest.raises(RuntimeError, match=re.escape("carries a removal line in 'n.py'")):
        _write_files(tmp_path, patchy)


def test_write_files_refuses_the_same_path_twice(tmp_path: Path) -> None:
    """Known-bad, F21.9's b-s1: nine `fees.py` sections in one emission,
    legal because `root ::= section+` repeats. A diff refused the second
    copy loudly; whole files would let the last one win in silence."""
    _git_repo(tmp_path)
    twice = whole_file("n.py", "x = 1") + whole_file("n.py", "x = 2")
    with pytest.raises(RuntimeError, match=re.escape("writes 'n.py' more than once")):
        _write_files(tmp_path, twice)
    assert (tmp_path / "n.py").read_text() == "x = 1\n"


def test_write_files_refuses_a_path_outside_the_tree(tmp_path: Path) -> None:
    """Known-bad. `git apply` refused an escaping path for free and writing
    files directly gives that up, so this is the only thing between a
    worker's header line and the rest of the filesystem."""
    _git_repo(tmp_path)
    for name in ("../escape.py", "/etc/saddle-escape.py", "."):
        with pytest.raises(RuntimeError, match=re.escape("names a path outside the tree")):
            _write_files(tmp_path, whole_file(name, "x = 1"))
    assert not (tmp_path.parent / "escape.py").exists()


def test_write_files_refuses_prose(tmp_path: Path) -> None:
    """Known-bad, and retryable for the same reason it always was: prose is
    exactly the case a fresh attempt can fix."""
    _git_repo(tmp_path)
    expected = f"worker content is not a file payload (no 'diff --git' header) in {str(tmp_path)!r}"
    with pytest.raises(RuntimeError, match=re.escape(expected)):
        _write_files(tmp_path, "not a payload\n")


def test_write_files_refuses_a_section_with_no_contents(tmp_path: Path) -> None:
    """Known-bad, both shapes. `_apply_diff` called this "a header but no
    hunk" and it applied nothing; here it would write an EMPTY file, so
    the same malformed emission turns from a no-op into a deletion."""
    _git_repo(tmp_path)
    headless = "diff --git a/n.py b/n.py\n--- /dev/null\n+++ b/n.py\n"
    with pytest.raises(RuntimeError, match=re.escape("names 'n.py' with no contents")):
        _write_files(tmp_path, headless)
    with pytest.raises(RuntimeError, match=re.escape("names 'n.py' with no contents")):
        _write_files(tmp_path, headless + "@@ -0,0 +1,0 @@\n")
    assert (tmp_path / "n.py").read_text() == "x = 1\n"


def test_payload_sections_round_trips_what_whole_file_spells(tmp_path: Path) -> None:
    """The encoder tests write with and the decoder the run reads with are
    inverses, so a payload built here is the payload a worker sends."""
    text = 'def f():\n    return "a b"\n\n# trailing comment\n'
    payload = whole_file("n.py", *text.split("\n")[:-1])
    assert _payload_sections(payload) == [("n.py", text)]


def test_write_files_finds_sections_by_header_not_by_position(tmp_path: Path) -> None:
    """A blank first line passes the envelope check, which reads
    `payload.lstrip()`, but is not itself a header -- so the parser has to
    locate sections by matching rather than assume the payload opens on
    one. `_unwrapped` strips a fence and nothing else, so whatever leads
    the response is still there when the parser sees it."""
    _git_repo(tmp_path)
    _write_files(tmp_path, "\n" + whole_file("n.py", "x = 9"))
    assert (tmp_path / "n.py").read_text() == "x = 9\n"
    assert _staged(tmp_path) == ["n.py"]


def test_write_files_skips_a_no_newline_marker_instead_of_refusing_it(
    tmp_path: Path,
) -> None:
    """`noeol` is in the grammar, so a worker may legally close a body with
    it, and it is the only non-`+` line a body may carry. It is git's
    marker rather than content: it neither lands in the file nor trips the
    context-line refusal sitting next to it. The file gets its final
    newline back, which is the direction that cannot destroy anything."""
    _git_repo(tmp_path)
    marker = "\\ No newline at end of file\n"
    _write_files(tmp_path, whole_file("n.py", "x = 9") + marker)
    assert (tmp_path / "n.py").read_text() == "x = 9\n"


def test_write_files_refuses_a_header_line_that_names_no_file(tmp_path: Path) -> None:
    """Known-bad. `header ::= "diff --git " line` admits a header carrying
    no paths, so a payload can open on the literal the envelope check
    looks for and still name nothing to write. That is refused, not taken
    as an empty success that stages an unchanged tree."""
    _git_repo(tmp_path)
    with pytest.raises(RuntimeError, match=re.escape("names no file")):
        _write_files(tmp_path, "diff --git \n")
    assert _staged(tmp_path) == []


# --- T6-48: packaging is not content ----------------------------------------

_PACKAGED = "diff --git a/n.py b/n.py\n--- a/n.py\n+++ b/n.py\n@@ -1 +1 @@\n-x = 1\n+x = 2\n"


def test_apply_diff_forgives_a_missing_final_newline(tmp_path: Path) -> None:
    """T6-48 known-good, loosened with proof. `git apply` calls a patch whose
    last line has no newline `corrupt patch at line N`, and four of the nine
    unconstrained round 3e draws died there with nothing else wrong -- a
    final `\n` and they apply on `strict` (F21.18). The bytes the model sent
    are still what the sidecar records; only the ladder sees the repair.
    """
    _git_repo(tmp_path)
    assert _apply_diff(tmp_path, _PACKAGED.rstrip("\n")) == "strict"
    assert (tmp_path / "n.py").read_text() == "x = 2\n"


def test_apply_diff_forgives_one_enclosing_markdown_fence(tmp_path: Path) -> None:
    """T6-48 known-good: three of the nine grammar-off draws wrapped the whole
    diff in a ```diff fence, which the structural precheck rejected before git
    ran at all."""
    _git_repo(tmp_path)
    fenced = "\n\n```diff\n" + _PACKAGED.rstrip("\n") + "\n```"
    assert _apply_diff(tmp_path, fenced) == "strict"
    assert (tmp_path / "n.py").read_text() == "x = 2\n"


def test_apply_diff_still_refuses_a_fenced_diff_with_prose_around_it(tmp_path: Path) -> None:
    """T6-48 known-bad: the allowance is one fence enclosing the whole content.
    Prose beside it is a worker that ignored "output ONLY the diff", which is
    the thing a fresh attempt can fix, so it stays a named precheck failure."""
    _git_repo(tmp_path)
    wrapped = "Here is the patch:\n\n```diff\n" + _PACKAGED.rstrip("\n") + "\n```"
    expected = f"worker content is not a unified diff (no 'diff --git' header) in {str(tmp_path)!r}"
    with pytest.raises(RuntimeError, match=re.escape(expected)):
        _apply_diff(tmp_path, wrapped)


def test_apply_diff_still_refuses_two_fenced_blocks(tmp_path: Path) -> None:
    """T6-48 known-bad, and the one a lazy regex gets wrong: a non-greedy match
    would splice two blocks into one body with a stray fence inside it. Two
    blocks are two answers, not packaging."""
    _git_repo(tmp_path)
    block = "```diff\n" + _PACKAGED.rstrip("\n") + "\n```"
    expected = f"worker content is not a unified diff (no 'diff --git' header) in {str(tmp_path)!r}"
    with pytest.raises(RuntimeError, match=re.escape(expected)):
        _apply_diff(tmp_path, block + "\n\n" + block)


def test_a_fenced_round3e_draw_unwraps_to_the_bytes_inside_its_fence() -> None:
    """T6-48 on real bytes, not a hand-written case. The fixture is round 3e
    n2 attempt 1's seed-0 draw with `DIFF_GRAMMAR` off, verbatim from
    `../saddle-bench/runs/round3e-probe/grammar_cell_low.json`: two leading
    blank lines, a ```diff fence, and no final newline. Unwrapping must
    change the packaging and nothing else.
    """
    raw = (Path(__file__).parent / "fixtures" / "fenced_draw_round3e.diff").read_text()
    assert raw.startswith("\n\n```diff\n")
    assert raw.endswith("\n```")
    inside = raw.strip().removeprefix("```diff\n").removesuffix("\n```")
    unwrapped = _unwrapped(raw)
    assert unwrapped == inside + "\n"
    assert unwrapped.startswith("diff --git ")
    assert "```" not in unwrapped


def test_whole_file_reconstruction_reads_the_new_side_of_a_line_1_hunk() -> None:
    """T6-62 (C). Every apply-failure in rounds 3h and 3i is one hunk per
    file anchored at line 1 whose context lines carry the model's intended
    output (F21.38), so the candidate is recoverable from the diff the
    ladder refused.

    Known-good: round 3i n1.r2 attempt 1 draw 0, frozen byte for byte --
    two files, both reconstruct, both parse. Known-bad: the same round's
    rangeless `@@` header yields nothing rather than a guess.
    """
    good = (Path(__file__).parent / "fixtures" / "whole_file_round3i.diff").read_text()
    files = whole_file_reconstruction(good)
    assert sorted(files) == ["accounts.py", "fees.py"]
    recovered = _reconstruction_evidence(good)["reconstruction"]
    assert [recovered[name]["parses"] for name in sorted(recovered)] == [True, True]
    # The NEW side, and read off the body rather than the header. The
    # header declares `@@ -1,95 +1,113 @@` and the body holds 136 lines
    # (F21.38 measured the same 136): the declared count is the model's
    # own miscount, which is why `--recount` exists and why nothing here
    # trusts the arithmetic. What comes back is the rewrite, not the
    # 73-line accounts.py the model was shown -- no whitespace flag
    # reaches that, which is why the ladder refused it.
    assert "+1,113 @@" in good
    assert len(files["accounts.py"].splitlines()) == 136
    assert "def _usd" not in files["accounts.py"]

    bad = (Path(__file__).parent / "fixtures" / "rangeless_hunk_round3i.diff").read_text()
    assert whole_file_reconstruction(bad) == {}

    # A context line for a blank line arrives with its leading space
    # stripped, and is a blank line in the file rather than a dropped
    # one -- `_HUNK_KINDS` admits the same shape. Anything that is not a
    # hunk line ends the hunk, so trailing prose from a worker that did
    # not stop at the diff is not swallowed into the reconstruction.
    with_blank = (
        "diff --git a/n.py b/n.py\n--- a/n.py\n+++ b/n.py\n"
        "@@ -1,3 +1,3 @@\n x = 1\n\n+y = 2\n"
        "That was the diff.\n"
    )
    assert whole_file_reconstruction(with_blank) == {"n.py": "x = 1\n\ny = 2\n"}


def test_whole_file_reconstruction_declines_what_it_cannot_read_off() -> None:
    """Known-bad, each alone: two hunks in one file, a hunk anchored past
    line 1, and a section with no hunk at all. A partial file reconstructed
    as if it were whole is worse than no reconstruction, because it reads
    as a candidate tree and is not one.
    """
    two_hunks = (
        "diff --git a/n.py b/n.py\n--- a/n.py\n+++ b/n.py\n"
        "@@ -1,2 +1,2 @@\n x = 1\n-y = 2\n+y = 3\n"
        "@@ -8,1 +8,1 @@\n-z = 4\n+z = 5\n"
    )
    assert whole_file_reconstruction(two_hunks) == {}
    not_at_one = (
        "diff --git a/n.py b/n.py\n--- a/n.py\n+++ b/n.py\n@@ -4,1 +4,1 @@\n-z = 4\n+z = 5\n"
    )
    assert whole_file_reconstruction(not_at_one) == {}
    no_hunk = "diff --git a/n.py b/n.py\n--- a/n.py\n+++ b/n.py\n"
    assert whole_file_reconstruction(no_hunk) == {}
    # Known-good beside them, so the three known-bads are not vacuous.
    whole = (
        "diff --git a/n.py b/n.py\n--- a/n.py\n+++ b/n.py\n"
        "@@ -1,1 +1,2 @@\n-x = 1\n+x = 2\n+y = 3\n"
    )
    assert whole_file_reconstruction(whole) == {"n.py": "x = 2\ny = 3\n"}


def test_reconstruction_evidence_records_whether_the_candidate_parses() -> None:
    """The sidecar says whether what it recovered is a tree at all, so a
    reader can tell a recoverable candidate from junk without staging it.
    Known-good: parsing content. Known-bad: content that does not parse.
    """
    whole = "diff --git a/n.py b/n.py\n--- a/n.py\n+++ b/n.py\n@@ -1,1 +1,1 @@\n-x = 1\n+x = 2\n"
    assert _reconstruction_evidence(whole) == {
        "reconstruction": {"n.py": {"content": "x = 2\n", "parses": True}}
    }
    broken = (
        "diff --git a/n.py b/n.py\n--- a/n.py\n+++ b/n.py\n"
        "@@ -1,1 +1,1 @@\n-x = 1\n+def broken( :\n"
    )
    assert _reconstruction_evidence(broken)["reconstruction"]["n.py"]["parses"] is False
    # A file the harness cannot parse carries no parse verdict rather
    # than an invented one: "parses": True on a README would read as a
    # check that ran.
    prose = (
        "diff --git a/README.md b/README.md\n--- a/README.md\n+++ b/README.md\n"
        "@@ -1,1 +1,1 @@\n-old\n+new\n"
    )
    assert _reconstruction_evidence(prose) == {
        "reconstruction": {"README.md": {"content": "new\n"}}
    }
    # Nothing to record is recorded as nothing, not as an empty candidate.
    assert _reconstruction_evidence("diff --git a/n.py b/n.py\n") == {}


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
        propose=lambda node, failure, seed: DiffProposal(GOOD_DIFF, "return two instead"),
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
    assert "- Gate coverage: PASS (every changed line runs)\n" in result.transcript
    assert "- Gate red-phase: PASS (fail pre-change, pass post-change)\n" in result.transcript
    assert "- Gate requirement-binding: PASS (1 requirement(s) bound)\n" in result.transcript
    assert "- Gate mutation: PASS (100.0% >= 85.0% over 5 mutant(s))\n" in result.transcript
    assert f"- Proof: {result.proofs['n1']}\n" in result.transcript
    assert "- Issues: none (chain verifies)\n" in result.transcript
    assert read_records(journal)[0].thinking == "return two instead"
    assert "  - thought: return two instead\n" in result.transcript
    # T6-62/A1: the worker path writes files and stages them; `git apply`
    # is no longer in it, so the span that proves the write happened is
    # the staging one.
    assert re.search(
        r"  - tool git: exit 0 in \d+ms: git add -A -- n\.py\n",
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
        # T6-34: the tree the gate is about to judge -- `add -u`,
        # `write-tree`, `commit-tree`, `update-ref` -- taken after autofix
        # and before the gate, journaled like the other two snapshots.
        *["git"] * 4,
        # T2-2: the staged-adds probe behind node-scope's file-creation rule.
        "git",
        # T3-2: changed-files list for target-scope.
        "git",
        "coverage",
        "git",
        # T6-3: the ruff baseline leg on the snapshot.
        "ruff",
        # One per red-phase baseline sample (#54): this node's test is
        # unchanged from its own baseline (T2-2a's honest impl fixture), so
        # `tests_changed` is False and only one sample is taken.
        *["coverage"] * 1,
        # T6-3: both ruff legs on the current tree run in the runner, before
        # the mutation sample, not inside the gate predicate.
        "ruff",
        "ruff",
        "timeout",
        # `results`, then one `show` per mutant in the sample (#49).
        "mutmut",
        *["mutmut"] * MIN_SIGNIFICANT_MUTANTS,
        # T3-10: the tree the gate passed on -- `add -u`, `write-tree`,
        # `commit-tree`, `update-ref` again -- taken after the verdict and
        # before the record is sealed, so the proof names its worktree.
        *["git"] * 4,
    ]
    assert all(span.node_id == "n1" for span in tools)
    (worker, run) = [span for span in spans if span.kind == "agent"]
    assert (worker.name, worker.exit_code) == ("worker:n1", 0)
    # The seal records sampling agreement: the correlation signal is
    # only useful if it is written down (#59). Known-bad-for-diversity: a
    # constant proposer still dedups to one distinct sample.
    # A passing seal carries the sampling count and nothing after it (T3-25).
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
        propose=lambda node, failure, seed: next(proposals),
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
        propose=lambda node, failure, seed: DiffProposal(GOOD_DIFF, "return two instead"),
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
        propose=lambda node, failure, seed: DiffProposal(GOOD_DIFF, "return two instead"),
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
        propose=lambda node, failure, seed: DiffProposal(BAD_DIFF, ""),
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

    def propose(node: Node, failure: str | None, seed: int = 0) -> DiffProposal:
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
    # T6-62/A1: the worker path writes files and stages them; `git apply`
    # is no longer in it, so the span that proves the write happened is
    # the staging one.
    assert re.search(
        r"  - tool git: exit 0 in \d+ms: git add -A -- n\.py\n",
        result.transcript,
    )
    journal = tmp_path / "proofs.jsonl"
    spans = read_spans(journal)
    agents = [span for span in spans if span.kind == "agent"]
    assert [span.name for span in agents] == ["worker:a", "worker:a", "run"]
    (first, second, run) = agents
    assert (first.exit_code, first.detail) == (1, "attempt 1/3: 1 gate(s) failed: coverage")
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
    # Every gate run hangs off the attempt that applied the diff; what hangs
    # off the halting attempt is the restore that undoes it and the two
    # diffs proving the tree is back at the baseline (T3-23).
    assert {tool.parent_id for tool in tools} == {first.span_id, second.span_id}
    assert [tool.name for tool in tools if tool.parent_id == second.span_id] == [
        "restore-baseline",
        "git",
        "git",
    ]


def test_run_slice_unappliable_diff_fails_without_checks(tmp_path: Path) -> None:
    _slice_repo(tmp_path)
    dag = Dag.model_validate({"nodes": [_node_dict("n1", [])]})
    result = run_slice(
        "Fix f.",
        dag,
        workdir=tmp_path,
        journal_path=tmp_path / "proofs.jsonl",
        propose=lambda node, failure, seed: DiffProposal("not a diff\n", ""),
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
    assert [_git_subcommand(span.argv) for span in git_runs] == [
        "add",
        "write-tree",
        "commit-tree",
        "update-ref",
    ]
    assert len([span for span in spans if span.kind == "agent"]) == 3
    assert "- Attempts: 2\n" in result.transcript


def test_run_slice_distinct_unappliable_diffs_exhaust_attempts(tmp_path: Path) -> None:
    _slice_repo(tmp_path)
    dag = Dag.model_validate({"nodes": [_node_dict("n1", [])]})
    journal = tmp_path / "proofs.jsonl"
    diffs = ["garbage one\n", "garbage two\n", "garbage three\n"]
    calls: list[str | None] = []

    def propose(node: Node, failure: str | None, seed: int = 0) -> DiffProposal:
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
    assert [_git_subcommand(span.argv) for span in git_runs] == [
        "add",
        "write-tree",
        "commit-tree",
        "update-ref",
    ]
    assert {span.parent_id for span in git_runs} == {workers[0].span_id}


def test_run_slice_retry_repairs_failing_tests(tmp_path: Path) -> None:
    _slice_repo(tmp_path)
    dag = Dag.model_validate({"nodes": [_node_dict("n1", [])]})
    journal = tmp_path / "proofs.jsonl"
    seen_failures: list[str | None] = []

    def propose(node: Node, failure: str | None, seed: int = 0) -> DiffProposal:
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
    assert agents[0].detail == "attempt 1/3: 3 gate(s) failed: tests, red-phase, mutation"
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

    def propose(_node: Node, failure: str | None, seed: int = 0) -> DiffProposal:
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
    # flip (T6-31, F21.13d): the tests gate failed, so its output reaches
    # this `lint`-only node; before, it was withheld and the worker repaired
    # only what it could see. The passed-gate half of T3-4 is pinned in
    # test_repair_prompt_withholds_output_of_a_passed_gate_without_its_tool.
    assert "FAILED" in failure
    assert "coverage run" in failure


def test_run_slice_exhausted_retries_fail_with_attempts(tmp_path: Path) -> None:
    _slice_repo(tmp_path)
    dag = Dag.model_validate({"nodes": [_node_dict("n1", [])]})
    journal = tmp_path / "proofs.jsonl"
    diffs = [BAD_DIFF, JUNK1_DIFF, JUNK2_DIFF]
    seen_failures: list[str | None] = []

    def propose(node: Node, failure: str | None, seed: int = 0) -> DiffProposal:
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
    # that -- one more failing gate at every attempt than before. Each seal
    # names its failed gates in check order (T3-25): the node has no proof
    # record and the transcript renders the last attempt only, so this is
    # the one place attempt 1's verdict survives.
    assert [span.detail for span in agents] == [
        "attempt 1/3: 3 gate(s) failed: tests, red-phase, mutation",
        "attempt 2/3: 4 gate(s) failed: tests, coverage, red-phase, mutation",
        "attempt 3/3: 4 gate(s) failed: tests, coverage, red-phase, mutation",
        "0 proven, 1 failed, 0 undispatched",
    ]


def test_run_slice_exhausted_node_leaves_its_baseline_tree_behind(tmp_path: Path) -> None:
    """A node that gave up takes its work with it (T3-23).

    The exhausted-retries fixture applies `BAD_DIFF` to `n.py` and stages
    two new files. Before the restore, all three stayed in the worktree
    and index after the node failed, so the next thing to run on the tree
    -- a replacement's snapshot, the merge suite -- ran over unproven
    edits. Worktree and index alike must read as the node's baseline ref
    (`--staged --worktree`: an index still carrying the diff is the
    mutant this pins), and the restore is one journaled span under the
    attempt that failed.
    """
    _slice_repo(tmp_path)
    dag = Dag.model_validate({"nodes": [_node_dict("n1", [])]})
    journal = tmp_path / "proofs.jsonl"
    diffs = [BAD_DIFF, JUNK1_DIFF, JUNK2_DIFF]
    seen_failures: list[str | None] = []

    def propose(node: Node, failure: str | None, seed: int = 0) -> DiffProposal:
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
    ref = "refs/saddle/baseline/n1"
    assert run_capture(["git", "diff", ref], tmp_path).stdout == ""
    assert run_capture(["git", "diff", "--diff-filter=A", ref], tmp_path).stdout == ""
    assert run_capture(["git", "diff", "--cached", ref], tmp_path).stdout == ""
    assert (tmp_path / "n.py").read_text() == "def f():\n    return 1\n"
    assert not (tmp_path / "junk1.py").exists()
    assert not (tmp_path / "junk2.py").exists()
    spans = read_spans(journal)
    restores = [span for span in spans if span.name == "restore-baseline"]
    assert len(restores) == 1
    (restore,) = restores
    assert (restore.node_id, restore.exit_code) == ("n1", 0)
    assert ref in restore.argv
    agents = [span for span in spans if span.kind == "agent" and span.node_id == "n1"]
    assert restore.parent_id == agents[-1].span_id


def test_run_slice_replacement_starts_from_the_failed_nodes_baseline(tmp_path: Path) -> None:
    """Known-good (T3-23): the 20b run in miniature, with the restore in place.

    `n1` proposes the right edit to the wrong file for its declared
    scope: `GOOD_DIFF` turns the suite green but `target-scope` rejects
    it (`target_files=["other.py"]`), and the identical re-proposal ends
    the node after two attempts. Without the restore, `n1.r1` snapshots
    the tree `n1` left behind -- `return 2`, staged -- as its own
    baseline, and can neither apply the fix nor be red before it (20b:
    `red-phase: FAIL (tests pass pre-change; prove nothing)` on every
    attempt). With it, `n1.r1`'s baseline is `n1`'s baseline, the same
    diff proves, and the merge suite runs over `n1.r1`'s work alone.
    """
    _slice_repo(tmp_path)
    node = _node_dict("n1", [])
    node["target_files"] = ["other.py"]
    dag = Dag.model_validate({"nodes": [node]})
    journal = tmp_path / "proofs.jsonl"

    def propose(node: Node, failure: str | None, seed: int = 0) -> DiffProposal:
        return DiffProposal(GOOD_DIFF, "")

    def replan(node: Node, history: str, reserved: Sequence[str] = ()) -> Dag:
        assert "touched file(s) outside target_files: n.py" in history
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
    assert result.passed is True, result.transcript
    assert list(result.proofs) == ["n1.r1"]
    assert "- Gate red-phase: PASS" in result.transcript
    run = next(span for span in read_spans(journal) if span.name == "run")
    # `n1` is excused by its replacement, so it is not counted as failed.
    assert run.detail == "1 proven, 0 failed, 0 undispatched, merge exit 0"
    baseline = run_capture(["git", "show", "refs/saddle/baseline/n1:n.py"], tmp_path).stdout
    replacement = run_capture(["git", "show", "refs/saddle/baseline/n1.r1:n.py"], tmp_path).stdout
    assert baseline == replacement == "def f():\n    return 1\n"
    # The proven edit, and nothing of `n1`'s, is what the merge suite saw.
    assert (tmp_path / "n.py").read_text() == "def f():\n    return 2\n"


def test_run_slice_propose_error_seals_worker_span(tmp_path: Path) -> None:
    _slice_repo(tmp_path)
    dag = Dag.model_validate({"nodes": [_node_dict("n1", [])]})
    journal = tmp_path / "proofs.jsonl"

    def propose(node: Node, failure: str | None, seed: int = 0) -> DiffProposal:
        msg = "boom"
        raise RuntimeError(msg)

    calls: list[str] = []

    def replan(node: Node, history: str, reserved: Sequence[str] = ()) -> Dag:
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

    def propose(node: Node, failure: str | None, seed: int = 0) -> DiffProposal:
        return DiffProposal("not a diff\n", "")

    with pytest.raises(NodeUnappliableError) as caught:
        asyncio.run(
            _run_node(
                node, tmp_path, tmp_path / "proofs.jsonl", propose, {}, "run", (), task_hash=""
            )
        )
    error = caught.value
    assert error.node_id == "n1"
    assert error.attempts == 2
    assert error.failure is not None
    assert error.failure.startswith("Attempt 1 of 3: diff did not apply: ")
    assert str(error) == "node 'n1': identical diff re-proposed after 2 non-applying attempt(s)"


def test_run_node_attaches_the_reconstruction_when_a_diff_will_not_apply(
    tmp_path: Path,
) -> None:
    """T6-62 (C) end-to-end: a refused whole-file draw leaves its candidate
    in the sidecar, on BOTH paths that record an apply failure.

    Attempt 1's failure is recorded on the sample. A retry's is recorded
    on the ATTEMPT, while its own sample still reads "retry draw, gated in
    place" (F21.38a) -- a reader filtering on sample outcome misses it
    entirely, which is how five rounds of candidates went unexamined.

    Known-good: a whole-file draw the ladder refuses leaves a parsing
    candidate on both. Known-bad is in the unit tests beside this: a draw
    the reconstruction declines leaves no key at all, so "declined" can
    never be read as "reconstructed nothing".
    """
    _slice_repo(tmp_path)
    node = Node.model_validate(_node_dict("n1", []))
    # One hunk from line 1, whose OLD side names content the tree does not
    # have: no rung applies it, and the new side is the candidate.
    stale = (
        "diff --git a/n.py b/n.py\n--- a/n.py\n+++ b/n.py\n"
        "@@ -1,2 +1,2 @@\n-def g():\n-    return 99\n+def f():\n+    return 2\n"
    )
    later = stale.replace("+    return 2", "+    return 3")

    def propose(node: Node, failure: str | None, seed: int = 0) -> DiffProposal:
        return DiffProposal(stale if failure is None else later, "")

    with pytest.raises(NodeUnappliableError):
        asyncio.run(
            _run_node(
                node, tmp_path, tmp_path / "proofs.jsonl", propose, {}, "run", (), task_hash=""
            )
        )
    # Sidecars are named by span id, so order them by the attempt they
    # record rather than by filename.
    sidecars = sorted(
        (json.loads(path.read_text()) for path in (tmp_path / "attempts").iterdir()),
        key=lambda card: card["attempt"],
    )
    assert sidecars, "every attempt leaves a sidecar"
    on_sample = [
        sample
        for card in sidecars
        for sample in card.get("samples") or []
        if "reconstruction" in sample
    ]
    assert on_sample, "attempt 1's refused sample carries its candidate"
    assert on_sample[0]["outcome"].startswith("did not apply: ")
    assert on_sample[0]["reconstruction"] == {
        "n.py": {"content": "def f():\n    return 2\n", "parses": True}
    }
    # The apply site is shared, so attempt 1 records on both; the retry
    # records ONLY here, which is the half that was being missed.
    on_attempt = [card for card in sidecars if "reconstruction" in card]
    assert [card["attempt"] for card in on_attempt] == [1, 2]
    retry = on_attempt[1]
    assert retry["reconstruction"] == {
        "n.py": {"content": "def f():\n    return 3\n", "parses": True}
    }
    # The trap itself, pinned: the retry's own sample says nothing about
    # the apply failure, so a filter on sample outcome finds none of it.
    assert [sample["outcome"] for sample in retry["samples"]] == ["retry draw, gated in place"]
    assert not any("reconstruction" in sample for sample in retry["samples"])


def test_run_node_exhausted_nonapply_reports_unappliable(tmp_path: Path) -> None:
    _slice_repo(tmp_path)
    node = Node.model_validate(_node_dict("n1", []))
    diffs = ["garbage one\n", "garbage two\n", "garbage three\n"]
    calls: list[str | None] = []

    def propose(node: Node, failure: str | None, seed: int = 0) -> DiffProposal:
        calls.append(failure)
        phase = 0 if failure is None else sum(1 for seen in calls if seen is not None)
        return DiffProposal(diffs[min(phase, len(diffs) - 1)], "")

    with pytest.raises(NodeUnappliableError) as caught:
        asyncio.run(
            _run_node(
                node, tmp_path, tmp_path / "proofs.jsonl", propose, {}, "run", (), task_hash=""
            )
        )
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

    def propose(node: Node, failure: str | None, seed: int = 0) -> DiffProposal:
        calls.append(failure)
        if len(calls) <= PROPOSAL_SAMPLES + 1:
            raise VllmResponseError(TRUNCATED)
        return DiffProposal(GOOD_DIFF, "")

    asyncio.run(
        _run_node(node, tmp_path, tmp_path / "proofs.jsonl", propose, {}, "run", (), task_hash="")
    )
    assert (tmp_path / "n.py").read_text() == "def f():\n    return 2\n"
    # The first attempt's samples and its fallback draw all raised, so the
    # node recovered on a later attempt rather than raising.
    assert len(calls) > PROPOSAL_SAMPLES


def test_run_node_every_worker_call_truncated_fails_the_node(tmp_path: Path) -> None:
    """Retrying is not waiving: exhausting attempts still fails the node."""
    _slice_repo(tmp_path)
    node = Node.model_validate(_node_dict("n1", []))

    def propose(node: Node, failure: str | None, seed: int = 0) -> DiffProposal:
        raise VllmResponseError(TRUNCATED)

    with pytest.raises(NodeUnappliableError) as caught:
        asyncio.run(
            _run_node(
                node, tmp_path, tmp_path / "proofs.jsonl", propose, {}, "run", (), task_hash=""
            )
        )
    error = caught.value
    assert error.attempts == 3
    assert error.failure is not None
    assert error.failure.startswith("Attempt 3 of 3: worker call failed: ")


def test_retry_sidecar_reports_its_own_draw_not_the_first_attempts_samples(
    tmp_path: Path,
) -> None:
    """T6-24a known-bad. Attempt 1 draws PROPOSAL_SAMPLES and fails; attempt
    2 makes one call and seals. Round 3c's n2 carried attempt 1's
    `samples` byte for byte into attempts 2 and 3 (identical SHA-256), so
    the sidecar said three draws where the attempt made one. A sidecar's
    `samples` are the calls that attempt made.
    """
    _slice_repo(tmp_path)
    dag = Dag.model_validate({"nodes": [_node_dict("n1", [])]})
    journal = tmp_path / "proofs.jsonl"

    def propose(node: Node, failure: str | None, seed: int = 0) -> DiffProposal:
        return DiffProposal(BAD_DIFF if failure is None else FIX_DIFF, "")

    result = run_slice(
        "Fix f.",
        dag,
        workdir=tmp_path,
        journal_path=journal,
        propose=propose,
        now=lambda: "2026-09-16T00:00:00+00:00",
    )
    assert result.passed is True, result.transcript
    agents = [s for s in read_spans(journal) if s.kind == "agent" and s.name == "worker:n1"]
    assert [s.exit_code for s in agents] == [1, 0]
    failed = _sidecar(journal, agents[0])
    sealed = _sidecar(journal, agents[1])
    assert len(failed["samples"]) == PROPOSAL_SAMPLES
    assert [x["diff_hash"] for x in sealed["samples"]] == [
        hashlib.sha256(FIX_DIFF.encode()).hexdigest()
    ]
    assert sealed["samples"][0]["outcome"] == "retry draw, gated in place"


def test_failed_retry_call_sidecar_carries_no_earlier_samples(tmp_path: Path) -> None:
    """T6-24a, the path a retry's own assignment cannot cover: attempt 2's
    single worker call fails before any proposal exists. Its sidecar has
    no draws to report, and must not report attempt 1's.
    """
    _slice_repo(tmp_path)
    dag = Dag.model_validate({"nodes": [_node_dict("n1", [])]})
    journal = tmp_path / "proofs.jsonl"
    calls: list[str | None] = []

    def propose(node: Node, failure: str | None, seed: int = 0) -> DiffProposal:
        calls.append(failure)
        retries = sum(1 for f in calls if f is not None)
        if failure is None:
            return DiffProposal(BAD_DIFF, "")
        if retries == 1:
            raise VllmResponseError(TRUNCATED)
        return DiffProposal(FIX_DIFF, "")

    result = run_slice(
        "Fix f.",
        dag,
        workdir=tmp_path,
        journal_path=journal,
        propose=propose,
        now=lambda: "2026-09-16T00:00:00+00:00",
    )
    assert result.passed is True, result.transcript
    agents = [s for s in read_spans(journal) if s.kind == "agent" and s.name == "worker:n1"]
    assert [s.exit_code for s in agents] == [1, 1, 0]
    assert len(_sidecar(journal, agents[0])["samples"]) == PROPOSAL_SAMPLES
    assert _sidecar(journal, agents[1])["samples"] == []
    assert len(_sidecar(journal, agents[2])["samples"]) == 1


def test_first_attempt_draws_its_samples_concurrently(tmp_path: Path) -> None:
    """T6-25 known-good. Every sample call waits at a barrier sized for
    all PROPOSAL_SAMPLES draws: the node seals only if the draws were in
    flight together. Serial sampling parks the first call alone until the
    barrier breaks, and the node fails.
    """
    _slice_repo(tmp_path)
    dag = Dag.model_validate({"nodes": [_node_dict("n1", [])]})
    journal = tmp_path / "proofs.jsonl"
    barrier = threading.Barrier(PROPOSAL_SAMPLES, timeout=5)

    def propose(node: Node, failure: str | None, seed: int = 0) -> DiffProposal:
        barrier.wait()
        return DiffProposal(GOOD_DIFF, "")

    result = run_slice(
        "Fix f.",
        dag,
        workdir=tmp_path,
        journal_path=journal,
        propose=propose,
        now=lambda: "2026-09-16T00:00:00+00:00",
    )
    assert result.passed is True, result.transcript
    worker = next(s for s in read_spans(journal) if s.name == "worker:n1")
    assert worker.detail == f"1 distinct of {PROPOSAL_SAMPLES} sample(s)"


def test_samples_are_evaluated_in_seed_order_not_arrival_order(tmp_path: Path) -> None:
    """T6-25 known-good. Seed 0's draw is slow and passes; the last seed's
    draw is instant and also passes, with different bytes. The sealed
    diff is seed 0's: evaluation order is the seed order, so the record
    does not depend on which request the server answered first.
    """
    _slice_repo(tmp_path)
    dag = Dag.model_validate({"nodes": [_node_dict("n1", [])]})
    journal = tmp_path / "proofs.jsonl"
    seeds: list[int] = []

    def propose(node: Node, failure: str | None, seed: int = 0) -> DiffProposal:
        seeds.append(seed)
        if seed == 0:
            time.sleep(0.3)
            return DiffProposal(GOOD_DIFF, "")
        return DiffProposal(SLOPPY_DIFF, "")

    result = run_slice(
        "Fix f.",
        dag,
        workdir=tmp_path,
        journal_path=journal,
        propose=propose,
        now=lambda: "2026-09-16T00:00:00+00:00",
    )
    assert result.passed is True, result.transcript
    assert sorted(seeds) == list(range(PROPOSAL_SAMPLES))
    worker = next(s for s in read_spans(journal) if s.name == "worker:n1")
    sealed = _sidecar(journal, worker)
    assert sealed["diff_hash"] == hashlib.sha256(GOOD_DIFF.encode()).hexdigest()
    assert [x["outcome"] for x in sealed["samples"]] == [
        "0 gate(s) failed",
        *["not evaluated: an earlier sample passed"] * (PROPOSAL_SAMPLES - 1),
    ]


SLOPPY_DIFF = GOOD_DIFF.replace("+    return 2\n", "+    return  2\n")


def test_sampler_scores_a_candidate_the_way_the_gate_will_see_it(tmp_path: Path) -> None:
    """T6-24b known-bad. The only thing wrong with the sample is spacing
    `ruff format` repairs; the live path runs `autofix` before the gate,
    so the node passes on the first draw. The sampler scored it one gate
    red (round 3c: delta one on three of four nodes) and drew all
    PROPOSAL_SAMPLES, each identical, paying two worker calls for a
    candidate that had already won. A candidate is scored by the same
    sequence the node is gated by.
    """
    _slice_repo(tmp_path)
    dag = Dag.model_validate({"nodes": [_node_dict("n1", [])]})
    journal = tmp_path / "proofs.jsonl"
    calls: list[str | None] = []

    def propose(node: Node, failure: str | None, seed: int = 0) -> DiffProposal:
        calls.append(failure)
        return DiffProposal(SLOPPY_DIFF, "")

    result = run_slice(
        "Fix f.",
        dag,
        workdir=tmp_path,
        journal_path=journal,
        propose=propose,
        now=lambda: "2026-09-16T00:00:00+00:00",
    )
    assert result.passed is True, result.transcript
    # All k draws are paid up front (T6-25); what autofix-aware scoring
    # buys is that the first sample is seen to pass and nothing after it
    # is evaluated -- before, every sample scored one gate red.
    assert calls == [None] * PROPOSAL_SAMPLES
    sealed = _sidecar(journal, next(s for s in read_spans(journal) if s.name == "worker:n1"))
    assert [x["outcome"] for x in sealed["samples"]] == [
        "0 gate(s) failed",
        *["not evaluated: an earlier sample passed"] * (PROPOSAL_SAMPLES - 1),
    ]


def _tree_at(root: Path, ref: str) -> str:
    return run_capture(["git", "rev-parse", f"{ref}^{{tree}}"], root).stdout.strip()


def test_a_gated_attempt_leaves_a_ref_for_the_autofixed_tree_the_gate_saw(
    tmp_path: Path,
) -> None:
    """T6-34 known-good. `SLOPPY_DIFF` is the one case where the applied
    text and the graded text differ: `autofix` reformats it before the
    gate runs. The ref holds what the gate was given, so it carries the
    single space, and the sidecar's recorded tree is that same tree --
    the sidecar's `diff` is the pre-autofix bytes and cannot stand in
    for it.
    """
    _slice_repo(tmp_path)
    node = Node.model_validate(_node_dict("n1", []))
    journal = tmp_path / "proofs.jsonl"

    def propose(node: Node, failure: str | None, seed: int = 0) -> DiffProposal:
        return DiffProposal(SLOPPY_DIFF, "")

    asyncio.run(_run_node(node, tmp_path, journal, propose, {}, "run", (), task_hash=""))
    ref = attempt_ref("n1", 1)
    shown = run_capture(["git", "show", f"{ref}:n.py"], tmp_path)
    assert shown.exit_code == 0, shown.stderr
    assert shown.stdout == "def f():\n    return 2\n"
    sealed = _sidecar(journal, next(s for s in read_spans(journal) if s.name == "worker:n1"))
    assert sealed["tree"] == _tree_at(tmp_path, ref)


def test_each_attempt_of_a_node_keeps_its_own_graded_tree(tmp_path: Path) -> None:
    """T6-34 known-good and vacuity guard. Attempt 1 fails its gate and
    attempt 2 repairs it in place, so the two trees differ by the one
    line the gate disagreed about. Both are named, both sidecars point
    at their own, and `/1` still resolves after `/2` is written -- a ref
    per node rather than per attempt reports the same two refs as one,
    which is how round 3d's attempt-1 trees became `lost-found` blobs
    (F21.15).
    """
    _slice_repo(tmp_path)
    node = Node.model_validate(_node_dict("n1", []))
    journal = tmp_path / "proofs.jsonl"

    def propose(node: Node, failure: str | None, seed: int = 0) -> DiffProposal:
        return DiffProposal(BAD_DIFF if failure is None else FIX_DIFF, "")

    asyncio.run(_run_node(node, tmp_path, journal, propose, {}, "run", (), task_hash=""))
    first, second = attempt_ref("n1", 1), attempt_ref("n1", 2)
    assert run_capture(["git", "show", f"{first}:n.py"], tmp_path).stdout.endswith("return 3\n")
    assert run_capture(["git", "show", f"{second}:n.py"], tmp_path).stdout.endswith("return 2\n")
    assert _tree_at(tmp_path, first) != _tree_at(tmp_path, second)
    workers = [s for s in read_spans(journal) if s.name == "worker:n1"]
    assert [_sidecar(journal, span)["tree"] for span in workers] == [
        _tree_at(tmp_path, first),
        _tree_at(tmp_path, second),
    ]


GOOD_EDITS = "edit n.py\n-    return 1\n=======\n+    return 2\n>>>>>>>\n"


def test_a_node_seals_on_an_edit_payload_the_way_it_does_on_a_file_payload(
    tmp_path: Path,
) -> None:
    """T6-77 known-good, driven from the caller rather than the function.

    The dispatch's first home was `_apply_diff`, which nothing in `src`
    calls; its own tests were green while every draw of the first
    `--emission edit` run was refused for a missing `diff --git` header.
    A unit test on an apply function cannot tell those two stories apart,
    so this one goes through `_run_node` -- the path a worker proposal
    actually takes, scoring copy included -- and stays red unless the
    dispatch sits where the proposal lands.
    """
    _slice_repo(tmp_path)
    node = Node.model_validate(_node_dict("n1", []))
    journal = tmp_path / "proofs.jsonl"

    def propose(node: Node, failure: str | None, seed: int = 0) -> DiffProposal:
        return DiffProposal(GOOD_EDITS, "")

    asyncio.run(_run_node(node, tmp_path, journal, propose, {}, "run", (), task_hash=""))
    assert (tmp_path / "n.py").read_text() == "def f():\n    return 2\n"
    ref = attempt_ref("n1", 1)
    assert run_capture(["git", "show", f"{ref}:n.py"], tmp_path).stdout.endswith("return 2\n")


def test_run_node_transport_failure_restores_the_tree_and_names_itself(tmp_path: Path) -> None:
    """F21.12b known-bad. Attempt 1 applies a diff that fails its gate;
    attempt 2's worker call dies in transport (round 3c: `request failed:
    timed out` after 1826 s). The error still fails the node, but the
    applied diff must not stay in the tree -- n2.r2 left 530 lines of
    unproven `money.py` staged and the oracle graded them -- and the
    sealed attempt's sidecar must say what kind of failure it was, not
    arrive as four keys with nothing the client knew.
    """
    _slice_repo(tmp_path)
    node = Node.model_validate(_node_dict("n1", []))
    journal = tmp_path / "proofs.jsonl"
    calls: list[str | None] = []
    timed_out = "request failed: timed out"

    def propose(node: Node, failure: str | None, seed: int = 0) -> DiffProposal:
        calls.append(failure)
        if failure is None:
            return DiffProposal(BAD_DIFF, "")
        raise VllmRequestError(timed_out)

    with pytest.raises(VllmRequestError):
        asyncio.run(_run_node(node, tmp_path, journal, propose, {}, "run", (), task_hash=""))
    assert calls[-1] is not None, "the transport failure was the retry, after an applied diff"
    assert (tmp_path / "n.py").read_text() == "def f():\n    return 1\n"
    status = run_capture(["git", "status", "--porcelain"], tmp_path).stdout.splitlines()
    # Untracked run artefacts (journal, sidecars, coverage data) are not
    # node work; nothing tracked may be modified or staged.
    assert [line for line in status if not line.startswith("??")] == []
    workers = [r for r in read_spans(journal) if r.name.startswith("worker:")]
    sidecar = _sidecar(journal, workers[-1])
    assert sidecar["detail"] == timed_out
    assert sidecar["error_type"] == "VllmRequestError"
    assert "samples" in sidecar


def test_run_node_exhausted_gate_failures_reports_failure(tmp_path: Path) -> None:
    _slice_repo(tmp_path)
    node = Node.model_validate(_node_dict("n1", []))
    diffs = [BAD_DIFF, JUNK1_DIFF, JUNK2_DIFF]
    seen: list[str | None] = []

    def propose(node: Node, failure: str | None, seed: int = 0) -> DiffProposal:
        seen.append(failure)
        phase = 0 if failure is None else sum(1 for entry in seen if entry is not None)
        return DiffProposal(diffs[min(phase, len(diffs) - 1)], "")

    with pytest.raises(NodeGateFailedError) as caught:
        asyncio.run(
            _run_node(
                node, tmp_path, tmp_path / "proofs.jsonl", propose, {}, "run", (), task_hash=""
            )
        )
    error = caught.value
    assert error.attempts == 3
    assert error.result.passed is False
    assert error.failure is not None
    assert "Attempt 3 of 3" in error.failure


def test_run_node_missing_proof_seals_attempt_with_tool_linkage(tmp_path: Path) -> None:
    _slice_repo(tmp_path)
    node = Node.model_validate(_node_dict("n1", ["ghost"]))
    journal = tmp_path / "proofs.jsonl"

    def propose(node: Node, failure: str | None, seed: int = 0) -> DiffProposal:
        return DiffProposal(GOOD_DIFF, "")

    with pytest.raises(KeyError):
        asyncio.run(_run_node(node, tmp_path, journal, propose, {}, "run-test", (), task_hash=""))
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
    # T6-31: the tests gate FAILED in `_failed_result`, so its output is
    # the node's own evidence and reaches the worker whatever the plan
    # declared (flip: this assertion said "withheld" until F21.13d showed
    # ruff failing 2/2 on a brief that named no rule while the gates with
    # inline detail improved). The passed-gate half is the test below.
    text = format_attempt_failure(
        _failed_result(), captured, attempt=1, max_attempts=3, tools=["lint"]
    )
    assert "assert 1 == 2" in text
    assert "--- `ruff check n.py` (exit 1) ---" in text
    assert "n.py:1:1 F401" in text
    assert "- tests: 'pytest test_n.py' exited 1" in text


def test_repair_prompt_withholds_output_of_a_passed_gate_without_its_tool() -> None:
    """T3-4 known-bad, kept under T6-31: a captured run whose gate PASSED
    stays governed by `allowed_tools`. Here ruff failed and the suite
    passed, so a `lint`-only node sees ruff and not the suite."""
    result = Tier1Result(
        node_id="n1",
        passed=False,
        checks=(
            GateCheck(name="tests", passed=True, detail="'pytest test_n.py' exited 0"),
            GateCheck(name="ruff", passed=False, detail="introduced 1 finding(s): n.py:1 F401"),
        ),
    )
    captured = [
        CapturedRun(
            argv=("coverage", "run", "-m", "pytest"),
            exit_code=1,
            stdout="E   assert 1 == 2",
            stderr="",
        ),
        CapturedRun(argv=("ruff", "check", "n.py"), exit_code=1, stdout="n.py:1:1 F401", stderr=""),
    ]
    text = format_attempt_failure(result, captured, attempt=1, max_attempts=3, tools=["lint"])
    assert "assert 1 == 2" not in text
    assert "n.py:1:1 F401" in text
    # And the node that declared nothing still sees the failed gate's output.
    text = format_attempt_failure(result, captured, attempt=1, max_attempts=3, tools=["read_file"])
    assert "n.py:1:1 F401" in text
    assert "assert 1 == 2" not in text


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
    # A failed gate that maps to no tool (T6-31 includes a failed gate's
    # output whatever the plan says), so only the binding decides here.
    result = Tier1Result(
        node_id="n1",
        passed=False,
        checks=(GateCheck(name="node-scope", passed=False, detail="impl node changed tests"),),
    )

    def prompt(tools: list[str]) -> str:
        return format_attempt_failure(result, captured, attempt=1, max_attempts=3, tools=tools)

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


def test_splice_replan_numbers_past_ids_the_dag_already_holds() -> None:
    """flip (T3-11): this pinned `ReplanFailedError("collides")` for a DAG
    that already held `a.r1`. `a.r1` is a legal, non-duplicate node id
    (`dag.py` rejects duplicates on its own), so the raise refused a
    legitimate input rather than guarding a contract; numbering past the
    taken id makes the collision unreachable."""
    dag = Dag.model_validate({"nodes": [_node_dict("a", []), _node_dict("a.r1", [])]})
    new = Dag.model_validate({"nodes": [_node_dict("m1", [])]})
    spliced, generated = splice_replan(dag, "a", new)
    assert generated == ["a.r2"]
    assert [node.id for node in spliced.nodes] == ["a", "a.r1", "a.r2"]


def test_splice_replan_numbers_past_taken_ids() -> None:
    """Known-good (T3-11): ids sealed in a journal count as taken even when
    the DAG does not carry them, and two replacements skip together."""
    dag = Dag.model_validate({"nodes": [_node_dict("a", []), _node_dict("b", ["a"])]})
    new = Dag.model_validate({"nodes": [_node_dict("m1", []), _node_dict("m2", ["m1"])]})
    spliced, generated = splice_replan(dag, "a", new, taken={"a.r1", "a.r3"})
    assert generated == ["a.r2", "a.r4"]
    by_id = {node.id: node for node in spliced.nodes}
    assert by_id["a.r4"].dependencies == ["a.r2"]
    assert by_id["b"].dependencies == ["a.r4"]


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

    def propose(node: Node, failure: str | None, seed: int = 0) -> DiffProposal:
        # The replacement starts from `n1`'s baseline (`return 1`), not from
        # the tree `n1`'s failed diff left behind (T3-23): it proposes the
        # whole fix, not a repair of `return 3`.
        return DiffProposal(BAD_DIFF if node.id == "n1" else GOOD_DIFF, "")

    def replan(node: Node, history: str, reserved: Sequence[str] = ()) -> Dag:
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


def test_run_slice_replan_is_told_which_files_a_pending_node_still_owes(
    tmp_path: Path,
) -> None:
    """T6-65: the scheduler computes the reserved set and hands it to the replanner.

    Round 3i's shape exactly -- `n2` depends on `n1`, `n1` fails, and
    `n2` has therefore not run. Without this argument the replanner is
    given the whole task and no way to know `n2` exists, and it plans
    `n2`'s work a second time (F21.40).

    The assertion is on the value, not on the call: threading a constant
    through would satisfy a test that only checked the replanner was
    called with three arguments.
    """
    _slice_repo(tmp_path)
    dag = Dag.model_validate(
        {
            "nodes": [
                {**_node_dict("n1", []), "kind": "test", "target_files": ["tests/test_f.py"]},
                {**_node_dict("n2", ["n1"]), "target_files": ["f.py", "g.py"]},
            ]
        }
    )
    seen: list[Sequence[str]] = []

    def propose(node: Node, failure: str | None, seed: int = 0) -> DiffProposal:
        return DiffProposal(BAD_DIFF if node.id == "n1" else GOOD_DIFF, "")

    def replan(node: Node, history: str, reserved: Sequence[str] = ()) -> Dag:
        seen.append(reserved)
        msg = "no subplan"
        raise ReplanFailedError(msg)

    run_slice(
        "Fix f.",
        dag,
        workdir=tmp_path,
        journal_path=tmp_path / "proofs.jsonl",
        propose=propose,
        replan=replan,
        now=lambda: "2026-09-16T00:00:00+00:00",
    )
    # `n2` is pending and owes both files; `n1` is the node being
    # replaced, so its own file is not reserved against its replacement.
    assert seen == [("f.py", "g.py")]


def test_run_slice_replan_across_resume_numbers_past_sealed_ids(tmp_path: Path) -> None:
    """Known-good (T3-11): a journal that already seals `n1.r1` from an
    earlier run's replan is not reused (the DAG has no `n1.r1` to compare
    with) and the `resume` span says why; the new replan is `n1.r2`, so
    the journal holds one record per id.

    Known-bad: with numbering that saw only the DAG, the second replan was
    `n1.r1` again and the journal held two records under that id, the
    later shadowing the earlier in `rebuild_proven`.
    """
    _slice_repo(tmp_path)
    journal = tmp_path / "proofs.jsonl"
    append_record(
        journal,
        build_record(
            evidence_id="n1.r1#1",
            node_id="n1.r1",
            diff="diff n1.r1\n",
            parent_proofs=[],
            gate_outputs=[GateOutput(name="tests", passed=True, detail="ok")],
            requirement_ids=["REQ-001"],
            thinking="an earlier run's replacement",
            node_hash="0" * 64,
        ),
    )
    dag = Dag.model_validate({"nodes": [_node_dict("n1", [])]})

    def propose(node: Node, failure: str | None, seed: int = 0) -> DiffProposal:
        return DiffProposal(BAD_DIFF if node.id == "n1" else GOOD_DIFF, "")

    def replan(node: Node, history: str, reserved: Sequence[str] = ()) -> Dag:
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
    assert result.passed is True, result.transcript
    assert list(result.proofs) == ["n1.r2"]
    assert _resume_span(journal).detail == "dropped n1.r1 (not in DAG)"
    ids = [record.node_id for record in read_records(journal)]
    assert ids == ["n1.r1", "n1.r2"]
    assert len(ids) == len(set(ids))


def test_run_slice_replanned_node_failure_stays_failed(tmp_path: Path) -> None:
    _slice_repo(tmp_path)
    dag = Dag.model_validate({"nodes": [_node_dict("n1", [])]})
    journal = tmp_path / "proofs.jsonl"
    calls: list[str] = []

    def propose(node: Node, failure: str | None, seed: int = 0) -> DiffProposal:
        calls.append(node.id)
        return DiffProposal(BAD_DIFF, "")

    def replan(node: Node, history: str, reserved: Sequence[str] = ()) -> Dag:
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

    def propose(node: Node, failure: str | None, seed: int = 0) -> DiffProposal:
        return DiffProposal(BAD_DIFF, "")

    def replan(node: Node, history: str, reserved: Sequence[str] = ()) -> Dag:
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

    def propose(node: Node, failure: str | None, seed: int = 0) -> DiffProposal:
        if node.id in ("a", "b"):
            return DiffProposal(BAD_DIFF, "")
        # `b.r1` starts from `b`'s baseline, not from `return 3` (T3-23).
        return DiffProposal(GOOD_DIFF, "")

    def replan(node: Node, history: str, reserved: Sequence[str] = ()) -> Dag:
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

    def replan(node: Node, history: str, reserved: Sequence[str] = ()) -> Dag:
        calls.append(node.id)
        return dag

    result = run_slice(
        "Fix f.",
        dag,
        workdir=tmp_path,
        journal_path=tmp_path / "proofs.jsonl",
        propose=lambda node, failure, seed: DiffProposal(GOOD_DIFF, ""),
        replan=replan,
        now=lambda: "2026-09-16T00:00:00+00:00",
    )
    assert result.passed is True
    assert calls == []


TIDY_DIFF = whole_file("n.py", "def f():", "    return 2", "# tidy")

# A refactor with a statement to its name: the value `n1` proved, spelled as
# a sum. Behaviour preserved, the same test still pins it, and the changed
# line is one coverage can cover and mutation can mutate.
REFACTOR_DIFF = whole_file("n.py", "def f():", "    return 1 + 1")


def _first_run(tmp_path: Path) -> tuple[Path, SliceResult]:
    _slice_repo(tmp_path)
    dag = Dag.model_validate({"nodes": [_node_dict("n1", [])]})
    journal = tmp_path / "proofs.jsonl"
    first = run_slice(
        "Fix f.",
        dag,
        workdir=tmp_path,
        journal_path=journal,
        propose=lambda node, failure, seed: DiffProposal(GOOD_DIFF, "return two instead"),
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

    def propose(node: Node, failure: str | None, seed: int = 0) -> DiffProposal:
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


class _Ticks:
    """A clock the test advances: every worker call costs `step` seconds."""

    def __init__(self, step: float) -> None:
        self.now = 0.0
        self.step = step

    def __call__(self) -> float:
        return self.now

    def spend(self) -> None:
        self.now += self.step


def test_run_slice_deadline_seals_what_it_has_and_resumes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Known-good (T6-9): two nodes, a deadline that fits one. `n1` runs
    (one 5 s worker call; its first sample passes), then `n2` is not
    started: 3 s are left and the median node wall is 5 s. The run seals `n1`'s proof,
    exits 3 with `deadline:` in its span, leaves the tree holding proven
    edits only, and a resume with no deadline finishes `n2` and passes.
    Known-bad, round 2 and round 3 T5: an external `timeout` ended the
    process with nothing sealed."""
    _slice_repo(tmp_path)
    tidy = _node_dict("n2", ["n1"])
    tidy["kind"] = "refactor"
    dag = Dag.model_validate({"nodes": [_node_dict("n1", []), tidy]})
    journal = tmp_path / "proofs.jsonl"
    ticks = _Ticks(step=5.0)
    proposed: list[str] = []

    def propose(node: Node, failure: str | None, seed: int = 0) -> DiffProposal:
        proposed.append(node.id)
        ticks.spend()
        return DiffProposal(GOOD_DIFF if node.id == "n1" else REFACTOR_DIFF, "ok")

    result = run_slice(
        "Fix f.",
        dag,
        workdir=tmp_path,
        journal_path=journal,
        propose=propose,
        now=lambda: "2026-09-16T00:00:00+00:00",
        deadline_s=8.0,
        clock=ticks,
    )
    assert result.deadline_hit is True
    assert result.passed is False
    assert list(result.proofs) == ["n1"]
    assert proposed == ["n1"] * PROPOSAL_SAMPLES
    run = [span for span in read_spans(journal) if span.name == "run"][-1]
    assert run.exit_code == 3
    assert run.detail == "deadline: 1 proven, 0 failed, 1 undispatched, merge exit 0"
    assert (tmp_path / "n.py").read_text() == "def f():\n    return 2\n"
    assert verify_journal(journal) == []
    # Resume, no clock: n2 runs on the proven tree and the run passes. Its
    # mutants sit on the line the refactor writes (as in the T3-8 tests).
    _refactor_mutmut(tmp_path / "stub", monkeypatch)
    second = run_slice(
        "Fix f.",
        dag,
        workdir=tmp_path,
        journal_path=journal,
        propose=propose,
        now=lambda: "2026-09-16T00:00:00+00:00",
    )
    assert second.passed is True, second.transcript
    assert second.deadline_hit is False
    assert list(second.proofs) == ["n1", "n2"]
    assert proposed[PROPOSAL_SAMPLES:] == ["n2"] * PROPOSAL_SAMPLES
    run = [span for span in read_spans(journal) if span.name == "run"][-1]
    assert run.exit_code == 0


def test_run_slice_deadline_lets_the_attempt_in_flight_finish_and_seal(tmp_path: Path) -> None:
    """Known-bad half (T6-9): a node running at the deadline is not killed
    mid-gate. The deadline (3 s) passes during `n1`'s first attempt (k
    concurrent draws, 5 s each on this clock, T6-25); the attempt finishes,
    its gate passes and its proof is sealed; only then does the run stop,
    `n2` undispatched, with the deadline recorded."""
    _slice_repo(tmp_path)
    dag = Dag.model_validate({"nodes": [_node_dict("n1", []), _node_dict("n2", ["n1"])]})
    journal = tmp_path / "proofs.jsonl"
    ticks = _Ticks(step=5.0)

    def propose(node: Node, failure: str | None, seed: int = 0) -> DiffProposal:
        ticks.spend()
        return DiffProposal(GOOD_DIFF, "ok")

    result = run_slice(
        "Fix f.",
        dag,
        workdir=tmp_path,
        journal_path=journal,
        propose=propose,
        now=lambda: "2026-09-16T00:00:00+00:00",
        deadline_s=3.0,
        clock=ticks,
    )
    assert ticks.now == 5.0 * PROPOSAL_SAMPLES
    assert list(result.proofs) == ["n1"]
    assert result.deadline_hit is True
    run = [span for span in read_spans(journal) if span.name == "run"][-1]
    assert run.exit_code == 3
    assert run.detail.startswith("deadline: 1 proven, 0 failed, 1 undispatched")


def test_run_slice_deadline_starts_no_retry_and_restores_the_tree(tmp_path: Path) -> None:
    """T6-9: attempt 1 fails its gate and the deadline has passed, so no
    recovery attempt is proposed; the node gives up through the ordinary
    path (T3-23): its diff leaves the worktree, the node counts as failed
    after one attempt, and nothing is sealed as proven. Three identical
    samples at 5 s each put the clock at 15 s against a 10 s deadline."""
    _slice_repo(tmp_path)
    dag = Dag.model_validate({"nodes": [_node_dict("n1", [])]})
    journal = tmp_path / "proofs.jsonl"
    ticks = _Ticks(step=5.0)
    calls: list[str | None] = []

    def propose(node: Node, failure: str | None, seed: int = 0) -> DiffProposal:
        calls.append(failure)
        ticks.spend()
        return DiffProposal(BAD_DIFF, "wrong value")

    result = run_slice(
        "Fix f.",
        dag,
        workdir=tmp_path,
        journal_path=journal,
        propose=propose,
        now=lambda: "2026-09-16T00:00:00+00:00",
        deadline_s=10.0,
        clock=ticks,
    )
    assert calls == [None] * PROPOSAL_SAMPLES
    assert result.proofs == {}
    assert result.deadline_hit is True
    assert (tmp_path / "n.py").read_text() == "def f():\n    return 1\n"
    workers = [s for s in read_spans(journal) if s.kind == "agent" and s.name == "worker:n1"]
    assert [w.detail for w in workers] == [
        "attempt 1/3: 3 gate(s) failed: tests, red-phase, mutation"
    ]
    run = [span for span in read_spans(journal) if span.name == "run"][-1]
    assert run.exit_code == 3
    assert run.detail == "deadline: 0 proven, 1 failed, 0 undispatched"


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
        propose=lambda node, failure, seed: DiffProposal(TIDY_DIFF, "tidy"),
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

    def propose(node: Node, failure: str | None, seed: int = 0) -> DiffProposal:
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


def _resume_span(journal: Path) -> SpanRecord:
    """The last `resume` span: what the seed decided, sealed (T3-9)."""
    return [span for span in read_spans(journal) if span.name == "resume"][-1]


def test_run_slice_resume_span_names_the_proofs_it_reused(tmp_path: Path) -> None:
    """Known-good (T3-9): same task, same node, so the proof is reused --
    and the run says so in one agent span under the run span, rather than
    a silently shorter schedule. `saddle tail` keeps following, because
    `is_run_end` matches the run span alone."""
    journal, first = _first_run(tmp_path)
    dag = Dag.model_validate({"nodes": [_node_dict("n1", [])]})

    def propose(node: Node, failure: str | None, seed: int = 0) -> DiffProposal:
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
    assert second.proofs == first.proofs
    resume = _resume_span(journal)
    assert resume.detail == "reused n1"
    assert resume.kind == "agent"
    run = [span for span in read_spans(journal) if span.name == "run"][-1]
    assert resume.parent_id == run.span_id
    assert not is_run_end(resume)


def test_run_slice_refuses_a_journal_sealed_for_a_different_task(tmp_path: Path) -> None:
    """Known-bad (T3-9a): the CLI's default journal is per repo, so a
    second task run in the same checkout resumed onto the first task's
    proofs and counted every same-id node as already proven. It now stops
    before any node runs, naming both hashes and the way out."""
    journal, _first = _first_run(tmp_path)
    dag = Dag.model_validate({"nodes": [_node_dict("n1", [])]})

    def propose(node: Node, failure: str | None, seed: int = 0) -> DiffProposal:
        msg = "nothing should be proposed"
        raise AssertionError(msg)

    sealed = hashlib.sha256(b"Fix f.").hexdigest()
    current = hashlib.sha256(b"Fix g.").hexdigest()
    with pytest.raises(ValueError, match=r"sealed for a different task") as caught:
        run_slice(
            "Fix g.",
            dag,
            workdir=tmp_path,
            journal_path=journal,
            propose=propose,
            now=lambda: "2026-09-16T00:00:00+00:00",
        )
    message = str(caught.value)
    assert sealed in message
    assert current in message
    assert "--journal" in message
    # Nothing was written: the refusal precedes the resume span too.
    assert [span.name for span in read_spans(journal) if span.name == "resume"] == []


def test_run_slice_resume_reschedules_a_node_changed_since_its_proof(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Known-bad (T3-9b): editing `n1`'s prompt and rerunning used to
    reuse the proof of the node as it was written before the edit.

    Both runs prove `n1` as a refactor, which is the only kind whose
    second proposal is legal here: the first run leaves the worktree
    green, so an `impl` node could not be red pre-change without editing
    the tests it is forbidden to touch.
    """
    _slice_repo(tmp_path)
    journal = tmp_path / "proofs.jsonl"
    before = _node_dict("n1", [])
    before["kind"] = "refactor"
    first = run_slice(
        "Fix f.",
        Dag.model_validate({"nodes": [before]}),
        workdir=tmp_path,
        journal_path=journal,
        propose=lambda node, failure, seed: DiffProposal(GOOD_DIFF, "return two instead"),
        now=lambda: "2026-09-16T00:00:00+00:00",
    )
    assert first.passed is True, first.transcript

    _refactor_mutmut(tmp_path / "stub", monkeypatch)
    after = _node_dict("n1", [])
    after["kind"] = "refactor"
    after["task_prompt"] = "Do n1, and spell the value as a sum."
    proposed: list[str] = []

    def propose(node: Node, failure: str | None, seed: int = 0) -> DiffProposal:
        proposed.append(node.id)
        return DiffProposal(REFACTOR_DIFF, "same value, spelled as a sum")

    second = run_slice(
        "Fix f.",
        Dag.model_validate({"nodes": [after]}),
        workdir=tmp_path,
        journal_path=journal,
        propose=propose,
        now=lambda: "2026-09-16T00:00:00+00:00",
    )
    assert second.passed is True, second.transcript
    assert proposed == ["n1"] * PROPOSAL_SAMPLES
    assert _resume_span(journal).detail == "dropped n1 (node changed)"
    records = read_records(journal)
    assert [record.node_id for record in records] == ["n1", "n1"]
    assert records[0].node_hash != records[1].node_hash
    assert records[0].task_hash == records[1].task_hash
    assert second.proofs["n1"] == records[1].record_hash
    assert second.proofs["n1"] != first.proofs["n1"]


def test_run_slice_resume_drops_a_record_that_predates_the_node_hash(tmp_path: Path) -> None:
    """A journal sealed before T3-9 says nothing about which node it
    proved, so it is not proof of this one: `n1` is scheduled again and
    the empty `task_hash` is tolerated rather than fatal."""
    _slice_repo(tmp_path)
    journal = tmp_path / "proofs.jsonl"
    append_record(
        journal,
        build_record(
            evidence_id="n1#1",
            node_id="n1",
            diff="diff n1\n",
            parent_proofs=[],
            gate_outputs=[GateOutput(name="tests", passed=True, detail="ok")],
            requirement_ids=["REQ-001"],
            thinking="sealed before T3-9",
        ),
    )
    dag = Dag.model_validate({"nodes": [_node_dict("n1", [])]})
    proposed: list[str] = []

    def propose(node: Node, failure: str | None, seed: int = 0) -> DiffProposal:
        proposed.append(node.id)
        return DiffProposal(GOOD_DIFF, "return two instead")

    result = run_slice(
        "Fix f.",
        dag,
        workdir=tmp_path,
        journal_path=journal,
        propose=propose,
        now=lambda: "2026-09-16T00:00:00+00:00",
    )
    assert result.passed is True, result.transcript
    assert proposed == ["n1"] * PROPOSAL_SAMPLES
    assert _resume_span(journal).detail == "dropped n1 (no node hash)"
    records = read_records(journal)
    assert [record.node_hash == "" for record in records] == [True, False]
    assert result.proofs["n1"] == records[1].record_hash


def _tree_now(root: Path) -> str:
    """The tracked tree as git ids it, computed the way a resume does."""
    assert run_argv(["git", "add", "-u", "--", "."], root) == 0
    return run_capture(["git", "write-tree"], root).stdout.strip()


def test_run_slice_resume_tree_hash_seals_the_worktree_it_proved(tmp_path: Path) -> None:
    """Known-good (T3-10): the sealed `tree_hash` is the tree the gate
    passed on, so the worktree the proof speaks for is nameable."""
    journal, _first = _first_run(tmp_path)
    record = read_records(journal)[-1]
    assert re.fullmatch(r"[0-9a-f]{40}", record.tree_hash)
    assert record.tree_hash == _tree_now(tmp_path)
    ref = run_capture(["git", "rev-parse", "refs/saddle/proven/n1^{tree}"], tmp_path)
    assert ref.stdout.strip() == record.tree_hash


def test_run_slice_resume_tree_check_passes_on_the_proven_worktree(tmp_path: Path) -> None:
    """Known-good (T3-10): an untouched worktree still hashes to the tree
    the proof was sealed against, so the resume proceeds as before."""
    journal, first = _first_run(tmp_path)
    dag = Dag.model_validate({"nodes": [_node_dict("n1", [])]})

    def propose(node: Node, failure: str | None, seed: int = 0) -> DiffProposal:
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


def test_run_slice_resume_tree_check_passes_after_the_proven_edits_are_committed(
    tmp_path: Path,
) -> None:
    """Known-good (T3-10): the CLI flow after a crash. `_ensure_clean`
    refuses the dirty tree, the user commits the proven edits, and the
    resume still matches -- a commit names the tree, it does not change
    it."""
    journal, first = _first_run(tmp_path)
    assert run_argv(["git", "add", "-u", "--", "."], tmp_path) == 0
    assert run_argv(["git", "commit", "-m", "proven"], tmp_path) == 0
    assert run_argv(["git", "diff-index", "--quiet", "HEAD", "--"], tmp_path) == 0
    dag = Dag.model_validate({"nodes": [_node_dict("n1", [])]})

    def propose(node: Node, failure: str | None, seed: int = 0) -> DiffProposal:
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


def test_run_slice_resume_tree_refuses_a_worktree_missing_the_proven_edit(
    tmp_path: Path,
) -> None:
    """Known-bad (T3-10): `git checkout HEAD -- n.py` throws away what `n1`
    proved. The resume used to seed `n1` as proven and gate the next node
    against a tree without its change; it now stops before any node runs,
    naming both trees and the way back."""
    journal, _first = _first_run(tmp_path)
    proven = read_records(journal)[-1].tree_hash
    # `HEAD --`, not `--`: a proven edit is staged (`_run_node` never
    # commits), so a bare `git checkout -- n.py` restores it from the index
    # and loses nothing. Discarding it takes the index too, which is what
    # `git reset --hard` and `git checkout HEAD -- .` do and what the user
    # reaches for when `_ensure_clean` refuses a crashed run's tree.
    assert run_argv(["git", "checkout", "HEAD", "--", "n.py"], tmp_path) == 0
    assert (tmp_path / "n.py").read_text() == "def f():\n    return 1\n"
    dag = Dag.model_validate({"nodes": [_node_dict("n1", [])]})

    def propose(node: Node, failure: str | None, seed: int = 0) -> DiffProposal:
        msg = "nothing should be proposed"
        raise AssertionError(msg)

    with pytest.raises(ValueError, match=r"worktree does not match") as caught:
        run_slice(
            "Fix f.",
            dag,
            workdir=tmp_path,
            journal_path=journal,
            propose=propose,
            now=lambda: "2026-09-16T00:00:00+00:00",
        )
    message = str(caught.value)
    assert proven in message
    assert _tree_now(tmp_path) in message
    assert "git restore --source refs/saddle/proven/n1 --staged --worktree -- ." in message
    assert [span.name for span in read_spans(journal) if span.name == "resume"] == []


def test_run_slice_resume_tree_refuses_an_edit_to_an_unrelated_file(tmp_path: Path) -> None:
    """Known-bad (T3-10): strict equality. The proof is about one tree,
    not about `n1`'s files alone, so an edit anywhere in the tracked tree
    ends the resume; a fresh `--journal` is the escape hatch."""
    journal, _first = _first_run(tmp_path)
    (tmp_path / "test_n.py").write_text(
        "from n import f\n\n\ndef test_f():  # REQ-001\n    assert f() == 2\n\n\n# unrelated\n"
    )
    dag = Dag.model_validate({"nodes": [_node_dict("n1", [])]})

    def propose(node: Node, failure: str | None, seed: int = 0) -> DiffProposal:
        msg = "nothing should be proposed"
        raise AssertionError(msg)

    with pytest.raises(ValueError, match=r"worktree does not match"):
        run_slice(
            "Fix f.",
            dag,
            workdir=tmp_path,
            journal_path=journal,
            propose=propose,
            now=lambda: "2026-09-16T00:00:00+00:00",
        )


def test_run_slice_refuses_a_journal_that_does_not_verify(tmp_path: Path) -> None:
    """Known-bad (T3-1): a tampered record breaks its hash, and resuming
    onto it raises before any node runs."""
    journal, _first = _first_run(tmp_path)
    text = journal.read_text()
    assert text.count("return two instead") == 1
    journal.write_text(text.replace("return two instead", "return three instead"))
    dag = Dag.model_validate({"nodes": [_node_dict("n1", [])]})

    def propose(node: Node, failure: str | None, seed: int = 0) -> DiffProposal:
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
            propose=lambda node, failure, seed: DiffProposal(GOOD_DIFF, ""),
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
            propose=lambda node, failure, seed: DiffProposal(GOOD_DIFF, ""),
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


# The baseline `fees.py` of the round-3e T5 seed and the hunk one draw
# wrote against it: sample 0 of node n2's first attempt, kept whole in
# its sidecar. Its old side is the file with one blank line moved and
# one dropped (F21.16 §5); every `git apply` rung refuses it.
ROUND_3E_FEES: Final = '''"""Flat USD fee schedule.

Every transaction pays the same flat fee in US dollars. The fee is
deducted from the transaction amount to produce the net credited
amount; batch callers can price many transactions up front with
total_fees.
"""

from __future__ import annotations

FLAT_FEE = 0.30


def fee_for(amount):
    """Return the flat USD fee charged on a transaction amount.

    The fee does not depend on the amount; the argument exists so
    callers can validate the amount against the fee schedule.
    Amounts below the fee cannot cover it and are rejected.
    """
    value = round(float(amount), 2)
    if value < FLAT_FEE:
        raise ValueError(
            "amount %.2f is below the flat fee %.2f" % (value, FLAT_FEE)
        )
    return FLAT_FEE


def apply_fee(amount):
    """Deduct the flat USD fee; returns the net amount."""
    net = round(float(amount), 2) - fee_for(amount)
    return round(net, 2)


def total_fees(count):
    """Return the combined flat USD fees for count transactions."""
    if isinstance(count, bool) or not isinstance(count, int):
        raise TypeError("count must be an int")
    if count < 0:
        raise ValueError("count must be >= 0")
    return round(FLAT_FEE * count, 2)
'''
ROUND_3E_FEES_DIFF: Final = '''diff --git a/fees.py b/fees.py
--- a/fees.py
+++ b/fees.py
@@ -1,42 +1,56 @@
-"""Flat USD fee schedule.
-
-Every transaction pays the same flat fee in US dollars. The fee is
-deducted from the transaction amount to produce the net credited
-amount; batch callers can price many transactions up front with
-total_fees.
+"""Multi-currency fee schedule.
+
+Per-currency flat fees. The fee is deducted from the transaction amount
+to produce the net credited amount; batch callers can price many
+transactions up front with total_fees.
 """
 
 from __future__ import annotations
 
+from decimal import Decimal
+
+from money import quantize, to_decimal, validate_currency
+
+FEES = {
+    "USD": Decimal("0.30"),
+    "EUR": Decimal("0.25"),
+    "JPY": Decimal("30"),
+}
 
-FLAT_FEE = 0.30
 
-def fee_for(amount):
-    """Return the flat USD fee charged on a transaction amount.
+def fee_for(amount, currency="USD"):
+    """Return the fee for the given currency.
 
-    The fee does not depend on the amount; the argument exists so
-    callers can validate the amount against the fee schedule.
-    Amounts below the fee cannot cover it and are rejected.
+    Validates the currency and amount type; returns the per-currency
+    fee as a quantized Decimal.
     """
-    value = round(float(amount), 2)
-    if value < FLAT_FEE:
-        raise ValueError(
-            "amount %.2f is below the flat fee %.2f" % (value, FLAT_FEE)
-        )
-    return FLAT_FEE
+    validate_currency(currency)
+    quantize(to_decimal(amount), currency)
+    return FEES[currency]
 
 
-def apply_fee(amount):
-    """Deduct the flat USD fee; returns the net amount."""
-    net = round(float(amount), 2) - fee_for(amount)
-    return round(net, 2)
+def apply_fee(amount, currency="USD"):
+    """Deduct the fee; returns the net amount as a quantized Decimal.
 
-def total_fees(count):
-    """Return the combined flat USD fees for count transactions."""
+    Raises ValueError when the quantized amount is below the fee.
+    """
+    validate_currency(currency)
+    value = quantize(to_decimal(amount), currency)
+    fee = FEES[currency]
+    if value < fee:
+        raise ValueError(
+            "amount %s is below the fee %s for %s" % (value, fee, currency)
+        )
+    net = value - fee
+    return quantize(net, currency)
+
+
+def total_fees(count, currency="USD"):
+    """Return the combined fees for count transactions."""
+    validate_currency(currency)
     if isinstance(count, bool) or not isinstance(count, int):
         raise TypeError("count must be an int")
     if count < 0:
         raise ValueError("count must be >= 0")
-    return round(FLAT_FEE * count, 2)
+    return quantize(FEES[currency] * count, currency)
'''


def _git_repo_holding(root: Path, name: str, text: str) -> None:
    for argv in (
        ["git", "init"],
        ["git", "config", "user.email", "test@example.com"],
        ["git", "config", "user.name", "test"],
    ):
        assert run_argv(argv, root) == 0
    (root / name).write_text(text)
    assert run_argv(["git", "add", name], root) == 0
    assert run_argv(["git", "commit", "-m", "base"], root) == 0


def test_apply_diff_reanchors_blank_line_drift_no_flag_reaches(tmp_path: Path) -> None:
    """A hunk whose only error is where its blank lines fall applies (T6-38).

    `--ignore-whitespace` ignores whitespace within a line; a blank line
    the file has and the hunk lacks is a line-level insertion, and this
    was every `did not apply` loss in rounds 3d and 3e (F21.16 §5). The
    rung re-derives the blank context from the file and applies strictly,
    so the result's non-blank lines are exactly the hunk's new side.
    """
    _git_repo_holding(tmp_path, "fees.py", ROUND_3E_FEES)
    assert _apply_diff(tmp_path, ROUND_3E_FEES_DIFF) == "blank-lines"
    text = (tmp_path / "fees.py").read_text()
    new_side = [line[1:] for line in ROUND_3E_FEES_DIFF.splitlines()[4:] if line[:1] in (" ", "+")]
    assert [line for line in text.splitlines() if line.strip()] == [
        line for line in new_side if line.strip()
    ]
    assert "FLAT_FEE" not in text
    assert run_argv(["git", "diff", "--cached", "--quiet", "--", "fees.py"], tmp_path) == 1


@pytest.mark.parametrize(
    ("old", "new"),
    [
        ("-    return round(FLAT_FEE * count, 2)", "-    return round(FLAT_FEE * count, 3)"),
        ("     if count < 0:", "     if count <= 0:"),
    ],
    ids=["deleted line", "context line"],
)
def test_apply_diff_blank_line_rung_refuses_a_code_line_the_file_lacks(
    tmp_path: Path, old: str, new: str
) -> None:
    """Blank-line tolerance is not code tolerance.

    The same hunk with one old code line altered -- a deletion or a
    context line -- is refused without the rung running git at all (the
    failure still names `three-way`), and the tree is untouched: a
    drifted `return` is a wrong edit, not drift.
    """
    assert ROUND_3E_FEES_DIFF.count(old + "\n") == 1
    _git_repo_holding(tmp_path, "fees.py", ROUND_3E_FEES)
    wrong = ROUND_3E_FEES_DIFF.replace(old + "\n", new + "\n")
    with pytest.raises(RuntimeError, match=r"\(last rung three-way: "):
        _apply_diff(tmp_path, wrong)
    assert (tmp_path / "fees.py").read_text() == ROUND_3E_FEES


def test_apply_diff_blank_line_rung_refuses_a_hunk_anchored_on_nothing(tmp_path: Path) -> None:
    """A hunk whose old side is blank lines only has nothing to anchor on."""
    _git_repo_holding(tmp_path, "d.py", "x = 1\ny = 2\n")
    diff = "diff --git a/d.py b/d.py\n--- a/d.py\n+++ b/d.py\n@@ -1,1 +1,2 @@\n \n+z = 3\n"
    with pytest.raises(RuntimeError, match=r"\(last rung three-way: "):
        _apply_diff(tmp_path, diff)
    assert (tmp_path / "d.py").read_text() == "x = 1\ny = 2\n"


def test_apply_diff_blank_line_rung_passes_a_missing_file_through_to_git(tmp_path: Path) -> None:
    """A section for a file the tree lacks is not re-anchored; git then
    refuses the whole diff, and the failure names this rung."""
    _git_repo_holding(tmp_path, "fees.py", ROUND_3E_FEES)
    diff = (
        ROUND_3E_FEES_DIFF
        + "diff --git a/q.py b/q.py\n--- a/q.py\n+++ b/q.py\n@@ -1 +1 @@\n-a\n+b\n"
    )
    with pytest.raises(RuntimeError, match=r"\(last rung blank-lines: error: q\.py: "):
        _apply_diff(tmp_path, diff)
    assert (tmp_path / "fees.py").read_text() == ROUND_3E_FEES


def test_apply_diff_blank_line_rung_refuses_an_anchor_the_file_has_twice(tmp_path: Path) -> None:
    """A hunk whose old lines occur twice cannot say where it goes."""
    _git_repo_holding(tmp_path, "d.py", "a = 1\n\nb = 2\n\n\na = 1\n\nb = 2\n")
    diff = "diff --git a/d.py b/d.py\n--- a/d.py\n+++ b/d.py\n@@ -1,2 +1,3 @@\n"
    diff += " a = 1\n+c = 3\n b = 2\n"
    with pytest.raises(RuntimeError, match=r"\(last rung three-way: "):
        _apply_diff(tmp_path, diff)
    assert (tmp_path / "d.py").read_text() == "a = 1\n\nb = 2\n\n\na = 1\n\nb = 2\n"


@pytest.mark.parametrize(
    ("text", "body", "expected"),
    [
        ("x = 1\n\ny = 2\n", " x = 1\n+z = 3\n y = 2\n", "x = 1\nz = 3\n\ny = 2\n"),
        ("x = 1\ny = 2\n", " x = 1\n \n+z = 3\n y = 2\n", "x = 1\nz = 3\ny = 2\n"),
        (
            "x = 1\n\ny = 2\n\nw = 4\n",
            " x = 1\n \n+z = 3\n y = 2\n-w = 4\n+w = 5\n",
            "x = 1\n\nz = 3\ny = 2\n\nw = 5\n",
        ),
    ],
    ids=["file has the blank", "hunk has the blank", "two edits, drift between them"],
)
def test_apply_diff_blank_line_rung_keeps_additions_where_the_hunk_put_them(
    tmp_path: Path, text: str, body: str, expected: str
) -> None:
    """The file's blank lines are kept, the hunk's spurious ones dropped,
    and an addition stays next to the old line it followed."""
    _git_repo_holding(tmp_path, "d.py", text)
    diff = "diff --git a/d.py b/d.py\n--- a/d.py\n+++ b/d.py\n@@ -1,2 +1,3 @@\n" + body
    assert _apply_diff(tmp_path, diff) == "blank-lines"
    assert (tmp_path / "d.py").read_text() == expected


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

    def propose(node: Node, failure: str | None, seed: int = 0) -> DiffProposal:
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

M_DIFF = whole_file("m.py", "def g():", "    return 2")

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
    "+\n"
    "+\n"
    "+@given(st.integers())\n"
    "+def test_f_is_never_three(_value):  # REQ-001\n"
    "+    assert (f() == 3) is False\n"
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
        {"id": "REQ-001", "statement": "REQ-001 holds.", "accepts": ["2"], "rejects": ["3"]},
        {"id": "REQ-002", "statement": "REQ-002 holds.", "accepts": ["2"], "rejects": ["3"]},
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
    `coverage` (no test runs `n.py:2`) and `red-phase` on work that was
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

    def propose(node: Node, failure: str | None, seed: int = 0) -> DiffProposal:
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
    # k draws per node, paid up front (T6-25); the first sample of each
    # gated clean, so nothing after it was evaluated and no retry ran.
    # Against `HEAD` the first sample would score `target-scope` red.
    assert calls == ["n1"] * PROPOSAL_SAMPLES + ["n2"] * PROPOSAL_SAMPLES
    assert "- Gate target-scope: PASS (1 touched file(s) within 1 target(s))\n" in result.transcript
    assert "- Gate coverage: PASS (every changed line runs)\n" in result.transcript
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
    assert refs.stdout.split() == [
        # T6-34: the tree each attempt was graded on, numbered by attempt.
        # Both nodes passed on attempt 1, so there is one apiece.
        "refs/saddle/attempt/n1/1",
        "refs/saddle/attempt/n2/1",
        "refs/saddle/baseline/n1",
        "refs/saddle/baseline/n2",
        # T3-10: one proven tree per sealed node, beside its baseline.
        "refs/saddle/proven/n1",
        "refs/saddle/proven/n2",
    ]


# `n2`'s own stray: the same `m.py` edit plus a comment on `n1`'s file.
STRAY_DIFF = M_DIFF + whole_file("n.py", "def f():", "    return 2", "# stray")


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

    def propose(node: Node, failure: str | None, seed: int = 0) -> DiffProposal:
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

    def propose(node: Node, failure: str | None, seed: int = 0) -> DiffProposal:
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
    assert calls == ["t1"] * PROPOSAL_SAMPLES + ["n1"] * PROPOSAL_SAMPLES
    assert "- Gate node-scope: PASS (test node changed 1 file(s) in scope)\n" in result.transcript
    assert "- Gate node-scope: PASS (impl node changed 1 file(s) in scope)\n" in result.transcript
    assert "- Gate tests: PASS (red specification: 1 failing test(s))\n" in result.transcript
    run = [span for span in read_spans(journal) if span.name == "run"][-1]
    assert run.detail == "2 proven, 0 failed, 0 undispatched, merge exit 0"
    # The property `t1` specified bites on `n1` (T3-3): its record says so.
    outputs = {o.name: o for o in read_records(journal)[1].gate_outputs}
    assert outputs["property-coverage"].basis == "oracle: killed 5 of 5 mutant(s) by test_n.py"


# --- T3-17: the merge suite runs the way the node gates run tests ---------

PKG_DIFF = whole_file("pkg/m.py", "def g():", "    return 2")


def _packaged_repo(root: Path) -> None:
    """A top-level package imported by tests in `tests/`, no conftest and no
    packaging: the layout the smoke run's planner produced. `python -m
    pytest` puts cwd on `sys.path` and imports `pkg`; bare `pytest` does not."""
    for argv in (
        ["git", "init"],
        ["git", "config", "user.email", "test@example.com"],
        ["git", "config", "user.name", "test"],
    ):
        assert run_argv(argv, root) == 0
    (root / "pkg").mkdir()
    (root / "pkg" / "__init__.py").write_text("")
    (root / "pkg" / "m.py").write_text("def g():\n    return 1\n")
    (root / "tests").mkdir()
    (root / "tests" / "test_m.py").write_text(
        "from pkg.m import g\n\n\ndef test_g():  # REQ-001\n    assert g() == 2\n"
    )
    assert run_argv(["git", "add", "-A"], root) == 0
    assert run_argv(["git", "commit", "-m", "baseline"], root) == 0


def _packaged_slice(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, **kwargs: object
) -> SliceResult:
    _packaged_repo(tmp_path)
    _mutmut_stub(
        tmp_path / "stub",
        monkeypatch,
        {
            f"p{index}": ("pkg/m.py", "    return 2")
            for index in range(1, 1 + MIN_SIGNIFICANT_MUTANTS)
        },
    )
    node = _node_dict("n1", [])
    node["target_files"] = ["pkg/m.py"]
    gate = node["deterministic_gate"]
    assert isinstance(gate, dict)
    gate["test_command"] = "pytest tests/test_m.py"
    dag = Dag.model_validate({"nodes": [node]})
    return run_slice(
        "Fix g.",
        dag,
        workdir=tmp_path,
        journal_path=tmp_path / "proofs.jsonl",
        propose=lambda node, failure, seed: DiffProposal(PKG_DIFF, ""),
        now=lambda: "2026-09-16T00:00:00+00:00",
        **kwargs,  # type: ignore[arg-type]
    )


def test_run_slice_merge_suite_runs_as_the_node_gates_do(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """T3-17 known-good: the node proves under `coverage run -m pytest` and
    the merge suite, under the same interpreter form, imports the same
    package and passes."""
    result = _packaged_slice(tmp_path, monkeypatch)
    assert result.passed is True, result.transcript
    run = [span for span in read_spans(tmp_path / "proofs.jsonl") if span.name == "run"][-1]
    assert run.detail == "1 proven, 0 failed, 0 undispatched, merge exit 0"
    merge = next(s for s in read_spans(tmp_path / "proofs.jsonl") if s.name == "merge-suite")
    assert merge.argv == ["python", "-m", "pytest", "-q"]


def test_run_slice_bare_pytest_merge_cannot_import_what_the_gates_could(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """T3-17 known-bad, kept so the reason for the default stays load-bearing:
    the same proven tree fails a bare `pytest -q` merge with a collection
    error (exit 2) that no node gate could observe."""
    result = _packaged_slice(tmp_path, monkeypatch, merge_command="pytest -q")
    assert list(result.proofs) == ["n1"]
    assert result.passed is False
    run = [span for span in read_spans(tmp_path / "proofs.jsonl") if span.name == "run"][-1]
    assert run.detail == "1 proven, 0 failed, 0 undispatched, merge exit 2"


# --- T3-24: a citation of a requirement another node of the plan declares ---


def test_run_slice_distinct_ids_first_node_sees_the_second_nodes_citation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Known-good (T3-24): the T3-8 two-node fixture with distinct ids.

    `n1` declares `REQ-001` alone and `n2` `REQ-002` alone; the committed
    `test_m.py` cites `REQ-002`. Binding reads every discovered test
    source, so `n1`'s gate sees `REQ-002` and, with the orphan set drawn
    from the node's own ids only, fails with "undeclared requirements
    cited: REQ-002" for an id the plan itself declares one node later.
    An id declared anywhere in the plan is planned, not hallucinated.
    """
    _two_module_repo(tmp_path)
    _two_module_mutmut(tmp_path / "stub", monkeypatch)
    second = _node_dict("n2", ["n1"])
    second["requirements"] = [
        {"id": "REQ-002", "statement": "REQ-002 holds.", "accepts": ["2"], "rejects": ["3"]}
    ]
    second["target_files"] = ["m.py"]
    gate = second["deterministic_gate"]
    assert isinstance(gate, dict)
    gate["test_command"] = "pytest test_m.py"
    dag = Dag.model_validate({"nodes": [_node_dict("n1", []), second]})
    journal = tmp_path / "proofs.jsonl"

    def propose(node: Node, failure: str | None, seed: int = 0) -> DiffProposal:
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
    bound = "- Gate requirement-binding: PASS (1 requirement(s) bound)\n"
    assert result.transcript.count(bound) == 2
    run = [span for span in read_spans(journal) if span.name == "run"][-1]
    assert run.detail == "2 proven, 0 failed, 0 undispatched, merge exit 0"


# `SPEC_DIFF` with the property tagged for the impl node's requirement: the
# specification a `test` node writes cites the ids of the nodes that will
# make it pass (session 20b's plan, where the impl node declared `REQ-002`).
SPEC_DIFF_TWO_IDS = SPEC_DIFF.replace(
    "+def test_f_is_an_int(_value):  # REQ-001\n", "+def test_f_is_an_int(_value):  # REQ-002\n"
)


def test_run_slice_test_node_may_cite_the_id_its_dependent_impl_node_declares(
    tmp_path: Path,
) -> None:
    """Session 20b in miniature (T3-24): the `test` node `t1` declares
    `REQ-001` and writes a specification citing `REQ-001` and `REQ-002`; the
    `impl` node `n1` that depends on it declares `REQ-002`. With the orphan
    set drawn from each node's own ids, `t1` fails "undeclared requirements
    cited: REQ-002" and no test-first split with distinct ids can prove; drawn
    from the dependencies' ids only, `t1` still fails, because the id it cites
    is declared downstream. The unbound half is per node either way: each
    node's own ids must be cited.
    """
    _spec_slice_repo(tmp_path)
    spec = _node_dict("t1", [])
    spec["kind"] = "test"
    impl = _node_dict("n1", ["t1"])
    impl["requirements"] = [
        {"id": "REQ-002", "statement": "REQ-002 holds.", "accepts": ["2"], "rejects": ["3"]}
    ]
    dag = Dag.model_validate({"nodes": [spec, impl]})
    journal = tmp_path / "proofs.jsonl"

    def propose(node: Node, failure: str | None, seed: int = 0) -> DiffProposal:
        return DiffProposal(SPEC_DIFF_TWO_IDS if node.id == "t1" else GOOD_DIFF, "")

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
    bound = "- Gate requirement-binding: PASS (1 requirement(s) bound"
    assert result.transcript.count(bound) == 2
    # The spec node's line also counts the examples it asserted on (T6-4).
    assert result.transcript.count(bound + ", 2 example(s) asserted)\n") == 1
    run = [span for span in read_spans(journal) if span.name == "run"][-1]
    assert run.detail == "2 proven, 0 failed, 0 undispatched, merge exit 0"


def _sidecar(journal: Path, span: SpanRecord) -> dict[str, Any]:
    path = attempt_sidecar_path(journal, span.span_id)
    assert hashlib.sha256(path.read_bytes()).hexdigest() == span.attempt_hash
    loaded: dict[str, Any] = json.loads(path.read_bytes())
    return loaded


def test_run_slice_seals_the_plan_before_the_first_node_and_each_replan(tmp_path: Path) -> None:
    """Known-good (T6-13): the journal's first entry is the plan, sealed
    before any worker call; a replan seals its replacement naming the
    failed node; `verify` is clean and the proofs match the plans."""
    _slice_repo(tmp_path)
    dag = Dag.model_validate({"nodes": [_node_dict("n1", [])]})
    journal = tmp_path / "proofs.jsonl"

    def propose(node: Node, failure: str | None, seed: int = 0) -> DiffProposal:
        return DiffProposal(BAD_DIFF if node.id == "n1" else GOOD_DIFF, "")

    def replan(node: Node, history: str, reserved: Sequence[str] = ()) -> Dag:
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
    assert result.passed is True, result.transcript
    entries = read_entries(journal)
    first = entries[0]
    assert isinstance(first, PlanRecord)
    assert [n.id for n in first.nodes] == ["n1"]
    assert first.nodes[0].node_hash == hash_node(dag.nodes[0])
    assert first.task_hash == hashlib.sha256(b"Fix f.").hexdigest()
    plans = read_plans(journal)
    # A replan record carries the whole post-replan plan, not just the
    # replacement: the failed node stays put and every rewired dependent
    # has a new hash to plan (T6-72).
    assert [(p.replaces, [n.id for n in p.nodes]) for p in plans] == [
        ("", ["n1"]),
        ("n1", ["n1", "n1.r1"]),
    ]
    assert verify_journal(journal) == []
    (record,) = read_records(journal)
    replacement = {node.id: node for node in plans[1].nodes}["n1.r1"]
    assert record.node_hash == replacement.node_hash


def test_run_slice_every_attempt_leaves_a_sidecar_with_its_evidence(tmp_path: Path) -> None:
    """Known-good (T6-12): a failed attempt keeps its reasoning, gate
    outcomes, samples and diff hash beside the journal, hashed into its
    span; the sealing attempt keeps the same. Known-bad was round-3 T5:
    three failed attempts, 160k tokens, nothing journaled."""
    _slice_repo(tmp_path)
    dag = Dag.model_validate({"nodes": [_node_dict("n1", [])]})
    journal = tmp_path / "proofs.jsonl"
    calls: list[str | None] = []

    def propose(node: Node, failure: str | None, seed: int = 0) -> DiffProposal:
        calls.append(failure)
        if failure is None:
            return DiffProposal(
                BAD_DIFF, "first thought", usage={"completion_tokens": 10}, max_tokens=500
            )
        return DiffProposal(
            FIX_DIFF, "second thought", usage={"completion_tokens": 20}, max_tokens=600
        )

    result = run_slice(
        "Fix f.",
        dag,
        workdir=tmp_path,
        journal_path=journal,
        propose=propose,
        now=lambda: "2026-09-16T00:00:00+00:00",
    )
    assert result.passed is True, result.transcript
    agents = [s for s in read_spans(journal) if s.kind == "agent" and s.name == "worker:n1"]
    assert [s.exit_code for s in agents] == [1, 0]
    failed = _sidecar(journal, agents[0])
    assert failed["thinking"] == "first thought"
    assert failed["finish_reason"] == "stop"
    assert failed["usage"] == {"completion_tokens": 10}
    assert failed["max_tokens"] == 500
    assert failed["diff_hash"] == hashlib.sha256(BAD_DIFF.encode()).hexdigest()
    assert [g["name"] for g in failed["gates"] if not g["passed"]] == [
        "tests",
        "red-phase",
        "mutation",
    ]
    assert len(failed["samples"]) == PROPOSAL_SAMPLES
    assert failed["samples"][0]["outcome"] == "3 gate(s) failed"
    assert failed["samples"][1]["outcome"] == "identical to an earlier sample"
    sealed = _sidecar(journal, agents[1])
    assert sealed["thinking"] == "second thought"
    assert sealed["max_tokens"] == 600
    assert verify_journal(journal) == []
    # The sidecar is load-bearing: editing it fails verification.
    path = attempt_sidecar_path(journal, agents[0].span_id)
    path.write_bytes(path.read_bytes().replace(b"first thought", b"first  thought"))
    assert [i.code for i in verify_journal(journal)] == ["attempt-sidecar"]


def test_run_slice_truncated_attempt_keeps_its_partial_reasoning(tmp_path: Path) -> None:
    """Known-bad shape (T6-12, F21.4): the most expensive failure used to
    leave nothing. A truncated call's sidecar carries the finish reason,
    the cap, the usage and the partial reasoning; the transcript line names
    the cap."""
    _slice_repo(tmp_path)
    dag = Dag.model_validate({"nodes": [_node_dict("n1", [])]})
    journal = tmp_path / "proofs.jsonl"
    attempts: list[str | None] = []

    def propose(node: Node, failure: str | None, seed: int = 0) -> DiffProposal:
        attempts.append(failure)
        if len(attempts) <= PROPOSAL_SAMPLES + 1:
            msg = "completion truncated at 4096 output tokens (finish_reason=length)"
            raise VllmResponseError(
                msg,
                reasoning="I was thinking about",
                content="diff --git a/n.py",
                usage={"completion_tokens": 4096, "reasoning_tokens": 4000},
                max_tokens=4096,
                finish_reason="length",
            )
        return DiffProposal(GOOD_DIFF, "done", max_tokens=8192)

    result = run_slice(
        "Fix f.",
        dag,
        workdir=tmp_path,
        journal_path=journal,
        propose=propose,
        now=lambda: "2026-09-16T00:00:00+00:00",
    )
    assert result.passed is True, result.transcript
    agents = [s for s in read_spans(journal) if s.kind == "agent" and s.name == "worker:n1"]
    assert agents[0].exit_code == 1
    assert "truncated at 4096 output tokens" in agents[0].detail
    truncated = _sidecar(journal, agents[0])
    assert truncated["finish_reason"] == "length"
    assert truncated["thinking"] == "I was thinking about"
    assert truncated["partial_content_chars"] == len("diff --git a/n.py")
    assert truncated["usage"] == {"completion_tokens": 4096, "reasoning_tokens": 4000}
    assert truncated["max_tokens"] == 4096
    assert all(s["finish_reason"] == "length" for s in truncated["samples"])
    assert verify_journal(journal) == []


def test_every_attempt_sidecar_carries_the_call_and_the_diff(tmp_path: Path) -> None:
    """T6-27 known-good, end to end. Attempt 1 fails its gate, attempt 2
    seals. Both sidecars retain the diff text, the prompt, the seed, the
    temperature, the start time and the wall of the call that drew them,
    beside the hashes; both worker spans carry an argv that names the
    attempt and the prompt's hash, so their `args_hash` differ; every
    span, tool or agent, has a `started_at`. Known-bad, the pre-T6-27
    shape: an empty argv hashing to sha256("[]") and no absolute time.
    """
    _slice_repo(tmp_path)
    dag = Dag.model_validate({"nodes": [_node_dict("n1", [])]})
    journal = tmp_path / "proofs.jsonl"

    def propose(node: Node, failure: str | None, seed: int = 0) -> DiffProposal:
        prompt = f"fix f ({'retry' if failure else 'first'})"
        return DiffProposal(
            BAD_DIFF if failure is None else FIX_DIFF,
            "thought",
            prompt=prompt,
            seed=seed,
            temperature=0.7,
            started_at="2026-09-20T17:30:00+00:00",
            wall_s=12.5,
        )

    result = run_slice(
        "Fix f.",
        dag,
        workdir=tmp_path,
        journal_path=journal,
        propose=propose,
        now=lambda: "2026-09-16T00:00:00+00:00",
        settings={"model": "qwen3.8-27b", "server": "0.28.0"},
    )
    assert result.passed is True, result.transcript
    spans = read_spans(journal)
    assert all(s.started_at for s in spans), [s.name for s in spans if not s.started_at]
    workers = [s for s in spans if s.name == "worker:n1"]
    assert [s.argv[:3] for s in workers] == [
        ["worker", "n1", "attempt=1"],
        ["worker", "n1", "attempt=2"],
    ]
    empty = hashlib.sha256(b"[]").hexdigest()
    assert empty not in {s.args_hash for s in workers}
    assert len({s.args_hash for s in workers}) == 2
    first, second = (_sidecar(journal, s) for s in workers)
    assert first["attempt"] == 1
    assert first["diff_hash"] == hashlib.sha256(BAD_DIFF.encode()).hexdigest()
    assert first["samples"][0]["diff"] == BAD_DIFF
    assert first["samples"][0]["seed"] == 0
    assert first["samples"][0]["prompt"] == "fix f (first)"
    assert workers[0].argv[3] == "prompt_sha256=" + hashlib.sha256(b"fix f (first)").hexdigest()
    assert second["diff"] == FIX_DIFF
    assert (second["seed"], second["temperature"], second["wall_s"]) == (
        PROPOSAL_SAMPLES,
        0.7,
        12.5,
    )
    assert second["started_at"] == "2026-09-20T17:30:00+00:00"
    run = next(s for s in spans if s.name == "run")
    assert run.argv == ["run", "model=qwen3.8-27b", "server=0.28.0"]
    assert run.started_at == "2026-09-16T00:00:00+00:00"
    assert verify_journal(journal) == []


def test_a_recorded_attempt_replays_to_the_same_diff_hash(tmp_path: Path) -> None:
    """T6-27 known-good for the contract's purpose: a fake client keyed on
    (prompt, seed, temperature) handed the sidecar's own fields reproduces
    the sealed diff hash. Known-bad: with the seed withheld the replay is a
    different draw.
    """
    _slice_repo(tmp_path)
    dag = Dag.model_validate({"nodes": [_node_dict("n1", [])]})
    journal = tmp_path / "proofs.jsonl"
    by_seed = {0: GOOD_DIFF, 1: SLOPPY_DIFF, 2: GOOD_DIFF}

    def model(prompt: str, seed: int, temperature: float) -> str:
        return by_seed[seed] if prompt.startswith("fix") and temperature == 0.7 else BAD_DIFF

    def propose(node: Node, failure: str | None, seed: int = 0) -> DiffProposal:
        return DiffProposal(
            model("fix f", seed, 0.7), "", prompt="fix f", seed=seed, temperature=0.7
        )

    assert run_slice(
        "Fix f.",
        dag,
        workdir=tmp_path,
        journal_path=journal,
        propose=propose,
        now=lambda: "2026-09-16T00:00:00+00:00",
    ).passed
    sealed = _sidecar(journal, next(s for s in read_spans(journal) if s.name == "worker:n1"))
    replayed = model(sealed["prompt"], sealed["seed"], sealed["temperature"])
    assert hashlib.sha256(replayed.encode()).hexdigest() == sealed["diff_hash"]
    other = model(sealed["prompt"], 1, sealed["temperature"])
    assert hashlib.sha256(other.encode()).hexdigest() != sealed["diff_hash"]


def test_a_recorded_attempt_names_the_effort_it_ran_at(tmp_path: Path) -> None:
    """T6-47 known-good: a replay keyed on the sidecar's own effort draws the
    arm the attempt drew; keyed on a guess, it draws a different one.

    Known-bad, and it is on the record (F21.18). The sidecar had no such
    field, so round 3e's grammar cell had to guess the effort. It guessed
    `xhigh` against an attempt that ran at `low`, billed 10 627 prompt
    tokens against the attempt's 10 615, and neither arm reproduced the
    draw -- so the cell was not a control for the thing it replayed. The
    recorded effort here is `low` precisely because it is not
    `DEFAULT_REASONING_EFFORT`: a field pinned to the default would pass a
    test that only asked whether a value was present.
    """
    _slice_repo(tmp_path)
    dag = Dag.model_validate({"nodes": [_node_dict("n1", [])]})
    journal = tmp_path / "proofs.jsonl"

    def model(effort: str) -> str:
        return GOOD_DIFF if effort == "low" else BAD_DIFF

    def propose(node: Node, failure: str | None, seed: int = 0) -> DiffProposal:
        return DiffProposal(
            model("low"),
            "",
            prompt="fix f",
            seed=seed,
            temperature=0.7,
            reasoning_effort="low",
        )

    assert run_slice(
        "Fix f.",
        dag,
        workdir=tmp_path,
        journal_path=journal,
        propose=propose,
        now=lambda: "2026-09-16T00:00:00+00:00",
    ).passed
    sealed = _sidecar(journal, next(s for s in read_spans(journal) if s.name == "worker:n1"))
    assert sealed["reasoning_effort"] == "low"
    replayed = model(sealed["reasoning_effort"])
    assert hashlib.sha256(replayed.encode()).hexdigest() == sealed["diff_hash"]
    guessed = model("xhigh")
    assert hashlib.sha256(guessed.encode()).hexdigest() != sealed["diff_hash"]


def test_failed_worker_call_sidecar_keeps_the_call_the_client_attached(tmp_path: Path) -> None:
    """T6-27: a transport failure's sidecar carries prompt, seed, temperature
    and start from the error's `evidence`, and the span's argv names the
    prompt's hash, so the 2494 s timeout of round 3d would not be blank."""
    _slice_repo(tmp_path)
    node = Node.model_validate(_node_dict("n1", []))
    journal = tmp_path / "proofs.jsonl"

    def propose(node: Node, failure: str | None, seed: int = 0) -> DiffProposal:
        if failure is None:
            return DiffProposal(BAD_DIFF, "", prompt="p0", seed=seed, temperature=0.7)
        exc = VllmRequestError("request failed: timed out")
        exc.evidence = {
            "prompt": "p1",
            "seed": seed,
            "temperature": 0.7,
            "started_at": "t",
            "wall_s": 1800.0,
        }
        raise exc

    with pytest.raises(VllmRequestError):
        asyncio.run(_run_node(node, tmp_path, journal, propose, {}, "run", (), task_hash=""))
    workers = [s for s in read_spans(journal) if s.name.startswith("worker:")]
    sidecar = _sidecar(journal, workers[-1])
    assert (sidecar["prompt"], sidecar["seed"], sidecar["wall_s"]) == (
        "p1",
        PROPOSAL_SAMPLES,
        1800.0,
    )
    assert workers[-1].argv[3] == "prompt_sha256=" + hashlib.sha256(b"p1").hexdigest()


# --- T6-29c: survivor-driven test node, the splice ---------------------------

BRANCH_DIFF = whole_file(
    "n.py", "def f(flag=0):", "    if flag == 1:", "        return 5", "    return 2"
)
"""`f` grows a branch no test reaches: `test_n.py` covers lines 1, 2 and 4."""

THREE_BRANCH_DIFF = whole_file(
    "n.py",
    "def f(flag=0):",
    "    if flag == 1:",
    "        return 5",
    "    if flag == 2:",
    "        return 7",
    "    if flag == 3:",
    "        return 9",
    "    return 2",
)


def _candidate_diff(source: str, path: str = "tests/test_REQ-001_s0.py") -> str:
    """A creation diff the way the grammar shapes one (T6-32)."""
    body = "".join(f"+{line}\n" for line in source.splitlines())
    count = source.count("\n")
    return (
        f"diff --git a/{path} b/{path}\n"
        "new file mode 100644\n"
        "--- /dev/null\n"
        f"+++ b/{path}\n"
        f"@@ -0,0 +1,{count} @@\n"
        f"{body}"
    )


def _flag_test(flag: int, expect: int) -> str:
    """A candidate pinning one branch, with the property a test node must state (T3-3)."""
    return (
        "from hypothesis import given\n"
        "from hypothesis import strategies as st\n"
        "\n"
        "from n import f\n"
        "\n"
        "\n"
        f"def test_f_flag_{flag}():  # REQ-001\n"
        f"    assert f(flag={flag}) == {expect}\n"
        "\n"
        "\n"
        "@given(st.integers(min_value=4))\n"
        "def test_f_other_flags_return_two(flag):  # REQ-001\n"
        "    assert f(flag=flag) == 2\n"
        "    assert (f(flag=flag) == 3) is False\n"
    )


def _survivor_span(journal: Path, node_id: str) -> SpanRecord:
    (span,) = [
        span
        for span in read_spans(journal)
        if span.name == "survivor-tests" and span.node_id == node_id
    ]
    return span


def test_run_slice_survivor_round_seals_a_test_node_then_the_impl_node(tmp_path: Path) -> None:
    """Known-good (T6-29c): an impl node fails coverage on a branch nothing
    reaches; the recovery draws k test candidates from a brief that names
    the untested function and never the code, keeps the one that is red on
    stubs, green on the real tree and covers the gap, drops the rest with
    a reason each, and the run seals the test node then the retried impl
    node with the kept test in its gate's scope."""
    _slice_repo(tmp_path)
    dag = Dag.model_validate({"nodes": [_node_dict("n1", [])]})
    journal = tmp_path / "proofs.jsonl"
    briefs: list[tuple[str, str, int]] = []
    candidates = {
        0: _candidate_diff(_flag_test(1, 5)),
        1: _candidate_diff("def test_broken(:  # REQ-001\n    pass\n"),
        2: _candidate_diff(_flag_test(1, 6)),
        3: _candidate_diff(_flag_test(1, 5)),
        4: TRUNCATED,
        5: GOOD_DIFF,
        6: _candidate_diff(_flag_test(1, 5).replace("  # REQ-001", "")),
    }

    def draw(node: Node, brief: str, seed: int) -> DiffProposal:
        briefs.append((node.id, brief, seed))
        if seed == 4:
            raise VllmResponseError(TRUNCATED)
        return DiffProposal(candidates[seed], "")

    result = run_slice(
        "Fix f.",
        dag,
        workdir=tmp_path,
        journal_path=journal,
        propose=lambda node, failure, seed: DiffProposal(BRANCH_DIFF, ""),
        survivor_draw=draw,
        survivor_samples=7,
        now=lambda: "2026-09-16T00:00:00+00:00",
    )
    assert result.passed is True, result.transcript
    assert list(result.proofs) == ["n1.r1", "n1.r2"]
    assert sorted(seed for _, _, seed in briefs) == list(range(7))
    assert {node_id for node_id, _, _ in briefs} == {"n1"}
    brief = briefs[0][1]
    assert all(other == brief for _, other, _ in briefs)
    assert "  n.py: f\n" in brief
    assert "  REQ-001: REQ-001 holds.\n" in brief
    assert "one section, one hunk" in brief
    assert "test_REQ-001_r1_s0.py" in brief
    # The implementation's bodies never reach the brief: a worker shown
    # `return 5` writes the test of the code (T6-29b).
    assert "return 5" not in brief
    assert "flag == 1" not in brief
    span = _survivor_span(journal, "n1")
    assert span.argv == ["survivor-tests", "n1", "round=1", "drawn=7", "kept=1", "dropped=6"]
    assert span.exit_code == 0
    assert "s0: kept: test_REQ-001_r1_s0.py killed 0, covered 1 line(s)" in span.detail
    assert "s1: dropped: does not parse" in span.detail
    assert "s2: dropped: test_REQ-001_r1_s2.py exited 1 against the real tree" in span.detail
    assert "s3: dropped: identical to sample 0" in span.detail
    assert f"s4: dropped: worker call failed: {TRUNCATED}" in span.detail
    assert "s5: dropped: not one created file" in span.detail
    assert "s6: dropped: cites no requirement id" in span.detail
    kept = tmp_path / "test_REQ-001_r1_s0.py"
    assert kept.read_text() == _flag_test(1, 5)
    assert not (tmp_path / "test_REQ-001_r1_s2.py").exists()
    plans = read_plans(journal)
    # The whole post-replan plan, failed node included (T6-72).
    assert [(p.replaces, [n.id for n in p.nodes]) for p in plans] == [
        ("", ["n1"]),
        ("n1", ["n1", "n1.r1", "n1.r2"]),
    ]
    generated = [node for node in plans[1].nodes if node.id != "n1"]
    assert [(n.kind, n.target_files) for n in generated] == [
        ("test", ["test_REQ-001_r1_s0.py"]),
        ("impl", []),
    ]
    assert "- Gate tests: PASS (red specification: 2 failing test(s))\n" in result.transcript
    assert (
        "- Gate tests: PASS ('pytest test_n.py test_REQ-001_r1_s0.py' exited 0)\n"
        in result.transcript
    )
    assert "- Gate coverage: PASS (every changed line runs)\n" in result.transcript
    assert verify_journal(journal) == []


def test_run_slice_survivor_recovery_starts_no_third_round(tmp_path: Path) -> None:
    """Known-bad (T6-29c): two rounds per requirement. Each round's kept
    test covers one more branch and the retried impl node still fails;
    after the second retry nothing is drawn and the node stays failed."""
    _slice_repo(tmp_path)
    dag = Dag.model_validate({"nodes": [_node_dict("n1", [])]})
    journal = tmp_path / "proofs.jsonl"
    draws: list[int] = []
    k = 3

    def draw(node: Node, brief: str, seed: int) -> DiffProposal:
        draws.append(seed)
        branch = 1 + (len(draws) - 1) // k
        return DiffProposal(_candidate_diff(_flag_test(branch, {1: 5, 2: 7, 3: 9}[branch])), "")

    result = run_slice(
        "Fix f.",
        dag,
        workdir=tmp_path,
        journal_path=journal,
        propose=lambda node, failure, seed: DiffProposal(THREE_BRANCH_DIFF, ""),
        survivor_draw=draw,
        survivor_samples=k,
        now=lambda: "2026-09-16T00:00:00+00:00",
    )
    assert result.passed is False
    assert len(draws) == 2 * k
    # Round 2 replaces the retried node, so its ids nest under it (T3-11).
    assert list(result.proofs) == ["n1.r1", "n1.r2.r1"]
    assert "## Node n1.r2.r2\n" in result.transcript
    assert "## Node n1.r2.r2.r1\n" not in result.transcript
    assert _survivor_span(journal, "n1").argv[2] == "round=1"
    assert _survivor_span(journal, "n1.r2").argv[2] == "round=2"
    assert (tmp_path / "test_REQ-001_r1_s0.py").exists()
    assert (tmp_path / "test_REQ-001_r2_s0.py").exists()
    assert verify_journal(journal) == []


def test_run_slice_gap_cited_by_a_sealed_test_retries_as_today(tmp_path: Path) -> None:
    """Known-bad (T6-29c): the sealed test node `t1` wrote `test_n.py`,
    which names `f`; `n1`'s gap is inside `f`, so nothing is drawn and the
    node replans exactly as before."""
    _spec_slice_repo(tmp_path)
    spec = _node_dict("t1", [])
    spec["kind"] = "test"
    dag = Dag.model_validate({"nodes": [spec, _node_dict("n1", ["t1"])]})
    journal = tmp_path / "proofs.jsonl"
    draws: list[int] = []
    replans: list[str] = []

    def propose(node: Node, failure: str | None, seed: int = 0) -> DiffProposal:
        if node.id == "t1":
            # Attempt 1 writes an examples-only spec that fails
            # property-coverage; attempt 2 deletes it and seals SPEC_DIFF,
            # so a sealed sidecar names a file the worktree no longer holds.
            return DiffProposal(DELETE_GONE + SPEC_DIFF if failure else EXAMPLES_ONLY_SPEC, "")
        return DiffProposal(BRANCH_DIFF if node.id == "n1" else GOOD_DIFF, "")

    def draw(node: Node, brief: str, seed: int) -> DiffProposal:
        draws.append(seed)
        return DiffProposal(_candidate_diff(_flag_test(1, 5)), "")

    def replan(node: Node, history: str, reserved: Sequence[str] = ()) -> Dag:
        replans.append(node.id)
        return Dag.model_validate({"nodes": [_node_dict("m1", [])]})

    result = run_slice(
        "Specify f, then fix it.",
        dag,
        workdir=tmp_path,
        journal_path=journal,
        propose=propose,
        replan=replan,
        survivor_draw=draw,
        now=lambda: "2026-09-16T00:00:00+00:00",
    )
    assert draws == []
    assert replans == ["n1"]
    assert list(result.proofs) == ["t1", "n1.r1"]
    assert not (tmp_path / "test_gone.py").exists()


EXAMPLES_ONLY_SPEC = (
    "diff --git a/test_gone.py b/test_gone.py\n"
    "new file mode 100644\n"
    "--- /dev/null\n"
    "+++ b/test_gone.py\n"
    "@@ -0,0 +1,5 @@\n"
    "+from n import f\n"
    "+\n"
    "+\n"
    "+def test_f():  # REQ-001\n"
    "+    assert f() == 2\n"
)

# A delete carries no body: the contents of a file being removed are not
# evidence of anything (T6-62/A1).
DELETE_GONE = (
    "diff --git a/test_gone.py b/test_gone.py\n"
    "deleted file mode 100644\n"
    "--- a/test_gone.py\n"
    "+++ /dev/null\n"
)


def test_run_slice_survivor_round_that_keeps_nothing_leaves_the_node_failed(
    tmp_path: Path,
) -> None:
    """Known-bad (T6-29c): every draw is a plausible test that is wrong on
    the real tree (F21.14); nothing is kept, the round is journaled with
    exit 1, nothing is spliced and the node stays failed."""
    _slice_repo(tmp_path)
    dag = Dag.model_validate({"nodes": [_node_dict("n1", [])]})
    journal = tmp_path / "proofs.jsonl"
    result = run_slice(
        "Fix f.",
        dag,
        workdir=tmp_path,
        journal_path=journal,
        propose=lambda node, failure, seed: DiffProposal(BRANCH_DIFF, ""),
        survivor_draw=lambda node, brief, seed: DiffProposal(_candidate_diff(_flag_test(1, 6)), ""),
        survivor_samples=2,
        now=lambda: "2026-09-16T00:00:00+00:00",
    )
    assert result.passed is False
    assert result.proofs == {}
    span = _survivor_span(journal, "n1")
    assert (span.exit_code, span.argv[4]) == (1, "kept=0")
    assert [p.replaces for p in read_plans(journal)] == [""]
    assert "## Node n1.r1\n" not in result.transcript


def test_survivor_gap_admits_impl_gate_failures_on_coverage_or_mutation_only() -> None:
    impl = Node.model_validate(_node_dict("n1", []))
    spec = Node.model_validate({**_node_dict("t1", []), "kind": "test"})
    gap = Tier1Result(
        node_id="n1",
        passed=False,
        checks=(GateCheck(name="coverage", passed=False, detail="75%"),),
        gaps=(("n.py", 3),),
    )
    assert slice_module._survivor_gap(impl, NodeGateFailedError(gap)) is True
    assert slice_module._survivor_gap(spec, NodeGateFailedError(gap)) is False
    assert slice_module._survivor_gap(impl, NodeUnappliableError("n1", "garbage")) is False
    other = dataclasses.replace(
        gap, checks=(*gap.checks, GateCheck(name="tests", passed=False, detail="exit 1"))
    )
    assert slice_module._survivor_gap(impl, NodeGateFailedError(other)) is False
    assert (
        slice_module._survivor_gap(impl, NodeGateFailedError(dataclasses.replace(gap, gaps=())))
        is False
    )


def test_run_slice_survivor_recovery_leaves_other_gate_failures_alone(tmp_path: Path) -> None:
    """Known-bad (T6-29c): a node that fails its tests gate is not a
    coverage or mutation gap; nothing is drawn and it replans as today."""
    _slice_repo(tmp_path)
    dag = Dag.model_validate({"nodes": [_node_dict("n1", [])]})
    journal = tmp_path / "proofs.jsonl"
    draws: list[int] = []
    replans: list[str] = []

    def draw(node: Node, brief: str, seed: int) -> DiffProposal:
        draws.append(seed)
        return DiffProposal(_candidate_diff(_flag_test(1, 5)), "")

    def replan(node: Node, history: str, reserved: Sequence[str] = ()) -> Dag:
        replans.append(node.id)
        return Dag.model_validate({"nodes": [_node_dict("m1", [])]})

    result = run_slice(
        "Fix f.",
        dag,
        workdir=tmp_path,
        journal_path=journal,
        # `return 3` fails the tests gate and still leaves line 3 uncovered:
        # a gap exists, and only the gate rule keeps the round from starting.
        propose=lambda node, failure, seed: DiffProposal(
            BRANCH_DIFF.replace("+    return 2\n", "+    return 3\n")
            if node.id == "n1"
            else GOOD_DIFF,
            "",
        ),
        replan=replan,
        survivor_draw=draw,
        now=lambda: "2026-09-16T00:00:00+00:00",
    )
    assert draws == []
    assert replans == ["n1"]
    assert result.passed is True


def test_created_file_reads_one_creation_and_nothing_else() -> None:
    """Known-good: a single creation yields its path and text. Known-bad:
    a modification, or two sections, is not one created test file."""
    source = _flag_test(1, 5)
    assert slice_module._created_file(_candidate_diff(source)) == (
        "tests/test_REQ-001_s0.py",
        source,
    )
    assert slice_module._created_file(GOOD_DIFF) is None
    assert slice_module._created_file(JUNK1_DIFF + JUNK2_DIFF) is None
    assert slice_module._created_file("") is None
    assert slice_module._created_file("diff --git a/t.py b/t.py\nnew file mode 100644\n") is None
    assert slice_module._created_file(JUNK1_DIFF.replace("+X = 1\n", "+X = 1\n Y = 2\n")) is None


def test_cut_to_last_test_keeps_what_parses() -> None:
    """Known-good: a whole file passes through; a file whose tail was cut
    mid-function keeps every complete test before the cut. Known-bad: a
    file with no complete test function, or one the cut leaves without
    any, yields nothing."""
    whole = _flag_test(1, 5)
    assert slice_module._cut_to_last_test(whole) == whole
    truncated = whole + "\n\ndef test_f_flag_two():  # REQ-001\n    assert f(flag=2) =="
    assert slice_module._cut_to_last_test(truncated) == whole
    assert slice_module._cut_to_last_test("def test_broken(:\n    pass\n") is None
    assert slice_module._cut_to_last_test("from n import f\n\nx = (\n") is None
    assert slice_module._cut_to_last_test("from n import f\n\n\ndef helper():\n    pass\n") is None


def test_run_slice_defers_an_uncovered_line_while_a_test_node_is_owed(tmp_path: Path) -> None:
    """The wiring, not the predicate (T6-53).

    Known-bad is the run above, `..._gate_fail_leaves_dependent_undispatched`:
    the identical diff and the identical uncovered `unused`, in a plan
    that owes no tests, and the node fails `coverage`. The only
    difference here is a `test` node the plan has yet to run, which is
    what round 3g's `n2.r1` had -- it was failed for eleven lines whose
    record shape `tests/test_store.py` exercises, owned by a node that
    had not been dispatched.

    This half is also the admission T6-53 makes, exhibited: `unused`
    really is code nothing runs, and it seals. Coverage keeps full force
    the moment nothing is owed, which the sibling test pins.
    """
    _slice_repo(tmp_path)
    dag = Dag.model_validate(
        {
            "nodes": [
                _node_dict("a", []),
                {**_node_dict("b", ["a"]), "kind": "test"},
            ]
        }
    )
    bad_diff = GOOD_DIFF.replace(
        "+    return 2\n",
        "+    return 2\n+\n+\n+def unused():\n+    return 3\n",
    ).replace("@@ -1,2 +1,2 @@", "@@ -1,2 +1,6 @@")

    def propose(node: Node, failure: str | None, seed: int = 0) -> DiffProposal:
        return DiffProposal(bad_diff, "")

    result = run_slice(
        "Fix f.",
        dag,
        workdir=tmp_path,
        journal_path=tmp_path / "proofs.jsonl",
        propose=propose,
        now=lambda: "2026-09-16T00:00:00+00:00",
    )
    assert (
        "- Gate coverage: PASS (deferred, no test node has run that can reach " in result.transcript
    )
    assert "a" in result.proofs


def test_replan_record_carries_every_node_of_the_post_replan_plan(tmp_path: Path) -> None:
    """T6-72. `splice_replan` rewires a failed node's dependents onto the
    new leaves, which changes those dependents' `node_hash`. The replan
    record journaled only the generated nodes, so a rewired survivor's
    proof sealed against a hash that appeared in no plan record and
    `verify` failed `unplanned-proof`.

    Known-bad on disk: `regress-99e3986/t2/t2-saddle` at `99e3986`. Plan
    at line 1 holds `test-retries` and `impl-retries`; the replan at line
    63 holds only `test-retries.r1`; the proof at line 135 seals
    `impl-retries` under `01c1cb70` where the plan said `afe0c878`, with
    the replacement's record as its parent. Both nodes sealed with zero
    gate failures and `oracle_t2.py` returned PASS -- an agreement
    reported as a failure.

    The record is the whole post-replan plan, not the delta. `verify`
    already accumulates across plan records, so the reading side is
    unchanged and the rewired hash is present by construction.
    """
    _slice_repo(tmp_path)
    dag = Dag.model_validate({"nodes": [_node_dict("n1", []), _node_dict("n2", ["n1"])]})
    journal = tmp_path / "proofs.jsonl"

    def propose(node: Node, failure: str | None, seed: int = 0) -> DiffProposal:
        return DiffProposal(BAD_DIFF if node.id == "n1" else GOOD_DIFF, "")

    def replan(node: Node, history: str, reserved: Sequence[str] = ()) -> Dag:
        return Dag.model_validate({"nodes": [_node_dict("m1", [])]})

    run_slice(
        "Fix f.",
        dag,
        workdir=tmp_path,
        journal_path=journal,
        propose=propose,
        replan=replan,
        now=lambda: "2026-09-16T00:00:00+00:00",
    )
    plans = read_plans(journal)
    assert [p.replaces for p in plans] == ["", "n1", "n2"]
    before = {n.id: n.node_hash for n in plans[0].nodes}
    after = {n.id: n.node_hash for n in plans[1].nodes}
    # The survivor is journaled by the replan that rewired it, under the
    # hash it was rewired to -- that pair is the whole contract.
    assert "n2" in after, "the rewired survivor must appear in the replan record"
    assert after["n2"] != before["n2"], "rewiring n2 onto n1.r1 must move its hash"
    planned = {node.node_hash for plan in plans for node in plan.nodes}
    for record in read_records(journal):
        assert not record.node_hash or record.node_hash in planned
    assert verify_journal(journal) == []


def test_a_draws_transport_failure_does_not_discard_its_completed_siblings(
    tmp_path: Path,
) -> None:
    """T6-73 known-good. One draw's timeout loses that draw, not the others.

    `draw` caught only `VllmResponseError`, and `VllmRequestError` is a
    sibling class rather than a subclass, so a transport failure escaped
    `pool.map` and took every finished draw with it. In
    `g1-0217c79/t5-s1` that sealed `samples: []` after 1800 s: the run's
    metrics sampler shows three draws in flight from t=411 s falling to
    two at t=693 s and one at t=863 s, so two draws had COMPLETED and
    neither was recorded. `_best_of_samples`'s own docstring states the
    contract it broke -- one bad packet "loses this draw, not the
    others".
    """
    _slice_repo(tmp_path)
    dag = Dag.model_validate({"nodes": [_node_dict("n1", [])]})
    journal = tmp_path / "proofs.jsonl"
    seeds: list[int] = []

    def propose(node: Node, failure: str | None, seed: int = 0) -> DiffProposal:
        seeds.append(seed)
        if seed == 1:
            raise VllmRequestError(TIMED_OUT)
        return DiffProposal(GOOD_DIFF, "")

    result = run_slice(
        "Fix f.",
        dag,
        workdir=tmp_path,
        journal_path=journal,
        propose=propose,
        now=lambda: "2026-09-16T00:00:00+00:00",
    )
    assert result.passed is True, result.transcript
    assert sorted(seeds) == list(range(PROPOSAL_SAMPLES)), "every seed is still drawn"
    agent = next(s for s in read_spans(journal) if s.kind == "agent" and s.name == "worker:n1")
    samples = _sidecar(journal, agent)["samples"]
    assert len(samples) == PROPOSAL_SAMPLES, "the failed draw is a sample, not the end of sampling"
    failed = [s for s in samples if s.get("error_type") == "VllmRequestError"]
    assert len(failed) == 1
    assert "timed out" in failed[0]["outcome"]
    survivors = [s for s in samples if s.get("error_type") is None]
    assert len(survivors) == PROPOSAL_SAMPLES - 1, "the completed siblings survive the failure"
    assert (tmp_path / "n.py").read_text() == "def f():\n    return 2\n"


def test_a_rejected_api_key_fails_the_node_rather_than_becoming_a_sample(
    tmp_path: Path,
) -> None:
    """T6-73 known-bad. Widening `draw`'s except must not swallow auth.

    A rejected key is not a per-draw condition -- every sibling and every
    retry fails the same way -- so it must keep failing the node through
    `_run_node`'s give-up path rather than being recorded as one sample
    among three. If `VllmAuthError` joined the caught tuple, seed 0's
    good diff would seal the node and a broken key would read as a pass.
    """
    _slice_repo(tmp_path)
    dag = Dag.model_validate({"nodes": [_node_dict("n1", [])]})
    journal = tmp_path / "proofs.jsonl"

    def propose(node: Node, failure: str | None, seed: int = 0) -> DiffProposal:
        if seed == 1:
            raise VllmAuthError(KEY_REJECTED)
        return DiffProposal(GOOD_DIFF, "")

    result = run_slice(
        "Fix f.",
        dag,
        workdir=tmp_path,
        journal_path=journal,
        propose=propose,
        now=lambda: "2026-09-16T00:00:00+00:00",
    )
    assert result.passed is False, "a rejected key must not let a sibling seal the node"
    agent = next(s for s in read_spans(journal) if s.kind == "agent" and s.name == "worker:n1")
    assert _sidecar(journal, agent)["error_type"] == "VllmAuthError"


def test_a_survivor_draws_transport_failure_does_not_discard_its_siblings(
    tmp_path: Path,
) -> None:
    """T6-73 known-good, the survivor drawer's copy of the same defect.

    `one` catches what `_best_of_samples`'s `draw` catches, so it carried
    the same gap: a transport failure in one candidate discarded every
    candidate drawn beside it and failed the recovery, where a truncation
    in the same position is already recorded per draw (`s4: dropped` in
    the T6-29c known-good).
    """
    _slice_repo(tmp_path)
    dag = Dag.model_validate({"nodes": [_node_dict("n1", [])]})
    journal = tmp_path / "proofs.jsonl"

    def draw(node: Node, brief: str, seed: int) -> DiffProposal:
        if seed == 1:
            raise VllmRequestError(TIMED_OUT)
        return DiffProposal(_candidate_diff(_flag_test(1, 5)), "")

    result = run_slice(
        "Fix f.",
        dag,
        workdir=tmp_path,
        journal_path=journal,
        propose=lambda node, failure, seed: DiffProposal(BRANCH_DIFF, ""),
        survivor_draw=draw,
        survivor_samples=2,
        now=lambda: "2026-09-16T00:00:00+00:00",
    )
    assert result.passed is True, result.transcript
    span = _survivor_span(journal, "n1")
    assert "s0: kept:" in span.detail
    assert f"s1: dropped: worker call failed: {TIMED_OUT}" in span.detail
