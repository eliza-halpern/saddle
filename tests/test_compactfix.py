"""COMPACTFIX: what an autonomous run keeps once its context is compacted.

Each test names the recommendation it pins (R1..R8, COMPACTRES's report)
and holds both halves: a known-good instance the contract keeps and a
known-bad one it must not. The runs are real `run_auto` runs over a git
repo, the only caller that sets `changed_files` and delivers audits, with a
scripted model; `context_tokens=40_000` puts the compaction limit near
14k estimated tokens, so a few reads of an ~8k-token file force it.
"""

from __future__ import annotations

import io
import json
import re
import shlex
import sys
from pathlib import Path
from typing import Any, cast

import pytest

from saddle import cli
from saddle.auto import AutoOptions, AutoResult, run_auto
from saddle.engine import AUTO_NUDGE, AutoRun, RunBudget, TurnOptions, run_turn
from saddle.events import Compaction, Event
from saddle.journal import COMPACTION_SPAN, read_spans, verify_journal
from saddle.memory import (
    ASK_USER,
    ELIDED,
    FAILURE_LINES,
    KEEP_RECENT,
    NOTE_HEAD,
    REREAD,
    compact,
    estimate_tokens,
    is_note,
    run_state,
)
from saddle.transcript import session_line
from saddle.vllm import StreamToken, VllmClient
from tests.test_auto import Scripted, call, finish, git, namespace, repo  # noqa: F401  (fixture)
from tests.test_feed import EDIT_COMMENT, FakeAuditor
from tests.test_keep_reasoning import _render

BIG = "".join(f"def helper_{i}(x):\n    return x * {i}  # filler line {i}\n" for i in range(600))
"""~33 KB: one read of it is ~8k estimated tokens."""
TASK = "TASK-5e1d: make add() in calc.py return the sum instead of the difference"
PY = shlex.quote(sys.executable)
PYTEST = f"{PY} -m pytest -q -p no:cacheprovider tests"
CTX = 40_000


def _commit_big(repo: Path) -> None:  # noqa: F811
    (repo / "big.py").write_text(BIG)
    git(repo, "add", "big.py")
    git(repo, "commit", "-q", "-m", "big")


def _reads(n: int, prefix: str = "r") -> list[list[Any]]:
    return [[call("read_file", f"{prefix}{i}", path="big.py")] for i in range(n)]


def _auto(
    repo: Path,  # noqa: F811
    client: Any,
    *,
    arm: str = "E",
    events: list[Event] | None = None,
    **kwargs: Any,
) -> AutoResult:
    options = AutoOptions(
        task=TASK,
        repo=repo,
        run_id=kwargs.pop("run_id", "cf"),
        arm=arm,  # type: ignore[arg-type]
        context_tokens=CTX,
        token_budget=10**7,
        **kwargs,
    )
    sink = events.append if events is not None else None
    return run_auto(options, cast(VllmClient, client), on_event=sink)


def _note(request: dict[str, Any]) -> str:
    notes = [m for m in request["messages"] if is_note(m)]
    assert len(notes) == 1, f"expected one note, got {len(notes)}"
    return str(notes[0]["content"])


def _last_note(client: Scripted) -> str:
    return _note(next(r for r in reversed(client.asked) if any(map(is_note, r["messages"]))))


def _field(note: str, name: str) -> str:
    """The text of one state-block field, continuation lines included."""
    lines = note.splitlines()
    start = next(i for i, ln in enumerate(lines) if ln.startswith(f"- {name}:"))
    body = [lines[start]]
    for ln in lines[start + 1 :]:
        if not ln.startswith("  "):
            break
        body.append(ln)
    return "\n".join(body)


# -- R1: compact before every request; seal it; print it -----------------------


def test_r1_each_compaction_is_sealed_and_printed_by_saddle_auto(
    repo: Path,  # noqa: F811
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _commit_big(repo)
    monkeypatch.setattr(
        cli, "AutoOptions", lambda **kw: AutoOptions(**{**kw, "context_tokens": CTX})
    )
    out = io.StringIO()
    client = Scripted([*_reads(8), finish()])
    code = cli.run_auto_command(namespace(repo), cast(VllmClient, client), stdout=out)
    assert code == 0
    printed = [ln for ln in out.getvalue().splitlines() if "context compacted:" in ln]
    journal = next(
        Path(ln.removeprefix("ledger "))
        for ln in out.getvalue().splitlines()
        if ln.startswith("ledger ")
    )
    sealed = [s for s in read_spans(journal) if s.name == COMPACTION_SPAN]
    assert len(sealed) >= 2  # more than once: per request, not once per turn
    assert len(printed) == len(sealed)
    counts = json.loads(sealed[0].argv[1])
    assert counts["n"] == 1
    assert counts["estimate_after"] < counts["estimate_before"]
    assert verify_journal(journal) == []  # the new span does not break the chain

    # known-bad: a run that never outgrows the window prints and seals nothing
    out = io.StringIO()
    code = cli.run_auto_command(namespace(repo), cast(VllmClient, Scripted([finish()])), stdout=out)
    assert "context compacted" not in out.getvalue()


def test_r1_the_task_card_shows_a_sealed_compaction_as_one_line(repo: Path) -> None:  # noqa: F811
    _commit_big(repo)
    result = _auto(repo, Scripted([*_reads(8), finish()]))
    spans = read_spans(result.journal)
    sealed = [s for s in spans if s.name == COMPACTION_SPAN]
    assert sealed
    line = session_line(sealed[0])
    assert line is not None
    assert line.text.startswith("context compacted · ")
    assert "\n" not in line.text
    # known-bad: it is not read as the run's outcome (the `auto:` branch)
    assert "finished" not in line.text
    assert line.tone == "info"


# -- R2: the task is pinned verbatim (auto and the chat Task lane) ------------


def test_r2_the_task_is_the_first_user_message_of_every_compacted_request(
    repo: Path,  # noqa: F811
) -> None:
    _commit_big(repo)
    client = Scripted([*_reads(8), finish()])
    _auto(repo, client)
    compacted = [r for r in client.asked if any(map(is_note, r["messages"]))]
    assert compacted
    for request in compacted:
        users = [m for m in request["messages"] if m["role"] == "user"]
        assert users[0] == {"role": "user", "content": TASK}
        # the note follows the task: system, task, note, then the recent work
        assert [m["role"] for m in request["messages"][:3]] == ["system", "user", "user"]
        assert is_note(request["messages"][2])


def test_r2_a_nudge_is_not_pinned_only_the_task_is() -> None:
    messages: list[dict[str, Any]] = [
        {"role": "system", "content": "s"},
        {"role": "user", "content": TASK},
    ]
    for i in range(10):
        messages.append({"role": "assistant", "content": f"thinking {i} " + "y" * 4000})
        messages.append({"role": "user", "content": AUTO_NUDGE})
    compact(messages, limit_tokens=3000, pin="first", hint=REREAD)
    kept = [m["content"] for m in messages if m["role"] == "user" and not is_note(m)]
    assert kept[0] == TASK
    assert len(kept) < 11  # early nudges went; the task did not


def test_r2_an_auto_run_with_nudges_still_pins_the_task(repo: Path) -> None:  # noqa: F811
    """The engine's pin choice, not only `compact`'s: nudges are user messages too."""
    _commit_big(repo)
    talk = [StreamToken(stream="content", text="Still looking.")]
    rounds: list[Any] = []
    for step in _reads(8):
        rounds += [step, talk]
    client = Scripted([*rounds, finish()])
    _auto(repo, client)
    compacted = [r for r in client.asked if any(map(is_note, r["messages"]))]
    assert compacted
    nudged = [r for r in compacted if any(m["content"] == AUTO_NUDGE for m in r["messages"])]
    assert nudged  # known-bad present: a later user message the pin could wrongly pick
    for request in compacted:
        users = [m for m in request["messages"] if m["role"] == "user"]
        assert users[0] == {"role": "user", "content": TASK}


def test_r2_a_chat_keeps_the_question_it_is_answering(tmp_path: Path) -> None:
    """A chat turn with many tool rounds: the question falls out of the tail."""
    (tmp_path / "big.py").write_text(BIG)
    question = "QUESTION-9c1: which helper returns x * 7?"
    history: list[dict[str, Any]] = [{"role": "system", "content": "You are a persona."}]
    for i in range(3):
        history += [
            {"role": "user", "content": f"old question {i} " + "q" * 8000},
            {"role": "assistant", "content": f"old answer {i}"},
        ]
    client = Scripted([*_reads(6), [call("read_file", "z", path="big.py")]], tail=[])
    options = TurnOptions(
        workdir=tmp_path,
        journal=tmp_path / "j.jsonl",
        system_prompt="You are a persona.",
        context_tokens=CTX,
    )
    events = list(run_turn(cast(VllmClient, client), history, question, options, turn=9))
    assert any(isinstance(e, Compaction) and e.dropped_messages for e in events)
    late = client.asked[-1]["messages"]
    assert {"role": "user", "content": question} in late
    # known-bad: the old questions are what went
    assert not any("old question 0" in str(m.get("content")) for m in late if not is_note(m))
    # and in chat the note still offers the user, not a re-read
    note = next(m for m in late if is_note(m))
    assert ASK_USER in note["content"]
    assert REREAD not in note["content"]
    _render({"messages": late, "tools": None, "reasoning_effort": "medium"})


# -- R3: a tool call and all its results go as one unit -------------------------


def _unpaired(messages: list[dict[str, Any]]) -> list[str]:
    """Orphan tool messages, and tool calls whose result is missing."""
    errors = []
    open_ids: set[str] = set()
    for i, m in enumerate(messages):
        if m["role"] == "tool":
            if m.get("tool_call_id") not in open_ids:
                errors.append(f"orphan tool message at {i}")
            open_ids.discard(m.get("tool_call_id"))
            continue
        if open_ids:
            errors.append(f"call(s) {sorted(open_ids)} have no result before {i}")
        open_ids = (
            {c["id"] for c in m.get("tool_calls") or []} if m["role"] == "assistant" else set()
        )
    if open_ids:
        errors.append(f"call(s) {sorted(open_ids)} have no result at the end")
    return errors


def test_r3_a_batch_of_calls_is_never_split_from_its_results() -> None:
    for limit in range(2000, 30000, 1231):
        messages: list[dict[str, Any]] = [
            {"role": "system", "content": "s"},
            {"role": "user", "content": TASK},
        ]
        for i in range(8):
            ids = [f"b{i}a", f"b{i}b", f"b{i}c"]
            messages.append(
                {
                    "role": "assistant",
                    "content": "x" * 3000,
                    "tool_calls": [
                        {
                            "id": c,
                            "type": "function",
                            "function": {"name": "read_file", "arguments": "{}"},
                        }
                        for c in ids
                    ],
                }
            )
            messages += [{"role": "tool", "tool_call_id": c, "content": "ok " * 400} for c in ids]
        assert _unpaired(messages) == []  # harness: the fixture is well paired
        compact(messages, limit_tokens=limit, pin="first", hint=REREAD)
        assert _unpaired(messages) == [], limit


# -- R4: one note, user role, renders under the served template ----------------


def test_r4_repeated_compaction_leaves_one_user_note_and_one_system_message(
    repo: Path,  # noqa: F811
) -> None:
    _commit_big(repo)
    client = Scripted([*_reads(10), finish()])
    result = _auto(repo, client)
    sealed = [s for s in read_spans(result.journal) if s.name == COMPACTION_SPAN]
    assert len(sealed) >= 3
    for request in client.asked:
        roles = [m["role"] for m in request["messages"]]
        assert roles.count("system") == 1
        assert roles[0] == "system"
        assert sum(map(is_note, request["messages"])) <= 1
        _render({"messages": request["messages"], "tools": None, "reasoning_effort": "medium"})
    # the note counts every message dropped so far, not only the latest pass
    counts = [json.loads(s.argv[1])["dropped"] for s in sealed]
    total = int(re.search(r"compacted: (\d+) earlier", _last_note(client)).group(1))  # type: ignore[union-attr]
    assert total == sum(counts)


# -- R5: an autonomous run is never told to ask the user ------------------------


def test_r5_the_auto_note_says_reread_and_never_ask(repo: Path) -> None:  # noqa: F811
    _commit_big(repo)
    client = Scripted([*_reads(8), finish()])
    _auto(repo, client)
    note = _last_note(client)
    assert REREAD in note
    assert "ask the user" not in note.lower()


# -- R7: the state block, field by field ---------------------------------------


def test_r7_files_lists_what_differs_from_the_starting_commit_not_what_was_touched(
    repo: Path,  # noqa: F811
) -> None:
    _commit_big(repo)
    client = Scripted(
        [
            [call("write_file", "w", path="src/util_math.py", content="X = 1\n")],
            [call("edit_file", "e1", path="calc.py", old="a - b", new="a + b")],
            [call("edit_file", "e2", path="calc.py", old="a + b", new="a - b")],  # reverted
            *_reads(8),
            finish(),
        ]
    )
    _auto(repo, client)
    notes = [_note(r) for r in client.asked if any(map(is_note, r["messages"]))]
    assert notes
    # every note, including the one from the pass that dropped the calc.py edits
    for note in notes:
        files = _field(note, "files changed since the run's starting commit")
        assert files.endswith(": src/util_math.py")  # known-good: still changed
        assert "calc.py" not in note  # known-bad: edited, then put back, named nowhere


def test_r7_files_says_none_when_nothing_is_changed(repo: Path) -> None:  # noqa: F811
    _commit_big(repo)
    client = Scripted([*_reads(8), finish()])
    _auto(repo, client)
    assert _field(_last_note(client), "files changed since the run's starting commit").endswith(
        ": none"
    )


def test_r7_the_last_test_run_is_the_newest_and_keeps_its_failed_lines(
    repo: Path,  # noqa: F811
) -> None:
    _commit_big(repo)
    failing = [call("run_command", "t1", command=PYTEST)]
    fix = [call("edit_file", "e1", path="calc.py", old="a - b", new="a + b")]
    passing = [call("run_command", "t2", command=PYTEST)]
    other = [call("run_command", "o1", command="true")]  # not a test run
    client = Scripted([failing, *_reads(6, "a"), fix, passing, other, *_reads(6, "b"), finish()])
    _auto(repo, client)
    fix_at = next(
        i
        for i, r in enumerate(client.asked)
        if any(c["id"] == "e1" for m in r["messages"] for c in m.get("tool_calls") or [])
    )
    early = _field(_note(client.asked[fix_at]), "last test run")
    assert PYTEST in early
    assert "exit 1" in early
    assert "FAILED tests/test_calc.py::test_add" in early  # known-good: the failure list
    late = _field(_last_note(client), "last test run")
    assert PYTEST in late
    assert "exit 0" in late
    assert "1 passed" in late
    assert "FAILED" not in late  # known-bad: the older run's failures are gone
    assert "-> exit 0" in late
    assert "true" not in late.split("->")[0]


def test_r7_a_delivered_audit_from_the_real_feed_is_in_the_note(repo: Path) -> None:  # noqa: F811
    _commit_big(repo)
    client = Scripted(
        [[EDIT_COMMENT], [call("finish", "f0", summary="done")], *_reads(8)],
        tail=[call("finish", "f9", summary="done")],
    )
    _auto(repo, client, arm="E+A+F", auditor_factory=lambda *a: FakeAuditor())
    audit = _field(_last_note(client), "latest audit delivered to you")
    assert "calc.py:3: return a - b" in audit


class StubFeed:
    """An audit feed whose deliveries are scripted by round, so ordering is exact."""

    def __init__(self, deliveries: dict[int, str]) -> None:
        self.deliveries = deliveries
        self.calls = 0
        self.checks: list[object] = []

    def before_tool(self, name: str) -> None:
        return None

    def after_tool(self, name: str, ok: bool) -> None:
        return None

    def collect(self) -> str:
        self.calls += 1
        return self.deliveries.get(self.calls, "")

    def final(self) -> tuple[bool, str]:
        return True, ""

    def check(self) -> str:
        return ""

    def unresolved(self) -> list[dict[str, object]]:
        return []

    def close(self) -> None:
        return None

    def last(self) -> dict[str, object] | None:
        return None

    def unchanged(self) -> bool:
        return False

    def accepted_unchanged(self) -> bool:
        return False

    def waivers(self) -> list[str]:
        return []


def test_r7_a_newer_delivered_audit_replaces_the_older_one(tmp_path: Path) -> None:
    (tmp_path / "big.py").write_text(BIG)
    old = "[audit checkpoint 1 on tree aaa: FAIL]\n- tests (tier 1): fail, code-wrong: OLD-FINDING"
    new = (
        "[audit checkpoint 2 on tree bbb: FAIL]\n- tests (tier 1): fail, code-wrong: NEW-FINDING\n"
        "(not proven, does not refuse) mutation (tier 2): SURVIVOR-1"
    )
    feed = StubFeed({1: old, 5: new})
    run = AutoRun(
        budget=RunBudget(time_s=600, tokens=10**7),
        run_span="s",
        feed=feed,
        changed_files=lambda: ["calc.py"],
    )
    options = TurnOptions(
        workdir=tmp_path,
        journal=tmp_path / "j.jsonl",
        auto=run,
        system_prompt="sys",
        context_tokens=CTX,
    )
    client = Scripted([*_reads(10), finish()])
    list(run_turn(cast(VllmClient, client), [], TASK, options, turn=1))
    before = next(
        _note(r)
        for r in client.asked
        if any(map(is_note, r["messages"])) and "OLD-FINDING" in _note(r)
    )
    assert "NEW-FINDING" not in before  # harness: the old one was the latest for a while
    note = _last_note(client)
    audit = _field(note, "latest audit delivered to you")
    assert "NEW-FINDING" in audit
    assert "OLD-FINDING" not in note  # known-bad: the older audit is replaced
    assert "SURVIVOR-1" in _field(note, "not proven")
    assert "SURVIVOR-1" not in audit


def test_r7_a_withheld_audit_never_reaches_the_note(repo: Path) -> None:  # noqa: F811
    _commit_big(repo)
    client = Scripted(
        [[EDIT_COMMENT], [call("run_command", "c", command="true")], *_reads(8), finish()]
    )
    result = _auto(repo, client, arm="E+A", auditor_factory=lambda *a: FakeAuditor())
    note = _last_note(client)
    assert _field(note, "latest audit delivered to you").endswith(": none")
    finding = "calc.py:3: return a - b"
    assert all(finding not in json.dumps(r["messages"]) for r in client.asked)
    # harness: the audit did run and found it; it was withheld, not absent
    withheld = [s for s in read_spans(result.journal) if s.name == "audit:withheld"]
    assert any(finding in s.detail for s in withheld)


def test_r7_the_budget_is_the_running_total_at_that_request(repo: Path) -> None:  # noqa: F811
    _commit_big(repo)
    client = Scripted([*_reads(8), finish()])
    result = _auto(repo, client)
    spends = [s for s in read_spans(result.journal) if s.name == "auto:spend"]
    totals = [int(re.search(r"; (\d+) of", s.detail).group(1)) for s in spends]  # type: ignore[union-attr]
    checked = 0
    for k, request in enumerate(client.asked):
        if not any(map(is_note, request["messages"])):
            continue
        budget = _field(_note(request), "budget")
        # request k is sent after k rounds were charged
        assert budget.startswith(f"- budget: {totals[k - 1]} of {10**7} generated tokens used")
        checked += 1
    assert checked >= 2
    assert totals[0] != totals[-1]  # harness: the total moves, so a stale block would show


def test_r7_run_state_fields_known_good_and_known_bad() -> None:
    audit = (
        "[audit finish on tree abc: FAIL]\n"
        "- tests (tier 1): fail, code-wrong: calc.py:3\n"
        "(not proven, does not refuse) mutation (tier 2): 2 survivors in calc.py\n"
        "(3 other check(s) passed or not applicable)"
    )
    state = run_state(
        files=["a.py"],
        test=("pytest -q", "exit 1\n....F\nFAILED t.py::x - boom\nE   noise\n1 failed in 0.1s\n"),
        audit=audit,
        spent_tokens=250,
        token_budget=1000,
        elapsed_s=90.4,
        time_budget_s=600,
    )
    unproven = _field(state, "not proven")
    assert "2 survivors in calc.py" in unproven
    assert "2 survivors" not in _field(state, "latest audit delivered to you")
    test = _field(state, "last test run")
    assert "pytest -q -> exit 1; 1 failed in 0.1s" in test
    assert "FAILED t.py::x - boom" in test
    assert "E   noise" not in test
    assert "....F" not in test
    assert _field(state, "budget") == (
        "- budget: 250 of 1000 generated tokens used, 750 left; 90s of 600s used, 510s left"
    )
    empty = run_state(
        files=[],
        test=None,
        audit="",
        spent_tokens=0,
        token_budget=1,
        elapsed_s=0,
        time_budget_s=1,
    )
    assert _field(empty, "not proven") == "- not proven: none"
    assert _field(empty, "last test run") == "- last test run: none yet"
    assert _field(empty, "latest audit delivered to you").endswith(": none")


# -- R8: stage 1 keeps the failure list, and only it --------------------------


def test_r8_stage1_keeps_failed_lines_elides_the_rest_and_caps_them() -> None:
    body = (
        "HEAD\n" * 400
        + "".join(f"FAILED t.py::case_{i}\nE   detail {i}\n" for i in range(FAILURE_LINES + 20))
        + "TAIL\n" * 200
    )
    messages: list[dict[str, Any]] = [
        {"role": "system", "content": "s"},
        {"role": "tool", "tool_call_id": "1", "content": body},
    ]
    messages += [{"role": "user", "content": f"recent {i}"} for i in range(KEEP_RECENT)]
    compact(messages, limit_tokens=estimate_tokens(messages) - 100)
    kept = str(messages[1]["content"])
    assert ELIDED in kept
    assert "FAILED t.py::case_0\n" in kept  # known-good
    assert "E   detail 0" not in kept  # known-bad: other middle lines are elided
    assert kept.count("FAILED ") == FAILURE_LINES  # capped


def test_r8_an_elided_result_is_not_elided_again() -> None:
    body = "A" * 5000 + "\nFAILED t.py::x\n" + "B" * 5000
    once = compact_once(body)
    assert once != body
    assert ELIDED in once  # known-good: the first pass did elide it
    assert "FAILED t.py::x" in once
    assert compact_once(once) == once  # known-bad: a second pass re-elides the marker


def _batch(first: str) -> list[dict[str, Any]]:
    """One call whose results run into the protected tail, so stage 2 cannot
    drop it and stage 1 is the only thing that may touch `first`."""
    ids = [str(i) for i in range(KEEP_RECENT + 1)]
    calls = [{"id": i, "function": {"name": "read_file", "arguments": "{}"}} for i in ids]
    messages: list[dict[str, Any]] = [{"role": "assistant", "content": "", "tool_calls": calls}]
    messages.append({"role": "tool", "tool_call_id": ids[0], "content": first})
    messages += [{"role": "tool", "tool_call_id": i, "content": "T" * 1500} for i in ids[1:]]
    return messages


def compact_once(body: str) -> str:
    messages = _batch(body)
    compact(messages, limit_tokens=1)
    assert messages[1]["role"] == "tool"  # the result under test is still there
    return str(messages[1]["content"])


def test_r3_a_call_whose_results_reach_the_tail_is_kept_whole() -> None:
    messages = _batch("F" * 3000)
    tail = [dict(m) for m in messages[-KEEP_RECENT:]]
    dropped, _ = compact(messages, limit_tokens=1)
    assert dropped == 0
    assert messages[-KEEP_RECENT:] == tail  # known-good: the protected tail is untouched
    assert messages[0]["role"] == "assistant"  # known-bad: the call went with its tail
    assert len(messages) == KEEP_RECENT + 2


# -- the note is its own, and is rebuilt rather than stacked -------------------


def test_the_note_is_rebuilt_not_stacked_and_carries_the_count() -> None:
    def history() -> list[dict[str, Any]]:
        return [{"role": "system", "content": "s"}, {"role": "user", "content": TASK}]

    messages = history()
    for i in range(6):
        messages += [
            {"role": "assistant", "content": "y" * 4000},
            {"role": "user", "content": f"n{i}"},
        ]
    calls = iter(["state one", "state two"])
    first, _ = compact(
        messages, limit_tokens=4000, pin="first", hint=REREAD, state=lambda: next(calls)
    )
    for i in range(6):
        messages += [
            {"role": "assistant", "content": "z" * 4000},
            {"role": "user", "content": f"m{i}"},
        ]
    second, _ = compact(
        messages, limit_tokens=4000, pin="first", hint=REREAD, state=lambda: next(calls)
    )
    notes = [m for m in messages if is_note(m)]
    assert len(notes) == 1
    assert notes[0]["content"].startswith(f"{NOTE_HEAD}{first + second} earlier message(s)")
    assert notes[0]["content"].count(NOTE_HEAD) == 1  # the old note was replaced, not dropped
    assert "state two" in notes[0]["content"]
    assert "state one" not in notes[0]["content"]


def test_a_stage1_only_pass_refreshes_an_existing_note_state() -> None:
    messages: list[dict[str, Any]] = [
        {"role": "system", "content": "s"},
        {"role": "user", "content": TASK},
        {
            "role": "user",
            "content": f"{NOTE_HEAD}4 earlier message(s) dropped to fit the window.]\n"
            "Dropped topics: t1\nold state\n" + REREAD,
        },
        {"role": "tool", "tool_call_id": "1", "content": "Z" * 40_000},
    ]
    messages += [{"role": "user", "content": f"recent {i}"} for i in range(KEEP_RECENT)]
    dropped, summary = compact(
        messages, limit_tokens=2000, pin="first", hint=REREAD, state=lambda: "new state"
    )
    assert (dropped, summary) == (0, "large tool results elided")
    note = str(messages[2]["content"])
    assert note.startswith(f"{NOTE_HEAD}4 earlier")
    assert "Dropped topics: t1" in note
    assert "new state" in note
    assert "old state" not in note


def test_a_chat_note_names_files_edited_in_the_dropped_part() -> None:
    messages: list[dict[str, Any]] = [{"role": "system", "content": "s"}]
    messages += [
        {"role": "user", "content": "please edit"},
        {
            "role": "assistant",
            "content": "",
            "tool_calls": [
                {
                    "id": "e",
                    "type": "function",
                    "function": {"name": "edit_file", "arguments": json.dumps({"path": "p.py"})},
                },
                {
                    "id": "w",
                    "type": "function",
                    "function": {"name": "write_file", "arguments": "{not json"},
                },
            ],
        },
        {"role": "tool", "tool_call_id": "e", "content": "ok" + "x" * 8000},
        {"role": "tool", "tool_call_id": "w", "content": "error"},
    ]
    messages += [{"role": "user", "content": f"recent {i}"} for i in range(KEEP_RECENT)]
    compact(messages, limit_tokens=100)
    note = next(m for m in messages if is_note(m))
    assert "Files edited in the dropped part: p.py" in note["content"]
    assert ASK_USER in note["content"]


def test_r7_a_surfaced_finish_findings_count_as_delivered(tmp_path: Path) -> None:
    """INTEG6: FEEDFIX item 7 delivers an accepted finish's not-proven findings
    to the model; the state block's "latest audit delivered" must hold them,
    as it does a refused finish's. A plain accepted finish delivers nothing."""
    from saddle.engine import FINISH_SURFACED, FINISH_TOOL, _note_round
    from saddle.vllm import ToolCall

    run = AutoRun(budget=RunBudget(time_s=600, tokens=10**7), run_span="s")
    done = ToolCall(id="1", name=FINISH_TOOL, arguments=json.dumps({"summary": "x"}))
    _note_round(run, done, "finished. Your summary is recorded as narrative, not as evidence.")
    assert run.delivered_audit == ""  # known-good: nothing was delivered
    _note_round(run, done, f"{FINISH_SURFACED}(not proven) mutation (tier 2): SURVIVOR-7")
    assert "SURVIVOR-7" in run.delivered_audit  # known-bad before the fix: stays ""
    assert FINISH_SURFACED.strip() not in run.delivered_audit  # the findings, not the preamble
