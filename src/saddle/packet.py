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
- A tier-0 finding (`audit-tier0:<gate>`: syntax, ruff, imports on one
  edited file; its record names the file under `path`) is an edit check,
  not an audit verdict. It is counted on its own "Edit checks" row,
  per gate per file, never in the Audit row or the verdict line (a run
  with 9 verdicts and 3 edit checks once read "12 of 12"). With more than
  one file, the row names each, so a failure on one cannot read under a
  pass on another.
"""

from __future__ import annotations

import ast
import dataclasses
import hashlib
import json
import re
from collections.abc import Iterable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Final, Literal

from saddle import coverage_text, mutant_text, prompt_constants
from saddle.anchor import anchor_issues
from saddle.journal import (
    AUDIT_QUESTION_STOP,
    AUDIT_SPAN_PREFIXES,
    AUTO_OUTCOMES,
    GUARDED_STOP_PREFIX,
    P1_EXTRACT_SPAN,
    PREMISE_DISPUTED_STOP,
    REFUSED_STOP,
    SEALED_CUT,
    STALL_STOP,
    ProofRecord,
    SpanRecord,
    attempt_sidecar_path,
    read_entries,
    redact_secrets,
    verify_journal,
)
from saddle.transcript import FEED_SPANS, start_field, tier_finding

CHECK_SPAN: Final = "audit:check"
"""A `check` call's record (`feed.CHECK_SPAN`); spelled here so the packet
stays a reader of the ledger."""

Status = Literal[
    "proven",
    "failed",
    "observed",
    "absent",
    "not-proven",
    "question",
    "sanctioned",
    "narrative",
    "cost",
]

CLAIMS: Final = frozenset({"proven", "failed", "observed", "question", "cost"})
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
    summary: str = ""
    """Lines beneath the row's text, compiled from the record the row cites:
    the Mutation row's English (`mutant_text.render_text`) when its finding
    span seals a `MutationOutcome`, the Audit row's coverage English
    (`coverage_text.render_coverage`) when a failing coverage finding seals
    its sources; "" otherwise, and then left out of the payload, so a packet
    without one is byte-identical to before this field. This is the FULL
    rendering: the web packet's fold shows it, and only the fold."""
    recap: str = ""
    """The same summary's COMPACT rendering (`compact=True`): what the
    terminal recap and the chat pre-fill print, capped so the recap does not
    scroll (user decision, 2026-09-26). "" exactly when `summary` is."""


@dataclass(frozen=True)
class Sentence:
    text: str
    flagged: bool


@dataclass(frozen=True)
class Packet:
    run_id: str
    task: str
    verdict: str
    """finished, stopped, unchanged, needs_you, or unrecorded: from the outcome span only."""
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
    questions: tuple[str, ...] = ()
    """A run that ended needing you (verdict `needs_you`): each question its
    finish audit asked, whole (`_asked`). Empty, and absent from the payload,
    otherwise."""
    guarded_paths: tuple[str, ...] = ()
    """A run the self-guard held (its finish audit accepted the tree, but it
    changed saddle's judges): the guarded paths it changed, from the sealed
    outcome. Only a person may land such a run (`branch_actions.approve_merge`).
    Empty, and absent from the payload, otherwise."""
    spend: dict[str, float] | None = None
    """The sealed outcome's own numbers -- `elapsed_s`, `time_budget_s`,
    `tokens`, `token_budget` -- for the card's meters; None without an
    outcome record. The Cost row says the same in words."""

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
                    **({"summary": row.summary} if row.summary else {}),
                }
                for row in self.rows
            ],
            "narrative": [{"text": s.text, "flagged": s.flagged} for s in self.narrative],
            "narrative_label": NARRATIVE_LABEL,
            "records": self.records,
            "test_edits": self.test_edits,
            "offer_test_edits": self.offer_test_edits,
            "spend": self.spend,
            **({"questions": list(self.questions)} if self.questions else {}),
            **({"guarded_paths": list(self.guarded_paths)} if self.guarded_paths else {}),
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


def _killers(outcome: dict[str, Any]) -> dict[str, str] | None:
    """The record's `killers` as `{mutant: test}`, spelled as a map or as pairs."""
    killers = outcome.get("killers")
    if isinstance(killers, dict):
        return {str(k): str(v) for k, v in killers.items()}
    if isinstance(killers, list):
        pairs = [p for p in killers if isinstance(p, list | tuple) and len(p) == 2]
        return {str(k): str(v) for k, v in pairs}
    return None


def _sealed(journal: Path, span: SpanRecord | None, *keys: str) -> dict[str, Any] | None:
    """A finding span's sealed sidecar when it holds every one of `keys`, else None.

    The auditor seals evidence beside a finding (`Auditor._journal`):
    `asdict(MutationOutcome)` on `audit-tier2:mutation`, `{"sources", "changed"}`
    on a failing `audit-tier1:coverage`. `_sidecar` refuses one that does not
    hash. A record with none (a seam span, a blocked tier, an older ledger)
    yields None, and the row then reads exactly as before.
    """
    sealed = _sidecar(journal, span) if span is not None else None
    if sealed is None or not set(keys) <= sealed.keys():
        return None
    return sealed


def _mutation_summary(journal: Path, span: SpanRecord | None) -> mutant_text.MutationSummary | None:
    """The mutation finding's summary from the outcome sealed in its span."""
    outcome = _sealed(journal, span, "killed", "total")
    if outcome is None:
        return None
    return mutant_text.describe_mutation(outcome, _killers(outcome))


def _coverage_summary(
    journal: Path, span: SpanRecord | None, mutation: mutant_text.MutationSummary | None
) -> coverage_text.CoverageSummary | None:
    """A failing coverage finding's summary, from the sources and changed
    set sealed in its span, and the lines it names: whole from the same
    sidecar (`auditor.coverage_evidence`), which the span's own line, fitted
    to the ledger, may hold only part of; from the line itself in a ledger
    sealed before that. None when nothing is sealed there."""
    sealed = _sealed(journal, span, "sources", "changed")
    if sealed is None or span is None:
        return None
    named = sealed.get("uncovered")
    lines: list[tuple[str, int]] | None = None
    if isinstance(named, list):
        finding: Any = {"cites": [str(sealed.get("basis", ""))]}
        lines = [(str(f), int(n)) for f, n in named]
    else:
        try:
            finding = json.loads(span.detail)
        except ValueError:
            return None
        if not isinstance(finding, dict):
            return None
    sources = _sealed_sources(sealed["sources"])
    changed = [(str(f), int(n)) for f, n in sealed["changed"]]
    summary = coverage_text.describe_coverage(finding, sources, changed, mutation, lines=lines)
    # The sources are sealed verbatim, so that they parse; the lines a person
    # reads are redacted, as every other sealed string is.
    return dataclasses.replace(
        summary,
        gaps=tuple(
            dataclasses.replace(g, text=tuple((n, redact_secrets(t)) for n, t in g.text))
            for g in summary.gaps
        ),
    )


def _sealed_sources(raw: Any) -> dict[str, str]:
    """Sealed sources as text, keeping only files that still parse.

    The auditor seals each file as a list of lines (`coverage_evidence`),
    because the sidecar writer caps a string at 4000 characters. A file
    sealed some other way and cut by that cap does not parse; it is left
    out here, and `coverage_text` lists its lines as "not placed" rather
    than this module guessing or crashing on it.
    """
    if not isinstance(raw, dict):
        return {}
    sources: dict[str, str] = {}
    for name, value in raw.items():
        text = "\n".join(str(x) for x in value) + "\n" if isinstance(value, list) else str(value)
        try:
            ast.parse(text)
        except (SyntaxError, ValueError):
            continue
        sources[str(name)] = text
    return sources


def display_record(entry: ProofRecord | SpanRecord) -> dict[str, Any]:
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
    tier: int = -1
    """An auditor finding's tier; -1 for a seam span."""
    path: str = ""
    """The file a tier-0 finding checked, from the `path` its record sealed
    (`auditor.finding_body`); "" for a seam span, for tiers 1 and 2, and for
    a ledger sealed before the field."""
    reason: str = ""
    """An auditor finding's own reason; `sanctioned` is the task's waiver of
    a failing finding (`auditor.sanction`). "" when it does not parse."""


def _audits(spans: Iterable[SpanRecord], *, edit_checks: bool = False) -> list[_Audit]:
    """Every auditor verdict in the ledger, in ledger order.

    A chat seam's `audit:<gate>` span is read as written. The real
    auditor's `audit-tier<N>:<gate>` spans are read from their sealed
    detail, and only the latest per gate is kept: each audit re-runs every
    gate of its tier on a newer tree, so an earlier checkpoint's failure
    that a later audit cleared is history, not a verdict on the change.

    Tier 0 checks one edited file at the check and is not a verdict on the
    change: it is left out, and `edit_checks=True` returns only it instead.

    The latest kept is per gate and file, not per gate alone: tier 0 runs on
    every edited file, so a later file's pass on a gate must not mask an
    earlier file's failure. A tier-0 record names its file (`Finding.path`);
    tiers 1 and 2, and a ledger sealed before the field, name none, and for
    those the key is the gate, as before.
    """
    seam: list[tuple[int, _Audit]] = []
    latest: dict[tuple[str, str], tuple[int, _Audit]] = {}
    for order, span in enumerate(spans):
        # A line that does not parse reads its verdict from its exit code.
        finding = tier_finding(span.name, span.detail, span.exit_code)
        if finding is not None:
            if (finding.tier == 0) != edit_checks:
                continue
            latest[(finding.gate, finding.path)] = (
                order,
                _Audit(
                    f"audit:{finding.gate}",
                    f"tier {finding.tier}, {finding.verdict}: {finding.detail}",
                    span.exit_code,
                    span.record_hash,
                    finding.verdict,
                    finding.detail,
                    finding.tier,
                    finding.path,
                    finding.reason,
                ),
            )
        elif not edit_checks and span.name.startswith("audit:") and span.name not in FEED_SPANS:
            seam.append((order, _Audit(span.name, span.detail, span.exit_code, span.record_hash)))
    return [a for _, a in sorted([*seam, *latest.values()], key=lambda pair: pair[0])]


def _whole(audits: list[_Audit], evidence: dict[str, Any] | None) -> list[_Audit]:
    """Each finding its span sealed cut to fit (`SEALED_CUT`), read whole from
    the outcome sidecar's final audit when that audit holds the same gate and
    tier with the same verdict; every other finding as it was."""
    final = evidence.get("audit") if evidence is not None else None
    found = {
        (str(f.get("gate")), f.get("tier")): f
        for f in (final.get("findings") or [] if isinstance(final, dict) else [])
        if isinstance(f, dict)
    }
    out = []
    for a in audits:
        whole = found.get((a.name.removeprefix("audit:"), a.tier))
        if SEALED_CUT in a.body and whole is not None and whole.get("verdict") == a.verdict:
            body = str(whole.get("detail", ""))
            a = dataclasses.replace(a, body=body, detail=f"tier {a.tier}, {a.verdict}: {body}")
        out.append(a)
    return out


P1_TITLE: Final = "Task text"


def _p1_row(
    journal: Path,
    start: SpanRecord | None,
    spans: list[SpanRecord],
    audits: list[_Audit],
    evidence: dict[str, Any] | None,
    span_by_hash: dict[str, SpanRecord],
) -> Row | None:
    """What P1 did in this run, when `auto:start` says it was on; else None,
    and the packet reads exactly as before.

    Always `observed` (or `absent`): the Audit row carries P1's verdict; this
    row is the measurement -- that it ran, how long the extraction took, and
    how many candidate units the last P1 finding judged, asked about, or left
    unjudged (`auditor.p1_tally`). A question is never a pass or a fail here.
    """
    if start is None or not start_field(start.detail, "task requirements"):
        return None
    extracted = [s for s in spans if s.name == P1_EXTRACT_SPAN][-1:]
    p1 = [a for a in audits if a.name == "audit:task-requirements"][-1:]
    final = evidence.get("audit") if evidence is not None else None
    whole = next(
        (
            f
            for f in (final.get("findings") or [] if isinstance(final, dict) else [])
            if isinstance(f, dict) and f.get("gate") == "task-requirements"
        ),
        None,
    )
    said: list[str] = []
    cites: list[str] = []
    items: list[str] = []
    strength = "question strength"
    if extracted:
        span = extracted[0]
        cites.append(span.record_hash)
        took = f"{span.duration_ms / 1000:.1f} s"
        if span.exit_code == 0:
            said.append(f"Extraction took {took}: {span.detail}.")
        else:
            said.append(f"Extraction failed after {took}.")
            items.append(f"Extraction failed: {span.detail}")
    else:
        given = start_field(start.detail, "task requirements")
        said.append(
            "No extraction ran: a sealed file was given."
            if not given.startswith("extracted")
            else "The extraction left no record."
        )
    tally = _sealed(journal, span_by_hash.get(p1[0].record_hash), "units") if p1 else None
    if tally is not None:
        units = tally["units"]
        cites.append(p1[0].record_hash)
        strength = "full strength" if tally.get("strength") == "full" else strength
        said.append(
            f"The last P1 finding judged {units['judged']} of {units['total']} candidate "
            f"unit(s), asked about {units['asked']}, and left {units['unjudged']} unjudged."
        )
    else:
        said.append("No P1 finding judged any unit.")
    if whole is not None and whole.get("verdict") == "question":
        items.append(f"Asked: {whole.get('detail', '')}")
    elif p1 and p1[0].verdict == "question":
        items.append(f"Asked: {p1[0].body}")
    head = (
        f"P1 ran at {strength}: its findings ask a person, never refuse. "
        if strength == "question strength"
        else f"P1 ran at {strength}. "
    )
    return Row(
        "p1",
        P1_TITLE,
        "observed" if cites else "absent",
        head + " ".join(said),
        tuple(cites),
        tuple(items),
    )


def _asked(audits: list[_Audit], evidence: dict[str, Any] | None) -> tuple[str, ...]:
    """The questions a finish audit asked, whole: from the outcome sidecar's
    final audit (which also holds a question the feed raised itself, such as
    P1's unfinished extraction), else from the ledger's question findings."""
    final = evidence.get("audit") if evidence is not None else None
    found = [
        f"{f.get('gate')} (tier {f.get('tier')}): {f.get('detail', '')}"
        for f in (final.get("findings") or [] if isinstance(final, dict) else [])
        if isinstance(f, dict) and f.get("verdict") == "question"
    ]
    if found:
        return tuple(found)
    return tuple(
        f"{a.name.removeprefix('audit:')} (tier {a.tier}): {a.body}"
        for a in audits
        if a.verdict == "question"
    )


def _status(audit: _Audit) -> Status:
    """A finding's row status. Its verdict first: `not-proven` journals exit
    0 like `pass` (`auditor._JOURNAL_EXIT`), so the exit code alone would
    call an open shortlist survivor proven. A `fail` the task sanctioned
    (`auditor.sanction`) is not a failure of the run: it is `sanctioned`,
    reported but not held against it."""
    if audit.verdict == "not-proven":
        return "not-proven"
    if audit.verdict == "question":
        return "question"  # exit 4: neither proven nor failed; a person decides
    if audit.reason == "sanctioned":
        return "sanctioned"  # exit 1, but the task waived it: reported, not held
    return "proven" if audit.exit_code == 0 else "failed"


def _finished_but(failed: list[_Audit], blocked: list[_Audit]) -> str:
    """A finished run's verdict line when audit findings failed or were blocked."""
    said = [f"{_n(len(failed), 'audit finding')} failed"] if failed else []
    if blocked:
        why = "; ".join(dict.fromkeys(a.body for a in blocked))
        said.append(f"{_n(len(blocked), 'audit finding')} blocked (not run): {why}")
    return f"Finished, but {', and '.join(said)}."


def _finished_verdict(
    audits: list[_Audit],
    blocked: list[_Audit],
    failed: list[_Audit],
    sanctioned: list[_Audit],
    asked: int,
) -> str:
    """A finished run's verdict line. A failure or a block is the refusal
    (`_finished_but`). With neither, a finding the task sanctioned
    (`auditor.sanction`: a test rewrite it ordered) is named as reported, not
    held against the run -- it never reads as the failure the plain gate
    verdict is. Then an open not-proven, then a clean pass."""
    if failed or blocked:
        return _finished_but(failed, blocked)
    if not audits:
        return (
            "The executor called finish. No auditor verdict covers the change, "
            "so this is finished, not proven done."
        )
    unproven = [a for a in audits if a.verdict == "not-proven"]
    if unproven:
        said = (
            "The executor called finish. No audit finding failed; "
            f"{_n(len(unproven), 'finding')} could not be proven (they do not "
            "refuse finish)."
        )
    elif sanctioned:
        said = (
            f"The executor called finish. No audit finding refused it; "
            f"{_n(len(sanctioned), 'finding')} was sanctioned (a test rewrite the "
            "task ordered), reported, not held against the run."
        )
    else:
        said = "The executor called finish, and every audit finding recorded passed."
    if sanctioned and unproven:
        said += (
            f" {_n(len(sanctioned), 'finding')} was also sanctioned (a test "
            "rewrite the task ordered); it is reported, not held against the run."
        )
    if asked:
        said += f" {_n(asked, 'audit finding')} asked a question only you can answer."
    return said


_MARK: Final[dict[str, str]] = {
    "proven": "✓",
    "failed": "✗",
    "not-proven": "?",
    "question": "?",
    "sanctioned": "↪",
}

AUDIT_UNRESOLVED: Final = "audit unresolved"
"""The finish-refusal cap's stop reason (`engine.AUDIT_UNRESOLVED`): finish refused on an
unchanged finding set until the cap. Spelled here, not imported, to keep
the packet a reader of the ledger rather than of the engine."""


@dataclass(frozen=True)
class _Spend:
    text: str
    estimated: bool
    gap: str


def _spend(evidence: dict[str, Any]) -> _Spend | None:
    """The run's token spend, saying whether it was measured.

    The measured-usage sidecar has `tokens_spent` with `token_source` ("usage",
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


def _meters(evidence: dict[str, Any]) -> dict[str, float] | None:
    """The outcome sidecar's time and token numbers, for the card's meters.

    Only fields the sidecar holds as numbers; a sidecar with no elapsed time
    gives None, so a card never shows a sealed 0 that was not sealed.
    """
    tokens = evidence.get("tokens_spent", evidence.get("tokens_spent_estimate"))
    fields = {
        "elapsed_s": evidence.get("elapsed_s"),
        "time_budget_s": evidence.get("time_budget_s"),
        "tokens": tokens,
        "token_budget": evidence.get("token_budget"),
    }
    numbers = {
        k: float(v)
        for k, v in fields.items()
        if isinstance(v, int | float) and not isinstance(v, bool)
    }
    return numbers if "elapsed_s" in numbers else None


def _unresolved(evidence: dict[str, Any]) -> list[str]:
    """The capped stop's `unresolved_findings` as "gate (reason)", skipping malformed entries."""
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


UNANSWERED: Final = "unanswered"
"""`engine.UNANSWERED`: an answer span sealed with no reply, its default taken."""


def _decided(question: SpanRecord, answer: SpanRecord) -> str:
    if answer.argv[2:3] == [UNANSWERED]:
        return f"You were asked: {question.detail} → no answer came: {answer.detail}"
    return f"You were asked: {question.detail} → you answered: {answer.detail}"


def _needs_a_test(evidence: dict[str, Any]) -> bool:
    found = evidence.get("unresolved_findings")
    return any(
        isinstance(f, dict) and (f.get("gate") in TEST_CLOSES or f.get("reason") in TEST_CLOSES)
        for f in (found if isinstance(found, list) else [])
    )


def _change_log(branch: str, base: str) -> str:
    """The command that shows the run's change: from its recorded base to its branch.

    A ledger sealed before the base was recorded names none; the run
    commits once, on its own branch (`auto.run_auto`), so that commit alone
    is shown rather than guessing a branch name the repository may not have.
    """
    return f"git log -p {base}..{branch}" if base else f"git log -p -1 {branch}"


def _anchor_text(journal: Path, repo: Path | None, *, sealed: bool) -> str:
    """The Reproduce row's sentence on the branch anchor, "" when it was not checked.

    With no outcome sealed yet (a run in flight, as the web page reads it)
    a clean check has nothing to match, so it says nothing; an anchor with
    no outcome behind it is still reported.
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
    ledger's outcome against its branch's `Saddle-Outcome` trailer.
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
    outcome = next((s for s in reversed(spans) if s.name in AUTO_OUTCOMES), None)
    # The model's tool calls: audit records are journaled as `tool` spans too,
    # but the auditor wrote them (the ledger's list excludes them alike).
    tools = [s for s in spans if s.kind == "tool" and not s.name.startswith(AUDIT_SPAN_PREFIXES)]
    refusals = [s for s in tools if s.name.startswith("refused:")]
    audits = _audits(spans)
    edit_checks = _audits(spans, edit_checks=True)
    span_by_hash = {s.record_hash: s for s in spans}
    # The mutation finding's sealed outcome, described once: the Mutation row
    # renders it, and the coverage English says where a mutant also survived.
    mutation = [a for a in audits if a.name == "audit:mutation"]
    mutation_summary = (
        _mutation_summary(journal, span_by_hash.get(mutation[-1].record_hash))
        if mutation and mutation[-1].verdict != "blocked"
        else None
    )
    questions = [s for s in spans if s.name == "question"]
    answers = {s.parent_id: s for s in spans if s.name == "answer"}
    evidence = _sidecar(journal, outcome) if outcome is not None else None
    audits = _whole(audits, evidence)
    guarded: tuple[str, ...] = ()
    # The list is the last refusal's; only the capped stop makes it the verdict.
    capped = outcome is not None and outcome.detail.startswith(f"stopped: {AUDIT_UNRESOLVED}")
    unresolved = _unresolved(evidence) if capped and evidence is not None else []
    task = start.argv[1] if start is not None and len(start.argv) > 1 else ""
    test_edits = _test_edits(start)
    granted = evidence is not None and evidence.get("test_edits_granted") is True
    offer_test_edits = (
        capped
        and test_edits is False
        and not granted
        and evidence is not None
        and _needs_a_test(evidence)
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
        # A blocked finding never ran (tier 2 after a tier-1 failure on that
        # tree): it is reported as blocked, and only gates that ran and
        # failed count as failed. A sanctioned finding (a fail the task
        # waived) is neither: it is never the refusal the verdict line names.
        blocked = [a for a in audits if a.verdict == "blocked"]
        failed_audits = [
            a
            for a in audits
            if a.exit_code != 0
            and a.verdict not in ("blocked", "question")
            and a.reason != "sanctioned"
        ]
        sanctioned = [a for a in audits if a.reason == "sanctioned"]
        asked = sum(a.verdict == "question" for a in audits)
        verdict = "finished"
        verdict_text = _finished_verdict(audits, blocked, failed_audits, sanctioned, asked)
    elif outcome.name == "auto:stopped" and outcome.detail.startswith(
        f"stopped: {AUDIT_QUESTION_STOP}"
    ):
        # The finish audit accepted the tree with a question: the run ends
        # needing you. Neither finished (nothing says it is done) nor a stop
        # on a fault or a budget.
        verdict = "needs_you"
        # The count only: the questions themselves are listed whole (`questions`).
        asked_text = outcome.detail.split(";")[0].removeprefix("stopped: needs you: ")
        verdict_text = (
            f"Needs you: {asked_text.split(':')[0]}. The run did not finish: a question is "
            "neither a pass nor a refusal, and only you can decide it."
        )
    elif outcome.name == "auto:stopped" and outcome.detail.startswith(
        f"stopped: {PREMISE_DISPUTED_STOP}"
    ):
        # The model pulled the ripcord (`engine._dispute`): the task's premise
        # is false, with rerun evidence sealed in the outcome. A person decides.
        verdict = "needs_you"
        claim = outcome.detail.split(";")[0].removeprefix(f"stopped: {PREMISE_DISPUTED_STOP}")
        verdict_text = (
            f"Needs you: the model disputes the task's premise ({claim}). It made no "
            "claim of done: read its finding and the evidence saddle reran, then agree "
            "(close the task) or disagree (run it again with a note)."
        )
    elif outcome.name == "auto:stopped" and outcome.detail.startswith(f"stopped: {REFUSED_STOP}"):
        # The model declined the task (`engine._refuse`) -- harmful, out of scope,
        # or against policy. No evidence: a refusal is not a factual claim about the
        # code. A person decides whether the task was fair to ask.
        verdict = "needs_you"
        reason = outcome.detail.split(";")[0].removeprefix(f"stopped: {REFUSED_STOP}")
        verdict_text = (
            f"Needs you: the model refused the task ({reason}). It made no claim of done "
            "and did not dispute the premise; read its reason, then withdraw the task or "
            "re-scope it."
        )
    elif outcome.name == "auto:stopped" and outcome.detail.startswith(
        f"stopped: {GUARDED_STOP_PREFIX}"
    ):
        # The self-guard held a run whose finish audit accepted the tree: it
        # changed one of saddle's judges, so a person decides, not the run.
        verdict = "needs_you"
        held = evidence.get("guarded_paths") if evidence is not None else None
        guarded = tuple(str(p) for p in held) if isinstance(held, list) else ()
        which = ", ".join(guarded) if guarded else "saddle's judges"
        verdict_text = (
            f"Needs you: this run changed code that judges runs ({which}). Its finish "
            "audit accepted the tree, but the model may not approve changes to its own "
            "judges: review the diff, then approve and merge it, or discard it."
        )
    elif outcome.name == "auto:stopped" and outcome.detail.startswith(f"stopped: {STALL_STOP}"):
        # --stall-check ejected the run: it made no progress and stayed in its
        # reasoning past the warmup. A person reads the reasoning and decides;
        # not a fault or a budget stop, and nothing says it is done.
        verdict = "needs_you"
        detail = outcome.detail.split(";")[0].removeprefix(f"stopped: {STALL_STOP} -- ")
        verdict_text = (
            f"Needs you: the run stalled -- {detail} It made no edit and no dispute, so "
            "there is nothing to audit; read its reasoning, then answer it or run it again."
        )
    elif outcome.name == "auto:unchanged":
        verdict = "unchanged"
        verdict_text = (
            "Unchanged: finish was called on a tree equal to the baseline, so there was "
            "nothing to audit. No change was made; this is not finished and not a refusal."
        )
    else:
        verdict = "stopped"
        reason = outcome.detail.split(";")[0].removeprefix("stopped: ")
        if unresolved:
            reason += f" (finish refused on the same findings: {', '.join(unresolved)})"
        verdict_text = f"Stopped: {reason}. It did not finish, and nothing here says it did."

    rows: list[Row] = []

    # -- contract ---------------------------------------------------------------
    decided = tuple(_decided(q, answers[q.span_id]) for q in questions if q.span_id in answers)
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
    # The environment the gates ran the tests on, as `auto:start` sealed it
    # (`sandbox.gate_environment`); a ledger from before that field says nothing.
    ran_on = ""
    on_cite: tuple[str, ...] = ()
    if start is not None and (environment := start_field(start.detail, "environment")):
        ran_on = f". Tests ran on {environment.replace('%3B', ';')}."
        on_cite = (start.record_hash,)
        # An approved install (`installs`) layers the run's own environment on
        # that one; the tests that ran after it ran there, so the row says so.
        installed = [s for s in tools if s.argv and s.argv[0] == "install" and s.exit_code == 0]
        if installed:
            ran_on = (
                f"{ran_on[:-1]}, with {len(installed)} approved install(s) layered on it "
                "for the tests after them."
            )
            on_cite = (*on_cite, *(s.record_hash for s in installed))
    if test_audits:
        last = test_audits[-1]
        rows.append(
            Row(
                "tests",
                "Tests",
                "proven" if last.exit_code == 0 else "failed",
                f"The auditor ran the suite: {last.detail.rstrip('.')}{ran_on}"
                if ran_on
                else f"The auditor ran the suite: {last.detail}",
                (last.record_hash, *on_cite),
            )
        )
    elif runs:
        last_run = runs[-1]
        code = _exit_of(last_run.detail)
        exited = f"exit {code}" if code is not None else "no exit code recorded"
        rows.append(
            Row(
                "tests",
                "Tests",
                "observed",
                f"The executor last ran `{_command(last_run)}` → {exited}. "
                f"That is its own run ({_n(len(runs), 'test command')} in all), "
                "not an auditor verdict.",
                (last_run.record_hash,),
            )
        )
    else:
        rows.append(
            Row("tests", "Tests", "absent", "The executor never ran the tests, and no auditor did.")
        )

    # -- mutation -------------------------------------------------------------------
    if mutation and mutation[-1].verdict == "blocked":
        # Tier 2 never ran: tier 1 failed on that tree. There is no mutation
        # result to call failed; the auditor's detail names the cause.
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
                _status(last),
                last.detail,
                (last.record_hash,),
                summary=mutant_text.render_text(mutation_summary) if mutation_summary else "",
                recap=(
                    mutant_text.render_text(mutation_summary, compact=True)
                    if mutation_summary
                    else ""
                ),
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
            refused_text += (
                " Tests were editable."
                if test_edits
                else " Tests were read-only until you allowed edits during the run."
                if granted
                else " Tests were read-only."
            )
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
        # A failing coverage finding's English, from what the auditor
        # sealed beside it, beneath the Audit row's items (inside its fold on
        # the web). A passing or absent finding, or one with nothing sealed,
        # adds nothing; there is no Coverage row of its own.
        # A not-proven one (`--tier2 shortlist`, exit 0) names the same lines.
        coverage = next(
            (
                a
                for a in other
                if a.name == "audit:coverage" and (a.exit_code != 0 or a.verdict == "not-proven")
            ),
            None,
        )
        coverage_summary = (
            _coverage_summary(journal, span_by_hash.get(coverage.record_hash), mutation_summary)
            if coverage is not None
            else None
        )
        if other:
            statuses = [_status(a) for a in other]
            unproven = statuses.count("not-proven")
            asked = statuses.count("question")
            # A sanctioned finding is a fail the task waived: neither a
            # failure that holds the row against the run nor a pass. It is
            # its own status, below a not-proven one and above a plain pass.
            waived = statuses.count("sanctioned")
            rows.append(
                Row(
                    "audit",
                    "Audit",
                    "failed"
                    if "failed" in statuses
                    else "question"
                    if asked
                    else "not-proven"
                    if unproven
                    else "sanctioned"
                    if waived
                    else "proven",
                    f"{statuses.count('proven')} of {_n(len(other), 'finding')} passed"
                    + (f", {unproven} not proven" if unproven else "")
                    + (f", {asked} need you" if asked else "")
                    + (f", {waived} sanctioned" if waived else "")
                    + ".",
                    tuple(a.record_hash for a in other),
                    tuple(
                        f"{_MARK[st]} {a.name.removeprefix('audit:')}: {a.detail}"
                        for a, st in zip(other, statuses, strict=True)
                    ),
                    summary=(
                        coverage_text.render_coverage(coverage_summary) if coverage_summary else ""
                    ),
                    recap=(
                        coverage_text.render_coverage(coverage_summary, compact=True)
                        if coverage_summary
                        else ""
                    ),
                )
            )

    # -- task text (P1, when the run turned it on) -----------------------------------
    p1_row = _p1_row(journal, start, spans, audits, evidence, span_by_hash)
    if p1_row is not None:
        rows.append(p1_row)

    # -- edit checks (tier 0: every edited file; not a verdict) -------------------------
    if edit_checks:
        passed = sum(a.exit_code == 0 for a in edit_checks)
        files = sorted({a.path for a in edit_checks if a.path})
        named = len(files) > 1
        # More than one file: name each in the text and in its item, or a
        # failure on one file reads under the other's pass. One file (or a
        # ledger sealed before the records learned the file) renders exactly
        # as before, byte for byte.
        rows.append(
            Row(
                "edit-checks",
                "Edit checks",
                # Never "proven": an edit check is not an audit verdict. A
                # failing one is still a failed record (and refuses a merge).
                "observed" if passed == len(edit_checks) else "failed",
                (
                    f"{passed} of {_n(len(edit_checks), 'edit check')} passed. Tier 0 checks "
                    + (
                        f"each edited file (syntax, ruff, imports) when it is written: "
                        f"{', '.join(files)}; it is not an audit verdict and is not counted "
                        "in Audit."
                        if named
                        else "one edited file (syntax, ruff, imports) when it is written; "
                        "it is not an audit verdict and is not counted in Audit."
                    )
                ),
                tuple(a.record_hash for a in edit_checks),
                tuple(
                    (
                        f"{'✓' if a.exit_code == 0 else '✗'} "
                        f"{a.name.removeprefix('audit:')} ({a.path}): {a.detail}"
                        if named and a.path
                        else f"{'✓' if a.exit_code == 0 else '✗'} "
                        f"{a.name.removeprefix('audit:')}: {a.detail}"
                    )
                    for a in edit_checks
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
    if audits and not mutation:
        # The Mutation row above says "No mutation record"; Not proven must
        # not then read "Nothing is left unproven" beside it.
        gaps.append("Changed lines were not mutation-tested: no mutation record.")
    if outcome is not None and outcome.name == "auto:stopped":
        stop_reason = outcome.detail.split(";")[0].removeprefix("stopped: ")
        gaps.append(
            (
                "The run ended needing you before finishing: "
                if verdict == "needs_you"
                else "The run stopped before finishing: "
            )
            # A reason that already ends a sentence (the stall's) gets no second period.
            + (stop_reason if stop_reason.endswith(".") else f"{stop_reason}.")
        )
        gap_cites.append(outcome.record_hash)
    if outcome is not None and outcome.name == "auto:unchanged":
        gaps.append("No change was made: the tree equals the baseline, so nothing was audited.")
        gap_cites.append(outcome.record_hash)
    # Each baseline definition the coverage gate did not judge
    # (sealed by the auditor from the gate's `spared-defs` basis), whatever
    # the finding's verdict: a pass is where they would otherwise hide.
    for cov in [a for a in audits if a.name == "audit:coverage"][-1:]:
        sealed_cov = _sealed(journal, span_by_hash.get(cov.record_hash), "spared")
        for name in sealed_cov["spared"] if sealed_cov is not None else []:
            gaps.append(
                f"{name} was not judged by coverage: a definition the baseline had, "
                "which the change may not delete and no test reaches."
            )
            gap_cites.append(cov.record_hash)
    # What the task-text check (P1) did not judge, named one by one.
    for p1 in [a for a in audits if a.name == "audit:task-requirements"][-1:]:
        sealed_p1 = _sealed(journal, span_by_hash.get(p1.record_hash), "unjudged")
        for entry in sealed_p1["unjudged"] if sealed_p1 is not None else []:
            gaps.append(f"Not judged by the task-text check: {entry}.")
            gap_cites.append(p1.record_hash)
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

    # -- prompt constants (reporting only) --------------------------------------------
    constants = evidence.get("prompt_constants") if evidence is not None else None
    if isinstance(constants, dict) and outcome is not None:
        unnamed = prompt_constants.items(constants)
        count = len(constants.get("named", []))
        rows.append(
            Row(
                "prompt-constants",
                "Prompt constants",
                "not-proven" if unnamed else "observed",
                (
                    f"The task names {_n(count, 'constant')}; the source never names "
                    f"{len(unnamed)} of them. Not a verdict: the source may be right for a "
                    "reason a name check cannot see."
                    if unnamed
                    else f"The task names {_n(count, 'constant')}; the source names every one "
                    "it did not say was replaced."
                ),
                (outcome.record_hash,),
                tuple(unnamed),
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
    base = start_field(start.detail, "base") if start is not None else ""
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
                *((_change_log(branch, base),) if branch else ()),
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
        header.append(
            "tests editable"
            if test_edits
            else "tests editable after you allowed it"
            if granted
            else "tests read-only"
        )
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
        questions=_asked(audits, evidence) if verdict == "needs_you" and outcome else (),
        guarded_paths=guarded,
        spend=_meters(evidence) if evidence is not None else None,
        records={
            h: display_record(e)
            for h in cited
            if isinstance(e := by_hash.get(h), ProofRecord | SpanRecord)
        },
    )
    check_packet(packet, by_hash)
    return packet


def render_packet_text(packet: Packet) -> str:
    """The packet as plain text: the recap a chat's context gets.

    Deterministic, so the same ledger always gives the same bytes. A row's
    summary is printed in its COMPACT form (`Row.recap`): the recap must not
    scroll, and the full rendering is the web fold's.
    """
    lines = [f"verdict: {packet.verdict} — {packet.verdict_text}"]
    lines.extend(f"  {h}" for h in packet.header)
    for row in packet.rows:
        if row.key == "narrative":
            continue
        lines.append(f"{row.title} [{row.status}]: {row.text}")
        lines.extend(f"  - {item}" for item in row.items)
        lines.extend(f"  {line}" for line in row.recap.splitlines())
    return "\n".join(lines)
