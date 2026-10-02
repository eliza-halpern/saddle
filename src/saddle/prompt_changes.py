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

The second half of the module is the benchmark's score contract, also pure:
the command's last stdout line must be a JSON object such as `{"score": 0.83,
"n": 12, "label": "smoke"}`. `score` is a finite number; `n` (how many cases
the score is over) and `label` are optional and, if present, must be a
non-negative integer and a string. Anything else, a command that exits
non-zero, times out or cannot be launched is "no score", and no score is
never a pass (`judge`).
"""

from __future__ import annotations

import ast
import json
from collections.abc import Iterator, Mapping
from dataclasses import dataclass
from typing import Final, Literal

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


# -- the benchmark's score ------------------------------------------------------


@dataclass(frozen=True)
class Score:
    """One benchmark run's reading of its last stdout line."""

    score: float
    n: int | None = None
    label: str = ""


@dataclass(frozen=True)
class Measured:
    """A benchmark run: its score, or in `failure` why it has none."""

    score: Score | None
    failure: str = ""


def read_score(stdout: str) -> Score | None:
    """The score on `stdout`'s last non-blank line, or None when that line is
    not a JSON object holding a finite numeric `score` and well-formed
    optional `n` and `label`. Earlier lines are the command's own log."""
    lines = stdout.strip().splitlines()
    if not lines:
        return None
    try:
        body = json.loads(lines[-1])
    except ValueError:
        return None
    if not isinstance(body, dict):
        return None
    value, n, label = body.get("score"), body.get("n"), body.get("label", "")
    if isinstance(value, bool) or not isinstance(value, int | float) or not -1e300 < value < 1e300:
        return None
    if "n" in body and (isinstance(n, bool) or not isinstance(n, int) or n < 0):
        return None
    if not isinstance(label, str):
        return None
    return Score(float(value), n, label)


def measured(exit_code: int, stdout: str, *, timed_out: bool, unavailable: bool) -> Measured:
    """A finished run as a `Measured`: a score only from a command that was
    launched, finished in time and exited 0 with a readable last line."""
    if unavailable:
        return Measured(None, "could not be launched")
    if timed_out:
        return Measured(None, "timed out")
    if exit_code != 0:
        return Measured(None, f"failed to run (exit {exit_code})")
    score = read_score(stdout)
    if score is None:
        return Measured(None, "ran but produced no readable score")
    return Measured(score, "")


def _shown(score: Score) -> str:
    n = "" if score.n is None else f" (n={score.n})"
    label = f" [{score.label}]" if score.label else ""
    return f"{score.score:.4g}{n}{label}"


def unconfigured_detail(lead: str) -> str:
    """The finding's words when prompt text changed and the project sets no benchmark."""
    return (
        f"{lead}; no test, coverage or mutation result measures a prompt's effect, and this "
        "project configures no prompt benchmark. Not proven: a person reads the change."
    )


def judge(
    lead: str,
    head: Measured,
    base: Measured | None,
    *,
    floor: float | None,
    margin: float | None,
) -> tuple[Literal["pass", "fail", "not-proven"], str]:
    """The verdict and words for a benchmark run on the head tree (and on the
    baseline's, when it was run), against the bars the project set at the baseline.

    Fails only on a readable head score that is below `floor`, or more than
    `margin` below a readable baseline score. A run with no head score is
    not proven, never a fail and never a pass: an environment the benchmark
    cannot run in is no evidence about the prompt. With neither bar set the
    scores are reported and not judged; a margin with no baseline score
    cannot be judged either."""
    if head.score is None:
        return "not-proven", (
            f"{lead}; the prompt benchmark {head.failure}, so the prompt's effect is not "
            "proven. A person runs it."
        )
    shown = f"head {_shown(head.score)}"
    if base is not None:
        shown = (
            f"base {_shown(base.score)} -> {shown}"
            if base.score is not None
            else f"base: the benchmark {base.failure}; {shown}"
        )
    got = head.score.score
    reasons = []
    if floor is not None and got < floor:
        reasons.append(
            f"head is below the floor {floor:g} (prompt-benchmark-floor, read at the baseline)"
        )
    if margin is not None and base is not None and base.score is not None:
        if got < base.score.score - margin:
            reasons.append(
                f"head is more than {margin:g} below base "
                f"(prompt-benchmark-margin, read at the baseline)"
            )
    if reasons:
        return "fail", f"{lead}; prompt benchmark {shown}: {'; '.join(reasons)}."
    if floor is None and margin is None:
        return "not-proven", (
            f"{lead}; prompt benchmark {shown}. Not proven: the project sets no "
            "prompt-benchmark-floor or prompt-benchmark-margin, so the score is reported, "
            "not judged."
        )
    if margin is not None and (base is None or base.score is None):
        return "not-proven", (
            f"{lead}; prompt benchmark {shown}. Not proven: a margin is set but the "
            "baseline has no score to compare with."
        )
    return "pass", f"{lead}; prompt benchmark {shown}, within the project's bar."
