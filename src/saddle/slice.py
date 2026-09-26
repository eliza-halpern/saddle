"""Vertical-slice driver (ARCHITECTURE.md §6 step 1).

Runs one validated DAG through scheduler, Tier-1 gates, and journal, then
renders the transcript from the sealed records. Emission and validation
stay caller-side: this module is the deterministic schedule→gate→seal→
transcribe path. Nodes run one at a time, not concurrently (see F7); wider
graphs ride the same path.
"""

from __future__ import annotations

import ast
import asyncio
import hashlib
import json
import re
import shlex
import shutil
import tempfile
import uuid
from collections.abc import Callable, Collection, Mapping, Sequence
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath
from statistics import median
from time import perf_counter
from typing import Any, Final

from saddle.dag import (
    Dag,
    ExecutionConstraints,
    Node,
    pending_test_nodes,
    planned_requirement_ids,
    reserved_target_files,
)
from saddle.edits import EditError, apply_edits
from saddle.evidence import (
    CapturedRun,
    attempt_ref,
    changed_statements,
    covered_lines,
    drop_test_caches,
    git_changed_files,
    git_diff,
    mutation_sample,
    proven_ref,
    pytest_scope,
    restore_baseline,
    ruff_argv,
    run_argv,
    run_shell,
    run_shell_capture,
    run_stdin_capture,
    snapshot_baseline,
    snapshot_tree,
    under_coverage,
)
from saddle.gates import SHELL_TIMEOUT, GateCheck, Tier1Result
from saddle.journal import (
    ProofRecord,
    SpanRecord,
    SpanRecorder,
    append_plan,
    append_record,
    append_span,
    attempt_sidecar_path,
    build_from_gate,
    build_plan,
    build_span,
    hash_node,
    proven_records,
    read_records,
    read_spans,
    tool_spans_by_node,
    tool_spans_for_node,
    verify_journal,
    write_attempt_sidecar,
)
from saddle.rule_d_run import RuleDDecision
from saddle.runner import read_sources, run_node_gate
from saddle.scheduler import Proof, schedule
from saddle.survivors import (
    CandidateRun,
    CandidateRunner,
    build_survivor_brief,
    candidate_test_path,
    enclosing_functions,
    keep_candidate,
    stubbed_sandbox,
)
from saddle.transcript import NodeTranscript, RunTranscript, render_transcript
from saddle.vllm import DiffProposal, VllmError, VllmRequestError, VllmResponseError


class NodeGateFailedError(Exception):
    """A node worker failed its Tier-1 gate; carries the verdict.

    `applied` is every diff the node's attempts applied, in order, and
    `baseline` the ref they applied onto (T6-29c): the tree the gate
    judged is restored before this leaves `_run_node`, and a survivor
    round has to rebuild it to judge candidate tests against it.
    """

    def __init__(
        self,
        result: Tier1Result,
        attempts: int = 1,
        failure: str | None = None,
        *,
        applied: Sequence[str] = (),
        baseline: str | None = None,
    ) -> None:
        super().__init__(f"node {result.node_id!r} failed its Tier-1 gate")
        self.result = result
        self.attempts = attempts
        self.failure = failure
        self.applied = tuple(applied)
        self.baseline = baseline


class NodeUnappliableError(RuntimeError):
    """No applicable diff emerged; carries attempts and last evidence."""

    def __init__(
        self, node_id: str, detail: str, attempts: int = 1, failure: str | None = None
    ) -> None:
        super().__init__(f"node {node_id!r}: {detail}")
        self.node_id = node_id
        self.attempts = attempts
        self.failure = failure


class NodeQuestionError(Exception):
    """Rule D asked instead of deciding (P2-3): the node halts with the question.

    Not a gate failure, so neither a survivor round nor a replan takes it up:
    the answer has to come from the user, and a redraw cannot supply one.
    """

    def __init__(self, node_id: str, question: str, attempts: int = 1) -> None:
        super().__init__(f"node {node_id!r} halted on a rule D question:\n{question}")
        self.node_id = node_id
        self.question = question
        self.attempts = attempts


RuleDCheck = Callable[[str, Path], RuleDDecision | None]
"""The verdict step after the gates: node kind and tree in, a decision or None out."""


class ReplanFailedError(Exception):
    """A replan attempt produced nothing schedulable; the node stays failed."""


Proposer = Callable[[Node, str | None, int], DiffProposal]
"""Propose a diff for a node; `failure` carries prior-attempt evidence."""

# T6-65: the third argument is the files pending siblings still owe. The
# replan prompt states the whole task, so without it a subplan re-plans
# work another node already owns -- round 3i, F21.40.
Replanner = Callable[[Node, str, Sequence[str]], Dag]
"""Re-emit a failed node's scope; history carries the failure evidence."""

MAX_RECOVERY_RETRIES: Final = 2
# Exit code of a run its deadline stopped (T6-9): not 0 (nothing is
# claimed proven that is not), not 1 (nothing failed), and the run span
# carries the same verdict so `verify` can tell the two apart.
DEADLINE_EXIT: Final = 3
# Exit code of a run that halted on a rule D question (P2-3) and failed
# nothing else: not 0 (nothing asked about is proven), not 1 (asking is not
# a failure: the answer has to come from the user), not 2 (argparse's usage
# error) and not 3 (the deadline). The run span and the transcript carry
# the same verdict, QUESTION, so a caller needs neither to tell them apart.
QUESTION_EXIT: Final = 4
# Independent proposals drawn for the first attempt. Sequential retry
# conditions each sample on the last rejection, which optimises against
# whichever gate pushes back hardest: on T7 recovery drove the node from
# four failing gates to one and then spent its budget on ruff while an
# infinite loop sat untouched (F13). SpecBench measures the same effect
# -- "longer search increases the severity of reward hacking". Drawing
# unconditioned samples and keeping the best is the parallel half of the
# compute-optimal split; sequential recovery still follows.
PROPOSAL_SAMPLES: Final = 3
RECOVERY_OUTPUT_CHARS: Final = 4000
# The merge-time suite runs under the same interpreter form as every
# node's tests gate (`coverage run -m pytest ...`): the module form puts
# the working directory on `sys.path`, bare `pytest` does not, and a repo
# whose tests import a top-level package without packaging passes every
# gate and fails the merge for a reason no gate can observe (T3-17; the
# smoke run of 2026-09-19: `pytest -q` exit 2 `No module named 'src'`,
# `python -m pytest -q` 6 passed on the same tree).
MERGE_COMMAND: Final = "python -m pytest -q"
# Which `allowed_tools` name governs a captured run's output in the repair
# prompt (T3-4), keyed by the executable's basename or, for `python -m X`,
# by X (`_captured_run_tool`). The suite runs under `coverage run -m
# pytest`, so the executable the node's `test_command` names is not always
# argv[0]. A run that resolves to no key is governed by no binding and is
# kept: these are the harness's own runs (git, mutmut), and dropping one
# would remove evidence no plan asked to be withheld.
CAPTURED_RUN_TOOL: Final[dict[str, str]] = {
    "pytest": "run_tests",
    "coverage": "run_tests",
    "python": "run_tests",
    "ruff": "lint",
}


class _DeadlineSkipError(Exception):
    """A node not dispatched because the run's deadline leaves no room for it (T6-9)."""


@dataclass
class _Deadline:
    """A run's clock (T6-9): when it ends, and how long nodes have taken so far."""

    at: float
    clock: Callable[[], float]
    walls: list[float] = field(default_factory=list)

    def expired(self) -> bool:
        return self.clock() >= self.at

    def room_for_node(self) -> bool:
        """Whether a node can still be started: time left, and at least the
        median node wall so far when any node has run to completion."""
        remaining = self.at - self.clock()
        if remaining <= 0:
            return False
        return not self.walls or remaining >= median(self.walls)


class _HaltRecoveryError(Exception):
    """Identical re-proposal: the attempt span is sealed, exit the loop."""

    def __init__(self, result: Tier1Result | None, attempts: int, failure: str | None) -> None:
        msg = "worker re-proposed an identical diff"
        super().__init__(msg)
        self.result = result
        self.attempts = attempts
        self.failure = failure


@dataclass(frozen=True)
class SliceResult:
    """Slice outcome: verdict, readable transcript, proven node hashes."""

    passed: bool
    transcript: str
    proofs: dict[str, str]
    deadline_hit: bool = False
    question: bool = False


def _utcnow() -> str:
    """Current UTC time as an ISO string for transcripts."""
    return datetime.now(UTC).isoformat()


def _elapsed_ms(start: float) -> int:
    """Whole milliseconds elapsed since a `perf_counter` reading."""
    return int((perf_counter() - start) * 1000)


# Progressively tolerant `git apply` modes, strictest first. Each is
# deterministic and costs no model call, which is the point: T1 spent two
# of three worker calls on diffs that would not apply and T7 lost a whole
# run to three consecutive failures one lint fix from passing (F4, F11,
# F13). The dominant cause is context reproduced from memory with drifted
# whitespace, not a wrong edit. Order matters -- a strict apply is tried
# first so a fuzzy mode never pre-empts an exact match. `--ignore-whitespace`
# only reaches whitespace *within* a line; a blank line the file has and
# the hunk lacks (or the reverse) is a line-level drift no git flag
# addresses, and it was the whole loss on `fees.py` in rounds 3d and 3e
# (F21.16). The last rung, `blank-lines`, is not a flag: it re-derives
# each hunk's blank context from the file and applies the result strictly.
_APPLY_MODES: Final = (
    ("strict", ()),
    ("ignore-whitespace", ("--ignore-whitespace",)),
    ("reduced-context", ("--ignore-whitespace", "-C1")),
    ("three-way", ("--3way",)),
)

_HUNK_KINDS: Final = (" ", "-", "+", "\\", "")


def _reanchor_hunk(
    hunk: Sequence[str], file_lines: Sequence[str], start: int
) -> tuple[list[str], int, int] | None:
    """`hunk` with its blank old lines re-derived from `file_lines`.

    The hunk's non-blank old lines (context and deletions) must occur
    exactly once, in order, at or after `start`, with nothing but blank
    lines between consecutive ones; a line is compared with its whitespace
    collapsed, the same tolerance the `ignore-whitespace` rung already
    grants. Anything else -- a code line the file does not have, or an
    anchor the file has twice -- returns None and the diff is refused as
    before: a drifted `return` is a wrong edit, not drift. Blank lines the
    file has become context; blank lines only the hunk has are dropped, so
    this rung never deletes a blank line. Additions keep their place
    relative to the old lines around them.

    Returns the rewritten body, the index of its first old line, and the
    index just past its last.
    """
    anchors = [line[1:] for line in hunk if line[:1] in (" ", "-") and line[1:].strip()]
    if not anchors:
        return None
    matches: list[tuple[int, int]] = []
    for position in range(start, len(file_lines)):
        cursor = position
        for text in anchors:
            while cursor < len(file_lines) and not file_lines[cursor].strip():
                cursor += 1
            if cursor >= len(file_lines) or file_lines[cursor].split() != text.split():
                break
            cursor += 1
        else:
            matches.append((position, cursor))
    if len(matches) != 1:
        return None
    position, end = matches[0]
    body: list[str] = []
    cursor = position
    for line in hunk:
        kind, text = line[:1], line[1:]
        if kind in ("+", "\\"):
            body.append(line)
        elif not text.strip():
            if cursor < end and not file_lines[cursor].strip():
                body.append(" " + file_lines[cursor])
                cursor += 1
        else:
            while not file_lines[cursor].strip():
                body.append(" " + file_lines[cursor])
                cursor += 1
            body.append(kind + file_lines[cursor])
            cursor += 1
    return body, position, end


def _reanchor_blank_lines(workdir: Path, diff: str) -> str | None:
    """`diff` with every hunk against an existing file re-anchored (T6-38).

    Hunks against files the diff creates, or that are not in the tree,
    pass through untouched. None when any hunk cannot be re-anchored, so
    the rung is skipped rather than applied in part.
    """
    lines = diff.splitlines()
    out: list[str] = []
    file_lines: list[str] | None = None
    cursor = delta = 0
    index = 0
    while index < len(lines):
        line = lines[index]
        if line.startswith("diff --git "):
            file_lines = None
            cursor = delta = 0
        elif line.startswith("--- a/"):
            target = workdir / line.removeprefix("--- a/")
            if target.is_file():
                file_lines = target.read_text().splitlines()
        elif line.startswith("@@ ") and file_lines is not None:
            stop = index + 1
            while stop < len(lines) and lines[stop][:1] in _HUNK_KINDS:
                stop += 1
            found = _reanchor_hunk(lines[index + 1 : stop], file_lines, cursor)
            if found is None:
                return None
            body, position, cursor = found
            olds = sum(entry[:1] in (" ", "-") for entry in body)
            news = sum(entry[:1] in (" ", "+") for entry in body)
            out.append(f"@@ -{position + 1},{olds} +{position + 1 + delta},{news} @@")
            out.extend(body)
            delta += news - olds
            index = stop
            continue
        out.append(line)
        index += 1
    return "\n".join(out) + "\n"


_FENCE = re.compile(r"```[A-Za-z]*\n(?P<body>.*)\n```", re.S)


def _unwrapped(diff: str) -> str:
    """Strip the packaging a worker response arrives in (T6-48).

    Two things are packaging and not content. A missing final newline:
    `git apply` calls such a patch `corrupt patch at line N`, and four of
    the nine unconstrained round 3e draws failed on exactly that with
    nothing else wrong (F21.18). And one markdown fence around the whole
    answer: three more draws carried one, and the structural precheck
    rejected them before git ran.

    The fence must enclose everything. Prose beside it is a worker that
    ignored "output ONLY the diff" -- the case a fresh attempt fixes --
    and two fenced blocks are two answers, so a body that still contains
    a fence is refused rather than spliced. That also declines to unwrap
    a fenced diff *of* a fenced document, which is the conservative
    direction for a loosening.

    The bytes the server sent are what the sidecar records; only the
    ladder sees this.
    """
    match = _FENCE.fullmatch(diff.strip())
    text = match["body"] if match is not None and "```" not in match["body"] else diff
    return text if text.endswith("\n") else text + "\n"


# `diff --git a/<old> b/<new>`, and the hunk header's NEW-side start. The
# new path is the one the reconstruction is filed under: a rename writes
# the content at its destination.
_DIFF_GIT_PATHS: Final = re.compile(r"^diff --git a/(?P<old>.+?) b/(?P<new>.+)$")
_HUNK_NEW_START: Final = re.compile(r"^@@ -\d+(?:,\d+)? \+(?P<start>\d+)(?:,\d+)? @@")


def whole_file_reconstruction(diff: str) -> dict[str, str]:
    """The files a whole-file diff describes, read off its new side.

    A worker that emits the file it wants, rather than an edit to the
    file it was shown, writes one hunk per file anchored at line 1 whose
    context lines already carry the new content. That is not a guess
    about the model: every apply-failure in rounds 3h and 3i has this
    shape, and reconstructing from it produced implementations the hidden
    oracle passed 16 of 16 (F21.38, F21.38a). The ladder still refuses
    those diffs -- `--recount` fixes counts and `--ignore-whitespace`
    fixes whitespace, but neither reaches context that is a rewrite --
    so the candidate is discarded with the envelope.

    This assembles; it does not judge. The result is evidence attached to
    a failed attempt, never a tree to gate: a reconstruction that lost
    content would fail `tests` like any other, which is exactly why the
    decision belongs downstream and not here.

    A file qualifies only when its section holds exactly one hunk whose
    header parses and whose new side starts at line 1. Two hunks, an
    anchor past line 1, or a header carrying no ranges at all (round 3i
    emitted a bare `@@ `) is omitted rather than guessed at: a partial
    file assembled as though it were whole reads as a candidate tree and
    is not one.
    """
    sections: dict[str, list[str]] = {}
    body: list[str] | None = None
    for line in diff.splitlines():
        header = _DIFF_GIT_PATHS.match(line)
        if header is not None:
            body = sections.setdefault(header["new"], [])
            continue
        if body is not None:
            body.append(line)
    out: dict[str, str] = {}
    for name, lines in sections.items():
        hunks = [index for index, line in enumerate(lines) if line.startswith("@@")]
        if len(hunks) != 1:
            continue
        start = _HUNK_NEW_START.match(lines[hunks[0]])
        if start is None or int(start["start"]) != 1:
            continue
        kept: list[str] = []
        for line in lines[hunks[0] + 1 :]:
            kind = line[:1]
            if kind in (" ", "+"):
                kept.append(line[1:])
            elif kind == "":
                # A context line for a blank line, with its leading space
                # stripped in transit -- `_HUNK_KINDS` admits the same.
                kept.append("")
            elif kind not in ("-", "\\"):
                break
        out[name] = "".join(f"{line}\n" for line in kept)
    return out


def _reconstruction_evidence(diff: str) -> dict[str, Any]:
    """What a refused diff still proves, assembled once at failure time.

    Recommendation 36 already landed in both halves -- the last rung's
    stderr rides in the failure message (T6-27) and the draw itself is
    `samples[i].diff` -- so the evidence is not lost, it is unassembled.
    Recovering what a rejected draw proposed still meant reading the
    diff, reconstructing the file its context lines describe and staging
    it by hand, five times across F21.38 and F21.38a. This does that
    once, and says whether the result is a tree at all, so a reader can
    tell a recoverable candidate from junk without staging anything.

    Nothing recovered is recorded as nothing rather than as an empty
    candidate, so a reader cannot mistake "declined to reconstruct" for
    "reconstructed an empty file".
    """
    files = whole_file_reconstruction(diff)
    if not files:
        return {}
    recovered: dict[str, Any] = {}
    for name, text in sorted(files.items()):
        entry: dict[str, Any] = {"content": text}
        if name.endswith(".py"):
            try:
                ast.parse(text)
            except SyntaxError:
                entry["parses"] = False
            else:
                entry["parses"] = True
        recovered[name] = entry
    return {"reconstruction": recovered}


def _apply_diff(workdir: Path, diff: str, *, recorder: SpanRecorder | None = None) -> str:
    """Apply a proposed diff from stdin and stage it; gates diff tracked content.

    Returns the mode that applied. Every attempt journals its own span, so
    a diff that needed loosening says so in the record rather than passing
    as though it had matched exactly.
    """
    # Primary defence, not a backstop. DIFF_SCHEMA carried a
    # `^diff --git ` pattern until the v3 T1 arm proved the token mask
    # cannot be trusted with this constraint: the decoder compiles
    # `pattern` as a full match, so it admitted the header and nothing
    # else. Every regex that is correct under those semantics let the
    # model emit a raw quote and break its own JSON packet; every regex
    # that kept the packet intact excluded code containing a quote. So
    # structure is checked here, where a violation is deterministic,
    # inspectable and retryable -- prose is exactly the case a fresh
    # attempt can fix, and a fatal parse error would throw the run away.
    # T6-48: the packaging comes off before anything judges the content,
    # so the precheck and every rung see the same bytes.
    diff = _unwrapped(diff)
    if not diff.lstrip().startswith("diff --git "):
        msg = f"worker content is not a unified diff (no 'diff --git' header) in {str(workdir)!r}"
        raise RuntimeError(msg)
    # A header with no hunk applies nothing, and git reports it as "No
    # valid patches in input" -- four times, once per _APPLY_MODES entry,
    # none of them informative. Catching it here costs no subprocess and
    # gives the retry loop a message that actually names the defect.
    if "@@ " not in diff:
        msg = f"worker diff has a header but no hunk ('@@ ' marker) in {str(workdir)!r}"
        raise RuntimeError(msg)
    stderr = ""
    for mode, flags in _APPLY_MODES:
        exit_code, stderr = run_stdin_capture(
            ["git", "apply", "--index", "--recount", *flags, "-"],
            workdir,
            diff,
            recorder=recorder,
        )
        if exit_code == 0:
            return mode
    reanchored = _reanchor_blank_lines(workdir, diff)
    if reanchored is not None:
        mode = "blank-lines"
        exit_code, stderr = run_stdin_capture(
            ["git", "apply", "--index", "--recount", "-"], workdir, reanchored, recorder=recorder
        )
        if exit_code == 0:
            return mode
    # The last rung's stderr rides in the failure (T6-27): the tool spans
    # carry each rung's, but the attempt's own record used to say only
    # "did not apply", and a reader had to go and find which line git
    # rejected.
    why = stderr.strip().splitlines()[-1] if stderr.strip() else "no stderr"
    msg = f"worker diff did not apply cleanly in {str(workdir)!r} (last rung {mode}: {why})"
    raise RuntimeError(msg)


def _payload_sections(payload: str) -> list[tuple[str, str | None]]:
    """The files a whole-file payload names, in order; `None` means delete.

    Strict where `whole_file_reconstruction` is lenient, and the two are
    not interchangeable. This reads the envelope the grammar admits, so a
    body line is `+`-prefixed new content and nothing else; that one
    reads a *rejected* diff of the old envelope, context lines and all,
    to recover what it was trying to say. Keeping them apart is what lets
    the forensic stay permissive while the live path stays exact.

    The path comes from the `diff --git a/X b/Y` header's new side, which
    is the only place it appears in a form the caller can use -- and note
    that `header ::= "diff --git " line` leaves it UNCONSTRAINED by the
    grammar. The grammar constrains form, not content (F21.9c); every
    check on the path itself therefore has to happen in `_write_files`.
    """
    sections: list[tuple[str, str | None]] = []
    lines = payload.splitlines()
    index = 0
    while index < len(lines):
        header = _DIFF_GIT_PATHS.match(lines[index])
        index += 1
        if header is None:
            continue
        name, body, deleted = header["new"], [], False
        anchored = False
        while index < len(lines) and not lines[index].startswith("diff --git "):
            line = lines[index]
            index += 1
            if line.startswith("+++ /dev/null"):
                deleted = True
            elif line.startswith("@@ "):
                anchored = True
            elif not anchored:
                continue
            elif line.startswith("+"):
                body.append(line[1:])
            elif not line.startswith("\\"):
                # Refuse, do not skip. A context or removal line means the
                # writer sent a PATCH, and the difference is not cosmetic:
                # keeping only the `+` lines would write a file holding
                # just the additions and silently delete everything else.
                # Silently destroying content the model never saw is the
                # known-bad T6-62 named; the grammar forbids these lines,
                # but the grammar is enforced by the server and this is
                # the only check that holds when it is not.
                kind = "removal" if line.startswith("-") else "context"
                msg = f"worker payload carries a {kind} line in {name!r}: {line[:60]!r}"
                raise RuntimeError(msg)
        if not deleted and not body:
            # A section with no anchor, or an anchor with nothing under
            # it, would otherwise write an EMPTY file -- a silent
            # deletion of everything that file held. `_apply_diff` named
            # the same shape "a header but no hunk"; it applies nothing
            # there and destroys everything here, so it is refused.
            msg = f"worker payload names {name!r} with no contents"
            raise RuntimeError(msg)
        sections.append((name, None if deleted else "".join(f"{x}\n" for x in body)))
    return sections


def _resolved_target(workdir: Path, name: str) -> Path:
    """*name* inside *workdir*, or a refusal naming why it is not.

    Under a diff envelope `git apply` refused an absolute path or one
    climbing out of the tree, and refused it for free. Writing files
    directly gives that up, so the check is re-established here: this is
    the only thing standing between a worker's header line and the rest
    of the filesystem (T6-62/A1).
    """
    root = workdir.resolve()
    target = (root / name).resolve()
    if not name.strip() or target == root or root not in target.parents:
        msg = f"worker payload names a path outside the tree: {name!r}"
        raise RuntimeError(msg)
    return target


# An edit payload opens with its operation and a path; a unified diff opens
# with "diff --git". The two are told apart by that first word alone, so a
# run can carry either without a flag reaching this far down.
_EDIT_HEAD: Final = re.compile(r"^(?:edit|create|delete) [^/\n]")


def _write_files(workdir: Path, payload: str, *, recorder: SpanRecorder | None = None) -> None:
    """Write what a worker payload carries, and stage it.

    The whole-file envelope replaces `git apply` on the worker path
    (T6-62/A1): the new side IS the file, so there is no context to match
    and nothing to reject for arithmetic. What remains is an envelope
    check, and every refusal here is a retryable `RuntimeError` for the
    same reason the old one was -- prose is exactly what a fresh attempt
    can fix.

    Both envelopes are dispatched here because both worker callers arrive
    here: the tree a node is gated on, and the throwaway copy a draw is
    scored against. `_apply_diff` is the older door and `src` no longer
    opens it; a dispatch put there reached neither caller, which is what
    `--emission edit`'s first run showed -- four draws, every one refused
    for a header an edit payload does not carry.
    """
    payload = _unwrapped(payload)
    if _EDIT_HEAD.match(payload.lstrip()):
        # A second envelope, not a second tree. The edit format names the
        # site it changes instead of restating the whole file, so the
        # emission costs the change's size rather than the file's -- the
        # reason round 3e lost all three of an attempt's draws to
        # whole-file emission (F21.43). What lands is staged the same way
        # and every gate downstream still reads the worktree, so nothing
        # below this point can tell which envelope arrived.
        try:
            touched = apply_edits(workdir, payload)
        except EditError as exc:
            msg = f"worker edits did not apply in {str(workdir)!r}: {exc}"
            raise RuntimeError(msg) from exc
        # The paths it named, the same as the whole-file arm below --
        # never a bare `git add -A`. `parse_edits` refuses a payload with
        # no blocks, so `touched` is never empty; an empty pathspec after
        # `--` is the one case where this would mean the whole tree.
        run_argv(["git", "add", "-A", "--", *touched], workdir, recorder=recorder)
        return
    if not payload.lstrip().startswith("diff --git "):
        msg = f"worker content is not a file payload (no 'diff --git' header) in {str(workdir)!r}"
        raise RuntimeError(msg)
    sections = _payload_sections(payload)
    if not sections:
        msg = f"worker payload names no file in {str(workdir)!r}"
        raise RuntimeError(msg)
    # A repeated path is the one failure the diff envelope caught for
    # free and this one does not: `root ::= section+` lets the model emit
    # the same file twice, and F21.9's b-s1 emitted NINE copies of
    # `fees.py`. Under a diff the second copy failed to apply, loudly.
    # Here the last write would simply win and nothing would say so.
    repeated = sorted({name for name, _ in sections if [n for n, _ in sections].count(name) > 1})
    if repeated:
        msg = f"worker payload writes {repeated[0]!r} more than once in {str(workdir)!r}"
        raise RuntimeError(msg)
    written: list[str] = []
    for name, contents in sections:
        target = _resolved_target(workdir, name)
        if contents is None:
            target.unlink(missing_ok=True)
        else:
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(contents, encoding="utf-8")
        written.append(name)
    run_argv(["git", "add", "-A", "--", *written], workdir, recorder=recorder)


def autofix(workdir: Path, *, baseline: str = "HEAD", recorder: SpanRecorder | None = None) -> None:
    """Apply ruff's mechanical fixes before any gate measures the tree.

    Repair attempts are scarce (MAX_RECOVERY_RETRIES) and iterative
    refinement follows the cheapest feedback signal, not the most
    important defect: lint emits precise localised errors while a failing
    property emits a counterexample that needs diagnosis. T7 spent its
    budget on ruff with an infinite loop untouched (F13), and the v3 T1
    re-run reproduced it -- 4 gates failing, then 2, with ruff red
    throughout. Formatting is entirely machine-solvable and most lint
    findings carry safe fixes, so spending a stochastic worker call on
    them buys nothing and displaces the correctness work.

    Scoped to the node's own changed files: formatting the whole tree
    would mark every pre-existing unformatted file changed, and
    `changed_line_coverage_min` would then demand coverage of lines the
    node never touched. Unsafe fixes stay off, and `check_ruff` still
    fails closed on whatever is left, so this narrows what the worker is
    asked to repair rather than lowering the bar.
    """
    changed = git_changed_files(workdir, baseline, recorder=recorder)
    targets = sorted(path for path in changed if path.endswith(".py"))
    if not targets:
        return
    # Order matters, and the test for this caught it: `--fix` rewrites
    # code (removing an unused import leaves a stray blank line), so
    # formatting has to be the last word or `ruff format --check` fails
    # on the mess the fixer just made. Exit codes are deliberately
    # ignored -- `ruff check --fix` reports what it could not fix, and
    # judging that is check_ruff's job.
    run_argv(ruff_argv("check", "--fix", *targets), workdir, recorder=recorder)
    run_argv(ruff_argv("format", *targets), workdir, recorder=recorder)
    run_argv(["git", "add", "--", *targets], workdir, recorder=recorder)


def _captured_run_tool(argv: Sequence[str]) -> str | None:
    """Which binding governs a captured run's output, by what it ran.

    The match is on the executable's basename and, for `python -m X`, on
    the module X: nothing constrains how a plan spells `test_command`, so
    `python3 -m pytest` and `.venv/bin/pytest` have to be `run_tests` as
    surely as `pytest` is, or omitting the name withholds nothing (T3-4
    follow-up; the same class as T3-13's unmatched spellings).
    """
    if not argv:
        return None
    name = PurePosixPath(argv[0]).name
    if name.startswith("python"):
        name = argv[2] if len(argv) >= 3 and argv[1] == "-m" else "python"
    return CAPTURED_RUN_TOOL.get(name)


# Which `allowed_tools` name a failed gate's output belongs to (T6-31): a
# captured run whose gate failed is the node's own evidence and reaches
# the worker whatever the plan declared.
GATE_TOOL: Final[dict[str, str]] = {
    "ruff": "lint",
    "tests": "run_tests",
    "coverage": "run_tests",
    "red-phase": "run_tests",
}


def _run_is_allowed(
    run: CapturedRun, tools: Collection[str], failed_gates: Collection[str]
) -> bool:
    """Is this run's output a capability the node asked for (T3-4), or the
    output of a gate the node failed (T6-31)?

    Round 3d's n2 saw `ruff check exited 1, format exited 0` twice and
    nothing else, because its plan had not declared `lint`; the gates
    whose verdicts carry file:line inline improved in the same attempts.
    `allowed_tools` bounds what the worker may do; a failed gate's output
    is evidence, not a capability.
    """
    governing = _captured_run_tool(run.argv)
    if governing is None or governing in tools:
        return True
    return governing in {GATE_TOOL.get(name) for name in failed_gates}


def _carrying(own: str, gate_failure: str | None) -> str:
    """`own`, plus the last gate failure when this one applied nothing.

    A truncated worker call and a failed apply both write nothing to the
    worktree, so the code the next attempt is handed is still the code
    the last GATE ruled on -- and that gate's detail is the only thing
    that describes it. Keeping only the newer message told attempt 3 of
    `runs/g1-0f6b83d` node-1 that a call had truncated, and nothing
    about the `requirement-binding` verdict it then failed again (T6-79).

    A node whose FIRST attempt fails this way has no gate verdict yet,
    and says so by carrying nothing rather than inventing one.
    """
    if gate_failure is None:
        return own
    return f"{own}\n\nNothing was applied, so the tree is unchanged since:\n\n{gate_failure}"


def format_attempt_failure(
    result: Tier1Result,
    captured: Sequence[CapturedRun],
    *,
    attempt: int,
    max_attempts: int,
    tools: Collection[str],
) -> str:
    """Render one failed attempt as repair evidence: gates plus failing output.

    `tools` is the node's `allowed_tools`, and it decides which captured
    output the worker gets back for gates that passed: `run_tests` for
    the suite, `lint` for ruff. The output of a gate the node failed is
    always included (T6-31). The gate verdict lines are unconditional --
    they are the node's own result, not a tool's.
    """
    failed = [check for check in result.checks if not check.passed]
    lines = [f"Attempt {attempt} of {max_attempts} failed {len(failed)} gate(s):"]
    lines.extend(f"- {check.name}: {check.detail}" for check in failed)
    failed_names = [check.name for check in failed]
    for run in captured:
        if run.exit_code == 0:
            continue
        if not _run_is_allowed(run, tools, failed_names):
            continue
        output = (run.stdout + "\n" + run.stderr).strip()
        if len(output) > RECOVERY_OUTPUT_CHARS:
            output = "[earlier output truncated]\n" + output[-RECOVERY_OUTPUT_CHARS:]
        lines.append(f"--- `{' '.join(run.argv)}` (exit {run.exit_code}) ---")
        lines.append(output or "(no output)")
    return "\n".join(lines) + "\n"


TASK_FIRST_PREAMBLE: Final = (
    "You are an expert coding assistant. Read the task and the files, then write the finished code."
)
"""The opening line of the task-first worker prompt (P2-1), and its mark."""


def prompt_shape(prompt: str) -> str:
    """Which worker prompt a draw came from, read off the prompt itself (P2-1).

    Read from the text the call sent, not from the caller's intent, so the
    sealed field cannot say "task-first" about a draw that was not.
    """
    return "task-first" if prompt.startswith(TASK_FIRST_PREAMBLE) else "structured"


def _proposal_evidence(proposal: DiffProposal) -> dict[str, Any]:
    """What a proposal leaves behind for its attempt's sidecar (T6-12)."""
    return {
        "thinking": proposal.reasoning,
        "diff_hash": hashlib.sha256(proposal.diff.encode()).hexdigest(),
        # The diff itself and the call that drew it (T6-27): a failed
        # attempt's diff used to survive only as a hash, and no attempt
        # could be replayed because its seed and temperature were not
        # written down.
        "diff": proposal.diff,
        "prompt": proposal.prompt,
        "prompt_shape": prompt_shape(proposal.prompt),
        "seed": proposal.seed,
        "temperature": proposal.temperature,
        "reasoning_effort": proposal.reasoning_effort,
        "started_at": proposal.started_at,
        "wall_s": proposal.wall_s,
        "finish_reason": "stop",
        "usage": dict(proposal.usage),
        "max_tokens": proposal.max_tokens,
    }


def _prompt_hash(prompt: str) -> str:
    """sha256 of a worker prompt, or empty when there was none to hash."""
    return hashlib.sha256(prompt.encode()).hexdigest() if prompt else ""


def _call_evidence(exc: BaseException) -> dict[str, Any]:
    """The call a failed worker request was, when the client attached it (T6-27)."""
    evidence = getattr(exc, "evidence", None)
    return dict(evidence) if isinstance(evidence, dict) else {}


def _error_evidence(exc: BaseException) -> dict[str, Any]:
    """What a failed worker call leaves behind.

    A truncation keeps its partial text, the usage the server reported
    and the cap the call sent (T6-12). Any other failure has no envelope
    -- a transport timeout arrives with nothing but its message -- so the
    sidecar names the exception type beside the detail (F21.12b): round
    3c's 1826 s timeout sealed four keys that could not say what it was.
    """
    evidence: dict[str, Any] = {"error_type": type(exc).__name__, **_call_evidence(exc)}
    if isinstance(exc, VllmResponseError):
        evidence.update(
            {
                "thinking": exc.reasoning,
                "partial_content_chars": len(exc.content),
                "finish_reason": exc.finish_reason,
                "usage": dict(exc.usage),
                "max_tokens": exc.max_tokens,
            }
        )
    return evidence


@dataclass
class _AttemptCtx:
    """One attempt's identity for its seal (T6-27): id, clocks, number, prompt."""

    worker_id: str
    start: float
    started_at: str
    number: int
    prompt_hash: str = ""


def _seal_attempt(
    journal_path: Path,
    node_id: str,
    run_span_id: str,
    ctx: _AttemptCtx,
    exit_code: int,
    detail: str,
    evidence: Mapping[str, Any] | None = None,
) -> None:
    """Append one attempt's agent span under the run span, with its sidecar.

    The span's argv names the attempt and the prompt's hash (T6-27):
    every worker span used to be appended with an empty argv, so its
    `args_hash` was the hash of `[]` and could not tell two calls apart.

    Every attempt, sealed or not, leaves `attempts/<span_id>.json` beside
    the journal (T6-12): round-3 T5 spent 160k output tokens on three
    failed attempts and journaled none of their reasoning, because
    `thinking` was written only onto proof records. The sidecar's hash is
    sealed in the span, so `verify` catches a missing or edited one.
    """
    payload: dict[str, Any] = {
        "node_id": node_id,
        "span_id": ctx.worker_id,
        "attempt": ctx.number,
        "exit_code": exit_code,
        "detail": detail,
        **(evidence or {}),
    }
    attempt_hash = write_attempt_sidecar(journal_path, ctx.worker_id, payload)
    append_span(
        journal_path,
        build_span(
            node_id=node_id,
            argv=["worker", node_id, f"attempt={ctx.number}", f"prompt_sha256={ctx.prompt_hash}"],
            duration_ms=_elapsed_ms(ctx.start),
            exit_code=exit_code,
            detail=detail,
            kind="agent",
            name=f"worker:{node_id}",
            parent_id=run_span_id,
            span_id=ctx.worker_id,
            attempt_hash=attempt_hash,
            started_at=ctx.started_at,
        ),
    )


def _evaluate_candidate(
    node: Node,
    workdir: Path,
    diff: str,
    baseline: str,
    planned: tuple[str, ...],
    owed: tuple[str, ...],
) -> tuple[Tier1Result | None, str | None]:
    """Gate `diff` on a throwaway copy of `workdir`; never touches it.

    Returns the gate result, or an error string when the diff does not
    apply. The copy carries `.git`, so the baseline ref the gate diffs
    against resolves exactly as it would in place.

    Nothing here reaches the journal. These are gate runs against trees
    that are discarded, and the chain records what was proven about the
    tree that was *sealed* -- a tool span with no sealed attempt to
    parent it would be unlinkable by construction. The sampling is still
    auditable: the attempt seal carries how many distinct candidates were
    drawn.
    """
    with tempfile.TemporaryDirectory() as tmp:
        candidate = Path(tmp) / "tree"
        shutil.copytree(workdir, candidate, symlinks=True)
        # copytree rewrites mtimes, so git reads every file as modified and
        # `git apply --index` refuses the patch. Refreshing the index
        # restores the stat cache; the same trap cost a benchmark arm a
        # run when `cp -r` produced "uncommitted changes" worktrees.
        run_argv(["git", "update-index", "--refresh"], candidate)
        try:
            _write_files(candidate, diff)
        except RuntimeError as exc:
            return None, str(exc)
        # The live path runs `autofix` before the gate; a candidate scored
        # without it read one gate redder than the tree the node would
        # actually be gated on, and `failures == 0` never ended sampling
        # (round 3c, F21.12c: delta one on three of four nodes).
        autofix(candidate, baseline=baseline)
        return (
            run_node_gate(
                node,
                candidate,
                baseline=baseline,
                planned_requirements=planned,
                owed_tests=owed,
            ),
            None,
        )


def _best_of_samples(
    node: Node,
    workdir: Path,
    propose: Proposer,
    baseline: str,
    planned: tuple[str, ...],
    owed: tuple[str, ...],
) -> tuple[DiffProposal | None, int, list[dict[str, Any]]]:
    """Draw PROPOSAL_SAMPLES unconditioned proposals concurrently; keep the best.

    The k worker calls go out together, each with its own seed (T6-25):
    the model is local and the server batches, so k draws cost about one
    draw's wall where drawing them one after another cost k -- round 3c's
    n1 spent 1028 s on three serial samples every other node waited on.
    The returned diffs are then evaluated one at a time in the worktree
    in seed order, never arrival order, so the sealed record does not
    depend on which request the server answered first; the first that
    passes seals, and the rest are recorded as not evaluated.

    Returns the winning diff, how many distinct diffs were drawn --
    agreement is the correlation signal: LLM samples "often fail on the
    same inputs", so k buys little when they agree, and recording it
    means rho is measured rather than assumed -- and one summary per
    sample for the attempt's sidecar (T6-12): a sample that failed its
    gates or did not apply used to vanish with its reasoning.
    """

    def draw(seed: int) -> DiffProposal | VllmError:
        # A failed call loses this draw, not the others: the samples are
        # independent, so one bad packet is not evidence about the rest.
        # A transport failure is per-draw too, and catching only the
        # response error let one draw's timeout discard its COMPLETED
        # siblings (T6-73): `VllmRequestError` is a sibling class, not a
        # subclass, so it escaped `pool.map` and took them with it.
        # `VllmAuthError` stays uncaught on purpose -- a rejected key is
        # not a per-draw condition, and letting a sibling seal the node
        # would report a broken key as a pass.
        try:
            return propose(node, None, seed)
        except (VllmResponseError, VllmRequestError) as exc:
            return exc

    with ThreadPoolExecutor(max_workers=PROPOSAL_SAMPLES) as pool:
        draws = list(pool.map(draw, range(PROPOSAL_SAMPLES)))
    scored: list[tuple[int, int, DiffProposal]] = []
    drawn: list[str] = []
    last: DiffProposal | None = None
    samples: list[dict[str, Any]] = []
    passed = False
    for index, outcome in enumerate(draws):
        if isinstance(outcome, VllmError):
            samples.append(
                {**_error_evidence(outcome), "outcome": f"worker call failed: {outcome}"}
            )
            continue
        proposal = outcome
        last = proposal
        drawn.append(proposal.diff)
        summary = _proposal_evidence(proposal)
        samples.append(summary)
        if passed:
            summary["outcome"] = "not evaluated: an earlier sample passed"
            continue
        if any(proposal.diff == earlier for earlier in drawn[:-1]):
            summary["outcome"] = "identical to an earlier sample"
            continue
        result, unappliable = _evaluate_candidate(
            node, workdir, proposal.diff, baseline, planned, owed
        )
        if unappliable is not None or result is None:
            summary["outcome"] = f"did not apply: {unappliable}"
            # What the refused draw still proves, assembled here rather
            # than by hand five rounds later (T6-62 C).
            summary.update(_reconstruction_evidence(proposal.diff))
            continue
        failures = sum(1 for check in result.checks if not check.passed)
        summary["outcome"] = f"{failures} gate(s) failed"
        # `index` breaks ties toward the earliest seed, so selection is
        # deterministic rather than dependent on sort stability.
        scored.append((failures, index, proposal))
        if failures == 0:
            passed = True
    if not scored:
        # Nothing gated cleanly and nothing applied. Hand back the last
        # sample rather than paying for another call: the attempt still
        # needs a diff to seal a failure against, and a fourth draw buys
        # no information the three already spent did not.
        return last, len(set(drawn)), samples
    scored.sort(key=lambda entry: entry[:2])
    return scored[0][2], len(set(drawn)), samples


def _abandon(
    workdir: Path, baseline: str | None, applied: Sequence[str], recorder: SpanRecorder
) -> None:
    """Undo a failed node's applied diffs before its failure leaves `_run_node` (T3-23).

    Every give-up path passes here. With at least one diff applied, the
    worktree and index go back to the node's own baseline ref, so a
    replacement node snapshots the tree this node started from (its
    `red-phase` pre leg can be red again) and the merge suite runs over
    proven edits only. A node that applied nothing restores nothing: the
    tree already is its baseline.
    """
    if applied and baseline is not None:
        restore_baseline(workdir, baseline, recorder=recorder)


async def _run_node(
    node: Node,
    workdir: Path,
    journal_path: Path,
    propose: Proposer,
    proofs: dict[str, str],
    run_span_id: str,
    planned: tuple[str, ...],
    # Empty is the strict reading: nothing owed, so every changed line is
    # the node's to cover. A single node gated on its own gets that.
    owed: tuple[str, ...] = (),
    *,
    task_hash: str,
    deadline: _Deadline | None = None,
    rule_d: RuleDCheck | None = None,
) -> Proof:
    """Execute one node: propose, apply, gate, seal — with bounded recovery.

    `planned` is every requirement id the node's plan declares, handed to
    each gate run so a citation of another node's id is not an orphan
    (T3-24). `task_hash` is the run's task, sealed into the record so a
    later resume can tell whose proof this is (T3-9).

    A failed gate (or a diff that does not apply) retries in a fresh
    worker call carrying the failure evidence, at most
    MAX_RECOVERY_RETRIES times; an identical re-proposal or exhausted
    retries fail the node. Fully synchronous inside, so a worker never
    yields mid-node and the proof map stays consistent without locks.
    """
    max_attempts = 1 + MAX_RECOVERY_RETRIES
    baseline: str | None = None
    failure: str | None = None
    gate_failure: str | None = None
    sampling = ""
    samples: list[dict[str, Any]] = []
    seen: list[str] = []
    applied: list[str] = []
    last_result: Tier1Result | None = None
    attempt = 0
    while attempt < max_attempts:
        # A sidecar's `samples` are the calls this attempt made: round 3c's
        # retries carried attempt 1's three draws byte for byte (F21.12c).
        samples = []
        # The attempt in flight runs to its end and may seal; the next one
        # is not started past the deadline (T6-9). The give-up path below
        # then restores the tree exactly as on any other exhaustion.
        if attempt and deadline is not None and deadline.expired():
            failure = f"deadline reached after {attempt} of {max_attempts} attempt(s); {failure}"
            break
        attempt += 1
        start = perf_counter()
        worker_id = uuid.uuid4().hex
        ctx = _AttemptCtx(worker_id, start, _utcnow(), attempt)
        recorder = SpanRecorder(path=journal_path, node_id=node.id, parent_id=worker_id)
        try:
            if baseline is None:
                # This node's own baseline, taken before any proposal is
                # drawn, so every gate below diffs this node's work and not
                # the staged edits of the nodes that ran before it (T3-8).
                # Under this attempt's recorder: the four `git` spans must
                # hang off a sealed attempt, as `_evaluate_candidate`
                # explains. Attempts 2..N keep the ref: a recovery diff
                # lands on the previous attempt's tree, and the proof is
                # the accumulated diff.
                baseline = snapshot_baseline(workdir, node.id, recorder=recorder)
            # A worker call that comes back truncated or malformed is a
            # spent attempt, not a dead node. The planner already retries
            # this exact condition (`_emit_valid_dag`); the worker path
            # raised instead, so T5's `finish_reason=length` failed the
            # node with the worktree untouched and nothing retried.
            try:
                if attempt == 1:
                    best, distinct, samples = _best_of_samples(
                        node, workdir, propose, baseline, planned, owed
                    )
                    proposal = (
                        best if best is not None else propose(node, failure, PROPOSAL_SAMPLES)
                    )
                    # Agreement across independent samples is the correlation
                    # signal: LLM samples "often fail on the same inputs", so
                    # k buys least exactly when they agree. Sealed so rho is
                    # measured across runs rather than assumed.
                    sampling = f"{distinct} distinct of {PROPOSAL_SAMPLES} sample(s)"
                else:
                    # A retry is one draw; its seed follows the sample seeds
                    # (0..k-1 for attempt 1, then k, k+1, ...).
                    proposal = propose(node, failure, PROPOSAL_SAMPLES - 2 + attempt)
                    samples = [
                        {**_proposal_evidence(proposal), "outcome": "retry draw, gated in place"}
                    ]
            except VllmResponseError as exc:
                ctx.prompt_hash = _prompt_hash(str(_call_evidence(exc).get("prompt", "")))
                own = f"Attempt {attempt} of {max_attempts}: worker call failed: {exc}"
                failure = _carrying(own, gate_failure)
                _seal_attempt(
                    journal_path,
                    node.id,
                    run_span_id,
                    ctx,
                    1,
                    own,
                    {**_error_evidence(exc), "samples": samples},
                )
                continue
            ctx.prompt_hash = _prompt_hash(proposal.prompt)
            if proposal.diff in seen:
                detail = (
                    f"attempt {attempt}/{max_attempts}: "
                    "worker re-proposed an identical diff; stopping recovery"
                )
                # No tool spans precede this seal, so its span id is unlinkable by design.
                _seal_attempt(
                    journal_path,
                    node.id,
                    run_span_id,
                    ctx,
                    1,
                    detail,
                    _proposal_evidence(proposal),
                )
                raise _HaltRecoveryError(last_result, attempt, failure)
            seen.append(proposal.diff)
            try:
                _write_files(workdir, proposal.diff, recorder=recorder)
            except RuntimeError as exc:
                own = f"Attempt {attempt} of {max_attempts}: diff did not apply: {exc}"
                failure = _carrying(own, gate_failure)
                # A retry's apply-failure is recorded HERE, in the
                # attempt, while its own sample reads "retry draw, gated
                # in place" -- a filter on sample outcome misses it
                # (F21.38a). The reconstruction rides with the failure so
                # both paths carry it.
                _seal_attempt(
                    journal_path,
                    node.id,
                    run_span_id,
                    ctx,
                    1,
                    own,
                    {
                        **_proposal_evidence(proposal),
                        **_reconstruction_evidence(proposal.diff),
                        "samples": samples,
                    },
                )
                continue
            applied.append(proposal.diff)
            autofix(workdir, baseline=baseline, recorder=recorder)
            # T6-34: the tree the gate is about to judge, named before it
            # runs. A failed attempt otherwise leaves nothing a `git gc`
            # cannot prune, and the sidecar's diff is the pre-autofix text,
            # so the graded tree was recoverable only from dangling blobs.
            gated_tree = snapshot_tree(workdir, attempt_ref(node.id, attempt), recorder=recorder)
            captured: list[CapturedRun] = []
            result = run_node_gate(
                node,
                workdir,
                baseline=baseline,
                recorder=recorder,
                capture=captured,
                planned_requirements=planned,
                owed_tests=owed,
            )
            # Rule D after the gates (P2-3), only with `--rule-d` and only on a
            # tree every gate passed: the gates judge the diff, rule D the
            # function's answers against the references and the answer book.
            decision = rule_d(node.kind, workdir) if rule_d is not None and result.passed else None
            if decision is not None and decision.halt:
                _seal_attempt(
                    journal_path,
                    node.id,
                    run_span_id,
                    ctx,
                    2,
                    f"attempt {attempt}/{max_attempts}: halted on a rule D question",
                    {
                        **_proposal_evidence(proposal),
                        "samples": samples,
                        "tree": gated_tree,
                        "rule_d": dict(decision.evidence),
                    },
                )
                raise NodeQuestionError(node.id, decision.detail, attempt)
            if decision is not None and decision.refuse:
                result = replace(
                    result,
                    passed=False,
                    checks=(*result.checks, GateCheck("rule-d", False, decision.detail)),
                )
            if result.passed:
                # The tree the gate just passed on, sealed into the record
                # and kept at its own ref (T3-10): a later resume has to
                # land on this tree, and `git restore --source` is how the
                # user puts it back when it does not.
                tree = snapshot_tree(workdir, proven_ref(node.id), recorder=recorder)
                parents = [proofs[dep] for dep in node.dependencies]
                record = build_from_gate(
                    node,
                    "\n".join(applied),
                    result,
                    parents,
                    f"{node.id}#{attempt}",
                    thinking=proposal.reasoning,
                    attempts=attempt,
                    task_hash=task_hash,
                    tree_hash=tree,
                )
                append_record(journal_path, record)
                proofs[node.id] = record.record_hash
                detail = sampling if attempt == 1 else f"recovered after {attempt} attempts"
                _seal_attempt(
                    journal_path,
                    node.id,
                    run_span_id,
                    ctx,
                    0,
                    detail,
                    {
                        **_proposal_evidence(proposal),
                        "samples": samples,
                        "tree": gated_tree,
                        **({"rule_d": dict(decision.evidence)} if decision is not None else {}),
                    },
                )
                return Proof(node_id=node.id)
            last_result = result
            failure = format_attempt_failure(
                result,
                captured,
                attempt=attempt,
                max_attempts=max_attempts,
                tools=node.execution_constraints.allowed_tools,
            )
            gate_failure = failure
            # The seal names the failed gates in check order (T3-25): a
            # failed node has no proof record and the transcript renders
            # its last attempt only, so the journal is the one place an
            # earlier attempt's verdict can be read back from.
            failed_names = [check.name for check in result.checks if not check.passed]
            _seal_attempt(
                journal_path,
                node.id,
                run_span_id,
                ctx,
                1,
                f"attempt {attempt}/{max_attempts}: {len(failed_names)} gate(s) failed"
                f": {', '.join(failed_names)}",
                {
                    **_proposal_evidence(proposal),
                    "samples": samples,
                    "tree": gated_tree,
                    "gates": [
                        {"name": check.name, "passed": check.passed, "detail": check.detail}
                        for check in result.checks
                    ],
                    **({"rule_d": dict(decision.evidence)} if decision is not None else {}),
                },
            )
            # A refusal is the tree's one verdict unless the run opted into
            # repair (`--rule-d-retry`): no second attempt, the node fails.
            if decision is not None and decision.refuse and not decision.retry:
                break
        except NodeQuestionError:
            _abandon(workdir, baseline, applied, recorder)
            raise
        except _HaltRecoveryError as exc:
            _abandon(workdir, baseline, applied, recorder)
            if exc.result is None:
                detail = f"identical diff re-proposed after {exc.attempts} non-applying attempt(s)"
                raise NodeUnappliableError(node.id, detail, exc.attempts, exc.failure) from exc
            raise NodeGateFailedError(
                exc.result, exc.attempts, exc.failure, applied=applied, baseline=baseline
            ) from exc
        except BaseException as exc:
            ctx.prompt_hash = ctx.prompt_hash or _prompt_hash(
                str(_call_evidence(exc).get("prompt", ""))
            )
            # A failure the loop does not retry (a transport error, a
            # cancelled task, a bug) still fails the node, and a failed
            # node leaves the tree at its baseline: round 3c's n2.r2 timed
            # out after applying 530 lines and the oracle graded them
            # unproven (F21.12b). Seal first so the restore span hangs off
            # the attempt that failed, as on the other give-up paths.
            _seal_attempt(
                journal_path,
                node.id,
                run_span_id,
                ctx,
                1,
                str(exc),
                {**_error_evidence(exc), "samples": samples},
            )
            _abandon(workdir, baseline, applied, recorder)
            raise
    # `recorder` is the last attempt's: the restore span hangs off the
    # attempt that failed, so the journal shows when the tree was reset.
    _abandon(workdir, baseline, applied, recorder)
    if last_result is None:
        detail = f"no proposed diff applied in {attempt} attempts"
        raise NodeUnappliableError(node.id, detail, attempt, failure)
    raise NodeGateFailedError(last_result, attempt, failure, applied=applied, baseline=baseline)


def splice_replan(
    dag: Dag, failed_id: str, new: Dag, *, taken: Collection[str] = ()
) -> tuple[Dag, list[str]]:
    """Replace `failed_id` with `new`'s nodes, rewiring dependents to new leaves.

    New ids are namespaced under the failed id and numbered past every id
    the DAG or `taken` already holds (T3-11): a resumed run whose journal
    sealed `n2.r1` replans `n2` as `n2.r2`, so one journal never carries
    two records under one id and `rebuild_proven`'s last-record-wins
    cannot shadow a proof. New roots inherit the failed node's
    dependencies. The failed node itself stays put so the transcript
    keeps its verdict. Acyclicity survives splicing: every new edge runs
    from proven nodes to new nodes, or new nodes to downstream nodes.
    """
    existing = {node.id for node in dag.nodes} | set(taken)
    mapping: dict[str, str] = {}
    for node in new.nodes:
        index = 1
        candidate = f"{failed_id}.r{index}"
        while candidate in existing:
            index += 1
            candidate = f"{failed_id}.r{index}"
        mapping[node.id] = candidate
        existing.add(candidate)
    internal = {node.id for node in new.nodes}
    for node in new.nodes:
        for dep in node.dependencies:
            if dep not in internal:
                msg = f"replan node {node.id!r} depends on unknown node {dep!r}"
                raise ReplanFailedError(msg)
    failed = next(node for node in dag.nodes if node.id == failed_id)
    remapped = [
        node.model_copy(
            update={
                "id": mapping[node.id],
                "dependencies": (
                    [mapping[dep] for dep in node.dependencies]
                    if node.dependencies
                    else list(failed.dependencies)
                ),
            }
        )
        for node in new.nodes
    ]
    leaves = {node.id for node in remapped} - {
        dep for node in remapped for dep in node.dependencies
    }
    if not leaves:
        msg = f"replan for node {failed_id!r} has no leaf nodes"
        raise ReplanFailedError(msg)
    merged = []
    for node in dag.nodes:
        if failed_id not in node.dependencies:
            merged.append(node)
            continue
        deps: list[str] = []
        for dep in node.dependencies:
            deps.extend(sorted(leaves) if dep == failed_id else [dep])
        merged.append(node.model_copy(update={"dependencies": deps}))
    merged.extend(remapped)
    return Dag(nodes=merged), [node.id for node in remapped]


def format_replan_history(node_id: str, error: NodeGateFailedError | NodeUnappliableError) -> str:
    """One-paragraph failure history for a replan prompt."""
    if isinstance(error, NodeGateFailedError):
        failed = [check for check in error.result.checks if not check.passed]
        lines = [f"Node {node_id!r} failed Tier-1 after {error.attempts} attempt(s):"]
        lines.extend(f"- {check.name}: {check.detail}" for check in failed)
    else:
        lines = [f"Node {node_id!r} produced no applicable diff after {error.attempts} attempt(s)."]
    if error.failure is not None:
        lines.extend(["Last attempt evidence:", error.failure])
    return "\n".join(lines) + "\n"


def _schedulable_nodes(
    dag: Dag, proofs: dict[str, str], ever_failed: dict[str, BaseException]
) -> list[Node]:
    """Nodes ready to (re)schedule: unproven, never failed, unblocked.

    Proven dependencies strip out since their proofs already exist;
    anything behind a failure stays out so proof-gating holds.
    """
    ready = []
    for node in dag.nodes:
        if node.id in proofs or node.id in ever_failed:
            continue
        if any(dep in ever_failed for dep in node.dependencies):
            continue
        ready.append(
            node.model_copy(
                update={"dependencies": [dep for dep in node.dependencies if dep not in proofs]}
            )
        )
    return ready


def _transcribe(
    node: Node,
    sealed: ProofRecord | None,
    failure: BaseException | None,
    tool_spans: tuple[SpanRecord, ...],
) -> NodeTranscript:
    """One node's transcript row from its sealed record or its failure."""
    if sealed is not None:
        checks = tuple(
            GateCheck(name=output.name, passed=output.passed, detail=output.detail)
            for output in sealed.gate_outputs
        )
        return NodeTranscript(
            node.id,
            tuple(node.requirement_ids),
            checks,
            sealed.record_hash,
            thinking=sealed.thinking,
            tool_spans=tool_spans,
            attempts=sealed.attempts,
        )
    if isinstance(failure, NodeGateFailedError):
        checks = failure.result.checks
        attempts = failure.attempts
    elif isinstance(failure, NodeUnappliableError):
        checks = ()
        attempts = failure.attempts
    elif isinstance(failure, NodeQuestionError):
        checks = (GateCheck("rule-d", False, failure.question),)
        attempts = failure.attempts
    else:
        checks = ()
        attempts = 1
    return NodeTranscript(
        node.id, tuple(node.requirement_ids), checks, None, tool_spans=tool_spans, attempts=attempts
    )


def _seed_proofs(
    journal_path: Path, dag: Dag, task_hash: str
) -> tuple[dict[str, str], str | None, ProofRecord | None]:
    """Which journalled proofs this run may reuse, and what it decided.

    A proof is reusable only for the task and the node it was sealed
    against (T3-9): a journal from task A run against task B's DAG used to
    count every same-id node as proven and never schedule it, and the CLI
    default journal is per repo, so that was the default flow for a second
    task. A record sealed for a different task is fatal -- the run stops
    before anything executes and the user picks a fresh `--journal` path.
    A record whose node no longer hashes the same, or that predates these
    fields, is simply not reused: the node is scheduled again. A record
    for an id the DAG does not contain (an earlier run's replacement) is
    dropped too, and named as such (T3-11).

    Returns the seed, a one-line summary (`None` when the journal held no
    proof and there was nothing to decide), and the last record actually
    reused -- whose `tree_hash` says which worktree the caller must be
    resuming onto (T3-10).
    """
    candidates = list(proven_records(journal_path).values())
    expected = {node.id: hash_node(node) for node in dag.nodes}
    for record in candidates:
        if record.task_hash and record.task_hash != task_hash:
            msg = (
                f"journal {str(journal_path)!r} was sealed for a different task: "
                f"node {record.node_id!r} carries task_hash {record.task_hash}, "
                f"this run's task hashes to {task_hash}. "
                "Pass a fresh --journal path to run a new task."
            )
            raise ValueError(msg)
    proofs: dict[str, str] = {}
    decided: list[str] = []
    reused: ProofRecord | None = None
    for record in candidates:
        if record.node_hash == expected.get(record.node_id):
            proofs[record.node_id] = record.record_hash
            reused = record
            decided.append(f"reused {record.node_id}")
        elif record.node_id not in expected:
            # A replan's leftover from an earlier run (T3-11): the DAG being
            # run has no node to compare the hash with, so it is not proof
            # of anything scheduled here -- and the reader should not
            # mistake it for an edited node.
            decided.append(f"dropped {record.node_id} (not in DAG)")
        elif not record.node_hash:
            # Sealed before the node was named in the record: nothing says
            # it proved this DAG's node, so it does not count as proof.
            decided.append(f"dropped {record.node_id} (no node hash)")
        else:
            decided.append(f"dropped {record.node_id} (node changed)")
    return proofs, "; ".join(decided) if candidates else None, reused


# --- T6-29c: survivor-driven test node ---------------------------------------

TestDrawer = Callable[[Node, str, int], DiffProposal]
"""Draw one candidate test file for a node from a brief, at a seed (T6-29c)."""

SURVIVOR_SAMPLES: Final = 10
"""Candidate draws per survivor round (T6-29a's k); `run_slice` takes it as a parameter."""

SURVIVOR_ROUNDS: Final = 2
"""Survivor rounds per failed node's lineage: a third is never started."""

SURVIVOR_GATES: Final = frozenset({"coverage", "mutation"})
"""The gates a new test can answer; a node failing any other gate retries as before."""

_TOP_LEVEL_START = re.compile(r"^(?:async def |def |class |@)")


def _created_file(diff: str) -> tuple[str, str] | None:
    """(path, text) of a diff that creates exactly one file; None otherwise.

    The cardinality bound the brief states (one section, one hunk): a
    draw that modifies a file, creates two, or carries context lines is
    not one new test file, and only the created text is kept -- the path
    the worker chose is replaced by the round's own (`_candidate_path`).
    """
    lines = diff.splitlines()
    if sum(line.startswith("diff --git ") for line in lines) != 1 or not lines[0].startswith(
        "diff --git "
    ):
        return None
    hunk = next((i for i, line in enumerate(lines) if line.startswith("@@ ")), None)
    if hunk is None:
        return None
    head = lines[1:hunk]
    target = next((line.removeprefix("+++ b/") for line in head if line.startswith("+++ b/")), None)
    if (
        target is None
        or "--- /dev/null" not in head
        or not any(line.startswith("new file mode ") for line in head)
    ):
        return None
    body = lines[hunk + 1 :]
    if any(not line.startswith(("+", "\\")) for line in body):
        return None
    return target, "".join(f"{line[1:]}\n" for line in body if line.startswith("+"))


def _cut_to_last_test(source: str) -> str | None:
    """`source` cut back to its last complete top-level definition.

    F21.14: a capped draw stops mid-function, and a syntax error must not
    score as "fails against the stub". The longest prefix ending at a
    top-level `def`, `class` or decorator that parses is kept, provided
    it still holds a test function; a file that never parses, or parses
    without one, yields None and the draw is dropped as unparseable.
    """
    lines = source.splitlines(keepends=True)
    starts = [i for i, line in enumerate(lines) if _TOP_LEVEL_START.match(line)]
    for cut in (len(lines), *reversed(starts)):
        text = "".join(lines[:cut]).rstrip("\n") + "\n"
        try:
            tree = ast.parse(text)
        except SyntaxError:
            continue
        has_test = any(
            isinstance(statement, ast.FunctionDef | ast.AsyncFunctionDef)
            and statement.name.startswith("test_")
            for statement in tree.body
        )
        return text if has_test else None
    return None


def _create_diff(path: str, source: str) -> str:
    """A creation diff for `source` at `path`, in the shape the grammar admits (T6-32)."""
    lines = source.splitlines()
    return (
        f"diff --git a/{path} b/{path}\n"
        "new file mode 100644\n"
        "--- /dev/null\n"
        f"+++ b/{path}\n"
        f"@@ -0,0 +1,{len(lines)} @@\n" + "".join(f"+{line}\n" for line in lines)
    )


def _candidate_path(node: Node, round_no: int, seed: int) -> str:
    """Where a round's candidate lands: beside the node's declared tests.

    T6-29b names candidates `tests/test_<req>_s<seed>.py`; the directory
    follows the first test file the node's gate command runs, so a flat
    suite's candidate imports what that suite imports. The round is part
    of the name: a second round's files never collide with the first's.
    """
    scope = [
        arg for arg in pytest_scope(node.deterministic_gate.test_command) if arg.endswith(".py")
    ]
    directory = PurePosixPath(scope[0]).parent if scope else PurePosixPath("tests")
    name = PurePosixPath(candidate_test_path(f"{node.requirement_ids[0]}_r{round_no}", seed)).name
    return (directory / name).as_posix()


def _sealed_test_names(
    journal_path: Path,
    dag: Dag,
    proofs: Mapping[str, str],
    workdir: Path,
    *,
    exclude: Collection[str] = (),
) -> set[str]:
    """Every identifier the test files sealed test nodes of this run wrote.

    The sidecars of a proven test node's attempts retain its diffs
    (T6-27); the files those diffs created or changed, as they stand in
    the worktree, are the run's own specification. A gap inside a
    function one of them names is that specification's miss, not a hole
    no test was ever asked to fill, and the node retries as before.
    `exclude` names the test nodes survivor rounds themselves spliced:
    a second round is judged against the retry's own gap, not barred by
    the first round's file naming the same function.
    """
    test_nodes = {
        node.id
        for node in dag.nodes
        if node.kind == "test" and node.id in proofs and node.id not in exclude
    }
    paths: set[str] = set()
    for span in read_spans(journal_path):
        if span.kind != "agent" or span.node_id not in test_nodes:
            continue
        sidecar = json.loads(attempt_sidecar_path(journal_path, span.span_id).read_text())
        for line in str(sidecar.get("diff", "")).splitlines():
            if line.startswith("+++ b/"):
                paths.add(line.removeprefix("+++ b/"))
    names: set[str] = set()
    for rel in sorted(paths):
        target = workdir / rel
        if target.is_file():
            names.update(re.findall(r"[A-Za-z_]\w*", target.read_text()))
    return names


def _survivor_gap(node: Node, exc: BaseException) -> bool:
    """Whether `exc` is a coverage or mutation miss a new test could answer."""
    if node.kind != "impl" or not isinstance(exc, NodeGateFailedError):
        return False
    failed = {check.name for check in exc.result.checks if not check.passed}
    return bool(failed) and failed <= SURVIVOR_GATES and bool(exc.result.gaps)


def _recovered_tree(workdir: Path, applied: Sequence[str], baseline: str, dest: Path) -> None:
    """Rebuild the tree the gate judged: the node's diffs on its baseline, autofixed."""
    shutil.copytree(workdir, dest, symlinks=True)
    run_argv(["git", "update-index", "--refresh"], dest)
    for diff in applied:
        _write_files(dest, diff)
    autofix(dest, baseline=baseline)


def _candidate_runner(real_tree: Path, baseline: str, node: Node) -> CandidateRunner:
    """T6-29b's injected runner: pytest on either tree, the sample on the real one.

    The candidate file alone runs under coverage; against `real_tree` a
    green run also re-runs `mutation_sample` over the node's changed
    lines with pytest scoped to that file, scoring every decided mutant
    on a changed line (P0-8), so the survivors it still reports are the
    ones the candidate failed to kill. The stub tree contributes its
    exit code and nothing else.
    """
    changed = changed_statements(real_tree, git_diff(real_tree, baseline))
    changed_files = sorted({path for path, _ in changed})
    ceiling = node.deterministic_gate.mutation_sample.max_mutants

    def run(tree: Path, candidate: str) -> CandidateRun:
        drop_test_caches(tree)
        data_file = str(tree / ".coverage.candidate")
        ran = run_shell_capture(under_coverage(f"pytest {candidate}", data_file), tree)
        if ran.exit_code != 0 or tree != real_tree:
            return CandidateRun(exit_code=ran.exit_code)
        covered = covered_lines(data_file, changed_files)
        tests = read_sources(tree, "test_*.py") | read_sources(tree, "*_test.py")
        outcome = mutation_sample(tree, changed, ceiling, test_files=tests, run_tests=(candidate,))
        return CandidateRun(
            exit_code=0, survivors=outcome.survivors, covered=tuple(sorted(covered))
        )

    return run


def _survivor_round(
    node: Node,
    exc: NodeGateFailedError,
    *,
    workdir: Path,
    journal_path: Path,
    run_span_id: str,
    draw: TestDrawer,
    samples: int,
    round_no: int,
    requirements_text: Mapping[str, str],
    sealed_names: Collection[str],
) -> tuple[Dag, DiffProposal] | None:
    """One survivor round: brief, draw k, filter, and plan the splice.

    The tree the gate judged is rebuilt in a scratch copy and stubbed
    (T6-29b); the brief names the untested functions and the modules'
    signatures, never their bodies. k draws go out together, each with
    its own seed, and are judged in seed order: a draw that is not one
    created file, does not parse (cut back to its last complete test
    first), cites no requirement, or repeats an earlier draw is dropped
    before anything runs; the rest pass through `keep_candidate`. A
    candidate red on the real tree is dropped and recorded, never handed
    to the impl node as a brief (F21.14: three of ten such draws were
    simply wrong). Every verdict is journaled as one `survivor-tests`
    tool span under the failed node.

    Returns the replacement subgraph -- a `test` node scoped to the kept
    files, then the impl node again with those files in its gate's
    scope -- and the union diff the test node applies, or None when
    nothing was kept or the gap sits in a function a sealed test of
    this run already names.
    """
    baseline = exc.baseline
    assert baseline is not None  # `_survivor_gap` admits gate failures only
    start = perf_counter()
    with tempfile.TemporaryDirectory(prefix="saddle-survivor-") as tmp:
        real = Path(tmp) / "real"
        _recovered_tree(workdir, exc.applied, baseline, real)
        gaps_rel = [
            (Path(path).relative_to(workdir).as_posix(), line) for path, line in exc.result.gaps
        ]
        functions = enclosing_functions(real, (), gaps_rel, baseline=baseline)
        if any(
            name.rpartition(".")[2] in sealed_names
            for names in functions.values()
            for name in names
        ):
            return None
        stub = stubbed_sandbox(real, sorted(functions))
        try:
            stubs = {rel: (stub / rel).read_text() for rel in sorted(functions)}
            conventions = read_sources(real, "test_*.py") | read_sources(real, "*_test.py")
            named = _candidate_path(node, round_no, 0)
            brief = build_survivor_brief(node, requirements_text, stubs, functions, conventions) + (
                f"\nCardinality: one section, one hunk: a single diff creating {named}.\n"
            )

            def one(seed: int) -> DiffProposal | VllmError:
                # Per-draw, transport failures included (T6-73): the
                # siblings are independent candidates and a timeout in
                # one is not evidence about the rest.
                try:
                    return draw(node, brief, seed)
                except (VllmResponseError, VllmRequestError) as error:
                    return error

            with ThreadPoolExecutor(max_workers=samples) as pool:
                draws = list(pool.map(one, range(samples)))
            gaps_real = tuple((str(real / rel), line) for rel, line in gaps_rel)
            runner = _candidate_runner(real, baseline, node)
            kept: list[tuple[str, str]] = []
            seen: dict[str, int] = {}
            verdicts: list[str] = []
            for seed, outcome in enumerate(draws):
                path = _candidate_path(node, round_no, seed)
                verdict = _judge_candidate(
                    outcome, path, seen, seed, node, stub, real, exc, gaps_real, runner
                )
                if verdict[0] == "kept":
                    kept.append((path, verdict[2]))
                verdicts.append(f"s{seed}: {verdict[0]}: {verdict[1]}")
        finally:
            shutil.rmtree(stub, ignore_errors=True)
    SpanRecorder(path=journal_path, node_id=node.id, parent_id=run_span_id).record(
        argv=[
            "survivor-tests",
            node.id,
            f"round={round_no}",
            f"drawn={samples}",
            f"kept={len(kept)}",
            f"dropped={samples - len(kept)}",
        ],
        duration_ms=_elapsed_ms(start),
        exit_code=0 if kept else 1,
        detail="; ".join(verdicts),
        name="survivor-tests",
    )
    if not kept:
        return None
    paths = [path for path, _ in kept]
    gate = node.deterministic_gate
    constraints = node.execution_constraints
    tests_node = node.model_copy(
        update={
            "id": "tests",
            "kind": "test",
            "dependencies": [],
            "task_prompt": brief,
            "target_files": paths,
            "deterministic_gate": gate.model_copy(
                update={"test_command": "pytest " + " ".join(paths)}
            ),
            "execution_constraints": constraints.model_copy(
                update={"allowed_tools": sorted({*constraints.allowed_tools, "write_file"})}
            ),
        }
    )
    impl_node = node.model_copy(
        update={
            "id": "impl",
            "dependencies": ["tests"],
            "deterministic_gate": gate.model_copy(
                update={"test_command": f"{gate.test_command} {' '.join(paths)}"}
            ),
        }
    )
    union = "".join(_create_diff(path, source) for path, source in kept)
    return Dag(nodes=[tests_node, impl_node]), DiffProposal(union, "")


def _judge_candidate(
    outcome: DiffProposal | VllmError,
    path: str,
    seen: dict[str, int],
    seed: int,
    node: Node,
    stub: Path,
    real: Path,
    exc: NodeGateFailedError,
    gaps_real: tuple[tuple[str, int], ...],
    runner: CandidateRunner,
) -> tuple[str, str, str]:
    """(decision, detail, source) for one draw; parse-first, then T6-29b's filters."""
    if isinstance(outcome, VllmError):
        return "dropped", f"worker call failed: {outcome}", ""
    created = _created_file(outcome.diff)
    if created is None:
        return "dropped", "not one created file", ""
    source = _cut_to_last_test(created[1])
    if source is None:
        return "dropped", "does not parse", ""
    if not any(rid in source for rid in node.requirement_ids):
        return "dropped", "cites no requirement id", ""
    earlier = seen.get(source)
    if earlier is not None:
        return "dropped", f"identical to sample {earlier}", ""
    seen[source] = seed
    for tree in (stub, real):
        (tree / path).parent.mkdir(parents=True, exist_ok=True)
        (tree / path).write_text(source)
    verdict = keep_candidate(stub, real, path, exc.result.survivors, gaps_real, run=runner)
    for tree in (stub, real):
        (tree / path).unlink()
    if verdict.decision == "kept":
        return "kept", verdict.detail, source
    return "dropped", verdict.detail, ""


def _prepared(proposal: DiffProposal) -> Proposer:
    """A proposer that answers every draw with the diff a survivor round kept."""
    return lambda _node, _failure, _seed: proposal


def _schedule_until_done(
    dag: Dag,
    *,
    replan: Replanner | None,
    workdir: Path,
    journal_path: Path,
    propose: Proposer,
    proofs: dict[str, str],
    run_span_id: str,
    task_hash: str,
    deadline: _Deadline | None = None,
    survivor_draw: TestDrawer | None = None,
    survivor_samples: int = SURVIVOR_SAMPLES,
    rule_d: RuleDCheck | None = None,
) -> tuple[Dag, dict[str, BaseException], set[str], bool]:
    """Run the schedule/replan loop until no node can progress further.

    Returns the final DAG (after any replan splices), every node's
    terminal failure, the set of node ids replanned away (excused from
    `failed_unexcused`), and whether the deadline stopped the run (T6-9):
    a node is not started when the time left is under the median node
    wall so far, or gone; a node that was skipped for that reason is
    undispatched, not failed, and nothing is replanned past the deadline.
    """
    remaining = dag
    replanned_from: set[str] = set()
    generated: set[str] = set()
    ever_failed: dict[str, BaseException] = {}
    deadline_hit = False
    # Survivor rounds (T6-29c): the diff each spliced test node applies,
    # which original node a retried impl node descends from, and how many
    # rounds that lineage has had.
    prepared: dict[str, DiffProposal] = {}
    roots: dict[str, str] = {}
    rounds: dict[str, int] = {}
    requirements_text = {
        requirement.id: requirement.statement
        for node in dag.nodes
        for requirement in node.requirements
    }

    async def worker(node: Node, _constraints: ExecutionConstraints) -> Proof:
        if deadline is not None and not deadline.room_for_node():
            raise _DeadlineSkipError(node.id)
        # The scheduler sees a copy with proven dependencies stripped; the
        # proof record must cite every parent, so run the original node.
        original = next(candidate for candidate in remaining.nodes if candidate.id == node.id)
        # From the plan as it stands: a replacement node's ids are planned
        # too, and a replaced node's are not (T3-24).
        planned = planned_requirement_ids(remaining)
        # T6-53: what the plan still owes tests from, as it stands. A node
        # already proven writes nothing further, so a line only defers
        # while some node that may write tests has yet to run.
        owed = pending_test_nodes(remaining, proofs)
        started = deadline.clock() if deadline is not None else 0.0
        try:
            return await _run_node(
                original,
                workdir,
                journal_path,
                _prepared(prepared[node.id]) if node.id in prepared else propose,
                proofs,
                run_span_id,
                planned,
                owed,
                task_hash=task_hash,
                deadline=deadline,
                rule_d=rule_d,
            )
        finally:
            if deadline is not None:
                deadline.walls.append(deadline.clock() - started)

    while True:
        ready = _schedulable_nodes(remaining, proofs, ever_failed)
        if not ready:
            # Only reachable on resume: every node already proven (or
            # blocked). A fresh DAG always has at least one root to run.
            break
        schedulable = Dag(nodes=ready)
        outcome = asyncio.run(schedule(schedulable, worker))
        skipped = {n for n, exc in outcome.failures.items() if isinstance(exc, _DeadlineSkipError)}
        deadline_hit = deadline_hit or bool(skipped)
        ever_failed.update({n: exc for n, exc in outcome.failures.items() if n not in skipped})
        if deadline_hit or (deadline is not None and deadline.expired()):
            deadline_hit = True
            break
        by_id = {node.id: node for node in remaining.nodes}
        progressed = False  # Observed only via `not`; falsy-init mutants are equivalent.
        recovered: set[str] = set()
        if survivor_draw is not None:
            sealed_names = _sealed_test_names(
                journal_path, remaining, proofs, workdir, exclude=prepared
            )
            for node_id, exc in outcome.failures.items():
                root = roots.get(node_id, node_id)
                if (
                    node_id in skipped
                    or rounds.get(root, 0) >= SURVIVOR_ROUNDS
                    or not isinstance(exc, NodeGateFailedError)
                    or not _survivor_gap(by_id[node_id], exc)
                ):
                    continue
                spliced = _survivor_round(
                    by_id[node_id],
                    exc,
                    workdir=workdir,
                    journal_path=journal_path,
                    run_span_id=run_span_id,
                    draw=survivor_draw,
                    samples=survivor_samples,
                    round_no=rounds.get(root, 0) + 1,
                    requirements_text=requirements_text,
                    sealed_names=sealed_names,
                )
                if spliced is None:
                    continue
                new, proposal = spliced
                taken = {record.node_id for record in read_records(journal_path)}
                remaining, gen_ids = splice_replan(remaining, node_id, new, taken=taken)
                # The whole post-replan plan, not just the generated nodes:
                # `splice_replan` rewires every dependent of the failed node
                # onto the new leaves, which changes their `hash_node` too. A
                # record holding only `gen_ids` leaves those rewired survivors
                # unplanned, and T6-27's check then reads their proofs as
                # `unplanned-proof` (T6-72). `verify` accumulates planned
                # hashes across records, so restating the unchanged nodes is
                # free.
                append_plan(
                    journal_path,
                    build_plan(remaining.nodes, task_hash=task_hash, replaces=node_id),
                )
                tests_id, impl_id = gen_ids
                prepared[tests_id] = proposal
                roots[impl_id] = root
                rounds[root] = rounds.get(root, 0) + 1
                # Replaced, like a replanned node: its failure is excused
                # and the verdict rests on the nodes that stand in for it.
                recovered.add(node_id)
                replanned_from.add(node_id)
                generated.update(gen_ids)
                progressed = True
        if replan is None:
            if not progressed:
                break
            continue
        eligible: dict[str, NodeGateFailedError | NodeUnappliableError] = {}
        for node_id, exc in outcome.failures.items():
            if (
                isinstance(exc, (NodeGateFailedError, NodeUnappliableError))
                and node_id not in replanned_from
                and node_id not in generated
                and node_id not in recovered
            ):
                eligible[node_id] = exc
        if not eligible and not progressed:
            break
        for node_id, exc in eligible.items():
            try:
                new = replan(
                    by_id[node_id],
                    format_replan_history(node_id, exc),
                    reserved_target_files(remaining, proofs, replacing=node_id),
                )
                taken = {record.node_id for record in read_records(journal_path)}
                remaining, gen_ids = splice_replan(remaining, node_id, new, taken=taken)
            except ReplanFailedError:
                continue
            # The whole post-replan plan: a rewired dependent's hash moved
            # too, and a record naming only `gen_ids` leaves it unplanned
            # (T6-72).
            append_plan(
                journal_path,
                build_plan(remaining.nodes, task_hash=task_hash, replaces=node_id),
            )
            replanned_from.add(node_id)
            generated.update(gen_ids)
            progressed = True
        if not progressed:
            break
    return remaining, ever_failed, replanned_from, deadline_hit


def _merge_gate(
    merge_command: str | None,
    *,
    workdir: Path,
    proofs: dict[str, str],
    run_span_id: str,
    journal_path: Path,
) -> tuple[int, bool]:
    """Run the unscoped merge-time suite once, after every node's own gate.

    Only runs when at least one node was proven (ARCHITECTURE Tier 2,
    #60); a non-zero exit fails the run without revisiting any per-node
    verdict.
    """
    merge_exit = 0
    merge_ran = merge_command is not None and bool(proofs)
    if merge_command is not None and proofs:
        merge_start = perf_counter()
        merge_started_at = _utcnow()
        merge_exit = run_shell(merge_command, workdir)
        timed_out = " (timed out)" if merge_exit == SHELL_TIMEOUT else ""
        append_span(
            journal_path,
            build_span(
                node_id="",
                argv=shlex.split(merge_command),
                duration_ms=_elapsed_ms(merge_start),
                exit_code=merge_exit,
                detail=f"merge-time full suite: exit {merge_exit}{timed_out}",
                name="merge-suite",
                parent_id=run_span_id,
                started_at=merge_started_at,
            ),
        )
    return merge_exit, merge_ran


def _seal_run(
    remaining: Dag,
    *,
    journal_path: Path,
    proofs: dict[str, str],
    ever_failed: dict[str, BaseException],
    replanned_from: set[str],
    run_span_id: str,
    run_start: float,
    merge_exit: int,
    merge_ran: bool,
    task: str,
    started: str,
    now: Callable[[], str],
    deadline_hit: bool = False,
    settings: Mapping[str, str] | None = None,
) -> SliceResult:
    """Write the run's terminal span and verdict, and render its transcript.

    `settings` (T6-27) are the run's knobs the journal never held -- served
    model and server version, temperatures, context window -- sealed as
    the run span's argv so rounds can be compared on more than faith.

    A run the deadline stopped (T6-9) seals exit code 3 and says so in
    the span, so `verify` and the transcript distinguish "ran out of
    time" from "failed"; proofs already sealed stay sealed and a later
    `saddle run` on the same journal resumes them.
    """
    sealed = {record.node_id: record for record in read_records(journal_path)}
    tools = tool_spans_by_node(read_spans(journal_path))
    transcripts = tuple(
        _transcribe(
            node,
            sealed.get(node.id),
            ever_failed.get(node.id),
            tool_spans_for_node(tools, node.id),
        )
        for node in remaining.nodes
    )
    failed_unexcused = {node_id for node_id in ever_failed if node_id not in replanned_from}
    undispatched = {node.id for node in remaining.nodes} - set(proofs) - set(ever_failed)
    passed = not failed_unexcused and not undispatched and merge_exit == 0
    # QUESTION only when every unexcused failure is a rule D halt: a gate
    # failure anywhere, or a red merge suite, keeps the run FAIL. Nodes left
    # undispatched can only be waiting on the halted ones, since nothing
    # else failed.
    asked = {n for n in failed_unexcused if isinstance(ever_failed[n], NodeQuestionError)}
    # A deadline outranks it, as it outranks FAIL: the run did not finish.
    question = bool(asked) and asked == failed_unexcused and merge_exit == 0 and not deadline_hit
    if deadline_hit:
        exit_code = DEADLINE_EXIT
    elif passed:
        exit_code = 0
    elif question:
        exit_code = QUESTION_EXIT
    else:
        exit_code = 1
    append_span(
        journal_path,
        build_span(
            node_id="",
            argv=["run", *(f"{key}={value}" for key, value in sorted((settings or {}).items()))],
            duration_ms=_elapsed_ms(run_start),
            exit_code=exit_code,
            started_at=started,
            detail=(
                ("deadline: " if deadline_hit else "") + f"{len(proofs)} proven, "
                f"{len(failed_unexcused) - len(asked)} failed, "
                + (f"{len(asked)} halted on a question, " if asked else "")
                + f"{len(undispatched)} undispatched"
                + (f", merge exit {merge_exit}" if merge_ran else "")
            ),
            kind="agent",
            name="run",
            span_id=run_span_id,
        ),
    )
    tail = [issue for issue in verify_journal(journal_path) if issue.code == "torn-tail"]
    if tail:
        msg = f"journal {str(journal_path)!r} has a torn tail after our own writes"
        raise RuntimeError(msg)
    text = render_transcript(
        RunTranscript(
            task=task,
            started=started,
            finished=now(),
            verdict="PASS" if passed else ("QUESTION" if question else "FAIL"),
            nodes=transcripts,
            journal_path=str(journal_path),
        )
    )
    return SliceResult(
        passed=passed,
        transcript=text,
        proofs=proofs,
        deadline_hit=deadline_hit,
        question=question,
    )


def run_slice(
    task: str,
    dag: Dag,
    *,
    workdir: Path,
    journal_path: Path,
    propose: Proposer,
    replan: Replanner | None = None,
    merge_command: str | None = MERGE_COMMAND,
    now: Callable[[], str] = _utcnow,
    deadline_s: float | None = None,
    clock: Callable[[], float] = perf_counter,
    settings: Mapping[str, str] | None = None,
    survivor_draw: TestDrawer | None = None,
    survivor_samples: int = SURVIVOR_SAMPLES,
    rule_d: RuleDCheck | None = None,
) -> SliceResult:
    """Run one validated DAG through gates and journal; return its transcript.

    A journal that verifies is resumed, not refused (T3-1): its proven
    nodes seed `proofs`, so only unproven nodes are scheduled and a crash
    loses at most the in-flight node, as `proven_records` promises. What
    a proof is a proof *of* bounds that reuse (T3-9): a record sealed for
    another task raises before any node runs, a record whose node has
    changed is dropped and the node scheduled again, and when the journal
    held any proof the decision is sealed as a `resume` span under the run
    span so the transcript and `saddle tail` show it. So does the tree the
    proof was sealed on (T3-10): if any proof is reused, the tracked
    worktree must hash to the `tree_hash` of the last one, or the run
    raises naming both ids and the `git restore` that puts the proven tree
    back. A journal that does not verify raises before anything runs.

    When `replan` is given, each exhausted node recompiles once into a
    replacement subgraph; replanned nodes that fail again stay failed.

    With `survivor_draw` (T6-29c), an impl node that fails only coverage
    or mutation is first answered with tests rather than a retry: k =
    `survivor_samples` candidate test files are drawn from a brief that
    names the untested functions and the modules' signatures, filtered
    (`saddle.survivors`), and the kept ones become a `test` node the
    impl node then retries behind, at most SURVIVOR_ROUNDS times per
    node. A gap inside a function a sealed test node of this run already
    names, or a failure on any other gate, retries and replans as before.

    Every node is gated by its own scoped `test_command`; nothing checks
    the union of their diffs until `merge_command` runs, once, unscoped,
    in `workdir` after the schedule loop ends (ARCHITECTURE Tier 2, #60).
    It runs only if at least one node was proven, seals a tool span named
    `merge-suite` under the run span, and a non-zero exit fails the run
    without revisiting any per-node verdict. `None` disables it.

    With `deadline_s` (T6-9) the run is on a clock: no node is started
    that the time left cannot fit (median node wall so far), no attempt
    is started past the deadline, the attempt in flight finishes and may
    seal, a node that gives up restores its tree as on any other give-up,
    the merge suite still runs over what was proven, and the run seals
    exit 3 with `deadline:` in its span. Round 2 twice and round 3's T5
    ended under an external `timeout` with nothing sealed and a log lost
    with the process; a deadline saddle can see coming ends with a
    journal that resumes.
    """
    task_hash = hashlib.sha256(task.encode()).hexdigest()
    started = now()
    run_start = perf_counter()
    deadline = _Deadline(at=clock() + deadline_s, clock=clock) if deadline_s is not None else None
    run_span_id = uuid.uuid4().hex
    proofs, resumed, reused = _seed_proofs(journal_path, dag, task_hash)
    # What was asked, sealed before anything is done about it (T6-13): a
    # run that dies leaves its plan in the chain, not in a buffered log.
    append_plan(journal_path, build_plan(dag.nodes, task_hash=task_hash))
    if reused is not None:
        # A proof is a proof about one worktree (T3-10). `rebuild_proven`
        # promises a crash loses at most the in-flight node, which holds
        # only while the tree still carries the proven edits: reverting
        # one and resuming used to gate the next node against code the
        # journal says is proven and the disk does not have.
        expected = reused.tree_hash
        current = snapshot_tree(workdir, None)
        if current != expected:
            msg = (
                "worktree does not match the proof being resumed onto: node "
                f"{reused.node_id!r} was proven on tree {expected}, "
                f"{str(workdir)!r} now hashes to {current}. Restore it with "
                f"git restore --source {proven_ref(reused.node_id)} "
                "--staged --worktree -- . "
                "(a commit of the proven edits keeps the same tree), "
                "or pass a fresh --journal path."
            )
            raise ValueError(msg)
    if resumed is not None:
        append_span(
            journal_path,
            build_span(
                node_id="",
                argv=[],
                duration_ms=_elapsed_ms(run_start),
                exit_code=0,
                detail=resumed,
                kind="agent",
                name="resume",
                parent_id=run_span_id,
                started_at=started,
            ),
        )
    remaining, ever_failed, replanned_from, deadline_hit = _schedule_until_done(
        dag,
        replan=replan,
        workdir=workdir,
        journal_path=journal_path,
        propose=propose,
        proofs=proofs,
        run_span_id=run_span_id,
        task_hash=task_hash,
        deadline=deadline,
        survivor_draw=survivor_draw,
        survivor_samples=survivor_samples,
        rule_d=rule_d,
    )
    merge_exit, merge_ran = _merge_gate(
        merge_command,
        workdir=workdir,
        proofs=proofs,
        run_span_id=run_span_id,
        journal_path=journal_path,
    )
    return _seal_run(
        remaining,
        journal_path=journal_path,
        proofs=proofs,
        ever_failed=ever_failed,
        replanned_from=replanned_from,
        run_span_id=run_span_id,
        run_start=run_start,
        merge_exit=merge_exit,
        merge_ran=merge_ran,
        task=task,
        started=started,
        now=now,
        deadline_hit=deadline_hit,
        settings=settings,
    )
