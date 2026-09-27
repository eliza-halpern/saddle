"""A finding the model reads is said once, however many times the audit raised it.

Tier 0 runs per changed file (`AuditFeed._tier`), and `check_ruff`'s format
detail names no file, so two unformatted files render two identical lines:

    - ruff (tier 0): fail, code-wrong: ruff format --check exited 1
    - ruff (tier 0): fail, code-wrong: ruff format --check exited 1

That is the finish refusal benchmark draw EAFS-t5 s2 read (its sealed
finish audit, reproduced below). Identical failing lines now render once,
with the count. Wording only: `passed`, `unresolved` and the refusal read
the findings, which are unchanged (tightened).
"""

from __future__ import annotations

from saddle.auditor import Finding
from saddle.feed import AuditResult, render

FORMAT = Finding(
    "ruff", 0, "fail", "code-wrong", "ruff format --check exited 1", ("saddle.gates.check_ruff",)
)
B904 = Finding(
    "ruff",
    0,
    "fail",
    "code-wrong",
    "introduced 1 finding(s): money.py:75 B904 Within an `except` clause, raise exceptions"
    " with `raise ... from err` or `raise ... from None` to distinguish them from errors in"
    " exception handling",
    ("saddle.gates.check_ruff",),
)
CLEAN = Finding("ruff", 0, "pass", "code-wrong", "1 file(s) clean", ("saddle.gates.check_ruff",))
LINE = "- ruff (tier 0): fail, code-wrong: ruff format --check exited 1"


def test_the_finish_refusal_says_the_format_failure_once() -> None:
    """Known-bad: EAFS-t5 s2's finish refusal said it twice."""
    text = render(AuditResult("finish", "3a2296e0100b" + "0" * 28, (FORMAT, FORMAT, B904)))
    assert text.count("ruff format --check exited 1") == 1, text
    assert f"{LINE} (2 findings)" in text.splitlines()
    assert text.splitlines()[2].startswith("- ruff (tier 0): fail, code-wrong: introduced 1")


def test_distinct_findings_read_as_before() -> None:
    """Known-good: one of each, no count, same lines in the same order."""
    text = render(AuditResult("finish", "t" * 40, (FORMAT, CLEAN, B904)))
    assert text.splitlines()[1:] == [
        LINE,
        f"- ruff (tier 0): fail, code-wrong: {B904.detail}",
        "(1 other check(s) passed or not applicable)",
    ]
