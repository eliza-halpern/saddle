"""Audit feedback for autonomous runs: the "F" of Phase 2's E+A+F arm.

The Daily Driver page, "Overlap, don't alternate": *"The GPU writes while
the CPU checks the last checkpoint. A failure arrives as a tool result, and
the agent handles it at its next step."* This module is that loop's CPU
half. `engine.run_turn` calls it at four points and knows nothing else
about auditing (the engine imports no auditor):

- `before_tool(name)` -- the **checkpoint trigger**. The page's tier table
  puts tier 1 "after each burst of edits"; a burst ends at the model's first
  tool call that is not a file edit (`write_file`/`edit_file`) after one or
  more successful edits. `run_command`, `read_file`, `finish`, anything:
  the model has stopped editing and started checking, reading or ending, so
  that tree is the checkpoint. The tree is snapshotted synchronously (a copy,
  so later edits cannot race the audit) and `Auditor.tier1` runs on it in a
  background thread, off the model's critical path.
- `collect()` -- called after every tool result and at every nudge. A
  checkpoint audit that has completed is journaled as a delivery span and,
  with feedback on, returned as text the engine appends to that tool result,
  so the model reads it at its next step. A pending audit is not waited for.
- `final()` -- before `finish` is accepted: the pending checkpoint is
  awaited, tier 1 (a cache hit if the tree has not changed since the last
  checkpoint) and tier 2 run synchronously on the finished tree. With
  feedback on, any `fail` or `blocked` finding refuses `finish` and the
  findings are its tool result; the run continues within its budgets
  (tightened: a run cannot end finished with a failing audit).
- `close()` -- at the end of the run; waits for a pending checkpoint so the
  outcome sidecar carries the last findings even on a budget stop.

Every completed audit is journaled once, as an `audit:delivered` span if
the model was shown it and `audit:withheld` if not (arm E+A always; E+A+F
for an accepted finish's audit and for one still pending at the end).

Arms (`ARMS`): "E+A+F" is the default. "E+A" (`--no-feedback`) runs the same
audits and journals them, delivers nothing, and never refuses `finish`; its
verdict is recorded beside the outcome. "E" (`--no-audit`) constructs no
`AuditFeed` and so no `Auditor`. Both opt-outs are scope-narrowed.

Snapshots: the run's worktree is a linked worktree whose `.git` is a file,
which `audit.staged_copy` refuses (it would write the shared index). A
snapshot is a `git clone --shared --no-checkout` of the worktree (so the
baseline commit resolves) with the worktree's files copied over; its
`git add -A && git write-tree` is the tree id every finding is reported
against, and equals the tree `auto` commits when nothing changed after it.
"""

from __future__ import annotations

import dataclasses
import shutil
import subprocess
import tempfile
import threading
from collections.abc import Callable
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path
from typing import Final, Literal, Protocol

from saddle.audit import AuditError
from saddle.auditor import Auditor, AuditorConfig, Finding, Findings, sanction
from saddle.journal import append_span, build_span
from saddle.tools import FINISH_TOOL

Arm = Literal["E", "E+A", "E+A+F"]
ARMS: Final[tuple[Arm, ...]] = ("E", "E+A", "E+A+F")

EDIT_TOOLS: Final = frozenset({"write_file", "edit_file"})
"""The tools whose successful calls make a burst. A `run_command` that edits
files is not counted as an edit; the finish audit still sees its result."""

FAILING: Final = frozenset({"fail", "blocked"})

DETAIL_CHARS: Final = 1200
"""Per finding, in the text the model reads. The journal keeps it whole."""


class AuditorLike(Protocol):
    def tier1(self, tree: Path | None = None) -> Findings: ...
    def tier2(self, tree: Path | None = None) -> Findings: ...


type AuditorFactory = Callable[[Path, str, AuditorConfig], AuditorLike]


def default_auditor(repo: Path, baseline: str, config: AuditorConfig) -> AuditorLike:
    return Auditor(repo, baseline, config)


@dataclass(frozen=True)
class AuditResult:
    """One audit's outcome over one snapshot: a checkpoint or the finish."""

    point: str
    """"checkpoint <n>" or "finish"."""
    tree: str
    findings: tuple[Finding, ...]
    note: str = ""

    @property
    def passed(self) -> bool:
        return not any(failing(f) for f in self.findings)

    def to_dict(self) -> dict[str, object]:
        return {
            "point": self.point,
            "tree": self.tree,
            "passed": self.passed,
            "note": self.note,
            "findings": [dataclasses.asdict(f) for f in self.findings],
        }


def failing(finding: Finding) -> bool:
    """A finding that refuses `finish`: fail or blocked, and not sanctioned."""
    return finding.verdict in FAILING and finding.reason != "sanctioned"


def render(result: AuditResult) -> str:
    """The compact text the model reads: failures in full, passes counted."""
    head = (
        f"[audit {result.point} on tree {result.tree[:12]}: {'PASS' if result.passed else 'FAIL'}]"
    )
    lines = [head]
    if result.note:
        lines.append(result.note)
    bad = [f for f in result.findings if failing(f)]
    for f in bad:
        detail = f.detail if len(f.detail) <= DETAIL_CHARS else f.detail[:DETAIL_CHARS] + " ..."
        lines.append(f"- {f.gate} (tier {f.tier}): {f.verdict}, {f.reason}: {detail}")
    allowed = [f for f in result.findings if f.reason == "sanctioned" and f.verdict in FAILING]
    for f in allowed:
        lines.append(f"(info) {f.gate} (tier {f.tier}): {f.detail}")
    passed = len(result.findings) - len(bad) - len(allowed)
    if passed:
        lines.append(f"({passed} other check(s) passed or not applicable)")
    return "\n".join(lines)


GIT_IDENTITY: Final = ("-c", "user.name=saddle", "-c", "user.email=saddle@localhost")


def _git(cwd: Path, *args: str) -> str:
    done = subprocess.run(
        [
            "git",
            "-C",
            str(cwd),
            "-c",
            "user.name=saddle",
            "-c",
            "user.email=saddle@localhost",
            *args,
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    if done.returncode != 0:
        msg = f"git {' '.join(args)} failed: {done.stderr.strip()}"
        raise AuditError(msg)
    return done.stdout.strip()


def _copy_files(source: Path, into: Path) -> None:
    shutil.copytree(
        source,
        into,
        ignore=lambda d, names: [".git"] if Path(d) == source else [],
        dirs_exist_ok=True,
    )


def snapshot(worktree: Path, files: Path, into: Path) -> str:
    """A standalone repo at `into`: `worktree`'s history, `files`' contents.

    Returns its `git add -A && git write-tree`, the tree id the findings are
    reported against.
    """
    _git(worktree, "clone", "-q", "--shared", "--no-checkout", str(worktree), str(into))
    _copy_files(files, into)
    _git(into, "add", "-A")
    return _git(into, "write-tree")


@dataclass
class AuditFeed:
    """The auditor side of one autonomous run (arms E+A and E+A+F)."""

    worktree: Path
    baseline: str
    journal: Path
    run_span: str
    feedback: bool = True
    factory: AuditorFactory = default_auditor
    sanctioned_test_rewrites: tuple[str, ...] = ()
    """Test functions the task orders rewritten; see `auditor.sanction`."""
    auditor: AuditorLike | None = None
    results: list[AuditResult] = field(default_factory=list)
    """Every completed audit, in completion order; the last is the verdict."""
    checkpoints: int = 0
    _dirty: bool = False
    _pending: Future[AuditResult] | None = None
    _ready: list[AuditResult] = field(default_factory=list)
    _pool: ThreadPoolExecutor | None = None
    _lock: threading.Lock = field(default_factory=threading.Lock)

    def __post_init__(self) -> None:
        if self.auditor is None:
            config = AuditorConfig(
                journal=self.journal, sanctioned_test_rewrites=self.sanctioned_test_rewrites
            )
            self.auditor = self.factory(self.worktree, self.baseline, config)

    # -- the audit itself ------------------------------------------------------

    def _audit(self, point: str, tiers: tuple[int, ...], files: Path, scratch: Path) -> AuditResult:
        assert self.auditor is not None
        try:
            tree = snapshot(self.worktree, files, scratch / "tree")
            found: list[Finding] = []
            for tier in tiers:
                run = self.auditor.tier1 if tier == 1 else self.auditor.tier2
                found.extend(
                    sanction(f, self.sanctioned_test_rewrites)
                    for f in run(scratch / "tree").findings
                )
            return AuditResult(point, tree, tuple(found))
        except AuditError as exc:
            if str(exc).startswith("nothing to audit"):
                return AuditResult(point, "", (), note=str(exc))
            return AuditResult(point, "", (_blocked(str(exc)),))
        except Exception as exc:  # an audit that crashed decided nothing
            return AuditResult(point, "", (_blocked(f"{type(exc).__name__}: {exc}"),))
        finally:
            shutil.rmtree(scratch, ignore_errors=True)

    def _checkpoint(self, point: str, scratch: Path) -> AuditResult:
        result = self._audit(point, (1,), scratch / "frozen", scratch)
        with self._lock:
            self._ready.append(result)
            self.results.append(result)
        return result

    # -- engine hooks ----------------------------------------------------------

    def before_tool(self, name: str) -> None:
        """Start a checkpoint audit if `name` ends a burst of edits."""
        if name in EDIT_TOOLS or not self._dirty:
            return
        if name == FINISH_TOOL:
            return  # `final` audits this tree at both tiers; no checkpoint too
        self._dirty = False
        self._await()  # one checkpoint in flight at a time; the auditor is not shared
        self.checkpoints += 1
        point = f"checkpoint {self.checkpoints}"
        scratch = Path(tempfile.mkdtemp(prefix="saddle-feed-"))
        # The copy is taken now, before the tool runs: it is this burst's tree,
        # whatever the model does while the audit is in flight.
        _copy_files(self.worktree, scratch / "frozen")
        if self._pool is None:
            self._pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix="saddle-audit")
        self._pending = self._pool.submit(self._checkpoint, point, scratch)

    def after_tool(self, name: str, ok: bool) -> None:
        """Mark the tree dirty after a successful edit."""
        if name in EDIT_TOOLS and ok:
            self._dirty = True

    def _take(self) -> list[AuditResult]:
        with self._lock:
            ready, self._ready = self._ready, []
        return ready

    def _record(self, ready: list[AuditResult], *, delivered: bool) -> str:
        texts = [render(result) for result in ready]
        for result, text in zip(ready, texts, strict=True):
            self._journal(result, text, delivered=delivered)
        return "\n\n".join(texts)

    def collect(self) -> str:
        """Completed checkpoint audits: journaled, and returned as text if delivered."""
        text = self._record(self._take(), delivered=self.feedback)
        return text if self.feedback else ""

    def final(self) -> tuple[bool, str]:
        """Tier 1 and tier 2 on the tree `finish` is called on.

        Returns (accept finish, text for the model). With feedback off,
        finish is always accepted and the text is empty. A checkpoint audit
        still undelivered is shown with a refusal, and journaled as withheld
        when finish is accepted (the model never reads that result).
        """
        self._await()
        pending = self._take()
        scratch = Path(tempfile.mkdtemp(prefix="saddle-feed-"))
        result = self._audit("finish", (1, 2), self.worktree, scratch)
        with self._lock:
            self.results.append(result)
        refuse = self.feedback and not result.passed
        before = self._record(pending, delivered=refuse)
        text = self._record([result], delivered=refuse)
        if not refuse:
            return True, ""
        return False, "\n\n".join(t for t in (before, text) if t)

    def unresolved(self) -> list[dict[str, object]]:
        """The last audit's failing findings as (gate, reason, cites), sorted.

        The identity of a finish refusal: the engine counts consecutive
        refusals on an unchanged set (`engine.AutoRun.finish_refusal_cap`).
        Detail text is left out; it carries counts that can drift while the
        finding stays the same.
        """
        if not self.results:
            return []
        keys = {(f.gate, f.reason, f.cites) for f in self.results[-1].findings if failing(f)}
        return [{"gate": g, "reason": r, "cites": list(c)} for g, r, c in sorted(keys)]

    def close(self) -> None:
        """End of run: a pending checkpoint is awaited and journaled, never delivered."""
        self._await()
        self._record(self._take(), delivered=False)
        if self._pool is not None:
            self._pool.shutdown(wait=True)

    def _await(self) -> None:
        if self._pending is not None:
            self._pending.result()
            self._pending = None

    def last(self) -> dict[str, object] | None:
        return self.results[-1].to_dict() if self.results else None

    def _journal(self, result: AuditResult, text: str, *, delivered: bool) -> None:
        append_span(
            self.journal,
            build_span(
                node_id="chat#1",
                argv=["audit", result.point, result.tree],
                duration_ms=0,
                exit_code=0 if result.passed else 1,
                detail=text,
                name=f"audit:{'delivered' if delivered else 'withheld'}",
                parent_id=self.run_span,
            ),
        )


def _blocked(detail: str) -> Finding:
    return Finding(
        gate="audit",
        tier=1,
        verdict="blocked",
        reason="unknown",
        detail=f"the audit could not run: {detail}",
        cites=("saddle.feed.AuditFeed",),
    )
