"""How a terminal keeps a command's output (`Terminal`)."""

from __future__ import annotations

import time

from saddle.sandbox import MAX_CAPTURE, Terminal


def test_a_long_output_is_read_in_linear_time_and_keeps_its_head_and_tail() -> None:
    """The gate (8 workers) cut `seq 1 20000` at 12129: each appended line
    re-summed every earlier one (20,000 lines took 4.5 s alone, four times the
    lines sixteen times the time), so the reader fell behind the command.
    Known-good: 200,000 lines go in within seconds, the head and the tail are
    exact, and what was dropped is counted."""
    terminal = Terminal(id="t", command="seq", started=0.0)
    lines = [f"{i}\n" for i in range(1, 200_001)]
    began = time.monotonic()
    for line in lines:
        terminal._append(line)
    assert time.monotonic() - began < 3
    text = "".join(lines)
    out = terminal.output()
    assert out.startswith(text[: MAX_CAPTURE // 2])
    assert out.endswith(text[-MAX_CAPTURE // 2 :])
    assert f"[... {len(text) - MAX_CAPTURE} characters elided ...]" in out


def test_a_short_output_is_whole() -> None:
    terminal = Terminal(id="t", command="echo", started=0.0)
    for part in ("a\n", "b\n"):
        terminal._append(part)
    assert terminal.output() == "a\nb\n"
