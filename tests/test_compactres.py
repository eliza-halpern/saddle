"""Red tests for what an autonomous run needs from compaction.

Each test states one property the agent needs to keep continuity once its
conversation outgrows the window, and is expected to FAIL on phase2-final
(4ee7cf7). No fix here; these are discrimination evidence only.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, cast

from test_auto import Scripted, call, finish, git, repo  # noqa: F401  (fixture)
from test_feed import FakeAuditor
from test_keep_reasoning import _render

from saddle.auto import SYSTEM_PROMPT, AutoOptions, run_auto
from saddle.engine import AUTO_NUDGE, AutoRun, RunBudget, TurnOptions, run_turn
from saddle.events import Compaction
from saddle.memory import KEEP_RECENT, compact, estimate_tokens
from saddle.vllm import VllmClient

TASK = "TASK-7f3a: make add() in calc.py return the sum instead of the difference"
BIG = "".join(f"def helper_{i}(x):\n    return x * {i}  # filler line {i}\n" for i in range(600))
"""~33 KB of Python: one read of it is ~8k estimated tokens."""


# -- helpers -------------------------------------------------------------------


def _auto_system() -> str:
    return SYSTEM_PROMPT.format(tests="Test files (tests/) are read-only.")


def _tool_round(i: int, name: str, args: dict[str, Any], result: str) -> list[dict[str, Any]]:
    cid = f"c{i}"
    return [
        {
            "role": "assistant",
            "content": "",
            "tool_calls": [
                {
                    "id": cid,
                    "type": "function",
                    "function": {"name": name, "arguments": json.dumps(args)},
                }
            ],
        },
        {"role": "tool", "tool_call_id": cid, "content": result},
    ]


PYTEST_FAIL = (
    "".join(
        f"____ test_case_{i} ____\n\n    def test_case_{i}():\n"
        f">       assert add({i}, 1) == {i + 1}\n"
        f"E       assert {i - 1} == {i + 1}\n\ntests/test_calc.py:{10 + i}: AssertionError\n"
        for i in range(12)
    )
    + "=========================== short test summary info ============================\n"
    + "".join(
        f"FAILED tests/test_calc.py::test_case_{i} - assert {i - 1} == {i + 1}\n" for i in range(12)
    )
    + "12 failed, 40 passed in 0.31s\n"
)
AUDIT = "calc.py:3: return a - b"
"""The finding tests.test_feed.FakeAuditor names while calc.py subtracts."""


def _auto_history(rounds: int) -> list[dict[str, Any]]:
    """What an auto run's messages look like mid-run: system, task, then work.

    Round 0 edits calc.py; round 1 runs the tests (12 failures) and the feed
    appends an audit finding; then `rounds` reads of a big file.
    """
    messages: list[dict[str, Any]] = [
        {"role": "system", "content": _auto_system()},
        {"role": "user", "content": TASK},
    ]
    messages += _tool_round(
        0, "edit_file", {"path": "src/util_math.py", "old": "a - b", "new": "a + b"}, "ok"
    )
    messages += _tool_round(
        1, "run_command", {"command": "pytest -q"}, PYTEST_FAIL + "\n\n" + AUDIT
    )
    for i in range(rounds):
        messages += _tool_round(2 + i, "read_file", {"path": f"mod{i}.py"}, BIG)
    return messages


def _auto_turn(tmp_path: Path, context_tokens: int) -> TurnOptions:
    run = AutoRun(budget=RunBudget(time_s=600, tokens=10_000_000), run_span="s")
    return TurnOptions(
        workdir=tmp_path,
        journal=tmp_path / "j.jsonl",
        auto=run,
        system_prompt=_auto_system(),
        context_tokens=context_tokens,
    )


def _compacted_request(tmp_path: Path, rounds: int = 8, ctx: int = 40_000) -> list[dict[str, Any]]:
    """The first request run_turn sends for a mid-run auto conversation."""
    client = Scripted([finish()])
    options = _auto_turn(tmp_path, ctx)
    events = list(run_turn(cast(VllmClient, client), _auto_history(rounds), None, options, turn=1))
    assert any(isinstance(e, Compaction) for e in events), "harness: compaction did not fire"
    return cast(list[dict[str, Any]], client.asked[0]["messages"])


# -- an auto run never compacts after its first request ------------------------


def test_f0_auto_run_keeps_every_request_under_the_compaction_limit(repo: Path) -> None:  # noqa: F811
    """Contract: no request an auto run sends exceeds `compaction_limit()`.

    run_auto hands one turn to run_turn; compaction runs once, before the
    round loop, so rounds 2..N are never compacted and the prompt grows
    until the server refuses it.
    """
    (repo / "big.py").write_text(BIG)
    git(repo, "add", "big.py")
    git(repo, "commit", "-q", "-m", "big")
    reads = [[call("read_file", f"r{i}", path="big.py")] for i in range(8)]
    client = Scripted([*reads, finish()])
    options = AutoOptions(
        task=TASK, repo=repo, run_id="f0", arm="E", context_tokens=40_000, token_budget=10**7
    )
    result = run_auto(options, cast(VllmClient, client))
    assert result.outcome == "finished"
    probe = TurnOptions(
        context_tokens=40_000,
        tools=client.asked[0]["tools"],
    )
    limit = probe.compaction_limit()
    sizes = [estimate_tokens(req["messages"]) for req in client.asked]
    assert max(sizes) <= limit, f"request sizes {sizes} vs compaction limit {limit}"


# -- D1: the task statement survives compaction ---------------------------------


def test_d1_task_statement_survives_compaction(tmp_path: Path) -> None:
    request = _compacted_request(tmp_path)
    assert any(TASK in str(m.get("content")) for m in request), [
        (m["role"], str(m.get("content"))[:60]) for m in request
    ]


# -- D2: the compaction note must not tell an auto run to ask the user ---------


def test_d2_note_does_not_tell_an_auto_run_to_ask_the_user(tmp_path: Path) -> None:
    request = _compacted_request(tmp_path)
    text = json.dumps(request)
    assert "Nobody will answer questions" in text  # harness: this is the auto prompt
    assert "Ask the user" not in text


# -- D3: working state survives stage 2 ------------------------------------------


def test_d3_edited_files_survive_compaction(repo: Path) -> None:  # noqa: F811
    # Respelled to the real caller's shape: run_auto is the only
    # caller that sets `changed_files`; the hand-built history had none.
    # Expectation unchanged: the edited path is in the compacted request.
    (repo / "big.py").write_text(BIG)
    git(repo, "add", "big.py")
    git(repo, "commit", "-q", "-m", "big")
    edit = [call("write_file", "w0", path="src/util_math.py", content="X = 1\n")]
    reads = [[call("read_file", f"r{i}", path="big.py")] for i in range(8)]
    client = Scripted([edit, *reads, finish()])
    options = AutoOptions(task=TASK, repo=repo, run_id="d3", arm="E", context_tokens=40_000)
    run_auto(options, cast(VllmClient, client))
    request = client.asked[-1]["messages"]
    assert not any(c["id"] == "w0" for m in request for c in m.get("tool_calls") or [])
    note = next(m for m in request if str(m.get("content")).startswith("[Earlier conversation"))
    assert "src/util_math.py" in note["content"]


def test_d3_latest_audit_finding_survives_compaction(repo: Path) -> None:  # noqa: F811
    # Respelled to the real caller's shape: the finding reaches
    # the model through the audit feed (arm E+A+F), which the hand-built
    # history never had. Expectation unchanged: the finding is in the request
    # after the message that delivered it is gone.
    (repo / "big.py").write_text(BIG)
    git(repo, "add", "big.py")
    git(repo, "commit", "-q", "-m", "big")
    edit = [call("edit_file", "e0", path="calc.py", old="def add", new="# adds\ndef add")]
    reads = [[call("read_file", f"r{i}", path="big.py")] for i in range(8)]
    client = Scripted([edit, [call("finish", "f0", summary="x")], *reads], tail=finish("y", "f9"))
    options = AutoOptions(
        task=TASK,
        repo=repo,
        run_id="d3a",
        arm="E+A+F",
        context_tokens=40_000,
        auditor_factory=lambda *a: FakeAuditor(),
    )
    run_auto(options, cast(VllmClient, client))
    request = client.asked[10]["messages"]  # the last request before the tail's finish
    assert not any(m.get("tool_call_id") == "f0" for m in request)
    note = next(m for m in request if str(m.get("content")).startswith("[Earlier conversation"))
    assert AUDIT in note["content"]


# -- D4: tool-call pairing and the served template --------------------------------


def _pairing_errors(messages: list[dict[str, Any]]) -> list[str]:
    errors = []
    open_ids: set[str] = set()
    for i, m in enumerate(messages):
        if m["role"] == "assistant":
            open_ids = {c["id"] for c in m.get("tool_calls") or []}
        elif m["role"] == "tool":
            if m.get("tool_call_id") not in open_ids:
                errors.append(f"orphan tool message at {i}")
        else:
            open_ids = set()
    return errors


def test_d4_no_orphan_tool_message_after_compaction() -> None:
    # odd pressure: a limit that makes stage 2 stop between a call and its result
    for heavy in (False, True):
        for rounds in range(3, 10):
            for limit in range(4000, 40000, 997):
                messages = _auto_history(rounds)
                if heavy:  # a long reply beside a short result: dropping the call alone fits
                    for m in messages:
                        if m["role"] == "assistant":
                            m["content"] = BIG[:12000]
                        elif m["role"] == "tool":
                            m["content"] = "ok"
                compact(messages, limit_tokens=limit)
                assert _pairing_errors(messages) == [], (heavy, rounds, limit)


def test_d4_compacted_request_renders_under_the_served_template(tmp_path: Path) -> None:
    request = _compacted_request(tmp_path)
    _render({"messages": request, "tools": None, "reasoning_effort": "medium"})


# -- D5: stage 1 keeps the failure list of an older pytest run ---------------------


def test_d5_stage1_keeps_every_failed_line_of_an_older_pytest_run() -> None:
    messages = _auto_history(3)  # pytest result is now older than KEEP_RECENT
    assert 5 < len(messages) - KEEP_RECENT  # harness: index 5 is unprotected
    # a limit stage 1 alone satisfies, so stage 2 does not muddy the question
    compact(messages, limit_tokens=estimate_tokens(messages) - 100)
    pytest_result = messages[5]["content"]
    assert "elided by compaction" in pytest_result  # harness: stage 1 ran on it
    missing = [
        f"test_case_{i}"
        for i in range(12)
        if f"FAILED tests/test_calc.py::test_case_{i}" not in pytest_result
    ]
    assert missing == []


# -- D6: the estimate counts what is actually sent ---------------------------------


def test_d6_estimate_counts_kept_reasoning() -> None:
    thought = "x" * 400_000  # ~100k tokens of reasoning kept on one assistant message
    messages = [
        {"role": "system", "content": "s"},
        {"role": "user", "content": TASK},
        {"role": "assistant", "content": "ok", "reasoning_content": thought, "reasoning": thought},
    ]
    assert estimate_tokens(messages) >= len(thought) // 4


def test_d6_compaction_fires_when_kept_reasoning_overflows() -> None:
    thought = "x" * 400_000
    messages: list[dict[str, Any]] = [
        {"role": "system", "content": "s"},
        {"role": "user", "content": TASK},
    ]
    for _i in range(4):
        messages.append({"role": "assistant", "content": "", "reasoning_content": thought})
        messages.append({"role": "user", "content": AUTO_NUDGE})
    dropped, summary = compact(messages, limit_tokens=50_000)
    assert (dropped, summary) != (0, "")


def test_d4_chat_request_after_stage2_renders_under_the_served_template(tmp_path: Path) -> None:
    """A browser chat with a persona: stage 2's note is a second system message."""
    history: list[dict[str, Any]] = [{"role": "system", "content": "You are a helpful persona."}]
    for i in range(8):
        history.append({"role": "user", "content": f"question {i}: explain this\n{BIG[:20000]}"})
        history.append({"role": "assistant", "content": f"answer {i}"})
    client = Scripted([[]], tail=[])
    options = TurnOptions(
        workdir=tmp_path,
        journal=tmp_path / "j.jsonl",
        system_prompt="You are a helpful persona.",
        context_tokens=40_000,
    )
    events = list(run_turn(cast(VllmClient, client), history, "and now?", options, turn=9))
    assert any(isinstance(e, Compaction) and e.dropped_messages for e in events)
    _render({"messages": client.asked[0]["messages"], "tools": None, "reasoning_effort": "medium"})
