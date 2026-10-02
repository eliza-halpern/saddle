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

from saddle.prompt_changes import PromptChanges, prompt_changes

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
