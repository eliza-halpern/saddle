"""The worker's plan (#209): steps kept word for word, closed only by `done` or `drop`,
shown after every compaction, recalled once at `finish`, and a reminder when a reply
lists steps the plan does not hold.

Each contract is pinned both ways. The detector's fixtures are a real run's words:
#132's part-b worker planned eight fixtures in its reasoning at 129.8 minutes
(`PLANNED`), restated them as six tests at 137.7 minutes without the one it most
needed (`RESTATED`), and a compaction at 138.5 minutes cleared the first version.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from test_auto import Scripted, auto, call, finish, git, outcome_span, repo, sidecar  # noqa: F401
from test_prompt_calibration import Counting, _talks
from test_run_env import _repo

from saddle.auto import PLAN_PROMPT, AutoOptions, run_auto
from saddle.engine import PLAN_OPEN
from saddle.journal import COMPACTION_SPAN, PLAN_REMINDER_SPAN, read_entries, read_spans
from saddle.labels import describe
from saddle.memory import compact, run_state
from saddle.plan import (
    ITEM_TOKENS,
    SECTION_HEAD,
    SECTION_TOKENS,
    UNRECOVERED,
    WHY_TOKENS,
    Plan,
    listed_steps,
)
from saddle.tools import PLAN_TOOL
from saddle.transcript import session_line
from saddle.vllm import StreamToken

PLANNED = (
    "the cleanest path now: bring the docstring and the fixture file into exact "
    "agreement, and add the few fixtures that are worth having (VALUES alias list; the "
    "join-condition derived table; a FROM that names no table; UPDATE "
    "aliased/set-from-another-table; output names read by clauses; aliased star; quoted "
    'alias; a recursive CTE as known-bad documenting "a recursive CTE that names itself"? '
    "hmm)."
)
"""#132b's plan at 129.8 minutes, word for word: in its reasoning and nowhere else."""

RESTATED = """Tests to add (concise):
1. `test_a_values_source_holds_only_the_names_it_declares` (pair).
2. `test_a_recursive_cte_reads_its_own_name` (pair).
3. `test_a_cte_reads_a_cte_named_beside_it` (pair).
4. `test_a_derived_table_holds_what_its_body_writes_or_reads` (pair: GOOD fixture too.)
5. `test_a_named_source_under_a_star_body_holds_only_its_declared_names` (pair).
6. Pair test for `derived-table-body-sees-no-name-of-its-own-source` (worth adding.)

Keep it manageable."""
"""The same worker's restatement at 137.7 minutes: six tests, the clause case gone."""

STATUS = """Done when checklist vs implementation:
1. ✓ good file exits 0 & no miss (tests + fixtures; also command-level).
2. ✓ misspelt table & misspelt column named at line/col, incl. later statement.
3. ✓ several misses each named (`several-misses-one-query`, `same-name-twice-one-query`).
"""
"""A list of what is done: its lead names a checklist, but its items carry check marks."""

PROBES = """Interesting: probe results now:
- `WITH c AS (...) SELECT c.bogon FROM c` -> miss (body's names not hoisted).
- `SELECT t.id FROM (SELECT id FROM users LIMIT 1) AS t, users;` -> ('column','id',1,9)
"""
"""Results the worker read back: no lead that announces work, no item that acts."""

READS = """Let me look first:
1. read tests/sql_check_fixtures.py
2. grep for recursive in src/saddle/sqlcheck.py
"""
"""This round's own moves: reading and searching are not a plan."""

CODE = 'Test parse("select 1;;select 2;") and parse("select 1; ;select 2").'
"""Semicolons inside a call's parentheses: code, not a list of steps."""


def _plan(*steps: str) -> Plan:
    plan = Plan()
    if steps:
        plan.apply(json.dumps({"action": "add", "items": list(steps)}))
    return plan


# -- the tool: add, done, drop, show ---------------------------------------------------


def test_add_keeps_each_step_word_for_word_under_the_next_id() -> None:
    plan = Plan()
    said = plan.apply(json.dumps({"action": "add", "items": ["write the alias fixture"]}))
    assert said.startswith("Added P1: write the alias fixture\n")
    said = plan.apply(
        json.dumps({"action": "add", "items": ["cover  ORDER BY\n  aliases", "add a VALUES test"]})
    )
    assert "Added P2: cover ORDER BY aliases" in said  # whitespace runs become one space
    assert "Added P3: add a VALUES test" in said
    assert [(i.id, i.text) for i in plan.items] == [
        ("P1", "write the alias fixture"),
        ("P2", "cover ORDER BY aliases"),
        ("P3", "add a VALUES test"),
    ]
    assert said.endswith(
        "Open:\nP1: write the alias fixture\nP2: cover ORDER BY aliases\nP3: add a VALUES test"
    )


def test_one_string_is_one_step_and_a_step_already_open_is_not_added_twice() -> None:
    plan = _plan("write the alias fixture")
    said = plan.apply(json.dumps({"action": "add", "items": "write the alias fixture"}))
    assert said.startswith("Already open as P1: write the alias fixture")
    assert len(plan.items) == 1


def test_a_restated_shorter_plan_leaves_every_earlier_step_open() -> None:
    """The traced loss: the restatement left five of eight steps out. Adding it
    adds what is new and closes nothing."""
    first = [part.strip() for part in PLANNED.split("(", 1)[1].rsplit(")", 1)[0].split(";")]
    plan = _plan(*first)
    plan.apply(json.dumps({"action": "add", "items": listed_steps(RESTATED)[0]}))
    texts = [i.text for i in plan.open_items()]
    assert "output names read by clauses" in texts
    assert len(texts) == len(first) + 6


@pytest.mark.parametrize(
    ("arguments", "said"),
    [
        ({"action": "done", "ids": ["P1"]}, "error: done needs why: what shows each is done"),
        ({"action": "drop", "ids": ["P1"], "why": "  "}, "error: drop needs why: why each"),
        ({"action": "done", "ids": ["P1", "P9"], "why": "t"}, "error: no item P9 in the plan"),
        ({"action": "done", "ids": ["P0"], "why": "t"}, "error: no item P0 in the plan"),
        ({"action": "drop", "ids": [0], "why": "t"}, "error: no item P0 in the plan"),
        ({"action": "done", "ids": [], "why": "t"}, "error: done needs ids"),
        ({"action": "done", "ids": [True], "why": "t"}, "error: done needs ids"),
        ({"action": "done", "ids": ["one"], "why": "t"}, "error: done needs ids"),
        ({"action": "done", "ids": ["P1"], "why": "w " * (WHY_TOKENS * 3)}, "error: why may"),
        ({"action": "replace", "items": ["x"]}, "error: plan's action must be add, done"),
        ({"action": "add"}, "error: add needs items"),
        ({"action": "add", "items": [3]}, "error: add needs items"),
        ({"action": "add", "items": ["ok", " "]}, "error: an item is empty"),
        ({"action": "add", "items": ["x" * (ITEM_TOKENS * 5)]}, "error: an item may hold at"),
        ({"action": "add", "item": "x"}, "error: plan does not take item"),
    ],
)
def test_a_call_that_cannot_be_carried_out_changes_nothing_and_says_how(
    arguments: dict[str, Any], said: str
) -> None:
    plan = _plan("write the alias fixture")
    before = [i.record() for i in plan.items]
    answer = plan.apply(json.dumps(arguments))
    assert answer.startswith(said)
    assert [i.record() for i in plan.items] == before


@pytest.mark.parametrize("arguments", ["{not json", "[1, 2]"])
def test_arguments_that_are_no_json_object_are_refused(arguments: str) -> None:
    assert _plan().apply(arguments).startswith("error: plan's arguments")


def test_done_and_drop_close_with_their_reason_and_a_second_close_changes_nothing() -> None:
    plan = _plan("write the alias fixture", "cover recursive CTEs", "add a VALUES test")
    said = plan.apply(json.dumps({"action": "done", "ids": ["p1", 2], "why": "test_alias passes"}))
    assert "Closed P1 as done: test_alias passes" in said
    assert "Closed P2 as done: test_alias passes" in said
    said = plan.apply(json.dumps({"action": "drop", "ids": "3", "why": "out of scope"}))
    assert "Closed P3 as dropped: out of scope" in said
    again = plan.apply(json.dumps({"action": "drop", "ids": ["P1"], "why": "changed my mind"}))
    assert again.startswith("P1 was already done: test_alias passes")
    assert [(i.status, i.why) for i in plan.items] == [
        ("done", "test_alias passes"),
        ("done", "test_alias passes"),
        ("dropped", "out of scope"),
    ]
    assert plan.listing() == "Plan: 0 open, 2 done, 1 dropped."


def test_show_lists_and_changes_nothing() -> None:
    assert Plan().apply('{"action": "show"}') == "Plan: no items yet."
    plan = _plan("write the alias fixture")
    assert plan.apply('{"action": "show"}') == (
        "Plan: 1 open, 0 done, 0 dropped.\nOpen:\nP1: write the alias fixture"
    )


# -- what compaction shows ---------------------------------------------------------------


def test_the_section_lists_open_steps_word_for_word_and_the_closed_ids() -> None:
    plan = _plan("write the alias fixture", "cover recursive CTEs")
    plan.apply(json.dumps({"action": "done", "ids": ["P1"], "why": "test_alias"}))
    assert plan.section() == (
        f"{SECTION_HEAD}1 open, 1 done, 0 dropped\n  P2: cover recursive CTEs\n  closed: P1 done"
    )
    assert Plan().section().startswith("- plan: no items yet")


def test_the_section_shows_the_oldest_open_steps_within_its_cap_and_counts_the_rest() -> None:
    long = "w" * 400
    plan = _plan(*(f"{n} {long}" for n in range(40)))
    shown = plan.section().splitlines()
    assert shown[1].startswith("  P1: 0 ")  # oldest first: the ones most at risk
    listed = [ln for ln in shown if ln.startswith("  P")]
    assert 0 < len(listed) < 40
    assert sum(len(ln) for ln in listed) // 4 <= SECTION_TOKENS
    assert (
        shown[-1]
        == f"  ... and {40 - len(listed)} more open item(s): plan with action show lists them"
    )


def test_the_state_block_carries_the_section_after_the_task() -> None:
    plan = _plan("write the alias fixture")
    block = run_state(
        plan=plan.section(),
        files=[],
        test=None,
        audit="",
        spent_tokens=0,
        token_budget=0,
        elapsed_s=0.0,
        time_budget_s=0.0,
    ).splitlines()
    assert block[1].startswith("- task:")
    assert block[2].startswith(SECTION_HEAD)
    assert block[3] == "  P1: write the alias fixture"


def _bulky(n: int) -> list[dict[str, Any]]:
    """A run's messages whose bulk is old reasoning, so stage 0 alone reaches the target."""
    messages: list[dict[str, Any]] = [
        {"role": "system", "content": "s"},
        {"role": "user", "content": "the task"},
    ]
    for i in range(n):
        messages.append(
            {
                "role": "assistant",
                "content": "",
                "reasoning_content": "r" * 4000,
                "tool_calls": [
                    {
                        "id": f"c{i}",
                        "type": "function",
                        "function": {"name": "list_dir", "arguments": "{}"},
                    }
                ],
            }
        )
        messages.append({"role": "tool", "tool_call_id": f"c{i}", "content": "ok"})
    return messages


def test_a_compaction_that_only_clears_reasoning_still_shows_the_plan() -> None:
    """Three of #132b's first compactions cleared reasoning alone and, with no note yet,
    told the model nothing: no state block, so no plan."""
    plan = _plan("write the alias fixture")
    messages = _bulky(12)
    dropped, summary = compact(
        messages, limit_tokens=8000, target_tokens=6000, pin="first", state=plan.section
    )
    assert dropped == 0
    assert summary.startswith("reasoning of ")
    note = messages[2]
    assert note["role"] == "user"
    assert "  P1: write the alias fixture" in note["content"]
    assert "Reasoning from earlier rounds was cleared" in note["content"]


def test_a_chat_compaction_that_only_clears_reasoning_adds_no_note() -> None:
    messages = _bulky(12)
    before = len(messages)
    compact(messages, limit_tokens=8000, target_tokens=6000, pin="last")
    assert len(messages) == before


# -- the reminder ------------------------------------------------------------------------


def test_the_traced_plans_are_found_as_lists_of_steps() -> None:
    planned = listed_steps(PLANNED)
    assert planned == [
        [
            "VALUES alias list",
            "the join-condition derived table",
            "a FROM that names no table",
            "UPDATE aliased/set-from-another-table",
            "output names read by clauses",
            "aliased star",
            "quoted alias",
            'a recursive CTE as known-bad documenting "a recursive CTE that names itself"? hmm',
        ]
    ]
    restated = listed_steps(RESTATED)
    assert len(restated) == 1
    assert len(restated[0]) == 6


@pytest.mark.parametrize("text", [STATUS, PROBES, READS, CODE, "1. one step only\n\nprose"])
def test_what_is_no_plan_is_not_found(text: str) -> None:
    assert listed_steps(text) == []


def test_a_list_whose_items_act_needs_no_lead() -> None:
    text = "Ok.\n- fix the E501 at line 127\n- add the VALUES fixture\n- grep the docs"
    assert listed_steps(text) == [
        ["fix the E501 at line 127", "add the VALUES fixture", "grep the docs"]
    ]
    blank = "Remaining work:\n1. fix the E501\n\n2. add the VALUES fixture\n   with a column list"
    assert listed_steps(blank) == [["fix the E501", "add the VALUES fixture"]]


def test_a_reply_listing_unplanned_steps_is_reminded_once_quoting_them() -> None:
    plan = Plan()
    told = plan.reminder(PLANNED, "")
    assert told is not None
    assert '- "VALUES alias list"' in told
    assert '- "output names read by clauses"' in told  # all eight quoted
    assert "more" not in told
    assert plan.reminder(PLANNED, "") is None  # the same list is quoted once
    assert plan.reminders == 1


def test_the_newest_list_of_a_reply_is_quoted_first() -> None:
    told = Plan().reminder(f"{RESTATED}\n\n{PLANNED}", "")
    assert told is not None
    assert told.splitlines()[1] == '- "VALUES alias list"'


def test_a_reminder_quotes_only_the_steps_the_plan_does_not_hold() -> None:
    """A restated plan beside new steps: the step the plan holds is not quoted back
    as missing, which would contradict the plan's own listing."""
    plan = _plan("add the VALUES fixture")
    told = plan.reminder(
        "Next steps:\n1. add the VALUES fixture\n2. fix the E501 at line 127\n"
        "3. write the recursive CTE test",
        "",
    )
    assert told is not None
    assert '- "fix the E501 at line 127"' in told
    assert '- "write the recursive CTE test"' in told
    assert "VALUES" not in told


def test_steps_the_plan_holds_are_not_reminded() -> None:
    plan = _plan(*listed_steps(RESTATED)[0])
    assert plan.reminder(RESTATED, "") is None
    plan = _plan("write a values source test that holds only the names it declares")
    left = plan.unplanned(listed_steps(RESTATED)[0])
    assert len(left) == 5  # restated words, not copied text, still count as planned


# -- a resumed run -----------------------------------------------------------------------


def test_a_resumed_plan_is_rebuilt_from_the_note_and_the_calls_after_it() -> None:
    plan = _plan("write the alias fixture", "cover recursive CTEs", "add a VALUES test")
    plan.apply(json.dumps({"action": "drop", "ids": ["P2"], "why": "out of scope"}))
    note = {
        "role": "user",
        "content": "[Earlier conversation compacted: 3 earlier message(s) "
        f"dropped to fit the window.]\nRun state:\n- task: t\n{plan.section()}\nRe-read files.",
    }
    later = plan.apply(json.dumps({"action": "add", "items": ["pin the clause case"]}))
    closed = plan.apply(json.dumps({"action": "done", "ids": ["P1"], "why": "test_alias"}))
    messages: list[dict[str, Any]] = [
        {"role": "system", "content": "s"},
        {"role": "user", "content": "the task"},
        note,
        {
            "role": "assistant",
            "content": "",
            "tool_calls": [
                {"id": "p1", "type": "function", "function": {"name": "plan", "arguments": "{}"}},
                {
                    "id": "x1",
                    "type": "function",
                    "function": {"name": "list_dir", "arguments": "{}"},
                },
            ],
        },
        {"role": "tool", "tool_call_id": "p1", "content": later},
        {"role": "tool", "tool_call_id": "x1", "content": "Added P9: not a plan result"},
        {
            "role": "assistant",
            "content": "",
            "tool_calls": [
                {"id": "p2", "type": "function", "function": {"name": "plan", "arguments": "{}"}},
            ],
        },
        {"role": "tool", "tool_call_id": "p2", "content": closed},
        {"role": "tool", "tool_call_id": "p2", "content": closed},  # a replay changes nothing
    ]
    resumed = Plan.from_messages(messages)
    assert [(i.id, i.status) for i in resumed.items] == [
        ("P1", "done"),
        ("P2", "dropped"),
        ("P3", "open"),
        ("P4", "open"),
    ]
    assert [i.text for i in resumed.open_items()] == ["add a VALUES test", "pin the clause case"]
    assert resumed.section() == plan.section()


def test_a_result_the_note_already_holds_is_not_replayed_twice() -> None:
    """A result kept after the note from before the compaction: its step is in the
    note already, so a resumed plan holds it once."""
    plan = Plan()
    added = plan.apply(json.dumps({"action": "add", "items": ["write the alias fixture"]}))
    note = (
        "[Earlier conversation compacted: 1 earlier message(s) dropped to fit the window.]\n"
        f"{plan.section()}"
    )
    messages: list[dict[str, Any]] = [
        {"role": "user", "content": note},
        {
            "role": "assistant",
            "content": "",
            "tool_calls": [
                {"id": "p0", "type": "function", "function": {"name": "plan", "arguments": "{}"}},
            ],
        },
        {"role": "tool", "tool_call_id": "p0", "content": added},
    ]
    resumed = Plan.from_messages(messages)
    assert [(i.id, i.text) for i in resumed.items] == [("P1", "write the alias fixture")]


def test_an_open_step_the_note_had_no_room_for_is_resumed_open() -> None:
    note = (
        "[Earlier conversation compacted: 1 earlier message(s) dropped to fit the window.]\n"
        f"{SECTION_HEAD}2 open, 0 done, 0 dropped\n  P1: shown\n  ... and 1 more open item(s): x"
    )
    plan = Plan.from_messages([{"role": "user", "content": "t"}, {"role": "user", "content": note}])
    assert [(i.id, i.text, i.status) for i in plan.items] == [
        ("P1", "shown", "open"),
        ("P2", UNRECOVERED, "open"),
    ]
    note = note.replace("  P1: shown", "  P2: shown")
    plan = Plan.from_messages([{"role": "user", "content": note}])
    assert [(i.id, i.text) for i in plan.open_items()] == [("P1", UNRECOVERED), ("P2", "shown")]


# -- through a run -----------------------------------------------------------------------


def _plan_call(call_id: str, **arguments: Any) -> list[Any]:
    return [call(PLAN_TOOL, call_id, **arguments)]


def test_a_finish_with_open_steps_is_answered_once_and_the_run_names_them(
    repo: Path,  # noqa: F811
) -> None:
    client = Scripted(
        [
            _plan_call("p1", action="add", items=["write the alias fixture", "cover CTEs"]),
            _plan_call("p2", action="done", ids=["P2"], why="test_cte passes"),
            finish(call_id="f1"),
            finish(call_id="f2"),
        ]
    )
    result = auto(repo, client)
    answered = [m for m in client.asked[3]["messages"] if m.get("tool_call_id") == "f1"]
    assert answered[0]["content"] == PLAN_OPEN.format(n=1, items="P1: write the alias fixture")
    assert result.outcome == "finished"
    record = sidecar(result)
    assert record["plan"] == [
        {"id": "P1", "text": "write the alias fixture", "status": "open", "why": ""},
        {"id": "P2", "text": "cover CTEs", "status": "done", "why": "test_cte passes"},
    ]
    assert record["plan_reminders"] == 0
    assert outcome_span(result).detail.endswith("; open plan items: P1")
    message = git(repo, "log", "-1", "--format=%B", result.branch)
    assert (
        "Plan items still open at the end (neither done nor dropped):\n"
        "- P1: write the alias fixture" in message
    )


def test_a_finish_with_every_step_closed_is_taken_at_once(repo: Path) -> None:  # noqa: F811
    client = Scripted(
        [
            _plan_call("p1", action="add", items=["write the alias fixture"]),
            _plan_call("p2", action="drop", ids=["P1"], why="the task does not ask it"),
            finish(),
        ]
    )
    result = auto(repo, client)
    assert result.outcome == "finished"
    assert len(client.asked) == 3
    assert "Plan items still open" not in git(repo, "log", "-1", "--format=%B", result.branch)


def test_a_reply_that_lists_steps_is_reminded_on_its_last_result(repo: Path) -> None:  # noqa: F811
    listing = StreamToken(stream="reasoning", text=PLANNED)
    client = Scripted(
        [
            [listing, call("list_dir", "l1", path="."), call("list_dir", "l2", path=".")],
            _plan_call("p1", action="add", items=["output names read by clauses"]),
            finish(),
        ]
    )
    result = auto(repo, client)
    results = [m for m in client.asked[1]["messages"] if m["role"] == "tool"]
    assert "[plan]" not in results[0]["content"]
    assert results[1]["content"].endswith("it is a reminder, not a check.")
    assert '- "output names read by clauses"' in results[1]["content"]
    spans = [s for s in read_spans(result.journal) if s.name == PLAN_REMINDER_SPAN]
    assert len(spans) == 1
    assert spans[0].detail.startswith("[plan] Your last reply listed")
    assert sidecar(result)["plan_reminders"] == 1


def test_a_reply_with_no_call_carries_its_reminder_in_the_nudge(repo: Path) -> None:  # noqa: F811
    client = Scripted([[StreamToken(stream="content", text=PLANNED)], finish()])
    auto(repo, client)
    nudge = client.asked[1]["messages"][-1]
    assert nudge["role"] == "user"
    assert "[plan] Your last reply listed" in nudge["content"]


def test_a_step_added_before_a_compaction_is_shown_word_for_word_after_it(tmp_path: Path) -> None:
    root = _repo(tmp_path / "repo", src=False)
    step = "write the fixture for output names read by ORDER BY, GROUP BY and HAVING"
    client = Counting(
        [_plan_call("p1", action="add", items=[step]), *_talks(60), finish(), finish()],
        ratio=1.22,
    )
    options = AutoOptions(
        task="make add add", repo=root, run_id="pl", arm="E", context_tokens=131_072
    )
    result = run_auto(options, client)  # type: ignore[arg-type]
    assert [s for s in read_spans(result.journal) if s.name == COMPACTION_SPAN]
    assert client.last is not None
    notes = [
        m for m in client.last if str(m.get("content", "")).startswith("[Earlier conversation")
    ]
    assert f"  P1: {step}" in notes[0]["content"]


def test_with_the_plan_off_there_is_no_tool_no_prompt_and_no_record(repo: Path) -> None:  # noqa: F811
    client = Scripted([_plan_call("p1", action="add", items=["x"]), finish()])
    result = auto(repo, client, plan=False)
    names = [t["function"]["name"] for t in client.asked[0]["tools"]]
    assert PLAN_TOOL not in names
    assert PLAN_PROMPT not in client.asked[0]["messages"][0]["content"]
    results = [m for m in client.asked[1]["messages"] if m["role"] == "tool"]
    assert results[0]["content"] == "error: unknown tool 'plan'"
    record = sidecar(result)
    assert "plan" not in record
    assert record["plan_tool"] is False


def test_with_the_plan_on_the_tool_and_its_rules_are_offered(repo: Path) -> None:  # noqa: F811
    client = Scripted([finish()])
    auto(repo, client)
    names = [t["function"]["name"] for t in client.asked[0]["tools"]]
    assert PLAN_TOOL in names
    assert PLAN_PROMPT in client.asked[0]["messages"][0]["content"]


# -- what a person sees --------------------------------------------------------------------


@pytest.mark.parametrize(
    ("arguments", "labels"),
    [
        (
            {"action": "add", "items": ["a", "b"]},
            (
                "Adding 2 steps to the plan",
                "Added 2 steps to the plan",
                "Failed to add to the plan",
            ),
        ),
        ({"action": "add", "items": "a"}, ("Adding 1 step to the plan",) * 1),
        ({"action": "done", "ids": ["P1", "P2"], "why": "t"}, ("Closing P1, P2 as done",)),
        ({"action": "drop", "ids": ["P3"], "why": "t"}, ("Dropping P3 from the plan",)),
        ({"action": "show"}, ("Reading the plan", "Read the plan", "Failed to read the plan")),
        ({"action": "zap"}, ("Updating the plan", "Updated the plan", "Failed to update the plan")),
    ],
)
def test_a_plan_call_is_labelled_by_what_it_does(
    arguments: dict[str, Any], labels: tuple[str, ...]
) -> None:
    said = describe(PLAN_TOOL, json.dumps(arguments))
    assert said[: len(labels)] == labels


def test_a_reminder_reads_as_one_session_line(repo: Path) -> None:  # noqa: F811
    client = Scripted([[StreamToken(stream="content", text=PLANNED)], finish()])
    result = auto(repo, client)
    lines = [session_line(e) for e in read_entries(result.journal)]
    said = [ln.text for ln in lines if ln is not None and "listed step" in ln.text]
    assert said == ["reminded of 8 listed step(s) not in its plan"]


def test_a_diff_in_the_reasoning_is_no_list_of_steps() -> None:
    diff = "Next, the change:\n- return a - b\n+ return a + b\n+ add(2, 2)\n"
    assert listed_steps(diff) == []
