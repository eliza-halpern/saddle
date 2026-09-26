"""The evidence packet: an autonomous run's report, compiled from its ledger.

The Daily Driver page's rule is "the model writes the code; only the ledger
writes the claims". This module is that rule as code. `compile_packet` is a
pure function of a run's `proofs.jsonl` and the sidecars its spans hash, and
`check_packet` refuses any packet in which a claim does not cite a record
that is in the ledger.

Rows follow the page's layout: verdict, then Contract, Tests, Mutation,
Scope, Audit, Not proven, Narrative, Cost, Reproduce. A row whose evidence
does not exist says so (status `absent`) instead of being left out, because
a missing row reads as a row nobody thought of, and an absent one reads as
a check nobody ran.

What the packet can and cannot say for an executor-only run (arm E):

- It can say what the executor *did*: which commands it ran and the exit
  codes the tools saw, which edits the tier-0 guard refused, which files
  changed, what it cost. Those are `observed`: sealed facts about the run.
- It cannot say the change is correct. Only an auditor verdict is
  `proven`: the real auditor's `audit-tier<N>:<gate>` finding spans (arms
  E+A and E+A+F, via `feed.AuditFeed`; the latest per gate, i.e. the last
  tree audited) or a chat seam's `audit:<gate>` span. The feed's
  `audit:delivered`/`audit:withheld` records are deliveries, not verdicts.
  With none, "finished" here never reads as "done".
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Iterable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Final, Literal

from saddle.anchor import anchor_issues
from saddle.journal import (
    AUDIT_SPAN_PREFIXES,
    ProofRecord,
    SpanRecord,
    attempt_sidecar_path,
    read_entries,
    verify_journal,
)
from saddle.transcript import FEED_SPANS, start_field, tier_finding

CHECK_SPAN: Final = "audit:check"
"""A `check` call's record (`feed.CHECK_SPAN`); spelled here so the packet
stays a reader of the ledger."""

Status = Literal["proven", "failed", "observed", "absent", "not-proven", "narrative", "cost"]

CLAIMS: Final = frozenset({"proven", "failed", "observed", "cost"})
"""Statuses that assert something about the run. Each needs a cite."""

NARRATIVE_LABEL: Final = "narrative, not evidence"

TEST_COMMAND: Final = re.compile(r"\b(pytest|py\.test|unittest|tox|nox|make\s+test|check\.sh)\b")

# A sentence that asserts a check's result: a check noun and a result word in
# the same sentence. Deliberately lexical and deliberately greedy -- see
# `flag_narrative` for what that buys and what it misses.
_CHECK_NOUN: Final = re.compile(
    r"\b(tests?|suite|pytest|coverage|covered|mutants?|mutation|lint|ruff|mypy|"
    r"checks?|gates?|build|ci|type[- ]?check\w*)\b",
    re.IGNORECASE,
)
_RESULT_WORD: Final = re.compile(
    r"\b(pass(es|ed|ing)?|green|fail(s|ed|ing)?|red|clean|killed|succeed(s|ed)?|"
    r"verified|works?|working|all\s+good)\b|\b\d+(\.\d+)?\s*%",
    re.IGNORECASE,
)
_SENTENCE: Final = re.compile(r"(?<=[.!?])\s+|\n+")


class PacketError(ValueError):
    """A packet that makes a claim the ledger does not back."""


@dataclass(frozen=True)
class Row:
    key: str
    title: str
    status: Status
    text: str
    cites: tuple[str, ...] = ()
    items: tuple[str, ...] = ()


@dataclass(frozen=True)
class Sentence:
    text: str
    flagged: bool


@dataclass(frozen=True)
class Packet:
    run_id: str
    task: str
    verdict: str
    """finished, stopped, needs_you, or unrecorded: from the outcome span only."""
    verdict_text: str
    header: tuple[str, ...]
    rows: tuple[Row, ...]
    narrative: tuple[Sentence, ...] = ()
    records: dict[str, dict[str, Any]] = field(default_factory=dict)
    """Every cited record, by hash, as display fields: what a cite opens."""
    test_edits: bool | None = None
    """Whether the run could edit tests, from its `auto:start` span; None if unrecorded."""
    offer_test_edits: bool = False
    """Stopped "audit unresolved" with tests read-only on a finding a test
    closes: the card offers the same task again with test edits allowed."""

    def payload(self) -> dict[str, Any]:
        return {
            "run_id": self.run_id,
            "task": self.task,
            "verdict": self.verdict,
            "verdict_text": self.verdict_text,
            "header": list(self.header),
            "rows": [
                {
                    "key": row.key,
                    "title": row.title,
                    "status": row.status,
                    "text": row.text,
                    "cites": list(row.cites),
                    "items": list(row.items),
                }
                for row in self.rows
            ],
            "narrative": [{"text": s.text, "flagged": s.flagged} for s in self.narrative],
            "narrative_label": NARRATIVE_LABEL,
            "records": self.records,
            "test_edits": self.test_edits,
            "offer_test_edits": self.offer_test_edits,
        }


def flag_narrative(text: str) -> tuple[Sentence, ...]:
    """Split the model's narrative into sentences; flag those asserting a check.

    The simplest honest version of the page's "check that rejects narrative
    sentences asserting a result": a sentence is flagged when it names a
    check (tests, coverage, mutation, lint, ...) and a result (passes, green,
    fails, 100%, ...). A flagged sentence is kept -- the narrative is the
    model's and is shown whole -- but it is rendered as rejected.

    Limits, stated so nobody mistakes this for more than it is:
    - It is English and lexical. "Everything is fine now" or "I confirmed the
      behaviour" asserts a result with no check noun and is not flagged.
    - It over-flags: "I fixed the failing test" names a check and a result
      and is flagged although it reports an edit. Erring that way costs a
      struck-through sentence; the other way costs a claim read as evidence.
    - It judges sentences, not meaning: a result asserted across two
      sentences ("I ran pytest. Everything passed.") is missed in the second.
    """
    sentences = [part.strip() for part in _SENTENCE.split(text) if part and part.strip()]
    return tuple(
        Sentence(s, bool(_CHECK_NOUN.search(s) and _RESULT_WORD.search(s))) for s in sentences
    )


def check_packet(packet: Packet, ledger: Iterable[str]) -> None:
    """Refuse a packet with a claim that does not cite the ledger.

    Every row whose status is a claim must cite at least one record, and
    every cite on every row must be a record hash in `ledger`. The narrative
    row may carry no evidence status at all.
    """
    known = set(ledger)
    for row in packet.rows:
        if row.status in CLAIMS and not row.cites:
            msg = f"row {row.key!r} claims {row.status!r} with no cite"
            raise PacketError(msg)
        for cite in row.cites:
            if cite not in known:
                msg = f"row {row.key!r} cites {cite[:12]!r}, which is not in the ledger"
                raise PacketError(msg)
        if row.key == "narrative" and row.status != "narrative":
            msg = f"the narrative row is marked {row.status!r}; it is never evidence"
            raise PacketError(msg)


def _sidecar(journal: Path, span: SpanRecord) -> dict[str, Any] | None:
    """The span's sidecar, only if it hashes to the span's `attempt_hash`."""
    if not span.attempt_hash:
        return None
    try:
        raw = attempt_sidecar_path(journal, span.span_id).read_bytes()
    except OSError:
        return None
    if hashlib.sha256(raw).hexdigest() != span.attempt_hash:
        return None
    try:
        loaded = json.loads(raw)
    except ValueError:
        return None
    return loaded if isinstance(loaded, dict) else None


def _display(entry: ProofRecord | SpanRecord) -> dict[str, Any]:
    """A record as the fields a person reads, not the JSON line."""
    if isinstance(entry, ProofRecord):
        return {
            "type": "proof",
            "hash": entry.record_hash,
            "node": entry.node_id,
            "kind": entry.kind,
            "gates": len(entry.gate_outputs),
        }
    return {
        "type": f"{entry.kind} span",
        "hash": entry.record_hash,
        "name": entry.name,
        "exit": entry.exit_code,
        "duration_ms": entry.duration_ms,
        "detail": entry.detail[:600],
        "sidecar": entry.attempt_hash[:16] if entry.attempt_hash else "",
    }


def _command(span: SpanRecord) -> str:
    try:
        args = json.loads(span.argv[1]) if len(span.argv) > 1 else {}
    except ValueError:
        return ""
    return str(args.get("command", "")) if isinstance(args, dict) else ""


def _exit_of(detail: str) -> int | None:
    match = re.match(r"exit (-?\d+)", detail)
    return int(match.group(1)) if match else None


def _minutes(seconds: float) -> str:
    seconds = max(0.0, seconds)
    if seconds < 60:
        return f"{seconds:.0f}s"
    return f"{int(seconds // 60)}m {int(seconds % 60):02d}s"


def _tokens(n: float) -> str:
    return f"{n / 1000:.1f}k" if n >= 1000 else f"{int(n)}"


def _n(count: int, noun: str) -> str:
    return f"{count} {noun}" if count == 1 else f"{count} {noun}s"


def _shown(journal: Path) -> str:
    """The ledger's path from the repo root, where `.saddle/runs/<id>/` puts it."""
    parts = journal.parts
    if len(parts) >= 4 and parts[-4] == ".saddle" and parts[-3] == "runs":
        return str(Path(*parts[-4:]))
    return str(journal)


def _unrecorded(run_id: str, why: str) -> Packet:
    return Packet(
        run_id=run_id,
        task="",
        verdict="unrecorded",
        verdict_text=why,
        header=(),
        rows=(Row("not-proven", "Not proven", "not-proven", why),),
    )


@dataclass(frozen=True)
class _Audit:
    """One auditor verdict as the rows read it, citing the span it came from."""

    name: str
    detail: str
    exit_code: int
    record_hash: str
    verdict: str = ""
    """An auditor finding's own verdict (pass, fail, blocked); "" for a seam span."""
    body: str = ""
    """An auditor finding's own detail, without the tier prefix."""


def _audits(spans: Iterable[SpanRecord]) -> list[_Audit]:
    """Every auditor verdict in the ledger, in ledger order.

    A chat seam's `audit:<gate>` span is read as written. The real
    auditor's `audit-tier<N>:<gate>` spans are read from their sealed
    detail, and only the latest per gate is kept: each audit re-runs every
    gate of its tier on a newer tree, so an earlier checkpoint's failure
    that a later audit cleared is history, not a verdict on the change.
    """
    seam: list[tuple[int, _Audit]] = []
    latest: dict[str, tuple[int, _Audit]] = {}
    for order, span in enumerate(spans):
        finding = tier_finding(span.name, span.detail)
        if finding is not None:
            latest[finding.gate] = (
                order,
                _Audit(
                    f"audit:{finding.gate}",
                    f"tier {finding.tier}, {finding.verdict}: {finding.detail}",
                    span.exit_code,
                    span.record_hash,
                    finding.verdict,
                    finding.detail,
                ),
            )
        elif span.name.startswith("audit:") and span.name not in FEED_SPANS:
            seam.append((order, _Audit(span.name, span.detail, span.exit_code, span.record_hash)))
    return [a for _, a in sorted([*seam, *latest.values()], key=lambda pair: pair[0])]


AUDIT_UNRESOLVED: Final = "audit unresolved"
"""FEEDCAP's stop reason (`engine.AUDIT_UNRESOLVED`): finish refused on an
unchanged finding set until the cap. Spelled here, not imported, to keep
the packet a reader of the ledger rather than of the engine."""


@dataclass(frozen=True)
class _Spend:
    text: str
    estimated: bool
    gap: str


def _spend(evidence: dict[str, Any]) -> _Spend | None:
    """The run's token spend, saying whether it was measured.

    USAGE's sidecar has `tokens_spent` with `token_source` ("usage",
    "estimate", "mixed", "none") and `tokens_by_source`. A sidecar written
    before it has only `tokens_spent_estimate`: that is an estimate, and a
    missing count is "not recorded", never a measured 0.
    """
    source = evidence.get("token_source")
    spent = evidence.get("tokens_spent")
    if not isinstance(spent, int | float) or source not in ("usage", "estimate", "mixed", "none"):
        old = evidence.get("tokens_spent_estimate")
        if not isinstance(old, int | float):
            return None
        return _Spend(
            f"~{_tokens(float(old))} estimated",
            True,
            "Token counts are estimates (characters / 4): this run predates usage metering.",
        )
    if source == "usage":
        return _Spend(f"{_tokens(float(spent))} measured", False, "")
    if source == "mixed":
        by = evidence.get("tokens_by_source") or {}
        measured = by.get("usage", 0) if isinstance(by, dict) else 0
        return _Spend(
            f"{_tokens(float(spent))} ({_tokens(float(measured))} measured, rest estimated)",
            True,
            "Some rounds' token counts are estimates (characters / 4): the server "
            "reported no usage for them.",
        )
    if source == "none":
        return _Spend("0 (no model round)", False, "")
    return _Spend(
        f"~{_tokens(float(spent))} estimated",
        True,
        "Token counts are estimates (characters / 4): the server reported no usage.",
    )


def _unresolved(evidence: dict[str, Any]) -> list[str]:
    """FEEDCAP's `unresolved_findings` as "gate (reason)", skipping malformed entries."""
    found = evidence.get("unresolved_findings")
    return [
        f"{f.get('gate', '?')} ({f.get('reason', '?')})"
        for f in (found if isinstance(found, list) else [])
        if isinstance(f, dict)
    ]


TEST_CLOSES: Final = frozenset({"coverage", "evidence-thin"})
"""A finding gate or reason that a new test is the repair for."""


def _test_edits(start: SpanRecord | None) -> bool | None:
    """The run's test-edit policy as its `auto:start` span sealed it."""
    word = start_field(start.detail, "test edits") if start is not None else ""
    return {"allowed": True, "refused": False}.get(word)


def _needs_a_test(evidence: dict[str, Any]) -> bool:
    found = evidence.get("unresolved_findings")
    return any(
        isinstance(f, dict) and (f.get("gate") in TEST_CLOSES or f.get("reason") in TEST_CLOSES)
        for f in (found if isinstance(found, list) else [])
    )


def _anchor_text(journal: Path, repo: Path | None, *, sealed: bool) -> str:
    """The Reproduce row's sentence on the branch anchor, "" when it was not checked.

    With no outcome sealed yet (a run in flight, as the web page reads it)
    a clean check has nothing to match, so it says nothing; an anchor with
    no outcome behind it is still reported (FIX-4).
    """
    if repo is None:
        return ""
    found = anchor_issues(journal, repo)
    if not found:
        return (
            "Its outcome matches the Saddle-Outcome trailer on the run branch. " if sealed else ""
        )
    return f"The branch anchor does not match: {', '.join(i.code for i in found)}. "


def compile_packet(journal: Path, *, run_id: str = "", anchor_repo: Path | None = None) -> Packet:
    """The packet for one run, from its ledger alone. No model call.

    With `anchor_repo`, the Reproduce row also reports the check of the
    ledger's outcome against its branch's `Saddle-Outcome` trailer (ANCHOR).
    """
    run_id = run_id or journal.parent.name
    if not journal.exists():
        return _unrecorded(run_id, "There is no ledger for this run yet.")
    try:
        entries = read_entries(journal)
    except ValueError as exc:
        return _unrecorded(run_id, f"The ledger does not verify: {exc}")
    issues = [issue.code for issue in verify_journal(journal)]
    spans = [e for e in entries if isinstance(e, SpanRecord)]
    proofs = [e for e in entries if isinstance(e, ProofRecord)]
    start = next((s for s in spans if s.name == "auto:start"), None)
    outcome = next(
        (s for s in reversed(spans) if s.name in ("auto:finished", "auto:stopped")), None
    )
    # The model's tool calls: audit records are journaled as `tool` spans too,
    # but the auditor wrote them (FIX-1; the ledger's list excludes them alike).
    tools = [s for s in spans if s.kind == "tool" and not s.name.startswith(AUDIT_SPAN_PREFIXES)]
    refusals = [s for s in tools if s.name.startswith("refused:")]
    audits = _audits(spans)
    questions = [s for s in spans if s.name == "question"]
    answers = {s.parent_id: s for s in spans if s.name == "answer"}
    evidence = _sidecar(journal, outcome) if outcome is not None else None
    # The list is the last refusal's; only FEEDCAP's stop makes it the verdict.
    capped = outcome is not None and outcome.detail.startswith(f"stopped: {AUDIT_UNRESOLVED}")
    unresolved = _unresolved(evidence) if capped and evidence is not None else []
    task = start.argv[1] if start is not None and len(start.argv) > 1 else ""
    test_edits = _test_edits(start)
    offer_test_edits = (
        capped and test_edits is False and evidence is not None and _needs_a_test(evidence)
    )

    # -- verdict: from the outcome span, and nothing else ---------------------
    unanswered = [q for q in questions if q.span_id not in answers]
    if outcome is None:
        verdict, verdict_text = (
            ("needs_you", f"Waiting on your answer: {unanswered[-1].detail}")
            if unanswered
            else (
                "unrecorded",
                "No finish or stop record is in the ledger, so this run has no outcome yet.",
            )
        )
    elif outcome.name == "auto:finished":
        failed_audits = [a for a in audits if a.exit_code != 0]
        verdict = "finished"
        verdict_text = (
            f"Finished, but {_n(len(failed_audits), 'audit finding')} failed."
            if failed_audits
            else "The executor called finish. No auditor verdict covers the change, "
            "so this is finished, not proven done."
            if not audits
            else "The executor called finish, and every audit finding recorded passed."
        )
    else:
        verdict = "stopped"
        reason = outcome.detail.split(";")[0].removeprefix("stopped: ")
        if unresolved:
            reason += f" (finish refused on the same findings: {', '.join(unresolved)})"
        verdict_text = f"Stopped: {reason}. It did not finish, and nothing here says it did."

    rows: list[Row] = []

    # -- contract ---------------------------------------------------------------
    decided = tuple(
        f"You were asked: {q.detail} → you answered: {answers[q.span_id].detail}"
        for q in questions
        if q.span_id in answers
    )
    if decided:
        rows.append(
            Row(
                "contract",
                "Contract",
                "observed",
                "No contract was sealed (the Small lane writes none). "
                "The decisions you made during the run are recorded:",
                tuple(
                    c
                    for q in questions
                    if q.span_id in answers
                    for c in (q.record_hash, answers[q.span_id].record_hash)
                ),
                decided,
            )
        )
    else:
        rows.append(
            Row(
                "contract",
                "Contract",
                "absent",
                "No contract was sealed. The Small lane writes none, and no question "
                "was put to you during the run.",
            )
        )

    # -- tests --------------------------------------------------------------------
    test_audits = [
        a for a in audits if a.name in ("audit:tests", "audit:suite", "audit:full-suite")
    ]
    runs = [
        s
        for s in tools
        if s.argv and s.argv[0] == "run_command" and TEST_COMMAND.search(_command(s))
    ]
    if test_audits:
        last = test_audits[-1]
        rows.append(
            Row(
                "tests",
                "Tests",
                "proven" if last.exit_code == 0 else "failed",
                f"The auditor ran the suite: {last.detail}",
                (last.record_hash,),
            )
        )
    elif runs:
        last = runs[-1]
        code = _exit_of(last.detail)
        said = f"exit {code}" if code is not None else "no exit code recorded"
        rows.append(
            Row(
                "tests",
                "Tests",
                "observed",
                f"The executor last ran `{_command(last)}` → {said}. "
                f"That is its own run ({_n(len(runs), 'test command')} in all), "
                "not an auditor verdict.",
                (last.record_hash,),
            )
        )
    else:
        rows.append(
            Row("tests", "Tests", "absent", "The executor never ran the tests, and no auditor did.")
        )

    # -- mutation -------------------------------------------------------------------
    mutation = [a for a in audits if a.name == "audit:mutation"]
    if mutation and mutation[-1].verdict == "blocked":
        # Tier 2 never ran: tier 1 failed on that tree. There is no mutation
        # result to call failed (FIX-3); the auditor's detail names the cause.
        last = mutation[-1]
        rows.append(
            Row("mutation", "Mutation", "not-proven", f"blocked: {last.body}", (last.record_hash,))
        )
    elif mutation:
        last = mutation[-1]
        rows.append(
            Row(
                "mutation",
                "Mutation",
                "proven" if last.exit_code == 0 else "failed",
                last.detail,
                (last.record_hash,),
            )
        )
    else:
        rows.append(
            Row(
                "mutation",
                "Mutation",
                "absent",
                "No mutation record. Changed-line mutation is a tier-2 audit, and no "
                "auditor has written one to this ledger.",
            )
        )

    # -- scope ------------------------------------------------------------------------
    files = list(evidence.get("files_changed", [])) if evidence else []
    if outcome is not None and evidence is not None:
        refused_text = (
            f" The tier-0 guard refused {_n(len(refusals), 'edit')}."
            if refusals
            else " The tier-0 guard refused nothing."
        )
        if test_edits is not None:
            refused_text += " Tests were editable." if test_edits else " Tests were read-only."
        rows.append(
            Row(
                "scope",
                "Scope",
                "observed",
                f"{_n(len(files), 'file')} changed on the run's branch.{refused_text}",
                (outcome.record_hash, *(r.record_hash for r in refusals)),
                tuple(files) + tuple(f"refused: {r.detail.split(': ', 2)[-1]}" for r in refusals),
            )
        )
    else:
        rows.append(
            Row("scope", "Scope", "absent", "No outcome record lists the files changed yet.")
        )

    # -- audit (the seam) ---------------------------------------------------------
    if audits:
        other = [a for a in audits if a not in test_audits and a not in mutation]
        if other:
            rows.append(
                Row(
                    "audit",
                    "Audit",
                    "proven" if all(a.exit_code == 0 for a in other) else "failed",
                    f"{sum(a.exit_code == 0 for a in other)} of "
                    f"{_n(len(other), 'finding')} passed.",
                    tuple(a.record_hash for a in other),
                    tuple(
                        f"{'✓' if a.exit_code == 0 else '✗'} "
                        f"{a.name.removeprefix('audit:')}: {a.detail}"
                        for a in other
                    ),
                )
            )

    # -- check (the model's pull, `--check-tool`) ---------------------------------
    checks = [s for s in spans if s.name == CHECK_SPAN]
    if checks:
        last_check = checks[-1]
        # `read_entries` above refuses a ledger whose sidecar does not hash,
        # so a check record that reaches here is the sealed one.
        record = _sidecar(journal, last_check) or {}
        failing = sum(
            f.get("verdict") in ("fail", "blocked") and f.get("reason") != "sanctioned"
            for f in record.get("findings", [])
        )
        rows.append(
            Row(
                "check",
                "Check",
                "observed",
                f"The model checked {_n(len(checks), 'time')}; last check: "
                f"{_n(failing, 'failing finding')}. A check is tiers 0 and 1 only; "
                "the finish audit is the verdict.",
                tuple(c.record_hash for c in checks),
            )
        )

    # -- not proven -----------------------------------------------------------------
    gaps: list[str] = []
    gap_cites: list[str] = []
    if not audits:
        gaps.append(
            "No auditor verdict: the suite, changed-line coverage and mutation "
            "were not checked by saddle."
        )
    if outcome is not None and outcome.name == "auto:stopped":
        gaps.append(
            "The run stopped before finishing: "
            f"{outcome.detail.split(';')[0].removeprefix('stopped: ')}."
        )
        gap_cites.append(outcome.record_hash)
    for q in unanswered:
        gaps.append(f"Unanswered question: {q.detail}")
        gap_cites.append(q.record_hash)
    if outcome is not None and evidence is None:
        gaps.append(
            "The outcome sidecar is missing or does not hash to its span; "
            "cost and files are unknown."
        )
        gap_cites.append(outcome.record_hash)
    if issues:
        gaps.append(f"The ledger reports: {', '.join(sorted(set(issues)))}.")
    spend = _spend(evidence) if evidence is not None else None
    if spend is not None and spend.estimated:
        gaps.append(spend.gap)
    for item in unresolved:
        gaps.append(f"Unresolved at finish: {item}.")
    if unresolved and outcome is not None:
        gap_cites.append(outcome.record_hash)
    rows.append(
        Row(
            "not-proven",
            "Not proven",
            "not-proven",
            "What this packet cannot vouch for:" if gaps else "Nothing is left unproven.",
            tuple(gap_cites),
            tuple(gaps),
        )
    )

    # -- narrative ------------------------------------------------------------------
    narrative_text = str(evidence.get("narrative", "")) if evidence else ""
    sentences = flag_narrative(narrative_text)
    flagged = sum(s.flagged for s in sentences)
    rows.append(
        Row(
            "narrative",
            "Narrative",
            "narrative",
            (
                "Written by the model and labelled as such. "
                + (
                    f"{_n(flagged, 'sentence')} asserting a check result struck through: "
                    "the rows above are the record."
                    if flagged
                    else "It asserts no check results."
                )
            )
            if sentences
            else "The model wrote no narrative (it did not call finish).",
        )
    )

    # -- cost -------------------------------------------------------------------------
    if outcome is not None and evidence is not None:
        rows.append(
            Row(
                "cost",
                "Cost",
                "cost",
                f"{_minutes(float(evidence.get('elapsed_s', 0)))} of "
                f"{_minutes(float(evidence.get('time_budget_s', 0)))} "
                f"· {spend.text if spend is not None else 'no spend recorded'} of "
                f"{_tokens(float(evidence.get('token_budget', 0)))} generated tokens "
                f"· {_n(len(tools), 'tool call')} over "
                f"{_n(int(evidence.get('rounds', 0)), 'round')}",
                (outcome.record_hash,),
            )
        )
    else:
        rows.append(Row("cost", "Cost", "absent", "No sealed budget record yet."))

    # -- reproduce ----------------------------------------------------------------------
    branch = start_field(start.detail, "branch") if start is not None else ""
    anchor = proofs[-1].record_hash if proofs else (start.record_hash if start else "")
    rows.append(
        Row(
            "reproduce",
            "Reproduce",
            "observed" if anchor else "absent",
            f"The ledger verifies ({_n(len(entries), 'record')}, "
            f"{'no issues' if not issues else str(len(issues)) + ' issue(s)'}). "
            f"{_anchor_text(journal, anchor_repo, sealed=outcome is not None)}"
            "Re-check it, and read the change:",
            (anchor,) if anchor else (),
            (
                f"saddle verify {_shown(journal)}{' --anchor' if anchor_repo is not None else ''}",
                *((f"git log -p main..{branch}",) if branch else ()),
            ),
        )
    )

    cited = {c for row in rows for c in row.cites}
    by_hash = {e.record_hash: e for e in entries}
    header = []
    if branch:
        header.append(f"branch {branch}")
    if evidence is not None:
        header.append(f"{_n(len(files), 'file')} changed")
    if test_edits is not None:
        header.append("tests editable" if test_edits else "tests read-only")
    header.append(f"{_n(len(proofs), 'proof')} · {_n(len(entries), 'ledger record')}")
    packet = Packet(
        run_id=run_id,
        task=task,
        verdict=verdict,
        verdict_text=verdict_text,
        header=tuple(header),
        rows=tuple(rows),
        narrative=sentences,
        test_edits=test_edits,
        offer_test_edits=offer_test_edits,
        records={
            h: _display(e)
            for h in cited
            if isinstance(e := by_hash.get(h), ProofRecord | SpanRecord)
        },
    )
    check_packet(packet, by_hash)
    return packet


def render_packet_text(packet: Packet) -> str:
    """The packet as plain text: the recap a chat's context gets (T5-7 (3), T5-9).

    Deterministic, so the same ledger always gives the same bytes.
    """
    lines = [f"verdict: {packet.verdict} — {packet.verdict_text}"]
    lines.extend(f"  {h}" for h in packet.header)
    for row in packet.rows:
        if row.key == "narrative":
            continue
        lines.append(f"{row.title} [{row.status}]: {row.text}")
        lines.extend(f"  - {item}" for item in row.items)
    return "\n".join(lines)
