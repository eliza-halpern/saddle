"""The project's node gate stages (`npx --no-install eslint .`) run in the audit sandbox.

Contract: with the checkout's `node_modules` installed, a `gate-checks` stage spelled
`npx --no-install X` runs X's own script under node, on the head and on the baseline,
and a regression it reports refuses; with `node_modules` absent the stage is
not-proven, naming the tool, never a pass. Known-good: a lint-clean JS change passes.
Known-bad: a change adding an eslint error fails and names it; no `node_modules`.
Real node and a real eslint, so they skip where either is missing.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest
from test_auditor import _init

from saddle.auditor import PROJECT_GATE, Auditor, Finding, _gate_stage_runs, node_stage

REPO = Path(__file__).resolve().parents[1]
needs_eslint = pytest.mark.skipif(
    shutil.which("node") is None or not (REPO / "node_modules/.bin/eslint").exists(),
    reason="node or eslint is not installed",
)

CONFIG = (
    "export default [{ files: ['**/*.js'], languageOptions: { sourceType: 'commonjs' },"
    " rules: { 'no-unused-vars': 'error' } }];\n"
)
BASE_JS = "function add(a, b) {\n  return a + b;\n}\nmodule.exports = { add };\n"
CLEAN_JS = BASE_JS.replace("a + b", "b + a")
BAD_JS = BASE_JS.replace("return", "const unused = 1;\n  return")
FILES = {
    "n.py": "def f():\n    return 1\n",
    "a.js": BASE_JS,
    "eslint.config.mjs": CONFIG,
    "pyproject.toml": (
        '[tool.saddle]\nsandbox-expose = ["node"]\n'
        'gate-checks = [["npx", "--no-install", "eslint", "."]]\n'
    ),
    ".gitignore": "node_modules\n",
}


def project(tmp_path: Path, head_js: str, *, modules: bool = True) -> Path:
    tree = tmp_path / "tree"
    _init(tree, FILES)
    (tree / "a.js").write_text(head_js)
    if modules:
        (tree / "node_modules").symlink_to(REPO / "node_modules")
    return tree


def gate(tree: Path) -> Finding:
    return {f.gate: f for f in Auditor(tree).tier1().findings}[PROJECT_GATE]


def status(tree: Path) -> str:
    return subprocess.run(
        ["git", "status", "--porcelain"], cwd=tree, capture_output=True, text=True, check=True
    ).stdout


@needs_eslint
def test_a_lint_clean_js_change_passes_the_eslint_stage(tmp_path: Path) -> None:
    tree = project(tmp_path, CLEAN_JS)
    found = gate(tree)
    assert found.verdict == "pass", found.detail
    assert found.detail.splitlines()[0] == "Gate: base ✓, head ✓ (1 stage)"
    assert status(tree).strip() == "M a.js"


@needs_eslint
def test_a_js_change_with_an_eslint_error_refuses_naming_the_rule(tmp_path: Path) -> None:
    found = gate(project(tmp_path, BAD_JS))
    assert found.verdict == "fail"
    assert "eslint: the change broke it, it passed at the base" in found.detail
    assert "no-unused-vars" in found.detail


def test_without_node_modules_the_stage_is_not_proven_and_names_the_tool(tmp_path: Path) -> None:
    found = gate(project(tmp_path, CLEAN_JS, modules=False))
    assert found.verdict == "not-proven"
    assert "eslint" in found.detail


def test_node_stage_maps_only_an_installed_npx_tool_to_its_script(tmp_path: Path) -> None:
    bin_dir = tmp_path / "node_modules/.bin"
    bin_dir.mkdir(parents=True)
    (bin_dir / "eslint").write_text("")
    npx = ("npx", "--no-install", "eslint", ".")
    assert node_stage(npx, tmp_path) == ("node", str((bin_dir / "eslint").resolve()), ".")
    assert node_stage(("npx", "--no-install", "tsc"), tmp_path) == ("npx", "--no-install", "tsc")
    assert node_stage(npx, None) == npx
    assert node_stage(("ruff", "check"), tmp_path) == ("ruff", "check")


@needs_eslint
def test_the_node_modules_link_is_removed_and_a_real_directory_is_left_alone(
    tmp_path: Path,
) -> None:
    stage = [("npx", "--no-install", "eslint", "--version")]
    tree = project(tmp_path, CLEAN_JS, modules=False)
    _gate_stage_runs(stage, tree, 60, REPO)
    assert not (tree / "node_modules").exists()
    (tree / "node_modules").mkdir()
    _gate_stage_runs(stage, tree, 60, REPO)
    assert (tree / "node_modules").is_dir()
    assert not (tree / "node_modules").is_symlink()
