"""The mutation row in English, compiled from the sealed record (never written).

The Daily Driver page asks for a summary at the end of a run of what killing
mutants specifically meant during that run. This module is that summary as a
pure function of the record: `describe_mutation` reads a mutation outcome
(the `MutationOutcome` fields as `asdict` spells them, plus a per-mutant
detail list of `{name, status, show}`) and an optional `{mutant name: test
id}` map, and `render_text` turns the result into lines.

Nothing here invents a claim. Every mutant line carries the mutant's own
name; every count line carries a `[record: field=value]` tag naming the
field it was read from; every heading is followed by the mutant lines it
heads. The phrase attached to a mutant is a fixed template per class, not
prose about the code.

What the record holds today (phase2-integ 913f4d2): `MutationOutcome` keeps
survivor *names*, a status tally and survivor lines, but no `mutmut show`
diff and no killing test. The CALIB harness recorded `survivor_detail`
(survivors only) beside the outcome. So a killed mutant can only be counted,
never described, until the record carries a `mutant_detail` for every scored
mutant and a `killers` map; both are proposals, read here when present.

Classes (`kind`), in precedence order:

- ``untested``: mutmut decided `no tests` -- no test runs the function.
- ``equivalent``: a known pattern that cannot change behaviour. Only one is
  known: `Decimal(1)` -> `Decimal(2)` used as a quantize exponent (both have
  exponent 0, so `quantize` and `scaleb` give the same result).
- ``text``: the change sits only in an exception's argument, in a message
  format line, or in a `_check_*` field-name argument.
- ``behaviour``: everything else.

Behaviour and untested mutants are further grouped by what they protect
(`protects`): boundary, accumulation, equality, branch, default, version,
argument, other -- see `protects`.
"""

from __future__ import annotations

import difflib
import re
from dataclasses import dataclass, field
from typing import Any

CAUGHT = ("killed", "timeout")

KINDS = ("behaviour", "text", "equivalent", "untested")

# What a wrong edit at a mutant of each group would do. Fixed templates: the
# renderer never writes anything about a mutant that is not one of these.
PHRASES = {
    "boundary": (
        "a wrong edit here moves a boundary: the value at the limit lands on the wrong side"
    ),
    "accumulation": "a wrong edit here overwrites a running total instead of adding to it",
    "equality": "a wrong edit here makes equal objects compare unequal, or the reverse",
    "branch": "a wrong edit here takes the other branch of this condition",
    "default": "a wrong edit here changes what happens when an input is missing",
    "version": "a wrong edit here accepts or rejects the wrong format version",
    "argument": "a wrong edit here drops or nulls a value this call passes on",
    "other": "a wrong edit here changes behaviour on this line",
    "text": "only message or argument text changes",
    "equivalent": "no behaviour can change (Decimal(1) and Decimal(2) share a quantize exponent)",
}
GROUPS = (
    "boundary",
    "accumulation",
    "equality",
    "branch",
    "default",
    "version",
    "argument",
    "other",
)

_NAME = re.compile(r"^(?P<mod>.+?)\.x(?:ǁ(?P<cls>[^ǁ]+)ǁ(?P<meth>.+)|_(?P<func>.+))__mutmut_\d+$")
_TOKEN = re.compile(r"\w+|\*\*=|//=|[<>=!]=|[+\-*/%]=|\S")
_CMP = {"<", "<=", ">", ">="}
_AUG = {"+=", "-=", "*=", "/=", "//=", "%=", "**="}
_FLIP = {("==", "!="), ("!=", "=="), ("and", "or"), ("or", "and"), ("True", "False")}
_FLIP |= {("False", "True")}
# A comparison against a format or schema version, not any line naming one.
_VERSION_CHECK = re.compile(
    r"\bversion\w*\s*(?:==|!=|<=|>=|<|>|not in|in)\s|(?:==|!=|<=|>=|<|>)\s*\w*version\b",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class MutantLine:
    """One mutant, as the record describes it."""

    name: str
    status: str
    file: str
    function: str
    before: str
    after: str
    kind: str
    protects: str | None
    killer: str | None = None

    @property
    def caught(self) -> bool:
        return self.status in CAUGHT


@dataclass(frozen=True)
class MutationSummary:
    """The mutation row's content: counts from the outcome, lines from the detail."""

    killed: int
    total: int
    text_only: int
    untested: int
    mutants: tuple[MutantLine, ...] = ()
    # Survivor names the outcome lists but no detail describes: counted and
    # named, never classified.
    undescribed: tuple[str, ...] = field(default=())

    @property
    def caught(self) -> tuple[MutantLine, ...]:
        return tuple(m for m in self.mutants if m.caught)

    @property
    def gaps(self) -> tuple[MutantLine, ...]:
        """Surviving behaviour (incl. untested) mutants: the only ones counted as gaps."""
        return tuple(
            m for m in self.mutants if not m.caught and m.kind in ("behaviour", "untested")
        )

    @property
    def not_gaps(self) -> tuple[MutantLine, ...]:
        """Surviving text and equivalent mutants: listed, never counted as gaps."""
        return tuple(m for m in self.mutants if not m.caught and m.kind in ("text", "equivalent"))


def function_of(name: str) -> str:
    """`accounts.xǁAccountǁwithdraw__mutmut_3` -> `Account.withdraw`."""
    m = _NAME.match(name)
    if m is None:
        return name
    if m["cls"]:
        return f"{m['cls']}.{m['meth']}"
    return m["func"]


def parse_show(show: str) -> tuple[str, str, str]:
    """(file, before, after) from a `mutmut show` diff; a multi-line hunk joins with one space."""
    path, removed, added = "", [], []
    for line in show.splitlines():
        if line.startswith("--- "):
            path = line[4:].strip()
        elif line.startswith("+++ "):
            continue
        elif line.startswith("-"):
            removed.append(line[1:].strip())
        elif line.startswith("+"):
            added.append(line[1:].strip())
    return path, " ".join(removed), " ".join(added)


def _changes(before: str, after: str) -> list[tuple[list[str], list[str]]]:
    a, b = _TOKEN.findall(before), _TOKEN.findall(after)
    ops = difflib.SequenceMatcher(a=a, b=b, autojunk=False).get_opcodes()
    return [(a[i1:i2], b[j1:j2]) for tag, i1, i2, j1, j2 in ops if tag != "equal"]


def _is_equivalent(before: str, after: str) -> bool:
    if before.replace("Decimal(1)", "Decimal(2)") != after or before == after:
        return False
    # Only where the value is used as a quantize exponent: an argument to
    # `quantize(`/`rounding=`, or a name that says it is one. A bare
    # `return Decimal(1).scaleb(...)` may be used for its value.
    return bool(re.search(r"quantize\(|rounding=|^exponent\w* = ", before))


def _swap_format(before: str) -> str:
    return before.replace(" % ", " / ", 1)


def _is_text(before: str, after: str) -> bool:
    raised = re.match(r"^raise (\w+)\((.*)\)( from \w+)?$", before)
    if raised:
        # The message argument nulled or dropped, its `%` format broken, or
        # only the values it formats changed. Other edits stay behaviour.
        tail = raised[3] or ""
        nulled = (f"raise {raised[1]}(None){tail}", f"raise {raised[1]}(){tail}")
        cut = before.find(" % ")
        return (
            after in nulled
            or after == _swap_format(before) != before
            or (cut > 0 and after[: cut + 3] == before[: cut + 3])
        )
    if re.match(r"""^[rbfu]?["']""", before):
        # A continuation line starting with a string: nulled or format broken.
        # A changed format argument stays behaviour (fail closed): the same
        # shape formats report output, not only exception messages.
        return after == "None" or after == _swap_format(before) != before
    m = re.match(r"^(_?check_\w+)\((.+?),\s*(\"[^\"]*\"|'[^']*')\)$", before)
    return m is not None and after.startswith(f"{m[1]}({m[2]},") and m[3] not in after


def classify(status: str, function: str, before: str, after: str) -> str:
    """The fixed classifier: one of KINDS."""
    if status == "no tests":
        return "untested"
    if _is_equivalent(before, after):
        return "equivalent"
    if _is_text(before, after):
        return "text"
    return "behaviour"


def protects(function: str, before: str, after: str) -> str:
    """What a behaviour mutant protects, from a small fixed taxonomy."""
    changes = _changes(before, after)
    pairs = [(" ".join(x), " ".join(y)) for x, y in changes]
    if _VERSION_CHECK.search(before):
        return "version"
    method = function.rsplit(".", 1)[-1]
    identity = re.sub(r"\bis not\b", "is", before) == re.sub(r"\bis not\b", "is", after)
    if method in ("__eq__", "__ne__", "__hash__") or (identity and re.search(r"\bis\b", before)):
        return "equality"
    for x, y in pairs:
        if x in _CMP and y in _CMP:
            return "boundary"
        if x.isdigit() and y.isdigit() and abs(int(x) - int(y)) == 1:
            if any(op in _TOKEN.findall(before) for op in _CMP | {"range"}):
                return "boundary"
        if x in _AUG and (y == "=" or y in _AUG):
            return "accumulation"
    for x, y in pairs:
        if (x, y) in _FLIP or (x, y) in (("in", "not in"), ("not in", "in")):
            return "branch"
        if (x == "" and y == "not") or (x == "not" and y == ""):
            return "branch"
    if re.search(r"\.get\([^,()]+,", before) and ".get(" in after:
        return "default"
    if before.startswith("def ") and "=" in before:
        return "default"
    if "(" in before and all(y in ("None", "", ",") or x.endswith(",") for x, y in pairs):
        return "argument"
    return "other"


def describe_mutation(
    outcome: dict[str, Any], killers: dict[str, str] | None = None
) -> MutationSummary:
    """Compile the summary from a recorded outcome and, when recorded, who killed what.

    `outcome` is `asdict(MutationOutcome)` plus a detail list under
    `mutant_detail` (proposed: every scored mutant) or `survivor_detail`
    (what CALIB recorded: survivors only). Each detail is `{name, status,
    show}`.
    """
    killers = killers or {}
    detail = outcome.get("mutant_detail") or outcome.get("survivor_detail") or []
    lines = []
    for entry in detail:
        name, status = entry["name"], entry["status"]
        path, before, after = parse_show(entry.get("show", ""))
        func = function_of(name)
        kind = classify(status, func, before, after)
        group = protects(func, before, after) if kind in ("behaviour", "untested") else None
        lines.append(
            MutantLine(name, status, path, func, before, after, kind, group, killers.get(name))
        )
    described = {m.name for m in lines}
    undescribed = tuple(n for n in outcome.get("survivors", ()) if n not in described)
    return MutationSummary(
        killed=int(outcome.get("killed", 0)),
        total=int(outcome.get("total", 0)),
        text_only=int(outcome.get("text_only", 0)),
        untested=int(outcome.get("untested", 0)),
        mutants=tuple(lines),
        undescribed=undescribed,
    )


def _verdict(m: MutantLine) -> str:
    if m.caught:
        return f"caught by {m.killer}" if m.killer else "caught"
    if m.status == "no tests":
        return "survived: nothing tests this"
    if m.status == "survived":
        return "survived: no test failed"
    return f"survived ({m.status}): no test failed"


def mutant_sentence(m: MutantLine) -> str:
    """`<file> <function>: <before> -> <after> -- <phrase>; <verdict> [<name>]`."""
    phrase = PHRASES[m.protects] if m.protects else PHRASES[m.kind]
    return (
        f"{m.file} {m.function}: `{m.before}` -> `{m.after}` -- {phrase}; {_verdict(m)} [{m.name}]"
    )


def _grouped(title: str, mutants: tuple[MutantLine, ...]) -> list[str]:
    if not mutants:
        return []
    out = [f"{title}:"]
    for group in GROUPS:
        members = [m for m in mutants if m.protects == group]
        if members:
            out.append(f"  {group}:")
            out.extend(f"    - {mutant_sentence(m)}" for m in members)
    return out


# The recap must not scroll (user decision, 2026-09-26): the terminal recap and
# the chat pre-fill get COMPACT; only the web packet's fold gets FULL.
SEVERITY = (
    "boundary",
    "accumulation",
    "equality",
    "version",
    "default",
    "branch",
    "argument",
    "other",
)
"""Survivor order in the compact rendering: what a wrong edit there costs most."""
COMPACT_CAP = 5
"""Survivors listed in the compact rendering before "and N more in the packet"."""


def _headline(s: MutationSummary) -> str:
    return (
        f"{s.killed} of {s.total} sampled mutants were caught by the suite "
        f"[record: killed={s.killed} total={s.total}]"
    )


def _severity(m: MutantLine) -> int:
    return SEVERITY.index(m.protects) if m.protects in SEVERITY else len(SEVERITY)


def render_compact(summary: MutationSummary) -> str:
    """The mutation row in at most `COMPACT_CAP + 4` lines, for the recap.

    The headline; the caught mutants as one line of counts by group, never
    listed; the surviving behaviour and untested mutants, worst group first
    (`SEVERITY`), capped at `COMPACT_CAP` with "and N more in the packet";
    text and equivalent survivors left out entirely. Every line still names
    its record: the same sentences and tags `render_text` writes.
    """
    s = summary
    out = [_headline(s)]
    if s.killed:
        counts: dict[str, int] = {}
        for m in s.caught:
            label = m.protects or m.kind
            counts[label] = counts.get(label, 0) + 1
        if counts:
            order = sorted(
                counts, key=lambda g: SEVERITY.index(g) if g in SEVERITY else len(SEVERITY)
            )
            out.append(f"{s.killed} caught: " + ", ".join(f"{counts[g]} {g}" for g in order))
        else:
            out.append(f"{s.killed} caught: no recorded diff, so counted, not described")
    survivors = [mutant_sentence(m) for m in sorted(s.gaps, key=_severity)]
    survivors += [f"survived; no diff recorded [{n}]" for n in s.undescribed]
    if survivors:
        out.append("Left untested:")
        out.extend(f"  - {line}" for line in survivors[:COMPACT_CAP])
        if len(survivors) > COMPACT_CAP:
            out.append(f"  and {len(survivors) - COMPACT_CAP} more in the packet")
    return "\n".join(out) + "\n"


def render_text(summary: MutationSummary, *, compact: bool = False) -> str:
    """The mutation row as lines of English, each backed by a mutant or a record field.

    `compact` is the recap's rendering (`render_compact`); the default lists
    every mutant, grouped, for the web packet's fold.
    """
    if compact:
        return render_compact(summary)
    s = summary
    out = [_headline(s)]
    if s.text_only:
        out.append(
            f"{s.text_only} message-only mutants were set aside before scoring "
            f"[record: text_only={s.text_only}]"
        )
    if s.untested:
        out.append(
            f"{s.untested} sampled mutants sit in code no test runs [record: untested={s.untested}]"
        )
    undescribed_killed = s.killed - len(s.caught)
    if undescribed_killed > 0:
        out.append(
            f"{undescribed_killed} caught mutants have no recorded diff, so they are counted, "
            f"not described [record: killed={s.killed}]"
        )
    out += _grouped("Caught", s.caught)
    out += _grouped("Left untested", s.gaps)
    if s.not_gaps:
        out.append("Survived but not gaps (text or equivalent):")
        out.extend(f"  - {mutant_sentence(m)}" for m in s.not_gaps)
    if s.undescribed:
        out.append("Survived with no recorded diff:")
        out.extend(f"  - survived; no diff recorded [{n}]" for n in s.undescribed)
    return "\n".join(out) + "\n"
