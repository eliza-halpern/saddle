"""markdownlint runs over the tracked Markdown with the repository's config.

Contract: `.markdownlint-cli2.jsonc` keeps the rules on that it does not name,
turns off only rules it explains, and leaves out only the files it lists;
the tracked Markdown passes it. These run the real linter on temp files, a
bad one that must fail and good and excluded ones that must not, so the
config is tested by what it accepts and refuses, never by its text.
"""

from __future__ import annotations

import re
import shutil
import subprocess
from pathlib import Path

import pytest
from doc_tree import ROOT, require_checkout, tracked

CONFIG = ".markdownlint-cli2.jsonc"
LINTER = ROOT / "node_modules" / ".bin" / "markdownlint-cli2"
BAD_DOC = "# Title\n\n```\ncode\n```\nsome text\n- item\n+ item\n<div>x</div>\n"
GOOD_DOC = "# Title\n\nSome text.\n\n- item\n- item\n\n```text\ncode\n```\n"


def undocumented_rules(config_text: str) -> list[str]:
    """Rule keys (`"MD013": ...`) that no `//` comment line sits directly above."""
    lines = config_text.splitlines()
    return [
        match.group(1)
        for number, line in enumerate(lines)
        if (match := re.match(r'\s*"(MD\d+)"\s*:', line))
        and not (number > 0 and lines[number - 1].lstrip().startswith("//"))
    ]


def _lint(cwd: Path) -> subprocess.CompletedProcess[str]:
    """The plain command check.sh runs, with the config's globs, in `cwd`."""
    require_checkout()
    assert LINTER.exists(), "node_modules is missing: run `npm ci --ignore-scripts` (check.sh does)"
    return subprocess.run([str(LINTER)], cwd=cwd, capture_output=True, text=True, check=False)


def _project(root: Path, files: dict[str, str]) -> Path:
    root.mkdir(parents=True, exist_ok=True)
    shutil.copy(ROOT / CONFIG, root / CONFIG)
    for rel, body in files.items():
        (root / rel).parent.mkdir(parents=True, exist_ok=True)
        (root / rel).write_text(body, encoding="utf-8")
    return root


def test_the_markdown_in_the_tree_passes_the_linter() -> None:
    done = _lint(ROOT)
    assert done.returncode == 0, done.stdout + done.stderr
    # The denominator: every tracked file but the two the config excludes was
    # linted (an untracked note adds to the count, never subtracts).
    files = tracked("*.md")
    expected = [f for f in files if not f.startswith("benchmark/tasks/") and f != "CLAUDE.md"]
    counted = re.search(r"Linting: (\d+) files?", done.stdout)
    assert counted is not None, done.stdout
    assert int(counted.group(1)) >= len(expected)
    assert len(expected) >= 10


def test_a_badly_formed_document_fails_and_a_clean_one_passes(tmp_path: Path) -> None:
    good = _lint(_project(tmp_path / "good", {"good.md": GOOD_DOC}))
    assert good.returncode == 0, good.stderr
    bad = _lint(_project(tmp_path / "bad", {"bad.md": BAD_DOC}))
    assert bad.returncode != 0
    # Rules the config does not name stay on: fences without a language,
    # lists without blank lines, mixed bullet styles, stray HTML.
    for rule in ("MD040", "MD032", "MD004", "MD033"):
        assert rule in bad.stderr, bad.stderr


def test_the_documents_the_config_excludes_are_not_linted(tmp_path: Path) -> None:
    for excluded in ("CLAUDE.md", "benchmark/tasks/t/SPEC.md", "venv/lib/x/README.md"):
        done = _lint(_project(tmp_path / excluded.replace("/", "_"), {excluded: BAD_DOC}))
        assert done.returncode == 0, (excluded, done.stderr)
    assert _lint(_project(tmp_path / "docs", {"docs/bad.md": BAD_DOC})).returncode != 0


def test_a_rule_the_config_turns_off_is_really_off(tmp_path: Path) -> None:
    # A 150-character line and a padded-unevenly table are the findings the
    # config explains away; they must pass, or the explanations are fiction.
    body = (
        "# Title\n\n" + ("word " * 30).strip() + "\n\n| a | b |\n|--|--|\n| long cell here | x |\n"
    )
    done = _lint(_project(tmp_path, {"turned-off.md": body}))
    assert done.returncode == 0, done.stderr


def test_every_rule_the_config_names_has_a_reason_above_it() -> None:
    text = (ROOT / CONFIG).read_text(encoding="utf-8")
    assert re.search(r'"MD\d+"', text), "the config names no rule: the check below would be vacuous"
    assert undocumented_rules(text) == []


@pytest.mark.parametrize(
    ("text", "bare"),
    [
        ('{\n  // why\n  "MD013": false,\n  "MD060": false\n}\n', ["MD060"]),
        ('{\n  "MD013": false\n}\n', ["MD013"]),
        ('{\n  // why\n  "MD013": false\n}\n', []),
    ],
)
def test_a_rule_without_a_comment_is_reported(text: str, bare: list[str]) -> None:
    assert undocumented_rules(text) == bare
