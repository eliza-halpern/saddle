"""The browser tests' shared guard: skip, or fail when `SADDLE_REQUIRE_BROWSER` is set.

Known-good: with node and Chrome on PATH the guard yields the Chrome path
and the browser tests run; without them and without the variable it yields
None and they skip; every driver in `tests/fixtures` is run by a module that
takes its guard from `browser_guard`. Known-bad: with the variable set and a
tool missing, importing the guard raises and names the tool; a driver no
module runs, and a module that guards itself with its own `which` pair, are
both reported.
"""

from __future__ import annotations

import ast
import re
import subprocess
import sys
from pathlib import Path

import pytest
from browser_guard import REQUIRE_ENV, BrowserRequiredError, resolve

HERE = Path(__file__).parent
# Built from fragments so this file does not contain the hand-rolled spelling it hunts.
CHROME = "google" + "-chrome"
SELF = Path(__file__).name


def _tools(directory: Path, *names: str) -> str:
    directory.mkdir(parents=True, exist_ok=True)
    for name in names:
        (directory / name).write_text("#!/bin/sh\nexit 0\n")
        (directory / name).chmod(0o755)
    return str(directory)


# -- the guard ---------------------------------------------------------------


def test_with_both_tools_the_guard_gives_the_chrome_path(tmp_path: Path) -> None:
    path = _tools(tmp_path / "bin", "node", CHROME)
    assert resolve({}, path) == str(tmp_path / "bin" / CHROME)
    assert resolve({REQUIRE_ENV: "1"}, path) == str(tmp_path / "bin" / CHROME)  # strict still runs


@pytest.mark.parametrize("have", [(), ("node",), (CHROME,)], ids=["neither", "node", "chrome"])
@pytest.mark.parametrize(
    "env", [{}, {REQUIRE_ENV: ""}, {REQUIRE_ENV: "0"}], ids=["unset", "empty", "zero"]
)
def test_a_missing_tool_without_the_variable_means_skip(
    tmp_path: Path, have: tuple[str, ...], env: dict[str, str]
) -> None:
    assert resolve(env, _tools(tmp_path / "bin", *have)) is None


@pytest.mark.parametrize(
    ("have", "named"),
    [((), "node and google-chrome"), (("node",), "google-chrome"), ((CHROME,), "node")],
    ids=["neither", "no-chrome", "no-node"],
)
def test_a_missing_tool_with_the_variable_set_raises_and_names_it(
    tmp_path: Path, have: tuple[str, ...], named: str
) -> None:
    with pytest.raises(BrowserRequiredError) as caught:
        resolve({REQUIRE_ENV: "1"}, _tools(tmp_path / "bin", *have))
    assert f"{REQUIRE_ENV} is set" in str(caught.value)
    assert str(caught.value).endswith(f"and {named} is not on PATH")


def test_importing_the_guard_is_what_raises_so_a_module_cannot_skip_past_it(
    tmp_path: Path,
) -> None:
    """The module-level `BROWSER` is computed at import: a test module that
    imports it dies at collection under the variable, with the message."""
    empty = _tools(tmp_path / "empty")
    program = f"import sys; sys.path.insert(0, {str(HERE)!r}); import browser_guard"
    base = {"PATH": empty, "PYTHONDONTWRITEBYTECODE": "1"}
    strict = subprocess.run(
        [sys.executable, "-c", program],
        env={**base, REQUIRE_ENV: "1"},
        capture_output=True,
        text=True,
        check=False,
    )
    assert strict.returncode != 0
    assert "BrowserRequiredError" in strict.stderr
    assert "node and google-chrome is not on PATH" in strict.stderr
    lax = subprocess.run(
        [sys.executable, "-c", program], env=base, capture_output=True, text=True, check=False
    )
    assert lax.returncode == 0, lax.stderr


# -- the census --------------------------------------------------------------


def _imports_browser(module: Path) -> list[str]:
    """Modules `module` imports `BROWSER` from."""
    found: list[str] = []
    for node in ast.walk(ast.parse(module.read_text(encoding="utf-8"))):
        if isinstance(node, ast.ImportFrom) and node.module:
            if any(alias.name == "BROWSER" for alias in node.names):
                found.append(node.module)
    return found


def guard_problems(tests: Path) -> list[str]:
    """Every way a browser driver can be skipped by a guard of its own.

    A driver no `test_*.py` names is never run; a module that names one must
    import `BROWSER` from `browser_guard` or from a sibling that does, and
    guard on it; and no other module spells out the Chrome lookup."""
    drivers = sorted(path.name for path in (tests / "fixtures").glob("*_cdp.mjs"))
    # This module holds the known-bad spellings as test data, so it is not scanned.
    modules = {path.stem: path for path in sorted(tests.glob("test_*.py")) if path.name != SELF}
    text = {name: path.read_text(encoding="utf-8") for name, path in modules.items()}
    problems = [
        f"{driver} is run by no test module"
        for driver in drivers
        if not any(driver in body for body in text.values())
    ]
    for name, body in text.items():
        if re.search(rf"which\(\s*[\"']{CHROME}[\"']", body):
            problems.append(f"{name}.py looks for Chrome itself instead of using browser_guard")

    def guarded(name: str, seen: frozenset[str] = frozenset()) -> bool:
        if name in seen or name not in modules:
            return False
        return any(
            source == "browser_guard" or guarded(source, seen | {name})
            for source in _imports_browser(modules[name])
        )

    for name, body in text.items():
        if not any(driver in body for driver in drivers):
            continue
        if not guarded(name):
            problems.append(f"{name}.py runs a driver but does not take BROWSER from browser_guard")
        elif not re.search(r"skipif\(\s*not BROWSER", body):
            problems.append(f"{name}.py imports BROWSER but never skips on it")
    return problems


def test_every_driver_is_run_by_a_module_that_takes_the_shared_guard() -> None:
    assert list((HERE / "fixtures").glob("*_cdp.mjs")), "a real checkout holds the drivers"
    assert guard_problems(HERE) == []


def _tree(root: Path, drivers: list[str], modules: dict[str, str]) -> Path:
    (root / "fixtures").mkdir(parents=True)
    for driver in drivers:
        (root / "fixtures" / driver).write_text("")
    for name, body in modules.items():
        (root / name).write_text(body)
    return root


GOOD = (
    "from browser_guard import BROWSER\nimport pytest\n"
    "CDP = 'fixtures/a_cdp.mjs'\n"
    "@pytest.mark.skipif(not BROWSER, reason='x')\ndef test_a(): pass\n"
)


def test_a_module_on_the_shared_guard_and_one_borrowing_it_pass(tmp_path: Path) -> None:
    borrowed = "from test_a import BROWSER\nCDP = 'fixtures/b_cdp.mjs'\nskipif(not BROWSER)\n"
    tree = _tree(tmp_path, ["a_cdp.mjs", "b_cdp.mjs"], {"test_a.py": GOOD, "test_b.py": borrowed})
    assert guard_problems(tree) == []


@pytest.mark.parametrize(
    ("body", "problem"),
    [
        (
            f"import shutil\nBROWSER = shutil.which('node') and shutil.which('{CHROME}')\n"
            "CDP = 'fixtures/a_cdp.mjs'\nskipif(not BROWSER)\n",
            "test_a.py looks for Chrome itself",
        ),
        (
            "BROWSER = None\nCDP = 'fixtures/a_cdp.mjs'\nskipif(not BROWSER)\n",
            "test_a.py runs a driver but does not take BROWSER from browser_guard",
        ),
        (
            "from browser_guard import BROWSER\nCDP = 'fixtures/a_cdp.mjs'\n",
            "test_a.py imports BROWSER but never skips on it",
        ),
    ],
    ids=["hand-rolled", "own-name", "never-skips"],
)
def test_a_module_with_its_own_guard_is_reported(tmp_path: Path, body: str, problem: str) -> None:
    tree = _tree(tmp_path, ["a_cdp.mjs"], {"test_a.py": body})
    assert any(problem in found for found in guard_problems(tree))


def test_a_driver_no_module_runs_is_reported(tmp_path: Path) -> None:
    tree = _tree(tmp_path, ["a_cdp.mjs", "lost_cdp.mjs"], {"test_a.py": GOOD})
    assert guard_problems(tree) == ["lost_cdp.mjs is run by no test module"]
