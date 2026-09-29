"""`task_units`: the deterministic parser that decides which task sentences exist.

Every rule is tested by instances, both halves (CONTRIBUTING, "A constraint
is verified by instances"): what splits and what does not, which unit binds
and which only asks.
"""

from __future__ import annotations

import pytest

from saddle.task_units import label_of, modality, parameters_of, split_sentences, task_units

T7_SIGNATURE = "`OrderedList(iterable=None)` holds values in ascending sorted order."
T7A = (
    "The optional constructor iterable may be any iterable (including generators); "
    "its values need not be sorted."
)
T7B = "Duplicates are allowed and preserved."
T7C = "you may split into a package as long as this import works"
BOTH = "Your constructor's iterable argument may be any iterable."


def texts(text: str) -> list[str]:
    return [u.text for u in task_units(text).units]


@pytest.mark.parametrize(
    ("text", "want"),
    [
        ("Call `ol.copy(). x` first. Then stop.", ["Call `ol.copy(). x` first.", "Then stop."]),
        ("Use e.g. bisect here. Done!", ["Use e.g. bisect here.", "Done!"]),
        ("Call ol.copy(). Then stop.", ["Call ol.copy(). Then stop."]),
        (
            "Values (like 1. 2. 3) stay. Why? Because.",
            ["Values (like 1. 2. 3) stay.", "Why?", "Because."],
        ),
        ("No terminator", ["No terminator"]),
        ("Two  spaces.  Next.", ["Two  spaces.", "Next."]),
    ],
)
def test_sentences_split_at_terminators_outside_spans_brackets_and_dotted_tokens(
    text: str, want: list[str]
) -> None:
    assert split_sentences(text) == want


def test_blocks_list_items_fences_and_headings() -> None:
    text = (
        "# Title\n"
        "\n"
        "Intro sentence. Second one.\n"
        "\n"
        "## Spec\n"
        "\n"
        "- `f(x)` returns x. It is wrapped\n"
        "  onto a second line.\n"
        "- second item\n"
        "\n"
        "```python\n"
        "x = 1  # must never be a unit. Really.\n"
        "```\n"
        "1. ordered item\n"
        "2) other item\n"
        "- ABC-12: labelled item\n"
        "\n"
        "### Deeper\n"
        "REQ-1 must hold.\n"
    )
    units = task_units(text)
    assert [u.text for u in units.units] == [
        "Intro sentence.",
        "Second one.",
        "`f(x)` returns x. It is wrapped onto a second line.",
        "second item",
        "1. ordered item",
        "2) other item",
        "ABC-12: labelled item",
        "REQ-1 must hold.",
    ]
    by = {u.text: u for u in units.units}
    assert by["Intro sentence."].heading == ("Title",)
    assert by["second item"].heading == ("Title", "Spec")
    assert by["REQ-1 must hold."].heading == ("Title", "Spec", "Deeper")
    assert [u.label for u in units.units] == [None, None, None, None, "1.", "2)", "ABC-12", "REQ-1"]
    assert [u.kind for u in units.units][:3] == ["sentence", "sentence", "list-item"]
    assert by["`f(x)` returns x. It is wrapped onto a second line."].line == 7
    assert [u.id for u in units.units][:2] == ["S-001", "S-002"]
    assert units.by_id("S-008") is by["REQ-1 must hold."]
    assert units.by_id("S-999") is None
    # the census holds every unit: its denominator equals the hand count, 8
    census = units.census()
    assert census.splitlines()[0] == "8 unit(s): 8 binding, 0 delegating, 0 binding-uncertain"
    assert len(census.splitlines()) == 9
    assert "S-007 L16 N3 binding [ABC-12]: ABC-12: labelled item" in census


def test_a_long_unit_is_shortened_in_the_census_only() -> None:
    long = "word " * 40 + "end."
    units = task_units(long)
    assert units.units[0].text == long.strip()
    assert units.census().splitlines()[1].endswith("...")


@pytest.mark.parametrize(
    ("text", "label"),
    [
        ("12. item", "12."),
        ("3) item", "3)"),
        ("AB-1 x", "AB-1"),
        ("ABCDEF-1 x", None),
        ("A-1 x", None),
        ("12.5 is a number", None),
        ("plain", None),
    ],
)
def test_labels_are_the_generic_enumerator_shapes_only(text: str, label: str | None) -> None:
    assert label_of(text) == label


def test_flags_record_why_a_unit_is_a_candidate_and_n3_is_kept() -> None:
    units = task_units(f"{T7_SIGNATURE} {T7B} Stdlib only. Call helper(x) now.").units
    assert [u.flags for u in units] == [("N1",), ("N3",), ("N2",), ("N1",)]
    assert units[2].keywords == ("only",)
    # a keyword inside a code span is not a keyword
    assert task_units("Use `may_fail()` and `return x`.").units[0].flags == ("N1",)


def test_parameters_come_from_signatures_in_code_spans() -> None:
    text = "`f(self, a, b=1, *args, c: int = 2, **kw)` and `g()`; h(z) is prose."
    assert parameters_of(text) == frozenset({"a", "b", "args", "c", "kw"})


# -- modality: the subject rule (R7-t7a/b/c, R7-u, R7-both) --------------------


def test_r7_t7a_a_caller_side_may_binds() -> None:
    units = task_units(f"{T7_SIGNATURE} {T7A}").units
    assert units[1].text == T7A
    assert units[1].modality == "binding"
    assert "iterable" in units[1].subject


def test_r7_t7a_the_parameter_name_alone_binds() -> None:
    sig = "`make(widgets=None)` builds one."
    assert modality("The widgets may be empty.", parameters_of(sig))[0] == "binding"
    assert modality("The widgets may be empty.")[0] == "binding-uncertain"


def test_r7_t7b_a_declarative_sentence_is_a_binding_candidate() -> None:
    (unit,) = task_units(T7B).units
    assert (unit.flags, unit.modality, unit.keywords) == (("N3",), "binding", ())


def test_r7_t7c_an_implementer_side_may_delegates() -> None:
    assert modality(T7C)[0] == "delegating"
    assert modality("You should prefer bisect.")[0] == "delegating"
    assert modality("The implementation may cache results.")[0] == "delegating"


def test_r7_u_a_may_with_neither_subject_is_uncertain() -> None:
    assert modality("The result may be cached.") == ("binding-uncertain", "the result")


def test_r7_both_implementer_and_caller_tokens_make_it_uncertain() -> None:
    assert modality(BOTH, parameters_of(T7_SIGNATURE)) == (
        "binding-uncertain",
        "your constructor's iterable argument",
    )


def test_should_only_units_follow_the_same_rule() -> None:
    assert modality("The caller should pass a list.")[0] == "binding"
    assert modality("Optional.")[0] == "binding-uncertain"
    assert modality("Any input is optional.")[0] == "binding"


def test_a_binding_keyword_binds_whatever_the_subject() -> None:
    assert modality("You must return a list.")[0] == "binding"
    assert modality("You may not raise.")[1] == "binding keyword 'raise'"


def test_clauses_are_read_one_by_one() -> None:
    # each clause decides alone; the unit binds only if every clause does
    assert modality("the value may be None; you may cache it")[0] == "binding-uncertain"
    assert modality("the value may be None, and the key may repeat")[0] == "binding"
    assert modality("you may cache; you should log")[0] == "delegating"
    # a comma inside a code span ends no clause
    assert modality("`g(x, z)` may be None", frozenset({"x"}))[0] == "binding"
