"""Code names a finish summary gives that the change does not have (#191).

A run's finish summary becomes its commit message's narrative and the packet's
account of the change. One run, after 15 compactions, put 31 code names in
backticks, and 3 of them existed nowhere: not in its tree, not in its baseline,
and not in anything it had read or written before that finish call. It made them
up while describing a file it could by then see only in parts, and nothing
compared the summary with the tree.

`absent` names each code name of a summary that no file of the run's tree or of
its baseline contains. The engine returns the first finish that has any, once
and not as a refusal (`engine.SUMMARY_NAMES_ABSENT`); the outcome seals what the
final summary still names, and the packet shows it beside the narrative.
"""

from __future__ import annotations

import re
import subprocess
from pathlib import Path, PurePosixPath
from typing import Final

CODE_NAME: Final = re.compile(r"`([A-Za-z_]\w*(?:\.[A-Za-z_]\w*)*)(\(\))?`")
"""A backticked identifier or dotted name, optionally written as a call (`f()`)."""


class SummaryNamesError(RuntimeError):
    """git could not be asked. The names are then unchecked, never read as present
    or as absent."""


def code_names(summary: str) -> list[str]:
    """The backticked names in `summary` that read as code, once each, in order.

    A name reads as code when it has an underscore or a dot, mixes capitals and
    lower case after its first letter (`YamlProblem`), or is written as a call.
    A plain word in backticks (`tightened`, `sql`, `JSON`) is prose, and a span
    holding any other character (a path, a flag, `PATH:LINE:COL`) is not a name."""
    names: list[str] = []
    for match in CODE_NAME.finditer(summary):
        name, call = match.group(1), match.group(2)
        mixed = any(c.isupper() for c in name[1:]) and any(c.islower() for c in name)
        if (call or "_" in name or "." in name or mixed) and name not in names:
            names.append(name)
    return names


def _git(worktree: Path, *args: str, ok: tuple[int, ...] = (0,)) -> str:
    done = subprocess.run(
        ["git", "-C", str(worktree), *args], capture_output=True, text=True, check=False
    )
    if done.returncode not in ok:
        msg = f"git {args[0]} failed: {done.stderr.strip() or f'exit {done.returncode}'}"
        raise SummaryNamesError(msg)
    return done.stdout


def absent(summary: str, worktree: Path, baseline: str) -> list[str]:
    """Each code name of `summary` that neither the tree nor `baseline` has, in order.

    A name is there when it is the path or the file name of a file in either (a
    module named as `yamlcheck.py`), or when every dot-separated part of it is a
    whole word in some file of either: the worktree's tracked and untracked files
    as they are now, ignored ones aside, and the baseline commit's. A name the
    change removed is still at the baseline, so describing a deletion is never
    read as naming what is not there. Raises `SummaryNamesError` when git cannot
    be asked."""
    names = code_names(summary)
    if not names:
        return []
    listed = _git(worktree, "ls-files", "--cached", "--others", "--exclude-standard").splitlines()
    listed += _git(worktree, "ls-tree", "-r", "--name-only", baseline).splitlines()
    files = {p for path in listed for p in (path, PurePosixPath(path).name)}
    parts = sorted({part for name in names for part in name.split(".")})
    patterns = [arg for part in parts for arg in ("-e", part)]
    grep = ("grep", "-I", "-w", "-F", "-o", "-h")
    found = set(_git(worktree, *grep, "--untracked", *patterns, ok=(0, 1)).split())
    found |= set(_git(worktree, *grep, *patterns, baseline, "--", ok=(0, 1)).split())
    return [n for n in names if n not in files and not all(p in found for p in n.split("."))]
