"""Task-text units: the sentences and list items a task's requirements live in.

A pure function, `task_units(text)`, splits a task text (Markdown-ish
prose) into units. It has no model and no I/O, so the model never decides
which sentences exist: a model that chose them would choose its own scope.
Every prose unit is a candidate; the flags record why, not whether.

Segmentation:

1. Fenced code blocks (```` ``` ```` or ``~~~``) are context, never units.
2. ATX headings (``#`` .. ``######``) are context; each unit records its
   heading path.
3. A list item (``-``, ``*``, ``+``, ``N.`` or ``N)``) is one unit,
   including its wrapped continuation lines.
4. A paragraph splits into sentences at ``.``, ``!`` or ``?`` followed by
   whitespace, never inside a backtick span or brackets, and never after a
   token that already holds a dot (``e.g.``, ``ol.copy().``): such a token
   is an abbreviation or a dotted identifier, not a sentence end.
5. A unit that begins with an enumerator (``N.``, ``N)`` or a token shaped
   ``[A-Z]{2,5}-\\d{1,4}``) records it verbatim as its `label`.

Flags: N1, the unit holds a code span or a call-shaped token; N2, it holds
a requirement keyword (`KEYWORDS`, case-insensitive, outside code spans);
N3, neither. Modality (`modality`): `binding`, `delegating` or
`binding-uncertain`, by the subject rule below.

Layering: pure, like `gates`; it imports nothing of saddle.
"""

from __future__ import annotations

import re
from collections.abc import Iterator
from dataclasses import dataclass
from typing import Final, Literal

Modality = Literal["binding", "delegating", "binding-uncertain"]
Kind = Literal["sentence", "list-item"]

KEYWORDS: Final[tuple[str, ...]] = (
    "must",
    "shall",
    "should",
    "may",
    "required",
    "optional",
    "never",
    "always",
    "only",
    "exactly",
    "raise",
    "raises",
    "return",
    "returns",
)
"""The requirement keywords (N2): the RFC 2119 set, never, always, only,
exactly, raise(s), return(s). "must not" is "must" here. Matched as whole
words, case-insensitively, outside code spans."""

PERMISSIVE: Final = frozenset({"may", "should", "optional"})
"""A unit whose only keywords are these is decided by the subject rule."""

MODALS: Final = ("may", "should")
"""The anchors of the subject rule, before `optional`: in "the optional
constructor iterable may be any iterable" the subject is what precedes
"may", not what precedes the adjective "optional"."""

IMPLEMENTER_TOKENS: Final[tuple[str, ...]] = (
    "you",
    "your",
    "the implementation",
    "an implementation",
    "implementations",
    "the implementer",
)
"""A permission whose subject holds one of these is the implementer's to
use (RFC 2119's MAY): `delegating`, question-only."""

CALLER_TOKENS: Final[tuple[str, ...]] = (
    "argument",
    "parameter",
    "iterable",
    "input",
    "value",
    "values",
    "caller",
    "user",
    "key",
    "item",
    "items",
)
"""A permission whose subject holds one of these, or a parameter name from
a signature in a code span, is given to the caller, so it binds the
implementation."""

CLAUSE_BREAK: Final = re.compile(r"[;:,]")
"""What ends a clause for the subject rule: the words before a keyword are
read back to the nearest of these."""

LABEL: Final = re.compile(r"^(?:(\d+[.)])\s|([A-Z]{2,5}-\d{1,4})\b)")
_FENCE: Final = re.compile(r"^\s*(```|~~~)")
_HEADING: Final = re.compile(r"^\s{0,3}(#{1,6})\s+(.*?)\s*#*\s*$")
_ITEM: Final = re.compile(r"^(\s*)([-*+]|\d+[.)])\s+(.*)$")
_CODE_SPAN: Final = re.compile(r"`+[^`]*`+")
_CALL: Final = re.compile(r"\b[A-Za-z_]\w*\(")
_SIGNATURE: Final = re.compile(r"\b[A-Za-z_]\w*\(([^()]*)\)")
_WORD: Final = re.compile(r"[a-z_][a-z0-9_']*")
_OPEN: Final = {"(": ")", "[": "]", "{": "}"}


@dataclass(frozen=True)
class Unit:
    """One candidate unit of the task text."""

    id: str
    """`S-001`, `S-002`, ... in text order."""
    text: str
    kind: Kind
    line: int
    """1-based line of the task text its paragraph or list item starts on."""
    heading: tuple[str, ...]
    label: str | None
    flags: tuple[str, ...]
    """`N1` and/or `N2`, or `N3` alone."""
    keywords: tuple[str, ...]
    modality: Modality
    subject: str
    """Why the modality: the subject words read, or "no permissive keyword"."""


@dataclass(frozen=True)
class Units:
    """Every unit of a task text, with the parameter names its signatures give."""

    units: tuple[Unit, ...]
    parameters: frozenset[str]

    def census(self) -> str:
        """Every unit with its flags and modality, and the counts: the denominator."""
        lines = [
            f"{len(self.units)} unit(s): "
            + ", ".join(
                f"{sum(m == u.modality for u in self.units)} {m}"
                for m in ("binding", "delegating", "binding-uncertain")
            )
        ]
        for u in self.units:
            label = f" [{u.label}]" if u.label else ""
            lines.append(
                f"{u.id} L{u.line} {'+'.join(u.flags)} {u.modality}{label}: {_short(u.text)}"
            )
        return "\n".join(lines)

    def by_id(self, unit_id: str) -> Unit | None:
        return next((u for u in self.units if u.id == unit_id), None)


def _short(text: str, limit: int = 100) -> str:
    return text if len(text) <= limit else text[: limit - 3] + "..."


def strip_code(text: str) -> str:
    """`text` with every backtick code span blanked."""
    return _CODE_SPAN.sub(" ", text)


def parameters_of(text: str) -> frozenset[str]:
    """Parameter names from every call-shaped signature inside a code span of `text`.

    `OrderedList(iterable=None)` gives `iterable`; `self`, `*`, `/` and the
    stars of `*args` are dropped, as are defaults and annotations.
    """
    names: set[str] = set()
    for span in _CODE_SPAN.findall(text):
        for params in _SIGNATURE.findall(span):
            for raw in params.split(","):
                name = raw.split("=")[0].split(":")[0].strip().lstrip("*")
                if name.isidentifier() and name != "self":
                    names.add(name)
    return frozenset(names)


def keywords_in(text: str) -> tuple[str, ...]:
    """The requirement keywords `text` holds outside code spans, in `KEYWORDS` order."""
    words = set(_WORD.findall(strip_code(text).lower()))
    return tuple(k for k in KEYWORDS if k in words)


def _holds(words: str, token: str) -> bool:
    return re.search(rf"(?<![\w']){re.escape(token)}(?![\w'])", words) is not None


def modality(text: str, parameters: frozenset[str] = frozenset()) -> tuple[Modality, str]:
    """The unit's modality and the subject words it was read from.

    - Any keyword outside `PERMISSIVE`, or no keyword at all: `binding`.
    - Otherwise each clause holding a permissive keyword is read up to its
      first anchor (`MODALS`, else "optional"): an implementer token alone
      makes it `delegating`, a caller token or a parameter name alone makes
      it `binding`, and both or neither make it `binding-uncertain`
      (neither list outranks the other). A unit is `binding` or
      `delegating` only if every such clause is; else `binding-uncertain`.
    """
    found = keywords_in(text)
    if not found:
        return "binding", "no keyword"
    if not set(found) <= PERMISSIVE:
        return "binding", f"binding keyword {next(k for k in found if k not in PERMISSIVE)!r}"
    plain = strip_code_marks(text).lower()
    readings: list[Modality] = []
    subjects: list[str] = []
    start = 0
    for clause_end in [*(m.start() for m in CLAUSE_BREAK.finditer(plain)), len(plain)]:
        clause = plain[start:clause_end]
        start = clause_end + 1
        anchor = _anchor(clause)
        if anchor is None:
            continue
        words = clause[:anchor]
        implementer = any(_holds(words, t) for t in IMPLEMENTER_TOKENS)
        caller = any(_holds(words, t) for t in (*CALLER_TOKENS, *sorted(parameters)))
        readings.append(
            "binding-uncertain"
            if implementer == caller
            else "delegating"
            if implementer
            else "binding"
        )
        subjects.append(words.strip())
    verdict: Modality = readings[0] if len(set(readings)) == 1 else "binding-uncertain"
    return verdict, " | ".join(subjects)


def _anchor(clause: str) -> int | None:
    """Where a clause's permissive keyword starts: the first modal verb, else "optional"."""
    for group in (MODALS, ("optional",)):
        hits = [m.start() for k in group for m in re.finditer(rf"\b{k}\b", clause)]
        if hits:
            return min(hits)
    return None


def strip_code_marks(text: str) -> str:
    """`text` with each code span's backticks and clause breaks removed and
    its words kept: a subject may be a parameter written as `iterable`, and
    the commas of `f(a, b)` end no clause. Keywords are still read with code
    spans blanked (`keywords_in`); here only the words before them."""
    return _CODE_SPAN.sub(lambda m: CLAUSE_BREAK.sub(" ", m.group(0).strip("`")), text)


def flags_of(text: str) -> tuple[str, ...]:
    """N1 (a code span or call-shaped token), N2 (a keyword), or N3 (neither)."""
    flags = []
    if _CODE_SPAN.search(text) or _CALL.search(text):
        flags.append("N1")
    if keywords_in(text):
        flags.append("N2")
    return tuple(flags) or ("N3",)


def label_of(text: str) -> str | None:
    """The enumerator a unit begins with, verbatim; None without one."""
    match = LABEL.match(text)
    if match is None:
        return None
    return match.group(1) or match.group(2)


def split_sentences(text: str) -> list[str]:
    """`text` split at `.`, `!` or `?` followed by whitespace (segmentation rule 4)."""
    out: list[str] = []
    start = 0
    depth: list[str] = []
    tick = False
    i = 0
    while i < len(text):
        c = text[i]
        if c == "`":
            tick = not tick
        elif not tick and c in _OPEN:
            depth.append(_OPEN[c])
        elif not tick and depth and c == depth[-1]:
            depth.pop()
        elif (
            c in ".!?"
            and not tick
            and not depth
            and (i + 1 == len(text) or text[i + 1].isspace())
            and not _dotted(text, start, i)
        ):
            out.append(text[start : i + 1].strip())  # never empty: it holds the terminator
            start = i + 1
        i += 1
    rest = text[start:].strip()
    if rest:
        out.append(rest)
    return out


def _dotted(text: str, start: int, end: int) -> bool:
    """The token ending at `end` (a terminator) already holds a dot."""
    head = text[start:end]
    token = head.split()[-1] if head.split() else ""
    return "." in token.strip("`")


def _blocks(text: str) -> Iterator[tuple[Kind, int, tuple[str, ...], str]]:
    """(kind, first line, heading path, text) for every paragraph and list item."""
    headings: list[tuple[int, str]] = []
    fenced = False
    current: tuple[Kind, int, list[str]] | None = None

    def close() -> Iterator[tuple[Kind, int, tuple[str, ...], str]]:
        nonlocal current
        if current is not None:
            kind, first, lines = current
            yield kind, first, tuple(h for _, h in headings), " ".join(lines)
        current = None

    for number, line in enumerate(text.splitlines(), 1):
        if _FENCE.match(line):
            yield from close()
            fenced = not fenced
            continue
        if fenced:
            continue
        heading = _HEADING.match(line)
        if heading:
            yield from close()
            level = len(heading.group(1))
            headings = [h for h in headings if h[0] < level] + [(level, heading.group(2))]
            continue
        if not line.strip():
            yield from close()
            continue
        item = _ITEM.match(line)
        if item:
            yield from close()
            marker = item.group(2)
            body = item.group(3).strip()
            # An ordered marker is the item's own enumerator: kept in its text.
            current = ("list-item", number, [f"{marker} {body}" if marker[0].isdigit() else body])
            continue
        if current is None:
            current = ("sentence", number, [line.strip()])
        else:
            current[2].append(line.strip())
    yield from close()


def task_units(text: str) -> Units:
    """Every candidate unit of `text`, in order, with flags and modality."""
    parameters = parameters_of(text)
    units: list[Unit] = []
    for kind, line, heading, body in _blocks(text):
        pieces = [body] if kind == "list-item" else split_sentences(body)
        for piece in pieces:
            found = keywords_in(piece)
            mode, subject = modality(piece, parameters)
            units.append(
                Unit(
                    id=f"S-{len(units) + 1:03d}",
                    text=piece,
                    kind=kind,
                    line=line,
                    heading=heading,
                    label=label_of(piece),
                    flags=flags_of(piece),
                    keywords=found,
                    modality=mode,
                    subject=subject,
                )
            )
    return Units(units=tuple(units), parameters=parameters)
