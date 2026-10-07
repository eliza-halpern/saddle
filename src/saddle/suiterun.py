"""Whether a shell command starts the whole test suite (#178).

A run that offers `check` refuses a `run_command` that starts the whole suite
and points it at `check` with `whole_suite` set to true: a dogfood run started
the suite by hand five times while `check` was offered and the prompt said not
to, and a hand-run suite tells the model nothing `check` does not (`check` names
the failing tests, and running one by its id stays allowed).

What counts, read off each command of a shell line (split at `&&`, `||`, `;`,
`|`, `&` and newlines, redirections dropped):

- `pytest`, `py.test` or `python -m pytest`, also under `uv run`, `coverage run
  -m`, `timeout`, `env`, `nice`, `time`, `exec`, a venv path or a `VAR=value`
  prefix, with no test file, node id or subdirectory of the tests directory
  as a target and no `-k`, `--lf` or `--collect-only` narrowing it. A target
  that is the tests directory itself does not narrow it.
- the project's `check.sh`, however it is invoked.

A script the model writes that calls pytest itself is not caught: the line
names no suite, and refusing every script would refuse ordinary work.
"""

from __future__ import annotations

import re
import shlex
from pathlib import PurePosixPath
from typing import Final

from saddle.gates import TEST_DIRECTORIES

_SEPARATORS: Final = frozenset({"&&", "||", ";", "|", "&", ";;", "|&"})
_REDIRECTS: Final = frozenset({">", ">>", "<", ">&", "&>", "<&", ">|", "&>>", "<<", "<<<"})
_ASSIGNMENT: Final = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*=")
_PYTHON: Final = re.compile(r"^python(\d+(\.\d+)?)?$")
_DURATION: Final = re.compile(r"^\d+(\.\d+)?[smhd]?$")
_WRAPPERS: Final = frozenset({"env", "nice", "nohup", "time", "exec", "command"})
_NARROWING: Final = frozenset({"--lf", "--last-failed", "--co", "--collect-only", "--sw"})
_UNTARGETED: Final = frozenset({"--ignore", "--ignore-glob", "--deselect"})
"""Options whose value is a path that removes tests rather than choosing them."""


def _tokens(line: str) -> list[str]:
    lexer = shlex.shlex(line, posix=True, punctuation_chars=True)
    lexer.whitespace_split = True
    lexer.commenters = ""
    try:
        return list(lexer)
    except ValueError:
        return line.split()


def _commands(text: str) -> list[list[str]]:
    """The shell text as its simple commands, each a word list, redirections
    and heredoc bodies dropped: a heredoc that writes a file naming pytest
    starts nothing."""
    commands: list[list[str]] = []
    delimiter: str | None = None
    for line in text.replace("\\\n", " ").split("\n"):
        if delimiter is not None:
            if line.strip() == delimiter:
                delimiter = None
            continue
        commands.append([])
        skip = heredoc = False
        for token in _tokens(line):
            if skip:
                skip = False
                if heredoc:
                    delimiter = token.lstrip("-")
            elif token in _SEPARATORS:
                commands.append([])
            elif token in _REDIRECTS:
                skip = True
                heredoc = token == "<<"
            else:
                commands[-1].append(token)
    return [c for c in commands if c]


def _unwrapped(words: list[str]) -> list[str]:
    """`words` without the prefixes that run another command unchanged."""
    while words:
        head = PurePosixPath(words[0]).name
        if _ASSIGNMENT.match(words[0]) or head in _WRAPPERS:
            words = words[1:]
        elif head == "timeout":
            # its options (some take a value) end at the duration
            words = words[1:]
            while words and not _DURATION.match(words[0]):
                words = words[1:]
            words = words[1:]
        elif head in {"uv", "coverage"} and len(words) > 1 and words[1] == "run":
            words = words[2:]
            while words and words[0].startswith("-") and words[0] != "-m":
                words = words[1:]
            if words[:2] == ["-m", "pytest"]:
                words = ["pytest", *words[2:]]
        else:
            return words
    return words


def _pytest_args(words: list[str]) -> list[str] | None:
    """The arguments `words` passes to pytest, or None when it is not pytest."""
    head = PurePosixPath(words[0]).name
    if head in {"pytest", "py.test"}:
        return words[1:]
    if _PYTHON.match(head) and "-m" in words:
        at = words.index("-m")
        if words[at + 1 : at + 2] == ["pytest"]:
            return words[at + 2 :]
    return None


def _narrows(target: str) -> bool:
    """Whether a pytest target chooses part of the suite: a file, a node id, or
    a directory inside the tests directory. The tests directory does not."""
    if "::" in target or target.endswith(".py"):
        return True
    path = PurePosixPath(target)
    return path.name not in TEST_DIRECTORIES and any(
        part in TEST_DIRECTORIES for part in path.parts
    )


def _whole_pytest(args: list[str]) -> bool:
    """Whether pytest run with `args` runs every test it collects."""
    targets: list[str] = []
    after_untargeted = False
    for arg in args:
        if after_untargeted:
            after_untargeted = False
        elif arg in _UNTARGETED:
            after_untargeted = True
        elif arg in _NARROWING or arg.startswith("-k"):
            return False
        elif not arg.startswith("-"):
            targets.append(arg)
    if any(PurePosixPath(t).name in TEST_DIRECTORIES for t in targets):
        return True
    return not any(_narrows(t) for t in targets)


def starts_whole_suite(line: str) -> bool:
    """Whether the shell `line` starts the whole test suite (module docstring)."""
    for words in _commands(line):
        words = _unwrapped(words)
        if not words:
            continue
        if PurePosixPath(words[0]).name == "check.sh" or (
            PurePosixPath(words[0]).name in {"bash", "sh"}
            and len(words) > 1
            and PurePosixPath(words[1]).name == "check.sh"
        ):
            return True
        args = _pytest_args(words)
        if args is not None and _whole_pytest(args):
            return True
    return False
