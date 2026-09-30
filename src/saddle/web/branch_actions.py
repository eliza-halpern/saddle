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
- `approve_merge`: land a run the self-guard held (its finish audit passed but
  it changed saddle's judges). Only a person may: the confirm names the branch
  and the guarded files, and the log records who approved.
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

from saddle import leakguard
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
    return _land(root, branch)


def _land(root: Path, branch: str) -> str:
    """Fast-forward the current branch to `branch`, or cherry-pick its commits;
    refused on a detached HEAD or a dirty checkout, and a failed pick is aborted."""
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


def approve_refusal(packet: Packet) -> str:
    """Why this packet may not be approved and merged, or "" if it may.

    Only a run the self-guard held is approved this way: its finish audit
    accepted the tree, but it changed one of saddle's judges, which the model
    may never approve for itself. A person may, when the record shows no failed
    check and an auditor verdict covers the change -- the same bar as Merge,
    without the "finished" verdict the guard withholds."""
    if packet.verdict != "needs_you" or not packet.guarded_paths:
        return "Only a run held for changing saddle's judges is approved here; use Merge."
    failed = [r.title for r in packet.rows if r.status == "failed"]
    if failed:
        return f"Failed on the record: {', '.join(failed)}."
    if not any(r.key in MERGE_KEYS and r.status == "proven" for r in packet.rows):
        return "No auditor verdict covers the change; it cannot be approved."
    return ""


def approver(root: Path) -> str:
    """The person approving, as the checkout's git identity names them."""
    name = git(root, "config", "--get", "user.name").stdout.strip()
    email = git(root, "config", "--get", "user.email").stdout.strip()
    who = f"{name} <{email}>" if name and email else name or email
    return who or "(no git identity configured)"


def approve_merge(root: Path, packet: Packet, branch: str, confirm: str, paths: str) -> str:
    """A person lands a run the self-guard held; the git output and who approved.

    `confirm` must name the branch, as for Merge, and `paths` must name the
    guarded files the run changed, exactly as the card lists them: the person
    says what they are approving, and a request without it merges nothing."""
    if confirm != branch:
        msg = "Not confirmed: the confirm step must name the branch."
        raise ActionRefusedError(msg, 400)
    why = approve_refusal(packet)
    if why:
        raise ActionRefusedError(why)
    held = ", ".join(packet.guarded_paths)
    if paths != held:
        msg = f"Not confirmed: the approval must name the guarded files ({held})."
        raise ActionRefusedError(msg, 400)
    landed = _land(root, branch)
    return f"Approved by {approver(root)}: changes to {held}.\n{landed}"


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


LEAKGUARD_HOOK: Final = "-m saddle.leakguard pre-push"
"""What saddle's own pre-push hook runs (`tools/githooks/pre-push`): a repo whose
hook is this one gets the same guard run in-process by `push_branch`."""

DEFAULT_BRANCHES: Final = frozenset({"main", "master"})


def branch_remote(root: Path) -> str:
    """The remote the current branch pushes to: its configured one, else origin."""
    target = current_branch(root)
    configured = git(root, "config", "--get", f"branch.{target}.remote").stdout.strip()
    return configured or "origin"


def default_branch(root: Path, remote: str) -> str:
    """The remote's default branch as the checkout knows it, or "" if unknown."""
    ref = git(root, "symbolic-ref", "--quiet", "--short", f"refs/remotes/{remote}/HEAD")
    return ref.stdout.strip().removeprefix(f"{remote}/") if ref.returncode == 0 else ""


def on_main_branch(root: Path) -> bool:
    """Whether the checkout's current branch is main, master or the remote's default."""
    target = current_branch(root)
    return target in DEFAULT_BRANCHES or target == default_branch(root, branch_remote(root))


def push_branch_refusal(root: Path) -> str:
    """Why the current branch may not be pushed from the page, or "" if it may."""
    target = current_branch(root)
    if not target:
        return "The checkout is on a detached HEAD; there is no branch to push."
    remote = branch_remote(root)
    if git(root, "remote", "get-url", remote).returncode != 0:
        return f"There is no remote {remote!r} to push to."
    if on_main_branch(root):
        return f"{target} is the main branch; land it through a pull request or your terminal."
    hook = pre_push_hook(root)
    if hook and LEAKGUARD_HOOK not in Path(hook).read_text(encoding="utf-8", errors="replace"):
        return (
            f"This repo has a pre-push hook ({hook}) that is not saddle's leak guard. "
            "saddle runs no repo hook, so push from a terminal, where it runs."
        )
    return ""


def push_branch(root: Path, confirm: str) -> str:
    """Push the current branch to its remote under the same name, setting its
    upstream; git's output, which on a new branch includes the host's
    pull-request link.

    A repo whose pre-push hook is saddle's leak guard gets that guard here,
    run from saddle's own installed copy over exactly the commits the remote
    lacks -- never by executing the repo's hook file, which a run could have
    planted. Any finding refuses the push and names it."""
    why = push_branch_refusal(root)
    if why:
        raise ActionRefusedError(why)
    target = current_branch(root)
    if confirm != target:
        msg = "Not confirmed: the confirm step must name the branch being pushed."
        raise ActionRefusedError(msg, 400)
    remote = branch_remote(root)
    hook = pre_push_hook(root)
    if hook:
        try:
            rules = (
                *leakguard.BUILTIN_RULES,
                *leakguard.load_private_rules(leakguard.patterns_path()),
            )
        except leakguard.GuardConfigError as exc:
            msg = f"The leak guard cannot start: {exc}"
            raise ActionRefusedError(msg) from exc
        head = _out(root, "rev-parse", "HEAD")
        there = git(root, "rev-parse", "--verify", "--quiet", f"refs/remotes/{remote}/{target}")
        old = there.stdout.strip() if there.returncode == 0 else leakguard.ZERO_SHA
        update = f"refs/heads/{target} {head} refs/heads/{target} {old}"
        hits = leakguard.scan_push(root, remote, [update], rules)
        if hits:
            named = "\n".join(hit.render() for hit in hits[:10])
            msg = f"The leak guard refused the push:\n{named}"
            raise ActionRefusedError(msg)
    pushed = git(root, "push", "--set-upstream", remote, f"HEAD:refs/heads/{target}")
    said = (pushed.stderr + pushed.stdout).strip()
    if pushed.returncode != 0:
        raise ActionRefusedError(said or "git push failed", 502)
    guarded = " (leak guard: clean)" if hook else ""
    return f"Pushed {target} to {remote}{guarded}.\n{said}"


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
