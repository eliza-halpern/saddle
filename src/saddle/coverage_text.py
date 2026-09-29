"""The coverage finding in English, at function level, compiled from the record.

`check_changed_line_coverage` seals a finding whose `detail` reads
``no test runs money.py:131, money.py:132, ...`` and whose cites carry the
counts (``changed-lines=155 compelled-lines=5``). This module maps each
listed ``file:line`` to its enclosing function with `ast` on the tree and
renders one line per function, in the voice of `mutant_text`: one item per
line, every line carrying the record it was read from, nothing asserted that
the record does not hold.

The heading is "Not proven by any test", never a failure: under the
shortlist design coverage is a locator whose verdict reads "not proven"
(PREREG addendum-EAFS §S3). This module changes no gate.

A line outside every function is "module level". A row says "nothing
exercises <function>" only when the finding lists every statement of that
function's body (`evidence.statement_lines`, the gate's own reading) as
never run, which is what the record holds for a function no test entered.
Otherwise some statement of it may have run -- a test that runs 27 of its
30 lines is not "not exercised" -- and the row says "no test reaches these
lines of <function>". Either way the function is its name, plus its
docstring's first line quoted when it has one; no other prose is written
about the code. Where
`mutant_text` recorded a surviving behaviour mutant in the same file and
function, the line says so; nothing else links the two rows.
"""

from __future__ import annotations

import ast
import re
from collections.abc import Collection, Mapping, Sequence
from dataclasses import dataclass

from saddle.evidence import statement_lines
from saddle.mutant_text import MutationSummary

MODULE_LEVEL = "module level"
HEADING = "Not proven by any test"

_LINE = re.compile(r"([^\s,]+):(\d+)")


def uncovered_lines(detail: str) -> list[tuple[str, int]]:
    """The ``file:line`` pairs a coverage finding's detail names, in order."""
    if not detail.startswith("no test runs "):
        return []
    return [(f, int(n)) for f, n in _LINE.findall(detail[len("no test runs ") :])]


@dataclass(frozen=True)
class _Scope:
    qualname: str
    first: int
    last: int
    doc: str | None
    body: int


def _scopes(source: str) -> list[_Scope]:
    out: list[_Scope] = []

    def visit(node: ast.AST, prefix: str) -> None:
        for child in ast.iter_child_nodes(node):
            if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                name = f"{prefix}{child.name}"
                if not isinstance(child, ast.ClassDef):
                    first = min([child.lineno] + [d.lineno for d in child.decorator_list])
                    doc = ast.get_docstring(child)
                    head = doc.strip().splitlines()[0].strip() if doc and doc.strip() else None
                    out.append(
                        _Scope(
                            name,
                            first,
                            child.end_lineno or child.lineno,
                            head,
                            child.body[0].lineno,
                        )
                    )
                visit(child, f"{name}.")

    visit(ast.parse(source), "")
    return out


def function_at(scopes: list[_Scope], line: int) -> _Scope | None:
    """The innermost function whose span (decorators included) holds `line`."""
    holding = [s for s in scopes if s.first <= line <= s.last]
    return max(holding, key=lambda s: s.first) if holding else None


def phrase(function: str, doc: str | None) -> str:
    """What the sentence says nothing exercises: the name, and the docstring's line if any."""
    if function == MODULE_LEVEL:
        return "the module-level code on these lines"
    if not doc:
        return function
    return f'{function} ("{doc.replace("``", "")}")'


def _same_file(recorded: str, named: str) -> bool:
    """`changed` spells paths `str(workdir / rel)`; the detail spells them `rel`."""
    return recorded == named or recorded.endswith("/" + named)


@dataclass(frozen=True)
class FunctionGap:
    file: str
    function: str
    doc: str | None
    uncovered: tuple[int, ...]
    changed: int | None
    mutant_survived: bool = False
    # The uncovered lines' own text, rstripped, from the same source the
    # function was placed in: only the lines the gate judged, never the
    # whole function. Empty for a file the tree did not hold.
    text: tuple[tuple[int, str], ...] = ()
    # The finding lists every statement of the function's body as never run:
    # the one case "nothing exercises <function>" is what the record holds.
    never_ran: bool = False


@dataclass(frozen=True)
class CoverageSummary:
    detail_lines: int
    cites: tuple[str, ...]
    gaps: tuple[FunctionGap, ...]
    # file:line pairs whose file the tree did not hold: named, never placed.
    unplaced: tuple[tuple[str, int], ...] = ()


def describe_coverage(
    finding: Mapping[str, object],
    sources: Mapping[str, str],
    changed: Collection[tuple[str, int]] | None = None,
    mutation: MutationSummary | None = None,
    *,
    lines: Sequence[tuple[str, int]] | None = None,
) -> CoverageSummary:
    """Group a sealed coverage finding's lines by enclosing function.

    `sources` maps each file the detail names to its text in the audited
    tree. `changed` is the node's changed-line set (`evidence.changed_statements`
    of the diff, the set the gate judged) when it is at hand; without it no "of M" is written.
    `mutation` is `mutant_text`'s summary of the same tree, if any. `lines`
    are the lines the finding names, when they were sealed whole beside it
    (`auditor.coverage_evidence`); else they are read from its detail.
    """
    lines = list(lines) if lines is not None else uncovered_lines(str(finding.get("detail", "")))
    parsed = {f: _scopes(sources[f]) for f in {f for f, _ in lines} if f in sources}
    survived = {(m.file, m.function) for m in mutation.gaps} if mutation else set()
    grouped: dict[tuple[str, str], list[int]] = {}
    docs: dict[tuple[str, str], str | None] = {}
    spans: dict[tuple[str, str], _Scope | None] = {}
    unplaced = []
    for file, line in lines:
        if file not in parsed:
            unplaced.append((file, line))
            continue
        scope = function_at(parsed[file], line)
        key = (file, scope.qualname if scope else MODULE_LEVEL)
        grouped.setdefault(key, []).append(line)
        docs[key] = scope.doc if scope else None
        spans[key] = scope
    listed: dict[str, set[int]] = {}
    for file, line in lines:
        listed.setdefault(file, set()).add(line)
    statements = {f: statement_lines(sources[f]) for f in parsed}
    gaps = []
    for (file, func), nums in grouped.items():
        span = spans[(file, func)]
        body = (
            {n for n in statements[file] if span.body <= n <= span.last}
            if span is not None
            else set()
        )
        source_lines = sources[file].splitlines()
        text = tuple((n, source_lines[n - 1].rstrip()) for n in nums if 1 <= n <= len(source_lines))
        count = None
        if changed is not None:
            scope = spans[(file, func)]
            count = sum(
                1
                for f, n in changed
                if _same_file(f, file)
                and (
                    (scope is not None and scope.first <= n <= scope.last)
                    or (scope is None and function_at(parsed[file], n) is None)
                )
            )
        gaps.append(
            FunctionGap(
                file,
                func,
                docs[(file, func)],
                tuple(nums),
                count,
                (file, func) in survived,
                text,
                bool(body) and body <= listed[file],
            )
        )
    raw = finding.get("cites")
    cites = raw if isinstance(raw, (list, tuple)) else ()
    return CoverageSummary(
        len(lines),
        tuple(c for c in cites if isinstance(c, str) and "=" in c),
        tuple(gaps),
        tuple(unplaced),
    )


def gap_sentence(g: FunctionGap) -> str:
    """`<file> <function>: N of M changed lines never run -- <what> [lines]`.

    <what> is "nothing exercises <phrase>" for module-level lines and for a
    function whose whole body the finding lists (`FunctionGap.never_ran`);
    for any other function it is "no test reaches these lines of <phrase>".
    """
    n = len(g.uncovered)
    count = f"{n} of {g.changed} changed lines" if g.changed is not None else f"{n} changed lines"
    tail = "; and a mutant there survived" if g.mutant_survived else ""
    nums = ", ".join(str(x) for x in g.uncovered)
    named = phrase(g.function, g.doc)
    if g.never_ran or g.function == MODULE_LEVEL:
        what = f"nothing exercises {named}"
    else:
        what = f"no test reaches these lines of {named}"
    return f"{g.file} {g.function}: {count} never run -- {what}{tail} [lines {nums}]"


def line_text(g: FunctionGap) -> list[str]:
    """The uncovered lines themselves, one per line, `<number>: <text>`, under the bullet."""
    return [f"      {n}: {text}" for n, text in g.text]


COMPACT_CAP = 5
"""Functions listed in the compact rendering before "and N more functions in the packet"."""
COMPACT_LINES = 2
"""Uncovered lines' text shown per function in the compact rendering."""


def _headline(s: CoverageSummary) -> str:
    basis = " ".join(s.cites)
    return (
        f"{HEADING}: {s.detail_lines} changed lines no test runs "
        f"[record: detail names {s.detail_lines} lines{'; ' + basis if basis else ''}]"
    )


def render_compact(summary: CoverageSummary, *, text: bool = True) -> str:
    """The coverage row for the recap, which must not scroll.

    The headline; the functions with the most uncovered lines first, capped
    at `COMPACT_CAP` with "and N more functions in the packet"; under each,
    at most `COMPACT_LINES` of its uncovered lines' text, then "and N more
    lines"; unplaced lines as one count. Same sentences and tags as the full
    rendering.
    """
    s = summary
    if not s.detail_lines:
        return ""
    out = [_headline(s)]
    gaps = sorted(s.gaps, key=lambda g: -len(g.uncovered))
    for g in gaps[:COMPACT_CAP]:
        out.append(f"  - {gap_sentence(g)}")
        if text:
            out.extend(line_text(g)[:COMPACT_LINES])
            if len(g.text) > COMPACT_LINES:
                out.append(f"      and {len(g.text) - COMPACT_LINES} more lines")
    if len(gaps) > COMPACT_CAP:
        out.append(f"  and {len(gaps) - COMPACT_CAP} more functions in the packet")
    if s.unplaced:
        out.append(f"  {len(s.unplaced)} lines not placed (file not in the tree read)")
    return "\n".join(out) + "\n"


TALLY_FILES = 6
"""Files `file_tally` names before "and N more files"."""


def file_tally(summary: CoverageSummary, *, top: int = TALLY_FILES) -> str:
    """One line for text read under a cap: uncovered lines per file, most first.

    The rows run in path order, so a cap can cut them before the file that
    carries most of the count; this line, placed under the heading, names
    it anyway. Ties read in path order. At most `top` files, then
    "and N more files (M lines)". Unplaced lines count under their file.
    "" when the finding names fewer than two files: the heading's count is
    that one file's.
    """
    counts: dict[str, int] = {}
    for g in summary.gaps:
        counts[g.file] = counts.get(g.file, 0) + len(g.uncovered)
    for f, _ in summary.unplaced:
        counts[f] = counts.get(f, 0) + 1
    if len(counts) < 2:
        return ""
    ranked = sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))
    named = ", ".join(f"{f} {n}" for f, n in ranked[:top])
    rest = ranked[top:]
    more = ""
    if rest:
        lines = sum(n for _, n in rest)
        files = "file" if len(rest) == 1 else "files"
        more = f"; and {len(rest)} more {files} ({lines} line{'' if lines == 1 else 's'})"
    return f"  Lines per file, most first: {named}{more}"


def render_coverage(summary: CoverageSummary, *, text: bool = True, compact: bool = False) -> str:
    """The coverage row as lines of English, each backed by the finding.

    With `text` (the default) each function bullet is followed by the text
    of its uncovered lines, read from the same source the function was
    placed in; a file the tree did not hold is "not placed" and has none.
    `compact` is the recap's rendering (`render_compact`).
    """
    if compact:
        return render_compact(summary, text=text)
    s = summary
    if not s.detail_lines:
        return ""
    out = [_headline(s)]
    for g in s.gaps:
        out.append(f"  - {gap_sentence(g)}")
        if text:
            out.extend(line_text(g))
    out.extend(
        f"  - {f}:{n}: file not in the tree read; not placed [record: {f}:{n}]"
        for f, n in s.unplaced
    )
    return "\n".join(out) + "\n"
