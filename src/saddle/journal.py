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
from dataclasses import dataclass
from pathlib import Path
from typing import Any

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

    evidence_id: str
    node_id: str
    diff_hash: str
    parent_proofs: list[str]
    gate_outputs: list[GateOutput]
    requirement_ids: list[str]
    record_hash: str


@dataclass(frozen=True)
class JournalIssue:
    """One machine-readable journal finding (`[]` from verify is valid)."""

    code: str
    line: int | None
    message: str


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
) -> ProofRecord:
    """Seal a record: copy caller data, hash the diff, then the payload."""
    payload: dict[str, Any] = {
        "evidence_id": evidence_id,
        "node_id": node_id,
        "diff_hash": hashlib.sha256(diff.encode()).hexdigest(),
        "parent_proofs": list(parent_proofs),
        "gate_outputs": [output.model_dump() for output in gate_outputs],
        "requirement_ids": list(requirement_ids),
    }
    return ProofRecord.model_validate({**payload, "record_hash": _canonical_hash(payload)})


def append_record(path: Path, record: ProofRecord) -> None:
    """Append one record line; fsync before returning so kill -9 keeps it."""
    path.parent.mkdir(parents=True, exist_ok=True)
    line = json.dumps(record.model_dump(), sort_keys=True) + "\n"
    with path.open("ab") as handle:
        handle.write(line.encode())
        handle.flush()
        os.fsync(handle.fileno())


def _load_journal(path: Path) -> tuple[list[ProofRecord], list[JournalIssue]]:
    """Read and verify: recompute every hash, check every parent link."""
    if not path.exists():
        return ([], [])
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
            record = ProofRecord.model_validate(raw)
        except ValidationError:
            issues.append(
                JournalIssue(
                    code="invalid-record", line=number, message="line is not a proof record"
                )
            )
            continue
        payload = record.model_dump(exclude={"record_hash"})
        if _canonical_hash(payload) != record.record_hash:
            issues.append(
                JournalIssue(
                    code="bad-hash",
                    line=number,
                    message=f"record hash mismatch for node {record.node_id!r}",
                )
            )
            continue
        if any(parent not in seen for parent in record.parent_proofs):
            issues.append(
                JournalIssue(
                    code="unknown-parent",
                    line=number,
                    message=f"node {record.node_id!r} cites unknown parent proof",
                )
            )
            continue
        records.append(record)
        seen.add(record.record_hash)
    return (records, issues)


def verify_journal(path: Path) -> list[JournalIssue]:
    """Verify the journal by recomputation; empty list means valid."""
    _, issues = _load_journal(path)
    return issues


def _verified_records(path: Path) -> list[ProofRecord]:
    """Verified sealed records; refuses hard corruption (torn tail aside)."""
    records, issues = _load_journal(path)
    hard = [issue for issue in issues if issue.code != "torn-tail"]
    if hard:
        codes = ", ".join(f"{issue.code}@line {issue.line}" for issue in hard)
        msg = f"journal {str(path)!r} failed verification: {codes}"
        raise ValueError(msg)
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


def build_from_gate(
    node: Node,
    diff: str,
    result: Tier1Result,
    parent_proofs: list[str],
    evidence_id: str,
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
    )
