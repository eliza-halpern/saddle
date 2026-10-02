"""Tier-1 per-node gates (ARCHITECTURE.md §3 Phase 3 Tier 1).

Checks run in gate order: syntax, ruff, tests, coverage, red-phase,
node-scope, target-scope, property-coverage, assertion-preservation,
requirement-binding, mutation. Each check is a small pure function so
killer fixtures stay fast and deterministic; subprocess runners are
injected at the boundary, never embedded in the predicates.
"""

from __future__ import annotations

import ast
import dataclasses
import re
import tomllib
from collections.abc import Callable, Collection, Mapping, Sequence
from dataclasses import dataclass
from fnmatch import fnmatch
from os.path import commonprefix
from pathlib import PurePath
from typing import TYPE_CHECKING, Final, Literal

from saddle.dag import Node
from saddle.mutant_text import PHRASES, classify, function_of, parse_show
from saddle.task_examples import Example, Row, TreeOutcome, judge
from saddle.task_examples import classify as classify_example
from saddle.task_units import Units

if TYPE_CHECKING:
    from saddle.evidence import CapturedRun, MutationOutcome, SurvivorDetail

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
# How many introduced ruff findings the gate detail names before eliding.
RUFF_NAMED_FINDINGS: Final = 5
RUFF_FORMAT_DIFF_LINES: Final = 60
"""The most lines of `ruff format --diff` a format finding quotes. The diff
is the finding: a run whose sandbox has no ruff (a Task-lane run on a box
without it) was told only "ruff format --check exited 1" and spent its last
twenty minutes reverse-engineering the formatter by hand. A longer diff is
cut here; fixing the part shown shrinks it, and the next audit shows the rest."""
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


# Directory names that mark everything beneath them as test code, whatever
# the language or the file's own name.
TEST_DIRECTORIES: Final = frozenset({"tests", "test", "__tests__"})
# Test-file spellings outside pytest's: `markdown.test.js`, `app.spec.ts`.
TEST_CODE_PATTERNS: Final = tuple(
    f"*.{kind}.{ext}"
    for kind in ("test", "spec")
    for ext in ("js", "mjs", "cjs", "ts", "tsx", "jsx")
)


def is_test_code(path: str) -> bool:
    """True when `path` is test code that a node on the implementation side
    of the split may not edit, in any language.

    `_is_test_file` answers a different question: would pytest collect this
    module. `check_node_scope` asked it of who may edit a file, so
    `tests/markdown.test.js`, a fixture under `tests/` and a `conftest.py`
    all counted as source, and an `impl` node could rewrite the test it was
    graded by. Test code is anything pytest collects, a `conftest.py`, a file
    under a `tests`/`test`/`__tests__` directory, or a `*.test.*`/`*.spec.*`
    script.
    """
    pure = PurePath(path)
    return (
        _is_test_file(path)
        or pure.name == "conftest.py"
        or any(part in TEST_DIRECTORIES for part in pure.parts)
        or any(fnmatch(pure.name, pattern) for pattern in TEST_CODE_PATTERNS)
    )


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
    5 both read "passed" (T7's 218-line module, CONTRIBUTING.md), so the
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
    """One ruff diagnostic, keyed for matching across trees.

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
    """Split the current findings into (introduced, inherited count).

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


STATIC_NAMED_LINES: Final = 12
"""How many output lines of a failing static check its finding quotes."""


def check_static(argv: Sequence[str], run: CapturedRun) -> GateCheck:
    """The project's own static check: pass iff it exited 0.

    A tool that could not be launched, or that ran out of time, fails by
    name rather than reading as a clean tree; a failure quotes the first
    `STATIC_NAMED_LINES` lines of its output (the checker's own errors), so
    the model is told which file and line, not only that it failed."""
    shown = " ".join(argv)
    if run.exit_code == TOOL_UNAVAILABLE:
        return GateCheck(
            name="static-check",
            passed=False,
            detail=f"{shown}: the tool could not be launched",
        )
    if run.timed_out:
        return GateCheck(name="static-check", passed=False, detail=f"{shown}: timed out")
    if run.exit_code == 0:
        return GateCheck(name="static-check", passed=True, detail=f"{shown}: clean")
    lines = [line for line in (run.stdout + run.stderr).splitlines() if line.strip()]
    quoted = "\n".join(lines[:STATIC_NAMED_LINES])
    more = len(lines) - STATIC_NAMED_LINES
    suffix = f"\n(+{more} more lines)" if more > 0 else ""
    return GateCheck(
        name="static-check",
        passed=False,
        detail=f"{shown} exited {run.exit_code}:\n{quoted}{suffix}",
    )


GATE_STAGE_LINES: Final = 8
"""How many output lines of a failing gate stage the `project-gate` finding
quotes; the stage's own errors, so the model is told which file and line."""

GateVerdict = Literal["pass", "fail", "not-proven"]


@dataclass(frozen=True)
class ProjectGate:
    """The project-gate finding's verdict and words (`check_project_gate`)."""

    verdict: GateVerdict
    detail: str


def gate_stage_name(argv: Sequence[str], taken: Collection[str] = ()) -> str:
    """A short name for a stage: its tool and subcommand (`ruff format`, `eslint`),
    skipping launchers and options, the tool by its file name rather than its path;
    the whole command when that name is `taken`."""
    words = list(argv)
    while words:
        if words[:2] == ["uv", "run"]:
            words = words[2:]
        elif words[0] == "npx" or words[0].startswith("-"):
            words = words[1:]
        else:
            break
    tool = PurePath(words[0]).name if words else " ".join(argv)
    sub = words[1] if len(words) > 1 and re.fullmatch(r"[A-Za-z][\w.-]*", words[1]) else ""
    name = f"{tool} {sub}".strip()
    return " ".join(argv) if name in taken else name


def _state(argv: Sequence[str], run: CapturedRun) -> str:
    """`missing` when the command could not start: exit 127, or the sandbox's own
    "no such file" for the command (it exits 1, which would read as a red stage)."""
    exec_failed = f"execvp {argv[0]}: No such file or directory" in run.stdout + run.stderr
    if run.exit_code == TOOL_UNAVAILABLE or (run.exit_code != 0 and exec_failed):
        return "missing"
    if run.timed_out:
        return "timeout"
    return "ok" if run.exit_code == 0 else "red"


def _mark(red: Sequence[str]) -> str:
    return f"✗ ({', '.join(red)})" if red else "✓"


_AT_BASE: Final = {
    "red": "was failing at the base",
    "timeout": "timed out at the base",
    "missing": "could not be launched at the base",
}


def check_project_gate(
    stages: Sequence[tuple[Sequence[str], CapturedRun, CapturedRun]],
) -> ProjectGate:
    """The project's own gate stages, each run on the head tree and on the base.

    `stages` is `(argv, head run, base run)` per stage. A head that passes a
    stage passes it. A head that fails one the base passed is a regression: the
    finding FAILS and quotes the stage's first `GATE_STAGE_LINES` lines. A
    stage red at the base too is `not-proven`: it was already failing, which is
    not this change's doing, and it is neither a pass nor a refusal. A stage
    whose command cannot start, or that timed out without the base showing the
    contrast, is `not-proven` and names the tool: a lookup that fails must not
    read as green. The first line is the packet's `Gate:` line; the second says
    what this check does not judge (the suite's whole-project coverage total
    is the project's own gate's call, `evidence.run_suite_capture` runs the
    suite without it)."""
    taken: list[str] = []
    named: list[tuple[str, Sequence[str], str, str, CapturedRun]] = []
    for argv, head, base in stages:
        name = gate_stage_name(argv, taken)
        taken.append(name)
        named.append((name, argv, _state(argv, head), _state(argv, base), head))
    head_red = [n for n, _, h, _, _ in named if h != "ok"]
    base_red = [n for n, _, _, b, _ in named if b != "ok"]
    count = f"{len(named)} stage{'s' if len(named) != 1 else ''}"
    line = f"Gate: base {_mark(base_red)}, head {_mark(head_red)} ({count})"
    broke = False
    notes: list[str] = []
    for name, argv, h, b, run in named:
        shown = " ".join(argv)
        if h == "ok":
            continue
        if h == "missing":
            notes.append(f"{name}: not proven, {argv[0]} could not be launched here ({shown})")
        elif b == "ok":
            broke = True
            how = "timed out" if h == "timeout" else f"exited {run.exit_code}"
            lines = [x for x in (run.stdout + run.stderr).splitlines() if x.strip()]
            quoted = "\n".join(lines[:GATE_STAGE_LINES])
            more = len(lines) - GATE_STAGE_LINES
            suffix = f"\n(+{more} more lines)" if more > 0 else ""
            body = f":\n{quoted}{suffix}" if quoted else ""
            notes.append(
                f"{name}: the change broke it, it passed at the base ({shown} {how}){body}"
            )
        else:
            at_base = _AT_BASE[b]
            notes.append(
                f"{name}: not proven, it {at_base}, so the change is not shown to have "
                f"broken it ({shown})"
            )
    scope = (
        "The suite's whole-project coverage total is judged by the project's own gate, not here."
    )
    verdict: GateVerdict = "fail" if broke else "not-proven" if notes else "pass"
    return ProjectGate(verdict, "\n".join([line, *notes, scope]))


def check_ruff(
    files: Collection[str],
    *,
    introduced: Sequence[RuffFinding],
    inherited: int,
    lint_exit: int,
    format_exit: int,
    format_diff: str = "",
) -> GateCheck:
    """The ruff gate: a node fails for lint its own diff introduced.

    `introduced` are the current tree's findings absent from the node's
    baseline, `inherited` how many the baseline already carried;
    `lint_exit` and `format_exit` are the two ruff runs' exits. A finding
    the node inherited is reported and does not fail it (T2's impl node
    burned three attempts on a BLE001 the baseline shipped, and the fix
    that was finally accepted was a `noqa` on code it never wrote).
    Formatting is unchanged: a format failure is always the node's. Where
    the harness does not format for it (an auto run without
    `--format-at-finish`), `format_diff` -- `ruff format --diff`'s output --
    is quoted, up to `RUFF_FORMAT_DIFF_LINES`, so the changes can be made
    without ruff. The detail names the rules, file and line (a bare
    `ruff check exited 1` told the worker nothing).
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
    if format_exit != 0 and format_diff.strip():
        lines = format_diff.strip().splitlines()
        shown = "\n".join(lines[:RUFF_FORMAT_DIFF_LINES])
        more = len(lines) - RUFF_FORMAT_DIFF_LINES
        cut = f"\n(+{more} more diff lines: make these, and the next audit shows the rest)"
        return GateCheck(
            name="ruff",
            passed=False,
            detail=(
                f"ruff format would reformat it{inherited_note}. Make exactly these changes "
                "(a unified diff: - is the current line, + what it must be); you do not "
                f"need ruff to apply them:\n{shown}{cut if more > 0 else ''}"
            ),
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
    """The `test`-kind tests verdict: the node's tests must fail now.

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
        failing = failing_tests(output)
        named = ", ".join(failing[:FAILING_NAMED])
        if len(failing) > FAILING_NAMED:
            named += f" and {len(failing) - FAILING_NAMED} more"
        return GateCheck(
            name="tests",
            passed=False,
            detail=f"{test_command!r} exited {exit_code}"
            + (f": {len(failing)} failing: {named}" if failing else ""),
        )
    return GateCheck(name="tests", passed=True, detail=f"{test_command!r} exited 0")


_FAILING_LINE: Final = re.compile(r"^(?:FAILED|ERROR) (\S+)", re.MULTILINE)
FAILING_NAMED: Final = 5
"""How many failing tests a failed `tests` check names before counting the rest."""


def failing_tests(output: str) -> list[str]:
    """The tests pytest's short summary names as failed or errored, once each,
    sorted: the same finding whether the suite ran serially or on workers.

    A failed suite's finding named only the exit code, so a watched run
    guessed at the cause ("might be the coverage gate") and weighed running
    the whole suite to find what the audit had already seen."""
    return sorted({match.group(1) for match in _FAILING_LINE.finditer(output)})


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
    the run's two remaining nodes with it.

    So what one gate compels, another must not punish. The exemption is
    that narrow on purpose: it covers only definitions the BASELINE
    already had, never a definition the node invents, and never a line
    outside one. What it admits, stated plainly: a node may pad the body
    of a baseline definition with code no test runs. `dead-code` and
    `mutation` still read those lines, and the node does not choose
    which members its baseline carries.

    `covered` narrows it further, and must. A definition whose
    BODY any test reaches is not exempt at all: it is judged line by
    line, exactly as before this exemption. Without that, the exemption swallows
    the gate for the commonest node shape there is -- an impl node
    editing a public function its baseline already had -- because every
    changed line then leaves the denominator, `judged` empties, and
    coverage returns pass having measured nothing. Eleven `run_slice`
    tests flipped from "every changed line runs" to that pass the moment
    the exemption began to fire, which is what a hollow gate looks like
    from the outside. The exemption is for the case it was actually
    built for -- a public definition NOTHING calls, which the node may
    not delete and cannot cover -- and reachability is what separates
    the two.

    `prefix` is the workdir the caller's `changed` set is keyed against.
    `baseline_sources` is `read_sources`, which is workdir-RELATIVE,
    while `runner.py` builds `changed` as `(str(workdir / path), line)`
    -- absolute, because `mutation_sample` and `covered_lines` need it
    that way. `check_changed_line_coverage` intersects the two, so
    without the prefix the intersection is empty and this exemption
    fires for nothing: it fired zero times in 48 runs, and `g1-79cd848`
    reproduced the very trap this exemption exists for on a tree carrying it.
    """
    compelled: set[tuple[str, int]] = set()
    for lines in compelled_definitions(baseline_sources, sources, prefix, covered).values():
        compelled |= lines
    return compelled


def compelled_definitions(
    baseline_sources: Mapping[str, str],
    sources: Mapping[str, str],
    prefix: str = "",
    covered: Collection[tuple[str, int]] = (),
) -> dict[str, set[tuple[str, int]]]:
    """`compelled_lines`, per definition: "<file>:<qualified name>" to its lines.

    The names are what `check_changed_line_coverage` writes into `basis`
    for each definition it spared, so a sealed pass says
    which baseline definitions it did not judge rather than only how many
    lines. The file is spelled as `baseline_sources` spells it (relative);
    the lines are keyed with `prefix`, as `compelled_lines` keys them.
    """
    reached = set(covered)
    compelled: dict[str, set[tuple[str, int]]] = {}
    for rel, text in baseline_sources.items():
        before = _public_definitions(text)
        if not before:
            continue
        key = str(PurePath(prefix) / rel) if prefix else rel
        for name, (whole, body) in _definition_lines(sources.get(rel, ""), before).items():
            if {(key, line) for line in body} & reached:
                continue
            compelled[f"{rel}:{name}"] = {(key, line) for line in whole}
    return compelled


def _fails_when_run(statement: ast.stmt) -> bool:
    """True for a statement whose running fails the test it is in.

    `assert <falsy constant>`, `raise AssertionError[(...)]` and
    `pytest.fail(...)`: the lines a test reaches only when the code under
    test is wrong.
    """
    if isinstance(statement, ast.Assert):
        return isinstance(statement.test, ast.Constant) and not statement.test.value
    if isinstance(statement, ast.Raise):
        exc = statement.exc
        target = exc.func if isinstance(exc, ast.Call) else exc
        return isinstance(target, ast.Name) and target.id == "AssertionError"
    if isinstance(statement, ast.Expr) and isinstance(statement.value, ast.Call):
        func = statement.value.func
        return (
            isinstance(func, ast.Attribute)
            and func.attr == "fail"
            and isinstance(func.value, ast.Name)
            and func.value.id == "pytest"
        )
    return False


def _is_main_guard(statement: ast.If) -> bool:
    """True for `if __name__ == "__main__":`, either operand first."""
    if not isinstance(statement.test, ast.Compare):
        return False
    test = statement.test
    if len(test.ops) != 1 or not isinstance(test.ops[0], ast.Eq):
        return False
    operands = {
        ("name" if isinstance(node, ast.Name) and node.id == "__name__" else None)
        or ("main" if isinstance(node, ast.Constant) and node.value == "__main__" else None)
        for node in (test.left, test.comparators[0])
    }
    return operands == {"name", "main"}


def never_run_test_lines(sources: Mapping[str, str], prefix: str = "") -> set[tuple[str, int]]:
    """Lines of a test module that no passing pytest run executes.

    Two shapes, both in modules pytest collects (`_is_test_file`):

    - a statement that fails the test when it runs (`_fails_when_run`),
      such as the `assert False` after a call that must raise. It runs
      only on wrong code, which the tests gate refuses, so asking
      coverage to see it run asks for a tree no gate set can accept;
    - the body of a module-level `if __name__ == "__main__":`. pytest
      imports a test module under its own name, and the coverage run is
      always a pytest run (`under_coverage`), so the body never runs.

    `check_changed_line_coverage` leaves these out of its judgement. What
    that admits, stated plainly: such a line in a test nothing collects
    is no longer named. The test's other lines still are, and a
    source module's lines, `__main__` included, are judged as before.
    Keys are spelled as `compelled_lines` spells them, with `prefix`.
    """
    never: set[tuple[str, int]] = set()
    for rel, text in sources.items():
        if not _is_test_file(rel):
            continue
        try:
            tree = ast.parse(text)
        except SyntaxError:
            continue
        key = str(PurePath(prefix) / rel) if prefix else rel
        spans = [
            _statement_span(node)
            for node in ast.walk(tree)
            if isinstance(node, ast.stmt) and _fails_when_run(node)
        ]
        for guard in tree.body:
            if isinstance(guard, ast.If) and _is_main_guard(guard):
                spans.extend(_statement_span(node) for node in guard.body)
        for first, last in spans:
            never.update((key, line) for line in range(first, last + 1))
    return never


EXEMPT_TEST_LINES: Final = "exempt-test-lines="
"""The `basis` field of `check_changed_line_coverage` counting the changed
lines it did not judge because no passing pytest run executes them
(`never_run_test_lines`)."""


PACKAGING_SCRIPT: Final = "setup.py"
"""The repository-root file `check_changed_line_coverage` does not judge."""


PACKAGING_LINES: Final = "packaging-lines="
"""The `basis` field of `check_changed_line_coverage` counting the changed
lines of the root packaging script it did not judge
(`packaging_script_lines`)."""


def packaging_script_lines(
    changed: Collection[tuple[str, int]], prefix: str = ""
) -> set[tuple[str, int]]:
    """The changed lines of the repository-root `setup.py`.

    A build frontend runs that file in its own environment; a test cannot
    import it without running a build, so a pytest coverage run never
    executes it and asking coverage to see it run asks for a tree that
    deletes it. `check_changed_line_coverage` leaves these lines out of its
    judgement and counts them. What that admits, stated plainly: logic
    placed in the root `setup.py` is no longer coverage-judged. A
    `setup.py` anywhere below the root is an ordinary module and stays
    judged, as does every other file. Keys are spelled as `changed` spells
    them, with `prefix` (the workdir).
    """
    root = str(PurePath(prefix) / PACKAGING_SCRIPT) if prefix else PACKAGING_SCRIPT
    return {(path, line) for path, line in changed if path == root}


SPARED_DEFS: Final = "spared-defs="
"""The `basis` field of `check_changed_line_coverage` naming each baseline
definition whose changed lines it did not judge (`compelled_definitions`)."""


def spared_definitions(basis: str) -> list[str]:
    """The definitions a coverage `basis` says were spared; [] if none."""
    for field in basis.split():
        if field.startswith(SPARED_DEFS):
            return [n for n in field.removeprefix(SPARED_DEFS).split(",") if n]
    return []


def check_changed_line_coverage(
    changed: set[tuple[str, int]],
    covered: set[tuple[str, int]],
    minimum: float,
    owed: Collection[str] = (),
    compelled: Collection[tuple[str, int]] | Mapping[str, Collection[tuple[str, int]]] = (),
    writable: bool = True,
    never_run: Collection[tuple[str, int]] = (),
    packaging: Collection[tuple[str, int]] = (),
) -> GateCheck:
    """Every changed line must be executed; `minimum` is the node threshold.

    `detail` is routed to the worker by `format_attempt_failure`, so it
    names the lines no test runs and carries no ratio. A
    percentage is satisfiable by a call that runs the line and asserts
    nothing; the lines themselves are the evidence. The counts stay in
    `basis`, which is sealed rather than worker-facing.

    `owed` is the nodes the plan still expects tests from. When
    it is non-empty the uncovered lines are **deferred** rather than
    failed: the node seals, and `basis` records what was set aside. An
    `impl` node may not write tests, so with a test node still owed the
    gate is asking a question whose answer cannot exist yet, and round
    3g shows what that costs -- `n2.r1` was failed for eleven lines on a
    tree that passes 16 of 16 hidden accounts-and-fees tests, because
    the record shape it had to write is exercised by a test file
    belonging to a node that had not run.

    `compelled` is the lines `public-deletions` will not let the node
    drop. They are removed from the judgement entirely, because
    failing a node for not covering code it was forbidden to delete asks
    it for a diff that does not exist -- see `compelled_lines`. Given per
    definition (`compelled_definitions`), `basis` also names each one a
    changed line was spared from, `spared-defs=<file>:<name>,...`: a pass
    that judged nothing in them says which.

    `never_run` is the test-module lines no passing pytest run executes
    (`never_run_test_lines`). Like `compelled` they leave the judgement,
    and `basis` counts them: requiring one to run is requiring the code
    under test to be wrong.

    `packaging` is the changed lines of the root packaging script
    (`packaging_script_lines`). They leave the judgement too, and are
    counted twice over: in `basis` as `packaging-lines=N` and at the end
    of every `detail` as "N packaging lines not judged (root setup.py)",
    so a reader of either sees what was set aside.

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
    # sinks the node for code it was required to carry.
    by_def = compelled if isinstance(compelled, Mapping) else {"": compelled}
    lines = {line for group in by_def.values() for line in group}
    spared = changed & lines
    # Only recorded when it happened: a "compelled-lines=0" on every
    # sealed node would churn every existing record to say nothing.
    note = f" compelled-lines={len(spared)}" if spared else ""
    names = sorted(name for name, group in by_def.items() if name and changed & set(group))
    if names:
        note += f" {SPARED_DEFS}{','.join(names)}"
    unrunnable = (changed - spared) & set(never_run)
    if unrunnable:
        note += f" {EXEMPT_TEST_LINES}{len(unrunnable)}"
    packaged = (changed - spared - unrunnable) & set(packaging)
    aside = ""
    if packaged:
        note += f" {PACKAGING_LINES}{len(packaged)}"
        plural = "line" if len(packaged) == 1 else "lines"
        aside = f"; {len(packaged)} packaging {plural} not judged (root {PACKAGING_SCRIPT})"
    judged = changed - spared - unrunnable - packaged
    if not judged:
        return GateCheck(
            name="coverage",
            passed=True,
            detail=(
                "every changed line is compelled or a test line no passing pytest run executes"
                if unrunnable
                else "every changed line is inside a definition the baseline already had"
                if spared
                else "every changed line is in the root packaging script"
            )
            + aside,
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
                detail=f"deferred, no test node has run that can reach {gaps}{aside}",
                basis=(
                    f"changed-lines={len(changed)} deferred-lines={len(missing)}"
                    f"{note} owed={','.join(sorted(owed))}"
                ),
            )
        if not writable:
            # Deferral asks whether the SCHEDULE could still cover the
            # line and deferred when it could. It never asked the
            # other half: whether THIS node could, and an `impl` node
            # never can. With no test node owed the two answers
            # diverge, which is the commonest plan the planner draws,
            # and the node's only way to green is to delete the branch
            # the task requires -- a pass the oracle then fails, which
            # is a gate refusing correct work: a gate defect.
            return GateCheck(
                name="coverage",
                passed=True,
                detail=f"deferred, no node that may write a test remains to reach {gaps}{aside}",
                basis=(f"changed-lines={len(changed)} unreachable-lines={len(missing)}{note}"),
            )
        return GateCheck(
            name="coverage",
            passed=False,
            detail=f"no test runs {gaps}{aside}",
            basis=f"changed-lines={len(changed)}{note}",
        )
    return GateCheck(
        name="coverage",
        passed=True,
        detail=f"every changed line runs{aside}",
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
    """A node may not delete a public definition its baseline had.

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
    """A recovery plan instruction that takes out public API.

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


# A requirement may not restate the gate. An offending clause
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
    """A subplan node declaring a file a still-pending node owns.

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
    """A node description or requirement statement that restates a gate.

    Returns the offending clause, or `None` when every node states
    behaviour. The shape to refuse is a requirement satisfiable by a
    no-op. "Every changed line executed by tests/test_accounts.py" is
    satisfied by calling a function whose body is `pass`; "fee_for
    returns the fee for a positive amount below the fee" is not.

    Stating requirements as behaviour removed the execution proxy from
    saddle's own worker rule but not from the instruction that
    regenerates it, so it still reached the worker laundered through the
    plan. Round 3g's `n2.r1` prompt, whose run has `e52912b` (that change)
    as an ancestor, carries it twice -- in the node description and
    inside REQ-002, under the heading "each test must fail if its
    statement is violated". Both are frozen beside this as fixtures.

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
    """Code nothing depends on is not an implementation.

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
    stood; it has since been replaced with the mutation form) and that
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
    reaches one shape of that and `public-deletions` carries the rest.
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


# Decorators that wrap a definition without registering it anywhere: a function
# behind one of these is still reached only by whoever names it. Any other
# decorator is taken as registration (a route, a command, a plugin hook).
_TRANSPARENT_DECORATORS: Final = frozenset(
    {
        "abstractmethod",
        "asynccontextmanager",
        "cache",
        "cached_property",
        "classmethod",
        "contextmanager",
        "dataclass",
        "final",
        "lru_cache",
        "overload",
        "property",
        "runtime_checkable",
        "staticmethod",
        "total_ordering",
        "wraps",
    }
)


def _module_names(path: str) -> set[str]:
    """The dotted names an entry point could import `path` as (`src/` layout or not)."""
    parts = list(PurePath(path).with_suffix("").parts)
    if parts[-1] == "__init__":
        parts.pop()
    names = {".".join(parts)}
    if parts[:1] == ["src"]:
        names.add(".".join(parts[1:]))
    return names


def _entry_point_targets(pyproject: str) -> set[tuple[str, str]]:
    """`(module, name)` for every `[project.scripts]`, `gui-scripts` and entry-point value.

    Raises `tomllib.TOMLDecodeError` when `pyproject` does not parse; the
    caller reports that rather than reading it as "no entry points".
    """
    project = tomllib.loads(pyproject).get("project", {})
    tables = [project.get("scripts"), project.get("gui-scripts")]
    tables.extend(project.get("entry-points", {}).values())
    targets: set[tuple[str, str]] = set()
    for table in tables:
        for value in table.values() if isinstance(table, dict) else ():
            module, colon, attr = str(value).partition(":")
            if colon:
                targets.add((module.strip(), attr.split("[")[0].strip().split(".")[0]))
    return targets


def _module_level_names(tree: ast.Module) -> dict[str, ast.stmt]:
    """Every module-level function, class and assigned name, with its statement."""
    names: dict[str, ast.stmt] = {}
    for statement in tree.body:
        if isinstance(statement, ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef):
            names[statement.name] = statement
        elif isinstance(statement, ast.Assign):
            for target in statement.targets:
                if isinstance(target, ast.Name):
                    names[target.id] = statement
        elif (
            isinstance(statement, ast.AnnAssign)
            and statement.value is not None
            and isinstance(statement.target, ast.Name)
        ):
            names[statement.target.id] = statement
    return names


def _imported_names(tree: ast.Module) -> set[str]:
    """Every dotted-name part an import statement of the module spells."""
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            names.update((node.module or "").split("."))
            names.update(alias.name for alias in node.names)
        elif isinstance(node, ast.Import):
            for alias in node.names:
                names.update(alias.name.split("."))
    return names


def _runs_as_a_script(path: str, tree: ast.Module) -> bool:
    """A `__main__.py`, or a module with an `if __name__ == ...` guard: run without an importer."""
    return PurePath(path).name == "__main__.py" or any(
        isinstance(node, ast.Compare)
        and isinstance(node.left, ast.Name)
        and node.left.id == "__name__"
        for node in ast.walk(tree)
    )


def _references(tree: ast.Module) -> list[tuple[str, int]]:
    """Each `(name, line)` the module reads: names, attribute tails, imports, `__all__` entries.

    Strings and docstrings are not references, except the entries of an
    `__all__`, which export the name they spell.
    """
    found: list[tuple[str, int]] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Name) and isinstance(node.ctx, ast.Load):
            found.append((node.id, node.lineno))
        elif isinstance(node, ast.Attribute) and isinstance(node.ctx, ast.Load):
            found.append((node.attr, node.lineno))
        elif isinstance(node, ast.ImportFrom):
            found.extend((alias.name, node.lineno) for alias in node.names)
    for statement in tree.body:
        targets = (
            statement.targets
            if isinstance(statement, ast.Assign)
            else [statement.target]
            if isinstance(statement, ast.AnnAssign | ast.AugAssign)
            else []
        )
        if any(isinstance(t, ast.Name) and t.id == "__all__" for t in targets):
            found.extend(
                (child.value, statement.lineno)
                for child in ast.walk(statement)
                if isinstance(child, ast.Constant) and isinstance(child.value, str)
            )
    return found


# Source suffixes in languages the Python gates cannot measure: changes to these files are the
# work the mutation and changed-line coverage checks never see (browser scripts, styles, markup,
# shell, systems languages). Data, config and prose are deliberately absent: a README edit beside
# a new library function does not say the function is padding.
OTHER_LANGUAGE_SUFFIXES: Final = frozenset(
    {
        ".js", ".mjs", ".cjs", ".ts", ".tsx", ".jsx", ".vue", ".svelte",
        ".css", ".scss", ".sass", ".less", ".html", ".htm", ".jinja", ".j2",
        ".sh", ".bash", ".rs", ".go", ".c", ".h", ".cc", ".cpp", ".java", ".kt",
        ".rb", ".php", ".swift", ".lua",
    }
)  # fmt: skip

TEST_ONLY_UNPROVEN: Final = "not proven: "
"""How `check_test_only_additions` begins a passing detail that names public
definitions it could not place: the auditor reports it `not-proven`."""


def _names_identifier(text: str, name: str) -> bool:
    """`name` appears in `text` as a whole identifier (case-sensitive), not inside a longer one."""
    return re.search(rf"(?<!\w){re.escape(name)}(?!\w)", text) is not None


@dataclass(frozen=True)
class JsDeadFinding:
    """One top-level JavaScript definition the change added that production code does not
    reach, as `jsdead.mjs` reports it. `kind` is `test-only` (only `callers`, test files, name it),
    `chain` (only other unreached definitions use it) or `nothing`."""

    file: str
    line: int
    name: str
    kind: str
    callers: tuple[str, ...] = ()


@dataclass(frozen=True)
class JsDeadReport:
    """What the JavaScript reach analysis said. `problem` is why it could not run (node or
    TypeScript missing, a failed run): never an empty list standing for it. `unresolved` are
    definitions it could not place, as `(file, line, name, why)`; `also` names the unreached
    definitions only a finding uses."""

    findings: tuple[JsDeadFinding, ...] = ()
    unresolved: tuple[tuple[str, int, str, str], ...] = ()
    also: tuple[str, ...] = ()
    problem: str = ""


def check_js_test_only_additions(
    report: JsDeadReport,
    *,
    task_text: str | None = None,
    touched: Collection[str] = (),
) -> GateCheck:
    """`check_test_only_additions` for JavaScript: the same rule, over `jsdead.mjs`'s findings.

    A function, class or constant added to a non-test `.js` file that only tests reach
    (`test-only`), or that nothing reaches (`nothing`, dead code), is refused when it is private
    (`_name`) or, for a test-only public name, when the change also touches non-test source in
    another file the other gates measure (the work is elsewhere: the padding shape); a public
    test-only name in a change that touches nothing else passes with a detail starting
    `TEST_ONLY_UNPROVEN` (a library function). A public name the task text spells was asked for.
    A name that nothing reaches is dead whatever the change touches. Anything the analysis could
    not place, or a run that could not happen, is `TEST_ONLY_UNPROVEN` and never a pass or a
    refusal. Findings read `file:line: name (why)`.
    """
    if report.problem:
        return GateCheck(
            name="dead-code",
            passed=True,
            detail=(
                f"{TEST_ONLY_UNPROVEN}the JavaScript reach analysis could not run: {report.problem}"
            ),
            basis="js-reach=unavailable",
        )
    refused: list[str] = []
    listed: list[str] = []
    for found in report.findings:
        private = found.name.startswith("_")
        asked = not private and task_text is not None and _names_identifier(task_text, found.name)
        if asked:
            continue
        where = f"{found.file}:{found.line}: {found.name}"
        padded = any(
            other != found.file
            and not is_test_code(other)
            and PurePath(other).suffix.lower() in OTHER_LANGUAGE_SUFFIXES | {".py"}
            for other in touched
        )
        if found.kind == "test-only":
            how = f"referenced only by {', '.join(found.callers)}"
            (refused if private or padded else listed).append(f"{where} ({how})")
        elif found.kind == "chain":
            refused.append(f"{where} (used only by other definitions no production code reaches)")
        else:
            refused.append(f"{where} (referenced by nothing)")
    if refused and report.also:
        more = f" (+{len(report.also) - 8} more)" if len(report.also) > 8 else ""
        refused.append(f"and what only these use: {', '.join(report.also[:8])}{more}")
    if refused:
        return GateCheck(
            name="dead-code",
            passed=False,
            detail=(
                "; ".join(refused)
                + ". A definition that exists only for tests, or for nothing, is not production "
                "code: wire each into code the page or the program runs, or delete it and its tests"
            ),
            basis=f"js-dead-definitions={len(refused)}",
        )
    unplaced = [f"{file}:{line}: {name} ({why})" for file, line, name, why in report.unresolved] + [
        f"{item} (public; only tests call it)" for item in listed
    ]
    if unplaced:
        return GateCheck(
            name="dead-code",
            passed=True,
            detail=TEST_ONLY_UNPROVEN + "; ".join(unplaced),
            basis=f"js-unplaced={len(unplaced)}",
        )
    return GateCheck(
        name="dead-code",
        passed=True,
        detail="no JavaScript definition added that production code does not reach",
        basis="js-reach=clean",
    )


def check_test_only_additions(
    sources: Mapping[str, str],
    added: Mapping[str, Collection[int]],
    *,
    baseline: Mapping[str, str] | None = None,
    pyproject: str | None = None,
    task_text: str | None = None,
    touched: Collection[str] = (),
) -> GateCheck:
    """A definition only a test calls is not production code.

    A run changed only browser JavaScript, which the mutation gate cannot
    see, and so added `copy_button_wiring` to a Python module with no
    production caller, plus a test that called it, so that the gate had
    something to mutate. `check_dead_additions` passed it: the function was
    public, and the test's mention counted as the tree mentioning it.

    This asks the question the other way round. Every module-level
    function, class or constant the diff adds to a non-test module -- public
    or private -- must be reached from production code: a name or attribute
    read in a non-test module outside its own body (recursion does not
    count, and neither does a read inside another definition that is itself
    only for tests), an import of it, an `__all__` entry, a decorator that
    registers it somewhere, or a `pyproject.toml` entry point. A string or a
    docstring that spells it is not a caller. Test code is `is_test_code`.

    References are matched by name, not resolved to a module, so a common
    name another module also reads passes: the rule leans towards
    accepting. Methods are not judged one by one, since a method is reached
    by protocols and overrides no name search can see; a class that is only
    for tests takes its methods with it. A definition the baseline's copy
    of the module already had is not an addition.

    Only a module production code reaches is judged: one some non-test
    module imports (matched by the module's own name, over-wide on purpose),
    that a `pyproject.toml` entry point names, or that runs as a script. A
    function added to a standalone module nothing imports -- a small library
    whose callers are its tests -- is the change itself, not padding, and
    refusing it would refuse correct work; the copy-button function sat in a
    module the whole app imports. A module that might be imported by a file
    that does not parse is judged, and so is every module when the diff also
    changes non-test source in another language (signal 3 below): there a new
    module only tests import is the padding shape, not a library.

    A file that does not parse never reads as "no references": an added
    module that does not parse, a non-test module that does and spells a
    candidate's name, or a `pyproject.toml` that does not parse while a
    candidate is unreached, fails the check naming the file.

    A library's callers are its users, so a public function added to a
    library module and called only by its tests can be the whole, correct
    change (the task said "add `sub`"): refusing it pushes the model to
    invent a production caller, the padding defect in reverse. Three
    signals separate it from the recorded case, which was a helper added
    to an application module to give a gate something to measure, in a run
    whose task never named it and whose real work was in browser files.
    (1) A private name (`_helper`) has no library excuse and is always
    refused. (2) A public name the task text spells as a whole identifier
    (`task_text`, case-sensitive) was asked for and is not judged; a short
    common word the task happens to use excuses a function of that name,
    which leans to accepting. (3) Otherwise a public name is refused when
    `touched` (every file the diff changes) holds non-test source in a
    language `OTHER_LANGUAGE_SUFFIXES` lists: the task's real work is in
    files this function does not serve, which is the recorded shape. A
    change with no such file is a Python-only change, where "a library
    function nobody has called yet" and "padding" look the same to the
    code: it passes with a detail starting `TEST_ONLY_UNPROVEN`, which the
    auditor reports `not-proven` (never a refusal) and a person reads.
    Rejected: the new test file being added by the same diff (a library
    function's test usually is too), and an ever-present task (plain
    `saddle audit` has none; the default there is signal 3 alone).
    """
    old = baseline or {}
    candidates: list[tuple[str, str, int, int]] = []
    unreadable: list[str] = []
    for path in sorted(added):
        source = sources.get(path)
        if source is None or is_test_code(path):
            continue
        try:
            tree = ast.parse(source)
        except SyntaxError:
            unreadable.append(f"{path} does not parse")
            continue
        try:
            before = set(_module_level_names(ast.parse(old[path]))) if path in old else set()
        except SyntaxError:
            before = set()
        lines = set(added[path])
        for name, statement in _module_level_names(tree).items():
            span = {n.lineno for n in ast.walk(statement) if isinstance(n, ast.stmt)}
            registered = any(
                _decorator_name(decorator) not in _TRANSPARENT_DECORATORS
                for decorator in getattr(statement, "decorator_list", ())
            )
            if (
                span <= lines
                and name not in before
                and not registered
                and not (name.startswith("__") and name.endswith("__"))
            ):
                start, end = _statement_span(statement)
                candidates.append((path, name, start, end))
    if not candidates and not unreadable:
        return GateCheck(
            name="dead-code",
            passed=True,
            detail="no function, class or constant added to a non-test module",
            basis=f"modules={len(added)}",
        )
    reads: list[tuple[str, str, int]] = []
    in_tests: dict[str, set[str]] = {}
    unparsed: dict[str, str] = {}
    imported: dict[str, set[str]] = {}
    scripts: set[str] = set()
    for path, text in sorted(sources.items()):
        try:
            tree = ast.parse(text)
        except SyntaxError:
            if not is_test_code(path):
                unparsed[path] = text
            continue
        found = _references(tree)
        if is_test_code(path):
            for name, _ in found:
                in_tests.setdefault(name, set()).add(path)
        else:
            reads.extend((path, name, line) for name, line in found)
            imported[path] = _imported_names(tree)
            if _runs_as_a_script(path, tree):
                scripts.add(path)
    entry: set[tuple[str, str]] = set()
    bad_toml = ""
    if pyproject is not None:
        try:
            entry = _entry_point_targets(pyproject)
        except tomllib.TOMLDecodeError as exc:
            bad_toml = str(exc)

    def reached(path: str) -> bool:
        where = PurePath(path)
        stem = where.parent.name if where.stem == "__init__" else where.stem
        return (
            path in scripts
            or any(module in {m for m, _ in entry} for module in _module_names(path))
            or any(stem in names for other, names in imported.items() if other != path)
            or any(stem in text for text in unparsed.values())
        )

    # The diff's real work is in another language's source: the recorded padding shape.
    padded = any(
        not is_test_code(f) and PurePath(f).suffix.lower() in OTHER_LANGUAGE_SUFFIXES
        for f in touched
    )
    # A module nothing imports is a library whose callers are its tests -- unless the
    # change's work is elsewhere, where a new module only tests import serves nothing
    # in it: saddle's audit passed exactly that beside a markdown.js fix.
    judged = [c for c in candidates if padded or reached(c[0])]
    skipped = sorted({c[0] for c in candidates} - {c[0] for c in judged})
    candidates = judged
    if not candidates and not unreadable:
        return GateCheck(
            name="dead-code",
            passed=True,
            detail="no function, class or constant added to a module production code imports"
            + (f"; not judged, nothing imports {', '.join(skipped)}" if skipped else ""),
            basis=f"modules={len(added)} unreached-modules={len(skipped)}",
        )
    holders: list[set[int | None]] = [set() for _ in candidates]
    for path, name, line in reads:
        inside = next(
            (i for i, c in enumerate(candidates) if c[0] == path and c[2] <= line <= c[3]), None
        )
        for index, (_, spelled, _, _) in enumerate(candidates):
            if spelled == name and inside != index:
                holders[index].add(inside)
    for index, (path, name, _, _) in enumerate(candidates):
        if any((module, name) in entry for module in _module_names(path)) or (
            not name.startswith("_")
            and task_text is not None
            and _names_identifier(task_text, name)
        ):
            holders[index].add(None)
    live = {i for i, held in enumerate(holders) if None in held}
    grew = True
    while grew:
        grew = False
        for index, held in enumerate(holders):
            if index not in live and held & live:
                live.add(index)
                grew = True
    dead = [i for i in range(len(candidates)) if i not in live]
    # Name what to act on: a definition that another dead one uses is cut with it.
    roots = [i for i in dead if not holders[i] & set(dead)] or dead
    listing: list[str] = []
    unplaced: list[str] = []
    for index in roots:
        path, name, _, _ = candidates[index]
        if bad_toml:
            unreadable.append(
                f"pyproject.toml does not parse ({bad_toml}), so no entry point of {name} is known"
            )
            continue
        blind = sorted(other for other, text in unparsed.items() if name in text)
        if blind:
            unreadable.append(f"{', '.join(blind)} does not parse and spells {name}")
            continue
        callers = sorted(in_tests.get(name, ()))
        how = (
            f"referenced only by {', '.join(callers)}"
            if callers
            else "used only by other definitions no production code reaches"
            if holders[index]
            else "referenced by nothing"
        )
        (listing if name.startswith("_") or padded else unplaced).append(f"{path}: {name} ({how})")
    rest = [candidates[i][1] for i in dead if i not in roots]
    if (listing or unplaced) and rest:
        more = f" (+{len(rest) - 8} more)" if len(rest) > 8 else ""
        (listing or unplaced).append(f"and what only these use: {', '.join(rest[:8])}{more}")
    if unreadable or listing:
        said = [
            *listing,
            *dict.fromkeys(f"cannot tell whether code reaches it: {why}" for why in unreadable),
        ]
        return GateCheck(
            name="dead-code",
            passed=False,
            detail=(
                "; ".join(said)
                + ". A definition that exists only for tests is not production code: "
                "wire each into production code that runs, or delete it and its tests"
            ),
            basis=f"test-only-definitions={len(dead)} unreadable={len(unreadable)}",
        )
    if unplaced:
        return GateCheck(
            name="dead-code",
            passed=True,
            detail=(
                TEST_ONLY_UNPROVEN
                + "; ".join(unplaced)
                + ". Public, and no production code reaches it; no non-Python source changed "
                "beside it and the task text does not name it. A library function a task "
                "asked for looks exactly like this, so it is not refused; code added only to "
                "give a check something to measure is a defect. A person reads it"
            ),
            basis=f"test-only-definitions={len(dead)} unproven=1",
        )
    return GateCheck(
        name="dead-code",
        passed=True,
        detail="every function, class and constant added is reached from production code"
        + (f"; not judged, nothing imports {', '.join(skipped)}" if skipped else ""),
        basis=(
            f"modules={len(added)} definitions={len(candidates)} unreached-modules={len(skipped)}"
        ),
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


RED_PHASE_NOT_SAMPLED: Final = "not measured: no baseline sample at tier 1"
"""`check_red_phase`'s detail for a run that took no baseline sample."""


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
    like a genuine one. The runner stops at a first observation of 0: any
    run of samples that starts with a pass is refused, whether the rest
    agree or not, so `(0,)` alone reads as "tests pass pre-change".

    The planner cannot waive this: whether it binds is read off the diff.
    A node that leaves every test AST untouched preserved behaviour by
    construction, so no test can fail pre-change; there the proof falls to
    changed-line coverage plus a hard mutation floor, which a tautological
    refactor cannot clear either.
    """
    # A test node has no differential: its tests are the specification
    # and they must fail now, which the tests check already observed, so
    # red-phase mirrors that verdict (`red_spec`) and has no baseline leg.
    # The impl node that depends on it takes the real differential.
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
    if not baseline_exits:
        # Tier 1 samples no baseline (`runner.run_node_gate(tier2=False)`):
        # red-phase is a tier-2 finding, and this placeholder is never read.
        return GateCheck(name="red-phase", passed=False, detail=RED_PHASE_NOT_SAMPLED)
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


type JsTestRow = tuple[str, str, str]
"""One node test as `(file, name, status)`, the status "pass", "fail" or
"skipped" (`jsevidence.JsTestResult.row`)."""

JS_NAMED: Final = 5
"""How many failing JavaScript tests a detail names; the rest are counted."""


def _js_names(rows: Sequence[JsTestRow]) -> str:
    names = [f"{file}: {name}" for file, name, _ in rows]
    more = f" and {len(names) - JS_NAMED} more" if len(names) > JS_NAMED else ""
    return "; ".join(names[:JS_NAMED]) + more


def check_js_tests(results: Sequence[JsTestRow], exit_code: int) -> GateCheck:
    """Every node test the harness ran must pass; skipped ones are counted.

    One pytest wrapper used to report a single pass or fail for the whole node
    suite, so a failing test was a line in a log. Here each test is a result:
    the detail counts them and names the failing ones. A nonzero exit with no
    failing test (the runner itself failed) is a failure too, never a pass,
    and no result at all is a failure: a run that found no test proves nothing.
    """
    passed = [r for r in results if r[2] == "pass"]
    failed = [r for r in results if r[2] == "fail"]
    skipped = [r for r in results if r[2] == "skipped"]
    counts = f"{len(passed)} passed, {len(failed)} failed, {len(skipped)} skipped"
    if not results:
        return GateCheck(
            name="js-tests", passed=False, detail=f"node --test ran no test (exit {exit_code})"
        )
    if failed:
        return GateCheck(
            name="js-tests", passed=False, detail=f"node --test: {counts}; {_js_names(failed)}"
        )
    if exit_code != 0:
        return GateCheck(
            name="js-tests",
            passed=False,
            detail=f"node --test: {counts}, yet exit {exit_code}",
        )
    return GateCheck(name="js-tests", passed=True, detail=f"node --test: {counts}")


def check_js_coverage(
    changed: Mapping[str, Collection[int]], hits: Mapping[str, Mapping[int, int]]
) -> GateCheck:
    """Every changed line of a line-measured `.js` file that c8 reports must run.

    `changed` is the changed code lines per file (blank and comment lines
    already left out: V8 reports every line, a blank one inside an unrun
    function as unrun), `hits` the lines c8 reported with their counts
    (`jsevidence.measure_coverage`). A changed line c8 does not report is not
    executable and is not judged; a reported line with no hits is named as
    `check_changed_line_coverage` names Python's, `file:line`, with no ratio.
    """
    judged = {
        (file, line)
        for file, lines in changed.items()
        for line in lines
        if line in hits.get(file, {})
    }
    missing = sorted((f, n) for f, n in judged if hits[f][n] == 0)
    if missing:
        gaps = ", ".join(f"{file}:{line}" for file, line in missing)
        return GateCheck(name="js-coverage", passed=False, detail=f"no test runs {gaps}")
    return GateCheck(
        name="js-coverage",
        passed=True,
        detail=f"every executable changed line runs ({len(judged)})",
    )


def check_js_red_phase(
    head: Sequence[JsTestRow], base: Sequence[JsTestRow], base_exit: int
) -> GateCheck:
    """The new or changed node tests must fail on the baseline's sources and
    pass on the change's, test by test.

    `head` and `base` are the results of the same test files on the two trees
    (`jsevidence.red_phase`). Pass iff every one of them passes on the head
    and at least one fails on the baseline: a file that cannot load there
    counts as a failure of its tests, as a missing module is Python's accepted
    collection error. The detail names the tests that were green on the
    baseline, which proved nothing. A baseline run that passed, or reported no
    test, fails: "tests pass pre-change; prove nothing".
    """
    if any(r[2] == "fail" for r in head):
        return GateCheck(name="js-red-phase", passed=False, detail="tests fail post-change")
    red = [r for r in base if r[2] == "fail"]
    if not red:
        why = "ran no test" if not base else "pass"
        return GateCheck(
            name="js-red-phase",
            passed=False,
            detail=f"node tests {why} pre-change (exit {base_exit}); prove nothing",
        )
    green = [r for r in base if r[2] == "pass"]
    kept = f"; green on the baseline: {_js_names(green)}" if green else ""
    return GateCheck(
        name="js-red-phase",
        passed=True,
        detail=f"{len(red)} of {len(base)} node tests fail pre-change, pass post-change{kept}",
    )


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
    """True when some `@given` property in the module rejects an input.

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
    move (CONTRIBUTING.md: "A threshold the model itself supplies lets the model
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
    gates.

    Properties are invariants over generated inputs rather than pairs the
    author chose, so the cases they probe are not the cases the author
    already had in mind. A `test` node is bound by presence. An `impl`
    node is bound by the oracle: `targets` are the property-bearing
    modules that import a changed module, and `oracle` is a mutation
    sample run with those modules alone as the test set; the property
    must kill at least one of the node's changed-line mutants, or it has
    no discriminating power over the code that implements it (T1's regex
    shipped behind a property that could not tell it from a correct one).
    No targets means no property claims this change and the check is not
    required; targets with no oracle means the runner did not run what it
    should have, which fails rather than passes. A refactor preserves the
    tests it moves and is not bound.

    Presence is floored by polarity: at least one of the `test`
    node's properties must reject an input, because T1's single property
    was positive and a validator that accepts everything satisfied it.
    Known limit: a lazy negative generator passes this floor; behavioural
    mutation is the real defence.
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
        # what narrowing the oracle to the node's test scope admits, said out loud rather than
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
        # this check reported only the second. The mutation gate
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

    The defect behind #44: the worker authors the implementation and
    the tests, so a misreading of the contract is encoded twice and the
    suite it is graded by is the suite it just rewrote. T4's worker fixed
    the wrong module, rewrote the behaviour-pinning test to match, and
    passed every Tier-1 gate then in force.

    An `impl` node may not edit tests; a `test` node may not ship the
    implementation. `refactor` may edit both sides -- a behaviour-preserving
    move carries code and its tests together, and splitting it would leave
    the first node red with nothing able to fix it -- but it may not create
    a file: that is the `impl`/`test` split done under the exempt name (#65).

    `may_create` is the `write_file` binding and it binds every
    kind, ahead of the kind branches: `allowed_tools` was validated
    against the global allowlist and then consumed by nothing, so a plan
    that withheld `write_file` still got a node that could add whatever it
    liked. The probe is the staged adds the runner already collects; no
    new subprocess runs for this.
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
    tests = sorted(path for path in changed_files if is_test_code(path))
    sources = sorted(path for path in changed_files if not is_test_code(path))
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

    Only a test that asserts something counts. Requiring an
    `Assert` or a `With` somewhere in the function is what keeps the
    tightening half of the call-shaped example rule: a function that performs the operation and
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

    From round 3f: the literal rule alone could not be
    satisfied by any real test: planners write examples as calls,
    `_asserted_literals` collects constants, and a call expression is
    never a constant. Its one satisfying source was a test that quoted
    the example as a string and asserted nothing about the behaviour --
    and even that depended on the planner's quote style surviving
    `autofix`, which it does not. So a call-shaped example is bound by
    the test that performs that operation and asserts on the values it
    was given, and the quoting escape closes with it.

    From round 3j: "asserts on the values" was read as
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

    Also from round 3j: a STRUCTURED example -- the planner's
    version-2 store record -- is not a call, so it fell to the literal
    rule and inherited exactly the defect repaired next door for calls. That
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
    still reads green -- the circularity of a worker grading itself, which survives adding
    statements unless citations are constrained to the declared set.

    `planned_ids` widens that set to the ids other nodes of the same plan
    declare: the test sources are read suite-wide, so a plan that
    gives its `test` node `REQ-001` and its `impl` node `REQ-002` cites
    both ids in one file, and with the node's own ids alone no node of
    such a plan can pass (seen in a smoke run). The first half is untouched: an
    id the node itself declares must be cited, whatever the plan holds.

    `examples` are the `(id, "accepts"|"rejects", text)` triples the
    node's requirements cite; each must be bound by some test in `suite`
    (the tests as the node leaves them; `flipped_tests` when the caller
    passes none). A statement can be cited without being tested -- T1's
    tests probed no reject at all -- and an example the planner chose is
    the one thing the party being graded did not.

    An example spelled as a literal is bound by a test that asserts on
    it. An example spelled as a **call** is bound by a test that performs
    that operation and asserts on the constants it was handed:
    binding it to the literal rule made it unsatisfiable by any test that
    asserts behaviour, and satisfiable only by one that quotes the
    example and asserts nothing.
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
        # test node's output and bills this one for it. The
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
    # The two ruff legs, already run by the runner: the current
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
    # The dead-code check's input: the lines the node added, per changed non-test module, and a
    # runner that re-runs the suite over sources with some of them gone.
    added_lines: Mapping[str, tuple[int, ...]]
    dead_code_runner: Callable[[Mapping[str, str]], int]
    baseline_sources: Mapping[str, str]
    # The workdir `changed` and `covered` are keyed against, so
    # `compelled_lines` can speak the same spelling. Empty means
    # those sets are already workdir-relative.
    workdir: str = ""
    # Nodes the plan still owes tests from. Non-empty defers an
    # uncovered changed line instead of failing the node for it.
    owed_tests: tuple[str, ...] = ()
    added_files: Collection[str] = ()
    # Repo-relative paths of every file the node changed or added.
    touched_files: Collection[str] = ()
    # The current suite run's captured text and the worktree's importable
    # top-level names; read only by a `test` node's red-specification
    # verdict.
    test_output: str = ""
    workdir_modules: Collection[str] = ()
    # Every id some node of the plan declares; the orphan half of
    # requirement-binding subtracts these before rejecting a citation
    # Empty means the node's own ids are the whole plan.
    planned_requirements: tuple[str, ...] = ()
    # The property oracle: `property_targets` are the property-bearing
    # test modules that import a changed module, and `property_oracle` is the
    # mutation sample run with those modules alone, or None when the runner
    # did not run it. Read only for `impl` nodes.
    property_oracle: MutationOutcome | None = None
    property_targets: tuple[str, ...] = ()
    # Property modules the change qualifies that the node's declared
    # pytest scope excludes, so a pass by vacuity names them.
    property_out_of_scope: tuple[str, ...] = ()
    # The audit's extra dead-code question (`check_test_only_additions`): whether anything
    # production reaches what the change added. Off for a plan's nodes, where an `impl`
    # node may be gated before the node that wires it; `pyproject_text` is the tree's
    # `pyproject.toml` (its entry points), or None when it has none.
    test_only_additions: bool = False
    pyproject_text: str | None = None
    # The task text the audit was given, if any: a public name it spells was asked for.
    task_text: str | None = None
    # The JavaScript half of that question (`check_js_test_only_additions`): the reach
    # analysis of the `.js` files the change added to, or None when it has none.
    js_dead: JsDeadReport | None = None


@dataclass(frozen=True)
class Tier1Result:
    """Aggregated Tier-1 verdict: every check runs, `passed` needs all green."""

    node_id: str
    passed: bool
    checks: tuple[GateCheck, ...]
    # What the verdict left unpinned: every surviving mutant by
    # name, and every changed line no test executed or a survivor sits on,
    # in the runner's own spelling. A recovery briefs a test node from
    # these without re-running the gate that found them.
    survivors: tuple[str, ...] = ()
    gaps: tuple[tuple[str, int], ...] = ()
    # The sampled-mutation evidence itself: killed, total, untested
    # and the survivors, which the mutation check's detail string only
    # summarises. Carried for a caller that must compare numbers; nothing in
    # `gates` reads it.
    mutation: MutationOutcome | None = None


def check_mutation(outcome: MutationOutcome, threshold: float) -> GateCheck:
    """Changed-line kill-rate must clear `threshold`; small samples take no
    partial credit, and an empty sample is no evidence rather than a pass.

    The old fail-open returned PASS when nothing was generated, reasoning
    that a change with no mutable surface cannot be under-tested. The
    premise is false: mutmut only mutates function bodies, so a
    module-scope `_RE = re.compile(...)` yields 0 mutants where the same
    expression inline yields 7. T7 added a 218-line module, generated
    nothing, and the gate passed an infinite loop.

    The threshold cannot rescue a thin sample either. T1's four-line
    regex admitted 2 mutants, two shallow tests killed both, and the gate
    read 100% over a validator that accepts `user@example..com`.
    Below MIN_SIGNIFICANT_MUTANTS a percentage is noise, so every mutant
    must die -- fewer mutants means a stricter bar, not a cheaper one.
    """
    if outcome.total == 0:
        if outcome.survivors:
            cause = ", ".join(sorted(outcome.survivors)[:5])
            # A tool that never ran is named as such; "no mutants
            # decided" describes a run that happened.
            failed_tool = any(s.startswith("mutmut run exited") for s in outcome.survivors)
            # mutmut baselines by running the suite, so a red tree fails
            # collection with the same exit a broken engine gives. Round
            # 3c's three impl attempts all died on "failed to collect
            # stats" and were reported as a tool failure the worker could
            # do nothing about; evidence marks the suite's case.
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
        # The count, then the first five names. An earlier reading took five
        # names as the whole set and called the gap an order of magnitude;
        # re-measured: 13 survivors on the gate's own population
        # against 66 on the tree, so the prefix showed 5 of 13, not 5 of
        # 66. The count is here because five names is not the set, and
        # the two numbers are different populations -- `mutation_sample`
        # keeps only mutants on a changed line, then slices to
        # `max_mutants`, while the probe scored every production file.
        names = sorted(outcome.survivors)
        shown = ", ".join(names[:5]) + (", ..." if len(names) > 5 else "")
        note = f" (small sample: {outcome.total} mutant(s), all must die)" if small else ""
        # A mutant no test runs at all is a missing test, not an
        # absence of evidence, so a failing detail names how many of the
        # survivors above are `no tests` rather than actually surviving a
        # run. Only on a fail, and only when there is at least one --
        # `untested == 0` renders byte-identical to before this task.
        untested = (
            f"; {outcome.untested} untested (no test runs the mutated function)"
            if outcome.untested
            else ""
        )
        changes = _survivor_changes(outcome, names[:5])
        return GateCheck(
            name="mutation",
            passed=False,
            detail=(
                f"killed {outcome.killed} of {outcome.total} changed-line mutants "
                f"({percent:.1f}% < {required:.1f}%){note}: survived {len(names)}: "
                f"{shown}{excluded}{untested}{changes}"
            ),
            basis=f"sampled n={outcome.total}",
        )
    return GateCheck(
        name="mutation",
        passed=True,
        detail=(
            f"killed {outcome.killed} of {outcome.total} changed-line mutants "
            f"({percent:.1f}% >= {required:.1f}%){excluded}"
        ),
        basis=f"sampled n={outcome.total}",
    )


def mutant_change(text: str) -> str:
    """What a mutant changes, from its `-`/`+` lines (`evidence.mutation_text`):
    the part of the first changed line that differs, old then new, with the
    text the two share left out but for a few characters before it, so the
    change can be found on a long line. "" when there is no line pair."""
    old = next((line[1:] for line in text.splitlines() if line.startswith("-")), None)
    new = next((line[1:] for line in text.splitlines() if line.startswith("+")), None)
    if old is None or new is None:
        return ""
    head = len(commonprefix([old, new]))
    tail = len(commonprefix([old[head:][::-1], new[head:][::-1]]))
    before, after = old[head : len(old) - tail], new[head : len(new) - tail]
    lead = old[max(0, head - 12) : head].lstrip()
    where = f" after `{lead}`" if lead else ""
    return f"`{before.strip() or before}` -> `{after.strip() or after}`{where}"


def _survivor_changes(outcome: MutationOutcome, names: Sequence[str]) -> str:
    """For each named survivor whose diff was recorded, what it changes: a name
    alone (`markdown.js:39:3 Regex`, six times over) says nothing a test can be
    written against."""
    # In order, not by name: StrykerJS names a mutant by file, line, column and mutator,
    # so six regex mutants on one line share one name.
    wanted = list(names)
    said = []
    for detail in outcome.survivor_details:
        if detail[0] in wanted and (change := mutant_change(detail[4])):
            wanted.remove(detail[0])
            said.append(f"{detail[0]}: {change}")
    return "; what they change: " + "; ".join(said) if said else ""


DEFAULT_MUTANT_SHORTLIST: Final = 5
"""How many surviving mutants a shortlist finding names (`--mutant-shortlist`)."""


def shortlist_order(details: Sequence[SurvivorDetail]) -> list[SurvivorDetail]:
    """Survivors in the order a shortlist names them: one per changed line
    first (Petrovic 2022's "at most one mutant per line"), by path and line,
    then the rest in the same order."""
    ranked = sorted(details, key=lambda d: (d[2], d[3], d[0]))
    first: list[SurvivorDetail] = []
    rest: list[SurvivorDetail] = []
    seen: set[tuple[str, int]] = set()
    for d in ranked:
        (rest if (d[2], d[3]) in seen else first).append(d)
        seen.add((d[2], d[3]))
    return first + rest


SET_ASIDE_KINDS: Final = ("equivalent", "text")
"""`mutant_text.classify` kinds the shortlist sets aside by static rule."""


def set_aside_kind(detail: SurvivorDetail) -> str | None:
    """`equivalent` or `text` when `mutant_text.classify` puts this survivor
    there, else None. A static rule over the mutant's own diff, never a
    claim: `untested` and `behaviour` survivors are never set aside."""
    _, before, after = parse_show(detail[4])
    kind = classify(detail[1], function_of(detail[0]), before, after)
    return kind if kind in SET_ASIDE_KINDS else None


def check_mutation_shortlist(
    outcome: MutationOutcome,
    threshold: float,
    *,
    shortlist: int = DEFAULT_MUTANT_SHORTLIST,
    accepted: Collection[tuple[str, int]] = (),
    sources: Mapping[str, str] | None = None,
) -> GateCheck:
    """Pass iff no surviving mutant on a changed line lacks an accepted reason.

    Scope narrowed: the kill-rate against `threshold` is still
    computed and recorded in `basis`, but it no longer decides. No source
    calibrates an 85% bar on changed-line mutants, and correct T5 trees
    scored 63-76% in a measured run. What decides is each survivor -- `no tests` ones
    included, since a mutant no test runs is a missing test --
    either dying or sitting on a line in `accepted` (a checked reason,
    `verify_untested_claims`). A survivor whose every change sits in the
    argument of a raised exception or a logging call
    (`evidence.message_only_mutant`, by AST) is excluded and counted, as
    text-only mutants are. The detail names up to `shortlist` of the open
    survivors (`shortlist_order`), each with its line, its mutation and,
    given `sources`, the changed line's text.

    Only `--tier2 shortlist` calls this; `--tier2 score` (the default)
    keeps `check_mutation`.

    An engine failure or an empty population is `check_mutation`'s verdict
    unchanged: no mutants decided is no evidence, never a pass.
    """
    if outcome.total == 0:
        return check_mutation(outcome, threshold)
    percent = 100.0 * outcome.killed / outcome.total
    basis = (
        f"sampled n={outcome.total}; score {percent:.1f}% vs {threshold:.1f}% "
        "(recorded, not decisive)"
    )
    allowed = set(accepted)
    messages = [d for d in outcome.survivor_details if d[5]]
    aside = [(d, k) for d in outcome.survivor_details if not d[5] if (k := set_aside_kind(d))]
    set_names = {d[0] for d, _ in aside}
    judged = [d for d in outcome.survivor_details if not d[5] and d[0] not in set_names]
    open_ = [d for d in shortlist_order(judged) if (d[2], d[3]) not in allowed]
    waived = len(judged) - len(open_)
    note = f"; {len(messages)} message-only survivor(s) excluded" if messages else ""
    if waived:
        note += f"; {waived} survivor(s) on lines with an accepted reason"
    untested = sum(1 for d in open_ if d[1] == "no tests")
    if untested:
        note += f"; {untested} untested (no test runs the mutated function)"
    if aside:
        note += f"; {len(aside)} set aside by a static rule"
    aside_rows = "".join(
        f"\n- set aside: {d[2]}:{d[3]} mutant {d[0]} ({k}: {PHRASES[k]})"
        for d, k in sorted(aside, key=lambda x: (x[0][2], x[0][3], x[0][0]))
    )
    if not open_:
        return GateCheck(
            name="mutation",
            passed=True,
            detail=(
                f"killed {outcome.killed} of {outcome.total} changed-line mutants; "
                f"no survivor without an accepted reason{note}" + aside_rows
            ),
            basis=basis,
        )
    shown = open_[: max(shortlist, 0)]
    rows = []
    for name, status, path, line, text, _ in shown:
        source = (sources or {}).get(path)
        lines = source.splitlines() if source is not None else []
        at = f" `{lines[line - 1].strip()}`" if 0 < line <= len(lines) else ""
        mutation = " -> ".join(t.strip() for t in text.splitlines()) or "(no diff shown)"
        rows.append(f"- {path}:{line}{at}: mutant {name} ({status}): {mutation}")
    more = f"\n(and {len(open_) - len(shown)} more)" if len(open_) > len(shown) else ""
    return GateCheck(
        name="mutation",
        passed=False,
        detail=(
            f"{len(open_)} surviving mutant(s) on changed lines have no accepted reason "
            f"(killed {outcome.killed} of {outcome.total}){note}; shortlist:\n"
            + "\n".join(rows)
            + more
            + aside_rows
        ),
        basis=basis,
    )


def _not_required(name: str) -> GateCheck:
    """A `test` node changes no source, so source-only evidence is moot."""
    return GateCheck(
        name=name, passed=True, detail="not required: no source changed", basis="test node"
    )


def _with_test_only_additions(dead: GateCheck, inputs: Tier1Inputs) -> GateCheck:
    """`dead` (the private-definition check) joined with the test-only check when asked for.

    Both answer the one `dead-code` gate: it fails when either does, and
    then says what each found.
    """
    if not inputs.test_only_additions:
        return dead
    only = check_test_only_additions(
        inputs.sources,
        inputs.added_lines,
        baseline=inputs.baseline_sources,
        pyproject=inputs.pyproject_text,
        task_text=inputs.task_text,
        touched=inputs.touched_files,
    )
    checks = [only]
    if inputs.js_dead is not None:
        checks.append(
            check_js_test_only_additions(
                inputs.js_dead, task_text=inputs.task_text, touched=inputs.touched_files
            )
        )
    failing = [c for c in checks if not c.passed]
    if not failing:
        # A placed-nowhere public name is not a refusal, but it must stay visible.
        unproven = [c for c in checks if c.detail.startswith(TEST_ONLY_UNPROVEN)]
        if unproven and dead.passed:
            detail = TEST_ONLY_UNPROVEN + "; ".join(
                c.detail.removeprefix(TEST_ONLY_UNPROVEN) for c in unproven
            )
            return GateCheck(
                name="dead-code",
                passed=True,
                detail=detail,
                basis=" ".join(c.basis or "" for c in unproven),
            )
        return dead
    if dead.passed:
        return GateCheck(
            name="dead-code",
            passed=False,
            detail="; ".join(c.detail for c in failing),
            basis=" ".join(c.basis or "" for c in failing),
        )
    return GateCheck(
        name="dead-code",
        passed=False,
        detail="; ".join([*(c.detail for c in failing), f"also {dead.detail}"]),
        basis=" ".join([*(c.basis or "" for c in failing), dead.basis or ""]),
    )


def run_tier1(node: Node, inputs: Tier1Inputs) -> Tier1Result:
    """Run all thirteen Tier-1 checks against `node`'s gate spec and aggregate.

    A `test` node is a red specification: its tests check inverts,
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
            compelled_definitions(
                inputs.baseline_sources, inputs.sources, inputs.workdir, inputs.covered
            ),
            writable=may_write_tests,
            never_run=never_run_test_lines(inputs.sources, inputs.workdir),
            packaging=packaging_script_lines(inputs.changed, inputs.workdir),
        )
    )
    checks = (
        syntax,
        ruff,
        tests,
        coverage,
        _not_required("dead-code")
        if is_spec
        else _with_test_only_additions(
            check_dead_additions(
                inputs.sources,
                inputs.added_lines,
                suite_passed=tests.passed,
                run_without=inputs.dead_code_runner,
            ),
            inputs,
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
            # A test node asserts the examples; an impl node cannot
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
        mutation=inputs.mutation,
    )


# -- task-requirements (P1): the task text's own examples, run on the tree ------

P1_REFUSAL_LICENSED: Final = False
"""Whether a refusal-eligible example that the tree fails may refuse (`code-wrong`).

False until a recorded dev-probe result meets the refusal floor with the
known-correct probe in place: until then every would-be refusal is a
question, "would refuse at full strength", and the gate's basis says it runs
at question strength. Read at each call, never bound as a default."""

P1_NAMED_LINES: Final = 5
"""How many changed `file:line` entries a finding names per example."""


@dataclass(frozen=True)
class TaskRequirementsCheck(GateCheck):
    """`check_task_requirements`'s result: a GateCheck with its four-way verdict.

    `passed` is False only for `fail`; a `question` and a `not-proven` do not
    refuse, and the auditor reports them as such, never as a pass.
    """

    verdict: Literal["pass", "fail", "question", "not-proven"] = "pass"
    rows: tuple[Row, ...] = ()
    unjudged: tuple[str, ...] = ()
    """Every unit or example the gate did not judge, named: what the packet's
    Not proven row lists (a gate that can pass on an empty judgement set says so)."""
    units: tuple[int, int, int, int] = (0, 0, 0, 0)
    """Candidate units as (total, judged, asked, unjudged): judged = cited by a
    `pass` or `code-wrong` example and by no `question`; asked = cited by a
    `question` example; unjudged = the rest. Each unit is counted once."""
    examples: tuple[int, int, int, int] = (0, 0, 0, 0)
    """Examples as (total, pass, code-wrong, question)."""


def _p1_example(row: Row) -> str:
    setup = "; ".join(row.example.setup)
    return f"{setup}; {row.example.call}" if setup else row.example.call


def _p1_line(row: Row, units: Units) -> str:
    unit = units.by_id(row.example.units[0]) if row.example.units else None
    head = row.example.units[0] if row.example.units else row.example.id
    if unit is not None:
        label = f" [{unit.label}]" if unit.label else ""
        text = unit.text if len(unit.text) <= 80 else unit.text[:77] + "..."
        head = f'{unit.id}{label} "{text}"'
    more = "".join(f" (+{u})" for u in row.example.units[1:])
    expected = row.klass.expected.show() if row.klass.expected is not None else "(split)"
    line = (
        f"{head}{more} via {row.klass.note}: {_p1_example(row)} expected {expected}, got {row.got}"
    )
    if row.ran_changed:
        line += f"; ran changed lines {', '.join(row.ran_changed)}"
    return f"{line} [{row.why}]" if row.status != "code-wrong" else line


def check_task_requirements(
    results: Mapping[str, TreeOutcome],
    units: Units,
    examples: Sequence[Example],
    *,
    changed: Collection[str] = (),
    not_executable: Sequence[tuple[str, str]] = (),
    cut: Sequence[str] = (),
    unanswered: Sequence[str] = (),
    cannot_run: str | None = None,
    licensed: bool | None = None,
) -> TaskRequirementsCheck:
    """The `task-requirements` gate over one tree's example outcomes.

    `results` maps an example id to what the driver saw; `changed` holds
    the tree's changed statements as `path:line`; `cannot_run` is why the
    gate could not run at all. The verdict, strongest first:

    - `fail` (`code-wrong`): a refusal-eligible example the tree fails,
      matching no recorded reading, while refusal is licensed;
    - `question`: P1 could not run (`cannot_run`), nothing could be judged,
      or some example is a question (`task_examples.judge`);
    - `not-proven`: some example could not be called, hung, or returned a
      value the gate cannot compare;
    - `pass`: every judged example matched.

    Every example, unit and cut is accounted for in `basis`.
    """
    allowed = P1_REFUSAL_LICENSED if licensed is None else licensed
    strength = "full strength" if allowed else "question strength: dev-probe floor unmet"
    ids = {u.id for u in units.units}
    if cannot_run is not None:
        return TaskRequirementsCheck(
            name="task-requirements",
            passed=True,
            detail=f"P1 could not run: {cannot_run}",
            basis=strength,
            verdict="question",
            units=(len(ids), 0, 0, len(ids)),
            examples=(len(examples), 0, 0, 0),
        )
    rows = []
    for example in examples:
        row = judge(
            example, classify_example(example, units), results.get(example.id), licensed=allowed
        )
        got = results.get(example.id)
        hits = [r for r in (got.ran if got else ()) if r.split(" ")[0] in changed]
        rows.append(dataclasses.replace(row, ran_changed=tuple(hits[:P1_NAMED_LINES])))
    judged = [r for r in rows if r.status in ("pass", "code-wrong", "question")]
    judged_units = {u for r in judged for u in r.example.units}
    counts = {s: sum(r.status == s for r in rows) for s in ("pass", "code-wrong", "question")}
    unjudged = (
        *(f"not executable {u}: {why}" for u, why in not_executable),
        *(f"cut by the example cap: {u}" for u in cut),
        *(f"no example and no reason given: {u}" for u in unanswered),
        *(
            f"{r.status} {r.example.id} ({', '.join(r.example.units)}): {r.why}"
            for r in rows
            if r.status in ("not-proven", "unknown", "not-judged")
        ),
    )
    basis = [
        strength,
        f"{len(judged_units)} of {len(units.units)} candidate unit(s) judged; "
        f"{len(judged)} of {len(rows)} example(s) judged: {counts['pass']} pass, "
        f"{counts['code-wrong']} code-wrong, {counts['question']} question",
        *unjudged,
    ]
    failing = [r for r in rows if r.status == "code-wrong"]
    asked = [r for r in rows if r.status == "question"]
    asked_units = ids & {u for r in asked for u in r.example.units}
    settled_units = ids & {
        u for r in [*failing, *(r for r in rows if r.status == "pass")] for u in r.example.units
    }
    settled_units -= asked_units
    unproven = [r for r in rows if r.status in ("not-proven", "unknown")]
    verdict: Literal["pass", "fail", "question", "not-proven"]
    if failing:
        verdict, lines = "fail", failing
    elif not judged:
        verdict, lines = "question", []
    elif asked:
        verdict, lines = "question", asked
    elif unproven:
        verdict, lines = "not-proven", unproven
    else:
        verdict, lines = "pass", []
    if verdict == "question" and not judged:
        detail = (
            f"P1 could not run: no example could be judged ({len(rows)} example(s): "
            f"{sum(r.status == 'not-proven' for r in rows)} could not call or compare, "
            f"{sum(r.status == 'unknown' for r in rows)} HANG or missing, "
            f"{sum(r.status == 'not-judged' for r in rows)} not judged)"
        )
    elif lines:
        detail = "; ".join(_p1_line(r, units) for r in lines)
    else:
        detail = f"{counts['pass']} example(s) matched the task text"
    return TaskRequirementsCheck(
        name="task-requirements",
        passed=verdict != "fail",
        detail=detail,
        basis="; ".join(basis),
        verdict=verdict,
        rows=tuple(rows),
        unjudged=unjudged,
        units=(
            len(ids),
            len(settled_units),
            len(asked_units),
            len(ids) - len(settled_units) - len(asked_units),
        ),
        examples=(len(rows), counts["pass"], counts["code-wrong"], counts["question"]),
    )
