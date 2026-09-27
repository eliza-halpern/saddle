"""The mutation row in English (mutant_text): classifier, taxonomy, renderer.

Every class has a known-good instance that lands in it and a known-bad
instance that does not. The T5 fixtures are real CALIB outcomes
(internal CALIB run records, not public), copied verbatim.
"""

from __future__ import annotations

import json
import re
from collections.abc import Sequence
from pathlib import Path
from typing import Any

import pytest

from saddle.mutant_text import (
    PHRASES,
    classify,
    describe_mutation,
    function_of,
    mutant_sentence,
    parse_show,
    protects,
    render_compact,
    render_text,
)

FIXTURES = Path(__file__).parent / "fixtures" / "mutant_text"


def load(tree: str) -> dict[str, Any]:
    data = json.loads((FIXTURES / f"{tree}.json").read_text())
    return dict(data["outcome"], survivor_detail=data["survivor_detail"])


def show(name: str, status: str, path: str, before: str, after: str) -> dict[str, str]:
    head = f"# {name}: {status}\n--- {path}\n+++ {path}\n@@ -1,1 +1,1 @@\n"
    body = f"{head}-    {before}\n+    {after}\n"
    return {"name": name, "status": status, "show": body}


# -- classify: known-good and known-bad per kind ---------------------------------

KIND_CASES = [
    # (status, function, before, after, expected kind)
    ("no tests", "Account.__eq__", "return a == b", "return a != b", "untested"),
    ("survived", "Account.__eq__", "return a == b", "return a != b", "behaviour"),
    (
        "survived",
        "quantize",
        "exponent = Decimal(1).scaleb(-_CURRENCY_DECIMALS[code])",
        "exponent = Decimal(2).scaleb(-_CURRENCY_DECIMALS[code])",
        "equivalent",
    ),
    (
        "survived",
        "q",
        "cents = int(scaled.quantize(Decimal(1), rounding=ROUND_HALF_UP))",
        "cents = int(scaled.quantize(Decimal(2), rounding=ROUND_HALF_UP))",
        "equivalent",
    ),
    # Bad: the value is returned, not used as an exponent.
    (
        "survived",
        "unit",
        "return Decimal(1).scaleb(-places)",
        "return Decimal(2).scaleb(-places)",
        "behaviour",
    ),
    # Bad: a different number, not the known pattern.
    ("survived", "q", "x = v.quantize(Decimal(1))", "x = v.quantize(Decimal(3))", "behaviour"),
    (
        "survived",
        "t",
        'raise ValueError("amount must be positive")',
        "raise ValueError(None)",
        "text",
    ),
    ("survived", "t", "raise KeyError(sku)", "raise KeyError()", "text"),
    (
        "survived",
        "t",
        'raise ValueError("bad: %r" % (value,)) from exc',
        "raise ValueError(None) from exc",
        "text",
    ),
    (
        "survived",
        "t",
        'raise ValueError("invalid decimal amount: %r" % (value,))',
        'raise ValueError("invalid decimal amount: %r" / (value,))',
        "text",
    ),
    (
        "survived",
        "t",
        'raise TypeError("got %r" % (type(value).__name__,))',
        'raise TypeError("got %r" % (type(None).__name__,))',
        "text",
    ),
    ("survived", "t", '"amount %s is below the fee %s" % (value, fee)', "None", "text"),
    ("survived", "t", '_check_int(percent, "percent")', "_check_int(percent, None)", "text"),
    # Bad: the raise changes exception type, not its message.
    ("survived", "t", 'raise ValueError("x")', 'raise TypeError("x")', "behaviour"),
    # Bad: a string-led line whose format argument changes may be report output.
    (
        "survived",
        "r",
        '"(%(count)d lines)" % dict(summary, currency=currency)',
        '"(%(count)d lines)" % dict(summary, currency=None)',
        "behaviour",
    ),
    # Bad: a check whose checked value, not its field name, is nulled.
    ("survived", "t", '_check_int(percent, "percent")', '_check_int(None, "percent")', "behaviour"),
    ("survived", "t", "if debit > current:", "if debit >= current:", "behaviour"),
]


@pytest.mark.parametrize(("status", "func", "before", "after", "kind"), KIND_CASES)
def test_classify(status: str, func: str, before: str, after: str, kind: str) -> None:
    assert classify(status, func, before, after) == kind


# -- protects: known-good and known-bad per group --------------------------------

GROUP_CASES = [
    ("Account.withdraw", "if debit > current:", "if debit >= current:", "boundary"),
    (
        "_positive",
        "return value.is_finite() and value > 0",
        "return value.is_finite() and value > 1",
        "boundary",
    ),
    ("f", "for i in range(n - 1):", "for i in range(n - 2):", "boundary"),
    # Bad: a +-1 constant outside any comparison is not a boundary.
    ("f", "x = y + 1", "x = y + 2", "other"),
    ("monthly_summary", "credits += converted", "credits = converted", "accumulation"),
    ("f", "total += x", "total -= x", "accumulation"),
    # Bad: plain assignment to a different value is not accumulation.
    ("f", "total = x", "total = None", "other"),
    (
        "Account.__eq__",
        "return self.owner == other.owner",
        "return self.owner != other.owner",
        "equality",
    ),
    ("f", "if x is None:", "if x is not None:", "equality"),
    # Bad: `==` -> `!=` outside __eq__ is a branch, not equality.
    ("_convert", 'if source == "USD":', 'if source != "USD":', "branch"),
    ("f", 'if "balances" in data:', 'if "balances" not in data:', "branch"),
    ("f", "if a and b:", "if a or b:", "branch"),
    ("f", "if flag:", "if not flag:", "branch"),
    ("f", "if not flag:", "if flag:", "branch"),
    # Bad: `<` -> `<=` is a boundary, not a branch.
    ("f", "if a < b:", "if a <= b:", "boundary"),
    (
        "Account.deposit",
        'current = self._balances.get(code, Decimal("0"))',
        "current = self._balances.get(code, None)",
        "default",
    ),
    ("f", "def f(x, rate=1):", "def f(x, rate=2):", "default"),
    # Bad: a `.get` with no default whose key is nulled is an argument, not a default.
    ("f", 'v = d.get("k")', "v = d.get(None)", "argument"),
    ("load_accounts", "if version not in (1, 2):", "if version not in (2, 2):", "version"),
    (
        "load_accounts",
        "if version not in (1, SCHEMA_VERSION):",
        "if version not in (2, SCHEMA_VERSION):",
        "version",
    ),
    ("f", "if SCHEMA_VERSION > version:", "if SCHEMA_VERSION >= version:", "version"),
    # Bad: naming a version is not checking one.
    ("f", "return [g(r, version) for r in rs]", "return [g(r, None) for r in rs]", "argument"),
    ("transfer", "source.withdraw(moved, currency)", "source.withdraw(moved, )", "argument"),
    (
        "f",
        "value = quantize_money(amount, currency)",
        "value = quantize_money(None, currency)",
        "argument",
    ),
    # Bad: nulling a bare name binding is not an argument.
    ("f", "from_record = _from_record_v1", "from_record = None", "other"),
    ("f", "return amount / RATE", "return amount * RATE", "other"),
]


@pytest.mark.parametrize(("func", "before", "after", "group"), GROUP_CASES)
def test_protects(func: str, before: str, after: str, group: str) -> None:
    assert protects(func, before, after) == group


def test_every_group_and_kind_has_a_good_and_a_bad_case() -> None:
    for group in (
        "boundary",
        "accumulation",
        "equality",
        "branch",
        "default",
        "version",
        "argument",
    ):
        assert any(g == group for *_, g in GROUP_CASES)
        assert any(g != group for *_, g in GROUP_CASES)
    for kind in ("untested", "equivalent", "text", "behaviour"):
        assert any(k == kind for *_, k in KIND_CASES)


# -- parsing ---------------------------------------------------------------------


def test_function_of() -> None:
    assert function_of("accounts.xǁAccountǁwithdraw__mutmut_18") == "Account.withdraw"
    assert function_of("pkg.report.x_monthly_summary__mutmut_53") == "monthly_summary"
    assert function_of("fees.x__check_amount__mutmut_8") == "_check_amount"
    assert function_of("not a mutant name") == "not a mutant name"


def test_parse_show_joins_a_multi_line_hunk() -> None:
    body = (
        "# a.x_f__mutmut_2: survived\n--- a.py\n+++ a.py\n@@ -8,6 +8,5 @@\n"
        "     raise ValueError(\n"
        '-        "one of %s, got %r"\n'
        "-        % (codes, currency)\n"
        "+        None\n"
        "     )\n"
    )
    assert parse_show(body) == ("a.py", '"one of %s, got %r" % (codes, currency)', "None")
    assert classify("survived", "f", *parse_show(body)[1:]) == "text"


# -- the known T5 examples, from the real E-t5-s1 record -------------------------


def test_t5_examples_render_from_the_record() -> None:
    summary = describe_mutation(load("E-t5-s1"))
    by_name = {m.name: m for m in summary.mutants}
    debit = by_name["accounts.xǁAccountǁwithdraw__mutmut_18"]
    assert (debit.before, debit.after, debit.protects) == (
        "if debit > current:",
        "if debit >= current:",
        "boundary",
    )
    credit = by_name["report.x_monthly_summary__mutmut_53"]
    assert (credit.before, credit.after, credit.protects) == (
        "credits += converted",
        "credits = converted",
        "accumulation",
    )
    eq = [m for m in summary.mutants if m.function == "Account.__eq__" and "!=" in m.after]
    assert eq
    assert all(m.kind == "untested" and m.protects == "equality" for m in eq)
    text = render_text(summary)
    assert (
        "accounts.py Account.withdraw: `if debit > current:` -> `if debit >= current:` -- "
        f"{PHRASES['boundary']}; survived: no test failed "
        "[accounts.xǁAccountǁwithdraw__mutmut_18]"
    ) in text
    assert f"{PHRASES['equality']}; survived: nothing tests this" in text


@pytest.mark.parametrize(
    ("func", "before", "after", "group"),
    [
        ("_convert", 'if source == "USD":', 'if source != "USD":', "branch"),
        ("load_accounts", "if version not in (1, 2):", "if version not in (2, 2):", "version"),
    ],
)
def test_t5_branch_and_version_examples(func: str, before: str, after: str, group: str) -> None:
    detail = [show(f"store.x_{func}__mutmut_1", "survived", "store.py", before, after)]
    summary = describe_mutation({"killed": 0, "total": 1, "survivor_detail": detail})
    assert summary.gaps[0].protects == group
    assert f"store.py {func}: `{before}` -> `{after}` -- {PHRASES[group]}" in render_text(summary)


# -- gaps: text and equivalent survivors are never counted -----------------------


def test_text_and_equivalent_survivors_are_not_gaps() -> None:
    detail = [
        show(
            "m.x_f__mutmut_1",
            "survived",
            "m.py",
            'raise ValueError("no")',
            "raise ValueError(None)",
        ),
        show(
            "m.x_q__mutmut_2",
            "survived",
            "m.py",
            "x = v.quantize(Decimal(1), rounding=R)",
            "x = v.quantize(Decimal(2), rounding=R)",
        ),
        show("m.x_g__mutmut_3", "survived", "m.py", "if a > b:", "if a >= b:"),
    ]
    summary = describe_mutation({"killed": 5, "total": 8, "survivor_detail": detail})
    assert [m.name for m in summary.gaps] == ["m.x_g__mutmut_3"]
    assert {m.kind for m in summary.not_gaps} == {"text", "equivalent"}
    text = render_text(summary)
    left, _, rest = text.partition("Survived but not gaps")
    assert "m.x_g__mutmut_3" in left
    assert "m.x_f__mutmut_1" not in left
    assert "m.x_f__mutmut_1" in rest
    assert "m.x_q__mutmut_2" in rest


def test_killed_mutants_with_killers() -> None:
    detail = [
        show("m.x_f__mutmut_1", "killed", "m.py", "if a > b:", "if a >= b:"),
        show("m.x_f__mutmut_2", "timeout", "m.py", "total += x", "total = x"),
    ]
    outcome = {"killed": 2, "total": 2, "mutant_detail": detail}
    summary = describe_mutation(outcome, {"m.x_f__mutmut_1": "tests/test_m.py::test_edge"})
    text = render_text(summary)
    assert "caught by tests/test_m.py::test_edge [m.x_f__mutmut_1]" in text
    assert "; caught [m.x_f__mutmut_2]" in text
    assert "no recorded diff" not in text
    assert "Left untested" not in text


def test_undescribed_survivors_are_named_not_classified() -> None:
    summary = describe_mutation({"killed": 1, "total": 2, "survivors": ["m.x_f__mutmut_9"]})
    assert summary.undescribed == ("m.x_f__mutmut_9",)
    assert "survived; no diff recorded [m.x_f__mutmut_9]" in render_text(summary)


def test_other_status_is_spelled() -> None:
    m = describe_mutation(
        {"survivor_detail": [show("m.x_f__mutmut_1", "segfault", "m.py", "x = a + b", "x = a - b")]}
    ).mutants[0]
    assert mutant_sentence(m).endswith("survived (segfault): no test failed [m.x_f__mutmut_1]")


# -- rule 4: no sentence without a mutant or a record field behind it -------------

_TAG = re.compile(r"\[record: ([^\]]+)\]$")


def unbacked_lines(text: str, outcome: dict[str, Any]) -> list[str]:
    """Lines that name no mutant, cite no matching record field, and head nothing."""
    summary = describe_mutation(outcome)
    names = {m.name for m in summary.mutants} | set(summary.undescribed)
    lines = text.splitlines()
    bad = []
    for i, line in enumerate(lines):
        named = re.search(r"\[([^\]\s]+)\]$", line)
        if named and named[1] in names:
            continue
        tag = _TAG.search(line)
        if tag and all(
            int(outcome.get(k, 0)) == int(v)
            for k, v in (pair.split("=") for pair in tag[1].split())
        ):
            continue
        heads = (
            line.endswith(":")
            and i + 1 < len(lines)
            and lines[i + 1].lstrip().startswith(("- ", *(f"{g}:" for g in PHRASES)))
        )
        if heads:
            continue
        bad.append(line)
    return bad


@pytest.mark.parametrize("tree", ["E-t5-s1", "EAF-t5-s3", "E-t8-s1", "U-t8-s1"])
def test_every_rendered_line_is_backed_by_the_record(tree: str) -> None:
    outcome = load(tree)
    text = render_text(describe_mutation(outcome))
    assert text.strip()
    assert unbacked_lines(text, outcome) == []


def test_the_backing_check_can_fail() -> None:
    outcome = load("E-t8-s1")
    text = render_text(describe_mutation(outcome))
    assert unbacked_lines(text + "All mutants were caught.\n", outcome) == [
        "All mutants were caught."
    ]
    forged = text.replace(f"killed={outcome['killed']}", "killed=999", 1)
    assert unbacked_lines(forged, outcome) != []


def test_empty_record_renders_only_counts() -> None:
    assert render_text(describe_mutation({})) == (
        "0 of 0 sampled mutants were caught by the suite [record: killed=0 total=0]\n"
    )


# -- the compact rendering: the recap must not scroll (PACKETHOOK-3) ---------------

BOUNDARY = ("if a > b:", "if a >= b:")
ACCUMULATION = ("total += x", "total = x")
BRANCH = ("if a == b:", "if a != b:")
ARGUMENT = ("f(a, b)", "f(a, )")
TEXT = ('raise ValueError("x")', "raise ValueError(None)")


def synthetic(
    survivors: list[tuple[str, str]], killed: Sequence[tuple[str, str]] = ()
) -> dict[str, Any]:
    """An outcome whose every mutant is described: `survivors` and `killed` are
    (before, after) pairs, one mutant each, in m.py functions f0, f1, ..."""
    detail = []
    for i, (before, after) in enumerate(survivors):
        detail.append(show(f"m.x_f{i}__mutmut_1", "survived", "m.py", before, after))
    for i, (before, after) in enumerate(killed):
        detail.append(show(f"m.x_k{i}__mutmut_1", "killed", "m.py", before, after))
    return {
        "killed": len(killed),
        "total": len(survivors) + len(killed),
        "generated": len(survivors) + len(killed),
        "survivors": [d["name"] for d in detail if d["status"] == "survived"],
        "mutant_detail": detail,
    }


def group_1(pattern: str, text: str) -> str:
    """The first group of `pattern` in `text`, which must match."""
    match = re.search(pattern, text)
    assert match is not None, text
    return match.group(1)


def bullets(text: str) -> list[str]:
    return [line for line in text.splitlines() if line.startswith("  - ")]


def test_compact_caps_survivors_at_five_and_counts_the_rest() -> None:
    text = render_text(describe_mutation(synthetic([BOUNDARY] * 80)), compact=True)
    assert len(bullets(text)) == 5
    assert text.splitlines()[-1] == "  and 75 more in the packet"
    assert text.splitlines()[1] == "Left untested:"
    assert len(text.splitlines()) == 8


def test_compact_lists_three_survivors_with_no_more_line() -> None:
    text = render_text(describe_mutation(synthetic([BOUNDARY] * 3)), compact=True)
    assert len(bullets(text)) == 3
    assert "more in the packet" not in text
    assert text == render_compact(describe_mutation(synthetic([BOUNDARY] * 3)))


def test_compact_orders_survivors_worst_group_first() -> None:
    text = render_text(
        describe_mutation(synthetic([BRANCH, ARGUMENT, BOUNDARY, ACCUMULATION])), compact=True
    )
    names = [group_1(r"\[(m\.x_f\d)__mutmut_1\]", b) for b in bullets(text)]
    # f2 boundary, f3 accumulation, f0 branch, f1 argument
    assert names == ["m.x_f2", "m.x_f3", "m.x_f0", "m.x_f1"]
    assert all(("a wrong edit here" in b) for b in bullets(text))


def test_compact_counts_caught_mutants_on_one_line_never_listing_them() -> None:
    text = render_text(
        describe_mutation(synthetic([BOUNDARY], killed=[BOUNDARY, BRANCH, BOUNDARY])), compact=True
    )
    lines = text.splitlines()
    assert lines[0].startswith("3 of 4 sampled mutants were caught by the suite")
    assert lines[1] == "3 caught: 2 boundary, 1 branch"
    assert "Caught:" not in text
    assert "[m.x_k0__mutmut_1]" not in text
    assert len(bullets(text)) == 1


def test_compact_says_undescribed_caught_mutants_are_counted_not_described() -> None:
    text = render_text(describe_mutation(load("E-t5-s1")), compact=True)
    assert text.splitlines()[1] == "220 caught: no recorded diff, so counted, not described"


def test_compact_omits_text_and_equivalent_survivors_entirely() -> None:
    text = render_text(describe_mutation(synthetic([TEXT, BOUNDARY, TEXT])), compact=True)
    assert len(bullets(text)) == 1
    assert "[m.x_f1__mutmut_1]" in text
    assert "only message or argument text changes" not in text
    assert "Survived but not gaps" not in text
    full = render_text(describe_mutation(synthetic([TEXT, BOUNDARY, TEXT])))
    assert "Survived but not gaps (text or equivalent):" in full


def test_compact_stays_within_its_line_budget_on_every_real_tree() -> None:
    for tree in ("E-t5-s1", "EAF-t5-s3", "E-t8-s1", "U-t8-s1"):
        summary = describe_mutation(load(tree))
        compact = render_text(summary, compact=True)
        assert len(compact.splitlines()) <= 9, tree
        assert len(compact.splitlines()) <= len(render_text(summary).splitlines()), tree
        assert compact.splitlines()[0] == render_text(summary).splitlines()[0], tree


def test_full_rendering_is_the_default_and_unchanged_by_the_flag() -> None:
    summary = describe_mutation(load("E-t5-s1"))
    assert render_text(summary) == render_text(summary, compact=False)
    assert "Left untested:\n  boundary:\n" in render_text(summary)
