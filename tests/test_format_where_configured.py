"""`ruff format` judges a project only where the project chose ruff (#130).

The ruff gate runs `--isolated` with saddle's defect rules (`RUFF_RULES`:
pyflakes, E4/E7/E9, bugbear), which find bugs in any project's style. Its
format check did more: on a project with no ruff configuration it imposed
ruff's own defaults, so a line past 88 columns in a project that never
chose ruff was refused as code-wrong.

The contract: the format check runs where the baseline commit configures
ruff (`.ruff.toml`, `ruff.toml`, or a `[tool.ruff]` table in
`pyproject.toml`, `evidence.RUFF_CONFIGS`); elsewhere the `ruff` row says
format was not checked (`FORMAT_NOT_CONFIGURED`) and the defect rules
still run.

Known-good: the same long line passes in an unconfigured project, saying
format was not checked. Known-bad: it is refused in a project that
configures ruff, and an undefined name is refused in both.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from saddle.audit import audit_tree
from saddle.auditor import Auditor
from saddle.evidence import ruff_configured, run_argv
from saddle.gates import FORMAT_NOT_CONFIGURED

QUERY = "SELECT a, b, c, d, e, f, g FROM main.clinic.visits WHERE a > 1 AND b < 2 AND c = 3"
LONG = f'def run(db):\n    return db.sql("{QUERY}")\n'
assert len(LONG.splitlines()[1]) > 100
UNDEFINED = "def run(db):\n    return missing(db)\n"
CONFIGS = {
    ".ruff.toml": "line-length = 88\n",
    "ruff.toml": "line-length = 88\n",
    "pyproject.toml": "[tool.ruff]\nline-length = 88\n",
}


def _git(root: Path, *argv: str) -> None:
    assert run_argv(["git", *argv], root) == 0


def _repo(tmp_path: Path, files: dict[str, str]) -> Path:
    root = tmp_path / "tree"
    root.mkdir(parents=True)
    _git(root, "init")
    _git(root, "config", "user.email", "t@t")
    _git(root, "config", "user.name", "t")
    for name, text in {"app.py": "def run(db):\n    return db\n", **files}.items():
        (root / name).write_text(text)
    _git(root, "add", "-A")
    _git(root, "commit", "-m", "base")
    return root


@pytest.mark.parametrize("name", sorted(CONFIGS))
def test_each_place_ruff_reads_its_settings_counts_as_configured(tmp_path: Path, name: str) -> None:
    assert ruff_configured(_repo(tmp_path, {name: CONFIGS[name]}), "HEAD")


def test_a_pyproject_without_a_ruff_table_or_no_config_is_not_ruff(tmp_path: Path) -> None:
    assert not ruff_configured(_repo(tmp_path / "a", {}), "HEAD")
    other = {"pyproject.toml": "[tool.black]\nline-length = 100\n"}
    assert not ruff_configured(_repo(tmp_path / "b", other), "HEAD")


def test_a_pyproject_that_does_not_parse_counts_as_configured(tmp_path: Path) -> None:
    """Known-bad input: unreadable settings must not silently switch the check off."""
    assert ruff_configured(_repo(tmp_path, {"pyproject.toml": "[tool.ruff\n"}), "HEAD")


def _ruff_row(tmp_path: Path, files: dict[str, str], text: str) -> tuple[str, str]:
    root = _repo(tmp_path, files)
    found = Auditor(root).tier0("app.py", text)
    (row,) = [f for f in found.findings if f.gate == "ruff"]
    return row.verdict, row.detail


def test_tier0_checks_format_only_where_the_project_configures_ruff(tmp_path: Path) -> None:
    verdict, detail = _ruff_row(tmp_path / "plain", {}, LONG)
    assert verdict == "pass"
    assert FORMAT_NOT_CONFIGURED in detail
    configured = {"pyproject.toml": CONFIGS["pyproject.toml"]}
    verdict, detail = _ruff_row(tmp_path / "ruff", configured, LONG)
    assert verdict == "fail"
    assert "ruff format would reformat it" in detail


def test_tier0_runs_the_defect_rules_on_every_project(tmp_path: Path) -> None:
    verdict, detail = _ruff_row(tmp_path, {}, UNDEFINED)
    assert verdict == "fail"
    assert "F821" in detail


def test_the_node_gate_checks_format_only_where_the_project_configures_ruff(
    tmp_path: Path,
) -> None:
    def ruff_check(files: dict[str, str]) -> tuple[bool, str]:
        root = _repo(tmp_path / str(len(list(tmp_path.iterdir()))), files)
        (root / "app.py").write_text(LONG)
        result = audit_tree(root, "HEAD", test_command="python -c pass")
        (check,) = [c for c in result.checks if c.name == "ruff"]
        return check.status == "pass", check.detail

    passed, detail = ruff_check({})
    assert passed
    assert FORMAT_NOT_CONFIGURED in detail
    passed, detail = ruff_check({"ruff.toml": CONFIGS["ruff.toml"]})
    assert not passed
    assert "ruff format --check exited 1" in detail
