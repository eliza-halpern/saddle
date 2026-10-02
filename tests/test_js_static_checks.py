"""The JavaScript linter's and the type checker's contracts, by instance.

`check.sh` runs ESLint and `tsc` over the browser files and the node test
drivers. A check that exists proves nothing about what it rejects, so each
test here feeds a known-bad file and a known-good file through the repo's own
configuration and asserts the verdicts differ; the tracked-file tests make a
new JavaScript file that no config group covers fail instead of going unlinted.
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
STATIC = "src/saddle/web/static"
ESLINT_CONFIG = REPO / "eslint.config.mjs"

# The checks need the repo's node_modules, configs and git index, none of which
# mutmut's work copy carries (see test_mutmut_layout). The marker is `.git`,
# not the config files, so deleting a config in a real checkout fails the
# tests instead of skipping them.
pytestmark = pytest.mark.skipif(
    not (REPO / ".git").exists(),
    reason="needs a git checkout with node_modules, not a mutant work copy",
)


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


# ---------------------------------------------------------------- tsc (types)

# Static scripts that may skip `// @ts-check`, each with its reason. Empty on
# purpose: a new entry is a decision, not a default.
TS_CHECK_EXCEPTIONS: dict[str, str] = {}

GOOD_TS = (
    "/** @param {string} s */\n"
    "function size(s) {\n"
    "  return s.length;\n"
    "}\n"
    "size('abc');\n"
    "Array.from(document.body.children).find((c) => c.tagName === 'SUMMARY');\n"
)

BAD_TS = [
    # A number where a string is declared.
    ("TS2345", "/** @param {string} s */\nfunction size(s) {\n  return s.length;\n}\nsize(1);\n"),
    # HTMLCollection has no find: the defect fillToolDetail shipped with, which
    # threw in a real browser while the node test's array-backed fake passed.
    ("TS2339", "document.body.children.find((c) => c.tagName === 'SUMMARY');\n"),
    # A parameter nobody typed.
    ("TS7006", "function size(s) {\n  return s.length;\n}\nsize('a');\n"),
]


def _tsc(cwd: Path, source: str) -> subprocess.CompletedProcess[str]:
    """Check `source` under the repo's compiler options, in a temp directory."""
    (cwd / "x.js").write_text(source)
    (cwd / "tsconfig.json").write_text(
        json.dumps({"extends": str(REPO / "tsconfig.json"), "include": ["x.js"]})
    )
    return subprocess.run(
        [_tool("tsc"), "-p", str(cwd / "tsconfig.json")],
        cwd=cwd,
        capture_output=True,
        text=True,
        timeout=120,
    )


@pytest.mark.parametrize(("code", "source"), BAD_TS)
def test_tsc_rejects_a_bad_script(tmp_path: Path, code: str, source: str) -> None:
    result = _tsc(tmp_path, source)
    assert result.returncode != 0, result.stdout
    assert code in result.stdout


def test_tsc_accepts_a_good_script(tmp_path: Path) -> None:
    result = _tsc(tmp_path, GOOD_TS)
    assert result.returncode == 0, result.stdout


def test_tsc_passes_on_the_real_browser_scripts() -> None:
    result = subprocess.run(
        [_tool("tsc"), "-p", "tsconfig.json"],
        cwd=REPO,
        capture_output=True,
        text=True,
        timeout=180,
    )
    assert result.returncode == 0, result.stdout


def test_tsconfig_lists_every_static_script() -> None:
    on_disk = sorted(p.name for p in (REPO / STATIC).glob("*.js"))
    assert on_disk, "no static scripts found: the census itself is broken"
    result = subprocess.run(
        [_tool("tsc"), "-p", "tsconfig.json", "--listFilesOnly"],
        cwd=REPO,
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert result.returncode == 0, result.stderr
    listed = {Path(line).name for line in result.stdout.splitlines() if f"/{STATIC}/" in line}
    assert sorted(listed) == on_disk


def test_every_static_script_opts_in_to_ts_check() -> None:
    scripts = sorted((REPO / STATIC).glob("*.js"))
    assert scripts, "no static scripts found: the census itself is broken"
    for script in scripts:
        if script.name in TS_CHECK_EXCEPTIONS:
            continue
        first = script.read_text().splitlines()[0]
        assert first == "// @ts-check", f"{script.name} does not start with // @ts-check"


def test_idtypes_names_only_elements_index_html_declares_with_that_tag() -> None:
    # `$` returns IdTypes[selector]; a stale entry would type an element as
    # something it is not, which is worse than the plain HTMLElement default.
    app = (REPO / STATIC / "app.js").read_text()
    html = (REPO / STATIC / "index.html").read_text()
    block = app[app.index("@typedef {{") : app.index("}} IdTypes")]
    declared = re.findall(r'"#([\w-]+)": HTML(\w+)Element', block)
    assert len(declared) > 20, "IdTypes was not parsed: the check would be vacuous"
    tags = {"Input": "input", "TextArea": "textarea", "Select": "select", "Dialog": "dialog"}
    tags |= {"Button": "button", "Form": "form", "Label": "label"}
    for ident, kind in declared:
        found = re.search(rf'<([a-z]+)\b[^>]*\bid="{re.escape(ident)}"', html)
        assert found, f"#{ident} is typed in IdTypes but not in index.html"
        assert found.group(1) == tags[kind], f"#{ident} is <{found.group(1)}>, typed as {kind}"


# ------------------------------------------------------------ prettier (format)


def _prettier(cwd: Path, *files: str) -> subprocess.CompletedProcess[str]:
    """`prettier --check` under copies of the repo's config and ignore file."""
    for name in (".prettierrc.json", ".prettierignore"):
        shutil.copy(REPO / name, cwd / name)
    return subprocess.run(
        [_tool("prettier"), "--check", *files],
        cwd=cwd,
        capture_output=True,
        text=True,
        timeout=120,
    )


FORMATTED = 'const a = { b: 1, c: [2, 3] };\nexport function f(x) {\n  return x + "s";\n}\n'
# Single quotes, no spaces in the braces, a missing semicolon: every one a prettier verdict.
UNFORMATTED = "const a = {b:1,c:[2,3]}\nexport function f(x){ return x + 's' }\n"


@pytest.mark.parametrize(
    "rel", ["tests/x.test.js", "tests/fixtures/x_cdp.mjs", "tests/fixtures/y.js"]
)
def test_prettier_rejects_an_unformatted_file_and_accepts_a_formatted_one(
    tmp_path: Path, rel: str
) -> None:
    bad = _prettier(tmp_path, _write(tmp_path, rel, UNFORMATTED))
    assert bad.returncode != 0, bad.stdout
    assert rel in bad.stderr
    assert _prettier(tmp_path, _write(tmp_path, rel, FORMATTED)).returncode == 0


def test_prettier_wraps_at_the_repo_width(tmp_path: Path) -> None:
    # 110 columns on one line is formatted at width 120 and a defect at the default 80.
    line = "const x = [" + ", ".join(["1"] * 36) + "];\n"
    assert 100 < len(line) <= 120
    assert _prettier(tmp_path, _write(tmp_path, "x.js", line)).returncode == 0


def test_prettier_leaves_fixture_json_bytes_alone(tmp_path: Path) -> None:
    # Fixture JSON keeps its exact bytes, so the ignore file hides it; JSON elsewhere is checked.
    messy = '{"a":1,\n"b":[1,2]}\n'
    fixture = _write(tmp_path, "tests/fixtures/g.json", messy)
    assert _prettier(tmp_path, fixture).returncode == 0
    assert _prettier(tmp_path, _write(tmp_path, "g.json", messy)).returncode != 0


def test_prettier_passes_on_the_real_tree() -> None:
    result = subprocess.run(
        [_tool("prettier"), "--check", "."], cwd=REPO, capture_output=True, text=True, timeout=180
    )
    assert result.returncode == 0, result.stderr


# ------------------------------------------- tsc over the node tests and drivers


def _tsc_tests(cwd: Path, rel: str, source: str) -> subprocess.CompletedProcess[str]:
    """Check one test-side file under tsconfig.tests.json's options, in a temp directory."""
    _write(cwd, rel, source)
    (cwd / "tsconfig.json").write_text(
        json.dumps(
            {
                "extends": str(REPO / "tsconfig.tests.json"),
                "include": [rel, str(REPO / "node-globals.d.ts")],
            }
        )
    )
    return subprocess.run(
        [_tool("tsc"), "-p", str(cwd / "tsconfig.json")],
        cwd=cwd,
        capture_output=True,
        text=True,
        timeout=120,
    )


# The globals file is listed too, so `require`, `process` and `node:` imports resolve.
GOOD_TEST_TS = (
    '"use strict";\n'
    'const assert = require("node:assert");\n'
    "/** @param {string} s */\n"
    "const size = (s) => s.length;\n"
    "assert.strictEqual(size('abc'), 3);\n"
)
BAD_TEST_TS = [
    ("TS2345", GOOD_TEST_TS.replace("size('abc')", "size(1)")),
    ("TS7006", GOOD_TEST_TS.replace("/** @param {string} s */\n", "")),
    ("TS18047", "/** @type {string | null} */\nlet s = null;\nconsole.log(s.length);\n"),
]


@pytest.mark.parametrize(("code", "source"), BAD_TEST_TS)
@pytest.mark.parametrize("rel", ["tests/x.test.js", "tests/fixtures/x_cdp.mjs"])
def test_tsc_rejects_a_type_error_in_a_test_side_file(
    tmp_path: Path, rel: str, code: str, source: str
) -> None:
    if rel.endswith(".mjs"):
        source = source.replace(
            'const assert = require("node:assert");', 'import assert from "node:assert";'
        )
    result = _tsc_tests(tmp_path, rel, source)
    assert result.returncode != 0, result.stdout
    assert code in result.stdout


def test_tsc_accepts_a_typed_test_side_file(tmp_path: Path) -> None:
    result = _tsc_tests(tmp_path, "tests/x.test.js", GOOD_TEST_TS)
    assert result.returncode == 0, result.stdout


def test_tsc_passes_on_the_real_node_tests_and_drivers() -> None:
    result = subprocess.run(
        [_tool("tsc"), "-p", "tsconfig.tests.json"],
        cwd=REPO,
        capture_output=True,
        text=True,
        timeout=180,
    )
    assert result.returncode == 0, result.stdout


def test_tsconfig_tests_lists_every_tracked_test_side_file() -> None:
    expected = sorted(f for f in _tracked_js() if f.startswith("tests/"))
    assert expected, "no tracked test-side JavaScript: the census itself is broken"
    result = subprocess.run(
        [_tool("tsc"), "-p", "tsconfig.tests.json", "--listFilesOnly"],
        cwd=REPO,
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert result.returncode == 0, result.stderr
    listed = {
        Path(line).relative_to(REPO).as_posix()
        for line in result.stdout.splitlines()
        if "/node_modules/" not in line and line.startswith(str(REPO))
    }
    assert set(expected) <= listed, sorted(set(expected) - listed)
