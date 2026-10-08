"""The packet card's first screen and action row, in a real browser.

Known-good: a finished, audited run shows the verdict, then the action row,
then a band of Tests / Mutation / Not proven, then Scope and Audit folded
to "N of M passed"; Contract, Narrative, Cost and Reproduce are under
Details; View diff lists the run's files; Merge asks in the page, naming
branch and target, and only its own button merges; Ask about this run seeds
the composer with the recap and names the full report on disk.

Known-bad: an honest stop's band is never green and lists the unresolved
findings; Merge is disabled on a stop and says why; a merge into a dirty
checkout shows git's refusal, not a success.
"""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path
from typing import Any

import pytest
from browser_guard import BROWSER
from packet_seed import GUARDED_SEEDED, git, make_repo, seed
from test_ui3_mode import NoModel, serving

from saddle.packet import compile_packet, plain_words, render_packet_text
from saddle.sessions import SessionStore
from saddle.web.app import build_app

CDP = Path(__file__).parent / "fixtures" / "packet_cdp.mjs"
pytestmark = pytest.mark.skipif(not BROWSER, reason="needs node and google-chrome")


def page(
    tmp_path: Path,
    kind: str,
    step: str,
    *,
    dirty: bool = False,
    width: int = 1200,
    mode: str | None = None,
    remote: bool = False,
) -> tuple[dict[str, Any], Path, str]:
    repo = make_repo(tmp_path / "repo")
    if remote:  # a bare origin holding main, set as main's upstream
        git(tmp_path, "init", "-q", "--bare", "-b", "main", str(tmp_path / "origin.git"))
        git(repo, "remote", "add", "origin", str(tmp_path / "origin.git"))
        git(repo, "push", "-q", "-u", "origin", "main")
    store = SessionStore(tmp_path / "s")
    _sid, _rid, branch = seed(store, repo, kind)  # type: ignore[arg-type]
    if mode is not None:
        store.update(_sid, mode=mode)
    if dirty:
        (repo / "README").write_text("edited, not committed\n")
    app = build_app(store, NoModel, default_workdir=repo)
    args = ["node", str(CDP), "", _sid, step]
    shots = os.environ.get("PACKET_SHOTS")
    args += [
        shots or "",
        f"{kind}{'-dirty' if dirty else ''}{'-phone' if width < 700 else ''}-",
        str(width),
    ]
    with serving(app) as base:
        args[2] = base
        out = subprocess.run(args, capture_output=True, text=True, timeout=90, check=False)
    assert out.returncode == 0, out.stderr
    got: dict[str, Any] = json.loads(out.stdout.strip().splitlines()[-1])
    return got, repo, branch


def test_a_finished_packet_leads_with_the_band_and_folds_the_rest(tmp_path: Path) -> None:
    got, _repo, branch = page(tmp_path, "audited", "read")
    r = got["read"]
    assert r["order"] == ["packet-kicker", "verdict", "actions", "band", "rows", "packet-details"]
    assert [(x["key"], x["tone"], x["glyph"]) for x in r["lines"]] == [
        ("tests", "ok", "✓"),
        ("mutation", "none", "○"),
        # flip: was ("not-proven", "ok", "✓"). The old pin held
        # "Nothing is left unproven" two lines under "No mutation record"
        # (seen in a screenshot review of the merge screen): it pinned
        # the contradiction, not a contract. The item is named below.
        ("not-proven", "warn", "!"),
    ]
    assert r["lines"][0]["text"] == "The auditor ran the suite: 2 passed"
    assert r["lines"][2]["items"] == ["Changed lines were not mutation-tested: no mutation record."]
    # Mutation has no record, so the band is not all-green: "partial".
    assert r["band"] == "partial"
    assert r["firstRows"][0].startswith("prow s-observed k-scope")
    assert r["audit"] == {"summary": "Audit1 passed", "open": False}
    assert r["details"] == {
        "summary": "Details",
        "open": False,
        "keys": ["contract", "narrative", "cost", "reproduce"],
    }
    # Every row the packet compiled is on the card: 3 inside the band's
    # lines (each opens to its full row), Scope, Audit, and 4 in Details.
    assert r["allRows"] == 9
    assert r["bandRows"] == ["tests", "mutation", "not-proven"]
    # Merge needs one proven audit row, not a mutation record; with none,
    # the button names what is missing instead of reading as fully proven.
    assert r["merge"] == {"text": "Merge into main (mutation unproven)", "disabled": False}
    assert r["mergeClass"] == "unproven"
    assert r["view"]["disabled"] is False
    assert r["discard"]["disabled"] is False
    assert r["why"] == ""
    assert r["panel"] == ""  # no confirm is open until asked for
    assert r["download"] == {"text": "Download full report", "disabled": False}
    assert branch.startswith("saddle/auto/")


def test_an_honest_stop_is_amber_lists_its_findings_and_cannot_merge(tmp_path: Path) -> None:
    got, _repo, _branch = page(tmp_path, "stopped", "read")
    r = got["read"]
    assert r["verdict"] == "Stopped"
    assert r["band"] == "stop"
    not_proven = r["lines"][2]
    assert not_proven["tone"] == "warn"
    assert not_proven["glyph"] == "!"
    assert "Unresolved at finish: coverage (the evidence is thin)." in not_proven["items"]
    assert r["merge"]["disabled"] is True
    assert "unproven" not in r["merge"]["text"]  # a disabled Merge drops the suffix
    assert r["why"].startswith("Merge is off: The run is stopped, not finished")
    assert r["discard"]["disabled"] is False
    assert r["audit"]["summary"] == "Audit1 failed"


def test_view_diff_shows_the_runs_files_inline(tmp_path: Path) -> None:
    got, _repo, _branch = page(tmp_path, "audited", "diff")
    assert [f["path"] for f in got["diff"]] == ["calc.py", "tests/test_zero.py"]
    assert got["diff"][0]["add"] == ["+    return a + b"]
    assert got["diff"][0]["del"] == ["-    return a - b"]
    assert got["closed"] == ""


def test_merge_asks_in_the_page_and_cancel_changes_nothing(tmp_path: Path) -> None:
    got, repo, branch = page(tmp_path, "audited", "merge-cancel")
    before = git(repo, "rev-parse", "HEAD").strip()
    assert got["confirm"]["text"].startswith(f"Merge {branch} into main?")
    assert got["confirm"]["yes"] == "Merge into main"
    assert got["confirm"]["focus"] == "Cancel"  # no silent default
    assert got["after"] == ""
    assert git(repo, "rev-parse", "HEAD").strip() == before
    assert git(repo, "rev-parse", "main").strip() != git(repo, "rev-parse", branch).strip()


def test_confirmed_merge_lands_and_says_it_was_not_sealed(tmp_path: Path) -> None:
    got, repo, branch = page(tmp_path, "audited", "merge")
    assert got["result"]["ok"] is True
    assert got["result"]["output"].startswith(f"Fast-forwarded main to {branch}")
    assert "Not sealed in the ledger" in got["result"]["note"]
    assert git(repo, "rev-parse", "main").strip() == git(repo, "rev-parse", branch).strip()


def test_merge_into_a_dirty_checkout_shows_the_refusal(tmp_path: Path) -> None:
    got, repo, branch = page(tmp_path, "audited", "merge", dirty=True)
    assert got["result"]["ok"] is False
    assert got["result"]["output"].startswith("The checkout has uncommitted changes")
    assert "M README" in got["result"]["output"]
    assert git(repo, "rev-parse", "main").strip() != git(repo, "rev-parse", branch).strip()


def test_discard_confirms_then_deletes_the_branch(tmp_path: Path) -> None:
    got, repo, branch = page(tmp_path, "stopped", "discard")
    assert got["confirm"].startswith(f"Delete the branch {branch}")
    assert "Deleted branch" in got["result"]
    assert got["after"]["merge"]["disabled"] is True
    assert git(repo, "branch", "--list", branch) == ""


def test_ask_about_this_run_adds_one_line_of_review_checks_before_the_report(
    tmp_path: Path,
) -> None:
    # The pre-fill is the one review prompt that reaches a model whatever
    # persona the session uses. Its extra line must be one line of its own,
    # between the recap and the report path (the last line, which the test
    # above pins), and must not be a line the packet already prints: a recap
    # alone would otherwise pass for it. The wording is not pinned; what the
    # line does to a model is measured by a replay probe, not asserted here.
    got, _repo, _branch = page(tmp_path, "audited", "chat", mode="task")
    paragraphs = got["input"].strip().split("\n\n")
    assert paragraphs[0].startswith('About the run "make add add":')
    assert paragraphs[-1].startswith("Full report: ")
    checks = paragraphs[-2]
    assert checks.strip()
    assert "\n" not in checks
    report = Path(paragraphs[-1].removeprefix("Full report: ").strip())
    assert checks not in report.read_text(encoding="utf-8")
    # Known-bad shape: with the line missing, the paragraph before the report
    # is the recap's last one, which the packet does print.
    recap_tail = paragraphs[1].splitlines()[-1]
    assert recap_tail in report.read_text(encoding="utf-8")


def test_ask_about_this_run_seeds_the_composer_with_the_recap(tmp_path: Path) -> None:
    # The session starts in Task, so the lane switch is what is under test:
    # "Ask about this run" must leave Task (where Enter opens the Run strip)
    # for Ask, the read-only talk lane, and never opt into unaudited Edit.
    # The label pins the user's wording: an outcome, in the lane's
    # own word, not a place.
    got, repo, _branch = page(tmp_path, "audited", "chat", mode="task")
    assert got["read"]["chat"] == {"text": "Ask about this run", "disabled": False}
    assert got["input"].startswith('About the run "make add add":\n\nverdict: finished')
    assert "Tests [proven]: The auditor ran the suite: 2 passed" in got["input"]
    assert got["mode"] == "ask"
    assert got["focused"] == "input"
    # The last line names the full report on disk, whose bytes are the full
    # packet text; the message itself stays short.
    lines = got["input"].strip().splitlines()
    assert lines[-1].startswith("Full report: ")
    report = Path(lines[-1].removeprefix("Full report: "))
    assert report.is_file()
    assert report.parent.parent == repo / ".saddle" / "runs"
    from saddle.packet import compile_packet, render_packet_text

    assert report.read_text(encoding="utf-8") == render_packet_text(
        compile_packet(report.parent / "proofs.jsonl", run_id=report.parent.name)
    )
    # 23 lines with today's recap (the full render, minus Narrative). The
    # user's bar is ~20: the compact recap rendering, not on this branch,
    # is what brings it under; this pins that nothing longer creeps in.
    assert len(lines) <= 25, len(lines)


def test_a_run_with_a_proven_mutation_row_merges_under_a_plain_label(tmp_path: Path) -> None:
    got, _repo, _branch = page(tmp_path, "mutated", "read")
    r = got["read"]
    assert [x["tone"] for x in r["lines"]] == ["ok", "ok", "ok"]
    assert r["band"] == "ok"
    assert r["merge"] == {"text": "Merge into main", "disabled": False}
    assert r["mergeClass"] == ""


def test_edit_checks_are_their_own_line_not_in_the_audit_count(tmp_path: Path) -> None:
    """The real auditor's three tier-0 records (syntax, ruff,
    imports) were folded into Audit ("4 of 4 passed"); they are one line of
    their own after it, and the Audit fold counts the one verdict it holds."""
    got, _repo, _branch = page(tmp_path, "edit-checked", "read")
    r = got["read"]
    assert r["audit"]["summary"] == "Audit1 passed"
    assert len(r["firstRows"]) == 3
    assert r["firstRows"][0].startswith("prow s-observed k-scope")
    assert r["firstRows"][1].startswith("audit-fold s-proven")
    assert r["firstRows"][2].startswith("prow s-observed k-edit-checks")
    assert "edit-checks" not in r["details"]["keys"]
    assert r["allRows"] == 10


def test_the_mutation_line_opens_to_the_english_summary(tmp_path: Path) -> None:
    """A mutation finding with a sealed outcome shows its count
    line as before and, inside the band's Mutation fold, the grouped English
    summary of what was left untested; the count line is not replaced."""
    got, _repo, _branch = page(tmp_path, "summarised", "mutation")
    m = got["mutation"]
    assert m["open"] is True
    assert m["text"] == "tier 2, fail: killed 220 of 322 sampled mutants; 21 untested"
    assert m["summaryInFold"] is True
    assert m["summary"].startswith("220 of 322 sampled mutants were caught by the suite")
    assert "Left untested:\n  boundary:\n" in m["summary"]
    assert "accounts.py Account.withdraw" in m["summary"]
    assert m["others"] == 1


def test_a_mutation_line_without_a_sealed_outcome_shows_no_summary_block(tmp_path: Path) -> None:
    got, _repo, _branch = page(tmp_path, "mutated", "mutation")
    m = got["mutation"]
    assert m["text"] == "killed 2 of 2 changed-line mutants"
    assert (m["summary"], m["others"]) == (None, 0)


def _report_items(repo: Path, title: str) -> list[str]:
    """The items `packet.md` lists under the row `title`, as the page's download writes it."""
    (journal,) = (repo / ".saddle" / "runs").glob("*/proofs.jsonl")
    lines = render_packet_text(compile_packet(journal, run_id=journal.parent.name)).splitlines()
    start = next(i for i, line in enumerate(lines) if line.startswith(f"{title} ["))
    items = []
    for line in lines[start + 1 :]:
        if not line.startswith("  - "):
            break
        items.append(line.removeprefix("  - "))
    return items


@pytest.mark.parametrize("kind", ["budget", "stopped", "audited"])
def test_not_proven_draws_each_packet_md_item_once_and_no_bullet_is_empty(
    tmp_path: Path, kind: str
) -> None:
    """Known-good: a run stopped on its token budget before any audit draws
    exactly its two packet.md items, with every fold open. Known-bad: an item
    drawn twice (the band's list and its row's list), or an empty bullet."""
    got, repo, _branch = page(tmp_path, kind, "not-proven")
    # packet.md keeps the auditor's codes; the page says them in words (#85 item 5).
    expected = [plain_words(item) for item in _report_items(repo, "Not proven")]
    if kind == "budget":
        assert expected == [
            "No auditor verdict: the suite, changed-line coverage and mutation were not "
            "checked by saddle.",
            "The run stopped before finishing: token budget exhausted: ~100000 of 100000 "
            "generated tokens spent.",
        ]
    assert got["notProven"]["items"] == expected
    assert got["notProven"]["empty"] == 0
    if kind == "budget":
        assert got["notProven"]["cites"] == 1  # the fold still opens to its cite


def test_push_is_offered_only_with_an_upstream(tmp_path: Path) -> None:
    got, _repo, _branch = page(tmp_path, "audited", "read")
    assert got["read"]["push"]["hidden"] is True


def test_merge_and_push_confirms_in_the_page_then_lands_upstream(tmp_path: Path) -> None:
    got, repo, branch = page(tmp_path, "audited", "push", remote=True)
    assert got["read"]["push"] == {
        "text": "Merge and push to origin/main (mutation unproven)",
        "disabled": False,
        "hidden": False,
    }
    asked = f"Merge {branch} into main, then push main to origin/main?"
    assert got["confirm"]["text"].startswith(asked)
    assert got["confirm"]["yes"] == "Merge and push to origin/main"
    assert got["confirm"]["focus"] == "Cancel"
    assert got["result"]["ok"] is True
    assert "Pushed to origin/main." in got["result"]["output"]
    origin = tmp_path / "origin.git"
    assert git(origin, "rev-parse", "main").strip() == git(repo, "rev-parse", branch).strip()


def test_a_stopped_run_cannot_push_either(tmp_path: Path) -> None:
    got, _repo, _branch = page(tmp_path, "stopped", "read", remote=True)
    assert got["read"]["push"]["hidden"] is False
    assert got["read"]["push"]["disabled"] is True


def test_a_held_run_offers_approve_and_merge_and_the_confirm_names_the_files(
    tmp_path: Path,
) -> None:
    """Known-good: a run the self-guard held reads Needs you, its Merge button is
    Approve and merge, the confirm names the guarded files, and approving lands it."""
    got, repo, branch = page(tmp_path, "guarded", "merge")
    assert got["read"]["verdict"] == "Needs you"
    assert got["read"]["merge"] == {"text": "Approve and merge into main", "disabled": False}
    assert got["read"]["push"]["hidden"] is True
    assert "Only you can approve it" in got["read"]["why"]
    for path in GUARDED_SEEDED:
        assert path in got["confirm"]["text"]
    assert got["confirm"]["yes"] == "Approve and merge into main"
    assert got["confirm"]["focus"] == "Cancel"
    assert got["result"]["ok"] is True
    assert got["result"]["output"].startswith("Approved by ")
    assert git(repo, "rev-parse", "main").strip() == git(repo, "rev-parse", branch).strip()
