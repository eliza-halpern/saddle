"""Suite hygiene from a project's ledgers, with no model call (#80, part b).

`saddle hygiene --journal PATH [--journal PATH ...] [--repo DIR] [--json]` lists three
kinds of test row. Each row cites the span ids in the ledgers that support it.

- **never-killed**: a test that ran against at least one scored mutant, and against
  no killed one, in every audit the ledgers recorded. The claim is one-way. mutmut
  and StrykerJS record which tests ran a mutated function (`mutant_detail`'s `tests`,
  #80 part a1), not which test failed. So a test that ran a killed mutant may still
  never have killed one, and is not listed; a listed test never killed one. A mutant
  row that names no tests (sealed before the field existed, or with no stats pass)
  supports no row.
- **duplicate**: tests whose covered lines are the same, by the fingerprints one
  impact map sealed (`audit:impact-map`'s `test_fingerprints`, #80 part a2).
  Fingerprints from different maps are never compared: they may cover different
  trees.
- **text-pin**: a test function every one of whose asserts compares a string
  literal against text read from a file (`.read_text()`, `open(...).read()`,
  `inspect.getsource`). A test with any other assert, or with `pytest.raises` or
  `pytest.warns`, is not one. It is read from the test's source in the repository
  and listed only for a test some sealed map recorded as run.
"""

from __future__ import annotations

import ast
import json
from collections import defaultdict
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Final, Literal

from saddle.journal import SpanRecord, attempt_sidecar_path, read_spans

MUTATION_SPAN: Final = "audit-tier2:mutation"
"""The auditor's tier-2 mutation finding, whose sidecar seals `mutant_detail`."""

MAP_SPAN: Final = "audit:impact-map"
"""The run-start impact map, whose sidecar seals `test_fingerprints`."""

KILLED: Final = frozenset({"killed", "timeout", "Killed", "Timeout"})
"""The statuses that count as killed: mutmut's (`evidence.mutation_sample`) and
StrykerJS's (`jsevidence._KILLED`)."""

ONE_WAY: Final = (
    "never-killed is one-way: mutation tools record which tests ran a mutated "
    "function, not which test failed. A listed test ran scored mutants and no killed "
    "one, so it never killed one; a test that ran a killed mutant is not listed, "
    "though it may never have killed one either."
)

Kind = Literal["never-killed", "duplicate", "text-pin"]


@dataclass(frozen=True)
class Row:
    """One finding of the report, and the spans it rests on."""

    kind: Kind
    tests: tuple[str, ...]
    records: tuple[str, ...]
    """Span ids, each present in a ledger read, that support the row."""
    note: str


@dataclass(frozen=True)
class Report:
    rows: tuple[Row, ...]
    ledgers: int
    mutation_audits: int
    maps: int


class HygieneError(RuntimeError):
    """A ledger that is missing, or does not verify."""


def _sidecars(journal: Path, name: str) -> Iterable[tuple[SpanRecord, Mapping[str, Any]]]:
    """Each span called `name` with a sealed sidecar, and the sidecar."""
    try:
        spans = read_spans(journal)
    except ValueError as exc:
        msg = f"{journal} does not verify: {exc}"
        raise HygieneError(msg) from None
    for span in spans:
        if span.name != name or not span.attempt_hash:
            continue
        # `read_spans` verified it against the hash its span sealed: what saddle wrote.
        yield span, json.loads(attempt_sidecar_path(journal, span.span_id).read_text())


def never_killed(journals: Sequence[Path]) -> tuple[list[Row], int]:
    """The never-killed rows, and how many mutation audits were read."""
    ran: dict[str, set[str]] = defaultdict(set)
    mutants: dict[str, int] = defaultdict(int)
    killed: set[str] = set()
    audits = 0
    for journal in journals:
        for span, sealed in _sidecars(journal, MUTATION_SPAN):
            audits += 1
            for row in sealed.get("mutant_detail") or ():
                tests = row.get("tests") if isinstance(row, dict) else None
                for test in tests or ():
                    ran[str(test)].add(span.span_id)
                    mutants[str(test)] += 1
                    if row.get("status") in KILLED:
                        killed.add(str(test))
    rows = [
        Row(
            "never-killed",
            (test,),
            tuple(sorted(ran[test])),
            f"ran {mutants[test]} scored mutant(s), none killed",
        )
        for test in sorted(set(ran) - killed)
    ]
    return rows, audits


def duplicates(journals: Sequence[Path]) -> tuple[list[Row], dict[str, set[str]], int]:
    """The duplicate rows; every test a map recorded, with the map spans; the maps."""
    groups: dict[tuple[str, ...], set[str]] = defaultdict(set)
    shared: dict[tuple[str, ...], str] = {}
    recorded: dict[str, set[str]] = defaultdict(set)
    maps = 0
    for journal in journals:
        for span, sealed in _sidecars(journal, MAP_SPAN):
            prints = sealed.get("test_fingerprints")
            if not isinstance(prints, dict):
                continue
            maps += 1
            by_print: dict[str, list[str]] = defaultdict(list)
            for test, digest in prints.items():
                recorded[str(test)].add(span.span_id)
                by_print[str(digest)].append(str(test))
            for digest, tests in by_print.items():
                if len(tests) >= 2:
                    key = tuple(sorted(tests))
                    groups[key].add(span.span_id)
                    shared[key] = digest
    rows = [
        Row(
            "duplicate",
            tests,
            tuple(sorted(spans)),
            f"the same covered lines (fingerprint {shared[tests][:12]})",
        )
        for tests, spans in sorted(groups.items())
    ]
    return rows, recorded, maps


def text_pins(repo: Path, recorded: Mapping[str, set[str]]) -> list[Row]:
    """The text-pin rows: test functions some map recorded whose asserts all compare
    a literal with text read from a file."""
    spans_by_function: dict[tuple[str, str], set[str]] = defaultdict(set)
    for node_id, spans in recorded.items():
        path, sep, rest = node_id.partition("::")
        if not sep or not path.endswith(".py"):
            continue
        spans_by_function[(path, rest.partition("[")[0])] |= spans
    rows: list[Row] = []
    trees: dict[str, ast.Module | None] = {}
    for (path, qualified), spans in sorted(spans_by_function.items()):
        if path not in trees:
            try:
                trees[path] = ast.parse((repo / path).read_text(encoding="utf-8"))
            except (OSError, SyntaxError, ValueError):
                trees[path] = None
        tree = trees[path]
        function = _function(tree, qualified) if tree is not None else None
        if function is not None and is_text_pin(function):
            rows.append(
                Row(
                    "text-pin",
                    (f"{path}::{qualified}",),
                    tuple(sorted(spans)),
                    f"every assert compares a literal with text read from a file "
                    f"({path}:{function.lineno})",
                )
            )
    return rows


def _function(tree: ast.Module, qualified: str) -> ast.FunctionDef | None:
    """The function `Class::name` or `name` names in `tree`."""
    scope: Sequence[ast.stmt] = tree.body
    *classes, name = qualified.split("::")
    for cls in classes:
        found = next((n for n in scope if isinstance(n, ast.ClassDef) and n.name == cls), None)
        if found is None:
            return None
        scope = found.body
    return next((n for n in scope if isinstance(n, ast.FunctionDef) and n.name == name), None)


def _walk(function: ast.FunctionDef) -> list[ast.AST]:
    """Every node of `function`'s body, nested functions' included: a helper the test
    defines and calls checks behaviour for it, so its asserts count."""
    return [node for statement in function.body for node in ast.walk(statement)]


def _is_open(node: ast.AST) -> bool:
    return isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == "open"


def _reads_text(node: ast.AST, names: set[str], handles: set[str]) -> bool:
    """Whether `node` is text read from a file, directly or through a name."""
    if isinstance(node, ast.Name):
        return node.id in names
    if not isinstance(node, ast.Call):
        return False
    func = node.func
    if isinstance(func, ast.Name):
        return func.id == "getsource"
    if not isinstance(func, ast.Attribute):
        return False
    if func.attr in ("read_text", "getsource"):
        return True
    if func.attr == "read":
        target = func.value
        return _is_open(target) or (isinstance(target, ast.Name) and target.id in handles)
    return False


def _literal(node: ast.AST) -> bool:
    if isinstance(node, ast.Constant):
        return isinstance(node.value, str)
    if isinstance(node, ast.Tuple):
        return bool(node.elts) and all(_literal(e) for e in node.elts)
    return False


_COMPARES: Final = (ast.In, ast.NotIn, ast.Eq, ast.NotEq)
_REGEX: Final = frozenset({"search", "match", "fullmatch"})


def _compares_text(test: ast.AST, names: set[str], handles: set[str]) -> bool:
    """Whether an assert's condition only compares literals with file text."""
    if isinstance(test, ast.UnaryOp) and isinstance(test.op, ast.Not):
        return _compares_text(test.operand, names, handles)
    if isinstance(test, ast.BoolOp):
        return all(_compares_text(v, names, handles) for v in test.values)
    if isinstance(test, ast.Compare):
        if len(test.ops) != 1 or not isinstance(test.ops[0], _COMPARES):
            return False
        left, right = test.left, test.comparators[0]
        return (_literal(left) and _reads_text(right, names, handles)) or (
            _literal(right) and _reads_text(left, names, handles)
        )
    if isinstance(test, ast.Call) and isinstance(test.func, ast.Attribute):
        func, args = test.func, test.args
        if func.attr in ("startswith", "endswith"):
            return len(args) == 1 and _literal(args[0]) and _reads_text(func.value, names, handles)
        if func.attr in _REGEX and isinstance(func.value, ast.Name) and func.value.id == "re":
            return len(args) >= 2 and _literal(args[0]) and _reads_text(args[1], names, handles)
    return False


def is_text_pin(function: ast.FunctionDef) -> bool:
    """Whether every assert of `function` compares a literal with file text, and
    there is at least one, and no `pytest.raises` or `pytest.warns` checks behaviour."""
    names: set[str] = set()
    handles: set[str] = set()
    nodes = list(_walk(function))
    for node in nodes:
        if isinstance(node, ast.withitem) and _is_open(node.context_expr):
            if isinstance(node.optional_vars, ast.Name):
                handles.add(node.optional_vars.id)
    for _ in range(2):  # a name read from a name read from a file
        for node in nodes:
            if isinstance(node, ast.Assign) and len(node.targets) == 1:
                target = node.targets[0]
                if isinstance(target, ast.Name) and _reads_text(node.value, names, handles):
                    names.add(target.id)
    asserts = [node for node in nodes if isinstance(node, ast.Assert)]
    for node in nodes:
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr in ("raises", "warns")
            and isinstance(node.func.value, ast.Name)
            and node.func.value.id == "pytest"
        ):
            return False
    return bool(asserts) and all(_compares_text(a.test, names, handles) for a in asserts)


def report(journals: Sequence[Path], repo: Path) -> Report:
    """Every row the ledgers support, never-killed first."""
    for journal in journals:
        if not journal.is_file():
            msg = f"no journal at {journal}"
            raise HygieneError(msg)
    killed_rows, audits = never_killed(journals)
    duplicate_rows, recorded, maps = duplicates(journals)
    pins = text_pins(repo, recorded)
    return Report(tuple(killed_rows + duplicate_rows + pins), len(journals), audits, maps)


def render(found: Report) -> str:
    """The report as text."""
    lines = [
        f"saddle hygiene: {found.ledgers} ledger(s), {found.mutation_audits} mutation "
        f"audit(s) with scored mutants, {found.maps} impact map(s)",
        ONE_WAY,
    ]
    if not found.rows:
        lines.append("no rows: nothing in these ledgers supports one")
    for row in found.rows:
        lines.append(
            f"{row.kind:13s} {', '.join(row.tests)} -- {row.note} "
            f"[records: {', '.join(row.records)}]"
        )
    return "\n".join(lines) + "\n"


def as_record(found: Report) -> dict[str, object]:
    """The report as JSON-ready data."""
    return {
        "ledgers": found.ledgers,
        "mutation_audits": found.mutation_audits,
        "maps": found.maps,
        "one_way": ONE_WAY,
        "rows": [
            {"kind": r.kind, "tests": list(r.tests), "records": list(r.records), "note": r.note}
            for r in found.rows
        ],
    }
