"""The audit's `test-changes` finding, over real git trees.

The defect it answers: a run rewrote a pre-existing JavaScript test to match
its new code, and the audit said every pre-existing test kept its assertions.
The recorded before/after of that file (`tests/fixtures/renderer_suite_*.txt`,
the two committed versions of one test file) is replayed here, along with the
Python cases, the `flip:` label, the circular-evidence refusal, the feed's
early refusal and the CLI's reading of commit messages.
"""

from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Any

import pytest
from test_feed import EDIT_COMMENT, FakeAuditor, Reactive, call, run

from saddle import flips as tc
from saddle.audit import AuditCheck, AuditError, AuditResult
from saddle.auditor import (
    TEST_CHANGES,
    Findings,
    changed_tests,
    commit_messages,
    flip_finding,
)
from saddle.cli import _with_test_changes
from saddle.feed import FLIPS_FIRST, AuditFeed, render

FIXTURES = Path(__file__).parent / "fixtures"
JS = "tests/markdown.test.js"
TITLE = "output that is not a diff is left alone"
GOOD = (
    f"flip: {TITLE} -- a created-file row such as 'created a.svg (9 bytes)' must carry the "
    "copy button, which the old assertion on bare text rejected\n"
    f"flip: {JS} (code outside the tests) -- the fake DOM gained the select and remove calls "
    "the copy button uses; no existing test reads them"
)

BASE_PY = """\
import pytest


def test_add():
    assert add(2, 2) == 4


def test_parse():
    with pytest.raises(ValueError):
        parse("x")


def test_other():
    assert other() == 1
"""


def git(root: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(root), "-c", "user.name=t", "-c", "user.email=t@t", *args],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


def make_repo(tmp_path: Path, files: dict[str, str]) -> Path:
    root = tmp_path / "repo"
    for rel, text in files.items():
        (root / rel).parent.mkdir(parents=True, exist_ok=True)
        (root / rel).write_text(text)
    git(tmp_path, "init", "-q", "-b", "main", str(root))
    git(root, "add", "-A")
    git(root, "commit", "-q", "-m", "init")
    return root


@pytest.fixture
def js_repo(tmp_path: Path) -> Path:
    base = (FIXTURES / "renderer_suite_base.txt").read_text()
    return make_repo(tmp_path, {JS: base, "src/app.js": "export const x = 1;\n"})


def head_js() -> str:
    return (FIXTURES / "renderer_suite_head.txt").read_text()


@pytest.fixture
def py_repo(tmp_path: Path) -> Path:
    return make_repo(tmp_path, {"tests/test_calc.py": BASE_PY, "calc.py": "def add(a, b): pass\n"})


# -- the recorded defect ------------------------------------------------------------


def test_the_recorded_rewrite_is_refused_and_names_the_test(js_repo: Path) -> None:
    (js_repo / JS).write_text(head_js())
    found = flip_finding(js_repo, "HEAD", "")
    assert found is not None
    assert (found.gate, found.verdict, found.tier) == (TEST_CHANGES, "fail", 1)
    assert TITLE in found.detail
    assert "flip: <exact test name> -- <evidence>" in found.detail


def test_the_same_tree_with_a_proper_flip_line_is_surfaced_for_a_person(js_repo: Path) -> None:
    (js_repo / JS).write_text(head_js())
    found = flip_finding(js_repo, "HEAD", f"Copy button\n\n{GOOD}\n")
    assert found is not None
    assert found.verdict == "not-proven"
    assert f"flipped test(s): {TITLE}" in found.detail
    assert "needs a person's review" in found.detail


def test_the_same_tree_with_the_circular_line_is_refused(js_repo: Path) -> None:
    (js_repo / JS).write_text(head_js())
    line = f"flip: {TITLE} -- the new code fails the old test"
    found = flip_finding(js_repo, "HEAD", line)
    assert found is not None
    assert found.verdict == "fail"
    assert tc.CIRCULAR_SAID in found.detail
    empty = flip_finding(js_repo, "HEAD", f"flip: {TITLE}")
    assert empty is not None
    assert tc.NO_EVIDENCE_SAID in empty.detail


def test_a_label_for_another_test_does_not_cover_it(js_repo: Path) -> None:
    (js_repo / JS).write_text(head_js())
    found = flip_finding(js_repo, "HEAD", "flip: some other test -- a real reason with words")
    assert found is not None
    assert found.verdict == "fail"


def test_an_untouched_tree_and_an_added_test_say_nothing(js_repo: Path) -> None:
    assert flip_finding(js_repo, "HEAD", "") is None
    base = (js_repo / JS).read_text()
    (js_repo / JS).write_text(base + '\ntest("extra", () => { assert.ok(1); });\n')
    assert flip_finding(js_repo, "HEAD", "") is None


def test_a_committed_rewrite_reads_its_label_from_the_commit_message(js_repo: Path) -> None:
    (js_repo / JS).write_text(head_js())
    git(js_repo, "add", "-A")
    git(js_repo, "commit", "-q", "-m", f"Copy button\n\n{GOOD}")
    assert GOOD in commit_messages(js_repo, "HEAD~1")
    found = flip_finding(js_repo, "HEAD~1", commit_messages(js_repo, "HEAD~1"))
    assert found is not None
    assert found.verdict == "not-proven"
    assert commit_messages(js_repo, "HEAD") == ""


# -- Python, over a tree --------------------------------------------------------------


def edited(root: Path, old: str, new: str) -> None:
    path = root / "tests" / "test_calc.py"
    path.write_text(path.read_text().replace(old, new))


def test_each_python_loosening_is_refused_and_each_tightening_is_not(py_repo: Path) -> None:
    path = py_repo / "tests" / "test_calc.py"
    cases = {
        "assertion changed": ("add(2, 2) == 4", "add(2, 2) == 5", "test_add"),
        "raises removed": (
            'with pytest.raises(ValueError):\n        parse("x")',
            'parse("x")',
            "test_parse",
        ),
        "deleted": ("def test_other():\n    assert other() == 1\n", "", "test_other"),
        "renamed": ("def test_other", "def test_the_other", "test_other"),
    }
    for label, (old, new, name) in cases.items():
        path.write_text(BASE_PY.replace(old, new))
        found = flip_finding(py_repo, "HEAD", "")
        assert found is not None, label
        assert found.verdict == "fail", label
        assert repr(name) in found.detail, label
    path.write_text(
        BASE_PY.replace("add(2, 2) == 4\n", "add(2, 2) == 4\n    assert add(1, 1) == 2\n")
    )
    assert flip_finding(py_repo, "HEAD", "") is None


def test_a_rewrite_the_task_ordered_needs_no_label_and_another_still_does(py_repo: Path) -> None:
    edited(py_repo, "add(2, 2) == 4", "add(2, 2) == 5")
    assert flip_finding(py_repo, "HEAD", "", ("test_add",)) is None
    found = flip_finding(py_repo, "HEAD", "", ("test_parse",))
    assert found is not None
    assert found.verdict == "fail"


def test_a_python_flip_line_with_evidence_is_surfaced_not_passed(py_repo: Path) -> None:
    edited(py_repo, "add(2, 2) == 4", "add(2, 2) == 5")
    message = (
        "flip: test_add -- add(2, 2) is 5 under the new overflow rule; the old assertion said 4"
    )
    found = flip_finding(py_repo, "HEAD", message)
    assert found is not None
    assert found.verdict == "not-proven"


def test_a_test_file_the_tree_added_and_one_it_deleted_are_both_read(py_repo: Path) -> None:
    moved = BASE_PY.replace("def test_other():\n    assert other() == 1\n", "")
    (py_repo / "tests" / "test_calc.py").write_text(moved)
    (py_repo / "tests" / "test_more.py").write_text("def test_other():\n    assert other() == 1\n")
    (py_repo / "notes.txt").write_text("not a test file\n")
    assert flip_finding(py_repo, "HEAD", "") is None  # untracked, found there
    git(py_repo, "add", "-A")
    assert flip_finding(py_repo, "HEAD", "") is None  # staged-new, found there
    (py_repo / "tests" / "test_calc.py").unlink()
    found = flip_finding(py_repo, "HEAD", "")
    assert found is not None
    assert "'test_add'" in found.detail


def test_a_test_file_that_is_not_utf8_is_reported_not_skipped(py_repo: Path) -> None:
    (py_repo / "tests" / "test_calc.py").write_bytes(b"def test_add():\n    assert '\xff'\n")
    found = changed_tests(py_repo, "HEAD")
    assert [(c.name, c.kind) for c in found] == [("tests/test_calc.py", tc.UNREADABLE)]
    assert "tree: not UTF-8" in found[0].detail
    git(py_repo, "add", "-A")
    git(py_repo, "commit", "-q", "-m", "bytes")
    (py_repo / "tests" / "test_calc.py").write_text(BASE_PY)
    again = changed_tests(py_repo, "HEAD")
    assert [(c.name, c.kind) for c in again] == [("tests/test_calc.py", tc.UNREADABLE)]
    assert "baseline: not UTF-8" in again[0].detail


def test_a_baseline_git_cannot_resolve_is_an_error_not_an_empty_answer(py_repo: Path) -> None:
    with pytest.raises(AuditError, match="rev-parse"):
        flip_finding(py_repo, "no-such-rev", "")
    with pytest.raises(AuditError, match="rev-parse"):
        commit_messages(py_repo, "no-such-rev")


# -- the CLI --------------------------------------------------------------------------


def accepted() -> AuditResult:
    check = AuditCheck("tests", "pass", "1 passed", None)
    return AuditResult("accept", "t", "b" * 40, "cmd", (check,), None, "s")


def test_the_plain_audit_refuses_a_tree_that_rewrote_a_test(js_repo: Path) -> None:
    (js_repo / JS).write_text(head_js())
    got = _with_test_changes(accepted(), js_repo, "HEAD")
    assert isinstance(got, AuditResult)
    assert got.verdict == "refuse"
    assert got.checks[-1].name == TEST_CHANGES
    assert got.checks[-1].status == "fail"
    assert TITLE in got.checks[-1].detail


def test_the_plain_audit_accepts_a_committed_flip_but_says_it_is_not_proven(js_repo: Path) -> None:
    (js_repo / JS).write_text(head_js())
    git(js_repo, "add", "-A")
    git(js_repo, "commit", "-q", "-m", f"Copy button\n\n{GOOD}")
    got = _with_test_changes(accepted(), js_repo, "HEAD~1")
    assert isinstance(got, AuditResult)
    assert got.verdict == "accept"
    assert got.checks[-1].status == "pass"
    assert got.checks[-1].detail.startswith("not proven: flipped test(s)")


def test_the_tiered_audit_gains_a_test_changes_tier_only_when_something_changed(
    js_repo: Path,
) -> None:
    quiet: tuple[Findings, ...] = (Findings(tier=1, key="k", findings=()),)
    assert _with_test_changes(quiet, js_repo, "HEAD") == quiet
    (js_repo / JS).write_text(head_js())
    got = _with_test_changes(quiet, js_repo, "HEAD")
    assert isinstance(got, tuple)
    assert len(got) == 2
    assert not got[1].passed
    assert got[1].findings[0].gate == TEST_CHANGES


def test_nothing_to_audit_stays_nothing_to_audit(js_repo: Path) -> None:
    nothing = AuditResult("nothing-to-audit", "t", "b" * 40, "cmd", (), None, "s")
    assert _with_test_changes(nothing, js_repo, "HEAD") is nothing


# -- the feed and the engine ----------------------------------------------------------


@pytest.fixture
def calc_repo(tmp_path: Path) -> Path:
    from test_feed import BUGGY, TEST

    return make_repo(
        tmp_path, {"calc.py": BUGGY.replace("a - b", "a + b"), "tests/test_calc.py": TEST}
    )


EDIT_TEST = call(
    "edit_file", "t1", path="tests/test_calc.py", old="add(2, 2) == 4", new="add(2, 3) == 5"
)
LABELLED_SUMMARY = (
    "Changed a pin.\n\nflip: test_add -- the spec says add(2, 3) is 5; the old input 2 + 2 hid it"
)
LABELLED = call("finish", "f2", summary=LABELLED_SUMMARY)
CIRCULAR = call("finish", "f3", summary="x\n\nflip: test_add -- the new code fails the old test")


def feed_for(root: Path, tmp_path: Path, **kwargs: Any) -> tuple[AuditFeed, FakeAuditor]:
    fake = FakeAuditor()
    fed = AuditFeed(root, "HEAD", tmp_path / "j.jsonl", "s", auditor=fake, **kwargs)
    return fed, fake


def test_finish_is_refused_before_the_suite_when_a_changed_test_has_no_label(
    calc_repo: Path, tmp_path: Path
) -> None:
    edited(calc_repo, "add(2, 2) == 4", "add(2, 3) == 5")
    fed, fake = feed_for(calc_repo, tmp_path)
    fed.tell_summary("done")
    accepted_, text = fed.final()
    assert not accepted_
    assert fed.results[-1].note == FLIPS_FIRST
    assert "test_add" in text
    assert "flip: <exact test name> -- <evidence>" in text
    assert [tier for tier, _ in fake.calls if tier != 0] == []  # the suite never ran


def test_the_same_tree_is_accepted_once_the_summary_carries_the_label(
    calc_repo: Path, tmp_path: Path
) -> None:
    edited(calc_repo, "add(2, 2) == 4", "add(2, 3) == 5")
    fed, _ = feed_for(calc_repo, tmp_path)
    fed.tell_summary("done")
    assert not fed.final()[0]
    fed.tell_summary(LABELLED_SUMMARY)
    ok, text = fed.final()
    assert ok  # a labelled flip refuses nothing
    assert "(not proven, does not refuse) test-changes (tier 1): flipped test(s): test_add" in text
    assert fed.results[-1].passed
    fed.tell_summary("again\n\nflip: test_add -- the new code fails the old test")
    assert not fed.final()[0]


def test_the_feed_passes_the_tasks_sanctioned_rewrites_to_the_check(
    calc_repo: Path, tmp_path: Path
) -> None:
    edited(calc_repo, "add(2, 2) == 4", "add(2, 3) == 5")
    fed, _ = feed_for(calc_repo, tmp_path, sanctioned_test_rewrites=("test_add",))
    fed.tell_summary("done")
    assert fed.final() == (True, "")
    assert TEST_CHANGES not in [f.gate for f in fed.results[-1].findings]


def test_without_feedback_the_finding_is_recorded_and_the_suite_still_runs(
    calc_repo: Path, tmp_path: Path
) -> None:
    edited(calc_repo, "add(2, 2) == 4", "add(2, 3) == 5")
    fed, fake = feed_for(calc_repo, tmp_path, feedback=False)
    fed.tell_summary("done")
    assert fed.final() == (True, "")
    result = fed.results[-1]
    assert result.note == ""
    assert not result.passed
    assert TEST_CHANGES in [f.gate for f in result.findings]
    assert {tier for tier, _ in fake.calls} >= {1, 2}


def test_a_checkpoint_tells_the_model_early(calc_repo: Path, tmp_path: Path) -> None:
    edited(calc_repo, "add(2, 2) == 4", "add(2, 3) == 5")
    fed, _ = feed_for(calc_repo, tmp_path)
    scratch = tmp_path / "scratch"
    scratch.mkdir()
    got = fed._audit("checkpoint 1", (1,), calc_repo, scratch)
    assert got is not None
    gates = [f.gate for f in got.findings]
    assert gates == ["tests", TEST_CHANGES]
    assert "flip:" in render(got)


def test_a_run_that_rewrites_a_test_finishes_only_with_a_label(
    calc_repo: Path, tmp_path: Path
) -> None:
    plain = Reactive([[EDIT_TEST], [call("finish", "f0", summary="done")]], tail=[LABELLED])
    result, _ = run(calc_repo, plain, "E+A+F", allow_test_edits=True)
    assert result.outcome == "finished"
    assert any("flip: <exact test name>" in seen for seen in plain.seen)


def test_a_run_whose_label_is_circular_is_refused_until_the_cap(calc_repo: Path) -> None:
    stuck = Reactive([[EDIT_TEST]], tail=[CIRCULAR])
    result, _ = run(calc_repo, stuck, "E+A+F", allow_test_edits=True, finish_refusal_cap=2)
    assert result.outcome == "stopped"
    assert any(tc.CIRCULAR_SAID in seen for seen in stuck.seen)


def test_a_fake_that_never_hears_the_summary_changes_nothing(calc_repo: Path) -> None:
    quiet = Reactive([[EDIT_COMMENT], [call("finish", "f", summary="done")]])
    result, _ = run(calc_repo, quiet, "E+A+F")
    assert result.outcome == "finished"
