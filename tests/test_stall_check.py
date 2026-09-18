"""Tests for benchmark/stall_check.py: M3 tripwires A and B."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from benchmark import stall_check


def _pi_session(path: Path, calls: list[tuple[str, dict[str, Any], str]]) -> Path:
    """Write a pi session whose tool calls are (name, args, timestamp)."""
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
    session = _pi_session(
        tmp_path / "loop.jsonl",
        [("bash", {"command": "ls"}, f"2026-01-01T00:00:0{second}Z") for second in range(3)],
    )

    assert stall_check.main(["--pi-session", str(session)]) == 1
    assert "tripwire A" in capsys.readouterr().out


def test_tripwire_a_fires_on_three_identical_spans(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    journal = _journal(tmp_path / "loop.jsonl", [["git", "diff"]] * 3)

    assert stall_check.main(["--saddle-journal", str(journal)]) == 1
    assert "tripwire A" in capsys.readouterr().out


def test_varied_calls_stay_silent(tmp_path: Path) -> None:
    session = _pi_session(
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

    assert stall_check.main(["--pi-session", str(session), "--saddle-journal", str(journal)]) == 0


def test_tripwire_b_fires_on_long_gap(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    session = _pi_session(
        tmp_path / "gap.jsonl",
        [
            ("bash", {"command": "ls"}, "2026-01-01T00:00:00Z"),
            ("bash", {"command": "pwd"}, "2026-01-01T00:11:00Z"),
        ],
    )

    assert stall_check.main(["--pi-session", str(session)]) == 1
    assert "tripwire B" in capsys.readouterr().out


def test_two_repeats_do_not_trip(tmp_path: Path) -> None:
    session = _pi_session(
        tmp_path / "twice.jsonl",
        [("bash", {"command": "ls"}, f"2026-01-01T00:00:0{second}Z") for second in range(2)],
    )

    assert stall_check.main(["--pi-session", str(session)]) == 0


def test_missing_file_exits_usage_error(tmp_path: Path) -> None:
    with pytest.raises(SystemExit) as exc_info:
        stall_check.main(["--pi-session", str(tmp_path / "absent.jsonl")])

    assert exc_info.value.code == 2


def _event(moment: str, *, tool: str | None = None, command: str = "") -> str:
    """One pi session line: a timestamped event, optionally a tool call."""
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
    _calls, times = stall_check.pi_tool_calls(session)
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
    _calls, times = stall_check.pi_tool_calls(session)
    assert stall_check.check_silence(times, 600.0) is None
