"""What a person does with a finished run's branch: read it, merge it, push it, drop it.

A run leaves a branch, `saddle/auto/<run-id>`, and nothing else in the
user's checkout. These are the three things the packet card offers to do
with it, as plain git in the checkout the session points at:

- `diff`: the run's files, per file, from the branch against its base (the
  merge base with the checkout's current branch: where the run started).
- `merge`: fast-forward, or failing that cherry-pick base..branch, into the
  checkout's current branch. Refused on a dirty checkout, and refused
  unless the packet is finished *and* audited with no failed row. A git
  failure is returned verbatim, and a half-done cherry-pick is aborted.
- `merge_and_push`: `merge`, then push the current branch to the upstream
  git has configured for it (`branch.<name>.remote` and `.merge`). Refused
  before anything merges when there is no upstream, or when the repo has a
  pre-push hook: saddle runs no repo hook (a run could have planted one), so
  pushing here would skip a guard the user set up; they push from a terminal.
  A push that fails after the merge says the merge landed locally.
- `discard`: delete the branch (and the run's worktree that holds it).
  Refused when the branch is the checkout's current branch.

Every changing action needs `confirm` equal to the branch name: the page's
confirm step names the branch, and a request without it does nothing.

There is no ledger seam for user actions (the chat journal seals turns,
tool calls and run references only), so these are not sealed. Each attempt
is appended to the session's plain-text `actions.log` instead.
"""

from __future__ import annotations

import os
import re
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Final

from saddle.packet import Packet
from saddle.sandbox import HOST_GIT_GUARD

RUN_BRANCH: Final = re.compile(r"saddle/auto/[0-9a-f]{6,64}")
MERGE_KEYS: Final = ("tests", "mutation", "audit")
"""Rows whose `proven` status is an auditor verdict: at least one must be."""


class ActionRefusedError(Exception):
    """An action the page asked for and git was never run for (or failed)."""

    def __init__(self, message: str, status: int = 409) -> None:
        super().__init__(message)
        self.status = status


@dataclass(frozen=True)
class FileDiff:
    path: str
    patch: str


def git(root: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", "-C", str(root), *HOST_GIT_GUARD, *args],
        capture_output=True,
        text=True,
        check=False,
    )


def _out(root: Path, *args: str) -> str:
    done = git(root, *args)
    if done.returncode != 0:
        raise ActionRefusedError((done.stderr or done.stdout).strip() or f"git {args[0]} failed")
    return done.stdout.strip()


def run_branch(packet: Packet) -> str:
    """The run's branch, from the packet's header (`branch saddle/auto/<id>`)."""
    for item in packet.header:
        name = item.removeprefix("branch ")
        if item.startswith("branch ") and RUN_BRANCH.fullmatch(name):
            return name
    msg = "This run's ledger names no branch."
    raise ActionRefusedError(msg, 404)


def current_branch(root: Path) -> str:
    done = git(root, "symbolic-ref", "--quiet", "--short", "HEAD")
    return done.stdout.strip() if done.returncode == 0 else ""


def branch_exists(root: Path, branch: str) -> bool:
    return git(root, "rev-parse", "--verify", "--quiet", f"refs/heads/{branch}").returncode == 0


def base_of(root: Path, branch: str) -> str:
    return _out(root, "merge-base", "HEAD", branch)


def dirty(root: Path) -> str:
    """`git status --porcelain` of the checkout: empty when it is clean."""
    return _out(root, "status", "--porcelain", "--untracked-files=normal")


def merge_refusal(packet: Packet) -> str:
    """Why this packet may not be merged, or "" if it may."""
    if packet.verdict != "finished":
        state = "needing you" if packet.verdict == "needs_you" else packet.verdict
        return f"The run is {state}, not finished; only a finished, audited run merges."
    failed = [r.title for r in packet.rows if r.status == "failed"]
    if failed:
        return f"Failed on the record: {', '.join(failed)}."
    if not any(r.key in MERGE_KEYS and r.status == "proven" for r in packet.rows):
        return "No auditor verdict covers the change; only a finished, audited run merges."
    return ""


def diff(root: Path, branch: str) -> list[FileDiff]:
    """The run's changes, one entry per file, base..branch."""
    base = base_of(root, branch)
    names = [n for n in _out(root, "diff", "--name-only", f"{base}..{branch}").splitlines() if n]
    return [FileDiff(n, _out(root, "diff", f"{base}..{branch}", "--", n)) for n in names]


def merge(root: Path, packet: Packet, branch: str, confirm: str) -> str:
    """Merge the run's branch into the current branch; the git output on success."""
    if confirm != branch:
        msg = "Not confirmed: the confirm step must name the branch."
        raise ActionRefusedError(msg, 400)
    why = merge_refusal(packet)
    if why:
        raise ActionRefusedError(why)
    target = current_branch(root)
    if not target:
        msg = "The checkout is on a detached HEAD; there is no branch to merge into."
        raise ActionRefusedError(msg)
    status = dirty(root)
    if status:
        msg = f"The checkout has uncommitted changes; commit or stash them first.\n{status}"
        raise ActionRefusedError(msg)
    base = base_of(root, branch)
    done = git(root, "merge", "--ff-only", branch)
    if done.returncode == 0:
        return f"Fast-forwarded {target} to {branch}.\n{done.stdout.strip()}"
    picked = git(root, "cherry-pick", f"{base}..{branch}")
    if picked.returncode == 0:
        return f"Cherry-picked {base[:10]}..{branch} onto {target}.\n{picked.stdout.strip()}"
    git(root, "cherry-pick", "--abort")
    raise ActionRefusedError((picked.stderr + picked.stdout).strip())


def upstream(root: Path) -> tuple[str, str]:
    """(remote, ref) the current branch pushes to, from its config; ("", "") if none."""
    target = current_branch(root)
    if not target:
        return "", ""
    remote = git(root, "config", "--get", f"branch.{target}.remote").stdout.strip()
    ref = git(root, "config", "--get", f"branch.{target}.merge").stdout.strip()
    return (remote, ref) if remote and ref else ("", "")


def upstream_name(root: Path) -> str:
    """The upstream as a person reads it (`origin/main`), or "" when none."""
    remote, ref = upstream(root)
    return f"{remote}/{ref.removeprefix('refs/heads/')}" if remote else ""


def pre_push_hook(root: Path) -> str:
    """The repo's pre-push hook's path if it has one git would run, else "".

    Asked without `HOST_GIT_GUARD`, whose `core.hooksPath=/dev/null` would
    hide the very folder this looks for; `--git-path hooks` follows the
    repo's `core.hooksPath` itself, and reading a path runs no hook."""
    done = subprocess.run(
        ["git", "-C", str(root), "-c", "core.fsmonitor=", "rev-parse", "--git-path", "hooks"],
        capture_output=True,
        text=True,
        check=False,
    )
    folder = Path(done.stdout.strip()).expanduser()
    hook = (folder if folder.is_absolute() else root / folder) / "pre-push"
    return str(hook) if hook.is_file() and os.access(hook, os.X_OK) else ""


def push_refusal(root: Path) -> str:
    """Why merge-and-push is off for this checkout, or "" if it may push."""
    if not upstream_name(root):
        return "The current branch has no upstream to push to."
    hook = pre_push_hook(root)
    if hook:
        return (
            f"This repo has a pre-push hook ({hook}). saddle runs no repo hook, "
            "so push from a terminal, where it runs."
        )
    return ""


def merge_and_push(root: Path, packet: Packet, branch: str, confirm: str) -> str:
    """`merge`, then push the current branch to its upstream; the git output."""
    if confirm != branch:
        msg = "Not confirmed: the confirm step must name the branch."
        raise ActionRefusedError(msg, 400)
    why = push_refusal(root)
    if why:
        raise ActionRefusedError(why)
    remote, ref = upstream(root)
    name = upstream_name(root)
    merged = merge(root, packet, branch, confirm)
    pushed = git(root, "push", remote, f"HEAD:{ref}")
    said = (pushed.stderr + pushed.stdout).strip()
    if pushed.returncode != 0:
        failed = f"Merged locally, but the push to {name} failed; nothing was pushed."
        msg = f"{merged}\n\n{failed}\n{said}"
        raise ActionRefusedError(msg, 502)
    return f"{merged}\nPushed to {name}.\n{said}".rstrip()


def _worktree_of(root: Path, branch: str) -> str:
    path = ""
    for line in _out(root, "worktree", "list", "--porcelain").splitlines():
        if line.startswith("worktree "):
            path = line.removeprefix("worktree ")
        elif line == f"branch refs/heads/{branch}":
            return path
    return ""


def discard(root: Path, branch: str, confirm: str) -> str:
    """Delete the run's branch and the worktree holding it; the git output."""
    if confirm != branch:
        msg = "Not confirmed: the confirm step must name the branch."
        raise ActionRefusedError(msg, 400)
    if branch == current_branch(root):
        msg = f"{branch} is the checkout's current branch; switch away first."
        raise ActionRefusedError(msg)
    if not branch_exists(root, branch):
        msg = f"There is no branch {branch} (already discarded?)."
        raise ActionRefusedError(msg, 404)
    held = _worktree_of(root, branch)
    if held:
        if (root.resolve() / ".saddle" / "worktrees") not in Path(held).resolve().parents:
            msg = f"{branch} is checked out in {held}, not a run worktree."
            raise ActionRefusedError(msg)
        _out(root, "worktree", "remove", "--force", held)
    return _out(root, "branch", "-D", branch)


def log_action(log: Path, run_id: str, action: str, outcome: str) -> None:
    """One line per attempt in the session's `actions.log` (not a ledger record)."""
    first = outcome.strip().splitlines()[0] if outcome.strip() else ""
    stamp = time.strftime("%Y-%m-%dT%H:%M:%S%z")
    with log.open("a", encoding="utf-8") as out:
        out.write(f"{stamp}\t{run_id}\t{action}\t{first}\n")
