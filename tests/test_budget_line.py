"""The run state tells an uncapped run it has no limit, never "0 left" (#187).

Runs have no caps by default: a budget of `engine.NO_LIMIT` (0) means the run is
never stopped for spending. `memory.run_state` printed the budget as numbers, so
after its first compaction every uncapped run read "49877 of 0 generated tokens
used, 0 left; 761s of 0s used, 0s left" (seen in a dogfood run): the text said
the budget was spent while the engine would never stop the run for it.

Known-bad: an uncapped run is told "0 left". Known-good: a capped run reads as
before, and with one cap and not the other each reads by its own rule.
"""

from __future__ import annotations

from pathlib import Path

from saddle.engine import NO_LIMIT, AutoRun, RunBudget, _run_state
from saddle.journal import read_spans
from saddle.memory import run_state


def budget(token_budget: int, time_budget_s: float) -> str:
    state = run_state(
        files=[],
        test=None,
        audit="",
        spent_tokens=250,
        token_budget=token_budget,
        elapsed_s=90,
        time_budget_s=time_budget_s,
    )
    (line,) = [ln for ln in state.splitlines() if ln.startswith("- budget:")]
    return line


def test_an_uncapped_run_is_told_it_has_no_limit() -> None:
    assert budget(NO_LIMIT, NO_LIMIT) == (
        "- budget: 250 generated tokens used, no cap; 90s used, no time limit"
    )


def test_a_capped_run_reads_as_before_and_each_cap_by_its_own_rule() -> None:
    assert budget(1000, 600) == (
        "- budget: 250 of 1000 generated tokens used, 750 left; 90s of 600s used, 510s left"
    )
    assert budget(1000, NO_LIMIT) == (
        "- budget: 250 of 1000 generated tokens used, 750 left; 90s used, no time limit"
    )
    assert budget(NO_LIMIT, 600) == (
        "- budget: 250 generated tokens used, no cap; 90s of 600s used, 510s left"
    )
    assert budget(1, 1) == (  # the smallest cap is a cap
        "- budget: 250 of 1 generated tokens used, 0 left; 90s of 1s used, 0s left"
    )


def test_the_engines_default_run_is_told_it_has_no_limit() -> None:
    """The engine passes its run's own budget, `NO_LIMIT` by default."""
    auto = AutoRun(budget=RunBudget(time_s=NO_LIMIT, tokens=NO_LIMIT), run_span="s")
    (line,) = [ln for ln in _run_state(auto).splitlines() if ln.startswith("- budget:")]
    assert line.startswith("- budget: 0 generated tokens used, no cap; ")
    assert line.endswith("s used, no time limit")


def test_each_rounds_spend_record_tells_an_uncapped_run_it_has_no_cap(tmp_path: Path) -> None:
    """The third place of the same rule: `saddle explain` prints each round's spend
    record, which read "310312 of 0 spent" for every round of an uncapped run."""
    from test_reply_budget import Server, auto, git

    details = {}
    for cap in (NO_LIMIT, 1_000_000):
        root = tmp_path / f"c{cap}"
        root.mkdir()
        (root / "calc.py").write_text("x = 1\n")
        git(root, "init", "-q", "-b", "main")
        git(root, "add", "-A")
        git(root, "commit", "-q", "-m", "init")
        result = auto(root, Server([104, 0]), token_budget=cap)
        details[cap] = [s.detail for s in read_spans(result.journal) if s.name == "auto:spend"]
    assert details[NO_LIMIT]
    assert all(d.endswith(" spent, no cap") and " of 0 " not in d for d in details[NO_LIMIT])
    assert all(d.endswith(" of 1000000 spent") for d in details[1_000_000])
