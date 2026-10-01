"""Tests for benchmark/stall_check.py: M3 tripwires A and B."""

from __future__ import annotations

import json
import runpy
import sys
from pathlib import Path
from typing import Any

import pytest

from benchmark import stall_check


def _baseline_session(path: Path, calls: list[tuple[str, dict[str, Any], str]]) -> Path:
    """Write a baseline-arm session whose tool calls are (name, args, timestamp)."""
    lines = ['{"type": "session", "timestamp": "2026-01-01T00:00:00Z"}']
    for index, (name, args, stamp) in enumerate(calls):
        lines.append(
            json.dumps(
                {
                    "type": "message",
                    "timestamp": stamp,
                    "message": {
                        "role": "assistant",
                        "content": [
                            {
                                "type": "toolCall",
                                "id": f"c{index}",
                                "name": name,
                                "arguments": args,
                            }
                        ],
                    },
                }
            )
        )
    path.write_text("\n".join(lines) + "\n")
    return path


def _journal(path: Path, spans: list[list[str]]) -> Path:
    """Write a saddle journal whose spans carry the given argv lists."""
    lines = [json.dumps({"record_type": "span", "name": argv[0], "argv": argv}) for argv in spans]
    path.write_text("\n".join(lines) + "\n")
    return path


def test_tripwire_a_fires_on_three_identical_pi_calls(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    session = _baseline_session(
        tmp_path / "loop.jsonl",
        [("bash", {"command": "ls"}, f"2026-01-01T00:00:0{second}Z") for second in range(3)],
    )

    assert stall_check.main(["--baseline-session", str(session)]) == 1
    assert "tripwire A" in capsys.readouterr().out


def test_tripwire_a_fires_on_three_identical_spans(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    journal = _journal(tmp_path / "loop.jsonl", [["git", "diff"]] * 3)

    assert stall_check.main(["--saddle-journal", str(journal)]) == 1
    assert "tripwire A" in capsys.readouterr().out


def test_varied_calls_stay_silent(tmp_path: Path) -> None:
    session = _baseline_session(
        tmp_path / "ok.jsonl",
        [
            ("write", {"path": "a"}, "2026-01-01T00:00:00Z"),
            ("bash", {"command": "ls"}, "2026-01-01T00:00:01Z"),
            ("write", {"path": "b"}, "2026-01-01T00:00:02Z"),
        ],
    )
    journal = _journal(
        tmp_path / "ok-journal.jsonl", [["git", "diff"], ["pytest"], ["git", "diff"]]
    )

    args = ["--baseline-session", str(session), "--saddle-journal", str(journal)]
    assert stall_check.main(args) == 0


def test_tripwire_b_fires_on_long_gap(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    session = _baseline_session(
        tmp_path / "gap.jsonl",
        [
            ("bash", {"command": "ls"}, "2026-01-01T00:00:00Z"),
            ("bash", {"command": "pwd"}, "2026-01-01T00:11:00Z"),
        ],
    )

    assert stall_check.main(["--baseline-session", str(session)]) == 1
    assert "tripwire B" in capsys.readouterr().out


def test_two_repeats_do_not_trip(tmp_path: Path) -> None:
    session = _baseline_session(
        tmp_path / "twice.jsonl",
        [("bash", {"command": "ls"}, f"2026-01-01T00:00:0{second}Z") for second in range(2)],
    )

    assert stall_check.main(["--baseline-session", str(session)]) == 0


def test_missing_file_exits_usage_error(tmp_path: Path) -> None:
    with pytest.raises(SystemExit) as exc_info:
        stall_check.main(["--baseline-session", str(tmp_path / "absent.jsonl")])

    assert exc_info.value.code == 2


def _event(moment: str, *, tool: str | None = None, command: str = "") -> str:
    """One baseline-arm session line: a timestamped event, optionally a tool call."""
    content: list[dict[str, object]] = []
    if tool is not None:
        args: dict[str, object] = {"command": command} if tool == "bash" else {"path": "n.py"}
        content.append({"type": "toolCall", "name": tool, "arguments": args})
    return json.dumps({"timestamp": moment, "message": {"content": content}})


def test_tripwire_b_counts_progress_not_any_event(tmp_path: Path) -> None:
    """BENCHMARK.md C5 says PROGRESS event: a diff change or a test
    transition. The checker measured any timestamped record, so a run
    doing nothing but reading files and thinking kept the clock alive
    forever while making no progress at all.
    """
    session = tmp_path / "s.jsonl"
    session.write_text(
        "\n".join(
            [
                _event("2026-09-18T00:00:00+00:00", tool="write"),
                # 15 minutes of reading and shell ceremony: timestamped,
                # frequent, and not progress by the stated definition.
                *[
                    _event(f"2026-09-18T00:{minute:02d}:00+00:00", tool="read")
                    for minute in range(1, 16)
                ],
                _event("2026-09-18T00:16:00+00:00", tool="write"),
            ]
        )
        + "\n"
    )
    _calls, times = stall_check.baseline_tool_calls(session)
    assert stall_check.check_silence(times, 600.0) is not None


def test_tripwire_b_accepts_a_test_run_as_progress(tmp_path: Path) -> None:
    """A pytest invocation is the "test transition" half of the rule."""
    session = tmp_path / "s.jsonl"
    session.write_text(
        "\n".join(
            [
                _event("2026-09-18T00:00:00+00:00", tool="write"),
                _event("2026-09-18T00:08:00+00:00", tool="bash", command="python -m pytest -q"),
                _event("2026-09-18T00:16:00+00:00", tool="edit"),
            ]
        )
        + "\n"
    )
    _calls, times = stall_check.baseline_tool_calls(session)
    assert stall_check.check_silence(times, 600.0) is None


def test_a_missing_or_malformed_timestamp_is_not_a_checkpoint() -> None:
    """`_parse_time` answers None for anything that is not an ISO string.

    A number or a null must not be read as an epoch, and a mangled string
    must not raise: one unreadable stamp in a long session may not abort
    the whole check.
    """
    assert stall_check._parse_time("2026-01-01T00:00:00+00:00") == 1767225600.0
    assert stall_check._parse_time(1767225600) is None
    assert stall_check._parse_time(None) is None
    assert stall_check._parse_time("yesterday around noon") is None


def test_events_without_a_usable_stamp_do_not_bound_the_session(tmp_path: Path) -> None:
    """Only readable timestamps are endpoints, so a bad one cannot move them.

    The last line has a garbled stamp: were it read as the end of the
    session, the final checkpoint would not be the 00:10 write.
    """
    session = tmp_path / "s.jsonl"
    session.write_text(
        "\n".join(
            [
                _event("2026-09-18T00:00:00+00:00", tool="read"),
                _event("2026-09-18T00:05:00+00:00", tool="write"),
                json.dumps({"timestamp": "not a time", "message": {"content": []}}),
                json.dumps({"message": {"content": []}}),
                _event("2026-09-18T00:10:00+00:00", tool="read"),
            ]
        )
        + "\n"
    )
    _calls, times = stall_check.baseline_tool_calls(session)
    stamps = ["2026-09-18T00:00:00+00:00", "2026-09-18T00:05:00+00:00", "2026-09-18T00:10:00+00:00"]
    assert times == [stall_check._parse_time(stamp) for stamp in stamps]


def test_only_tool_call_parts_are_calls(tmp_path: Path) -> None:
    """Prose parts, non-dict parts, and messages of the wrong shape are skipped.

    A session carries user turns, assistant text and tool results beside
    the tool calls; counting any of them would let ordinary chatter
    repeat three times and trip A.
    """
    session = tmp_path / "s.jsonl"
    text = {"type": "text", "text": "hello"}
    call = {"type": "toolCall", "name": "write", "arguments": {"path": "a.py"}}
    lines = [
        {"timestamp": "2026-09-18T00:00:00+00:00", "message": "not a dict"},
        {"timestamp": "2026-09-18T00:01:00+00:00", "message": {"content": "not a list"}},
        {"timestamp": "2026-09-18T00:02:00+00:00", "message": {"content": [text, "bare", call]}},
    ]
    session.write_text("\n".join(json.dumps(line) for line in lines) + "\n")

    calls, _times = stall_check.baseline_tool_calls(session)

    assert calls == [("write", json.dumps({"path": "a.py"}, sort_keys=True))]


def test_a_session_without_any_timestamp_has_no_checkpoints(tmp_path: Path) -> None:
    """With no readable stamp there is nothing to bound: calls survive, times are empty."""
    session = tmp_path / "s.jsonl"
    call = {"type": "toolCall", "name": "bash", "arguments": {"command": "ls"}}
    session.write_text(json.dumps({"message": {"content": [call]}}) + "\n")

    calls, times = stall_check.baseline_tool_calls(session)

    assert calls == [("bash", json.dumps({"command": "ls"}, sort_keys=True))]
    assert times == []


def test_a_journal_counts_spans_and_ignores_every_other_record(tmp_path: Path) -> None:
    """Only `span` records are calls, named by `name` or else by argv[0].

    The repeat below is split by a non-span record that would otherwise
    look identical; counting it would make four calls out of two spans.
    """
    journal = tmp_path / "j.jsonl"
    records = [
        {"record_type": "span", "name": "git", "argv": ["git", "diff"]},
        {"record_type": "event", "name": "git", "argv": ["git", "diff"]},
        {"record_type": "span", "argv": ["git", "status"]},
        {"record_type": "span", "argv": []},
    ]
    journal.write_text("\n".join(json.dumps(record) for record in records) + "\n")

    calls = stall_check.saddle_tool_calls(journal)

    assert calls == [("git", '["diff"]'), ("git", '["status"]'), ("", "[]")]


def test_main_exits_with_its_verdict_when_run_as_a_script(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """`python benchmark/stall_check.py ...` turns main's verdict into the exit status."""
    journal = _journal(tmp_path / "loop.jsonl", [["git", "diff"]] * 3)
    quiet = _journal(tmp_path / "ok.jsonl", [["git", "diff"], ["pytest"]])

    for target, expected in ((journal, 1), (quiet, 0)):
        monkeypatch.setattr(sys, "argv", ["stall_check.py", "--saddle-journal", str(target)])
        with pytest.raises(SystemExit) as exc_info:
            runpy.run_path(stall_check.__file__, run_name="__main__")
        assert exc_info.value.code == expected
    assert "tripwire A" in capsys.readouterr().out
