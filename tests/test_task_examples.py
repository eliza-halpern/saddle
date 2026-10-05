"""The pure half of P1: classification, one example's verdict, the gate's verdict.

Fixtures are synthetic task texts modelled on non-held-out shapes (a sorted
container, a wrapper, a numeric helper). Unless a test says otherwise, the
stub predictions agree, their executed references agree, three known-correct
probes accept the example, and refusal is licensed for the test (spec §5.1).
"""

from __future__ import annotations

import sys
from decimal import Decimal
from fractions import Fraction
from typing import Any, Final

import pytest

from saddle import gates
from saddle.gates import check_task_requirements
from saddle.task_examples import (
    DELEGATED_NOTE,
    LITERAL_DISAGREES,
    NO_RAISE_NOTE,
    UNCERTAIN_NOTE,
    Alternative,
    Example,
    Outcome,
    Prediction,
    Probe,
    Reference,
    TreeOutcome,
    classify,
    compare,
    decode_value,
    effective_k,
    encode_value,
    input_literals,
    literal_abs_tol,
    literal_route,
    literal_text,
    parse_value,
    reference_problem,
    snippet_problem,
)
from saddle.task_units import Units, task_units

TEXT = """# Spec

`OrderedList(iterable=None)` holds values in ascending sorted order.
Duplicates are allowed and preserved.

- `wrap(x)`: a non-list is wrapped as `[x]`.
- `half(2)` returns `1.0`.
- `f(2)` must not return `4`.
- `iter_slice(start, stop)`: iterator over positions `[start, stop)`.
- `make(size)` raises `ValueError` on a negative size.
- `grow(size)` returns a list of that size.
- The result may be cached.
- Your constructor's iterable argument may be any iterable.
- you may split into a package as long as this import works
"""
UNITS = task_units(TEXT)
STDLIB = frozenset(sys.stdlib_module_names)


def test_the_fixture_units_are_the_ones_the_tests_cite() -> None:
    got = {u.id: (u.modality, u.text[:20]) for u in UNITS.units}
    assert got["S-002"] == ("binding", "Duplicates are allow")
    assert got["S-005"] == ("binding", "`f(2)` must not retu")
    assert got["S-009"][0] == "binding-uncertain"
    assert got["S-010"][0] == "binding-uncertain"
    assert got["S-011"][0] == "delegating"
    assert len(got) == 11


def out(kind: str, text: str) -> Outcome:
    return Outcome.of(kind, text)


def ex(
    call: str,
    expected: Outcome,
    *,
    units: tuple[str, ...] = ("S-002",),
    setup: tuple[str, ...] = (),
    decides: str = "",
    raws: tuple[str, str, str] = ("h0", "h1", "h2"),
    predicted: tuple[Outcome | None, ...] | None = None,
    refs: tuple[Reference, ...] | None = None,
    probes: tuple[Probe, ...] | None = None,
    alts: tuple[Outcome, ...] = (),
) -> Example:
    outcomes = predicted if predicted is not None else (expected, expected, expected)
    return Example(
        id="E-001",
        units=units,
        setup=setup,
        call=call,
        predictions=tuple(
            Prediction(o, decides, raw) for o, raw in zip(outcomes, raws, strict=False)
        ),
        references=tuple(Reference("ran", expected) for _ in range(3)) if refs is None else refs,
        alternatives=tuple(Alternative(a, "words") for a in alts),
        probes=tuple(Probe(f"{i}" * 64, "ran", expected) for i in range(3))
        if probes is None
        else probes,
    )


def val(v: object, *ran: str) -> TreeOutcome:
    return TreeOutcome("value", encode_value(v), ran=ran or ("m.py:3 (f)",))


def gate(
    example: Example,
    got: TreeOutcome | None,
    *,
    licensed: bool | None = True,
    units: Units = UNITS,
    **kw: Any,
) -> gates.TaskRequirementsCheck:
    results = {example.id: got} if got is not None else {}
    return check_task_requirements(results, units, [example], licensed=licensed, **kw)


DEDUP = ex(
    "list(OrderedList([1, 1]))", out("value", "[1, 1]"), setup=("from m import OrderedList",)
)


# -- R1 / R1-g / R14: route (b) with executed references and probes -----------


def test_r1_route_b_refuses_naming_unit_example_route_and_changed_lines() -> None:
    check = gate(DEDUP, val([1], "m.py:3 (OrderedList.__init__)", "m.py:9 (x)"), changed={"m.py:3"})
    assert (check.verdict, check.passed) == ("fail", False)
    assert check.detail == (
        'S-002 "Duplicates are allowed and preserved." via executed-reference (3/3), '
        "probe (3/3): from m import OrderedList; list(OrderedList([1, 1])) expected [1, 1], "
        "got [1]; ran changed lines m.py:3 (OrderedList.__init__)"
    )
    assert check.basis is not None
    assert check.basis.startswith("full strength; 1 of 11 candidate unit(s) judged")


def test_r1_g_the_known_good_twin_passes() -> None:
    check = gate(DEDUP, val([1, 1]))
    assert (check.verdict, check.passed, check.detail) == (
        "pass",
        True,
        "1 example(s) matched the task text",
    )


def test_r14_unlicensed_a_would_be_refusal_is_a_question() -> None:
    check = gate(DEDUP, val([1]), licensed=False)
    assert (check.verdict, check.passed) == ("question", True)
    assert check.detail.endswith("got [1] [would refuse at full strength]")
    assert check.basis is not None
    assert check.basis.startswith("question strength: dev-probe floor unmet")


def test_r14_the_default_reads_the_constant_at_call_time(monkeypatch: pytest.MonkeyPatch) -> None:
    assert gates.P1_REFUSAL_LICENSED is False
    assert gate(DEDUP, val([1]), licensed=None).verdict == "question"
    monkeypatch.setattr(gates, "P1_REFUSAL_LICENSED", True)
    assert gate(DEDUP, val([1]), licensed=None).verdict == "fail"


def test_a_label_and_a_long_unit_are_shown_and_extra_units_named() -> None:
    text = "- ABC-12: " + "word " * 30 + "end.\n- other.\n"
    units = task_units(text)
    e = ex("g()", out("value", "1"), units=("S-001", "S-002"))
    check = gate(e, val(2), units=units)
    assert check.detail.startswith('S-001 [ABC-12] "ABC-12: word word')
    assert '..." (+S-002) via executed-reference' in check.detail
    e = ex("g()", out("value", "1"), units=("S-404",))
    assert gate(e, val(2), units=units).detail.startswith("S-404 via it cites no unit")


# -- R2 / R2-lit / R2-lit-neg: route (a) --------------------------------------


WRAP = ex("wrap(None)", out("value", "[None]"), units=("S-003",))


def test_r2_the_wrapper_shape_refuses_naming_the_unit() -> None:
    check = gate(WRAP, val([]))
    assert check.verdict == "fail"
    assert check.detail.startswith('S-003 "`wrap(x)`: a non-list is wrapped as `[x]`."')


def test_r2_lit_a_literal_in_the_text_refuses_without_references_or_probes() -> None:
    e = ex(
        "half(2)",
        out("value", "1.0"),
        units=("S-004",),
        decides="`half(2)` returns `1.0`",
        refs=(),
        probes=(),
    )
    assert classify(e, UNITS).route == "literal"
    check = gate(e, val(0.5))
    assert check.verdict == "fail"
    assert "via literal: half(2) expected 1.0, got 0.5" in check.detail


def test_r2_lit_the_literal_wins_over_disagreeing_predictors() -> None:
    lit = out("value", "1.0")
    e = ex(
        "half(2)",
        lit,
        units=("S-004",),
        decides="`half(2)` returns `1.0`",
        predicted=(lit, out("value", "2.0"), out("value", "2.0")),
        refs=(),
        probes=(),
    )
    klass = classify(e, UNITS)
    assert (klass.route, klass.expected, klass.note) == ("literal", lit, LITERAL_DISAGREES)
    assert klass.disagree_with_text
    # the predictors' reading is recorded: a tree that follows it is asked, not refused
    assert gate(e, val(2.0)).verdict == "question"


def test_r2_lit_neg_a_negated_literal_is_not_route_a() -> None:
    e = ex(
        "f(2)",
        out("value", "4"),
        units=("S-005",),
        decides="`f(2)` must not return `4`",
        refs=(),
        probes=(),
    )
    klass = classify(e, UNITS)
    assert klass.route == "decided-unverified"
    check = gate(e, val(5))
    assert check.verdict == "question"


@pytest.mark.parametrize(
    ("decides", "why"),
    [
        ("", "no decides span"),
        ("not in the text at all", "the decides span is not in a cited unit"),
        ("`half(2)` returns `1.0`", ""),
        ("`half(2)` returns", "1.0 is not written in the text"),
    ],
)
def test_literal_route_instances(decides: str, why: str) -> None:
    e = ex("half(2)", out("value", "1.0"), units=("S-004",))
    cited = [u for u in UNITS.units if u.id == "S-004"]
    assert literal_route(e, out("value", "1.0"), decides, cited) == why


def test_literal_route_needs_a_binding_home_and_every_input_literal() -> None:
    delegating = [u for u in UNITS.units if u.id == "S-011"]
    e = ex("g()", out("value", "1"))
    assert literal_route(e, out("value", "1"), "you may split", delegating) == (
        "the decides span is not in a binding unit"
    )
    units = task_units("- `g([1, 2], 'a')` gives `3`, while `g(x)` does not give `3`.\n")
    e = ex("g([1,2], 'a')", out("value", "3"))
    home = list(units.units)
    assert literal_route(e, out("value", "3"), "`g([1, 2], 'a')` gives `3`", home) == ""
    assert literal_route(e, out("value", "3"), "`g(x)` does not give `3`", home) == (
        "[1,2] is not written in the text"
    )
    assert input_literals(("x = [1, 2]", "y = f(z)", "(("), "g('a', -1)") == [
        "[1, 2]",
        "'a'",
        "-1",
    ]


@pytest.mark.parametrize(
    "between",
    ["must not", "never", "no longer", "instead of", "rather than", "unlike", "cannot", "doesn't"],
)
def test_every_negation_token_blocks_route_a(between: str) -> None:
    units = task_units(f"- `f(2)` {between} return `4`.\n")
    e = ex("f(2)", out("value", "4"), units=("S-001",))
    assert literal_route(e, out("value", "4"), units.units[0].text, list(units.units)) == (
        "a negation stands between the input and the outcome"
    )
    ok = task_units("- `f(2)` returns `4`, not `5`.\n")
    assert literal_route(e, out("value", "4"), ok.units[0].text, list(ok.units)) == ""


# -- R3 / R3-x / M3: other readings and splits --------------------------------

SLICE = "list(OrderedList([10, 20, 30]).iter_slice(0, 2))"


def test_r3_a_tree_following_a_recorded_alternative_is_asked() -> None:
    e = ex(SLICE, out("value", "[10, 20]"), units=("S-006",), alts=(out("value", "[0, 1]"),))
    check = gate(e, val([0, 1]))
    assert check.verdict == "question"
    assert "the tree follows another recorded reading ([0, 1])" in check.detail


def test_r3_x_without_the_alternative_the_same_tree_is_refused() -> None:
    e = ex(SLICE, out("value", "[10, 20]"), units=("S-006",))
    assert gate(e, val([0, 1])).verdict == "fail"


def test_m3_a_two_to_one_split_asks_whatever_the_tree_does() -> None:
    a, b = out("value", "[10, 20]"), out("value", "[0, 1]")
    e = ex(SLICE, a, units=("S-006",), predicted=(a, a, b))
    klass = classify(e, UNITS)
    assert (klass.route, klass.expected) == ("split", None)
    check = gate(e, val([30]))
    assert check.verdict == "question"
    assert "the predictors read the cited words differently: [10, 20] / [0, 1]" in check.detail
    assert "expected (split)" in check.detail


def test_an_unparsed_prediction_or_a_missing_one_is_a_split() -> None:
    a = out("value", "[1, 1]")
    assert classify(ex("g()", a, predicted=(a, a, None)), UNITS).route == "split"
    assert classify(ex("g()", a, predicted=(a, a), raws=("h0", "h1", "h2")), UNITS).route == (
        "split"
    )


# -- R4 / R5: could not call, HANG -------------------------------------------


def test_r4_an_interface_failure_is_not_proven_never_a_refusal() -> None:
    got = TreeOutcome("could-not-call", detail="AttributeError: no attribute 'iterslice'")
    check = gate(DEDUP, got)
    assert (check.verdict, check.passed) == ("question", True)  # the judged set is empty
    assert "1 could not call or compare" in check.detail
    both = check_task_requirements(
        {"E-001": got, "E-002": val([1, 1])},
        UNITS,
        [DEDUP, Example(**{**DEDUP.__dict__, "id": "E-002"})],
        licensed=True,
    )
    assert (both.verdict, both.passed) == ("not-proven", True)
    assert "could not call: AttributeError" in both.detail
    assert both.basis is not None
    assert "not-proven E-001 (S-002): could not call: AttributeError" in both.basis


def test_r5_a_hang_is_unknown_not_fail() -> None:
    both = check_task_requirements(
        {"E-001": TreeOutcome("hang"), "E-002": val([1, 1])},
        UNITS,
        [DEDUP, Example(**{**DEDUP.__dict__, "id": "E-002"})],
        licensed=True,
    )
    assert both.verdict == "not-proven"
    assert both.rows[0].status == "unknown"
    assert both.rows[0].why == "HANG: no outcome within 5 s"


def test_a_missing_result_an_opaque_or_undecodable_value_never_refuses() -> None:
    assert gate(DEDUP, None).rows[0].status == "unknown"
    opaque = gate(DEDUP, TreeOutcome("opaque", detail="OrderedList")).rows[0]
    assert (opaque.status, opaque.got) == ("not-proven", "opaque: OrderedList")
    bad = gate(DEDUP, TreeOutcome("value", {"t": "nope"})).rows[0]
    assert bad.status == "not-proven"
    assert TreeOutcome("value", {"t": "nope"}).show() == "an undecodable value"


# -- R8: effective k -----------------------------------------------------------


def test_r8_identical_samples_are_one_sample_and_cannot_refuse() -> None:
    same = ex(**_dedup_kwargs(), raws=("h", "h", "h"))
    distinct = DEDUP
    assert (effective_k(same.predictions), effective_k(distinct.predictions)) == (1, 3)
    klass = classify(same, UNITS)
    assert klass.route == "decided-unverified"
    assert klass.note == "effective k = 1: the 3 predictions are one sample"
    assert gate(same, val([1])).verdict == "question"
    assert classify(distinct, UNITS).effective_k == 3


def _dedup_kwargs() -> dict[str, Any]:
    return {
        "call": DEDUP.call,
        "expected": out("value", "[1, 1]"),
        "setup": DEDUP.setup,
    }


# -- R12 / R12-t / R12-p: executed references ----------------------------------


DISAGREE: Final = "the predictors' executed references do not all agree with them"


@pytest.mark.parametrize(
    ("bad", "note"),
    [
        # R12: disagrees with its own prediction
        (Reference("ran", out("value", "[1]")), DISAGREE),
        # R12-p: accepts everything
        (Reference("ran", form="ok", accepts=False), DISAGREE),
        # R12-t, and the other references that never ran: said nothing, so no disagreement
        (Reference("timeout"), "not every predictor's reference ran (timeout)"),
        (
            Reference("refused: import of os is not allowed"),
            "not every predictor's reference ran (refused: import of os is not allowed)",
        ),
        (
            Reference("could not call: missing a required argument: 'qty'"),
            "not every predictor's reference ran "
            "(could not call: missing a required argument: 'qty')",
        ),
    ],
)
def test_r12_a_reference_that_does_not_agree_leaves_a_question(bad: Reference, note: str) -> None:
    good = Reference("ran", out("value", "[1, 1]"))
    e = ex(**_dedup_kwargs(), refs=(good, good, bad))
    assert classify(e, UNITS).route == "decided-unverified"
    assert classify(e, UNITS).note == note
    check = gate(e, val([1]))
    assert check.verdict == "question"
    assert f"[{note}]" in check.detail


def test_a_discriminating_ok_predicate_counts_as_agreement() -> None:
    ok = Reference("ran", form="ok", accepts=True)
    e = ex(**_dedup_kwargs(), refs=(ok, ok, ok))
    assert classify(e, UNITS).route == "executed-reference"
    short = classify(ex(**_dedup_kwargs(), refs=(ok, ok)), UNITS)
    assert (short.route, short.note) == (
        "decided-unverified",
        "not every predictor's reference ran (not recorded)",
    )


# -- R13: floats, each row naming its route -----------------------------------


@pytest.mark.parametrize(
    ("written", "got", "verdict"),
    [
        ("0.35", 0.35000000000000003, "pass"),
        ("0.35", 0.36, "fail"),
        ("1.0", 1.0000001, "pass"),
    ],
)
def test_r13_route_a_floats(written: str, got: float, verdict: str) -> None:
    units = task_units(f"- `g()` returns `{written}`.\n")
    e = ex(
        "g()",
        out("value", written),
        units=("S-001",),
        decides=units.units[0].text,
        refs=(),
        probes=(),
    )
    assert classify(e, units).route == "literal"
    assert gate(e, val(got), units=units).verdict == verdict


@pytest.mark.parametrize(
    ("written", "got", "verdict"),
    [
        ("0.0", 1e-17, "pass"),
        ("1e-13", 0.0, "pass"),  # the small-magnitude cost, pinned
        ("1.0", 1.0000001, "question"),  # float near-miss
        ('float("nan")', float("nan"), "pass"),
        ("1.0", float("nan"), "fail"),
        ('float("inf")', float("inf"), "pass"),
        ('float("inf")', float("-inf"), "fail"),
        ("[1.0, 2.0]", [1.0, 2.0000001], "question"),
    ],
)
def test_r13_route_b_floats(written: str, got: object, verdict: str) -> None:
    e = ex("g()", out("value", written))
    assert classify(e, UNITS).route == "executed-reference"
    check = gate(e, val(got))
    assert check.verdict == verdict
    if verdict == "question":
        assert check.detail.endswith("[float near-miss]")


# -- R16: raises ---------------------------------------------------------------


def test_r16_i_a_unit_that_names_the_raise_may_refuse() -> None:
    e = ex("make(-1)", out("raises", "ValueError"), units=("S-007",))
    assert gate(e, val([])).verdict == "fail"
    # a subclass of the expected exception matches
    sub = TreeOutcome("raises", raises=("SizeError", "ValueError", "Exception"))
    assert gate(e, sub).verdict == "pass"
    assert sub.show() == "raises SizeError"


def test_r16_ii_a_raise_the_task_never_names_is_a_question() -> None:
    e = ex("grow(-1)", out("raises", "ValueError"), units=("S-008",))
    check = gate(e, val([]))
    assert check.verdict == "question"
    assert check.detail.endswith(f"[{NO_RAISE_NOTE}]")
    assert gate(e, TreeOutcome("raises", raises=("ValueError",))).verdict == "pass"


def test_a_value_expected_where_the_tree_raises_differs() -> None:
    assert gate(DEDUP, TreeOutcome("raises", raises=("TypeError",))).verdict == "fail"


# -- R17 (the gate's half; step 2 builds the probes) --------------------------


def test_r17_i_no_probe_is_a_question() -> None:
    check = gate(ex(**_dedup_kwargs(), probes=()), val([1]))
    assert check.verdict == "question"
    assert check.detail.endswith("[no known-correct probe]")


def test_r17_ii_a_disagreeing_probe_rejects_the_example_whatever_the_tree_does() -> None:
    good = out("value", "[1, 1]")
    probes = (
        Probe("a" * 64, "ran", good),
        Probe("b" * 64, "ran", out("value", "[1]")),
        Probe("c" * 64, "ran", good),
    )
    e = ex(**_dedup_kwargs(), probes=probes)
    for tree in ([1], [1, 1]):
        check = gate(e, val(tree))
        assert check.rows[0].status == "not-judged"
        assert check.basis is not None
        assert (
            "not-judged E-001 (S-002): probe-rejected: known-correct probe bbbbbbbbbbbb "
            "gives [1]" in check.basis
        )
        assert check.verdict == "question"  # nothing else was judged


def test_r17_iii_a_probe_that_could_not_run_leaves_a_question() -> None:
    probes = (
        Probe("a" * 64, "ran", out("value", "[1, 1]")),
        Probe("b" * 64, "could not call: AttributeError"),
    )
    check = gate(ex(**_dedup_kwargs(), probes=probes), val([1]))
    assert check.detail.endswith("[no known-correct probe (a probe could not run on it)]")


# -- modality: question-only units ---------------------------------------------


def test_r7_u_and_r7_both_uncertain_units_ask_and_say_why() -> None:
    for unit in ("S-009", "S-010"):
        e = ex("g()", out("value", "1"), units=(unit,))
        check = gate(e, val(2))
        assert check.verdict == "question"
        assert check.detail.endswith(f"[{UNCERTAIN_NOTE}]")
        assert gate(e, val(1)).verdict == "pass"


def test_a_delegated_unit_is_recorded_and_not_judged() -> None:
    e = ex("g()", out("value", "1"), units=("S-011",))
    row = gate(e, val(2)).rows[0]
    assert (row.status, row.why) == ("not-judged", f"recorded: {DELEGATED_NOTE}")


# -- R9-c (the gate's half) / R10 -----------------------------------------------


def test_r9_c_a_gate_that_cannot_run_is_a_question_never_a_pass() -> None:
    check = check_task_requirements({}, UNITS, [], cannot_run="the driver crashed", licensed=True)
    assert (check.verdict, check.passed, check.detail) == (
        "question",
        True,
        "P1 could not run: the driver crashed",
    )
    empty = check_task_requirements({}, UNITS, [], licensed=True)
    assert empty.verdict == "question"
    assert empty.detail.startswith("P1 could not run: no example could be judged (0 example(s)")


def test_r10_not_executable_units_and_cut_units_are_named_in_the_basis() -> None:
    check = gate(
        DEDUP,
        val([1, 1]),
        not_executable=[("S-001", "packaging-or-install")],
        cut=["S-006"],
    )
    assert check.basis is not None
    assert "not executable S-001: packaging-or-install" in check.basis
    assert "cut by the example cap: S-006" in check.basis


# -- the whitelists (R6, R6-ref) ----------------------------------------------


@pytest.mark.parametrize(
    ("setup", "call"),
    [
        (
            ("from orderedlist import OrderedList", "ol = OrderedList([2, 1])", "ol *= 2"),
            "list(ol)",
        ),
        ((), "xs[1:3]"),
        ((), "ol.index(1, start=0)"),
        (("from decimal import Decimal",), 'Decimal("0.1") + 1 if a else -b'),
        ((), "{1: 2} == {1, 2} and not (a < b)"),
    ],
)
def test_r6_good_snippets_are_admitted(setup: tuple[str, ...], call: str) -> None:
    assert snippet_problem(setup, call, STDLIB) is None


@pytest.mark.parametrize(
    ("setup", "call", "why"),
    [
        (("import os",), "1", "Import is not allowed"),
        ((), "open('x')", "name 'open' is not allowed"),
        ((), "__import__('os')", "name '__import__' is not allowed"),
        ((), "().__class__.__subclasses__()", "attribute '__subclasses__' is not allowed"),
        ((), "lambda: 1", "Lambda is not allowed"),
        ((), "[x for x in y]", "ListComp is not allowed"),
        (("from os import system",), "system('x')", "import from os is not allowed"),
        (("from . import x",), "x", "import from . is not allowed"),
        (("from m import *",), "x", "import of * is not allowed"),
        (("x.a = 1",), "x", "assignment to anything but a plain name is not allowed"),
        (("del x",), "1", "Delete is not allowed"),
        ((), "f(", "does not parse: '(' was never closed"),
    ],
)
def test_r6_bad_snippets_are_refused_before_any_run(
    setup: tuple[str, ...], call: str, why: str
) -> None:
    assert snippet_problem(setup, call, STDLIB) == why


@pytest.mark.parametrize(
    "source",
    [
        "def ref(xs):\n    return sorted(xs) * 2\n",
        "import bisect\n\ndef ref(xs, x):\n    return bisect.bisect_left(xs, x)\n",
        "def ref(xs):\n    return [x for x in xs if x]\n",
        "from math import isclose\n\ndef ok(inp, out):\n    return isclose(out, 1.0)\n",
        "def ref(n):\n    if n < 0:\n        raise ValueError(n)\n    return n\n",
    ],
)
def test_r6_ref_good_references_are_admitted(source: str) -> None:
    assert reference_problem(source, STDLIB) is None


@pytest.mark.parametrize(
    ("source", "why"),
    [
        ("import os\n\ndef ref():\n    return 1\n", "import of os is not allowed"),
        ("def ref():\n    return open('x')\n", "name 'open' is not allowed"),
        ("from mypkg import x\n\ndef ref():\n    return x\n", "import from mypkg is not allowed"),
        ("def ref(x):\n    return x.__class__\n", "attribute '__class__' is not allowed"),
        ("def ref(x):\n    return eval(x)\n", "name 'eval' is not allowed"),
        ("def ref():\n    global y\n    return 1\n", "Global is not allowed"),
        (
            "def ref():\n    return 1\n\ndef ok():\n    return 1\n",
            "must be imports and exactly one top-level def named ref or ok",
        ),
        (
            "x = 1\n\ndef ref():\n    return x\n",
            "must be imports and exactly one top-level def named ref or ok",
        ),
        (
            "def other():\n    return 1\n",
            "must be imports and exactly one top-level def named ref or ok",
        ),
        ("@cache\ndef ref():\n    return 1\n", "decorators are not allowed"),
        ("def ref(:\n", "does not parse: invalid syntax"),
    ],
)
def test_r6_ref_bad_references_are_refused_before_any_run(source: str, why: str) -> None:
    assert reference_problem(source, STDLIB) == why


# -- values and comparison -----------------------------------------------------


@pytest.mark.parametrize(
    ("text", "value"),
    [
        ("[1, (2, 'a')]", [1, (2, "a")]),
        ("-3", -3),
        ("+2.5", 2.5),
        ("{1, 2}", {1, 2}),
        ("{'a': [1]}", {"a": [1]}),
        ('Decimal("0.1")', Decimal("0.1")),
        ('Fraction("1/3")', Fraction(1, 3)),
        ("frozenset([1])", frozenset([1])),
        ("set()", set()),
        ("None", None),
    ],
)
def test_parse_value_accepts_literals_and_whitelisted_constructors(
    text: str, value: object
) -> None:
    assert parse_value(text) == value


@pytest.mark.parametrize("text", ["f(1)", "x", "-True", "[", "{**a}", "Decimal(x=1)", "-'a'"])
def test_parse_value_refuses_everything_else(text: str) -> None:
    with pytest.raises(ValueError, match="not a literal"):
        parse_value(text)


@pytest.mark.parametrize(
    "value",
    [
        None,
        True,
        3,
        1.5,
        "s",
        Decimal("1.1"),
        Fraction(1, 3),
        [1, (2,)],
        {1},
        frozenset({2}),
        {"a": [1.0]},
    ],
)
def test_encode_and_decode_round_trip(value: object) -> None:
    assert decode_value(encode_value(value)) == value


@pytest.mark.parametrize(
    "value",
    [
        float("nan"),
        float("-inf"),
        [float("inf"), (1,), (), {2}, set(), frozenset(), {"k": Decimal("2.5")}],
        Fraction(2, 3),
    ],
)
def test_literal_text_reads_back(value: object) -> None:
    text = Outcome.of("value", literal_text(value)).text
    assert compare(value, parse_value(text), 0.0) == "equal"


def test_encode_refuses_an_opaque_value_and_decode_an_unknown_tag() -> None:
    with pytest.raises(TypeError, match="object is not a comparable value"):
        encode_value(object())
    with pytest.raises(ValueError, match="unknown value tag"):
        decode_value({"t": "blob", "v": []})


@pytest.mark.parametrize(
    ("expected", "actual", "closeness"),
    [
        ([1, 2], [1, 2, 3], "differ"),
        ({"a": 1}, {"b": 1}, "differ"),
        ({"a": 1.0}, {"a": 1.0000001}, "near-miss"),
        ((1,), [1], "differ"),
        (True, 1, "differ"),
        (1, 1.0, "equal"),
        ({1}, {1}, "equal"),
    ],
)
def test_compare_instances(expected: object, actual: object, closeness: str) -> None:
    assert compare(expected, actual, 1e-12) == closeness


def test_an_equality_that_raises_decides_nothing() -> None:
    class Weird:
        def __eq__(self, other: object) -> bool:
            raise RuntimeError

        __hash__ = object.__hash__

    w = Weird()
    assert compare(w, w, 1e-12) == "differ"


@pytest.mark.parametrize(
    ("text", "tol"),
    [
        ("0.35", 0.005),
        ("1.0", 0.05),
        ("2.5e-3", 5e-5),
        ("[1, 2]", 1e-12),
        ("1e3", 500.0),
        ("[0.5, 1.25]", 0.005),
    ],
)
def test_literal_abs_tol_is_half_the_last_written_place(text: str, tol: float) -> None:
    assert literal_abs_tol(text) == pytest.approx(tol)


def test_outcome_parsing_both_halves() -> None:
    assert Outcome.of("true", "x") == Outcome("value", "True")
    assert Outcome.of("value", "[1,2]").text == "[1, 2]"
    assert out("raises", "ValueError").show() == "raises ValueError"
    for kind, text in (("raises", "not a name"), ("number", "1"), ("value", "f(x)")):
        with pytest.raises(
            ValueError, match=r"not an exception name|unknown outcome|not a literal"
        ):
            Outcome.of(kind, text)
    assert not out("value", "1").same(out("raises", "ValueError"))
    assert Outcome.from_dict(out("value", "1").to_dict()) == out("value", "1")


def test_the_sealed_record_reads_back() -> None:
    raw = {
        "id": "E-9",
        "units": ["S-002"],
        "setup": ["x = 1"],
        "call": "f(x)",
        "predictions": [
            {"outcome": {"kind": "value", "text": "[1, 1]"}, "decides": "d", "raw_sha256": "a"},
            {"outcome": {"kind": "value", "text": "f(("}, "raw_sha256": "b"},
            {"outcome": None, "raw_sha256": "c"},
        ],
        "references": [
            {"status": "ran", "outcome": {"kind": "value", "text": "1"}},
            {"status": "timeout"},
            {"status": "ran", "form": "ok", "accepts": True},
        ],
        "alternatives": [{"outcome": {"kind": "raises", "text": "KeyError"}, "words": "w"}],
        "probes": [
            {"sha256": "p", "status": "ran", "outcome": {"kind": "value", "text": "2"}},
            {"sha256": "q", "status": "HANG"},
        ],
    }
    e = Example.from_dict(raw)
    assert [p.outcome for p in e.predictions] == [out("value", "[1, 1]"), None, None]
    assert e.predictions[0].decides == "d"
    assert [r.status for r in e.references] == ["ran", "timeout", "ran"]
    assert e.references[2].form == "ok"
    assert e.references[2].accepts
    assert e.alternatives[0].outcome == out("raises", "KeyError")
    assert e.probes[1].outcome is None
    minimal = Example.from_dict({"id": "E", "units": [], "call": "g()", "predictions": []})
    assert (minimal.setup, minimal.references, minimal.probes) == ((), (), ())


def test_a_tree_outcome_reads_back_and_refuses_an_unknown_kind() -> None:
    got = TreeOutcome.from_dict(
        {"kind": "raises", "raises": ["ValueError"], "detail": "d", "ran": ["m.py:1 (f)"]}
    )
    assert got == TreeOutcome("raises", None, ("ValueError",), "d", ("m.py:1 (f)",))
    assert TreeOutcome("raises").show() == "raises ?"
    assert TreeOutcome("hang", detail="x").show() == "hang: x"
    with pytest.raises(ValueError, match="unknown outcome kind"):
        TreeOutcome.from_dict({"kind": "weird"})
