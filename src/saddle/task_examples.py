"""Task-requirement examples: their shape, their checks, and one example's verdict.

The pure half of P1 (the `task-requirements` gate). An example is an input
(`setup` statements and one `call` expression), written without seeing any
code, with the predictions, executed mini-references, recorded alternative
readings and known-correct probe outcomes sealed beside it at extraction
(`task_requirements.json`). Nothing here runs code or calls a model:

- `snippet_problem` and `reference_problem` are the two AST whitelists,
  checked before anything runs (the sandbox is the boundary; these are a
  second layer).
- `parse_value`, `decode_value` and `compare` are the comparison rule:
  Python `==`, except floats (`math.isclose` with `rel_tol=1e-9` and an
  absolute tolerance from the route, a near-miss band, NaN matches NaN,
  infinities by sign) and containers holding floats (elementwise).
- `classify` decides, mechanically, what an example may do: refuse by
  route (a) (`literal`: the input and the outcome appear in a cited binding
  unit, with no negation between them) or route (b) (`executed-reference`:
  k of k predictions and their executed references agree (a reference whose
  args were at fault abstains, if two others ran), k is effectively
  more than one, and every known-correct probe returns the outcome, from
  at least `PROBES_NEEDED` probes of one source), or only ask. The model
  can mark nothing eligible.
- `judge` turns one tree outcome into one row: pass, code-wrong, question,
  not-proven (could not call, or a value it cannot compare), unknown (HANG)
  or not-judged.
- A `raises` outcome names a type (K2 R-2). `Exception` and
  `BaseException` (`UNTYPED`) type nothing: they match almost any crash, so
  such an example is at most a question. Any other name is resolved on the
  tree by the driver (`task_requirements.DRIVER`: a builtin, else the
  callee's module, else a class defined in exactly one module of the tree)
  and matched by `isinstance`; a name that does not resolve is a question,
  never `code-wrong`.

Layering: pure, beside `gates`; imports only `task_units`.
"""

from __future__ import annotations

import ast
import dataclasses
import math
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from decimal import Decimal
from fractions import Fraction
from typing import Any, Final, Literal

from saddle.task_units import Unit, Units

# -- constants: saddle's, never the model's -----------------------------------

K_PREDICTORS: Final = 3
"""Blind predictors in P-b; route (b) needs all of them to agree."""

REFERENCES_NEEDED: Final = 2
"""How many of the k executed references route (b) needs to have run, all
agreeing, when the others abstained (`Reference.abstains`): saddle's, never
the model's."""

ARGS_REFUSED: Final = "refused: its args: "
"""A reference's status when its predictor's args hold a value the input does
not (`args_problem`, `task_passes.run_references`)."""
NOT_BOUND: Final = "could not call: "
"""A reference's status when the args do not bind to its own signature. The
driver checks `inspect.signature(ref).bind(*args)` before the call, so a
TypeError the body raises at run time is an outcome (`ran`), never this."""

MAX_INPUTS_PER_UNIT: Final = 3
MAX_EXAMPLES: Final = 150
EXAMPLE_TIMEOUT_S: Final = 5.0
REFERENCE_CALL_TIMEOUT_S: Final = 2.0
REFERENCE_TOTAL_TIMEOUT_S: Final = 60.0

REL_TOL: Final = 1e-9
ROUTE_B_ABS_TOL: Final = 1e-12
NEAR_MISS_REL: Final = 1e-6
NEAR_MISS_ABS: Final = 1e-12
WOULD_REFUSE: Final = "would refuse at full strength"
"""A question row's reason when refusal is not licensed but the example would refuse."""

PROBE_SOURCES: Final[tuple[str, ...]] = ("oracle-pass", "user")
"""Where a known-correct probe's correctness comes from, outside the model
(spec §4.5): an oracle-PASS tree of the same task (the bench), or a
reference the user supplied (the product)."""

PROBES_NEEDED: Final[Mapping[str, int]] = {"oracle-pass": 3, "user": 1}
"""How many probes of one source route (b) needs, all agreeing: m = 3 on the
bench (the top of the held-out k = 1-3 range), at least 1 user reference in
the product. Fewer leave the example a question."""

NOT_EXECUTABLE: Final[tuple[str, ...]] = (
    "packaging-or-install",
    "performance-or-time",
    "environment",
    "prose-only",
    "needs-external-resource",
)
"""The closed reasons a unit may be marked not executable; each is a named row."""

UNTYPED: Final[tuple[str, ...]] = ("Exception", "BaseException")
"""Raise type names that type nothing: under the subclass rule they match
almost any crash, a fall-through after a deleted validation included."""

RAISE_TOKENS: Final[tuple[str, ...]] = ("raise", "error", "exception")
"""A `raises` example may refuse only if a cited unit contains one of these,
case-insensitively, anywhere in its text: "raise" covers raises and raised,
"error" covers `ValueError` and "errors"."""

NEGATION: Final = re.compile(
    r"\b(?:not|never|no longer|instead of|rather than|unlike|cannot)\b|n't\b", re.IGNORECASE
)
"""Between an example's input literal and its outcome literal, any of these
makes route (a) unavailable: the text may state a forbidden outcome."""

REFERENCE_MODULES: Final = frozenset(
    {
        "math",
        "cmath",
        "decimal",
        "fractions",
        "bisect",
        "heapq",
        "itertools",
        "functools",
        "operator",
        "collections",
        "string",
        "re",
        "copy",
    }
)
"""What a mini-reference may import, and the only stdlib modules a snippet may
import from: nothing that reaches files, processes, the network or the interpreter."""

FORBIDDEN_NAMES: Final = frozenset(
    {
        "open",
        "exec",
        "eval",
        "compile",
        "__import__",
        "globals",
        "locals",
        "vars",
        "getattr",
        "setattr",
        "delattr",
        "breakpoint",
        "input",
        "exit",
        "quit",
        "help",
        "memoryview",
    }
)

VALUE_CONSTRUCTORS: Final = frozenset({"Decimal", "Fraction", "frozenset", "set", "float"})
"""The calls an expected value may use: `Decimal("0.1")`, `float("nan")`."""

Route = Literal["literal", "executed-reference", "decided-unverified", "split", "question-only"]
RowStatus = Literal["pass", "code-wrong", "question", "not-proven", "unknown", "not-judged"]


# -- values and the comparison rule -------------------------------------------


def parse_value(text: str) -> object:
    """An expected value's literal text as a Python value, or ValueError.

    Literals, displays, unary minus, and the `VALUE_CONSTRUCTORS` called on
    literals. Nothing else is evaluated.
    """
    try:
        node = ast.parse(text.strip(), mode="eval").body
    except SyntaxError as exc:
        msg = f"not a literal: {text!r}"
        raise ValueError(msg) from exc
    return _value(node)


def _value(node: ast.expr) -> object:
    if isinstance(node, ast.Constant):
        return node.value
    if isinstance(node, ast.UnaryOp) and isinstance(node.op, ast.USub | ast.UAdd):
        inner = _value(node.operand)
        if isinstance(inner, int | float | Decimal | Fraction) and not isinstance(inner, bool):
            return -inner if isinstance(node.op, ast.USub) else inner
    elif isinstance(node, ast.List):
        return [_value(e) for e in node.elts]
    elif isinstance(node, ast.Tuple):
        return tuple(_value(e) for e in node.elts)
    elif isinstance(node, ast.Set):
        return {_value(e) for e in node.elts}
    elif isinstance(node, ast.Dict) and all(k is not None for k in node.keys):
        keys = [_value(k) for k in node.keys if k is not None]
        return dict(zip(keys, [_value(v) for v in node.values], strict=True))
    elif (
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id in VALUE_CONSTRUCTORS
        and not node.keywords
    ):
        args = [_value(a) for a in node.args]
        return {
            "Decimal": Decimal,
            "Fraction": Fraction,
            "frozenset": frozenset,
            "set": set,
            "float": float,
        }[node.func.id](*args)
    msg = f"not a literal: {ast.unparse(node)!r}"
    raise ValueError(msg)


def literal_text(v: object) -> str:
    """`v` written so that `parse_value` reads it back: `repr`, except that
    NaN and the infinities, which `repr` writes as bare names, are written as
    `float("nan")`, `float("inf")` and `-float("inf")`, containers recursively."""
    if isinstance(v, float) and (math.isnan(v) or math.isinf(v)):
        name = "nan" if math.isnan(v) else "inf"
        return f'{"-" if v < 0 else ""}float("{name}")'
    if isinstance(v, Decimal | Fraction):
        return f'{type(v).__name__}("{v}")'
    if isinstance(v, list):
        return f"[{', '.join(literal_text(x) for x in v)}]"
    if isinstance(v, tuple):
        return f"({', '.join(literal_text(x) for x in v)}{',' if len(v) == 1 else ''})"
    if isinstance(v, frozenset):
        return f"frozenset([{', '.join(literal_text(x) for x in v)}])"
    if isinstance(v, set):
        return f"{{{', '.join(literal_text(x) for x in v)}}}" if v else "set()"
    if isinstance(v, dict):
        return f"{{{', '.join(f'{literal_text(k)}: {literal_text(x)}' for k, x in v.items())}}}"
    return repr(v)


def encode_value(v: object) -> dict[str, Any]:
    """A value as tagged JSON (`decode_value` reverses it); TypeError for
    anything else, which the driver reports as `opaque`.

    Self-contained: the example driver runs this function's own source in
    the tree's interpreter (`task_requirements.DRIVER`)."""
    from decimal import Decimal as _Decimal
    from fractions import Fraction as _Fraction

    if v is None:
        return {"t": "none"}
    if isinstance(v, bool):
        return {"t": "bool", "v": v}
    if isinstance(v, int):
        return {"t": "int", "v": int(v)}
    if isinstance(v, float):
        return {"t": "float", "v": repr(float(v))}
    if isinstance(v, str):
        return {"t": "str", "v": str(v)}
    if isinstance(v, _Decimal):
        return {"t": "decimal", "v": str(v)}
    if isinstance(v, _Fraction):
        return {"t": "fraction", "v": str(v)}
    if isinstance(v, dict):
        return {"t": "dict", "v": [[encode_value(a), encode_value(b)] for a, b in v.items()]}
    for kind, cls in (("list", list), ("tuple", tuple), ("set", set), ("frozenset", frozenset)):
        if type(v) is cls:
            return {"t": kind, "v": [encode_value(x) for x in v]}
    msg = f"{type(v).__name__} is not a comparable value"
    raise TypeError(msg)


def decode_value(tagged: Any) -> object:
    """A tree value the driver encoded (`{"t": tag, "v": ...}`) as a Python value."""
    tag, raw = tagged["t"], tagged.get("v")
    if tag in ("none", "bool", "int", "str"):
        return raw
    if tag == "float":
        return float(raw)
    if tag == "decimal":
        return Decimal(raw)
    if tag == "fraction":
        return Fraction(raw)
    if tag == "dict":
        return {decode_value(k): decode_value(v) for k, v in raw}
    items = [decode_value(x) for x in raw]
    if tag == "list":
        return items
    if tag == "tuple":
        return tuple(items)
    if tag == "set":
        return set(items)
    if tag == "frozenset":
        return frozenset(items)
    msg = f"unknown value tag {tag!r}"
    raise ValueError(msg)


Closeness = Literal["equal", "near-miss", "differ"]


def _number(x: object) -> bool:
    return isinstance(x, int | float) and not isinstance(x, bool)


def compare(expected: object, actual: object, abs_tol: float) -> Closeness:
    """`actual` against `expected` under the comparison rule (spec §3.3)."""
    if (
        _number(expected)
        and _number(actual)
        and (isinstance(expected, float) or isinstance(actual, float))
    ):
        return _floats(float(expected), float(actual), abs_tol)  # type: ignore[arg-type]
    if type(expected) is type(actual) and isinstance(expected, list | tuple):
        assert isinstance(actual, list | tuple)
        if len(expected) != len(actual):
            return "differ"
        return _worst([compare(e, a, abs_tol) for e, a in zip(expected, actual, strict=True)])
    if isinstance(expected, dict) and isinstance(actual, dict):
        if expected.keys() != actual.keys():
            return "differ"
        return _worst([compare(expected[k], actual[k], abs_tol) for k in expected])
    try:
        return "equal" if type(expected) is type(actual) and expected == actual else "differ"
    except Exception:  # an `==` that raises decides nothing
        return "differ"


def _floats(expected: float, actual: float, abs_tol: float) -> Closeness:
    if math.isnan(expected) or math.isnan(actual):
        return "equal" if math.isnan(expected) and math.isnan(actual) else "differ"
    if math.isinf(expected) or math.isinf(actual):
        return "equal" if expected == actual else "differ"
    if math.isclose(actual, expected, rel_tol=REL_TOL, abs_tol=abs_tol):
        return "equal"
    if math.isclose(actual, expected, rel_tol=NEAR_MISS_REL, abs_tol=NEAR_MISS_ABS):
        return "near-miss"
    return "differ"


def _worst(parts: Sequence[Closeness]) -> Closeness:
    return "differ" if "differ" in parts else "near-miss" if "near-miss" in parts else "equal"


def literal_abs_tol(text: str) -> float:
    """Half a unit in the last decimal place the literal `text` writes, the
    smallest over its float literals; `ROUTE_B_ABS_TOL` if it writes none.

    `0.35` gives 0.005, `1.0` gives 0.05, `2.5e-3` gives 5e-5."""
    tols = []
    for token in re.findall(r"(?<![\w.])\d+\.\d*(?:[eE][-+]?\d+)?|\d+[eE][-+]?\d+", text):
        mantissa, _, exponent = token.lower().partition("e")
        places = len(mantissa.partition(".")[2])
        tols.append(0.5 * 10.0 ** (int(exponent or 0) - places))
    return min(tols) if tols else ROUTE_B_ABS_TOL


# -- outcomes -----------------------------------------------------------------


@dataclass(frozen=True)
class Outcome:
    """An expected or observed outcome: a value's literal text, or a raise."""

    kind: Literal["value", "raises"]
    text: str

    @staticmethod
    def of(kind: str, text: str) -> Outcome:
        """A written outcome: `value` (a literal), `raises` (a type name) or
        `true` (the call is a boolean expression that holds)."""
        if kind == "true":
            return Outcome("value", "True")
        if kind == "raises":
            if not text.strip().isidentifier():
                msg = f"not an exception name: {text!r}"
                raise ValueError(msg)
            return Outcome("raises", text.strip())
        if kind != "value":
            msg = f"unknown outcome kind {kind!r}"
            raise ValueError(msg)
        return Outcome("value", literal_text(parse_value(text)))

    def value(self) -> object:
        return parse_value(self.text)

    def show(self) -> str:
        return self.text if self.kind == "value" else f"raises {self.text}"

    def same(self, other: Outcome, abs_tol: float = ROUTE_B_ABS_TOL) -> bool:
        """The same canonical outcome: one raise name, or values `compare` calls equal."""
        if self.kind != other.kind:
            return False
        if self.kind == "raises":
            return self.text == other.text
        return compare(self.value(), other.value(), abs_tol) == "equal"

    def to_dict(self) -> dict[str, str]:
        return {"kind": self.kind, "text": self.text}

    @staticmethod
    def from_dict(data: Mapping[str, Any]) -> Outcome:
        return Outcome.of(str(data["kind"]), str(data["text"]))


# -- the whitelists -----------------------------------------------------------


_SNIPPET_NODES: Final = (
    ast.Module,
    ast.Expression,
    ast.Expr,
    ast.Assign,
    ast.AugAssign,
    ast.ImportFrom,
    ast.alias,
    ast.Name,
    ast.Attribute,
    ast.Call,
    ast.keyword,
    ast.Constant,
    ast.List,
    ast.Tuple,
    ast.Dict,
    ast.Set,
    ast.Subscript,
    ast.Slice,
    ast.BinOp,
    ast.UnaryOp,
    ast.BoolOp,
    ast.Compare,
    ast.IfExp,
    ast.expr_context,
    ast.operator,
    ast.unaryop,
    ast.boolop,
    ast.cmpop,
)

_REFERENCE_EXTRA: Final = (
    ast.FunctionDef,
    ast.arguments,
    ast.arg,
    ast.Return,
    ast.If,
    ast.For,
    ast.While,
    ast.Break,
    ast.Continue,
    ast.Pass,
    ast.Raise,
    ast.Import,
    ast.ListComp,
    ast.SetComp,
    ast.DictComp,
    ast.GeneratorExp,
    ast.comprehension,
    ast.Lambda,
    ast.AnnAssign,
    ast.Starred,
    ast.JoinedStr,
    ast.FormattedValue,
)


def _dunder(name: str) -> bool:
    return name.startswith("__") and name.endswith("__")


READABLE_DUNDERS: Final = frozenset({"__name__"})
"""The dunder attributes a reference may read (never assign): `type(x).__name__`
yields a str for an error message and opens no way out of the whitelist."""


def _common_problem(
    tree: ast.AST,
    allowed: tuple[type[ast.AST], ...],
    stdlib: frozenset[str],
    readable: frozenset[str] = frozenset(),
) -> str | None:
    for node in ast.walk(tree):
        if not isinstance(node, allowed):
            return f"{type(node).__name__} is not allowed"
        if isinstance(node, ast.Name) and (node.id in FORBIDDEN_NAMES or _dunder(node.id)):
            return f"name {node.id!r} is not allowed"
        if (
            isinstance(node, ast.Attribute)
            and _dunder(node.attr)
            and not (node.attr in readable and isinstance(node.ctx, ast.Load))
        ):
            return f"attribute {node.attr!r} is not allowed"
        if isinstance(node, ast.Import):
            bad = [a.name for a in node.names if a.name not in REFERENCE_MODULES]
            if bad:
                return f"import of {', '.join(bad)} is not allowed"
        if isinstance(node, ast.ImportFrom):
            module = node.module or ""
            top = module.split(".")[0]
            if node.level or not module or (top in stdlib and top not in REFERENCE_MODULES):
                return f"import from {'.' * node.level}{module} is not allowed"
            if any(a.name == "*" or _dunder(a.name) for a in node.names):
                return f"import of {', '.join(a.name for a in node.names)} is not allowed"
    return None


def snippet_problem(setup: Sequence[str], call: str, stdlib: frozenset[str]) -> str | None:
    """Why an example's `setup` and `call` may not run, or None: the snippet whitelist.

    Assignments and augmented assignments to plain names, `from M import N`
    (M inside the repo, or in `REFERENCE_MODULES`), names, attributes (no
    dunders), calls, literals, displays, subscripts, slices, arithmetic,
    comparison and boolean operators. `stdlib` is the interpreter's
    `sys.stdlib_module_names`, passed in to keep this module pure.
    """
    try:
        statements = ast.parse("\n".join(setup))
        expression = ast.parse(call.strip(), mode="eval")
    except SyntaxError as exc:
        return f"does not parse: {exc.msg}"
    for node in ast.walk(statements):
        if isinstance(node, ast.Assign | ast.AugAssign):
            targets = node.targets if isinstance(node, ast.Assign) else [node.target]
            if not all(isinstance(t, ast.Name) for t in targets):
                return "assignment to anything but a plain name is not allowed"
    return _common_problem(statements, _SNIPPET_NODES, stdlib) or _common_problem(
        expression, _SNIPPET_NODES, stdlib
    )


def reference_problem(source: str, stdlib: frozenset[str]) -> str | None:
    """Why a mini-reference may not run, or None: the reference whitelist.

    One top-level `def ref(...)` (or `def ref_<anything>(...)`, run as `ref`;
    see `reference_def`) or `def ok(inp, out)`, optionally after imports from
    `REFERENCE_MODULES`; inside, the snippet language plus control flow,
    comprehensions, lambdas and `raise`. Never `open`, `exec`, `eval`,
    `compile`, `__import__`, `global`, `nonlocal`, a dunder (reading
    `x.__name__` aside: `READABLE_DUNDERS`), or an import from the repo or
    any other module.
    """
    try:
        tree = ast.parse(source)
    except SyntaxError as exc:
        return f"does not parse: {exc.msg}"
    defs = [n for n in tree.body if isinstance(n, ast.FunctionDef)]
    rest = [
        n for n in tree.body if not isinstance(n, ast.FunctionDef | ast.Import | ast.ImportFrom)
    ]
    if rest or len(defs) != 1 or _form(defs[0].name) is None:
        return "must be imports and exactly one top-level def named ref, ref_*, or ok"
    if defs[0].decorator_list:
        return "decorators are not allowed"
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and (node.module or "").split(".")[0] not in (
            REFERENCE_MODULES
        ):
            return f"import from {node.module} is not allowed"
    return _common_problem(
        tree, (*_SNIPPET_NODES, *_REFERENCE_EXTRA), stdlib, READABLE_DUNDERS
    ) or (_restates(defs[0]) if _form(defs[0].name) == "ref" else None)


def _form(name: str) -> Literal["ref", "ok"] | None:
    if name == "ok":
        return "ok"
    return "ref" if name == "ref" or name.startswith("ref_") else None


def reference_def(source: str) -> tuple[Literal["ref", "ok"], str]:
    """An admitted reference's form and the name of the def the driver calls:
    `ref_balance` is called by its own name (so a recursive call inside it
    still resolves) in the `ref` form. Only for a source `reference_problem`
    admitted."""
    name = next(n.name for n in ast.parse(source).body if isinstance(n, ast.FunctionDef))
    return ("ok" if name == "ok" else "ref"), name


def _restates(ref: ast.FunctionDef) -> str | None:
    """Why `ref` cannot check a prediction, or None: one that never reads its
    input, or hands an argument back unchanged, returns what it was given and
    derives nothing."""
    a = ref.args
    params = {p.arg for p in (*a.posonlyargs, *a.args, *a.kwonlyargs, a.vararg, a.kwarg) if p}
    body = [
        n
        for n in ref.body
        if not (isinstance(n, ast.Expr) and isinstance(n.value, ast.Constant))  # a docstring
    ]
    if (
        len(body) == 1
        and isinstance(body[0], ast.Return)
        and isinstance(body[0].value, ast.Name)
        and body[0].value.id in params
    ):
        return "returns its input unchanged, so it derives nothing"
    if not any(isinstance(n, ast.Name) and n.id in params for b in body for n in ast.walk(b)):
        return "never reads its input, so it only restates the prediction"
    return None


# -- the sealed example -------------------------------------------------------


@dataclass(frozen=True)
class Prediction:
    """One blind predictor's outcome for one input, with the words it rests on."""

    outcome: Outcome | None
    """None when the written outcome did not parse: that predictor decided nothing."""
    decides: str
    raw_sha256: str
    """The predictor's whole raw output, hashed: byte-identical outputs are one sample."""
    missing: str = ""
    """Why it gave no outcome, as the extraction sealed it (`task_passes._missing`):
    its reply cut at the token cap, its call failed or returned nothing, and so
    on. "" when it gave one, and in a file sealed before the reason was."""

    @staticmethod
    def from_dict(data: Mapping[str, Any]) -> Prediction:
        raw = data.get("outcome")
        try:
            outcome = Outcome.from_dict(raw) if isinstance(raw, Mapping) else None
        except (ValueError, KeyError):
            outcome = None
        return Prediction(
            outcome,
            str(data.get("decides", "")),
            str(data["raw_sha256"]),
            str(data.get("missing", "")),
        )


@dataclass(frozen=True)
class Reference:
    """One predictor's executed mini-reference for one input."""

    status: str
    """`ran` when it ran; otherwise why not (missing, refused: ..., timeout,
    raised X, could not call: ..., not-canonical, not-discriminating)."""
    outcome: Outcome | None = None
    """`ref` form: what it returned or raised."""
    form: Literal["ref", "ok"] = "ref"
    accepts: bool = False
    """`ok` form: it returned True on the prediction and False on another outcome."""

    def abstains(self) -> bool:
        """It never ran only because the args it was handed are not the input's
        (`ARGS_REFUSED`) or do not bind to its signature (`NOT_BOUND`): a fault
        of the args, so it says nothing about the behaviour either way. Every
        other reason it did not run (refused source, timeout, crash, missing)
        still blocks route (b)."""
        return self.status.startswith((ARGS_REFUSED, NOT_BOUND))

    def agrees(self, prediction: Outcome) -> bool:
        if self.status != "ran":
            return False
        if self.form == "ok":
            return self.accepts
        return self.outcome is not None and self.outcome.same(prediction)

    @staticmethod
    def from_dict(data: Mapping[str, Any]) -> Reference:
        raw = data.get("outcome")
        return Reference(
            status=str(data["status"]),
            outcome=Outcome.from_dict(raw) if isinstance(raw, Mapping) else None,
            form="ok" if data.get("form") == "ok" else "ref",
            accepts=data.get("accepts") is True,
        )


@dataclass(frozen=True)
class Probe:
    """One known-correct implementation's outcome on one input (sealed at extraction)."""

    sha256: str
    status: str
    """`ran`, or why it could not (could not call, HANG, crash)."""
    outcome: Outcome | None = None
    source: str = "oracle-pass"
    """One of `PROBE_SOURCES`; the sealed file's probe list says which."""
    raises: tuple[str, ...] = ()
    """A raise outcome's class and its bases, by name, most derived first."""

    def agrees(self, expected: Outcome) -> bool:
        """It returned `expected`: the same value, or a raise of that type or a subclass."""
        if self.outcome is None:
            return False
        if expected.kind == "raises" and self.outcome.kind == "raises":
            return expected.text in (self.outcome.text, *self.raises)
        return self.outcome.same(expected)

    @staticmethod
    def from_dict(data: Mapping[str, Any]) -> Probe:
        raw = data.get("outcome")
        return Probe(
            str(data["sha256"]),
            str(data["status"]),
            Outcome.from_dict(raw) if isinstance(raw, Mapping) else None,
            source=str(data.get("source", "oracle-pass")),
            raises=tuple(str(r) for r in data.get("raises", ())),
        )


@dataclass(frozen=True)
class Alternative:
    """Another reading of the cited words (P-c), with its outcome and the words that allow it."""

    outcome: Outcome
    words: str

    @staticmethod
    def from_dict(data: Mapping[str, Any]) -> Alternative:
        return Alternative(Outcome.from_dict(data["outcome"]), str(data.get("words", "")))


@dataclass(frozen=True)
class Example:
    """One sealed example, as `task-requirements.json` holds it."""

    id: str
    units: tuple[str, ...]
    setup: tuple[str, ...]
    call: str
    predictions: tuple[Prediction, ...]
    references: tuple[Reference, ...] = ()
    alternatives: tuple[Alternative, ...] = ()
    probes: tuple[Probe, ...] = ()
    args: tuple[str, ...] = ()
    """The input values, as literals, a mini-reference `ref(*args)` takes."""

    @staticmethod
    def from_dict(data: Mapping[str, Any]) -> Example:
        return Example(
            id=str(data["id"]),
            units=tuple(str(u) for u in data["units"]),
            setup=tuple(str(s) for s in data.get("setup", ())),
            call=str(data["call"]),
            predictions=tuple(Prediction.from_dict(p) for p in data["predictions"]),
            references=tuple(Reference.from_dict(r) for r in data.get("references", ())),
            alternatives=tuple(Alternative.from_dict(a) for a in data.get("alternatives", ())),
            probes=tuple(Probe.from_dict(p) for p in data.get("probes", ())),
            args=tuple(str(a) for a in data.get("args", ())),
        )


# -- classification: what an example may do -----------------------------------


@dataclass(frozen=True)
class Class:
    """What `classify` decided for one example, before any tree is run."""

    route: Route
    expected: Outcome | None
    """The outcome a tree is held to; None for a split input."""
    readings: tuple[Outcome, ...]
    """Every other recorded reading (split outcomes and P-c alternatives)."""
    note: str
    """Why the route: what was missing, in words a question can quote."""
    abs_tol: float = ROUTE_B_ABS_TOL
    eligible: bool = False
    """Refusal-eligible, before the licence (`P1_REFUSAL_LICENSED`)."""
    probe_rejected: str = ""
    """A known-correct probe returned a different outcome: the example is suspect."""
    effective_k: int = 0
    disagree_with_text: bool = False


def _squash(text: str) -> str:
    return re.sub(r"\s+", "", text.replace("`", ""))


def _loose(literal: str) -> str:
    """A regex matching `literal` with any whitespace between its characters."""
    return r"\s*".join(re.escape(c) for c in _squash(literal))


def input_literals(setup: Sequence[str], call: str) -> list[str]:
    """The source text of every maximal literal in the example's input."""
    found: list[str] = []
    for source in (*setup, call):
        try:
            tree = ast.parse(source)
        except SyntaxError:
            continue
        found.extend(_literal_segments(tree, source))
    return found


def args_problem(args: Sequence[str], setup: Sequence[str], call: str) -> str | None:
    """Why `args` a predictor chose for its reference are not the input's, or
    None. Every number, string and constant inside them must be written in
    the input: they may regroup the input's values, never add one (such as
    the expected outcome) that the input does not hold."""
    held: dict[tuple[str, object], object] = {}
    for text in input_literals(setup, call):
        try:
            held |= _leaves(parse_value(text))
        except ValueError:
            continue
    for arg in args:
        try:
            leaves = _leaves(parse_value(arg))
        except ValueError:
            return f"{arg} is not a literal"
        extra = sorted(repr(v) for k, v in leaves.items() if k not in held)
        if extra:
            return f"{arg} holds {', '.join(extra)}, which the input does not"
    return None


def _leaves(value: object) -> dict[tuple[str, object], object]:
    """The scalars inside `value`, keyed so that numbers compare by value
    (`1 == 1.0`) and a bool is never a number."""
    if isinstance(value, dict):
        return {k: v for kv in value.items() for x in kv for k, v in _leaves(x).items()}
    if isinstance(value, list | tuple | set | frozenset):
        return {k: v for x in value for k, v in _leaves(x).items()}
    if isinstance(value, bool) or value is None:
        return {("const", value): value}
    if isinstance(value, int | float | Decimal | Fraction):
        try:
            return {("num", Fraction(value)): value}
        except (ValueError, OverflowError):  # nan and infinity compare by spelling
            return {("num", repr(value)): value}
    return {(type(value).__name__, value): value}


def _literal_segments(node: ast.AST, source: str) -> list[str]:
    if isinstance(node, ast.expr) and not isinstance(node, ast.Name):
        try:
            ast.literal_eval(node)
        except (ValueError, TypeError, SyntaxError, MemoryError, RecursionError):
            pass
        else:
            segment = ast.get_source_segment(source, node)
            return [segment] if segment else []
    return [s for child in ast.iter_child_nodes(node) for s in _literal_segments(child, source)]


def literal_route(example: Example, outcome: Outcome, decides: str, units: Sequence[Unit]) -> str:
    """ "" when route (a) holds for `outcome` stated in `decides`; else why not.

    The span must lie inside a cited binding unit; every input literal and
    the outcome's literal must appear in it (whitespace ignored); and no
    `NEGATION` token may stand between them.
    """
    if not decides.strip():
        return "no decides span"
    home = [u for u in units if _squash(decides) in _squash(u.text)]
    if not home:
        return "the decides span is not in a cited unit"
    if not any(u.modality == "binding" for u in home):
        return "the decides span is not in a binding unit"
    wanted = [*input_literals(example.setup, example.call), outcome.text]
    spans = []
    for literal in wanted:
        match = re.search(_loose(literal), decides)
        if match is None:
            return f"{literal} is not written in the text"
        spans.append(match.span())
    start, end = min(s for s, _ in spans), max(e for _, e in spans)
    between = decides[start:end]
    if NEGATION.search(between):
        return "a negation stands between the input and the outcome"
    return ""


def raise_named(cited: Sequence[Unit]) -> bool:
    """A cited unit names a raise, an error or an exception (`RAISE_TOKENS`)."""
    return any(t in u.text.lower() for u in cited for t in RAISE_TOKENS)


def raise_names(example: Example) -> list[str]:
    """Every raise type name the example's predictions and alternative
    readings write, once each, in order: what the driver resolves on a tree."""
    outcomes = [
        *(p.outcome for p in example.predictions),
        *(a.outcome for a in example.alternatives),
    ]
    names = [o.text for o in outcomes if o is not None and o.kind == "raises"]
    return list(dict.fromkeys(names))


RAISE_ROW: Final = "not-judged: raise-named, no raising example"
"""The named row of a binding raise-named unit no decided example raises for."""
RAISE_NOISE: Final = "token matches include non-raise uses of `error`/`exception`"


def raise_obligation(units: Units, examples: Sequence[Example]) -> list[tuple[str, int]]:
    """Every binding unit that names a raise (`raise_named`, P1's own token
    guard), with how many decided examples citing it expect a raise (K2 R-1).

    A count the model cannot lower: zero is a named row (`RAISE_ROW`), never a
    pass by silence and never a refusal; the extractor's gap, not the tree's.
    The guard matches tokens, so a unit that says "error" without describing
    a raise is counted too (`RAISE_NOISE`)."""
    raising: dict[str, int] = {}
    for e in examples:
        expected = classify(e, units).expected
        if expected is not None and expected.kind == "raises":
            for u in e.units:
                raising[u] = raising.get(u, 0) + 1
    return [
        (u.id, raising.get(u.id, 0))
        for u in units.units
        if u.modality == "binding" and raise_named([u])
    ]


def effective_k(predictions: Sequence[Prediction]) -> int:
    """How many distinct samples the predictions are: byte-identical raw outputs count once."""
    return len({p.raw_sha256 for p in predictions})


def classify(example: Example, units: Units) -> Class:
    """What `example` may do, from the sealed record alone (spec §2.2, §2.3, §4.5).

    Route (a) first: a literal the text states wins over the predictors. Else
    the k predictions must agree (a split input only asks); then route (b)
    needs effective k > 1, the k executed references agreeing (one whose args
    were at fault abstains, if `REFERENCES_NEEDED` others ran), and every
    known-correct probe returning the outcome. A unit that is not `binding`,
    or a `raises` outcome no cited unit names, can only ask.
    """
    cited = [u for u in (units.by_id(i) for i in example.units) if u is not None]
    alternatives = [a.outcome for a in example.alternatives]
    k = effective_k(example.predictions)
    written = [p.outcome for p in example.predictions if p.outcome is not None]
    readings = list(dict.fromkeys([*written, *alternatives]))
    if not cited:
        return Class("question-only", None, tuple(readings), "it cites no unit of the task")
    literal: list[Outcome] = []
    for p in example.predictions:
        o = p.outcome
        if o is not None and not literal_route(example, o, p.decides, cited):
            if not any(x.same(o) for x in literal):
                literal.append(o)
    decided = len(written) == K_PREDICTORS == len(example.predictions) and all(
        o.same(written[0]) for o in written
    )
    if len(literal) == 1:
        expected = literal[0]
        base = Class(
            "literal",
            expected,
            tuple(r for r in readings if not r.same(expected)),
            "literal" if decided and written[0].same(expected) else LITERAL_DISAGREES,
            abs_tol=literal_abs_tol(expected.text),
            effective_k=k,
            disagree_with_text=not (decided and written[0].same(expected)),
        )
    elif not decided:
        return Class("split", None, tuple(readings), undecided(example.predictions), effective_k=k)
    else:
        expected = written[0]
        note, verified = _references_note(example, expected, k)
        base = Class(
            "decided-unverified" if note else "executed-reference",
            expected,
            tuple(r for r in readings if not r.same(expected)),
            note or verified,
            effective_k=k,
        )
    modes = {u.modality for u in cited}
    if modes != {"binding"}:
        why = UNCERTAIN_NOTE if "binding-uncertain" in modes else DELEGATED_NOTE
        return _with(base, route="question-only", note=why)
    if expected.kind == "raises" and not raise_named(cited):
        return _with(base, note=NO_RAISE_NOTE)
    if expected.kind == "raises" and expected.text in UNTYPED:
        return _with(base, note=NO_TYPE_NOTE)
    if base.route == "literal":
        return _with(base, eligible=True)
    if base.route == "decided-unverified":
        return base
    return _probed(base, expected, example.probes)


def _references_note(example: Example, expected: Outcome, k: int) -> tuple[str, str]:
    """Why a decided example's executed references do not verify `expected`
    ("" when they do), and the route note when they do.

    Every reference that did not abstain (`Reference.abstains`) must have
    run and agree; at least `REFERENCES_NEEDED` must have run, and those must
    be more than one sample (their predictors' raw outputs differ: reference
    i is predictor i's). A reference that abstained is named in the note, so
    a reader sees the example was decided on fewer than k."""
    refs = example.references
    counted = [(i, r) for i, r in enumerate(refs) if not r.abstains()]
    abstained = [r.status for r in refs if r.abstains()]
    # A reference that never ran said nothing: the note names why rather
    # than reading it as a disagreement.
    unrun = [r.status for _, r in counted if r.status != "ran"]
    unrun += ["not recorded"] * (K_PREDICTORS - len(refs))
    if len(refs) > K_PREDICTORS:
        unrun.append(f"{len(refs)} references recorded where {K_PREDICTORS} are expected")
    if k < 2:
        return f"effective k = {k}: the {K_PREDICTORS} predictions are one sample", ""
    if unrun:
        return f"not every predictor's reference ran ({unrun[0]})", ""
    ran_k = len({example.predictions[i].raw_sha256 for i, _ in counted})
    if not all(r.agrees(expected) for _, r in counted):
        return "the predictors' executed references do not all agree with them", ""
    if len(counted) < REFERENCES_NEEDED:
        return (
            f"only {len(counted)} of {K_PREDICTORS} references ran, "
            f"{REFERENCES_NEEDED} are needed (abstained: {'; '.join(abstained)})",
            "",
        )
    if ran_k < 2:
        return "effective k = 1 among the references that ran: they are one sample", ""
    why = f"; {len(abstained)} abstained: {'; '.join(abstained)}" if abstained else ""
    return "", f"executed-reference ({len(counted)}/{K_PREDICTORS}{why})"


SPLIT_NOTE: Final = "the predictors read the cited words differently"
NO_OUTCOME: Final = "a predictor gave no outcome for this input"
NOTHING_RETURNED: Final = "a prediction call returned nothing"
EMPTY_SHA256: Final = "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855"
"""The sha256 of no text: the `raw_sha256` of a prediction whose call returned nothing."""


def undecided(predictions: Sequence[Prediction]) -> str:
    """Why `predictions` do not decide their input, in words a question can
    quote: what happened. Predictions that are missing say why
    (`Prediction.missing`); a disagreement is named only where written
    outcomes differ, never for a prediction that never came."""
    written = [p.outcome for p in predictions if p.outcome is not None]
    missing = [p for p in predictions if p.outcome is None]
    said = []
    if missing:
        whys = dict.fromkeys(
            p.missing or (NOTHING_RETURNED if p.raw_sha256 == EMPTY_SHA256 else NO_OUTCOME)
            for p in missing
        )
        said.append(f"{len(missing)} of {len(predictions)} predictions missing ({'; '.join(whys)})")
    if len(predictions) != K_PREDICTORS:
        said.append(f"{len(predictions)} predictions recorded where {K_PREDICTORS} are needed")
    if any(not o.same(written[0]) for o in written[1:]):
        said.append(SPLIT_NOTE)
    return "; ".join(said)


LITERAL_DISAGREES: Final = "literal (the predictors disagree with the text; the text wins)"
UNCERTAIN_NOTE: Final = "the rule could not tell who the permission is for"
DELEGATED_NOTE: Final = "the task leaves this to the implementer"
NO_RAISE_NOTE: Final = "the task does not say this raises"
NO_TYPE_NOTE: Final = "no named type"
"""An example expecting `Exception` or `BaseException` (`UNTYPED`)."""


def _with(base: Class, **changes: Any) -> Class:
    return dataclasses.replace(base, **changes)


def _probed(base: Class, expected: Outcome, probes: Sequence[Probe]) -> Class:
    """Route (b) is refusal-eligible only if every known-correct probe agrees (D-9),
    and there are `PROBES_NEEDED` of one source. A probe that disagrees
    rejects the example whatever the count: a known-correct implementation
    says the example, not the tree, is suspect."""
    if not probes:
        return _with(base, note="no known-correct probe")
    for probe in probes:
        if probe.status == "ran" and probe.outcome is not None and not probe.agrees(expected):
            return _with(
                base,
                probe_rejected=f"known-correct probe {probe.sha256[:12]} gives "
                f"{probe.outcome.show()}",
            )
    if any(p.status != "ran" or p.outcome is None for p in probes):
        return _with(base, note="no known-correct probe (a probe could not run on it)")
    short = too_few_probes(probes)
    if short:
        return _with(base, note=f"too few known-correct probes ({short})")
    return _with(base, eligible=True, note=f"{base.note}, probe ({len(probes)}/{len(probes)})")


def too_few_probes(probes: Sequence[Probe]) -> str:
    """ "" when some source has its `PROBES_NEEDED`; else the counts, in words."""
    have = {s: sum(p.source == s for p in probes) for s in PROBE_SOURCES}
    if any(have[s] >= n for s, n in PROBES_NEEDED.items()):
        return ""
    return ", ".join(f"{have[s]} of {n} {s}" for s, n in PROBES_NEEDED.items())


# -- one tree outcome -> one row ------------------------------------------------


@dataclass(frozen=True)
class TreeOutcome:
    """What the driver saw one example do on the tree."""

    kind: Literal["value", "raises", "could-not-call", "hang", "opaque"]
    value: Any = None
    """`value`: the driver's tagged encoding (`decode_value`)."""
    raises: tuple[str, ...] = ()
    """`raises`: the exception's class and its bases, by name, most derived first."""
    detail: str = ""
    ran: tuple[str, ...] = ()
    """`path:line (qualname)` for every tree line the example executed."""
    types: tuple[tuple[str, str], ...] = ()
    """Each raise type name the example writes, resolved on the tree by the
    driver: `match` (the outcome is an instance of it), `differ` (it is not,
    or no exception was raised), or why the name did not resolve."""

    def type_status(self, name: str) -> str:
        """`match`, `differ`, or why `name` did not resolve on the tree."""
        return dict(self.types).get(name, "not resolved by the driver")

    def show(self) -> str:
        if self.kind == "value":
            try:
                return repr(decode_value(self.value))
            except (ValueError, KeyError, TypeError):
                return "an undecodable value"
        if self.kind == "raises":
            return f"raises {self.raises[0] if self.raises else '?'}"
        return f"{self.kind}: {self.detail}"

    def matches(self, outcome: Outcome, abs_tol: float) -> Closeness:
        """`equal`, `near-miss` or `differ`. A raise matches by `isinstance`
        against its resolved type; a name that did not resolve (only a
        recorded reading can reach here with one) falls back to the class
        names of the raised exception, which can only add a question."""
        if outcome.kind == "raises":
            status = self.type_status(outcome.text)
            if status in ("match", "differ"):
                return "equal" if status == "match" else "differ"
            return "equal" if self.kind == "raises" and outcome.text in self.raises else "differ"
        if self.kind != "value":
            return "differ"
        return compare(outcome.value(), decode_value(self.value), abs_tol)

    @staticmethod
    def from_dict(data: Mapping[str, Any]) -> TreeOutcome:
        kind = data["kind"]
        if kind not in ("value", "raises", "could-not-call", "hang", "opaque"):
            msg = f"unknown outcome kind {kind!r}"
            raise ValueError(msg)
        return TreeOutcome(
            kind=kind,
            value=data.get("value"),
            raises=tuple(str(r) for r in data.get("raises", ())),
            detail=str(data.get("detail", "")),
            ran=tuple(str(r) for r in data.get("ran", ())),
            types=tuple((str(k), str(v)) for k, v in dict(data.get("types", {})).items()),
        )


@dataclass(frozen=True)
class Row:
    """One example's verdict on one tree."""

    example: Example
    status: RowStatus
    why: str
    klass: Class
    got: str = ""
    ran_changed: tuple[str, ...] = field(default=())
    by_type: bool = False
    """`code-wrong` because the tree raised another resolvable exception type
    than the one named: the admitted cost of a named type (K2-5), reported on
    its own."""


def judge(example: Example, klass: Class, got: TreeOutcome | None, *, licensed: bool) -> Row:
    """One example's row (spec §3.3). `got` None: the driver returned nothing for it."""
    if got is None:
        return Row(example, "unknown", "the driver returned no result for it", klass)
    shown = got.show()
    if got.kind == "could-not-call":
        return Row(example, "not-proven", f"could not call: {got.detail}", klass, shown)
    if got.kind == "hang":
        return Row(
            example, "unknown", f"HANG: no outcome within {EXAMPLE_TIMEOUT_S:g} s", klass, shown
        )
    if klass.probe_rejected:
        return Row(example, "not-judged", f"probe-rejected: {klass.probe_rejected}", klass, shown)
    # A tree that returned where every reading raises has differed, whatever it
    # returned: only a value reading needs the value itself to compare.
    returned_where_raise = klass.expected is not None and all(
        o.kind == "raises" for o in (klass.expected, *klass.readings)
    )
    if got.kind == "opaque" and not returned_where_raise:
        return Row(
            example,
            "not-proven",
            f"the tree's value cannot be compared ({got.detail})",
            klass,
            shown,
        )
    if got.kind == "value" and not returned_where_raise:
        try:
            decode_value(got.value)
        except (ValueError, KeyError, TypeError) as exc:
            return Row(example, "not-proven", f"the tree's value is undecodable ({exc})", klass)
    if klass.expected is None:
        # A split input: the readings are recorded, and a person decides.
        why = f"{klass.note}: {_readings(klass.readings)}" if klass.readings else klass.note
        return Row(example, "question", why, klass, shown)
    expected = klass.expected
    if expected.kind == "raises" and expected.text in UNTYPED:
        return _untyped(example, klass, got, shown)
    status = got.type_status(expected.text) if expected.kind == "raises" else "match"
    if status not in ("match", "differ"):
        return Row(example, "question", f"named type {expected.text} {status}", klass, shown)
    closeness = got.matches(expected, klass.abs_tol)
    if closeness == "equal":
        return Row(example, "pass", "matches", klass, shown)
    if klass.route == "question-only":
        if klass.note == DELEGATED_NOTE:
            return Row(example, "not-judged", f"recorded: {klass.note}", klass, shown)
        return Row(example, "question", klass.note, klass, shown)
    if any(got.matches(r, klass.abs_tol) == "equal" for r in klass.readings):
        return Row(
            example,
            "question",
            f"the tree follows another recorded reading ({_readings(klass.readings)})",
            klass,
            shown,
        )
    if closeness == "near-miss":
        return Row(example, "question", "float near-miss", klass, shown)
    if not klass.eligible:
        return Row(example, "question", klass.note, klass, shown)
    if not licensed:
        return Row(example, "question", WOULD_REFUSE, klass, shown)
    by_type = expected.kind == "raises" and got.kind == "raises"
    return Row(
        example, "code-wrong", "differs from the expected outcome", klass, shown, by_type=by_type
    )


def _untyped(example: Example, klass: Class, got: TreeOutcome, shown: str) -> Row:
    """An `Exception`/`BaseException` example: never a refusal. A raise passes
    only when it is the very type every known-correct probe raised; anything
    else, a fall-through crash included, is a question."""
    probe_types = {p.outcome.text for p in example.probes if p.outcome is not None}
    if (
        got.kind == "raises"
        and got.raises
        and probe_types == {got.raises[0]}
        and all(p.outcome is not None and p.outcome.kind == "raises" for p in example.probes)
    ):
        return Row(example, "pass", "the type every known-correct probe raised", klass, shown)
    if klass.route == "question-only" and klass.note == DELEGATED_NOTE:
        return Row(example, "not-judged", f"recorded: {klass.note}", klass, shown)
    return Row(example, "question", NO_TYPE_NOTE, klass, shown)


def _readings(readings: Sequence[Outcome]) -> str:
    return " / ".join(r.show() for r in readings) or "none recorded"
