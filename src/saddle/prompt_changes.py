"""Which prompt text a change edited, named, from the baseline and head sources.

A prompt is a constraint on a model's behaviour that no test, coverage figure
or mutant can judge: a changed sentence is "covered" by any test that imports
its module, and a mutant inside a prompt string is a text-only mutant the
audit leaves out. So a prompt edit used to pass the audit with nothing said
about its effect. This module only finds the edit; the audit decides what to
say about it (`auditor.prompt_effect_detail`).

`saddle.prompt_constants` is not a detector for this: it checks which
constants a task's own prompt names against that task's source. No other
module says which strings in a project are prompts, so the definition is
here, and deliberately narrow, because a false name costs a reader's
attention and a missed one costs the very silence this exists to end.

Prompt text is one of two things:

- A module-level assignment to an UPPER_CASE name whose last `_` segment is
  `PROMPT`, `PROMPTS`, `RULE`, `RULES`, `PERSONAS`, `GRAMMAR` or
  `INSTRUCTIONS` (`SYSTEM_PROMPT`, `EDIT_RULES`, `BUILTIN_PERSONAS`), when
  the value holds a string of at least `MIN_WORDS` words. The last segment,
  not any: `PROMPT_STYLE` is a stylesheet and `RUFF_RULES` is a tuple of
  rule codes, and neither is a prompt. Reported as `path: NAME`.
- A function or method (at module or class level) whose name's last `_`
  segment is `prompt` (`build_worker_prompt`, `Class.provider_prompt`): the
  string pieces its body holds, minus docstrings and the messages of a
  `raise`; an f-string counts whole, fields included. Reported as `path: qualname`.

A name qualifies if it does on either side, so deleting or renaming a prompt
is reported, and a constant that stops looking like prose is still reported
once, for the edit that did it. Values are compared as the parser reads them
(adjacent literals joined, `"a" + "b"` folded, dict key order ignored), so
re-wrapping a string without changing its value is no change; a value that is
not a literal (an f-string, a call) is compared by its expression tree.
Comments and docstrings are not parsed into values and never count.

What is not detected: a prompt assembled across helpers or from a name that
does not follow the convention, a prompt passed inline as an argument, a tool
description, a prompt kept in a data file, and a prompt-named function nested
inside another function. A file that cannot be read as Python on either side
is returned in `unreadable`, never read as "nothing changed".
"""

from __future__ import annotations

import ast
from collections.abc import Iterator, Mapping
from dataclasses import dataclass
from typing import Final

PROMPT_WORDS: Final = frozenset(
    {"PROMPT", "PROMPTS", "RULE", "RULES", "PERSONAS", "GRAMMAR", "INSTRUCTIONS"}
)
"""The last segment of a module-level name that marks it as prompt text."""

MIN_WORDS: Final = 3
"""A constant is prose when one of its strings holds this many words: a tuple
of rule codes or a style name is not a prompt."""


@dataclass(frozen=True)
class PromptChanges:
    """The prompt text a change edited, and the files that could not be read."""

    names: tuple[str, ...] = ()
    """`path: NAME` or `path: qualname`, sorted."""
    unreadable: tuple[str, ...] = ()
    """Changed files that did not parse on one side, sorted: no verdict on them."""

    def __bool__(self) -> bool:
        return bool(self.names or self.unreadable)


class _Fold(ast.NodeTransformer):
    """Normalises what does not change a value: `"a" + "b"` becomes `"ab"`
    and a dict with constant keys is ordered by key."""

    def visit_BinOp(self, node: ast.BinOp) -> ast.AST:
        self.generic_visit(node)
        left, right = node.left, node.right
        if (
            isinstance(node.op, ast.Add)
            and isinstance(left, ast.Constant)
            and isinstance(right, ast.Constant)
            and isinstance(left.value, str)
            and isinstance(right.value, str)
        ):
            return ast.Constant(left.value + right.value)
        return node

    def visit_Dict(self, node: ast.Dict) -> ast.AST:
        self.generic_visit(node)
        pairs = [(k, v) for k, v in zip(node.keys, node.values, strict=True) if k is not None]
        if len(pairs) == len(node.keys) and all(isinstance(k, ast.Constant) for k, _ in pairs):
            pairs.sort(key=lambda pair: ast.dump(pair[0]))
            node.keys = [key for key, _ in pairs]
            node.values = [value for _, value in pairs]
        return node


def _folded(node: ast.AST) -> ast.AST:
    folded: ast.AST = _Fold().visit(node)
    return ast.fix_missing_locations(folded)


def _prose(value: ast.AST) -> bool:
    return any(
        isinstance(n, ast.Constant)
        and isinstance(n.value, str)
        and len(n.value.split()) >= MIN_WORDS
        for n in ast.walk(value)
    )


def _prompt_name(name: str) -> bool:
    return name.upper() == name and name.strip("_").rsplit("_", 1)[-1] in PROMPT_WORDS


def _pieces(node: ast.AST) -> Iterator[str]:
    """The string pieces under `node`, in source order, without a bare string
    statement (a docstring) or anything under a `raise`."""
    for child in ast.iter_child_nodes(node):
        if isinstance(child, ast.Raise) or (
            isinstance(child, ast.Expr)
            and isinstance(child.value, ast.Constant)
            and isinstance(child.value.value, str)
        ):
            continue
        if isinstance(child, ast.Constant) and isinstance(child.value, str):
            yield child.value
        elif isinstance(child, ast.JoinedStr):
            # Its fields and conversions (`{task!r}`) change the rendered text
            # as much as its literal parts, so the whole expression is the piece.
            yield ast.dump(child)
        else:
            yield from _pieces(child)


def _constants(body: list[ast.stmt]) -> dict[str, str | None]:
    """Each prompt-named module-level constant: its value's normal form, or
    None when the name is not prompt text on this side (no prose in it)."""
    found: dict[str, str | None] = {}
    for stmt in body:
        if isinstance(stmt, ast.Assign) and len(stmt.targets) == 1:
            target, value = stmt.targets[0], stmt.value
        elif isinstance(stmt, ast.AnnAssign) and stmt.value is not None:
            target, value = stmt.target, stmt.value
        else:
            continue
        if isinstance(target, ast.Name) and _prompt_name(target.id):
            found[target.id] = ast.dump(_folded(value)) if _prose(value) else None
    return found


def _functions(body: list[ast.stmt], prefix: str = "") -> dict[str, str]:
    """Each prompt-named function or method (module or class level): its
    string pieces' normal form."""
    found: dict[str, str] = {}
    for stmt in body:
        if isinstance(stmt, ast.ClassDef):
            found.update(_functions(stmt.body, f"{prefix}{stmt.name}."))
        elif isinstance(stmt, ast.FunctionDef | ast.AsyncFunctionDef) and (
            stmt.name.strip("_").rsplit("_", 1)[-1] == "prompt"
        ):
            found[f"{prefix}{stmt.name}"] = repr(tuple(_pieces(_folded(stmt))))
    return found


def _parsed(source: str | None) -> ast.Module | None:
    try:
        return ast.parse(source or "")
    except (SyntaxError, ValueError):
        return None


def prompt_changes(base: Mapping[str, str | None], head: Mapping[str, str | None]) -> PromptChanges:
    """The prompt text that differs between the `base` and `head` sources.

    Both map a path to its source, None for a file absent on that side (added
    or deleted). Only the paths of `head` and `base` are looked at; the caller
    passes the changed Python files that are not tests."""
    names: set[str] = set()
    unreadable: set[str] = set()
    for path in sorted(set(base) | set(head)):
        before, after = _parsed(base.get(path)), _parsed(head.get(path))
        if before is None or after is None:
            unreadable.add(path)
            continue
        old_c, new_c = _constants(before.body), _constants(after.body)
        for name in set(old_c) | set(new_c):
            if (old_c.get(name) is not None or new_c.get(name) is not None) and old_c.get(
                name
            ) != new_c.get(name):
                names.add(f"{path}: {name}")
        old_f, new_f = _functions(before.body), _functions(after.body)
        names.update(
            f"{path}: {n}" for n in set(old_f) | set(new_f) if old_f.get(n) != new_f.get(n)
        )
    return PromptChanges(tuple(sorted(names)), tuple(sorted(unreadable)))
