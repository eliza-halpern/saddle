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
  awaited, then tier 0 (`Auditor.tier0` on every changed Python file, the
  same `--diff-filter=AMR` set the post-hoc `Auditor.audit` sends it),
  tier 1 (a cache hit if the tree has not changed since the last
  checkpoint) and tier 2 run synchronously on the finished tree. With
  feedback on, any `fail` or `blocked` finding refuses `finish` and the
  findings are its tool result; the run continues within its budgets
  (tightened: a run cannot end finished with a failing audit). Tier 0 runs at
  finish because the M3 EAF-t5 trees all
  finished with a passing in-run audit and were refused post hoc for
  `ruff format --check` and B904/F401, because only tiers 1 and 2 ran
  here. The contract now: a tree `finish` accepts is a tree the post-hoc
  tier 0 accepts, on the changed files. Tier 0 runs at finish and on
  `check`, never per edit (the edit guard stays syntax-only).
- `check()` -- the model's **pull** (`--check-tool`, arm E+A+F only): tier 0
  (`Auditor.tier0` on every changed Python file) and tier 1 of the same
  auditor, synchronously, on the tree as it is now, rendered by the same
  `render` a finish refusal uses. Never tier 2, never a finish refusal, and
  refused (no audit run) while the tree is unchanged since the last check.
  Each check that runs is journaled as a `CHECK_SPAN` span whose sidecar
  holds its findings. The finish audit is unaffected by it.
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
import uuid
from collections.abc import Callable, Sequence
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path
from typing import Final, Literal, Protocol

from saddle import coverage_text, sandbox
from saddle.audit import AuditError
from saddle.auditor import (
    Auditor,
    AuditorConfig,
    Finding,
    Findings,
    Tier2Mode,
    coverage_evidence,
    rewritten,
    sanction,
)
from saddle.gates import DEFAULT_MUTANT_SHORTLIST
from saddle.journal import append_span, build_span, write_attempt_sidecar
from saddle.tools import CHECK_TOOL, FINISH_TOOL

Arm = Literal["E", "E+A", "E+A+F"]
ARMS: Final[tuple[Arm, ...]] = ("E", "E+A", "E+A+F")

EDIT_TOOLS: Final = frozenset({"write_file", "edit_file"})
"""The tools whose successful calls make a burst. A `run_command` that edits
files is not counted as an edit; the finish audit still sees its result."""

FAILING: Final = frozenset({"fail", "blocked"})

CHECK_SPAN: Final = "audit:check"
"""A `check` call's audit record: the rendered text as detail, the findings
(`AuditResult.to_dict`) in the span's attempt sidecar. Its `audit:` prefix
puts it in the outcome's sealed audit list."""

CHECK_UNCHANGED: Final = "error: check refused: the tree is unchanged since check "
"""Prefix of a refused `check`. Not the tier-0 guard's `REFUSED`, so it is
not counted among the run's guard refusals, and not a finish refusal."""

NOTHING_TO_AUDIT: Final = "nothing to audit"
"""How `Auditor` begins the error for a tree equal to its baseline."""

DETAIL_CHARS: Final = 1200
"""Per finding, in the text the model reads. The journal keeps it whole."""


class AuditorLike(Protocol):
    def tier0(self, path: str, new_text: str) -> Findings: ...
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
    mutant_detail: tuple[tuple[str, str, str], ...] = ()
    """Tier 2's (name, status, show) for every scored mutant; sealed in the
    audit span's sidecar under `mutant_detail` when non-empty."""
    coverage: str = ""
    """A failing or not-proven coverage finding in `coverage_text`'s words
    (function, its docstring's first line, the lines), read off the audited
    snapshot; what `render` shows the model in place of the bare line list.
    Sealed under `coverage_text` when non-empty."""

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
            **(
                {
                    "mutant_detail": [
                        {"name": n, "status": s, "show": t} for n, s, t in self.mutant_detail
                    ]
                }
                if self.mutant_detail
                else {}
            ),
            **({"coverage_text": self.coverage} if self.coverage else {}),
        }


def failing(finding: Finding) -> bool:
    """A finding that refuses `finish`: fail or blocked, and not sanctioned."""
    return finding.verdict in FAILING and finding.reason != "sanctioned"


SANCTIONED_REWRITE: Final = "sanctioned test rewrite: "
"""How `waivers` names one test a sanctioned finding let through."""


def waivers(result: AuditResult) -> list[str]:
    """What let `result` pass that a plain audit would not have.

    One entry per test a `sanctioned` finding names (a rewrite the task
    ordered, `auditor.sanction`), sorted, and the "nothing to audit" note
    if the audit carried one. Sealed on every accepted finish so an accept
    that stood on a waiver says so in the ledger; an accept with none
    seals []. The engine never ends an unchanged tree finished, so
    the note reaches the field only from a hand-built result.
    """
    names = sorted(
        {n for f in result.findings if f.reason == "sanctioned" for n in rewritten(f.detail)}
    )
    out = [f"{SANCTIONED_REWRITE}{n}" for n in names]
    if result.note.startswith(NOTHING_TO_AUDIT):
        out.append(result.note)
    return out


def _worded(result: AuditResult, finding: Finding) -> str:
    """A finding's detail as the model reads it, capped at `DETAIL_CHARS`.

    The coverage finding reads in `coverage_text`'s words when the audit
    could place its lines (`AuditResult.coverage`): which function each
    uncovered line is in and what that function is for, then the lines,
    rather than "no test runs money.py:131, money.py:132, ...".
    Every other finding, and a coverage finding with
    nothing placed, reads its detail as before.
    """
    text = result.coverage if finding.gate == "coverage" and result.coverage else finding.detail
    return text if len(text) <= DETAIL_CHARS else text[:DETAIL_CHARS] + " ..."


def _coverage_words(tree: Path, baseline: str, findings: Sequence[Finding]) -> str:
    """`coverage_text`'s rendering of the failing or not-proven coverage
    finding among `findings`, over the snapshot it judged; "" if none.

    Under the heading, when the finding spans two or more files, a per-file
    tally, most lines first (`coverage_text.file_tally`). `_worded` caps the
    text at `DETAIL_CHARS` and the rows run in path order: in benchmark draw
    EAF-t5 s1 the cap cut them seven rows in, and the file holding 223 of
    the 246 lines the heading counted was never named. Wording only: the
    rows and every verdict are unchanged (tightened).
    """
    found = next(
        (f for f in findings if f.gate == "coverage" and f.verdict in ("fail", "not-proven")),
        None,
    )
    sealed = coverage_evidence(tree, baseline, found.detail) if found is not None else None
    if found is None or sealed is None:
        return ""
    sources = {name: "\n".join(lines) + "\n" for name, lines in sealed["sources"].items()}
    changed = [(str(path), int(line)) for path, line in sealed["changed"]]
    summary = coverage_text.describe_coverage(dataclasses.asdict(found), sources, changed)
    rendered = coverage_text.render_coverage(summary, text=False).rstrip("\n")
    tally = coverage_text.file_tally(summary)
    if not tally:
        return rendered
    head, _, rows = rendered.partition("\n")
    return f"{head}\n{tally}\n{rows}"


def render(result: AuditResult) -> str:
    """The compact text the model reads: failures in full, passes counted."""
    head = (
        f"[audit {result.point} on tree {result.tree[:12]}: {'PASS' if result.passed else 'FAIL'}]"
    )
    lines = [head]
    if result.note:
        lines.append(result.note)
    bad = [f for f in result.findings if failing(f)]
    # Identical failing lines are said once, with their count: tier 0 runs per
    # file and its format detail names none, so two unformatted files read
    # "ruff format --check exited 1" twice (EAFS-t5 s2's finish refusal).
    said: dict[str, int] = {}
    for f in bad:
        detail = _worded(result, f)
        text = f"- {f.gate} (tier {f.tier}): {f.verdict}, {f.reason}: {detail}"
        said[text] = said.get(text, 0) + 1
    lines.extend(text if n == 1 else f"{text} ({n} findings)" for text, n in said.items())
    allowed = [f for f in result.findings if f.reason == "sanctioned" and f.verdict in FAILING]
    for f in allowed:
        lines.append(f"(info) {f.gate} (tier {f.tier}): {f.detail}")
    unproven = [f for f in result.findings if f.verdict == "not-proven"]
    for f in unproven:
        detail = _worded(result, f)
        lines.append(f"(not proven, does not refuse) {f.gate} (tier {f.tier}): {detail}")
    passed = len(result.findings) - len(bad) - len(allowed) - len(unproven)
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
    tier2: Tier2Mode = "score"
    """`--tier2`; "shortlist" turns on this module's shortlist behaviour too."""
    mutant_shortlist: int = DEFAULT_MUTANT_SHORTLIST
    """How many survivors a mutation finding names (`--mutant-shortlist`)."""
    project_env: Path | None = None
    """The project's virtualenv (`sandbox.project_env`) every audit runs the
    tests on; None leaves the gates on saddle's PATH. Set per audit, inside
    `_audit`, because a checkpoint audit runs on the feed's own thread."""
    auditor: AuditorLike | None = None
    results: list[AuditResult] = field(default_factory=list)
    """Every completed audit, in completion order; the last is the verdict."""
    checkpoints: int = 0
    checks: list[AuditResult] = field(default_factory=list)
    """Every `check` the model ran, in order. Not in `results`: a check is
    never the run's verdict, and `unresolved` must read the finish audit."""
    surfaced_tree: str | None = None
    """The tree of the accepted finish whose not-proven findings were
    delivered (`final`); None until one is."""
    _checked_tree: str | None = None
    _dirty: bool = False
    _pending: Future[AuditResult] | None = None
    _ready: list[AuditResult] = field(default_factory=list)
    _pool: ThreadPoolExecutor | None = None
    _lock: threading.Lock = field(default_factory=threading.Lock)

    def __post_init__(self) -> None:
        if self.auditor is None:
            config = AuditorConfig(
                journal=self.journal,
                sanctioned_test_rewrites=self.sanctioned_test_rewrites,
                tier2=self.tier2,
                mutant_shortlist=self.mutant_shortlist,
            )
            self.auditor = self.factory(self.worktree, self.baseline, config)

    # -- the audit itself ------------------------------------------------------

    def _audit(
        self,
        point: str,
        tiers: tuple[int, ...],
        files: Path,
        scratch: Path,
        *,
        check: bool = False,
    ) -> AuditResult | None:
        """The audit of `files` at `tiers`. For a `check`, None (nothing run)
        when the tree is the one the last check audited."""
        with sandbox.using_project_env(self.project_env):
            return self._audit_on(point, tiers, files, scratch, check=check)

    def _audit_on(
        self,
        point: str,
        tiers: tuple[int, ...],
        files: Path,
        scratch: Path,
        *,
        check: bool,
    ) -> AuditResult | None:
        """`_audit`'s body, run with the project environment in place."""
        assert self.auditor is not None
        try:
            tree = snapshot(self.worktree, files, scratch / "tree")
            if check:
                if tree == self._checked_tree:
                    return None
                self._checked_tree = tree
            found: list[Finding] = []
            detail: tuple[tuple[str, str, str], ...] = ()
            for tier in tiers:
                got = self._tier(tier, scratch / "tree")
                found.extend(sanction(f, self.sanctioned_test_rewrites) for f in got.findings)
                detail = detail or got.mutant_detail
            words = _coverage_words(scratch / "tree", self.baseline, found)
            return AuditResult(point, tree, tuple(found), mutant_detail=detail, coverage=words)
        except AuditError as exc:
            if str(exc).startswith(NOTHING_TO_AUDIT):
                return AuditResult(point, "", (), note=str(exc))
            return AuditResult(point, "", (_blocked(str(exc)),))
        except Exception as exc:  # an audit that crashed decided nothing
            return AuditResult(point, "", (_blocked(f"{type(exc).__name__}: {exc}"),))
        finally:
            shutil.rmtree(scratch, ignore_errors=True)

    def _tier(self, tier: int, tree: Path) -> Findings:
        assert self.auditor is not None
        if tier == 0:
            # The changed set is the post-hoc one (`Auditor.audit`): every
            # added, modified or renamed Python file in the snapshot against
            # the baseline. Tier 0 over anything else could refuse a tree the
            # post-hoc audit accepts.
            changed = _git(
                tree, "diff", "--cached", "--name-only", "--diff-filter=AMR", self.baseline
            ).split()
            found = [
                f
                for name in sorted(n for n in changed if n.endswith(".py"))
                for f in self.auditor.tier0(name, (tree / name).read_text()).findings
            ]
            return Findings(tier=0, key="", findings=tuple(found))
        return self.auditor.tier1(tree) if tier == 1 else self.auditor.tier2(tree)

    def _checkpoint(self, point: str, scratch: Path) -> AuditResult:
        result = self._audit(point, (1,), scratch / "frozen", scratch)
        assert result is not None
        with self._lock:
            self._ready.append(result)
            self.results.append(result)
        return result

    # -- engine hooks ----------------------------------------------------------

    def before_tool(self, name: str) -> None:
        """Start a checkpoint audit if `name` ends a burst of edits."""
        if name in EDIT_TOOLS or not self._dirty:
            return
        if name in (FINISH_TOOL, CHECK_TOOL):
            return  # `final` / `check` audit this tree themselves; no checkpoint too
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

    def check(self) -> str:
        """The model's pull: tiers 0 and 1 on the tree as it is now.

        Rendered exactly as a finish refusal renders its audit (`render`), so
        a finding reads the same whichever way it arrives. Tier 2 is never
        run here. On a tree unchanged since the last check nothing runs and
        the call is refused. A check never touches the finish refusal count
        and never shortens the finish audit.
        """
        self._await()  # the auditor is not shared with a pending checkpoint
        scratch = Path(tempfile.mkdtemp(prefix="saddle-check-"))
        result = self._audit(
            f"check {len(self.checks) + 1}", (0, 1), self.worktree, scratch, check=True
        )
        if result is None:
            return (
                f"{CHECK_UNCHANGED}{len(self.checks)}; edit a file before checking again. "
                "That check's findings still stand."
            )
        self._dirty = False  # this tree is audited; no checkpoint of it too
        self.checks.append(result)
        text = render(result)
        span_id = uuid.uuid4().hex
        digest = write_attempt_sidecar(self.journal, span_id, result.to_dict())
        append_span(
            self.journal,
            build_span(
                node_id="chat#1",
                argv=["check", result.point, result.tree],
                duration_ms=0,
                exit_code=0 if result.passed else 1,
                detail=text,
                name=CHECK_SPAN,
                parent_id=self.run_span,
                span_id=span_id,
                attempt_hash=digest,
            ),
        )
        return text

    def final(self) -> tuple[bool, str]:
        """Tiers 0, 1 and 2 on the tree `finish` is called on.

        Returns (accept finish, text for the model). With feedback off,
        finish is always accepted and the text is empty. A checkpoint audit
        still undelivered is shown with a refusal, and journaled as withheld
        when finish is accepted (the model never reads that result).

        An accepted finish returns its audit's text once per run, the first
        time that audit carries a `not-proven` finding: the surviving mutants and
        uncovered lines the shortlist surfaces reach the model as feedback,
        journaled `audit:delivered`. The verdict is not changed by it: the
        finish is accepted, nothing counts as a refusal, and the engine
        lets the model read it (`engine.FINISH_SURFACED`). Every later
        accepted finish returns "" as before.

        A tree equal to the baseline ("nothing to audit") is never accepted,
        in either arm: it returns (False, "") and `unchanged()` is True, so
        the engine ends the run `unchanged` rather than refusing.
        """
        self._await()
        pending = self._take()
        scratch = Path(tempfile.mkdtemp(prefix="saddle-feed-"))
        result = self._audit("finish", (0, 1, 2), self.worktree, scratch)
        assert result is not None
        with self._lock:
            self.results.append(result)
        if result.note.startswith(NOTHING_TO_AUDIT):
            # No verdict, so no accept; `engine._finish` ends the
            # run `unchanged` (read off `unchanged()`), never as a refusal.
            self._record(pending, delivered=False)
            self._record([result], delivered=False)
            return False, ""
        refuse = self.feedback and not result.passed
        # The first accepted finish that carries not-proven
        # findings delivers them; the accept itself is unchanged.
        surface = (
            self.feedback
            and result.passed
            and self.surfaced_tree is None
            and any(f.verdict == "not-proven" for f in result.findings)
        )
        if surface:
            self.surfaced_tree = result.tree
        before = self._record(pending, delivered=refuse)
        text = self._record([result], delivered=refuse or surface)
        if surface:
            return True, text
        if not refuse:
            return True, ""
        return False, "\n\n".join(t for t in (before, text) if t)

    def accepted_unchanged(self) -> bool:
        """True when a finish was accepted with surfaced findings (`final`)
        and the worktree is still the tree that finish audit accepted."""
        if not self.surfaced_tree:
            return False
        scratch = Path(tempfile.mkdtemp(prefix="saddle-feed-"))
        try:
            return snapshot(self.worktree, self.worktree, scratch / "tree") == self.surfaced_tree
        except AuditError:
            return False
        finally:
            shutil.rmtree(scratch, ignore_errors=True)

    def unchanged(self) -> bool:
        """The last audit found nothing to audit: the tree equals the baseline.

        `engine._finish` ends such a run `unchanged`: not
        accepted, not a refusal. Before, the empty finding set passed
        vacuously and the run ended `finished`.
        """
        return bool(self.results) and self.results[-1].note.startswith(NOTHING_TO_AUDIT)

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

    def waivers(self) -> list[str]:
        """`waivers` of the last completed audit; [] before any."""
        return waivers(self.results[-1]) if self.results else []

    def last(self) -> dict[str, object] | None:
        return self.results[-1].to_dict() if self.results else None

    def _journal(self, result: AuditResult, text: str, *, delivered: bool) -> None:
        """One span per completed audit, delivered or withheld.

        Its sidecar seals the whole result (`AuditResult.to_dict`): every
        finding in full and, for an audit that ran tier 2, every scored
        mutant's (name, status, show). The span's detail is capped at 500
        characters in the journal, so without it only the final audit's
        rows survived; with it a reader can
        count the survivors killed between two audits.
        """
        span_id = uuid.uuid4().hex
        digest = write_attempt_sidecar(self.journal, span_id, result.to_dict())
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
                span_id=span_id,
                attempt_hash=digest,
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
