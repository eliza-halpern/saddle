"""The node tests' coverage requirement, by instance, and the list it applies to.

`check.sh` runs the node tests under c8 and requires 100% lines, branches and
functions on every browser file listed as measured in
`tests/fixtures/js_coverage_scope.json`. A requirement that is never seen to
fail proves nothing, so the same command is run here on a known-good tree
(green) and on two known-bad ones (a covered file that gains an untested
branch; a suite that loses the test for one behaviour), each red. The other
browser files are 1,000-line scripts over the live DOM that only the
Chrome-driven tests run; they are named in the same file, each with its
reason, and the census test makes a new script fail until it is placed in
exactly one list, so "not measured" is a decision and never an accident.
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest
from test_check_sh import check_sh_stages

REPO = Path(__file__).resolve().parent.parent
STATIC = "src/saddle/web/static"
SCOPE = "tests/fixtures/js_coverage_scope.json"

# Needs the checkout's node_modules, configs and git index (see test_mutmut_layout).
pytestmark = pytest.mark.skipif(
    not (REPO / ".git").exists(),
    reason="needs a git checkout with node_modules, not a mutant work copy",
)


def _scope() -> tuple[list[str], dict[str, str]]:
    """(measured files, not-measured file -> reason) from the scope file."""
    data = json.loads((REPO / SCOPE).read_text())
    return list(data["measured"]), dict(data["not_measured"])


def census_problems(
    tracked: list[str], measured: list[str], not_measured: dict[str, str]
) -> list[str]:
    """What is wrong with how the tracked browser scripts are split into the two lists."""
    problems = [
        f"{f}: in neither list" for f in tracked if f not in measured and f not in not_measured
    ]
    problems += [f"{f}: in both lists" for f in measured if f in not_measured]
    listed = [*measured, *not_measured]
    problems += [f"{f}: listed but not a tracked script" for f in listed if f not in tracked]
    problems += [f"{f}: no reason given" for f, why in not_measured.items() if not why.strip()]
    return problems


def _tracked_static_js() -> list[str]:
    out = subprocess.run(
        ["git", "ls-files", f"{STATIC}/*.js"], cwd=REPO, capture_output=True, text=True, check=True
    ).stdout.split()
    return sorted(out)


def _c8_args() -> list[str]:
    stages = [s for s in check_sh_stages((REPO / "check.sh").read_text()) if s.key == "c8"]
    assert len(stages) == 1, "check.sh must run the node tests under c8 exactly once"
    return list(stages[0].args)


def _c8(cwd: Path) -> subprocess.CompletedProcess[str]:
    c8 = REPO / "node_modules" / ".bin" / "c8"
    assert c8.exists(), f"{c8} is missing: run `npm ci` (check.sh does)"
    node = shutil.which("node")
    assert node, "node is required: the JavaScript checks are part of the gate"
    return subprocess.run(
        [str(c8), *_c8_args()], cwd=cwd, capture_output=True, text=True, timeout=180, check=False
    )


def _tree(root: Path) -> Path:
    """A copy of what the c8 command needs: its config, the suite, the shim, the scripts."""
    for rel in [".c8rc.json", *_c8_test_files(), "tests/fixtures/dom_shim.js"]:
        (root / rel).parent.mkdir(parents=True, exist_ok=True)
        shutil.copy(REPO / rel, root / rel)
    shutil.copy(
        REPO / "tests/fixtures/classic_script.js", root / "tests/fixtures/classic_script.js"
    )
    shutil.copytree(REPO / STATIC, root / STATIC)
    return root


def _c8_test_files() -> list[str]:
    return [a for a in _c8_args() if a.endswith(".test.js")]


def test_the_real_tree_meets_the_requirement() -> None:
    result = _c8(REPO)
    assert result.returncode == 0, result.stdout + result.stderr
    # The report names every measured file, so a vacuous run (nothing measured) is visible.
    for rel in _scope()[0]:
        assert Path(rel).name in result.stdout, result.stdout


def test_a_measured_file_with_an_untested_branch_fails(tmp_path: Path) -> None:
    root = _tree(tmp_path)
    target = root / STATIC / "markdown.js"
    target.write_text(target.read_text() + "\nfunction neverCalled(x) {\n  return x ? 1 : 2;\n}\n")
    result = _c8(root)
    assert result.returncode != 0, result.stdout
    assert re.search(r"ERROR: Coverage for (lines|functions|branches|statements)", result.stderr)


def test_a_suite_that_loses_a_behaviours_test_fails(tmp_path: Path) -> None:
    root = _tree(tmp_path)
    suite = root / "tests" / "markdown.test.js"
    text = suite.read_text()
    start = text.index('test("underscore marks render like asterisk marks"')
    end = text.index("\ntest(", start)
    suite.write_text(text[:start] + text[end:])
    result = _c8(root)
    assert result.returncode != 0, result.stdout
    assert "ERROR: Coverage for" in result.stderr


def test_the_config_measures_exactly_the_files_listed_as_measured() -> None:
    config = json.loads((REPO / ".c8rc.json").read_text())
    assert sorted(config["include"]) == sorted(_scope()[0])
    for key in ("lines", "statements", "branches", "functions"):
        assert config[key] == 100, f"{key} must stay at 100 for every measured file"
    assert config["perFile"] is True
    assert config["checkCoverage"] is True


def test_every_tracked_browser_script_is_in_exactly_one_list() -> None:
    tracked = _tracked_static_js()
    assert len(tracked) >= 5, f"git ls-files found {tracked}: the census itself is broken"
    measured, not_measured = _scope()
    assert census_problems(tracked, measured, not_measured) == []


def test_the_census_rejects_an_unplaced_doubled_stale_or_unexplained_script() -> None:
    tracked = ["a.js", "b.js", "c.js"]
    assert census_problems(tracked, ["a.js"], {"b.js": "why", "c.js": "why"}) == []
    assert census_problems([*tracked, "new.js"], ["a.js"], {"b.js": "why", "c.js": "why"}) == [
        "new.js: in neither list"
    ]
    assert census_problems(tracked, ["a.js", "b.js"], {"b.js": "why", "c.js": "why"}) == [
        "b.js: in both lists"
    ]
    assert census_problems(tracked, ["a.js", "gone.js"], {"b.js": "why", "c.js": "why"}) == [
        "gone.js: listed but not a tracked script"
    ]
    assert census_problems(tracked, ["a.js"], {"b.js": "why", "c.js": " "}) == [
        "c.js: no reason given"
    ]
