"""Follow-up commits are attached to a run's ledger with evidence of their own.

Review commits made by hand after a run were outside what its ledger vouched
for, with nothing to say so. `saddle audit --attach-to LEDGER` audits the
commits after the ones the ledger covers and records the result in the ledger's
coverage file as a section of its own, chained to the record before it; after
that `saddle verify` and the packet report coverage through the new head and
list the follow-up apart from the run.

Known-good: an attached follow-up extends the covered range to the new head and
shows as follow-up 1 beside the run's own commit; a second one continues from
the first; a failing audit is recorded as failing. Known-bad: a tampered
follow-up (edited, its sidecar edited, deleted from the middle, spliced in for
another range, resealed with a gap) breaks verification; a ledger that does not
verify, one with no covered commit, a head that is not on top of the covered
commit, and a range with nothing in it each refuse to attach and write nothing.
"""

from __future__ import annotations

import hashlib
import io
import json
from pathlib import Path

import pytest
from test_ledger_anchor import git, lines, repo, reseal, run, write
from test_ledger_covers import commit_on, reproduce_text, short, verify

from saddle import cli, covers
from saddle.auto import AutoResult
from saddle.covers import (
    AttachError,
    FollowupAudit,
    attach_followup,
    coverage_lines,
    plan_attach,
    read_coverage,
)
from saddle.journal import (
    FOLLOWUP_SPAN,
    attempt_sidecar_path,
    coverage_path,
    read_spans,
    verify_journal,
)

__all__ = ["repo"]  # the fixture, re-exported for pytest

REFUSED = FollowupAudit(
    verdict="refuse",
    findings=(
        {"tier": "1", "gate": "tests", "verdict": "fail", "reason": "x", "detail": "1 failed"},
        {"tier": "1", "gate": "lint", "verdict": "pass", "reason": "y", "detail": "clean"},
    ),
)
ACCEPTED = FollowupAudit(verdict="accept", findings=())


def codes(journal: Path) -> list[str]:
    return [issue.code for issue in verify_journal(journal)]


def attach(result: AutoResult, repo: Path, audit: FollowupAudit, rev: str | None = None) -> None:
    attach_followup(result.journal, plan_attach(result.journal, repo, rev=rev), audit)


def test_an_attached_followup_extends_the_covered_range_and_is_listed_apart(repo: Path) -> None:
    started_from = git(repo, "rev-parse", "HEAD").strip()
    result = run(repo)
    first = commit_on(result, "r1.txt", "review fix one")
    second = commit_on(result, "r2.txt", "review fix two")
    assert "not covered by this ledger: 2 later commit(s)" in verify(result.journal, repo)

    attach(result, repo, REFUSED)  # default head: the run branch's tip
    text = verify(result.journal, repo)
    tree = git(repo, "rev-parse", f"{second}^{{tree}}").strip()
    assert (
        f"covers: {short(started_from)}..{short(second)} on {result.branch} (tree {short(tree)})\n"
    ) in text
    assert f"the run itself ended at {short(result.commit)}" in text
    assert (
        f"follow-up 1: {short(result.commit)}..{short(second)}, 2 finding(s), verdict refuse "
        "(1 fail, 1 pass), audited after the run and attached to this ledger, not the run's own\n"
    ) in text
    assert f"later commits on {result.branch}: none" in text
    assert "not covered" not in text
    assert codes(result.journal) == []
    assert f"Follow-up 1: {short(result.commit)}..{short(second)}" in reproduce_text(
        result.journal, repo
    )
    third = commit_on(result, "r3.txt", "review fix three")
    assert f"not covered by this ledger: 1 later commit(s): {short(third)} review fix three" in (
        verify(result.journal, repo)
    )
    assert first != second


def test_a_second_followup_continues_from_the_first(repo: Path) -> None:
    result = run(repo)
    first = commit_on(result, "r1.txt", "review fix one")
    second = commit_on(result, "r2.txt", "review fix two")
    attach(result, repo, ACCEPTED, rev=first)
    midway = verify(result.journal, repo)
    assert f"..{short(first)} on {result.branch}" in midway
    assert f"not covered by this ledger: 1 later commit(s): {short(second)}" in midway
    attach(result, repo, REFUSED)
    text = verify(result.journal, repo)
    assert (
        f"follow-up 1: {short(result.commit)}..{short(first)}, 0 finding(s), verdict accept" in text
    )
    assert f"follow-up 2: {short(first)}..{short(second)}, 2 finding(s), verdict refuse" in text
    [cov] = read_coverage(result.journal, read_spans(result.journal))
    assert cov.head == second
    assert [f.covered_to for f in cov.followups] == [first, second]
    assert codes(result.journal) == []


@pytest.mark.parametrize(
    ("verdict", "exit_code"),
    [("refuse", 1), ("accept", 0), ("question", 0), ("nothing-to-audit", 0)],
)
def test_a_failing_audit_is_recorded_as_failing(repo: Path, verdict: str, exit_code: int) -> None:
    result = run(repo)
    commit_on(result, "r1.txt", "review fix one")
    attach(result, repo, FollowupAudit(verdict=verdict, findings=()))
    [record] = read_spans(coverage_path(result.journal))[-1:]
    assert (record.name, record.exit_code) == (FOLLOWUP_SPAN, exit_code)
    assert f"verdict {verdict}" in verify(result.journal, repo)


def test_a_tampered_followup_breaks_verification(repo: Path) -> None:
    result = run(repo)
    first = commit_on(result, "r1.txt", "review fix one")
    commit_on(result, "r2.txt", "review fix two")
    attach(result, repo, ACCEPTED, rev=first)
    attach(result, repo, REFUSED)
    cfile = coverage_path(result.journal)
    honest = lines(cfile)
    assert codes(result.journal) == []
    one, two = honest[1], honest[2]

    # edited after sealing: its own hash fails
    write(cfile, [honest[0], one, {**two, "detail": two["detail"].replace("refuse", "accept")}])
    assert codes(result.journal) == ["bad-hash"]
    # the sealed findings edited: the sidecar no longer hashes to the record
    write(cfile, honest)
    sidecar = attempt_sidecar_path(cfile, two["span_id"])
    original = sidecar.read_bytes()
    edited = json.loads(original)
    edited["verdict"] = "accept"
    sidecar.write_text(json.dumps(edited, sort_keys=True, indent=1))
    assert codes(result.journal) == ["attempt-sidecar"]
    sidecar.write_bytes(original)
    # the first follow-up deleted from the middle: the second no longer continues anything
    write(cfile, [honest[0], two])
    assert codes(result.journal) == ["followup-chain"]
    # a follow-up spliced in for a range that does not start where coverage ended (resealed)
    gap = reseal({**one, "argv": [one["argv"][0], "0" * 40, *one["argv"][2:]]})
    write(cfile, [honest[0], gap])
    assert codes(result.journal) == ["followup-chain"]
    # one citing the wrong record as its predecessor (resealed)
    wrong = reseal({**one, "argv": [*one["argv"][:3], "1" * 64]})
    write(cfile, [honest[0], wrong])
    assert codes(result.journal) == ["followup-chain"]
    # one with no commit record before it
    write(cfile, [one])
    assert codes(result.journal) == ["followup-chain"]
    # known-good again
    write(cfile, honest)
    assert codes(result.journal) == []


def test_a_ledger_that_cannot_take_a_followup_refuses_and_writes_nothing(
    repo: Path, tmp_path: Path
) -> None:
    result = run(repo)
    commit_on(result, "r1.txt", "review fix one")
    cfile = coverage_path(result.journal)
    before = cfile.read_bytes()

    with pytest.raises(AttachError, match="nothing to attach"):
        plan_attach(result.journal, repo, rev=result.commit)
    with pytest.raises(AttachError, match="cannot resolve"):
        plan_attach(result.journal, repo, rev="no-such-rev")
    with pytest.raises(AttachError, match="is not an ancestor of main"):
        plan_attach(result.journal, repo, rev="main")  # the base: behind the covered commit

    rows = lines(result.journal)
    write(result.journal, [{**rows[0], "detail": "x"}, *rows[1:]])
    with pytest.raises(AttachError, match="does not verify"):
        plan_attach(result.journal, repo, rev=None)
    write(result.journal, rows)

    cfile.unlink()  # a ledger from before the coverage file: no covered commit to extend
    with pytest.raises(AttachError, match="predates"):
        plan_attach(result.journal, repo, rev=None)
    assert not cfile.exists()
    cfile.write_bytes(before)
    assert plan_attach(result.journal, repo, rev=None).covered_to != result.commit


def test_followup_audit_keeps_findings_and_untiered_checks() -> None:
    from saddle.audit import AuditCheck, AuditResult
    from saddle.auditor import Finding, Findings

    finding = Finding(gate="tests", tier=1, verdict="fail", reason="scope", detail="d", cites=())
    tiered = cli.followup_audit((Findings(tier=1, key="k", findings=(finding,)),))
    assert tiered.verdict == "refuse"
    assert tiered.findings == (
        {"tier": "1", "gate": "tests", "verdict": "fail", "reason": "scope", "detail": "d"},
    )
    empty = AuditResult(
        verdict="nothing-to-audit",
        tree="t",
        baseline="b",
        test_command="c",
        checks=(AuditCheck(name="tests", status="pass", detail="ok", basis=None),),
        mutation=None,
        surface="s",
    )
    plain = cli.followup_audit(empty)
    assert plain.verdict == "nothing-to-audit"
    assert plain.findings == ({"gate": "tests", "verdict": "pass", "detail": "ok"},)


def test_the_cli_attaches_a_real_audit_of_the_commits_after_the_run(
    repo: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    result = run(repo)
    commit_on(result, "r1.txt", "review fix one")
    head = git(repo, "rev-parse", result.branch).strip()
    out, err = io.StringIO(), io.StringIO()
    code = cli.main(
        ["audit", "--repo", str(repo), "--no-cache", "--attach-to", str(result.journal)],
        stdout=out,
        stderr=err,
    )
    assert code == 1, err.getvalue()  # the range changes only tests: nothing proves it
    assert (
        f"attached to {result.journal} as follow-up 1: {short(result.commit)}..{short(head)}"
        in (out.getvalue())
    )
    assert codes(result.journal) == []
    text = verify(result.journal, repo)
    assert f"follow-up 1: {short(result.commit)}..{short(head)}" in text
    assert "verdict refuse" in text
    assert f"later commits on {result.branch}: none" in text


@pytest.mark.parametrize("extra", [["--baseline", "main"], ["no-such-rev"]])
def test_the_cli_refuses_an_attach_it_cannot_resolve(repo: Path, extra: list[str]) -> None:
    result = run(repo)
    commit_on(result, "r1.txt", "review fix one")
    before = coverage_path(result.journal).read_bytes()
    out, err = io.StringIO(), io.StringIO()
    argv = ["audit", "--repo", str(repo), "--attach-to", str(result.journal), *extra]
    assert cli.main(argv, stdout=out, stderr=err) == 2
    assert err.getvalue().startswith("error: ")
    assert coverage_path(result.journal).read_bytes() == before


def test_a_json_attach_keeps_stdout_json_and_notes_the_attachment_on_stderr(
    repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    result = run(repo)
    commit_on(result, "r1.txt", "review fix one")
    monkeypatch.setattr(cli, "_tiered_audit", lambda *a, **k: ())
    out, err = io.StringIO(), io.StringIO()
    argv = ["audit", "--repo", str(repo), "--json", "--attach-to", str(result.journal)]
    cli.main(argv, stdout=out, stderr=err)
    assert json.loads(out.getvalue())["verdict"] == "accept"
    assert "attached to" in err.getvalue()
    assert codes(result.journal) == []


def test_a_followup_resealed_over_a_sidecar_that_is_not_findings_reads_as_not_readable(
    repo: Path,
) -> None:
    result = run(repo)
    commit_on(result, "r1.txt", "review fix one")
    attach(result, repo, REFUSED)
    cfile = coverage_path(result.journal)
    row = lines(cfile)[-1]
    forged = b"[]"
    attempt_sidecar_path(cfile, row["span_id"]).write_bytes(forged)
    resealed = reseal({**row, "attempt_hash": hashlib.sha256(forged).hexdigest()})
    write(cfile, [lines(cfile)[0], resealed])
    assert codes(result.journal) == []  # it verifies: the hashes were all redone
    [cov] = read_coverage(result.journal, read_spans(result.journal))
    assert cov.followups[0].verdict == ""  # never a verdict it cannot show
    assert cov.followups[0].findings == ()
    assert "verdict not readable" in "\n".join(coverage_lines(cov, None))


def test_the_packet_says_when_the_coverage_file_does_not_verify(repo: Path) -> None:
    result = run(repo)
    cfile = coverage_path(result.journal)
    [row] = lines(cfile)
    write(cfile, [{**row, "detail": "x"}])
    said = reproduce_text(result.journal, repo)
    assert "Its coverage file does not verify, so no coverage is stated. " in said
    assert "Covers:" not in said


def test_an_unreadable_tree_refuses_to_attach(repo: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    result = run(repo)
    commit_on(result, "r1.txt", "review fix one")
    real = covers._git

    def fake(repo_: Path, *args: str) -> tuple[int, str]:
        return (128, "bad object") if args[-1].endswith("^{tree}") else real(repo_, *args)

    monkeypatch.setattr(covers, "_git", fake)
    with pytest.raises(AttachError, match="cannot read the tree"):
        plan_attach(result.journal, repo, rev=None)
