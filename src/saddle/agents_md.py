"""A repository's AGENTS.md: the project's own instructions for an agent.

AGENTS.md is an open convention: a Markdown file at a repository's root that
tells coding agents what its humans would tell a new contributor (how to run
the tests, which helpers exist, what not to touch). saddle reads it once, at
the start of a run, from the commit the run started from, so a run cannot
rewrite its own instructions by editing the file. A line that is only
`@path` is replaced by that file's text (relative to the file holding the
line, never outside the repository), which lets AGENTS.md share one source
with another file, as `@CONTRIBUTING.md` does.

The text goes in the system prompt, which compaction never removes, under a
token budget; a cut is said where it falls, never silent.
"""

from __future__ import annotations

import posixpath
import re
import subprocess
from pathlib import Path
from typing import Final

from saddle.memory import CHARS_PER_TOKEN

AGENTS_FILE: Final = "AGENTS.md"

MAX_TOKENS: Final = 8_000
"""Estimated tokens of project instructions a run carries. saddle's own
(AGENTS.md importing CONTRIBUTING.md) are ~4,000; the window is 131,072."""

MAX_DEPTH: Final = 4
"""How deep `@path` imports nest before one is refused."""

_IMPORT: Final = re.compile(r"^@(\S+)\s*$")

HEADING: Final = (
    "\n\nProject instructions, from the repository's {name} as committed when "
    "this run started. They are the project's own rules for anyone changing it: "
    "follow them where they bear on your task. Where one conflicts with the task "
    "or with the rules above, the task and those rules come first.\n\n"
)


def _show(worktree: Path, rev: str, path: str) -> str | None:
    shown = subprocess.run(
        ["git", "-C", str(worktree), "show", f"{rev}:{path}"],
        capture_output=True,
        text=True,
        check=False,
    )
    return shown.stdout if shown.returncode == 0 else None


def _expand(worktree: Path, rev: str, path: str, depth: int, seen: tuple[str, ...]) -> str:
    text = _show(worktree, rev, path)
    if text is None:
        return f"[{path}: not found at the run's start]"
    lines = []
    for line in text.splitlines():
        found = _IMPORT.match(line)
        if found is None:
            lines.append(line)
            continue
        target = posixpath.normpath(posixpath.join(posixpath.dirname(path), found.group(1)))
        if target.startswith("../") or target == ".." or posixpath.isabs(found.group(1)):
            lines.append(f"[{line.strip()}: outside the repository, not read]")
        elif target in seen:
            lines.append(f"[{line.strip()}: already included above]")
        elif depth >= MAX_DEPTH:
            lines.append(f"[{line.strip()}: imports nest deeper than {MAX_DEPTH}, not read]")
        else:
            lines.append(_expand(worktree, rev, target, depth + 1, (*seen, target)))
    return "\n".join(lines)


def _capped(text: str, name: str, where: str) -> str:
    tokens = len(text) // CHARS_PER_TOKEN
    if tokens <= MAX_TOKENS:
        return text
    return (
        text[: MAX_TOKENS * CHARS_PER_TOKEN].rstrip()
        + f"\n\n[{name} cut here: {MAX_TOKENS:,} of about {tokens:,} tokens"
        f" kept. {where}]"
    )


def project_instructions(worktree: Path, rev: str = "HEAD") -> str:
    """The system-prompt section for `worktree`'s AGENTS.md at `rev`, imports
    expanded and cut to `MAX_TOKENS`; "" when there is no AGENTS.md."""
    if _show(worktree, rev, AGENTS_FILE) is None:
        return ""
    text = _expand(worktree, rev, AGENTS_FILE, 1, (AGENTS_FILE,)).strip()
    where = "The rest is in the file; read it if you need it."
    return HEADING.format(name=AGENTS_FILE) + _capped(text, AGENTS_FILE, where)


LOCAL_FILE: Final = ".saddle/instructions.md"
"""The person's own notes for a repository, kept in their checkout and never
committed (saddle's `.saddle/.gitignore` holds `*`): what is theirs rather than
the project's, such as how they run saddle on it, which every contributor
reading AGENTS.md need not see. Read once at the run's start from the
checkout, which the run's sandbox cannot see, so a run cannot rewrite them."""

LOCAL_HEADING: Final = (
    "\n\nThe person's own notes for this repository, from {name} in their "
    "checkout: not part of the repository, so not in your worktree. Follow them "
    "where they bear on your task. Where one conflicts with the task or with the "
    "rules above, the task and those rules come first.\n\n"
)


def local_instructions(checkout: Path) -> str:
    """The system-prompt section for `checkout`'s `LOCAL_FILE`, cut to
    `MAX_TOKENS`; "" when there is none or it is empty. A file that exists but
    cannot be read says so, never reads as no notes."""
    path = checkout / LOCAL_FILE
    heading = LOCAL_HEADING.format(name=LOCAL_FILE)
    try:
        text = path.read_text(encoding="utf-8").strip()
    except FileNotFoundError:
        return ""
    except (OSError, UnicodeDecodeError) as error:
        return heading + f"[{LOCAL_FILE} exists but could not be read: {error}]"
    if not text:
        return ""
    return heading + _capped(text, LOCAL_FILE, "Ask the person if you need the rest.")
