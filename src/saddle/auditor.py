"""The tiered auditor (Phase 2): the gate battery split by when it runs.

The daily-driver design's "audit tiers" table, as a library an executor can
call at a checkpoint and `saddle audit --tiered` calls over any diff:

- **tier 0, at the tool** (`Auditor.tier0(path, new_text)`): `syntax`
  (`gates.check_syntax`), `ruff` on the one file (`evidence.ruff_findings`
  on the new text and on the baseline's copy, `gates.introduced_findings`,
  `gates.check_ruff`) and `imports` (`check_imports` below, the one check
  that is not one of the thirteen gates). No suite runs.
- **tier 1, per checkpoint** (`Auditor.tier1(tree)`): `tests`, `coverage`,
  `dead-code`, `public-deletions`, `node-scope`, `target-scope` and
  `assertion-preservation`, read off one `runner.run_node_gate(...,
  tier2=False)` run, which skips the mutation run, the property oracle and
  the red-phase samples, so the suite runs once.
- **tier 2, asynchronous** (`Auditor.tier2(tree)`): `mutation` (untested
  mutants counted as survivors, `evidence.mutation_sample`),
  `property-coverage`, `red-phase`, `requirement-binding` and `full-suite`
  (the `tests` check of the same full `runner.run_node_gate` run: with no
  plan the audit's test command is the whole suite). Tier 2 never runs on a
  tree whose tier 1 failed: it returns one `blocked` finding instead.

Every verdict is keyed by the tree (`audit.staged_copy`'s `git write-tree`
over the worktree with untracked files staged) plus the resolved baseline,
the test command, the node and `audit.gate_surface()`, so an identical tree
is never gated twice at the same tier. Tier 0 is keyed by the file's path
and bytes instead, since it sees one file and no tree. Every run of the
tests takes the project's time limit (`evidence.suite_limit`) and worker
count (`evidence.suite_workers`), read at the resolved baseline: the key
names them, and the tree audited cannot move them.

Without a plan node the four plan-relative checks are `not-applicable`
(`audit.NOT_APPLICABLE`); with one (`AuditorConfig.node`), they run.

Layering: imports `audit`, `evidence`, `gates`, `journal` and `runner`;
`gates` still imports nothing of this. Only the CLI imports it.
"""

from __future__ import annotations

import ast
import contextvars
import dataclasses
import functools
import hashlib
import importlib.util
import json
import os
import re
import shlex
import shutil
import sys
import tempfile
import tomllib
import uuid
from collections.abc import Callable, Mapping, Sequence, Set
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import dataclass, field
from fnmatch import fnmatch
from pathlib import Path, PurePosixPath
from typing import Any, Final, Literal

from saddle import coverage_text, impact, runner, sandbox
from saddle.audit import (
    AUDIT_TEST_COMMAND,
    AuditError,
    audit_checks,
    audit_node,
    baseline_tree,
    gate_surface,
    staged_copy,
)
from saddle.dag import Node
from saddle.evidence import (
    DEFAULT_TEST_TIMEOUT_S,
    CapturedRun,
    MutationOutcome,
    SuiteLimitError,
    changed_statements,
    drop_test_caches,
    format_overrides,
    git_diff,
    ruff_argv,
    ruff_findings,
    run_capture,
    run_suite_capture,
    suite_limit,
    suite_run,
    suite_workers,
    tree_memory_limit,
)
from saddle.gates import (
    DEFAULT_MUTANT_SHORTLIST,
    GateCheck,
    RuffFinding,
    TaskRequirementsCheck,
    Tier1Result,
    check_mutation_shortlist,
    check_ruff,
    check_syntax,
    introduced_findings,
    set_aside_kind,
    shortlist_order,
    spared_definitions,
)
from saddle.impact import ImpactMemo
from saddle.journal import (
    JOURNAL_QUESTION_EXIT,
    MAX_SPAN_DETAIL_CHARS,
    SEALED_CUT,
    append_span,
    build_span,
    scrub_thinking,
    write_attempt_sidecar,
)
from saddle.task_examples import WOULD_REFUSE
from saddle.task_requirements import check_tree

Verdict = Literal["pass", "fail", "not-applicable", "blocked", "not-proven", "question"]
"""`question`: the gate could not decide, and a person must. It is neither a
pass nor a refusal: it does not count against `Findings.passed` (so it
neither blocks tier 2 nor refuses `finish`), and `Findings.needs_you` carries
it so the run cannot end on it silently (`feed.AuditFeed.questions`)."""
Tier2Mode = Literal["score", "shortlist"]
TIER2_MODES: Final[tuple[Tier2Mode, ...]] = ("score", "shortlist")
Reason = Literal["code-wrong", "evidence-thin", "scope", "unknown", "sanctioned"]
"""`sanctioned`: a failing `assertion-preservation` finding that names only
tests the task itself declared rewritable (`AuditorConfig.sanctioned_test_rewrites`).
The verdict stays `fail` -- the gate did see rewritten assertions -- but the
finding does not count against `Findings.passed`, so it neither blocks tier 2
nor refuses an autonomous run's `finish`."""

TASK_REQUIREMENTS: Final = "task-requirements"
"""P1's tier-1 gate (`task_requirements.check_tree`): emitted after `TIER1`,
and only when the audit was given a requirements file
(`AuditorConfig.task_requirements`); without one there is no task text to
judge against, and no finding."""

# Which gate runs at which tier; `Auditor` emits them in exactly this order.
TIER0: Final[tuple[str, ...]] = ("syntax", "ruff", "imports")
TIER1: Final[tuple[str, ...]] = (
    "tests",
    "coverage",
    "dead-code",
    "public-deletions",
    "node-scope",
    "target-scope",
    "assertion-preservation",
)
TIER2: Final[tuple[str, ...]] = (
    "mutation",
    "property-coverage",
    "red-phase",
    "requirement-binding",
    "full-suite",
)

# The function each finding's verdict comes from (the `cites` of a finding).
REUSES: Final[dict[str, str]] = {
    "syntax": "saddle.gates.check_syntax",
    "ruff": "saddle.gates.check_ruff",
    "imports": "saddle.auditor.check_imports",
    "tests": "saddle.gates.check_test_command",
    "coverage": "saddle.gates.check_changed_line_coverage",
    "dead-code": "saddle.gates.check_dead_additions",
    "public-deletions": "saddle.gates.check_public_deletions",
    "node-scope": "saddle.gates.check_node_scope",
    "target-scope": "saddle.gates.check_target_files",
    "assertion-preservation": "saddle.gates.check_assertion_preservation",
    "mutation": "saddle.gates.check_mutation",
    "property-coverage": "saddle.gates.check_property_coverage",
    "red-phase": "saddle.gates.check_red_phase",
    "requirement-binding": "saddle.gates.check_requirement_binding",
    "full-suite": "saddle.gates.check_test_command",
    TASK_REQUIREMENTS: "saddle.gates.check_task_requirements",
}

# The calibration hook: what a failure of each gate claims about the change.
# A gate whose failure says the code is wrong, one whose failure says the
# evidence for it is thin, and one whose failure says the change strayed.
REASONS: Final[dict[str, Reason]] = {
    "syntax": "code-wrong",
    "ruff": "code-wrong",
    "imports": "code-wrong",
    "tests": "code-wrong",
    "public-deletions": "code-wrong",
    "full-suite": "code-wrong",
    "coverage": "evidence-thin",
    "dead-code": "evidence-thin",
    "assertion-preservation": "evidence-thin",
    "mutation": "evidence-thin",
    "property-coverage": "evidence-thin",
    "red-phase": "evidence-thin",
    "requirement-binding": "evidence-thin",
    TASK_REQUIREMENTS: "code-wrong",
    "node-scope": "scope",
    "target-scope": "scope",
}

# A mutation detail that names a tool that never decided anything is no
# claim about the code or its tests (gates.check_mutation's own wording).
_TOOL_FAILURE_PREFIXES: Final = ("mutation tool failed", "mutation not measured")


def _reads_back(text: str, finding: Finding) -> bool:
    """`text` still parses, as `finding`'s gate and verdict, once the ledger
    line has redacted and capped it (`journal.build_span`)."""
    try:
        body = json.loads(scrub_thinking(text)[:MAX_SPAN_DETAIL_CHARS])
    except ValueError:
        return False
    return (
        isinstance(body, dict)
        and body.get("gate") == finding.gate
        and body.get("verdict") == finding.verdict
    )


def finding_body(finding: Finding) -> dict[str, object]:
    """The finding's fields as the dict its records carry.

    `path` is absent when it holds none: tiers 1 and 2 judge the tree, not
    a file, so their records read exactly as a ledger sealed before the
    field; a record that lacks the key reads back with it empty
    (`Findings.from_dict`)."""
    body = dataclasses.asdict(finding)
    if not body["path"]:
        body.pop("path")
    return body


def sealed_finding(finding: Finding) -> str:
    """The finding as the JSON its `audit-tier<N>:<gate>` span seals: one that
    parses on the ledger line, whatever the verdict, with its gate, tier,
    verdict and reason whole.

    A span's detail is redacted and capped at `MAX_SPAN_DETAIL_CHARS`
    (`journal.build_span`), and JSON cut mid-string does not parse: a reader
    then has only the exit code, which cannot tell a pass from a not-proven
    finding (both exit 0). So a finding the line cannot hold is sealed with
    only its first cite (the gate) and, if that is not enough, its detail cut
    to fit, marked `SEALED_CUT`, keeping "would refuse at full strength"
    when the whole said it. So is one whose line redaction would break (a
    secret-shaped value last in a string eats the closing quote). A finding
    the line holds is sealed as before, byte for byte. The whole finding is
    in the audit's own sidecar (`feed.AuditResult`), and a coverage
    finding's lines and basis in its span's sidecar (`coverage_evidence`).

    A tier-0 finding seals the file it checked under `path`, so a reader can
    tell which file a per-file check checked; a finding that checked no
    file seals no key.
    """
    body = finding_body(finding)
    text = json.dumps(body, sort_keys=True)
    if _reads_back(text, finding):
        return text
    body = {**body, "cites": list(finding.cites[:1])}
    text = json.dumps(body, sort_keys=True)
    if _reads_back(text, finding):
        return text  # the gate's basis was what did not fit; the detail is whole
    mark = SEALED_CUT + (f" [{WOULD_REFUSE}]" if WOULD_REFUSE in finding.detail else "")
    detail = finding.detail
    while True:
        text = json.dumps({**body, "detail": detail + mark}, sort_keys=True)
        if not detail or _reads_back(text, finding):
            return text
        over = len(text) - MAX_SPAN_DETAIL_CHARS
        detail = detail[: len(detail) - max(1, over)]


def p1_tally(check: TaskRequirementsCheck) -> dict[str, Any]:
    """What the P1 finding's span seals beside it: every unjudged unit or
    example named, and the counts a trial reads (`TaskRequirementsCheck.units`
    and `.examples`), with the strength the gate ran at."""
    total, judged, asked, unjudged = check.units
    examples, passed, wrong, questions = check.examples
    return {
        "unjudged": list(check.unjudged),
        "units": {"total": total, "judged": judged, "asked": asked, "unjudged": unjudged},
        "examples": {"total": examples, "pass": passed, "code-wrong": wrong, "question": questions},
        "strength": "full" if (check.basis or "").startswith("full strength") else "question",
    }


_JOURNAL_EXIT: Final[dict[Verdict, int]] = {
    "pass": 0,
    "fail": 1,
    "not-applicable": 0,
    "blocked": 2,
    "not-proven": 0,
    "question": JOURNAL_QUESTION_EXIT,
}


@dataclass(frozen=True)
class Survivor:
    """One surviving changed-line mutant, as a shortlist names it."""

    path: str
    line: int
    name: str
    status: str
    mutation: str
    """The mutant's removed and added lines (`evidence.mutation_text`)."""
    source: str
    """The changed line's own text, stripped."""
    behaviour: str
    """What the line serves: the enclosing function's name and docstring's first line."""


@dataclass(frozen=True)
class Finding:
    """One gate's verdict at one tier, in a shape a tool result can carry."""

    gate: str
    tier: int
    verdict: Verdict
    reason: Reason
    detail: str
    cites: tuple[str, ...]
    path: str = ""
    """The file this finding checked, relative to the audited tree's root:
    tier 0 checks one file's bytes, so its records name the file, and a
    failure on one file cannot read under a pass on another (the packet's
    Edit checks row keeps the latest finding per gate per file). Tiers 1
    and 2 judge the tree, so their findings leave it empty, as does a
    record sealed before the field."""


@dataclass(frozen=True)
class Findings:
    """A tier's findings over one tree (or, at tier 0, one file's bytes,
    named in each finding's `path`)."""

    tier: int
    key: str
    findings: tuple[Finding, ...]
    cached: bool = False
    survivors: tuple[Survivor, ...] = ()
    """`--tier2 shortlist` only: the tier-2 mutation finding's open survivors,
    all of them, in shortlist order (its detail names the first few). Empty,
    and absent from `to_dict`, otherwise."""
    mutant_detail: tuple[tuple[str, str, str], ...] = ()
    """Tier 2 only: (name, status, show) for every scored mutant
    (`MutationOutcome.mutant_detail`), in either mode; absent from `to_dict`
    when empty. Recording only."""

    @property
    def passed(self) -> bool:
        """Nothing refuses: a `question` does not, so it is counted here and
        reported by `needs_you` instead."""
        return all(
            f.verdict in ("pass", "not-applicable", "not-proven", "question")
            or f.reason == "sanctioned"
            for f in self.findings
        )

    @property
    def needs_you(self) -> bool:
        """Some finding is a `question`: a person must decide it."""
        return any(f.verdict == "question" for f in self.findings)

    def to_dict(self) -> dict[str, object]:
        return {
            "tier": self.tier,
            "key": self.key,
            "passed": self.passed,
            **({"needs_you": True} if self.needs_you else {}),
            "cached": self.cached,
            "findings": [finding_body(f) for f in self.findings],
            **(
                {"survivors": [dataclasses.asdict(v) for v in self.survivors]}
                if self.survivors
                else {}
            ),
            **(
                {
                    "mutant_detail": [
                        {"name": n, "status": s, "show": t} for n, s, t in self.mutant_detail
                    ]
                }
                if self.mutant_detail
                else {}
            ),
        }

    @staticmethod
    def from_dict(data: dict[str, object]) -> Findings:
        raw = data["findings"]
        assert isinstance(raw, list)
        return Findings(
            tier=int(str(data["tier"])),
            key=str(data["key"]),
            findings=tuple(Finding(**{**f, "cites": tuple(f["cites"])}) for f in raw),
            survivors=tuple(Survivor(**v) for v in data.get("survivors", ())),  # type: ignore[attr-defined]
            mutant_detail=tuple(
                (d["name"], d["status"], d["show"])
                for d in data.get("mutant_detail", ())  # type: ignore[attr-defined]
            ),
        )


@dataclass(frozen=True)
class AuditorConfig:
    test_command: str = AUDIT_TEST_COMMAND
    # A plan node makes the plan-relative checks applicable; None audits as
    # `audit.audit_node`, a `refactor` node with no plan behind it.
    node: Node | None = None
    journal: Path | None = None
    cache_dir: Path | None = None
    extra_import_roots: tuple[str, ...] = field(default=("src",))
    sanctioned_test_rewrites: tuple[str, ...] = ()
    """Test functions the task orders rewritten (T5's rule 8). Declared by
    the caller, sealed in the run's ledger, part of the cache key; never a
    blanket exemption: a finding naming any other test still fails."""
    tier2: Tier2Mode = "score"
    """`--tier2`: "score" (default) is the 85% kill-rate verdict, byte for byte
    as before the shortlist mode existed. "shortlist" decides tier 2 on open survivors
    (`gates.check_mutation_shortlist`), makes coverage a locator
    (`not-proven`, never a refusal) and turns on the finish-time behaviour in
    `feed` (format, cheap-route checks, way-out claims)."""
    mutant_shortlist: int = DEFAULT_MUTANT_SHORTLIST
    """How many open survivors a mutation finding's detail names (`--mutant-shortlist`)."""
    task_requirements: Path | None = None
    """`--task-requirements`: a sealed P1 file. Tier 1 then runs the
    `task-requirements` gate beside the tests, and the file's bytes join the
    cache key, so a changed file never reuses a verdict."""
    impact: ImpactMemo | None = None
    """The test-impact map one run's audits share (`saddle.impact`). None runs
    the whole suite at every audit, as before. With a memo the first audit
    runs it with per-test contexts and records the map, and every later one
    runs only the test files the change can reach; the whole suite again
    whenever `impact.select` cannot say."""


def _file_sha(path: Path) -> str:
    """The sha256 of `path`'s bytes, or "unreadable": part of a cache key."""
    try:
        return hashlib.sha256(path.read_bytes()).hexdigest()
    except OSError:
        return "unreadable"


_REWROTE: Final = " rewrote assertions in: "
"""`gates.check_assertion_preservation`'s wording before the test names."""

GREEN_ON_BASELINE: Final = "; not sanctioned: green on the baseline sources: "
"""Appended to a sanctionable finding whose rewritten test(s) pass against
the baseline's sources (`green_on_baseline`). `sanction` never reclasses a
finding carrying it, so the feed cannot re-sanction what the auditor refused."""


SANCTIONED_SUFFIX: Final = " (all sanctioned by the task)"
"""What `sanction` appends to a finding it reclasses."""

REWRITE_QUESTION: Final = (
    "; each fails on the original code, so it may pin behaviour the task "
    "changes: a person approves or rejects the rewrite when the run ends"
)
"""Appended to an assertion-preservation finding turned into a question: the
rewritten tests are red on the baseline (they assert new behaviour) and no one
sanctioned them at the start, so a person decides rather than the run refusing
work the task may require. A watched run had to rewrite two tests that pinned
the very masking its task removed, and could not finish any other way."""


def _selection(memo: ImpactMemo | None, tree: Path, baseline: str) -> tuple[str, ...] | None:
    """The test files `tree`'s change can reach, or None for the whole suite:
    no memo, no map yet, or a selection that failed (never read as "none")."""
    if memo is None or memo.tests is None:
        return None
    try:
        return impact.select(tree, baseline, memo.tests)
    except (OSError, RuntimeError, ValueError):
        return None


def _record_impact(memo: ImpactMemo, tree: Path, data_file: str, suite: CapturedRun) -> None:
    """Draw the map from a whole-suite run whose tests ran to the end (exit 0
    or 1): one cut short records only the tests it reached."""
    if suite.exit_code in (0, 1) and not suite.timed_out:
        drawn = impact.build(data_file, tree)
        if drawn is not None:
            memo.tests = drawn


MUTATION_UNMEASURED: Final = (
    "not measured: the mutation run's time budget ran out before mutmut decided a "
    "mutant ({generated} generated; mutmut first runs every test that covers a changed "
    "line, one at a time). This is no finding against the change and no edit can clear "
    "it: call finish again to end the run, and a person decides whether to measure it "
    "on its own."
)
"""The mutation finding when `MutationOutcome.budget_spent` and nothing was
decided: not proven, never a refusal. A watched run's correct tree was sent
back with "no mutants decided" because mutmut spent the whole budget running
the 408 tests that covered its changed lines before its first mutant."""


def syntax_key(copy: Path) -> str:
    """The staged tree as the interpreter sees it: each `.py` file by its
    syntax tree (formatting and comments dropped, docstrings kept), every other
    file by its bytes. Two trees with the same key run the same code."""
    listed = run_capture(["git", "ls-files", "-z"], copy).stdout.split("\0")
    digest = hashlib.sha256()
    for name in sorted(n for n in listed if n):
        try:
            data = (copy / name).read_bytes()
        except OSError:
            continue
        if name.endswith(".py"):
            try:
                data = ast.dump(ast.parse(data)).encode()
            except (SyntaxError, ValueError):
                pass
        digest.update(name.encode() + b"\0" + hashlib.sha256(data).digest())
    return digest.hexdigest()


def _reusable(found: Findings) -> bool:
    """Findings a format-only edit cannot change or make stale: nothing failed
    or was blocked (a failure is re-proved on the tree it is shown for), and
    nothing not proven names lines, except the budget-bound mutation note."""
    for f in found.findings:
        if f.verdict in ("fail", "blocked") and f.reason != "sanctioned":
            return False
        if f.verdict == "not-proven" and not f.detail.startswith(MUTATION_UNMEASURED[:12]):
            return False
    return True


def _blocked_tier2(key: str, first: Findings) -> Findings:
    """Tier 2's one finding when tier 1 on the same tree failed: blocked, naming
    what failed (a sanctioned finding did not fail, so it is not named)."""
    failed = ", ".join(
        f.gate for f in first.findings if f.verdict == "fail" and f.reason != "sanctioned"
    )
    blocked = _finding("mutation", 2, "blocked", f"tier 1 failed ({failed}); tier 2 not run", None)
    return Findings(tier=2, key=key, findings=(blocked,))


def rewritten(detail: str) -> set[str]:
    """The test names an assertion-preservation detail says were rewritten,
    read before any suffix `sanction` or the baseline check appended."""
    detail = detail.split(SANCTIONED_SUFFIX, 1)[0].split(GREEN_ON_BASELINE, 1)[0]
    _, sep, names = detail.partition(_REWROTE)
    return {n.strip() for n in names.split(",") if n.strip()} if sep else set()


def sanction(finding: Finding, sanctioned: Sequence[str]) -> Finding:
    """Reclass a failing assertion-preservation finding that names only
    sanctioned tests; every other finding is returned unchanged.

    A finding the auditor marked `GREEN_ON_BASELINE` is never reclassed:
    a sanctioned rewrite is accepted only if it is red on the baseline's
    sources."""
    if finding.gate != "assertion-preservation" or finding.verdict != "fail":
        return finding
    if GREEN_ON_BASELINE in finding.detail:
        return finding
    named = rewritten(finding.detail)
    if not named or not named <= set(sanctioned):
        return finding
    return dataclasses.replace(
        finding,
        reason="sanctioned",
        detail=f"{finding.detail}{SANCTIONED_SUFFIX}",
    )


_TEST_NAMES: Final = ("test_*.py", "*_test.py", "conftest.py")
_TEST_DIRS: Final = frozenset({"tests", "test"})
_BASELINE_IGNORE: Final = shutil.ignore_patterns(
    ".git", "__pycache__", ".pytest_cache", ".coverage*", "mutants", ".saddle"
)


def _test_side(path: str) -> bool:
    """A file the test suite owns: a test module, a conftest, or anything
    under a `tests`/`test` directory. Everything else is a source."""
    parts = PurePosixPath(path).parts
    return any(fnmatch(parts[-1], p) for p in _TEST_NAMES) or bool(_TEST_DIRS & set(parts[:-1]))


def green_on_baseline(
    copy: Path,
    resolved: str,
    names: Sequence[str],
    test_command: str,
    *,
    timeout: float = DEFAULT_TEST_TIMEOUT_S,
) -> list[str]:
    """Which of the rewritten tests `names` PASS with the baseline's sources.

    A behavioural negative control: a sanctioned
    rewrite is the task's order to assert the *redefined* behaviour, so it
    must fail on the code before the change. One that passes there did not
    assert it (`construct/vacuous`: `assert True`; or it kept the old
    value). The audited tree is copied, every changed non-test file is put
    back as the baseline has it (added ones removed, deleted ones restored),
    and the named tests run there under the test command's limits (`timeout`
    is the audit's `evidence.suite_limit`). A name
    is green when every test node it names passed; a name that did not
    collect or did not run on the baseline is red (it cannot pass there).
    """
    changed = run_capture(
        ["git", "diff", "--cached", "--name-status", "--no-renames", resolved], copy
    ).stdout.splitlines()
    with tempfile.TemporaryDirectory(prefix="saddle-baseline-") as scratch:
        base = Path(scratch) / "tree"
        shutil.copytree(copy, base, ignore=_BASELINE_IGNORE)
        for line in changed:
            status, _, path = line.partition("\t")
            if not path or _test_side(path):
                continue
            if status == "A":
                (base / path).unlink(missing_ok=True)
                continue
            shown = run_capture(["git", "show", f"{resolved}:{path}"], copy)
            (base / path).parent.mkdir(parents=True, exist_ok=True)
            (base / path).write_text(shown.stdout)
        argv = shlex.split(test_command)
        listed = run_capture(
            [*argv, "--collect-only", "--verbosity=-1", "-p", "no:cacheprovider"],
            base,
            timeout=timeout,
            memory_limit=tree_memory_limit(),
        ).stdout.splitlines()
        wanted = set(names)
        nodes = [n for n in listed if "::" in n and n.rsplit("::", 1)[1].split("[")[0] in wanted]
        if not nodes:
            return []
        ran = run_capture(
            [*argv, "--verbosity=-1", "-rA", "-p", "no:cacheprovider", *nodes],
            base,
            timeout=timeout,
            memory_limit=tree_memory_limit(),
        ).stdout.splitlines()
    passed = {line.split(" ", 1)[1].split(" ")[0] for line in ran if line.startswith("PASSED ")}
    return sorted(
        name
        for name in wanted
        if (mine := [n for n in nodes if n.rsplit("::", 1)[1].split("[")[0] == name])
        and all(n in passed for n in mine)
    )


def _reason(gate: str, verdict: Verdict, detail: str) -> Reason:
    if verdict == "blocked" or (gate == "mutation" and detail.startswith(_TOOL_FAILURE_PREFIXES)):
        return "unknown"
    return REASONS[gate]


COVERAGE_GAP_PREFIX: Final = "no test runs"
"""How `check_changed_line_coverage` begins a detail that names uncovered lines."""


def coverage_evidence(
    copy: Path, baseline: str, detail: str, basis: str = ""
) -> dict[str, Any] | None:
    """What `coverage_text` needs beside a failing coverage finding.

    The text of every file the detail names, as it stood in the audited
    tree, and the changed-statement set the gate judged, spelled relative
    to the tree; and the lines the detail names (`uncovered`), with the
    gate's `basis`, whole: the finding's own line is fitted to the ledger
    (`sealed_finding`), and a long one keeps only part of its list. None
    for a detail that names no uncovered line (a passing finding, or the
    gate's other wordings): nothing is sealed then. A file the detail names
    that cannot be read is left out, and `coverage_text` lists its lines as
    "not placed".

    Each file is sealed as a list of its lines, not one string:
    `write_attempt_sidecar` caps every string at `MAX_THINKING_CHARS`
    (4000), which cut a 141-line module at line 119 and left it unparsable.
    """
    if not detail.startswith(COVERAGE_GAP_PREFIX):
        return None
    named = coverage_text.uncovered_lines(detail)
    sources: dict[str, list[str]] = {}
    for rel in sorted({f for f, _ in named}):
        try:
            sources[rel] = (copy / rel).read_text().splitlines()
        except (OSError, UnicodeDecodeError):
            continue
    changed = changed_statements(copy, git_diff(copy, baseline))
    relative = sorted((str(Path(path).relative_to(copy)), line) for path, line in changed)
    return {
        "sources": sources,
        "changed": [[path, line] for path, line in relative],
        "uncovered": [[path, line] for path, line in named],
        "basis": basis,
    }


def _finding(gate: str, tier: int, verdict: Verdict, detail: str, basis: str | None) -> Finding:
    cites = (REUSES[gate],) if basis is None else (REUSES[gate], basis)
    return Finding(gate, tier, verdict, _reason(gate, verdict, detail), detail, cites)


def _from_check(check: GateCheck, tier: int, name: str | None = None) -> Finding:
    return _finding(
        name or check.name, tier, "pass" if check.passed else "fail", check.detail, check.basis
    )


def _rooted(outcome: MutationOutcome, copy: Path) -> MutationOutcome:
    """`outcome` with every survivor path relative to the audited tree's root."""

    def rel(path: str) -> str:
        return os.path.relpath(path, copy) if os.path.isabs(path) else path

    return dataclasses.replace(
        outcome,
        survivor_details=tuple(
            (name, status, rel(path), line, text, message)
            for name, status, path, line, text, message in outcome.survivor_details
        ),
    )


def _sources(copy: Path, outcome: MutationOutcome) -> dict[str, str]:
    """The text of every file a survivor sits in, read from the audited copy."""
    found: dict[str, str] = {}
    for _, _, path, _, _, _ in outcome.survivor_details:
        target = copy / path
        if path not in found and target.is_file():
            found[path] = target.read_text()
    return found


def behaviour_at(source: str, line: int) -> str:
    """The innermost def containing `line`: its name and its docstring's first line."""
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return "module top level"
    best: ast.FunctionDef | ast.AsyncFunctionDef | None = None
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef)
            and node.lineno <= line <= (node.end_lineno or node.lineno)
            and (best is None or node.lineno > best.lineno)
        ):
            best = node
    if best is None:
        return "module top level"
    doc = ast.get_docstring(best)
    first = doc.strip().splitlines()[0] if doc and doc.strip() else ""
    return f"`{best.name}`" + (f": {first}" if first else "")


def _survivors(outcome: MutationOutcome, sources: dict[str, str]) -> tuple[Survivor, ...]:
    found = []
    judged = [d for d in outcome.survivor_details if not d[5] and set_aside_kind(d) is None]
    for name, status, path, line, text, _ in shortlist_order(judged):
        source = sources.get(path, "")
        lines = source.splitlines()
        found.append(
            Survivor(
                path=path,
                line=line,
                name=name,
                status=status,
                mutation=text,
                source=lines[line - 1].strip() if 0 < line <= len(lines) else "",
                behaviour=behaviour_at(source, line),
            )
        )
    return tuple(found)


_FIND_SPECS: Final = (
    "import importlib.util, json, sys; "
    "print(json.dumps([n for n in sys.argv[1:] if importlib.util.find_spec(n) is not None]))"
)


def gate_interpreter_finds(names: Sequence[str]) -> set[str]:
    """The names among `names` that the `python` the gates run the tests with
    finds (`sandbox.gate_path`: the user's PATH first, saddle's own last).

    Asked from an empty directory, so no file beside the check shadows a
    distribution. A python that cannot be started or answers with anything
    but a JSON list finds nothing: a lookup that fails leaves the names
    unresolved, never resolved."""
    if not names:
        return set()
    with tempfile.TemporaryDirectory(prefix="saddle-find-spec-") as empty:
        run = run_capture(["python", "-c", _FIND_SPECS, *names], Path(empty))
    try:
        found = json.loads(run.stdout) if run.exit_code == 0 else []
    except ValueError:
        found = []
    return {name for name in found if name in names} if isinstance(found, list) else set()


_REQUIREMENT_NAME: Final = re.compile(r"\s*([A-Za-z0-9](?:[A-Za-z0-9._-]*[A-Za-z0-9])?)")


def build_requirement_names(pyproject: str | None) -> set[str]:
    """The top-level import names a build frontend installs into its isolated
    build environment before it runs the tree's `setup.py`, lower-cased with
    `-` and `.` read as `_`.

    Those are the names in pyproject.toml's `[build-system] requires`; with no
    pyproject.toml, or one without a `[build-system]` table, the frontend
    falls back to setuptools (PEP 518). A table whose `requires` is not a
    list, or a file that does not parse, provides nothing: a lookup that fails
    excuses no import."""
    if pyproject is None:
        return {"setuptools"}
    try:
        data = tomllib.loads(pyproject)
    except tomllib.TOMLDecodeError:
        return set()
    if "build-system" not in data:
        return {"setuptools"}
    system = data["build-system"]
    requires = system.get("requires") if isinstance(system, dict) else None
    if not isinstance(requires, list):
        return set()
    names = set()
    for requirement in requires:
        match = _REQUIREMENT_NAME.match(requirement) if isinstance(requirement, str) else None
        if match:
            names.add(re.sub(r"[-.]", "_", match.group(1).lower()))
    return names


def setup_py_build_requirements(repo: Path) -> set[str]:
    """`build_requirement_names` of `repo`'s pyproject.toml; a file that is
    absent is the setuptools fallback, one that cannot be read provides nothing."""
    try:
        text = (repo / "pyproject.toml").read_text()
    except FileNotFoundError:
        return build_requirement_names(None)
    except (OSError, UnicodeDecodeError):
        return set()
    return build_requirement_names(text)


def check_imports(
    path: str,
    source: str,
    roots: Sequence[Path],
    interpreter_finds: Callable[[Sequence[str]], set[str]] | None = None,
    build_provides: Set[str] = frozenset(),
) -> GateCheck:
    """Every absolute import's top-level name resolves: stdlib, a module or
    package under one of `roots`, or an installed distribution.

    Only top-level names are looked up, with `importlib.util.find_spec`,
    which imports nothing for a top-level name; relative imports are not
    checked and are counted in the detail. saddle's own interpreter is asked
    first; a name it cannot find is then put to `interpreter_finds` (the
    auditor passes `gate_interpreter_finds`), because the tree's tests run
    under the python on the user's PATH, whose environment holds the
    project's dependencies and saddle's does not.

    A name in `build_provides` (lower-cased) is not looked up at all: the
    auditor passes a root `setup.py` the names its build system installs
    (`setup_py_build_requirements`), because a build frontend runs that file
    in an isolated environment holding them, never in the test environment.
    """
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return GateCheck(name="imports", passed=True, detail="not checked: file does not parse")
    names: list[str] = []
    relative = 0
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names.extend(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            if node.level:
                relative += 1
            else:
                names.append(str(node.module).split(".")[0])
    provided = {name for name in names if name.lower() in build_provides}
    missing = sorted(
        {
            name
            for name in names
            if name not in provided
            and name not in sys.stdlib_module_names
            and not any((r / f"{name}.py").is_file() or (r / name).is_dir() for r in roots)
            and importlib.util.find_spec(name) is None
        }
    )
    if missing and interpreter_finds is not None:
        missing = sorted(set(missing) - interpreter_finds(missing))
    note = f"; {relative} relative import(s) not checked" if relative else ""
    if provided:
        note += f"; {len(provided)} build requirement(s) not looked up"
    if missing:
        return GateCheck(
            name="imports",
            passed=False,
            detail=f"{path}: unresolved import(s): {', '.join(missing)}{note}",
        )
    return GateCheck(
        name="imports", passed=True, detail=f"{len(set(names))} name(s) resolved{note}"
    )


class Auditor:
    """The tiered battery over `repo` against `baseline_rev`, with a verdict cache."""

    def __init__(self, repo: Path, baseline_rev: str = "HEAD", config: AuditorConfig | None = None):
        self.repo = Path(repo)
        self.baseline_rev = baseline_rev
        self.config = config or AuditorConfig()
        self.node = self.config.node or audit_node(self.config.test_command)
        self._memory: dict[str, Findings] = {}
        self._by_syntax: dict[str, str] = {}
        """`_key(tier, "syntax", syntax_key, baseline)` to the verdict key it was
        decided under: a format-only edit reuses it (`_reuse`)."""

    # -- cache ---------------------------------------------------------------

    def _key(self, tier: int, *parts: str) -> str:
        payload = json.dumps(
            [
                tier,
                *parts,
                self.config.test_command,
                self.node.model_dump_json(),
                gate_surface(),
                sandbox.environment_key(),
                *(
                    ["shortlist", self.config.mutant_shortlist]
                    if self.config.tier2 == "shortlist"
                    else []
                ),
                *(
                    [sorted(self.config.sanctioned_test_rewrites)]
                    if self.config.sanctioned_test_rewrites
                    else []
                ),
                *(
                    [TASK_REQUIREMENTS, _file_sha(self.config.task_requirements)]
                    if self.config.task_requirements is not None
                    else []
                ),
                *(["impact"] if self.config.impact is not None else []),
            ]
        )
        return hashlib.sha256(payload.encode()).hexdigest()

    def _cached(self, key: str) -> Findings | None:
        hit = self._memory.get(key)
        if hit is None and self.config.cache_dir is not None:
            try:
                data = json.loads((self.config.cache_dir / f"{key}.json").read_text())
                hit = Findings.from_dict(data)
            except (OSError, ValueError, KeyError, TypeError, AssertionError):
                hit = None
        if hit is None or hit.key != key:
            return None
        return dataclasses.replace(hit, cached=True)

    def _store(
        self, result: Findings, sidecars: Mapping[str, Mapping[str, Any]] | None = None
    ) -> Findings:
        self._memory[result.key] = result
        if self.config.cache_dir is not None:
            self.config.cache_dir.mkdir(parents=True, exist_ok=True)
            (self.config.cache_dir / f"{result.key}.json").write_text(
                json.dumps(result.to_dict(), sort_keys=True)
            )
        self._journal(result, sidecars or {})
        return result

    def _journal(self, result: Findings, sidecars: Mapping[str, Mapping[str, Any]]) -> None:
        """One span per finding. `sidecars` maps a gate to evidence sealed as
        that finding's sidecar: the tier-2 mutation finding's
        `MutationOutcome`, a failing tier-1 coverage finding's sources and
        changed set (`coverage_evidence`), so the packet can say which
        mutants survived and which lines no test runs, not only how many. A
        blocked tier has no outcome and seals nothing. Verdicts and details
        are unchanged: the sidecar is evidence beside the finding, not in it."""
        if self.config.journal is None:
            return
        for f in result.findings:
            span_id = uuid.uuid4().hex
            attempt_hash = ""
            evidence = sidecars.get(f.gate)
            if evidence is not None:
                attempt_hash = write_attempt_sidecar(self.config.journal, span_id, evidence)
            append_span(
                self.config.journal,
                build_span(
                    node_id=self.node.id,
                    argv=["saddle-audit", f"tier{f.tier}", f.gate, result.key],
                    duration_ms=0,
                    exit_code=_JOURNAL_EXIT[f.verdict],
                    detail=sealed_finding(f),
                    name=f"audit-tier{f.tier}:{f.gate}",
                    span_id=span_id,
                    attempt_hash=attempt_hash,
                ),
            )

    # -- tier 0 --------------------------------------------------------------

    def tier0(self, path: str, new_text: str) -> Findings:
        """Syntax, ruff and import resolution over one file's proposed text."""
        rel = PurePosixPath(path).as_posix()
        key = self._key(0, self.baseline_rev, rel, hashlib.sha256(new_text.encode()).hexdigest())
        hit = self._cached(key)
        if hit is not None:
            return hit
        syntax = check_syntax({rel: new_text})
        with tempfile.TemporaryDirectory(prefix="saddle-tier0-") as tmp:
            current = Path(tmp) / "current"
            before = Path(tmp) / "baseline"
            (current / rel).parent.mkdir(parents=True)
            (current / rel).write_text(new_text)
            lint, found = ruff_findings(current, [rel])
            overrides = format_overrides(self.repo, self.baseline_rev)
            fmt = run_capture(ruff_argv("format", "--check", *overrides, rel), current)
            shown = run_capture(["git", "show", f"{self.baseline_rev}:{rel}"], self.repo)
            old: list[RuffFinding] = []
            if shown.exit_code == 0:
                (before / rel).parent.mkdir(parents=True)
                (before / rel).write_text(shown.stdout)
                old = ruff_findings(before, [rel])[1]
        introduced, inherited = introduced_findings(found, old)
        ruff = check_ruff(
            [rel],
            introduced=tuple(introduced),
            inherited=inherited,
            lint_exit=lint.exit_code,
            format_exit=fmt.exit_code,
        )
        roots = [
            self.repo,
            (self.repo / rel).parent,
            *(self.repo / r for r in self.config.extra_import_roots),
        ]
        build = setup_py_build_requirements(self.repo) if rel == "setup.py" else set()
        imports = check_imports(rel, new_text, roots, gate_interpreter_finds, build)
        # Each finding names the file it checked, so the ledger's records
        # tell the files apart and the packet's Edit checks row keeps a
        # failure on one from reading under a pass on another; the feed
        # renders it for the model (`feed.render`).
        findings = tuple(
            dataclasses.replace(_from_check(c, 0), path=rel) for c in (syntax, ruff, imports)
        )
        return self._store(Findings(tier=0, key=key, findings=findings))

    # -- tiers 1 and 2 -------------------------------------------------------

    def _gate(self, tier: int, tree: Path | None) -> Findings:
        with staged_copy(tree or self.repo, self.baseline_rev) as (copy, staged, resolved):
            if staged == baseline_tree(copy, resolved):
                msg = f"nothing to audit: the tree equals baseline {resolved[:12]}"
                raise AuditError(msg)
            key = self._key(tier, staged, resolved)
            hit = self._cached(key)
            if hit is not None:
                return hit
            # A format-only edit of an audited tree: the interpreter runs the
            # same code, so the suite and mutation are not asked again.
            shape = syntax_key(copy)
            same1 = self._key(1, "syntax", shape, resolved)
            same = self._key(tier, "syntax", shape, resolved)
            reused = self._reuse(same, key)
            if reused is not None:
                return reused
            try:
                # Read at the resolved baseline, which `key` already names: the
                # limit and the worker count are functions of that commit, not
                # of the tree audited.
                limit = suite_limit(copy, resolved).seconds
                workers = suite_workers(copy, resolved).count
            except SuiteLimitError as exc:
                raise AuditError(str(exc)) from exc
            # One run of the battery serves both tiers: at tier 2 the tier-1
            # findings come from the same suite run, and are cached under
            # tier 1's key, unless that tree's tier 1 is already known. The
            # finish audit used to run the whole suite once per tier on one tree.
            key1 = self._key(1, staged, resolved)
            first = (self._cached(key1) or self._reuse(same1, key1)) if tier == 2 else None
            if first is not None and not first.passed:
                return self._store(_blocked_tier2(key, first))
            p1: Future[TaskRequirementsCheck] | None = None
            pool: ThreadPoolExecutor | None = None
            if first is None and self.config.task_requirements is not None:
                # Beside the tests, not after them: it adds to the wall only if
                # it outlasts them. The context carries the project environment.
                pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix="saddle-p1")
                p1 = pool.submit(
                    contextvars.copy_context().run,
                    check_tree,
                    copy,
                    resolved,
                    self.config.task_requirements,
                )
            memo = self.config.impact
            selection = _selection(memo, copy, resolved)
            try:
                gated = runner.run_node_gate(
                    self.node,
                    copy,
                    baseline=resolved,
                    tier2=tier == 2,
                    test_timeout=limit,
                    test_workers=workers,
                    test_selection=selection,
                    # A whole-suite run under a memo (re)draws the map.
                    on_suite=(
                        functools.partial(_record_impact, memo, copy)
                        if memo is not None and selection is None
                        else None
                    ),
                )
            finally:
                if pool is not None:
                    pool.shutdown(wait=True)
            if tier == 2:
                if first is None:
                    first = self._tiered(1, key1, gated, copy, resolved, limit, p1)
                self._by_syntax[same1] = first.key
                if not first.passed:
                    return self._store(_blocked_tier2(key, first))
                second = self._tiered(2, key, gated, copy, resolved, limit, None)
                self._by_syntax[same] = second.key
                return second
            done = self._tiered(1, key, gated, copy, resolved, limit, p1)
            self._by_syntax[same] = done.key
            return done

    def _reuse(self, same: str, key: str) -> Findings | None:
        """The findings decided for a tree with the same syntax key, stored and
        returned under `key`, when `_reusable`; else None."""
        earlier = self._memory.get(self._by_syntax.get(same, ""))
        if earlier is None or not _reusable(earlier):
            return None
        return dataclasses.replace(self._store(dataclasses.replace(earlier, key=key)), cached=True)

    def _tiered(
        self,
        tier: int,
        key: str,
        gated: Tier1Result,
        copy: Path,
        resolved: str,
        limit: float,
        p1: Future[TaskRequirementsCheck] | None,
    ) -> Findings:
        """`tier`'s findings from one run of the battery (`_gate`), stored under `key`."""
        # Every entry becomes a Finding verdict (`_finding` below), and the
        # shortlist paths write not-proven, so the table holds Verdicts.
        statuses: dict[str, tuple[Verdict, str, str | None]]
        if self.config.node is None:
            checks, _ = audit_checks(gated.checks, gated.mutation, copy)
            statuses = {c.name: (c.status, c.detail, c.basis) for c in checks}
        else:
            statuses = {
                c.name: ("pass" if c.passed else "fail", c.detail, c.basis) for c in gated.checks
            }
        if self.config.tier2 == "shortlist" and statuses.get("coverage", ("",))[0] == "fail":
            # Under the shortlist, coverage is a locator. An uncovered changed line is
            # "not proven", never a refusal; the detail keeps its lines.
            statuses["coverage"] = ("not-proven", *statuses["coverage"][1:])
        survivors: tuple[Survivor, ...] = ()
        scored = gated.mutation.mutant_detail if gated.mutation is not None else ()
        cites: dict[str, str] = {}
        shortlist = self.config.tier2 == "shortlist"
        if shortlist and tier == 2 and gated.mutation is not None:
            outcome = _rooted(gated.mutation, copy)
            sources = _sources(copy, outcome)
            shortlisted = check_mutation_shortlist(
                outcome,
                self.node.deterministic_gate.mutation_sample.kill_threshold,
                shortlist=self.config.mutant_shortlist,
                sources=sources,
            )
            statuses["mutation"] = (
                "pass" if shortlisted.passed else "not-proven",
                shortlisted.detail,
                shortlisted.basis,
            )
            cites["mutation"] = "saddle.gates.check_mutation_shortlist"
            if not shortlisted.passed:
                survivors = _survivors(outcome, sources)
        spent = gated.mutation
        if (
            tier == 2
            and spent is not None
            and spent.budget_spent
            and spent.total == 0
            and statuses.get("mutation", ("",))[0] == "fail"
        ):
            # No evidence either way, and nothing the change could do about it.
            detail = MUTATION_UNMEASURED.format(generated=spent.generated)
            statuses["mutation"] = ("not-proven", detail, statuses["mutation"][2])
        sidecars: dict[str, Mapping[str, Any]] = {}
        if gated.mutation is not None:
            # The shortlist records `mutant_detail` as (name, status, show)
            # tuples; the sidecar seals the record shape Findings.to_dict
            # and feed.AuditResult.to_dict already use, which is what
            # mutant_text.describe_mutation reads.
            sidecars["mutation"] = {
                **dataclasses.asdict(gated.mutation),
                "mutant_detail": [
                    {"name": n, "status": s, "show": t} for n, s, t in gated.mutation.mutant_detail
                ],
            }
        rewrote = statuses.get("assertion-preservation")
        sanctioned = set(self.config.sanctioned_test_rewrites)
        if (
            tier == 1
            and rewrote is not None
            and rewrote[0] == "fail"
            and (named := rewritten(rewrote[1]))
        ):
            # A rewrite that is sanctioned, or put to a person, must be red
            # on the baseline: one that passes there asserts nothing new.
            green = green_on_baseline(
                copy, resolved, sorted(named), self.config.test_command, timeout=limit
            )
            if green:
                statuses["assertion-preservation"] = (
                    "fail",
                    f"{rewrote[1]}{GREEN_ON_BASELINE}{', '.join(green)}",
                    rewrote[2],
                )
            elif not named <= sanctioned:
                statuses["assertion-preservation"] = (
                    "question",
                    f"{rewrote[1]}{REWRITE_QUESTION}",
                    rewrote[2],
                )
        if tier == 1:
            status, detail, basis = statuses["coverage"]
            # A not-proven coverage finding names the same lines;
            # its sidecar is what `coverage_text` renders.
            sealed = (
                coverage_evidence(copy, resolved, detail, basis or "")
                if status in ("fail", "not-proven")
                else None
            )
            # The baseline definitions coverage did not judge,
            # sealed whatever the verdict (a pass is where they hide).
            spared = spared_definitions(basis or "")
            if spared:
                sealed = {**(sealed or {}), "spared": spared}
            if sealed is not None:
                sidecars["coverage"] = sealed
        if p1 is not None:
            check = p1.result()
            statuses[TASK_REQUIREMENTS] = (check.verdict, check.detail, check.basis)
            sidecars[TASK_REQUIREMENTS] = p1_tally(check)
        wanted = TIER1 if tier == 1 else TIER2
        if TASK_REQUIREMENTS in statuses:
            wanted = (*wanted, TASK_REQUIREMENTS)
        findings = []
        for gate in wanted:
            status, detail, basis = statuses["tests" if gate == "full-suite" else gate]
            found = _finding(gate, tier, status, detail, basis)
            if gate in cites:
                found = dataclasses.replace(found, cites=(cites[gate], *found.cites[1:]))
            findings.append(sanction(found, self.config.sanctioned_test_rewrites))
        return self._store(
            Findings(
                tier=tier,
                key=key,
                findings=tuple(findings),
                survivors=survivors,
                mutant_detail=scored if tier == 2 else (),
            ),
            sidecars,
        )

    def draw_map(self, tree: Path | None = None) -> str:
        """Draw the run's test-impact map now (`AuditorConfig.impact`), before
        any audit needs it, and say how: read from the memo's cache when this
        tree, test command, worker count and environment were mapped before,
        else from one whole-suite run over `tree` with per-test contexts,
        which is then cached. Nothing without a memo, or once it holds a map.

        `saddle auto` calls it at run start, beside the model's first reading,
        so no audit of the run pays a whole-suite run to learn which tests a
        change reaches; a watched run's finish audit paid seven minutes."""
        memo = self.config.impact
        if memo is None or memo.tests is not None:
            return "no map wanted" if memo is None else "map already drawn"
        with staged_copy(tree or self.repo, self.baseline_rev) as (copy, staged, resolved):
            try:
                limit = suite_limit(copy, resolved).seconds
                workers = suite_workers(copy, resolved).count
            except SuiteLimitError as exc:
                raise AuditError(str(exc)) from exc
            command = self.node.deterministic_gate.test_command
            payload = [impact.MAP_VERSION, staged, command, workers, sandbox.environment_key()]
            key = hashlib.sha256(json.dumps(payload).encode()).hexdigest()
            cached = memo.cache / f"{key}.json" if memo.cache is not None else None
            if cached is not None and cached.is_file():
                memo.tests = impact.loads(cached.read_text())
                if memo.tests is not None:
                    return f"map read from {cached.name}"
            data_file = str(copy / ".coverage.map")
            drop_test_caches(copy)
            mode = suite_run(copy, command, workers)
            ran = run_suite_capture(mode, command, copy, data_file, timeout=limit, contexts=True)
            _record_impact(memo, copy, data_file, ran)
            if memo.tests is None:
                why = "timed out" if ran.timed_out else f"exit {ran.exit_code}"
                return f"no map: the suite recorded no test context ({why})"
            if cached is not None:
                cached.parent.mkdir(parents=True, exist_ok=True)
                partial = cached.with_suffix(".partial")
                partial.write_text(impact.dumps(memo.tests))
                os.replace(partial, cached)
            return f"map drawn over {len(memo.tests)} files"

    def prime(self, tree: Path | None = None) -> None:
        """Run tiers 1 and 2 on `tree` as one run of the battery and cache both.

        `_gate` at tier 2 derives tier 1 from the same suite run, so a caller
        about to ask for both (the finish audit) asks for tier 2 first and
        reads tier 1 cached: one run of the tests, where it was two."""
        self.tier2(tree)

    def tier1(self, tree: Path | None = None) -> Findings:
        """The checkpoint tier over `tree` (default: the repo's working tree)."""
        return self._gate(1, tree)

    def tier2(self, tree: Path | None = None) -> Findings:
        """The asynchronous tier; `blocked` when tier 1 on the same tree fails."""
        return self._gate(2, tree)

    def audit(self, tree: Path | None = None) -> tuple[Findings, ...]:
        """Tiers 0-2 over the diff: tier 0 on every changed Python file's text."""
        root = tree or self.repo
        with staged_copy(root, self.baseline_rev) as (copy, _staged, resolved):
            names = run_capture(
                ["git", "diff", "--cached", "--name-only", "--diff-filter=AMR", resolved], copy
            ).stdout.split()
            texts = {n: (copy / n).read_text() for n in names if n.endswith(".py")}
        zero = [self.tier0(n, t) for n, t in sorted(texts.items())]
        # Tier 2 first: its one run of the battery also yields tier 1 (`_gate`).
        two = self.tier2(root)
        return (*zero, self.tier1(root), two)
