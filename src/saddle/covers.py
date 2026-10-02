"""Which commits a run's ledger covers, and which came after it.

A run's ledger vouches for the tree its own commit holds. Two review commits
were once made by hand on top of a finished run's branch, and the ledger, the
session and the packet said nothing that separated the run's commit from the
ones after it. The facts are now in the ledger: `auto.run_auto` seals the
branch, base, commit and tree in an `auto:committed` record of the coverage
file beside the ledger (`journal.coverage_path`), and a follow-up audit
attached later (`attach_followup`) extends the covered range with a section of
its own, chained to the record before it.

This module only reads them (`read_coverage`) and says them in one voice
(`coverage_lines`), so `saddle verify` and the packet cannot word the same
fact two ways. A ledger with no commit record says "not recorded"; nothing here guesses a
commit for it.
"""

from __future__ import annotations

import hashlib
import json
import subprocess
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Final

from saddle.journal import (
    AUTO_COMMITTED,
    AUTO_OUTCOMES,
    AUTO_START,
    FOLLOWUP_SPAN,
    SpanRecord,
    attempt_sidecar_path,
    coverage_path,
    read_spans,
)
from saddle.transcript import start_field

SHORT: Final = 12
"""Hex digits of a sha shown in prose; the ledger holds the whole sha."""
LISTED_LATER: Final = 10
"""Later commits named one by one before the line says how many more."""


@dataclass(frozen=True)
class Followup:
    """One follow-up audit attached to a run: a range and what the audit found."""

    covered_from: str
    covered_to: str
    verdict: str
    findings: tuple[dict[str, Any], ...]
    tree: str
    record_hash: str


@dataclass(frozen=True)
class RunCoverage:
    """What one autonomous run's ledger covers."""

    run_span: str
    ended: bool
    """The run has an outcome span: its commit is expected to be sealed."""
    branch: str
    base: str
    commit: str
    """The run's own commit; "" when the ledger holds no commit record."""
    tree: str
    record_hash: str
    """The commit record's hash: what the first follow-up continues."""
    followups: tuple[Followup, ...] = ()

    @property
    def head(self) -> str:
        """The newest commit covered: the last follow-up's, else the run's own."""
        return self.followups[-1].covered_to if self.followups else self.commit

    @property
    def head_tree(self) -> str:
        return self.followups[-1].tree if self.followups else self.tree

    @property
    def tip(self) -> str:
        """The record the next follow-up must continue."""
        return self.followups[-1].record_hash if self.followups else self.record_hash


def _sidecar(journal: Path, span: SpanRecord) -> dict[str, Any]:
    """The span's sidecar when it hashes to the span's `attempt_hash`, else {}."""
    try:
        raw = attempt_sidecar_path(journal, span.span_id).read_bytes()
        loaded = json.loads(raw)
    except (OSError, ValueError):
        return {}
    if hashlib.sha256(raw).hexdigest() != span.attempt_hash or not isinstance(loaded, dict):
        return {}
    return loaded


def _followup(journal: Path, span: SpanRecord) -> Followup:
    sealed = _sidecar(journal, span)
    findings = sealed.get("findings")
    return Followup(
        covered_from=span.argv[1] if len(span.argv) > 1 else "",
        covered_to=span.argv[2] if len(span.argv) > 2 else "",
        verdict=str(sealed.get("verdict", "")),
        findings=tuple(f for f in findings if isinstance(f, dict))
        if isinstance(findings, list)
        else (),
        tree=str(sealed.get("tree", "")),
        record_hash=span.record_hash,
    )


def read_coverage(journal: Path, spans: Sequence[SpanRecord]) -> list[RunCoverage]:
    """The coverage of every autonomous run in `spans` (a verified ledger's spans).

    The commit and follow-ups come from the coverage file beside the ledger
    (`journal.coverage_path`); a run's commit record is the one naming its
    outcome span, and the follow-ups after it, up to the next commit record,
    are its own. A run with no such record (an older ledger, or one whose run
    ended before its commit was sealed) has `commit == ""`. A ledger holding
    no start span (chat, slice runs) covers nothing and returns [].
    Raises `ValueError` when the coverage file does not verify.
    """
    cpath = coverage_path(journal)
    sealed = read_spans(cpath) if cpath.exists() else []
    found: list[RunCoverage] = []
    for start in (s for s in spans if s.kind == "agent" and s.name == AUTO_START):
        outcome = next(
            (s for s in spans if s.parent_id == start.span_id and s.name in AUTO_OUTCOMES), None
        )
        at = next(
            (
                i
                for i, s in enumerate(sealed)
                if s.name == AUTO_COMMITTED
                and outcome is not None
                and s.argv[2:3] == [outcome.record_hash]
            ),
            None,
        )
        committed = sealed[at] if at is not None else None
        after = sealed[at + 1 :] if at is not None else []
        mine = after[: next((i for i, s in enumerate(after) if s.name == AUTO_COMMITTED), None)]
        detail = committed.detail if committed is not None else ""
        found.append(
            RunCoverage(
                run_span=start.span_id,
                ended=outcome is not None,
                branch=start_field(detail, "branch"),
                base=start_field(detail, "base"),
                commit=start_field(detail, "commit"),
                tree=start_field(detail, "tree"),
                record_hash=committed.record_hash if committed is not None else "",
                followups=tuple(_followup(journal, s) for s in mine if s.name == FOLLOWUP_SPAN),
            )
        )
    return found


@dataclass(frozen=True)
class Later:
    """The commits on a run's branch after the newest one its ledger covers."""

    commits: tuple[tuple[str, str], ...] = ()
    """(sha, subject), oldest first."""
    problem: str = ""
    """Why the lookup could not be made; when set, `commits` says nothing."""


def _git(repo: Path, *args: str) -> tuple[int, str]:
    try:
        done = subprocess.run(
            ["git", "-C", str(repo), *args], capture_output=True, text=True, check=False
        )
    except OSError as exc:
        return (-1, f"git could not run: {exc}")
    return (done.returncode, done.stdout if done.returncode == 0 else done.stderr.strip())


def later_commits(repo: Path, branch: str, head: str) -> Later:
    """The commits on `branch` after `head`, or why they could not be read.

    A failed lookup is a `problem`, never an empty list: "no later commits"
    and "could not look" must not read the same.
    """
    code, out = _git(repo, "rev-parse", "--git-dir")
    if code != 0:
        return Later(problem=f"{repo} is not a git repository" if code > 0 else out)
    if _git(repo, "rev-parse", "--verify", "-q", f"refs/heads/{branch}")[0] != 0:
        return Later(problem=f"branch {branch} is not in that repository")
    if _git(repo, "cat-file", "-e", f"{head}^{{commit}}")[0] != 0:
        return Later(problem=f"the covered commit {head[:SHORT]} is not in that repository")
    code, out = _git(repo, "merge-base", "--is-ancestor", head, f"refs/heads/{branch}")
    if code != 0:
        return Later(
            problem=f"the covered commit {head[:SHORT]} is not on branch {branch} "
            "(its history was rewritten)"
            if code == 1
            else f"git merge-base failed: {out}"
        )
    code, out = _git(
        repo, "log", "--reverse", "--format=%H%x09%s", f"{head}..refs/heads/{branch}", "--"
    )
    if code != 0:
        return Later(problem=f"git log failed: {out}")
    rows = [line.split("\t", 1) for line in out.splitlines() if line]
    return Later(commits=tuple((r[0], r[1] if len(r) > 1 else "") for r in rows))


def _tally(findings: Sequence[dict[str, Any]]) -> str:
    counts: dict[str, int] = {}
    for finding in findings:
        verdict = str(finding.get("verdict", "?"))
        counts[verdict] = counts.get(verdict, 0) + 1
    return ", ".join(f"{n} {v}" for v, n in sorted(counts.items(), key=lambda kv: (-kv[1], kv[0])))


def coverage_lines(cov: RunCoverage, later: Later | None) -> list[str]:
    """The ledger's coverage in words, one statement per line.

    `later` is the lookup of commits after the covered one; None when none
    was attempted, which the last line says instead of staying silent.
    """
    if not cov.commit:
        return [
            "covers: not recorded (ledger predates this field, or the run ended before "
            "its commit was sealed)"
            if cov.ended
            else "covers: nothing yet (the run has not ended)"
        ]
    lines = [
        f"covers: {cov.base[:SHORT]}..{cov.head[:SHORT]} on {cov.branch} "
        f"(tree {cov.head_tree[:SHORT]})"
    ]
    if cov.followups:
        lines.append(f"the run itself ended at {cov.commit[:SHORT]} (tree {cov.tree[:SHORT]})")
    for number, followup in enumerate(cov.followups, start=1):
        shown = _tally(followup.findings)
        lines.append(
            f"follow-up {number}: {followup.covered_from[:SHORT]}..{followup.covered_to[:SHORT]}, "
            f"{len(followup.findings)} finding(s), verdict {followup.verdict or 'not readable'}"
            + (f" ({shown})" if shown else "")
            + ", audited after the run and attached to this ledger, not the run's own"
        )
    if later is None:
        lines.append("later commits on the branch: not checked here")
    elif later.problem:
        lines.append(f"later commits on the branch: not checked ({later.problem})")
    elif not later.commits:
        lines.append(f"later commits on {cov.branch}: none")
    else:
        named = [f"{sha[:SHORT]} {subject}" for sha, subject in later.commits[:LISTED_LATER]]
        more = len(later.commits) - LISTED_LATER
        lines.append(
            f"not covered by this ledger: {len(later.commits)} later commit(s): "
            + "; ".join(named)
            + (f"; and {more} more" if more > 0 else "")
        )
    return lines


def foreign_records(journal: Path, spans: Sequence[SpanRecord]) -> int:
    """How many commit records in the coverage file name an outcome the ledger does not hold.

    They are not counted as any run's coverage (`read_coverage` never matches
    them); a coverage file copied from another run, or a ledger cut back past
    its outcome, shows up here instead of as a silent "not recorded".
    """
    cpath = coverage_path(journal)
    held = {s.record_hash for s in spans if s.name in AUTO_OUTCOMES}
    sealed = read_spans(cpath) if cpath.exists() else []
    return sum(
        1 for s in sealed if s.name == AUTO_COMMITTED and s.argv[2:3] not in ([h] for h in held)
    )


def verify_lines(journal: Path, spans: Sequence[SpanRecord], repo: Path) -> list[str]:
    """What `saddle verify` prints about coverage, for every autonomous run in the ledger."""
    lines: list[str] = []
    foreign = foreign_records(journal, spans)
    if foreign:
        lines.append(
            f"coverage file: {foreign} commit record(s) name an outcome this ledger does not "
            "hold; they are not counted"
        )
    for cov in read_coverage(journal, spans):
        later = later_commits(repo, cov.branch, cov.head) if cov.commit else None
        lines.extend(coverage_lines(cov, later))
    return lines


def packet_text(journal: Path, spans: Sequence[SpanRecord], repo: Path | None) -> str:
    """The Reproduce row's sentences on coverage, "" for a ledger holding no run.

    Without `repo` the later commits are not looked up, and the text says so.
    """
    try:
        found = read_coverage(journal, spans)
    except ValueError:
        return "Its coverage file does not verify, so no coverage is stated. "
    if not found:
        return ""
    cov = found[0]
    later = later_commits(repo, cov.branch, cov.head) if repo is not None and cov.commit else None
    return "".join(f"{line[0].upper()}{line[1:]}. " for line in coverage_lines(cov, later))
