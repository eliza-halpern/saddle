"""Hash-chained proof journal (ARCHITECTURE.md §3 Phase 3).

Append-only JSONL: one sealed record per verified node, fsync'd on write.
Verification is recomputation (sha256 over canonical JSON) plus
parent-linkage checks — no trust, no PKI. A missing journal is a fresh
journal; only an unterminated tail line is tolerated (crash-torn write),
everything else fails strict.

Spans are self-hashed, not chained. An autonomous run is held whole by
its outcome span instead: the sealed sidecar lists every tool span's hash
in order, and verification requires exactly those spans, in that order,
with none after the outcome (`_auto_run_issues`).
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import uuid
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
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
    # Evidence basis: "sampled n=<mutants>" for the mutation check,
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
    # What this proof is a proof *of*. Sealed inside the hash, and
    # defaulted so a journal written before they existed still parses and
    # still reproduces its own hash (verification dumps with
    # `exclude_unset=True`, as `basis` above relies on too).
    task_hash: str = ""
    node_hash: str = ""
    kind: str = ""
    target_files: list[str] = Field(default_factory=list)
    # The worktree the gate passed on: the `git write-tree` id of
    # the tracked files, kept at `refs/saddle/proven/<node>`. A resume
    # compares the tree it was handed with this one; defaulted for the
    # same reason as the fields above.
    tree_hash: str = ""
    record_hash: str


class PlanNode(BaseModel):
    """One node as planned: what was asked of it, sealed before it runs."""

    model_config = ConfigDict(extra="forbid")

    id: str
    kind: str
    target_files: list[str]
    requirement_ids: list[str]
    reasoning_budget: str
    max_context_tokens: int
    node_hash: str
    # What the node may do: a round 3d lint failure could not say
    # whether the plan declared `lint`, because nothing recorded it.
    # Unset on plans sealed before this field existed.
    allowed_tools: list[str] = []


class PlanRecord(BaseModel):
    """The plan a run executed, in the chain beside its outcomes.

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
    # UTC ISO-8601 start of the span. Round 3d's journal held no
    # absolute time anywhere, so nothing in it could be joined to a server
    # log, and three concurrent draws had no recoverable order. Unset on
    # spans sealed before this field existed.
    started_at: str = ""
    # sha256 of the attempt sidecar `attempts/<span_id>.json` beside the
    # journal: the attempt's reasoning, finish reason, token usage,
    # the cap it was sent, and its failure. Empty for tool spans and for
    # spans sealed before attempt sidecars existed.
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
_KEY_PATTERN: Final = re.compile(r"(?<![A-Za-z0-9_])sk-[A-Za-z0-9_-]{8,}")
"""An `sk-` key, but not the `sk-` inside a word such as `task-requirements`:
the `sk` may not run on from a letter, digit or underscore. A key glued to one
(`xsk-...`) is therefore not redacted; by shape it cannot be told from a word."""
_AWS_PATTERN: Final = re.compile(r"AKIA[0-9A-Z]{16}")
_NAMED_PATTERN: Final = re.compile(r"(?i)(api[_-]?key|password|secret|token)\s*[:=]\s*([^\s,;]+)")


def _scrub_bearer(text: str) -> str:
    """Replace bearer token values, keeping the scheme word for context."""
    return re.sub(r"(Bearer)\s+[A-Za-z0-9_.~+/-]+", r"\1 " + STAR, text)


def redact_secrets(text: str) -> str:
    """Redact secret-shaped spans. Length is NOT preserved: the named-key
    rule replaces its whole match, separator and value, with `name=***`."""
    scrubbed = _KEY_PATTERN.sub(STAR, text)
    scrubbed = _AWS_PATTERN.sub(STAR, scrubbed)
    scrubbed = _NAMED_PATTERN.sub(r"\1=" + STAR, scrubbed)
    return _scrub_bearer(scrubbed)


def scrub_thinking(text: str) -> str:
    """Redact secret-shaped spans, then cap length with a truncation marker."""
    scrubbed = redact_secrets(text)
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
    started_at: str = "",
) -> SpanRecord:
    """Seal one span: scrubbed argv, hashed args, capped detail, start time."""
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
    if started_at:
        payload["started_at"] = started_at
    if attempt_hash:
        payload["attempt_hash"] = attempt_hash
    return SpanRecord.model_validate({**payload, "record_hash": _canonical_hash(payload)})


def build_plan(nodes: Sequence[Node], *, task_hash: str, replaces: str = "") -> PlanRecord:
    """Seal what was asked: every node's kind, scope, budgets and hash."""
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
                "allowed_tools": list(node.execution_constraints.allowed_tools),
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
    """Write one attempt's evidence and return its sha256 for the span.

    The evidence is what a failed attempt used to lose: the worker's
    reasoning (redacted, and whole), the finish reason, the
    token usage the server reported, the cap the call was sent, and the
    failure. It lives beside the journal rather than in it so the chain
    and `saddle tail` stay small; the span's `attempt_hash` is what makes
    it tamper-evident.
    """
    path = attempt_sidecar_path(journal_path, span_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    scrubbed = {key: _scrub_evidence(key, value) for key, value in evidence.items()}
    encoded = json.dumps(scrubbed, sort_keys=True, indent=1).encode()
    path.write_bytes(encoded)
    return hashlib.sha256(encoded).hexdigest()


# Sidecar text retained VERBATIM: neither capped nor redacted. A `diff`
# has to hash to its `diff_hash` or the `sidecar-diff-hash` check fails
# the journal, and `_proposal_evidence` takes that hash before this module
# sees the text, so any rewrite here breaks the invariant. Redaction did
# rewrite it: `_NAMED_PATTERN` matched `Token = namedtuple(...)` in a
# tokenizer's own source and the journal failed `sidecar-diff-hash` on a
# run whose every gate had passed. A diff is source code, and it
# is already in the worktree and in git by the time the sidecar is
# authored, so scrubbing this copy protects nothing the tree does not
# already expose. `sources` are the audited tree's own files, sealed beside
# a coverage finding (`auditor.coverage_evidence`) so the packet can place
# each uncovered line in its function: redaction rewrote their code
# (`CHARS_PER_TOKEN: Final = 4` became `CHARS_PER_TOKEN=*** = 4`), the file no
# longer parsed, and the packet said it was "not in the tree read". They are
# in the worktree as the diff is; the lines the packet shows are redacted
# there (`packet._coverage_summary`).
_RETAINED_VERBATIM: Final = frozenset({"diff", "sources"})

# Sidecar text that is retained whole but still redacted: a prompt is what
# a replay needs verbatim and can carry an injected key, and `thinking` is
# the reasoning every reading of a failed attempt starts from.
# Every other string is capped. The journal's own entries are unaffected --
# they cap thinking through `scrub_thinking`, and these sets are read only
# by `write_attempt_sidecar`, so `proofs.jsonl` and `saddle tail` stay
# small while the per-attempt file beside them keeps the whole record.
_RETAINED_WHOLE: Final = frozenset({"prompt", "thinking"})


def _scrub_evidence(key: str, value: Any) -> Any:
    """Redact every string in `value`, at any depth; cap all but the retained
    keys; keep a `_RETAINED_VERBATIM` key's value, whatever its shape, as is.

    Round 3e: the scrub was one level deep and capped every
    string, so a 4001+ character `diff` no longer hashed to its
    `diff_hash` -- every large attempt failed verification, the run
    aborted on its own journal and `saddle explain` refused it -- while
    the nested `samples[i]` text, where nearly all of the sidecar lives,
    was neither capped nor redacted.

    Round 3f: going recursive carried the cap *into* that nested
    text, and `samples[i].thinking` is where a draw's reasoning lives.
    Three samples lost 31 911, 58 927 and 45 345 characters, so the first
    step of reading a failed attempt -- what did the model think it was
    doing -- could not be taken from the record at all. Reasoning is
    retained whole here for that reason; the cap still applies to every
    other string, at every depth.
    """
    if key in _RETAINED_VERBATIM:
        return value
    if isinstance(value, str):
        return redact_secrets(value) if key in _RETAINED_WHOLE else scrub_thinking(value)
    if isinstance(value, Mapping):
        return {str(k): _scrub_evidence(str(k), v) for k, v in value.items()}
    if isinstance(value, list | tuple):
        return [_scrub_evidence(key, item) for item in value]
    return value


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


def utc_now() -> datetime:
    return datetime.now(UTC)


def started_before(duration_ms: int, now: datetime) -> str:
    """The UTC ISO-8601 instant `duration_ms` before `now`."""
    return (now - timedelta(milliseconds=duration_ms)).isoformat()


@dataclass(frozen=True)
class SpanRecorder:
    """Journal sink for one node's tool spans."""

    path: Path
    node_id: str
    parent_id: str | None = None
    # Wall clock for `started_at`; injectable so a test can pin it.
    clock: Callable[[], datetime] = utc_now

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
        whose purpose the journal should show (`restore-baseline`).
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
                started_at=started_before(duration_ms, self.clock()),
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

    Also: an agent span carrying `attempt_hash` must have
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
    auto_proofs: list[tuple[int, ProofRecord]] = []
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
                elif _sidecar_diff_mismatch(sidecar):
                    # A retained diff that does not hash to the
                    # sealed `diff_hash` is a sidecar telling two stories.
                    issues.append(
                        JournalIssue(
                            code="sidecar-diff-hash",
                            line=number,
                            message=f"attempt sidecar for span {entry.span_id!r} "
                            "retains a diff that does not hash to its diff_hash",
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
        if entry.kind.startswith(AUTO_PROOF_PREFIX):
            auto_proofs.append((number, entry))
        # Only a proof sealed after a plan record is judged against plans:
        # earlier proofs predate plan records or were resumed from a journal that
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
    issues.extend(_auto_run_issues(path, span_entries, auto_proofs, issues))
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


AUTO_START: Final = "auto:start"
AUTO_PROOF_PREFIX: Final = "auto-"
TOOL_SPAN_HASHES: Final = "tool_span_hashes"
AUDIT_SPAN_HASHES: Final = "audit_span_hashes"
"""The outcome sidecar's list of the run's audit records, as a set:
the auditor writes from the feed's thread, so their order is not the run's."""
AUTO_COMMITTED: Final = "auto:committed"
"""The first record of a run's coverage file (`coverage_path`), sealed by
`auto.run_auto` once the run's branch has its commit: argv is `[name, commit,
outcome]`, `outcome` the `record_hash` of the ledger's outcome span, and the
detail names the branch, the base sha, the commit and its tree. The commit's
message carries the outcome span's hash, so the commit cannot be named in the
outcome itself, and a span appended to the ledger after the outcome would
change what a ledger's last record is; so the commit is recorded beside the
ledger, bound to it by the outcome hash. A ledger with no coverage file
covers no commit it can name."""
FOLLOWUP_SPAN: Final = "followup:audit"
"""A follow-up audit attached to a finished run's coverage file
(`covers.attach_followup`): argv is `[name, covered_from, covered_to,
previous]`, `previous` being the
`record_hash` of the record it continues (the commit record, or the follow-up
before it), so the sections form a chain. Its findings and verdict sit in its
sealed sidecar. Not `audit`-prefixed: an audit of the run itself is held to the
outcome's list, and this is not one."""
COMPACTION_SPAN: Final = "compaction"
"""The span an autonomous run seals each time its context is compacted
(`engine._compact`): argv[1] is the counts as JSON, detail the summary.
Not `auto:`-prefixed, since `transcript.session_line` and the packet read
those as the run's start, spend and outcome; not a tool span, so the chain
does not hold it to the outcome's tool list."""
SNAPSHOT_SPAN: Final = "snapshot"
"""The span an autonomous run seals for each timed copy of its worktree it keeps
(`snapshots.Snapshots.snapshot_due`, `saddle auto --snapshot-marks`): argv is
`[name, m<mark>, content_sha256]`, detail names the mark and that hash, so the hash of
a tree the run no longer holds sits in the sealed chain. Not `auto:`-prefixed, as
`compaction` is not: a reader of the ledger's outcomes must not mistake a copy for a
run's start, spend or outcome. Not a tool span either: the run copies its own tree, the
model never asks for it, and a copy taken as the run ends would fail the chain's rule
that no tool span follows the outcome."""
PLAN_REMINDER_SPAN: Final = "plan:reminder"
"""The span an autonomous run seals each time the model is reminded that a reply
listed steps its plan does not hold (`engine._remind`, `plan.Plan.reminder`):
argv[1] is the run's count of reminders so far, detail the reminder as the model
read it. An agent span like `compaction`: the harness says it, the model never
calls it, so the chain does not hold it to the outcome's tool list."""
AUTO_OUTCOMES: Final = ("auto:finished", "auto:stopped", "auto:unchanged")
"""A run's outcome span names (`engine._seal_outcome`); `auto:unchanged` is
the third ending (finish on a tree equal to the baseline). Not `auto:spend`,
which the measured-usage record seals under the start span once per round."""
JOURNAL_QUESTION_EXIT: Final = 4
"""A `question` finding's span exit code (`auditor._JOURNAL_EXIT`): not 0 (a
pass), 1 (a fail) or 2 (blocked). Readers that cannot parse a finding's
detail still read this code as a question, never as a failure."""
AUDIT_QUESTION_STOP: Final = "needs you: the audit asks "
"""How the stop reason of a run whose finish audit asked a question opens
(`engine.needs_you_reason`): the run ends needing you, not stopped on a fault."""
PREMISE_DISPUTED_STOP: Final = "needs you: the task's premise is disputed: "
"""The stop reason when the model calls `dispute` (`engine._dispute`): the run
ends needing a person, who reads the claim and the rerun evidence."""
REFUSED_STOP: Final = "needs you: the task was refused: "
"""The stop reason when the model calls `refuse` (`engine._refuse`): the model
declines the task on grounds it will not act on (harmful, out of scope, against
policy). Unlike a dispute it carries no evidence -- a refusal is not a factual
claim about the code -- only the model's reason, and a person reviews it."""
BLOCKED_STOP: Final = "needs you: the run is blocked: "
"""The stop reason when the model calls `blocked` (`engine._blocked`): the model
is stuck on information or a decision only a person can give. Not done, not a
disputed premise, not a refusal: its reason and what it already tried are
sealed, and a person unblocks the task or withdraws it."""
STALL_STOP: Final = "needs you: stalled"
"""The stop-reason prefix when `--stall-check` ejects a stalled run
(`engine.STALLED`): the run made no progress and ends needing a person to read
its reasoning and decide. A needs-you verdict, not a fault or budget stop."""
GUARDED_STOP_PREFIX: Final = "needs you: this run changed code that judges runs"
"""How the stop reason of a run held by the self-guard opens (`engine.GUARDED_STOP`):
its finish audit accepted the tree, but it changed one of saddle's judges, so
only a person may land it (`web.branch_actions.approve_merge`)."""
SEALED_CUT: Final = " [cut to fit the ledger line; the audit sidecar holds it whole]"
"""What a `question` finding's sealed detail ends with when it was cut to fit
(`auditor.sealed_finding`)."""
FEED_QUESTION_LINE: Final = "(question for a person, does not refuse)"
"""How the audit text the model reads (`feed.render`) opens a `question`
finding's line; the card reads a delivered audit holding one as a question."""
P1_EXTRACT_SPAN: Final = "p1:extract"
"""The span `saddle auto --extract-requirements` seals when P1's extraction
ends (`auto._requirements`): its wall time as `duration_ms`, exit 0 with the
file's counts, or exit 1 with why it failed. An agent span under the run's
start, not a tool call; it may follow the outcome, since the run waits for
an extraction still running before it commits."""
AUDIT_SPAN_PREFIXES: Final = ("audit:", "audit-tier")
"""Audit records a run's journal also holds: the feed's `audit:delivered` /
`audit:withheld`, the chat seam's `audit:<gate>`, and the auditor's own
`audit-tier<N>:<gate>` findings (written from the feed's background thread,
so their journal order is not the engine's). They are not the model's tool
calls, the engine does not list them, and the chain does not judge them."""


def _auto_run_issues(
    path: Path,
    span_entries: Sequence[tuple[int, SpanRecord]],
    auto_proofs: Sequence[tuple[int, ProofRecord]],
    found: Sequence[JournalIssue],
) -> list[JournalIssue]:
    """Hold an autonomous run's tool spans to its outcome's list.

    Spans are hashed one by one, so deleting, reordering or appending a
    whole line leaves every remaining hash intact. The outcome span's
    sidecar (`engine._seal_outcome`) lists every tool span's
    `record_hash` in the order they ran, and the sidecar is sealed by the
    outcome span's `attempt_hash`. So, per `auto:start` span: the run's
    tool spans before its outcome must be exactly that list, in order;
    none may follow the outcome; there is one outcome; and once an
    `auto-*` proof is sealed for the node, the outcome must exist.

    A run's tool spans are those citing the start span as parent, plus
    any tool span after the start line and before the next start line
    (an inserted span need not cite the parent honestly). A journal with
    no `auto:start` span -- chat, slice runs -- is not judged here.

    The outcome is the run's `auto:finished`/`auto:stopped` span, by name
    (`AUTO_OUTCOMES`): the audit feed, the chat's question seam and the
    measured-usage `auto:spend` also write agent spans under the start span. Audit records
    (`AUDIT_SPAN_PREFIXES`) are not tool calls and are not held to the
    tool list (scope narrowed, INTEG); they are held, as a set, to the
    sidecar's `audit_span_hashes` when it has one (tightened).
    """
    issues: list[JournalIssue] = []
    starts = [
        (number, span)
        for number, span in span_entries
        if span.kind == "agent" and span.name == AUTO_START
    ]
    bad_sidecars = {issue.line for issue in found if issue.code == "attempt-sidecar"}
    for index, (start_line, start) in enumerate(starts):
        end_line = starts[index + 1][0] if index + 1 < len(starts) else None
        in_run = [
            (number, span)
            for number, span in span_entries
            if span.parent_id == start.span_id or _within(number, start_line, end_line)
        ]
        outcomes = [
            (number, span)
            for number, span in in_run
            if span.kind == "agent"
            and span.parent_id == start.span_id
            and span.name in AUTO_OUTCOMES
        ]
        tools = [
            (number, span)
            for number, span in in_run
            if span.kind == "tool" and not span.name.startswith(AUDIT_SPAN_PREFIXES)
        ]
        if not outcomes:
            for number, proof in auto_proofs:
                if proof.node_id == start.node_id and _within(number, start_line, end_line):
                    issues.append(
                        JournalIssue(
                            code="outcome-missing",
                            line=number,
                            message=f"proof for node {proof.node_id!r} ({proof.kind}) has no "
                            f"outcome span for run {start.span_id!r}",
                        )
                    )
            continue
        if len(outcomes) > 1:
            issues.append(
                JournalIssue(
                    code="outcome-duplicate",
                    line=outcomes[1][0],
                    message=f"run {start.span_id!r} has a second outcome span "
                    f"{outcomes[1][1].span_id!r}",
                )
            )
        outcome_line, outcome = outcomes[0]
        for number, span in tools:
            if number > outcome_line:
                issues.append(
                    JournalIssue(
                        code="span-after-outcome",
                        line=number,
                        message=f"span {span.span_id!r} ({span.name}) follows the outcome "
                        f"span {outcome.span_id!r}",
                    )
                )
        if outcome_line in bad_sidecars:
            continue  # its list cannot be trusted; attempt-sidecar already fails it
        issues.extend(_audit_list_issues(path, in_run, outcome_line, outcome))
        listed = _listed_span_hashes(path, outcome)
        if listed is None:
            issues.append(
                JournalIssue(
                    code="outcome-list",
                    line=outcome_line,
                    message=f"outcome span {outcome.span_id!r} has no {TOOL_SPAN_HASHES} list",
                )
            )
            continue
        present = [(number, span) for number, span in tools if number < outcome_line]
        present_hashes = [span.record_hash for _, span in present]
        for position, digest in enumerate(listed):
            if digest not in present_hashes:
                issues.append(
                    JournalIssue(
                        code="span-missing",
                        line=outcome_line,
                        message=f"outcome span {outcome.span_id!r} lists tool span #{position + 1} "
                        f"(record_hash {digest}) that is not in the journal",
                    )
                )
        for number, span in present:
            if span.record_hash not in listed:
                issues.append(
                    JournalIssue(
                        code="span-unlisted",
                        line=number,
                        message=f"span {span.span_id!r} ({span.name}) is not in the tool span "
                        f"list of outcome span {outcome.span_id!r}",
                    )
                )
        common = [digest for digest in listed if digest in present_hashes]
        for (number, span), digest in zip(
            [(n, s) for n, s in present if s.record_hash in listed], common, strict=False
        ):
            if span.record_hash != digest:
                issues.append(
                    JournalIssue(
                        code="span-order",
                        line=number,
                        message=f"span {span.span_id!r} ({span.name}) is out of the order "
                        f"outcome span {outcome.span_id!r} lists",
                    )
                )
                break
    return issues


def coverage_path(journal: Path) -> Path:
    """The coverage file that goes with a ledger: `proofs.jsonl` -> `proofs.covers.jsonl`."""
    return journal.with_suffix(".covers.jsonl")


def _coverage_issues(path: Path) -> list[JournalIssue]:
    """Verify the coverage file beside a ledger.

    The file is a journal of its own (same records, same per-record hash and
    sidecar checks); on top of that each follow-up names the record it
    continues and starts its range where the one before ended, and no outcome
    has two commit records. Which outcome a commit record names is not judged
    here: `covers.read_coverage` matches it to the ledger's runs, and a record
    that matches none is stated, not counted. Spans are hashed one by one,
    so without the chain a follow-up could be deleted from the middle, or one
    for another range spliced in, and every remaining hash would still check.
    Deleting the newest follow-up leaves a shorter chain that still verifies:
    an append-only file cannot show its own tail was cut, and the branch anchor
    does not cover follow-ups either. No file is no issue: a ledger from before
    the file existed covers nothing it can name.
    """
    cpath = coverage_path(path)
    if not cpath.exists():
        return []
    _, spans, found, _ = _load_journal(cpath)
    issues = [
        JournalIssue(code=issue.code, line=issue.line, message=f"{cpath.name}: {issue.message}")
        for issue in found
    ]
    bound: set[str] = set()
    tip: tuple[str, str] | None = None  # (record_hash, covered_to) the next follow-up continues
    for number, span in enumerate(spans, start=1):
        if span.name == AUTO_COMMITTED:
            outcome = _argv_at(span, 2)
            if outcome in bound:
                issues.append(
                    JournalIssue(
                        code="committed-duplicate",
                        line=number,
                        message=f"{cpath.name}: a second commit record {span.span_id!r} for "
                        "the same outcome",
                    )
                )
            bound.add(outcome)
            tip = (span.record_hash, _argv_at(span, 1))
        else:  # a follow-up, or a record that is neither kind (which cannot continue a chain)
            if (
                tip is None
                or _argv_at(span, 3) != tip[0]
                or _argv_at(span, 1) != tip[1]
                or not span.attempt_hash
            ):
                issues.append(
                    JournalIssue(
                        code="followup-chain",
                        line=number,
                        message=f"{cpath.name}: follow-up {span.span_id!r} does not continue "
                        "the record before it (a missing commit record, a gap in the range, "
                        "or no sidecar)",
                    )
                )
            tip = (span.record_hash, _argv_at(span, 2))
    return issues


def _argv_at(span: SpanRecord, index: int) -> str:
    """`span.argv[index]`, or "" when the span has fewer arguments."""
    return span.argv[index] if len(span.argv) > index else ""


def _audit_list_issues(
    path: Path,
    in_run: Sequence[tuple[int, SpanRecord]],
    outcome_line: int,
    outcome: SpanRecord,
) -> list[JournalIssue]:
    """Hold a run's audit records before its outcome to the sealed set.

    An outcome sealed before the list existed has none and is not judged
    here (scope stated in the commit that added the list): rewriting a sealed sidecar to
    drop the key reseals the outcome, which the branch anchor catches.
    """
    listed = _listed_hashes(path, outcome, AUDIT_SPAN_HASHES)
    if listed is None:
        return []
    present = [
        (number, span)
        for number, span in in_run
        if number < outcome_line and span.name.startswith(AUDIT_SPAN_PREFIXES)
    ]
    hashes = {span.record_hash for _, span in present}
    issues = [
        JournalIssue(
            code="audit-span-missing",
            line=outcome_line,
            message=f"outcome span {outcome.span_id!r} lists audit record "
            f"(record_hash {digest}) that is not in the journal",
        )
        for digest in listed
        if digest not in hashes
    ]
    issues.extend(
        JournalIssue(
            code="audit-span-unlisted",
            line=number,
            message=f"audit record {span.span_id!r} ({span.name}) is not in the audit list "
            f"of outcome span {outcome.span_id!r}",
        )
        for number, span in present
        if span.record_hash not in listed
    )
    return issues


def run_audit_hashes(path: Path, run_span: str) -> list[str]:
    """The `record_hash` of every audit record in run `run_span`, in journal order.

    The same membership `_auto_run_issues` judges: a span citing the start
    span as parent, or any span after the start line and before the next
    start line. Read by the engine when it seals the outcome, so it
    never raises on a line it cannot read (verify reports those), and a run
    whose start span is not in this journal keeps only the spans citing it.
    """
    spans: list[tuple[int, SpanRecord]] = []
    text = path.read_text(encoding="utf-8") if path.exists() else ""
    for number, line in enumerate(text.splitlines(), start=1):
        try:
            raw = json.loads(line)
            if isinstance(raw, dict) and raw.get("record_type") == "span":
                spans.append((number, SpanRecord.model_validate(raw)))
        except ValueError:  # pydantic's ValidationError is one
            continue
    start_line = next((n for n, s in spans if s.span_id == run_span), None)
    starts = [n for n, s in spans if s.kind == "agent" and s.name == AUTO_START]
    end_line = next((n for n in starts if start_line is not None and n > start_line), None)
    return [
        span.record_hash
        for number, span in spans
        if span.name.startswith(AUDIT_SPAN_PREFIXES)
        and (
            span.parent_id == run_span
            or (start_line is not None and _within(number, start_line, end_line))
        )
    ]


def _within(number: int, start_line: int, end_line: int | None) -> bool:
    """Whether journal line `number` lies after a run's start and before the next one."""
    return number > start_line and (end_line is None or number < end_line)


def _listed_span_hashes(path: Path, outcome: SpanRecord) -> list[str] | None:
    """The outcome sidecar's `tool_span_hashes`, or None when it has no such list."""
    return _listed_hashes(path, outcome, TOOL_SPAN_HASHES)


def _listed_hashes(path: Path, outcome: SpanRecord, key: str) -> list[str] | None:
    """The outcome sidecar's list under `key`, or None when it has no such list."""
    if not outcome.attempt_hash:
        return None
    try:
        evidence = json.loads(attempt_sidecar_path(path, outcome.span_id).read_bytes())
    except (OSError, ValueError):
        return None
    listed = evidence.get(key) if isinstance(evidence, dict) else None
    if not isinstance(listed, list) or not all(isinstance(item, str) for item in listed):
        return None
    return listed


def _sidecar_diff_mismatch(sidecar: Path) -> bool:
    """Whether a hashing sidecar retains a `diff` that is not its `diff_hash`."""
    try:
        evidence = json.loads(sidecar.read_bytes())
    except (OSError, ValueError):
        return False
    if not isinstance(evidence, dict):
        return False
    diff, diff_hash = evidence.get("diff"), evidence.get("diff_hash")
    if not isinstance(diff, str) or not isinstance(diff_hash, str):
        return False
    return hashlib.sha256(diff.encode()).hexdigest() != diff_hash


def verify_journal(path: Path) -> list[JournalIssue]:
    """Verify the journal by recomputation; empty list means valid.

    A coverage file beside it (`coverage_path`) is verified and bound to it too.
    """
    _, _, issues, _ = _load_journal(path)
    return issues + _coverage_issues(path)


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
    to see which task and which node the proof was sealed for.
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
    for an auditor. `tree_hash` is the worktree the gate passed
    on, so a resume can check it is resuming onto that tree.
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
