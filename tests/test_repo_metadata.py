"""The metadata files agree with each other and with what the package declares.

Contract: CITATION.cff parses, carries the keys CFF 1.2.0 requires, and names
the version, license and authors that `pyproject.toml` (and AUTHORS) declare;
LICENSE is the AGPL-3.0 text, unchanged; and no tracked file is one that
`.gitignore` ignores.

`cffconvert --validate` is the reference validator, but every release of it
pins `jsonschema` below the 4.26 the dev group needs, so the CFF rules that
matter here are checked directly. PyYAML is imported by name because it
arrives through `libcst` (a `mutmut` dependency), not through a declared
dependency of this project: if it ever stops being installed these tests fail
at import, they do not skip.
"""

from __future__ import annotations

import hashlib
import importlib
import re
import subprocess
import tomllib
from collections.abc import Callable
from pathlib import Path

import pytest
from doc_tree import ROOT, git_ls_files, require_checkout

yaml = importlib.import_module("yaml")

# Every key CFF 1.2.0 allows at the top level; the schema rejects the rest,
# which is how a misspelt `licence:` is caught.
CFF_KEYS = frozenset(
    {
        "abstract",
        "authors",
        "cff-version",
        "commit",
        "contact",
        "date-released",
        "doi",
        "identifiers",
        "keywords",
        "license",
        "license-url",
        "message",
        "preferred-citation",
        "references",
        "repository",
        "repository-artifact",
        "repository-code",
        "title",
        "type",
        "url",
        "version",
    }
)

# SHA-256 of LICENSE after `normalised_license`. There is no copy of the FSF's
# agpl-3.0.txt on this machine or in the dependencies to compare with, so this
# pins "unchanged since it was committed", not "identical to the canonical
# text"; the title, version line and section 13 checks below are what tie the
# file to AGPL-3.0 itself. A deliberate change to LICENSE updates this value.
LICENSE_SHA256 = "0531b39424321b061df4627ba1528e69674cfd2df381fc54e9afa63d3bdbec98"
AGPL_SPDX = {"AGPL-3.0-only", "AGPL-3.0-or-later"}


def _author_names_in_authors_file(text: str) -> list[str]:
    _, _, body = text.partition("\n\n")
    return [re.sub(r"\s*<[^>]*>\s*$", "", line).strip() for line in body.splitlines() if line]


def citation_problems(citation: str, pyproject: str, authors: str) -> list[str]:
    """Everything wrong with a CITATION.cff, judged against pyproject and AUTHORS."""
    try:
        data = yaml.safe_load(citation)
    except yaml.YAMLError as exc:
        return [f"not valid YAML: {exc}"]
    if not isinstance(data, dict):
        return ["not a mapping"]
    problems: list[str] = []
    project = tomllib.loads(pyproject)["project"]
    problems += [f"unknown top-level key {key!r}" for key in data if key not in CFF_KEYS]
    if data.get("cff-version") != "1.2.0":
        problems.append(f"cff-version is {data.get('cff-version')!r}, not '1.2.0'")
    for key in ("message", "title"):
        if not isinstance(data.get(key), str) or not data[key].strip():
            problems.append(f"{key} is missing or empty")
    if data.get("type", "software") not in {"software", "dataset"}:
        problems.append(f"type is {data['type']!r}")
    if data.get("version") != project["version"]:
        problems.append(
            f"version is {data.get('version')!r}, pyproject says {project['version']!r}"
        )
    if data.get("license") != project["license"]:
        problems.append(
            f"license is {data.get('license')!r}, pyproject says {project['license']!r}"
        )
    people = data.get("authors")
    names: list[object] = []
    if not isinstance(people, list) or not people:
        problems.append("authors is missing or empty")
    else:
        for person in people:
            if not isinstance(person, dict) or not (
                person.get("name") or (person.get("family-names") and person.get("given-names"))
            ):
                problems.append(f"author {person!r} has neither name nor family and given names")
            else:
                names.append(
                    person.get("name") or f"{person['given-names']} {person['family-names']}"
                )
    declared = [author["name"] for author in project["authors"]]
    if names != declared:
        problems.append(f"authors are {names!r}, pyproject says {declared!r}")
    if declared != _author_names_in_authors_file(authors):
        problems.append(f"AUTHORS names {_author_names_in_authors_file(authors)!r} != {declared!r}")
    return problems


def normalised_license(text: str) -> str:
    """Line endings and trailing blanks removed, so a checkout's quirks do not matter."""
    return (
        "\n".join(line.rstrip() for line in text.replace("\r\n", "\n").splitlines()).strip() + "\n"
    )


def license_problems(text: str, spdx: str) -> list[str]:
    """Why `text` is not the AGPL-3.0 text that `spdx` declares."""
    if spdx not in AGPL_SPDX:
        return [f"pyproject declares {spdx!r}, which is not an AGPL-3.0 identifier"]
    lines = [line.strip() for line in normalised_license(text).splitlines() if line.strip()]
    problems: list[str] = []
    if lines[:2] != ["GNU AFFERO GENERAL PUBLIC LICENSE", "Version 3, 19 November 2007"]:
        problems.append(f"opens with {lines[:2]!r}")
    # Section 13 is what separates the AGPL from the GPL it is built on.
    if "13. Remote Network Interaction; Use with the GNU General Public License." not in lines:
        problems.append("section 13 (Remote Network Interaction) is missing")
    if "END OF TERMS AND CONDITIONS" not in lines:
        problems.append("the terms are cut short: no END OF TERMS AND CONDITIONS")
    digest = hashlib.sha256(normalised_license(text).encode()).hexdigest()
    if digest != LICENSE_SHA256:
        problems.append(f"sha256 is {digest}, recorded {LICENSE_SHA256}")
    return problems


def tracked_but_ignored(root: Path) -> list[str]:
    """Tracked files that `.gitignore` would ignore (`git ls-files -ci`)."""
    return git_ls_files(root, "-ci", "--exclude-standard")


def _real_inputs() -> tuple[str, str, str]:
    require_checkout()
    return (
        (ROOT / "CITATION.cff").read_text(encoding="utf-8"),
        (ROOT / "pyproject.toml").read_text(encoding="utf-8"),
        (ROOT / "AUTHORS").read_text(encoding="utf-8"),
    )


def test_citation_cff_agrees_with_pyproject_and_authors() -> None:
    citation, pyproject, authors = _real_inputs()
    assert citation_problems(citation, pyproject, authors) == []


def _drift(old: str, new: str) -> Callable[[str], str]:
    def apply(citation: str) -> str:
        assert old in citation, f"{old!r} not in the real CITATION.cff: update this case"
        return citation.replace(old, new)

    return apply


@pytest.mark.parametrize(
    ("edit", "expected"),
    [
        (_drift('version: "0.1.1"', 'version: "0.1.2"'), "version is '0.1.2'"),
        (_drift('version: "0.1.1"', "version: 0.1"), "version is 0.1"),
        (_drift("cff-version: 1.2.0", "cff-version: 1.1.0"), "cff-version is '1.1.0'"),
        (_drift("cff-version: 1.2.0", "cff-version: 1.2"), "cff-version is 1.2"),
        (_drift("license: AGPL-3.0-or-later", "license: MIT"), "license is 'MIT'"),
        (_drift("license:", "licence:"), "unknown top-level key 'licence'"),
        (_drift("message:", "msg:"), "message is missing"),
        (_drift('title: "saddle"', 'title: ""'), "title is missing"),
        (_drift("type: software", "type: article"), "type is 'article'"),
        (_drift('  - name: "Eryn Lipkowitz"\n', ""), "authors are ['Eliza Halpern']"),
        (_drift('name: "Eliza Halpern"', 'name: "E. Halpern"'), "authors are ['E. Halpern'"),
        (_drift("authors:", "writers:"), "authors is missing"),
        (_drift('title: "saddle"', 'title: ["saddle"'), "not valid YAML"),
    ],
)
def test_citation_cff_drift_is_reported(edit: Callable[[str], str], expected: str) -> None:
    citation, pyproject, authors = _real_inputs()
    found = citation_problems(edit(citation), pyproject, authors)
    assert any(expected in problem for problem in found), found


def test_citation_cff_checks_authors_against_the_authors_file() -> None:
    citation, pyproject, authors = _real_inputs()
    assert citation_problems(citation, pyproject, authors + "Someone Else\n") != []


def test_citation_cff_accepts_family_and_given_names() -> None:
    _, pyproject, authors = _real_inputs()
    person = '  - family-names: "Halpern"\n    given-names: "Eliza"\n'
    other = '  - name: "Eryn Lipkowitz"\n'
    citation = (
        'cff-version: 1.2.0\nmessage: "m"\ntitle: "t"\nauthors:\n'
        + person
        + other
        + f'version: "{tomllib.loads(pyproject)["project"]["version"]}"\n'
        + f"license: {tomllib.loads(pyproject)['project']['license']}\n"
    )
    assert citation_problems(citation, pyproject, authors) == []
    half = citation.replace('    given-names: "Eliza"\n', "")
    assert any("neither name nor family" in p for p in citation_problems(half, pyproject, authors))


def test_the_license_is_the_agpl_text_declared_in_pyproject() -> None:
    _, pyproject, _ = _real_inputs()
    project = tomllib.loads(pyproject)["project"]
    assert "LICENSE" in project["license-files"]
    text = (ROOT / "LICENSE").read_text(encoding="utf-8")
    assert license_problems(text, project["license"]) == []


def test_license_checks_reject_other_texts_and_edits() -> None:
    _, pyproject, _ = _real_inputs()
    spdx = tomllib.loads(pyproject)["project"]["license"]
    text = (ROOT / "LICENSE").read_text(encoding="utf-8")
    # The same text with CRLF endings and trailing blanks is still the license.
    assert license_problems(text.replace("\n", "  \r\n"), spdx) == []
    cases = {
        "gpl title": text.replace(
            "GNU AFFERO GENERAL PUBLIC LICENSE", "GNU GENERAL PUBLIC LICENSE"
        ),
        "version": text.replace("Version 3, 19 November 2007", "Version 2, June 1991"),
        "no section 13": text.replace("13. Remote Network Interaction", "13. Something else"),
        "truncated": text.split("END OF TERMS AND CONDITIONS")[0],
        "one word changed": text.replace("Permission", "Permissions", 1),
    }
    for name, edited in cases.items():
        assert license_problems(edited, spdx) != [], name
    assert any("opens with" in p for p in license_problems(cases["gpl title"], spdx))
    assert any("section 13" in p for p in license_problems(cases["no section 13"], spdx))
    assert any("END OF TERMS" in p for p in license_problems(cases["truncated"], spdx))
    assert any("sha256" in p for p in license_problems(cases["one word changed"], spdx))


def test_license_must_be_declared_as_agpl() -> None:
    text = (ROOT / "LICENSE").read_text(encoding="utf-8")
    found = license_problems(text, "MIT")
    assert len(found) == 1
    assert "not an AGPL-3.0 identifier" in found[0]


def _git(root: Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=root, check=True, capture_output=True)


def test_no_tracked_file_is_ignored() -> None:
    require_checkout()
    assert tracked_but_ignored(ROOT) == []


def test_a_tracked_file_that_gitignore_ignores_is_listed(tmp_path: Path) -> None:
    _git(tmp_path, "init", "-q")
    (tmp_path / "kept.txt").write_text("ok\n")
    (tmp_path / "noise.log").write_text("x\n")
    _git(tmp_path, "add", "kept.txt", "noise.log")
    assert tracked_but_ignored(tmp_path) == []
    (tmp_path / ".gitignore").write_text("*.log\n")
    assert tracked_but_ignored(tmp_path) == ["noise.log"]


def test_a_git_failure_is_an_error_not_an_empty_list(tmp_path: Path) -> None:
    with pytest.raises(RuntimeError, match="git ls-files"):
        tracked_but_ignored(tmp_path)
