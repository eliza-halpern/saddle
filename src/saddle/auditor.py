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
import subprocess
import sys
import tempfile
import tomllib
import uuid
from collections.abc import Callable, Iterable, Mapping, Sequence, Set
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import dataclass, field
from fnmatch import fnmatch
from pathlib import Path, PurePosixPath
from typing import Any, Final, Literal

from saddle import coverage_text, flips, impact, runner, sandbox
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
    DEPENDENCY_DIRS,
    RUFF_TIMEOUT_S,
    SKIP_REPORT_NAME,
    CapturedRun,
    MutationOutcome,
    PromptBenchmark,
    SkippedTests,
    SuiteLimitError,
    changed_statements,
    drop_test_caches,
    format_overrides,
    gate_checks,
    gate_stage_languages,
    git_changed_files,
    git_diff,
    materialize_baseline,
    prompt_benchmark,
    read_skip_report,
    ruff_argv,
    ruff_configured,
    ruff_findings,
    run_capture,
    run_prompt_benchmark,
    run_static_check,
    run_suite_capture,
    sandbox_expose,
    skip_reportable,
    static_check,
    suite_limit,
    suite_run,
    suite_workers,
    tree_memory_limit,
)
from saddle.gates import (
    DEFAULT_MUTANT_SHORTLIST,
    DOCUMENTED_RAISES,
    DOCUMENTED_RAISES_HELD,
    NO_TESTS_COLLECTED,
    RED_PHASE_TESTS_ONLY,
    TEST_ONLY_UNPROVEN,
    TOOL_UNAVAILABLE,
    GateCheck,
    ProjectGate,
    RuffFinding,
    TaskRequirementsCheck,
    Tier1Result,
    check_documented_raises,
    check_js_coverage,
    check_js_red_phase,
    check_js_tests,
    check_mutation_shortlist,
    check_project_gate,
    check_ruff,
    check_static,
    check_syntax,
    gate_stage_name,
    introduced_findings,
    is_test_code,
    set_aside_kind,
    shortlist_order,
    spared_definitions,
)
from saddle.impact import ImpactMemo, is_test_file
from saddle.journal import (
    JOURNAL_QUESTION_EXIT,
    MAX_SPAN_DETAIL_CHARS,
    SEALED_CUT,
    append_span,
    build_span,
    scrub_thinking,
    write_attempt_sidecar,
)
from saddle.jsevidence import (
    COVERAGE_SCOPE,
    JS_SUFFIX,
    NOT_LINE_MEASURED,
    JsCoverage,
    JsTestResult,
    changed_js_lines,
    is_js_test_file,
    js_test_files,
    measure_chrome_coverage,
    measure_coverage,
    read_coverage_scope,
    red_phase,
    run_node_tests,
    stryker_entry,
)
from saddle.languages import FINDING_LANGUAGES, JAVASCRIPT, OTHER, classify, stage_visible, visible
from saddle.prompt_changes import Measured, judge, measured, prompt_changes, unconfigured_detail
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

STATIC_CHECK: Final = "static-check"
"""The project's own static check (`evidence.static_check`, a type checker for
saddle itself): a tier-1 gate emitted after `TIER1`, and only when the
project's committed `[tool.saddle]` names one; without it, no finding."""

PROJECT_GATE: Final = "project-gate"
"""The project's own gate stages (`evidence.gate_checks`, plus its static check),
each run on the audited tree and on the baseline (`gates.check_project_gate`): a
tier-1 gate emitted after `STATIC_CHECK`, and only when the project's committed
`[tool.saddle]` names a stage. Its first line is the packet's `Gate:` line."""

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
    STATIC_CHECK: "saddle.gates.check_static",
    PROJECT_GATE: "saddle.gates.check_project_gate",
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
    STATIC_CHECK: "code-wrong",
    PROJECT_GATE: "code-wrong",
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
        "code-wrong-by-type": check.by_type,
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


NoTests = Literal["refuse", "not-proven"]

NO_TESTS_DETAIL: Final = (
    "not proven: the project has no tests (none at the baseline, none collected now), "
    "so nothing here was run; a person reads the change"
)
"""Every check that needs a test, for a person's audit of a project without tests."""


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
    dependencies: Path | None = None
    """Where the project's installed dependency directories (`DEPENDENCY_DIRS`) and
    its node tools live; None: the audited repository. An autonomous run audits
    its worktree, which holds tracked files only and has `node_modules` only as
    a mount point, so it passes its checkout: a watched run's finish audit found
    no c8, no typescript and no npx tools, and refused on 17 tests that needed them."""
    mutant_shortlist: int = DEFAULT_MUTANT_SHORTLIST
    """How many open survivors a mutation finding's detail names (`--mutant-shortlist`)."""
    task_requirements: Path | None = None
    """`--task-requirements`: a sealed P1 file. Tier 1 then runs the
    `task-requirements` gate beside the tests, and the file's bytes join the
    cache key, so a changed file never reuses a verdict."""
    task_text: str | None = None
    """The task the run was given, when the caller has it: a new public function
    the task names as a whole identifier was asked for, so
    `gates.check_test_only_additions` does not judge it. Part of the cache key."""
    no_tests: NoTests = "refuse"
    """How a project with no tests is judged. "refuse" (the default, and a Task
    run's): pytest collecting nothing fails `tests`, since an agent can write the
    tests. "not-proven" (`saddle audit --tiered`, a person's commits): when the
    baseline has no tests either, every check that needs one is not proven
    (`NO_TESTS_DETAIL`), never a refusal no edit could clear (#129)."""
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

MUTATION_TOOL_FAILED_PREFIX: Final = "mutation tool failed"
"""How `gates.check_mutation` begins the detail of an engine that failed."""

MUTATION_TOOL_CRASHED: Final = (
    "{detail}. This is the mutation tool failing, not a finding against the change, "
    "and no edit can clear it: call finish again to end the run. The tool's own output "
    "is sealed with this finding for a person to read."
)
"""The mutation finding when the engine itself failed (not a red suite): not
proven, never a refusal, and never a pass. A dogfood run's correct tree (9 of 9
on its sealed oracle) was refused on `mutmut run exited 1: KeyError: 10062`, and
the worker spent 40 minutes reading mutmut's source with nothing it could change."""


MUTATION_DATA_ONLY: Final = (
    "not proven: the source this change touches is module-level constants only ({files}); "
    "mutation mutates code, not data, so it has nothing here to test. This is no finding "
    "against the change and no edit can clear it: a person reviews the data."
)
"""The mutation finding when mutmut generated nothing and every changed source
line is a module-level literal constant (`data_only_change`): not proven, never
a refusal. A correct change that added one entry to a tuple of module names was
refused "no mutants on changed lines", and the model rewrote it into a function
body to give the gate something to mutate. Module-level code that is not a
literal (a call, a loop, a comprehension) still fails: that is where the fail
exists for, a module mutmut cannot reach passing an infinite loop."""


def data_only_change(copy: Path, baseline: str) -> list[str]:
    """The changed source files, when every changed line in them sits in a
    module-level assignment of a literal value; else []. Test files are not
    source, nor is any other test code (`is_test_code`). No changed source line
    at all is [] too: there is no data to name."""
    changed = changed_statements(copy, git_diff(copy, baseline))
    by_file: dict[str, set[int]] = {}
    root = f"{copy}{os.sep}"
    for spelled, line in changed:
        rel = spelled.removeprefix(root)
        if not is_test_code(rel):
            by_file.setdefault(rel, set()).add(line)
    for rel, lines in by_file.items():
        # `changed_statements` lists only files that exist and parse.
        module = ast.parse((copy / rel).read_text())
        literal: set[int] = set()
        for node in module.body:
            if isinstance(node, ast.Assign | ast.AnnAssign) and node.value is not None:
                try:
                    ast.literal_eval(node.value)
                except ValueError:
                    continue
                literal.update(range(node.lineno, (node.end_lineno or node.lineno) + 1))
        if not lines <= literal:
            return []
    return sorted(by_file)


MEASURABLE_SUFFIXES: Final[Mapping[str, str]] = {".py": "Python", JS_SUFFIX: "JavaScript"}
"""The file suffixes the mutation and changed-line coverage checks measure, each
with its language's name. A changed file whose suffix is not a key is listed
under `NOT_MEASURABLE_GATE` instead of being refused or ignored. The one place
to widen when a language gains measurement: a suffix added here leaves the
unmeasurable set, and the findings' wording follows the names. A browser-only
change was refused "no mutants on changed lines" because the checks mutate
Python only, and the model added a dead Python function for them to mutate.
`.js` is measured by StrykerJS (`jsevidence.mutation_sample`), so it counts only
where StrykerJS is installed (`measurable_here`); without it a changed `.js` file
is listed as not measurable, as before."""

NOT_MEASURABLE_GATE: Final = "not-measurable"
"""The tier-2 finding that lists the changed files `MEASURABLE_SUFFIXES` does
not cover. Emitted only when there is one; always `not-proven`."""

JS_TESTS_GATE: Final = "js-tests"
"""The tier-1 finding of the node tests' own run (`gates.check_js_tests`): every
`.js` test of the audited tree, one result each. Emitted only when the change
touches a `.js` file; `not-proven` when node, or a test file, cannot be had."""

NO_NODE_TEST: Final = "no node test file in the tree, so the changed .js files have no test run"
"""The `JS_TESTS_GATE` detail for a tree with no `.js` test."""

JS_COVERAGE_GATE: Final = "js-coverage"
"""The tier-1 finding of changed-line coverage for `.js` (`gates.check_js_coverage`),
the counterpart of the Python `coverage` check: emitted when a non-test `.js`
code line changed. `fail` when a changed line of a line-measured file runs in no
node test; `not-proven` for a changed file coverage is not measured on
(`jsevidence.COVERAGE_SCOPE`), or when c8 or the report is missing."""

JS_RED_PHASE_GATE: Final = "js-red-phase"
"""The tier-2 finding of the new or changed `.js` tests run on the baseline
(`gates.check_js_red_phase`). Emitted only when such a test exists."""

SKIPPED_GATE: Final = "skipped-tests"
"""The tier-2 finding that lists the tests the audit's suite run skipped or
expected to fail, or says it could not tell. Emitted only when there is one."""

LISTED: Final = 6
"""How many files or tests a not-proven finding names; the rest are counted
(`_listed`), and the whole list is sealed beside the finding."""

REASON_WORDS: Final = 16
"""How many words of a skip reason a finding quotes."""

MUTATION_NOT_MEASURABLE: Final = (
    "not proven: no Python source line changed, so mutation had nothing to mutate; "
    f"the changed files it cannot measure are listed under {NOT_MEASURABLE_GATE}. "
    "No edit can clear this, and code added only to give mutation something to mutate "
    "is a defect."
)
"""The mutation finding when mutmut generated nothing, no changed line is Python
source and some changed file is outside `MEASURABLE_SUFFIXES`: not proven,
never a refusal."""

RED_PHASE_NOT_MEASURABLE: Final = (
    "not proven: tests unchanged and no Python source line changed, so red-phase has no "
    f"behaviour to measure; see {NOT_MEASURABLE_GATE}"
)
"""The red-phase finding in the same case (`RED_PHASE_NO_MUTANTS`)."""

MUTATION_TESTS_ONLY: Final = (
    "not proven: every changed Python line is test code, so mutation had no source "
    "line to mutate; the tests' strength is not measured here. No edit can clear this, "
    "and code added only to give mutation something to mutate is a defect."
)
"""The mutation finding when mutmut generated nothing and every changed Python
line is test code (`is_test_code`): not proven, never a refusal (#181)."""

RED_PHASE_NO_MUTANTS: Final = "tests unchanged and no mutants decided"

RED_PHASE_TESTS_UNCHANGED: Final = "tests unchanged and"
"""How `gates.check_red_phase` begins a refusal under the refactor rule: no Python
test changed, so changed-line coverage and a mutation floor carry the proof."""

RED_PHASE_NO_PYTHON: Final = (
    "no Python source line changed: Python's red phase has nothing to judge. Changed "
    "JavaScript tests are judged by js-red-phase, and every changed line's mutants by mutation"
)
"""The red-phase finding of a change with no Python source line in it, where the
refactor rule would otherwise refuse on another language's mutation score."""
"""How `gates._check_behaviour_preserved` begins its detail when mutation decided
nothing; the one red-phase refusal `MUTATION_NOT_MEASURABLE` also lifts."""

RED_PHASE_ONLY_TESTS: Final = f"not applicable: {RED_PHASE_TESTS_ONLY}"
"""The red-phase finding of a change to test code alone (`gates.RED_PHASE_TESTS_ONLY`):
a regression test for code that already works can never fail on it, so this is
never a refusal; the tests check and mutation judge such a change (#183)."""


def measurable_here(copy: Path, tools: Path | None) -> Mapping[str, str]:
    """`MEASURABLE_SUFFIXES` less `.js` where StrykerJS cannot be had in `copy`
    or in `tools`: a language is measured only where its mutation tool is."""
    if stryker_entry(copy, tools) is not None:
        return MEASURABLE_SUFFIXES
    return {k: v for k, v in MEASURABLE_SUFFIXES.items() if k != JS_SUFFIX}


def unmeasurable_files(
    files: Sequence[str], measurable: Mapping[str, str] | None = None
) -> list[str]:
    """The paths in `files` whose suffix `measurable` (default `MEASURABLE_SUFFIXES`)
    does not name, sorted."""
    named = MEASURABLE_SUFFIXES if measurable is None else measurable
    return sorted(f for f in set(files) if PurePosixPath(f).suffix.lower() not in named)


def _measured_languages(measurable: Mapping[str, str] | None = None) -> str:
    """The names `measurable` gives its languages, "Python" or "JavaScript and Python"."""
    named = MEASURABLE_SUFFIXES if measurable is None else measurable
    return " and ".join(sorted(set(named.values())))


def _listed(items: Sequence[str]) -> str:
    """The first `LISTED` of `items`, then how many more there are."""
    shown = ", ".join(items[:LISTED])
    return f"{shown}, ... and {len(items) - LISTED} more" if len(items) > LISTED else shown


def not_measurable_detail(files: Sequence[str], measurable: Mapping[str, str] | None = None) -> str:
    """The `NOT_MEASURABLE_GATE` finding's words: the files, what cannot see
    them, and that adding code only to give a check something to measure is a defect."""
    noun = "file" if len(files) == 1 else "files"
    named = MEASURABLE_SUFFIXES if measurable is None else measurable
    # `.js` is measured only in the files the coverage scope lists as measured;
    # saying "JavaScript" alone would read as every `.js` file.
    scoped = (
        f", and JavaScript changed-line coverage only in the files {COVERAGE_SCOPE} lists as "
        f"measured (see {JS_COVERAGE_GATE})"
        if JS_SUFFIX in named
        else ""
    )
    return (
        f"not mutation-measurable: {_listed(files)} ({len(files)} {noun}; the mutation and "
        f"changed-line coverage checks measure {_measured_languages(measurable)} only{scoped}). "
        "Recorded as not proven and not refused: no edit can make these files measurable, and code "
        "added only to give those checks something to measure is a defect. A person "
        "reads the files."
    )


def source_lines_changed(copy: Path, baseline: str) -> bool:
    """Whether any changed line is in a Python file that is not test code
    (`is_test_code`, a helper under `tests/` included): the lines mutation could
    mutate."""
    root = f"{copy}{os.sep}"
    return any(
        not is_test_code(spelled.removeprefix(root))
        for spelled, _ in changed_statements(copy, git_diff(copy, baseline))
    )


def _quoted(pair: tuple[str, str], label: str = "") -> str:
    """One skipped test as a finding names it: its id, its reason cut to
    `REASON_WORDS` words, and `label` when it is not a plain skip."""
    words = pair[1].split()
    reason = " ".join(words[:REASON_WORDS]) + (" ..." if len(words) > REASON_WORDS else "")
    return f"{pair[0]}{label}" + (f" ({reason})" if reason else "")


def skipped_detail(skips: SkippedTests, scope: str) -> str | None:
    """The `SKIPPED_GATE` finding's words, or None when the run skipped nothing.

    `scope` says which run this was ("the suite run", or the impact-scoped one
    that ran only the test files a change reaches). A run whose report could
    not be read is said so, never counted as zero skips."""
    if not skips.known:
        return (
            f"skipped tests: could not be determined for {scope} ({skips.why}); read this "
            "as unproven, not as none skipped. It refuses nothing."
        )
    if not skips.skipped and not skips.xfailed:
        return None
    named = [_quoted(p) for p in skips.skipped] + [_quoted(p, " [xfail]") for p in skips.xfailed]
    counts = f"{len(skips.skipped)} skipped"
    if skips.xfailed:
        counts += f", {len(skips.xfailed)} expected to fail (xfail)"
    return (
        f"{counts} in {scope}: {_listed(named)}. Not proven: whatever those tests check "
        "was not checked here. It refuses nothing; the whole list is sealed with this finding."
    )


PROMPT_EFFECT_GATE: Final = "prompt-effect"
"""The tier-2 finding for changed prompt text (`prompt_changes.prompt_changes`):
what the project's prompt benchmark scored, or that none measures it. Emitted
only when prompt text changed. Not proven unless the project sets a floor or a
margin at its baseline (`evidence.prompt_benchmark`), then a pass or a fail."""


def _source_at(copy: Path, rev: str, rel: str) -> str | None:
    """`rel` as commit `rev` has it, None when `git show` finds nothing there.
    A lookup that fails reads as an added file, which only ever adds names."""
    shown = run_capture(["git", "show", f"{rev}:{rel}"], copy)
    return shown.stdout if shown.exit_code == 0 else None


def _benchmark_run(argv: Sequence[str], tree: Path, limit: float) -> Measured:
    ran = run_prompt_benchmark(argv, tree, timeout=limit)
    return measured(
        ran.exit_code,
        ran.stdout,
        timed_out=ran.timed_out,
        unavailable=ran.exit_code == TOOL_UNAVAILABLE,
    )


def _measure_json(run: Measured) -> dict[str, Any]:
    score = run.score
    return {
        "score": None if score is None else score.score,
        "n": None if score is None else score.n,
        "label": "" if score is None else score.label,
        "failure": run.failure,
    }


def prompt_effect(
    copy: Path,
    resolved: str,
    limit: float,
    bench: PromptBenchmark | None,
    seen: dict[tuple[str, tuple[str, ...]], Measured],
) -> tuple[Finding, dict[str, Any]] | None:
    """The `PROMPT_EFFECT_GATE` finding and its sealed record, or None when no
    prompt text changed between `resolved` and the audited `copy`.

    Prompt text is what `prompt_changes` names, over the changed Python files
    that are not tests. With no `bench`, the finding says so and is not proven.
    With one, the command runs on the head tree and on a copy of the baseline's
    (`seen` keeps a baseline score, by commit and command, for the next audit
    of the run), and `prompt_changes.judge` reads both against the bars read at
    the baseline."""
    files = [
        f for f in git_changed_files(copy, resolved) if f.endswith(".py") and not _test_side(f)
    ]
    heads = {
        f: (copy / f).read_text(errors="replace") if (copy / f).is_file() else None for f in files
    }
    changes = prompt_changes({f: _source_at(copy, resolved, f) for f in files}, heads)
    if not changes:
        return None
    parts = []
    if changes.names:
        parts.append(f"prompt text changed: {_listed(changes.names)}")
    if changes.unreadable:
        parts.append(
            f"could not tell whether prompt text changed in {_listed(changes.unreadable)} "
            "(not readable as Python)"
        )
    lead = "; ".join(parts)
    record: dict[str, Any] = {"names": list(changes.names), "unreadable": list(changes.unreadable)}
    verdict: Verdict = "not-proven"
    if bench is None or not changes.names:
        detail = (
            unconfigured_detail(lead)
            if bench is None
            else f"{lead}. Not proven: a person reads the files."
        )
    else:
        head = _benchmark_run(bench.argv, copy, limit)
        base = seen.get((resolved, bench.argv))
        if base is None:
            with tempfile.TemporaryDirectory(prefix="saddle-prompt-base-") as scratch:
                before = Path(scratch) / "tree"
                shutil.copytree(copy, before, symlinks=True)
                run_capture(["git", "reset", "--hard", "-q", resolved], before)
                base = _benchmark_run(bench.argv, before, limit)
            if base.score is not None:
                seen[(resolved, bench.argv)] = base
        verdict, detail = judge(lead, head, base, floor=bench.floor, margin=bench.margin)
        record |= {
            "command": list(bench.argv),
            "floor": bench.floor,
            "margin": bench.margin,
            "head": _measure_json(head),
            "base": _measure_json(base),
        }
    found = Finding(
        PROMPT_EFFECT_GATE,
        2,
        verdict,
        "code-wrong" if verdict == "fail" else "evidence-thin",
        detail,
        (
            "saddle.prompt_changes.judge"
            if bench is not None
            else "saddle.prompt_changes.prompt_changes",
        ),
    )
    return found, record


def _not_proven(gate: str, detail: str, cite: str) -> Finding:
    """A tier-2 `not-proven` finding of a gate that is not one of the battery's
    thirteen (see `REUSES`): emitted only when it has something to say."""
    return Finding(gate, 2, "not-proven", "evidence-thin", detail, (cite,))


def js_coverage_finding(
    copy: Path,
    resolved: str,
    limit: float,
    tools: Path | None,
    tests: Sequence[str],
) -> tuple[Finding, dict[str, Any]] | None:
    """Changed-line coverage of the `.js` files, or None when no non-test `.js`
    code line changed.

    The changed code lines are `jsevidence.changed_js_lines` (blank and comment
    lines are not code, and V8 reports them as run or unrun with the function
    around them). A file listed `measured` in the coverage scope is run under
    c8 and judged (`gates.check_js_coverage`); a file listed `not_measured`, or
    in neither list, is "not line-measured" and makes the finding not-proven:
    never a pass, never skipped. c8 or its report missing is not-proven naming
    it. A failing measured line outranks not-proven.
    """
    root = f"{copy}{os.sep}"
    changed: dict[str, list[int]] = {}
    for spelled, line in sorted(changed_js_lines(copy, git_diff(copy, resolved))):
        changed.setdefault(spelled.removeprefix(root), []).append(line)
    if not changed:
        return None
    scope = read_coverage_scope(copy)
    measured = sorted(f for f in changed if f in scope.measured)
    chrome = sorted(f for f in changed if f in scope.chrome_measured and f not in scope.measured)
    unmeasured = sorted(f for f in changed if f not in scope.measured and f not in chrome)
    why = {f: scope.not_measured.get(f, "in no coverage list") for f in unmeasured}
    cite = "saddle.gates.check_js_coverage"
    problem = scope.problem
    check = None
    sidecar: dict[str, Any] = {"changed": changed, "not_line_measured": unmeasured}
    hits: dict[str, Mapping[int, int]] = {}
    judged = list(measured)
    if measured:
        got = (
            measure_coverage(copy, measured, tests, tools=tools, timeout=limit)
            if tests
            else JsCoverage(problem=NO_NODE_TEST)
        )
        absent = [f for f in measured if f not in got.lines]
        if got.problem:
            problem = f"{problem}; {got.problem}" if problem else got.problem
        elif absent:
            problem = f"c8 reported no lines for {', '.join(absent)}"
        else:
            hits.update(got.lines)
        if got.problem or absent:
            judged = []
    if chrome:
        # A page script's lines come from the Chrome-driven tests the scope names,
        # run only now that a changed line lies in one of them (they are the slow part).
        seen = (
            measure_chrome_coverage(
                copy,
                scope.chrome_tests,
                tools=tools,
                timeout=limit,
                workers=suite_workers(copy, resolved).count,
            )
            if scope.chrome_tests
            else JsCoverage(problem="the coverage scope names no Chrome test")
        )
        gone = [f for f in chrome if f not in seen.lines]
        if seen.problem or gone:
            reason = seen.problem or f"c8 reported no lines for {', '.join(gone)}"
            unmeasured = sorted([*unmeasured, *chrome])
            why.update(dict.fromkeys(chrome, reason))
            sidecar["not_line_measured"] = unmeasured
        else:
            hits.update(seen.lines)
            judged += chrome
    named = "; ".join(f"{f} ({why[f]})" for f in unmeasured)
    if judged:
        check = check_js_coverage({f: changed[f] for f in judged}, hits)
        sidecar["hits"] = {f: sorted(hits[f].items()) for f in judged}
    if check is not None and not check.passed:
        detail = check.detail
        page_gaps = [f for f in chrome if f in judged and 0 in (hits[f].get(n) for n in changed[f])]
        if page_gaps and scope.chrome_helper:
            detail += (
                f" (page code runs in a Chrome-driven test: see {scope.chrome_helper}; a test"
                f" counts only once it is listed under `chrome_tests` in {COVERAGE_SCOPE})"
            )
        if unmeasured:
            detail += f"; also {NOT_LINE_MEASURED}: {named}"
        return Finding(JS_COVERAGE_GATE, 2, "fail", REASONS["coverage"], detail, (cite,)), sidecar
    parts = []
    if problem:
        parts.append(f"coverage could not be measured: {problem}")
    if unmeasured:
        parts.append(f"{NOT_LINE_MEASURED}: {named}")
    if not parts:
        assert check is not None
        return Finding(
            JS_COVERAGE_GATE, 2, "pass", REASONS["coverage"], check.detail, (cite,)
        ), sidecar
    detail = "not proven: " + "; ".join(parts)
    if check is not None:
        detail += f" ({check.detail} in the measured files)"
    return _not_proven(JS_COVERAGE_GATE, detail, cite), sidecar


def js_findings(
    copy: Path,
    resolved: str,
    limit: float,
    tools: Path | None = None,
    locator: bool = False,
    tier: int = 1,
) -> tuple[list[Finding], dict[str, Mapping[str, Any]]]:
    """The `.js` tests' findings at `tier` and their sidecars, for a change that
    touches a `.js` file: nothing otherwise.

    As for Python, tier 1 holds the tests and changed-line coverage: `js-tests`
    runs every node test of the tree and records each one's result, and
    `js-coverage` judges the changed lines, so a gap blocks tier 2. Tier 2 holds
    `js-red-phase`, the new or changed test files run on the baseline. A tool
    that could not run, or a tree with no node test, is `not-proven` and says
    which, never a pass and never an empty list.
    """
    if not any(f.endswith(JS_SUFFIX) for f in git_changed_files(copy, resolved)):
        return [], {}
    found: list[Finding] = []
    sidecars: dict[str, Mapping[str, Any]] = {}
    tests = js_test_files(copy)
    ran = run_node_tests(copy, tests, timeout=limit)
    if tier == 2:
        return _js_red_phase(copy, resolved, limit, ran.results)
    cite = "saddle.gates.check_js_tests"
    covered = js_coverage_finding(copy, resolved, limit, tools, tests)
    if covered is not None:
        gap = covered[0]
        if locator and gap.verdict == "fail":
            # Under the shortlist, coverage is a locator, as the Python check is.
            gap = dataclasses.replace(gap, verdict="not-proven")
        found.append(dataclasses.replace(gap, tier=1))
        sidecars[JS_COVERAGE_GATE] = covered[1]
    if ran.problem or not tests:
        missing = _not_proven(JS_TESTS_GATE, ran.problem or NO_NODE_TEST, cite)
        found.append(dataclasses.replace(missing, tier=1))
        return found, sidecars
    check = check_js_tests([r.row() for r in ran.results], ran.exit_code)
    found.append(
        Finding(
            JS_TESTS_GATE,
            1,
            "pass" if check.passed else "fail",
            "code-wrong",
            check.detail,
            (cite,),
        )
    )
    sidecars[JS_TESTS_GATE] = {
        "exit_code": ran.exit_code,
        "results": [list(r.row()) for r in ran.results],
    }
    return found, sidecars


def _js_red_phase(
    copy: Path, resolved: str, limit: float, head: Sequence[JsTestResult]
) -> tuple[list[Finding], dict[str, Mapping[str, Any]]]:
    """Tier 2's `js-red-phase` finding: the new or changed `.js` test files run on
    the baseline, judged against `head`, the head tree's results. Nothing when
    no `.js` test changed."""
    found: list[Finding] = []
    sidecars: dict[str, Mapping[str, Any]] = {}
    red = red_phase(copy, resolved, head, timeout=limit)
    if red is not None:
        cite = "saddle.gates.check_js_red_phase"
        if red.base.problem:
            found.append(_not_proven(JS_RED_PHASE_GATE, red.base.problem, cite))
        else:
            judged = check_js_red_phase(
                [r.row() for r in red.head], [r.row() for r in red.base.results], red.base.exit_code
            )
            found.append(
                Finding(
                    JS_RED_PHASE_GATE,
                    2,
                    "pass" if judged.passed else "fail",
                    "evidence-thin",
                    judged.detail,
                    (cite,),
                )
            )
            sidecars[JS_RED_PHASE_GATE] = {
                "files": list(red.files),
                "head": [list(r.row()) for r in red.head],
                "base": [list(r.row()) for r in red.base.results],
                "base_exit_code": red.base.exit_code,
            }
    return found, sidecars


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
    nothing not proven names lines, except the budget-bound mutation note. A
    tree that holds a `PROJECT_GATE` finding is never reused: a format-only edit
    is what a format stage (`ruff format --check`, a linter's whitespace rules)
    judges, so its earlier pass says nothing about the reformatted tree."""
    for f in found.findings:
        if f.gate == PROJECT_GATE:
            return False
        if f.verdict in ("fail", "blocked") and f.reason != "sanctioned":
            return False
        if f.verdict == "not-proven" and not f.detail.startswith(MUTATION_UNMEASURED[:12]):
            return False
    return True


def _run_static(argv: tuple[str, ...], copy: Path, limit: float) -> GateCheck:
    """Run the project's static check on the audited copy and judge it."""
    return check_static(argv, run_static_check(argv, copy, timeout=limit))


def node_stage(argv: Sequence[str], tools: Path | None) -> tuple[str, ...]:
    """`argv` with `npx --no-install X ...` run as `node <X's script> ...` when `tools`
    holds `node_modules/.bin/X`; any other stage, or a tool not installed, unchanged.

    npx lives under HOME and its symlinks point there, where the audit's sandbox
    sees nothing, so every `npx` stage read not-proven. The package's own script
    needs only node, which `sandbox-expose` shows. An unchanged `npx` argv still
    ends not-proven, naming the tool, never passing."""
    if tools is not None and len(argv) > 2 and tuple(argv[:2]) == ("npx", "--no-install"):
        script = tools / "node_modules" / ".bin" / argv[2]
        if script.is_file():
            return ("node", str(script.resolve()), *argv[3:])
    return tuple(argv)


def link_dependencies(copy: Path, checkout: Path) -> tuple[Path, ...]:
    """Link each of `checkout`'s `DEPENDENCY_DIRS` into its staged `copy`, once,
    before anything runs there; the directories to show read-only.

    The copy leaves out what git ignores, so a suite that needs `node_modules`
    failed in the audit and passed in the project's own gate: 80 of saddle's own
    tests did, and every change to it was refused. Linked once for the whole
    audit, not per gate stage, so no run sees it come and go. The copy is
    scratch, deleted with the audit; its staged tree id is taken before."""
    shown = []
    for name in DEPENDENCY_DIRS:
        real, link = checkout / name, copy / name
        if real.is_dir() and not link.exists() and not link.is_symlink():
            link.symlink_to(real.resolve())
            shown.append(real.resolve())
    return tuple(shown)


def _gate_stage_runs(
    stages: Sequence[tuple[str, ...]], tree: Path, limit: float, tools: Path | None = None
) -> list[CapturedRun]:
    """Each stage run in `tree`, in the staged copy's sandbox (`run_static_check`).

    With `tools` (the checkout, whose git-ignored `node_modules` a staged copy
    lacks) the node stages run from its installed packages (`node_stage`):
    `node_modules` is shown read-only and linked into `tree` for the run, so a
    config that imports a package finds it, and the link is removed after, so the
    tree is left as it was."""
    modules = None if tools is None else tools / "node_modules"
    if modules is None or not modules.is_dir():
        return [run_static_check(argv, tree, timeout=limit) for argv in stages]
    link = tree / "node_modules"
    linked = not link.exists() and not link.is_symlink()
    if linked:
        link.symlink_to(modules.resolve())
    try:
        return [
            run_static_check(node_stage(argv, tools), tree, timeout=limit, shown=(modules,))
            for argv in stages
        ]
    finally:
        if linked:
            link.unlink()


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


NO_TESTS_GATES: Final = ("tests", "coverage", "mutation", "red-phase")
"""The checks that need a test (`full-suite` reads `tests`' status)."""


def baseline_has_tests(copy: Path, resolved: str) -> bool:
    """Whether the baseline tracks any test file, Python or JavaScript, by name
    (`_test_side`, `is_js_test_file`). A test file that collects nothing still
    counts: a change cannot be judged against tests it may have emptied."""
    names = run_capture(["git", "ls-tree", "-r", "--name-only", resolved], copy).stdout
    return any(_test_side(n) or is_js_test_file(n) for n in names.splitlines())


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


TEST_CHANGES: Final = "test-changes"
"""The finding for pre-existing tests the tree changed, in every language
(`flips`). Emitted only when something changed or could not be read,
and not one of `TIER1`: it is judged against the finish summary, which changes
between finish calls on one tree, so it is never cached with the battery."""


def _git_out(root: Path, *argv: str) -> bytes:
    """Stdout of `git <argv>` in `root`; an `AuditError` when it exits nonzero, so a
    lookup that failed never reads as "no test changed"."""
    run = subprocess.run(["git", *argv], cwd=root, capture_output=True, check=False)
    if run.returncode != 0:
        said = run.stderr.decode(errors="replace").strip()
        msg = f"git {' '.join(argv)} failed: {said}"
        raise AuditError(msg)
    return run.stdout


def changed_tests(root: Path, baseline: str) -> list[flips.ChangedTest]:
    """The pre-existing test code `root`'s tree changed against `baseline`, read-only.

    Every test-code file (`flips.language`) git says differs from the baseline, as
    the baseline had it and as the tree has it, plus every Python or JavaScript
    file the tree added (a test that moved is found there; a new file is never a
    change). A Python or JavaScript file that is not UTF-8 is reported as
    unreadable; data and other languages are compared byte for byte.
    """
    resolved = _git_out(root, "rev-parse", "--verify", f"{baseline}^{{commit}}").decode().strip()
    listing = _git_out(root, "diff", "--name-status", "--no-renames", "-z", resolved)
    fields = listing.decode().split("\0")
    status = {path: kind for kind, path in zip(fields[0::2], fields[1::2], strict=False)}
    untracked = _git_out(root, "ls-files", "-z", "--others", "--exclude-standard").decode()
    base: dict[str, str] = {}
    head: dict[str, str] = {}
    unreadable: list[flips.ChangedTest] = []

    def put(into: dict[str, str], path: str, data: bytes, where: str) -> None:
        if flips.language(path) == "other":
            # Data and other languages are compared byte for byte: surrogateescape
            # round-trips any bytes, so a binary fixture is read, never refused.
            into[path] = data.decode(errors="surrogateescape")
            return
        try:
            into[path] = data.decode()
        except UnicodeDecodeError:
            unreadable.append(
                flips.ChangedTest(path, path, flips.UNREADABLE, f"{where}: not UTF-8")
            )

    for path, kind in status.items():
        if flips.language(path) is None:
            continue
        if kind != "A":
            put(base, path, _git_out(root, "show", f"{resolved}:{path}"), "baseline")
        if kind != "D":
            put(head, path, (root / path).read_bytes(), "tree")
    for path in (p for p in untracked.split("\0") if p):
        if flips.language(path) in ("python", "js"):  # a new file is never a change
            put(head, path, (root / path).read_bytes(), "tree")
    # An unreadable file is reported once, not also as a file whose tests all vanished.
    for bad in {c.path for c in unreadable}:
        base.pop(bad, None)
        head.pop(bad, None)
    return sorted([*flips.detect(base, head), *unreadable], key=lambda c: (c.path, c.name, c.kind))


def commit_messages(root: Path, baseline: str) -> str:
    """The messages of the commits after `baseline` in `root`, newest first; "" for none.

    What `saddle audit` reads a `flip:` line from when the change is committed.
    """
    resolved = _git_out(root, "rev-parse", "--verify", f"{baseline}^{{commit}}").decode().strip()
    return _git_out(root, "log", "--format=%B", f"{resolved}..HEAD").decode()


def flip_finding(
    root: Path, baseline: str, message: str, sanctioned: Sequence[str] = ()
) -> Finding | None:
    """The `test-changes` finding for `root`'s tree, or None when no pre-existing test changed.

    A test the task itself ordered rewritten (`AuditorConfig.sanctioned_test_rewrites`)
    needs no label: `sanction` and the red-on-baseline check already judge it.

    `fail` until every changed test has its `flip:` line in `message` with evidence
    `flips.circular_evidence` does not refuse; then `not-proven`, never
    `pass`: the label buys a person's review, not a verdict.
    """
    changes = [c for c in changed_tests(root, baseline) if c.name not in sanctioned]
    judged = flips.judge(changes, message)
    if judged is None:
        return None
    return Finding(
        gate=TEST_CHANGES,
        tier=1,
        verdict=judged.verdict,
        reason="evidence-thin",
        detail=judged.detail,
        cites=("saddle.flips.judge",),
    )


def _reason(gate: str, verdict: Verdict, detail: str) -> Reason:
    if verdict == "not-proven" and detail == NO_TESTS_DETAIL:
        return "evidence-thin"
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


def raise_gaps(
    copy: Path,
    baseline: str,
    detail: str,
    basis: str,
    entered: Sequence[tuple[str, int]] | None = None,
) -> list[dict[str, Any]]:
    """Each changed `raise` the coverage record says no test enters (K2 §3.3).

    Read from the finding the gate already made: the lines its detail names
    and the definitions its `basis` spared, placed in the audited tree by
    `coverage_text.unreached_raises`. No test is run and no verdict changes;
    the rows are sealed beside the coverage finding for the packet's Not
    proven section. `entered` is what a P1 raise example ran, if any.
    """
    uncovered = coverage_text.uncovered_lines(detail)
    spared = spared_definitions(basis)
    files = {f for f, _ in uncovered} | {n.rpartition(":")[0] for n in spared}
    if not files:
        return []
    sources: dict[str, str] = {}
    for rel in sorted(files):
        try:
            sources[rel] = (copy / rel).read_text()
        except (OSError, UnicodeDecodeError):
            continue
    changed = {
        (str(Path(path).relative_to(copy)), line)
        for path, line in changed_statements(copy, git_diff(copy, baseline))
    }
    gaps = coverage_text.unreached_raises(sources, changed, uncovered, spared, entered)
    return [dataclasses.asdict(g) for g in gaps]


def documented_raises_finding(copy: Path, baseline: str) -> Finding | None:
    """The docstring `Raises` question (K2 D4), or None when it asks nothing.

    Read from the tree with `ast` alone (`gates.check_documented_raises`):
    the changed non-test `.py` files, and every test file git lists. A
    `question`, so the audit reports it under `needs_you` and never refuses.
    """
    changed = {
        (str(Path(path).relative_to(copy)), line)
        for path, line in changed_statements(copy, git_diff(copy, baseline))
    }

    def read(names: Iterable[str]) -> dict[str, str]:
        out: dict[str, str] = {}
        for rel in names:
            try:
                out[rel] = (copy / rel).read_text()
            except (OSError, UnicodeDecodeError):
                continue
        return out

    sources = read(sorted({f for f, _ in changed if f.endswith(".py") and not is_test_file(f)}))
    listed = run_capture(
        ["git", "ls-files", "-z", "--cached", "--others", "--exclude-standard"], copy
    ).stdout.split("\0")
    tests = read(sorted(n for n in listed if n.endswith(".py") and is_test_file(n)))
    check = check_documented_raises(sources, changed, tests)
    if check.detail == DOCUMENTED_RAISES_HELD:
        return None
    return Finding(
        DOCUMENTED_RAISES,
        1,
        "question",
        "evidence-thin",
        check.detail,
        ("saddle.gates.check_documented_raises", check.basis or ""),
    )


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
        self.tools = Path(self.config.dependencies) if self.config.dependencies else self.repo
        """Where `link_dependencies` and the node stages find the installed packages."""
        self.node = self.config.node or audit_node(self.config.test_command)
        self._memory: dict[str, Findings] = {}
        self._base_runs: dict[str, list[tuple[int, bool]]] = {}
        self._by_syntax: dict[str, str] = {}
        self._bench_seen: dict[tuple[str, tuple[str, ...]], Measured] = {}
        """A readable baseline prompt-benchmark score by (commit, command): the
        baseline does not change within a run, so it is measured once."""
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
                *(
                    ["task-text", hashlib.sha256(self.config.task_text.encode()).hexdigest()]
                    if self.config.task_text is not None
                    else []
                ),
                *(["impact"] if self.config.impact is not None else []),
                *(["no-tests", "not-proven"] if self.config.no_tests == "not-proven" else []),
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
            configured = ruff_configured(self.repo, self.baseline_rev)
            overrides = format_overrides(self.repo, self.baseline_rev)
            # `--diff`, not `--check`: the same exits, and the diff it prints is
            # what the finding hands a model that has no ruff (`check_ruff`).
            # Not run where the project did not choose ruff (#130).
            fmt = (
                run_capture(
                    ruff_argv("format", "--diff", *overrides, rel),
                    current,
                    timeout=RUFF_TIMEOUT_S,
                )
                if configured
                else None
            )
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
            format_exit=0 if fmt is None else fmt.exit_code,
            format_diff="" if fmt is None else fmt.stdout,
            format_checked=fmt is not None,
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

    def _gate(self, tier: int, tree: Path | None, *, whole_suite: bool = False) -> Findings:
        mode = ("whole-suite",) if whole_suite else ()
        with staged_copy(tree or self.repo, self.baseline_rev) as (copy, staged, resolved):
            if staged == baseline_tree(copy, resolved):
                msg = f"nothing to audit: the tree equals baseline {resolved[:12]}"
                raise AuditError(msg)
            key = self._key(tier, staged, resolved, *mode)
            hit = self._cached(key)
            if hit is not None:
                return hit
            # A format-only edit of an audited tree: the interpreter runs the
            # same code, so the suite and mutation are not asked again.
            shape = syntax_key(copy)
            same1 = self._key(1, "syntax", shape, resolved)
            same = self._key(tier, "syntax", shape, resolved, *mode)
            reused = self._reuse(same, key)
            if reused is not None:
                return reused
            try:
                # Read at the resolved baseline, which `key` already names: the
                # limit and the worker count are functions of that commit, not
                # of the tree audited.
                limit = suite_limit(copy, resolved).seconds
                workers = suite_workers(copy, resolved).count
                statics = static_check(copy, resolved)
                declared = gate_checks(copy, resolved)
                # One lookup of the diff's languages serves every filter below.
                touched = classify(git_changed_files(copy, resolved))
                about = gate_stage_languages(copy, resolved)
                # A stage about a language the diff does not touch is not run or shown.
                declared = tuple(
                    s for s in declared if stage_visible(gate_stage_name(s), touched, about)
                )
                if statics and not stage_visible(gate_stage_name(statics), touched, about):
                    statics = ()
                # `static-check` is one more stage of the gate, unless it is listed too.
                stages = (*declared, *((statics,) if statics and statics not in declared else ()))
                exposed = sandbox_expose(copy, resolved)
                bench = prompt_benchmark(copy, resolved)
            except SuiteLimitError as exc:
                raise AuditError(str(exc)) from exc
            # One run of the battery serves both tiers: at tier 2 the tier-1
            # findings come from the same suite run, and are cached under
            # tier 1's key, unless that tree's tier 1 is already known. The
            # finish audit used to run the whole suite once per tier on one tree.
            deps = link_dependencies(copy, self.config.dependencies or tree or self.repo)
            with sandbox.also_exposing(exposed), sandbox.also_showing(deps):
                key1 = self._key(1, staged, resolved)
                first = (self._cached(key1) or self._reuse(same1, key1)) if tier == 2 else None
                if first is not None and not first.passed:
                    return self._store(_blocked_tier2(key, first))
                p1: Future[TaskRequirementsCheck] | None = None
                pool: ThreadPoolExecutor | None = None
                static: Future[GateCheck] | None = None
                project: Future[ProjectGate] | None = None
                if first is None and statics:
                    # Beside the tests too: a type check takes seconds, the suite minutes.
                    pool = ThreadPoolExecutor(max_workers=3, thread_name_prefix="saddle-p1")
                    static = pool.submit(
                        contextvars.copy_context().run, _run_static, statics, copy, limit
                    )
                if first is None and stages:
                    # Beside the tests too, as the static check: the base runs are cached.
                    pool = pool or ThreadPoolExecutor(max_workers=3, thread_name_prefix="saddle-p1")
                    project = pool.submit(
                        contextvars.copy_context().run,
                        self._project_gate,
                        stages,
                        copy,
                        resolved,
                        limit,
                    )
                if first is None and self.config.task_requirements is not None:
                    # Beside the tests, not after them: it adds to the wall only if
                    # it outlasts them. The context carries the project environment.
                    pool = pool or ThreadPoolExecutor(max_workers=3, thread_name_prefix="saddle-p1")
                    p1 = pool.submit(
                        contextvars.copy_context().run,
                        check_tree,
                        copy,
                        resolved,
                        self.config.task_requirements,
                    )
                memo = self.config.impact
                selection = None if whole_suite else _selection(memo, copy, resolved)
                try:
                    gated = runner.run_node_gate(
                        self.node,
                        copy,
                        baseline=resolved,
                        tier2=tier == 2,
                        test_timeout=limit,
                        test_workers=workers,
                        test_selection=selection,
                        skip_report=copy / SKIP_REPORT_NAME,
                        test_only_additions=True,
                        task_text=self.config.task_text,
                        js_tools=self.tools,
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
                        first = self._tiered(
                            1,
                            key1,
                            gated,
                            copy,
                            resolved,
                            limit,
                            p1,
                            static,
                            project,
                            touched=touched,
                        )
                    self._by_syntax[same1] = first.key
                    if not first.passed:
                        return self._store(_blocked_tier2(key, first))
                    second = self._tiered(
                        2,
                        key,
                        gated,
                        copy,
                        resolved,
                        limit,
                        None,
                        selected=selection,
                        bench=bench,
                        touched=touched,
                    )
                    self._by_syntax[same] = second.key
                    return second
                done = self._tiered(
                    1, key, gated, copy, resolved, limit, p1, static, project, touched=touched
                )
                self._by_syntax[same] = done.key
                return done

    def _project_gate(
        self, stages: Sequence[tuple[str, ...]], copy: Path, resolved: str, limit: float
    ) -> ProjectGate:
        """The project's gate stages on the audited copy and on the baseline, judged
        (`check_project_gate`). The baseline's runs are a function of its tree and
        the stages, so they are asked once per baseline (`_base_stage_runs`)."""
        head = _gate_stage_runs(stages, copy, limit, self.tools)
        base = self._base_stage_runs(stages, copy, resolved, limit)
        return check_project_gate(list(zip(stages, head, base, strict=True)))

    def _base_stage_runs(
        self, stages: Sequence[tuple[str, ...]], copy: Path, resolved: str, limit: float
    ) -> list[CapturedRun]:
        """`stages` run on the baseline's own files, from the cache when its tree and
        stages were run before. Only each run's exit and whether it timed out are
        kept: the baseline's output is never quoted. A cache file that cannot be
        read is a miss, not an empty result."""
        payload = json.dumps(
            [PROJECT_GATE, baseline_tree(copy, resolved), stages, sandbox.environment_key()]
        )
        key = hashlib.sha256(payload.encode()).hexdigest()
        recorded = self._base_runs.get(key)
        path = None if self.config.cache_dir is None else self.config.cache_dir / f"gate-{key}.json"
        if recorded is None and path is not None:
            try:
                data = json.loads(path.read_text())
                recorded = [(int(code), bool(timed)) for code, timed in data]
                if len(recorded) != len(stages):
                    recorded = None
            except (OSError, ValueError, TypeError):
                recorded = None
        if recorded is None:
            with tempfile.TemporaryDirectory(prefix="saddle-gate-base-") as scratch:
                base = Path(scratch) / "tree"
                materialize_baseline(copy, resolved, base)
                ran = _gate_stage_runs(stages, base, limit, self.tools)
            recorded = [(r.exit_code, r.timed_out) for r in ran]
            if path is not None:
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(json.dumps(recorded))
        self._base_runs[key] = recorded
        return [
            CapturedRun(argv=argv, exit_code=code, stdout="", stderr="", timed_out=timed)
            for argv, (code, timed) in zip(stages, recorded, strict=True)
        ]

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
        static: Future[GateCheck] | None = None,
        project: Future[ProjectGate] | None = None,
        selected: Sequence[str] | None = None,
        bench: PromptBenchmark | None = None,
        touched: frozenset[str] = frozenset({OTHER}),
    ) -> Findings:
        """`tier`'s findings from one run of the battery (`_gate`), stored under `key`.
        `selected` is the test files an impact-scoped suite run was limited to, None
        for the whole suite. `touched` is the diff's languages (`languages.classify`):
        a finding about another language is dropped unless it refuses
        (`languages.visible`); the default hides nothing."""
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
        dead_status, dead_detail, dead_basis = statuses["dead-code"]
        if dead_status == "pass" and dead_detail.startswith(TEST_ONLY_UNPROVEN):
            # Public names no production code reaches, in a change that gives no sign of
            # padding: listed for a person, never a refusal (`check_test_only_additions`).
            statuses["dead-code"] = ("not-proven", dead_detail, dead_basis)
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
        measurable = measurable_here(copy, self.tools)
        unmeasured = (
            unmeasurable_files(git_changed_files(copy, resolved), measurable) if tier == 2 else []
        )
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
        elif (
            tier == 2
            and spent is not None
            and spent.total == 0
            and statuses.get("mutation", ("",))[0] == "fail"
            and statuses["mutation"][1].startswith(MUTATION_TOOL_FAILED_PREFIX)
        ):
            # The engine crashed (a red suite reads "mutation not measured" and
            # keeps its refusal): no evidence either way, and no edit clears it.
            detail = MUTATION_TOOL_CRASHED.format(detail=statuses["mutation"][1])
            statuses["mutation"] = ("not-proven", detail, statuses["mutation"][2])
        elif (
            tier == 2
            and spent is not None
            and spent.generated == 0
            and spent.total == 0
            and not spent.survivors  # an engine that failed still fails
            and statuses.get("mutation", ("",))[0] == "fail"
            and (data := data_only_change(copy, resolved))
        ):
            detail = MUTATION_DATA_ONLY.format(files=", ".join(data))
            statuses["mutation"] = ("not-proven", detail, statuses["mutation"][2])
        elif (
            tier == 2
            and spent is not None
            and spent.generated == 0
            and spent.total == 0
            and not spent.survivors
            and statuses.get("mutation", ("",))[0] == "fail"
            and unmeasured
            and not source_lines_changed(copy, resolved)
        ):
            # Nothing the checks measure changed, and the files that did are listed
            # (`NOT_MEASURABLE_GATE`): no evidence either way, never a refusal.
            statuses["mutation"] = ("not-proven", MUTATION_NOT_MEASURABLE, statuses["mutation"][2])
            red = statuses.get("red-phase", ("", "", None))
            if red[0] == "fail" and red[1].startswith(RED_PHASE_NO_MUTANTS):
                statuses["red-phase"] = ("not-proven", RED_PHASE_NOT_MEASURABLE, red[2])
        elif (
            tier == 2
            and spent is not None
            and spent.generated == 0
            and spent.total == 0
            and not spent.survivors
            and statuses.get("mutation", ("",))[0] == "fail"
            and not source_lines_changed(copy, resolved)
        ):
            # Only test code changed: mutation measures source lines, and there are
            # none, so it has no evidence either way (#181).
            statuses["mutation"] = ("not-proven", MUTATION_TESTS_ONLY, statuses["mutation"][2])
        elif unmeasured and statuses.get("mutation", ("",))[0] in ("pass", "fail"):
            # Python lines were measured, other files were not: the verdict must not
            # read as covering the whole change.
            status, detail, basis = statuses["mutation"]
            count = f"{len(unmeasured)} changed non-{_measured_languages(measurable)} file(s)"
            statuses["mutation"] = (
                status,
                f"{detail}; {_measured_languages(measurable)} lines only, {count} not measurable "
                f"(listed under {NOT_MEASURABLE_GATE})",
                basis,
            )
        red = statuses.get("red-phase", ("", "", None))
        if (
            tier == 2
            and red[0] == "fail"
            and red[1].startswith(RED_PHASE_TESTS_UNCHANGED)
            and not source_lines_changed(copy, resolved)
        ):
            # Python's refactor rule (no Python test changed, so mutation carries the
            # proof) read a change with no Python source line in it: the mutants it
            # scored were another language's, whose own checks judge them.
            statuses["red-phase"] = ("not-applicable", RED_PHASE_NO_PYTHON, red[2])
        elif tier == 2 and red[0] == "fail" and red[1] == RED_PHASE_TESTS_ONLY:
            statuses["red-phase"] = ("not-applicable", RED_PHASE_ONLY_TESTS, red[2])
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
        checked = p1.result() if p1 is not None else None
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
            # Each changed raise no test enters, read from the same record
            # whatever the verdict: a report beside the finding, never a verdict.
            entered = checked.raise_ran if checked is not None else None
            raises = raise_gaps(copy, resolved, detail, basis or "", entered)
            if raises:
                sealed = {**(sealed or {}), "raises": raises}
            if sealed is not None:
                sidecars["coverage"] = sealed
        if checked is not None:
            statuses[TASK_REQUIREMENTS] = (checked.verdict, checked.detail, checked.basis)
            sidecars[TASK_REQUIREMENTS] = p1_tally(checked)
        if static is not None:
            ran = static.result()
            statuses[STATIC_CHECK] = ("pass" if ran.passed else "fail", ran.detail, None)
        if project is not None:
            judged = project.result()
            statuses[PROJECT_GATE] = (judged.verdict, judged.detail, None)
        tests_status, tests_detail, _ = statuses["tests"]
        if (
            self.config.no_tests == "not-proven"
            and tests_status == "fail"
            and tests_detail.endswith(NO_TESTS_COLLECTED)
            and not baseline_has_tests(copy, resolved)
        ):
            # A person's commits to a project with no tests: nothing was run, and
            # no edit to the change could make a test run. Last, so no rule above
            # reads a check this replaces.
            for gate in NO_TESTS_GATES:
                statuses[gate] = ("not-proven", NO_TESTS_DETAIL, statuses[gate][2])
        wanted = TIER1 if tier == 1 else TIER2
        if STATIC_CHECK in statuses:
            wanted = (*wanted, STATIC_CHECK)
        if PROJECT_GATE in statuses:
            wanted = (*wanted, PROJECT_GATE)
        if TASK_REQUIREMENTS in statuses:
            wanted = (*wanted, TASK_REQUIREMENTS)
        findings = []
        for gate in wanted:
            status, detail, basis = statuses["tests" if gate == "full-suite" else gate]
            found = _finding(gate, tier, status, detail, basis)
            if gate in cites:
                found = dataclasses.replace(found, cites=(cites[gate], *found.cites[1:]))
            findings.append(sanction(found, self.config.sanctioned_test_rewrites))
        if tier == 1 and (documented := documented_raises_finding(copy, resolved)) is not None:
            findings.append(documented)
        if unmeasured:
            findings.append(
                _not_proven(
                    NOT_MEASURABLE_GATE,
                    not_measurable_detail(unmeasured, measurable),
                    "saddle.auditor.unmeasurable_files",
                )
            )
            sidecars[NOT_MEASURABLE_GATE] = {"files": unmeasured}
        if tier == 2 and skip_reportable(self.node.deterministic_gate.test_command, copy):
            skips = read_skip_report(copy / SKIP_REPORT_NAME)
            scope = (
                "the suite run"
                if selected is None
                else f"the impact-scoped run ({len(selected)} test file(s) the change can reach; "
                "skips in the others are not counted)"
            )
            if (said := skipped_detail(skips, scope)) is not None:
                findings.append(_not_proven(SKIPPED_GATE, said, "saddle.auditor.skipped_detail"))
                sidecars[SKIPPED_GATE] = {
                    "known": skips.known,
                    "scope": scope,
                    "skipped": [list(p) for p in skips.skipped],
                    "xfailed": [list(p) for p in skips.xfailed],
                }
        js_found, js_sidecars = js_findings(
            copy, resolved, limit, self.tools, self.config.tier2 == "shortlist", tier
        )
        findings.extend(js_found)
        sidecars.update(js_sidecars)
        if tier == 2 and (effect := prompt_effect(copy, resolved, limit, bench, self._bench_seen)):
            findings.append(effect[0])
            sidecars[PROMPT_EFFECT_GATE] = effect[1]
        blind = () if JS_SUFFIX in measurable else (JAVASCRIPT,)
        findings = [f for f in findings if visible(f.gate, f.verdict, touched, blind)]
        shown = {f.gate for f in findings}
        sidecars = {g: v for g, v in sidecars.items() if g in shown or g not in FINDING_LANGUAGES}
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
                exposed = sandbox_expose(copy, resolved)
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
            deps = link_dependencies(copy, self.config.dependencies or tree or self.repo)
            with sandbox.also_exposing(exposed), sandbox.also_showing(deps):
                data_file = str(copy / ".coverage.map")
                drop_test_caches(copy)
                mode = suite_run(copy, command, workers)
                ran = run_suite_capture(
                    mode, command, copy, data_file, timeout=limit, contexts=True
                )
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

    def tier1_whole_suite(self, tree: Path | None = None) -> Findings:
        """Tier 1 with every test file, never the impact map's selection, under
        its own cache keys: a narrowed result is never handed back for it, nor it
        for a narrowed one. A `check` with `whole_suite` asks for it (#171)."""
        return self._gate(1, tree, whole_suite=True)

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
