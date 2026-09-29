"""P1 in `saddle auto` (opt-in): a sealed file, or an extraction beside the worker.

R9-p: a `finish` called while the extraction has not finished, or after it
failed, is accepted (a question refuses nothing) and the run ends "needs
you" with the reason; before `finish` a pending extraction is only "not
proven" at a checkpoint.
"""

from __future__ import annotations

import io
import json
import threading
from concurrent.futures import Future
from pathlib import Path
from typing import Any, cast

import pytest
from test_feed import FINISH, FakeAuditor, Reactive, call, repo, sidecar
from test_task_passes import Scripted

from saddle import cli
from saddle.auditor import TASK_REQUIREMENTS, AuditorConfig, Finding, Findings
from saddle.auto import P1_FILE, AutoError, AutoOptions, AutoResult, run_auto
from saddle.feed import P1_PENDING, P1_UNFINISHED, AuditFeed
from saddle.journal import read_spans
from saddle.vllm import VllmClient

__all__ = ["repo"]

FIX = call("edit_file", "e", path="calc.py", old="a - b", new="a + b")


class P1Auditor(FakeAuditor):
    """Passes; tier 1 adds a passing task-requirements finding once it has a file."""

    def __init__(self, config: AuditorConfig) -> None:
        super().__init__()
        self.config = config

    def tier1(self, tree: Path | None = None) -> Findings:
        got = super().tier1(tree)
        if self.config.task_requirements is None:
            return got
        ok = Finding(TASK_REQUIREMENTS, 1, "pass", "code-wrong", "1 example(s) matched", ("x",))
        return Findings(tier=1, key="k1", findings=(*got.findings, ok))


class Factory:
    def __init__(self) -> None:
        self.configs: list[AuditorConfig] = []

    def __call__(self, repo: Path, baseline: str, config: AuditorConfig) -> P1Auditor:
        self.configs.append(config)
        return P1Auditor(config)


def feed(repo: Path, tmp_path: Path, p1: Future[Path], factory: Factory, wait: float) -> AuditFeed:
    (repo / "calc.py").write_text("def add(a, b):\n    return a + b\n")
    return AuditFeed(
        worktree=repo,
        baseline="HEAD",
        journal=tmp_path / "j.jsonl",
        run_span="s",
        factory=factory,
        p1=p1,
        p1_wait=lambda: wait,
    )


def test_r9_p_finish_while_the_extraction_is_pending_asks(repo: Path, tmp_path: Path) -> None:
    factory = Factory()
    pending: Future[Path] = Future()
    fed = feed(repo, tmp_path, pending, factory, 0.05)
    assert "(not proven, does not refuse) task-requirements (tier 1): " + P1_PENDING in fed.check()
    assert fed.final() == (True, "")  # accepted: a question refuses nothing
    assert fed.questions() == [f"task-requirements (tier 1): P1 could not run: {P1_UNFINISHED}"]
    assert [c.task_requirements for c in factory.configs] == [None]


def test_r9_p_a_failed_extraction_asks_at_finish(repo: Path, tmp_path: Path) -> None:
    failed: Future[Path] = Future()
    failed.set_exception(RuntimeError("server down"))
    fed = feed(repo, tmp_path, failed, Factory(), 0.0)
    assert "the extraction failed: RuntimeError: server down" in fed.check()
    assert fed.final() == (True, "")
    assert fed.questions() == [
        "task-requirements (tier 1): P1 could not run: the extraction failed: "
        "RuntimeError: server down"
    ]


def test_a_ready_file_goes_to_the_auditor_and_nothing_is_asked(repo: Path, tmp_path: Path) -> None:
    factory = Factory()
    ready: Future[Path] = Future()
    ready.set_result(tmp_path / "f.json")
    fed = feed(repo, tmp_path, ready, factory, 0.0)
    assert fed.final() == (True, "")
    assert fed.questions() == []
    assert [c.task_requirements for c in factory.configs] == [None, tmp_path / "f.json"]
    assert [f.gate for f in fed.results[-1].findings] == [
        "ruff",
        "tests",
        TASK_REQUIREMENTS,
        "mutation",
    ]


# -- through run_auto ---------------------------------------------------------------


class Both(Reactive):
    """The worker's script (stream_chat) and the extraction's passes (complete)."""

    def __init__(
        self, script: list[list[Any]], scripted: Scripted, gate: threading.Event | None = None
    ) -> None:
        super().__init__(script)
        self.scripted = scripted
        self.gate = gate

    def complete(self, prompt: str, **kw: Any) -> str:
        if self.gate is not None:
            self.gate.wait(10)
        return self.scripted.complete(prompt, **kw)


def run(repo: Path, client: Both, factory: Factory, **kw: Any) -> AutoResult:
    options = AutoOptions(
        task="make add add", repo=repo, run_id="p1", auditor_factory=factory, **kw
    )
    return run_auto(options, cast(VllmClient, client))


def test_extraction_beside_the_worker_seals_a_file_the_finish_audit_uses(repo: Path) -> None:
    factory = Factory()
    client = Both([[FIX], [FINISH]], Scripted("no inputs, not json"))
    result = run(repo, client, factory, extract_requirements=True)
    assert result.outcome == "finished"
    sealed = result.journal.parent / P1_FILE
    assert json.loads(sealed.read_text())["examples"] == []
    assert factory.configs[-1].task_requirements == sealed
    assert sidecar(result)["task_requirements"] == "extracted at run start"
    start = next(s for s in read_spans(result.journal) if s.name == "auto:start")
    assert start.detail.endswith("; task requirements extracted at run start")


def test_r9_p_end_to_end_an_unfinished_extraction_ends_the_run_needing_you(repo: Path) -> None:
    gate = threading.Event()
    releaser = threading.Timer(3.0, gate.set)
    releaser.start()
    try:
        client = Both([[FIX], [FINISH]], Scripted("x"), gate=gate)
        result = run(repo, client, Factory(), extract_requirements=True, time_budget_s=1.0)
    finally:
        releaser.cancel()
        gate.set()
    assert result.outcome == "stopped"
    assert result.reason.startswith("needs you: the audit asks 1 question(s): task-requirements")
    assert P1_UNFINISHED in result.reason
    assert sidecar(result)["finish_refusals"] == 0


def test_a_given_file_is_used_from_the_first_audit(repo: Path, tmp_path: Path) -> None:
    factory = Factory()
    given = tmp_path / "given.json"
    result = run(repo, Both([[FIX], [FINISH]], Scripted("x")), factory, task_requirements=given)
    assert result.outcome == "finished"
    assert [c.task_requirements for c in factory.configs] == [None, given.resolve()]
    assert sidecar(result)["task_requirements"] == str(given)


def test_p1_options_are_refused_where_they_cannot_run(repo: Path, tmp_path: Path) -> None:
    both = {"task_requirements": tmp_path / "f", "extract_requirements": True}
    with pytest.raises(AutoError, match="give one, not both"):
        run(repo, Both([], Scripted("x")), Factory(), **both)
    with pytest.raises(AutoError, match="arm E has none"):
        run(repo, Both([], Scripted("x")), Factory(), extract_requirements=True, arm="E")


def test_the_cli_flags_reach_the_options(repo: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    seen: list[AutoOptions] = []

    def spy(options: AutoOptions, client: Any, **kw: Any) -> AutoResult:
        seen.append(options)
        msg = "stop here"
        raise AutoError(msg)

    monkeypatch.setattr(cli, "run_auto", spy)
    for flags in (["--task-requirements", "f.json"], ["--extract-requirements"]):
        args = cli.build_parser().parse_args(["auto", "t", "--repo", str(repo), *flags])
        assert cli.run_auto_command(args, cast(VllmClient, None), stdout=io.StringIO()) == 1
    assert [(o.task_requirements, o.extract_requirements) for o in seen] == [
        (Path("f.json"), False),
        (None, True),
    ]
    with pytest.raises(SystemExit):
        cli.build_parser().parse_args(
            ["auto", "t", "--task-requirements", "f", "--extract-requirements"]
        )
