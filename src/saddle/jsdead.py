"""Which JavaScript definitions a change added does production code reach?

`jsdead.mjs` parses the tree's `.js` files with TypeScript's parser (the page's
classic scripts share globals through `<script>` tags, so reach is judged by name
across files, and by the names `index.html` itself uses). This module hands it the
changed lines, runs it confined like every other run of the tree's tooling, and returns
`gates.JsDeadReport` for `gates.check_js_test_only_additions`.

Layering: sits beside `jsevidence` (imports it) and below `runner`.
"""

from __future__ import annotations

import json
import shutil
import tempfile
from importlib import resources
from pathlib import Path
from typing import Any, Final

from saddle.evidence import changed_lines, run_capture, tree_memory_limit
from saddle.gates import (
    SHELL_TIMEOUT,
    TOOL_UNAVAILABLE,
    JsDeadFinding,
    JsDeadReport,
    is_test_code,
)
from saddle.journal import SpanRecorder
from saddle.jsevidence import is_js_test_side, js_files

SCRIPT_SUFFIXES: Final = (".js", ".mjs", ".cjs")
"""The suffixes the reach analysis reads as scripts."""

DEAD_TIMEOUT_S: Final = 120
"""How long one reach analysis may take; it parses a handful of files."""

TYPESCRIPT: Final = Path("node_modules/typescript")


def _is_test_side(path: str) -> bool:
    return is_test_code(path) or is_js_test_side(path)


def changed_script_ranges(workdir: Path, diff: str) -> dict[str, list[list[int]]]:
    """Added line ranges `[first, last]` per non-test script of `workdir` that `diff` changed."""
    by_file: dict[str, list[int]] = {}
    for rel, number in sorted(changed_lines(diff)):
        if rel.endswith(SCRIPT_SUFFIXES) and not _is_test_side(rel) and (workdir / rel).is_file():
            by_file.setdefault(rel, []).append(number)
    spans: dict[str, list[list[int]]] = {}
    for rel, numbers in by_file.items():
        for number in numbers:
            if spans.get(rel) and spans[rel][-1][1] == number - 1:
                spans[rel][-1][1] = number
            else:
                spans.setdefault(rel, []).append([number, number])
    return spans


def analyse(
    workdir: Path,
    diff: str,
    *,
    tools: Path | None = None,
    recorder: SpanRecorder | None = None,
    timeout: float = DEAD_TIMEOUT_S,
) -> JsDeadReport | None:
    """The reach analysis of the scripts `diff` changed, or None when it changed none.

    `tools` is the checkout holding the ignored `node_modules` when `workdir` (a staged
    copy) has none. A tool that could not run is a report with a `problem`, never an
    empty report."""
    added = changed_script_ranges(workdir, diff)
    if not added:
        return None
    return reach(workdir, added, tools=tools, recorder=recorder, timeout=timeout)


def reach(
    workdir: Path,
    added: dict[str, list[list[int]]],
    *,
    tools: Path | None = None,
    recorder: SpanRecorder | None = None,
    timeout: float = DEAD_TIMEOUT_S,
) -> JsDeadReport:
    """`jsdead.mjs` over `workdir`, judging the definitions wholly inside `added`'s ranges."""
    tests = [rel for rel in js_files(workdir) if _is_test_side(rel)]
    modules = next(
        (
            (base / TYPESCRIPT).parent
            for base in (workdir, tools)
            if base and (base / TYPESCRIPT).is_dir()
        ),
        None,
    )
    with tempfile.TemporaryDirectory(prefix="saddle-jsdead-") as tmp:
        script = Path(tmp) / "jsdead.mjs"
        with resources.as_file(resources.files("saddle") / "jsdead.mjs") as source:
            shutil.copyfile(source, script)
        ran = run_capture(
            [
                "node",
                str(script),
                str(workdir),
                str(modules.resolve().parent if modules else ""),
                json.dumps({"added": added, "tests": tests}),
            ],
            workdir,
            recorder=recorder,
            timeout=timeout,
            memory_limit=tree_memory_limit(),
            shown=[
                Path(tmp).resolve(),
                *(
                    [modules.resolve()]
                    if modules and not modules.resolve().is_relative_to(workdir.resolve())
                    else []
                ),
            ],
        )
    if ran.exit_code == TOOL_UNAVAILABLE:
        return JsDeadReport(problem=f"node could not be launched: {ran.stderr.strip()}")
    if ran.exit_code == SHELL_TIMEOUT:
        return JsDeadReport(problem="the analysis timed out")
    try:
        said = json.loads(ran.stdout)
    except ValueError:
        last = (ran.stderr.strip().splitlines() or ["no output"])[-1]
        return JsDeadReport(problem=f"its output did not parse (exit {ran.exit_code}: {last})")
    if "unavailable" in said:
        return JsDeadReport(problem=str(said["unavailable"]))
    return report_from(said)


def report_from(said: dict[str, Any]) -> JsDeadReport:
    """`gates.JsDeadReport` from the script's JSON."""
    return JsDeadReport(
        findings=tuple(
            JsDeadFinding(f["file"], f["line"], f["name"], f["kind"], tuple(f["callers"]))
            for f in said["findings"]
        ),
        unresolved=tuple((u["file"], u["line"], u["name"], u["why"]) for u in said["unresolved"]),
        also=tuple(said["also"]),
    )
