"""The three hooks in `tools/githooks`, driven by git itself in a temporary repo.

`test_leakguard.py` tests the guard's logic in-process. This file tests what
that cannot see: the shell scripts. A hook that exits 0 before it reaches
Python, calls the wrong mode, or drops the arguments git passes it leaves every
in-process test green and publishes the leak, so each hook is run by real
`git commit` and `git push` (to a local bare repo) with `core.hooksPath`
pointing at the real hook files.

The contract, each half both ways:

- `commit-msg` refuses a message holding any leak shape and accepts clean text
  and the near misses beside each bad value; a refused commit does not exist;
- `pre-commit` refuses a leak in the staged content, judges the staged version
  rather than the working file, and accepts clean content;
- `pre-push` refuses a pushed commit whose message, identity or added lines
  leak and leaves the remote untouched, accepts clean commits, and does not
  re-judge a leak the remote already has.

The environment is closed on purpose: `HOME` and the git config files are
isolated and `SADDLE_LEAK_PATTERNS` names a file under `tmp_path`, so the
owner's private patterns and git identity never enter a test. Known-bad values
come from `test_leakguard`, which builds them from fragments so the guard can
scan this file.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest
from test_leakguard import BAD, FINDING, GOOD, HOST_LAN, PEM

from saddle.leakguard import EXIT_CLEAN, EXIT_HITS, PATTERNS_ENV

ROOT = Path(__file__).resolve().parents[1]
HOOKS = ROOT / "tools" / "githooks"
VENV_BIN = Path(sys.executable).parent


class Sandbox:
    """A temporary repo whose hooks are the real ones, in a closed environment."""

    def __init__(self, tmp_path: Path) -> None:
        self.tmp = tmp_path
        self.patterns = tmp_path / "leak-patterns.txt"
        self.patterns.write_text("")
        home = tmp_path / "home"
        home.mkdir()
        self.env = {
            "PATH": f"{VENV_BIN}:{os.environ['PATH']}",
            "HOME": str(home),
            "GIT_CONFIG_GLOBAL": "/dev/null",
            "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_TERMINAL_PROMPT": "0",
            "LANG": "C.UTF-8",
            PATTERNS_ENV: str(self.patterns),
        }
        self.repo = tmp_path / "repo"
        self.repo.mkdir()
        self.git("init", "-q", "-b", "main")
        self.git("config", "core.hooksPath", str(HOOKS))
        self.git("config", "commit.gpgsign", "false")

    def git(
        self, *args: str, email: str = "t@example.com", cwd: Path | None = None
    ) -> subprocess.CompletedProcess[str]:
        head = ["git", "-c", "user.name=t", "-c", f"user.email={email}"]
        return subprocess.run(
            [*head, *args],
            cwd=cwd or self.repo,
            env=self.env,
            capture_output=True,
            text=True,
            check=False,
        )

    def stage(self, name: str, content: str) -> None:
        (self.repo / name).write_text(content)
        assert self.git("add", name).returncode == 0

    def commit(self, message: str, *flags: str, email: str = "t@example.com") -> str:
        """Commit what is staged, hooks bypassed by `--no-verify`; returns the new sha."""
        done = self.git("commit", "-q", *flags, "-m", message, email=email)
        assert done.returncode == 0, done.stderr
        return self.git("rev-parse", "HEAD").stdout.strip()

    def commit_count(self) -> int:
        done = self.git("rev-list", "--count", "HEAD")
        return int(done.stdout) if done.returncode == 0 else 0

    def remote(self) -> Path:
        """A bare `origin`; the repo is already pointed at it."""
        bare = self.tmp / "origin.git"
        subprocess.run(["git", "init", "-q", "--bare", str(bare)], check=True, env=self.env)
        assert self.git("remote", "add", "origin", str(bare)).returncode == 0
        return bare

    def remote_head(self, bare: Path) -> str:
        done = self.git("rev-parse", "--verify", "-q", "refs/heads/main", cwd=bare)
        return done.stdout.strip()

    def hook(self, name: str, *args: str, stdin: str = "") -> subprocess.CompletedProcess[str]:
        """Run one hook script directly, as git would, from the repo."""
        return subprocess.run(
            [str(HOOKS / name), *args],
            cwd=self.repo,
            env=self.env,
            input=stdin,
            capture_output=True,
            text=True,
            check=False,
        )


@pytest.fixture
def box(tmp_path: Path) -> Sandbox:
    return Sandbox(tmp_path)


# -- commit-msg --------------------------------------------------------------


@pytest.mark.parametrize(("rule", "value"), BAD, ids=[f"{r}:{v[:14]}" for r, v in BAD])
def test_commit_msg_refuses_each_leak_shape(box: Sandbox, rule: str, value: str) -> None:
    msg = box.tmp / "MSG"
    msg.write_text(f"subject\n\nbody mentions {value} here\n")
    done = box.hook("commit-msg", str(msg))
    assert done.returncode == EXIT_HITS, done.stderr
    assert rule in done.stderr


@pytest.mark.parametrize("value", GOOD)
def test_commit_msg_accepts_the_near_misses(box: Sandbox, value: str) -> None:
    msg = box.tmp / "MSG"
    msg.write_text(f"subject\n\nbody mentions {value} here\n")
    done = box.hook("commit-msg", str(msg))
    assert done.returncode == EXIT_CLEAN, done.stderr


def test_commit_msg_refuses_through_git_and_the_refused_commit_does_not_exist(box: Sandbox) -> None:
    box.stage("a.md", "clean\n")
    refused = box.git("commit", "-q", "-m", f"clean subject\n\nsee {FINDING}")
    assert refused.returncode != 0
    assert "finding-ref" in refused.stderr
    assert box.commit_count() == 0  # the refused commit does not exist
    assert box.git("commit", "-q", "-m", "clean subject").returncode == 0
    assert box.commit_count() == 1


def test_commit_msg_applies_the_owners_private_patterns(box: Sandbox) -> None:
    box.patterns.write_text("zebra-codename\n")
    box.stage("a.md", "clean\n")
    refused = box.git("commit", "-q", "-m", "ship zebra-codename")
    assert refused.returncode != 0
    assert "private:1" in refused.stderr
    assert box.git("commit", "-q", "-m", "ship the thing").returncode == 0


# -- pre-commit --------------------------------------------------------------


@pytest.mark.parametrize(("rule", "value"), BAD, ids=[f"{r}:{v[:14]}" for r, v in BAD])
def test_pre_commit_refuses_each_leak_shape_in_staged_content(
    box: Sandbox, rule: str, value: str
) -> None:
    box.stage("notes.md", f"first\n{value}\n")
    done = box.hook("pre-commit")
    assert done.returncode == EXIT_HITS, done.stderr
    assert f"notes.md:2: {rule}" in done.stderr


@pytest.mark.parametrize("value", GOOD)
def test_pre_commit_accepts_the_near_misses(box: Sandbox, value: str) -> None:
    box.stage("notes.md", f"first\n{value}\n")
    assert box.hook("pre-commit").returncode == EXIT_CLEAN


def test_pre_commit_judges_the_staged_version_not_the_working_file(box: Sandbox) -> None:
    box.stage("a.md", f"{PEM}\n")
    (box.repo / "a.md").write_text("clean now\n")  # fixed on disk, still leaking in the index
    refused = box.git("commit", "-q", "-m", "subject")
    assert refused.returncode != 0
    assert "private-key" in refused.stderr
    assert box.commit_count() == 0
    box.stage("a.md", "clean now\n")
    assert box.git("commit", "-q", "-m", "subject").returncode == 0
    # Leaking on disk only, with a clean change staged: the index is what is judged.
    (box.repo / "a.md").write_text(f"{PEM}\n")
    box.stage("b.md", "clean\n")
    assert box.git("commit", "-q", "-m", "second").returncode == 0
    assert box.commit_count() == 2


# -- pre-push ----------------------------------------------------------------


def test_pre_push_refuses_a_leaking_message_and_the_remote_stays_empty(box: Sandbox) -> None:
    bare = box.remote()
    box.stage("a.md", "clean\n")
    box.commit(f"subject\n\nsee {FINDING}", "--no-verify")
    refused = box.git("push", "-q", "origin", "main")
    assert refused.returncode != 0
    assert " message:3: finding-ref" in refused.stderr
    assert "finding-ref" in refused.stderr
    assert box.remote_head(bare) == ""


def test_pre_push_refuses_a_leaking_added_line_with_its_file_and_line(box: Sandbox) -> None:
    bare = box.remote()
    box.stage("a.md", "clean\n")
    box.commit("one", "--no-verify")
    box.stage("a.md", f"clean\n{PEM}\n")
    box.commit("two", "--no-verify")
    refused = box.git("push", "-q", "origin", "main")
    assert refused.returncode != 0
    assert "a.md:2: private-key" in refused.stderr
    assert box.remote_head(bare) == ""


def test_pre_push_refuses_a_machine_named_author_identity(box: Sandbox) -> None:
    bare = box.remote()
    box.stage("a.md", "clean\n")
    box.commit("subject", "--no-verify", email=HOST_LAN)
    refused = box.git("push", "-q", "origin", "main")
    assert refused.returncode != 0
    assert "host-email" in refused.stderr
    assert box.remote_head(bare) == ""


def test_pre_push_accepts_clean_commits_and_does_not_rejudge_remote_history(
    box: Sandbox,
) -> None:
    bare = box.remote()
    box.stage("a.md", f"{PEM}\n")
    old = box.commit(f"base {FINDING}", "--no-verify")
    # The remote already holds a leaking commit: published history is not re-judged.
    assert box.git("push", "-q", "--no-verify", "origin", "main").returncode == 0
    assert box.remote_head(bare) == old
    box.stage("b.md", "clean\n")
    new = box.commit("a clean change")
    pushed = box.git("push", "-q", "origin", "main")
    assert pushed.returncode == 0, pushed.stderr
    assert box.remote_head(bare) == new


def test_pre_push_judges_only_the_new_commits_of_a_mixed_range(box: Sandbox) -> None:
    bare = box.remote()
    box.stage("a.md", "clean\n")
    good = box.commit("fine")
    box.stage("b.md", "clean\n")
    box.commit(f"bad {FINDING}", "--no-verify")
    refused = box.git("push", "-q", "origin", "main")
    assert refused.returncode != 0
    assert box.remote_head(bare) == ""
    # Pushing only the clean first commit goes through.
    assert box.git("push", "-q", "origin", f"{good}:refs/heads/main").returncode == 0
    assert box.remote_head(bare) == good
