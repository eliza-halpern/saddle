"""The checkpoint's coverage finding in `coverage_text`'s words.

The feed showed the model a failing coverage finding as the gate wrote it,
"no test runs u.py:3", a bare line list. This puts the `coverage_text`
wording in the feed event itself: which function each uncovered
line is in and what that function says it is for. The packet already had
it; the model, who can act on it, did not. Wording only: no verdict moves.
"""

from __future__ import annotations

import json
from pathlib import Path

from saddle.auditor import Finding
from saddle.evidence import run_argv
from saddle.feed import AuditFeed, AuditResult, render
from saddle.journal import attempt_sidecar_path, read_spans

UNCOVERED = 'def g():\n    """Return the rate."""\n    return 7\n'
WORDED = (
    'u.py g: 2 of 2 changed lines never run -- nothing exercises g ("Return the rate.") '
    "[lines 1, 3]"
)


def _git(root: Path, *argv: str) -> None:
    assert run_argv(["git", *argv], root) == 0


def _repo(tmp_path: Path) -> Path:
    root = tmp_path / "repo"
    root.mkdir()
    for argv in (["init", "-q"], ["config", "user.email", "t@t"], ["config", "user.name", "t"]):
        _git(root, *argv)
    (root / "n.py").write_text("def f():\n    return 1\n")
    (root / "test_n.py").write_text("from n import f\n\n\ndef test_f():\n    assert f() == 1\n")
    _git(root, "add", "-A")
    _git(root, "commit", "-q", "-m", "base")
    return root


def _feed(root: Path, tmp_path: Path) -> AuditFeed:
    return AuditFeed(worktree=root, baseline="HEAD", journal=tmp_path / "j.jsonl", run_span="s")


def test_a_checkpoint_names_the_uncovered_function_and_what_it_is_for(tmp_path: Path) -> None:
    root = _repo(tmp_path)
    (root / "u.py").write_text(UNCOVERED)
    feed = _feed(root, tmp_path)
    feed.after_tool("write_file", ok=True)
    feed.before_tool("run_command")
    assert feed._pending is not None
    feed._pending.result()
    text = feed.collect()
    feed.close()
    line = next(x for x in text.splitlines() if x.startswith("- coverage (tier 1): fail"))
    # known-bad (before): "... evidence-thin: no test runs u.py:3"
    assert "no test runs u.py" not in line
    assert text.count(WORDED) == 1, text
    # the same words are sealed with the audit the model read
    span = next(s for s in read_spans(feed.journal) if s.name == "audit:delivered")
    sealed = json.loads(attempt_sidecar_path(feed.journal, span.span_id).read_text())
    assert WORDED in sealed["coverage_text"]


def test_a_covered_tree_reads_as_before_and_seals_no_wording(tmp_path: Path) -> None:
    """Known-good: nothing uncovered, so no coverage line and no new key."""
    root = _repo(tmp_path)
    (root / "n.py").write_text("def f():\n    return 1  # one\n")
    feed = _feed(root, tmp_path)
    text = feed.check()
    feed.close()
    assert "- coverage" not in text
    assert "coverage_text" not in feed.checks[-1].to_dict()


def test_render_words_only_the_coverage_finding_and_caps_it() -> None:
    cov = Finding("coverage", 1, "fail", "evidence-thin", "no test runs u.py:3", ("c",))
    tests = Finding("tests", 1, "fail", "code-wrong", "1 failed", ("t",))
    text = render(AuditResult("checkpoint 1", "t" * 40, (cov, tests), coverage="WORDS"))
    assert "- coverage (tier 1): fail, evidence-thin: WORDS" in text
    assert "- tests (tier 1): fail, code-wrong: 1 failed" in text
    # with nothing placed, the gate's own detail is what the model reads
    bare = render(AuditResult("checkpoint 1", "t" * 40, (cov,)))
    assert "evidence-thin: no test runs u.py:3" in bare
    long = render(AuditResult("finish", "t" * 40, (cov,), coverage="x" * 5000))
    assert long.endswith("x ...")


def test_a_not_proven_coverage_finding_is_worded_too(tmp_path: Path) -> None:
    """Under --tier2 shortlist the finding is not-proven, and reads the same way."""
    root = _repo(tmp_path)
    (root / "u.py").write_text(UNCOVERED)
    feed = AuditFeed(
        worktree=root,
        baseline="HEAD",
        journal=tmp_path / "j.jsonl",
        run_span="s",
        tier2="shortlist",
    )
    text = feed.check()
    feed.close()
    assert "(not proven, does not refuse) coverage (tier 1): Not proven by any test" in text
    assert WORDED in text
