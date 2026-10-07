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
import re
import shlex
import shutil
import tempfile
import xml.etree.ElementTree as ET  # node's own report, never a document from the tree
from collections.abc import Collection, Iterator, Mapping, Sequence
from dataclasses import dataclass, field, replace
from pathlib import Path, PurePosixPath
from typing import Any, Final

from saddle import sandbox
from saddle.evidence import (
    CapturedRun,
    MutationOutcome,
    changed_lines,
    git_ls_files,
    materialize_baseline,
    mutation_text,
    run_capture,
    src_layout_env,
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


C8_PACKAGE: Final = Path("node_modules/c8/bin/c8.js")

COVERAGE_SCOPE: Final = "tests/fixtures/js_coverage_scope.json"
"""The tracked file naming which `.js` files the node tests are held to line
coverage on (`measured`) and which are knowingly outside it (`not_measured`,
each with its reason)."""

NOT_LINE_MEASURED: Final = "not line-measured"
"""What a changed line of a `.js` file reads when no line coverage exists for it."""


@dataclass(frozen=True)
class CoverageScope:
    """`COVERAGE_SCOPE` as data. `problem` is why it could not be read: an
    unreadable scope leaves every file unlisted, never a silent all-measured."""

    measured: frozenset[str] = frozenset()
    not_measured: Mapping[str, str] = field(default_factory=dict)
    chrome_measured: frozenset[str] = frozenset()
    chrome_tests: tuple[str, ...] = ()
    chrome_helper: str = ""
    """Where the project shows how to write a Chrome-driven test (`chrome_test_helper`):
    named when a changed page-script line runs in no test, so the gap says how to close it."""
    problem: str = ""


def read_coverage_scope(workdir: Path) -> CoverageScope:
    """The tree's `COVERAGE_SCOPE`; a tree without one has an empty scope (no
    file is line-measured), a malformed one carries a `problem`."""
    path = workdir / COVERAGE_SCOPE
    if not path.is_file():
        return CoverageScope()
    try:
        data = json.loads(path.read_text())
        return CoverageScope(
            frozenset(data["measured"]),
            {str(k): str(v) for k, v in data["not_measured"].items()},
            frozenset(data.get("chrome_measured", {})),
            tuple(str(t) for t in data.get("chrome_tests", ())),
            str(data.get("chrome_test_helper", "")),
        )
    except (ValueError, KeyError, TypeError, AttributeError) as exc:
        return CoverageScope(problem=f"{COVERAGE_SCOPE} could not be read: {exc!r}")


CHROME_COVERAGE_ENV: Final = "SADDLE_JS_COVERAGE_DIR"
"""The directory the Chrome-driven tests write V8 coverage into when it is set."""

CHROME_RC: Final = ".c8rc.chrome.json"
"""c8's configuration for the page scripts the Chrome-driven tests load."""

NO_CHROME: Final = "no Chrome ran"
"""Why the page scripts have no line coverage when the Chrome-driven tests left
none (no browser, or every one of them skipped)."""


def c8_entry(workdir: Path, tools: Path | None = None) -> Path | None:
    """c8's script, found as `stryker_entry` finds StrykerJS."""
    for root in (workdir, tools):
        if root is not None and (root / C8_PACKAGE).is_file():
            return root / C8_PACKAGE
    return None


@dataclass(frozen=True)
class JsCoverage:
    """c8's per-line execution counts: `lines[file][line]` is how often a test
    ran it, for every line V8 reports. `problem` is why none could be had."""

    lines: Mapping[str, Mapping[int, int]] = field(default_factory=dict)
    problem: str = ""


def parse_lcov(text: str, root: Path) -> dict[str, dict[int, int]]:
    """`{file relative to root: {line: hits}}` from an lcov report's `SF` and
    `DA` records. Raises `ValueError` on a `DA` record outside a file or one
    that is not two integers: a report that does not parse is a problem to
    name, never zero lines."""
    found: dict[str, dict[int, int]] = {}
    current: dict[int, int] | None = None
    for raw in text.splitlines():
        if raw.startswith("SF:"):
            name = raw[3:]
            rel = os.path.relpath(name, root) if os.path.isabs(name) else os.path.normpath(name)
            current = found.setdefault(Path(rel).as_posix(), {})
        elif raw.startswith("DA:"):
            if current is None:
                msg = f"an lcov DA record outside a file: {raw!r}"
                raise ValueError(msg)
            number, hits = raw[3:].split(",")[:2]
            current[int(number)] = int(hits)
    return found


def measure_coverage(
    workdir: Path,
    include: Sequence[str],
    tests: Sequence[str],
    *,
    tools: Path | None = None,
    recorder: SpanRecorder | None = None,
    timeout: float = JS_TEST_TIMEOUT_S,
) -> JsCoverage:
    """Per-line counts for the `include` files (relative to `workdir`) while
    every `tests` file runs under c8.

    The report and c8's raw data go to a temp directory, never into the tree,
    and c8's own thresholds are off: the audit judges the changed lines, not a
    percentage. A run that could not start, timed out, failed a test, or left no
    report names the problem and returns no lines: coverage read off a red
    suite would be a number about the wrong program."""
    entry = c8_entry(workdir, tools)
    if entry is None:
        return JsCoverage(problem="c8 was not found in node_modules")
    entry = entry.resolve()
    modules = entry.parents[2]
    with tempfile.TemporaryDirectory(prefix="saddle-c8-") as tmp:
        out = Path(tmp)
        ran = run_capture(
            [
                "node",
                str(entry),
                "--reporter=lcov",
                f"--reports-dir={out / 'report'}",
                f"--temp-directory={out / 'v8'}",
                "--check-coverage=false",
                "--all",
                *(f"--include={rel}" for rel in include),
                "node",
                "--test",
                *tests,
            ],
            workdir,
            recorder=recorder,
            timeout=timeout,
            memory_limit=tree_memory_limit(),
            writable=[out],
            shown=[] if modules.is_relative_to(workdir.resolve()) else [modules],
        )
        if ran.exit_code == TOOL_UNAVAILABLE:
            return JsCoverage(problem=f"c8 could not be launched: {ran.stderr.strip()}")
        if ran.exit_code == SHELL_TIMEOUT:
            return JsCoverage(problem="c8 timed out")
        if ran.exit_code != 0:
            last = (ran.stderr.strip().splitlines() or ["no output"])[-1]
            return JsCoverage(problem=f"the node tests under c8 exited {ran.exit_code}: {last}")
        report = out / "report" / "lcov.info"
        if not report.is_file():
            return JsCoverage(problem="c8 wrote no lcov report")
        try:
            return JsCoverage(parse_lcov(report.read_text(), workdir))
        except ValueError as exc:
            return JsCoverage(problem=f"the c8 report did not parse: {exc}")


def measure_chrome_coverage(
    workdir: Path,
    tests: Sequence[str],
    *,
    tools: Path | None = None,
    recorder: SpanRecorder | None = None,
    timeout: float = JS_TEST_TIMEOUT_S,
    workers: int = 1,
) -> JsCoverage:
    """Per-line counts of the page scripts while the Chrome-driven pytest files
    `tests` (relative to `workdir`) run in a real Chrome.

    The tests run confined, with `CHROME_COVERAGE_ENV` naming a scratch
    directory outside the tree that the drivers write V8 coverage into; c8 then
    reports it under `CHROME_RC`. A page script no test loaded is reported with
    every line unrun, so a run that left no coverage files must never reach c8:
    it is the problem `NO_CHROME`, as is a run in which any test skipped (a
    skipped driver's lines would read as unrun, a failure the change did not
    earn). A red suite, a timeout, or a report that does not parse is a problem
    too: coverage read off a failed run is a number about the wrong program."""
    entry = c8_entry(workdir, tools)
    if entry is None:
        return JsCoverage(problem="c8 was not found in node_modules")
    entry = entry.resolve()
    modules = entry.parents[2]
    shown = [] if modules.is_relative_to(workdir.resolve()) else [modules]
    with tempfile.TemporaryDirectory(prefix="saddle-chrome-") as tmp:
        out = Path(tmp)
        raw = out / "v8"
        raw.mkdir()
        ran = run_capture(
            [
                "python",
                "-m",
                "pytest",
                "-q",
                "--no-cov",
                "-p",
                "no:cacheprovider",
                *(["-n", str(workers)] if workers > 1 else []),
                *tests,
            ],
            workdir,
            recorder=recorder,
            timeout=timeout,
            memory_limit=tree_memory_limit(),
            writable=[out],
            extra_env={**src_layout_env(workdir), CHROME_COVERAGE_ENV: str(raw)},
            shown=shown,
        )
        if ran.exit_code == TOOL_UNAVAILABLE:
            return JsCoverage(
                problem=f"the Chrome tests could not be launched: {ran.stderr.strip()}"
            )
        if ran.exit_code == SHELL_TIMEOUT:
            return JsCoverage(problem="the Chrome tests timed out")
        if ran.exit_code != 0:
            last = (ran.stdout.strip().splitlines() or ["no output"])[-1]
            return JsCoverage(problem=f"the Chrome tests exited {ran.exit_code}: {last}")
        skipped = re.search(r"(\d+) skipped", ran.stdout)
        if skipped or not any(raw.glob("coverage-*.json")):
            why = (
                f"{skipped.group(1)} Chrome tests skipped, and a skip leaves every page"
                " line unproven: run the `chrome_tests` and make each one run"
                if skipped
                else NO_CHROME
            )
            return JsCoverage(problem=why)
        report = run_capture(
            [
                "node",
                str(entry),
                "report",
                f"--config={CHROME_RC}",
                f"--temp-directory={raw}",
                "--reporter=lcovonly",
                f"--reports-dir={out / 'report'}",
                "--check-coverage=false",
            ],
            workdir,
            recorder=recorder,
            timeout=timeout,
            memory_limit=tree_memory_limit(),
            writable=[out],
            shown=shown,
        )
        if report.exit_code != 0:
            last = (report.stderr.strip().splitlines() or ["no output"])[-1]
            return JsCoverage(
                problem=f"c8 report of the Chrome coverage exited {report.exit_code}: {last}"
            )
        lcov = out / "report" / "lcov.info"
        if not lcov.is_file():
            return JsCoverage(problem="c8 wrote no lcov report of the Chrome coverage")
        try:
            return JsCoverage(parse_lcov(lcov.read_text(), workdir))
        except ValueError as exc:
            return JsCoverage(problem=f"the c8 report of the Chrome coverage did not parse: {exc}")


def stryker_entry(workdir: Path, tools: Path | None = None) -> Path | None:
    """StrykerJS's script: from `workdir`'s `node_modules`, else `tools`'s (the
    checkout the staged copy was made from, which holds the ignored
    `node_modules`)."""
    for root in (workdir, tools):
        if root is not None and (root / STRYKER_PACKAGE).is_file():
            return root / STRYKER_PACKAGE
    return None


def _config(mutate: Sequence[str], tests: Sequence[str]) -> dict[str, object]:
    """StrykerJS's configuration: the command runner over `node --test` of
    `tests`, no coverage analysis, the JSON report.

    `coverageAnalysis` stays "off" because the command runner reports no
    per-test coverage: asked for "perTest" it still runs its one synthetic
    test, the whole command, for every mutant. The scoping is done by `_reach`
    instead, which chooses `tests` and tells `_outcome` which mutants no test
    executes."""
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


def stryker_invocation(tests: Sequence[str]) -> str:
    """How the audit runs StrykerJS over `tests`, as a sentence a worker can
    copy: the command and its configuration file (`_config`, so it cannot
    drift from the audit's own). A run guessed at flags ("unknown option
    '--config'") and built a sweep of its own, then the audit scored a
    configuration it was never shown (#176)."""
    config = json.dumps(_config(["<file>:<first line>-<last line>"], tests))
    return (
        f"`node {STRYKER_PACKAGE} run stryker.conf.json`, where stryker.conf.json is "
        f"{config}; the configuration file is the argument to `run`, there is no "
        "`--config` option"
    )


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


def _line_starts(text: str) -> list[int]:
    """V8's offset (UTF-16 code units) of each line's first character."""
    starts, here = [], 0
    for line in text.split("\n"):
        starts.append(here)
        here += len(line.encode("utf-16-le")) // 2 + 1
    return starts


def _covered(ranges: Sequence[tuple[int, int, int]], offset: int) -> bool:
    """Whether V8's block coverage `ranges` (start, end, count) say `offset`
    ran: the innermost range holding it decides, as nested ranges override."""
    inner: tuple[int, int, int] | None = None
    for start, end, count in ranges:
        if start <= offset < end and (inner is None or end - start <= inner[1] - inner[0]):
            inner = (start, end, count)
    return inner is not None and inner[2] > 0


def _v8_ranges(cov_dir: Path, url: str) -> list[tuple[int, int, int]] | None:
    """Every block range V8 recorded for the script `url` under `cov_dir`, or
    None when no coverage file could be read at all (a run that did not
    record is unknown, never "nothing ran")."""
    found: list[tuple[int, int, int]] = []
    readable = False
    for report in sorted(cov_dir.glob("*.json")):
        try:
            result = json.loads(report.read_text())["result"]
        except (ValueError, KeyError, OSError):
            continue
        readable = True
        for script in result:
            if script.get("url") == url:
                found.extend(
                    (r["startOffset"], r["endOffset"], r["count"])
                    for fn in script["functions"]
                    for r in fn["ranges"]
                )
    return found if readable else None


@dataclass(frozen=True)
class Reach:
    """Which of a tree's node test files execute which parts of the changed
    files, from one V8-coverage run per test file: `tests` are the files that
    execute a changed line, `ranges[test][rel]` is each one's block coverage."""

    tests: tuple[str, ...]
    ranges: dict[str, dict[str, list[tuple[int, int, int]]]]

    def reached(self, rel: str, offset: int) -> bool:
        return any(_covered(per.get(rel, ()), offset) for per in self.ranges.values())


def _reach(
    scratch: Path,
    tests: Sequence[str],
    by_file: dict[str, set[int]],
    *,
    recorder: SpanRecorder | None,
    timeout_s: int,
    shown: Sequence[Path],
) -> Reach | None:
    """Run each test file alone under V8 coverage and keep the ones that
    execute a changed line (the JavaScript counterpart of `covering_tests`).

    None when any run recorded nothing readable: the caller then runs every
    test file and counts every mutant as it was, never a smaller suite chosen
    from coverage that was not there."""
    ranges: dict[str, dict[str, list[tuple[int, int, int]]]] = {}
    chosen: list[str] = []
    starts = {rel: _line_starts((scratch / rel).read_text()) for rel in by_file}
    texts = {rel: (scratch / rel).read_text().split("\n") for rel in by_file}
    for index, test in enumerate(tests):
        cov_dir = scratch / ".v8-coverage" / str(index)
        cov_dir.mkdir(parents=True)
        run_capture(
            ["node", "--test", test],
            scratch,
            recorder=recorder,
            timeout=timeout_s,
            memory_limit=tree_memory_limit(),
            extra_env={"NODE_V8_COVERAGE": str(cov_dir)},
            shown=shown,
        )
        per: dict[str, list[tuple[int, int, int]]] = {}
        for rel in by_file:
            found = _v8_ranges(cov_dir, (scratch / rel).as_uri())
            if found is None:
                return None
            per[rel] = found
        ranges[test] = per
        if any(
            _covered(per[rel], starts[rel][line - 1] + column)
            for rel, lines in by_file.items()
            for line in lines
            for column in range(len(texts[rel][line - 1]))
            if not texts[rel][line - 1][column].isspace()
        ):
            chosen.append(test)
    return Reach(tuple(chosen), ranges)


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
    with `node --test` over the test files that execute a changed line (all of
    them when coverage could not be read) as each mutant's command. A mutant
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
    entry = entry.resolve()  # the confined run sees real paths, not the link a checkout may hold
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
        # Shown wherever it lives: the run is confined to the scratch copy, which
        # never holds the tree, so a tree's own node_modules is as unseen as a
        # checkout's. Hiding it there crashed StrykerJS before any mutant ran.
        seen = [entry.parents[3]]
        reach = _reach(scratch, tests, by_file, recorder=recorder, timeout_s=timeout_s, shown=seen)
        # No test file executes a changed line: the suite still runs once (the
        # dry run needs a green command) and every mutant reads "no coverage".
        chosen = list(reach.tests) if reach and reach.tests else tests
        (scratch / "stryker.conf.json").write_text(json.dumps(_config(mutate, chosen)))
        ran = run_capture(
            ["node", str(entry), "run", "stryker.conf.json"],
            scratch,
            recorder=recorder,
            timeout=timeout_s,
            memory_limit=tree_memory_limit(),
            shown=seen,
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
        return _outcome(
            json.loads(report.read_text()), scratch, by_file, spelled, ran, reach, chosen
        )


UNTESTED_SHOWN: Final = 5
"""How many of the node test files `untested_note` names before "and N more"."""


def untested_note(tests: Sequence[str]) -> str:
    """What the mutation detail adds after its untested count: the test files
    StrykerJS ran, and that only `node --test` files count. A watched run took
    "no test runs the mutated function" to mean its browser-driven tests were
    unseen by accident, and went reading the harness to find out why."""
    shown = ", ".join(tests[:UNTESTED_SHOWN])
    more = f" and {len(tests) - UNTESTED_SHOWN} more" if len(tests) > UNTESTED_SHOWN else ""
    return (
        f" (JavaScript mutants are run against node --test files only, here {shown}{more};"
        " a test that drives the code through a browser never counts, so call the changed"
        " code from a node test file)"
    )


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
    reach: Reach | None = None,
    ran_tests: Sequence[str] = (),
) -> MutationOutcome:
    files = report["files"]
    scored: list[tuple[str, str, str, int, str]] = []  # name, status, rel, line, show
    undecided = 0
    for rel, entry in sorted(files.items()):
        wanted = by_file.get(rel)
        if wanted is None:
            continue
        source = (scratch / rel).read_text().splitlines()
        starts = _line_starts((scratch / rel).read_text())
        for mutant in entry["mutants"]:
            line = mutant["location"]["start"]["line"]
            if line not in wanted:
                continue
            status = mutant["status"]
            offset = starts[line - 1] + mutant["location"]["start"]["column"] - 1
            if status == "Survived" and reach is not None and not reach.reached(rel, offset):
                status = "NoCoverage"  # the command runner cannot say so; V8 coverage can
            if status in _UNDECIDED:
                undecided += 1
                continue
            name = f"{rel}:{line}:{mutant['location']['start']['column']} {mutant['mutatorName']}"
            scored.append((name, status, rel, line, _shown(rel, source, mutant)))
    survivors = [s for s in scored if s[1] not in _KILLED]
    untested = sum(1 for s in scored if s[1] == "NoCoverage")
    tally: dict[str, int] = {}
    for _, status, _, _, _ in scored:
        tally[status] = tally.get(status, 0) + 1
    return MutationOutcome(
        killed=len(scored) - len(survivors),
        total=len(scored),
        generated=len(scored) + undecided,
        survivors=tuple(name for name, _, _, _, _ in survivors),
        survivor_lines=tuple(sorted({(spelled[rel], line) for _, _, rel, line, _ in survivors})),
        untested=untested,
        untested_note=untested_note(ran_tests) if untested else "",
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
    is the merge: a failed JavaScript run cannot hide behind a Python run that
    decided mutants, nor the reverse, and "no mutants decided" names the tool."""
    for failed in (first, second):
        if failed.total == 0 and failed.survivors:
            return failed
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
        untested_note=first.untested_note or second.untested_note,
        statuses=tuple(sorted(tally.items())),
        survivor_details=(*first.survivor_details, *second.survivor_details),
        mutant_detail=(*first.mutant_detail, *second.mutant_detail),
        budget_spent=first.budget_spent or second.budget_spent,
    )


def main(argv: Sequence[str]) -> int:
    """`ci-mutate.sh`'s JavaScript phase: StrykerJS over the changed lines of the
    non-test `.js` files `argv[0]...HEAD` changed, in the current directory.

    Prints one line per survivor and returns 1 when any survived or the tool
    failed (a named survivor, never a pass), 2 when git could not say what
    changed, else 0. Nothing changed, or no mutants made of what did, is 0."""
    root = Path.cwd()
    diff = run_capture(["git", "diff", "-U0", f"{argv[0]}...HEAD", "--", "*.js"], root)
    if diff.exit_code != 0:
        print(f"git diff {argv[0]}...HEAD failed: {diff.stderr.strip()}")
        return 2
    changed = changed_js_lines(root, diff.stdout)
    if not changed:
        print("no JavaScript lines changed; skipping")
        return 0
    with sandbox.also_exposing(["node"]):
        out = mutation_sample(root, changed, tools=root)
    print(f"javascript mutants: {out.killed} of {out.total} killed, {out.generated} generated")
    for name in out.survivors:
        print(f"survived: {name}")
    return 1 if out.survivors else 0
