"""Pre-existing tests that changed, and the `flip:` line that owns up to it.

A run rewrote a pre-existing JavaScript test to match its new code, and the
audit said "2893 pre-existing test(s) keep their assertions": the only
comparison it made read Python test modules. This module is the comparison
for every language, and the rule that follows from it (CONTRIBUTING.md, "A
test that flips from red to green is a loosening"): a pre-existing test whose
expectations changed, vanished or were renamed is allowed only when the
finish summary carries a line

    flip: <exact test name> -- <evidence>

and the evidence is not the one shape the rule bans.

Pure: texts in, `ChangedTest`s and a verdict out. `Auditor.flips` reads the
files and builds the finding; nothing here touches git or the disk.

Detection errs toward "changed": it only costs a label. What it must never
do is read as "nothing changed" when it could not look: a file the scan
cannot read is a `ChangedTest` of its own (`UNREADABLE`), named by its path.
Python is read with `ast`. There is no JavaScript parser in Python, so the
scan is textual but sound about what it needs: it masks comments, strings,
template literals and regular-expression literals, balances every bracket,
and refuses (`UNREADABLE`) any file where the brackets do not balance.
"""

from __future__ import annotations

import ast
import copy
import difflib
import re
from collections.abc import Collection, Mapping
from dataclasses import dataclass
from fnmatch import fnmatch
from pathlib import PurePosixPath
from typing import Final, Literal

ASSERTION_CHANGED: Final = "assertion-changed"
RAISES_REMOVED: Final = "expectation-removed"
MARKED: Final = "skipped-or-xfailed"
BODY_CHANGED: Final = "body-changed"
DELETED: Final = "deleted"
RENAMED: Final = "renamed"
FILE_CHANGED: Final = "file-changed"
UNREADABLE: Final = "unreadable"

FLIP_RULE: Final = (
    "For each, put one line in your finish summary: `flip: <exact test name> -- <evidence>`. "
    "Evidence is a known-good instance the old assertion rejected, or a real run in which it "
    'let something wrong through. "The new code fails the old test" is not evidence: that is '
    "what a flip is. If you can show neither, restore the test and change the code instead."
)
"""What the model is told, whole, whenever a changed test lacks its label."""

CIRCULAR_SAID: Final = (
    "its evidence only says the old test fails against the new code, which is what a flip is, "
    "not a reason for it"
)
NO_EVIDENCE_SAID: Final = "it carries no evidence"
"""Why a `flip:` line was refused (`judge`)."""

_JS_SUFFIXES: Final = frozenset({".js", ".mjs", ".cjs", ".jsx", ".ts", ".mts", ".cts", ".tsx"})
_OTHER_SUFFIXES: Final = frozenset(
    {".go", ".rs", ".rb", ".java", ".kt", ".cs", ".php", ".swift", ".c", ".cc", ".cpp", ".sh"}
    | {".bats", ".lua", ".pl", ".r"}
)
_TEST_DIRS: Final = frozenset({"tests", "test", "__tests__"})

Language = Literal["python", "js", "other"]


@dataclass(frozen=True)
class ChangedTest:
    """One pre-existing test that is not, in the tree, what it was at the baseline."""

    path: str
    name: str
    """The exact name a `flip:` line must carry: a Python function name, a JavaScript test
    or group title, or the file's path when the file could not be read test by test."""
    kind: str
    detail: str


def language(path: str) -> Language | None:
    """The language a file's tests are read in, or None when it is not test code.

    Python by pytest's collection rule (`test_*.py`, `*_test.py`); JavaScript and
    TypeScript by name (`*.test.*`, `*.spec.*`) or by living under a `tests`, `test`
    or `__tests__` directory, which is where every test lives that the name rule
    misses; any other language only by a test-shaped file name, and then it is
    compared as a whole file (`FILE_CHANGED`).
    """
    pure = PurePosixPath(path)
    name = pure.name
    marked = ".test." in name or ".spec." in name
    if pure.suffix == ".py":
        return "python" if fnmatch(name, "test_*.py") or fnmatch(name, "*_test.py") else None
    if pure.suffix in _JS_SUFFIXES:
        return "js" if marked or _TEST_DIRS & set(pure.parts[:-1]) else None
    if pure.suffix in _OTHER_SUFFIXES and (
        marked or pure.stem.startswith("test_") or pure.stem.endswith("_test")
    ):
        return "other"
    return None


# -- Python ----------------------------------------------------------------------


_EXPECT_CALLS: Final = frozenset(
    {"raises", "warns", "deprecated_call", "assertRaises", "assertRaisesRegex"}
    | {"assertWarns", "assertWarnsRegex"}
)
_MARKS: Final = frozenset({"skip", "skipif", "xfail"})


@dataclass(frozen=True)
class _PyTest:
    asserts: Mapping[str, str]
    expects: Mapping[str, str]
    marks: Mapping[str, str]
    whole: str


def _last_name(node: ast.expr) -> str:
    node = node.func if isinstance(node, ast.Call) else node
    if isinstance(node, ast.Attribute):
        return node.attr
    return node.id if isinstance(node, ast.Name) else ""


def _is_mark(node: ast.expr) -> bool:
    """A skip or xfail: a `pytest.mark` decorator or call, or `pytest.skip()`/`pytest.xfail()`."""
    name = _last_name(node)
    inner = node.func if isinstance(node, ast.Call) else node
    chain = ast.unparse(inner)
    return name in _MARKS and (".mark." in chain or chain in ("pytest.skip", "pytest.xfail"))


def _py_test(
    node: ast.FunctionDef | ast.AsyncFunctionDef, inherited: Collection[ast.expr]
) -> _PyTest:
    asserts: dict[str, str] = {}
    expects: dict[str, str] = {}
    marks: dict[str, str] = {}
    for stmt in ast.walk(node):
        if isinstance(stmt, ast.Assert):
            asserts[ast.dump(stmt)] = ast.unparse(stmt)
        elif isinstance(stmt, ast.Call):
            name = _last_name(stmt)
            if name in _EXPECT_CALLS:
                expects[ast.dump(stmt)] = ast.unparse(stmt)
            elif name.startswith("assert"):
                asserts[ast.dump(stmt)] = ast.unparse(stmt)
            if _is_mark(stmt):
                marks[ast.dump(stmt)] = ast.unparse(stmt)
    for deco in [*node.decorator_list, *inherited]:
        if _is_mark(deco):
            marks[ast.dump(deco)] = ast.unparse(deco)
    blank = copy.copy(node)
    blank.name = "_"
    return _PyTest(asserts, expects, marks, ast.dump(blank))


def _py_tests(source: str) -> dict[str, _PyTest]:
    """Every `test*` function of a module by name; same-named ones are merged, as the
    assertion-preservation gate has always keyed them."""
    tree = ast.parse(source)
    module_marks: list[ast.expr] = []
    for stmt in tree.body:
        if isinstance(stmt, ast.Assign) and any(
            isinstance(t, ast.Name) and t.id == "pytestmark" for t in stmt.targets
        ):
            module_marks += [
                sub for sub in ast.walk(stmt.value) if isinstance(sub, ast.expr) and _is_mark(sub)
            ]
    found: dict[str, _PyTest] = {}

    def visit(node: ast.AST, inherited: list[ast.expr]) -> None:
        for child in ast.iter_child_nodes(node):
            if isinstance(child, ast.ClassDef):
                visit(child, [*inherited, *(d for d in child.decorator_list if _is_mark(d))])
            elif isinstance(child, ast.FunctionDef | ast.AsyncFunctionDef):
                if child.name.startswith("test"):
                    got = _py_test(child, inherited)
                    old = found.get(child.name)
                    found[child.name] = (
                        got
                        if old is None
                        else _PyTest(
                            {**old.asserts, **got.asserts},
                            {**old.expects, **got.expects},
                            {**old.marks, **got.marks},
                            old.whole + got.whole,
                        )
                    )
                visit(child, inherited)
            else:
                visit(child, inherited)

    visit(tree, module_marks)
    return found


def _lost(before: Mapping[str, str], after: Mapping[str, str]) -> list[str]:
    return [text for key, text in before.items() if key not in after]


def _short(text: str, limit: int = 100) -> str:
    one = " ".join(text.split())
    return one if len(one) <= limit else one[: limit - 3] + "..."


def _py_changes(
    path: str,
    before: Mapping[str, _PyTest],
    here: Mapping[str, _PyTest],
    elsewhere: Mapping[str, _PyTest],
    all_before: Collection[str],
) -> list[ChangedTest]:
    found: list[ChangedTest] = []
    moved: list[tuple[str, _PyTest]] = [
        (name, test) for name, test in {**elsewhere, **here}.items() if name not in all_before
    ]
    for name, old in before.items():
        new = here.get(name) or elsewhere.get(name)
        if new is None:
            found.append(_vanished(path, name, old.whole, {n: t.whole for n, t in moved}))
            continue
        for lost in _lost(old.asserts, new.asserts):
            found.append(ChangedTest(path, name, ASSERTION_CHANGED, f"no longer asserts: {lost}"))
        for lost in _lost(old.expects, new.expects):
            found.append(ChangedTest(path, name, RAISES_REMOVED, f"no longer expects: {lost}"))
        for added in _lost(new.marks, old.marks):
            found.append(ChangedTest(path, name, MARKED, f"now carries: {added}"))
    return found


def _vanished(path: str, name: str, whole: str, candidates: Mapping[str, str]) -> ChangedTest:
    """A test that is gone by that name: renamed when some new test is its body,
    or most of it, and deleted otherwise."""
    for other, text in candidates.items():
        if text == whole:
            return ChangedTest(path, name, RENAMED, f"now named {other!r}, body unchanged")
    best, share = "", 0.0
    if len(whole) <= _SIMILARITY_LIMIT:
        for other, text in candidates.items():
            if len(text) > _SIMILARITY_LIMIT:
                continue
            matcher = difflib.SequenceMatcher(None, whole, text, autojunk=False)
            kept = sum(block.size for block in matcher.get_matching_blocks()) / max(1, len(whole))
            if kept > share:
                best, share = other, kept
    if share >= _RENAME_SHARE:
        return ChangedTest(path, name, RENAMED, f"now named {best!r}, body also changed")
    return ChangedTest(path, name, DELETED, f"no test named {name!r} remains")


_SIMILARITY_LIMIT: Final = 20_000
_RENAME_SHARE: Final = 0.6


# -- JavaScript ------------------------------------------------------------------


class JsScanError(ValueError):
    """The text could not be read as JavaScript well enough to trust the scan."""


_KEYWORDS_BEFORE_REGEX: Final = frozenset(
    {"return", "typeof", "case", "in", "of", "do", "else", "delete", "void", "throw", "new"}
    | {"yield", "await"}
)
_BEFORE_REGEX: Final = frozenset("(,=:[!&|?{};+-*%<>~^")
_LITERAL: Final = "\0"
_OPENERS: Final = {"(": ")", "[": "]", "{": "}"}


def _end_string(src: str, start: int) -> int:
    quote = src[start]
    i = start + 1
    while i < len(src):
        char = src[i]
        if char == "\\":
            i += 2
        elif char == quote:
            return i
        elif char == "\n":
            break
        else:
            i += 1
    msg = "unterminated string"
    raise JsScanError(msg)


def _end_template(src: str, start: int) -> int:
    i = start + 1
    while i < len(src):
        char = src[i]
        if char == "\\":
            i += 2
        elif char == "`":
            return i
        elif src.startswith("${", i):
            i = _end_expression(src, i + 2) + 1
        else:
            i += 1
    msg = "unterminated template literal"
    raise JsScanError(msg)


def _end_expression(src: str, start: int) -> int:
    """The index of the `}` closing a `${` whose contents start at `start`."""
    depth = 0
    i = start
    while i < len(src):
        char = src[i]
        if char in "\"'":
            i = _end_string(src, i)
        elif char == "`":
            i = _end_template(src, i)
        elif char == "{":
            depth += 1
        elif char == "}":
            if depth == 0:
                return i
            depth -= 1
        i += 1
    msg = "unterminated template expression"
    raise JsScanError(msg)


def _end_regex(src: str, start: int) -> int:
    i = start + 1
    in_class = False
    while i < len(src):
        char = src[i]
        if char == "\\":
            i += 2
            continue
        if char == "[":
            in_class = True
        elif char == "]":
            in_class = False
        elif char == "/" and not in_class:
            return i
        elif char == "\n":
            break
        i += 1
    msg = "unterminated regular expression"
    raise JsScanError(msg)


def _mask(src: str) -> tuple[str, str]:
    """`(masked, plain)`, both as long as `src`.

    `plain` is `src` with comments blanked. `masked` is `plain` with the inside
    of every string, template literal and regular-expression literal replaced
    by `_LITERAL`, so a bracket or a keyword inside one is never read as code.
    """
    plain = list(src)
    masked = list(src)

    def blank(start: int, stop: int, *, comment: bool) -> None:
        for k in range(start, stop):
            if comment and src[k] != "\n":
                masked[k] = plain[k] = " "
            elif not comment:
                masked[k] = _LITERAL

    i = 0
    previous = ""
    word = ""
    while i < len(src):
        char = src[i]
        if src.startswith("//", i):
            stop = src.find("\n", i)
            stop = len(src) if stop < 0 else stop
            blank(i, stop, comment=True)
            i = stop
            continue
        if src.startswith("/*", i):
            stop = src.find("*/", i + 2)
            if stop < 0:
                msg = "unterminated comment"
                raise JsScanError(msg)
            blank(i, stop + 2, comment=True)
            i = stop + 2
            continue
        if char in "\"'`":
            stop = _end_string(src, i) if char != "`" else _end_template(src, i)
            blank(i + 1, stop, comment=False)
            i = stop + 1
            previous, word = char, ""
            continue
        if char == "/" and (
            not previous or previous in _BEFORE_REGEX or word in _KEYWORDS_BEFORE_REGEX
        ):
            stop = _end_regex(src, i)
            blank(i + 1, stop, comment=False)
            i = stop + 1
            previous, word = "/", ""
            continue
        if not char.isspace():
            previous = char
            word = word + char if char.isalnum() or char in "_$" else ""
        i += 1
    return "".join(masked), "".join(plain)


def _partners(masked: str) -> dict[int, int]:
    """Each opening bracket's closing partner; the file is unreadable when they do not nest."""
    stack: list[int] = []
    partner: dict[int, int] = {}
    for i, char in enumerate(masked):
        if char in _OPENERS:
            stack.append(i)
        elif char in ")]}":
            if not stack or _OPENERS[masked[stack[-1]]] != char:
                msg = f"unbalanced {char!r}"
                raise JsScanError(msg)
            partner[stack.pop()] = i
    if stack:
        msg = f"unclosed {masked[stack[-1]]!r}"
        raise JsScanError(msg)
    return partner


_CALL: Final = re.compile(
    r"(?<![\w$.])(?P<fn>test|it|describe|suite)(?P<mod>\.(?:skip|only|todo))?\s*\(\s*(?P<quote>[\"'`])?"
)


@dataclass(frozen=True)
class _Call:
    title: str
    group: bool
    start: int
    stop: int
    named: int = 0
    own: str = ""


_COMPUTED: Final = "<computed title>"


def _collapse(plain: str, masked: str, start: int, stop: int, holes: list[tuple[int, int]]) -> str:
    """`plain[start:stop]` with whitespace in code collapsed to one space, and dropped next
    to brackets, commas and semicolons so a reformat is not a change (inside a literal it is
    kept, so a changed string is never normalised away), and each span in `holes`
    left out (a group's tests are judged one by one, so adding or
    removing one is not a change of the group)."""
    out: list[str] = []
    gap = False  # code whitespace seen since the last thing written
    i = start
    spans = iter(sorted(holes))
    hole = next(spans, None)
    while i < stop:
        if hole is not None and i == hole[0]:
            i = hole[1]
            hole = next(spans, None)
            gap = True
            continue
        char = plain[i]
        if masked[i] != _LITERAL and char.isspace():
            gap = True
        else:
            if gap and out and out[-1] not in "([{" and char not in ")]},;":
                out.append(" ")
            gap = False
            out.append(char)
        i += 1
    return "".join(out).strip()


def _js_calls(src: str) -> list[_Call]:
    masked, plain = _mask(src)
    partner = _partners(masked)
    found: list[_Call] = []
    bodies: dict[int, int] = {}
    for match in _CALL.finditer(masked):
        if masked[: match.start("fn")].rstrip().endswith("function"):
            continue  # a declaration named `test`, not a call
        opening = masked.index("(", match.start("fn"))
        stop = partner[opening]
        if match.group("quote"):
            end = masked.index(match.group("quote"), match.end())
            title, bodies[opening] = src[match.end() : end], end + 1
        else:
            title, bodies[opening] = _COMPUTED, opening + 1
        found.append(
            _Call(
                title, match.group("fn") in ("describe", "suite"), opening, stop, match.start("fn")
            )
        )
    # A call's own text is everything after its title but the calls nested in
    # it, so a group is judged on its options and hooks, and each test on its
    # body alone, and a renamed test with the same body reads as the same body.
    # The modifier (`.skip`, `.only`) is part of what the call says.
    owned: list[_Call] = []
    for call in found:
        inside = [c for c in found if call.start < c.start and c.stop < call.stop]
        direct = [
            (c.named, _after_statement(masked, c.stop))
            for c in inside
            if not any(o.start < c.start and c.stop < o.stop for o in inside)
        ]
        modifier = _modifier(masked, call.start)
        own = modifier + _collapse(plain, masked, bodies[call.start], call.stop + 1, direct)
        owned.append(_Call(call.title, call.group, call.start, call.stop, call.named, own))
    return owned


def _after_statement(masked: str, close: int) -> int:
    """The index just past a call closed at `close` and the `;` that ends its statement."""
    rest = masked[close + 1 :]
    gap = len(rest) - len(rest.lstrip())
    return close + 1 + (gap + 1 if rest.lstrip().startswith(";") else 0)


def _modifier(masked: str, opening: int) -> str:
    """`.skip` / `.only` / `.todo` written between the call's name and its `(`."""
    before = masked[:opening].rstrip()
    for mod in (".skip", ".only", ".todo"):
        if before.endswith(mod):
            return mod
    return ""


def _group_by_title(calls: list[_Call]) -> dict[str, list[str]]:
    grouped: dict[str, list[str]] = {}
    for call in calls:
        grouped.setdefault(call.title, []).append(call.own)
    return grouped


def _first_difference(old: str, new: str) -> str:
    matcher = difflib.SequenceMatcher(None, old, new, autojunk=False)
    for tag, a0, a1, b0, b1 in matcher.get_opcodes():
        if tag != "equal":
            was = _short(old[a0:a1] or "(nothing)", 80)
            now = _short(new[b0:b1] or "(nothing)", 80)
            return f"was `{was}`, now `{now}`"
    return "text differs"  # pragma: no cover  (equal texts are never compared)


def _js_changes(
    path: str,
    before: Mapping[str, list[str]],
    here: Mapping[str, list[str]],
    elsewhere: Mapping[str, list[str]],
    all_before: Collection[str],
) -> list[ChangedTest]:
    found: list[ChangedTest] = []
    new_titles = {**elsewhere, **here}
    moved = {t: " ".join(owns) for t, owns in new_titles.items() if t not in all_before}
    for title, owns in before.items():
        now = list(here.get(title) or elsewhere.get(title) or [])
        if not now:
            found.append(_vanished(path, title, " ".join(owns), moved))
            continue
        for own in owns:
            if own in now:
                now.remove(own)
                continue
            new = now.pop(0) if now else ""
            detail = _first_difference(own, new) if new else "an occurrence of this title is gone"
            found.append(ChangedTest(path, title, BODY_CHANGED, detail))
    return found


# -- every language -----------------------------------------------------------------


def _js_tests(text: str) -> dict[str, list[str]]:
    return _group_by_title(_js_calls(text))


def detect(base: Mapping[str, str], head: Mapping[str, str]) -> list[ChangedTest]:
    """The pre-existing tests the tree changed.

    `base` is the baseline's text of each test file that differs in the tree, and
    `head` the tree's text of those same paths (a path missing from `head` was
    deleted) together with every test file the tree added: a test that moved to
    another file is found there, and is not reported as lost. A file whose text
    is the same in both is skipped.
    """
    changes: list[ChangedTest] = []
    py_before: dict[str, dict[str, _PyTest]] = {}
    js_before: dict[str, dict[str, list[str]]] = {}
    for path, text in base.items():
        kind = language(path)
        if kind is None or head.get(path) == text:
            continue
        try:
            if kind == "python":
                py_before[path] = _py_tests(text)
            elif kind == "js":
                js_before[path] = _js_tests(text)
            else:
                changes.append(_other(path, head.get(path)))
        except (SyntaxError, ValueError) as exc:
            changes.append(ChangedTest(path, path, UNREADABLE, f"baseline: {_short(str(exc))}"))
    py_head: dict[str, dict[str, _PyTest]] = {}
    js_head: dict[str, dict[str, list[str]]] = {}
    for path, text in head.items():
        kind = language(path)
        try:
            if kind == "python":
                py_head[path] = _py_tests(text)
            elif kind == "js":
                js_head[path] = _js_tests(text)
        except (SyntaxError, ValueError) as exc:
            if path in py_before or path in js_before:
                changes.append(ChangedTest(path, path, UNREADABLE, f"tree: {_short(str(exc))}"))
                py_before.pop(path, None)
                js_before.pop(path, None)
    py_names = {n for tests in py_before.values() for n in tests}
    js_names = {n for tests in js_before.values() for n in tests}
    for path, tests in py_before.items():
        elsewhere = {
            n: t for other, got in py_head.items() if other != path for n, t in got.items()
        }
        changes += _py_changes(path, tests, py_head.get(path, {}), elsewhere, py_names)
    for path, titles in js_before.items():
        elsewhere_js: dict[str, list[str]] = {}
        for other, got in js_head.items():
            if other != path:
                for title, owns in got.items():
                    elsewhere_js.setdefault(title, []).extend(owns)
        changes += _js_changes(path, titles, js_head.get(path, {}), elsewhere_js, js_names)
    return sorted(changes, key=lambda c: (c.path, c.name, c.kind, c.detail))


def _other(path: str, head: str | None) -> ChangedTest:
    said = "deleted" if head is None else "its text differs from the baseline"
    return ChangedTest(
        path,
        path,
        FILE_CHANGED,
        f"a test file in a language this check cannot read test by test: {said}",
    )


# -- the label ----------------------------------------------------------------------


_FLIP_LINE: Final = re.compile(r"^\s*flip:\s*(?P<rest>.*)$", re.IGNORECASE)
_QUOTES: Final = "\"'`"


def parse_flips(message: str, names: Collection[str]) -> dict[str, str]:
    """The evidence written for each of `names` by a `flip: <name> -- <evidence>` line.

    A line names a test when, after `flip:`, it carries exactly that name (quoted or
    not) and then a non-word character or the end; where two names fit one line
    the longer wins, so `flip: parses a path -- ...` never answers for `parses`.
    The evidence is the rest of the line and the lines that follow it up to a blank
    line or the next `flip:` line (a wrapped commit message). A name with no line
    is absent from the result; one whose line has no evidence maps to "".
    """
    ordered = sorted(names, key=len, reverse=True)
    lines = message.splitlines()
    found: dict[str, str] = {}
    for index, line in enumerate(lines):
        match = _FLIP_LINE.match(line)
        if match is None:
            continue
        rest = match.group("rest").strip()
        for name in ordered:
            tail = _after_name(rest, name)
            if tail is None:
                continue
            more: list[str] = []
            for follow in lines[index + 1 :]:
                if not follow.strip() or _FLIP_LINE.match(follow):
                    break
                more.append(follow.strip())
            found.setdefault(name, " ".join([tail, *more]).strip())
            break
    return found


def _after_name(rest: str, name: str) -> str | None:
    """What follows `name` at the start of `rest`, separator trimmed; None if `rest`
    does not start with it."""
    for lead in ("", *_QUOTES):
        start = lead + name + lead
        if rest.startswith(start):
            tail = rest[len(start) :]
            if tail and (tail[0].isalnum() or tail[0] == "_"):
                continue
            return tail.lstrip(" \t-\u2013\u2014:;,.").strip()
    return None


_OLD_TEST: Final = r"(?:the |this |that |an? )?(?:old|existing|original|previous|pre-existing)? ?"
_THING: Final = r"(?:test|assertion|expectation|check|pin)s?"
_FAILS: Final = (
    r"(?:fail(?:s|ed|ing)?|break(?:s|ing)?|broke(?:n)?|(?:is|are|was|were|went|turn(?:s|ed)?) red"
    r"|(?:no longer|not|n'?t|stopped?) (?:pass(?:es|ed|ing)?|match(?:es|ed|ing)?|hold(?:s|ing)?)"
    r"|(?:does|do|did) not (?:pass|match|hold)|doesn'?t (?:pass|match|hold))"
)
_CIRCULAR: Final = (
    re.compile(rf"\b{_OLD_TEST}{_THING}\b.{{0,40}}\b{_FAILS}\b", re.IGNORECASE),
    re.compile(
        rf"\b(?:the |this |my )?(?:new|updated|changed|current) "
        rf"(?:code|change|implementation|behaviou?r|output|version)\b.{{0,40}}\b{_FAILS}\b",
        re.IGNORECASE,
    ),
    re.compile(
        r"\b(?:fails?|failed|failing|breaks?|broke) (?:on|against|with|under) the "
        r"(?:new|updated|changed|current)\b",
        re.IGNORECASE,
    ),
)


def circular_evidence(evidence: str) -> str | None:
    """Why `evidence` is no evidence, or None when this check cannot say it is not.

    CONTRIBUTING.md bans exactly one shape: "the new code fails the old test" is the
    definition of a flip, not a reason it is right. A clause is circular when it says
    only that (the old test, assertion or expectation fails, breaks or no longer
    passes; or the new code makes it). Evidence is refused when it is empty or every
    clause of it is circular; one clause that says anything else stands the whole.

    This catches the banned shape and nothing more. It cannot tell a true reason
    from a false one, and a flip it accepts is still surfaced for a person
    (`judge`): the label buys a review, not a pass.
    """
    clauses = [c.strip() for c in re.split(r"[.;\n]|\band\b|,", evidence) if re.search(r"\w", c)]
    if not clauses:
        return NO_EVIDENCE_SAID
    if all(any(p.search(c) for p in _CIRCULAR) for c in clauses):
        return CIRCULAR_SAID
    return None


@dataclass(frozen=True)
class Judgement:
    verdict: Literal["fail", "not-proven"]
    detail: str


def judge(changes: list[ChangedTest], message: str) -> Judgement | None:
    """None when nothing changed. Otherwise `fail` unless every changed test has its
    `flip:` line with evidence that is not circular, and then `not-proven`: an accepted
    flip is carried as a finding of its own, never as a pass."""
    if not changes:
        return None
    names = {c.name for c in changes}
    said = parse_flips(message, names)
    unlabelled = sorted(n for n in names if n not in said)
    refused = {n: circular_evidence(said[n]) for n in names if n in said}
    refused = {n: why for n, why in refused.items() if why is not None}
    if unlabelled or refused:
        lines = [
            f"{len(names)} pre-existing test(s) changed, "
            f"{len(unlabelled) + len(refused)} without a usable flip label. {FLIP_RULE}"
        ]
        for change in changes:
            note = ""
            if change.name in refused:
                note = f" [flip line refused: {refused[change.name]}]"
            elif change.name in unlabelled:
                note = " [no flip line]"
            lines.append(
                f"- {change.path}: {change.name!r} -- {change.kind}: {change.detail}{note}"
            )
        return Judgement("fail", "\n".join(lines))
    shown = ", ".join(sorted(names))
    lines = [
        f"flipped test(s): {shown}; evidence as written by the author; needs a person's review"
    ]
    lines += [f"- {n!r}: {_short(said[n], 160)}" for n in sorted(names)]
    return Judgement("not-proven", "\n".join(lines))
