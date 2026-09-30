"""Audit feedback into autonomous runs (arms E+A+F, E+A and E).

The contract, each half both ways:

- a checkpoint is the first non-edit tool call after a burst of edits, and
  its tier-1 audit runs off the model's critical path, arriving appended to
  a later tool result (E+A+F only);
- `finish` is refused while the finish audit (tier 0 on the changed files
  + tier 1 + tier 2) fails, in E+A+F only; a budget that runs out first ends
  the run `stopped` with the last findings in the outcome sidecar;
- E+A delivers nothing and never refuses, but journals and records the
  verdict; E never constructs an auditor;
- the arm is sealed, so the ledger alone tells the arms apart;
- vacuity: a model that acts on a delivered finding ends finished with a
  passing audit, while the same script without feedback ends with a
  recorded failing one.
"""

from __future__ import annotations

import io
import json
import re
import subprocess
import threading
import time
from pathlib import Path
from typing import Any, cast

import pytest
from test_audit import VENV_TEST, _files_outside_git

from saddle import cli, engine, feed
from saddle.audit import AuditError
from saddle.auditor import AuditorConfig, Finding, Findings, sanction
from saddle.auto import AutoError, AutoOptions, AutoResult, run_auto
from saddle.engine import FINISH_REFUSED
from saddle.events import AuditNote, Event
from saddle.feed import AuditFeed, AuditResult, render, snapshot
from saddle.journal import attempt_sidecar_path, read_spans, verify_journal
from saddle.packet import compile_packet
from saddle.vllm import ToolCall, VllmClient, VllmError

BUGGY = "def add(a, b):\n    return a - b\n"
TEST = "from calc import add\n\n\ndef test_add():\n    assert add(2, 2) == 4\n"


def git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(repo), "-c", "user.name=t", "-c", "user.email=t@t", *args],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    root = tmp_path / "repo"
    (root / "tests").mkdir(parents=True)
    (root / "calc.py").write_text(BUGGY)
    (root / "tests" / "test_calc.py").write_text(TEST)
    git(root, "init", "-q", "-b", "main")
    git(root, "add", "-A")
    git(root, "commit", "-q", "-m", "init")
    return root


def call(name: str, call_id: str = "c1", **arguments: Any) -> ToolCall:
    return ToolCall(id=call_id, name=name, arguments=json.dumps(arguments))


EDIT_COMMENT = call("edit_file", "e1", path="calc.py", old="def add", new="# adds\ndef add")
CHECK = call("run_command", "r1", command="true")
FINISH = call("finish", "f1", summary="done")


class FakeAuditor:
    """Fails `tests` while calc.py still subtracts; names the line."""

    def __init__(self, latch: threading.Event | None = None) -> None:
        self.latch = latch
        self.started = threading.Event()
        self.returned = threading.Event()
        self.calls: list[tuple[int, str]] = []

    def _findings(self, tier: int, tree: Path | None) -> Findings:
        assert tree is not None
        text = (tree / "calc.py").read_text()
        self.calls.append((tier, text))
        lines = text.splitlines()
        bad = [f"calc.py:{i}: {line.strip()}" for i, line in enumerate(lines, 1) if "a - b" in line]
        verdict = "fail" if bad else "pass"
        detail = "; ".join(bad) or "1 passed"
        gate = "tests" if tier == 1 else "mutation"
        found = Finding(gate, tier, verdict, "code-wrong", detail, ("fake",))  # type: ignore[arg-type]
        return Findings(tier=tier, key=f"k{tier}", findings=(found,))

    def tier0(self, path: str, new_text: str) -> Findings:
        self.calls.append((0, path))
        found = Finding("ruff", 0, "pass", "code-wrong", "ruff clean", ("fake",))
        return Findings(tier=0, key="k0", findings=(found,))

    def tier1(self, tree: Path | None = None) -> Findings:
        self.started.set()
        if self.latch is not None:
            assert self.latch.wait(10)
        try:
            return self._findings(1, tree)
        finally:
            self.returned.set()

    def tier2(self, tree: Path | None = None) -> Findings:
        return self._findings(2, tree)


class Reactive:
    """Replays `script`; after an audit FAIL naming `a - b`, edits that line.

    It is the stand-in for a model that acts on a delivered finding. It never
    sees the finding unless the engine puts it in a message it is sent.
    """

    model = "reactive-stand-in"
    """What a record of its calls names, as `VllmClient.model` is for a real client."""

    def __init__(self, script: list[list[Any]], tail: list[Any] | None = None) -> None:
        self.script = list(script)
        self.tail = tail if tail is not None else [FINISH]
        self.seen: list[str] = []
        self.reacted = 0
        self.hooks: dict[int, Any] = {}
        self.round = 0
        self.messages: list[dict[str, Any]] = []

    def stream_chat(self, messages: Any, **kwargs: Any) -> Any:
        self.round += 1
        hook = self.hooks.get(self.round)
        if hook is not None:
            hook()
        self.messages = list(messages)
        last = messages[-1]
        content = str(last.get("content") or "")
        self.seen.extend(str(m.get("content") or "") for m in messages)
        if "[audit" in content and "FAIL]" in content and "a - b" in content:
            self.reacted += 1
            return iter(
                [call("edit_file", f"fix{self.reacted}", path="calc.py", old="a - b", new="a + b")]
            )
        if self.script:
            return iter(self.script.pop(0))
        return iter(list(self.tail))


def run(
    repo: Path, client: Any, arm: str = "E+A+F", auditor: Any = None, **kwargs: Any
) -> tuple[AutoResult, list[Event]]:
    events: list[Event] = []
    fake = auditor if auditor is not None else FakeAuditor()
    options = AutoOptions(
        task="make add add",
        repo=repo,
        run_id=kwargs.pop("run_id", "r1"),
        arm=arm,  # type: ignore[arg-type]
        auditor_factory=lambda *a: fake,
        **kwargs,
    )
    return run_auto(options, cast(VllmClient, client), on_event=events.append), events


def sidecar(result: AutoResult) -> dict[str, Any]:
    span = [s for s in read_spans(result.journal) if s.name.startswith("auto:")][-1]
    return cast(
        dict[str, Any], json.loads(attempt_sidecar_path(result.journal, span.span_id).read_text())
    )


def audit_spans(result: AutoResult) -> list[str]:
    return [s.name for s in read_spans(result.journal) if s.name.startswith("audit:")]


# -- vacuity: a delivered finding can change the model's next action ----------


def test_feedback_turns_the_same_script_from_a_recorded_fail_into_a_pass(repo: Path) -> None:
    script = [[EDIT_COMMENT], [CHECK], [FINISH]]
    with_feedback = Reactive(script)
    result, events = run(repo, with_feedback, "E+A+F", run_id="f")
    assert result.outcome == "finished"
    assert with_feedback.reacted >= 1
    record = sidecar(result)
    assert record["arm"] == "E+A+F"
    assert record["audit"]["passed"] is True
    assert record["audit"]["point"] == "finish"
    assert "a + b" in (result.worktree / "calc.py").read_text()
    assert any(isinstance(e, AuditNote) for e in events)
    assert verify_journal(result.journal) == []

    without = Reactive(script)
    other, _ = run(repo, without, "E+A", run_id="n")
    assert other.outcome == "finished"
    assert without.reacted == 0
    record = sidecar(other)
    assert record["arm"] == "E+A"
    assert record["audit"]["passed"] is False
    assert record["audit"]["findings"][1]["detail"] == "calc.py:3: return a - b"
    assert "a - b" in (other.worktree / "calc.py").read_text()


def test_no_feedback_never_shows_the_model_an_audit(repo: Path) -> None:
    spy = Reactive([[EDIT_COMMENT], [CHECK], [CHECK], [FINISH]])
    fake = FakeAuditor()
    result, events = run(repo, spy, "E+A", auditor=fake)
    assert fake.calls, "the auditor ran"
    assert not any("[audit" in text for text in spy.seen)
    assert not any(isinstance(e, AuditNote) for e in events)
    assert set(audit_spans(result)) == {"audit:withheld"}
    assert sidecar(result)["finish_refusals"] == 0


def test_no_audit_never_constructs_an_auditor(repo: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    made: list[object] = []

    def spy(*args: object) -> FakeAuditor:
        made.append(args)
        return FakeAuditor()

    class Boom:
        def __init__(self, *args: object) -> None:
            msg = "Auditor constructed in arm E"
            raise AssertionError(msg)

    monkeypatch.setattr(feed, "Auditor", Boom)
    client = Reactive([[EDIT_COMMENT], [CHECK], [FINISH]])
    options = AutoOptions(task="t", repo=repo, run_id="e", arm="E", auditor_factory=spy)
    result = run_auto(options, cast(VllmClient, client))
    assert made == []
    assert result.outcome == "finished"
    assert audit_spans(result) == []
    record = sidecar(result)
    assert record["arm"] == "E"
    assert record["audit"] is None
    # and the default factory really is the one Boom replaced
    with pytest.raises(AssertionError, match="arm E"):
        feed.default_auditor(repo, "HEAD", AuditorConfig())


# -- the arm is sealed ---------------------------------------------------------


def test_the_three_arms_are_distinguishable_from_the_ledger_alone(repo: Path) -> None:
    seen = {}
    for arm in ("E", "E+A", "E+A+F"):
        result, _ = run(
            repo, Reactive([[EDIT_COMMENT], [CHECK], [FINISH]]), arm, run_id=arm[-1] + str(len(arm))
        )
        start = next(s for s in read_spans(result.journal) if s.name == "auto:start")
        end = [s for s in read_spans(result.journal) if s.name.startswith("auto:")][-1]
        assert start.detail.startswith(f"arm {arm};")
        assert f"; arm {arm};" in end.detail
        seen[arm] = (sidecar(result)["arm"], set(audit_spans(result)))
        assert verify_journal(result.journal) == []
    assert seen == {
        "E": ("E", set()),
        "E+A": ("E+A", {"audit:withheld"}),
        "E+A+F": ("E+A+F", {"audit:delivered", "audit:withheld"}),
    }


def test_an_unknown_arm_is_refused_before_anything_runs(repo: Path) -> None:
    with pytest.raises(AutoError, match="unknown arm"):
        run(repo, Reactive([]), "A")
    assert not (repo / ".saddle").exists()


def test_the_cli_flags_select_the_arm(repo: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    arms = []
    real = run_auto  # the object cli.run_auto names (saddle.auto's)

    def spy(options: AutoOptions, client: Any, **kw: Any) -> AutoResult:
        arms.append(options.arm)
        return real(
            AutoOptions(**{**options.__dict__, "auditor_factory": lambda *a: FakeAuditor()}),
            client,
            **kw,
        )

    monkeypatch.setattr(cli, "run_auto", spy)
    shown = {}
    for flags in ([], ["--no-feedback"], ["--no-audit"]):
        args = cli.build_parser().parse_args(["auto", "t", "--repo", str(repo), *flags])
        out = io.StringIO()
        client = Reactive([[EDIT_COMMENT], [CHECK], [FINISH]])
        cli.run_auto_command(args, cast(VllmClient, client), stdout=out)
        text = out.getvalue()
        arm = re.search(r"\(arm ([^)]+)\)", text)
        assert arm is not None, text
        shown[" ".join(flags)] = ("[audit checkpoint 1 on tree" in text, arm[1])
    assert shown == {
        "": (True, "E+A+F"),
        "--no-feedback": (False, "E+A"),
        "--no-audit": (False, "E"),
    }
    assert arms == ["E+A+F", "E+A", "E"]
    with pytest.raises(SystemExit):
        cli.build_parser().parse_args(["auto", "t", "--no-audit", "--no-feedback"])


# -- the checkpoint trigger ----------------------------------------------------


@pytest.fixture
def unit(repo: Path, tmp_path: Path) -> tuple[AuditFeed, FakeAuditor]:
    fake = FakeAuditor()
    unit_feed = AuditFeed(
        worktree=repo,
        baseline=git(repo, "rev-parse", "HEAD"),
        journal=tmp_path / "j.jsonl",
        run_span="s",
        auditor=fake,
    )
    return unit_feed, fake


def test_a_checkpoint_is_the_first_non_edit_call_after_edits(
    unit: tuple[AuditFeed, FakeAuditor],
) -> None:
    f, fake = unit
    f.before_tool("read_file")
    assert f.checkpoints == 0  # nothing edited yet
    f.after_tool("edit_file", ok=False)
    f.before_tool("run_command")
    assert f.checkpoints == 0  # a refused edit is not an edit
    f.after_tool("edit_file", ok=True)
    f.before_tool("write_file")
    f.after_tool("write_file", ok=True)
    assert f.checkpoints == 0  # still inside the burst
    f.before_tool("run_command")
    assert f.checkpoints == 1
    f.after_tool("run_command", ok=True)
    f.before_tool("read_file")
    assert f.checkpoints == 1  # the burst already ended
    f.after_tool("edit_file", ok=True)
    f.before_tool("finish")
    assert f.checkpoints == 1  # finish is audited by `final`, not as a checkpoint
    f.close()
    assert [c[0] for c in fake.calls] == [1]


def test_the_checkpoint_audits_the_tree_as_it_was_when_the_burst_ended(
    repo: Path, tmp_path: Path
) -> None:
    latch = threading.Event()
    fake = FakeAuditor(latch)
    f = AuditFeed(
        worktree=repo,
        baseline=git(repo, "rev-parse", "HEAD"),
        journal=tmp_path / "j.jsonl",
        run_span="s",
        auditor=fake,
    )
    f.after_tool("edit_file", ok=True)
    f.before_tool("run_command")
    (repo / "calc.py").write_text("def add(a, b):\n    return a + b\n")  # a later edit
    latch.set()
    f.close()
    assert fake.calls == [(1, BUGGY)]
    assert f.results[0].tree == git(repo, "rev-parse", "HEAD^{tree}")
    assert not f.results[0].passed


def test_a_burst_that_ends_mid_audit_never_waits_and_is_audited_next(
    repo: Path, tmp_path: Path
) -> None:
    latch = threading.Event()
    fake = FakeAuditor(latch)
    f = AuditFeed(
        worktree=repo,
        baseline=git(repo, "rev-parse", "HEAD"),
        journal=tmp_path / "j.jsonl",
        run_span="s",
        auditor=fake,
    )
    f.after_tool("edit_file", ok=True)
    f.before_tool("run_command")  # checkpoint 1, held by the latch
    assert fake.started.wait(10)
    passed_over = "def add(a, b):\n    return b - a\n"
    fixed = "def add(a, b):\n    return a + b\n"
    returned = threading.Event()

    def two_bursts_end() -> None:
        for text in (passed_over, fixed):
            (repo / "calc.py").write_text(text)
            f.after_tool("edit_file", ok=True)
            f.before_tool("run_command")
        returned.set()

    threading.Thread(target=two_bursts_end, daemon=True).start()
    try:
        assert returned.wait(5)  # neither tool call waited for checkpoint 1
        assert f.checkpoints == 1  # nor started a second audit beside it
    finally:
        latch.set()
    deadline = time.monotonic() + 10
    while f.checkpoints == 1 and time.monotonic() < deadline:
        f.before_tool("read_file")  # the first call after checkpoint 1 completes
        time.sleep(0.01)
    f.close()
    # scope narrowed: the newest tree is audited; the one it superseded never is
    assert fake.calls == [(1, BUGGY), (1, fixed)]


# -- async delivery ------------------------------------------------------------


def test_the_model_keeps_generating_while_the_checkpoint_audit_is_pending(
    repo: Path, landed: threading.Event
) -> None:
    latch = threading.Event()
    fake = FakeAuditor(latch)
    client = Reactive(
        [[EDIT_COMMENT], [CHECK], [call("read_file", "r2", path="calc.py")]]
        + [[call("read_file", f"x{i}", path="calc.py")] for i in range(20)]
    )
    pending: list[bool] = []

    def while_pending() -> None:
        pending.append(fake.started.wait(10) and not fake.returned.is_set())
        assert not any("[audit" in text for text in client.seen)
        latch.set()
        assert landed.wait(10)

    client.hooks[3] = while_pending  # the generation after the checkpoint's tool
    result, events = run(repo, client, "E+A+F", auditor=fake)
    assert pending == [True]
    notes = [e for e in events if isinstance(e, AuditNote)]
    assert notes
    assert notes[0].text.startswith("[audit checkpoint 1 on tree ")
    assert "FAIL]" in notes[0].text
    assert "- tests (tier 1): fail, code-wrong: calc.py:3: return a - b" in notes[0].text
    assert client.reacted >= 1
    assert result.outcome == "finished"
    delivered = [s for s in read_spans(result.journal) if s.name == "audit:delivered"]
    assert delivered[0].argv[:2] == ["audit", "checkpoint 1"]
    # it landed during round 3's generation, so round 3's tool result carries it
    answer = next(m for m in client.messages if m.get("tool_call_id") == "r2")
    assert "[audit checkpoint 1" in answer["content"]


def test_render_shows_failures_in_full_and_counts_passes() -> None:
    ok = Finding("coverage", 1, "pass", "evidence-thin", "fine", ())
    bad = Finding("tests", 1, "fail", "code-wrong", "x" * (feed.DETAIL_CHARS + 5), ())
    text = render(AuditResult("finish", "a" * 40, (ok, bad), note="n"))
    assert text.splitlines()[0] == f"[audit finish on tree {'a' * 12}: FAIL]"
    assert text.splitlines()[1] == "n"
    assert text.endswith("(1 other check(s) passed or not applicable)")
    assert "x" * feed.DETAIL_CHARS + " ..." in text
    assert render(AuditResult("finish", "b" * 40, (ok,))).startswith(
        "[audit finish on tree bbbbbbbbbbbb: PASS]"
    )


# -- finish ---------------------------------------------------------------------


def test_finish_is_refused_while_the_audit_fails_and_names_the_findings(repo: Path) -> None:
    class Stubborn(Reactive):
        def stream_chat(self, messages: Any, **kwargs: Any) -> Any:
            self.seen.append(str(messages[-1].get("content") or ""))
            if self.script:
                return iter(self.script.pop(0))
            return iter([FINISH])

    client = Stubborn([[EDIT_COMMENT], [FINISH]])
    fake = FakeAuditor()
    # the cap is lifted so the token budget, not the cap, ends this run
    result, _ = run(repo, client, "E+A+F", auditor=fake, token_budget=40, finish_refusal_cap=10_000)
    assert result.outcome == "stopped"
    assert "token budget exhausted" in result.reason
    refusals = [t for t in client.seen if t.startswith(FINISH_REFUSED)]
    assert refusals
    assert "calc.py:3: return a - b" in refusals[0]
    record = sidecar(result)
    finishes = [s for s in read_spans(result.journal) if s.argv[:1] == ["finish"]]
    assert record["finish_refusals"] == len(finishes) >= len(refusals)
    assert all(s.exit_code == 1 for s in finishes)
    assert record["audit"]["passed"] is False
    assert record["audit"]["point"] == "finish"
    # tier 0 (ruff, passing here) precedes the failing tier-1 finding
    assert [f["tier"] for f in record["audit"]["findings"]] == [0, 1, 2]
    assert record["audit"]["findings"][1]["verdict"] == "fail"
    # tier 2 ran at finish, not only tier 1
    assert (2, fake.calls[-1][1]) in fake.calls


def test_the_finish_audit_runs_tier_two_and_a_tier_two_failure_refuses(repo: Path) -> None:
    class Tier2Fails(FakeAuditor):
        def tier2(self, tree: Path | None = None) -> Findings:
            self.calls.append((2, ""))
            found = Finding("mutation", 2, "fail", "evidence-thin", "calc.py:2 survived", ())
            return Findings(tier=2, key="k2", findings=(found,))

    fake = Tier2Fails()
    client = Reactive(
        [[call("edit_file", "e", path="calc.py", old="a - b", new="a + b")], [FINISH]]
    )
    result, _ = run(repo, client, "E+A+F", auditor=fake, token_budget=60)
    assert [c[0] for c in fake.calls][:3] == [0, 1, 2]
    assert result.outcome == "stopped"
    record = sidecar(result)
    assert record["audit"]["passed"] is False
    assert [f["gate"] for f in record["audit"]["findings"]] == ["ruff", "tests", "mutation"]


def test_a_finished_run_records_the_tree_its_final_audit_saw(repo: Path) -> None:
    client = Reactive(
        [[call("edit_file", "e", path="calc.py", old="a - b", new="a + b")], [FINISH]]
    )
    result, _ = run(repo, client, "E+A+F")
    assert result.outcome == "finished"
    record = sidecar(result)
    assert record["audit"]["tree"] == git(result.worktree, "rev-parse", f"{result.commit}^{{tree}}")
    assert record["audit"]["passed"] is True


class NothingToAudit(FakeAuditor):
    def tier1(self, tree: Path | None = None) -> Findings:
        from saddle.audit import AuditError

        msg = "nothing to audit: the tree equals baseline 0123"
        raise AuditError(msg)


@pytest.mark.parametrize("arm", ["E+A+F", "E+A"])
def test_an_unchanged_tree_is_nothing_to_audit_and_ends_unchanged_not_finished(
    repo: Path, arm: str
) -> None:
    """flip: was `outcome == "finished"`, which sealed a tree equal to its
    baseline as accepted work with nothing audited. Not accepted and not a
    refusal: no refusal counted, no cap, the note sealed, ends `unchanged`."""
    result, _ = run(repo, Reactive([[FINISH]]), arm, auditor=NothingToAudit(), finish_refusal_cap=1)
    assert result.outcome == "unchanged"
    assert "no change was made" in result.reason
    record = sidecar(result)
    assert record["outcome"] == "unchanged"
    assert record["finish_refusals"] == 0
    assert record["unchanged_refusals"] == 0
    assert record["audit"]["findings"] == []
    assert record["audit"]["note"].startswith("nothing to audit")
    assert finish_results(result) == [engine.FINISH_UNCHANGED]
    assert verify_journal(result.journal) == []
    end = [s for s in read_spans(result.journal) if s.name.startswith("auto:")][-1]
    assert (end.name, end.exit_code) == ("auto:unchanged", 3)


def test_an_ending_already_decided_is_not_overwritten_by_unchanged() -> None:
    auto = engine.AutoRun(budget=engine.RunBudget(time_s=1, tokens=1), run_span="s")
    auto.stop("cancelled")
    auto.end_unchanged("done")
    assert (auto.outcome, auto.reason, auto.narrative) == ("stopped", "cancelled", "")


def test_the_real_auditor_does_not_accept_an_unchanged_tree(real_repo: Path) -> None:
    """Known-bad (a finish that changed nothing): the finish
    audit of a tree equal to its baseline must not accept it."""
    fed = AuditFeed(real_repo, "HEAD", real_repo.parent / "j.jsonl", "probe")
    assert fed.final() == (False, "")
    assert fed.unchanged()
    client = Reactive([[FINISH]])
    result = run_auto(
        AutoOptions(task="make f return 2", repo=real_repo, run_id="u"), cast(VllmClient, client)
    )
    assert result.outcome == "unchanged"
    packet = compile_packet(result.journal)
    assert packet.verdict == "unchanged"
    assert packet.verdict_text.startswith("Unchanged: finish was called on a tree equal")
    gaps = {r.key: r for r in packet.rows}["not-proven"].items
    assert "No change was made: the tree equals the baseline, so nothing was audited." in gaps
    assert transcript_verdict(result.journal)[1].startswith("UNCHANGED")


def test_a_changed_tree_still_finishes_and_unchanged_is_false(repo: Path) -> None:
    """Known-good: a real change is audited and accepted as before."""
    fix = call("edit_file", "e1", path="calc.py", old="a - b", new="a + b")
    result, _ = run(repo, Reactive([[fix], [FINISH]]))
    assert result.outcome == "finished"


def test_an_audit_that_crashes_is_blocked_and_refuses_finish(repo: Path) -> None:
    class Crashes(FakeAuditor):
        def tier1(self, tree: Path | None = None) -> Findings:
            msg = "disk full"
            raise RuntimeError(msg)

    client = Reactive(
        [[call("edit_file", "e", path="calc.py", old="a - b", new="a + b")], [FINISH]]
    )
    result, _ = run(repo, client, "E+A+F", auditor=Crashes(), token_budget=40)
    assert result.outcome == "stopped"
    finding = sidecar(result)["audit"]["findings"][0]
    assert (finding["verdict"], finding["gate"]) == ("blocked", "audit")
    assert "RuntimeError: disk full" in finding["detail"]


def test_an_audit_error_other_than_nothing_to_audit_is_blocked(tmp_path: Path) -> None:
    (tmp_path / "plain").mkdir()
    f = AuditFeed(
        worktree=tmp_path / "plain",  # not a git repository: the snapshot's clone fails
        baseline="HEAD",
        journal=tmp_path / "j",
        run_span="s",
        auditor=FakeAuditor(),
    )
    ok, text = f.final()
    assert not ok
    finding = f.results[-1].findings[0]
    assert finding.verdict == "blocked"
    assert finding.detail.startswith("the audit could not run: git clone")
    assert "- audit (tier 1): blocked, unknown:" in text


def test_snapshot_mirrors_additions_and_deletions(repo: Path, tmp_path: Path) -> None:
    (repo / "new.py").write_text("x = 1\n")
    (repo / "tests" / "test_calc.py").unlink()
    tree = snapshot(repo, repo, tmp_path / "snap")
    listed = git(tmp_path / "snap", "ls-tree", "-r", "--name-only", tree).splitlines()
    assert listed == ["calc.py", "new.py"]


class SeesFiles(FakeAuditor):
    """Records, at each tier-1 audit, every file of the tree it is handed."""

    def __init__(self) -> None:
        super().__init__()
        self.trees: list[set[str]] = []

    def tier1(self, tree: Path | None = None) -> Findings:
        assert tree is not None
        self.trees.append(_files_outside_git(tree))
        return super().tier1(tree)


def test_no_audited_tree_holds_a_path_git_ignores_and_each_holds_every_untracked_file(
    repo: Path, tmp_path: Path
) -> None:
    """Contract: every tree the feed audits -- a checkpoint's, through its
    frozen copy, and a check's or finish's, straight from the worktree --
    holds no path git ignores in the worktree and every untracked file git
    does not ignore.

    Red at 0dd289d: both copies took everything but `.git`, so a gitignored
    `.venv` the model made was copied twice per checkpoint, the first time on
    the model's critical path, and was handed to the audit."""
    worktree = tmp_path / "worktree"
    git(repo, "worktree", "add", "-q", "-b", "run", str(worktree), "HEAD")
    assert (worktree / ".git").is_file()  # linked, as `auto` makes a run's worktree
    (worktree / ".gitignore").write_text(".venv/\n")
    (worktree / VENV_TEST).parent.mkdir(parents=True)
    (worktree / VENV_TEST).write_text("def test_x():\n    pass\n")
    (repo / ".git" / "info").mkdir(exist_ok=True)
    (repo / ".git" / "info" / "exclude").write_text("scratch.txt\n")  # shared by worktrees
    (worktree / "scratch.txt").write_text("ignored by the repository's info/exclude\n")
    (worktree / "new.py").write_text("x = 1\n")
    seen = SeesFiles()
    f = AuditFeed(
        worktree=worktree,
        baseline=git(worktree, "rev-parse", "HEAD"),
        journal=tmp_path / "j.jsonl",
        run_span="s",
        auditor=seen,
    )
    f.after_tool("edit_file", ok=True)
    f.before_tool("run_command")  # a checkpoint: the frozen copy, then its snapshot
    f.check()  # the worktree's own snapshot, once the checkpoint is done
    f.close()
    expected = {".gitignore", "calc.py", "new.py", "tests/test_calc.py"}
    assert seen.trees == [expected, expected]


def test_a_checkpoint_whose_copy_fails_reports_it_and_the_tool_call_goes_ahead(
    repo: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A checkpoint that cannot take its frozen copy (git cannot say what it
    ignores) is a blocked checkpoint naming why, never an exception out of
    the tool hook: that would end the run over an advisory audit."""
    worktree = tmp_path / "worktree"
    git(repo, "worktree", "add", "-q", "-b", "run", str(worktree), "HEAD")

    def fails(_root: Path) -> frozenset[str]:
        msg = "git ls-files failed: broken"
        raise AuditError(msg)

    monkeypatch.setattr(feed, "git_ignored", fails)
    seen = SeesFiles()
    f = AuditFeed(
        worktree=worktree,
        baseline=git(worktree, "rev-parse", "HEAD"),
        journal=tmp_path / "j.jsonl",
        run_span="s",
        auditor=seen,
    )
    f.after_tool("edit_file", ok=True)
    f.before_tool("run_command")
    f.close()
    assert seen.trees == []
    (result,) = f.results
    (finding,) = result.findings
    assert finding.verdict == "blocked"
    assert "git ls-files failed: broken" in finding.detail


class RuffFails(FakeAuditor):
    """Tier 0 fails (a formatting finding); records every tier asked for."""

    def tier0(self, path: str, new_text: str) -> Findings:
        self.calls.append((0, path))
        found = Finding("ruff", 0, "fail", "code-wrong", "ruff format --check exited 1", ("fake",))
        return Findings(tier=0, key="k0", findings=(found,))


@pytest.mark.parametrize(("auditor", "later_tiers_run"), [(RuffFails, False), (FakeAuditor, True)])
def test_the_finish_audit_refuses_on_a_failing_edit_check_before_running_the_suite(
    repo: Path, tmp_path: Path, auditor: type[FakeAuditor], later_tiers_run: bool
) -> None:
    """Red before: the finish ran tiers 1 and 2 (the suite, mutation: ten
    minutes on saddle) and only then reported a ruff finding that tier 0
    knows in seconds. A failing edit check now refuses at once and says the
    other tiers were not run; passing edit checks change nothing."""
    worktree = tmp_path / "worktree"
    git(repo, "worktree", "add", "-q", "-b", "run", str(worktree), "HEAD")
    (worktree / "calc.py").write_text(BUGGY.replace("a - b", "a + b"))
    fake = auditor()
    f = AuditFeed(
        worktree=worktree,
        baseline=git(worktree, "rev-parse", "HEAD"),
        journal=tmp_path / "j.jsonl",
        run_span="s",
        auditor=fake,
    )
    accepted, text = f.final()
    f.close()
    tiers = {tier for tier, _ in fake.calls}
    assert (1 in tiers and 2 in tiers) is later_tiers_run
    if not later_tiers_run:
        assert not accepted
        assert "ruff format --check exited 1" in text
        assert "tiers 1 and 2 were not run" in text


class MapsAuditor(FakeAuditor):
    """Records each `draw_map` call's tree, and answers with `said` (or raises it)."""

    def __init__(self, said: str | Exception) -> None:
        super().__init__()
        self.said = said
        self.drawn: list[set[str]] = []

    def draw_map(self, tree: Path | None = None) -> str:
        assert tree is not None
        self.drawn.append(_files_outside_git(tree))
        if isinstance(self.said, Exception):
            raise self.said
        return self.said


@pytest.mark.parametrize(
    ("said", "detail", "exit_code"),
    [
        ("map drawn over 2 files", "map drawn over 2 files", 0),
        (OSError("disk full"), "no map: OSError: disk full", 1),
    ],
    ids=["drawn", "failed"],
)
def test_the_feed_draws_the_map_at_the_start_and_journals_how(
    repo: Path, tmp_path: Path, said: str | Exception, detail: str, exit_code: int
) -> None:
    """The run's test-impact map is drawn over the worktree as it is when the
    feed starts, before any edit, so no audit pays a whole-suite run for it;
    a map that cannot be drawn is journaled as such and ends nothing."""
    worktree = tmp_path / "worktree"
    git(repo, "worktree", "add", "-q", "-b", "run", str(worktree), "HEAD")
    maps = MapsAuditor(said)
    f = AuditFeed(
        worktree=worktree,
        baseline=git(worktree, "rev-parse", "HEAD"),
        journal=tmp_path / "j.jsonl",
        run_span="s",
        auditor=maps,
    )
    f.close()
    assert maps.drawn == [{"calc.py", "tests/test_calc.py"}]
    (span,) = [s for s in read_spans(tmp_path / "j.jsonl") if s.name == "audit:impact-map"]
    assert (span.detail, span.exit_code) == (detail, exit_code)


@pytest.fixture
def landed(monkeypatch: pytest.MonkeyPatch) -> threading.Event:
    """Set once a checkpoint audit's result is ready for `collect`."""
    done = threading.Event()
    original = AuditFeed._checkpoint

    def spy(self: AuditFeed, point: str, scratch: Path) -> AuditResult:
        result = original(self, point, scratch)
        done.set()
        return result

    monkeypatch.setattr(AuditFeed, "_checkpoint", spy)
    return done


def test_the_nudge_carries_a_completed_audit(repo: Path, landed: threading.Event) -> None:
    latch = threading.Event()
    fake = FakeAuditor(latch)
    client = Reactive([[EDIT_COMMENT], [CHECK], []])

    def release() -> None:
        latch.set()
        assert landed.wait(10)

    client.hooks[3] = release  # the audit lands while the model replies with no call
    result, events = run(repo, client, "E+A+F", auditor=fake)
    assert result.outcome == "finished"
    nudges = [m for m in client.seen if m.startswith("No one is here to reply.")]
    assert any("[audit checkpoint 1 on tree" in m for m in nudges)
    assert any(isinstance(e, AuditNote) for e in events)


# -- the real auditor, end to end ----------------------------------------------


REAL_BASE = "def f():\n    return 1\n"
REAL_TEST = "from n import f\n\n\ndef test_f():\n    assert f() == 2\n"


@pytest.fixture
def real_repo(tmp_path: Path) -> Path:
    root = tmp_path / "real"
    root.mkdir()
    (root / "n.py").write_text(REAL_BASE)
    (root / "test_n.py").write_text(REAL_TEST)
    git(root, "init", "-q", "-b", "main")
    git(root, "add", "-A")
    git(root, "commit", "-q", "-m", "init")
    return root


def _real(repo: Path, new: str, **kw: Any) -> AutoResult:
    client = Reactive([[call("edit_file", "e", path="n.py", old="return 1", new=new)], [FINISH]])
    options = AutoOptions(task="make f return 2", repo=repo, run_id=new[-1], **kw)
    return run_auto(options, cast(VllmClient, client))


def test_the_real_auditor_accepts_a_correct_fix(real_repo: Path) -> None:
    result = _real(real_repo, "return 2")
    record = sidecar(result)
    assert result.outcome == "finished", record["audit"]
    assert record["audit"]["passed"] is True
    gates = [f["gate"] for f in record["audit"]["findings"]]
    # tier 0 on the one changed file, then tier 1, then tier 2
    assert gates[:5] == ["syntax", "ruff", "imports", "tests", "coverage"]
    assert "mutation" in gates


def test_the_real_auditor_refuses_a_wrong_fix(real_repo: Path) -> None:
    result = _real(real_repo, "return 3", token_budget=30)
    assert result.outcome == "stopped"
    record = sidecar(result)
    assert record["finish_refusals"] >= 1
    tests = next(f for f in record["audit"]["findings"] if f["gate"] == "tests")
    assert tests["verdict"] == "fail"


# -- sanctioned test rewrites (the task declares them; sealed; never blanket) --

REWROTE = Finding(
    "assertion-preservation",
    1,
    "fail",
    "evidence-thin",
    "refactor node rewrote assertions in: test_a, test_b",
    ("saddle.gates.check_assertion_preservation",),
)


def test_sanction_reclasses_only_a_finding_naming_only_sanctioned_tests() -> None:
    ok = sanction(REWROTE, ("test_a", "test_b", "test_c"))
    assert (ok.verdict, ok.reason) == ("fail", "sanctioned")
    assert ok.detail.endswith("(all sanctioned by the task)")
    assert Findings(1, "k", (ok,)).passed
    # known-bad: one unsanctioned name keeps the whole finding a failure
    bad = sanction(REWROTE, ("test_a",))
    assert bad == REWROTE
    assert not Findings(1, "k", (bad,)).passed
    assert sanction(REWROTE, ()) == REWROTE
    other = Finding("coverage", 1, "fail", "evidence-thin", "rewrote assertions in: test_a", ())
    assert sanction(other, ("test_a",)) == other
    passing = Finding("assertion-preservation", 1, "pass", "evidence-thin", "2 keep", ())
    assert sanction(passing, ("test_a",)) == passing
    odd = Finding("assertion-preservation", 1, "fail", "evidence-thin", "something else", ())
    assert sanction(odd, ("something else",)) == odd


def _rewrite_repo(tmp_path: Path) -> Path:
    root = tmp_path / "rw"
    root.mkdir()
    (root / "n.py").write_text("def f():\n    return 1\n")
    (root / "test_n.py").write_text("from n import f\n\n\ndef test_f():\n    assert f() == 1\n")
    git(root, "init", "-q", "-b", "main")
    git(root, "add", "-A")
    git(root, "commit", "-q", "-m", "init")
    (root / "n.py").write_text("def f():\n    return 2\n")
    (root / "test_n.py").write_text("from n import f\n\n\ndef test_f():\n    assert f() == 2\n")
    return root


@pytest.mark.parametrize(
    ("sanctioned", "verdict"),
    [(("test_f",), "fail"), (("test_other",), "question"), ((), "question")],
)
def test_the_real_auditor_honours_a_sanctioned_rewrite_and_asks_about_the_rest(
    tmp_path: Path, sanctioned: tuple[str, ...], verdict: str
) -> None:
    """A sanctioned rewrite passes as before. One nobody sanctioned, red on
    the baseline (`f() == 2` fails where f returns 1), is a question for a
    person: it refuses nothing, blocks nothing, and the run needs you."""
    from saddle.auditor import Auditor

    tree = _rewrite_repo(tmp_path)
    auditor = Auditor(tree, "HEAD", AuditorConfig(sanctioned_test_rewrites=sanctioned))
    first = auditor.tier1(tree)
    kept = next(f for f in first.findings if f.gate == "assertion-preservation")
    assert kept.verdict == verdict
    assert kept.detail.startswith("refactor node rewrote assertions in: test_f")
    assert first.passed
    assert (kept.reason == "sanctioned") is (verdict == "fail")
    assert first.needs_you is (verdict == "question")
    second = auditor.tier2(tree)
    assert [f for f in second.findings if f.verdict == "blocked"] == []


class RewritesTests(FakeAuditor):
    def tier1(self, tree: Path | None = None) -> Findings:
        self.calls.append((1, ""))
        return Findings(tier=1, key="k", findings=(REWROTE,))


def test_a_sanctioned_rewrite_is_information_not_a_refusal(repo: Path) -> None:
    client = Reactive([[EDIT_COMMENT], [CHECK], [FINISH]])
    result, events = run(
        repo,
        client,
        "E+A+F",
        auditor=RewritesTests(),
        sanctioned_test_rewrites=("test_a", "test_b"),
    )
    assert result.outcome == "finished"
    record = sidecar(result)
    assert record["audit"]["passed"] is True
    assert record["sanctioned_test_rewrites"] == ["test_a", "test_b"]
    note = next(e for e in events if isinstance(e, AuditNote)).text
    assert "PASS]" in note
    assert "(info) assertion-preservation (tier 1): refactor node rewrote" in note
    assert "- assertion-preservation" not in note
    start = next(s for s in read_spans(result.journal) if s.name == "auto:start")
    assert "sanctioned test rewrites test_a,test_b;" in start.detail


def test_an_unsanctioned_rewrite_still_refuses_finish(repo: Path) -> None:
    client = Reactive([[EDIT_COMMENT], [CHECK], [FINISH]])
    result, events = run(
        repo,
        client,
        "E+A+F",
        auditor=RewritesTests(),
        sanctioned_test_rewrites=("test_a",),
        token_budget=40,
    )
    assert result.outcome == "stopped"
    record = sidecar(result)
    assert record["audit"]["passed"] is False
    assert record["audit"]["findings"][1]["reason"] == "evidence-thin"
    note = next(e for e in events if isinstance(e, AuditNote)).text
    assert "- assertion-preservation (tier 1): fail, evidence-thin:" in note


# -- an accepted finish seals the waivers it stood on -----------------------


def test_a_sanctioned_accept_seals_the_names_it_stood_on(repo: Path) -> None:
    # known-bad for the field: the accept stood on a sanctioned rewrite, so
    # the sidecar names each test (EAF-t5-s2-0's four).
    client = Reactive([[EDIT_COMMENT], [CHECK], [FINISH]])
    result, _ = run(
        repo,
        client,
        "E+A+F",
        auditor=RewritesTests(),
        sanctioned_test_rewrites=("test_b", "test_a"),
    )
    assert result.outcome == "finished"
    assert sidecar(result)["waivers"] == [
        "sanctioned test rewrite: test_a",
        "sanctioned test rewrite: test_b",
    ]


@pytest.mark.parametrize("arm", ["E+A+F", "E+A"])
def test_an_accept_on_no_waiver_seals_an_empty_list(repo: Path, arm: str) -> None:
    # known-good: a plain accept (EAF-t8's reference shape) seals [].
    client = Reactive(
        [[call("edit_file", "e", path="calc.py", old="a - b", new="a + b")], [FINISH]]
    )
    result, _ = run(repo, client, arm)
    assert result.outcome == "finished"
    assert sidecar(result)["waivers"] == []


def test_a_run_with_no_accepted_finish_seals_no_waivers(repo: Path) -> None:
    refused = run(
        repo,
        Reactive([[EDIT_COMMENT], [CHECK], [FINISH]]),
        "E+A+F",
        auditor=RewritesTests(),
        sanctioned_test_rewrites=("test_a",),
        token_budget=40,
    )[0]
    assert refused.outcome == "stopped"
    assert "waivers" not in sidecar(refused)


def test_arm_e_has_no_audit_and_seals_no_waivers(repo: Path) -> None:
    plain = run(repo, Reactive([[EDIT_COMMENT], [FINISH]]), "E")[0]
    assert plain.outcome == "finished"
    assert "waivers" not in sidecar(plain)


def test_waivers_name_sanctioned_tests_once_and_the_nothing_to_audit_note() -> None:
    from saddle.feed import AuditResult, waivers

    twice = sanction(REWROTE, ("test_a", "test_b"))
    passed = Finding("tests", 1, "pass", "code-wrong", "1 passed", ("fake",))
    result = AuditResult("finish", "t" * 40, (twice, passed, twice))
    assert waivers(result) == ["sanctioned test rewrite: test_a", "sanctioned test rewrite: test_b"]
    # a failing rewrite the task did not sanction is not a waiver
    assert waivers(AuditResult("finish", "t" * 40, (REWROTE, passed))) == []
    # the names are read before any suffix: sanction's, or the baseline check's
    from saddle.auditor import GREEN_ON_BASELINE, rewritten

    assert rewritten(twice.detail) == {"test_a", "test_b"}
    assert rewritten(f"{REWROTE.detail}{GREEN_ON_BASELINE}test_b") == {"test_a", "test_b"}
    note = "nothing to audit: the tree equals baseline abc"
    assert waivers(AuditResult("finish", "t" * 40, (), note)) == [note]


def test_the_feed_has_no_waivers_before_any_audit(unit: tuple[AuditFeed, FakeAuditor]) -> None:
    feed, _ = unit
    assert feed.waivers() == []


def test_feedback_and_the_finish_gate_are_the_same_with_test_edits_allowed(repo: Path) -> None:
    edit_test = call(
        "edit_file", "t1", path="tests/test_calc.py", old="add(2, 2) == 4", new="add(2, 3) == 5"
    )
    client = Reactive([[edit_test], [CHECK], [FINISH]])
    fake = FakeAuditor()
    result, _ = run(repo, client, "E+A+F", auditor=fake, allow_test_edits=True)
    assert result.outcome == "finished"
    assert client.reacted >= 1  # the test edit made a burst; its checkpoint was delivered
    assert [c[0] for c in fake.calls][:1] == [1]
    record = sidecar(result)
    assert record["allow_test_edits"] is True
    assert record["audit"]["passed"] is True
    stubborn = Reactive([[edit_test], [CHECK]], tail=[FINISH])
    stubborn.stream_chat = lambda messages, **kw: iter(  # type: ignore[method-assign]
        stubborn.script.pop(0) if stubborn.script else [FINISH]
    )
    other, _ = run(
        repo,
        stubborn,
        "E+A+F",
        auditor=FakeAuditor(),
        allow_test_edits=True,
        run_id="s",
        token_budget=40,
    )
    assert other.outcome == "stopped"
    assert sidecar(other)["finish_refusals"] >= 1


def test_the_sampling_is_sealed_in_the_outcome(repo: Path) -> None:
    result, _ = run(repo, Reactive([[FINISH]]), "E", temperature=0.8, reasoning_effort="xhigh")
    record = sidecar(result)
    assert (record["temperature"], record["reasoning_effort"]) == (0.8, "xhigh")
    start = next(s for s in read_spans(result.journal) if s.name == "auto:start")
    assert start.detail.startswith("arm E; temperature 0.8; effort xhigh;")


def test_the_cli_passes_sanctioned_rewrites(repo: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    got: list[tuple[str, ...]] = []

    def spy(options: AutoOptions, client: Any, **kw: Any) -> AutoResult:
        got.append(options.sanctioned_test_rewrites)
        return cast(AutoResult, None)

    monkeypatch.setattr(cli, "run_auto", spy)
    argv = [
        "auto",
        "t",
        "--repo",
        str(repo),
        "--sanctioned-test-rewrite",
        "test_a",
        "--sanctioned-test-rewrite",
        "test_b",
    ]
    with pytest.raises(AttributeError):
        cli.run_auto_command(
            cli.build_parser().parse_args(argv),
            cast(VllmClient, None),
            stdout=None,  # type: ignore[arg-type]  # deliberately not an IO: the call must fail
        )
    assert got == [("test_a", "test_b")]
    assert cli.build_parser().parse_args(["auto", "t"]).sanctioned_test_rewrite == []


# -- the finish refusal cap (a run once made 7,244 refusals on a correct T5 tree) --


class KeepsFinishing(Reactive):
    """A model that ignores every refusal and calls finish again."""

    def stream_chat(self, messages: Any, **kwargs: Any) -> Any:
        self.seen.append(str(messages[-1].get("content") or ""))
        if self.script:
            return iter(self.script.pop(0))
        return iter([FINISH])


class Shifting(FakeAuditor):
    """Tier 1 passes; tier 2 fails, citing `sets[n]` on the n-th finish audit."""

    def __init__(self, sets: list[str]) -> None:
        super().__init__()
        self.sets = sets
        self.finals = 0

    def tier1(self, tree: Path | None = None) -> Findings:
        return Findings(tier=1, key="k1", findings=())

    def tier2(self, tree: Path | None = None) -> Findings:
        cite = self.sets[min(self.finals, len(self.sets) - 1)]
        self.finals += 1
        # the detail drifts on every call; only (gate, reason, cites) is the identity
        found = Finding("mutation", 2, "fail", "evidence-thin", f"{self.finals} survived", (cite,))
        return Findings(tier=2, key=f"k2-{self.finals}", findings=(found,))


def test_an_unchanged_refusal_stops_the_run_at_the_cap_as_audit_unresolved(repo: Path) -> None:
    client = KeepsFinishing([[EDIT_COMMENT], [FINISH]])
    result, _ = run(repo, client, "E+A+F", token_budget=5000)
    assert (result.outcome, result.reason) == ("stopped", "audit unresolved")
    record = sidecar(result)
    assert record["outcome"] == "stopped"
    assert record["reason"] == "audit unresolved"
    assert record["finish_refusals"] == 3
    assert record["unchanged_refusals"] == 3
    assert record["finish_refusal_cap"] == 3
    assert record["unresolved_findings"] == [
        {"gate": "mutation", "reason": "code-wrong", "cites": ["fake"]},
        {"gate": "tests", "reason": "code-wrong", "cites": ["fake"]},
    ]
    assert record["audit"]["passed"] is False
    span = [s for s in read_spans(result.journal) if s.name.startswith("auto:")][-1]
    assert span.argv == ["auto:stopped"]
    assert span.exit_code == 3
    assert "unresolved findings: mutation (code-wrong), tests (code-wrong)" in span.detail
    finishes = [s for s in read_spans(result.journal) if s.argv[:1] == ["finish"]]
    assert len(finishes) == 3
    assert "the run stops (audit unresolved)" in finishes[-1].detail
    assert "calc.py:3: return a - b" in finishes[-1].detail
    assert "Fix what it names" in finishes[1].detail
    assert "saddle auto r1: stopped (audit unresolved)" in git(
        result.worktree, "log", "-1", "--format=%B"
    )


def test_a_tier0_only_refusal_counts_toward_the_cap(repo: Path) -> None:
    """A lint-only refusal is a refusal like any other. Tiers 1 and
    2 pass (the fix is right), tier 0 fails on every finish, and the run stops
    at the cap naming the ruff finding as unresolved."""

    class Unformatted(FakeAuditor):
        def tier0(self, path: str, new_text: str) -> Findings:
            self.calls.append((0, path))
            found = Finding(
                "ruff", 0, "fail", "code-wrong", f"{path}: ruff format --check exited 1", ("fake",)
            )
            return Findings(tier=0, key="k0", findings=(found,))

    client = KeepsFinishing(
        [[call("edit_file", "e", path="calc.py", old="a - b", new="a + b")], [FINISH]]
    )
    result, _ = run(repo, client, "E+A+F", auditor=Unformatted(), token_budget=5000)
    assert (result.outcome, result.reason) == ("stopped", "audit unresolved")
    record = sidecar(result)
    assert record["finish_refusals"] == record["unchanged_refusals"] == 3
    assert record["unresolved_findings"] == [
        {"gate": "ruff", "reason": "code-wrong", "cites": ["fake"]},
    ]
    finishes = [s for s in read_spans(result.journal) if s.argv[:1] == ["finish"]]
    assert len(finishes) == 3  # the third refusal stops the run; the model sees two
    refusals = [t for t in client.seen if t.startswith(FINISH_REFUSED)]
    assert len(refusals) == 2
    assert "- ruff (tier 0): fail, code-wrong: calc.py: ruff format --check exited 1" in refusals[0]
    # tiers 1 and 2 are not run while an edit check fails, and the refusal says so
    assert "tiers 1 and 2 were not run" in refusals[0]


def test_the_cap_is_configurable_and_one_stops_on_the_first_refusal(repo: Path) -> None:
    for cap in (1, 2):
        client = KeepsFinishing([[EDIT_COMMENT], [FINISH]])
        result, _ = run(
            repo, client, "E+A+F", token_budget=5000, finish_refusal_cap=cap, run_id=f"c{cap}"
        )
        assert result.reason == "audit unresolved"
        assert sidecar(result)["finish_refusals"] == cap


def test_a_changed_finding_set_restarts_the_count(repo: Path) -> None:
    fake = Shifting(["a.py", "a.py", "b.py", "a.py", "a.py"])
    client = KeepsFinishing([[EDIT_COMMENT], [FINISH]])
    result, _ = run(repo, client, "E+A+F", auditor=fake, token_budget=5000)
    assert result.reason == "audit unresolved"
    record = sidecar(result)
    # a a | b | a a a: the third unchanged refusal in a row is the sixth refusal
    assert record["finish_refusals"] == 6
    assert record["unchanged_refusals"] == 3
    assert record["unresolved_findings"] == [
        {"gate": "mutation", "reason": "evidence-thin", "cites": ["a.py"]}
    ]


def test_a_drifting_detail_is_not_a_changed_finding_set(repo: Path) -> None:
    result, _ = run(
        repo,
        KeepsFinishing([[EDIT_COMMENT]]),
        "E+A+F",
        auditor=Shifting(["a.py"]),
        token_budget=5000,
    )
    assert sidecar(result)["finish_refusals"] == 3


def test_without_feedback_the_cap_never_fires(repo: Path) -> None:
    result, _ = run(repo, KeepsFinishing([[EDIT_COMMENT], [FINISH]]), "E+A")
    assert result.outcome == "finished"
    record = sidecar(result)
    assert (record["finish_refusals"], record["unresolved_findings"]) == (0, [])


def test_a_cap_below_one_is_refused_before_anything_runs(repo: Path) -> None:
    with pytest.raises(AutoError, match="at least 1"):
        run(repo, KeepsFinishing([]), "E+A+F", finish_refusal_cap=0)
    assert git(repo, "worktree", "list").count("\n") == 0


def test_unresolved_is_empty_before_any_audit(repo: Path) -> None:
    fed = AuditFeed(repo, "HEAD", repo / "j.jsonl", "s", factory=lambda *a: FakeAuditor())
    assert fed.unresolved() == []


def test_the_cli_passes_the_finish_refusal_cap(repo: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    got: list[int] = []

    def spy(options: AutoOptions, client: Any, **kw: Any) -> AutoResult:
        got.append(options.finish_refusal_cap)
        return cast(AutoResult, None)

    monkeypatch.setattr(cli, "run_auto", spy)
    parser = cli.build_parser()
    for argv in (["auto", "t", "--repo", str(repo), "--finish-refusal-cap", "5"], ["auto", "t"]):
        with pytest.raises(AttributeError):
            cli.run_auto_command(parser.parse_args(argv), cast(VllmClient, None), stdout=None)  # type: ignore[arg-type]
    assert got == [5, 3]


@pytest.mark.parametrize(
    ("sanctioned", "assertion", "listed"),
    [(("test_f",), "== 2", False), ((), "== 2", False), ((), "in (1, 2)", True)],
)
def test_a_blocked_tier_2_names_only_the_unsanctioned_tier_1_failures(
    tmp_path: Path, sanctioned: tuple[str, ...], assertion: str, listed: bool
) -> None:
    """The blocked detail listed a
    sanctioned assertion-preservation finding as a cause. An uncovered new
    function keeps tier 1 failing either way; the rewrite is named only when
    it fails: not sanctioned and green on the baseline (`in (1, 2)` passes
    where f returns 1). One red there and not sanctioned is a question."""
    from saddle.auditor import Auditor

    tree = _rewrite_repo(tmp_path)
    (tree / "n.py").write_text("def f():\n    return 2\n\n\ndef g():\n    return 3\n")
    (tree / "test_n.py").write_text(
        f"from n import f\n\n\ndef test_f():\n    assert f() {assertion}\n"
    )
    auditor = Auditor(tree, "HEAD", AuditorConfig(sanctioned_test_rewrites=sanctioned))
    first = auditor.tier1(tree)
    assert not first.passed
    unsanctioned = [
        f.gate for f in first.findings if f.verdict == "fail" and not f.reason == "sanctioned"
    ]
    assert "coverage" in unsanctioned
    (blocked,) = auditor.tier2(tree).findings
    assert blocked.verdict == "blocked"
    assert blocked.detail == f"tier 1 failed ({', '.join(unsanctioned)}); tier 2 not run"
    assert ("assertion-preservation" in blocked.detail) is listed


# -- mutant_detail: every scored mutant's show text, sealed in the audit span --


class DetailAuditor(FakeAuditor):
    """Tier 2 carries one killed and one surviving mutant's detail."""

    DETAIL = (("m1", "killed", "-    return a - b\n+    return a + b"), ("m2", "survived", "x"))

    def tier2(self, tree: Path | None = None) -> Findings:
        found = self._findings(2, tree)
        return Findings(tier=2, key="k2", findings=found.findings, mutant_detail=self.DETAIL)


def test_the_finish_audit_record_carries_every_scored_mutants_detail(repo: Path) -> None:
    """`feed.last()` is what the engine seals as the outcome sidecar's `audit`."""
    (repo / "calc.py").write_text("def add(a, b):\n    return a + b\n")
    fed = AuditFeed(repo, "HEAD", repo / "j.jsonl", "s", factory=lambda *a: DetailAuditor())
    fed.final()
    record = fed.last()
    assert record is not None
    assert record["mutant_detail"] == [
        {"name": "m1", "status": "killed", "show": "-    return a - b\n+    return a + b"},
        {"name": "m2", "status": "survived", "show": "x"},
    ]


def test_an_audit_with_no_mutants_carries_no_mutant_detail_key() -> None:
    assert "mutant_detail" not in AuditResult("finish", "t", ()).to_dict()


# -- an accepted finish delivers its not-proven findings once -------------------

ROWS = tuple(
    f"- calc.py:{n}: `return a + b`: mutant calc.x_add__mutmut_{n} (survived)" for n in (1, 2, 3)
)


def finish_results(result: AutoResult) -> list[str]:
    return [s.detail for s in read_spans(result.journal) if s.argv and s.argv[0] == "finish"]


class Surfaces(FakeAuditor):
    """Passes every gate; tier 2's mutation finding is not-proven with 3 rows."""

    def tier2(self, tree: Path | None = None) -> Findings:
        self.calls.append((2, "surface"))
        detail = "3 surviving mutant(s) on changed lines:\n" + "\n".join(ROWS)
        found = Finding("mutation", 2, "not-proven", "evidence-thin", detail, ("fake",))
        return Findings(tier=2, key="k2", findings=(found,))


def test_an_accepted_finish_shows_the_model_its_three_surviving_mutants(repo: Path) -> None:
    """Known-good: the finish is accepted, and the model's next request
    carries all three rows; nothing is counted as a refusal."""
    fix = call("edit_file", "e1", path="calc.py", old="a - b", new="a + b")
    client = Reactive([[fix], [FINISH]])
    result, _ = run(repo, client, auditor=Surfaces())
    assert result.outcome == "finished"
    assert result.reason == "finish called"
    assert client.round == 3
    read = [m["content"] for m in client.messages if m.get("role") == "tool"]
    assert read[-1].startswith(engine.FINISH_SURFACED)
    assert all(row in read[-1] for row in ROWS)
    assert finish_results(result)[-1].startswith("finished.")
    record = sidecar(result)
    assert record["finish_refusals"] == 0
    assert record["finish_surfaced"] is True
    assert record["audit"]["passed"] is True
    spans = [s for s in read_spans(result.journal) if s.name.startswith("audit:")]
    finish = [s for s in spans if s.argv[:2] == ["audit", "finish"]]
    assert [s.name for s in finish] == ["audit:delivered", "audit:withheld"]
    assert all(row in finish[0].detail for row in ROWS)
    assert verify_journal(result.journal) == []


def test_a_finish_with_nothing_unproven_ends_at_once_and_seals_no_surfacing(repo: Path) -> None:
    """Known-bad for the delivery: an all-pass finish is not held open."""
    fix = call("edit_file", "e1", path="calc.py", old="a - b", new="a + b")
    client = Reactive([[fix], [FINISH]])
    result, _ = run(repo, client)
    assert result.outcome == "finished"
    assert client.round == 2
    assert "finish_surfaced" not in sidecar(result)


def test_a_refused_finish_is_unchanged_by_surfacing(repo: Path) -> None:
    """Refusal path: a failing finish audit still refuses, not-proven or not."""

    class FailsAndSurfaces(Surfaces):
        def tier1(self, tree: Path | None = None) -> Findings:
            return self._findings(1, tree)

    client = Reactive([[FINISH]], tail=[])
    result, _ = run(repo, client, auditor=FailsAndSurfaces(), finish_refusal_cap=1)
    assert result.outcome == "stopped"
    assert finish_results(result)[0].startswith(FINISH_REFUSED)
    assert "finish_surfaced" not in sidecar(result)


class StopsAfter(Reactive):
    """Replays `script`, then the model errors (the run stops, no finish)."""

    def stream_chat(self, messages: Any, **kwargs: Any) -> Any:
        if not self.script:
            self.messages = list(messages)
            msg = "gone"
            raise VllmError(msg)
        return super().stream_chat(messages, **kwargs)


def test_a_run_that_stops_on_the_accepted_tree_still_ends_finished(repo: Path) -> None:
    fix = call("edit_file", "e1", path="calc.py", old="a - b", new="a + b")
    client = StopsAfter([[fix], [FINISH]])
    result, _ = run(repo, client, auditor=Surfaces())
    assert result.outcome == "finished"
    assert result.reason.startswith("finish accepted with not-proven findings")
    assert "model error" in result.reason
    assert sidecar(result)["narrative"] == "done"


def test_a_run_that_stops_after_changing_the_accepted_tree_ends_stopped(repo: Path) -> None:
    fix = call("edit_file", "e1", path="calc.py", old="a - b", new="a + b")
    more = call("edit_file", "e2", path="calc.py", old="def add", new="# more\ndef add")
    client = StopsAfter([[fix], [FINISH], [more]])
    result, _ = run(repo, client, auditor=Surfaces())
    assert result.outcome == "stopped"
    assert "model error" in result.reason


def test_accepted_unchanged_is_false_before_any_surfacing_and_when_no_snapshot_can_be_taken(
    unit: tuple[AuditFeed, FakeAuditor], monkeypatch: pytest.MonkeyPatch
) -> None:
    fed, _ = unit
    assert fed.accepted_unchanged() is False
    fed.surfaced_tree = "t" * 40

    def broken(*args: object) -> str:
        from saddle.audit import AuditError

        msg = "git clone failed"
        raise AuditError(msg)

    monkeypatch.setattr(feed, "snapshot", broken)
    assert fed.accepted_unchanged() is False


# -- every audit span seals its whole result -------------------------------------


class Killing(DetailAuditor):
    """Tier 2's second audit has killed the mutant the first left alive."""

    def __init__(self) -> None:
        super().__init__()
        self.audits = 0

    def tier2(self, tree: Path | None = None) -> Findings:
        self.audits += 1
        status = "survived" if self.audits == 1 else "killed"
        found = self._findings(2, tree)
        detail = (("m1", "killed", "a"), ("m2", status, "b"))
        return Findings(
            tier=2, key=f"k{self.audits}", findings=found.findings, mutant_detail=detail
        )


def test_each_audit_span_seals_its_own_mutant_rows(repo: Path, tmp_path: Path) -> None:
    """Known-good: two finish audits, a survivor killed between them, both
    readable from the ledger; known-bad: the rows are not the same record."""
    (repo / "calc.py").write_text("def add(a, b):\n    return a + b\n")
    journal = tmp_path / "j.jsonl"
    fed = AuditFeed(repo, "HEAD", journal, "s", factory=lambda *a: Killing())
    fed.final()
    (repo / "calc.py").write_text("def add(a, b):\n    return b + a\n")
    fed.final()
    spans = [s for s in read_spans(journal) if s.name.startswith("audit:")]
    assert len(spans) == 2
    rows = [
        json.loads(attempt_sidecar_path(journal, s.span_id).read_text())["mutant_detail"]
        for s in spans
    ]
    assert [r["status"] for r in rows[0]] == ["killed", "survived"]
    assert [r["status"] for r in rows[1]] == ["killed", "killed"]
    # The run span "s" is not in this bare journal; every sidecar still hashes.
    assert {i.code for i in verify_journal(journal)} == {"orphan-span"}


def transcript_verdict(journal: Path) -> tuple[str, str]:
    from saddle.transcript import _auto_verdict

    spans = read_spans(journal)
    start = next(s for s in spans if s.name == "auto:start")
    return _auto_verdict(start, spans)
