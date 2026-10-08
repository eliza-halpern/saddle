"""A JavaScript mutation record is read in Stryker's words, and the text says what it holds (#195).

`jsevidence` seals each scored mutant with StrykerJS's own status -- `Killed`,
`Timeout`, `Survived`, `NoCoverage` -- and `mutant_text` spoke only mutmut's
(`killed`, `timeout`, `survived`, `no tests`). A dogfood run's packet read
"killed 62 of 62" and then listed the same 62 under "Left untested", each ending
"survived (Killed): no test failed".

Known-bad: a killed or timed-out JavaScript mutant listed as left untested, and one
no test reaches told "no test failed". Known-good: they read as caught; a survivor
as left untested, "no test failed"; an unreached one as "nothing tests this"; in the
full text and in the recap. mutmut's spellings are tests/test_mutant_text.py's, and
read as they did.
"""

from __future__ import annotations

from typing import Any

from saddle.jsevidence import _shown
from saddle.mutant_text import describe_mutation, render_text

REL = "src/pkg/page.js"
SOURCE = [
    "function shown(a, b) {",
    "  if (a === b) return 1;",
    "  if (a !== b) return 2;",
    "  if (a < b) return 3;",
    "  return a > b;",
    "}",
]


def _entry(line: int, column: int, end: int, replacement: str, status: str) -> dict[str, str]:
    """One scored mutant as the record seals it: jsevidence's name and diff, Stryker's status."""
    mutant: dict[str, object] = {
        "location": {
            "start": {"line": line, "column": column},
            "end": {"line": line, "column": end},
        },
        "replacement": replacement,
    }
    name = f"{REL}:{line}:{column} EqualityOperator"
    return {"name": name, "status": status, "show": _shown(REL, SOURCE, mutant)}


DETAIL = [
    _entry(2, 7, 14, "a !== b", "Killed"),
    _entry(3, 7, 14, "a === b", "Timeout"),
    _entry(4, 7, 12, "a <= b", "Survived"),
    _entry(5, 10, 15, "a >= b", "NoCoverage"),
]
KILLED, TIMED_OUT, SURVIVED, UNREACHED = (d["name"] for d in DETAIL)
# The outcome as `jsevidence` counts it and the packet reads it back from the seal.
OUTCOME: dict[str, Any] = {
    "killed": 2,
    "total": 4,
    "untested": 1,
    "survivors": [SURVIVED, UNREACHED],
    "mutant_detail": DETAIL,
}


def _line(text: str, name: str) -> str:
    (line,) = [x for x in text.splitlines() if x.endswith(f"[{name}]")]
    return line


def test_killed_and_timed_out_javascript_mutants_read_as_caught() -> None:
    summary = describe_mutation(OUTCOME)
    assert [m.name for m in summary.caught] == [KILLED, TIMED_OUT]
    assert [m.name for m in summary.gaps] == [SURVIVED, UNREACHED]
    text = render_text(summary)
    assert _line(text, KILLED).endswith(f"; caught [{KILLED}]"), text
    assert _line(text, TIMED_OUT).endswith(f"; caught [{TIMED_OUT}]"), text
    assert "survived (" not in text, text


def test_a_surviving_and_an_unreached_javascript_mutant_say_which_they_are() -> None:
    text = render_text(describe_mutation(OUTCOME))
    left = text.partition("Left untested")[2]
    assert _line(left, SURVIVED).endswith(f"; survived: no test failed [{SURVIVED}]"), text
    assert _line(left, UNREACHED).endswith(f"; survived: nothing tests this [{UNREACHED}]"), text


def test_the_recap_counts_the_caught_ones_and_lists_only_the_survivors() -> None:
    recap = render_text(describe_mutation(OUTCOME), compact=True)
    assert "no recorded diff" not in recap, recap
    assert KILLED not in recap, recap  # caught mutants are counted, never listed
    assert TIMED_OUT not in recap, recap
    left = recap.partition("Left untested:")[2]
    assert SURVIVED in left, recap
    assert UNREACHED in left, recap
