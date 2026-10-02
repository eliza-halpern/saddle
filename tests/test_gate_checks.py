"""`[tool.saddle] gate-checks` is `check.sh`'s fast stages, so the audit and the gate cannot drift.

The audit runs the stages the list names on head and base (`auditor.PROJECT_GATE`);
`check.sh` is what CI runs. A stage added to one and not the other would make the
audit pass what the gate refuses, which is the defect the list exists to close.
Known-good: the list plus the `static-check` stage equals the stages parsed from
`check.sh` (the suite, `npm ci` and the worker count's `python -c` are not stages
of this list). Known-bad: a stage missing from the list, an extra one, or one
spelled differently is reported.
"""

from __future__ import annotations

import tomllib
from typing import Any

from test_check_sh import Stage, check_sh_stages
from test_shell_scripts import ROOT, needs_checkout

# Stages the list does not carry: the dependency install, the suite, and the worker count
# the suite is told (a `python -c` reading pyproject.toml).
NOT_FAST_STAGES = frozenset({"npm", "pytest"})


def stage_argv(stage: Stage) -> list[str]:
    """The command a stage runs, with `uv run` dropped and shell globs expanded
    (the audit runs argv with no shell, so the list names the files)."""
    words = [stage.key, *stage.args]
    if stage.exe == "npx":
        words = ["npx", "--no-install", *words]
    elif stage.exe == "uv":
        words = ["uv", *words]
    expanded: list[str] = []
    for word in words:
        found = (
            sorted(p.relative_to(ROOT).as_posix() for p in ROOT.glob(word)) if "*" in word else []
        )
        expanded.extend(found or [word])
    return expanded


def fast_stages(script: str) -> list[list[str]]:
    return [
        stage_argv(s)
        for s in check_sh_stages(script)
        if s.exe not in NOT_FAST_STAGES and not (s.exe == "python" and s.args[:1] == ("-c",))
    ]


def drift(declared: list[list[str]], static: list[str], script: str) -> list[str]:
    """How `check.sh`'s fast stages and the audit's (the list plus `static-check`) differ."""
    audit = [*declared, static]
    gate = fast_stages(script)
    return [f"not in check.sh: {a}" for a in audit if a not in gate] + [
        f"not in the audit: {g}" for g in gate if g not in audit
    ]


def read_saddle_table() -> dict[str, Any]:
    table: dict[str, Any] = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    saddle: dict[str, Any] = table["tool"]["saddle"]
    return saddle


@needs_checkout
def test_gate_checks_and_static_check_are_exactly_check_shs_fast_stages() -> None:
    table = read_saddle_table()
    static = table["static-check"]
    declared = table["gate-checks"]
    assert drift(declared, static, (ROOT / "check.sh").read_text(encoding="utf-8")) == []
    assert [s for s in declared if s[0] == "npx"], "the browser stages are in the list"


@needs_checkout
def test_check_sh_has_fast_stages_to_compare() -> None:
    assert len(fast_stages((ROOT / "check.sh").read_text(encoding="utf-8"))) >= 10


def test_a_stage_missing_from_the_list_an_extra_one_and_a_misspelled_one_are_reported() -> None:
    script = "uv run ruff check .\nuv run mypy src\nnpx --no-install eslint .\nuv run pytest -q\n"
    good = [["ruff", "check", "."], ["npx", "--no-install", "eslint", "."]]
    assert drift(good, ["mypy", "src"], script) == []
    assert drift(good[:1], ["mypy", "src"], script) == [
        "not in the audit: ['npx', '--no-install', 'eslint', '.']"
    ]
    extra = [*good, ["shellcheck", "x.sh"]]
    assert drift(extra, ["mypy", "src"], script) == ["not in check.sh: ['shellcheck', 'x.sh']"]
    typo = [["ruff", "check", "src"], good[1]]
    assert len(drift(typo, ["mypy", "src"], script)) == 2
