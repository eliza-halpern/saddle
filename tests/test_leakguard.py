"""The leak guard (`saddle.leakguard`) and its hooks.

The contract, each half both ways: a commit or push that would publish a
match of a rule is refused -- in staged content, in the commit message, in
author/committer identities and in the lines a pushed commit adds -- and
text that matches no rule, or matches one only as an exact known test fake,
passes. A push is judged only on commits the remote does not already have.

Every known-bad value below is assembled from fragments at run time: the
guard scans this file too, and must not refuse it.
"""

from __future__ import annotations

import io
import os
import subprocess
from pathlib import Path

import pytest

from saddle import leakguard
from saddle.leakguard import (
    ALLOWED_VALUES,
    BUILTIN_RULES,
    EXIT_CLEAN,
    EXIT_CONFIG,
    EXIT_HITS,
    ZERO_SHA,
    GuardConfigError,
    Hit,
    added_lines,
    commits_to_push,
    load_private_rules,
    main,
    patterns_path,
    scan_message,
    scan_text,
)

HOOKS = Path(__file__).resolve().parents[1] / "tools" / "githooks"

# Known-bad values, one or more per rule, never written whole in this file.
HOME = "/" + "home/alice/src/x.py"
USERS = "/" + "Users/bob/notes"
SCRATCH = "/tmp/" + "claude-1001/scratch"
HOST_LAN = "me" + "@box.lan"
HOST_NONE = "me" + "@box.(none)"
HOST_ROUTER = "me" + "@pc.mynetworksettings.com"
HOST_COMCAST = "me" + "@c-1-2-3-4.hsd1.pa.comcast.net"
AWS = "AKIA" + "Q7" * 8
API = "sk-" + "proj-" + "Zx9" * 8
PEM_RSA = "-----BEGIN " + "RSA PRIVATE KEY-----"
PEM = "-----BEGIN " + "PRIVATE KEY-----"
GHP = "ghp" + "_" + "a1B2" * 9
GH_PAT = "github" + "_pat_" + "c3" * 11
FINDING = "F" + "21.38"
REC = "Rec" + " 95"
RECOMMENDATION = "Recommend" + "ation 36"

BAD: list[tuple[str, str]] = [
    ("home-path", HOME),
    ("home-path", USERS),
    ("agent-scratch", SCRATCH),
    ("host-email", HOST_LAN),
    ("host-email", HOST_NONE),
    ("host-email", HOST_ROUTER),
    ("host-email", HOST_COMCAST),
    ("aws-key", AWS),
    ("api-key", API),
    ("private-key", PEM_RSA),
    ("private-key", PEM),
    ("github-token", GHP),
    ("github-token", GH_PAT),
    ("finding-ref", FINDING),
    ("rec-ref", REC),
    ("rec-ref", RECOMMENDATION),
]

# Known-good near misses: each is one step from a bad instance above.
GOOD: list[str] = [
    "/home/<user>/src",  # placeholder, not an account
    "~/.config/saddle/leak-patterns.txt",
    "/tmp/cache/claude",
    "me@example.com",
    "user+tag@gmail.com",
    "a@comcast.net",  # a real mailbox domain
    "saddle@localhost",  # a bare suffix names no machine
    "user@example",  # dotless validator fragment
    "uses: actions/checkout@v4",
    "AKIA" + "Q7" * 7,  # one character short
    "task-firstabcdefghijklmnop",  # `sk-` inside a word
    "sk-short",
    "-----BEGIN PUBLIC KEY-----",
    "gh" + "p_short",
    "the F1 score",
    "version F1.2.3 of the format",
    "Record 95",
    "rec 95",
    "Recommendations follow",
    *sorted(ALLOWED_VALUES),
]


@pytest.fixture(autouse=True)
def _no_private_patterns(
    tmp_path_factory: pytest.TempPathFactory, monkeypatch: pytest.MonkeyPatch
) -> Path:
    """Keep the maintainer's real pattern file out of every test."""
    path = tmp_path_factory.mktemp("patterns") / "absent.txt"
    monkeypatch.setenv(leakguard.PATTERNS_ENV, str(path))
    return path


def git(repo: Path, *args: str, hooks: bool = False) -> str:
    """git with a fixed identity; hooks off unless the test is about them."""
    head = ["git", "-C", str(repo), "-c", "user.name=t", "-c", "user.email=t@example.com"]
    if not hooks:
        head += ["-c", "core.hooksPath=/dev/null"]
    return subprocess.run([*head, *args], capture_output=True, text=True, check=True).stdout


def new_repo(path: Path) -> Path:
    path.mkdir()
    git(path, "init", "-q", "-b", "main")
    return path


def commit(repo: Path, name: str, content: str, message: str = "change") -> str:
    (repo / name).write_text(content)
    git(repo, "add", name)
    git(repo, "commit", "-q", "-m", message)
    return git(repo, "rev-parse", "HEAD").strip()


def rules_hit(text: str) -> set[str]:
    return {hit.rule for hit in scan_text(text, "x", BUILTIN_RULES)}


# -- the rules, both halves -------------------------------------------------


@pytest.mark.parametrize(("rule", "value"), BAD)
def test_each_rule_refuses_its_known_bad(rule: str, value: str) -> None:
    assert rules_hit(f"text before {value} and after") == {rule}


@pytest.mark.parametrize("value", GOOD)
def test_known_good_near_misses_pass(value: str) -> None:
    assert rules_hit(f"text before {value} and after") == set()


def test_every_builtin_rule_has_a_known_bad() -> None:
    assert {rule.name for rule in BUILTIN_RULES} == {rule for rule, _ in BAD}


def test_host_domain_judges_the_domain_not_the_mailbox() -> None:
    assert leakguard.is_host_domain(HOST_LAN)
    assert not leakguard.is_host_domain("lan@example.com")


def test_the_guard_source_holds_no_instance_of_its_own_rules() -> None:
    """The guard is public: it must not publish what it refuses."""
    source = Path(leakguard.__file__).read_text()
    assert scan_text(source, "leakguard.py", BUILTIN_RULES) == []


# -- the allowlist is by exact value, never by place -------------------------


def test_allowlisted_fake_passes_anywhere_and_a_real_key_beside_it_fails(tmp_path: Path) -> None:
    repo = new_repo(tmp_path / "r")
    fixture = "tests/fixtures/keys.txt"
    (repo / "tests" / "fixtures").mkdir(parents=True)
    (repo / fixture).write_text(f"fake AKIAABCDEFGHIJKLMNOP\nreal {AWS}\n")
    git(repo, "add", fixture)
    hits = leakguard.scan_staged(repo, BUILTIN_RULES)
    assert hits == [Hit(fixture, 2, "aws-key", AWS)]


def test_an_allowlisted_value_inside_a_longer_match_is_not_allowed() -> None:
    longer = "sk-abcdefghijkl" + "MORE"
    assert rules_hit(longer) == {"api-key"}


# -- the private pattern file ------------------------------------------------


def test_patterns_path_defaults_to_the_config_file(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(leakguard.PATTERNS_ENV)
    assert patterns_path() == Path("~/.config/saddle/leak-patterns.txt").expanduser()


def test_private_patterns_are_read_and_refuse_their_literals(tmp_path: Path) -> None:
    path = tmp_path / "p.txt"
    path.write_text("# private names\n\n  secret" + "proj\\b  \n")
    rules = load_private_rules(path)
    assert [rule.name for rule in rules] == ["private:3"]
    assert scan_text("see secret" + "proj here", "x", rules) == [
        Hit("x", 1, "private:3", "secret" + "proj")
    ]
    assert scan_text("see secretproject here", "x", rules) == []


def test_an_absent_pattern_file_is_no_rules(tmp_path: Path) -> None:
    assert load_private_rules(tmp_path / "none.txt") == ()


def test_an_invalid_pattern_is_an_error_not_silence(tmp_path: Path) -> None:
    path = tmp_path / "p.txt"
    path.write_text("ok\n(unclosed\n")
    with pytest.raises(GuardConfigError, match=r"p\.txt:2:"):
        load_private_rules(path)


def test_an_unreadable_pattern_file_is_an_error(tmp_path: Path) -> None:
    with pytest.raises(GuardConfigError):
        load_private_rules(tmp_path)  # a directory: exists, cannot be read


def test_main_applies_the_private_file_to_a_commit_message(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    patterns = tmp_path / "p.txt"
    patterns.write_text("hidden" + "repo\n")
    monkeypatch.setenv(leakguard.PATTERNS_ENV, str(patterns))
    message = tmp_path / "MSG"
    message.write_text("fix: see ../hidden" + "repo/notes\n")
    out = io.StringIO()
    assert main(["commit-msg", str(message)], out=out) == EXIT_HITS
    assert "private:1: hidden" + "repo" in out.getvalue()
    assert "no private pattern file" not in out.getvalue()


def test_main_reports_an_unusable_pattern_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    patterns = tmp_path / "p.txt"
    patterns.write_text("[\n")
    monkeypatch.setenv(leakguard.PATTERNS_ENV, str(patterns))
    message = tmp_path / "MSG"
    message.write_text("fine\n")
    out = io.StringIO()
    assert main(["commit-msg", str(message)], out=out) == EXIT_CONFIG
    assert "unusable private pattern file" in out.getvalue()


def test_main_announces_a_missing_pattern_file(tmp_path: Path) -> None:
    message = tmp_path / "MSG"
    message.write_text("fine\n")
    out = io.StringIO()
    assert main(["commit-msg", str(message)], out=out) == EXIT_CLEAN
    assert "no private pattern file at" in out.getvalue()


# -- commit messages ---------------------------------------------------------


def test_commit_msg_refuses_a_bad_message_and_passes_a_good_one(tmp_path: Path) -> None:
    bad, good = tmp_path / "BAD", tmp_path / "GOOD"
    bad.write_text(f"gates: close {FINDING}\n\nbody\n")
    good.write_text("gates: close the finding\n\nbody\n")
    out = io.StringIO()
    assert main(["commit-msg", str(bad)], out=out) == EXIT_HITS
    assert f"commit message:1: finding-ref: {FINDING}" in out.getvalue()
    assert "refused, 1 hit(s): finding-ref 1" in out.getvalue()
    assert main(["commit-msg", str(good)], out=io.StringIO()) == EXIT_CLEAN


def test_commit_msg_ignores_comment_lines_git_strips() -> None:
    assert scan_message(f"subject\n# {HOME}\n", "m", BUILTIN_RULES) == []
    assert scan_message(f"subject\n {HOME}\n", "m", BUILTIN_RULES) != []


# -- pre-commit: the index, not the worktree ---------------------------------


def test_pre_commit_scans_the_staged_version(tmp_path: Path) -> None:
    repo = new_repo(tmp_path / "r")
    (repo / "a.md").write_text(f"one\npath {HOME}\n")
    git(repo, "add", "a.md")
    (repo / "a.md").write_text("cleaned in the worktree only\n")
    out = io.StringIO()
    assert main(["--repo", str(repo), "pre-commit"], out=out) == EXIT_HITS
    assert f"a.md:2: home-path: {HOME.split('src')[0]}" in out.getvalue()
    git(repo, "add", "a.md")
    assert main(["--repo", str(repo), "pre-commit"], out=io.StringIO()) == EXIT_CLEAN


def test_pre_commit_with_nothing_staged_is_clean(tmp_path: Path) -> None:
    repo = new_repo(tmp_path / "r")
    assert leakguard.scan_staged(repo, BUILTIN_RULES) == []


def test_pre_commit_skips_a_staged_deletion(tmp_path: Path) -> None:
    repo = new_repo(tmp_path / "r")
    commit(repo, "a.md", f"{HOME}\n")  # hooks are off: history may hold anything
    git(repo, "rm", "-q", "a.md")
    assert leakguard.scan_staged(repo, BUILTIN_RULES) == []


# -- pre-push: only what the remote lacks ------------------------------------


def pushed_repo(tmp_path: Path) -> tuple[Path, str]:
    """A repo whose `origin` already holds one commit with a bad message."""
    remote = tmp_path / "remote.git"
    subprocess.run(["git", "init", "-q", "--bare", str(remote)], check=True)
    repo = new_repo(tmp_path / "r")
    git(repo, "remote", "add", "origin", str(remote))
    old = commit(repo, "a.md", "base\n", f"base, see {FINDING}")
    git(repo, "push", "-q", "origin", "main")
    return repo, old


def push_line(repo: Path, old: str) -> str:
    head = git(repo, "rev-parse", "HEAD").strip()
    return f"refs/heads/main {head} refs/heads/main {old}\n"


def test_pre_push_does_not_rejudge_history_the_remote_has(tmp_path: Path) -> None:
    repo, old = pushed_repo(tmp_path)
    commit(repo, "b.md", "fine\n", "a clean change")
    args = ["--repo", str(repo), "pre-push", "origin", "url"]
    assert main(args, stdin=io.StringIO(push_line(repo, old)), out=io.StringIO()) == EXIT_CLEAN


def test_pre_push_refuses_a_new_commit_message(tmp_path: Path) -> None:
    repo, old = pushed_repo(tmp_path)
    new = commit(repo, "b.md", "fine\n", f"clean subject\n\nper {REC}")
    out = io.StringIO()
    args = ["--repo", str(repo), "pre-push", "origin"]
    assert main(args, stdin=io.StringIO(push_line(repo, old)), out=out) == EXIT_HITS
    assert f"{new[:12]} message:3: rec-ref: {REC}\n" in out.getvalue()


def test_pre_push_refuses_added_content_with_its_path_and_line(tmp_path: Path) -> None:
    repo, old = pushed_repo(tmp_path)
    new = commit(repo, "c.md", f"one\ntwo\n{GHP}\n", "clean")
    hits = leakguard.scan_push(repo, "origin", [push_line(repo, old)], BUILTIN_RULES)
    assert hits == [Hit(f"{new[:12]}:c.md", 3, "github-token", GHP)]


def test_pre_push_refuses_a_host_identity(tmp_path: Path) -> None:
    repo, old = pushed_repo(tmp_path)
    (repo / "d.md").write_text("fine\n")
    git(repo, "add", "d.md")
    git(repo, "-c", f"user.email={HOST_NONE}", "commit", "-q", "-m", "clean")
    hits = leakguard.scan_push(repo, "origin", [push_line(repo, old)], BUILTIN_RULES)
    assert {(h.where.split()[-1], h.rule, h.value) for h in hits} == {
        ("identity", "host-email", HOST_NONE)
    }
    assert [h.line for h in hits] == [1, 2]  # author and committer


def test_pre_push_sees_an_added_line_that_looks_like_a_header(tmp_path: Path) -> None:
    repo, old = pushed_repo(tmp_path)
    commit(repo, "e.md", f"++ b/{AWS}\n", "clean")
    hits = leakguard.scan_push(repo, "origin", [push_line(repo, old)], BUILTIN_RULES)
    assert [(h.rule, h.value) for h in hits] == [("aws-key", AWS)]


def test_pre_push_new_branch_takes_only_commits_no_remote_ref_has(tmp_path: Path) -> None:
    repo, _old = pushed_repo(tmp_path)
    new = commit(repo, "b.md", "fine\n", "clean")
    line = f"refs/heads/main {new} refs/heads/topic {ZERO_SHA}\n"
    assert commits_to_push(repo, "origin", [line]) == [new]


def test_pre_push_to_a_bare_url_excludes_the_remote_old_tip(tmp_path: Path) -> None:
    """Pushing to a URL leaves no remote-tracking refs; the old tip still bounds it."""
    repo, old = pushed_repo(tmp_path)
    new = commit(repo, "b.md", "fine\n", "clean")
    assert commits_to_push(repo, "/some/url", [push_line(repo, old)]) == [new]
    assert commits_to_push(repo, "/some/url", [push_line(repo, ZERO_SHA)]) == [new, old]


def test_pre_push_with_an_unknown_old_tip_still_lists_the_new_commits(tmp_path: Path) -> None:
    repo, _old = pushed_repo(tmp_path)
    new = commit(repo, "b.md", "fine\n", "clean")
    assert commits_to_push(repo, "origin", [push_line(repo, "1" * 40)]) == [new]


def test_pre_push_ignores_deletions_and_malformed_lines(tmp_path: Path) -> None:
    repo, old = pushed_repo(tmp_path)
    commit(repo, "b.md", "fine\n", f"bad {REC}")
    lines = [f"(delete) {ZERO_SHA} refs/heads/main {old}", "garbage", ""]
    assert commits_to_push(repo, "origin", lines) == []


def test_pre_push_lists_a_commit_once_across_refs(tmp_path: Path) -> None:
    repo, old = pushed_repo(tmp_path)
    new = commit(repo, "b.md", "fine\n", "clean")
    line = push_line(repo, old)
    assert commits_to_push(repo, "origin", [line, line.replace("main\n", "other\n")]) == [new]


def test_added_lines_tracks_paths_and_line_numbers() -> None:
    patch = (
        "diff --git a/x b/x\nnew file mode 100644\n--- /dev/null\n+++ b/x\n"
        "@@ -0,0 +1,2 @@\n+one\n+two\n"
        "diff --git a/y b/y\ndeleted file mode 100644\n--- a/y\n+++ /dev/null\n"
        "@@ -1 +0,0 @@\n-gone\n"
        "diff --git a/z b/z\n--- a/z\n+++ b/z\n@@ -4,0 +5 @@\n+++ looks like a header\n"
    )
    assert list(added_lines(patch)) == [
        ("x", 1, "one"),
        ("x", 2, "two"),
        ("z", 5, "++ looks like a header"),
    ]


# -- audit modes -------------------------------------------------------------


def test_tree_and_messages_modes_report_by_rule(tmp_path: Path) -> None:
    repo = new_repo(tmp_path / "r")
    commit(repo, "a.md", f"{SCRATCH}\nfine\n", f"subject {FINDING}")
    commit(repo, "b.md", "clean\n", "clean")
    out = io.StringIO()
    assert main(["--repo", str(repo), "tree"], out=out) == EXIT_HITS
    assert f"a.md:1: agent-scratch: {SCRATCH.split('1001')[0]}" in out.getvalue()
    out = io.StringIO()
    assert main(["--repo", str(repo), "messages", "HEAD"], out=out) == EXIT_HITS
    assert "refused, 1 hit(s): finding-ref 1" in out.getvalue()
    assert main(["--repo", str(repo), "messages", "HEAD~1..HEAD"], out=io.StringIO()) == 0


# -- the installed hooks, end to end -----------------------------------------


def hooked_repo(tmp_path: Path) -> Path:
    repo = new_repo(tmp_path / "r")
    git(repo, "config", "core.hooksPath", str(HOOKS))
    return repo


def try_git(repo: Path, *args: str) -> subprocess.CompletedProcess[str]:
    head = ["git", "-C", str(repo), "-c", "user.name=t", "-c", "user.email=t@example.com"]
    return subprocess.run([*head, *args], capture_output=True, text=True, check=False)


def test_hooks_are_executable() -> None:
    for name in ("pre-commit", "commit-msg", "pre-push"):
        assert os.access(HOOKS / name, os.X_OK), name


def test_installed_hooks_refuse_bad_commits_and_admit_good_ones(tmp_path: Path) -> None:
    repo = hooked_repo(tmp_path)
    (repo / "a.md").write_text(f"{PEM}\n")
    try_git(repo, "add", "a.md")
    refused = try_git(repo, "commit", "-q", "-m", "clean")
    assert refused.returncode != 0
    assert "private-key" in refused.stderr
    (repo / "a.md").write_text("fine\n")
    try_git(repo, "add", "a.md")
    refused = try_git(repo, "commit", "-q", "-m", f"clean {RECOMMENDATION}")
    assert refused.returncode != 0
    assert "rec-ref" in refused.stderr
    assert try_git(repo, "commit", "-q", "-m", "clean").returncode == 0


def test_installed_pre_push_refuses_a_bad_message_the_remote_lacks(tmp_path: Path) -> None:
    remote = tmp_path / "remote.git"
    subprocess.run(["git", "init", "-q", "--bare", str(remote)], check=True)
    repo = hooked_repo(tmp_path)
    try_git(repo, "remote", "add", "origin", str(remote))
    (repo / "a.md").write_text("fine\n")
    try_git(repo, "add", "a.md")
    assert try_git(repo, "commit", "-q", "-m", "clean").returncode == 0
    assert try_git(repo, "push", "-q", "origin", "main").returncode == 0
    (repo / "b.md").write_text("fine\n")
    try_git(repo, "add", "b.md")
    # --no-verify skips pre-commit and commit-msg, leaving pre-push to catch it.
    assert try_git(repo, "commit", "-q", "--no-verify", "-m", f"see {FINDING}").returncode == 0
    refused = try_git(repo, "push", "-q", "origin", "main")
    assert refused.returncode != 0
    assert "finding-ref" in refused.stderr
