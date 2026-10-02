"""Line coverage of the browser scripts, collected from the Chrome-driven tests.

Known-good: with `SADDLE_JS_COVERAGE_DIR` set, a driver run in a real Chrome
leaves V8 coverage files that c8 reads as the checkout's own scripts, and a
line the page ran (inside `attachCopy`, which the copy-button driver clicks)
reports hits. Known-bad: a line of the same script the driver never reaches
(inside `deleteSession`, which only the runs driver uses) reports zero hits,
and `tools/chrome_coverage.mjs check` fails a file below its floor naming the
file and the metric; with no data it says "not measured here" and exits 0, and
under `SADDLE_REQUIRE_BROWSER` the same emptiness is an error. A script served
from anywhere but /static/, or by a path that climbs out of it, is never
attributed to a file of the checkout.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
from pathlib import Path

import pytest
from browser_guard import BROWSER
from test_card_ui import (
    test_the_cards_meters_survive_a_reload_and_the_strip_is_not_evidence as run_card_driver,
)
from test_check_sh import check_sh_stages
from test_copy_button_ui import drive
from test_js_coverage import STATIC
from test_web_tasks import repo  # noqa: F401 -- the fixture

REPO = Path(__file__).resolve().parent.parent
TOOL = "tools/chrome_coverage.mjs"
SCOPE = "tests/fixtures/js_coverage_scope.json"
needs_checkout = pytest.mark.skipif(
    not (REPO / ".git").exists(), reason="needs a git checkout with node_modules"
)
needs_chrome = pytest.mark.skipif(not BROWSER, reason="needs node and google-chrome")


def _node(script: str) -> object:
    ran = subprocess.run(
        ["node", "--input-type=module", "-e", script],
        cwd=REPO,
        capture_output=True,
        text=True,
        timeout=60,
        check=True,
    )
    return json.loads(ran.stdout)


@needs_checkout
def test_only_static_scripts_map_to_a_checkout_file() -> None:
    urls = [
        "http://127.0.0.1:1/static/app.js?v=3",
        "http://127.0.0.1:1/static/markdown.js",
        "http://127.0.0.1:1/static/app.css",
        "http://127.0.0.1:1/other/app.js",
        "http://127.0.0.1:1/static/sub/app.js",
        "http://127.0.0.1:1/static/%2e%2e/x.js",
        "not a url",
        "",
    ]
    got = _node(
        f"import {{ sourceUrl }} from './tests/fixtures/cdp_coverage.mjs';"
        f"console.log(JSON.stringify({json.dumps(urls)}.map(sourceUrl)));"
    )
    assert isinstance(got, list)
    assert got[0] == (REPO / STATIC / "app.js").as_uri()
    assert got[1] == (REPO / STATIC / "markdown.js").as_uri()
    assert got[2:] == [""] * 6


@needs_checkout
def test_floors_are_judged_per_file_and_metric() -> None:
    summary = {
        str(REPO / "a.js"): {k: {"pct": 90} for k in ("lines", "branches", "functions")},
        str(REPO / "b.js"): {
            "lines": {"pct": 100},
            "branches": {"pct": 50},
            "functions": {"pct": 100},
        },
    }
    floors = {
        "a.js": {"lines": 90, "branches": 90, "functions": 90},
        "b.js": {"lines": 100, "branches": 60, "functions": 100},
        "c.js": {"lines": 0, "branches": 0, "functions": 0},
    }
    got = _node(
        "import { shortfalls } from './tools/chrome_coverage.mjs';"
        f"console.log(JSON.stringify(shortfalls({json.dumps(floors)}, {json.dumps(summary)})));"
    )
    assert got == [
        "b.js: branches 50% is below the floor 60%",
        "c.js: no coverage was reported for it",
    ]


def _check(*args: str, env: dict[str, str] | None = None) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["node", TOOL, "check", *args],
        cwd=REPO,
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
        env={**os.environ, **(env or {})},
    )


@needs_checkout
def test_no_coverage_is_not_measured_unless_a_browser_is_required(tmp_path: Path) -> None:
    quiet = _check(str(tmp_path / "none"), env={"SADDLE_REQUIRE_BROWSER": ""})
    assert quiet.returncode == 0
    assert "not measured here" in quiet.stdout
    strict = _check(str(tmp_path / "none"), env={"SADDLE_REQUIRE_BROWSER": "1"})
    assert strict.returncode == 1
    assert "SADDLE_REQUIRE_BROWSER" in strict.stderr


def _hits(lcov: str, name: str) -> dict[int, int]:
    """Line -> hits of `name`'s record in an lcov report."""
    found: dict[int, int] = {}
    inside = False
    for raw in lcov.splitlines():
        if raw.startswith("SF:"):
            inside = raw.endswith(f"/{name}")
        elif inside and raw.startswith("DA:"):
            number, hits = raw[3:].split(",")[:2]
            found[int(number)] = int(hits)
    return found


def _line_of(name: str, needle: str) -> int:
    for number, text in enumerate((REPO / STATIC / name).read_text().splitlines(), 1):
        if needle in text:
            return number
    message = f"{needle!r} not in {name}"
    raise AssertionError(message)


@needs_checkout
@needs_chrome
def test_a_chrome_run_leaves_coverage_that_names_run_and_unrun_lines(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    data = tmp_path / "cov"
    monkeypatch.setenv("SADDLE_JS_COVERAGE_DIR", str(data))
    drive(tmp_path, "127.0.0.1")
    assert list(data.glob("coverage-*.json")), "the driver wrote no coverage"
    report = tmp_path / "report"
    c8 = REPO / "node_modules" / ".bin" / "c8"
    subprocess.run(
        [
            str(c8), "report", "--config", str(REPO / ".c8rc.chrome.json"),
            "--temp-directory", str(data), "--reporter", "lcovonly",
            "--reports-dir", str(report),
        ],
        cwd=REPO, check=True, capture_output=True, timeout=120,
    )  # fmt: skip
    lcov = (report / "lcov.info").read_text()
    ran = _hits(lcov, "app.js")
    assert ran, "the report has no record of a browser script"
    # Known-good: the page's boot, run by every driver.
    assert ran[_line_of("app.js", "async function boot")] > 0
    # Known-bad: the runs driver alone exercises deleting a session.
    assert _hits(lcov, "runs.js")[_line_of("runs.js", "async function deleteSession") + 1] == 0


@needs_checkout
@needs_chrome
def test_check_fails_a_file_below_its_floor_and_passes_at_its_level(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    data = tmp_path / "cov"
    monkeypatch.setenv("SADDLE_JS_COVERAGE_DIR", str(data))
    drive(tmp_path, "127.0.0.1")
    scope = json.loads((REPO / SCOPE).read_text())
    files = list(scope["chrome_measured"])
    scope["chrome_measured"] = {f: {"lines": 0, "branches": 0, "functions": 0} for f in files}
    low = tmp_path / "low.json"
    low.write_text(json.dumps(scope))
    ok = _check(str(data), "--scope", str(low))
    assert ok.returncode == 0, ok.stdout + ok.stderr
    scope["chrome_measured"][files[0]]["lines"] = 100
    high = tmp_path / "high.json"
    high.write_text(json.dumps(scope))
    bad = _check(str(data), "--scope", str(high))
    assert bad.returncode == 1
    assert re.search(rf"{re.escape(files[0])}: lines [\d.]+% is below the floor 100%", bad.stderr)
    # c8's own table names the unrun lines, file by file.
    assert Path(files[0]).name in bad.stdout


@needs_checkout
def test_the_scope_rc_and_check_sh_agree() -> None:
    scope = json.loads((REPO / SCOPE).read_text())
    chrome = scope["chrome_measured"]
    rc = json.loads((REPO / ".c8rc.chrome.json").read_text())
    assert sorted(rc["include"]) == sorted(chrome)
    for name, floors in chrome.items():
        assert name in scope["not_measured"], f"{name}: the node tests do not measure it"
        assert (REPO / name).is_file()
        assert set(floors) == {"lines", "branches", "functions"}
        assert all(0 <= v <= 100 for v in floors.values())
    # The tests that collect the data run with the directory set, and the check follows them.
    stages = check_sh_stages((REPO / "check.sh").read_text())
    keys = [s.key for s in stages]
    pytest_at = keys.index("pytest")
    assert "rm" in keys[:pytest_at]
    check_at = next(i for i, s in enumerate(stages) if s.args[:2] == (TOOL, "check"))
    assert check_at > pytest_at
    assert (
        "export SADDLE_JS_COVERAGE_DIR="
        in (REPO / "check.sh").read_text().split("uv run pytest")[0]
    )


@needs_checkout
@needs_chrome
def test_every_navigation_of_a_driver_is_drained_before_the_page_goes(
    tmp_path: Path,
    repo: Path,  # noqa: F811
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The card driver loads the page four times; a navigation drops the old page's
    scripts, so each load is drained before it, and the last page when the driver ends."""
    data = tmp_path / "cov"
    monkeypatch.setenv("SADDLE_JS_COVERAGE_DIR", str(data))
    run_card_driver(tmp_path, repo)
    files = sorted(data.glob("coverage-*.json"))
    assert len(files) == 4, [f.name for f in files]
    for f in files:
        urls = {e["url"] for e in json.loads(f.read_text())["result"]}
        assert (REPO / STATIC / "app.js").as_uri() in urls
