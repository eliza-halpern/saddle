"""The audit's changed-line coverage reaches the four page scripts, through Chrome.

Contract: a changed code line of a script the coverage scope lists under
`chrome_measured` that no Chrome-driven test runs is a `js-coverage` failure
naming `file:line`; one a Chrome test runs passes; with no Chrome, a skipped or
failed or silent Chrome run, or no named Chrome test, the file reads `not-proven`
("not line-measured", naming why), never covered. The Chrome tests run only when
a changed line lies in such a file. Known-good and known-bad instances for the
real page script (`runs.js`) are real audits of a copy of this repository under
a real Chrome inside the audit's confinement; the other branches use small trees
whose "Chrome tests" skip, stay silent or fail.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from collections.abc import Mapping
from pathlib import Path
from typing import Any

import pytest
from browser_guard import BROWSER
from test_auditor import _init
from test_js_audit import FILES, NOTHING, finding

from saddle import auditor, jsevidence, runner
from saddle.auditor import JS_COVERAGE_GATE, Auditor
from saddle.evidence import CapturedRun
from saddle.gates import SHELL_TIMEOUT, TOOL_UNAVAILABLE
from saddle.jsevidence import JsCoverage, measure_chrome_coverage, read_coverage_scope
from saddle.sandbox import CONFINED_ENV

REPO = Path(__file__).resolve().parents[1]
SCOPE = "tests/fixtures/js_coverage_scope.json"
RUNS_JS = "src/saddle/web/static/runs.js"
# The two real audits below run a whole audit, Chrome included, of a copy of this
# repository. Inside an audit's own sandbox that nests one confinement in another,
# which cannot reproduce the outer run (4 of its 32 Chrome tests failed there; all
# 164 pass one level deep), so there they skip and the outer audit lists them as
# not proven. check.sh runs them.
not_nested = pytest.mark.skipif(
    os.environ.get(CONFINED_ENV) == "1",
    reason="a real audit inside an audit's sandbox: nested confinement (check.sh runs it)",
)
needs_chrome = pytest.mark.skipif(
    not BROWSER
    or not (REPO / "node_modules/c8/bin/c8.js").is_file()
    or not (REPO / ".git").exists(),
    reason="needs node, google-chrome, c8 and a git checkout",
)


@pytest.fixture(autouse=True)
def python_mutation_is_not_under_test(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(runner, "mutation_sample", lambda *_a, **_k: NOTHING)


def small(tmp_path: Path, test: str, *, tests: list[str] | None = None, c8: bool = True) -> Path:
    """A tree whose page script `static/p.js` changed and whose scope names one
    Chrome test file holding `test`."""
    scope = {
        "measured": [],
        "not_measured": {},
        "chrome_measured": {"static/p.js": {"lines": 0}},
        "chrome_tests": ["tests/test_page.py"] if tests is None else tests,
    }
    root = tmp_path / "tree"
    (root / "static").mkdir(parents=True)
    (root / "tests/fixtures").mkdir(parents=True)
    _init(root, {**FILES, SCOPE: json.dumps(scope), "tests/test_page.py": test})
    (root / "static/p.js").write_text("let x = 1;\nlet y = 2;\n")
    if c8:
        (root / "node_modules").symlink_to(REPO / "node_modules")
    return root


SKIPS = "import pytest\n\n\ndef test_page():\n    pytest.skip('needs node and google-chrome')\n"
SILENT = "def test_page():\n    pass\n"
RED = "def test_page():\n    assert False\n"


def verdict(root: Path) -> Any:
    return finding(Auditor(root, "HEAD").tier1(), JS_COVERAGE_GATE)


def test_the_scope_names_the_chrome_files_and_the_tests_that_run_them() -> None:
    scope = read_coverage_scope(REPO)
    assert scope.chrome_measured == {
        f"src/saddle/web/static/{n}.js" for n in ("app", "tasks", "notify", "runs")
    }
    assert scope.chrome_tests
    assert all((REPO / t).is_file() for t in scope.chrome_tests)
    assert read_coverage_scope(REPO / "tests").chrome_tests == ()


def test_every_chrome_driver_belongs_to_a_named_test_module() -> None:
    named = "".join((REPO / t).read_text() for t in read_coverage_scope(REPO).chrome_tests)
    drivers = sorted(p.name for p in (REPO / "tests/fixtures").glob("*_cdp.mjs"))
    assert drivers
    assert [d for d in drivers if d not in named] == []


def test_a_skipped_chrome_run_is_not_proven_never_covered(tmp_path: Path) -> None:
    got = verdict(small(tmp_path, SKIPS))
    assert got.verdict == "not-proven"
    assert got.detail == "not proven: not line-measured: static/p.js (1 Chrome tests skipped)"


def test_a_chrome_run_that_left_no_coverage_is_not_proven(tmp_path: Path) -> None:
    got = verdict(small(tmp_path, SILENT))
    assert got.verdict == "not-proven"
    assert "not line-measured: static/p.js (no Chrome ran)" in got.detail


def test_a_red_chrome_suite_is_not_proven_not_a_number_about_the_wrong_program(
    tmp_path: Path,
) -> None:
    got = verdict(small(tmp_path, RED))
    assert got.verdict == "not-proven"
    assert "static/p.js (the Chrome tests exited 1:" in got.detail


def test_a_scope_that_names_no_chrome_test_is_not_proven(tmp_path: Path) -> None:
    got = verdict(small(tmp_path, SILENT, tests=[]))
    assert got.verdict == "not-proven"
    assert "static/p.js (the coverage scope names no Chrome test)" in got.detail


def test_without_c8_a_chrome_file_is_not_proven_naming_the_tool(tmp_path: Path) -> None:
    got = verdict(small(tmp_path, SILENT, c8=False))
    assert got.verdict == "not-proven"
    assert "static/p.js (c8 was not found in node_modules)" in got.detail


def test_chrome_runs_only_when_a_chrome_measured_file_changed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def never(*_a: Any, **_k: Any) -> JsCoverage:
        msg = "the Chrome tests ran for a change that touches no page script"
        raise AssertionError(msg)

    monkeypatch.setattr(auditor, "measure_chrome_coverage", never)
    root = small(tmp_path, SILENT)
    (root / "static/p.js").unlink()
    (root / "static/other.js").write_text("let z = 3;\n")
    got = verdict(root)
    assert got.verdict == "not-proven"
    assert "not line-measured: static/other.js (in no coverage list)" in got.detail


def lines(hits: Mapping[int, int]) -> JsCoverage:
    return JsCoverage({"static/p.js": hits})


def test_a_changed_line_chrome_never_ran_fails_naming_it_and_a_run_one_passes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    seen: dict[str, Any] = {}

    def fake(copy: Path, tests: Any, **kwargs: Any) -> JsCoverage:
        seen.update(tests=tuple(tests), workers=kwargs["workers"])
        return lines({1: 3, 2: 0})

    monkeypatch.setattr(auditor, "measure_chrome_coverage", fake)
    got = verdict(small(tmp_path / "a", SILENT))
    assert (got.verdict, got.detail) == ("fail", "no test runs static/p.js:2")
    assert seen == {"tests": ("tests/test_page.py",), "workers": 1}
    monkeypatch.setattr(auditor, "measure_chrome_coverage", lambda *_a, **_k: lines({1: 3, 2: 1}))
    ok = verdict(small(tmp_path / "b", SILENT))
    assert (ok.verdict, ok.detail) == ("pass", "every executable changed line runs (2)")


def test_a_page_script_gap_names_the_projects_chrome_test_helper(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The audit is the same for every project; where to learn to write a
    # Chrome-driven test is the project's own (`chrome_test_helper`).
    def with_helper(root: Path, measured: list[str]) -> Path:
        scope = json.loads((root / SCOPE).read_text())
        scope.update(chrome_test_helper="tests/chrome_page.py", measured=measured)
        (root / SCOPE).write_text(json.dumps(scope))
        return root

    monkeypatch.setattr(auditor, "measure_chrome_coverage", lambda *_a, **_k: lines({1: 3, 2: 0}))
    got = verdict(with_helper(small(tmp_path / "a", SILENT), []))
    assert (got.verdict, got.detail) == (
        "fail",
        "no test runs static/p.js:2"
        " (page code runs in a Chrome-driven test: see tests/chrome_page.py)",
    )
    # A gap only in node-measured code is closed by a node test: no Chrome hint.
    root = with_helper(small(tmp_path / "b", SILENT), ["static/a.js"])
    monkeypatch.setattr(auditor, "measure_chrome_coverage", lambda *_a, **_k: lines({1: 1, 2: 1}))
    monkeypatch.setattr(
        auditor, "measure_coverage", lambda *_a, **_k: JsCoverage({"static/a.js": {6: 0}})
    )
    (root / "static/a.js").write_text("a\nb\nc\nd\ne\nf\n")
    (root / "tests/a.test.js").write_text("// a node test\n")
    node_only = verdict(root)
    assert (node_only.verdict, node_only.detail) == ("fail", "no test runs static/a.js:6")


def test_a_chrome_report_missing_a_changed_file_is_not_proven(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(auditor, "measure_chrome_coverage", lambda *_a, **_k: JsCoverage())
    got = verdict(small(tmp_path, SILENT))
    assert got.verdict == "not-proven"
    assert "static/p.js (c8 reported no lines for static/p.js)" in got.detail


def test_a_node_measured_file_and_a_chrome_file_are_judged_together(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = small(tmp_path, SILENT)
    scope = json.loads((root / SCOPE).read_text())
    scope["measured"] = ["static/a.js"]
    (root / SCOPE).write_text(json.dumps(scope))
    monkeypatch.setattr(auditor, "measure_chrome_coverage", lambda *_a, **_k: lines({1: 1, 2: 0}))
    monkeypatch.setattr(
        auditor, "measure_coverage", lambda *_a, **_k: JsCoverage({"static/a.js": {6: 0}})
    )
    (root / "static").mkdir(exist_ok=True)
    (root / "static/a.js").write_text("a\nb\nc\nd\ne\nf\n")
    (root / "tests/a.test.js").write_text("// a node test\n")
    got = verdict(root)
    assert got.verdict == "fail"
    assert got.detail == "no test runs static/a.js:6, static/p.js:2"


def fake_capture(
    monkeypatch: pytest.MonkeyPatch, steps: list[tuple[int, str, str]], v8: bool = True
) -> list[list[str]]:
    """`run_capture` standing in for the pytest run, then for c8's report."""
    argvs: list[list[str]] = []

    def fake(argv: list[str], *_a: Any, **kwargs: Any) -> CapturedRun:
        argvs.append(argv)
        code, out, err = steps[len(argvs) - 1]
        if len(argvs) == 1 and v8:
            (
                Path(kwargs["extra_env"][jsevidence.CHROME_COVERAGE_ENV]) / "coverage-1-0.json"
            ).write_text("{}")
        if len(argvs) == 2:
            report = Path(next(a for a in argv if a.startswith("--reports-dir=")).split("=", 1)[1])
            if out:
                report.mkdir(parents=True)
                (report / "lcov.info").write_text(out)
                out = ""
        return CapturedRun(tuple(argv), code, out, err)

    monkeypatch.setattr(jsevidence, "run_capture", fake)
    return argvs


def measure(root: Path, workers: int = 1) -> JsCoverage:
    return measure_chrome_coverage(root, ["tests/test_page.py"], workers=workers)


GOOD = "SF:static/p.js\nDA:1,2\nDA:2,0\nend_of_record\n"


def test_the_chrome_run_is_parallel_when_workers_say_so_and_reads_the_lcov(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    argvs = fake_capture(monkeypatch, [(0, "3 passed", ""), (0, GOOD, "")])
    got = measure(small(tmp_path, SILENT), workers=4)
    assert got == JsCoverage({"static/p.js": {1: 2, 2: 0}})
    assert argvs[0][argvs[0].index("-n") + 1] == "4"
    assert "-n" not in fake_serial(tmp_path, monkeypatch)


def fake_serial(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> list[str]:
    argvs = fake_capture(monkeypatch, [(0, "3 passed", ""), (0, GOOD, "")])
    measure(small(tmp_path / "s", SILENT))
    return argvs[0]


@pytest.mark.parametrize(
    ("steps", "problem"),
    [
        (
            [(TOOL_UNAVAILABLE, "", "no python")],
            "the Chrome tests could not be launched: no python",
        ),
        ([(SHELL_TIMEOUT, "", "")], "the Chrome tests timed out"),
        ([(1, "1 failed", "")], "the Chrome tests exited 1: 1 failed"),
        ([(1, "", "")], "the Chrome tests exited 1: no output"),
        ([(0, "2 passed, 3 skipped", "")], "3 Chrome tests skipped"),
        ([(0, "", ""), (2, "", "boom")], "c8 report of the Chrome coverage exited 2: boom"),
        ([(0, "", ""), (2, "", "")], "c8 report of the Chrome coverage exited 2: no output"),
        ([(0, "", ""), (0, "", "")], "c8 wrote no lcov report of the Chrome coverage"),
        ([(0, "", ""), (0, "DA:1,1\n", "")], "outside a file"),
    ],
)
def test_each_way_the_chrome_run_fails_is_a_named_problem_with_no_lines(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, steps: list[tuple[int, str, str]], problem: str
) -> None:
    fake_capture(monkeypatch, steps)
    got = measure(small(tmp_path, SILENT))
    assert problem in got.problem
    assert got.lines == {}


def test_tests_that_pass_without_writing_coverage_never_reach_c8(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    argvs = fake_capture(monkeypatch, [(0, "3 passed", "")], v8=False)
    assert measure(small(tmp_path, SILENT)).problem == jsevidence.NO_CHROME
    assert len(argvs) == 1


def test_a_missing_c8_is_named(tmp_path: Path) -> None:
    assert "c8 was not found" in measure(small(tmp_path, SILENT, c8=False)).problem


def real_copy(tmp_path: Path) -> Path:
    """A git repository of this checkout's tracked files whose Chrome tests are
    the runs page's, so one audit stays cheap."""
    root = tmp_path / "repo"
    names = (
        subprocess.run(["git", "ls-files", "-z"], cwd=REPO, capture_output=True, check=True)
        .stdout.decode()
        .split("\0")
    )
    for name in filter(None, names):
        if (REPO / name).is_file():
            (root / name).parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(REPO / name, root / name, follow_symlinks=False)
    scope = json.loads((root / SCOPE).read_text())
    scope["chrome_tests"] = ["tests/test_runs.py"]
    (root / SCOPE).write_text(json.dumps(scope))
    (root / "node_modules").symlink_to(REPO / "node_modules")
    subprocess.run(["git", "init", "-q"], cwd=root, check=True)
    for key, value in (("user.email", "t@example.com"), ("user.name", "t")):
        subprocess.run(["git", "config", key, value], cwd=root, check=True)
    subprocess.run(["git", "add", "-A"], cwd=root, check=True)
    subprocess.run(["git", "commit", "-qm", "baseline"], cwd=root, check=True)
    return root


def audited(root: Path) -> Any:
    from saddle import sandbox
    from saddle.evidence import sandbox_expose

    with sandbox.also_exposing(sandbox_expose(root, "HEAD")):
        return auditor.js_coverage_finding(root, "HEAD", 900, REPO, [])


@needs_chrome
@not_nested
def test_a_changed_runs_line_a_real_chrome_test_reaches_passes(tmp_path: Path) -> None:
    root = real_copy(tmp_path)
    path = root / RUNS_JS
    path.write_text(path.read_text().replace("const s = Math.max(0, ", "const s = Math.max(1, "))
    got = audited(root)
    assert got is not None
    assert (got[0].verdict, got[0].detail) == ("pass", "every executable changed line runs (1)")


@needs_chrome
@not_nested
def test_a_changed_runs_line_in_an_unreached_branch_fails_naming_it(tmp_path: Path) -> None:
    root = real_copy(tmp_path)
    path = root / RUNS_JS
    path.write_text(
        path.read_text().replace("if (m < 60) return `${m}m`;", "if (m < 61) return `${m}m`;")
    )
    got = audited(root)
    assert got is not None
    # This repository's scope names its Chrome test helper, so the gap says where to look.
    assert (got[0].verdict, got[0].detail) == (
        "fail",
        f"no test runs {RUNS_JS}:64"
        " (page code runs in a Chrome-driven test: see tests/chrome_page.py)",
    )
