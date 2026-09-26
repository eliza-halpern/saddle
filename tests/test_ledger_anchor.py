"""An autonomous run's ledger is anchored outside itself, in its branch (ANCHOR).

CHAIN holds a run's spans to the list its outcome seals, but nothing is
keyed: delete a span, drop it from the sidecar list, recompute the outcome
span's `attempt_hash` and `record_hash`, and the ledger verifies. So the
run branch's final commit carries the outcome span's `record_hash` as a
`Saddle-Outcome` trailer, and `saddle verify --anchor` compares the two.

Known-good: an untouched finished run, and an honest stop, verify with the
anchor. Known-bad: that forgery fails `anchor-mismatch`; the outcome and
its proof both deleted fails `outcome-missing-anchored` (without the anchor
it reads as in flight); a branch whose anchor is gone fails `anchor-missing`.
"""

from __future__ import annotations

import hashlib
import io
import json
import subprocess
from collections.abc import Iterator
from pathlib import Path
from typing import Any, cast

import pytest

from saddle import cli
from saddle.anchor import (
    anchor_issues,
    default_anchor_repo,
    outcome_hash,
    parse_trailers,
)
from saddle.auto import AutoOptions, AutoResult, run_auto
from saddle.journal import attempt_sidecar_path, verify_journal
from saddle.packet import compile_packet
from saddle.vllm import ToolCall, VllmClient


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
    (root / "calc.py").write_text("def add(a, b):\n    return a - b\n")
    (root / "tests" / "test_calc.py").write_text("def test_x():\n    assert True\n")
    git(root, "init", "-q", "-b", "main")
    git(root, "add", "-A")
    git(root, "commit", "-q", "-m", "init")
    return root


def call(name: str, call_id: str, **arguments: Any) -> ToolCall:
    return ToolCall(id=call_id, name=name, arguments=json.dumps(arguments))


class Scripted:
    def __init__(self, rounds: list[list[Any]]) -> None:
        self.rounds = list(rounds)

    def __enter__(self) -> Scripted:
        return self

    def __exit__(self, *exc: object) -> None:
        return None

    def stream_chat(self, messages: Any, **kwargs: Any) -> Iterator[Any]:
        return iter(self.rounds.pop(0) if self.rounds else [])


def run(repo: Path, *, finish: bool = True, summary: str = "fixed add") -> AutoResult:
    """edit_file, a refused test edit, read_file, then finish (or no finish: a stop)."""
    rounds = [
        [call("edit_file", "c1", path="calc.py", old="a - b", new="a + b")],
        [call("edit_file", "c2", path="tests/test_calc.py", old="True", new="False")],
        [call("read_file", "c3", path="calc.py")],
    ]
    if finish:
        rounds.append([call("finish", "f1", summary=summary)])
    ticks = iter(range(0, 10**6, 60))  # a stop: the time budget runs out within ~30 rounds
    options = AutoOptions(
        task="make add add", repo=repo, run_id="r1", arm="E", clock=lambda: float(next(ticks))
    )
    return run_auto(options, cast(VllmClient, Scripted(rounds)))


def lines(journal: Path) -> list[dict[str, Any]]:
    return [json.loads(line) for line in journal.read_text().splitlines()]


def write(journal: Path, rows: list[dict[str, Any]]) -> None:
    journal.write_text("".join(json.dumps(row, sort_keys=True) + "\n" for row in rows))


def index_of(rows: list[dict[str, Any]], name: str) -> int:
    return next(i for i, row in enumerate(rows) if row.get("name") == name)


def reseal(row: dict[str, Any]) -> dict[str, Any]:
    payload = {k: v for k, v in row.items() if k != "record_hash"}
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    return {**payload, "record_hash": hashlib.sha256(canonical.encode()).hexdigest()}


def codes(journal: Path, repo: Path) -> list[str]:
    return [i.code for i in verify_journal(journal) + anchor_issues(journal, repo)]


def tip_message(result: AutoResult) -> str:
    return git(result.worktree, "log", "-1", "--format=%B")


def forge(result: AutoResult) -> None:
    """Delete the refusal span, drop it from the list, reseal the outcome: CHAIN's limit."""
    rows = lines(result.journal)
    refused = rows.pop(index_of(rows, "refused:edit_file"))
    at = next(i for i, r in enumerate(rows) if r.get("name") in ("auto:finished", "auto:stopped"))
    path = attempt_sidecar_path(result.journal, rows[at]["span_id"])
    evidence = json.loads(path.read_text())
    evidence["tool_span_hashes"].remove(refused["record_hash"])
    encoded = json.dumps(evidence, sort_keys=True, indent=1).encode()
    path.write_bytes(encoded)
    rows[at] = reseal({**rows[at], "attempt_hash": hashlib.sha256(encoded).hexdigest()})
    write(result.journal, rows)


# -- known-good ----------------------------------------------------------------


@pytest.mark.parametrize(("finish", "name"), [(True, "auto:finished"), (False, "auto:stopped")])
def test_the_final_commit_ends_with_the_outcome_spans_record_hash(
    repo: Path, finish: bool, name: str
) -> None:
    result = run(repo, finish=finish)
    rows = lines(result.journal)
    outcome = rows[index_of(rows, name)]
    message = tip_message(result)
    last = message.strip().split("\n\n")[-1]
    assert last == (
        f"Saddle-Outcome: {outcome['record_hash']}\nSaddle-Ledger: .saddle/runs/r1/proofs.jsonl"
    )
    # the span's own hash, not its sidecar's (attempt_hash)
    assert outcome["attempt_hash"] not in message
    assert codes(result.journal, repo) == []


def test_verify_with_the_anchor_passes_an_untouched_run_and_says_so(repo: Path) -> None:
    result = run(repo)
    out = io.StringIO()
    assert cli.main(["verify", str(result.journal), "--anchor"], stdout=out) == 0
    assert f", anchored in {repo.resolve()}\n" in out.getvalue()
    out = io.StringIO()
    assert cli.main(["verify", str(result.journal), "--anchor", str(repo)], stdout=out) == 0
    assert default_anchor_repo(result.journal) == repo.resolve()


def test_verify_without_the_anchor_does_not_consult_the_branch(repo: Path) -> None:
    result = run(repo)
    git(repo, "worktree", "remove", "--force", str(result.worktree))
    git(repo, "branch", "-D", result.branch)
    out = io.StringIO()
    assert cli.main(["verify", str(result.journal)], stdout=out) == 0
    assert "anchored" not in out.getvalue()


def test_commits_made_on_the_branch_after_the_run_do_not_hide_its_anchor(repo: Path) -> None:
    result = run(repo)
    (result.worktree / "notes.txt").write_text("later\n")
    git(result.worktree, "add", "notes.txt")
    git(result.worktree, "commit", "-q", "-m", "a human commit after the run")
    assert codes(result.journal, repo) == []


def test_a_trailer_in_the_models_narrative_is_not_the_anchor(repo: Path) -> None:
    result = run(repo, summary=f"done\n\nSaddle-Outcome: {'0' * 64}")
    assert "Saddle-Outcome: " + "0" * 64 in tip_message(result)
    assert codes(result.journal, repo) == []


def test_an_in_flight_run_with_neither_outcome_nor_anchor_verifies(repo: Path) -> None:
    result = run(repo)
    git(result.worktree, "reset", "-q", "--hard", "HEAD~1")
    rows = [
        r
        for r in lines(result.journal)
        if r["record_type"] != "proof" and r.get("name") != "auto:finished"
    ]
    write(result.journal, rows)
    assert codes(result.journal, repo) == []


# -- known-bad -----------------------------------------------------------------


def test_a_resealed_forgery_verifies_without_the_anchor_and_fails_with_it(repo: Path) -> None:
    result = run(repo)
    forge(result)
    assert verify_journal(result.journal) == []  # CHAIN's documented limit
    found = anchor_issues(result.journal, repo)
    assert [i.code for i in found] == ["anchor-mismatch"]
    assert result.branch in found[0].message
    out = io.StringIO()
    assert cli.main(["verify", str(result.journal), "--anchor"], stdout=out) == 1
    assert out.getvalue().startswith("anchor-mismatch@line 1: ")


def test_a_resealed_forgery_of_an_honest_stop_fails_with_the_anchor(repo: Path) -> None:
    result = run(repo, finish=False)
    forge(result)
    assert codes(result.journal, repo) == ["anchor-mismatch"]


def test_a_deleted_outcome_and_proof_read_in_flight_unless_anchored(repo: Path) -> None:
    result = run(repo)
    rows = [
        r
        for r in lines(result.journal)
        if r["record_type"] != "proof" and r.get("name") != "auto:finished"
    ]
    write(result.journal, rows)
    assert verify_journal(result.journal) == []  # CHAIN's documented limit
    found = anchor_issues(result.journal, repo)
    assert [i.code for i in found] == ["outcome-missing-anchored"]
    assert outcome_hash(result.journal, rows[0]["span_id"]) == ""


def test_a_finished_run_whose_branch_lost_its_anchor_fails(repo: Path) -> None:
    result = run(repo)
    git(result.worktree, "commit", "-q", "--amend", "-m", "saddle auto r1: finished")
    assert codes(result.journal, repo) == ["anchor-missing"]


def test_a_finished_run_whose_branch_is_gone_fails(repo: Path) -> None:
    result = run(repo)
    git(repo, "worktree", "remove", "--force", str(result.worktree))
    git(repo, "branch", "-D", result.branch)
    found = anchor_issues(result.journal, repo)
    assert [i.code for i in found] == ["anchor-missing"]
    assert repr(result.branch) in found[0].message


def test_a_start_span_naming_no_branch_cannot_be_anchored(repo: Path) -> None:
    result = run(repo)
    rows = lines(result.journal)
    rows[0] = reseal({**rows[0], "detail": rows[0]["detail"].replace("branch ", "br ")})
    write(result.journal, rows)
    found = anchor_issues(result.journal, repo)
    assert [i.code for i in found] == ["anchor-missing"]
    assert "no branch named" in found[0].message


# -- the reader's edges ----------------------------------------------------------


def test_the_anchor_reader_skips_lines_verify_already_rejects(repo: Path) -> None:
    result = run(repo)
    with result.journal.open("a") as fh:
        fh.write("not json\n")
    assert anchor_issues(result.journal, repo) == []
    assert anchor_issues(repo / "absent.jsonl", repo) == []


def test_trailers_are_read_from_the_last_paragraph_only() -> None:
    assert parse_trailers("s\n\nSaddle-Outcome: a\n\nbody") == {}
    assert parse_trailers("s\n\nOther: x\nSaddle-Outcome: b\nSaddle-Ledger: l") == {
        "Saddle-Outcome": "b",
        "Saddle-Ledger": "l",
    }
    assert parse_trailers("") == {}


# -- the packet --------------------------------------------------------------------


def reproduce(journal: Path, anchor_repo: Path | None) -> tuple[str, tuple[str, ...]]:
    row = next(
        r for r in compile_packet(journal, anchor_repo=anchor_repo).rows if r.key == "reproduce"
    )
    return row.text, row.items


def test_the_packet_reports_the_anchor_check_only_when_it_ran(repo: Path) -> None:
    result = run(repo)
    text, items = reproduce(result.journal, None)
    assert "anchor" not in text.lower()
    assert items[0].startswith("saddle verify ")
    assert "--anchor" not in items[0]
    text, items = reproduce(result.journal, repo)
    assert "matches the Saddle-Outcome trailer on the run branch" in text
    assert items[0].startswith("saddle verify ")
    assert items[0].endswith(" --anchor")
    forge(result)
    text, _ = reproduce(result.journal, repo)
    assert "The branch anchor does not match: anchor-mismatch." in text


def in_flight(result: AutoResult) -> None:
    rows = [
        r
        for r in lines(result.journal)
        if r["record_type"] != "proof" and r.get("name") != "auto:finished"
    ]
    write(result.journal, rows)


def test_the_packet_claims_no_match_for_a_run_in_flight(repo: Path) -> None:
    """FIX-4: the web page reads packets mid-run. With no outcome sealed, a
    clean anchor check has nothing to match; an anchor with no outcome
    behind it is still reported."""
    result = run(repo)
    git(result.worktree, "reset", "-q", "--hard", "HEAD~1")
    in_flight(result)
    text, items = reproduce(result.journal, repo)
    assert "Saddle-Outcome" not in text
    assert "anchor" not in text.lower()
    assert items[0].endswith(" --anchor")


def test_the_packet_reports_an_anchor_with_no_outcome_behind_it(repo: Path) -> None:
    anchored = run(repo)
    in_flight(anchored)
    text, _ = reproduce(anchored.journal, repo)
    assert "The branch anchor does not match: outcome-missing-anchored." in text
