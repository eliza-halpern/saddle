"""The packet card's first screen and action row, in a real browser.

Known-good: a finished, audited run shows the verdict, then the action row,
then a band of Tests / Mutation / Not proven, then Scope and Audit folded
to "N of M passed"; Contract, Narrative, Cost and Reproduce are under
Details; View diff lists the run's files; Merge asks in the page, naming
branch and target, and only its own button merges; Continue in chat seeds
the composer with the recap.

Known-bad: an honest stop's band is never green and lists the unresolved
findings; Merge is disabled on a stop and says why; a merge into a dirty
checkout shows git's refusal, not a success.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path
from typing import Any

import pytest
from packet_seed import git, make_repo, seed
from test_ui3_mode import NoModel, serving

from saddle.sessions import SessionStore
from saddle.web.app import build_app

CDP = Path(__file__).parent / "fixtures" / "packet_cdp.mjs"
BROWSER = shutil.which("node") and shutil.which("google-chrome")
pytestmark = pytest.mark.skipif(not BROWSER, reason="needs node and google-chrome")


def page(
    tmp_path: Path,
    kind: str,
    step: str,
    *,
    dirty: bool = False,
    width: int = 1200,
    mode: str | None = None,
) -> tuple[dict[str, Any], Path, str]:
    repo = make_repo(tmp_path / "repo")
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
        ("not-proven", "ok", "✓"),
    ]
    assert r["lines"][0]["text"] == "The auditor ran the suite: 2 passed"
    # Mutation has no record, so the band is not all-green: "partial".
    assert r["band"] == "partial"
    assert r["firstRows"][0].startswith("prow s-observed k-scope")
    assert r["audit"] == {"summary": "Audit1 of 1 passed", "open": False}
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
    assert branch.startswith("saddle/auto/")


def test_an_honest_stop_is_amber_lists_its_findings_and_cannot_merge(tmp_path: Path) -> None:
    got, _repo, _branch = page(tmp_path, "stopped", "read")
    r = got["read"]
    assert r["verdict"] == "Stopped"
    assert r["band"] == "stop"
    not_proven = r["lines"][2]
    assert not_proven["tone"] == "warn"
    assert not_proven["glyph"] == "!"
    assert "Unresolved at finish: coverage (evidence-thin)." in not_proven["items"]
    assert r["merge"]["disabled"] is True
    assert r["why"].startswith("Merge is off: The run is stopped, not finished")
    assert r["discard"]["disabled"] is False
    assert r["audit"]["summary"] == "Audit0 of 1 passed"


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


def test_continue_in_chat_seeds_the_composer_with_the_recap(tmp_path: Path) -> None:
    # The session starts in Task, so the lane switch is what is under test:
    # "Continue in chat" must leave Task (where Enter opens the Run strip) for
    # Ask, the read-only talk lane, and never opt into unaudited Edit.
    got, _repo, _branch = page(tmp_path, "audited", "chat", mode="task")
    assert got["input"].startswith('About the run "make add add":\n\nverdict: finished')
    assert "Tests [proven]: The auditor ran the suite: 2 passed" in got["input"]
    assert got["mode"] == "ask"
    assert got["focused"] == "input"


def test_a_run_with_a_proven_mutation_row_merges_under_a_plain_label(tmp_path: Path) -> None:
    got, _repo, _branch = page(tmp_path, "mutated", "read")
    r = got["read"]
    assert [x["tone"] for x in r["lines"]] == ["ok", "ok", "ok"]
    assert r["band"] == "ok"
    assert r["merge"] == {"text": "Merge into main", "disabled": False}
    assert r["mergeClass"] == ""


def test_edit_checks_are_their_own_line_not_in_the_audit_count(tmp_path: Path) -> None:
    """PACKETFIX-1: the real auditor's three tier-0 records (syntax, ruff,
    imports) were folded into Audit ("4 of 4 passed"); they are one line of
    their own after it, and the Audit fold counts the one verdict it holds."""
    got, _repo, _branch = page(tmp_path, "edit-checked", "read")
    r = got["read"]
    assert r["audit"]["summary"] == "Audit1 of 1 passed"
    assert len(r["firstRows"]) == 3
    assert r["firstRows"][0].startswith("prow s-observed k-scope")
    assert r["firstRows"][1].startswith("audit-fold s-proven")
    assert r["firstRows"][2].startswith("prow s-observed k-edit-checks")
    assert "edit-checks" not in r["details"]["keys"]
    assert r["allRows"] == 10
