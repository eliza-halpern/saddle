"""The packet's Cost row tells an uncapped run it had no limit, never "of 0s" (#187).

A dogfood run's packet (uncapped, as every run is by default) read "134m 01s of 0s
· 310.3k measured of 0 generated tokens": the row printed each budget as a number,
as the run state did before #187 fixed it there.

Known-bad: an uncapped run's row says "of 0". Known-good: a capped run's row reads
each budget, and with one cap and not the other each reads by its own rule.
"""

from __future__ import annotations

from pathlib import Path

from test_check_tool import Scripted, edit, repo, run

from saddle.auto import AutoResult
from saddle.packet import _cost, compile_packet

__all__ = ["repo"]  # the fixture, shared with test_check_tool


def cost_row(result: AutoResult) -> str:
    return next(r for r in compile_packet(result.journal).rows if r.key == "cost").text


def test_an_uncapped_runs_cost_row_says_it_had_no_limit(repo: Path) -> None:
    result, _ = run(repo, Scripted([[edit("e", "a - b", "a + b")]]))
    text = cost_row(result)
    assert ", no time limit · " in text
    assert " generated tokens, no cap · " in text
    assert " of 0" not in text


def test_a_capped_runs_cost_row_reads_each_budget(repo: Path) -> None:
    result, _ = run(
        repo,
        Scripted([[edit("e", "a - b", "a + b")]]),
        time_budget_s=3600,
        token_budget=1_000_000,
    )
    text = cost_row(result)
    assert " of 60m 00s · " in text
    assert " of 1000.0k generated tokens · " in text
    assert "no time limit" not in text
    assert "no cap" not in text


def test_with_one_cap_each_budget_reads_by_its_own_rule() -> None:
    assert _cost(90, 600, "250 measured", 0) == (
        "1m 30s of 10m 00s · 250 measured generated tokens, no cap"
    )
    assert _cost(90, 0, "250 measured", 1000) == (
        "1m 30s, no time limit · 250 measured of 1.0k generated tokens"
    )
    assert _cost(90, 1, "250 measured", 1) == (  # the smallest cap is a cap
        "1m 30s of 1s · 250 measured of 1 generated tokens"
    )
