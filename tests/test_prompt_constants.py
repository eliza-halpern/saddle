"""FEEDFIX item 1: a reporting-only row for the constants a task prompt names.

SURVIVORRES found no survivor ordering that surfaces the T5 rounding bug on
the broken tree, but a name check sees it: the source names
`ROUND_HALF_EVEN`, a sibling of the `ROUND_HALF_UP` the prompt names, and
never `ROUND_HALF_UP`. The row words it as untested behaviour the prompt
names, never as a bug, and changes no verdict.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path
from typing import Any, cast

import pytest

from saddle.auditor import _test_side
from saddle.auto import AutoOptions, AutoResult, run_auto
from saddle.journal import attempt_sidecar_path, read_spans
from saddle.packet import compile_packet
from saddle.prompt_constants import UNTESTED, check, items, named, superseded, tree_sources
from saddle.vllm import ToolCall, VllmClient

# The two T5 rules the check reads (the internal benchmark's T5 prompt, rules 3 and 5).
T5 = (
    "3. **Rounding.** `ROUND_HALF_UP`, quantized to 2 decimal places for\n"
    "   USD/EUR and 0 decimal places for JPY.\n"
    "5. **Fees (`fees.py`).** Per-currency table\n"
    '   `FEES = {"USD": Decimal("0.30"), "EUR": Decimal("0.25"),\n'
    '   "JPY": Decimal("30")}` (supersedes `FLAT_FEE`).\n'
)
FEES = 'FEES = {"USD": 1, "EUR": 1, "JPY": 1}\n'
RIGHT = "from decimal import ROUND_HALF_UP\n" + FEES
BROKEN = "from decimal import ROUND_HALF_EVEN\n" + FEES


def test_named_reads_underscored_names_anywhere_and_words_only_in_code_spans() -> None:
    assert named(T5) == ["EUR", "FEES", "FLAT_FEE", "JPY", "ROUND_HALF_UP", "USD"]
    # "USD/EUR" in prose is not a code span; "REQ" has no underscore
    assert named("REQ-009 rounds with ROUND_HALF_UP in USD") == ["ROUND_HALF_UP"]
    assert superseded(T5) == ["FLAT_FEE"]
    assert superseded("`A_B` replaces `C_D`; use X instead of `E`") == ["C_D", "E"]


def test_a_sibling_constant_produces_the_row() -> None:
    """Known-bad (DETECT breakage a): HALF_EVEN where the prompt says HALF_UP."""
    record = check(T5, {"money.py": BROKEN})
    assert record["rows"] == [{"constant": "ROUND_HALF_UP", "siblings": ["ROUND_HALF_EVEN"]}]
    assert items(record) == [
        f"{UNTESTED}: the prompt names ROUND_HALF_UP; the source never does and names "
        "ROUND_HALF_EVEN of the same family instead."
    ]


def test_the_right_constant_and_a_superseded_one_produce_nothing() -> None:
    """Known-good: HALF_UP present; FLAT_FEE dropped because rule 5 replaces it."""
    assert check(T5, {"money.py": RIGHT})["rows"] == []
    # both spellings present: the named one is there, so no row
    assert check(T5, {"a.py": RIGHT, "b.py": BROKEN})["rows"] == []


def test_a_constant_missing_entirely_is_named_without_siblings() -> None:
    record = check(T5, {"money.py": FEES})
    assert record["rows"] == [{"constant": "ROUND_HALF_UP", "siblings": []}]
    assert items(record) == [f"{UNTESTED}: the prompt names ROUND_HALF_UP; the source never does."]
    # a one-word constant has no family, only presence
    assert check("`USD` and `GBP`", {"m.py": '"USD"'})["rows"] == [
        {"constant": "GBP", "siblings": []}
    ]


def test_tree_sources_skips_tests_and_tool_directories(tmp_path: Path) -> None:
    for rel in ("money.py", "pkg/fees.py", "tests/test_money.py", "test_x.py", ".saddle/r.py"):
        (tmp_path / rel).parent.mkdir(parents=True, exist_ok=True)
        (tmp_path / rel).write_text("X = 1\n")
    (tmp_path / "notes.txt").write_text("ROUND_HALF_UP\n")
    (tmp_path / "bad.py").write_bytes(b"\xff\xfe")
    assert sorted(tree_sources(tmp_path, _test_side)) == ["money.py", "pkg/fees.py"]


# -- the wiring: sealed at the end of a run, rendered as one packet row ---------


def _call(name: str, call_id: str, **arguments: Any) -> ToolCall:
    return ToolCall(id=call_id, name=name, arguments=json.dumps(arguments))


class Script:
    def __init__(self, turns: list[list[ToolCall]]) -> None:
        self.turns = turns

    def stream_chat(self, messages: Any, **kwargs: Any) -> Any:
        return iter(self.turns.pop(0) if self.turns else [_call("finish", "f", summary="done")])


def _run(tmp_path: Path, task: str, new: str) -> AutoResult:
    root = tmp_path / "repo"
    root.mkdir()
    (root / "money.py").write_text(RIGHT)
    for argv in (["init", "-q", "-b", "main"], ["add", "-A"], ["commit", "-q", "-m", "i"]):
        subprocess.run(
            ["git", "-C", str(root), "-c", "user.name=t", "-c", "user.email=t@t", *argv],
            check=True,
        )
    edit = _call("edit_file", "e", path="money.py", old=RIGHT, new=new)
    options = AutoOptions(task=task, repo=root, run_id="r1", arm="E")
    return run_auto(options, cast(VllmClient, Script([[edit]])))


def _sealed(result: AutoResult) -> dict[str, Any]:
    span = [s for s in read_spans(result.journal) if s.name.startswith("auto:")][-1]
    return cast(
        dict[str, Any], json.loads(attempt_sidecar_path(result.journal, span.span_id).read_text())
    )


def test_a_run_on_the_broken_tree_seals_the_row_and_the_packet_shows_it(tmp_path: Path) -> None:
    result = _run(tmp_path, T5, BROKEN)
    assert result.outcome == "finished"
    assert _sealed(result)["prompt_constants"]["rows"][0]["constant"] == "ROUND_HALF_UP"
    row = next(r for r in compile_packet(result.journal).rows if r.key == "prompt-constants")
    assert row.status == "not-proven"
    assert row.text.startswith("The task names 6 constants; the source never names 1 of them.")
    assert row.items == tuple(items(_sealed(result)["prompt_constants"]))


def test_a_run_on_the_right_tree_seals_an_empty_row_and_reads_observed(tmp_path: Path) -> None:
    result = _run(tmp_path, T5, RIGHT + "# fees\n")
    assert _sealed(result)["prompt_constants"]["rows"] == []
    row = next(r for r in compile_packet(result.journal).rows if r.key == "prompt-constants")
    assert row.status == "observed"
    assert row.items == ()


@pytest.mark.parametrize("task", ["make it add", "use rounding half up"])
def test_a_task_that_names_no_constant_seals_nothing_and_has_no_row(
    tmp_path: Path, task: str
) -> None:
    result = _run(tmp_path, task, BROKEN)
    assert "prompt_constants" not in _sealed(result)
    assert "prompt-constants" not in {r.key for r in compile_packet(result.journal).rows}
