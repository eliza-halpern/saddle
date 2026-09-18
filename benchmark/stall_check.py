"""M3 stall check: tripwires A (repeat loop) and B (silence gap).

Reads baseline-arm session JSONL and/or saddle journals. Exits 0 when silent,
1 with a printed reason when a tripwire fires. Backstop C (absolute
30 min cap) is enforced by running each arm under `timeout 1800`.

Tripwire B needs event timestamps, which saddle journal spans do not
carry, so B is baseline-only; saddle runs are round-bounded by construction
and harness-capped by `timeout`.
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime
from itertools import pairwise
from pathlib import Path

REPEAT_TRIPS = 3
MAX_SILENCE_SECONDS = 600.0
# BENCHMARK.md C5 defines tripwire B over PROGRESS events: a diff change
# or a test transition. Reading files and shelling out to `ls` are not
# progress, however frequent -- a run can emit a timestamped event every
# few seconds for a quarter of an hour and have moved nothing.
PROGRESS_TOOLS = frozenset({"write", "edit"})
TEST_RUNNER_MARKER = "pytest"


def _parse_time(value: object) -> float | None:
    """ISO timestamp to epoch seconds; None when missing or malformed."""
    if not isinstance(value, str):
        return None
    try:
        return datetime.fromisoformat(value).timestamp()
    except ValueError:
        return None


def _is_progress(name: str, arguments: object) -> bool:
    """A diff change or a test transition, per BENCHMARK.md C5."""
    if name in PROGRESS_TOOLS:
        return True
    if name != "bash":
        return False
    command = arguments.get("command") if isinstance(arguments, dict) else None
    return isinstance(command, str) and TEST_RUNNER_MARKER in command


def baseline_tool_calls(path: Path) -> tuple[list[tuple[str, str]], list[float]]:
    """Tool-call sequence plus PROGRESS checkpoints bounded by the session.

    The returned times are the moments work actually moved, with the
    session's first and last timestamps as endpoints. The bounds matter:
    without them a run that never makes progress at all yields fewer than
    two checkpoints, and a gap check over an empty pairing reports silence
    as success. With them, a long preamble before the first progress and a
    long tail after the last one both trip.
    """
    calls: list[tuple[str, str]] = []
    progress: list[float] = []
    bounds: list[float] = []
    for line in path.read_text().splitlines():
        event = json.loads(line)
        moment = _parse_time(event.get("timestamp"))
        if moment is not None:
            bounds.append(moment)
        message = event.get("message")
        if not isinstance(message, dict):
            continue
        content = message.get("content")
        if not isinstance(content, list):
            continue
        for part in content:
            if isinstance(part, dict) and part.get("type") == "toolCall":
                name = str(part.get("name", ""))
                arguments = part.get("arguments")
                calls.append((name, json.dumps(arguments, sort_keys=True)))
                if moment is not None and _is_progress(name, arguments):
                    progress.append(moment)
    if not bounds:
        return calls, progress
    return calls, [min(bounds), *progress, max(bounds)]


def saddle_tool_calls(path: Path) -> list[tuple[str, str]]:
    """Tool-call (name, canonical argv) sequence from journal spans."""
    calls: list[tuple[str, str]] = []
    for line in path.read_text().splitlines():
        record = json.loads(line)
        if record.get("record_type") != "span":
            continue
        argv = record.get("argv", [])
        name = record.get("name") or (argv[0] if argv else "")
        calls.append((str(name), json.dumps(argv[1:])))
    return calls


def check_repeats(calls: list[tuple[str, str]], limit: int) -> str | None:
    """Tripwire A: `limit` identical consecutive tool calls."""
    run = 0
    previous: tuple[str, str] | None = None
    for call in calls:
        run = run + 1 if call == previous else 1
        previous = call
        if run >= limit:
            return f"tripwire A: {call[0]} repeated {run}x consecutively"
    return None


def check_silence(times: list[float], limit: float) -> str | None:
    """Tripwire B: any gap between PROGRESS checkpoints longer than `limit`."""
    ordered = sorted(times)
    for before, after in pairwise(ordered):
        if after - before > limit:
            return f"tripwire B: {after - before:.0f}s without progress"
    return None


def main(argv: list[str]) -> int:
    """Check the given artifacts; 0 silent, 1 tripped, 2 usage error."""
    parser = argparse.ArgumentParser(description="M3 no-progress stall check.")
    parser.add_argument(
        "--baseline-session",
        action="append",
        default=[],
        help="Baseline-arm session JSONL (repeatable).",
    )
    parser.add_argument(
        "--saddle-journal", action="append", default=[], help="Saddle journal (repeatable)."
    )
    parser.add_argument("--max-silence", type=float, default=MAX_SILENCE_SECONDS)
    parser.add_argument("--repeat", type=int, default=REPEAT_TRIPS)
    args = parser.parse_args(argv)
    try:
        verdicts: list[tuple[str, str | None]] = []
        for raw in args.baseline_session:
            calls, times = baseline_tool_calls(Path(raw))
            hit = check_repeats(calls, args.repeat) or check_silence(times, args.max_silence)
            verdicts.append((raw, hit))
        for raw in args.saddle_journal:
            hit = check_repeats(saddle_tool_calls(Path(raw)), args.repeat)
            verdicts.append((raw, hit))
    except (OSError, ValueError) as exc:
        parser.error(str(exc))
    for raw, hit in verdicts:
        print(f"{raw}: {'PASS' if hit is None else hit}")
    if args.saddle_journal:
        print("note: tripwire B unchecked for saddle journals (no span timestamps)")
    return 1 if any(hit is not None for _, hit in verdicts) else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
