"""Trap signatures name whose block a run's stop was, from its records alone (#164).

Each signature has a known-good instance it fires on, from the run that was trapped
that way, and known-bad ones it must not fire on: the same text where the model
could act on it, a name that is only a prefix, the same failure on an unchanged
tree. The verdict gives no block to a run that ended with a verdict, and "unclassified"
when no signature fired, never a guess.
"""

from __future__ import annotations

import json
from typing import Any

import pytest

from saddle.conversation import Request
from saddle.engine import SUMMARY_NAMES_ABSENT
from saddle.feed import FLIPS_FIRST
from saddle.journal import AUDIT_QUESTION_STOP, AUTO_START, STALL_STOP, SpanRecord, build_span
from saddle.traps import (
    environment,
    misreport,
    overflow,
    repeat,
    summary_names,
    unsatisfiable,
    verdict,
)
from saddle.triage import Call, Round, Timeline

STRATA = (
    'stopped: model error: server returned HTTP 400: {"error": {"type": '
    '"invalid_request_error", "message": "prompt (191553 tokens) + max tokens (8192) '
    'exceeds the context (131072); requests are never truncated."}}'
)
VLLM = (
    "stopped: model error: server returned HTTP 400: This model's maximum context length "
    "is 131072 tokens. However, you requested 140000 tokens."
)


def span(name: str, *argv: str, detail: str = "", exit_code: int = 0) -> SpanRecord:
    return build_span(
        node_id="chat#1",
        argv=[name, *argv],
        duration_ms=0,
        exit_code=exit_code,
        detail=detail,
        kind="tool" if not name.startswith(("auto:", "audit")) else "agent",
    )


def call(name: str, result: str, call_id: str = "c", **arguments: Any) -> Call:
    text = json.dumps(arguments)
    return Call(call_id, name, text, result, span(name, text, detail=result[:500]))


def request(number: int, *roles: str) -> Request:
    return Request(number, 0.0, 9, 0, [{"role": role, "content": ""} for role in roles], [])


def round_(
    number: int,
    *calls: Call,
    spans: tuple[SpanRecord, ...] = (),
    asked: Request | None = None,
) -> Round:
    sealed = tuple(c.span for c in calls if c.span is not None)
    return Round(number, {"request": number}, asked, None, calls, (*sealed, *spans), None)


def run(*rounds: Round, outcome: str | None = "stopped: audit unresolved") -> Timeline:
    end = None
    if outcome is not None:
        name = "auto:finished" if outcome == "finished" else "auto:stopped"
        end = span(name, detail=outcome)
    return Timeline(span(AUTO_START, "t"), rounds, end, "whole")


def check_on(tree: str, note: str = FLIPS_FIRST) -> str:
    return f"[audit check 1 on tree {tree}: FAIL]\n{note}\n- test-changes (tier 1): fail"


def finish(summary: str, result: str) -> Call:
    return call("finish", result, "f", summary=summary)


# -- overflow ----------------------------------------------------------------------


@pytest.mark.parametrize("said", [STRATA, VLLM])
def test_a_request_refused_as_over_the_window_is_an_overflow(said: str) -> None:
    sign = overflow(run(round_(1), round_(2), outcome=said))
    assert sign is not None
    assert (sign.block, sign.rounds) == ("harness", (2,))
    assert "over the window" in sign.why


@pytest.mark.parametrize(
    "said",
    [
        "stopped: model error: server returned HTTP 500: internal error",
        "stopped: audit unresolved; the prompt exceeds the context of the page",
        None,
    ],
)
def test_any_other_stop_is_no_overflow(said: str | None) -> None:
    assert overflow(run(round_(1), outcome=said)) is None


# -- unsatisfiable -----------------------------------------------------------------


def test_a_check_answered_only_a_finish_summary_clears_it_on_changed_trees() -> None:
    seen = run(round_(3, call("check", check_on("aaa"))), round_(7, call("check", check_on("bbb"))))
    sign = unsatisfiable(seen)
    assert sign is not None
    assert (sign.block, sign.rounds) == ("harness", (3, 7))


def test_the_same_answer_on_an_unchanged_tree_or_from_finish_is_not_unsatisfiable() -> None:
    same_tree = run(
        round_(1, call("check", check_on("aaa"))), round_(2, call("check", check_on("aaa")))
    )
    assert unsatisfiable(same_tree) is None
    # finish takes a summary: there the note can be met
    from_finish = run(
        round_(1, finish("s", check_on("aaa"))), round_(2, finish("s", check_on("b")))
    )
    assert unsatisfiable(from_finish) is None


# -- misreport ---------------------------------------------------------------------


NO_FLIP = "error: finish refused\n- tests/t.py: 'test_y' [no flip line] -- assertion-changed: x"


@pytest.mark.parametrize(
    "line",
    [
        "flip: tests/t.py::test_y -- the old test rejected a known-good row",
        "flip: tests/t.py::Group::test_y — the old test rejected it",
        "flip: `test_y`: the old test rejected it",
    ],
)
def test_a_test_reported_unlabelled_that_the_summary_names_is_a_misreport(line: str) -> None:
    sign = misreport(run(round_(5, finish(f"done\n{line}", NO_FLIP))))
    assert sign is not None
    assert (sign.block, sign.rounds) == ("harness", (5,))
    assert "test_y" in sign.why


@pytest.mark.parametrize(
    "line",
    [
        "flip: tests/t.py::test_yz -- a different test",
        "flip: test_z -- the same shape as test_y",
        "nothing labelled at all",
    ],
)
def test_a_summary_that_does_not_name_the_test_is_no_misreport(line: str) -> None:
    assert misreport(run(round_(5, finish(line, NO_FLIP)))) is None


def test_a_finish_whose_arguments_do_not_parse_is_no_misreport() -> None:
    broken = Call("f", "finish", "not json", NO_FLIP, None)
    assert misreport(run(round_(1, broken))) is None


# -- environment -------------------------------------------------------------------


FAILING = (
    "'python -m pytest -q' exited 1: 17 failing: tests/test_c.py::test_a, "
    "tests/test_c.py::test_b and 15 more; impact: 45 of 214 test files ran"
)


def audit(tree: str, failing: str = FAILING) -> tuple[SpanRecord, ...]:
    body = {"detail": failing, "gate": "tests", "tier": 1, "verdict": "fail"}
    return (
        span("audit-tier1:tests", detail=json.dumps(body), exit_code=1),
        span("audit:delivered", "checkpoint", detail=f"[audit checkpoint 1 on tree {tree}: FAIL]"),
    )


def edit(path: str) -> Call:
    return call("edit_file", "ok", "e", path=path, old="a", new="b")


def test_the_same_failures_on_changed_trees_outside_the_runs_edits_are_the_environment() -> None:
    seen = run(round_(2, edit("src/x.py"), spans=audit("aaa")), round_(4, spans=audit("bbb")))
    sign = environment(seen)
    assert sign is not None
    assert (sign.block, sign.rounds) == ("harness", (2, 4))
    assert "17 test(s)" in sign.why


def test_failures_in_a_file_the_run_edited_are_the_runs_own() -> None:
    seen = run(
        round_(2, edit("tests/test_c.py"), spans=audit("aaa")), round_(4, spans=audit("bbb"))
    )
    assert environment(seen) is None


def test_failures_on_one_unchanged_tree_or_that_differ_are_not_the_environment() -> None:
    assert environment(run(round_(2, spans=audit("aaa")), round_(4, spans=audit("aaa")))) is None
    other = FAILING.replace("test_b and 15", "test_q and 15")
    assert (
        environment(run(round_(2, spans=audit("aaa")), round_(4, spans=audit("b", other)))) is None
    )


def test_a_failing_list_cut_by_the_ledger_drops_only_its_last_name() -> None:
    cut = "'python -m pytest -q' exited 1: 3 failing: tests/a.py::t1, tests/a.py::t2, tests/a.py::t"
    seen = run(round_(1, spans=audit("aaa", cut)), round_(2, spans=audit("bbb", cut)))
    sign = environment(seen)
    assert sign is not None
    assert "tests/a.py::t1, tests/a.py::t2" in sign.why
    assert "tests/a.py::t," not in sign.why


# -- the model's own ---------------------------------------------------------------


def test_a_summary_returned_for_naming_absent_code_is_the_models() -> None:
    named = SUMMARY_NAMES_ABSENT.format(names="`helper`")
    sign = summary_names(run(round_(9, finish("s", named))))
    assert sign is not None
    assert (sign.block, sign.rounds) == ("model", (9,))
    assert summary_names(run(round_(9, finish("s", "error: finish refused")))) is None


def test_the_same_calls_with_the_same_results_three_rounds_running_are_the_models() -> None:
    same = [round_(n, call("run_command", "exit 1\nboom", command="pytest")) for n in (4, 5, 6)]
    sign = repeat(run(round_(3, edit("a.py")), *same))
    assert sign is not None
    assert (sign.block, sign.rounds) == ("model", (4, 5, 6))
    assert repeat(run(*same[:2])) is None
    moved = round_(6, call("run_command", "exit 0", command="pytest"))
    assert repeat(run(*same[:2], moved)) is None


# -- the verdict -------------------------------------------------------------------


def harness_run(*, last_role: str = "tool", outcome: str = "stopped: audit unresolved") -> Timeline:
    return run(
        round_(1, edit("a.py"), asked=request(1, "system", "user")),
        round_(2, call("check", check_on("aaa")), asked=request(2, "system", "user", "tool")),
        round_(3, edit("b.py"), asked=request(3, "system", "user", "tool", "tool")),
        round_(
            4,
            call("check", check_on("bbb")),
            call("run_command", "exit 0", "r", command="ls"),
            asked=request(4, "system", "tool", last_role),
        ),
        outcome=outcome,
    )


def test_a_harness_block_names_its_trap_and_where_to_resume() -> None:
    found = verdict(harness_run())
    assert (found.ended, found.block, found.trap) == ("stopped", "harness", 2)
    assert [sign.name for sign in found.signs] == ["unsatisfiable"]
    assert found.rewind is not None
    assert (found.rewind.request, found.rewind.round) == (2, 2)
    assert found.rewind.edited_after == ("b.py",)
    assert found.rewind.commands_after == 1
    assert found.last_progress == 3  # round 4's check repeats round 2's findings


def test_a_rewind_point_steps_back_to_a_request_ending_on_a_tool_result() -> None:
    seen = harness_run()
    first, second, *rest = seen.rounds
    moved = Round(
        2, second.spend, request(2, "system", "user"), None, second.calls, second.spans, None
    )
    stepped = verdict(Timeline(seen.start, (first, moved, *rest), seen.outcome, seen.log))
    assert stepped.trap == 2
    assert stepped.rewind is None  # round 1's request ends on the task: nothing to resume from


@pytest.mark.parametrize(
    "outcome",
    [
        "finished",
        f"stopped: {AUDIT_QUESTION_STOP}1 question(s): x",
        "stopped: needs you: the run is blocked: y",
    ],
)
def test_a_run_that_ended_with_a_verdict_has_no_block_and_keeps_its_signs(outcome: str) -> None:
    found = verdict(harness_run(outcome=outcome))
    assert found.block is None
    assert found.ended in ("finished", "needs you")
    assert [sign.name for sign in found.signs] == ["unsatisfiable"]
    assert (found.trap, found.rewind) == (None, None)


def test_a_stall_eject_is_a_stop_not_a_verdict() -> None:
    found = verdict(harness_run(outcome=f"stopped: {STALL_STOP}: no tool call in 20 minutes"))
    assert (found.ended, found.block) == ("stopped", "harness")


def test_only_the_models_signs_make_the_models_block_and_none_make_it_unclassified() -> None:
    named = SUMMARY_NAMES_ABSENT.format(names="`helper`")
    assert verdict(run(round_(1, finish("s", named)))).block == "model"
    nothing = verdict(run(round_(1, edit("a.py"))))
    assert (nothing.block, nothing.signs, nothing.trap) == ("unclassified", (), None)
    assert verdict(run(round_(1), outcome=None)).ended == "none"


def test_without_a_log_the_signs_read_the_ledger_and_no_rewind_is_offered() -> None:
    ledger_only = run(
        round_(2, spans=(span("check", "{}", detail=check_on("aaa")),)),
        round_(5, spans=(span("check", "{}", detail=check_on("bbb")),)),
    )
    found = verdict(ledger_only)
    assert (found.block, found.trap, found.rewind) == ("harness", 2, None)


def test_an_overflow_with_no_rounds_is_still_the_harnesss_block() -> None:
    found = verdict(run(outcome=STRATA))
    assert (found.block, found.trap, found.rewind) == ("harness", None, None)


# -- what the records hold when they hold less -------------------------------------


def test_a_call_the_log_holds_no_result_for_is_read_from_its_span() -> None:
    def unlogged(tree: str) -> Call:
        return Call("c", "check", "{}", None, span("check", "{}", detail=check_on(tree)))

    sign = unsatisfiable(run(round_(1, unlogged("aaa")), round_(2, unlogged("bbb"))))
    assert sign is not None
    assert sign.rounds == (1, 2)


def test_only_tool_spans_with_arguments_are_read_as_calls() -> None:
    frames = (span("auto:spend", "{}"), span("check"))  # an agent span; a call with no arguments
    assert verdict(run(round_(1, spans=frames), round_(2, spans=frames))).signs == ()


@pytest.mark.parametrize(
    "sealed",
    [
        span("edit_file", "not json"),
        span("edit_file", '{"path": "tests/test_c.py"}', exit_code=1),  # refused: nothing changed
        span("write_file", '{"path": 3}'),
    ],
)
def test_an_edit_that_did_not_change_a_named_file_is_not_an_edit(sealed: SpanRecord) -> None:
    seen = run(round_(2, spans=(sealed, *audit("aaa"))), round_(4, spans=audit("bbb")))
    assert environment(seen) is not None  # the failing file was never edited
    assert verdict(run(round_(1, spans=(sealed,)))).last_progress is None


def test_a_failing_tests_finding_that_lists_no_test_is_not_the_environment() -> None:
    collection = "'python -m pytest -q' exited 2: interrupted: 1 error during collection"
    empty = "'python -m pytest -q' exited 1: 0 failing: ; impact: 1 of 2 test files ran"
    for failing in (collection, empty):
        seen = run(round_(1, spans=audit("aaa", failing)), round_(2, spans=audit("bbb", failing)))
        assert environment(seen) is None
