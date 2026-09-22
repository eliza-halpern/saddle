"""Session names summarised from the message that opened them.

The model writes the title; this module's job is to never trust it to write
one that fits. Everything here is about what happens when the answer is not
a title: a preamble, a quoted string, a paragraph, an empty completion, a
server that is down. None of those may leave a wrong name on a session, and
none of them may fail the turn that triggered them.
"""

from __future__ import annotations

import pytest

from saddle.titles import (
    MAX_TITLE,
    TITLE_EFFORT,
    TITLE_TOKENS,
    clean_title,
    fallback_title,
    title_for,
)


class Answering:
    def __init__(self, answer: str | BaseException) -> None:
        self.answer = answer
        self.asked: list[dict[str, object]] = []

    def complete(self, prompt: str, **kwargs: object) -> str:
        self.asked.append({"prompt": prompt, **kwargs})
        if isinstance(self.answer, BaseException):
            raise self.answer
        return self.answer


# -- cleaning a model's answer ------------------------------------------------


@pytest.mark.parametrize(
    ("raw", "want"),
    [
        ("Slow Rust Build", "Slow Rust Build"),
        ('"Slow Rust Build"', "Slow Rust Build"),  # quoted
        ("'Slow Rust Build'", "Slow Rust Build"),
        ("`Slow Rust Build`", "Slow Rust Build"),
        ("**Slow Rust Build**", "Slow Rust Build"),  # markdown
        ("## Slow Rust Build", "Slow Rust Build"),
        ("Slow Rust Build.", "Slow Rust Build"),  # trailing stop
        ("Title: Slow Rust Build", "Slow Rust Build"),  # preamble
        ("title: Slow Rust Build", "Slow Rust Build"),
        ("Session title - Slow Rust Build", "Slow Rust Build"),
        ("Here's a title: Slow Rust Build", "Slow Rust Build"),
        ("Here is a short title: Slow Rust Build", "Slow Rust Build"),
        # ...but a title that merely starts with one of those words keeps it.
        ("Title bar rendering bug", "Title bar rendering bug"),
        ("Summary view is blank", "Summary view is blank"),
        ("\n\n  Slow   Rust  Build \n", "Slow Rust Build"),  # stray whitespace
        ("Slow Rust Build\nAnother line entirely", "Slow Rust Build"),
        ("", ""),
        ("   \n  \n ", ""),
        ('"".', ""),
    ],
)
def test_a_models_answer_is_reduced_to_something_that_fits(raw: str, want: str) -> None:
    assert clean_title(raw) == want


def test_a_title_that_runs_on_is_clipped_on_a_word_boundary() -> None:
    long = "Investigating why the release build of the rendering crate is slow"
    got = clean_title(long)
    assert len(got) <= MAX_TITLE + 1  # the ellipsis
    assert got.endswith("…")
    assert not got.rstrip("…").endswith(" ")
    assert long.startswith(got.rstrip("…"))  # a prefix, not a paraphrase


def test_a_single_unbroken_word_is_still_clipped() -> None:
    got = clean_title("x" * 200)
    assert len(got) == MAX_TITLE + 1
    assert got.endswith("…")


# -- falling back to the user's own words -------------------------------------


def test_the_fallback_is_the_first_line_of_the_request() -> None:
    assert fallback_title("fix the stream\n\nit splits between tabs") == "fix the stream"


def test_a_request_with_only_blank_lines_has_no_fallback() -> None:
    assert fallback_title("\n \n\t\n") == ""
    assert fallback_title("") == ""


# -- asking the model ---------------------------------------------------------


def test_the_model_names_the_session() -> None:
    client = Answering("Slow Rust Build")
    assert title_for(client, "why is my rust build taking 4 minutes") == "Slow Rust Build"


def test_the_request_is_in_the_prompt_and_deliberation_is_not_asked_for() -> None:
    client = Answering("A Title")
    title_for(client, "why is my rust build slow")
    asked = client.asked[0]
    assert "why is my rust build slow" in asked["prompt"]
    # Naming a session does not benefit from thinking, and thinking is 6x
    # slower: measured 153-224ms at "none" against 1179-1299ms at "low".
    assert asked["reasoning_effort"] == TITLE_EFFORT == "none"
    assert asked["temperature"] == 0.0
    assert asked["max_tokens"] == TITLE_TOKENS


def test_a_very_long_request_is_truncated_before_it_is_sent() -> None:
    client = Answering("A Title")
    title_for(client, "x" * 5_000)
    assert len(client.asked[0]["prompt"]) < 1_200


def test_a_server_that_is_down_falls_back_to_the_users_words_not_an_exception() -> None:
    client = Answering(RuntimeError("connection refused"))
    assert title_for(client, "why is my rust build slow") == "why is my rust build slow"


def test_an_empty_completion_falls_back_rather_than_naming_it_nothing() -> None:
    assert title_for(Answering("   "), "fix the stream") == "fix the stream"


def test_a_request_with_no_words_is_not_titled_at_all() -> None:
    # Nothing to summarise and nothing to fall back to: leave the name alone
    # rather than inventing one.
    client = Answering("Some Title")
    assert title_for(client, "  \n ") == ""
    assert client.asked == []  # and do not spend a call
