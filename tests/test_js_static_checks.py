"""The JavaScript linter's and the type checker's contracts, by instance.

`check.sh` runs ESLint and `tsc` over the browser files and the node test
drivers. A check that exists proves nothing about what it rejects, so each
test here feeds a known-bad file and a known-good file through the repo's own
configuration and asserts the verdicts differ; the tracked-file tests make a
new JavaScript file that no config group covers fail instead of going unlinted.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
STATIC = "src/saddle/web/static"
ESLINT_CONFIG = REPO / "eslint.config.mjs"


def _tool(name: str) -> str:
    # Missing node tooling must read as a failure, never as "nothing to lint".
    # The local binary, not `npx`, because these tests run in a temp directory
    # that has no node_modules of its own.
    path = REPO / "node_modules" / ".bin" / name
    assert path.exists(), f"{path} is missing: run `npm ci` (check.sh does)"
    return str(path)


def _node() -> str:
    node = shutil.which("node")
    assert node, "node is required: the JavaScript checks are part of the gate"
    return node


def _eslint(cwd: Path, *files: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [_tool("eslint"), "--config", str(ESLINT_CONFIG), *files],
        cwd=cwd,
        capture_output=True,
        text=True,
        timeout=120,
    )


def _write(root: Path, rel: str, text: str) -> str:
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)
    return rel


def _tracked_js() -> list[str]:
    out = subprocess.run(
        ["git", "ls-files", "*.js", "*.mjs", "*.cjs"],
        cwd=REPO,
        capture_output=True,
        text=True,
        check=True,
    ).stdout.split()
    return sorted(out)


GOOD_STATIC = "function ok(items) {\n  return items.filter((item) => item === 1).length;\n}\n"

BAD_STATIC = [
    # An undefined global: a typo, or a sibling script's name left off the list.
    ("no-undef", "function f() {\n  return missingGlobal();\n}\n"),
    ("no-unused-vars", "function f() {\n  const unusedLocal = 1;\n  return 2;\n}\n"),
    ("eqeqeq", "function f(a, b) {\n  return a == b;\n}\n"),
    ("no-shadow", "function f(a) {\n  return [1].map((a) => a);\n}\n"),
]


@pytest.mark.parametrize(("rule", "source"), BAD_STATIC)
def test_eslint_rejects_a_bad_browser_file(tmp_path: Path, rule: str, source: str) -> None:
    rel = _write(tmp_path, f"{STATIC}/tasks.js", source)
    result = _eslint(tmp_path, rel)
    assert result.returncode != 0, result.stdout
    assert rule in result.stdout


def test_eslint_accepts_a_good_browser_file(tmp_path: Path) -> None:
    rel = _write(tmp_path, f"{STATIC}/tasks.js", GOOD_STATIC)
    result = _eslint(tmp_path, rel)
    assert result.returncode == 0, result.stdout


def test_a_sibling_scripts_global_is_known_but_not_ones_own(tmp_path: Path) -> None:
    # `$` is app.js's: a global to every other script, undefined to app.js
    # itself (which must declare it, not inherit it).
    use = "function f() {\n  return $('#x');\n}\n"
    sibling = _eslint(tmp_path, _write(tmp_path, f"{STATIC}/tasks.js", use))
    own = _eslint(tmp_path, _write(tmp_path, f"{STATIC}/app.js", use))
    assert sibling.returncode == 0, sibling.stdout
    assert own.returncode != 0
    assert "no-undef" in own.stdout


def test_eslint_uses_node_globals_for_node_tests_and_not_in_the_browser(tmp_path: Path) -> None:
    use_process = "console.log(process.argv);\n"
    node_side = _eslint(tmp_path, _write(tmp_path, "tests/x.test.js", use_process))
    browser_side = _eslint(tmp_path, _write(tmp_path, f"{STATIC}/tasks.js", use_process))
    assert node_side.returncode == 0, node_side.stdout
    assert browser_side.returncode != 0
    assert "no-undef" in browser_side.stdout


def test_eslint_lints_a_bad_browser_driver_module(tmp_path: Path) -> None:
    imp = "import { join } from 'node:path';\n"
    bad = _write(tmp_path, "tests/fixtures/x_cdp.mjs", imp + "const y = 1;\n")
    good = _write(tmp_path, "tests/fixtures/y_cdp.mjs", imp + "export const z = join('a');\n")
    rejected = _eslint(tmp_path, bad)
    assert rejected.returncode != 0
    assert "no-unused-vars" in rejected.stdout
    assert _eslint(tmp_path, good).returncode == 0


def test_every_tracked_js_file_is_in_a_config_group_and_lints_clean() -> None:
    files = _tracked_js()
    assert files, "git ls-files found no JavaScript: the census itself is broken"
    probe = (
        "import { ESLint } from 'eslint';\n"
        "const eslint = new ESLint();\n"
        "const out = {};\n"
        "for (const f of process.argv.slice(1)) {\n"
        "  const ignored = await eslint.isPathIgnored(f);\n"
        "  const g = (await eslint.calculateConfigForFile(f)).languageOptions.globals ?? {};\n"
        "  out[f] = { ignored, browser: 'window' in g, node: 'process' in g };\n"
        "}\n"
        "console.log(JSON.stringify(out));\n"
    )
    result = subprocess.run(
        [_node(), "--input-type=module", "-e", probe, *files],
        cwd=REPO,
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert result.returncode == 0, result.stderr
    seen = json.loads(result.stdout)
    assert sorted(seen) == files
    for name, info in seen.items():
        assert not info["ignored"], f"{name} is ignored by the ESLint config"
        assert info["browser"] or info["node"], f"{name} is outside every environment group"
        if name.startswith(STATIC + "/"):
            assert info["browser"], f"{name} is a browser file without browser globals"
        else:
            assert info["node"], f"{name} is a node file without node globals"
    lint = subprocess.run(
        [_tool("eslint"), *files],
        cwd=REPO,
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert lint.returncode == 0, lint.stdout
