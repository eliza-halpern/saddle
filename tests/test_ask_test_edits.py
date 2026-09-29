"""ASK-1: a refused finish that needs a test asks whether tests may be edited.

Contract: when `finish` is refused, tests are read-only and a failing
finding is one a new test closes (coverage / evidence-thin), the run puts
one question through the auditor seam -- "Allow" or "Keep read-only" --
sealed as a `question` span with its `answer` as a child. Allow lifts the
read-only guard for the rest of the run and is recorded; Keep read-only
takes the existing cap path. Asked at most once per run. With no answer
channel (`saddle auto`, headless) the question is sealed "unanswered,
default taken" and the default is Keep read-only, so the run never blocks.

Known-good: Allow lets the covering test be written and the run finishes;
the packet's Contract row lists the question and answer, cited.
Known-bad: Keep read-only still refuses the test edit and stops "audit
unresolved" after one question, not three; tests already editable, or a
finding no test closes, ask nothing.
"""

from __future__ import annotations

import io
import json
import threading
from pathlib import Path
from typing import Any, cast

import pytest
from test_evidence import _without_stubbed_mutmut
from test_integ import BASE, CLAMP, NEG_TEST, TEST
from test_web_tasks import git

from saddle import cli
from saddle.auto import AutoOptions, AutoResult, run_auto
from saddle.engine import (
    ALLOW_TEST_EDITS,
    AUDIT_UNRESOLVED,
    KEEP_READ_ONLY,
    AutoRun,
    RunBudget,
    TurnOptions,
    run_turn,
)
from saddle.events import Answered, Question
from saddle.journal import attempt_sidecar_path, read_spans, verify_journal
from saddle.packet import compile_packet
from saddle.tools import ToolContext
from saddle.vllm import ToolCall, VllmClient


def call(name: str, cid: str, **arguments: Any) -> ToolCall:
    return ToolCall(id=cid, name=name, arguments=json.dumps(arguments))


ADD_TEST = call("edit_file", "t1", path="test_n.py", old="== 2\n", new="== 2\n" + NEG_TEST)


class Learner:
    """Clamp None to 0, finish (refused: `return 0` is uncovered), then try
    the covering test once the question is settled, else call finish."""

    def __init__(self) -> None:
        self.round = 0
        self.heard: list[str] = []

    def __enter__(self) -> Learner:
        return self

    def __exit__(self, *_: object) -> None:
        return None

    def stream_chat(self, messages: Any, **_: Any) -> Any:
        self.round += 1
        last = str(messages[-1].get("content") or "")
        self.heard.append(last)
        if self.round == 1:
            return iter([call("edit_file", "e1", path="n.py", old="    return x", new=CLAMP)])
        if "allowed test edits" in last or "kept tests read-only" in last:
            return iter([ADD_TEST])  # refused unless allowed
        return iter([call("finish", f"f{self.round}", summary="Clamped None to 0.")])


@pytest.fixture
def repo(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """test_integ's repo (tests at the root, `test_n.py`), the real mutmut."""
    _without_stubbed_mutmut(monkeypatch)
    root = tmp_path / "repo"
    root.mkdir()
    (root / "n.py").write_text(BASE)
    (root / "test_n.py").write_text(TEST)
    (root / ".gitignore").write_text("__pycache__/\n.saddle/\n")
    git(root, "init", "-q", "-b", "main")
    git(root, "add", "-A")
    git(root, "commit", "-q", "-m", "init")
    return root


def run(repo: Path, answer: Any, *, allow: bool = False, **kw: Any) -> tuple[AutoResult, Learner]:
    client = Learner()
    options = AutoOptions(
        task="f(None) should be 0", repo=repo, run_id="ask", allow_test_edits=allow, **kw
    )
    return run_auto(options, cast(VllmClient, client), answer=answer), client


def asked(result: AutoResult) -> list[tuple[Any, Any]]:
    spans = read_spans(result.journal)
    answers = {s.parent_id: s for s in spans if s.name == "answer"}
    return [(q, answers.get(q.span_id)) for q in spans if q.name == "question"]


def sidecar(result: AutoResult) -> dict[str, Any]:
    end = next(s for s in reversed(read_spans(result.journal)) if s.name.startswith("auto:"))
    return cast(
        dict[str, Any], json.loads(attempt_sidecar_path(result.journal, end.span_id).read_text())
    )


# -- known-good ---------------------------------------------------------------


def test_allow_lifts_read_only_for_the_rest_of_the_run_and_it_finishes(repo: Path) -> None:
    questions: list[Question] = []

    def answer(q: Question) -> str:
        questions.append(q)
        return "allow"  # compared without case

    result, client = run(repo, answer)
    assert (result.outcome, result.reason) == ("finished", "finish called")
    [q] = questions
    assert q.id == "test-edits"
    assert q.text == (
        "The auditor needs a test that covers n.py:3. Tests are read-only in this run. "
        "Allow test edits for the rest of this run?"
    )
    assert q.options == [ALLOW_TEST_EDITS, KEEP_READ_ONLY]
    [(question, reply)] = asked(result)
    assert reply is not None
    assert reply.parent_id == question.span_id
    assert reply.argv == ["answer", "allow"]
    # the test edit went through, onto the branch
    assert NEG_TEST in git_show(repo, result.branch, "test_n.py")
    assert not [s for s in read_spans(result.journal) if s.name.startswith("refused:")]
    assert sidecar(result)["test_edits_granted"] is True
    assert sidecar(result)["allow_test_edits"] is False  # what the run started with
    assert any("allowed test edits" in h for h in client.heard)
    assert verify_journal(result.journal) == []
    packet = compile_packet(result.journal)
    contract = {r.key: r for r in packet.rows}["contract"]
    assert contract.status == "observed"
    assert contract.items == (f"You were asked: {q.text} → you answered: allow",)
    assert contract.cites == (question.record_hash, reply.record_hash)
    assert packet.offer_test_edits is False
    assert "tests editable after you allowed it" in packet.header
    scope = {r.key: r for r in packet.rows}["scope"]
    assert scope.text.endswith("Tests were read-only until you allowed edits during the run.")


def git_show(repo: Path, branch: str, path: str) -> str:
    import subprocess

    return subprocess.run(
        ["git", "-C", str(repo), "show", f"{branch}:{path}"],
        capture_output=True,
        text=True,
        check=True,
    ).stdout


# -- known-bad ----------------------------------------------------------------


def test_keep_read_only_takes_the_cap_path_and_is_asked_once(repo: Path) -> None:
    questions: list[Question] = []

    def answer(q: Question) -> str:
        questions.append(q)
        return KEEP_READ_ONLY

    result, client = run(repo, answer)
    assert (result.outcome, result.reason) == ("stopped", AUDIT_UNRESOLVED)
    assert len(questions) == 1  # three refusals, one question
    [(_q, reply)] = asked(result)
    assert reply is not None
    assert reply.detail == KEEP_READ_ONLY
    assert NEG_TEST not in git_show(repo, result.branch, "test_n.py")
    assert [s.name for s in read_spans(result.journal) if s.name.startswith("refused:")] == [
        "refused:edit_file"
    ]
    assert "test_edits_granted" not in sidecar(result)
    assert any("kept tests read-only" in h for h in client.heard)
    assert compile_packet(result.journal).offer_test_edits is True  # UI2's offer still stands


def test_a_run_whose_tests_are_editable_is_never_asked(repo: Path) -> None:
    questions: list[Question] = []
    result, _ = run(repo, questions.append, allow=True, finish_refusal_cap=2)
    assert result.reason == AUDIT_UNRESOLVED  # the learner never wrote the test
    assert questions == []
    assert asked(result) == []


def test_a_headless_saddle_auto_takes_keep_read_only_and_does_not_block(repo: Path) -> None:
    args = cli.build_parser().parse_args(["auto", "f(None) should be 0", "--repo", str(repo)])
    out = io.StringIO()
    codes: list[int] = []
    worker = threading.Thread(
        target=lambda: codes.append(
            cli.run_auto_command(args, cast(VllmClient, Learner()), stdout=out)
        ),
        daemon=True,
    )
    worker.start()
    worker.join(timeout=240)
    assert not worker.is_alive(), "a headless run blocked on a question nobody can answer"
    assert codes == [cli.AUTO_STOPPED]
    journal = Path(out.getvalue().split("ledger ")[1].split()[0])
    spans = read_spans(journal)
    [question] = [s for s in spans if s.name == "question"]
    [reply] = [s for s in spans if s.name == "answer"]
    assert reply.parent_id == question.span_id
    assert reply.argv == ["answer", KEEP_READ_ONLY, "unanswered"]
    assert reply.detail == (
        "unanswered, default taken: Keep read-only (no answer channel: a headless run)"
    )
    assert "asked: The auditor needs a test that covers n.py:3." in out.getvalue()
    assert "→: unanswered, default taken: Keep read-only" in out.getvalue()
    packet = compile_packet(journal)
    assert packet.verdict == "stopped"  # not "needs you": the question was settled
    contract = {r.key: r for r in packet.rows}["contract"]
    assert contract.items[0].endswith(
        "→ no answer came: unanswered, default taken: Keep read-only "
        "(no answer channel: a headless run)"
    )


# -- the engine's branches, with a scripted feed ------------------------------


class Feed:
    """An AuditHooks that refuses finish on `findings` until `ok` is set."""

    def __init__(self, findings: list[dict[str, Any]] | None) -> None:
        self.findings = findings
        self.ok = False

    def before_tool(self, name: str) -> None:
        return None

    def after_tool(self, name: str, ok: bool) -> None:
        if name == "write_file":
            self.ok = True

    def collect(self) -> str:
        return ""

    def final(self) -> tuple[bool, str]:
        # An accepted finish returns no text unless it surfaces not-proven
        # findings; this fake surfaces none.
        return self.ok, "" if self.ok else "coverage FAIL"

    def waivers(self) -> list[str]:
        return []

    def questions(self) -> list[str]:
        return []

    def unchanged(self) -> bool:
        return False

    def unresolved(self) -> list[dict[str, object]]:
        return [{"gate": "coverage", "reason": "evidence-thin", "cites": []}]

    def close(self) -> None:
        return None

    def last(self) -> dict[str, object] | None:
        return None if self.findings is None else {"findings": self.findings}


class Rounds:
    def __init__(self, *rounds: list[ToolCall]) -> None:
        self.rounds = list(rounds)

    def stream_chat(self, messages: Any, **_: Any) -> Any:
        if self.rounds:
            return iter(self.rounds.pop(0))
        return iter([call("finish", "z", summary="done")])


FINISH = [call("finish", "f", summary="done")]
WRITE_TEST = [call("write_file", "w", path="tests/test_x.py", content="def test_x():\n    pass\n")]
THIN = [
    {
        "gate": "coverage",
        "reason": "evidence-thin",
        "verdict": "fail",
        "detail": "no test runs a.py:7",
    }
]


def engine_run(
    tmp_path: Path,
    findings: list[dict[str, Any]] | None,
    answer: Any,
    *rounds: list[ToolCall],
    cap: int = 3,
) -> tuple[AutoRun, list[Any], ToolContext]:
    run = AutoRun(
        budget=RunBudget(time_s=600, tokens=100_000),
        run_span="s",
        feed=cast(Any, Feed(findings)),
        answer=answer,
        finish_refusal_cap=cap,
    )
    ctx = ToolContext(workdir=tmp_path, protected_tests=("tests",))
    options = TurnOptions(workdir=tmp_path, journal=tmp_path / "j" / "proofs.jsonl", auto=run)
    client = cast(VllmClient, Rounds(*rounds))
    events = list(run_turn(client, [], "t", options, turn=1, context=ctx))
    return run, events, ctx


def test_allow_on_the_capping_refusal_reopens_the_run(tmp_path: Path) -> None:
    run, events, ctx = engine_run(tmp_path, THIN, lambda q: "Allow", FINISH, WRITE_TEST, cap=1)
    assert ctx.protected_tests is None
    assert (run.outcome, run.reason) == ("finished", "finish called")
    assert (tmp_path / "tests" / "test_x.py").is_file()
    assert [e.text for e in events if isinstance(e, Question)] == [
        "The auditor needs a test that covers a.py:7. Tests are read-only in this run. "
        "Allow test edits for the rest of this run?"
    ]


def test_keep_on_the_capping_refusal_stops(tmp_path: Path) -> None:
    run, _events, ctx = engine_run(tmp_path, THIN, lambda q: "Keep read-only", FINISH, cap=1)
    assert ctx.protected_tests == ("tests",)
    assert (run.outcome, run.reason) == ("stopped", AUDIT_UNRESOLVED)


def test_a_reply_that_is_no_option_takes_the_default_and_says_so(tmp_path: Path) -> None:
    run, events, ctx = engine_run(tmp_path, THIN, lambda q: "sure, go on", FINISH, cap=1)
    assert ctx.protected_tests == ("tests",)
    assert run.reason == AUDIT_UNRESOLVED
    [reply] = [s for s in read_spans(tmp_path / "j" / "proofs.jsonl") if s.name == "answer"]
    assert reply.argv == ["answer", "sure, go on", "default", KEEP_READ_ONLY]
    assert reply.detail == (
        "sure, go on (not one of Allow, Keep read-only; default taken: Keep read-only)"
    )
    assert [e.text for e in events if isinstance(e, Answered)] == [reply.detail]


def test_a_run_stopped_while_waiting_seals_the_default(tmp_path: Path) -> None:
    run, _events, _ctx = engine_run(tmp_path, THIN, lambda q: None, FINISH, cap=1)
    [reply] = [s for s in read_spans(tmp_path / "j" / "proofs.jsonl") if s.name == "answer"]
    assert reply.detail == "unanswered, default taken: Keep read-only (the run was stopped)"
    assert run.reason == AUDIT_UNRESOLVED


@pytest.mark.parametrize(
    ("findings", "where"),
    [
        (THIN, "a.py:7"),
        (
            [
                {
                    "gate": "mutation",
                    "reason": "evidence-thin",
                    "verdict": "fail",
                    "detail": "3 survived",
                }
            ],
            "the mutation finding",
        ),
        (
            [
                {
                    "gate": "coverage",
                    "reason": "evidence-thin",
                    "verdict": "pass",
                    "detail": "a.py:1",
                },
                *THIN,
            ],
            "a.py:7",
        ),
        (["junk", *THIN], "a.py:7"),
    ],
)
def test_the_question_names_what_the_audit_wants_covered(
    tmp_path: Path, findings: list[Any], where: str
) -> None:
    _run, events, _ctx = engine_run(tmp_path, findings, lambda q: "Keep read-only", FINISH, cap=1)
    [q] = [e for e in events if isinstance(e, Question)]
    assert q.text.startswith(f"The auditor needs a test that covers {where}. ")


@pytest.mark.parametrize(
    "findings",
    [
        None,
        [{"gate": "tests", "reason": "code-wrong", "verdict": "fail", "detail": "a.py:3"}],
        "junk",
    ],
)
def test_a_refusal_no_test_closes_asks_nothing(tmp_path: Path, findings: Any) -> None:
    run, events, _ctx = engine_run(tmp_path, findings, lambda q: "Allow", FINISH, cap=1)
    assert not [e for e in events if isinstance(e, Question)]
    assert run.reason == AUDIT_UNRESOLVED
