"""Tier-1 per-node gates (ARCHITECTURE.md §3 Phase 3 Tier 1).

Checks run in gate order: syntax, ruff, tests, coverage, red-phase,
node-scope, target-scope, property-coverage, assertion-preservation,
requirement-binding, mutation. Each check is a small pure function so
killer fixtures stay fast and deterministic; subprocess runners are
injected at the boundary, never embedded in the predicates.
"""

from __future__ import annotations

import ast
import re
from collections.abc import Callable, Collection, Mapping, Sequence
from dataclasses import dataclass
from fnmatch import fnmatch
from pathlib import PurePath
from typing import TYPE_CHECKING, Final

from saddle.dag import Node

if TYPE_CHECKING:
    from saddle.evidence import MutationOutcome

# pytest exit codes that carry red-phase evidence (see `check_red_phase`).
PYTEST_TESTS_FAILED: Final = 1
PYTEST_COLLECTION_ERROR: Final = 2
# Exit code for a command killed on timeout, following GNU `timeout(1)`.
# Pytest reserves 0-5, so this cannot collide with a real suite verdict.
SHELL_TIMEOUT: Final = 124
# Exit code for a gate tool that could not be launched at all, following
# the shell convention for "command not found". A tool that raises rather
# than returning bypasses recovery and leaves the run with no stated
# reason: T1 v2 failed with no gate lines in the transcript, the only
# evidence being a journal span reading "[Errno 2] ... 'coverage'".
TOOL_UNAVAILABLE: Final = 127
# How many introduced ruff findings the gate detail names before eliding (T6-3).
RUFF_NAMED_FINDINGS: Final = 5
# Baseline runs sampled per red-phase check. Red-phase is the only gate
# that reasons over two runs, so its evidence is worth exactly what the
# stability of the pre-change leg is worth; one observation cannot tell a
# genuine failure from a flake.
RED_PHASE_SAMPLES: Final = 3
# Requirement IDs as they appear in test sources. Shape is pinned in the
# schema (dag.RequirementId), so anything matching here and absent from
# the node's declared set was invented by the worker.
REQUIREMENT_CITATION: Final = re.compile(r"REQ-\d{3}")
# Below this many decided mutants a kill percentage is noise: with 2
# mutants the only rates available are 0, 50 and 100. Demand all of them
# instead, so a thin sample is a stricter bar rather than a cheaper one.
MIN_SIGNIFICANT_MUTANTS: Final = 5
# pytest's own discovery rules, so "is this a test file" means the same
# thing to the gate as it does to the runner that will execute it.
TEST_FILE_PATTERNS: Final = ("test_*.py", "*_test.py")


def _is_test_file(path: str) -> bool:
    """True when pytest would collect `path` as a test module."""
    name = PurePath(path).name
    return any(fnmatch(name, pattern) for pattern in TEST_FILE_PATTERNS)


# Fixed floor for behaviour-preserving nodes; ARCHITECTURE.md's own gate
# example uses 85.0. Deliberately not the node's own kill_threshold.
REFACTOR_KILL_FLOOR: Final = 85.0


def _names_changed_source(baseline_output: str, changed_files: Collection[str]) -> bool:
    """True when a collection error blames a source file the node changed.

    Import failures name the module (`No module named 'n'`), collection
    headers name the file (`ERROR collecting n.py`), so both spellings
    are checked against every changed path.
    """
    return any(
        PurePath(path).name in baseline_output or f"'{PurePath(path).stem}'" in baseline_output
        for path in changed_files
    )


@dataclass(frozen=True)
class GateCheck:
    """Outcome of one Tier-1 check: `passed` plus human-readable `detail`.

    `basis` names the evidence the verdict rests on where a bare verdict
    would hide its weight: a mutation verdict over 0 mutants and one over
    5 both read "passed" (T7's 218-line module, CLAUDE.md), so the
    mutation check records how many mutants were sampled. Checks whose
    detail already carries the count leave it None.
    """

    name: str
    passed: bool
    detail: str = ""
    basis: str | None = None


def check_syntax(sources: Mapping[str, str]) -> GateCheck:
    """Parse each source; the first SyntaxError fails the gate."""
    for path, source in sources.items():
        try:
            ast.parse(source)
        except SyntaxError as exc:
            return GateCheck(name="syntax", passed=False, detail=f"{path}:{exc.lineno}: {exc.msg}")
    return GateCheck(name="syntax", passed=True, detail=f"{len(sources)} file(s) parsed")


@dataclass(frozen=True)
class RuffFinding:
    """One ruff diagnostic, keyed for matching across trees (T6-3).

    `line` is the stripped source line the finding sits on, so a finding
    the baseline already carried is the same finding after the node's
    edit shifts it down three lines; `row` is for the human reading the
    detail, not for matching.
    """

    code: str
    path: str
    row: int
    message: str
    line: str
    column: int = 0

    @property
    def key(self) -> tuple[str, str, str]:
        return (self.code, self.path, self.line)


def introduced_findings(
    current: Sequence[RuffFinding], baseline: Sequence[RuffFinding]
) -> tuple[list[RuffFinding], int]:
    """Split the current findings into (introduced, inherited count) (T6-3).

    A current finding is inherited when the baseline holds one with the
    same (code, path, source line) not already claimed by an earlier
    current finding; every other current finding is the node's own.
    """
    pool: dict[tuple[str, str, str], int] = {}
    for finding in baseline:
        pool[finding.key] = pool.get(finding.key, 0) + 1
    introduced: list[RuffFinding] = []
    inherited = 0
    for finding in current:
        if pool.get(finding.key, 0) > 0:
            pool[finding.key] -= 1
            inherited += 1
        else:
            introduced.append(finding)
    return introduced, inherited


def check_ruff(
    files: Collection[str],
    *,
    introduced: Sequence[RuffFinding],
    inherited: int,
    lint_exit: int,
    format_exit: int,
) -> GateCheck:
    """The ruff gate (T6-3): a node fails for lint its own diff introduced.

    `introduced` are the current tree's findings absent from the node's
    baseline, `inherited` how many the baseline already carried;
    `lint_exit` and `format_exit` are the two ruff runs' exits. A finding
    the node inherited is reported and does not fail it (T2's impl node
    burned three attempts on a BLE001 the baseline shipped, and the fix
    that was finally accepted was a `noqa` on code it never wrote).
    Formatting is unchanged: the harness formats the diff, so a format
    failure is always the node's. The detail names the rules, file and
    line (F21.13d: `ruff check exited 1` told the worker nothing).
    """
    ordered = sorted(files)
    if not ordered:
        return GateCheck(name="ruff", passed=True, detail="no files to lint")
    if TOOL_UNAVAILABLE in (lint_exit, format_exit):
        return GateCheck(
            name="ruff",
            passed=False,
            detail="ruff unavailable: the gate tool could not be launched",
        )
    inherited_note = f"; inherited: {inherited}" if inherited else ""
    if introduced:
        named = ", ".join(
            f"{f.path}:{f.row} {f.code} {f.message}" for f in introduced[:RUFF_NAMED_FINDINGS]
        )
        more = len(introduced) - RUFF_NAMED_FINDINGS
        suffix = f" (+{more} more)" if more > 0 else ""
        return GateCheck(
            name="ruff",
            passed=False,
            detail=f"introduced {len(introduced)} finding(s): {named}{suffix}{inherited_note}",
        )
    if lint_exit != 0 and not inherited:
        # Nonzero with nothing parsed is the tool failing, not a verdict.
        return GateCheck(
            name="ruff",
            passed=False,
            detail=f"ruff check exited {lint_exit} with no findings parsed{inherited_note}",
        )
    if format_exit != 0:
        return GateCheck(
            name="ruff",
            passed=False,
            detail=f"ruff format --check exited {format_exit}{inherited_note}",
        )
    return GateCheck(
        name="ruff", passed=True, detail=f"{len(ordered)} file(s) clean{inherited_note}"
    )


def _failing_test_count(output: str) -> int:
    """Failing tests as pytest's summary line counts them (`3 failed, 1 passed`)."""
    match = re.search(r"(\d+) failed", output)
    return int(match.group(1)) if match else 0


def _missing_module(output: str) -> str | None:
    """Top-level name of the module an import error says is absent, if any."""
    match = re.search(r"No module named '([\w.]+)'", output)
    return match.group(1).split(".")[0] if match else None


def _red_specification(
    test_command: str, exit_code: int, output: str, workdir_modules: Collection[str]
) -> GateCheck:
    """The `test`-kind tests verdict: the node's tests must fail now (T3-7a).

    A test node writes the specification the impl node depending on it
    must satisfy, so a suite that passes against the current code
    specified nothing. Exit 1 with at least one failing test is red. A
    collection error is red only when it names a module the worktree
    does not have -- the greenfield spec whose module the impl node will
    create; one naming an existing module is a broken test, not a
    specification. Any other exit never ran the tests.
    """
    if exit_code == 0:
        return GateCheck(
            name="tests",
            passed=False,
            detail=f"{test_command!r} exited 0: tests already pass, nothing specified",
        )
    if exit_code == PYTEST_TESTS_FAILED:
        failing = _failing_test_count(output)
        if failing:
            return GateCheck(
                name="tests",
                passed=True,
                detail=f"red specification: {failing} failing test(s)",
            )
        return GateCheck(
            name="tests",
            passed=False,
            detail=f"{test_command!r} exited 1 but its output counts no failing test",
        )
    if exit_code == PYTEST_COLLECTION_ERROR:
        missing = _missing_module(output)
        if missing is not None and missing not in workdir_modules:
            return GateCheck(
                name="tests",
                passed=True,
                detail=f"red specification: module {missing!r} does not exist yet",
            )
        blamed = f"names existing module {missing!r}" if missing else "names no missing module"
        return GateCheck(
            name="tests",
            passed=False,
            detail=f"{test_command!r} collection error {blamed}: broken, not a specification",
        )
    return GateCheck(
        name="tests",
        passed=False,
        detail=f"{test_command!r} exited {exit_code}: tests never ran",
    )


def check_test_command(
    test_command: str,
    run: Callable[[str], int],
    *,
    kind: str = "impl",
    output: str = "",
    workdir_modules: Collection[str] = (),
) -> GateCheck:
    """Run the node's declared pytest scope; nonzero exit fails the gate.

    A timeout is reported as a hang rather than as an exit code. The two
    are different defects: a failing assertion names the behaviour it
    disagrees with, while a suite that never terminates yields no verdict
    at all -- downstream harnesses that parse pytest counts read partial
    or zero results, so a hang scores worse than the failure it hides.

    A `test` node is graded the other way round (`_red_specification`):
    `output` is the run's captured text and `workdir_modules` the
    worktree's importable top-level names, both supplied by the runner so
    the predicate stays subprocess-free.
    """
    exit_code = run(test_command)
    if exit_code == TOOL_UNAVAILABLE:
        return GateCheck(
            name="tests",
            passed=False,
            detail=f"{test_command!r} unavailable: the gate tool could not be launched",
        )
    if exit_code == SHELL_TIMEOUT:
        return GateCheck(
            name="tests",
            passed=False,
            detail=f"{test_command!r} hangs: no verdict within the time limit",
        )
    if kind == "test":
        return _red_specification(test_command, exit_code, output, workdir_modules)
    if exit_code != 0:
        return GateCheck(
            name="tests",
            passed=False,
            detail=f"{test_command!r} exited {exit_code}",
        )
    return GateCheck(name="tests", passed=True, detail=f"{test_command!r} exited 0")


def _definition_lines(source: str, wanted: Collection[str]) -> dict[str, tuple[set[int], set[int]]]:
    """Each definition in `wanted` to (its whole lines, its body lines).

    Per definition, not one flat set, because the exemption is decided a
    whole definition at a time: a definition some test reaches is not
    exempt at all, and that question cannot be asked of a loose line.

    The two sets differ where it matters. The `def` line and decorators
    execute at import, so coverage records them for every definition in
    a module anything imports -- asking "did a test reach this" of the
    whole span answers yes always, and the exemption would never apply.
    Reachability is a question about the BODY.
    """
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return {}
    lines: dict[str, tuple[set[int], set[int]]] = {}

    def span(name: str, node: ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef) -> None:
        first = min([node.lineno, *(d.lineno for d in node.decorator_list)])
        last = node.end_lineno or node.lineno
        body = node.body[0].lineno if node.body else last + 1
        lines[name] = (set(range(first, last + 1)), set(range(body, last + 1)))

    for statement in tree.body:
        if isinstance(statement, ast.FunctionDef | ast.AsyncFunctionDef):
            if statement.name in wanted:
                span(statement.name, statement)
        elif isinstance(statement, ast.ClassDef):
            for child in statement.body:
                if not isinstance(child, ast.FunctionDef | ast.AsyncFunctionDef):
                    continue
                qualified = f"{statement.name}.{child.name}"
                if qualified in wanted:
                    span(qualified, child)
    return lines


def compelled_lines(
    baseline_sources: Mapping[str, str],
    sources: Mapping[str, str],
    prefix: str = "",
    covered: Collection[tuple[str, int]] = (),
) -> set[tuple[str, int]]:
    """Lines the node had no choice about: definitions `public-deletions` compels.

    `check_public_deletions` refuses to let a node drop a public
    definition its baseline had. Nothing guarantees a test reaches one.
    In `g1-cw100k/t5-s1` the t5 baseline carries `Account.to_dict`,
    `from_dict`, `__eq__` and `__repr__` that **nothing** calls -- not
    the rest of the baseline, not the four visible test files, not the
    five hidden ones -- so `impl-money-core` had to rewrite them for
    multi-currency, and coverage then failed it on exactly those lines.
    Attempts 1 and 3 kept them and failed coverage; attempt 2 deleted
    them and failed public-deletions; the node was unprovable and took
    the run's two remaining nodes with it (T6-75).

    So what one gate compels, another must not punish. The exemption is
    that narrow on purpose: it covers only definitions the BASELINE
    already had, never a definition the node invents, and never a line
    outside one. What it admits, stated plainly: a node may pad the body
    of a baseline definition with code no test runs. `dead-code` and
    `mutation` still read those lines, and the node does not choose
    which members its baseline carries.

    `covered` narrows it further, and must (T6-86). A definition whose
    BODY any test reaches is not exempt at all: it is judged line by
    line, exactly as before T6-75. Without that, the exemption swallows
    the gate for the commonest node shape there is -- an impl node
    editing a public function its baseline already had -- because every
    changed line then leaves the denominator, `judged` empties, and
    coverage returns pass having measured nothing. Eleven `run_slice`
    tests flipped from "every changed line runs" to that pass the moment
    the exemption began to fire, which is what a hollow gate looks like
    from the outside. The exemption is for the case T6-75 actually
    described -- a public definition NOTHING calls, which the node may
    not delete and cannot cover -- and reachability is what separates
    the two.

    `prefix` is the workdir the caller's `changed` set is keyed against.
    `baseline_sources` is `read_sources`, which is workdir-RELATIVE,
    while `runner.py` builds `changed` as `(str(workdir / path), line)`
    -- absolute, because `mutation_sample` and `covered_lines` need it
    that way. `check_changed_line_coverage` intersects the two, so
    without the prefix the intersection is empty and this exemption
    fires for nothing: it fired zero times in 48 runs, and `g1-79cd848`
    reproduced T6-75's own trap on a tree carrying T6-75's fix (T6-86).
    """
    reached = set(covered)
    compelled: set[tuple[str, int]] = set()
    for rel, text in baseline_sources.items():
        before = _public_definitions(text)
        if not before:
            continue
        key = str(PurePath(prefix) / rel) if prefix else rel
        for whole, body in _definition_lines(sources.get(rel, ""), before).values():
            if {(key, line) for line in body} & reached:
                continue
            compelled |= {(key, line) for line in whole}
    return compelled


def check_changed_line_coverage(
    changed: set[tuple[str, int]],
    covered: set[tuple[str, int]],
    minimum: float,
    owed: Collection[str] = (),
    compelled: Collection[tuple[str, int]] = (),
    writable: bool = True,
) -> GateCheck:
    """Every changed line must be executed; `minimum` is the node threshold.

    `detail` is routed to the worker by `format_attempt_failure`, so it
    names the lines no test runs and carries no ratio (T6-44). A
    percentage is satisfiable by a call that runs the line and asserts
    nothing; the lines themselves are the evidence. The counts stay in
    `basis`, which is sealed rather than worker-facing.

    `owed` is the nodes the plan still expects tests from (T6-53). When
    it is non-empty the uncovered lines are **deferred** rather than
    failed: the node seals, and `basis` records what was set aside. An
    `impl` node may not write tests, so with a test node still owed the
    gate is asking a question whose answer cannot exist yet, and round
    3g shows what that costs -- `n2.r1` was failed for eleven lines on a
    tree that passes 16 of 16 hidden accounts-and-fees tests, because
    the record shape it had to write is exercised by a test file
    belonging to a node that had not run.

    `compelled` is the lines `public-deletions` will not let the node
    drop (T6-75). They are removed from the judgement entirely, because
    failing a node for not covering code it was forbidden to delete asks
    it for a diff that does not exist -- see `compelled_lines`.

    Deferral does not fail the run later. A line still uncovered when
    the DAG drains is uncovered against the arm's own suite, and the
    hidden suite that decides the task is a different one, so failing on
    it would fail runs whose artifact is correct. What this admits is
    stated plainly: a node may add code nothing ever runs, and seal.
    Coverage keeps full force on every line the node could have covered
    -- with no test node owed, the check is exactly what it was.
    """
    if not changed:
        return GateCheck(
            name="coverage", passed=True, detail="no changed lines", basis="changed-lines=0"
        )
    # Lines `public-deletions` compels are not judged here at all -- they
    # leave the denominator, not just the shortfall, or the percentage
    # sinks the node for code it was required to carry (T6-75).
    spared = changed & set(compelled)
    # Only recorded when it happened: a "compelled-lines=0" on every
    # sealed node would churn every existing record to say nothing.
    note = f" compelled-lines={len(spared)}" if spared else ""
    judged = changed - spared
    if not judged:
        return GateCheck(
            name="coverage",
            passed=True,
            detail="every changed line is inside a definition the baseline already had",
            basis=f"changed-lines={len(changed)}{note}",
        )
    missing = sorted(judged - covered)
    percent = (len(judged) - len(missing)) / len(judged) * 100.0
    if percent < minimum:
        gaps = ", ".join(f"{path}:{line}" for path, line in missing)
        if owed:
            return GateCheck(
                name="coverage",
                passed=True,
                detail=f"deferred, no test node has run that can reach {gaps}",
                basis=(
                    f"changed-lines={len(changed)} deferred-lines={len(missing)}"
                    f"{note} owed={','.join(sorted(owed))}"
                ),
            )
        if not writable:
            # T6-53 asked whether the SCHEDULE could still cover the
            # line and deferred when it could. It never asked the
            # other half: whether THIS node could, and an `impl` node
            # never can. With no test node owed the two answers
            # diverge, which is the commonest plan the planner draws,
            # and the node's only way to green is to delete the branch
            # the task requires -- a pass the oracle then fails, which
            # is Goal G1's own definition of a gate defect (F21.66).
            return GateCheck(
                name="coverage",
                passed=True,
                detail=f"deferred, no node that may write a test remains to reach {gaps}",
                basis=(f"changed-lines={len(changed)} unreachable-lines={len(missing)}{note}"),
            )
        return GateCheck(
            name="coverage",
            passed=False,
            detail=f"no test runs {gaps}",
            basis=f"changed-lines={len(changed)}{note}",
        )
    return GateCheck(
        name="coverage",
        passed=True,
        detail="every changed line runs",
        basis=f"changed-lines={len(changed)}{note}",
    )


def _identifiers(node: ast.AST) -> set[str]:
    """Every name `node` could be referring to, spelled any way it could be.

    Bare names, attribute tails, imported names and *string constants*, so
    `__all__`, a `getattr` and a pytest marker all count as mentions. The
    set is deliberately over-wide: it decides what is NOT dead, and a name
    this misses is a node failed for code that something does use.
    """
    names: set[str] = set()
    for child in ast.walk(node):
        if isinstance(child, ast.Name):
            names.add(child.id)
        elif isinstance(child, ast.Attribute):
            names.add(child.attr)
        elif isinstance(child, ast.alias):
            names.add(child.name.split(".")[0])
            names.add(child.name.rsplit(".", 1)[-1])
        elif isinstance(child, ast.Constant) and isinstance(child.value, str):
            names.add(child.value)
    return names


def _statement_span(statement: ast.stmt) -> tuple[int, int]:
    """First and last source line of `statement`, decorators included."""
    decorators = getattr(statement, "decorator_list", [])
    start = min([statement.lineno, *(node.lineno for node in decorators)])
    return start, statement.end_lineno or statement.lineno


def _without_dead_additions(
    source: str, added: Collection[int], elsewhere: set[str]
) -> tuple[dict[str, int], str]:
    """The definitions `source` adds that nothing mentions, and `source` without them.

    A top-level definition is the node's own when every statement line it
    spans is in `added`. It is dead when its name appears nowhere in
    `elsewhere` -- no other module, no test, and no line of this module the
    node did not write. Only private names are candidates: a public one is
    the module's surface, and the node that writes the tests naming it may
    not have run yet -- round 3e's `fee_for` is new, public, and mentioned
    by nothing in the tree it was gated in. Statements that mention a dead name go with it, so
    a call that exists only to run a dead body is removed alongside its
    definition; the remaining lines are untouched, not reformatted.

    The count is how many times the name is defined: a definition repeated
    sixty times is one name and sixty copies, and the reader needs both.
    """
    try:
        tree = ast.parse(source)
    except SyntaxError:  # the syntax gate owns this; say nothing about it here
        return {}, source
    lines = set(added)
    spans = [
        (statement, {node.lineno for node in ast.walk(statement) if isinstance(node, ast.stmt)})
        for statement in tree.body
    ]
    definitions = [
        statement
        for statement, span in spans
        if span <= lines
        and isinstance(statement, ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef)
        and statement.name.startswith("_")
        and not statement.name.startswith("__")
    ]
    mentioned = set(elsewhere)
    for statement, span in spans:
        if not span <= lines:
            mentioned |= _identifiers(statement)
    dead: dict[str, int] = {}
    for statement in definitions:
        if statement.name not in mentioned:
            dead[statement.name] = dead.get(statement.name, 0) + 1
    if not dead:
        return {}, source
    cut: set[int] = set()
    for statement, span in spans:
        if not span <= lines:
            continue
        names = _identifiers(statement)
        if isinstance(statement, ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef):
            names = names | {statement.name}
        if names & set(dead):
            start, end = _statement_span(statement)
            cut |= set(range(start, end + 1))
    kept = [
        line for number, line in enumerate(source.splitlines(keepends=True), 1) if number not in cut
    ]
    return dead, "".join(kept)


def _is_public(name: str) -> bool:
    """A dunder is public API; a single or double underscore prefix is not."""
    if name.startswith("__") and name.endswith("__"):
        return True
    return not name.startswith("_")


def _public_definitions(source: str) -> set[str] | None:
    """Public top-level definitions and the public methods of public classes.

    `None` when the source does not parse, so the syntax gate owns that
    and this check reports nothing. Methods are included because the
    deletion this gate exists to reject was four of them.
    """
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return None
    names: set[str] = set()
    for statement in tree.body:
        if isinstance(statement, ast.FunctionDef | ast.AsyncFunctionDef):
            if _is_public(statement.name):
                names.add(statement.name)
        elif isinstance(statement, ast.ClassDef):
            if not _is_public(statement.name):
                continue
            names.add(statement.name)
            for child in statement.body:
                if isinstance(child, ast.FunctionDef | ast.AsyncFunctionDef) and _is_public(
                    child.name
                ):
                    names.add(f"{statement.name}.{child.name}")
    return names


def check_public_deletions(
    baseline_sources: Mapping[str, str], sources: Mapping[str, str]
) -> GateCheck:
    """A node may not delete a public definition its baseline had (T6-42).

    Round 3d's n2 attempt 2 is the first draw in either round whose
    reasoning names the gates -- mutation 26 times, coverage 14, against
    zero for every first attempt -- and what the repair brief produced
    was a plan to delete: "to fix coverage, I need to either: 1. Remove
    the uncovered code (to_dict, from_dict, `__eq__`, `__repr__` ...)",
    and "the simplified transfer will have fewer mutation sites". That is
    the `max_mutants=1` exploit (#50) re-derived from the brief, because
    both rates rise when the denominator falls. `store.py` needs
    `to_dict`/`from_dict`, so the repair proposed deleting required
    behaviour to raise two metrics.

    The check is a set difference, not a threshold: there is no number to
    optimise, and a deletion either happened or did not. It compares the
    tree against the node's baseline rather than the diff text, so a
    node that rewrites a module wholesale passes as long as the
    definitions come back -- which is what both round 3e draws do.
    """
    gone: dict[str, list[str]] = {}
    for rel, text in baseline_sources.items():
        before = _public_definitions(text)
        after = _public_definitions(sources.get(rel, ""))
        if before is None or after is None or not before:
            continue
        missing = sorted(before - after)
        if missing:
            gone[rel] = missing
    if not gone:
        return GateCheck(
            name="public-deletions",
            passed=True,
            detail="every public definition the baseline had is still defined",
            basis=f"baseline-modules={len(baseline_sources)}",
        )
    shown = "; ".join(
        f"{rel} no longer defines {', '.join(names)}" for rel, names in sorted(gone.items())
    )
    return GateCheck(
        name="public-deletions",
        passed=False,
        detail=f"{shown}; other modules and later nodes still expect them",
        basis=f"deleted-public={sum(len(names) for names in gone.values())}",
    )


# Verbs a recovery plan uses to propose taking code out, and the words that
# turn one into its opposite. Both are read within a clause, never across
# one: a removal verb governs the names standing with it, so "remove the
# `_helper` and inline its body into `api`" removes only the helper, and a
# negator turns around only the verb it shares a clause with, so "the
# failure is not in fees.py; remove `to_dict`" keeps its removal.
_REMOVAL_VERBS: Final = (
    "remove",
    "removing",
    "delete",
    "deleting",
    "drop",
    "dropping",
    "strip",
    "stripping",
    "eliminate",
    "eliminating",
)
_NEGATORS: Final = (
    "not",
    "never",
    "avoid",
    "without",
    "cannot",
    "can't",
    "don't",
    "doesn't",
    "shouldn't",
    "mustn't",
    "than",
    "of",
)
_REMOVAL_RE: Final = re.compile(r"\b(?:" + "|".join(_REMOVAL_VERBS) + r")\b", re.IGNORECASE)
# What separates one instruction from the next inside a single line.
_CLAUSE_RE: Final = re.compile(r";|\band\b|\bthen\b", re.IGNORECASE)


def plan_prescribes_deletion(plan: str, baseline_sources: Mapping[str, str]) -> str | None:
    """A recovery plan instruction that takes out public API (T6-54).

    Returns the offending line, or `None` when the plan is safe to route.

    Round 3g's `n2.r1` failed `coverage` on eleven lines, and the
    harness's own diagnosis step answered with "In `accounts.py`, remove
    the `to_dict()`, `from_dict()`, `__eq__`, and `__repr__` methods (no
    test exercises them)" -- round 3d's behaviour, prescribed to the
    worker by the harness. `check_public_deletions` would have rejected
    the diff that followed it, so the instruction cost an attempt that
    could not have succeeded: 1871 s and 149 151 output tokens ending
    `length` with no diff.

    The judgement is the same set `check_public_deletions` computes, read
    against the plan's text instead of against a tree, so no model
    decides whether a plan is good advice. A line offends when it pairs
    an un-negated removal verb with a name the baseline defines publicly;
    a plan that merely names those members, or that proposes removing a
    private helper, routes unchanged.
    """
    public: set[str] = set()
    for rel, text in baseline_sources.items():
        if _is_test_file(rel):
            # A test's own helpers are not the API later nodes expect, and
            # a test node may legitimately be told to drop a test it wrote.
            continue
        names = _public_definitions(text)
        if names is None:
            continue
        for name in names:
            public.add(name)
            # `Class.method` is how the gate spells a method and "remove
            # the `to_dict()` methods" is how a plan does.
            public.add(name.rpartition(".")[2])
    if not public:
        return None
    named = re.compile(r"\b(?:" + "|".join(re.escape(name) for name in sorted(public)) + r")\b")
    for line in plan.splitlines():
        if not named.search(line):
            continue
        for clause in _CLAUSE_RE.split(line):
            match = _REMOVAL_RE.search(clause)
            if match is None or not named.search(clause[match.end() :]):
                continue
            before = clause[: match.start()].lower().split()
            if not any(word.strip(".,;:()`\"'") in _NEGATORS for word in before):
                return line.strip()
    return None


# A requirement may not restate the gate (T6-58). An offending clause
# pairs a change-word with a line-word under an execution or coverage
# verb -- "every changed line executed by tests/test_accounts.py" -- or
# names a coverage ratio. Three word classes rather than one phrase,
# because the two frozen known-bads spell it differently ("all changed
# lines covered by", "every changed line executed by") and the planner
# bullet that taught it spells it a third way ("every line you change
# must be executed by a test").
_SENTENCE_RE: Final = re.compile(r"(?<=[.;])\s+")
_CHANGED_RE: Final = re.compile(
    r"\b(?:changed|added|new|modified|touched|you\s+change|we\s+change)\b", re.IGNORECASE
)
_LINE_RE: Final = re.compile(
    r"\b(?:line|lines|statement|statements|branch|branches)\b", re.IGNORECASE
)
_EXECUTED_RE: Final = re.compile(
    r"\b(?:cover|covers|covered|covering|coverage|execut\w*|exercis\w*|hit|reached"
    r"|run\s+by|tested\s+by)\b",
    re.IGNORECASE,
)
_RATIO_RE: Final = re.compile(r"\b\d{1,3}(?:\.\d+)?\s*%")


def plan_retargets_reserved_files(nodes: Sequence[Node], reserved: Collection[str]) -> str | None:
    """A subplan node declaring a file a still-pending node owns (T6-65).

    Returns the offence, or `None` when the subplan stays out of the way.
    `reserved` comes from `reserved_target_files`, which already excludes
    the node being replaced and anything proven, so every path here is
    work some other node is still expected to do.

    This is refused at emission and REDRAWN rather than spliced and
    reconciled later, for the reason round 3i demonstrates: by the time
    the duplicate has sealed, the original node's work is gone and there
    is nothing left for it to prove. Its gates then fire correctly on an
    empty change (`tests pass pre-change; prove nothing`) and the run
    dies of a plan nobody can see. Refusing costs one redraw; the message
    names the files, and `_emit_valid_dag` feeds it back to the planner,
    which is also the only way the planner ever learns a sibling exists.
    """
    owned = set(reserved)
    for node in nodes:
        clash = sorted(set(node.target_files) & owned)
        if clash:
            return f"node {node.id!r} declares {', '.join(clash)}"
    return None


def plan_restates_the_gate(nodes: Sequence[Node]) -> str | None:
    """A node description or requirement statement that restates a gate (T6-58).

    Returns the offending clause, or `None` when every node states
    behaviour. Goal G1 names the shape: a requirement satisfiable by a
    no-op. "Every changed line executed by tests/test_accounts.py" is
    satisfied by calling a function whose body is `pass`; "fee_for
    returns the fee for a positive amount below the fee" is not.

    T6-44 removed the execution proxy from saddle's own worker rule but
    not from the instruction that regenerates it, so it still reached the
    worker laundered through the plan. Round 3g's `n2.r1` prompt, whose
    run has `e52912b` (T6-44) as an ancestor, carries it twice -- in the
    node description and inside REQ-002, under the heading "each test
    must fail if its statement is violated". Both are frozen beside this
    as fixtures.

    The judgement is textual and keys on the CLAIM, never on the gate's
    test-file names: a test node's requirement legitimately says "when
    the accounts and fees tests run, the suite shall assert ...", and
    that is a statement about behaviour. Checked against every statement
    and description rounds 3e-3i retained: 29 pass, and the two frozen
    known-bads are the only ones caught.
    """
    for node in nodes:
        texts = [node.task_prompt, *(req.statement for req in node.requirements)]
        for text in texts:
            for clause in _SENTENCE_RE.split(text):
                if _EXECUTED_RE.search(clause) is None:
                    continue
                if (_CHANGED_RE.search(clause) and _LINE_RE.search(clause)) or _RATIO_RE.search(
                    clause
                ):
                    return clause.strip()
    return None


def check_dead_additions(
    sources: Mapping[str, str],
    added: Mapping[str, Collection[int]],
    *,
    suite_passed: bool,
    run_without: Callable[[Mapping[str, str]], int],
) -> GateCheck:
    """Code nothing depends on is not an implementation (T6-41).

    Round 3e's n2 attempt 1 emitted a 21-line block sixty times, taking
    `fees.py` from 41 lines to 1337, and nine of eleven gates passed it --
    mutation included, at 88.8% over 80 mutants, because a `pass` body
    admits no mutant and so never enters the population, while importing
    the module executes it and satisfies coverage.

    Not a gaming story, and the record was corrected: that was attempt 1,
    which carries no failure brief, and its 47 586 characters of reasoning
    name no gate, no percentage and none of the emitted helpers. The
    repetition is an emission phenomenon -- the reasoning ends coherently
    and the duplication begins in the content tokens after it. The
    worker's prompt states the requirement behaviourally already ("every
    changed line must be executed by the new tests" -- the rule as it then
    stood; T6-44 has since replaced it with the mutation form) and that
    phrasing did not help, because execution is a proxy under any wording.
    The emitted docstrings paraphrase that very line, which is why it
    changed and why this check does not rely on the change.

    So this check does not read intent and does not count lines: it asks
    whether anything depends on what the node added, in `keep_candidate`'s
    image. The private definitions no line of the tree mentions are
    removed and the suite is run again; if it still passes, they carry
    nothing. The suite failing is the honest answer and passes the gate.
    A suite already red says nothing either way and the node fails on the
    tests check instead.

    The general form of the defect is that a line counts as exercised when
    a test fails if its behaviour changes, not when it runs; this check
    reaches one shape of that and T6-42 carries the rest.
    """
    if not suite_passed:
        return GateCheck(
            name="dead-code",
            passed=True,
            detail="the suite is already failing; removing anything proves nothing",
            basis="suite=red",
        )
    edited: dict[str, str] = {}
    dead: dict[str, dict[str, int]] = {}
    for path in sorted(added):
        source = sources.get(path)
        if source is None:
            continue
        elsewhere: set[str] = set()
        for other, text in sources.items():
            if other == path:
                continue
            try:
                elsewhere |= _identifiers(ast.parse(text))
            except SyntaxError:
                continue
        found, rest = _without_dead_additions(source, added[path], elsewhere)
        if found:
            dead[path] = found
            edited[path] = rest
    if not dead:
        return GateCheck(
            name="dead-code",
            passed=True,
            detail="every private definition added is mentioned elsewhere in the tree",
            basis=f"modules={len(added)}",
        )
    listing = "; ".join(
        f"{path} adds "
        + ", ".join(
            name if count == 1 else f"{name} ({count} copies)"
            for name, count in sorted(found.items())
        )
        for path, found in sorted(dead.items())
    )
    if run_without(edited) != 0:
        return GateCheck(
            name="dead-code",
            passed=True,
            detail=f"{listing}; the suite fails without them, so they carry the work",
            basis=f"dead-candidates={sum(len(f) for f in dead.values())}",
        )
    return GateCheck(
        name="dead-code",
        passed=False,
        detail=(
            f"{listing}, which nothing else in the tree mentions; the suite still "
            f"passes with them removed, so they implement no requirement"
        ),
        basis=f"dead-definitions={sum(len(f) for f in dead.values())}",
    )


def _check_behaviour_preserved(coverage: GateCheck, mutation: MutationOutcome) -> GateCheck:
    """Red-phase stand-in for a node whose diff changes no test.

    Coverage alone would pass on tests that touch the changed lines
    without pinning them, so the mutation floor is fixed here rather than
    taken from the node: a planner-chosen `kill_threshold` of 0 would
    otherwise reopen the waiver this gate exists to close.
    """
    if not coverage.passed:
        return GateCheck(
            name="red-phase",
            passed=False,
            detail="tests unchanged and coverage failed; nothing proves the change",
        )
    if mutation.total == 0:
        return GateCheck(
            name="red-phase",
            passed=False,
            detail="tests unchanged and no mutants decided; nothing proves the change",
        )
    percent = 100.0 * mutation.killed / mutation.total
    if percent < REFACTOR_KILL_FLOOR:
        return GateCheck(
            name="red-phase",
            passed=False,
            detail=(
                f"tests unchanged and mutation {percent:.1f}% < "
                f"{REFACTOR_KILL_FLOOR:.1f}%; nothing proves the change"
            ),
        )
    return GateCheck(
        name="red-phase",
        passed=True,
        detail=(
            f"tests unchanged (behaviour preserved); coverage and "
            f"mutation {percent:.1f}% carry the proof"
        ),
    )


def check_red_phase(
    baseline_exits: Sequence[int],
    run_current: Callable[[], int],
    *,
    baseline_output: str,
    changed_files: Collection[str],
    tests_changed: bool,
    kind: str,
    coverage: GateCheck,
    mutation: MutationOutcome,
    red_spec: GateCheck | None = None,
) -> GateCheck:
    """New tests must fail pre-change for a reason the change explains.

    The baseline leg runs the node's own test sources against pre-change
    code, so exit 1 is a genuine assertion failure. A collection error
    (exit 2) counts only when it names a source file the node changed --
    the greenfield case, where the module under test does not exist yet.
    Any other nonzero exit (missing files, usage errors, no tests
    collected) says nothing about the new tests and fails the gate. A
    baseline that hangs is called out separately: it fails like the rest,
    but "tests never ran" would send recovery hunting a missing file
    instead of a loop.

    `baseline_exits` carries RED_PHASE_SAMPLES observations rather than
    one. They must agree: a test that fails on one pre-change run and
    passes on the next yields "fail pre-change, pass post-change" with no
    causal relation to the diff, which is a vacuous red that looks exactly
    like a genuine one.

    The planner cannot waive this: whether it binds is read off the diff.
    A node that leaves every test AST untouched preserved behaviour by
    construction, so no test can fail pre-change; there the proof falls to
    changed-line coverage plus a hard mutation floor, which a tautological
    refactor cannot clear either.
    """
    # A test node has no differential: its tests are the specification
    # and they must fail now, which the tests check already observed, so
    # red-phase mirrors that verdict (`red_spec`) and has no baseline leg
    # (T3-7a). The impl node that depends on it takes the real differential.
    if kind == "test":
        if red_spec is not None and red_spec.passed:
            return GateCheck(
                name="red-phase",
                passed=True,
                detail="red by construction: the specification fails now",
            )
        why = red_spec.detail if red_spec is not None else "no tests verdict to mirror"
        return GateCheck(name="red-phase", passed=False, detail=f"specification is not red: {why}")
    # Only a refactor is behaviour-preserving by construction. An impl
    # node also changes no tests, but its tests were written by the test
    # node it depends on and already fail at its baseline, so it takes the
    # real differential -- grading a behaviour change on a refactor's
    # evidence is how T4 passed while fixing the wrong module.
    if not tests_changed and kind == "refactor":
        return _check_behaviour_preserved(coverage, mutation)
    if len(set(baseline_exits)) > 1:
        seen = ", ".join(str(code) for code in baseline_exits)
        return GateCheck(
            name="red-phase",
            passed=False,
            detail=f"baseline nondeterministic across {len(baseline_exits)} runs "
            f"(exits {seen}); prove nothing",
        )
    baseline_exit = baseline_exits[0]
    if baseline_exit == 0:
        return GateCheck(
            name="red-phase", passed=False, detail="tests pass pre-change; prove nothing"
        )
    if baseline_exit == SHELL_TIMEOUT:
        return GateCheck(
            name="red-phase",
            passed=False,
            detail="baseline hangs: no pre-change verdict; prove nothing",
        )
    if baseline_exit == PYTEST_COLLECTION_ERROR:
        if not _names_changed_source(baseline_output, changed_files):
            return GateCheck(
                name="red-phase",
                passed=False,
                detail="baseline collection error names no changed source; prove nothing",
            )
    elif baseline_exit != PYTEST_TESTS_FAILED:
        return GateCheck(
            name="red-phase",
            passed=False,
            detail=f"baseline exit {baseline_exit}: tests never ran; prove nothing",
        )
    if run_current() != 0:
        return GateCheck(name="red-phase", passed=False, detail="tests fail post-change")
    return GateCheck(name="red-phase", passed=True, detail="fail pre-change, pass post-change")


def _has_property(source: str) -> bool:
    """True when a test module drives at least one hypothesis property."""
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return False
    for node in ast.walk(tree):
        if not isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef):
            continue
        if any(_decorator_name(d) == "given" for d in node.decorator_list):
            return True
    return False


def _is_negative_assert(stmt: ast.Assert) -> bool:
    """`assert not f(x)`, `assert f(x) is False`, `assert f(x) == False`."""
    test = stmt.test
    if isinstance(test, ast.UnaryOp) and isinstance(test.op, ast.Not):
        return True
    if isinstance(test, ast.Compare) and len(test.ops) == 1:
        (op,) = test.ops
        (right,) = test.comparators
        return (
            isinstance(op, ast.Is | ast.Eq)
            and isinstance(right, ast.Constant)
            and right.value is False
        )
    return False


def _raises(stmt: ast.With) -> bool:
    for item in stmt.items:
        call = item.context_expr
        func = call.func if isinstance(call, ast.Call) else call
        name = func.attr if isinstance(func, ast.Attribute) else getattr(func, "id", "")
        if name == "raises":
            return True
    return False


def _rejects_an_input(source: str) -> bool:
    """True when some `@given` property in the module rejects an input (T6-5).

    A property is negative when it asserts `not f(x)`, `f(x) is False`,
    `f(x) == False`, or runs under `pytest.raises`. Everything else is
    positive: it can only say what the code accepts, so a validator that
    accepts everything satisfies it. Called only on sources `_has_property`
    already parsed.
    """
    for node in ast.walk(ast.parse(source)):
        if not isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef):
            continue
        if not any(_decorator_name(d) == "given" for d in node.decorator_list):
            continue
        for stmt in ast.walk(node):
            if isinstance(stmt, ast.Assert) and _is_negative_assert(stmt):
                return True
            if isinstance(stmt, ast.With) and _raises(stmt):
                return True
    return False


def _decorator_name(decorator: ast.expr) -> str:
    call = decorator.func if isinstance(decorator, ast.Call) else decorator
    return call.attr if isinstance(call, ast.Attribute) else getattr(call, "id", "")


def check_target_files(target_files: Collection[str], touched_files: Collection[str]) -> GateCheck:
    """Opt-in localisation (#64): a node that names its files may not touch others.

    Empty `target_files` is unrestricted, so an emitting model that omits
    the field loses nothing; a declared list can only narrow the node's
    own scope, which is the one direction a planner-supplied value may
    move (CLAUDE.md: "A threshold the model itself supplies lets the model
    set its own bar" -- this one cannot lower it). T4's worker fixed the
    wrong module while passing every gate then in force; a node that had
    said which module would have been caught here.
    """
    if not target_files:
        return GateCheck(
            name="target-scope", passed=True, detail="unrestricted: no target_files declared"
        )
    allowed = set(target_files)
    stray = sorted(path for path in touched_files if path not in allowed)
    if stray:
        return GateCheck(
            name="target-scope",
            passed=False,
            detail=f"touched file(s) outside target_files: {', '.join(stray)}",
        )
    return GateCheck(
        name="target-scope",
        passed=True,
        detail=f"{len(touched_files)} touched file(s) within {len(target_files)} target(s)",
    )


def check_property_coverage(
    kind: str,
    test_sources: Mapping[str, str],
    *,
    oracle: MutationOutcome | None = None,
    targets: Collection[str] = (),
    out_of_scope: Collection[str] = (),
) -> GateCheck:
    """A test node must state a property; the impl node must show it bites.

    LLMs "generate ordinary programs following similar patterns seen in
    their massive training corpora, while fuzzing favors unusual inputs
    that cover edge cases". Two happy-path examples over an unbounded
    domain is the predicted output, and it is what T1 produced: a regex
    accepting `.u@example.com` and `user@example..com` behind 7/7 green
    gates (F1).

    Properties are invariants over generated inputs rather than pairs the
    author chose, so the cases they probe are not the cases the author
    already had in mind. A `test` node is bound by presence. An `impl`
    node is bound by the oracle (T3-3): `targets` are the property-bearing
    modules that import a changed module, and `oracle` is a mutation
    sample run with those modules alone as the test set; the property
    must kill at least one of the node's changed-line mutants, or it has
    no discriminating power over the code that implements it (F1's regex
    shipped behind a property that could not tell it from a correct one).
    No targets means no property claims this change and the check is not
    required; targets with no oracle means the runner did not run what it
    should have, which fails rather than passes. A refactor preserves the
    tests it moves and is not bound.

    Presence is floored by polarity (T6-5): at least one of the `test`
    node's properties must reject an input, because T1's single property
    was positive and a validator that accepts everything satisfied it.
    Known limit: a lazy negative generator passes this floor; behavioural
    mutation (T6-6) is the real defence.
    """
    if kind == "impl":
        return _check_property_oracle(oracle, tuple(targets), tuple(out_of_scope))
    if kind != "test":
        return GateCheck(name="property-coverage", passed=True, detail=f"{kind} node: not required")
    with_property = sorted(path for path, src in test_sources.items() if _has_property(src))
    if not with_property:
        return GateCheck(
            name="property-coverage",
            passed=False,
            detail="no hypothesis property in the node's tests: examples only",
        )
    if not any(_rejects_an_input(test_sources[path]) for path in with_property):
        return GateCheck(
            name="property-coverage",
            passed=False,
            detail=(
                f"{len(with_property)} module(s) drive a property, none rejects an input: "
                "a positive-only property cannot tell the code from one that accepts everything"
            ),
        )
    return GateCheck(
        name="property-coverage",
        passed=True,
        detail=f"{len(with_property)} module(s) drive a property, one rejects an input",
    )


def _check_property_oracle(
    oracle: MutationOutcome | None,
    targets: tuple[str, ...],
    out_of_scope: tuple[str, ...] = (),
) -> GateCheck:
    name = "property-coverage"
    if not targets:
        # A pass over an empty judgement set records that it judged
        # nothing, and which empty it is: no property claims this change
        # at all, or the node's own scope excludes the ones that do --
        # what F21.65's narrowing now admits, said out loud rather than
        # left as a silent pass.
        if out_of_scope:
            outside = ", ".join(out_of_scope)
            return GateCheck(
                name=name,
                passed=True,
                detail=(
                    "not required: no property module in this node's test scope "
                    f"({outside} outside it)"
                ),
                basis=f"oracle: not run, 0 of {len(out_of_scope)} property module(s) in scope",
            )
        return GateCheck(
            name=name,
            passed=True,
            detail="not required: no property targets this change",
            basis="oracle: not run, no property module names a changed file",
        )
    by = ", ".join(targets)
    if oracle is None:
        return GateCheck(name=name, passed=False, detail=f"property oracle did not run for {by}")
    if oracle.total == 0:
        # The engine failing and the engine finding nothing are different
        # facts, and `survivors` carried the first one all along while
        # this check reported only the second (F21.65). The mutation gate
        # names its tool failures; so does this one now.
        cause = f": {oracle.survivors[0]}" if oracle.survivors else ""
        return GateCheck(
            name=name,
            passed=False,
            detail=f"no mutants sampled for the property oracle ({by}){cause}",
        )
    if oracle.killed == 0:
        return GateCheck(
            name=name,
            passed=False,
            detail=f"property killed 0 of {oracle.total} mutant(s): no discriminating power ({by})",
        )
    return GateCheck(
        name=name,
        passed=True,
        detail=f"property killed {oracle.killed} of {oracle.total} mutant(s)",
        basis=f"oracle: killed {oracle.killed} of {oracle.total} mutant(s) by {by}",
    )


def _assertions_by_test(sources: Mapping[str, str]) -> dict[str, set[str]]:
    """Assertion ASTs per test function, keyed by function name.

    Keyed by name rather than by file so relocating a test during a
    refactor is not mistaken for rewriting it.
    """
    found: dict[str, set[str]] = {}
    for source in sources.values():
        try:
            tree = ast.parse(source)
        except SyntaxError:
            continue
        for node in ast.walk(tree):
            if not isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef):
                continue
            if not node.name.startswith("test"):
                continue
            asserts = {ast.dump(stmt) for stmt in ast.walk(node) if isinstance(stmt, ast.Assert)}
            found.setdefault(node.name, set()).update(asserts)
    return found


def check_assertion_preservation(
    kind: str, baseline_tests: Mapping[str, str], current_tests: Mapping[str, str]
) -> GateCheck:
    """Assertions in pre-existing tests are append-only, except for test nodes.

    T4's worker fixed the wrong module and rewrote the behaviour-pinning
    test to match -- `3.03` became `3.02` -- and every Tier-1 gate then in
    force passed (#44). #57 stops an impl node touching tests at all, but a refactor
    may carry code and tests together, and behaviour-preserving means the
    assertions survive the move.

    A `test` node is exempt because repairing stale assertions is its job
    (T2's task was exactly that), and #57 already stops it shipping the
    implementation alongside. That is the whole reason this needs no
    planner-set waiver: the exemption is structural, not chosen by the
    party being graded.
    """
    if kind == "test":
        return GateCheck(
            name="assertion-preservation", passed=True, detail="test node: may restate assertions"
        )
    before = _assertions_by_test(baseline_tests)
    after = _assertions_by_test(current_tests)
    dropped = sorted(name for name, asserts in before.items() if asserts - after.get(name, set()))
    if dropped:
        return GateCheck(
            name="assertion-preservation",
            passed=False,
            detail=f"{kind} node rewrote assertions in: {', '.join(dropped)}",
        )
    return GateCheck(
        name="assertion-preservation",
        passed=True,
        detail=f"{len(before)} pre-existing test(s) keep their assertions",
    )


def check_node_scope(
    kind: str,
    changed_files: Collection[str],
    added_files: Collection[str] = (),
    *,
    may_create: bool,
) -> GateCheck:
    """A node stays on its own side of the test/implementation split, and
    creates files only if it declared `write_file`.

    F5 and #44 are one defect: the worker authors the implementation and
    the tests, so a misreading of the contract is encoded twice and the
    suite it is graded by is the suite it just rewrote. T4's worker fixed
    the wrong module, rewrote the behaviour-pinning test to match, and
    passed every Tier-1 gate then in force.

    An `impl` node may not edit tests; a `test` node may not ship the
    implementation. `refactor` may edit both sides -- a behaviour-preserving
    move carries code and its tests together, and splitting it would leave
    the first node red with nothing able to fix it -- but it may not create
    a file: that is the `impl`/`test` split done under the exempt name (#65).

    `may_create` is the `write_file` binding (T3-4) and it binds every
    kind, ahead of the kind branches: `allowed_tools` was validated
    against the global allowlist and then consumed by nothing, so a plan
    that withheld `write_file` still got a node that could add whatever it
    liked. The probe is T2-2's staged adds, already collected; no new
    subprocess runs for this.
    """
    if added_files and not may_create:
        return GateCheck(
            name="node-scope",
            passed=False,
            detail=(
                "node may not create files: write_file not in allowed_tools; "
                f"added {', '.join(sorted(added_files))}"
            ),
        )
    if kind == "refactor":
        if added_files:
            return GateCheck(
                name="node-scope",
                passed=False,
                detail=f"refactor node added file(s): {', '.join(sorted(added_files))}",
            )
        return GateCheck(
            name="node-scope", passed=True, detail="refactor: edits both sides, adds nothing"
        )
    tests = sorted(path for path in changed_files if _is_test_file(path))
    sources = sorted(path for path in changed_files if not _is_test_file(path))
    stray = tests if kind == "impl" else sources
    if stray:
        other = "test" if kind == "impl" else "source"
        return GateCheck(
            name="node-scope",
            passed=False,
            detail=f"{kind} node changed {other} file(s): {', '.join(stray)}",
        )
    kept = sources if kind == "impl" else tests
    return GateCheck(
        name="node-scope", passed=True, detail=f"{kind} node changed {len(kept)} file(s) in scope"
    )


def _asserted_literals(source: str) -> set[str]:
    """Every literal a test function asserts on, spelled as the planner spells examples.

    A string constant counts by value, any other constant by its source
    spelling, when it sits in an `assert`, under a `with` (a `raises`
    block) or in a decorator (a `parametrize` table) of a `test*`
    function.
    """
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return set()
    found: set[str] = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef):
            continue
        if not node.name.startswith("test"):
            continue
        holders: list[ast.AST] = [*node.decorator_list]
        holders.extend(s for s in ast.walk(node) if isinstance(s, ast.Assert | ast.With))
        for holder in holders:
            for constant in ast.walk(holder):
                if isinstance(constant, ast.Constant):
                    value = constant.value
                    found.add(value if isinstance(value, str) else ast.unparse(constant))
    return found


def _performed_calls(source: str) -> set[tuple[str, frozenset[str]]]:
    """Every call a `test*` function performs, as `(operation, constants)`.

    `deposit('10.00', 'USD')` is performed by `acc.deposit(...)` as much
    as by a bare `deposit(...)`: the planner names the operation, not
    whatever receiver it happens to hang off. Constants are spelled the
    way `_asserted_literals` spells what it finds, so an example's
    arguments and a call's arguments are comparable.

    Only a test that asserts something counts (T6-69). Requiring an
    `Assert` or a `With` somewhere in the function is what keeps the
    tightening half of T6-50: a function that performs the operation and
    makes no claim about it binds nothing.
    """
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return set()
    found: set[tuple[str, frozenset[str]]] = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef):
            continue
        if not node.name.startswith("test"):
            continue
        if not any(isinstance(s, ast.Assert | ast.With) for s in ast.walk(node)):
            continue
        for call in ast.walk(node):
            if not isinstance(call, ast.Call):
                continue
            if isinstance(call.func, ast.Name):
                name = call.func.id
            elif isinstance(call.func, ast.Attribute):
                name = call.func.attr
            else:
                continue
            found.add(
                (
                    name,
                    frozenset(
                        argument.value if isinstance(argument.value, str) else ast.unparse(argument)
                        for argument in [*call.args, *(k.value for k in call.keywords)]
                        if isinstance(argument, ast.Constant)
                    ),
                )
            )
    return found


def _example_call(text: str) -> tuple[str, frozenset[str]] | None:
    """An example spelled as a call, as `(operation, constant arguments)`.

    `None` for anything else -- a bare literal, prose, an expression that
    is not a call -- which leaves the literal rule in charge. Arguments
    are spelled the way `_asserted_literals` spells what it finds, so the
    two sets are comparable, and by *value* for strings: `autofix` runs
    `ruff format` before the gate, and it rewrites `'10.00'` to `"10.00"`.
    """
    try:
        parsed = ast.parse(text.strip(), mode="eval")
    except SyntaxError:
        return None
    if not isinstance(call := parsed.body, ast.Call):
        return None
    if isinstance(call.func, ast.Name):
        name = call.func.id
    elif isinstance(call.func, ast.Attribute):
        name = call.func.attr
    else:
        return None
    constants: set[str] = set()
    for argument in [*call.args, *(keyword.value for keyword in call.keywords)]:
        if isinstance(argument, ast.Constant):
            value = argument.value
            constants.add(value if isinstance(value, str) else ast.unparse(argument))
    return name, frozenset(constants)


def _example_values(text: str) -> frozenset[str] | None:
    """The constants inside an example written as structured data, or `None`.

    `None` for anything that is not a dict, list, tuple or set display --
    prose, a bare literal, a call -- which leaves those to the rules that
    already own them. Constants are spelled the way `_asserted_literals`
    spells what it finds, so the two sets are comparable.
    """
    try:
        parsed = ast.parse(text.strip(), mode="eval")
    except SyntaxError:
        return None
    if not isinstance(parsed.body, ast.Dict | ast.List | ast.Tuple | ast.Set):
        return None
    return frozenset(
        node.value if isinstance(node.value, str) else ast.unparse(node)
        for node in ast.walk(parsed.body)
        if isinstance(node, ast.Constant)
    )


def _example_unbound(
    text: str, literals: set[str], performed: set[tuple[str, frozenset[str]]]
) -> str | None:
    """Why no test binds `text`, as a detail suffix, or `None` when one does.

    T6-50, from round 3f (F21.20). The literal rule alone could not be
    satisfied by any real test: planners write examples as calls,
    `_asserted_literals` collects constants, and a call expression is
    never a constant. Its one satisfying source was a test that quoted
    the example as a string and asserted nothing about the behaviour --
    and even that depended on the planner's quote style surviving
    `autofix`, which it does not. So a call-shaped example is bound by
    the test that performs that operation and asserts on the values it
    was given, and the quoting escape closes with it.

    T6-69, from round 3j (F21.41): "asserts on the values" was read as
    "the constants appear inside an `assert`", which is not where Python
    puts the arguments of the call a test exercises. A reject is spelled
    `with pytest.raises(...): deposit("0.001", "USD")` -- a `With`, which
    `_asserted_literals` collects -- and an accept is spelled
    `result = deposit("1.001", "USD")` then `assert result == ...`, an
    `Assign`, which nothing collects. The gate bound every reject and
    missed every accept, six attempts running. The proxy is replaced by
    what it stood for: the test must CALL the operation with those
    arguments. Looser for the accept it always missed, TIGHTER against a
    test that calls with different values while the example's constants
    sit in an unrelated assert, which the proxy bound.

    T6-66, from round 3j (F21.41): a STRUCTURED example -- the planner's
    version-2 store record -- is not a call, so it fell to the literal
    rule and inherited exactly the defect T6-50 repaired next door. That
    rule bound it only to a test quoting the whole blob as one string,
    which inverted the gate: the suite that saved a ledger and asserted
    the written JSON equals the record was refused, while a test that
    quoted the text and asserted nothing passed. Four attempts across two
    nodes died on it and no suite could have passed. A data example now
    binds by the constants inside it, the same standard as a call's
    arguments. Widened: it admits a test that asserts the values without
    exercising the behaviour -- which the flat-literal rule admitted too,
    so the latitude is inherited, not created.
    """
    if (call := _example_call(text)) is None:
        if text in literals:
            return None
        if (values := _example_values(text)) is None:
            return ""
        if unasserted := sorted(values - literals):
            return f" (no test asserts on {', '.join(repr(value) for value in unasserted)})"
        return None
    name, constants = call
    if not any(performed_name == name for performed_name, _ in performed):
        return f" (no test calls {name})"
    if any(
        performed_name == name and constants <= arguments for performed_name, arguments in performed
    ):
        return None
    return f" (no test calls {name} with {', '.join(repr(v) for v in sorted(constants))})"


def check_requirement_binding(
    requirement_ids: Collection[str],
    flipped_tests: Mapping[str, str],
    *,
    planned_ids: Collection[str] = (),
    examples: Collection[tuple[str, str, str]] = (),
    suite: Mapping[str, str] | None = None,
    writable: bool = True,
) -> GateCheck:
    """Every declared requirement is cited, and every citation is planned.

    The second half is traceSDD's orphan rule: an ID cited in code that
    nobody declared is a hallucinated requirement, and detecting it is
    what stops the binding being satisfiable in both directions. A
    worker free to invent IDs can tag whatever it likes and the gate
    still reads green -- F5's circularity, which survives adding
    statements unless citations are constrained to the declared set.

    `planned_ids` widens that set to the ids other nodes of the same plan
    declare (T3-24): the test sources are read suite-wide, so a plan that
    gives its `test` node `REQ-001` and its `impl` node `REQ-002` cites
    both ids in one file, and with the node's own ids alone no node of
    such a plan can pass (session 20b). The first half is untouched: an
    id the node itself declares must be cited, whatever the plan holds.

    `examples` (T6-4) are the `(id, "accepts"|"rejects", text)` triples the
    node's requirements cite; each must be bound by some test in `suite`
    (the tests as the node leaves them; `flipped_tests` when the caller
    passes none). A statement can be cited without being tested -- T1's
    tests probed no reject at all -- and an example the planner chose is
    the one thing the party being graded did not.

    An example spelled as a literal is bound by a test that asserts on
    it. An example spelled as a **call** is bound by a test that performs
    that operation and asserts on the constants it was handed (T6-50):
    binding it to the literal rule made it unsatisfiable by any test that
    asserts behaviour, and satisfiable only by one that quotes the
    example and asserts nothing (F21.20).
    """
    unbound = sorted(
        req
        for req in requirement_ids
        if not any(req in source for source in flipped_tests.values())
    )
    if not writable:
        # Every clause here is a function of the tests and the plan.
        # A node that may not edit tests cannot move any of them, so
        # the gate reads the same whatever it writes -- it judges the
        # test node's output and bills this one for it (F21.66). The
        # examples clause was already exempted on this argument; the
        # other two were not. The gap is recorded, not lost.
        note = f" unbound={','.join(unbound)}" if unbound else ""
        return GateCheck(
            name="requirement-binding",
            passed=True,
            detail="not judged: every clause reads tests this node may not write",
            basis=f"requirement-binding: not judged, node may not write tests{note}",
        )
    if unbound:
        return GateCheck(
            name="requirement-binding",
            passed=False,
            detail=f"unbound requirements: {', '.join(unbound)}",
        )
    declared = set(requirement_ids)
    cited = {
        found for source in flipped_tests.values() for found in REQUIREMENT_CITATION.findall(source)
    }
    orphans = sorted(cited - declared - set(planned_ids))
    if orphans:
        return GateCheck(
            name="requirement-binding",
            passed=False,
            detail=f"undeclared requirements cited: {', '.join(orphans)}",
        )
    literals: set[str] = set()
    performed: set[tuple[str, frozenset[str]]] = set()
    for source in (suite if suite is not None else flipped_tests).values():
        literals |= _asserted_literals(source)
        performed |= _performed_calls(source)
    unasserted = [
        f"{rid} {polarity} {text!r}{why}"
        for rid, polarity, text in examples
        if (why := _example_unbound(text, literals, performed)) is not None
    ]
    if unasserted:
        return GateCheck(
            name="requirement-binding",
            passed=False,
            detail=f"examples no test asserts on: {', '.join(unasserted)}",
        )
    bound = f"{len(list(requirement_ids))} requirement(s) bound"
    if examples:
        bound += f", {len(list(examples))} example(s) asserted"
    return GateCheck(name="requirement-binding", passed=True, detail=bound)


@dataclass(frozen=True)
class Tier1Inputs:
    """Collected evidence for one node: subprocess-free, runner-injected."""

    sources: Mapping[str, str]
    ruff_files: Collection[str]
    # The two ruff legs, already run by the runner (T6-3): the current
    # tree's findings the baseline did not carry, how many it did, and
    # the exits of `ruff check` (current) and `ruff format --check`.
    ruff_introduced: tuple[RuffFinding, ...]
    ruff_inherited: int
    ruff_lint_exit: int
    ruff_format_exit: int
    test_runner: Callable[[str], int]
    changed: set[tuple[str, int]]
    covered: set[tuple[str, int]]
    baseline_exits: tuple[int, ...]
    baseline_output: str
    baseline_tests: Mapping[str, str]
    tests_changed: bool
    current_runner: Callable[[], int]
    flipped_tests: Mapping[str, str]
    mutation: MutationOutcome
    # T6-41: the lines the node added, per changed non-test module, and a
    # runner that re-runs the suite over sources with some of them gone.
    added_lines: Mapping[str, tuple[int, ...]]
    dead_code_runner: Callable[[Mapping[str, str]], int]
    baseline_sources: Mapping[str, str]
    # The workdir `changed` and `covered` are keyed against, so
    # `compelled_lines` can speak the same spelling (T6-86). Empty means
    # those sets are already workdir-relative.
    workdir: str = ""
    # T6-53: nodes the plan still owes tests from. Non-empty defers an
    # uncovered changed line instead of failing the node for it.
    owed_tests: tuple[str, ...] = ()
    added_files: Collection[str] = ()
    # Repo-relative paths of every file the node changed or added (T3-2).
    touched_files: Collection[str] = ()
    # The current suite run's captured text and the worktree's importable
    # top-level names; read only by a `test` node's red-specification
    # verdict (T3-7a).
    test_output: str = ""
    workdir_modules: Collection[str] = ()
    # Every id some node of the plan declares; the orphan half of
    # requirement-binding subtracts these before rejecting a citation
    # (T3-24). Empty means the node's own ids are the whole plan.
    planned_requirements: tuple[str, ...] = ()
    # The property oracle (T3-3): `property_targets` are the property-bearing
    # test modules that import a changed module, and `property_oracle` is the
    # mutation sample run with those modules alone, or None when the runner
    # did not run it. Read only for `impl` nodes.
    property_oracle: MutationOutcome | None = None
    property_targets: tuple[str, ...] = ()
    # Property modules the change qualifies that the node's declared
    # pytest scope excludes, so a pass by vacuity names them (F21.65).
    property_out_of_scope: tuple[str, ...] = ()


@dataclass(frozen=True)
class Tier1Result:
    """Aggregated Tier-1 verdict: every check runs, `passed` needs all green."""

    node_id: str
    passed: bool
    checks: tuple[GateCheck, ...]
    # What the verdict left unpinned (T6-29c): every surviving mutant by
    # name, and every changed line no test executed or a survivor sits on,
    # in the runner's own spelling. A recovery briefs a test node from
    # these without re-running the gate that found them.
    survivors: tuple[str, ...] = ()
    gaps: tuple[tuple[str, int], ...] = ()


def check_mutation(outcome: MutationOutcome, threshold: float) -> GateCheck:
    """Changed-line kill-rate must clear `threshold`; small samples take no
    partial credit, and an empty sample is no evidence rather than a pass.

    The old fail-open returned PASS when nothing was generated, reasoning
    that a change with no mutable surface cannot be under-tested. The
    premise is false: mutmut only mutates function bodies, so a
    module-scope `_RE = re.compile(...)` yields 0 mutants where the same
    expression inline yields 7. T7 added a 218-line module, generated
    nothing, and the gate passed an infinite loop (F12).

    The threshold cannot rescue a thin sample either. T1's four-line
    regex admitted 2 mutants, two shallow tests killed both, and the gate
    read 100% over a validator that accepts `user@example..com` (F1).
    Below MIN_SIGNIFICANT_MUTANTS a percentage is noise, so every mutant
    must die -- fewer mutants means a stricter bar, not a cheaper one.
    """
    if outcome.total == 0:
        if outcome.survivors:
            cause = ", ".join(sorted(outcome.survivors)[:5])
            # A tool that never ran is named as such (T3-20); "no mutants
            # decided" describes a run that happened.
            failed_tool = any(s.startswith("mutmut run exited") for s in outcome.survivors)
            # mutmut baselines by running the suite, so a red tree fails
            # collection with the same exit a broken engine gives. Round
            # 3c's three impl attempts all died on "failed to collect
            # stats" and were reported as a tool failure the worker could
            # do nothing about (T6-63); evidence marks the suite's case.
            red_suite = any(s.startswith("suite is red") for s in outcome.survivors)
            if red_suite:
                detail = f"mutation not measured: {cause}"
            else:
                detail = (
                    f"mutation tool failed: {cause}"
                    if failed_tool
                    else f"no mutants decided: {cause}"
                )
            return GateCheck(name="mutation", passed=False, detail=detail, basis="sampled n=0")
        if outcome.generated == 0:
            return GateCheck(
                name="mutation",
                passed=False,
                detail="no mutants on changed lines: mutation provided no evidence",
                basis="sampled n=0",
            )
        return GateCheck(
            name="mutation", passed=False, detail="no mutants decided", basis="sampled n=0"
        )
    percent = 100.0 * outcome.killed / outcome.total
    small = outcome.total < MIN_SIGNIFICANT_MUTANTS
    required = 100.0 if small else threshold
    excluded = f"; {outcome.text_only} text-only mutant(s) excluded" if outcome.text_only else ""
    if percent < required:
        # The count, then the first five names. F21.14 read five names as
        # the whole set and called the gap an order of magnitude; F21.15
        # re-measured it: 13 survivors on the gate's own population
        # against 66 on the tree, so the prefix showed 5 of 13, not 5 of
        # 66. The count is here because five names is not the set, and
        # the two numbers are different populations -- `mutation_sample`
        # keeps only mutants on a changed line, then slices to
        # `max_mutants`, while the probe scored every production file.
        names = sorted(outcome.survivors)
        shown = ", ".join(names[:5]) + (", ..." if len(names) > 5 else "")
        note = f" (small sample: {outcome.total} mutant(s), all must die)" if small else ""
        return GateCheck(
            name="mutation",
            passed=False,
            detail=(
                f"{percent:.1f}% < {required:.1f}%{note}: survived {len(names)}: {shown}{excluded}"
            ),
            basis=f"sampled n={outcome.total}",
        )
    return GateCheck(
        name="mutation",
        passed=True,
        detail=f"{percent:.1f}% >= {required:.1f}% over {outcome.total} mutant(s){excluded}",
        basis=f"sampled n={outcome.total}",
    )


def _not_required(name: str) -> GateCheck:
    """A `test` node changes no source, so source-only evidence is moot (T3-7a)."""
    return GateCheck(
        name=name, passed=True, detail="not required: no source changed", basis="test node"
    )


def run_tier1(node: Node, inputs: Tier1Inputs) -> Tier1Result:
    """Run all twelve Tier-1 checks against `node`'s gate spec and aggregate.

    A `test` node is a red specification (T3-7a): its tests check inverts,
    red-phase mirrors that verdict, and the three source-only checks
    (coverage, dead-code, mutation) are substituted with "not required" so
    the order pin and the count stay the same for every kind.
    """
    gate = node.deterministic_gate
    sample = gate.mutation_sample
    is_spec = node.kind == "test"
    # The `impl`/`test` split (`check_node_scope`): an `impl` node may
    # not edit tests, so no gate may bill it for what the tests say.
    may_write_tests = node.kind != "impl"
    syntax = check_syntax(inputs.sources)
    ruff = check_ruff(
        inputs.ruff_files,
        introduced=inputs.ruff_introduced,
        inherited=inputs.ruff_inherited,
        lint_exit=inputs.ruff_lint_exit,
        format_exit=inputs.ruff_format_exit,
    )
    tests = check_test_command(
        gate.test_command,
        inputs.test_runner,
        kind=node.kind,
        output=inputs.test_output,
        workdir_modules=inputs.workdir_modules,
    )
    coverage = (
        _not_required("coverage")
        if is_spec
        else check_changed_line_coverage(
            inputs.changed,
            inputs.covered,
            gate.changed_line_coverage_min,
            inputs.owed_tests,
            compelled_lines(
                inputs.baseline_sources, inputs.sources, inputs.workdir, inputs.covered
            ),
            writable=may_write_tests,
        )
    )
    checks = (
        syntax,
        ruff,
        tests,
        coverage,
        _not_required("dead-code")
        if is_spec
        else check_dead_additions(
            inputs.sources,
            inputs.added_lines,
            suite_passed=tests.passed,
            run_without=inputs.dead_code_runner,
        ),
        _not_required("public-deletions")
        if is_spec
        else check_public_deletions(inputs.baseline_sources, inputs.sources),
        check_red_phase(
            inputs.baseline_exits,
            inputs.current_runner,
            baseline_output=inputs.baseline_output,
            changed_files=sorted({path for path, _ in inputs.changed}),
            tests_changed=inputs.tests_changed,
            kind=node.kind,
            coverage=coverage,
            mutation=inputs.mutation,
            red_spec=tests,
        ),
        check_node_scope(
            node.kind,
            sorted({path for path, _ in inputs.changed}),
            inputs.added_files,
            may_create="write_file" in node.execution_constraints.allowed_tools,
        ),
        check_target_files(node.target_files, inputs.touched_files),
        check_property_coverage(
            node.kind,
            inputs.flipped_tests,
            oracle=inputs.property_oracle,
            targets=inputs.property_targets,
            out_of_scope=inputs.property_out_of_scope,
        ),
        check_assertion_preservation(node.kind, inputs.baseline_tests, inputs.flipped_tests),
        check_requirement_binding(
            node.requirement_ids,
            inputs.flipped_tests,
            planned_ids=inputs.planned_requirements,
            # A test node asserts the examples (T6-4); an impl node cannot
            # edit tests and a refactor preserves them, so neither is asked.
            examples=node.requirement_examples if is_spec else (),
            suite={**inputs.baseline_tests, **inputs.flipped_tests},
            writable=may_write_tests,
        ),
        _not_required("mutation")
        if is_spec
        else check_mutation(inputs.mutation, sample.kill_threshold),
    )
    gaps = (inputs.changed - inputs.covered) | set(inputs.mutation.survivor_lines)
    return Tier1Result(
        node_id=node.id,
        passed=all(check.passed for check in checks),
        checks=checks,
        survivors=tuple(inputs.mutation.survivors),
        gaps=tuple(sorted(gaps)),
    )
