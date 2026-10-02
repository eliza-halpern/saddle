"""`prompt_changes`: which prompt text a change edited, named.

Known-good: an edited prompt constant, an added or deleted one, an edited
persona dict and an edited string in a `*_prompt` function are each named as
`path: NAME`. Known-bad (what must not be named): a string re-wrapped or
re-joined to the same value, a reordered dict, a changed docstring or comment,
a constant whose name is not a prompt name or whose value is not prose, and a
`raise` message inside a prompt function. A file that cannot be parsed is
returned apart, never read as "nothing changed".
"""

from __future__ import annotations

import pytest

from saddle.prompt_changes import (
    Measured,
    PromptChanges,
    Score,
    judge,
    measured,
    prompt_changes,
    read_score,
    unconfigured_detail,
)

OLD = 'SYSTEM_PROMPT = "You are a careful engineer. Read the code first."\n'


def names(base: str | None, head: str | None, path: str = "m.py") -> tuple[str, ...]:
    return prompt_changes({path: base}, {path: head}).names


def test_an_edited_prompt_constant_is_named_with_its_file() -> None:
    new = 'SYSTEM_PROMPT = "You are a careful engineer. Read the code twice."\n'
    assert names(OLD, new, "src/pkg/auto.py") == ("src/pkg/auto.py: SYSTEM_PROMPT",)


def test_the_same_value_written_another_way_is_no_change() -> None:
    wrapped = (
        "SYSTEM_PROMPT = (\n"
        '    "You are a careful engineer. "\n'
        '    "Read the code first."\n'
        ")  # comment\n"
    )
    joined = 'SYSTEM_PROMPT = "You are a careful engineer. " + "Read the code first."\n'
    typed = 'SYSTEM_PROMPT: Final = "You are a careful engineer. Read the code first."\n'
    for same in (wrapped, joined, typed):
        assert names(OLD, same) == ()
    # And a value that really differs is still caught after the same re-wrapping.
    assert names(OLD, wrapped.replace("first", "last")) == ("m.py: SYSTEM_PROMPT",)


def test_a_dict_of_personas_is_compared_by_value_not_by_key_order() -> None:
    old = 'BUILTIN_PERSONAS = {"a": "Be brief and exact.", "b": "Be thorough and slow."}\n'
    reordered = 'BUILTIN_PERSONAS = {"b": "Be thorough and slow.", "a": "Be brief and exact."}\n'
    edited = 'BUILTIN_PERSONAS = {"b": "Be thorough and slow.", "a": "Be brief and vague."}\n'
    assert names(old, reordered) == ()
    assert names(old, edited) == ("m.py: BUILTIN_PERSONAS",)


def test_a_dict_with_a_splat_is_still_compared() -> None:
    old = 'BUILTIN_PERSONAS = {**BASE, "a": "Be brief and exact."}\n'
    assert names(old, old) == ()
    assert names(old, old.replace("exact", "loose")) == ("m.py: BUILTIN_PERSONAS",)


def test_added_and_deleted_prompts_are_named() -> None:
    assert names(None, OLD) == ("m.py: SYSTEM_PROMPT",)
    assert names(OLD, None) == ("m.py: SYSTEM_PROMPT",)
    assert names(OLD, OLD.replace("SYSTEM_PROMPT", "SYSTEM_TEXT")) == ("m.py: SYSTEM_PROMPT",)


def test_a_non_literal_value_is_compared_by_its_expression() -> None:
    old = 'EDIT_RULES = "Edit only what the task names. {extra}".format(extra=X)\n'
    assert names(old, old) == ()
    assert names(old, old.replace("X)", "Y)")) == ("m.py: EDIT_RULES",)


def test_names_that_are_not_prompt_names_or_not_prose_are_not_prompts() -> None:
    # PROMPT_STYLE is a stylesheet name, RUFF_RULES a tuple of codes, the third
    # is lower case, and the fourth has no prose in it.
    src = (
        'PROMPT_STYLE = "color: red; font-weight: bold; margin: 0"\n'
        'RUFF_RULES = ("E", "F", "B")\n'
        'system_prompt = "You are a careful engineer. Read the code first."\n'
        'FLAG_PROMPT = "short"\n'
        "N_PROMPT = 3\n"
        "SOME_PROMPT, OTHER = 1, 2\n"
    )
    changed = (
        'PROMPT_STYLE = "color: blue; font-weight: bold; margin: 1"\n'
        'RUFF_RULES = ("E", "F", "B", "I")\n'
        'system_prompt = "You are a careless engineer. Read the code never."\n'
        'FLAG_PROMPT = "shorter"\n'
        "N_PROMPT = 4\n"
        "SOME_PROMPT, OTHER = 3, 4\n"
    )
    assert names(src, changed) == ()
    assert names("", src) == ()


def test_a_prompt_that_stops_being_prose_is_reported_once() -> None:
    assert names(OLD, 'SYSTEM_PROMPT = "gone"\n') == ("m.py: SYSTEM_PROMPT",)


def test_comments_and_attribute_docstrings_are_not_prompt_text() -> None:
    old = OLD + '"""Appended to the system prompt."""\n# a comment\n'
    new = OLD + '"""Appended to the user prompt instead."""\n# another comment\n'
    assert names(old, new) == ()


FUNC = (
    "def build_worker_prompt(task):\n"
    '    """Why this prompt exists."""\n'
    '    if not task:\n        raise ValueError("task is empty")\n'
    '    return f"Do this task: {task}. Then call finish."\n'
)


def test_a_prompt_function_is_named_when_its_text_changes() -> None:
    assert names(FUNC, FUNC) == ()
    assert names(FUNC, FUNC.replace("Then call", "Afterwards call")) == (
        "m.py: build_worker_prompt",
    )
    # A new variable in the template is a text change too.
    assert names(FUNC, FUNC.replace("{task}", "{task!r}")) == ("m.py: build_worker_prompt",)


def test_a_prompt_function_ignores_docstrings_raise_messages_and_logic() -> None:
    same = FUNC.replace("Why this prompt exists.", "Other words.").replace(
        "task is empty", "no task given"
    )
    assert names(FUNC, same) == ()
    assert names(FUNC, FUNC.replace("if not task:", "if task is None:")) == ()
    plain = 'def build_prompt():\n    return "Do this task. Then call finish."\n'
    split = 'def build_prompt():\n    return "Do this task. " + "Then call finish."\n'
    assert names(plain, split) == ()


def test_a_method_is_named_by_its_class_and_a_look_alike_is_not_a_prompt() -> None:
    old = (
        "class Persona:\n    def provider_prompt(self):\n        return 'Use the tools as asked.'\n"
    )
    assert names(old, old.replace("asked", "told")) == ("m.py: Persona.provider_prompt",)
    other = "def prompt_shape(x):\n    return 'shape of the prompt text'\n"
    assert names(other, other.replace("shape of", "form of")) == ()


def test_a_prompt_function_added_or_deleted_is_named() -> None:
    assert names(None, FUNC) == ("m.py: build_worker_prompt",)
    assert names(FUNC, "") == ("m.py: build_worker_prompt",)


def test_an_unreadable_file_is_returned_apart_and_never_as_nothing() -> None:
    broken = "def f(:\n"
    got = prompt_changes({"a.py": OLD, "b.py": broken}, {"a.py": OLD, "b.py": OLD})
    assert got == PromptChanges((), ("b.py",))
    assert got
    assert prompt_changes({"a.py": OLD}, {"a.py": broken}).unreadable == ("a.py",)
    assert prompt_changes({"a.py": OLD}, {"a.py": "x = 1\0"}).unreadable == ("a.py",)
    assert not prompt_changes({"a.py": OLD}, {"a.py": OLD})


def test_names_are_sorted_and_each_listed_once() -> None:
    base = {"b.py": OLD, "a.py": OLD + FUNC}
    head = {
        "b.py": OLD.replace("first", "last"),
        "a.py": (OLD + FUNC).replace("first", "last").replace("Then", "Next"),
    }
    assert prompt_changes(base, head).names == (
        "a.py: SYSTEM_PROMPT",
        "a.py: build_worker_prompt",
        "b.py: SYSTEM_PROMPT",
    )


@pytest.mark.parametrize(
    "last", ["PROMPT", "PROMPTS", "RULE", "RULES", "PERSONAS", "GRAMMAR", "INSTRUCTIONS"]
)
def test_each_prompt_word_ends_a_prompt_name(last: str) -> None:
    old = f'X_{last} = "one two three"\n'
    assert names(old, old.replace("two", "2")) == (f"m.py: X_{last}",)
    assert names(f'{last}_X = "one two three"\n', f'{last}_X = "one 2 three"\n') == ()


def test_a_sum_that_is_not_two_strings_is_compared_as_written() -> None:
    old = 'EDIT_RULES = "Edit only what the task names. " + TAIL\n'
    assert names(old, old) == ()
    assert names(old, old.replace("TAIL", "OTHER")) == ("m.py: EDIT_RULES",)
    assert names('X_RULES = "one two three " * 2\n', 'X_RULES = "one two three " * 3\n') == (
        "m.py: X_RULES",
    )


# -- the benchmark's score ---------------------------------------------------------


def last_line(*lines: str) -> str:
    return "\n".join(lines) + "\n"


@pytest.mark.parametrize(
    ("stdout", "expected"),
    [
        ('{"score": 0.83}', Score(0.83)),
        ('log\n{"score": 1, "n": 12, "label": "smoke"}\n\n', Score(1.0, 12, "smoke")),
        ('{"score": 0, "n": 0}', Score(0.0, 0)),
        ('{"score": -2.5}', Score(-2.5)),
        # the contract is the LAST line, nothing earlier and nothing later
        ('{"score": 0.9}\ndone', None),
        ("", None),
        ("   \n", None),
        ("0.83", None),
        ('["score", 1]', None),
        ('{"n": 3}', None),
        ('{"score": "0.8"}', None),
        ('{"score": true}', None),
        ('{"score": NaN}', None),
        ('{"score": Infinity}', None),
        ('{"score": 0.5, "n": 1.5}', None),
        ('{"score": 0.5, "n": -1}', None),
        ('{"score": 0.5, "n": true}', None),
        ('{"score": 0.5, "n": null}', None),
        ('{"score": 0.5, "label": 3}', None),
        ('{"score": 0.5', None),
    ],
)
def test_the_last_stdout_line_must_be_a_json_score(stdout: str, expected: Score | None) -> None:
    assert read_score(stdout) == expected


def test_a_run_has_a_score_only_when_it_launched_finished_and_exited_zero() -> None:
    good = last_line("log", '{"score": 0.5}')
    assert measured(0, good, timed_out=False, unavailable=False) == Measured(Score(0.5))
    for kwargs, why in (
        ({"timed_out": False, "unavailable": True}, "could not be launched"),
        ({"timed_out": True, "unavailable": False}, "timed out"),
    ):
        assert measured(0, good, **kwargs) == Measured(None, why)
    # a readable score on a non-zero exit is not a score
    assert measured(2, good, timed_out=False, unavailable=False) == Measured(
        None, "failed to run (exit 2)"
    )
    assert measured(0, "no json", timed_out=False, unavailable=False) == Measured(
        None, "ran but produced no readable score"
    )


LEAD = "prompt text changed: a.py: X"


def ran(score: float, n: int | None = None, label: str = "") -> Measured:
    return Measured(Score(score, n, label))


def test_scores_without_a_bar_are_reported_and_not_judged() -> None:
    verdict, text = judge(LEAD, ran(0.83, 12), ran(0.8, 12), floor=None, margin=None)
    assert verdict == "not-proven"
    assert "prompt benchmark base 0.8 (n=12) -> head 0.83 (n=12)." in text
    assert "scores are reported, not judged" in text or "score is reported, not judged" in text
    # a better head and a worse head are both only reported
    assert judge(LEAD, ran(0.1), ran(0.9), floor=None, margin=None)[0] == "not-proven"


def test_a_floor_is_met_or_missed_by_the_head_score_alone() -> None:
    assert judge(LEAD, ran(0.75), None, floor=0.75, margin=None)[0] == "pass"
    verdict, text = judge(LEAD, ran(0.74), None, floor=0.75, margin=None)
    assert verdict == "fail"
    assert "head is below the floor 0.75" in text
    # the baseline's own score does not excuse a head below the floor
    assert judge(LEAD, ran(0.74), ran(0.2), floor=0.75, margin=None)[0] == "fail"


def test_a_margin_compares_the_head_with_a_readable_baseline() -> None:
    assert judge(LEAD, ran(0.7), ran(0.8), floor=None, margin=0.1000001)[0] == "pass"
    verdict, text = judge(LEAD, ran(0.69), ran(0.8), floor=None, margin=0.1)
    assert verdict == "fail"
    assert "head is more than 0.1 below base" in text
    assert judge(LEAD, ran(0.99), ran(0.8), floor=None, margin=0.0)[0] == "pass"
    both = judge(LEAD, ran(0.1), ran(0.9), floor=0.5, margin=0.1)
    assert both[0] == "fail"
    assert "below the floor" in both[1]
    assert "below base" in both[1]


def test_a_margin_without_a_baseline_score_is_not_proven_not_passed() -> None:
    gone = Measured(None, "timed out")
    for base in (None, gone):
        verdict, text = judge(LEAD, ran(0.9), base, floor=None, margin=0.1)
        assert verdict == "not-proven"
        assert "baseline has no score to compare with" in text
    assert (
        "base: the benchmark timed out; head 0.9"
        in judge(LEAD, ran(0.9), gone, floor=0.5, margin=0.1)[1]
    )
    # a floor still decides on the head alone
    assert judge(LEAD, ran(0.9), gone, floor=0.5, margin=None)[0] == "pass"
    assert judge(LEAD, ran(0.4), gone, floor=0.5, margin=0.1)[0] == "fail"


def test_a_head_without_a_score_is_never_a_pass_or_a_fail() -> None:
    for why in ("timed out", "failed to run (exit 3)", "ran but produced no readable score"):
        verdict, text = judge(LEAD, Measured(None, why), ran(0.9), floor=0.99, margin=0.0)
        assert verdict == "not-proven"
        assert f"the prompt benchmark {why}" in text


def test_the_score_shows_its_cases_and_label_and_the_unconfigured_note_names_the_change() -> None:
    assert (
        "head 0.8333 (n=3) [smoke]"
        in judge(LEAD, ran(0.83333, 3, "smoke"), None, floor=None, margin=None)[1]
    )
    text = unconfigured_detail(LEAD)
    assert text.startswith(LEAD + "; no test, coverage or mutation result measures")
    assert "configures no prompt benchmark" in text
