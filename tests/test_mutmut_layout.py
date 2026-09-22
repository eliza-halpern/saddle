"""The mutant work copy must be able to run the tests it carries.

mutmut runs pytest from a `mutants/` copy containing only source_paths,
also_copy, and tests. Two ways a test module breaks there whatever the
mutation, and each kills every mutant, so the Tier-2 self-mutation score
(ARCHITECTURE.md §3) reads 100% while measuring nothing:

1. An import outside the copy at module level breaks collection for every
   mutant run. `test_mutant_layout_collects` rebuilds the layout from
   pyproject and proves collection succeeds.
2. A read outside the copy at run time -- `docs/ARCHITECTURE.md`, a
   `tools` import inside a test body -- collects fine and fails only when
   the test runs. Collect-only cannot see it, so
   `test_mutant_layout_runs_the_tests_that_read_outside_src` runs those
   modules in the rebuilt layout and requires them green. The rest of the
   suite stays collect-only here: a full run in the layout is minutes and
   belongs in CI's time-boxed mutation run, not check.sh.

A test that reads outside src/ and tests/ has to be listed in
OUTSIDE_READERS, or the work copy is back to killing mutants by accident.
"""

from __future__ import annotations

import shutil
import subprocess
import sys
import tomllib
from collections.abc import Iterator
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]

# Test modules that read the repo outside src/ and tests/, as pytest node
# ids. Each must pass, unmutated, in the work copy; an id that no longer
# exists is a pytest usage error (exit 4), which is red here, not silence.
OUTSIDE_READERS: tuple[tuple[str, ...], ...] = (
    # reads docs/ARCHITECTURE.md
    ("tests/test_docs.py",),
    # imports tools.diff_grammar_check inside the test body
    ("tests/test_vllm.py::test_diff_grammar_requires_the_file_lines_before_a_hunk",),
    # imports tools.edit_grammar_check inside the test body
    (
        "tests/test_edits.py::test_the_grammar_bounds_a_run_of_blank_content_lines",
        "tests/test_edits.py::test_the_parser_accepts_every_payload_the_grammar_calls_complete",
        "tests/test_edits.py::test_nothing_the_grammar_refuses_reaches_the_tree",
    ),
)


@pytest.fixture(scope="module")
def layout(tmp_path_factory: pytest.TempPathFactory) -> Iterator[tuple[Path, list[str]]]:
    """The work copy mutmut would build: source_paths + also_copy + tests."""
    with open(ROOT / "pyproject.toml", "rb") as fh:
        mutmut_cfg = tomllib.load(fh)["tool"]["mutmut"]
    root = tmp_path_factory.mktemp("layout") / "mutants"
    copied = [*mutmut_cfg["source_paths"], *mutmut_cfg.get("also_copy", []), "tests"]
    for rel in copied:
        dest = root / rel
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copytree(ROOT / rel, dest, ignore=shutil.ignore_patterns("__pycache__"))
    yield root, list(mutmut_cfg.get("pytest_add_cli_args", []))
    shutil.rmtree(root, ignore_errors=True)


def _pytest(layout: tuple[Path, list[str]], *args: str) -> subprocess.CompletedProcess[str]:
    root, cli_args = layout
    return subprocess.run(
        [sys.executable, "-m", "pytest", "-p", "no:cacheprovider", *cli_args, *args],
        cwd=root,
        capture_output=True,
        text=True,
    )


def test_mutant_layout_collects(layout: tuple[Path, list[str]]) -> None:
    proc = _pytest(layout, "--collect-only")
    assert proc.returncode == 0, (proc.stdout + proc.stderr)[-2000:]


@pytest.mark.parametrize("target", OUTSIDE_READERS, ids=lambda t: t[0])
def test_mutant_layout_runs_the_tests_that_read_outside_src(
    layout: tuple[Path, list[str]], target: tuple[str, ...]
) -> None:
    proc = _pytest(layout, *target)
    assert proc.returncode == 0, (proc.stdout + proc.stderr)[-2000:]
