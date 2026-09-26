"""An autonomous run's ledger, anchored outside itself in its branch (ANCHOR).

`saddle verify` holds a run's tool spans to the list its outcome span seals
(CHAIN), but nothing in the ledger is keyed: someone who deletes a span,
rewrites the sidecar list and recomputes the outcome span's `attempt_hash`
and `record_hash` gets a ledger that verifies, and deleting the outcome
together with its proof makes a finished run read as in-flight.

So `auto.run_auto` writes the outcome span's `record_hash` into the run
branch's final commit as a git trailer, and `saddle verify --anchor`
compares it with the outcome span the ledger holds. Rewriting the ledger
then also means rewriting the branch. That is the limit of this anchor:
it is as strong as the branch history is (a pushed or signed branch
strengthens it; saddle does neither).
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path
from typing import Final

from saddle.journal import AUTO_OUTCOMES, AUTO_START, JournalIssue, SpanRecord
from saddle.transcript import start_field

OUTCOME_TRAILER: Final = "Saddle-Outcome"
LEDGER_TRAILER: Final = "Saddle-Ledger"


def anchor_trailers(outcome_hash: str, ledger: str) -> str:
    """The trailer block the run branch's final commit ends with."""
    return f"{OUTCOME_TRAILER}: {outcome_hash}\n{LEDGER_TRAILER}: {ledger}"


def _spans(journal: Path) -> list[tuple[int, SpanRecord]]:
    """Every parseable span line with its 1-based line number.

    Unparseable or invalid lines are skipped: `verify_journal` reports them.
    """
    found: list[tuple[int, SpanRecord]] = []
    for number, line in enumerate(journal.read_text(encoding="utf-8").splitlines(), start=1):
        try:
            raw = json.loads(line)
            if isinstance(raw, dict) and raw.get("record_type") == "span":
                found.append((number, SpanRecord.model_validate(raw)))
        except ValueError:  # pydantic's ValidationError is one
            continue
    return found


def outcome_hash(journal: Path, run_span: str) -> str:
    """The `record_hash` of run `run_span`'s outcome span, or "" if none is sealed."""
    for _, span in _spans(journal):
        if span.parent_id == run_span and span.name in AUTO_OUTCOMES:
            return span.record_hash
    return ""


def parse_trailers(message: str) -> dict[str, str]:
    """`Key: value` lines of a commit message's last paragraph (git's trailer block)."""
    paragraphs = [p for p in message.strip().split("\n\n") if p.strip()]
    found: dict[str, str] = {}
    for line in paragraphs[-1].splitlines() if paragraphs else []:
        key, sep, value = line.partition(": ")
        if sep and key in (OUTCOME_TRAILER, LEDGER_TRAILER):
            found[key] = value.strip()
    return found


def branch_anchor(repo: Path, branch: str) -> str | None:
    """The `Saddle-Outcome` hash on `branch`'s newest anchored commit.

    "" when the branch has no anchored commit (a run still in flight: its
    tip is the commit it started from); None when the branch is absent.
    Commits made on the branch after the run do not hide its anchor.
    """
    done = subprocess.run(
        [
            "git",
            "-C",
            str(repo),
            "log",
            "-1",
            "--format=%B",
            f"--grep=^{OUTCOME_TRAILER}: ",
            f"refs/heads/{branch}",
            "--",
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    if done.returncode != 0:
        return None
    return parse_trailers(done.stdout).get(OUTCOME_TRAILER, "")


def anchor_issues(journal: Path, repo: Path) -> list[JournalIssue]:
    """Hold each autonomous run in `journal` to the anchor on its branch in `repo`.

    - `anchor-mismatch`: the branch's `Saddle-Outcome` is not the outcome
      span the ledger holds (a resealed or substituted outcome).
    - `anchor-missing`: the ledger holds an outcome, but the branch carries
      no anchor, or the branch is gone.
    - `outcome-missing-anchored`: the branch carries an anchor, so the run
      ended, but the ledger holds no outcome for it.
    A run with neither an outcome nor an anchor is in flight: no issue.
    """
    spans = _spans(journal) if journal.exists() else []
    issues: list[JournalIssue] = []
    for number, start in spans:
        if start.kind != "agent" or start.name != AUTO_START:
            continue
        outcome = next(
            (s for _, s in spans if s.parent_id == start.span_id and s.name in AUTO_OUTCOMES),
            None,
        )
        branch = start_field(start.detail, "branch")
        anchored = branch_anchor(repo, branch) if branch else None
        if outcome is not None and not anchored:
            where = f"branch {branch!r}" if branch else "the start span (no branch named)"
            issues.append(
                JournalIssue(
                    code="anchor-missing",
                    line=number,
                    message=f"run {start.span_id!r} has outcome span {outcome.span_id!r} but "
                    f"{where} in {repo} carries no {OUTCOME_TRAILER} trailer",
                )
            )
        elif outcome is None and anchored:
            issues.append(
                JournalIssue(
                    code="outcome-missing-anchored",
                    line=number,
                    message=f"branch {branch!r} anchors outcome {anchored} but the ledger "
                    f"holds no outcome span for run {start.span_id!r}",
                )
            )
        elif outcome is not None and anchored != outcome.record_hash:
            issues.append(
                JournalIssue(
                    code="anchor-mismatch",
                    line=number,
                    message=f"branch {branch!r} anchors outcome {anchored} but the ledger's "
                    f"outcome span {outcome.span_id!r} hashes to {outcome.record_hash}",
                )
            )
    return issues


def default_anchor_repo(journal: Path) -> Path:
    """The checkout a run's ledger `<root>/.saddle/runs/<id>/proofs.jsonl` belongs to."""
    return journal.resolve().parent.parent.parent.parent
