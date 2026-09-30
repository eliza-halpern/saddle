"""Test impact: the test files a change can reach, read off one full run of the suite.

`saddle auto` audits one worktree many times: a checkpoint after each burst
of edits, every `check` the model asks for, and the finish. Each ran the
project's whole suite, and on a large project that was most of every
audit's wall. Now the first audit of a run still runs the whole suite, with
per-test contexts (`--cov-context=test`), and `build` records which test
files ran the body of each function of each file. Every later audit of the
run runs `select`'s test files only:

- every test file that ran a changed function, a function being compared by
  its text between the baseline and the tree, so a deleted line counts;
- for what the map cannot answer by itself -- a changed statement outside
  every function (an import, a constant, a class attribute) or a function no
  test ran -- the test files that ran the functions naming what it binds,
  followed through imports, re-exports and module-level assignments
  (`_Index`): a constant read in another module runs none of its own
  module's lines. A statement that binds nothing selects every test file
  that ran its file, as does a changed method the map does not know (it may
  override one that unchanged code calls);
- every new or changed test file, and every test file that names a changed
  file that is not Python (a doc, a data file), and the test files of every
  source file whose strings name it.

The whole suite runs instead when a change can reach tests the map does not
record: a conftest.py, a config or lock file (`WHOLE_SUITE`), a deleted or
unparseable Python file, a data file nothing names, a changed function that
also ran outside every test (at import, in a pytest hook, in a subprocess),
a tree with a session- or package-scoped fixture (its setup is charged to
one test), or an empty selection.

What it cannot see, stated plainly: code a test reaches only by a name built
at run time (getattr, entry points) or only in a subprocess that records no
coverage. A full run -- the first audit of every run, `saddle audit`, CI --
is the net for those.
"""

from __future__ import annotations

import ast
import json
import os
import subprocess
from collections import Counter
from collections.abc import Iterator
from dataclasses import dataclass
from fnmatch import fnmatch
from pathlib import Path, PurePosixPath
from typing import Final

import coverage

from saddle.evidence import _tree_files, git_changed_files

WHOLE_SUITE: Final = (
    "conftest.py",
    "setup.py",
    "noxfile.py",
    "*.toml",
    "*.cfg",
    "*.ini",
    "*.lock",
    "requirements*.txt",
    "constraints*.txt",
    ".coveragerc",
    "Pipfile",
    ".python-version",
)
"""Basenames whose change can reach every test: pytest's own configuration,
the environment the suite runs in, or a conftest."""

MAP_VERSION: Final = 1
"""Part of a cached map's key: bumped whenever `build` changes what a map
holds, so a map drawn by other code is never read back."""

TEST_FILES: Final = ("test_*.py", "*_test.py")
"""pytest's default test modules, the files the gate's suite run can skip."""

NARROW_SCOPES: Final = ("function", "class", "module")
"""Fixture scopes whose setup runs inside the file that uses it."""

type Where = tuple[str, ...]
"""Where a name is used: ("function", qualname), ("class", its outermost
class) or ("module", *the names the enclosing statement binds)."""


@dataclass(frozen=True)
class FileImpact:
    """What one file's lines tell about the tests that ran them."""

    tests: frozenset[str]
    """Every test file that ran any line of the file inside a test."""
    functions: dict[str, frozenset[str]]
    """By qualname: the test files that ran the function's body."""
    outside: frozenset[str]
    """Qualnames whose body also ran outside every test."""


type ImpactMap = dict[str, FileImpact]


@dataclass
class ImpactMemo:
    """The map one run's audits share (`AuditorConfig.impact`): None until a
    whole-suite run recorded with per-test contexts has been read."""

    tests: ImpactMap | None = None
    cache: Path | None = None
    """Where drawn maps are kept (`Auditor.draw_map`), one file per tree,
    test command, worker count and environment; None keeps none."""


def dumps(known: ImpactMap) -> str:
    """`known` as JSON, sorted, for the cache."""
    return json.dumps(
        {
            rel: {
                "tests": sorted(impact.tests),
                "functions": {name: sorted(files) for name, files in impact.functions.items()},
                "outside": sorted(impact.outside),
            }
            for rel, impact in known.items()
        },
        sort_keys=True,
    )


def loads(text: str) -> ImpactMap | None:
    """A map `dumps` wrote; None for anything else, never a partial map."""
    try:
        raw = json.loads(text)
        return {
            rel: FileImpact(
                frozenset(entry["tests"]),
                {name: frozenset(files) for name, files in entry["functions"].items()},
                frozenset(entry["outside"]),
            )
            for rel, entry in raw.items()
        }
    except (ValueError, KeyError, TypeError, AttributeError):
        return None


def is_test_file(rel: str) -> bool:
    name = PurePosixPath(rel).name
    return any(fnmatch(name, pattern) for pattern in TEST_FILES)


type Function = ast.FunctionDef | ast.AsyncFunctionDef


def _functions(tree: ast.AST, prefix: str = "") -> Iterator[tuple[str, Function]]:
    """Every function and method with its dotted qualname, outermost first."""
    for child in ast.iter_child_nodes(tree):
        if isinstance(child, ast.FunctionDef | ast.AsyncFunctionDef):
            name = f"{prefix}{child.name}"
            yield name, child
            yield from _functions(child, f"{name}.")
        elif isinstance(child, ast.ClassDef):
            yield from _functions(child, f"{prefix}{child.name}.")
        else:
            yield from _functions(child, prefix)


def _owners(source: str) -> dict[int, str]:
    """Line -> the innermost function whose body holds it. A `def` line runs
    where the function is defined, so it belongs to the enclosing scope."""
    spans = sorted(
        (fn.body[0].lineno, fn.end_lineno or fn.body[0].lineno, name)
        for name, fn in _functions(ast.parse(source))
    )
    owner: dict[int, str] = {}
    for start, end, name in spans:
        for line in range(start, end + 1):
            owner[line] = name
    return owner


def build(data_file: str, tree: Path) -> ImpactMap | None:
    """The map from a whole-suite run recorded with per-test contexts, keyed
    by `tree`-relative path; None when the data holds no test context."""
    cov = coverage.Coverage(data_file=data_file, config_file=False)
    try:
        cov.load()
    except coverage.CoverageException:
        return None
    data = cov.get_data()
    root = os.path.realpath(tree)
    found: ImpactMap = {}
    seen_test = False
    for measured in data.measured_files():
        real = os.path.realpath(measured)
        if not real.startswith(f"{root}{os.sep}"):
            continue
        try:
            owner = _owners(Path(real).read_text())
        except (OSError, SyntaxError, ValueError):
            owner = {}
        tests: set[str] = set()
        functions: dict[str, set[str]] = {}
        outside: set[str] = set()
        for line, contexts in data.contexts_by_lineno(measured).items():
            name = owner.get(line)
            ran = {context.split("::", 1)[0] for context in contexts if "::" in context}
            tests |= ran
            if name is not None:
                functions.setdefault(name, set()).update(ran)
                if any("::" not in context for context in contexts):
                    outside.add(name)
        seen_test = seen_test or bool(tests)
        found[Path(os.path.relpath(real, root)).as_posix()] = FileImpact(
            frozenset(tests),
            {name: frozenset(files) for name, files in functions.items() if files},
            frozenset(outside),
        )
    return found if seen_test else None


def _module(rel: str) -> tuple[str, str]:
    """(dotted module, its package) for a tree-relative .py path; src/ layout aware."""
    parts = list(PurePosixPath(rel).with_suffix("").parts)
    if parts[:1] == ["src"]:
        parts = parts[1:]
    if parts[-1:] == ["__init__"]:
        parts = parts[:-1]
        return ".".join(parts), ".".join(parts)
    return ".".join(parts), ".".join(parts[:-1])


def _bound(stmt: ast.stmt) -> set[str]:
    """The names a module-level statement binds."""
    names: set[str] = set()
    for node in ast.walk(stmt):
        if isinstance(node, ast.Name) and isinstance(node.ctx, ast.Store):
            names.add(node.id)
        elif isinstance(node, ast.alias):
            names.add((node.asname or node.name).split(".")[0])
        elif isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef):
            names.add(node.name)
    return names


def _dotted(node: ast.expr) -> str | None:
    """`a.b.c` for an attribute chain rooted at a name, else None."""
    parts: list[str] = []
    while isinstance(node, ast.Attribute):
        parts.append(node.attr)
        node = node.value
    if not isinstance(node, ast.Name):
        return None
    return ".".join([node.id, *reversed(parts)])


def _docstring(body: list[ast.stmt]) -> bool:
    first = body[0] if body else None
    return (
        isinstance(first, ast.Expr)
        and isinstance(first.value, ast.Constant)
        and isinstance(first.value.value, str)
    )


class _Index(ast.NodeVisitor):
    """One file's references: every name and attribute chain it uses, where,
    the imports that say what those names stand for, and its string constants."""

    def __init__(self, rel: str) -> None:
        self.module, self.package = _module(rel)
        self.aliases: dict[str, str] = {}
        self.stars: list[str] = []
        self.uses: list[tuple[str, Where]] = []
        self.imports: list[tuple[str, Where]] = []
        self.strings: list[str] = []
        self.broad_fixture = False
        self._where: Where = ("module",)
        self._prefix = ""
        self._in_function = False

    def index(self, tree: ast.Module) -> None:
        for stmt in tree.body[1:] if _docstring(tree.body) else tree.body:
            if isinstance(stmt, ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef):
                self.visit(stmt)
                continue
            self._where = ("module", *sorted(_bound(stmt)))
            self.visit(stmt)
            self._where = ("module",)

    def _function(self, node: Function) -> None:
        # Decorators and defaults are part of what the function does.
        name = f"{self._prefix}{node.name}"
        saved = (self._where, self._prefix, self._in_function)
        self._where, self._prefix, self._in_function = ("function", name), f"{name}.", True
        for decorator in node.decorator_list:
            self.visit(decorator)
        self.visit(node.args)
        if node.returns is not None:
            self.visit(node.returns)
        for stmt in node.body[1:] if _docstring(node.body) else node.body:
            self.visit(stmt)
        self._where, self._prefix, self._in_function = saved

    def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
        self._function(node)

    def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> None:
        self._function(node)

    def visit_ClassDef(self, node: ast.ClassDef) -> None:
        saved = (self._where, self._prefix)
        if not self._in_function:
            top = self._where[1] if self._where[0] == "class" else node.name
            self._where = ("class", top)
        self._prefix = f"{self._prefix}{node.name}."
        for child in (*node.decorator_list, *node.bases, *node.keywords):
            self.visit(child)
        for stmt in node.body[1:] if _docstring(node.body) else node.body:
            self.visit(stmt)
        self._where, self._prefix = saved

    def visit_Name(self, node: ast.Name) -> None:
        if not isinstance(node.ctx, ast.Store):
            self.uses.append((node.id, self._where))

    def visit_Attribute(self, node: ast.Attribute) -> None:
        chain = _dotted(node)
        if chain is None:
            self.generic_visit(node)
        else:
            self.uses.append((chain, self._where))

    def visit_Constant(self, node: ast.Constant) -> None:
        if isinstance(node.value, str):
            self.strings.append(node.value)

    def visit_Import(self, node: ast.Import) -> None:
        for alias in node.names:
            if alias.asname:
                self.aliases[alias.asname] = alias.name
            else:
                root = alias.name.split(".")[0]
                self.aliases[root] = root

    def visit_ImportFrom(self, node: ast.ImportFrom) -> None:
        base = self._resolve(node.module, node.level)
        if base is None:
            return
        for alias in node.names:
            if alias.name == "*":
                self.stars.append(base)
                continue
            self.aliases[alias.asname or alias.name] = f"{base}.{alias.name}"
            self.imports.append((f"{base}.{alias.name}", self._where))

    def visit_Call(self, node: ast.Call) -> None:
        func = node.func
        called = (
            func.attr
            if isinstance(func, ast.Attribute)
            else func.id
            if isinstance(func, ast.Name)
            else ""
        )
        if called == "fixture":
            for keyword in node.keywords:
                value = keyword.value
                narrow = isinstance(value, ast.Constant) and value.value in NARROW_SCOPES
                if keyword.arg == "scope" and not narrow:
                    self.broad_fixture = True
        self.generic_visit(node)

    def _resolve(self, module: str | None, level: int) -> str | None:
        if level == 0:
            return module
        parts = self.package.split(".") if self.package else []
        if level - 1 > len(parts):
            return None
        base = parts[: len(parts) - (level - 1)]
        return ".".join([*base, *([module] if module else [])]) or None

    def full(self, text: str) -> list[str]:
        """What `text` (a name or chain used here) can stand for, as dotted names."""
        root, _, rest = text.partition(".")
        tail = f".{rest}" if rest else ""
        if root in self.aliases:
            return [f"{self.aliases[root]}{tail}"]
        return [f"{self.module}.{text}", *(f"{star}.{text}" for star in self.stars)]


def _prefixes(name: str) -> Iterator[str]:
    parts = name.split(".")
    for end in range(1, len(parts) + 1):
        yield ".".join(parts[:end])


def _outside_functions(tree: ast.Module) -> Counter[tuple[str, Where]]:
    """The statements outside every function, each with what a change to it reaches."""
    found: Counter[tuple[str, Where]] = Counter()

    def walk(body: list[ast.stmt], top: str | None) -> None:
        for stmt in body:
            if isinstance(stmt, ast.FunctionDef | ast.AsyncFunctionDef):
                continue
            if isinstance(stmt, ast.ClassDef):
                owner = top or stmt.name
                head = [*stmt.decorator_list, *stmt.bases, *stmt.keywords]
                found[
                    (f"class {stmt.name} {[ast.unparse(h) for h in head]}", ("class", owner))
                ] += 1
                walk(stmt.body, owner)
                continue
            where: Where = ("class", top) if top else ("module", *sorted(_bound(stmt)))
            found[(ast.unparse(stmt), where)] += 1

    walk(tree.body, None)
    return found


def _function_texts(tree: ast.Module) -> dict[str, str]:
    texts: dict[str, str] = {}
    for name, fn in _functions(tree):
        texts[name] = texts.get(name, "") + ast.unparse(fn)
    return texts


def _at(tree: Path, rev: str, rel: str) -> str | None:
    shown = subprocess.run(
        ["git", "-C", str(tree), "show", f"{rev}:{rel}"],
        capture_output=True,
        text=True,
        check=False,
    )
    return shown.stdout if shown.returncode == 0 else None


def select(tree: Path, baseline: str, known: ImpactMap) -> tuple[str, ...] | None:
    """The test files of `tree` that `tree`'s change against `baseline` can
    reach, by `known`; None when only the whole suite can say (module doc)."""
    files = _tree_files(tree)
    texts: dict[str, str] = {}
    indexes: dict[str, _Index] = {}
    for rel in files:
        if not rel.endswith(".py"):
            continue
        try:
            texts[rel] = (tree / rel).read_text()
            parsed = ast.parse(texts[rel])
        except (OSError, SyntaxError, ValueError, UnicodeDecodeError):
            return None
        indexes[rel] = _Index(rel)
        indexes[rel].index(parsed)
        if indexes[rel].broad_fixture:
            return None
    refs: dict[str, set[tuple[str, Where]]] = {}
    for rel, index in indexes.items():
        for text, where in index.uses:
            for name in index.full(text):
                for prefix in _prefixes(name):
                    refs.setdefault(prefix, set()).add((rel, where))
        for name, where in index.imports:
            for prefix in _prefixes(name):
                refs.setdefault(prefix, set()).add((rel, where))
    tests = [rel for rel in files if is_test_file(rel)]
    chosen: set[str] = set()
    functions: set[tuple[str, str]] = set()
    whole_files: set[str] = set()
    targets: list[str] = []

    for rel in git_changed_files(tree, baseline):
        name = PurePosixPath(rel).name
        if any(fnmatch(name, pattern) for pattern in WHOLE_SUITE):
            return None
        if is_test_file(rel):
            chosen.add(rel)
            continue
        if not rel.endswith(".py"):
            chosen.update(t for t in tests if name in texts.get(t, ""))
            naming = [
                src
                for src, index in indexes.items()
                if not is_test_file(src) and any(name in s for s in index.strings)
            ]
            whole_files.update(naming)
            if not naming and not any(name in texts.get(t, "") for t in tests):
                if PurePosixPath(rel).suffix not in (".md", ".rst", ".txt", ".adoc"):
                    return None
            continue
        if rel not in texts:
            return None  # deleted: its importers fail to import
        try:
            old = ast.parse(_at(tree, baseline, rel) or "")
        except (SyntaxError, ValueError):
            return None
        new = ast.parse(texts[rel])
        module = indexes[rel].module
        impact = known.get(rel)
        old_texts, new_texts = _function_texts(old), _function_texts(new)
        edited = [
            q
            for q in sorted(old_texts.keys() | new_texts.keys())
            if old_texts.get(q) != new_texts.get(q)
        ]
        for qualname in edited:
            if impact is not None and qualname in impact.outside:
                return None
            if impact is not None and qualname in impact.functions:
                functions.add((rel, qualname))
                continue
            # Not in the map: new, or no test ran it. Whatever calls it by
            # name is followed; a method may override one unchanged code calls.
            targets.append(f"{module}.{qualname}")
            if "." in qualname:
                whole_files.add(rel)
        old_rest, new_rest = _outside_functions(old), _outside_functions(new)
        moved = (old_rest - new_rest) + (new_rest - old_rest)
        for _text, where in moved:
            _reach(rel, module, where, known, targets, functions, whole_files)
        if not edited and not moved:
            whole_files.add(rel)  # comments or layout only: its tests still run

    seen: set[str] = set()
    while targets:
        target = targets.pop()
        if target in seen:
            continue
        seen.add(target)
        for rel, where in refs.get(target, ()):
            if is_test_file(rel):
                chosen.add(rel)
                continue
            if PurePosixPath(rel).name == "conftest.py":
                return None
            _reach(rel, indexes[rel].module, where, known, targets, functions, whole_files)

    for rel, qualname in functions:
        impact = known.get(rel)
        if impact is not None:
            chosen.update(impact.functions.get(qualname, frozenset()))
    for rel in whole_files:
        impact = known.get(rel)
        if impact is not None:
            chosen.update(impact.tests)
    selected = tuple(sorted(chosen & set(tests)))
    return selected or None


def _reach(
    rel: str,
    module: str,
    where: Where,
    known: ImpactMap,
    targets: list[str],
    functions: set[tuple[str, str]],
    whole_files: set[str],
) -> None:
    """Follow one changed (or reached) spot of `rel`: a function reaches its
    tests; a class, whatever names it and its methods' tests; a statement,
    whatever names what it binds, or its whole file when it binds nothing."""
    kind, *rest = where
    if kind == "function":
        functions.add((rel, rest[0]))
    elif kind == "class":
        targets.append(f"{module}.{rest[0]}")
        impact = known.get(rel)
        if impact is not None:
            functions.update((rel, q) for q in impact.functions if q.startswith(f"{rest[0]}."))
    elif rest:
        targets.extend(f"{module}.{name}" for name in rest)
    else:
        whole_files.add(rel)
