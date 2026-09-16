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
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Final, Literal

from pydantic import BaseModel, ConfigDict, ValidationError

from saddle.dag import Node
from saddle.gates import Tier1Result


class GateOutput(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    name: str
    passed: bool
    detail: str


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
    record_hash: str


class SpanRecord(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    record_type: Literal["span"] = "span"
    span_id: str
    node_id: str
    name: str
    argv: list[str]
    args_hash: str
    duration_ms: int
    exit_code: int
    detail: str
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


def build_record(
    *,
    evidence_id: str,
    node_id: str,
    diff: str,
    parent_proofs: list[str],
    gate_outputs: list[GateOutput],
    requirement_ids: list[str],
    thinking: str,
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
) -> SpanRecord:
    """Seal one tool invocation: scrubbed argv, hashed args, capped detail."""
    scrubbed = [scrub_thinking(part) for part in argv]
    encoded_args = json.dumps(scrubbed).encode()
    payload: dict[str, Any] = {
        "record_type": "span",
        "span_id": uuid.uuid4().hex,
        "node_id": node_id,
        "name": _tool_name(scrubbed),
        "argv": scrubbed,
        "args_hash": hashlib.sha256(encoded_args).hexdigest(),
        "duration_ms": duration_ms,
        "exit_code": exit_code,
        "detail": scrub_thinking(detail)[:MAX_SPAN_DETAIL_CHARS],
    }
    return SpanRecord.model_validate({**payload, "record_hash": _canonical_hash(payload)})


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
    _append_line(path, json.dumps(span.model_dump(), sort_keys=True))


@dataclass(frozen=True)
class SpanRecorder:
    """Journal sink for one node's tool spans."""

    path: Path
    node_id: str

    def record(self, *, argv: Sequence[str], duration_ms: int, exit_code: int, detail: str) -> None:
        """Seal and append one completed tool invocation."""
        append_span(
            self.path,
            build_span(
                node_id=self.node_id,
                argv=list(argv),
                duration_ms=duration_ms,
                exit_code=exit_code,
                detail=detail,
            ),
        )


def _parse_line(raw: object) -> ProofRecord | SpanRecord:
    """Validate one decoded line as the record kind it claims to be."""
    if isinstance(raw, dict) and raw.get("record_type") == "span":
        try:
            return SpanRecord.model_validate(raw)
        except ValidationError as exc:
            msg = "line is not a span record"
            raise ValueError(msg) from exc
    try:
        return ProofRecord.model_validate(raw)
    except ValidationError as exc:
        msg = "line is not a proof record"
        raise ValueError(msg) from exc


def _load_journal(
    path: Path,
) -> tuple[list[ProofRecord], list[SpanRecord], list[JournalIssue]]:
    """Read and verify: recompute every hash, check every parent link."""
    if not path.exists():
        return ([], [], [])
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
    spans: list[SpanRecord] = []
    seen: set[str] = set()
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
        payload = entry.model_dump(exclude={"record_hash"})
        if _canonical_hash(payload) != entry.record_hash:
            issues.append(
                JournalIssue(
                    code="bad-hash",
                    line=number,
                    message=f"record hash mismatch for node {entry.node_id!r}",
                )
            )
            continue
        if isinstance(entry, SpanRecord):
            spans.append(entry)
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
        seen.add(entry.record_hash)
    return (records, spans, issues)


def verify_journal(path: Path) -> list[JournalIssue]:
    """Verify the journal by recomputation; empty list means valid."""
    _, _, issues = _load_journal(path)
    return issues


def _verified_contents(path: Path) -> tuple[list[ProofRecord], list[SpanRecord]]:
    """Verified sealed entries; refuses hard corruption (torn tail aside)."""
    records, spans, issues = _load_journal(path)
    hard = [issue for issue in issues if issue.code != "torn-tail"]
    if hard:
        codes = ", ".join(f"{issue.code}@line {issue.line}" for issue in hard)
        msg = f"journal {str(path)!r} failed verification: {codes}"
        raise ValueError(msg)
    return (records, spans)


def _verified_records(path: Path) -> list[ProofRecord]:
    """Verified sealed records; refuses hard corruption (torn tail aside)."""
    records, _ = _verified_contents(path)
    return records


def rebuild_proven(path: Path) -> dict[str, str]:
    """Rebuild scheduler state: proven node ids to their record hashes.

    Refuses a journal with hard corruption (only a crash-torn tail is
    tolerated); a crash therefore loses at most the in-flight node.
    """
    return {record.node_id: record.record_hash for record in _verified_records(path)}


def read_records(path: Path) -> list[ProofRecord]:
    """Verified sealed records in journal order, for transcripts and audits."""
    return _verified_records(path)


def read_spans(path: Path) -> list[SpanRecord]:
    """Verified sealed tool spans in journal order, for audits and timelines."""
    _, spans = _verified_contents(path)
    return spans


def build_from_gate(
    node: Node,
    diff: str,
    result: Tier1Result,
    parent_proofs: list[str],
    evidence_id: str,
    *,
    thinking: str,
) -> ProofRecord:
    """Seal a Tier-1 verdict as the node's proof record."""
    return build_record(
        evidence_id=evidence_id,
        node_id=node.id,
        diff=diff,
        parent_proofs=parent_proofs,
        gate_outputs=[
            GateOutput(name=check.name, passed=check.passed, detail=check.detail)
            for check in result.checks
        ],
        requirement_ids=list(node.requirement_ids),
        thinking=thinking,
    )
