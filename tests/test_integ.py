"""Phase 2 integration: the real auditor's findings reach the chat's card and packet.

FEED's `AuditFeed` (arm E+A+F, the real `auditor.Auditor`) is the source of
findings; UI's session lines and packet read them from the ledger alone;
The verifier accepts the journal the two write together. The mutation count
reaches the packet only through the sealed `audit-tier2:mutation`
span, which is the contract under test. Tier 2 runs the real mutmut
engine (`test_evidence._without_stubbed_mutmut`): the conftest stub mutates
a line these trees do not change and would report no mutants.
"""

from __future__ import annotations

import json
import shutil
import subprocess
import time
from pathlib import Path
from typing import Any, cast

import pytest
from test_evidence import _without_stubbed_mutmut

from saddle.auto import AutoOptions, AutoResult, run_auto
from saddle.journal import attempt_sidecar_path, read_entries, read_spans, verify_journal
from saddle.packet import _spend, _unresolved, compile_packet
from saddle.transcript import session_line, start_field, tier_finding
from saddle.vllm import StreamUsage, ToolCall, VllmClient

BASE = "def f(x):\n    return x\n"
TEST = "from n import f\n\n\ndef test_f():\n    assert f(2) == 2\n"
NEG_TEST = "\n\ndef test_none():\n    assert f(None) == 0\n"
CLAMP = "    if x is None:\n        return 0\n    return x"


def git(repo: Path, *args: str) -> None:
    subprocess.run(
        ["git", "-C", str(repo), "-c", "user.name=t", "-c", "user.email=t@t", *args],
        check=True,
        capture_output=True,
    )


@pytest.fixture
def repo(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
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


def call(name: str, call_id: str, **arguments: Any) -> ToolCall:
    return ToolCall(id=call_id, name=name, arguments=json.dumps(arguments))


class Scripted:
    """Map None to 0 (leaving `return 0` uncovered), check, wait for the
    checkpoint's finding, then -- if `learns` -- add the covering test."""

    def __init__(self, journal: Path, *, learns: bool, usage: int | None = None) -> None:
        self.journal = journal
        self.usage = usage
        self.learns = learns
        self.round = 0
        self.fixed = False
        self.heard: list[str] = []

    def _audited(self) -> bool:
        return self.journal.is_file() and "audit-tier1:coverage" in self.journal.read_text()

    def stream_chat(self, messages: Any, **kwargs: Any) -> Any:
        items = list(self._round(messages))
        if self.usage is not None:  # a server that reports usage, as vLLM does
            items.append(StreamUsage(prompt_tokens=100, completion_tokens=self.usage))
        return iter(items)

    def _round(self, messages: Any) -> Any:
        self.round += 1
        content = str(messages[-1].get("content") or "")
        self.heard.append(content)
        if self.round == 1:
            return iter([call("edit_file", "e1", path="n.py", old="    return x", new=CLAMP)])
        if self.round == 2:
            return iter([call("run_command", "r1", command="python -m pytest -q")])
        if self.round == 3:
            deadline = time.monotonic() + 60
            while not self._audited():  # the checkpoint audit lands off the critical path
                assert time.monotonic() < deadline, "the checkpoint audit never landed"
                time.sleep(0.05)
            return iter([call("read_file", "r2", path="n.py")])
        if self.learns and not self.fixed and "coverage" in content and "FAIL" in content:
            self.fixed = True
            return iter(
                [call("edit_file", "e2", path="test_n.py", old="== 2\n", new="== 2\n" + NEG_TEST)]
            )
        if self.fixed and self.round < 20 and "run_command" not in str(messages[-2:]):
            return iter([call("run_command", "r3", command="python -m pytest -q")])
        return iter([call("finish", f"f{self.round}", summary="Clamped. All tests pass now.")])


def run(
    repo: Path, *, learns: bool, usage: int | None = None, **kw: Any
) -> tuple[AutoResult, Scripted]:
    run_id = "learns" if learns else "stubborn"
    journal = repo / ".saddle" / "runs" / run_id / "proofs.jsonl"
    client = Scripted(journal, learns=learns, usage=usage)
    options = AutoOptions(
        task="f(None) should be 0", repo=repo, run_id=run_id, allow_test_edits=True, **kw
    )
    return run_auto(options, cast(VllmClient, client)), client


def test_a_real_finding_reaches_the_model_the_card_and_the_packet(repo: Path) -> None:
    result, client = run(repo, learns=True)
    assert result.outcome == "finished", result.reason
    assert verify_journal(result.journal) == []  # verify accepts the feed's and auditor's spans
    spans = read_spans(result.journal)
    # the checkpoint's coverage finding, delivered to the model as a tool result
    delivered = [s for s in spans if s.name == "audit:delivered"]
    assert delivered
    assert delivered[0].exit_code == 1
    assert "coverage" in delivered[0].detail
    assert any("[audit checkpoint 1" in h and "coverage" in h for h in client.heard)
    # ... and on the card as live session lines citing their records
    lines = [line for e in read_entries(result.journal) if (line := session_line(e))]
    texts = [line.text for line in lines]
    assert any(t.startswith("audit tier 1 coverage fail") for t in texts), texts
    assert any(t.startswith("audit checkpoint 1 failed, delivered to the model") for t in texts)
    assert any(t.startswith("started on saddle/auto/learns · arm E+A+F") for t in texts), texts
    # the packet: the latest verdict per gate, cited, and tier 2 from its own span
    packet = compile_packet(result.journal)
    rows = {row.key: row for row in packet.rows}
    assert packet.verdict == "finished"
    assert packet.verdict_text.endswith("every audit finding recorded passed.")
    mutation = [s for s in spans if s.name == "audit-tier2:mutation"][-1]
    finding = tier_finding(mutation.name, mutation.detail)
    assert finding is not None
    assert rows["mutation"].status == "proven"
    assert rows["mutation"].text == f"tier 2, {finding.verdict}: {finding.detail}"
    assert rows["mutation"].cites == (mutation.record_hash,)
    assert rows["tests"].status == "proven"
    assert rows["audit"].status == "proven"
    assert any(i.startswith("✓ coverage: tier 1, pass") for i in rows["audit"].items)
    assert "saddle verify .saddle/runs/learns/proofs.jsonl" in rows["reproduce"].items
    assert "git log -p main..saddle/auto/learns" in rows["reproduce"].items


def test_a_refused_finish_reads_failed_on_the_packet(repo: Path) -> None:
    result, _ = run(repo, learns=False, token_budget=60)
    assert result.outcome == "stopped"
    assert verify_journal(result.journal) == []
    packet = compile_packet(result.journal)
    rows = {row.key: row for row in packet.rows}
    assert packet.verdict == "stopped"
    assert rows["audit"].status == "failed"
    assert any(i.startswith("✗ coverage: tier 1, fail") for i in rows["audit"].items)
    # flip: blocked by tier 1 is not a failed mutation result, which
    # would not exist; it is still recorded and cited, never absent.
    assert rows["mutation"].status == "not-proven"
    assert rows["mutation"].text.startswith("blocked: tier 1 failed (")
    assert rows["mutation"].cites


def test_only_the_latest_finding_per_gate_is_a_verdict(repo: Path) -> None:
    """Known-bad half: an earlier failure a later audit cleared is history."""
    result, _ = run(repo, learns=True)
    coverage = [
        f.verdict
        for s in read_spans(result.journal)
        if (f := tier_finding(s.name, s.detail)) is not None and f.gate == "coverage"
    ]
    assert coverage[0] == "fail"
    assert coverage[-1] == "pass"
    assert not [r for r in compile_packet(result.journal).rows if r.status == "failed"]


@pytest.mark.parametrize(
    ("detail", "verdict", "text"),
    [
        (json.dumps({"verdict": "fail", "detail": "1 of 2 lines"}), "fail", "1 of 2 lines"),
        ("not json", "unreadable", "not json"),
        ("[]", "unreadable", "[]"),
    ],
)
def test_a_tier_span_is_read_from_its_sealed_detail(detail: str, verdict: str, text: str) -> None:
    finding = tier_finding("audit-tier2:mutation", detail)
    assert finding is not None
    assert (finding.gate, finding.tier, finding.verdict, finding.detail) == (
        "mutation",
        2,
        verdict,
        text,
    )
    assert tier_finding("audit:mutation", detail) is None


def test_start_fields_are_read_by_key_not_position() -> None:
    feed = "arm E+A+F; temperature 0.0; branch saddle/auto/x; budgets 1s"
    assert start_field(feed, "branch") == "saddle/auto/x"
    assert start_field("branch saddle/auto/y; budgets 1s", "branch") == "saddle/auto/y"
    assert start_field(feed, "missing") == ""


# -- cost: measured vs estimated (the measured-usage sidecar fields) ----------


def test_the_cost_row_says_measured_when_the_server_reported_usage(repo: Path) -> None:
    result, _ = run(repo, learns=True, usage=7)
    rows = {row.key: row for row in compile_packet(result.journal).rows}
    spent = sidecar(result)
    assert spent["token_source"] == "usage"
    assert f"{spent['tokens_spent']} measured of " in rows["cost"].text
    assert "~" not in rows["cost"].text
    assert not any("estimate" in item for item in rows["not-proven"].items)


def test_the_cost_row_says_estimate_when_no_usage_came(repo: Path) -> None:
    result, _ = run(repo, learns=True)
    rows = {row.key: row for row in compile_packet(result.journal).rows}
    spent = sidecar(result)
    assert spent["token_source"] == "estimate"
    assert f"~{spent['tokens_spent']} estimated of " in rows["cost"].text
    assert any("estimate" in item for item in rows["not-proven"].items)


def test_an_old_sidecar_is_never_read_as_zero_measured_tokens(tmp_path: Path) -> None:
    """Known-bad: a sidecar written before usage was measured has only `tokens_spent_estimate`."""
    runs = tmp_path / "old"
    shutil.copytree(Path(__file__).parent / "fixtures" / "auto_pre_chain", runs)
    rows = {row.key: row for row in compile_packet(runs / "proofs.jsonl").rows}
    assert "measured" not in rows["cost"].text
    assert "~33 estimated of " in rows["cost"].text
    assert any("estimate" in item for item in rows["not-proven"].items)


# -- the finish-refusal cap: "audit unresolved" ------------------------------------


def test_an_unresolved_audit_stop_names_its_findings_on_the_card_and_packet(
    repo: Path,
) -> None:
    result, _ = run(repo, learns=False)  # default cap: 3 unchanged refusals
    assert (result.outcome, result.reason) == ("stopped", "audit unresolved")
    packet = compile_packet(result.journal)
    assert packet.verdict == "stopped"
    assert "audit unresolved" in packet.verdict_text
    assert "coverage (evidence-thin)" in packet.verdict_text
    gaps = {row.key: row for row in packet.rows}["not-proven"].items
    assert any(g.startswith("Unresolved at finish: coverage (evidence-thin)") for g in gaps), gaps
    texts = [line.text for e in read_entries(result.journal) if (line := session_line(e))]
    assert any("unresolved findings: coverage (evidence-thin)" in t for t in texts), texts


def sidecar(result: AutoResult) -> dict[str, Any]:
    span = [s for s in read_spans(result.journal) if s.name == f"auto:{result.outcome}"][-1]
    return cast(
        dict[str, Any], json.loads(attempt_sidecar_path(result.journal, span.span_id).read_bytes())
    )


@pytest.mark.parametrize(
    ("evidence", "text", "estimated"),
    [
        ({"tokens_spent": 1500, "token_source": "usage"}, "1.5k measured", False),
        ({"tokens_spent": 40, "token_source": "estimate"}, "~40 estimated", True),
        (
            {"tokens_spent": 50, "token_source": "mixed", "tokens_by_source": {"usage": 30}},
            "50 (30 measured, rest estimated)",
            True,
        ),
        (
            {"tokens_spent": 50, "token_source": "mixed", "tokens_by_source": None},
            "50 (0 measured, rest estimated)",
            True,
        ),
        ({"tokens_spent": 0, "token_source": "none"}, "0 (no model round)", False),
        ({"tokens_spent_estimate": 33}, "~33 estimated", True),
        # known-bad: a source this reader does not know is not trusted as measured
        (
            {"tokens_spent": 9, "token_source": "guess", "tokens_spent_estimate": 9},
            "~9 estimated",
            True,
        ),
    ],
)
def test_spend_says_measured_only_for_usage(
    evidence: dict[str, Any], text: str, estimated: bool
) -> None:
    spend = _spend(evidence)
    assert spend is not None
    assert (spend.text, spend.estimated) == (text, estimated)
    assert bool(spend.gap) == estimated


def test_no_spend_record_is_not_a_zero() -> None:
    assert _spend({}) is None
    assert _spend({"tokens_spent": "12", "token_source": "usage"}) is None


def test_malformed_unresolved_entries_are_skipped() -> None:
    assert _unresolved({"unresolved_findings": [{"gate": "coverage"}, "junk"]}) == ["coverage (?)"]
    assert _unresolved({"unresolved_findings": "junk"}) == []


@pytest.mark.parametrize("allowed", [True, False])
def test_the_confirm_strip_is_told_the_servers_test_edit_policy(
    tmp_path: Path, allowed: bool
) -> None:
    from starlette.testclient import TestClient

    from saddle.sessions import SessionStore
    from saddle.web.app import build_app

    app = build_app(
        SessionStore(tmp_path), object, default_workdir=tmp_path, allow_test_edits=allowed
    )
    with TestClient(app) as client:
        assert client.get("/api/task-policy").json() == {
            "test_edits": allowed,
            "task_text_check": False,
        }
