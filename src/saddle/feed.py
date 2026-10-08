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
  so later edits cannot race the audit) and tier 0 (`Auditor.tier0` on each
  changed Python file) then `Auditor.tier1` run on it in a background thread, off
  the model's critical path. Tier 0 is there so a ruff finding reaches the model
  after the edit that made it, not first at finish (#99). One checkpoint runs at a
  time, and a tool call never waits for it: a burst that ends while one is
  running is audited by the next checkpoint, which the first call after the
  running one completes starts on the tree as it is then. On a project whose
  suite takes minutes, waiting would stall the model that long per burst.
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
  tier 0 accepts, on the changed files. Tier 0 runs at checkpoints, at finish
  and on `check`, never per edit (the edit guard stays syntax-only).
- `check()` -- the model's **pull** (`--check-tool`, arm E+A+F only): tier 0
  (`Auditor.tier0` on every changed Python file) and tier 1 of the same
  auditor, synchronously, on the tree as it is now, rendered by the same
  `render` a finish refusal uses; tier 2 too, as finish runs it, when the
  check asks for mutation (#180). Never a finish refusal, and refused (no
  audit run) while the tree is unchanged since the last check of that kind.
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
baseline commit resolves) with the worktree's files copied over, less every
path git ignores there (a `.venv` the model made is neither copied nor
audited); its `git add -A && git write-tree` is the tree id every finding is
reported against, and equals the tree `auto` commits when nothing changed
after it.
"""

from __future__ import annotations

import dataclasses
import os
import shutil
import subprocess
import tempfile
import threading
import time
import uuid
from collections.abc import Callable, Sequence
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path
from typing import Final, Literal, Protocol

from saddle import coverage_text, sandbox
from saddle.audit import AuditError, git_ignored
from saddle.auditor import (
    TASK_REQUIREMENTS,
    TEST_CHANGES,
    Auditor,
    AuditorConfig,
    Finding,
    Findings,
    Tier2Mode,
    coverage_evidence,
    finding_body,
    flip_finding,
    raise_gaps,
    rewritten,
    sanction,
)
from saddle.gates import DEFAULT_MUTANT_SHORTLIST, untyped_raise_asserts
from saddle.impact import ImpactMemo, is_test_file
from saddle.journal import FEED_QUESTION_LINE, append_span, build_span, write_attempt_sidecar
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

CHECK_MUTATION: Final = "mutation"
"""The last word of a `CHECK_SPAN`'s argv when the check ran tier 2 (#180)."""

CHECK_UNCHANGED: Final = "error: check refused: the tree is unchanged since check "
"""Prefix of a refused `check`. Not the tier-0 guard's `REFUSED`, so it is
not counted among the run's guard refusals, and not a finish refusal."""

UNCHANGED_HOW: Final = (
    "; that {kind} saw these same files byte for byte (the worktree's new files "
    "included, gitignored files and anything outside it not), so it would answer the "
    "same. Edit a file before checking again; check {n}'s findings still stand."
)
"""What a refused `check` compared and with which check (#176): a run argued
"but the tree has changed since check 1" against a refusal that said only the
number. A check of another kind (`whole_suite`, `mutation`) of the same files is
not refused, so the kind is named too."""

FLIP_CLEARED_AT_FINISH: Final = (
    "Only your finish summary can clear this: give each changed test its `flip:` line "
    "there, with its evidence. Until finish, checks and checkpoints keep failing on it "
    "while the test stays changed, and say so in one line."
)
FLIP_SAID: Final = "unchanged since {point}, which said it in full: the finish summary clears it"

NOTHING_TO_AUDIT: Final = "nothing to audit"

WHOLE_SUITE_UNSUPPORTED: Final = "this run's auditor cannot run the whole suite in a check"
"""Why a `check` with `whole_suite` decided nothing: blocked, never a narrowed
run that the model would read as the whole suite's answer."""

EDIT_CHECKS_FIRST: Final = (
    "tiers 1 and 2 were not run: an edit check below failed. Fix it (seconds) and "
    "the tests, coverage and mutation run on the next audit."
)
"""An audit's note when tier 0 failed and the later tiers were skipped (`_audit_on`)."""

FLIPS_FIRST: Final = (
    "tiers 1 and 2 were not run: a pre-existing test changed and the finish summary has no "
    "usable `flip:` line for it. Write the line (or restore the test) and the tests, coverage "
    "and mutation run on the next audit."
)
"""An audit's note when the `test-changes` finding refused finish before the suite ran."""
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
    obligation: str = ""
    """`--raise-obligation`, checkpoints only: each changed raise no test
    enters and each type-free raise assertion in a changed test
    (`raise_obligation`). Feedback: `passed` never reads it. Sealed under
    `raise_obligation` when non-empty."""
    untracked: tuple[str, ...] = ()
    """Every file the audited snapshot holds that the worktree does not track
    (`untracked_in`), sorted: the worker's new files and anything else it
    left there, a tool's output included. A failing audit names them
    (`render`), so a test the copy failed is never a guess at what the copy
    held. Sealed under `untracked` when non-empty."""
    duration_ms: int = 0
    """How long the audit took by the clock, snapshot included (`_audit`): the
    duration of its span. Every audit span recorded 0, so a profile could not
    tell the audit's share of a run's wall (#175). Not sealed in the sidecar."""

    @property
    def passed(self) -> bool:
        return not any(failing(f) for f in self.findings)

    @property
    def needs_you(self) -> bool:
        """Some finding is a `question`: it refuses nothing, and the run
        cannot end on it silently."""
        return any(f.verdict == "question" for f in self.findings)

    def to_dict(self) -> dict[str, object]:
        return {
            "point": self.point,
            "tree": self.tree,
            "passed": self.passed,
            **({"needs_you": True} if self.needs_you else {}),
            "note": self.note,
            "findings": [finding_body(f) for f in self.findings],
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
            **({"raise_obligation": self.obligation} if self.obligation else {}),
            **({"untracked": list(self.untracked)} if self.untracked else {}),
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


RAISE_OBLIGATION_HEAD: Final = "Raise-entry check (feedback only; it never refuses finish):"


def raise_obligation(tree: Path, baseline: str, findings: Sequence[Finding]) -> str:
    """The in-loop raise-entry obligation (K2 D3) for one checkpoint, or "".

    Names each changed `raise` the coverage finding says no test enters
    (`auditor.raise_gaps`, the packet's §3.3 rows) and asks for a test that
    takes the branch and asserts the exception by its type; and names each
    `pytest.raises(Exception)`-shaped assertion in a changed test file
    (`gates.untyped_raise_asserts`, B017), which passes on any exception.
    Feedback only: it is no finding, so no verdict reads it.
    """
    coverage = next((f for f in findings if f.gate == "coverage"), None)
    gaps = (
        raise_gaps(
            tree, baseline, coverage.detail, coverage.cites[1] if len(coverage.cites) > 1 else ""
        )
        if coverage is not None
        else []
    )
    names = _git(tree, "diff", "--cached", "--name-only", "--diff-filter=AMR", baseline).split()
    tests = {
        n: (tree / n).read_text()
        for n in sorted(names)
        if n.endswith(".py") and is_test_file(n) and (tree / n).is_file()
    }
    rows = [
        f"- {coverage_text.raise_row(coverage_text.RaiseGap(**g))}. Add a test that takes "
        "this branch and asserts the exception by the type raised there, "
        "`pytest.raises(<that type>)`, not `Exception`."
        for g in gaps
    ]
    rows += [
        f"- {rel}:{line} asserts `{spelled}`: it passes on any exception, a crash "
        "before the check included. Assert the type the code raises."
        for rel, line, spelled in untyped_raise_asserts(tests)
    ]
    return "\n".join([RAISE_OBLIGATION_HEAD, *rows]) if rows else ""


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
        # A tier-0 finding names its file (`Finding.path`) when its detail's
        # headline does not: a format finding's quoted diff names the file in
        # its `--- path` header, below the line the model reads first.
        headline = detail.split("\n", 1)[0]
        where = f"{f.path}: " if f.path and f.path not in headline else ""
        text = f"- {f.gate} (tier {f.tier}): {f.verdict}, {f.reason}: {where}{detail}"
        said[text] = said.get(text, 0) + 1
    lines.extend(text if n == 1 else f"{text} ({n} findings)" for text, n in said.items())
    allowed = [f for f in result.findings if f.reason == "sanctioned" and f.verdict in FAILING]
    for f in allowed:
        lines.append(f"(info) {f.gate} (tier {f.tier}): {f.detail}")
    unproven = [f for f in result.findings if f.verdict == "not-proven"]
    for f in unproven:
        detail = _worded(result, f)
        lines.append(f"(not proven, does not refuse) {f.gate} (tier {f.tier}): {detail}")
    asked = [f for f in result.findings if f.verdict == "question"]
    for f in asked:
        detail = _worded(result, f)
        lines.append(f"{FEED_QUESTION_LINE} {f.gate} (tier {f.tier}): {detail}")
    passed = len(result.findings) - len(bad) - len(allowed) - len(unproven) - len(asked)
    if passed:
        lines.append(f"({passed} other check(s) passed or not applicable)")
    if result.obligation:
        lines.append(result.obligation)
    if result.untracked and not result.passed:
        lines.append(untracked_line(result.untracked))
    return "\n".join(lines)


UNTRACKED_HEAD: Final = "Files the audit copied that git does not track in your worktree:"
"""How `untracked_line` begins."""

UNTRACKED_SHOWN: Final = 10
"""How many untracked files `untracked_line` names before counting the rest."""


def untracked_line(names: Sequence[str]) -> str:
    """The line a failing audit ends with: what its copy held beyond the tracked
    files. The audit judges the worktree as the worker leaves it, and `auto`
    commits it with `git add -A`, so these files are part of the change: a
    pytest-cov worker's `.coverage.<host>.pid<n>...` data file, left by a suite
    still running in the worktree, failed a whole-suite audit's registry test
    twice in one dogfood run while the finding named nothing (#173)."""
    shown = ", ".join(names[:UNTRACKED_SHOWN])
    more = f" and {len(names) - UNTRACKED_SHOWN} more" if len(names) > UNTRACKED_SHOWN else ""
    return (
        f"{UNTRACKED_HEAD} {shown}{more}. They are audited as part of your change: "
        "delete any you did not mean to add (a tool's output, say)."
    )


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


def tree_state(worktree: Path) -> dict[str, tuple[int, int]]:
    """Each file `git status` reports changed or new in `worktree`, with its
    modification time and size; one the disk no longer has is (-1, -1). Read with
    `--no-optional-locks`, so it writes nothing in the run's worktree. Two reads
    that differ mean the tree changed between them, by an edit or by a command
    (#188). Raises `AuditError` when git cannot say."""
    # Not `_git`: it strips its output, and the first entry's status can begin
    # with a space (" M calc.py"), which would shift every path read from it.
    done = subprocess.run(
        [
            "git",
            "-C",
            str(worktree),
            "--no-optional-locks",
            "status",
            "--porcelain",
            "-z",
            "--untracked-files=all",
            "--no-renames",
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    if done.returncode != 0:
        msg = f"git status failed: {done.stderr.strip()}"
        raise AuditError(msg)
    state: dict[str, tuple[int, int]] = {}
    for entry in done.stdout.split("\0"):
        if len(entry) < 4:
            continue
        rel = entry[3:]
        try:
            stat = (worktree / rel).stat()
        except OSError:
            state[rel] = (-1, -1)
            continue
        state[rel] = (stat.st_mtime_ns, stat.st_size)
    return state


STALE_CHECKPOINT: Final = (
    "[This {point} audited the tree as it was before your later changes to {files}: "
    "its findings are about that tree, not about those files as they are now. `"
    + CHECK_TOOL
    + "` audits them as they are now.]"
)
"""What follows a checkpoint delivered after the tree changed (#188). A checkpoint
audits the tree as the burst of edits left it, and arrives later: in one run the
worker read a `ruff format` failure it had fixed with a command since, and spent a
minute working out that the checkpoint was of the tree before its fix."""

STALE_UNKNOWN: Final = "[Whether the tree changed since this {point} could not be read: {why}]"
"""The same place when `git status` could not say: never read as unchanged."""

STALE_NAMED: Final = 5
"""How many changed files `STALE_CHECKPOINT` names; the rest are counted."""


def _copy_files(source: Path, into: Path) -> None:
    """Copy `source` into `into`, less its top-level `.git` and, when `source`
    is a git checkout (it has a `.git`: the run's worktree), every path git
    ignores there (`audit.git_ignored`).

    A gitignored `.venv`, cache or build output the model made is no part of
    the tree `auto` commits; copied, it cost a copy on the model's critical
    path at every checkpoint and was handed to the audit. A directory with no
    `.git` (a checkpoint's frozen copy, filtered when it was taken) is copied
    whole.
    """
    ignored = git_ignored(source) if (source / ".git").exists() else frozenset()
    root = os.fspath(source)

    def ignore(directory: str, names: list[str]) -> set[str]:
        below = os.path.relpath(directory, root)
        prefix = "" if below == "." else f"{below}/"
        skip = {name for name in names if f"{prefix}{name}" in ignored}
        if Path(directory) == source:
            skip.add(".git")
        return skip

    shutil.copytree(source, into, ignore=ignore, dirs_exist_ok=True)


def snapshot(worktree: Path, files: Path, into: Path) -> str:
    """A standalone repo at `into`: `worktree`'s history, `files`' contents.

    Returns its `git add -A && git write-tree`, the tree id the findings are
    reported against.
    """
    _git(worktree, "clone", "-q", "--shared", "--no-checkout", str(worktree), str(into))
    _copy_files(files, into)
    _git(into, "add", "-A")
    return _git(into, "write-tree")


def untracked_in(snap: Path) -> tuple[str, ...]:
    """The files of `snapshot`'s repo at `snap` that its HEAD -- the worktree's
    -- does not have: the worktree's untracked files the copy holds, sorted."""
    added = _git(snap, "diff", "--cached", "--name-only", "--no-renames", "--diff-filter=A", "HEAD")
    return tuple(sorted(added.splitlines()))


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
    task: str | None = None
    """The task text, handed to every audit (`AuditorConfig.task_text`)."""
    tier2: Tier2Mode = "score"
    """`--tier2`; "shortlist" turns on this module's shortlist behaviour too."""
    mutant_shortlist: int = DEFAULT_MUTANT_SHORTLIST
    """How many survivors a mutation finding names (`--mutant-shortlist`)."""
    project_env: Path | None = None
    """The project's virtualenv (`sandbox.project_env`) every audit runs the
    tests on; None leaves the gates on saddle's PATH. Set per audit, inside
    `_audit`, because a checkpoint audit runs on the feed's own thread."""
    p1: Future[Path] | None = None
    """`saddle auto --task-requirements` / `--extract-requirements`: the sealed
    P1 file, or the extraction still producing it. Until it is ready a
    checkpoint reports `task-requirements` not proven ("examples pending"),
    which refuses nothing; at `finish` the feed waits for it (`p1_wait`), and
    one that failed or has still not finished is a question: the run ends
    "needs you", never finished on a check that did not run."""
    raise_obligation: bool = False
    """`--raise-obligation` (off by default): each checkpoint also names the
    changed raises no test enters and the type-free raise assertions
    (`raise_obligation`). Feedback only; it never refuses finish."""
    p1_wait: Callable[[], float] = field(default=lambda: 0.0)
    """Seconds `final` may wait for a pending extraction: the run's remaining time."""
    summary: str = ""
    """The finish summary the model has written so far (`tell_summary`): the text
    a `flip:` line is read from. Empty until `finish` is called."""
    impact_cache: Path | None = None
    dependencies: Path | None = None
    """`AuditorConfig.dependencies`: the run's checkout, whose installed packages its
    worktree lacks."""
    """Where drawn test-impact maps are kept across runs (`Auditor.draw_map`);
    `auto` passes the repository's `.saddle/impact`."""
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
    _checked_tree: tuple[str, bool, bool] | None = None
    _flips_said: dict[str, str] = field(default_factory=dict)
    """Each failing `test-changes` detail the model has read before finish, to
    the point that said it in full (`_shown`)."""
    _dirty: bool = False
    _pending: Future[AuditResult | None] | None = None
    _map_job: Future[AuditResult | None] | None = None
    """The run-start map job (`_draw_map`); a checkpoint may queue behind it."""
    _ready: list[AuditResult] = field(default_factory=list)
    _pool: ThreadPoolExecutor | None = None
    _lock: threading.Lock = field(default_factory=threading.Lock)
    _states: dict[str, dict[str, tuple[int, int]] | str] = field(default_factory=dict)
    """`tree_state` when each checkpoint was taken, by point, or why it could not be read."""
    _config: AuditorConfig | None = None
    _p1_ready: bool = False

    def __post_init__(self) -> None:
        self._config = AuditorConfig(
            journal=self.journal,
            sanctioned_test_rewrites=self.sanctioned_test_rewrites,
            task_text=self.task,
            tier2=self.tier2,
            mutant_shortlist=self.mutant_shortlist,
            # One map for the run, drawn at its start (below) or by its first
            # audit; every other audit runs the test files a change can reach.
            impact=ImpactMemo(cache=self.impact_cache),
            dependencies=self.dependencies,
        )
        if self.auditor is None:
            self.auditor = self.factory(self.worktree, self.baseline, self._config)
        draw = getattr(self.auditor, "draw_map", None)
        if draw is not None and self.impact_cache is not None:
            # Beside the model's first reading, in the one audit slot: a
            # checkpoint that comes due meanwhile waits its turn as it would
            # for another checkpoint, and `check` and `final` wait for it.
            self._pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix="saddle-audit")
            self._pending = self._pool.submit(self._draw_map, draw)
            self._map_job = self._pending

    def _p1_state(self, *, final: bool) -> Finding | None:
        """None once the P1 file is in the auditor's hands (or there is no P1);
        else the finding that says why not: not proven at a checkpoint, a
        question at finish, after waiting up to `p1_wait()` seconds."""
        if self.p1 is None or self._p1_ready:
            return None
        if not final and not self.p1.done():
            return _p1_finding("not-proven", P1_PENDING)
        try:
            path = self.p1.result(timeout=max(0.0, self.p1_wait()) if final else None)
        except TimeoutError:
            return _p1_finding("question", f"P1 could not run: {P1_UNFINISHED}")
        except Exception as exc:  # the extraction's own failure, named
            said = f"P1 could not run: the extraction failed: {type(exc).__name__}: {exc}"
            return _p1_finding("question" if final else "not-proven", said)
        assert self._config is not None
        self._config = dataclasses.replace(self._config, task_requirements=path)
        self.auditor = self.factory(self.worktree, self.baseline, self._config)
        self._p1_ready = True
        return None

    # -- the audit itself ------------------------------------------------------

    def _audit(
        self,
        point: str,
        tiers: tuple[int, ...],
        files: Path,
        scratch: Path,
        *,
        check: bool = False,
        whole_suite: bool = False,
    ) -> AuditResult | None:
        """The audit of `files` at `tiers`. For a `check`, None (nothing run)
        when the tree and the mode are the ones the last check audited."""
        started = time.monotonic()
        with sandbox.using_project_env(self.project_env):
            result = self._audit_on(
                point, tiers, files, scratch, check=check, whole_suite=whole_suite
            )
        if result is None:
            return None
        return dataclasses.replace(result, duration_ms=int((time.monotonic() - started) * 1000))

    def _audit_on(
        self,
        point: str,
        tiers: tuple[int, ...],
        files: Path,
        scratch: Path,
        *,
        check: bool,
        whole_suite: bool = False,
    ) -> AuditResult | None:
        """`_audit`'s body, run with the project environment in place."""
        assert self.auditor is not None
        try:
            tree = snapshot(self.worktree, files, scratch / "tree")
            copied = untracked_in(scratch / "tree")
            if check:
                mode = (tree, whole_suite, 2 in tiers)
                if mode == self._checked_tree:
                    return None
                self._checked_tree = mode
            pending = self._p1_state(final=point == "finish") if 1 in tiers else None
            found: list[Finding] = []
            detail: tuple[tuple[str, str, str], ...] = ()
            if 0 in tiers:
                # Tier 0 first: its checks take seconds, and a failing one is
                # refused before the suite and mutation spend minutes on a tree
                # that must change anyway.
                found.extend(self._tier(0, scratch / "tree").findings)
                if 2 in tiers and any(failing(f) for f in found):
                    return AuditResult(
                        point, tree, tuple(found), note=EDIT_CHECKS_FIRST, untracked=copied
                    )
            flips = (
                flip_finding(
                    scratch / "tree", self.baseline, self.summary, self.sanctioned_test_rewrites
                )
                if 1 in tiers
                else None
            )
            if flips is not None and failing(flips) and 2 in tiers and self.feedback:
                # Cheap, and the same on a retry of this tree: refused before
                # the suite runs, as an edit check is.
                return AuditResult(point, tree, (*found, flips), note=FLIPS_FIRST, untracked=copied)
            prime = getattr(self.auditor, "prime", None)
            if 1 in tiers and 2 in tiers and prime is not None:
                # One run of the battery for both tiers (`Auditor.prime`): the
                # loop below then reads tiers 1 and 2 from the cache.
                prime(scratch / "tree")
            for tier in (t for t in tiers if t != 0):
                got = self._tier(tier, scratch / "tree", whole_suite=whole_suite)
                found.extend(sanction(f, self.sanctioned_test_rewrites) for f in got.findings)
                detail = detail or got.mutant_detail
                if tier == 1 and flips is not None:
                    found.append(flips)
                if tier == 1 and pending is not None:
                    found.append(pending)
            words = _coverage_words(scratch / "tree", self.baseline, found)
            owed = (
                raise_obligation(scratch / "tree", self.baseline, found)
                if self.raise_obligation and point.startswith("checkpoint")
                else ""
            )
            return AuditResult(
                point,
                tree,
                tuple(found),
                mutant_detail=detail,
                coverage=words,
                obligation=owed,
                untracked=copied,
            )
        except AuditError as exc:
            if str(exc).startswith(NOTHING_TO_AUDIT):
                return AuditResult(point, "", (), note=str(exc))
            return AuditResult(point, "", (_blocked(str(exc)),))
        except Exception as exc:  # an audit that crashed decided nothing
            return AuditResult(point, "", (_blocked(f"{type(exc).__name__}: {exc}"),))
        finally:
            shutil.rmtree(scratch, ignore_errors=True)

    def _tier(self, tier: int, tree: Path, *, whole_suite: bool = False) -> Findings:
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
        if tier == 2:
            return self.auditor.tier2(tree)
        if not whole_suite:
            return self.auditor.tier1(tree)
        whole: Callable[[Path], Findings] | None = getattr(self.auditor, "tier1_whole_suite", None)
        if whole is None:
            raise AuditError(WHOLE_SUITE_UNSUPPORTED)
        return whole(tree)

    def _checkpoint(self, point: str, scratch: Path) -> AuditResult:
        result = self._audit(point, (0, 1), scratch / "frozen", scratch)
        assert result is not None
        with self._lock:
            self._ready.append(result)
            self.results.append(result)
        return result

    # -- engine hooks ----------------------------------------------------------

    def before_tool(self, name: str) -> None:
        """Start a checkpoint audit if `name` ends a burst of edits and none is
        in flight. The tool call never waits for one: a burst that ends while a
        checkpoint is running stays dirty, and the first call after that audit
        completes starts the next checkpoint on the tree as it is then."""
        if name in EDIT_TOOLS or not self._dirty:
            return
        if name in (FINISH_TOOL, CHECK_TOOL):
            return  # `final` / `check` audit this tree themselves; no checkpoint too
        busy = self._pending is not None and not self._pending.done()
        if busy and self._pending is not self._map_job:
            return  # one checkpoint in flight at a time; the auditor is not shared
        # Behind the run-start map job, a checkpoint queues: the one audit slot
        # runs it once the map is drawn, on the tree frozen now.
        self._dirty = False
        if self._pending is not None and self._pending.done():
            self._await()  # collects the finished job's future; never waits
        self.checkpoints += 1
        point = f"checkpoint {self.checkpoints}"
        self._states[point] = self._tree_state()
        scratch = Path(tempfile.mkdtemp(prefix="saddle-feed-"))
        # The copy is taken now, before the tool runs: it is this burst's tree,
        # whatever the model does while the audit is in flight.
        try:
            _copy_files(self.worktree, scratch / "frozen")
        except (AuditError, OSError) as exc:
            # A blocked checkpoint that says why; the tool call it rides on
            # goes ahead, and the next burst of edits tries again.
            shutil.rmtree(scratch, ignore_errors=True)
            failed = AuditResult(point, "", (_blocked(f"checkpoint copy failed: {exc}"),))
            with self._lock:
                self._ready.append(failed)
                self.results.append(failed)
            return
        if self._pool is None:
            self._pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix="saddle-audit")
        self._pending = self._pool.submit(self._checkpoint, point, scratch)

    def after_tool(self, name: str, ok: bool) -> None:
        """Mark the tree dirty after a successful edit."""
        if name in EDIT_TOOLS and ok:
            self._dirty = True

    def _draw_map(self, draw: Callable[[Path], str]) -> None:
        """Draw the run's test-impact map over the worktree as it is at the
        start, and journal how (`Auditor.draw_map`). A map that cannot be
        drawn leaves every audit on the whole suite, and says why."""
        scratch = Path(tempfile.mkdtemp(prefix="saddle-map-"))
        started = time.monotonic()
        try:
            with sandbox.using_project_env(self.project_env):
                snapshot(self.worktree, self.worktree, scratch / "tree")
                said = draw(scratch / "tree")
        except Exception as exc:  # the audits then run the whole suite
            said = f"no map: {type(exc).__name__}: {exc}"
        finally:
            shutil.rmtree(scratch, ignore_errors=True)
        # With a map, its tests' fingerprints are sealed beside its span (#80).
        held = getattr(self.auditor, "map_fingerprints", None)
        prints = held() if callable(held) and said.startswith("map ") else None
        span_id = uuid.uuid4().hex
        digest = (
            write_attempt_sidecar(self.journal, span_id, {"test_fingerprints": prints})
            if prints is not None
            else ""
        )
        append_span(
            self.journal,
            build_span(
                node_id="chat#1",
                argv=["audit", "impact-map"],
                duration_ms=int((time.monotonic() - started) * 1000),
                exit_code=0 if said.startswith("map ") else 1,
                detail=said,
                name="audit:impact-map",
                parent_id=self.run_span,
                span_id=span_id,
                attempt_hash=digest,
            ),
        )

    def _take(self) -> list[AuditResult]:
        with self._lock:
            ready, self._ready = self._ready, []
        return ready

    def _record(self, ready: list[AuditResult], *, delivered: bool) -> str:
        texts = [render(self._shown(result)) + self._since(result) for result in ready]
        for result, text in zip(ready, texts, strict=True):
            self._journal(result, text, delivered=delivered)
        return "\n\n".join(texts)

    def collect(self) -> str:
        """Completed checkpoint audits: journaled, and returned as text if delivered."""
        text = self._record(self._take(), delivered=self.feedback)
        return text if self.feedback else ""

    def check(self, *, whole_suite: bool = False, mutation: bool = False) -> str:
        """The model's pull: tiers 0 and 1 on the tree as it is now, and tier 2 too
        with `mutation`.

        `whole_suite` runs every test file at tier 1, not only those the change
        can reach (#171): the run's way to ask about a test far from its change
        without starting the suite by hand.

        `mutation` runs tier 2 as finish runs it, mutation on the changed lines
        with finish's engines, sample and kill bar, so its survivors are the ones
        finish would name on this tree (#180): workers wrote their own mutation
        loops because nothing answered that before finish. Finish still runs tier
        2 on its own tree.

        Rendered exactly as a finish refusal renders its audit (`render`), so
        a finding reads the same whichever way it arrives. Without `mutation`
        tier 2 is never run here. On a tree unchanged since the last check of
        the same kind nothing runs and the call is refused. A check never
        touches the finish refusal count and never shortens the finish audit.
        """
        self._await()  # the auditor is not shared with a pending checkpoint
        scratch = Path(tempfile.mkdtemp(prefix="saddle-check-"))
        result = self._audit(
            f"check {len(self.checks) + 1}",
            (0, 1, 2) if mutation else (0, 1),
            self.worktree,
            scratch,
            check=True,
            whole_suite=whole_suite,
        )
        if result is None:
            kind = "whole-suite check" if whole_suite else "check"
            if mutation:
                kind = f"mutation {kind}"
            n = len(self.checks)
            return f"{CHECK_UNCHANGED}{n}{UNCHANGED_HOW.format(kind=kind, n=n)}"
        self._dirty = False  # this tree is audited; no checkpoint of it too
        self.checks.append(result)
        text = render(self._shown(result))
        span_id = uuid.uuid4().hex
        digest = write_attempt_sidecar(self.journal, span_id, result.to_dict())
        append_span(
            self.journal,
            build_span(
                node_id="chat#1",
                argv=[
                    "check",
                    result.point,
                    result.tree,
                    *(["whole-suite"] if whole_suite else []),
                    *([CHECK_MUTATION] if mutation else []),
                ],
                duration_ms=result.duration_ms,
                exit_code=0 if result.passed else 1,
                detail=text,
                name=CHECK_SPAN,
                parent_id=self.run_span,
                span_id=span_id,
                attempt_hash=digest,
            ),
        )
        return text

    def _shown(self, result: AuditResult) -> AuditResult:
        """`result` as the model reads it before finish: a failing `test-changes`
        finding in full the first time, with what clears it, and after that one
        line naming where it was said (#176). Only finish's summary can clear it,
        so a run read the same paragraph at every checkpoint from 17 on. The
        finding keeps its verdict, so a check still answers as finish would, and
        the journal seals it whole; finish always shows it in full."""
        if result.point == "finish":
            return result
        shown: list[Finding] = []
        for f in result.findings:
            if f.gate == TEST_CHANGES and failing(f):
                first = self._flips_said.setdefault(f.detail, result.point)
                said = (
                    f"{f.detail}\n{FLIP_CLEARED_AT_FINISH}"
                    if first == result.point
                    else FLIP_SAID.format(point=first)
                )
                f = dataclasses.replace(f, detail=said)
            shown.append(f)
        return dataclasses.replace(result, findings=tuple(shown))

    def tell_summary(self, summary: str) -> None:
        """Record the summary `finish` was called with, before it is audited: a changed
        pre-existing test is allowed only with a `flip:` line in it (`flip_finding`)."""
        self.summary = summary

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
        # A question ends the run "needs you" with this audit as finish's
        # result (`engine._finish`), so the model is shown it.
        asked = self.feedback and result.needs_you
        text = self._record([result], delivered=refuse or surface or asked)
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

    def questions(self) -> list[str]:
        """The last audit's `question` findings, one line each, for the
        "needs you" stop; [] with feedback off (arm E+A records them in the
        audit sidecar and changes nothing) or before any audit."""
        if not self.feedback or not self.results:
            return []
        return [
            f"{f.gate} (tier {f.tier}): {f.detail}"
            for f in self.results[-1].findings
            if f.verdict == "question"
        ]

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

    def _tree_state(self) -> dict[str, tuple[int, int]] | str:
        try:
            return tree_state(self.worktree)
        except AuditError as exc:
            return str(exc)

    def _since(self, result: AuditResult) -> str:
        """`STALE_CHECKPOINT` for a checkpoint whose tree has changed since it was
        taken, `STALE_UNKNOWN` when that cannot be read, else "" (#188)."""
        before = self._states.pop(result.point, None)
        if before is None:
            return ""
        now = self._tree_state()
        if isinstance(before, str) or isinstance(now, str):
            why = before if isinstance(before, str) else now
            return "\n" + STALE_UNKNOWN.format(point=result.point, why=why)
        changed = sorted(p for p in before.keys() | now.keys() if before.get(p) != now.get(p))
        if not changed:
            return ""
        files = ", ".join(changed[:STALE_NAMED])
        if len(changed) > STALE_NAMED:
            files += f" and {len(changed) - STALE_NAMED} more"
        return "\n" + STALE_CHECKPOINT.format(point=result.point, files=files)

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
                duration_ms=result.duration_ms,
                exit_code=0 if result.passed else 1,
                detail=text,
                name=f"audit:{'delivered' if delivered else 'withheld'}",
                parent_id=self.run_span,
                span_id=span_id,
                attempt_hash=digest,
            ),
        )


P1_PENDING: Final = "examples pending: the task-text extraction has not finished"
P1_UNFINISHED: Final = "the task-text extraction had not finished when finish was called"


def _p1_finding(verdict: Literal["not-proven", "question"], detail: str) -> Finding:
    """The `task-requirements` finding the feed reports while P1 cannot run."""
    return Finding(
        gate=TASK_REQUIREMENTS,
        tier=1,
        verdict=verdict,
        reason="unknown",
        detail=detail,
        cites=("saddle.feed.AuditFeed",),
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
