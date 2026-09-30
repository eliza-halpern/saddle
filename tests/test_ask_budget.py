"""ASK-2: at 80% of a budget, a running run asks once whether to extend it.

Contract: when the run's generated tokens or wall time first reach
`BUDGET_ASK_AT` (80%) of the budget, with the run still going, it puts one
question through the auditor seam -- "Extend" (by the same amount again)
or "Stop at limit" -- sealed as a `question` span with its `answer` as a
child. Extend raises that budget once and seals `budget_extended`; Stop at
limit leaves it. With no answer channel (headless) the answer is sealed
"unanswered, default taken: Stop at limit" and the run stops where it
always did. The wait for an answer is not charged (UI-1).

Known-good: Extend lets a run that needed 1.2x its token budget finish;
the question fires at the first round at or past 80%, not before.
Known-bad: Stop at limit and a headless run stop "token budget exhausted"
at the original budget; neither is asked twice.
"""

from __future__ import annotations

import io
import json
import threading
from pathlib import Path
from typing import Any, cast

import pytest
from test_ask_web import app_for, start
from test_web_tasks import git, idle, wait_for

from saddle import cli
from saddle.engine import (
    BUDGET_ASK_AT,
    EXTEND_BUDGET,
    STOP_AT_LIMIT,
    AutoRun,
    RunBudget,
    TurnOptions,
    run_turn,
)
from saddle.events import Question, RunProgress
from saddle.journal import attempt_sidecar_path, read_spans
from saddle.memory import CHARS_PER_TOKEN
from saddle.sessions import SessionStore
from saddle.vllm import StreamToken, ToolCall, VllmClient

ROUND = 100
"""Estimated tokens per scripted round (reasoning only, no usage)."""


def call(name: str, cid: str, **arguments: Any) -> ToolCall:
    return ToolCall(id=cid, name=name, arguments=json.dumps(arguments))


class Reader:
    """Reads a file each round (~ROUND generated tokens), finishing on round `done`."""

    def __init__(self, done: int) -> None:
        self.done = done
        self.round = 0

    def __enter__(self) -> Reader:
        return self

    def __exit__(self, *_: object) -> None:
        return None

    def max_model_len(self) -> int:
        return 200_000

    def stream_chat(self, messages: Any, **_: Any) -> Any:
        self.round += 1
        think = StreamToken(stream="reasoning", text="x" * (ROUND * CHARS_PER_TOKEN))
        if self.round >= self.done:
            return iter([call("finish", "f", summary="read enough")])
        return iter([think, call("read_file", f"r{self.round}", path="calc.py")])


class Clock:
    def __init__(self, step: float) -> None:
        self.now, self.step = 0.0, step

    def __call__(self) -> float:
        self.now += self.step
        return self.now


def engine_run(
    tmp_path: Path,
    answer: Any,
    *,
    done: int,
    tokens: int = 1000,
    time_s: float = 3600,
    clock: Any = None,
) -> tuple[AutoRun, list[Any], list[float]]:
    at: list[float] = []
    budget = RunBudget(time_s=time_s, tokens=tokens, **({"clock": clock} if clock else {}))

    def asked(q: Question) -> Any:
        at.append(budget.spent_tokens / budget.tokens)
        return answer(q) if answer is not None else None

    run = AutoRun(budget=budget, run_span="s", answer=asked if answer is not None else None)
    (tmp_path / "calc.py").write_text("x = 1\n")
    options = TurnOptions(workdir=tmp_path, journal=tmp_path / "j" / "proofs.jsonl", auto=run)
    events = list(run_turn(cast(VllmClient, Reader(done)), [], "t", options, turn=1))
    return run, events, at


def spans(tmp_path: Path, name: str) -> list[Any]:
    return [s for s in read_spans(tmp_path / "j" / "proofs.jsonl") if s.name == name]


# -- known-good ---------------------------------------------------------------


def test_extend_raises_the_token_budget_once_and_the_run_finishes(tmp_path: Path) -> None:
    run, events, at = engine_run(tmp_path, lambda q: EXTEND_BUDGET, done=13)
    assert (run.outcome, run.reason) == ("finished", "finish called")
    assert run.budget.tokens == 2000
    assert run.sealed["budget_extended"] == {"budget": "token", "by": 1000}
    [q] = [e for e in events if isinstance(e, Question)]
    assert q.id == "budget"
    assert q.text == (
        "This run has used 80% of its token budget and has not finished. "
        "Extend by 1000 generated tokens or stop at the limit?"
    )
    assert q.options == [EXTEND_BUDGET, STOP_AT_LIMIT]
    [share] = at  # asked at the first round boundary at 80%, not a round later
    assert BUDGET_ASK_AT <= share < BUDGET_ASK_AT + 0.11
    [asked] = spans(tmp_path, "question")
    [answered] = spans(tmp_path, "answer")
    assert answered.parent_id == asked.span_id
    assert answered.argv == ["answer", EXTEND_BUDGET]
    assert any(isinstance(e, RunProgress) and e.token_budget == 2000 for e in events)


def test_the_time_budget_is_asked_about_and_extended_too(tmp_path: Path) -> None:
    run, events, _at = engine_run(
        tmp_path, lambda q: "extend", done=12, tokens=10**6, time_s=100, clock=Clock(4)
    )
    [q] = [e for e in events if isinstance(e, Question)]
    assert q.text.startswith("This run has used 80% of its time budget and has not finished. ")
    assert "Extend by 100s or stop" in q.text
    assert run.sealed["budget_extended"] == {"budget": "time", "by": 100}
    assert run.budget.time_s == 200


# -- known-bad ----------------------------------------------------------------


def test_stop_at_limit_stops_at_the_original_budget_asked_once(tmp_path: Path) -> None:
    run, _events, at = engine_run(tmp_path, lambda q: STOP_AT_LIMIT, done=13)
    assert run.outcome == "stopped"
    assert run.reason.startswith("token budget exhausted: ~")
    assert run.reason.endswith(" of 1000 generated tokens spent")
    assert len(at) == 1
    assert "budget_extended" not in run.sealed
    [answered] = spans(tmp_path, "answer")
    assert answered.detail == STOP_AT_LIMIT


def test_a_headless_run_takes_stop_at_limit_and_says_so(tmp_path: Path) -> None:
    run, _events, _at = engine_run(tmp_path, None, done=13)
    assert run.reason.endswith(" of 1000 generated tokens spent")
    [answered] = spans(tmp_path, "answer")
    assert answered.argv == ["answer", STOP_AT_LIMIT, "unanswered"]
    assert answered.detail == (
        "unanswered, default taken: Stop at limit (no answer channel: a headless run)"
    )


def test_a_run_that_finishes_before_80_percent_is_not_asked(tmp_path: Path) -> None:
    run, events, _at = engine_run(tmp_path, lambda q: EXTEND_BUDGET, done=8)
    assert run.outcome == "finished"
    assert not [e for e in events if isinstance(e, Question)]


@pytest.mark.parametrize(
    ("spent", "elapsed", "near"),
    [
        (799, 0, None),
        (800, 0, "token"),
        (999, 0, "token"),
        (1000, 0, None),
        (0, 80, "time"),
        (0, 100, None),
    ],
)
def test_near_is_the_band_from_80_percent_to_the_limit(
    spent: int, elapsed: float, near: Any
) -> None:
    budget = RunBudget(time_s=100, tokens=1000, clock=lambda: 0.0)
    budget.start()
    budget.spent_tokens = spent
    budget.clock = lambda: elapsed
    assert budget.near(BUDGET_ASK_AT) == near


# -- end to end: the headless CLI, and the web API ----------------------------


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    root = tmp_path / "repo"
    root.mkdir()
    (root / "calc.py").write_text("x = 1\n")
    git(root, "init", "-q", "-b", "main")
    git(root, "add", "-A")
    git(root, "commit", "-q", "-m", "init")
    return root


def test_headless_saddle_auto_stops_at_the_limit_without_blocking(repo: Path) -> None:
    args = cli.build_parser().parse_args(
        ["auto", "read", "--repo", str(repo), "--no-audit", "--token-budget", "1000"]
    )
    out = io.StringIO()
    codes: list[int] = []
    worker = threading.Thread(
        target=lambda: codes.append(
            cli.run_auto_command(args, cast(VllmClient, Reader(13)), stdout=out)
        ),
        daemon=True,
    )
    worker.start()
    worker.join(timeout=60)
    assert not worker.is_alive(), "a headless run blocked on a question nobody can answer"
    assert codes == [cli.AUTO_STOPPED]
    assert "asked: This run has used 80% of its token budget" in out.getvalue()
    assert "→: unanswered, default taken: Stop at limit" in out.getvalue()


def test_extend_is_answered_through_the_web_api(tmp_path: Path, repo: Path) -> None:
    store = SessionStore(tmp_path / "s")
    with app_for(store, repo, Reader(13)) as (http, server):
        server.arm = "E"
        sid, rid = start(http, token_budget=1000)
        wait_for(lambda: server.tasks[rid].state == "needs_you", timeout=60)
        question = server.tasks[rid].state_event().question
        assert question is not None
        assert question["id"] == "budget"
        assert http.post(f"/api/tasks/{rid}/answer", json={"text": "Extend"}).json() == {"ok": True}
        wait_for(lambda: idle(server, sid), timeout=60)
        packet = http.get(f"/api/sessions/{sid}/tasks/{rid}/packet").json()
    run = server.tasks[rid]
    assert run.state == "finished"
    assert run.journal is not None
    all_spans = read_spans(run.journal)
    [asked] = [s for s in all_spans if s.name == "question"]
    [answered] = [s for s in all_spans if s.name == "answer"]
    contract = next(r for r in packet["rows"] if r["key"] == "contract")
    assert contract["items"] == [f"You were asked: {asked.detail} → you answered: Extend"]
    assert contract["cites"] == [asked.record_hash, answered.record_hash]
    end = next(s for s in reversed(all_spans) if s.name == "auto:finished")
    sealed = json.loads(attempt_sidecar_path(run.journal, end.span_id).read_text())
    assert sealed["budget_extended"] == {"budget": "token", "by": 1000}
