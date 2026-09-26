"""The auditor seam in an autonomous run: findings, questions, progress.

A finding is sealed as an `audit:<gate>` span and shown to the model in the
tool result; a question is sealed, answered, and the answer sealed as its
child; a person's time spent answering is not charged to the time budget
(loosened, deliberately: the budget bounds the run's work, not the reader).
"""

from __future__ import annotations

import json
import subprocess
from collections.abc import Iterator
from pathlib import Path
from typing import Any, cast

import pytest

from saddle.auto import AutoError, AutoOptions, ledger_path, repo_root, run_auto
from saddle.events import Answered, AuditFinding, Event, Question, RunProgress
from saddle.journal import read_spans, verify_journal
from saddle.vllm import ToolCall, VllmClient


def git(repo: Path, *args: str) -> None:
    subprocess.run(
        ["git", "-C", str(repo), "-c", "user.name=t", "-c", "user.email=t@t", *args],
        check=True,
        capture_output=True,
    )


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    root = tmp_path / "repo"
    root.mkdir()
    (root / "calc.py").write_text("def add(a, b):\n    return a - b\n")
    git(root, "init", "-q", "-b", "main")
    git(root, "add", "-A")
    git(root, "commit", "-q", "-m", "init")
    return root


def call(name: str, cid: str, **arguments: Any) -> ToolCall:
    return ToolCall(id=cid, name=name, arguments=json.dumps(arguments))


ROUNDS = [
    [call("edit_file", "c1", path="calc.py", old="a - b", new="(a + b)")],
    [call("read_file", "c2", path="calc.py")],
    [call("finish", "c3", summary="done")],
]


class Scripted:
    def __init__(self) -> None:
        self.rounds = [list(r) for r in ROUNDS]
        self.seen: list[Any] = []

    def __enter__(self) -> Scripted:
        return self

    def __exit__(self, *_: object) -> None:
        return None

    def stream_chat(self, messages: Any, **_: Any) -> Iterator[Any]:
        self.seen.append([dict(m) for m in messages])
        return iter(self.rounds.pop(0))


class Manual:
    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now


def audit(name: str, _arguments: str, _result: str) -> list[Event]:
    if name == "edit_file":
        return [Question(id="q", text="Keep the name add?", options=["yes"])]
    if name == "read_file":
        return [AuditFinding(gate="lint", ok=False, detail="E501 line too long")]
    return []


def test_questions_findings_and_progress_through_the_seam(repo: Path) -> None:
    clock = Manual()
    events: list[Event] = []

    def answer(question: Question) -> str:
        clock.now += 10_000  # a slow reader: far past the 100 s budget
        return "yes"

    client = Scripted()
    options = AutoOptions(task="t", repo=repo, run_id="r1", time_budget_s=100, clock=clock)
    result = run_auto(
        options, cast(VllmClient, client), on_event=events.append, audit=audit, answer=answer
    )

    assert result.outcome == "finished"  # waiting was not charged
    spans = read_spans(result.journal)
    names = [s.name for s in spans]
    assert "question" in names
    assert "answer" in names
    assert "audit:lint" in names
    question = next(s for s in spans if s.name == "question")
    assert question.argv == ["question", "Keep the name add?", "yes"]
    assert next(s for s in spans if s.name == "answer").parent_id == question.span_id
    lint = next(s for s in spans if s.name == "audit:lint")
    assert lint.exit_code == 1
    assert lint.detail == "E501 line too long"
    # the model sees the finding and the answer in the tool results
    tool_text = json.dumps(client.seen[-1])
    assert "E501 line too long" in tool_text
    assert "yes" in tool_text
    kinds = [type(e) for e in events]
    assert Question in kinds
    assert Answered in kinds
    assert RunProgress in kinds
    asked = next(e for e in events if isinstance(e, Question))
    assert asked.span_id == question.span_id
    assert verify_journal(result.journal) == []


def test_no_answer_stops_the_run_needing_you(repo: Path) -> None:
    options = AutoOptions(task="t", repo=repo, run_id="r1")
    result = run_auto(options, cast(VllmClient, Scripted()), audit=audit, answer=lambda _q: None)
    assert result.outcome == "stopped"
    assert result.reason.startswith("needs you: Keep the name add?")
    assert "answer" not in [s.name for s in read_spans(result.journal)]


def test_a_question_with_no_answer_callback_stops_too(repo: Path) -> None:
    options = AutoOptions(task="t", repo=repo, run_id="r1")
    result = run_auto(options, cast(VllmClient, Scripted()), audit=audit)
    assert result.outcome == "stopped"


def test_ledger_path_and_repo_root(repo: Path, tmp_path: Path) -> None:
    assert ledger_path(repo, "x") == repo / ".saddle" / "runs" / "x" / "proofs.jsonl"
    (repo / "sub").mkdir()
    assert repo_root(repo / "sub") == repo.resolve()
    with pytest.raises(AutoError):
        repo_root(tmp_path / "nowhere")


def test_an_audit_event_of_another_kind_is_ignored(repo: Path) -> None:
    def other(_n: str, _a: str, _r: str) -> list[Event]:
        return [RunProgress(elapsed_s=0, time_budget_s=1, tokens=0, token_budget=1)]

    result = run_auto(
        AutoOptions(task="t", repo=repo, run_id="r1"), cast(VllmClient, Scripted()), audit=other
    )
    assert result.outcome == "finished"
    assert not any(s.name.startswith("audit:") for s in read_spans(result.journal))


def test_asking_before_the_clock_starts_charges_nothing(tmp_path: Path) -> None:
    from saddle.engine import AutoRun, RunBudget, _ask

    run = AutoRun(budget=RunBudget(time_s=1, tokens=1), run_span="s", answer=lambda _q: "ok")
    steps = list(_ask(run, tmp_path / "j.jsonl", "n", Question(id="q", text="?")))
    assert run.budget.started is None
    assert isinstance(steps[-1], Answered)
    assert steps[-1].text == "ok"
