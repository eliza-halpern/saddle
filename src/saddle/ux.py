"""Operator UX primitives: progress reporting and human-gate prompts.

All IO is stream-injected so every path is unit-testable without a terminal.
The rich live display wraps ProgressReporter later (scheduler issue).
Nodes run one at a time, not concurrently.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import IO


@dataclass
class ProgressState:
    """Clamped progress fraction over a known total."""

    total: int
    done: int = 0

    @property
    def fraction(self) -> float:
        if self.total <= 0:
            return 1.0
        return min(1.0, max(0.0, self.done / self.total))

    def advance(self, step: int = 1) -> float:
        self.done += step
        return self.fraction


class ProgressReporter:
    """Line-oriented progress sink."""

    def __init__(self, stream: IO[str], label: str, total: int) -> None:
        self._stream = stream
        self._label = label
        self._state = ProgressState(total=total)

    @property
    def fraction(self) -> float:
        return self._state.fraction

    def advance(self, step: int = 1) -> None:
        fraction = self._state.advance(step)
        self._stream.write(f"{self._label}: {fraction:.0%}\n")


def ask_confirm(
    prompt: str,
    *,
    stdin: IO[str],
    stdout: IO[str],
    default: bool = False,
    attempts: int = 3,
) -> bool:
    """Human-gate yes/no prompt. Empty input takes the default; garbage retries."""
    hint = "Y/n" if default else "y/N"
    for _ in range(max(1, attempts)):
        stdout.write(f"{prompt} [{hint}] ")
        stdout.flush()
        answer = stdin.readline().strip().lower()
        if answer == "":
            return default
        if answer in {"y", "yes"}:
            return True
        if answer in {"n", "no"}:
            return False
        stdout.write("Please answer y or n.\n")
    return default
