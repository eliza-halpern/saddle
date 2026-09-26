"""An autonomous run's ledger is tamper-evident against lost, moved and added spans.

Spans are hashed one by one, so each line proves only itself. The outcome
span's sealed sidecar lists every tool span's hash in run order, and
`saddle verify` holds the spans present to that list. Known-good: an
untouched run verifies. Known-bad, each named by the span it concerns: a
deleted tool span, a deleted refusal span, two spans swapped, a span after
the outcome, the list edited (plainly, or with the outcome re-sealed), and
a deleted outcome.
"""

from __future__ import annotations

import hashlib
import io
import json
import shutil
import subprocess
from collections.abc import Iterator
from pathlib import Path
from typing import Any, cast

import pytest

from saddle import cli
from saddle.auto import AutoOptions, AutoResult, run_auto
from saddle.journal import (
    append_span,
    attempt_sidecar_path,
    build_span,
    verify_journal,
)
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


def run(repo: Path, run_id: str = "r1") -> AutoResult:
    """edit_file, a refused test edit, read_file, finish: four tool spans."""
    client = Scripted(
        [
            [call("edit_file", "c1", path="calc.py", old="a - b", new="a + b")],
            [call("edit_file", "c2", path="tests/test_calc.py", old="True", new="False")],
            [call("read_file", "c3", path="calc.py")],
            [call("finish", "f1", summary="fixed add")],
        ]
    )
    options = AutoOptions(task="make add add", repo=repo, run_id=run_id, arm="E")
    return run_auto(options, cast(VllmClient, client))


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


def verify_text(journal: Path) -> tuple[int, str]:
    out = io.StringIO()
    return cli.run_verify(journal, stdout=out), out.getvalue()


# -- known-good ----------------------------------------------------------------


def test_an_untouched_run_verifies_and_its_list_is_every_tool_span_in_order(repo: Path) -> None:
    result = run(repo)
    rows = lines(result.journal)
    names = [row.get("name") for row in rows if row["record_type"] == "span"]
    assert names.count("auto:spend") == 4  # USAGE: one agent span per round, never listed
    names = [name for name in names if name != "auto:spend"]
    assert names == ["auto:start", "edit_file", "refused:edit_file", "read_file", "finish",
                     "auto:finished"]  # fmt: skip
    code, text = verify_text(result.journal)
    assert (code, verify_journal(result.journal)) == (0, [])
    proofs = sum(1 for row in rows if row["record_type"] == "proof")
    # INTEG-4: 6 listed spans + USAGE's 4 unlisted auto:spend spans = 10; plus 1 proof.
    assert (proofs, len(rows)) == (1, 11)
    assert text.startswith(
        f"OK: {result.journal}: ledger verifies: 11 records (1 proof(s), "
        "10 span(s), 0 plan(s)), outcome span list intact (1 autonomous run(s))\n"
    )
    assert "chain verifies" not in text


def two_runs(repo: Path) -> Path:
    first = run(repo, "r1")
    second = run(repo, "r2")
    combined = first.journal.parent / "both.jsonl"
    combined.write_text(first.journal.read_text() + second.journal.read_text())
    for source in (first, second):
        for sidecar in (source.journal.parent / "attempts").iterdir():
            target = combined.parent / "attempts" / sidecar.name
            target.write_bytes(sidecar.read_bytes())
    return combined


def test_two_runs_in_one_journal_are_each_held_to_their_own_list(repo: Path) -> None:
    assert verify_journal(two_runs(repo)) == []


def test_a_deleted_outcome_is_charged_to_its_own_run_only(repo: Path) -> None:
    combined = two_runs(repo)
    rows = lines(combined)
    at = index_of(rows, "auto:finished")
    rows.pop(at)
    write(combined, rows)
    issues = verify_journal(combined)
    assert [(issue.code, issue.line) for issue in issues] == [("outcome-missing", at + 1)]


def test_a_journal_without_an_auto_run_is_not_judged_by_the_list(tmp_path: Path) -> None:
    journal = tmp_path / "chat.jsonl"
    append_span(journal, build_span(node_id="chat#1", argv=["read_file"], duration_ms=0,
                                    exit_code=0, detail=""))  # fmt: skip
    assert verify_journal(journal) == []


def test_an_in_flight_run_with_no_outcome_and_no_proof_verifies(repo: Path) -> None:
    result = run(repo)
    rows = lines(result.journal)
    write(result.journal, rows[: index_of(rows, "auto:finished")])
    assert verify_journal(result.journal) == []


# -- known-bad: each names the span --------------------------------------------


def codes(journal: Path) -> dict[str, str]:
    return {issue.code: issue.message for issue in verify_journal(journal)}


@pytest.mark.parametrize("name", ["edit_file", "refused:edit_file"])
def test_a_deleted_span_fails_verify_naming_its_hash(repo: Path, name: str) -> None:
    result = run(repo)
    rows = lines(result.journal)
    gone = rows.pop(index_of(rows, name))
    write(result.journal, rows)
    code, text = verify_text(result.journal)
    assert code == 1
    assert "span-missing" in text
    assert gone["record_hash"] in text


def test_two_swapped_spans_fail_verify_naming_the_first_out_of_order(repo: Path) -> None:
    result = run(repo)
    rows = lines(result.journal)
    a, b = index_of(rows, "edit_file"), index_of(rows, "read_file")
    rows[a], rows[b] = rows[b], rows[a]
    write(result.journal, rows)
    found = codes(result.journal)
    assert list(found) == ["span-order"]
    assert rows[a]["span_id"] in found["span-order"]


def test_a_span_appended_after_the_outcome_fails_verify_naming_it(repo: Path) -> None:
    result = run(repo)
    extra = build_span(node_id="chat#1", argv=["run_command"], duration_ms=0, exit_code=0,
                       detail="")  # fmt: skip
    append_span(result.journal, extra)
    found = codes(result.journal)
    assert list(found) == ["span-after-outcome"]
    assert extra.span_id in found["span-after-outcome"]


def test_a_span_inserted_before_the_outcome_fails_verify_naming_it(repo: Path) -> None:
    result = run(repo)
    rows = lines(result.journal)
    extra = build_span(node_id="chat#1", argv=["run_command"], duration_ms=0, exit_code=0,
                       detail="").model_dump(exclude_unset=True)  # fmt: skip
    rows.insert(index_of(rows, "auto:finished"), extra)
    write(result.journal, rows)
    found = codes(result.journal)
    assert list(found) == ["span-unlisted"]
    assert extra["span_id"] in found["span-unlisted"]


def outcome_and_sidecar(result: AutoResult) -> tuple[list[dict[str, Any]], int, Path]:
    rows = lines(result.journal)
    at = index_of(rows, "auto:finished")
    return rows, at, attempt_sidecar_path(result.journal, rows[at]["span_id"])


def test_a_plainly_edited_list_fails_verify_on_the_outcome_sidecar(repo: Path) -> None:
    result = run(repo)
    _, _, path = outcome_and_sidecar(result)
    evidence = json.loads(path.read_text())
    evidence["tool_span_hashes"].pop(0)
    path.write_text(json.dumps(evidence, sort_keys=True, indent=1))
    assert list(codes(result.journal)) == ["attempt-sidecar"]


def test_a_list_dropped_one_entry_and_resealed_fails_verify_naming_the_span(repo: Path) -> None:
    result = run(repo)
    rows, at, path = outcome_and_sidecar(result)
    evidence = json.loads(path.read_text())
    dropped = evidence["tool_span_hashes"].pop(1)
    encoded = json.dumps(evidence, sort_keys=True, indent=1).encode()
    path.write_bytes(encoded)
    rows[at] = reseal({**rows[at], "attempt_hash": hashlib.sha256(encoded).hexdigest()})
    write(result.journal, rows)
    found = codes(result.journal)
    assert list(found) == ["span-unlisted"]
    span = next(row for row in rows if row.get("record_hash") == dropped)
    assert span["name"] == "refused:edit_file"
    assert span["span_id"] in found["span-unlisted"]


def test_a_deleted_outcome_fails_verify_once_the_proof_is_sealed(repo: Path) -> None:
    result = run(repo)
    rows = lines(result.journal)
    start = rows[index_of(rows, "auto:start")]["span_id"]
    rows.pop(index_of(rows, "auto:finished"))
    write(result.journal, rows)
    found = codes(result.journal)
    assert list(found) == ["outcome-missing"]
    assert start in found["outcome-missing"]


def test_a_second_outcome_fails_verify_naming_it(repo: Path) -> None:
    result = run(repo)
    rows = lines(result.journal)
    at = index_of(rows, "auto:finished")
    twin = reseal({**rows[at], "span_id": "twin"})
    attempt_sidecar_path(result.journal, "twin").write_bytes(
        attempt_sidecar_path(result.journal, rows[at]["span_id"]).read_bytes()
    )
    rows.insert(at + 1, twin)
    write(result.journal, rows)
    found = codes(result.journal)
    assert list(found) == ["outcome-duplicate"]
    assert "'twin'" in found["outcome-duplicate"]


@pytest.mark.parametrize(
    "evidence", [None, b"not json", b"[1]", b'{"tool_span_hashes": [1]}', b"{}"]
)
def test_an_outcome_without_a_readable_list_fails_verify(
    repo: Path, evidence: bytes | None
) -> None:
    result = run(repo)
    rows, at, path = outcome_and_sidecar(result)
    if evidence is None:
        rows[at] = reseal({k: v for k, v in rows[at].items() if k != "attempt_hash"})
    else:
        path.write_bytes(evidence)
        rows[at] = reseal({**rows[at], "attempt_hash": hashlib.sha256(evidence).hexdigest()})
    write(result.journal, rows)
    found = codes(result.journal)
    assert list(found) == ["outcome-list"]
    assert rows[at]["span_id"] in found["outcome-list"]


# -- compatibility: a journal written before this check ----------------------

PRE_CHAIN = Path(__file__).parent / "fixtures" / "auto_pre_chain"
"""Written by `saddle auto` at origin/phase2-executor (1325bd7), before the
span-list check existed; the write path is unchanged, so it must verify."""


def test_a_journal_written_before_the_check_still_verifies() -> None:
    assert verify_journal(PRE_CHAIN / "proofs.jsonl") == []


def test_a_journal_written_before_the_check_is_held_to_its_list(tmp_path: Path) -> None:
    copy = tmp_path / "pre"
    shutil.copytree(PRE_CHAIN, copy)
    rows = lines(copy / "proofs.jsonl")
    gone = rows.pop(index_of(rows, "refused:edit_file"))
    write(copy / "proofs.jsonl", rows)
    found = codes(copy / "proofs.jsonl")
    assert list(found) == ["span-missing"]
    assert gone["record_hash"] in found["span-missing"]


def test_verify_counts_every_record_and_every_autonomous_run(repo: Path) -> None:
    """POLISH-1: the OK line's counts are the journal's, not constants."""
    journal = two_runs(repo)
    code, text = verify_text(journal)
    total = len(lines(journal))
    assert code == 0
    assert f": ledger verifies: {total} records (" in text
    assert "outcome span list intact (2 autonomous run(s))\n" in text


def test_a_deleted_span_prints_no_ok_line_and_its_issue_names_the_span(repo: Path) -> None:
    """POLISH-1 known-bad: the issue line leads with the code and names the hash."""
    result = run(repo)
    rows = lines(result.journal)
    gone = rows.pop(index_of(rows, "read_file"))
    write(result.journal, rows)
    code, text = verify_text(result.journal)
    assert code == 1
    assert "verifies" not in text
    issue = [line for line in text.splitlines() if line.startswith("span-missing@")]
    assert len(issue) == 1
    assert gone["record_hash"] in issue[0]
