"""`check.sh`'s mypy line covers the Python that lives outside `src/` and `tests/`.

`tools/*.py` and `benchmark/stall_check.py` are scripts, not packages. mypy
refuses a script given on the command line that a test also imports as
`tools.x`: "source file found twice under different module names". The repo
config (`explicit_package_bases` and `mypy_path`) gives each file one name;
these tests run the exact command `check.sh` runs, with the repo's own
`[tool.mypy]` table, on a scratch tree that holds copies of those files.

The scratch tree carries a stand-in `saddle` package with the three names
the tools import, so the run takes seconds and does not re-check `src/`
(the gate does that on the real tree).
"""

from __future__ import annotations

import re
import shlex
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

SCRIPTS = (
    "tools/diff_grammar_check.py",
    "tools/edit_grammar_check.py",
    "tools/structured_output_probe.py",
    "benchmark/stall_check.py",
)

STAND_IN = {
    "src/saddle/__init__.py": "",
    "src/saddle/vllm.py": (
        "from typing import Final\n"
        'DEFAULT_BASE_URL: Final = "http://127.0.0.1:1/v1"\n'
        'DEFAULT_MODEL: Final = "m"\n'
        'DIFF_GRAMMAR: Final = "g"\n'
    ),
    "src/saddle/edits.py": 'from typing import Final\nEDIT_GRAMMAR: Final = "g"\n',
    # The way the real tests reach the scripts: as `tools.x` and `benchmark.x`.
    "tests/test_importer.py": (
        "from benchmark import stall_check\n"
        "from tools import diff_grammar_check, edit_grammar_check, structured_output_probe\n"
        "USED = (stall_check, diff_grammar_check, edit_grammar_check, structured_output_probe)\n"
    ),
}


def _gate_command() -> list[str]:
    """The mypy command line `check.sh` runs, without its `uv run` prefix."""
    lines = [
        line.strip()
        for line in (ROOT / "check.sh").read_text().splitlines()
        if re.match(r"\s*uv run mypy\b", line)
    ]
    assert len(lines) == 1, f"expected one mypy line in check.sh, found {lines}"
    return shlex.split(lines[0])[2:]


def _tree(tmp_path: Path) -> Path:
    tree = tmp_path / "tree"
    tree.mkdir()
    shutil.copy(ROOT / "pyproject.toml", tree / "pyproject.toml")
    for rel, text in STAND_IN.items():
        (tree / rel).parent.mkdir(parents=True, exist_ok=True)
        (tree / rel).write_text(text)
    for rel in SCRIPTS:
        (tree / rel).parent.mkdir(parents=True, exist_ok=True)
        shutil.copy(ROOT / rel, tree / rel)
    return tree


def _mypy(tree: Path, cache: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-m", *_gate_command(), f"--cache-dir={cache}"],
        cwd=tree,
        capture_output=True,
        text=True,
        check=False,
    )


def test_the_gate_command_names_every_script(tmp_path: Path) -> None:
    """Known-good: the scripts as they stand pass the command `check.sh` runs."""
    proc = _mypy(_tree(tmp_path), tmp_path / "cache")

    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert "Success: no issues found in 8 source files" in proc.stdout


def test_a_type_error_in_any_script_fails_the_gate_command(tmp_path: Path) -> None:
    """Known-bad: one wrongly typed line in each script is reported, in that script.

    All four are broken in one run and each must be named: a command that
    dropped any one of them would leave that script out of the report.
    """
    tree = _tree(tmp_path)
    # No test imports the scripts here: mypy follows imports, so an importer
    # would report the error even from a command that never named the script.
    (tree / "tests" / "test_importer.py").write_text("")
    for script in SCRIPTS:
        target = tree / script
        target.write_text(target.read_text() + "\nWRONG: int = 'not an int'\n")

    proc = _mypy(tree, tmp_path / "cache")

    assert proc.returncode == 1, proc.stdout + proc.stderr
    errors = [line for line in proc.stdout.splitlines() if ": error:" in line]
    assert sorted(line.split(":")[0] for line in errors) == sorted(SCRIPTS)
    assert all("Incompatible types in assignment" in line for line in errors)


def test_the_config_gives_each_script_one_module_name(tmp_path: Path) -> None:
    """Without the config the command fails with "found twice": the config is load-bearing."""
    tree = _tree(tmp_path)
    (tree / "pyproject.toml").write_text("[tool.mypy]\nstrict = true\n")

    proc = _mypy(tree, tmp_path / "cache")

    assert proc.returncode != 0
    assert "found twice" in proc.stdout + proc.stderr
