"""Checkpoints run the edit checks too, so ruff findings come before finish (#99).

In measured T5 draws the finish audit refused a finish for a ruff finding the model
could have fixed right after its first edit: one refusal round per draw, about 10k
tokens and 260-305 s. Tier 0 (syntax, ruff and imports on each changed Python file)
ran at finish and on `check`; a checkpoint ran tier 1 only.

Known-bad: a checkpoint of a tree whose change leaves an unused import names the
ruff finding. Known-good: a checkpoint of a clean change names no failing edit
check, and one whose change touches no Python file runs no edit check at all.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, cast

from test_check_tool import FINISH, REAL_TEST, Scripted, call, edit, make_repo

from saddle.auto import AutoOptions, AutoResult, run_auto
from saddle.journal import attempt_sidecar_path, read_spans
from saddle.vllm import VllmClient

BASE = "def f():\n    return 1\n"
READ = call("read_file", "r", path="n.py")  # any tool after an edit ends the burst


def _run(root: Path, change: list[Any]) -> AutoResult:
    make_repo(root, {"n.py": BASE, "test_n.py": REAL_TEST, "README.md": "notes\n"})
    client = Scripted([change, [READ], [FINISH]])
    options = AutoOptions(
        task="make f return 2", repo=root, run_id="cp", check_tool=False, finish_refusal_cap=1
    )
    return run_auto(options, cast(VllmClient, client))


def _checkpoint_1(result: AutoResult) -> list[dict[str, Any]]:
    """The findings checkpoint 1 sealed, delivered or withheld."""
    for span in read_spans(result.journal):
        if span.name in ("audit:delivered", "audit:withheld"):
            record = json.loads(attempt_sidecar_path(result.journal, span.span_id).read_text())
            if record.get("point") == "checkpoint 1":
                return cast(list[dict[str, Any]], record["findings"])
    message = "checkpoint 1 was never sealed"
    raise AssertionError(message)


def test_a_checkpoint_names_an_unused_import_the_change_left(tmp_path: Path) -> None:
    change = [
        edit("e", "def f():\n    return 1", "import os\n\n\ndef f():\n    return 2", path="n.py")
    ]
    findings = _checkpoint_1(_run(tmp_path / "bad", change))
    ruff = [f for f in findings if f["tier"] == 0 and f["gate"] == "ruff"]
    assert [f["verdict"] for f in ruff] == ["fail"]
    assert "F401" in ruff[0]["detail"]
    assert any(f["tier"] == 1 for f in findings)  # its tests still run beside it


def test_a_checkpoint_of_a_clean_change_names_no_failing_edit_check(tmp_path: Path) -> None:
    findings = _checkpoint_1(
        _run(tmp_path / "good", [edit("e", "return 1", "return 2", path="n.py")])
    )
    edit_checks = [f for f in findings if f["tier"] == 0]
    assert {f["gate"] for f in edit_checks} == {"syntax", "ruff", "imports"}
    assert all(f["verdict"] == "pass" for f in edit_checks)


def test_a_checkpoint_of_a_change_with_no_python_file_runs_no_edit_check(tmp_path: Path) -> None:
    findings = _checkpoint_1(
        _run(tmp_path / "docs", [edit("e", "notes", "more notes", path="README.md")])
    )
    assert [f for f in findings if f["tier"] == 0] == []
