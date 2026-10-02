"""The registry of how each tracked file type is checked, as data.

A key is a file's suffix (`.py`, `.md`); a file with no suffix, which includes
dotfiles such as `.gitignore`, is keyed by its repo-relative path, so a new
extensionless file needs its own decision. Each entry either names the checker
and the text that proves the checker is wired (a needle that must appear in a
named file: a stage in `check.sh`, a rule in a test module), or says why the
type is not checked. Nothing here reads the repository at import time.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import PurePosixPath


@dataclass(frozen=True)
class Check:
    """A checker and the (file, needle) pairs that show it is wired."""

    checker: str
    proof: tuple[tuple[str, str], ...]


@dataclass(frozen=True)
class NotChecked:
    """A type nothing checks, and why."""

    reason: str


Entry = Check | NotChecked

FIXTURES = ("tests/test_fixtures_integrity.py", "HANDLED_SUFFIXES")
CHECK_SH = "check.sh"

REGISTRY: dict[str, Entry] = {
    ".py": Check(
        "ruff, ruff format, mypy strict, pytest at 100% coverage",
        ((CHECK_SH, "uv run ruff check ."), (CHECK_SH, "uv run mypy"), (CHECK_SH, "uv run pytest")),
    ),
    ".js": Check(
        "eslint, prettier, tsc (the browser scripts; the node tests and drivers)",
        (
            (CHECK_SH, "eslint ."),
            (CHECK_SH, "prettier --check ."),
            (CHECK_SH, "tsc -p tsconfig.tests.json"),
        ),
    ),
    ".mjs": Check(
        "eslint, prettier and tsc (the browser drivers)",
        (
            (CHECK_SH, "eslint ."),
            (CHECK_SH, "prettier --check ."),
            (CHECK_SH, "tsc -p tsconfig.tests.json"),
        ),
    ),
    ".ts": Check("tsc", ((CHECK_SH, "tsc -p tsconfig.json"),)),
    ".html": Check(
        "html-validate (the page only)",
        ((CHECK_SH, "html-validate src/saddle/web/static/index.html"),),
    ),
    ".css": Check("stylelint", ((CHECK_SH, "stylelint src/saddle/web/static/app.css"),)),
    ".md": Check("markdownlint-cli2", ((CHECK_SH, "markdownlint-cli2"),)),
    ".yml": Check("actionlint", ((CHECK_SH, "actionlint .github/workflows"),)),
    ".toml": Check("taplo fmt --check", ((CHECK_SH, "taplo fmt --check"),)),
    ".sh": Check("shellcheck", ((CHECK_SH, "shellcheck check.sh ci-mutate.sh"),)),
    ".lock": Check("uv lock --check", ((CHECK_SH, "uv lock --check"),)),
    ".png": Check(
        "png_check, in the image test and the fixture integrity rule",
        (FIXTURES, ("tests/test_images.py", "png_check")),
    ),
    ".json": Check(
        "the fixture integrity rule (fixtures) and prettier (the other JSON)",
        (FIXTURES, (CHECK_SH, "prettier --check .")),
    ),
    ".jsonl": Check("the fixture integrity rule (fixtures only)", (FIXTURES,)),
    ".diff": Check("the fixture integrity rule (git apply --check expectations)", (FIXTURES,)),
    ".jinja": Check("the fixture integrity rule (loads and renders)", (FIXTURES,)),
    ".txt": Check("the fixture integrity rule (UTF-8)", (FIXTURES,)),
    "tools/githooks/commit-msg": Check("shellcheck", ((CHECK_SH, "tools/githooks/commit-msg"),)),
    "tools/githooks/pre-commit": Check("shellcheck", ((CHECK_SH, "tools/githooks/pre-commit"),)),
    "tools/githooks/pre-push": Check("shellcheck", ((CHECK_SH, "tools/githooks/pre-push"),)),
    ".prettierignore": Check(
        "test_prettier_leaves_fixture_json_bytes_alone (it hides fixtures; the rest is checked)",
        (("tests/test_js_static_checks.py", "test_prettier_leaves_fixture_json_bytes_alone"),),
    ),
    ".jsonc": NotChecked(
        "only .markdownlint-cli2.jsonc; its own parse is exercised by every markdownlint run"
    ),
    ".cff": NotChecked("CITATION.cff has no validator wired yet"),
    ".svg": NotChecked("one logo, shown in the README; nothing parses it"),
    ".node-version": NotChecked("a one-line version pin; nothing compares it with the toolchain"),
    ".python-version": NotChecked(
        "a one-line version pin; nothing compares it with pyproject.toml"
    ),
    ".gitignore": NotChecked("git ignores nothing it cannot parse; no test reads it"),
    "AUTHORS": NotChecked("plain credit text"),
    "LICENSE": NotChecked("verbatim licence text; nothing compares it with the declared licence"),
}


def key_for(path: str) -> str:
    """The registry key of a tracked path: its suffix, else the path itself."""
    return PurePosixPath(path).suffix or path
