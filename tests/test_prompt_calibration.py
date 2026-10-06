"""The prompt estimate on a server that cannot count tokens (no /tokenize).

Strata serves no /tokenize, so every estimate there was characters over four
times INPUT_SAFETY (2.0). Its own usage put the real ratio near 1.22, so a
131,072 window compacted at ~57k real tokens, 163 times in one run. The
server's reported `prompt_tokens` calibrates the estimate instead.
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path
from typing import Any, cast

from test_auto import call, finish, repo  # noqa: F401  (fixture)
from test_compactfix import BIG_LINES, TASK, _commit_big

from saddle.auto import AutoOptions, AutoResult, run_auto
from saddle.engine import (
    CALIBRATION_MARGIN,
    INPUT_SAFETY,
    RATIO_FLOOR,
    TurnOptions,
)
from saddle.journal import COMPACTION_SPAN, read_spans
from saddle.memory import estimate_tokens
from saddle.vllm import StreamToken, StreamUsage, VllmClient

WINDOW = 131_072
MESSAGES = [{"role": "user", "content": "x" * 40_000}]


def test_the_estimate_is_the_blanket_factor_until_the_server_has_counted() -> None:
    options = TurnOptions(context_tokens=WINDOW)
    raw = estimate_tokens(MESSAGES) + options.tool_tokens()
    assert options.input_estimate(MESSAGES) == int(raw * INPUT_SAFETY)


def test_a_calibrated_estimate_follows_the_measured_ratio_both_ways() -> None:
    looser = TurnOptions(context_tokens=WINDOW, prompt_ratio=1.22)
    stricter = TurnOptions(context_tokens=WINDOW, prompt_ratio=2.5)
    raw = estimate_tokens(MESSAGES) + looser.tool_tokens()
    assert looser.input_estimate(MESSAGES) == int(raw * 1.22 * CALIBRATION_MARGIN)
    assert stricter.input_estimate(MESSAGES) == int(raw * 2.5 * CALIBRATION_MARGIN)
    assert stricter.input_estimate(MESSAGES) > int(raw * INPUT_SAFETY)  # worse text, stricter


def test_the_calibrated_limit_keeps_the_reply_room_without_the_safety_divisor() -> None:
    assert TurnOptions(context_tokens=WINDOW).compaction_limit_calibrated() == 96_256
    small = TurnOptions(context_tokens=40_000).compaction_limit_calibrated()
    assert small == 40_000 - 10_000 - 2048  # a quarter of a small window, as the fallback keeps


class Counting:
    """A server with no /tokenize that reports the prompt it was sent at
    `ratio` real tokens per estimated one, the way Strata's usage does."""

    def __init__(self, rounds: list[list[Any]], ratio: float | None) -> None:
        self.rounds, self.ratio = list(rounds), ratio
        self.sizes: list[int] = []
        self.caps: list[int] = []
        self.last: list[dict[str, Any]] | None = None

    def __enter__(self) -> Counting:
        return self

    def __exit__(self, *_: object) -> None:
        return None

    def stream_chat(self, messages: Any, **kwargs: Any) -> Iterator[Any]:
        raw = estimate_tokens(list(messages)) + TurnOptions().tool_tokens()
        self.sizes.append(raw)
        self.last = [dict(m) for m in messages]
        self.caps.append(kwargs["max_tokens"])
        yield from self.rounds.pop(0) if self.rounds else finish()
        if self.ratio is not None:
            yield StreamUsage(prompt_tokens=int(raw * self.ratio), completion_tokens=5)


def _auto(root: Path, client: Counting, run_id: str = "cal") -> AutoResult:
    options = AutoOptions(task=TASK, repo=root, run_id=run_id, arm="E", context_tokens=WINDOW)
    return run_auto(options, cast(VllmClient, client))


def _reads(n: int) -> list[list[Any]]:
    return [[call("read_file", f"r{i}", path="big.py", limit=BIG_LINES)] for i in range(n)]


def _compactions(result: AutoResult) -> list[Any]:
    return [s for s in read_spans(result.journal) if s.name == COMPACTION_SPAN]


def test_a_run_whose_server_reports_usage_compacts_at_its_real_size(
    repo: Path,  # noqa: F811
    tmp_path: Path,
) -> None:
    """Seven ~8k-token reads: ~57k estimated, ~70k real at ratio 1.22. Under
    the blanket x2.0 that crosses the ~46.6k estimate limit; calibrated, the
    limit is 96,256 real tokens and nothing is compacted."""
    _commit_big(repo)
    calibrated = Counting([*_reads(7), finish()], ratio=1.22)
    result = _auto(repo, calibrated)
    assert max(calibrated.sizes) * 1.22 > 60_000  # harness: the prompt really grew
    assert _compactions(result) == []

    blind = Counting([*_reads(7), finish()], ratio=None)  # known-bad: no usage to calibrate on
    assert _compactions(_auto(repo, blind, run_id="blind")) != []


def test_a_ratio_below_the_floor_is_not_believed(repo: Path) -> None:  # noqa: F811
    """A server (or a fake) reporting a handful of prompt tokens for a 40k
    prompt would otherwise shrink the estimate toward nothing and offer the
    reply the whole window: an HTTP 400 on a server that enforces it."""
    _commit_big(repo)
    liar = Counting([*_reads(5), finish()], ratio=0.0001)
    _auto(repo, liar)
    floored = int(liar.sizes[-1] * RATIO_FLOOR * CALIBRATION_MARGIN)
    assert liar.caps[-1] <= WINDOW - floored  # known-good: the prompt is charged at the floor
    assert liar.sizes[-1] > 30_000  # harness: a prompt the lie would have hidden


def _talks(n: int) -> list[list[Any]]:
    """Rounds that grow the prompt with the model's own words (~2k tokens
    each), which stage 1 cannot shrink as it does a large tool result."""
    return [
        [StreamToken(stream="content", text=f"note {i} " + "w" * 8000), call("list_files", f"l{i}")]
        for i in range(n)
    ]


def test_a_run_compacts_rarely_because_each_pass_reaches_the_target(repo: Path) -> None:  # noqa: F811
    """Sixty ~2k-token rounds past a 96,256 limit. Compacting only to just
    under the limit fires again on nearly every round (the 163-pass run); to
    0.7 of it, a pass leaves room for a dozen rounds before the next."""
    client = Counting([*_talks(60), finish()], ratio=1.22)
    passes = _compactions(_auto(repo, client))
    assert passes  # harness: the run did outgrow the limit
    assert len(passes) <= 5, len(passes)
