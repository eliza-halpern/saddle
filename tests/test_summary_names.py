"""A finish summary that names code the tree does not have is returned once (#191).

A dogfood run's finish summary named four helpers in backticks that existed
nowhere: not in its tree, its baseline, or anything it had read or written before
that finish call. They reached its commit message, the narrative a reviewer reads.

Known-bad: a summary naming a function no file defines is returned once, not as a
refusal; a summary that still names it is accepted, and the packet names it.
Known-good: a summary naming only code in the tree, in an untracked file, or code
the change removed (still at the baseline) finishes as before; a second finish is
never returned for it; arm E+A, which delivers nothing, never returns one; a
lookup git cannot make is never read as "nothing missing".
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from test_check_tool import Scripted, call, edit, git, make_repo, outcome, repo, results, run

from saddle.auto import FEED_PROMPT
from saddle.engine import SUMMARY_NAMES_ABSENT
from saddle.packet import compile_packet
from saddle.summary_names import SummaryNamesError, absent, code_names

__all__ = ["repo"]  # the fixture, shared with test_check_tool

BAD = "Added `_invented_helper` beside `add` in `calc.py`."
GOOD = "Fixed `add` in `calc.py`; `add()` now adds."


# -- which spans read as code -------------------------------------------------------


def test_code_names_are_identifiers_dotted_names_mixed_case_and_calls() -> None:
    summary = (
        "`_locate` and `check_paths` call `YamlProblem.order` and `load()`; "
        "`saddle.yamlcheck` is the module. `_locate` again."
    )
    assert code_names(summary) == [
        "_locate",
        "check_paths",
        "YamlProblem.order",
        "load",
        "saddle.yamlcheck",
    ]


def test_prose_paths_and_flags_in_backticks_are_not_code_names() -> None:
    summary = (
        "`tightened`, `sql`, `JSON`, `--schema`, `PATH:LINE:COL`, `tests/test_x.py`, "
        "`a b`, `v1.2`, `resources.jobs.nightly.tasks[0]`"
    )
    assert code_names(summary) == []


# -- where a name is looked for -----------------------------------------------------


@pytest.fixture
def tree(tmp_path: Path) -> tuple[Path, str]:
    root = make_repo(
        tmp_path / "t",
        {"calc.py": "def add(a, b):\n    return a + b\n\n\ndef _gone():\n    return 0\n"},
    )
    baseline = git(root, "rev-parse", "HEAD")
    (root / "calc.py").write_text(
        "def add(a, b):\n    return a + b\n\n\ndef _children():\n    return []\n"
    )  # the change removes `_gone` and adds `_children`
    (root / "notes.py").write_text("UNTRACKED_NAME = 1\n")
    return root, baseline


def test_a_name_in_the_tree_an_untracked_file_or_the_baseline_is_there(
    tree: tuple[Path, str],
) -> None:
    root, baseline = tree
    summary = "`_children` (new), `UNTRACKED_NAME` (untracked), `_gone` (removed), `calc.py`."
    assert absent(summary, root, baseline) == []


def test_a_name_no_file_has_is_absent_and_only_a_whole_word_counts(
    tree: tuple[Path, str],
) -> None:
    root, baseline = tree
    # `_children` is not in backticks: as a pattern of its own, git's longest match
    # would hide the substring `_child` and the test could not see a missing `-w`.
    summary = "`_invented_helper`, `_child` (only inside _children), `calc.no_such`."
    assert absent(summary, root, baseline) == ["_invented_helper", "_child", "calc.no_such"]


def test_a_lookup_git_cannot_make_raises_and_is_never_read_as_nothing_missing(
    tree: tuple[Path, str],
) -> None:
    root, _ = tree
    with pytest.raises(SummaryNamesError, match=r"^git ls-tree failed: "):
        absent("`_invented_helper`", root, "no-such-revision")


def test_a_summary_with_no_code_names_asks_git_nothing(tmp_path: Path) -> None:
    assert absent("Fixed it. `tightened`.", tmp_path / "not-a-repo", "HEAD") == []


# -- the run: returned once, sealed, shown ------------------------------------------


def names_sealed(result_outcome: dict[str, object]) -> object:
    return result_outcome.get("summary_names")


def narrative_row(journal: Path) -> str:
    return next(r for r in compile_packet(journal).rows if r.key == "narrative").text


def test_a_summary_naming_absent_code_is_returned_once_and_the_next_finish_is_taken(
    repo: Path,
) -> None:
    client = Scripted(
        [
            [edit("e", "a - b", "a + b")],
            [call("finish", "f1", summary=BAD)],
            [call("finish", "f2", summary=GOOD)],
        ]
    )
    result, _ = run(repo, client)
    first, second = results(result, "finish")
    assert first == SUMMARY_NAMES_ABSENT.format(names="`_invented_helper`")
    assert not second.startswith("finish returned")
    assert result.outcome == "finished"
    record = outcome(result)
    assert record["finish_refusals"] == 0  # a return is not a refusal
    assert names_sealed(record) == {"absent": [], "returned": ["_invented_helper"]}
    assert "Its first summary named `_invented_helper`" in narrative_row(result.journal)


def test_a_second_finish_naming_the_same_absent_code_is_taken_and_the_packet_names_it(
    repo: Path,
) -> None:
    client = Scripted(
        [
            [edit("e", "a - b", "a + b")],
            [call("finish", "f1", summary=BAD)],
            [call("finish", "f2", summary=BAD)],
        ]
    )
    result, _ = run(repo, client)
    first, second = results(result, "finish")
    assert first.startswith("finish returned, not refused: ")
    assert not second.startswith("finish returned")
    assert result.outcome == "finished"
    assert names_sealed(outcome(result)) == {
        "absent": ["_invented_helper"],
        "returned": ["_invented_helper"],
    }
    row = narrative_row(result.journal)
    assert "It names 1 code name that no file of the tree or the baseline contains" in row
    assert "`_invented_helper`" in row


def test_a_summary_naming_only_code_the_tree_has_is_never_returned(repo: Path) -> None:
    client = Scripted([[edit("e", "a - b", "a + b")], [call("finish", "f1", summary=GOOD)]])
    result, _ = run(repo, client)
    (only,) = results(result, "finish")
    assert not only.startswith("finish returned")
    assert names_sealed(outcome(result)) == {"absent": []}
    assert "code name" not in narrative_row(result.journal)


def test_arm_e_a_never_returns_a_finish_but_the_packet_still_names_the_code(
    repo: Path,
) -> None:
    client = Scripted([[edit("e", "a - b", "a + b")], [call("finish", "f1", summary=BAD)]])
    result, _ = run(repo, client, arm="E+A", check_tool=False)
    (only,) = results(result, "finish")
    assert not only.startswith("finish returned")
    assert names_sealed(outcome(result)) == {"absent": ["_invented_helper"]}
    assert "`_invented_helper`" in narrative_row(result.journal)


def test_the_feedback_arm_is_told_the_rule_and_the_others_are_not(repo: Path) -> None:
    told = {}
    for arm in ("E+A+F", "E+A"):
        client = Scripted([[edit("e", "a - b", "a + b")]])
        run(repo, client, arm=arm, check_tool=arm == "E+A+F", run_id=arm.replace("+", ""))
        told[arm] = "returned to you once, not as a refusal, to correct" in client.system
    assert told == {"E+A+F": True, "E+A": False}
    assert "returned to you once, not as a refusal, to correct" in FEED_PROMPT


def test_a_check_that_cannot_run_returns_nothing_and_the_seal_says_why(
    repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import saddle.auto as auto_module

    def broken(summary: str, worktree: Path, baseline: str) -> list[str]:
        message = "git grep failed: no index"
        raise SummaryNamesError(message)

    monkeypatch.setattr(auto_module, "absent_code_names", broken)
    client = Scripted([[edit("e", "a - b", "a + b")], [call("finish", "f1", summary=BAD)]])
    result, _ = run(repo, client)
    (only,) = results(result, "finish")
    assert not only.startswith("finish returned")
    assert names_sealed(outcome(result)) == {"error": "git grep failed: no index"}
    assert "could not be checked: git grep failed: no index" in narrative_row(result.journal)
    assert json.dumps(outcome(result))  # the seal stays JSON
