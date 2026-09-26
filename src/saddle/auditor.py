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
  all but one red-phase sample.
- **tier 2, asynchronous** (`Auditor.tier2(tree)`): `mutation` (untested
  mutants counted as survivors, `evidence.mutation_sample` since P0-3),
  `property-coverage`, `red-phase`, `requirement-binding` and `full-suite`
  (the `tests` check of the same full `runner.run_node_gate` run: with no
  plan the audit's test command is the whole suite). Tier 2 never runs on a
  tree whose tier 1 failed: it returns one `blocked` finding instead.

Every verdict is keyed by the tree (`audit.staged_copy`'s `git write-tree`
over the worktree with untracked files staged) plus the resolved baseline,
the test command, the node and `audit.gate_surface()`, so an identical tree
is never gated twice at the same tier. Tier 0 is keyed by the file's path
and bytes instead, since it sees one file and no tree.

Without a plan node the four plan-relative checks are `not-applicable`
(`audit.NOT_APPLICABLE`); with one (`AuditorConfig.node`), they run.

Layering: imports `audit`, `evidence`, `gates`, `journal` and `runner`;
`gates` still imports nothing of this. Only the CLI imports it.
"""

from __future__ import annotations

import ast
import dataclasses
import hashlib
import importlib.util
import json
import os
import sys
import tempfile
from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from typing import Final, Literal

from saddle import runner
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
from saddle.evidence import MutationOutcome, ruff_argv, ruff_findings, run_capture
from saddle.gates import (
    DEFAULT_MUTANT_SHORTLIST,
    GateCheck,
    RuffFinding,
    check_mutation_shortlist,
    check_ruff,
    check_syntax,
    introduced_findings,
    set_aside_kind,
    shortlist_order,
)
from saddle.journal import append_span, build_span

Verdict = Literal["pass", "fail", "not-applicable", "blocked", "not-proven"]
Tier2Mode = Literal["score", "shortlist"]
TIER2_MODES: Final[tuple[Tier2Mode, ...]] = ("score", "shortlist")
Reason = Literal["code-wrong", "evidence-thin", "scope", "unknown", "sanctioned"]
"""`sanctioned`: a failing `assertion-preservation` finding that names only
tests the task itself declared rewritable (`AuditorConfig.sanctioned_test_rewrites`).
The verdict stays `fail` -- the gate did see rewritten assertions -- but the
finding does not count against `Findings.passed`, so it neither blocks tier 2
nor refuses an autonomous run's `finish`."""

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
    "node-scope": "scope",
    "target-scope": "scope",
}

# A mutation detail that names a tool that never decided anything is no
# claim about the code or its tests (gates.check_mutation's own wording).
_TOOL_FAILURE_PREFIXES: Final = ("mutation tool failed", "mutation not measured")

_JOURNAL_EXIT: Final[dict[Verdict, int]] = {
    "pass": 0,
    "fail": 1,
    "not-applicable": 0,
    "blocked": 2,
    "not-proven": 0,
}


@dataclass(frozen=True)
class Survivor:
    """One surviving changed-line mutant, as a shortlist names it (SHORTLIST)."""

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


@dataclass(frozen=True)
class Findings:
    """A tier's findings over one tree (or, at tier 0, one file's bytes)."""

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
        return all(
            f.verdict in ("pass", "not-applicable", "not-proven") or f.reason == "sanctioned"
            for f in self.findings
        )

    def to_dict(self) -> dict[str, object]:
        return {
            "tier": self.tier,
            "key": self.key,
            "passed": self.passed,
            "cached": self.cached,
            "findings": [dataclasses.asdict(f) for f in self.findings],
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
    as before SHORTLIST. "shortlist" decides tier 2 on open survivors
    (`gates.check_mutation_shortlist`), makes coverage a locator
    (`not-proven`, never a refusal) and turns on the finish-time behaviour in
    `feed` (format, cheap-route checks, way-out claims)."""
    mutant_shortlist: int = DEFAULT_MUTANT_SHORTLIST
    """How many open survivors a mutation finding's detail names (`--mutant-shortlist`)."""


_REWROTE: Final = " rewrote assertions in: "
"""`gates.check_assertion_preservation`'s wording before the test names."""


def sanction(finding: Finding, sanctioned: Sequence[str]) -> Finding:
    """Reclass a failing assertion-preservation finding that names only
    sanctioned tests; every other finding is returned unchanged."""
    if finding.gate != "assertion-preservation" or finding.verdict != "fail":
        return finding
    _, sep, names = finding.detail.partition(_REWROTE)
    named = {n.strip() for n in names.split(",") if n.strip()}
    if not sep or not named or not named <= set(sanctioned):
        return finding
    return dataclasses.replace(
        finding,
        reason="sanctioned",
        detail=f"{finding.detail} (all sanctioned by the task)",
    )


def _reason(gate: str, verdict: Verdict, detail: str) -> Reason:
    if verdict == "blocked" or (gate == "mutation" and detail.startswith(_TOOL_FAILURE_PREFIXES)):
        return "unknown"
    return REASONS[gate]


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


def check_imports(path: str, source: str, roots: Sequence[Path]) -> GateCheck:
    """Every absolute import's top-level name resolves: stdlib, a module or
    package under one of `roots`, or an installed distribution.

    Only top-level names are looked up, with `importlib.util.find_spec`,
    which imports nothing for a top-level name; relative imports are not
    checked and are counted in the detail. The interpreter asked is saddle's
    own, not necessarily the one the tree's tests run under.
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
    missing = sorted(
        {
            name
            for name in names
            if name not in sys.stdlib_module_names
            and not any((r / f"{name}.py").is_file() or (r / name).is_dir() for r in roots)
            and importlib.util.find_spec(name) is None
        }
    )
    note = f"; {relative} relative import(s) not checked" if relative else ""
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

    # -- cache ---------------------------------------------------------------

    def _key(self, tier: int, *parts: str) -> str:
        payload = json.dumps(
            [
                tier,
                *parts,
                self.config.test_command,
                self.node.model_dump_json(),
                gate_surface(),
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

    def _store(self, result: Findings) -> Findings:
        self._memory[result.key] = result
        if self.config.cache_dir is not None:
            self.config.cache_dir.mkdir(parents=True, exist_ok=True)
            (self.config.cache_dir / f"{result.key}.json").write_text(
                json.dumps(result.to_dict(), sort_keys=True)
            )
        self._journal(result)
        return result

    def _journal(self, result: Findings) -> None:
        if self.config.journal is None:
            return
        for f in result.findings:
            append_span(
                self.config.journal,
                build_span(
                    node_id=self.node.id,
                    argv=["saddle-audit", f"tier{f.tier}", f.gate, result.key],
                    duration_ms=0,
                    exit_code=_JOURNAL_EXIT[f.verdict],
                    detail=json.dumps(dataclasses.asdict(f), sort_keys=True),
                    name=f"audit-tier{f.tier}:{f.gate}",
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
            fmt = run_capture(ruff_argv("format", "--check", rel), current)
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
        imports = check_imports(rel, new_text, roots)
        findings = tuple(_from_check(c, 0) for c in (syntax, ruff, imports))
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
            if tier == 2:
                first = self.tier1(copy)
                if not first.passed:
                    # The cause is what failed tier 1: a sanctioned finding did not
                    # (`Findings.passed`), so it is not named (FIX-3, M3F finding 6).
                    failed = ", ".join(
                        f.gate
                        for f in first.findings
                        if f.verdict == "fail" and f.reason != "sanctioned"
                    )
                    blocked = _finding(
                        "mutation", 2, "blocked", f"tier 1 failed ({failed}); tier 2 not run", None
                    )
                    return self._store(Findings(tier=2, key=key, findings=(blocked,)))
            gated = runner.run_node_gate(self.node, copy, baseline=resolved, tier2=tier == 2)
            if self.config.node is None:
                checks, _ = audit_checks(gated.checks, gated.mutation, copy)
                statuses = {c.name: (c.status, c.detail, c.basis) for c in checks}
            else:
                statuses = {
                    c.name: ("pass" if c.passed else "fail", c.detail, c.basis)
                    for c in gated.checks
                }
            if self.config.tier2 == "shortlist" and statuses.get("coverage", ("",))[0] == "fail":
                # SHORTLIST-4: coverage is a locator. An uncovered changed line is
                # "not proven", never a refusal; the detail keeps its lines.
                statuses["coverage"] = ("not-proven", *statuses["coverage"][1:])  # type: ignore[assignment]
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
        wanted = TIER1 if tier == 1 else TIER2
        findings = []
        for gate in wanted:
            status, detail, basis = statuses["tests" if gate == "full-suite" else gate]
            found = _finding(gate, tier, status, detail, basis)  # type: ignore[arg-type]
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
            )
        )

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
        return (*zero, self.tier1(root), self.tier2(root))
