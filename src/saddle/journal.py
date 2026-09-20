"""Hash-chained proof journal (ARCHITECTURE.md §3 Phase 3).

Append-only JSONL: one sealed record per verified node, fsync'd on write.
Verification is recomputation (sha256 over canonical JSON) plus
parent-linkage checks — no trust, no PKI. A missing journal is a fresh
journal; only an unterminated tail line is tolerated (crash-torn write),
everything else fails strict.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import uuid
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Final, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from saddle.dag import Node
from saddle.gates import Tier1Result


class GateOutput(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    name: str
    passed: bool
    detail: str
    # Evidence basis (T2-4): "sampled n=<mutants>" for the mutation check,
    # None elsewhere. Optional so a journal written before this field
    # parses unchanged; verification hashes with exclude_unset=True, so a
    # record that never carried the key still matches its sealed hash.
    basis: str | None = None


class ProofRecord(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    record_type: Literal["proof"] = "proof"
    evidence_id: str
    node_id: str
    diff_hash: str
    parent_proofs: list[str]
    gate_outputs: list[GateOutput]
    requirement_ids: list[str]
    thinking: str
    attempts: int = 1
    # What this proof is a proof *of* (T3-9). Sealed inside the hash, and
    # defaulted so a journal written before they existed still parses and
    # still reproduces its own hash (verification dumps with
    # `exclude_unset=True`, as `basis` above relies on too).
    task_hash: str = ""
    node_hash: str = ""
    kind: str = ""
    target_files: list[str] = Field(default_factory=list)
    # The worktree the gate passed on (T3-10): the `git write-tree` id of
    # the tracked files, kept at `refs/saddle/proven/<node>`. A resume
    # compares the tree it was handed with this one; defaulted for the
    # same reason as the fields above.
    tree_hash: str = ""
    record_hash: str


class PlanNode(BaseModel):
    """One node as planned: what was asked of it, sealed before it runs (T6-13)."""

    model_config = ConfigDict(extra="forbid")

    id: str
    kind: str
    target_files: list[str]
    requirement_ids: list[str]
    reasoning_budget: str
    max_context_tokens: int
    node_hash: str


class PlanRecord(BaseModel):
    """The plan a run executed, in the chain beside its outcomes (T6-13).

    Round-3 T5 timed out with a journal of spans and an empty log: nothing
    said which kind its node was, what effort it ran at, or which files it
    declared. Sealing the plan before the first worker call means a run's
    journal records what was asked, not only what happened; a replan
    seals its replacement with `replaces` naming the failed node.
    """

    model_config = ConfigDict(extra="forbid")

    record_type: Literal["plan"] = "plan"
    task_hash: str
    nodes: list[PlanNode]
    replaces: str = ""
    record_hash: str


class SpanRecord(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    record_type: Literal["span"] = "span"
    span_id: str
    parent_id: str | None = None
    kind: Literal["tool", "agent"] = "tool"
    node_id: str
    name: str
    argv: list[str]
    args_hash: str
    duration_ms: int
    exit_code: int
    detail: str
    # sha256 of the attempt sidecar `attempts/<span_id>.json` beside the
    # journal (T6-12): the attempt's reasoning, finish reason, token usage,
    # the cap it was sent, and its failure. Empty for tool spans and for
    # spans sealed before T6-12.
    attempt_hash: str = ""
    record_hash: str


@dataclass(frozen=True)
class JournalIssue:
    """One machine-readable journal finding (`[]` from verify is valid)."""

    code: str
    line: int | None
    message: str


MAX_THINKING_CHARS: Final = 4000
STAR: Final = "***"
_KEY_PATTERN: Final = re.compile(r"sk-[A-Za-z0-9_-]{8,}")
_AWS_PATTERN: Final = re.compile(r"AKIA[0-9A-Z]{16}")
_NAMED_PATTERN: Final = re.compile(r"(?i)(api[_-]?key|password|secret|token)\s*[:=]\s*([^\s,;]+)")


def _scrub_bearer(text: str) -> str:
    """Replace bearer token values, keeping the scheme word for context."""
    return re.sub(r"(Bearer)\s+[A-Za-z0-9_.~+/-]+", r"\1 " + STAR, text)


def scrub_thinking(text: str) -> str:
    """Redact secret-shaped spans, then cap length with a truncation marker."""
    scrubbed = _KEY_PATTERN.sub(STAR, text)
    scrubbed = _AWS_PATTERN.sub(STAR, scrubbed)
    scrubbed = _NAMED_PATTERN.sub(r"\1=" + STAR, scrubbed)
    scrubbed = _scrub_bearer(scrubbed)
    if len(scrubbed) > MAX_THINKING_CHARS:
        over = len(scrubbed) - MAX_THINKING_CHARS
        scrubbed = scrubbed[:MAX_THINKING_CHARS] + f"\n[truncated {over} chars]"
    return scrubbed


def _canonical_hash(payload: dict[str, Any]) -> str:
    """sha256 over canonical JSON: sorted keys, compact separators."""
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode()).hexdigest()


def hash_node(node: Node) -> str:
    """The node's content as one hash: what a proof for it was sealed against.

    One spelling, used both when a record is sealed and when a resume
    decides whether the DAG still holds the node it proved, so the two
    can never drift apart.
    """
    return hashlib.sha256(node.model_dump_json().encode()).hexdigest()


def build_record(
    *,
    evidence_id: str,
    node_id: str,
    diff: str,
    parent_proofs: list[str],
    gate_outputs: list[GateOutput],
    requirement_ids: list[str],
    thinking: str,
    attempts: int = 1,
    task_hash: str = "",
    node_hash: str = "",
    kind: str = "",
    target_files: Sequence[str] = (),
    tree_hash: str = "",
) -> ProofRecord:
    """Seal a record: copy caller data, hash the diff, then the payload."""
    payload: dict[str, Any] = {
        "record_type": "proof",
        "evidence_id": evidence_id,
        "node_id": node_id,
        "diff_hash": hashlib.sha256(diff.encode()).hexdigest(),
        "parent_proofs": list(parent_proofs),
        "gate_outputs": [output.model_dump() for output in gate_outputs],
        "requirement_ids": list(requirement_ids),
        "thinking": scrub_thinking(thinking),
        "attempts": attempts,
        "task_hash": task_hash,
        "node_hash": node_hash,
        "kind": kind,
        "target_files": list(target_files),
        "tree_hash": tree_hash,
    }
    return ProofRecord.model_validate({**payload, "record_hash": _canonical_hash(payload)})


MAX_SPAN_DETAIL_CHARS: Final = 500


def _tool_name(argv: list[str]) -> str:
    """Basename of the invoked tool, or "?" when argv is empty."""
    if not argv:
        return "?"
    first = argv[0]
    return first[first.rfind("/") + 1 :]


def build_span(
    *,
    node_id: str,
    argv: Sequence[str],
    duration_ms: int,
    exit_code: int,
    detail: str,
    kind: Literal["tool", "agent"] = "tool",
    name: str | None = None,
    parent_id: str | None = None,
    span_id: str | None = None,
    attempt_hash: str = "",
) -> SpanRecord:
    """Seal one span: scrubbed argv, hashed args, capped detail."""
    scrubbed = [scrub_thinking(part) for part in argv]
    encoded_args = json.dumps(scrubbed).encode()
    payload: dict[str, Any] = {
        "record_type": "span",
        "span_id": span_id if span_id is not None else uuid.uuid4().hex,
        "parent_id": parent_id,
        "kind": kind,
        "node_id": node_id,
        "name": name if name is not None else _tool_name(scrubbed),
        "argv": scrubbed,
        "args_hash": hashlib.sha256(encoded_args).hexdigest(),
        "duration_ms": duration_ms,
        "exit_code": exit_code,
        "detail": scrub_thinking(detail)[:MAX_SPAN_DETAIL_CHARS],
    }
    if attempt_hash:
        payload["attempt_hash"] = attempt_hash
    return SpanRecord.model_validate({**payload, "record_hash": _canonical_hash(payload)})


def build_plan(nodes: Sequence[Node], *, task_hash: str, replaces: str = "") -> PlanRecord:
    """Seal what was asked: every node's kind, scope, budgets and hash (T6-13)."""
    payload: dict[str, Any] = {
        "record_type": "plan",
        "task_hash": task_hash,
        "nodes": [
            {
                "id": node.id,
                "kind": node.kind,
                "target_files": list(node.target_files),
                "requirement_ids": list(node.requirement_ids),
                "reasoning_budget": node.execution_constraints.reasoning_budget,
                "max_context_tokens": node.execution_constraints.max_context_tokens,
                "node_hash": hash_node(node),
            }
            for node in nodes
        ],
    }
    if replaces:
        payload["replaces"] = replaces
    return PlanRecord.model_validate({**payload, "record_hash": _canonical_hash(payload)})


ATTEMPTS_DIR: Final = "attempts"


def attempt_sidecar_path(journal_path: Path, span_id: str) -> Path:
    """Where an attempt's evidence lives: `attempts/<span_id>.json` beside the journal."""
    return journal_path.parent / ATTEMPTS_DIR / f"{span_id}.json"


def write_attempt_sidecar(journal_path: Path, span_id: str, evidence: Mapping[str, Any]) -> str:
    """Write one attempt's evidence and return its sha256 for the span (T6-12).

    The evidence is what a failed attempt used to lose: the worker's
    reasoning (scrubbed like every other journaled text), the finish
    reason, the token usage the server reported, the cap the call was
    sent, and the failure. It lives beside the journal rather than in it
    so the chain and `saddle tail` stay small; the span's `attempt_hash`
    is what makes it tamper-evident.
    """
    path = attempt_sidecar_path(journal_path, span_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    scrubbed = {
        key: scrub_thinking(value) if isinstance(value, str) else value
        for key, value in evidence.items()
    }
    encoded = json.dumps(scrubbed, sort_keys=True, indent=1).encode()
    path.write_bytes(encoded)
    return hashlib.sha256(encoded).hexdigest()


def _append_line(path: Path, line: str) -> None:
    """Append one line; fsync before returning so kill -9 keeps it."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("ab") as handle:
        handle.write((line + "\n").encode())
        handle.flush()
        os.fsync(handle.fileno())


def append_record(path: Path, record: ProofRecord) -> None:
    """Append one proof record line."""
    _append_line(path, json.dumps(record.model_dump(), sort_keys=True))


def append_span(path: Path, span: SpanRecord) -> None:
    """Append one tool-span line."""
    _append_line(path, json.dumps(span.model_dump(exclude_unset=True), sort_keys=True))


def append_plan(path: Path, plan: PlanRecord) -> None:
    """Append one plan record line."""
    _append_line(path, json.dumps(plan.model_dump(exclude_unset=True), sort_keys=True))


@dataclass(frozen=True)
class SpanRecorder:
    """Journal sink for one node's tool spans."""

    path: Path
    node_id: str
    parent_id: str | None = None

    def record(
        self,
        *,
        argv: Sequence[str],
        duration_ms: int,
        exit_code: int,
        detail: str,
        name: str | None = None,
    ) -> None:
        """Seal and append one completed tool invocation.

        `name` overrides the tool name derived from `argv`, for a git run
        whose purpose the journal should show (`restore-baseline`, T3-23).
        """
        append_span(
            self.path,
            build_span(
                node_id=self.node_id,
                argv=list(argv),
                duration_ms=duration_ms,
                exit_code=exit_code,
                detail=detail,
                name=name,
                parent_id=self.parent_id,
            ),
        )


JournalEntry = ProofRecord | SpanRecord | PlanRecord


def _parse_line(raw: object) -> JournalEntry:
    """Validate one decoded line as the record kind it claims to be."""
    if isinstance(raw, dict) and raw.get("record_type") == "span":
        try:
            return SpanRecord.model_validate(raw)
        except ValidationError as exc:
            msg = "line is not a span record"
            raise ValueError(msg) from exc
    if isinstance(raw, dict) and raw.get("record_type") == "plan":
        try:
            return PlanRecord.model_validate(raw)
        except ValidationError as exc:
            msg = "line is not a plan record"
            raise ValueError(msg) from exc
    try:
        return ProofRecord.model_validate(raw)
    except ValidationError as exc:
        msg = "line is not a proof record"
        raise ValueError(msg) from exc


def _load_journal(
    path: Path,
) -> tuple[list[ProofRecord], list[SpanRecord], list[JournalIssue], list[JournalEntry]]:
    """Read and verify: recompute every hash, check every parent link.

    Also (T6-12, T6-13): an agent span carrying `attempt_hash` must have
    its sidecar beside the journal hashing to it, and once a journal
    holds a plan record, every proof's `node_hash` must be one the plan
    records name -- a proof for a node nobody planned is a chain error.
    """
    if not path.exists():
        return ([], [], [], [])
    text = path.read_bytes().decode()
    lines = text.splitlines()
    issues: list[JournalIssue] = []
    if lines and not text.endswith("\n"):
        issues.append(
            JournalIssue(
                code="torn-tail", line=len(lines), message="unterminated tail line discarded"
            )
        )
        lines = lines[:-1]
    records: list[ProofRecord] = []
    span_entries: list[tuple[int, SpanRecord]] = []
    ordered: list[JournalEntry] = []
    seen: set[str] = set()
    planned_hashes: set[str] = set()
    proof_lines: list[tuple[int, ProofRecord]] = []
    for number, line in enumerate(lines, start=1):
        try:
            raw = json.loads(line)
        except json.JSONDecodeError:
            issues.append(
                JournalIssue(code="unparseable-line", line=number, message="line is not valid JSON")
            )
            continue
        try:
            entry = _parse_line(raw)
        except ValueError as exc:
            issues.append(JournalIssue(code="invalid-record", line=number, message=str(exc)))
            continue
        payload = entry.model_dump(exclude={"record_hash"}, exclude_unset=True)
        if _canonical_hash(payload) != entry.record_hash:
            who = f"for node {entry.node_id!r}" if not isinstance(entry, PlanRecord) else "for plan"
            issues.append(
                JournalIssue(code="bad-hash", line=number, message=f"record hash mismatch {who}")
            )
            continue
        if isinstance(entry, PlanRecord):
            planned_hashes.update(node.node_hash for node in entry.nodes)
            ordered.append(entry)
            continue
        if isinstance(entry, SpanRecord):
            span_entries.append((number, entry))
            ordered.append(entry)
            if entry.attempt_hash:
                sidecar = attempt_sidecar_path(path, entry.span_id)
                try:
                    actual = hashlib.sha256(sidecar.read_bytes()).hexdigest()
                except OSError:
                    actual = ""
                if actual != entry.attempt_hash:
                    state = "missing" if not actual else "does not hash"
                    issues.append(
                        JournalIssue(
                            code="attempt-sidecar",
                            line=number,
                            message=f"attempt sidecar for span {entry.span_id!r} {state}",
                        )
                    )
            continue
        if any(parent not in seen for parent in entry.parent_proofs):
            issues.append(
                JournalIssue(
                    code="unknown-parent",
                    line=number,
                    message=f"node {entry.node_id!r} cites unknown parent proof",
                )
            )
            continue
        records.append(entry)
        ordered.append(entry)
        seen.add(entry.record_hash)
        # Only a proof sealed after a plan record is judged against plans:
        # earlier proofs predate T6-13 or were resumed from a journal that
        # never had one.
        if planned_hashes:
            proof_lines.append((number, entry))
    for number, record in proof_lines:
        if record.node_hash and record.node_hash not in planned_hashes:
            issues.append(
                JournalIssue(
                    code="unplanned-proof",
                    line=number,
                    message=f"proof for node {record.node_id!r} matches no plan record",
                )
            )
    spans = [entry for _, entry in span_entries]
    known = {entry.span_id for entry in spans}
    for number, entry in span_entries:
        if entry.parent_id is not None and entry.parent_id not in known:
            issues.append(
                JournalIssue(
                    code="orphan-span",
                    line=number,
                    message=f"span {entry.span_id!r} cites unknown parent {entry.parent_id!r}",
                )
            )
    return (records, spans, issues, ordered)


def verify_journal(path: Path) -> list[JournalIssue]:
    """Verify the journal by recomputation; empty list means valid."""
    _, _, issues, _ = _load_journal(path)
    return issues


def _verified_contents(
    path: Path,
) -> tuple[list[ProofRecord], list[SpanRecord], list[JournalEntry]]:
    """Verified sealed entries; refuses hard corruption.

    A torn tail and in-flight orphans (children precede their parents)
    signal incompleteness, not corruption, so reads tolerate them;
    `verify_journal` still reports both.
    """
    records, spans, issues, ordered = _load_journal(path)
    soft = ("torn-tail", "orphan-span")
    hard = [issue for issue in issues if issue.code not in soft]
    if hard:
        codes = ", ".join(f"{issue.code}@line {issue.line}" for issue in hard)
        msg = f"journal {str(path)!r} failed verification: {codes}"
        raise ValueError(msg)
    return (records, spans, ordered)


def _verified_records(path: Path) -> list[ProofRecord]:
    """Verified sealed records; refuses hard corruption (torn tail aside)."""
    records, _, _ = _verified_contents(path)
    return records


def rebuild_proven(path: Path) -> dict[str, str]:
    """Rebuild scheduler state: proven node ids to their record hashes.

    Refuses a journal with hard corruption (only a crash-torn tail is
    tolerated); a crash therefore loses at most the in-flight node.
    """
    return {record.node_id: record.record_hash for record in _verified_records(path)}


def proven_records(path: Path) -> dict[str, ProofRecord]:
    """Rebuild scheduler state with the whole record, not just its hash.

    Last verified record per node id, in journal order; same corruption
    policy as `rebuild_proven`, which keeps its narrower contract for
    callers that only need the hashes. A resume needs the record itself
    to see which task and which node the proof was sealed for (T3-9).
    """
    return {record.node_id: record for record in _verified_records(path)}


def read_records(path: Path) -> list[ProofRecord]:
    """Verified sealed records in journal order, for transcripts and audits."""
    return _verified_records(path)


def read_spans(path: Path) -> list[SpanRecord]:
    """Verified sealed tool spans in journal order, for audits and timelines."""
    _, spans, _ = _verified_contents(path)
    return spans


def read_plans(path: Path) -> list[PlanRecord]:
    """Verified plan records in journal order: what each run (and replan) asked."""
    _, _, ordered = _verified_contents(path)
    return [entry for entry in ordered if isinstance(entry, PlanRecord)]


def read_entries(path: Path) -> list[JournalEntry]:
    """Verified sealed entries in journal order, for live tailing."""
    _, _, entries = _verified_contents(path)
    return entries


def tool_spans_by_node(spans: Sequence[SpanRecord]) -> dict[str, tuple[SpanRecord, ...]]:
    """Tool spans per node in journal order; agent spans are frames, not calls."""
    grouped: dict[str, list[SpanRecord]] = {}
    for span in spans:
        if span.kind == "tool":
            grouped.setdefault(span.node_id, []).append(span)
    return {node_id: tuple(entries) for node_id, entries in grouped.items()}


def tool_spans_for_node(
    grouped: Mapping[str, tuple[SpanRecord, ...]], node_id: str
) -> tuple[SpanRecord, ...]:
    """Tool spans sealed for `node_id`; nodes that never ran have none."""
    return grouped.get(node_id, ())


def build_from_gate(
    node: Node,
    diff: str,
    result: Tier1Result,
    parent_proofs: list[str],
    evidence_id: str,
    *,
    thinking: str,
    attempts: int = 1,
    task_hash: str = "",
    tree_hash: str = "",
) -> ProofRecord:
    """Seal a Tier-1 verdict as the node's proof record.

    The record names what it proves, not only that something passed:
    `task_hash` is the task the run was given, `node_hash` the node as
    validated, and `kind`/`target_files` the same facts spelled readably
    for an auditor (T3-9). `tree_hash` is the worktree the gate passed
    on, so a resume can check it is resuming onto that tree (T3-10).
    """
    return build_record(
        evidence_id=evidence_id,
        node_id=node.id,
        diff=diff,
        parent_proofs=parent_proofs,
        gate_outputs=[
            GateOutput(name=check.name, passed=check.passed, detail=check.detail, basis=check.basis)
            for check in result.checks
        ],
        requirement_ids=list(node.requirement_ids),
        thinking=thinking,
        attempts=attempts,
        task_hash=task_hash,
        node_hash=hash_node(node),
        kind=node.kind,
        target_files=list(node.target_files),
        tree_hash=tree_hash,
    )
