"""The constants a task prompt names, checked against the source (FEEDFIX item 1).

A reporting-only row, never a verdict. SURVIVORRES (out/SURVIVORRES/report.md)
found the T5 rounding bug invisible to every survivor ordering on the broken
tree -- the bug replaces `ROUND_HALF_UP` with `ROUND_HALF_EVEN`, and the
surviving mutant there is equivalent -- but directly visible as a *sibling
constant*: the source names a constant of the same family as one the prompt
names, and never the one the prompt names. It also flags a prompt constant
the source never names at all.

What this reports is **untested behaviour the prompt names**, not a bug:
the source may be right for a reason this module cannot see. It reads
names, never behaviour: a name anywhere in a source file counts as
present (a string, a comment or a docstring included), so a wrong tree
that still mentions the constant is not reported, and a correct tree
that reaches the behaviour without the name (an alias, a library
default) is.

A prompt constant is an upper-case identifier with an underscore anywhere
in the prompt (`ROUND_HALF_UP`), or an upper-case word of two or more
letters inside a backtick code span (`USD`, `FEES`). One the prompt says
is replaced (`supersedes`, `replaces`, `instead of` before it) is not
checked: T5 rule 5's `FEES` "supersedes `FLAT_FEE`", which 10 of 15
correct T5 trees legitimately drop. The family of a constant with an
underscore is its first segment (`ROUND_`); a one-word constant has no
siblings and is only checked for presence.

Fitted after seeing the 13 DETECT16 C1 trees; see the FEEDFIX (1) commit
for what it has and has not been measured on.
"""

from __future__ import annotations

import os
import re
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any, Final

_SPAN: Final = re.compile(r"`([^`]*)`")
_UNDERSCORED: Final = re.compile(r"\b[A-Z][A-Z0-9]*(?:_[A-Z0-9]+)+\b")
_WORD: Final = re.compile(r"\b[A-Z][A-Z0-9]+\b")
_REPLACED: Final = re.compile(
    r"(?:supersedes|replaces|instead of)\s+`?([A-Z][A-Z0-9]*(?:_[A-Z0-9]+)*)`?", re.IGNORECASE
)
_SKIP_DIRS: Final = frozenset({".git", ".saddle", ".venv", "mutants", "__pycache__"})

UNTESTED: Final = "untested behaviour the prompt names"
"""How every item of the row begins: a gap to test, never a claimed bug."""


def named(prompt: str) -> list[str]:
    """Every constant the prompt names, sorted."""
    found = set(_UNDERSCORED.findall(prompt))
    for span in _SPAN.findall(prompt):
        found |= set(_WORD.findall(span))
    return sorted(found)


def superseded(prompt: str) -> list[str]:
    """The constants the prompt itself says are replaced, sorted."""
    return sorted(set(_REPLACED.findall(prompt)))


def _family(constant: str) -> str | None:
    head, sep, _ = constant.partition("_")
    return f"{head}_" if sep else None


def check(prompt: str, sources: Mapping[str, str]) -> dict[str, Any]:
    """The row's record: what the prompt names, what it replaces, and one
    entry per checked constant the source never names, with the family
    siblings the source names instead (empty: missing entirely)."""
    constants = named(prompt)
    replaced = superseded(prompt)
    tokens: set[str] = set()
    for text in sources.values():
        tokens |= set(re.findall(r"\b[A-Za-z_][A-Za-z0-9_]*\b", text))
    rows = []
    for constant in constants:
        if constant in replaced or constant in tokens:
            continue
        family = _family(constant)
        siblings = sorted(
            t for t in tokens if family and t.startswith(family) and t not in constants
        )
        rows.append({"constant": constant, "siblings": siblings})
    return {"named": constants, "superseded": replaced, "rows": rows}


def items(record: Mapping[str, Any]) -> list[str]:
    """One sentence per row, each beginning `UNTESTED`."""
    out = []
    for row in record.get("rows", []):
        constant, siblings = row["constant"], row["siblings"]
        if siblings:
            out.append(
                f"{UNTESTED}: the prompt names {constant}; the source never does and "
                f"names {', '.join(siblings)} of the same family instead."
            )
        else:
            out.append(f"{UNTESTED}: the prompt names {constant}; the source never does.")
    return out


def tree_sources(root: Path, is_test: Callable[[str], bool]) -> dict[str, str]:
    """Every `.py` file under `root` that `is_test` (given its relative
    path) does not claim, relative path to text. `os.walk`, so the skipped
    directories are skipped by name, not silently by a glob."""
    out: dict[str, str] = {}
    for here, dirs, files in os.walk(root):
        dirs[:] = sorted(d for d in dirs if d not in _SKIP_DIRS)
        for name in sorted(files):
            rel = Path(here, name).relative_to(root).as_posix()
            if name.endswith(".py") and not is_test(rel):
                try:
                    out[rel] = Path(here, name).read_text()
                except (OSError, UnicodeDecodeError):
                    continue
    return out
