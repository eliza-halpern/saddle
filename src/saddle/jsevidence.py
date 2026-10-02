"""JavaScript evidence: the harness's own `node --test` runs and StrykerJS.

Python gets its evidence from `evidence`: pytest's per-test report, a
baseline run of the node's new tests (red-phase) and mutmut over changed
lines. A browser file changed in the same diff had none of it: one pytest
wrapper reported a single pass or fail for every node test, a new `.js` test
was never run on the baseline, and no mutant was ever made of a changed `.js`
line, so a change confined to `markdown.js` was refused for want of evidence
or padded with dead Python for mutmut to mutate.

This module runs the three, over the same tree and baseline the Python
checks use, and returns plain data: `run_node_tests` the result of every test,
`red_phase` those results again on the baseline, `mutation_sample` a
`MutationOutcome` the audit counts as it counts mutmut's (`merge_outcomes`).
The verdicts are `gates.check_js_tests` and `gates.check_js_red_phase`.

Layering: sits beside `evidence` (imports it) and below `runner`.
"""

from __future__ import annotations

import fnmatch
import json
import os
import shlex
import shutil
import tempfile
import xml.etree.ElementTree as ET  # node's own report, never a document from the tree
from collections.abc import Collection, Iterator, Mapping, Sequence
from dataclasses import dataclass, replace
from pathlib import Path, PurePosixPath
from typing import Any, Final

from saddle.evidence import (
    CapturedRun,
    MutationOutcome,
    changed_lines,
    git_ls_files,
    materialize_baseline,
    mutation_text,
    run_capture,
    tree_memory_limit,
)
from saddle.gates import SHELL_TIMEOUT, TOOL_UNAVAILABLE
from saddle.journal import SpanRecorder

JS_SUFFIX: Final = ".js"
"""The one suffix the JavaScript checks measure; the audit's
`auditor.MEASURABLE_SUFFIXES` names it too."""

_TEST_NAMES: Final = ("*.test.js", "*-test.js", "*_test.js", "test-*.js")
"""The file names `node --test` runs when given a directory."""

_SKIPPED_DIRS: Final = frozenset({"node_modules", ".git", ".saddle", ".stryker-tmp"})

_TEST_DIRS: Final = frozenset({"test", "tests"})

JS_TEST_TIMEOUT_S: Final = 300
"""How long one `node --test` run of the node's JavaScript tests may take."""

STRYKER_TIMEOUT_S: Final = 600
"""The budget of one StrykerJS run, the mutmut run's own (`evidence`'s)."""

STRYKER_PACKAGE: Final = Path("node_modules/@stryker-mutator/core/bin/stryker.js")

_STRYKER_COPIED: Final = frozenset(
    {".js", ".mjs", ".cjs", ".json", ".html", ".css", ".svg", ".txt", ".md", ".png"}
)
"""The suffixes of the tracked files a mutant's test run may read: the page's
own files and the tests' fixtures. The Python tree is not copied."""

_STRYKER_NOT_COPIED: Final = frozenset({"tsconfig.json", "package-lock.json"})
"""StrykerJS 10 rewrites a `tsconfig.json` it finds with a TypeScript API the
project's compiler may not have (`ts.parseConfigFileTextToJson`), and dies; no
`.js` run needs one."""

_COMMENT: Final = ("//", "/*", "*", "*/")


def is_js_test_file(path: str) -> bool:
    """Whether `path` names a file `node --test` runs by its name."""
    name = PurePosixPath(path).name
    return any(fnmatch.fnmatch(name, pattern) for pattern in _TEST_NAMES)


def is_js_test_side(path: str) -> bool:
    """Whether `path` is test code in the sense `check_node_scope` means: a
    test file by name, or any `.js` under a `test` or `tests` directory (the
    fake DOM and the other fixtures are test code, never mutated)."""
    pure = PurePosixPath(path)
    return is_js_test_file(path) or any(part in _TEST_DIRS for part in pure.parts[:-1])


def js_files(root: Path) -> list[str]:
    """Every `.js` file under `root`, posix-relative and sorted. A walk, not a
    glob: a glob skips directories whose names start with a dot, and a census
    that reads a fraction of the files raises nothing."""
    found: list[str] = []
    for here, dirs, names in os.walk(root):
        dirs[:] = sorted(d for d in dirs if d not in _SKIPPED_DIRS)
        base = Path(here).relative_to(root)
        found.extend((base / n).as_posix() for n in names if n.endswith(JS_SUFFIX))
    return sorted(found)


def js_test_files(root: Path) -> list[str]:
    """The `node --test` test files under `root` (`is_js_test_file`)."""
    return [rel for rel in js_files(root) if is_js_test_file(rel)]


@dataclass(frozen=True)
class JsTestResult:
    """One node test: the file that holds it, its full name and its status."""

    file: str
    name: str
    status: str  # "pass", "fail" or "skipped"

    def row(self) -> tuple[str, str, str]:
        return (self.file, self.name, self.status)


@dataclass(frozen=True)
class JsTestRun:
    """What one `node --test` run reported. `problem` is why no result could be
    read (the tool missing, a report that did not parse), never an empty list
    standing for it."""

    exit_code: int
    results: tuple[JsTestResult, ...] = ()
    problem: str = ""


def _cases(
    node: ET.Element, trail: tuple[str, ...]
) -> Iterator[tuple[tuple[str, ...], ET.Element]]:
    for child in node:
        if child.tag == "testsuite":
            yield from _cases(child, (*trail, child.get("name", "")))
        elif child.tag == "testcase":
            yield trail, child


def parse_junit(text: str, root: Path) -> tuple[JsTestResult, ...]:
    """The tests of a `node --test --test-reporter=junit` report.

    A test's name is its `describe` names and its own, joined by ` > `; its file
    is relative to `root`. A `failure` or `error` child is a failure, a
    `skipped` one a skip. Raises `ValueError` on a report that does not parse
    or holds a test with no file, so a caller can name the problem."""
    try:
        tree = ET.fromstring(text)
    except ET.ParseError as exc:
        msg = f"the node test report did not parse: {exc}"
        raise ValueError(msg) from exc
    results: list[JsTestResult] = []
    for trail, case in _cases(tree, ()):
        file = case.get("file")
        if not file:
            msg = f"a test in the node report names no file: {case.get('name')!r}"
            raise ValueError(msg)
        status = "pass"
        for child in case:
            if child.tag in ("failure", "error"):
                status = "fail"
                break
            if child.tag == "skipped":
                status = "skipped"
        rel = os.path.relpath(os.path.realpath(file), os.path.realpath(root))
        name = " > ".join([*(t for t in trail if t), case.get("name", "")])
        results.append(JsTestResult(Path(rel).as_posix(), name, status))
    return tuple(results)


def run_node_tests(
    root: Path,
    files: Sequence[str],
    *,
    recorder: SpanRecorder | None = None,
    timeout: float = JS_TEST_TIMEOUT_S,
    shown: Sequence[Path] = (),
) -> JsTestRun:
    """`node --test` over `files` (relative to `root`), one result per test.

    The run is confined like every gate run on the tree's code. A run whose
    tool could not start, that timed out, or whose report does not parse
    carries a `problem` and no results."""
    if not files:
        return JsTestRun(exit_code=0)
    ran = run_capture(
        ["node", "--test", "--test-reporter=junit", *files],
        root,
        recorder=recorder,
        timeout=timeout,
        memory_limit=tree_memory_limit(),
        shown=shown,
    )
    if ran.exit_code == TOOL_UNAVAILABLE:
        return JsTestRun(ran.exit_code, problem=f"node could not be launched: {ran.stderr.strip()}")
    if ran.exit_code == SHELL_TIMEOUT:
        return JsTestRun(ran.exit_code, problem="node --test timed out")
    try:
        results = parse_junit(ran.stdout, root)
    except ValueError as exc:
        last = (ran.stderr.strip().splitlines() or ["no output"])[-1]
        return JsTestRun(ran.exit_code, problem=f"{exc} (exit {ran.exit_code}: {last})")
    return JsTestRun(ran.exit_code, results)


@dataclass(frozen=True)
class JsRedPhase:
    """The node's new or changed JavaScript tests, run on the head and on the
    baseline. `base` is the baseline run of those files alone."""

    files: tuple[str, ...]
    head: tuple[JsTestResult, ...]
    base: JsTestRun


def changed_js_tests(workdir: Path, baseline_root: Path) -> list[str]:
    """The test files of `workdir` that are new, or whose text is not the
    baseline's text at `baseline_root`."""
    changed: list[str] = []
    for rel in js_test_files(workdir):
        old = baseline_root / rel
        if not old.is_file() or old.read_bytes() != (workdir / rel).read_bytes():
            changed.append(rel)
    return changed


def red_phase(
    workdir: Path,
    baseline: str,
    head: Sequence[JsTestResult],
    *,
    recorder: SpanRecorder | None = None,
    timeout: float = JS_TEST_TIMEOUT_S,
    shown: Sequence[Path] = (),
) -> JsRedPhase | None:
    """The new or changed `.js` tests, run on the baseline's sources.

    The baseline is a checkout of `baseline`; the node's own test-side files
    (every changed or new `.js` under a test directory, the tests and their
    fixtures) are written over it, so a test that fails there fails because the
    sources are the old ones, not because a fixture it needs is missing. `head`
    is the head run's results, narrowed to the changed test files. `None` when
    no `.js` test changed."""
    with tempfile.TemporaryDirectory(prefix="saddle-js-red-") as tmp:
        old = Path(tmp)
        materialize_baseline(workdir, baseline, old, recorder=recorder)
        changed = changed_js_tests(workdir, old)
        if not changed:
            return None
        for rel in js_files(workdir):
            if is_js_test_side(rel) and (
                not (old / rel).is_file()
                or (old / rel).read_bytes() != (workdir / rel).read_bytes()
            ):
                target = old / rel
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(workdir / rel, target)
        base = run_node_tests(old, changed, recorder=recorder, timeout=timeout, shown=shown)
    return JsRedPhase(
        files=tuple(changed),
        head=tuple(r for r in head if r.file in changed),
        base=base,
    )


def changed_js_lines(workdir: Path, diff: str) -> set[tuple[str, int]]:
    """`(str(workdir / rel), line)` for every added code line of a non-test `.js`
    file that exists under `workdir`: blank lines and comment lines are not
    mutable, so they are not here. Spelled like `evidence.changed_statements`."""
    found: set[tuple[str, int]] = set()
    cache: dict[str, list[str]] = {}
    for rel, number in changed_lines(diff):
        if not rel.endswith(JS_SUFFIX) or is_js_test_side(rel):
            continue
        path = workdir / rel
        if not path.is_file():
            continue
        text = cache.setdefault(rel, path.read_text().splitlines())
        stripped = text[number - 1].strip() if 0 < number <= len(text) else ""
        if stripped and not stripped.startswith(_COMMENT):
            found.add((str(path), number))
    return found


def stryker_entry(workdir: Path, tools: Path | None = None) -> Path | None:
    """StrykerJS's script: from `workdir`'s `node_modules`, else `tools`'s (the
    checkout the staged copy was made from, which holds the ignored
    `node_modules`)."""
    for root in (workdir, tools):
        if root is not None and (root / STRYKER_PACKAGE).is_file():
            return root / STRYKER_PACKAGE
    return None


def _config(mutate: Sequence[str], tests: Sequence[str]) -> dict[str, object]:
    """StrykerJS's configuration: the command runner over `node --test`, no
    coverage analysis (the command runner has none), the JSON report."""
    return {
        "testRunner": "command",
        "commandRunner": {"command": shlex.join(["node", "--test", *tests])},
        "coverageAnalysis": "off",
        "reporters": ["json"],
        "jsonReporter": {"fileName": "stryker-report.json"},
        "mutate": list(mutate),
        "tsconfigFile": "no-such-tsconfig.json",
        "tempDirName": ".stryker-tmp",
        "concurrency": 2,
        "timeoutMS": 30000,
        "cleanTempDir": True,
    }


_KILLED: Final = frozenset({"Killed", "Timeout"})
_UNDECIDED: Final = frozenset({"Ignored", "CompileError", "Pending"})


def _ranges(lines: Collection[int]) -> list[tuple[int, int]]:
    """`lines` as inclusive runs of consecutive numbers."""
    runs: list[tuple[int, int]] = []
    for number in sorted(lines):
        if runs and number == runs[-1][1] + 1:
            runs[-1] = (runs[-1][0], number)
        else:
            runs.append((number, number))
    return runs


def _shown(rel: str, source: list[str], mutant: dict[str, object]) -> str:
    """The mutant as a `-`/`+` diff of the lines it changes: the line before,
    then the line with the mutant's replacement spliced in."""
    location = mutant["location"]
    assert isinstance(location, dict)
    start, end = location["start"], location["end"]
    first, last = start["line"], end["line"]
    before = source[first - 1 : last]
    head = source[first - 1][: start["column"] - 1]
    tail = source[last - 1][end["column"] - 1 :]
    after = (head + str(mutant.get("replacement", "")) + tail).splitlines() or [""]
    return "\n".join(
        [f"--- a/{rel}", f"+++ b/{rel}", *(f"-{x}" for x in before), *(f"+{x}" for x in after)]
    )


def mutation_sample(
    workdir: Path,
    changed: Collection[tuple[str, int]],
    *,
    tools: Path | None = None,
    recorder: SpanRecorder | None = None,
    timeout_s: int = STRYKER_TIMEOUT_S,
) -> MutationOutcome:
    """Kill rate over every StrykerJS mutant that starts on a changed `.js` line.

    Runs in a scratch copy (the tests run there, confined) under `timeout_s`,
    with `node --test` over every test file as each mutant's command. A mutant
    counts when its first line is a changed line; `Killed` and `Timeout` are
    kills, every other decided status is a survivor (as for mutmut), and
    `CompileError` or `Ignored` mutants are not in the population. Nothing
    changed is an empty outcome; StrykerJS missing or failing is a named
    survivor, never an empty population."""
    if not changed:
        return MutationOutcome(killed=0, total=0, generated=0, survivors=())
    entry = stryker_entry(workdir, tools)
    if entry is None:
        return MutationOutcome(
            killed=0, total=0, generated=0, survivors=("stryker not found in node_modules",)
        )
    root = os.path.realpath(workdir)
    by_file: dict[str, set[int]] = {}
    spelled: dict[str, str] = {}
    for path, line in changed:
        rel = Path(os.path.relpath(os.path.realpath(path), root)).as_posix()
        by_file.setdefault(rel, set()).add(line)
        spelled.setdefault(rel, path)
    with tempfile.TemporaryDirectory(prefix="saddle-stryker-") as tmp:
        scratch = Path(tmp)
        copied = [
            rel
            for rel in git_ls_files(workdir)
            if Path(rel).suffix in _STRYKER_COPIED and Path(rel).name not in _STRYKER_NOT_COPIED
        ]
        for rel in copied:
            target = scratch / rel
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(workdir / rel, target)
        tests = js_test_files(scratch)
        if not tests:
            return MutationOutcome(
                killed=0, total=0, generated=0, survivors=("no node test file in the tree",)
            )
        mutate = [
            f"{rel}:{first}-{last}"
            for rel, lines in sorted(by_file.items())
            for first, last in _ranges(lines)
        ]
        (scratch / "stryker.conf.json").write_text(json.dumps(_config(mutate, tests)))
        modules = entry.parents[3]
        ran = run_capture(
            ["node", str(entry), "run", "stryker.conf.json"],
            scratch,
            recorder=recorder,
            timeout=timeout_s,
            memory_limit=tree_memory_limit(),
            shown=[] if modules.is_relative_to(workdir) else [modules],
        )
        report = scratch / "stryker-report.json"
        if ran.exit_code not in (0, SHELL_TIMEOUT) or not report.is_file():
            return MutationOutcome(
                killed=0,
                total=0,
                generated=0,
                survivors=(_tool_failure(ran),),
                budget_spent=ran.exit_code == SHELL_TIMEOUT,
            )
        return _outcome(json.loads(report.read_text()), scratch, by_file, spelled, ran)


def _tool_failure(ran: CapturedRun) -> str:
    output = (ran.stderr.strip() or ran.stdout.strip()).splitlines()
    last = output[-1].strip() if output else "no output"
    return f"stryker run exited {ran.exit_code}: {last}"


def _outcome(
    report: Mapping[str, Any],
    scratch: Path,
    by_file: dict[str, set[int]],
    spelled: dict[str, str],
    ran: CapturedRun,
) -> MutationOutcome:
    files = report["files"]
    scored: list[tuple[str, str, str, int, str]] = []  # name, status, rel, line, show
    undecided = 0
    for rel, entry in sorted(files.items()):
        wanted = by_file.get(rel)
        if wanted is None:
            continue
        source = (scratch / rel).read_text().splitlines()
        for mutant in entry["mutants"]:
            line = mutant["location"]["start"]["line"]
            if line not in wanted:
                continue
            status = mutant["status"]
            if status in _UNDECIDED:
                undecided += 1
                continue
            name = f"{rel}:{line}:{mutant['location']['start']['column']} {mutant['mutatorName']}"
            scored.append((name, status, rel, line, _shown(rel, source, mutant)))
    survivors = [s for s in scored if s[1] not in _KILLED]
    tally: dict[str, int] = {}
    for _, status, _, _, _ in scored:
        tally[status] = tally.get(status, 0) + 1
    return MutationOutcome(
        killed=len(scored) - len(survivors),
        total=len(scored),
        generated=len(scored) + undecided,
        survivors=tuple(name for name, _, _, _, _ in survivors),
        survivor_lines=tuple(sorted({(spelled[rel], line) for _, _, rel, line, _ in survivors})),
        untested=sum(1 for s in scored if s[1] == "NoCoverage"),
        statuses=tuple(sorted(tally.items())),
        survivor_details=tuple(
            (name, status, spelled[rel], line, mutation_text(show), False)
            for name, status, rel, line, show in survivors
        ),
        mutant_detail=tuple((name, status, show) for name, status, _, _, show in scored),
        budget_spent=ran.exit_code == SHELL_TIMEOUT,
    )


def merge_outcomes(first: MutationOutcome, second: MutationOutcome) -> MutationOutcome:
    """The two mutation runs as one population: counts add, lists concatenate.

    An engine failure (`total == 0` with a survivor naming it) in either run
    stays visible in the merge's survivors, so a failed JavaScript run cannot
    hide behind a Python run that decided mutants."""
    tally: dict[str, int] = dict(first.statuses)
    for status, count in second.statuses:
        tally[status] = tally.get(status, 0) + count
    return replace(
        first,
        killed=first.killed + second.killed,
        total=first.total + second.total,
        generated=first.generated + second.generated,
        survivors=(*first.survivors, *second.survivors),
        text_only=first.text_only + second.text_only,
        survivor_lines=(*first.survivor_lines, *second.survivor_lines),
        untested=first.untested + second.untested,
        statuses=tuple(sorted(tally.items())),
        survivor_details=(*first.survivor_details, *second.survivor_details),
        mutant_detail=(*first.mutant_detail, *second.mutant_detail),
        budget_spent=first.budget_spent or second.budget_spent,
    )
