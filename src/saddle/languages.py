"""Which languages a change touches, and which findings are about which.

An audit of a Python-only change used to carry the JavaScript stages and an
audit of a JavaScript-only change the Python suite's coverage and mutation
findings: lines about code the change never touched, which a reader must
dismiss one by one. The language of each changed file is decided once per audit
(`classify`), and a finding is shown only when it is about a touched language
(`visible`).

Hiding is for noise, never for a refusal: a finding about a language the diff
does not touch is dropped only when it is not a refusal (`visible`), so a
JavaScript-only change that breaks the Python suite is still refused.
"""

from __future__ import annotations

from collections.abc import Collection, Iterable, Mapping
from pathlib import PurePosixPath
from typing import Final

PYTHON: Final = "python"
JAVASCRIPT: Final = "javascript"
MARKDOWN: Final = "markdown"
SHELL: Final = "shell"
HTML: Final = "html"
CSS: Final = "css"
SQL: Final = "sql"
CONFIG: Final = "config"
OTHER: Final = "other"

SUFFIX_LANGUAGE: Final[Mapping[str, str]] = {
    ".py": PYTHON,
    ".pyi": PYTHON,
    ".js": JAVASCRIPT,
    ".mjs": JAVASCRIPT,
    ".cjs": JAVASCRIPT,
    ".md": MARKDOWN,
    ".sh": SHELL,
    ".bash": SHELL,
    ".html": HTML,
    ".css": CSS,
    ".sql": SQL,
    ".toml": CONFIG,
    ".json": CONFIG,
    ".yml": CONFIG,
    ".yaml": CONFIG,
    ".cfg": CONFIG,
    ".ini": CONFIG,
    ".lock": CONFIG,
}
"""The language of a changed file, by its suffix (lower-cased). A suffix not
listed, or none, is `OTHER`."""

LANGUAGE_NAMES: Final = frozenset({PYTHON, JAVASCRIPT, MARKDOWN, SHELL, HTML, CSS, SQL, CONFIG})
"""The names a project's `gate-stage-languages` may use."""

WILDCARDS: Final = frozenset({CONFIG, OTHER})
"""Files whose effect on the project's tools is not read from their name: a
`pyproject.toml` edit can change what ruff or mypy does, an unknown file type
can be anything. A change touching one hides nothing."""


def classify(files: Iterable[str]) -> frozenset[str]:
    """The languages of the changed `files`, one lookup per audit. A deleted file
    is listed by git like any other, so it counts. No files at all is `OTHER`:
    a diff nothing could be read from hides nothing."""
    found = {
        SUFFIX_LANGUAGE.get(PurePosixPath(f).suffix.lower(), OTHER) for f in files if f.strip()
    }
    return frozenset(found or {OTHER})


def touches(touched: Collection[str], about: Collection[str]) -> bool:
    """Whether a change touching `touched` is touched by something `about`
    names: they share a language, or the change holds a `WILDCARDS` file."""
    return bool(WILDCARDS & set(touched)) or bool(set(about) & set(touched))


FINDING_LANGUAGES: Final[Mapping[str, frozenset[str]]] = {
    "tests": frozenset({PYTHON}),
    "full-suite": frozenset({PYTHON}),
    "coverage": frozenset({PYTHON}),
    "dead-code": frozenset({PYTHON, JAVASCRIPT}),
    "public-deletions": frozenset({PYTHON}),
    "assertion-preservation": frozenset({PYTHON}),
    "mutation": frozenset({PYTHON, JAVASCRIPT}),
    "property-coverage": frozenset({PYTHON}),
    "red-phase": frozenset({PYTHON}),
    "requirement-binding": frozenset({PYTHON}),
    "skipped-tests": frozenset({PYTHON}),
    "js-tests": frozenset({JAVASCRIPT}),
    "js-coverage": frozenset({JAVASCRIPT}),
    "js-red-phase": frozenset({JAVASCRIPT}),
}
"""Which languages each finding is about. A gate not listed is language-neutral
and always shown: the scope checks (`node-scope`, `target-scope`),
`task-requirements`, `not-measurable`, `prompt-effect`, and the two gate lines
(`static-check`, `project-gate`), whose stages are filtered one by one
(`stage_visible`). `dead-code` and `mutation` cover both measured languages."""

REFUSALS: Final = frozenset({"fail", "blocked", "question"})
"""The verdicts `visible` never hides: a refusal, or a question a person owes."""


def visible(
    gate: str, verdict: str, touched: Collection[str], unmeasured: Collection[str] = ()
) -> bool:
    """Whether the finding of `gate` with `verdict` is shown for a change touching
    `touched`: always when the gate is language-neutral, when it is about a
    touched language, or when it is a refusal (`REFUSALS`). `unmeasured` names
    languages whose mutation tool is not installed: the `mutation` finding is
    not about them, since it measured none of their lines."""
    about = FINDING_LANGUAGES.get(gate)
    if about is None or verdict in REFUSALS:
        return True
    if gate == "mutation":
        about = about - set(unmeasured)
    return touches(touched, about)


def stage_visible(
    name: str, touched: Collection[str], declared: Mapping[str, tuple[str, ...]]
) -> bool:
    """Whether the project's gate stage `name` (`gates.gate_stage_name`) runs for
    a change touching `touched`. `declared` is the project's
    `gate-stage-languages`, keyed by the stage's name or by its tool alone; a
    stage it does not name is language-neutral and always runs."""
    about = declared.get(name, declared.get(name.split(" ")[0]))
    return about is None or touches(touched, about)
