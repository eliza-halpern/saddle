"""Each round's whole reasoning is kept with the run, redacted, as narrative (#126).

The outcome record keeps the first `MAX_THINKING_CHARS` of a whole run's reasoning,
so one 80-minute run kept 4,025 of about 454,566 characters, and the one question
that mattered about it (why the model chose not to call `dispute`) could be
answered only from an outside logging relay.

Known-bad: a round's reasoning longer than the cap is cut, or a secret-shaped
string in it reaches the run directory. Known-good: each round with reasoning
seals one sidecar beside its spend record holding the whole redacted text, a
round with none seals nothing, `--no-save-reasoning` keeps none, `saddle
reasoning` prints the rounds in order and names a record that does not match the
ledger instead of printing it, and nothing that judges the run reads any of it.
"""

from __future__ import annotations

import io
import json
import os
from pathlib import Path
from typing import Any

from test_auto import namespace, repo
from test_usage import FakeServer, _delta, _finish_call, _sealed, _sse

from saddle import cli
from saddle.auto import AutoOptions, AutoResult, run_auto
from saddle.journal import MAX_THINKING_CHARS, STAR, attempt_sidecar_path, read_spans
from saddle.packet import compile_packet

__all__ = ["repo"]  # the fixture, shared with test_auto

# Built in two parts, so the leak guard can scan this file.
SECRET = "s" + "k-" + "dummyNotARealKey0123456789"
LONG_1 = "first round: " + "weighing calc.py and its test. " * (MAX_THINKING_CHARS // 20)
LONG_2 = "third round: " + "the fix holds; finishing. " * (MAX_THINKING_CHARS // 20)
THOUGHT_1 = f"{LONG_1} I will not paste the key {SECRET} anywhere."


def _read_call() -> dict[str, Any]:
    call = {"index": 0, "id": "r1", "type": "function"}
    call["function"] = {"name": "read_file", "arguments": json.dumps({"path": "calc.py"})}
    return _delta({"tool_calls": [call]})


def _script() -> list[str]:
    # round 1 thinks at length (with a key in it), round 2 thinks nothing, round 3 finishes
    return [
        _sse(_delta({"reasoning_content": THOUGHT_1}), _read_call()),
        _sse(_read_call()),
        _sse(_delta({"reasoning_content": LONG_2}), _finish_call()),
    ]


def _run(repo_path: Path, *, save: bool = True, run_id: str = "s1") -> AutoResult:
    server = FakeServer(_script())
    options = AutoOptions(
        task="make add add", repo=repo_path, run_id=run_id, arm="E", save_reasoning=save
    )
    return run_auto(options, server.client())


def _spend_sidecars(result: AutoResult) -> list[dict[str, Any] | None]:
    """Each round's spend sidecar, in order; None for a round that sealed none."""
    out: list[dict[str, Any] | None] = []
    for span in read_spans(result.journal):
        if span.name == "auto:spend":
            path = attempt_sidecar_path(result.journal, span.span_id)
            out.append(json.loads(path.read_text()) if span.attempt_hash else None)
    return out


def test_each_round_with_reasoning_keeps_all_of_it_and_a_round_with_none_keeps_nothing(
    repo: Path,
) -> None:
    result = _run(repo)
    first, second, third = _spend_sidecars(result)
    assert first is not None
    assert third is not None
    assert first["thinking"] == THOUGHT_1.replace(SECRET, STAR)
    assert len(first["thinking"]) > MAX_THINKING_CHARS  # whole: never cut to the cap
    assert "[truncated" not in first["thinking"]
    assert second is None  # no reasoning, no record
    assert third["thinking"] == LONG_2


def test_a_secret_in_the_reasoning_appears_nowhere_in_the_run_directory(repo: Path) -> None:
    result = _run(repo)
    run_dir = result.journal.parent
    seen = 0
    for root, _dirs, files in os.walk(run_dir):
        for name in files:
            data = (Path(root) / name).read_bytes()
            seen += 1
            assert SECRET.encode() not in data, Path(root) / name
    assert seen > 2  # the journal and its sidecars were read, not an empty directory


def test_without_the_option_no_round_keeps_its_reasoning_and_the_seal_says_so(
    repo: Path,
) -> None:
    result = _run(repo, save=False)
    assert all(s is None or "thinking" not in s for s in _spend_sidecars(result))
    sealed, _ = _sealed(result)
    assert sealed["save_reasoning"] is False
    on, _ = _sealed(_run(repo, run_id="s2"))
    assert "save_reasoning" not in on  # on by default: the seal's keys are as before


def test_nothing_that_judges_the_run_reads_the_saved_reasoning(repo: Path) -> None:
    verdicts = {}
    for save in (True, False):
        result = _run(repo, save=save, run_id=f"v{save}")
        packet = compile_packet(result.journal)
        verdicts[save] = (result.outcome, packet.verdict)
    assert verdicts[True] == verdicts[False]


def test_saddle_reasoning_prints_the_rounds_in_order(repo: Path) -> None:
    result = _run(repo)
    out = io.StringIO()
    assert cli.main(["reasoning", str(result.journal)], stdout=out) == 0
    text = out.getvalue()
    assert text.index("--- round 1 of 3") < text.index(LONG_1[:40]) < text.index("--- round 3 of 3")
    assert text.index("--- round 3 of 3") < text.index(LONG_2[:40])
    assert "--- round 2 of 3" not in text  # it thought nothing
    assert SECRET not in text


def test_a_record_that_does_not_match_the_ledger_is_refused_never_printed(repo: Path) -> None:
    result = _run(repo)
    span = next(s for s in read_spans(result.journal) if s.name == "auto:spend")
    path = attempt_sidecar_path(result.journal, span.span_id)
    path.write_text(path.read_text().replace("first round", "edited round"))
    out = io.StringIO()
    assert cli.main(["reasoning", str(result.journal)], stdout=out) == 1
    text = out.getvalue()
    assert text.startswith("error: ")
    assert "failed verification: attempt-sidecar" in text
    assert "edited round" not in text
    assert "--- round" not in text


def test_a_run_that_saved_none_says_so(repo: Path) -> None:
    result = _run(repo, save=False)
    out = io.StringIO()
    assert cli.main(["reasoning", str(result.journal)], stdout=out) == 0
    assert out.getvalue() == f"no saved reasoning in {result.journal} (3 rounds)\n"


def test_a_reply_cut_with_saving_off_keeps_its_partial_text_and_no_reasoning(
    repo: Path,
) -> None:
    from test_reply_budget import FOREVER, Server, auto

    result = auto(repo, Server([104, FOREVER, 0]), token_budget=1_000, save_reasoning=False)
    sealed = [s for s in _spend_sidecars(result) if s is not None]
    assert sealed  # the cut replies' partial text is still kept
    assert all(s["partial"] is True and "thinking" not in s for s in sealed)
    out = io.StringIO()
    assert cli.main(["reasoning", str(result.journal)], stdout=out) == 0
    assert out.getvalue().startswith("no saved reasoning in ")


def test_an_unreadable_journal_is_an_error(tmp_path: Path) -> None:
    out = io.StringIO()
    missing = tmp_path / "missing.jsonl"
    assert cli.main(["reasoning", str(missing)], stdout=out) == 1
    assert out.getvalue() == f"error: no journal at {missing}\n"  # never "no saved reasoning"


def test_the_cli_flag_is_on_by_default_and_reaches_the_run(repo: Path) -> None:
    assert cli.build_parser().parse_args(["auto", "t"]).save_reasoning is True
    off = cli.build_parser().parse_args(["auto", "t", "--no-save-reasoning"])
    assert off.save_reasoning is False
    server = FakeServer(_script())
    code = cli.run_auto_command(
        namespace(repo, save_reasoning=False), server.client(), stdout=io.StringIO()
    )
    assert code == 0
    journal = next((repo / ".saddle" / "runs").glob("*/proofs.jsonl"))
    spends = [s for s in read_spans(journal) if s.name == "auto:spend"]
    assert spends
    assert not any(s.attempt_hash for s in spends)
