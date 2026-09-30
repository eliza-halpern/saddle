"""Autonomous runs (arm E): a task in, a branch and a verifiable ledger out.

What is pinned is the contract, each half both ways: a run that calls
`finish` leaves its edit on a branch and never in the user's checkout; a
test edit or a syntax-breaking edit is refused as a tool result and a
ledger record; a run that runs out of tokens or time ends `stopped`,
naming the budget, and never `finished`.
"""

from __future__ import annotations

import argparse
import hashlib
import io
import json
import os
import subprocess
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any, ClassVar, cast

import pytest

from saddle import cli
from saddle.anchor import parse_trailers
from saddle.auto import (
    DEFAULT_TEST_ROOTS,
    TASK_TEMPERATURE,
    AutoError,
    AutoOptions,
    AutoResult,
    changed_files,
    create_worktree,
    guarded_test_roots,
    run_auto,
)
from saddle.engine import AUTO_NUDGE, MAX_TOOL_ROUNDS, AutoRun, RunBudget, TurnOptions, run_turn
from saddle.events import ErrorEvent, Event
from saddle.journal import (
    attempt_sidecar_path,
    read_entries,
    read_records,
    read_spans,
    verify_journal,
)
from saddle.tools import REFUSED, ToolContext, execute_tool, is_test_path
from saddle.vllm import StreamToken, ToolCall, VllmClient, VllmRequestError

BUGGY = "def add(a, b):\n    return a - b\n"
FIXED = "def add(a, b):\n    return a + b\n"
TEST = "from calc import add\n\n\ndef test_add():\n    assert add(2, 2) == 4\n"


def git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(repo), "-c", "user.name=t", "-c", "user.email=t@t", *args],
        check=True,
        capture_output=True,
        text=True,
    ).stdout


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    root = tmp_path / "repo"
    (root / "tests").mkdir(parents=True)
    (root / "calc.py").write_text(BUGGY)
    (root / "tests" / "test_calc.py").write_text(TEST)
    git(root, "init", "-q", "-b", "main")
    git(root, "add", "-A")
    git(root, "commit", "-q", "-m", "init")
    (root / "notes.txt").write_text("uncommitted user work\n")
    return root


def snapshot(root: Path) -> dict[str, str]:
    """Every file outside .git and .saddle, by content hash, plus git status."""
    files = {
        str(p.relative_to(root)): hashlib.sha256(p.read_bytes()).hexdigest()
        for p in sorted(root.rglob("*"))
        if p.is_file() and p.relative_to(root).parts[0] not in (".git", ".saddle")
    }
    files["<status>"] = git(root, "status", "--porcelain", "--untracked-files=all")
    files["<head>"] = git(root, "rev-parse", "HEAD")
    return files


def call(name: str, call_id: str = "c1", **arguments: Any) -> ToolCall:
    return ToolCall(id=call_id, name=name, arguments=json.dumps(arguments))


class Scripted:
    """A model that replays rounds, then repeats `tail` forever if given."""

    def __init__(self, rounds: list[list[Any] | BaseException], tail: list[Any] | None = None):
        self.rounds = list(rounds)
        self.tail = tail
        self.asked: list[dict[str, Any]] = []

    def __enter__(self) -> Scripted:
        return self

    def __exit__(self, *exc: object) -> None:
        return None

    def stream_chat(self, messages: Any, **kwargs: Any) -> Iterator[Any]:
        self.asked.append({"messages": [dict(m) for m in messages], **kwargs})
        if not self.rounds:
            return iter(list(self.tail or []))
        step = self.rounds.pop(0)
        if isinstance(step, BaseException):
            raise step
        return iter(step)


def finish(text: str = "fixed add", call_id: str = "f1") -> list[Any]:
    return [call("finish", call_id, summary=text)]


def auto(repo: Path, client: Scripted, **kwargs: Any) -> AutoResult:
    kwargs.setdefault("arm", "E")  # arm E's contract; the audited arms are test_feed.py's
    options = AutoOptions(task="make add add", repo=repo, run_id="r1", **kwargs)
    return run_auto(options, cast(VllmClient, client))


def outcome_span(result: AutoResult) -> Any:
    ends = [s for s in read_spans(result.journal) if s.name.startswith("auto:")]
    assert ends[0].name == "auto:start"
    return ends[-1]


def sidecar(result: AutoResult) -> dict[str, Any]:
    span = outcome_span(result)
    return cast(
        dict[str, Any], json.loads(attempt_sidecar_path(result.journal, span.span_id).read_text())
    )


# -- known-good: a run to finish ----------------------------------------------


def test_finished_run_leaves_a_branch_a_verified_ledger_and_the_checkout_untouched(
    repo: Path,
) -> None:
    before = snapshot(repo)
    client = Scripted([[call("edit_file", path="calc.py", old="a - b", new="a + b")], finish()])
    result = auto(repo, client)

    assert (result.outcome, result.reason) == ("finished", "finish called")
    assert git(repo, "show", f"{result.branch}:calc.py") == FIXED
    assert result.worktree == repo / ".saddle" / "worktrees" / "r1"
    assert snapshot(repo) == before
    assert (repo / "calc.py").read_text() == BUGGY
    assert verify_journal(result.journal) == []
    out = io.StringIO()
    assert cli.run_verify(result.journal, stdout=out) == 0

    spans = read_spans(result.journal)
    start = spans[0]
    assert start.name == "auto:start"
    # each round's spend is sealed before its tool spans
    assert [s.name for s in spans[1:]] == [
        "auto:spend",
        "edit_file",
        "auto:spend",
        "finish",
        "auto:finished",
    ]
    assert {s.parent_id for s in spans[1:]} == {start.span_id}
    (record,) = read_records(result.journal)
    assert record.kind == "auto-finished"
    assert record.gate_outputs == []
    evidence = sidecar(result)
    assert evidence["narrative_label"] == "narrative, not evidence"
    assert evidence["narrative"] == "fixed add"
    assert evidence["files_changed"] == ["calc.py"]
    assert evidence["tool_span_hashes"] == [s.record_hash for s in spans if s.kind == "tool"]
    assert "Narrative (model-written, not evidence):\nfixed add" in git(
        repo, "log", "-1", "--format=%B", result.branch
    )
    # the model was offered finish and told tests are read-only
    asked = client.asked[0]
    assert "finish" in [t["function"]["name"] for t in asked["tools"]]
    assert "read-only" in asked["messages"][0]["content"]
    assert asked["temperature"] == TASK_TEMPERATURE == 1.0


def test_a_second_run_gets_its_own_worktree_and_branch(repo: Path) -> None:
    first = auto(repo, Scripted([finish()]))
    second = run_auto(
        AutoOptions(task="again", repo=repo, run_id="r2", arm="E"),
        cast(VllmClient, Scripted([finish()])),
    )
    assert first.worktree != second.worktree
    assert git(repo, "branch", "--list", "--format=%(refname:short)", "saddle/auto/*").split() == [
        "saddle/auto/r1",
        "saddle/auto/r2",
    ]
    assert (repo / ".saddle" / ".gitignore").read_text() == "*\n"


def test_an_unnamed_run_gets_a_generated_id(repo: Path) -> None:
    result = run_auto(
        AutoOptions(task="t", repo=repo, arm="E"), cast(VllmClient, Scripted([finish()]))
    )
    assert len(result.run_id) == 12
    assert result.branch == f"saddle/auto/{result.run_id}"


def test_a_reply_with_no_tool_call_is_nudged_not_taken_as_done(repo: Path) -> None:
    client = Scripted([[StreamToken(stream="content", text="I think it is done.")], finish()])
    result = auto(repo, client)
    assert result.outcome == "finished"
    assert client.asked[1]["messages"][-1] == {"role": "user", "content": AUTO_NUDGE}


def test_finish_without_a_string_summary_does_not_finish(repo: Path) -> None:
    bad = [ToolCall(id="f0", name="finish", arguments="{not json")]
    client = Scripted([bad, [call("finish", "f1", summary=3)], finish()])
    result = auto(repo, client)
    results = [m for m in client.asked[2]["messages"] if m["role"] == "tool"]
    assert [m["content"] for m in results] == [
        "error: finish needs a string summary argument",
        "error: finish needs a string summary argument",
    ]
    assert result.outcome == "finished"


def test_events_reach_the_caller(repo: Path) -> None:
    seen: list[Event] = []
    options = AutoOptions(task="t", repo=repo, run_id="r1", arm="E")
    run_auto(options, cast(VllmClient, Scripted([finish()])), on_event=seen.append)
    assert seen[0].kind == "turn.start"
    assert seen[-1].kind == "turn.end"


# -- known-bad: the tier-0 guard ------------------------------------------------


def test_a_test_edit_is_refused_seen_by_the_model_and_recorded(repo: Path) -> None:
    edit = call("edit_file", path="tests/test_calc.py", old="== 4", new="== 0")
    client = Scripted([[edit], finish()])
    result = auto(repo, client)

    tool_result = client.asked[1]["messages"][-1]["content"]
    assert tool_result.startswith(REFUSED)
    assert "read-only" in tool_result
    refused = [s for s in read_spans(result.journal) if s.name == "refused:edit_file"]
    assert len(refused) == 1
    assert refused[0].exit_code == 2
    assert git(repo, "show", f"{result.branch}:tests/test_calc.py") == TEST
    assert sidecar(result)["refusals"] == 1
    assert verify_journal(result.journal) == []


def test_test_edits_go_through_when_the_run_allows_them(repo: Path) -> None:
    edit = call("edit_file", path="tests/test_calc.py", old="== 4", new="== 0")
    result = auto(repo, Scripted([[edit], finish()]), allow_test_edits=True)
    assert "== 0" in git(repo, "show", f"{result.branch}:tests/test_calc.py")
    assert "tests are read-only" not in json.dumps(result.reason)


def test_write_file_is_guarded_too(tmp_path: Path) -> None:
    ctx = ToolContext(workdir=tmp_path, protected_tests=DEFAULT_TEST_ROOTS)
    result = execute_tool(
        call("write_file", path="tests/test_new.py", content="x = 1\n"),
        workdir=tmp_path,
        context=ctx,
    )
    assert result.startswith(REFUSED)
    assert not (tmp_path / "tests").exists()


@pytest.mark.parametrize(
    ("path", "protected"),
    [
        ("tests/test_a.py", True),
        ("tests", True),
        ("tests/data/x.json", True),
        ("pkg/test_b.py", True),
        ("pkg/b_test.py", True),
        ("pkg/conftest.py", True),
        ("testsuite.py", False),
        ("pkg/tests_helper.py", False),
        ("src/calc.py", False),
        ("test_data.txt", False),
    ],
)
def test_test_paths(path: str, protected: bool) -> None:
    assert is_test_path(path, ("tests/", "")) is protected


def test_guarded_roots_come_from_pytest_testpaths(tmp_path: Path) -> None:
    assert guarded_test_roots(tmp_path) == DEFAULT_TEST_ROOTS
    (tmp_path / "pyproject.toml").write_text("[tool.pytest.ini_options]\ntestpaths = ['spec']\n")
    assert guarded_test_roots(tmp_path) == ("spec",)
    (tmp_path / "pyproject.toml").write_text("[tool.pytest.ini_options]\ntestpaths = 'spec'\n")
    assert guarded_test_roots(tmp_path) == DEFAULT_TEST_ROOTS
    (tmp_path / "pyproject.toml").write_text("not = [toml")
    assert guarded_test_roots(tmp_path) == DEFAULT_TEST_ROOTS


def test_a_syntax_breaking_edit_is_refused_with_the_error(repo: Path) -> None:
    edit = call("edit_file", path="calc.py", old="return a - b", new="return a +")
    client = Scripted([[edit], finish()])
    result = auto(repo, client)
    tool_result = client.asked[1]["messages"][-1]["content"]
    assert tool_result.startswith(REFUSED)
    assert "SyntaxError" in tool_result
    assert "(line 2)" in tool_result
    assert git(repo, "show", f"{result.branch}:calc.py") == BUGGY
    assert [s.name for s in read_spans(result.journal)][1:3] == ["auto:spend", "refused:edit_file"]


def test_syntax_guard_cases(tmp_path: Path) -> None:
    ctx = ToolContext(workdir=tmp_path, syntax_guard=True)

    def run(**arguments: Any) -> str:
        return execute_tool(call("write_file", **arguments), workdir=tmp_path, context=ctx)

    assert run(path="new.py", content="def f(:\n").startswith(REFUSED)
    assert run(path="nul.py", content="x = 1\0\n").startswith(REFUSED)
    assert not (tmp_path / "new.py").exists()
    assert run(path="notes.txt", content="def f(:\n").startswith("created")
    (tmp_path / "broken.py").write_text("def f(:\n")
    # a file already broken may be written, or it could never be repaired
    assert not run(path="broken.py", content="def f(:\n  pass(\n").startswith("error")
    assert not run(path="ok.py", content="x = 1\n").startswith("error")


def test_the_guards_are_off_in_chat(tmp_path: Path) -> None:
    ctx = ToolContext(workdir=tmp_path)
    result = execute_tool(
        call("write_file", path="tests/test_x.py", content="def f(:\n"),
        workdir=tmp_path,
        context=ctx,
    )
    assert result.startswith("created")


# -- known-bad: honest stops -----------------------------------------------------


def reading_round(n: int) -> list[Any]:
    return [
        StreamToken(stream="reasoning", text="x" * 400),
        call("read_file", f"r{n}", path="calc.py"),
    ]


def test_token_budget_exhausted_ends_stopped_not_finished(repo: Path) -> None:
    edit = call("edit_file", path="calc.py", old="a - b", new="a + b")
    run_cmd = call("run_command", "c2", command="true")
    client = Scripted(
        [[StreamToken(stream="reasoning", text="y" * 400), edit, run_cmd]], tail=reading_round(0)
    )
    result = auto(repo, client, token_budget=300)

    assert result.outcome == "stopped"
    assert result.reason.startswith("token budget exhausted")
    names = [s.name for s in read_spans(result.journal)]
    assert "auto:finished" not in names
    assert "finish" not in names
    span = outcome_span(result)
    assert span.name == "auto:stopped"
    assert span.exit_code == 3
    assert "token budget exhausted" in span.detail
    assert "files changed: calc.py" in span.detail
    assert "commands run: 1" in span.detail
    evidence = sidecar(result)
    assert evidence["commands"] == ["true"]
    assert evidence["narrative"] == ""
    assert read_records(result.journal)[0].kind == "auto-stopped"
    assert verify_journal(result.journal) == []
    assert "stopped (token budget" in git(repo, "log", "-1", "--format=%s", result.branch)
    # the work done before the stop is still on the branch, labelled stopped
    assert git(repo, "show", f"{result.branch}:calc.py") == FIXED
    # each reply was capped at what the budget had left
    assert client.asked[-1]["max_tokens"] <= 300


class Clock:
    def __init__(self, step: float) -> None:
        self.now, self.step = 0.0, step

    def __call__(self) -> float:
        self.now += self.step
        return self.now


def test_time_budget_exhausted_ends_stopped_not_finished(repo: Path) -> None:
    client = Scripted([], tail=reading_round(0))
    result = auto(repo, client, time_budget_s=250, clock=Clock(100))
    assert result.outcome == "stopped"
    assert result.reason.startswith("time budget exhausted")
    assert "auto:finished" not in [s.name for s in read_spans(result.journal)]
    assert len(client.asked) >= 1
    assert verify_journal(result.journal) == []


def test_a_model_error_is_a_stop(repo: Path) -> None:
    result = auto(repo, Scripted([VllmRequestError("boom")]))
    assert result.outcome == "stopped"
    assert result.reason == "model error: boom"


# -- stall check (--stall-check): eject a never-acted, still-hedging run -------

# A round of high-hedge reasoning (>=50 words, dense markers) with a non-progress
# read. Its density is well above STALL_HEDGE_PER_K.
HEDGY = (
    "hmm wait what is the task even saying here maybe i should re-read the task "
    "i think perhaps the real bug must be somewhere else not sure this seems "
    "unclear why is it like this or is it actually fine it doesnt make sense to me "
    "apparently the count must mean something else presumably i guess i am confused "
    "hold on wait what does the record actually hold here re-read again not clear"
) * 2
# The same length of plain, productive reasoning: no hedging markers.
PLAIN = (
    "open the coverage module and locate describe coverage it computes the per "
    "function count from the changed set and the span map add a parameter and "
    "adjust the caller in packet and feed then run the covering tests to confirm "
    "the new behavior lands correctly and the row shows the gate own count now "
    "read the sidecar writer and seal the compelled lines as a relative path list"
) * 2


def hedgy_tail() -> list[Any]:
    return [StreamToken(stream="reasoning", text=HEDGY), call("read_file", "rd", path="calc.py")]


def test_stall_check_ejects_a_never_acted_still_hedging_run(repo: Path) -> None:
    # Past the warmup, only reading and hedging, never an edit/premise/dispute.
    result = auto(
        repo,
        Scripted([], tail=hedgy_tail()),
        stall_check=True,
        time_budget_s=100000,
        clock=Clock(80),
    )
    assert result.outcome == "stopped"
    assert result.reason.startswith("needs you: stalled")
    assert "no edit, premise_check or dispute" in result.reason
    assert "auto:finished" not in [s.name for s in read_spans(result.journal)]
    assert verify_journal(result.journal) == []


def test_stall_check_spares_a_run_that_acted_even_if_it_then_hedges(repo: Path) -> None:
    # One premise_check up front exempts the run for the rest of it: it then
    # hedges past the warmup but is never ejected for the stall (times out).
    edit = call("edit_file", path="calc.py", old="a - b", new="a + b")
    result = auto(
        repo,
        Scripted([[edit]], tail=hedgy_tail()),
        stall_check=True,
        time_budget_s=1200,
        clock=Clock(80),
    )
    assert result.outcome == "stopped"
    assert not result.reason.startswith("needs you: stalled")
    assert result.reason.startswith("time budget exhausted")


def test_stall_check_spares_a_never_acted_run_below_the_hedge_threshold(repo: Path) -> None:
    # Deep exploration with no hedging: never acts, past the warmup, but plain
    # reasoning stays below the threshold, so no eject (times out instead).
    plain_tail = [
        StreamToken(stream="reasoning", text=PLAIN),
        call("read_file", "rd", path="calc.py"),
    ]
    result = auto(
        repo, Scripted([], tail=plain_tail), stall_check=True, time_budget_s=1200, clock=Clock(80)
    )
    assert result.outcome == "stopped"
    assert not result.reason.startswith("needs you: stalled")
    assert result.reason.startswith("time budget exhausted")


def test_stall_check_off_never_ejects_even_a_hedging_idle_run(repo: Path) -> None:
    # Default (flag off): the hedge is scored for nothing; the run times out.
    result = auto(repo, Scripted([], tail=hedgy_tail()), time_budget_s=1200, clock=Clock(80))
    assert result.outcome == "stopped"
    assert not result.reason.startswith("needs you: stalled")
    assert result.reason.startswith("time budget exhausted")


def test_stall_check_does_not_eject_before_the_warmup(repo: Path) -> None:
    # Hedging and never acting, but the clock is slow (2s/tick), so the whole
    # run stays under the 600s warmup and reaches its finish: the warmup must
    # hold the eject off. (A mutant that armed before the warmup would stop it
    # as stalled on the first hedgy round instead.)
    hedgy = [StreamToken(stream="reasoning", text=HEDGY), call("read_file", "rd", path="calc.py")]
    client = Scripted([hedgy, hedgy, finish()])
    result = auto(repo, client, stall_check=True, time_budget_s=1200, clock=Clock(2))
    assert (result.outcome, result.reason) == ("finished", "finish called")


def budget_options(tmp_path: Path, **budget: Any) -> tuple[TurnOptions, AutoRun]:
    run = AutoRun(budget=RunBudget(**{"time_s": 60, "tokens": 1000, **budget}), run_span="s")
    options = TurnOptions(workdir=tmp_path, journal=tmp_path / "j.jsonl", auto=run)
    return options, run


def test_a_cancelled_run_is_a_stop(tmp_path: Path) -> None:
    options, run = budget_options(tmp_path)
    client = Scripted([], tail=[StreamToken(stream="content", text="hm")])
    events = list(run_turn(cast(VllmClient, client), [], "t", options, turn=1, cancel=lambda: True))
    assert run.outcome == "stopped"
    assert run.reason == "cancelled"
    assert any(isinstance(e, ErrorEvent) and e.message == "stopped by you" for e in events)


def test_a_loop_that_ends_without_finish_or_stop_is_recorded_as_stopped(tmp_path: Path) -> None:
    options, run = budget_options(tmp_path)
    client = Scripted([[call("read_file", path="x")]])
    stops = iter([False, False, False, True, True, True, True])
    list(run_turn(cast(VllmClient, client), [], "t", options, turn=1, cancel=lambda: next(stops)))
    assert run.outcome == "stopped"


def test_budget_arithmetic() -> None:
    clock = Clock(10)
    budget = RunBudget(time_s=25, tokens=10, clock=clock)
    assert budget.elapsed() == 0.0
    budget.start()
    budget.start()
    assert budget.started == 10
    assert budget.exhausted() is None  # 10s elapsed
    budget.charge(3)  # below one token's worth still costs one
    assert budget.spent_tokens == 1
    budget.charge(40)
    assert budget.remaining_tokens() == 0
    assert budget.exhausted() == "token budget exhausted: ~11 of 10 generated tokens spent"
    over = RunBudget(time_s=25, tokens=10, clock=Clock(10))
    over.start()
    assert over.exhausted() is None
    assert over.exhausted() is None
    assert over.exhausted() == "time budget exhausted: 30s of 25s spent"


def test_a_finish_never_overwrites_a_stop_and_vice_versa() -> None:
    run = AutoRun(budget=RunBudget(time_s=1, tokens=1), run_span="s")
    run.stop("token budget")
    run.finish("done")
    assert (run.outcome, run.narrative) == ("stopped", "")
    other = AutoRun(budget=RunBudget(time_s=1, tokens=1), run_span="s")
    other.finish("done")
    other.stop("late")
    assert (other.outcome, other.reason) == ("finished", "finish called")


def test_chat_turns_keep_the_round_cap(tmp_path: Path) -> None:
    options = TurnOptions(workdir=tmp_path, journal=tmp_path / "j.jsonl")
    client = Scripted([], tail=[call("list_dir")])
    events = list(run_turn(cast(VllmClient, client), [], "t", options, turn=1))
    assert len(client.asked) == MAX_TOOL_ROUNDS
    assert "finish" not in [t["function"]["name"] for t in client.asked[0]["tools"]]
    assert next(e for e in events if isinstance(e, ErrorEvent)).message == (
        f"stopped after {MAX_TOOL_ROUNDS} tool rounds"
    )
    assert read_records(tmp_path / "j.jsonl")[0].kind == ""


def test_non_object_command_arguments_are_not_listed(tmp_path: Path) -> None:
    options, _ = budget_options(tmp_path)
    client = Scripted(
        [
            [ToolCall(id="a", name="run_command", arguments="[1]")],
            [ToolCall(id="b", name="run_command", arguments="{bad")],
            finish(),
        ]
    )
    list(run_turn(cast(VllmClient, client), [], "t", options, turn=1))
    span = [
        e for e in read_entries(tmp_path / "j.jsonl") if getattr(e, "name", "") == "auto:finished"
    ]
    assert "commands run: 0" in span[0].detail  # type: ignore[union-attr]


# -- setup failures and the command line -----------------------------------------


def test_not_a_git_repo_is_an_auto_error(tmp_path: Path) -> None:
    with pytest.raises(AutoError, match="rev-parse"):
        create_worktree(tmp_path, "r1")


def test_changed_files_lists_edits_additions_and_deletions(repo: Path) -> None:
    worktree, _ = create_worktree(repo, "r9")
    (worktree / "calc.py").write_text(FIXED)
    (worktree / "new" / "a.py").parent.mkdir()
    (worktree / "new" / "a.py").write_text("")
    (worktree / "tests" / "test_calc.py").unlink()
    assert changed_files(worktree) == ["calc.py", "new/a.py", "tests/test_calc.py"]


def namespace(repo: Path, **extra: Any) -> argparse.Namespace:
    args = cli.build_parser().parse_args(["auto", "fix add", "--repo", str(repo), "--no-audit"])
    for key, value in extra.items():
        setattr(args, key, value)
    return args


def test_the_command_exits_zero_only_when_finished(repo: Path) -> None:
    out = io.StringIO()
    client = Scripted([[call("edit_file", path="calc.py", old="a - b", new="a + b")], finish()])
    assert cli.run_auto_command(namespace(repo), cast(VllmClient, client), stdout=out) == 0
    text = out.getvalue()
    assert "finished: finish called" in text
    assert "ledger " in text
    out = io.StringIO()
    code = cli.run_auto_command(
        namespace(repo, token_budget=1),
        cast(VllmClient, Scripted([VllmRequestError("x")])),
        stdout=out,
    )
    assert code == cli.AUTO_STOPPED
    assert "stopped: model error: x" in out.getvalue()


@pytest.mark.parametrize("finished", [True, False])
def test_the_command_prints_the_packet_after_its_outcome_lines(repo: Path, finished: bool) -> None:
    # The terminal once printed three lines and no packet. Contract:
    # after "ledger <path>" comes render_packet_text of the ledger's packet,
    # the same bytes the chat card's recap gets, for a finish and for a stop.
    from saddle.packet import compile_packet, render_packet_text

    out = io.StringIO()
    client = (
        Scripted([[call("edit_file", path="calc.py", old="a - b", new="a + b")], finish()])
        if finished
        else Scripted([VllmRequestError("x")])
    )
    args = namespace(repo) if finished else namespace(repo, token_budget=1)
    cli.run_auto_command(args, cast(VllmClient, client), stdout=out)
    text = out.getvalue()
    ledger = Path(
        next(line for line in text.splitlines() if line.startswith("ledger ")).split(" ", 1)[1]
    )
    packet = compile_packet(ledger)
    assert packet.verdict == ("finished" if finished else "stopped")
    tail = text.split(f"ledger {ledger}\n", 1)[1]
    assert tail == render_packet_text(packet) + "\n"
    assert "Not proven [not-proven]:" in tail


def test_the_command_reports_setup_failure(tmp_path: Path) -> None:
    out = io.StringIO()
    code = cli.run_auto_command(namespace(tmp_path), cast(VllmClient, Scripted([])), stdout=out)
    assert code == 1
    assert out.getvalue().startswith("error: git rev-parse")


def test_parser_defaults_come_from_the_documented_budgets() -> None:
    args = cli.build_parser().parse_args(["auto", "t"])
    assert (args.time_budget, args.token_budget, args.allow_test_edits) == (1800, 100_000, False)
    assert args.temperature == TASK_TEMPERATURE == 1.0
    pinned = cli.build_parser().parse_args(["auto", "--temperature", "0.0", "t"])
    assert pinned.temperature == 0.0  # a measurement can still ask for greedy


class MainClient(Scripted):
    made: ClassVar[list[dict[str, Any]]] = []

    def __init__(self, **kwargs: Any) -> None:
        MainClient.made.append(kwargs)
        super().__init__([finish()])


def test_main_dispatches_auto(
    repo: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("SADDLE_VLLM_API_KEY", "k")
    monkeypatch.setattr("saddle.cli.VllmClient", MainClient)
    assert cli.main(["auto", "t", "--repo", str(repo), "--time-budget", "60", "--no-audit"]) == 0
    assert "finished" in capsys.readouterr().out
    assert MainClient.made[-1]["api_key"] == "k"


_: Callable[..., object] = run_turn


# -- stale bytecode: a same-length edit inside one mtime second ---------------


class _Hooked(Scripted):
    """Scripted, plus a callable run just before round `i` is served."""

    def __init__(self, rounds: list[list[Any] | BaseException], hooks: dict[int, Any]):
        super().__init__(rounds)
        self.hooks = hooks
        self.served = 0

    def stream_chat(self, messages: Any, **kwargs: Any) -> Iterator[Any]:
        hook = self.hooks.get(self.served)
        if hook is not None:
            hook()
        self.served += 1
        return super().stream_chat(messages, **kwargs)


def test_a_same_length_edit_in_the_same_second_runs_the_new_code(
    repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The hazard needs bytecode writes on, whatever the outer run sets.
    monkeypatch.delenv("PYTHONDONTWRITEBYTECODE", raising=False)
    import shlex
    import sys

    py = shlex.quote(sys.executable)
    calc = repo / ".saddle" / "worktrees" / "r1" / "calc.py"
    saved: list[int] = []

    def remember() -> None:  # after the first import, before the edit
        saved.append(calc.stat().st_mtime_ns)

    def rewind() -> None:  # after the edit: same size, same mtime as the old source
        os.utime(calc, ns=(saved[0], saved[0]))

    probe = f"{py} -c 'import calc; print(\"ADD\", calc.add(2, 2))'"
    client = _Hooked(
        [
            [call("run_command", "c1", command=probe)],
            [call("edit_file", "c2", path="calc.py", old="a - b", new="a + b")],
            [call("run_command", "c3", command=probe)],
            finish(),
        ],
        hooks={1: remember, 2: rewind},
    )
    result = auto(repo, client)

    results = [m["content"] for m in client.asked[3]["messages"] if m.get("role") == "tool"]
    assert "ADD 0" in results[0]  # the buggy code ran first
    assert "ADD 4" in results[2], results[2]  # and the fixed code ran second
    assert result.outcome == "finished"


# -- bytecode and saddle state never reach the run branch ---------------------


def test_bytecode_is_neither_listed_nor_committed_without_a_gitignore(repo: Path) -> None:
    import shlex
    import sys

    assert not (repo / ".gitignore").exists()
    py = shlex.quote(sys.executable)
    # cached bytecode under __pycache__, plus a legacy .pyc beside its source
    compile_all = (
        f"{py} -m py_compile calc.py tests/test_calc.py && "
        f"{py} -c \"import py_compile; py_compile.compile('calc.py', cfile='calc.pyc')\""
    )
    client = Scripted(
        [
            [call("run_command", "c1", command=compile_all)],
            [call("edit_file", "c2", path="calc.py", old="a - b", new="a + b")],
            [call("run_command", "c3", command="mkdir -p .saddle && echo x > .saddle/s")],
            finish(),
        ]
    )
    result = auto(repo, client)

    wt = result.worktree
    assert (wt / "calc.pyc").exists()
    assert list(wt.rglob("__pycache__/*.pyc"))
    assert (wt / ".saddle" / "s").exists()
    tree = git(repo, "ls-tree", "-r", "--name-only", result.branch).split()
    assert sorted(tree) == ["calc.py", "tests/test_calc.py"]
    assert changed_files(wt) == []  # committed; bytecode and .saddle/ still unlisted
    assert sidecar(result)["files_changed"] == ["calc.py"]
    assert git(repo, "show", f"{result.branch}:calc.py") == FIXED


@pytest.mark.parametrize(("coauthor", "credited"), [(True, True), (False, False)])
def test_the_run_commit_credits_saddle_unless_asked_not_to(
    repo: Path, coauthor: bool, credited: bool
) -> None:
    """Saddle wrote the change, so its commit says so by default: the trailer
    sits in the anchor's trailer block, which still parses. `--no-coauthor`
    (coauthor=False) leaves it off."""
    client = Scripted([[call("edit_file", path="calc.py", old="a - b", new="a + b")], finish()])
    result = auto(repo, client, coauthor=coauthor)
    message = git(repo, "log", "-1", "--format=%B", result.branch)
    last = message.strip().split("\n\n")[-1].splitlines()
    assert ("Co-Authored-By: Saddle" in last) is credited
    assert set(parse_trailers(message)) == {"Saddle-Outcome", "Saddle-Ledger"}


# -- dispute: the ripcord for a false premise ------------------------------------


def test_a_dispute_with_rerun_evidence_ends_the_run_needing_you(repo: Path) -> None:
    """The model finds the task's premise false and says so with evidence:
    saddle reruns the command itself, seals its output, and the run ends
    needing a person. It is offered and stated up front, and never a finish."""
    client = Scripted(
        [
            [
                call(
                    "dispute",
                    claim="add subtracts",
                    finding="add already adds",
                    evidence=["echo premise-probe-output"],
                )
            ]
        ]
    )
    result = auto(repo, client)
    assert result.outcome == "stopped"
    assert result.reason == "needs you: the task's premise is disputed: add subtracts"
    sealed = sidecar(result)["dispute"]
    assert sealed["finding"] == "add already adds"
    assert sealed["evidence"][0]["command"] == "echo premise-probe-output"
    assert "premise-probe-output" in sealed["evidence"][0]["output"]
    asked = client.asked[0]
    assert "dispute" in [t["function"]["name"] for t in asked["tools"]]
    assert "call dispute" in asked["messages"][0]["content"]
    from saddle.packet import compile_packet

    packet = compile_packet(result.journal)
    assert packet.verdict == "needs_you"


@pytest.mark.parametrize(
    "arguments",
    [
        {"claim": "add subtracts", "finding": "it adds", "evidence": []},
        {"claim": "add subtracts", "finding": "it adds", "evidence": ["  "]},
        {"claim": "", "finding": "it adds", "evidence": ["echo x"]},
        {"claim": "add subtracts", "finding": "it adds", "evidence": ["echo x"] * 6},
    ],
    ids=["no-command", "blank-command", "no-claim", "too-many"],
)
def test_a_dispute_without_evidence_is_refused_and_the_run_goes_on(
    repo: Path, arguments: dict[str, Any]
) -> None:
    """The ripcord is not a cheap way out: without a claim and runnable
    evidence it is refused, the model is told why, and the run continues."""
    client = Scripted(
        [
            [call("dispute", **arguments)],
            [call("edit_file", path="calc.py", old="a - b", new="a + b")],
            finish(),
        ]
    )
    result = auto(repo, client)
    assert (result.outcome, result.reason) == ("finished", "finish called")
    assert "dispute" not in sidecar(result)
    told = [m for m in client.asked[1]["messages"] if m.get("role") == "tool"]
    assert told[-1]["content"].startswith("error: dispute refused")


@pytest.mark.parametrize("formats", [True, False], ids=["format-at-finish", "default"])
def test_format_at_finish_formats_the_changed_files_and_says_so_only_when_asked(
    repo: Path, formats: bool
) -> None:
    """`--format-at-finish`: the run's changed Python files are ruff-formatted
    before the finish audit, and the model is told which. Off by default: the
    model owns its changes, and nothing is touched."""
    client = Scripted([[call("edit_file", path="calc.py", old="a - b", new="a+b")], finish()])
    result = auto(repo, client, format_at_finish=formats)
    committed = git(repo, "show", f"{result.branch}:calc.py")
    assert ("a + b" in committed) is formats
    assert ("a+b" in committed) is not formats
    (told,) = [s.detail for s in read_spans(result.journal) if s.argv[:1] == ["finish"]]
    assert ("saddle formatted calc.py before the audit" in told) is formats


def test_premise_check_refuses_edits_until_the_problem_is_shown_then_asks_the_question(
    repo: Path,
) -> None:
    """`--premise-check`: an edit before `premise_check` is refused and says why;
    `premise_check` reruns the commands, seals them, returns their output with
    the question (does it show the problem? if not, dispute), and unlocks edits."""
    client = Scripted(
        [
            [call("edit_file", path="calc.py", old="a - b", new="a + b")],
            [call("premise_check", claim="add subtracts", commands=["echo shown-output"])],
            [call("edit_file", path="calc.py", old="a - b", new="a + b")],
            finish(),
        ]
    )
    result = auto(repo, client, premise_check=True)
    assert (result.outcome, result.reason) == ("finished", "finish called")
    told = [m["content"] for m in client.asked[3]["messages"] if m.get("role") == "tool"]
    assert told[0].startswith("error: refused: edits wait for premise_check")
    assert "shown-output" in told[1]
    assert "call dispute now" in told[1]
    assert not told[2].startswith("error")
    sealed = sidecar(result)["premise"]
    assert sealed["claim"] == "add subtracts"
    assert "shown-output" in sealed["evidence"][0]["output"]
    asked = client.asked[0]
    assert "premise_check" in [t["function"]["name"] for t in asked["tools"]]
    assert "call premise_check" in asked["messages"][0]["content"]


def test_without_premise_check_edits_are_not_held_and_the_tool_is_not_offered(
    repo: Path,
) -> None:
    client = Scripted([[call("edit_file", path="calc.py", old="a - b", new="a + b")], finish()])
    result = auto(repo, client)
    assert (result.outcome, result.reason) == ("finished", "finish called")
    assert "premise" not in sidecar(result)
    assert "premise_check" not in [t["function"]["name"] for t in client.asked[0]["tools"]]


def test_a_premise_check_whose_probe_crashes_keeps_edits_held(repo: Path) -> None:
    """A probe that dies with a traceback shows nothing either way: the check is
    refused, says so, and edits stay held until a probe that runs."""
    crash = (
        "python -c 'import sys; sys.stderr.write(\"Traceback (most recent call last):\\n\"); 1/0'"
    )
    client = Scripted(
        [
            [call("premise_check", claim="add subtracts", commands=[crash])],
            [call("edit_file", path="calc.py", old="a - b", new="a + b")],
            [call("premise_check", claim="add subtracts", commands=["echo ran"])],
            finish(),
        ]
    )
    result = auto(repo, client, premise_check=True)
    told = [m["content"] for m in client.asked[3]["messages"] if m.get("role") == "tool"]
    assert told[0].startswith("error: premise_check refused: command 1 crashed")
    assert told[1].startswith("error: refused: edits wait for premise_check")
    assert "ran" in sidecar(result)["premise"]["evidence"][0]["output"]


def test_premise_check_lets_a_probe_be_written_outside_the_worktree(repo: Path) -> None:
    """The gate holds edits of the code, not the probe it asks for: a file
    written outside the worktree (the run's /tmp) is not held before the check."""
    client = Scripted(
        [
            [call("write_file", path="/tmp/probe.py", content="print('probe')\n")],
            [call("write_file", path="calc.py", content="x = 1\n")],
            finish(),
        ]
    )
    auto(repo, client, premise_check=True)
    told = [m["content"] for m in client.asked[2]["messages"] if m.get("role") == "tool"]
    assert not told[0].startswith("error: refused: edits wait for premise_check")
    assert told[1].startswith("error: refused: edits wait for premise_check")


# -- dispute, premise_check and the edit gate, argument by argument ----------------


def _run() -> AutoRun:
    return AutoRun(budget=RunBudget(time_s=60, tokens=1000), run_span="s")


@pytest.mark.parametrize(
    ("arguments", "refusal"),
    [
        ("not json", "error: premise_check needs a JSON object"),
        ('["a list"]', "error: premise_check needs a JSON object"),
        ("", "error: premise_check refused: name the claim"),
        (json.dumps({"claim": "  ", "commands": ["echo x"]}), "error: premise_check refused: name"),
        (json.dumps({"claim": "c", "commands": []}), "error: premise_check refused: give one to 5"),
        (json.dumps({"claim": "c", "commands": "echo x"}), "error: premise_check refused: give"),
        (json.dumps({"claim": "c", "commands": ["echo x"] * 6}), "error: premise_check refused"),
        (json.dumps({"claim": "c", "commands": [" "]}), "error: premise_check refused: give"),
    ],
    ids=["not-json", "not-object", "empty", "blank-claim", "none", "a-string", "six", "blank"],
)
def test_a_premise_check_without_a_claim_and_commands_is_refused_and_seals_nothing(
    tmp_path: Path, arguments: str, refusal: str
) -> None:
    from saddle.engine import _premise

    run = _run()
    told = _premise(run, arguments, tmp_path, ToolContext(workdir=tmp_path))
    assert told.startswith(refusal), told
    assert run.premise is None
    # Known-good control: the same run takes a well-formed check and seals it.
    good = json.dumps({"claim": "c", "commands": ["echo shown"] * 5})
    assert "shown" in _premise(run, good, tmp_path, ToolContext(workdir=tmp_path))
    assert run.premise is not None
    assert len(run.premise["evidence"]) == 5


@pytest.mark.parametrize(
    ("arguments", "refusal"),
    [
        ("{", "error: dispute needs a JSON object"),
        ("7", "error: dispute needs a JSON object"),
        (json.dumps({"claim": "c", "finding": " ", "evidence": ["echo x"]}), "say what you found"),
        (json.dumps({"claim": "c", "evidence": ["echo x"]}), "say what you found"),
    ],
    ids=["not-json", "not-object", "blank-finding", "no-finding"],
)
def test_a_dispute_that_is_not_an_object_or_names_no_finding_is_refused(
    tmp_path: Path, arguments: str, refusal: str
) -> None:
    from saddle.engine import _dispute

    run = _run()
    told = _dispute(run, arguments, tmp_path, ToolContext(workdir=tmp_path))
    assert refusal in told
    assert told.startswith("error: dispute")
    assert (run.dispute, run.outcome) == (None, "")
    good = json.dumps({"claim": "c", "finding": "f", "evidence": ["echo x"]})
    _dispute(run, good, tmp_path, ToolContext(workdir=tmp_path))
    assert run.dispute is not None
    assert run.reason.startswith("needs you")


@pytest.mark.parametrize("tool", ["premise_check", "dispute"])
def test_evidence_that_does_not_run_refuses_the_call_and_seals_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, tool: str
) -> None:
    """A command the tool layer cannot run (an "error: " result) is not evidence:
    the call is refused naming which command, and nothing is sealed or stopped."""
    from saddle import engine

    ran: list[str] = []

    def execute(call: ToolCall, **_kw: Any) -> str:
        command = json.loads(call.arguments)["command"]
        ran.append(command)
        return "error: no sandbox" if command == "bad" else "exit 0\nok"

    monkeypatch.setattr(engine, "execute_tool", execute)
    run = _run()
    ctx = ToolContext(workdir=tmp_path)
    if tool == "premise_check":
        told = engine._premise(
            run, json.dumps({"claim": "c", "commands": ["ok", "bad"]}), tmp_path, ctx
        )
        assert told.startswith("error: premise_check refused: command 2 did not run (no sandbox)")
        assert run.premise is None
    else:
        args = {"claim": "c", "finding": "f", "evidence": ["ok", "bad"]}
        told = engine._dispute(run, json.dumps(args), tmp_path, ctx)
        assert told.startswith(
            "error: dispute refused: evidence command 2 did not run (no sandbox)"
        )
        assert (run.dispute, run.outcome) == (None, "")
    assert ran == ["ok", "bad"]


@pytest.mark.parametrize(
    ("arguments", "held"),
    [
        ("not json", True),
        ('["path"]', True),
        (json.dumps({"path": ""}), True),
        (json.dumps({"path": 3}), True),
        (json.dumps({"path": "calc.py"}), True),
        (json.dumps({"path": "."}), True),
        (json.dumps({"path": "/tmp/probe.py"}), False),
        (json.dumps({"path": "../elsewhere.py"}), False),
    ],
    ids=["not-json", "not-object", "empty", "not-a-string", "inside", "root", "tmp", "parent"],
)
def test_an_edit_is_held_unless_its_path_is_plainly_outside_the_worktree(
    tmp_path: Path, arguments: str, held: bool
) -> None:
    """What cannot be read as a path is held: the gate fails closed."""
    from saddle.engine import _in_worktree

    worktree = tmp_path / "wt"
    worktree.mkdir()
    assert _in_worktree(arguments, worktree) is held


# -- resume and format-at-finish, directly -------------------------------------------


@pytest.mark.parametrize("system", [None, "Old prompt with no directory named."])
def test_a_recording_without_a_named_worktree_is_resumed_verbatim_behind_the_new_prompt(
    tmp_path: Path, system: str | None
) -> None:
    from saddle.auto import resumed

    kept = [{"role": "user", "content": "cd /old/wt && fix it"}]
    head = [{"role": "system", "content": system}] if system else []
    recorded = tmp_path / "request.json"
    recorded.write_text(json.dumps({"messages": head + kept}))
    assert resumed(recorded, tmp_path / "new", "NEW") == [
        {"role": "system", "content": "NEW"},
        *kept,
    ]
    # Known-good contrast: a named worktree is rewritten to this run's.
    named = [{"role": "system", "content": "Your working directory is /old/wt, go"}]
    recorded.write_text(json.dumps({"messages": named + kept}))
    assert resumed(recorded, tmp_path / "new", "NEW")[1]["content"] == (
        f"cd {tmp_path / 'new'} && fix it"
    )


def test_format_changed_touches_nothing_when_no_python_file_changed(repo: Path) -> None:
    from saddle.auto import format_changed

    (repo / "calc.py").write_text("def add(a,b):\n    return a+b\n")
    (repo / "calc.py").rename(repo / "calc.txt")
    assert format_changed(repo, "HEAD") == ""
    assert (repo / "calc.txt").read_text() == "def add(a,b):\n    return a+b\n"
    (repo / "calc.txt").rename(repo / "calc.py")
    assert format_changed(repo, "HEAD").startswith("saddle formatted calc.py")
    assert (repo / "calc.py").read_text() == FIXED
