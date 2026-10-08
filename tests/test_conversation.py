"""A run's conversation log rebuilds each request exactly as it was sent (#164).

The ledger keeps a tool result cut to 500 characters, so no request of a run could be
rebuilt from it, and triage and resume read the logging relay's copies instead, which
only a measured run has. Known-good: every request replayed from the log equals what
the model was sent, through a compaction and a message changed in place, with only
what changed written. Known-bad: a log cut short read as a shorter run, a request the
log does not hold read as an empty one, and a log that cannot be written stopping the
run.
"""

from __future__ import annotations

import json
import subprocess
from collections.abc import Iterator
from itertools import pairwise
from pathlib import Path
from typing import Any, cast

import pytest

from saddle.auto import AutoOptions, run_auto
from saddle.conversation import (
    CONVERSATION_LOG,
    ConversationError,
    ConversationLog,
    Request,
    ending,
    request_at,
    requests,
)
from saddle.engine import TurnOptions, run_turn
from saddle.events import Compaction
from saddle.journal import STAR
from saddle.vllm import StreamToken, ToolCall, VllmClient

SYSTEM: dict[str, Any] = {"role": "system", "content": "be brief"}
ASK: dict[str, Any] = {"role": "user", "content": "read the file"}
CALL: dict[str, Any] = {
    "role": "assistant",
    "content": "",
    "tool_calls": [{"id": "c1", "function": {"name": "read_file", "arguments": "{}"}}],
}
RESULT: dict[str, Any] = {"role": "tool", "tool_call_id": "c1", "content": "exit 0\n"}
TOOL_A = {"type": "function", "function": {"name": "read_file"}}
TOOL_B = {"type": "function", "function": {"name": "recall"}}


def log_at(tmp_path: Path) -> ConversationLog:
    return ConversationLog(tmp_path / CONVERSATION_LOG, clock=lambda: 1.0)


def sent(log: ConversationLog, *rounds: list[dict[str, Any]]) -> None:
    for messages in rounds:
        log.record(messages, max_tokens=100, tools=[TOOL_A])


def numbers_until_cut(path: Path) -> tuple[list[int], str]:
    """The requests read whole before the log's error, and the error."""
    whole: list[int] = []
    try:
        for request in requests(path):
            whole.append(request.number)
    except ConversationError as cut:
        return whole, str(cut)
    pytest.fail(f"{path} read whole: {whole}")


# -- the log -----------------------------------------------------------------------


def test_each_request_is_rebuilt_exactly_and_only_its_new_messages_are_written(
    tmp_path: Path,
) -> None:
    log = log_at(tmp_path)
    first, second = [SYSTEM, ASK], [SYSTEM, ASK, CALL, RESULT]
    sent(log, first, second)
    assert log.requests == 2
    assert log.failed is None
    one, two = request_at(log.path, 1), request_at(log.path, 2)
    assert (one.messages, one.kept, one.max_tokens, one.tools) == (first, 0, 100, [TOOL_A])
    assert (two.messages, two.kept, two.added) == (second, 2, [CALL, RESULT])
    assert one.sent == two.sent == 1.0
    # two first lines, and each message once
    assert len(log.path.read_text().splitlines()) == 2 + 4


def test_a_request_that_adds_nothing_is_still_numbered_and_the_next_reads_whole(
    tmp_path: Path,
) -> None:
    log = log_at(tmp_path)
    sent(log, [SYSTEM, ASK], [SYSTEM, ASK], [SYSTEM, ASK, CALL])
    again = request_at(log.path, 2)
    assert (again.kept, again.added) == (2, [])
    assert request_at(log.path, 3).messages == [SYSTEM, ASK, CALL]


def test_a_compaction_keeps_the_unchanged_prefix_and_writes_the_rest_again(
    tmp_path: Path,
) -> None:
    log = log_at(tmp_path)
    note = {"role": "user", "content": "[2 earlier messages compacted]"}
    sent(log, [SYSTEM, ASK, CALL, RESULT], [SYSTEM, note, RESULT])
    after = request_at(log.path, 2)
    assert (after.messages, after.kept) == ([SYSTEM, note, RESULT], 1)
    assert request_at(log.path, 1).messages == [SYSTEM, ASK, CALL, RESULT]


def test_a_message_changed_in_place_is_written_again_from_where_it_changed(
    tmp_path: Path,
) -> None:
    """Older screenshots are trimmed in place: the messages after the changed one are
    the same, but only the ones before it are the previous request's."""
    log = log_at(tmp_path)
    messages = [SYSTEM, dict(ASK), CALL, RESULT]
    sent(log, messages)
    messages[1]["content"] = "[screenshot elided]"
    sent(log, messages)
    trimmed = request_at(log.path, 2)
    assert (trimmed.kept, trimmed.messages) == (1, messages)
    assert request_at(log.path, 1).messages[1] == ASK


def test_the_tools_are_written_only_when_they_change(tmp_path: Path) -> None:
    log = log_at(tmp_path)
    for tools in ([TOOL_A], [TOOL_A], [TOOL_A, TOOL_B]):
        log.record([SYSTEM, ASK], max_tokens=100, tools=tools)
    lines = [json.loads(line) for line in log.path.read_text().splitlines()]
    firsts = [line for line in lines if "request" in line]
    assert ["tools" in head for head in firsts] == [True, False, True]
    assert request_at(log.path, 2).tools == [TOOL_A]
    assert request_at(log.path, 3).tools == [TOOL_A, TOOL_B]


# -- the end: what came after the last request -------------------------------------

REPLY: dict[str, Any] = {
    "role": "assistant",
    "content": "",
    "tool_calls": [{"id": "f1", "function": {"name": "finish", "arguments": "{}"}}],
}
REFUSED: dict[str, Any] = {"role": "tool", "tool_call_id": "f1", "content": "finish refused"}


def test_the_end_holds_the_last_reply_and_its_results_which_no_request_carries(
    tmp_path: Path,
) -> None:
    log = log_at(tmp_path)
    sent(log, [SYSTEM, ASK], [SYSTEM, ASK, CALL, RESULT])
    log.end([SYSTEM, ASK, CALL, RESULT, REPLY, REFUSED])
    last = ending(log.path)
    assert last is not None
    assert (last.after, last.ended, last.kept, last.added) == (2, 1.0, 4, [REPLY, REFUSED])
    assert last.messages == [SYSTEM, ASK, CALL, RESULT, REPLY, REFUSED]
    assert [r.number for r in requests(log.path)] == [1, 2]


def test_a_log_with_no_end_says_so_and_invents_none(tmp_path: Path) -> None:
    log = log_at(tmp_path)
    sent(log, [SYSTEM, ASK])
    assert ending(log.path) is None


def test_a_request_after_an_end_is_rebuilt_from_it(tmp_path: Path) -> None:
    """A chat session's next turn sends what the last one ended on, and more."""
    log = log_at(tmp_path)
    sent(log, [SYSTEM, ASK])
    log.end([SYSTEM, ASK, REPLY])
    sent(log, [SYSTEM, ASK, REPLY, REFUSED])
    after = request_at(log.path, 2)
    assert (after.kept, after.messages) == (3, [SYSTEM, ASK, REPLY, REFUSED])


def test_a_turn_that_dies_leaves_no_end(tmp_path: Path) -> None:
    class Dying(Recording):
        def stream_chat(self, messages: Any, **kwargs: Any) -> Iterator[Any]:
            super().stream_chat(messages, **kwargs)
            msg = "the client broke"
            raise RuntimeError(msg)

    log = ConversationLog(tmp_path / CONVERSATION_LOG)
    options = TurnOptions(workdir=tmp_path, journal=tmp_path / "j.jsonl", conversation=log)
    with pytest.raises(RuntimeError, match="the client broke"):
        list(run_turn(cast(VllmClient, Dying([])), [], "hi", options, turn=1))
    assert [r.number for r in requests(log.path)] == [1]
    assert ending(log.path) is None


# -- no secret, and each redacted message named ------------------------------------

SECRET = "s" + "k-" + "dummyNotARealKey0123456789"  # built from parts for the leak guard


def test_a_secret_is_written_nowhere_and_each_message_it_changed_is_named(
    tmp_path: Path,
) -> None:
    log = log_at(tmp_path)
    thought = {"role": "assistant", "content": "", "reasoning_content": f"the key is {SECRET}"}
    long = {"role": "tool", "tool_call_id": "c1", "content": f"{SECRET}\n" + "y" * 20_000}
    sent(log, [SYSTEM, ASK, thought, long], [SYSTEM, ASK, thought, long, RESULT])
    assert SECRET.encode() not in log.path.read_bytes()
    first, second = request_at(log.path, 1), request_at(log.path, 2)
    assert first.messages[2]["reasoning_content"] == f"the key is {STAR}"
    assert first.messages[3]["content"] == f"{STAR}\n" + "y" * 20_000  # whole, never capped
    assert first.redacted == second.redacted == (2, 3)
    assert second.messages[4] == RESULT
    # a compaction that writes the secret's message again names it again, at its new place
    sent(log, [SYSTEM, long])
    assert request_at(log.path, 3).redacted == (1,)
    log.end([SYSTEM, long, thought])
    last = ending(log.path)
    assert last is not None
    assert last.redacted == (1, 2)
    assert SECRET.encode() not in log.path.read_bytes()


def test_tool_schemas_the_redaction_changed_are_named_until_they_change_again(
    tmp_path: Path,
) -> None:
    log = log_at(tmp_path)
    keyed = {"type": "function", "function": {"name": "f", "description": f"key {SECRET}"}}
    for tools in ([keyed], [keyed], [TOOL_A]):
        log.record([SYSTEM], max_tokens=100, tools=tools)
    assert SECRET.encode() not in log.path.read_bytes()
    assert [request_at(log.path, n).tools_redacted for n in (1, 2, 3)] == [True, True, False]
    assert request_at(log.path, 1).tools[0]["function"]["description"] == f"key {STAR}"


# -- what the log does not hold ----------------------------------------------------


def test_a_log_cut_inside_a_line_gives_every_whole_request_then_says_where(
    tmp_path: Path,
) -> None:
    log = log_at(tmp_path)
    sent(log, [SYSTEM], [SYSTEM, ASK], [SYSTEM, ASK, CALL])
    log.path.write_bytes(log.path.read_bytes()[:-10])
    whole, why = numbers_until_cut(log.path)
    assert whole == [1, 2]
    assert "is cut at line 6" in why
    assert request_at(log.path, 2).messages == [SYSTEM, ASK]


def test_a_log_cut_between_a_requests_lines_is_cut_not_a_shorter_request(
    tmp_path: Path,
) -> None:
    log = log_at(tmp_path)
    sent(log, [SYSTEM], [SYSTEM, ASK, CALL, RESULT])
    lines = log.path.read_text().splitlines(keepends=True)
    log.path.write_text("".join(lines[:-1]))
    whole, why = numbers_until_cut(log.path)
    assert whole == [1]
    assert "cut inside request 2: 3 of 4" in why
    with pytest.raises(ConversationError, match="cut inside request 2"):
        request_at(log.path, 2)


def test_a_request_the_log_does_not_hold_is_an_error_not_an_empty_request(
    tmp_path: Path,
) -> None:
    log = log_at(tmp_path)
    sent(log, [SYSTEM], [SYSTEM, ASK])
    with pytest.raises(ConversationError, match=r"holds 2 request\(s\), not request 3"):
        request_at(log.path, 3)


@pytest.mark.parametrize(
    ("text", "why"),
    [
        ("not json\n", "line 1 is not JSON"),
        ("[1]\n", "line 1 is not a record"),
        ('{"role": "user"}\n', "line 1 is not a request's or an end's first line"),
        ('{"request": 1, "kept": 0, "messages": 0}\n', "line 1 is not a request's or an end's"),
        (
            '{"request": 1, "sent": 1, "max_tokens": 9, "kept": 1, "messages": 1}\n{}\n',
            "request 1 keeps 1 of 0",
        ),
    ],
)
def test_a_line_that_is_not_the_logs_record_is_an_error(
    tmp_path: Path, text: str, why: str
) -> None:
    path = tmp_path / CONVERSATION_LOG
    path.write_text(text)
    with pytest.raises(ConversationError, match=why):
        list(requests(path))


def test_a_log_that_cannot_be_written_stops_and_never_raises(tmp_path: Path) -> None:
    (tmp_path / CONVERSATION_LOG).mkdir()
    log = log_at(tmp_path)
    sent(log, [SYSTEM], [SYSTEM, ASK])
    log.end([SYSTEM, ASK, REPLY])
    assert log.requests == 0
    assert log.failed is not None
    assert log.failed.startswith("request 1: ")


def test_an_end_that_cannot_be_written_is_named_as_the_end(tmp_path: Path) -> None:
    log = log_at(tmp_path)
    sent(log, [SYSTEM])
    log.path.chmod(0o444)
    log.end([SYSTEM, REPLY])
    assert log.failed is not None
    assert log.failed.startswith("the end after request 1: ")
    assert ending(log.path) is None


def test_a_message_that_cannot_be_written_stops_the_log_and_writes_nothing(
    tmp_path: Path,
) -> None:
    log = log_at(tmp_path)
    sent(log, [SYSTEM])
    sent(log, [SYSTEM, {"role": "user", "content": object()}], [SYSTEM, ASK])
    assert log.requests == 1
    assert log.failed is not None
    assert log.failed.startswith("request 2: ")
    assert [r.number for r in requests(log.path)] == [1]


# -- the engine logs what it sends -------------------------------------------------


class Recording:
    """A model that replays rounds and keeps a copy of each request as sent."""

    def __init__(self, rounds: list[list[Any]]) -> None:
        self.rounds = list(rounds)
        self.asked: list[dict[str, Any]] = []

    def __enter__(self) -> Recording:
        return self

    def __exit__(self, *exc: object) -> None:
        return None

    def stream_chat(self, messages: Any, **kwargs: Any) -> Iterator[Any]:
        request = {"messages": list(messages), **kwargs}
        self.asked.append(json.loads(json.dumps(request)))
        return iter(self.rounds.pop(0) if self.rounds else [])


def read(path: str, call_id: str) -> list[Any]:
    return [ToolCall(id=call_id, name="read_file", arguments=json.dumps({"path": path}))]


def matches(logged: Request, asked: dict[str, Any]) -> bool:
    return (logged.messages, logged.tools, logged.max_tokens) == (
        asked["messages"],
        asked["tools"],
        asked["max_tokens"],
    )


def test_the_engine_logs_each_request_as_the_model_received_it_through_compactions(
    tmp_path: Path,
) -> None:
    (tmp_path / "big.txt").write_text("word " * 6000)
    (tmp_path / "small.txt").write_text("small\n")
    log = ConversationLog(tmp_path / CONVERSATION_LOG)
    options = TurnOptions(
        workdir=tmp_path, journal=tmp_path / "j.jsonl", context_tokens=20_000, conversation=log
    )
    history = [{"role": "user", "content": f"question {i} " + "x" * 4000} for i in range(7)]
    client = Recording(
        [read("big.txt", "c1"), read("small.txt", "c2"), [StreamToken("content", "done")]]
    )
    events = list(run_turn(cast(VllmClient, client), history, "read them", options, turn=1))
    assert any(isinstance(e, Compaction) for e in events)
    logged = list(requests(log.path))
    assert len(logged) == len(client.asked) == 3
    assert all(matches(one, asked) for one, asked in zip(logged, client.asked, strict=True))
    # a compaction rewrote what came before, and the log wrote it again
    assert any(now.kept < len(before.messages) for before, now in pairwise(logged))
    last = ending(log.path)
    assert last is not None
    assert last.after == 3
    assert last.messages == json.loads(json.dumps(history))  # the turn's last reply included
    assert last.messages[-1]["content"] == "done"


def test_a_chat_turn_given_no_log_writes_none(tmp_path: Path) -> None:
    options = TurnOptions(workdir=tmp_path, journal=tmp_path / "j.jsonl")
    list(run_turn(cast(VllmClient, Recording([[]])), [], "hi", options, turn=1))
    assert not (tmp_path / CONVERSATION_LOG).exists()


# -- an autonomous run keeps one beside its ledger ---------------------------------


def git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(repo), "-c", "user.name=t", "-c", "user.email=t@t", *args],
        check=True,
        capture_output=True,
        text=True,
    ).stdout


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    root = tmp_path / "repo"
    (root / "tests").mkdir(parents=True)
    (root / "calc.py").write_text("def add(a, b):\n    return a - b\n")
    (root / "tests" / "test_calc.py").write_text(
        "from calc import add\n\n\ndef test_add():\n    assert add(2, 2) == 4\n"
    )
    git(root, "init", "-q", "-b", "main")
    git(root, "add", "-A")
    git(root, "commit", "-q", "-m", "init")
    return root


def fix_then_finish() -> Recording:
    edit = {"path": "calc.py", "old": "a - b", "new": "a + b"}
    return Recording(
        [
            [ToolCall(id="c1", name="edit_file", arguments=json.dumps(edit))],
            [ToolCall(id="f1", name="finish", arguments=json.dumps({"summary": "fixed add"}))],
        ]
    )


def run(repo: Path, client: Recording) -> Any:
    options = AutoOptions(task="make add add", repo=repo, run_id="r1", arm="E")
    return run_auto(options, cast(VllmClient, client))


def test_an_autonomous_run_keeps_its_conversation_beside_its_ledger(repo: Path) -> None:
    client = fix_then_finish()
    result = run(repo, client)
    assert result.outcome == "finished"
    logged = list(requests(result.journal.parent / CONVERSATION_LOG))
    assert len(logged) == len(client.asked) == 2
    assert all(matches(one, asked) for one, asked in zip(logged, client.asked, strict=True))
    assert [m["role"] for m in logged[0].messages] == ["system", "user"]
    last = ending(result.journal.parent / CONVERSATION_LOG)
    assert last is not None
    assert [m["role"] for m in last.added] == ["assistant", "tool"]
    assert last.added[0]["tool_calls"][0]["function"]["name"] == "finish"
    assert "Conversation log" not in git(repo, "log", "-1", "--format=%B", result.branch)


def test_a_run_whose_log_cannot_be_written_still_finishes_and_its_commit_says_so(
    repo: Path,
) -> None:
    (repo / ".saddle" / "runs" / "r1" / CONVERSATION_LOG).mkdir(parents=True)
    result = run(repo, fix_then_finish())
    assert result.outcome == "finished"
    message = git(repo, "log", "-1", "--format=%B", result.branch)
    assert f"The conversation log ({CONVERSATION_LOG}) stopped at request 1: " in message
    assert "it holds the 0 request(s) before it." in message
