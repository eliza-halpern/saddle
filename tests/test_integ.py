"""Phase 2 integration: the real auditor's findings reach the chat's card and packet.

FEED's `AuditFeed` (arm E+A+F, the real `auditor.Auditor`) is the source of
findings; UI's session lines and packet read them from the ledger alone;
CHAIN's verifier accepts the journal the two write together. The mutation count
reaches the packet only through the sealed `audit-tier2:mutation`
span, which is the contract under test. Tier 2 runs the real mutmut
engine (`test_evidence._without_stubbed_mutmut`): the conftest stub mutates
a line these trees do not change and would report no mutants.
"""

from __future__ import annotations

import json
import subprocess
import time
from pathlib import Path
from typing import Any, cast

import pytest
from test_evidence import _without_stubbed_mutmut

from saddle.auto import AutoOptions, AutoResult, run_auto
from saddle.journal import read_entries, read_spans, verify_journal
from saddle.packet import compile_packet
from saddle.transcript import session_line, start_field, tier_finding
from saddle.vllm import ToolCall, VllmClient

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

    def __init__(self, journal: Path, *, learns: bool) -> None:
        self.journal = journal
        self.learns = learns
        self.round = 0
        self.fixed = False
        self.heard: list[str] = []

    def _audited(self) -> bool:
        return self.journal.is_file() and "audit-tier1:coverage" in self.journal.read_text()

    def stream_chat(self, messages: Any, **_: Any) -> Any:
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


def run(repo: Path, *, learns: bool, **kw: Any) -> tuple[AutoResult, Scripted]:
    run_id = "learns" if learns else "stubborn"
    client = Scripted(repo / ".saddle" / "runs" / run_id / "proofs.jsonl", learns=learns)
    options = AutoOptions(
        task="f(None) should be 0", repo=repo, run_id=run_id, allow_test_edits=True, **kw
    )
    return run_auto(options, cast(VllmClient, client)), client


def test_a_real_finding_reaches_the_model_the_card_and_the_packet(repo: Path) -> None:
    result, client = run(repo, learns=True)
    assert result.outcome == "finished", result.reason
    assert verify_journal(result.journal) == []  # CHAIN accepts the feed's and auditor's spans
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
    assert rows["mutation"].status == "failed"  # blocked by tier 1: a verdict, not absent
    assert "blocked" in rows["mutation"].text


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
