"""The rows a compaction leaves, as the server gives them to the page (#84).

A compaction note is stored under the user role, because the served chat
template refuses a system message after index 0 -- `saddle.memory` says so where
it writes the note. Straight out of the store it is indistinguishable from a
question the person typed, and the page drew it as one: a user bubble with retry
and edit buttons, whose index cut the chat at the note itself and threw away
every question the compaction kept.

So each row the page is given says who wrote it, on a `note` field, and a rewind
refuses to land on a row the system wrote. Both halves are rules about what is
accepted, so each is shown with a known-good instance and a known-bad one: a
note the page cannot tell anything else about is a question nobody asked, a flag
on the wrong row moves the buttons off the question they belong to, and a rewind
that takes a note's index is data loss with a confirmation dialog on it.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from test_chat_server import app_for, store  # noqa: F401 -- the SessionStore fixture

from saddle.memory import NOTE_HEAD, compact, is_note
from saddle.sessions import SessionStore
from saddle.vision import IMAGE_FOLLOWUP
from saddle.web.app import asked_rows, history_for_display

NOTE_WORDS = (
    f"{NOTE_HEAD}4 earlier message(s) dropped to fit the window.]\n"
    "Run state, rebuilt from the run's records (not model-written):\n"
    "- task: fix the reporter\n"
    "- files changed since the run's starting commit: src/saddle/report.py"
)


def note_row() -> dict[str, Any]:
    """What `saddle.memory` puts in the history, in the role it has to use."""
    return {"role": "user", "content": NOTE_WORDS}


def a_history() -> list[dict[str, Any]]:
    """A chat as a compaction leaves it: a question, the system's line where
    four older messages were, and a question asked after it."""
    return [
        {"role": "system", "content": "instructions for this repository"},
        {"role": "user", "content": "why does the reporter drop the count?"},
        {"role": "assistant", "content": "because it reads the wrong column."},
        note_row(),
        {"role": "user", "content": "fix it and show the test"},
        {"role": "assistant", "content": "the test is in tests/test_reporter.py."},
    ]


def test_history_rows_say_who_wrote_each_one(store: SessionStore, tmp_path: Path) -> None:  # noqa: F811
    """`history_for_display` marks the note, and only the note.

    Mutation it answers: dropping the flag, and setting it from a substring
    match instead of `is_note`.
    """
    history = a_history()
    rows = history_for_display(history, tmp_path)

    assert [row.get("note", False) for row in rows] == [False, False, False, True, False, False]
    # The indexes still name the rows they did before: the note is not dropped,
    # and the questions after it do not slide down a place.
    assert [row["index"] for row in rows] == [0, 1, 2, 3, 4, 5]
    assert rows[3]["content"] == NOTE_WORDS
    # The marked row is still stored under the user role: the flag is the only
    # thing that tells the page the system wrote it, not the person.
    assert rows[3]["role"] == "user"
    assert [row["role"] for row in rows] == [
        "system",
        "user",
        "assistant",
        "user",
        "user",
        "assistant",
    ]


def test_a_line_that_is_not_a_note_is_not_marked_as_one(
    store: SessionStore,  # noqa: F811
    tmp_path: Path,
) -> None:
    """The flag follows `saddle.memory`'s own test for a note, not a word in it.

    A message that merely carries the note's words -- the task the note was
    rebuilt for, quoted into a question -- stays the person's, and an empty
    message is not a note either: `is_note` asks for the head at the start.
    """
    quoting = {"role": "user", "content": f"please quote this: {NOTE_HEAD} ...]"}
    history = [
        {"role": "system", "content": "instructions"},
        quoting,
        {"role": "user", "content": ""},
        note_row(),
    ]
    rows = history_for_display(history, tmp_path)
    assert [row.get("note", False) for row in rows] == [False, False, False, True]
    assert [row.get("note") for row in rows[:3]] == [None, None, None]
    assert rows[3]["note"] is True
    assert not is_note(quoting)
    assert is_note(note_row())


def test_asked_rows_are_the_questions_the_page_can_name(store: SessionStore) -> None:  # noqa: F811
    """`/api/sessions/{sid}/messages` answers with exactly these rows.

    The page ends a turn by matching its question bubbles against this list, so
    it must be the rows as the page drew them: the note kept and marked, what
    `read_file` queued for the model dropped as `history_for_display` drops it.
    """
    history = [
        *a_history(),
        {
            "role": "user",
            "content": [{"type": "text", "text": IMAGE_FOLLOWUP + " what is in the picture"}],
        },
        {"role": "assistant", "content": "a screenshot of the chart."},
    ]
    rows = asked_rows(history)

    assert [(row["index"], row["note"]) for row in rows] == [(1, False), (3, True), (4, False)]
    # Known bad for the pairing: the screenshot follow-up is not something the
    # person said, and a row counted as one shifts every index after it.
    assert [row["content"] for row in rows] == [
        "why does the reporter drop the count?",
        NOTE_WORDS,
        "fix it and show the test",
    ]
    assert asked_rows([]) == []
    # An ordinary list-content question is a question, so it stays.
    parts = list(asked_rows([{"role": "user", "content": [{"type": "text", "text": "look"}]}]))
    assert [(row["index"], row["note"]) for row in parts] == [(0, False)]


def test_a_note_the_compactor_wrote_is_the_row_history_marks(
    store: SessionStore,  # noqa: F811
    tmp_path: Path,
) -> None:
    """Not a hand-written stand-in: `saddle.memory.compact`'s own output."""
    sid = store.create(title="compacting", workdir=str(tmp_path)).id
    store.save_messages(sid, a_history())
    compact(store.load_messages(sid), limit_tokens=1)
    history = store.load_messages(sid)

    notes = [index for index, message in enumerate(history) if is_note(message)]
    assert notes
    assert history[notes[0]]["role"] == "user"  # the role the template forces on it
    marked = [row["index"] for row in history_for_display(history, tmp_path) if row.get("note")]
    assert marked == notes
    flagged = [row["index"] for row in asked_rows(history) if row["note"]]
    assert flagged == notes


def test_rewinding_to_a_compaction_note_is_refused(store: SessionStore, tmp_path: Path) -> None:  # noqa: F811
    """A note's index is not a place to rewind to, on either end of the dialog.

    Taking it would cut the chat at the system's own line: the question that
    was being answered at the time, and every question the compaction kept.
    """
    with app_for(store, tmp_path) as (client, _app):
        sid = client.post("/api/sessions").json()["id"]
        store.save_messages(sid, a_history())
        note = next(
            index for index, message in enumerate(store.load_messages(sid)) if is_note(message)
        )

        preview = client.get(f"/api/sessions/{sid}/rewind", params={"index": note})
        assert preview.status_code == 404
        assert preview.json() == {"error": "no question there"}
        done = client.post(f"/api/sessions/{sid}/rewind", json={"index": note})
        assert done.status_code == 404
        assert done.json() == {"error": "no question there"}
        assert store.load_messages(sid) == a_history()
        # A refused rewind must not leave the session wedged as busy.
        assert client.post(f"/api/sessions/{sid}/stop").json() == {"stopping": False}


def test_rewinding_to_the_question_a_note_follows_still_works(
    store: SessionStore,  # noqa: F811
    tmp_path: Path,
) -> None:
    """The refusal is about the note's row, not about rewinding after one."""
    with app_for(store, tmp_path) as (client, _app):
        sid = client.post("/api/sessions").json()["id"]
        store.save_messages(sid, a_history())
        asked = 4  # the question asked after the note

        preview = client.get(f"/api/sessions/{sid}/rewind", params={"index": asked})
        assert preview.status_code == 200
        assert preview.json()["text"] == "fix it and show the test"
        assert (
            client.post(f"/api/sessions/{sid}/rewind", json={"index": asked}).json()["ok"] is True
        )
        kept = store.load_messages(sid)
        assert [message["content"] for message in kept[: asked + 1]] == [
            row["content"] for row in a_history()[: asked + 1]
        ]
        assert len(kept) == asked + 1


def test_the_rows_the_page_is_given_when_a_session_opens_are_marked_too(
    store: SessionStore,  # noqa: F811
    tmp_path: Path,
) -> None:
    """The transcript a reload draws comes from the same rows, flagged alike."""
    with app_for(store, tmp_path) as (client, _app):
        sid = client.post("/api/sessions").json()["id"]
        store.save_messages(sid, a_history())
        rows = client.get(f"/api/sessions/{sid}/messages").json()
        assert [(row["index"], row.get("note", False)) for row in rows] == [
            (1, False),
            (3, True),
            (4, False),
        ]
