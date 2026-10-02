"""Every tracked file type has a decision: a checker that is wired, or a reason.

Contract: each tracked path's key (suffix, else the path) has a registry entry,
each entry matches at least one tracked path, and each checker entry's proof
text is found in its named file; a new file type fails until someone decides.
"""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path

import pytest
from file_checks import REGISTRY, Check, Entry, NotChecked, key_for
from test_fixtures_integrity import ROOT, _is_own_toplevel, tracked

pytestmark = pytest.mark.skipif(
    not _is_own_toplevel(ROOT),
    reason="needs a real checkout: the mutation work copy lacks the files",
)


def problems(paths: list[str], registry: Mapping[str, Entry], root: Path) -> list[str]:
    """What is wrong with `registry` for the tracked `paths`; empty when sound."""
    found: list[str] = []
    keys = {key_for(p) for p in paths}
    for key in sorted(keys - registry.keys()):
        found.append(f"{key}: tracked but no registry entry; decide how it is checked")
    for key in sorted(registry.keys() - keys):
        found.append(f"{key}: registry entry but no tracked file has this type")
    for key, entry in sorted(registry.items()):
        if isinstance(entry, Check):
            for rel, needle in entry.proof:
                target = root / rel
                if not target.is_file() or needle not in target.read_text(encoding="utf-8"):
                    found.append(f"{key}: {rel} does not contain {needle!r}")
    return found


def test_the_tracked_tree_is_fully_registered() -> None:
    paths = tracked(ROOT, ".")
    assert len(paths) > 100  # the denominator: a failed lookup must not read as empty
    assert problems(paths, REGISTRY, ROOT) == []


def test_an_unregistered_type_a_stale_entry_and_a_false_proof_are_reported(tmp_path: Path) -> None:
    (tmp_path / "gate.sh").write_text("run lint\n", encoding="utf-8")
    registry: dict[str, Entry] = {
        ".sh": Check("lint", (("gate.sh", "run lint"),)),
        ".rs": NotChecked("no such files"),
        ".md": Check("lint", (("gate.sh", "run markdownlint"),)),
    }
    out = problems(["gate.sh", "a.zig", "x.md", "hooks/pre-commit"], registry, tmp_path)
    assert out == [
        ".zig: tracked but no registry entry; decide how it is checked",
        "hooks/pre-commit: tracked but no registry entry; decide how it is checked",
        ".rs: registry entry but no tracked file has this type",
        ".md: gate.sh does not contain 'run markdownlint'",
    ]
    good: dict[str, Entry] = {".sh": Check("lint", (("gate.sh", "run lint"),))}
    assert problems(["gate.sh", "b.sh"], good, tmp_path) == []


def test_extensionless_and_dotfiles_are_keyed_by_path() -> None:
    assert key_for("tools/githooks/pre-push") == "tools/githooks/pre-push"
    assert key_for(".gitignore") == ".gitignore"
    assert key_for("a/b.tar.gz") == ".gz"


def test_a_proof_naming_a_file_that_is_gone_is_reported(tmp_path: Path) -> None:
    (tmp_path / "gate.sh").write_text("run lint\n", encoding="utf-8")
    registry: dict[str, Entry] = {".sh": Check("lint", (("old-gate.sh", "run lint"),))}
    assert problems(["gate.sh"], registry, tmp_path) == [
        ".sh: old-gate.sh does not contain 'run lint'"
    ]
