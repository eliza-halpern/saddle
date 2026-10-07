"""What the page does with a compaction note (#84), in Chrome.

The other module in this pair, `tests/test_compaction_note.py`, is the server's
side: every row the chat page is given says who wrote it, on a `note` field, and
a rewind refuses to land on a row the system wrote. This one is the page, on a
real headless Chrome against the served app:

* A reload after a compaction draws the note as the system's own line -- not a
  user bubble, and with nothing on it that rewinds. Before, the note rode in
  under the user role (the stored history keeps one system row, at index 0), so
  it drew as a question of the person's with Retry and Edit, and its index was a
  place a rewind could cut the chat at the system's own line.
* The buttons a turn ends with name the question the store still holds, at the
  index the store says it sits at, after a real `saddle.memory.compact` moved the
  rows under a transcript that is still painted. Numbering the two lists by
  counting gave a surviving question a dropped one's index, so rewinding it lost
  turns, and left a dropped question with buttons naming something else.

The turn is scripted rather than generated -- as `tests/test_chat_server.py`
does -- and the compaction is `saddle.memory.compact` itself, so the note in the
store is the note a compacting turn writes. Screenshots of each state are saved
beside the run.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from chrome_page import ChatSite, drive_page
from test_ui3_mode import NoModel, serving

from saddle.events import ContentDelta
from saddle.memory import compact, is_note
from saddle.sessions import SessionStore
from saddle.web.app import asked_rows, build_app

NOTE_BODY = (
    "[Earlier conversation compacted: 4 earlier message(s) dropped to fit the window.]\n"
    "Run state, rebuilt from the run's records (not model-written):\n"
    "- task: fix the reporter\n"
    "- files changed since the run's starting commit: src/saddle/report.py"
)
# A chat as a compaction left it on disk: a question, the system's line where
# older messages used to be, a question asked after it, and what answered it.
COMPACTED: list[dict[str, Any]] = [
    {"role": "system", "content": "instructions for this repository"},
    {"role": "user", "content": "why does the reporter drop the count?"},
    {"role": "assistant", "content": "because it reads the wrong column."},
    {"role": "user", "content": NOTE_BODY},
    {"role": "user", "content": "fix it and show the test"},
    {"role": "assistant", "content": "the test is in tests/test_reporter.py."},
]

# Where the system's line sits in that chat, and where the question asked after
# it sits: the two indexes the page has to tell apart.
NOTE_INDEX = next(index for index, row in enumerate(COMPACTED) if is_note(row))
ASKED_INDEX = 4

# The same chat before that compaction, which is what the third test seeds so a
# turn can compact it: four questions and the answers between them, long enough
# that a turn has to take the oldest out to fit the window.
LONG: list[dict[str, Any]] = [
    {"role": "system", "content": "instructions for this repository"},
    {"role": "user", "content": "why does the reporter drop the count?"},
    {"role": "assistant", "content": "because it reads the wrong column."},
    {"role": "user", "content": "which column does it read?"},
    {"role": "assistant", "content": "the one the ledger writes."},
    {"role": "user", "content": "and who writes that?"},
    {"role": "assistant", "content": "the audit stage."},
]


@contextmanager
def scripted_chat(tmp_path: Path, *, compact_from: int | None = None) -> Iterator[ChatSite]:
    """A served chat whose turns are scripted rather than generated, one session.

    `compact_from` is how many stored messages make the scripted turn compact the
    chat with `saddle.memory.compact`: the history is rewritten while the turn is
    running, which is what an over-long chat does for real, and the row it leaves
    is the note the page then has to number around.
    """
    workdir = tmp_path / "work"
    workdir.mkdir()
    store = SessionStore(tmp_path / "sessions")
    script: list[Any] = [
        ContentDelta(text="because it reads the wrong column."),
        ContentDelta(text="the test is in tests/test_reporter.py."),
    ]

    def fake_run_turn(
        _client: Any, messages: list[dict[str, Any]], text: str, _options: Any, **_kw: Any
    ) -> Any:
        messages.append({"role": "user", "content": text})
        if compact_from is not None and len(messages) >= compact_from:
            compact(messages, limit_tokens=1)
        yield from script

    import saddle.web.app as module

    original = module.run_turn
    module.run_turn = fake_run_turn  # type: ignore[assignment]
    try:
        app = build_app(store, NoModel, default_workdir=workdir)
        with serving(app) as base:
            sid = store.create(title="compacted chat", workdir=str(workdir)).id
            yield ChatSite(base, store, sid)
    finally:
        module.run_turn = original


def test_a_compaction_note_reloads_as_the_speaking_system_with_no_rewind(tmp_path: Path) -> None:
    """The note's row, drawn on a page that reloaded after the compaction."""
    with scripted_chat(tmp_path) as site:
        site.store.save_messages(site.sid, COMPACTED)
        seen = drive_page(
            site.base,
            """
            await page.chat(args.sid);
            const drawn = await page.js(
              async (sid) => {
                const note = document.querySelector("#transcript .compaction");
                // The rows the page is given, exactly as a reload receives them.
                const stored = await api(`/api/sessions/${sid}/messages`);
                return {
                  lines: [...document.querySelectorAll("#transcript > *")].map(
                    (row) => row.className,
                  ),
                  note:
                    note === null
                      ? null
                      : {
                          said: note.textContent,
                          head: note.childNodes[0] ? note.childNodes[0].textContent : null,
                          role: note.getAttribute("role"),
                          buttons: note.querySelectorAll("button").length,
                          rewinds: note.querySelectorAll("[data-index]").length,
                          insideATurn: note.closest(".turn") !== null,
                          askedMarkup: note.querySelector(".user") !== null,
                        },
                  answers: [...document.querySelectorAll("#transcript .assistant")].map(
                    (box) => box.textContent,
                  ),
                  turns: [...document.querySelectorAll("#transcript .turn")]
                    .filter((turn) => turn.querySelector(".user"))
                    .map((turn) => ({
                      said: turn.querySelector(".user").textContent,
                      index: turn.dataset.index ?? null,
                      buttons: [...turn.querySelectorAll(".turn-tools button")].map(
                        (button) => button.textContent,
                      ),
                    })),
                  stored: stored.map((row) => ({ index: row.index, note: row.note })),
                };
              },
              args.sid,
            );
            await page.shot("note-after-reload");
            return drawn;
            """,
            sid=site.sid,
            shots=str(tmp_path),
        )

    # Known-good: the note is one line of the system's own, labelled as the
    # system speaking, with nothing on it that rewinds and none of the person's
    # question markup.
    assert seen["note"] == {
        "said": f"Context compacted{NOTE_BODY}",
        "head": "Context compacted",
        "role": "note",
        "buttons": 0,
        "rewinds": 0,
        "insideATurn": False,
        "askedMarkup": False,
    }, seen["note"]
    # One line per stored message, in the order the store holds them: the note is
    # not dropped, and the questions after it do not slide up a place.
    assert seen["lines"] == ["turn", "turn", "compaction", "turn", "turn"], seen["lines"]
    # The answers are their own words; the note was not folded into one of them.
    assert seen["answers"] == [
        "because it reads the wrong column.",
        "the test is in tests/test_reporter.py.",
    ], seen["answers"]
    # Known-bad, which is the bug: the note drawn as a third question, with the
    # buttons that rewind, and an index of its own to rewind to.
    assert [turn["said"] for turn in seen["turns"]] == [
        "why does the reporter drop the count?",
        "fix it and show the test",
    ], seen["turns"]
    assert [turn["buttons"] for turn in seen["turns"]] == [["↻", "✎"], ["↻", "✎"]], seen["turns"]
    assert [turn["index"] for turn in seen["turns"]] == ["1", "4"], seen["turns"]
    # The note keeps the place it was stored at, and the question after it keeps
    # its own: the two rows the page drew answer to store rows 1 and 4, with the
    # note's row 3 between them and named nowhere.
    assert all(turn["index"] != str(NOTE_INDEX) for turn in seen["turns"]), seen["turns"]


def test_rewinding_a_compaction_note_refuses_on_the_page_as_it_does_on_the_server(
    tmp_path: Path,
) -> None:
    """A page cannot aim a rewind at the system's line, on either end of it."""
    with scripted_chat(tmp_path) as site:
        site.store.save_messages(site.sid, COMPACTED)
        seen = drive_page(
            site.base,
            """
            await page.chat(args.sid);
            const read = await page.js(
              async (sid, noteIndex, askedIndex) => {
                /** What the page's own request helper makes of a rewind request. */
                const fail = async (index) => {
                  try {
                    return await api(`/api/sessions/${sid}/rewind?index=${index}`);
                  } catch (error) {
                    return String(error.message);
                  }
                };
                return {
                  note: await fail(noteIndex),
                  asked: await fail(askedIndex),
                };
              },
              args.sid,
              args.note,
              args.asked,
            );
            await page.shot("note-rewind-refused");
            return read;
            """,
            sid=site.sid,
            note=NOTE_INDEX,
            asked=ASKED_INDEX,
            shots=str(tmp_path),
        )
        stored = site.store.load_messages(site.sid)

    # The note's index is not a question, and the page is told so in so many
    # words -- the message its rewind dialog would show. Rewinding there would
    # cut the chat at the system's own line, the question the note was written
    # for, and every question the compaction kept after it.
    assert seen["note"] == "no question there"
    # The question the person asked after the note is still question-shaped, so
    # the dialog can name it and rewind to it.
    assert seen["asked"] == {"text": "fix it and show the test", "reverted": [], "deleted": []}
    # Nothing was cut: the history is as it was stored.
    assert asked_rows(stored) == asked_rows(COMPACTED)


def test_a_turn_that_compacted_the_chat_under_never_keeps_buttons_on_a_dropped_question(
    tmp_path: Path,
) -> None:
    """The buttons a turn ends with answer to the rows the store holds now.

    The transcript on screen was painted before the compaction, so its questions
    are not the rows the store has any more. A question the compaction dropped
    keeps neither its index nor its buttons; a question it kept is named by its
    own row, wherever the compaction moved it.
    """
    question = "and show the gate it fails"
    with scripted_chat(tmp_path, compact_from=4) as site:
        site.store.save_messages(site.sid, LONG)
        seen = drive_page(
            site.base,
            """
            await page.chat(args.sid);
            await page.type("#input", args.question);
            await page.key("Enter");
            await page.until(
              () => !document.querySelector("#transcript .turn.pending") && state.lastAct !== null,
            );
            // `turn.end` reaches the page before the turn's messages are saved,
            // so the store the numbering answers to is a moment behind. Wait for
            // the row the turn added -- what the next reload would see -- and let
            // the page stamp the transcript again, as it does on reload.
            await page.until(
              async (sid, asked) => (await api(`/api/sessions/${sid}/messages`)).some(
                (row) => !row.note && row.content === asked,
              ),
              args.sid,
              args.question,
            );
            await page.js(() => restampTurns());
            const read = await page.js(async (sid) => ({
              turns: [...document.querySelectorAll("#transcript .turn")]
                .filter((turn) => turn.querySelector(".user"))
                .map((turn) => ({
                  said: turn.querySelector(".user").textContent,
                  index: turn.dataset.index ?? null,
                  buttons: [...turn.querySelectorAll(".turn-tools button")].map(
                    (button) => button.textContent,
                  ),
                })),
              notes: document.querySelectorAll("#transcript .compaction").length,
              stored: await api(`/api/sessions/${sid}/messages`),
            }), args.sid);
            await page.shot("turn-after-compaction");
            return read;
            """,
            sid=site.sid,
            question=question,
            shots=str(tmp_path),
            timeout=90,
        )
        stored = asked_rows(site.store.load_messages(site.sid))

    # The store, after `saddle.memory.compact` ran inside the turn: the note the
    # turn wrote, and what is left of the questions, each with the index it now
    # sits at -- which is not where its bubble sits in the painted transcript.
    questions = {int(row["index"]): str(row["content"]) for row in stored if not row["note"]}
    dropped_words = [
        row["content"]
        for row in LONG
        if row["role"] == "user" and row["content"] not in questions.values()
    ]
    assert questions, stored
    assert dropped_words, stored
    assert question in questions.values(), stored
    # Four questions were painted; the store holds fewer, because the compaction
    # took the oldest ones out from under the screen that still shows them.
    assert len(seen["turns"]) > len(questions), (seen["turns"], stored)
    # Known-good: every painted question the store still holds is named by its own
    # row, and its buttons name that row's index -- not its own place in the
    # transcript, which the compaction invalidated.
    numbered = [turn for turn in seen["turns"] if turn["index"] is not None]
    assert [(int(turn["index"]), questions[int(turn["index"])]) for turn in numbered] == sorted(
        questions.items()
    ), (
        seen["turns"],
        questions,
    )
    assert all(turn["buttons"] == ["↻", "✎"] for turn in numbered), seen["turns"]
    # Known-bad, which is the bug: numbering the painted questions by counting
    # numbered a question the store no longer has, and gave one the index of a row
    # the compaction dropped or moved, so rewinding it cut more turns than it
    # named. A question the compaction dropped now keeps neither index nor buttons.
    unnumbered = [turn for turn in seen["turns"] if turn["index"] is None]
    assert [turn["said"] for turn in unnumbered] == dropped_words, (seen["turns"], dropped_words)
    assert all(turn["buttons"] == [] for turn in unnumbered), seen["turns"]
    # Note the person never asked: the store's answer to "who wrote this row".
    assert any(row["note"] for row in stored), stored
