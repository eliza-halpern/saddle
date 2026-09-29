"""The test gate's time limit is the project's setting, read where the task started.

Contract: every run of a project's tests under a gate is bounded by
`evidence.suite_limit(tree, rev)`: `[tool.saddle] test-timeout` in the
`pyproject.toml` committed at the task's starting commit, else the built-in
300 s. Nothing the tree under audit does to its own config changes the limit
its own audit runs under.

Why it is a setting (loosened): saddle's own suite takes about twenty
minutes, so under a fixed 300 s every audit of saddle's own repo reported
"'python -m pytest -q' hangs: no verdict within the time limit" on a tree
whose `check.sh` passed (2994 passed in 1209 s). A slow suite with a small
built-in limit stands in for that case here (`lowered_default`).

Known-good: a suite slower than the built-in limit passes when the committed
config admits it. Known-bad: the same suite with no setting, or with a
setting only the working tree has, is still refused as a hang; and a true
hang is a hang under the configured limit.
"""

from __future__ import annotations

import importlib
import inspect
import pkgutil
from collections.abc import Callable
from pathlib import Path
from time import perf_counter
from typing import Any, Final

import pytest

import saddle
from saddle import auditor, evidence, runner
from saddle import slice as slice_module
from saddle.audit import AuditError, audit_tree
from saddle.auditor import GREEN_ON_BASELINE, Auditor, AuditorConfig, Finding
from saddle.cli import main
from saddle.dag import Dag
from saddle.evidence import (
    DEFAULT_TEST_TIMEOUT_S,
    SUITE_LIMIT_MAX_S,
    SuiteLimit,
    SuiteLimitError,
    run_argv,
    suite_limit,
)
from saddle.gates import SHELL_TIMEOUT
from saddle.slice import run_slice
from saddle.vllm import DiffProposal

LOWERED: Final = 1.0
"""The built-in limit as the behavioural tests see it: a sandboxed
`coverage run -m pytest` of a one-test suite alone takes about 1.6 s, so
every suite here is slower than this, the way saddle's own is slower than
300 s."""

SLOW_S: Final = 1.0
"""How long the slow suite's one test sleeps, on top of that overhead: past
`LOWERED` by construction, well inside the limits the projects below set."""

ADMITS: Final = 60.0
"""A limit the slow suite fits inside with a wide margin on a loaded box."""

BOUND: Final[tuple[tuple[Callable[..., Any], str], ...]] = (
    (evidence.run_shell, "timeout"),
    (evidence.run_shell_capture, "timeout"),
    (runner.run_node_gate, "test_timeout"),
    (auditor.green_on_baseline, "timeout"),
    (slice_module._run_node, "test_timeout"),
)
"""Every parameter whose default is the built-in limit (the census test
below keeps this list whole). A gate that forgets to pass the project's
limit falls back to one of these."""


@pytest.fixture
def lowered_default(monkeypatch: pytest.MonkeyPatch) -> None:
    """The built-in limit lowered to `LOWERED` everywhere it is bound, so only
    the project's setting can admit a suite slower than it, and a path that
    drops the setting falls back to a limit the suite cannot meet."""
    monkeypatch.setattr(evidence, "DEFAULT_TEST_TIMEOUT_S", LOWERED)
    for function, name in BOUND:
        defaults = function.__kwdefaults__
        if defaults is None:
            # mutmut's work copy: a trampoline with no defaults of its own. The
            # tests still pass there (`suite_limit` reads the constant when
            # called); only a dropped limit goes unnoticed in that copy.
            continue
        assert defaults[name] is DEFAULT_TEST_TIMEOUT_S
        monkeypatch.setitem(defaults, name, LOWERED)


def _git(root: Path, *argv: str) -> None:
    assert run_argv(["git", *argv], root) == 0


def _commit(root: Path, files: dict[str, str], message: str = "commit") -> str:
    """Write `files`, commit everything, return the commit's sha."""
    if not (root / ".git").exists():
        root.mkdir(parents=True, exist_ok=True)
        _git(root, "init", "-q")
        _git(root, "config", "user.email", "test@example.com")
        _git(root, "config", "user.name", "test")
    for name, text in files.items():
        (root / name).parent.mkdir(parents=True, exist_ok=True)
        (root / name).write_text(text)
    _git(root, "add", "-A")
    _git(root, "commit", "-q", "--allow-empty", "-m", message)
    found = evidence.run_capture(["git", "rev-parse", "HEAD"], root)
    return found.stdout.strip()


def _setting(value: str) -> str:
    return f'[project]\nname = "p"\n\n[tool.saddle]\ntest-timeout = {value}\n'


# -- the resolver ---------------------------------------------------------------


@pytest.mark.parametrize(
    "files",
    [
        {},
        {"pyproject.toml": '[project]\nname = "p"\n'},
        {"pyproject.toml": "tool = 1\n"},
        {"pyproject.toml": "[tool.other]\nx = 1\n"},
        {"pyproject.toml": "[tool.saddle]\n"},
    ],
    ids=["no-file", "no-tool", "tool-not-a-table", "other-tool", "empty-table"],
)
def test_without_a_setting_the_limit_is_the_built_in_300_s(
    tmp_path: Path, files: dict[str, str]
) -> None:
    sha = _commit(tmp_path, {"n.py": "x = 1\n", **files})
    found = suite_limit(tmp_path, "HEAD")
    assert found.seconds == 300.0
    assert found.source.startswith("built-in default 300 s (no ")
    assert sha[:12] in found.source


@pytest.mark.parametrize(
    ("written", "seconds"),
    [("2400", 2400.0), ("2.5", 2.5), ("0.001", 0.001), (f"{SUITE_LIMIT_MAX_S:g}", 86400.0)],
)
def test_the_setting_committed_at_the_rev_is_the_limit(
    tmp_path: Path, written: str, seconds: float
) -> None:
    sha = _commit(tmp_path, {"pyproject.toml": _setting(written)})
    assert suite_limit(tmp_path, sha) == SuiteLimit(
        seconds, f"[tool.saddle] test-timeout in pyproject.toml at {sha[:12]}"
    )


@pytest.mark.parametrize(
    "written",
    [
        "0",
        "-5",
        "86400.5",
        "1e400",
        "inf",
        "-inf",
        "nan",
        "100000000000000000000000000000000000000",
        '"600"',
        "true",
        "[600]",
        "{ seconds = 600 }",
    ],
)
def test_a_value_that_is_not_a_limit_is_refused_by_name(tmp_path: Path, written: str) -> None:
    """Known-bad values: a string, a bool (TOML's `true` is not 1 s), zero, a
    negative, past a day, and the three non-numbers; a huge integer must be
    compared, not converted (`math.isfinite` raises on it)."""
    sha = _commit(tmp_path, {"pyproject.toml": _setting(written)})
    with pytest.raises(SuiteLimitError) as caught:
        suite_limit(tmp_path, "HEAD")
    message = str(caught.value)
    assert f"pyproject.toml at {sha[:12]}: test-timeout = " in message
    assert "is not a number of seconds above 0 and at most 86400" in message


@pytest.mark.parametrize(
    "table",
    ["test_timeout = 2400\n", "test-timeout = 2400\ntimeout = 5\n"],
    ids=["typo", "extra-key"],
)
def test_a_key_saddle_does_not_read_is_refused_not_ignored(tmp_path: Path, table: str) -> None:
    """A typo must not read as "no setting" and quietly keep 300 s."""
    _commit(tmp_path, {"pyproject.toml": f"[tool.saddle]\n{table}"})
    reads = "the keys saddle reads there are test-timeout and test-workers"
    with pytest.raises(SuiteLimitError, match=reads):
        suite_limit(tmp_path, "HEAD")


@pytest.mark.parametrize(
    ("files", "problem"),
    [
        ({"pyproject.toml": "[tool]\nsaddle = 5\n"}, "[tool.saddle] is not a table"),
        ({"pyproject.toml": "[tool.saddle\n"}, "is not TOML"),
        ({"pyproject.toml/inner.txt": "x\n"}, "is not a file"),
    ],
    ids=["not-a-table", "not-toml", "a-directory"],
)
def test_a_config_that_cannot_be_read_is_refused(
    tmp_path: Path, files: dict[str, str], problem: str
) -> None:
    _commit(tmp_path, files)
    with pytest.raises(SuiteLimitError, match="cannot read the test time limit") as caught:
        suite_limit(tmp_path, "HEAD")
    assert problem in str(caught.value)


def test_a_rev_that_is_not_a_commit_is_refused(tmp_path: Path) -> None:
    _commit(tmp_path, {"pyproject.toml": _setting("5")})
    tree_id = evidence.run_capture(["git", "rev-parse", "HEAD^{tree}"], tmp_path).stdout.strip()
    for rev in ("no-such-branch", tree_id):
        with pytest.raises(SuiteLimitError, match=f"{rev!r} is not a commit"):
            suite_limit(tmp_path, rev)


def test_the_trees_own_pyproject_is_read_not_the_repositorys_root(tmp_path: Path) -> None:
    """pytest reads the config beside the suite; so does the limit."""
    _commit(tmp_path, {"pyproject.toml": _setting("5"), "sub/pyproject.toml": _setting("7")})
    assert suite_limit(tmp_path / "sub", "HEAD").seconds == 7.0
    assert suite_limit(tmp_path, "HEAD").seconds == 5.0


def test_edits_after_the_rev_change_nothing(tmp_path: Path) -> None:
    """Known-bad at the source: the tree raising its own limit, unstaged,
    staged or committed on top, never reaches the limit read at the rev."""
    base = _commit(tmp_path, {"pyproject.toml": _setting("5")})
    (tmp_path / "pyproject.toml").write_text(_setting("9999"))
    assert suite_limit(tmp_path, base).seconds == 5.0
    assert suite_limit(tmp_path, "HEAD").seconds == 5.0
    _git(tmp_path, "add", "-A")
    assert suite_limit(tmp_path, "HEAD").seconds == 5.0
    later = _commit(tmp_path, {}, "raise the limit")
    assert suite_limit(tmp_path, base).seconds == 5.0
    # ...and it does read commits: the later one says what it says.
    assert suite_limit(tmp_path, later).seconds == 9999.0


def test_every_default_bound_to_the_built_in_limit_is_lowered_by_the_fixture() -> None:
    """The denominator for `lowered_default`: a new parameter defaulting to
    the built-in limit is either listed in `BOUND` or fails this test."""
    found = set()
    for info in pkgutil.walk_packages(saddle.__path__, "saddle."):
        module = importlib.import_module(info.name)
        for owner in (module, *(c for c in vars(module).values() if inspect.isclass(c))):
            for value in vars(owner).values():
                function = inspect.unwrap(value) if callable(value) else value
                if not inspect.isfunction(function) or function.__module__ != info.name:
                    continue
                if "__mutmut_" in function.__qualname__:
                    continue  # mutmut's renamed copies, in its work copy only
                defaults = {**(function.__kwdefaults__ or {})}
                names = inspect.signature(function).parameters
                positional = [p for p in names.values() if p.kind != p.KEYWORD_ONLY]
                for param, default in zip(
                    reversed(positional), reversed(function.__defaults__ or ()), strict=False
                ):
                    defaults[param.name] = default
                found |= {
                    (function.__module__, function.__qualname__, name)
                    for name, default in defaults.items()
                    if default is DEFAULT_TEST_TIMEOUT_S
                }
    listed = {(f.__module__, f.__qualname__, name) for f, name in BOUND}
    assert found == listed


# -- the auditor: tiers 1 and 2 ------------------------------------------------------

BASE_CODE: Final = "def f():\n    return 1\n"
FIXED_CODE: Final = "def f():\n    return 2\n"


def _slow_test(sleep: float = SLOW_S) -> str:
    return (
        "import time\n\nfrom n import f\n\n\n"
        f"def test_f():\n    time.sleep({sleep})\n    assert f() == 2\n"
    )


def _slow_tree(
    tmp_path: Path, committed: str | None, *, sleep: float = SLOW_S, new_test: bool = False
) -> Path:
    """A baseline whose one test sleeps `sleep` seconds and fails (with
    `committed` as its pyproject.toml, if any), and a working-tree fix that
    greens it. The test is unchanged, so a red-phase leg samples once; with
    `new_test` it is written with the fix instead, so red-phase reads its
    baseline runs."""
    tree = tmp_path / "tree"
    files = {"n.py": BASE_CODE} if new_test else {"n.py": BASE_CODE, "test_n.py": _slow_test(sleep)}
    if committed is not None:
        files["pyproject.toml"] = committed
    _commit(tree, files, "baseline")
    (tree / "n.py").write_text(FIXED_CODE)
    (tree / "test_n.py").write_text(_slow_test(sleep))
    return tree


def _finding(result: Any, gate: str) -> Finding:
    found: Finding = next(f for f in result.findings if f.gate == gate)
    return found


@pytest.mark.usefixtures("lowered_default")
def test_a_suite_slower_than_the_default_passes_under_the_projects_limit(
    tmp_path: Path,
) -> None:
    """Known-good the fixed limit refused (saddle's own suite under 300 s)."""
    tree = _slow_tree(tmp_path, _setting(f"{ADMITS:g}"))
    tests = _finding(Auditor(tree).tier1(), "tests")
    assert tests.verdict == "pass", tests.detail


@pytest.mark.usefixtures("lowered_default")
def test_without_the_setting_the_same_suite_is_refused_as_a_hang(tmp_path: Path) -> None:
    tree = _slow_tree(tmp_path, None)
    tests = _finding(Auditor(tree).tier1(), "tests")
    assert tests.verdict == "fail"
    assert "hangs: no verdict within the time limit" in tests.detail


@pytest.mark.usefixtures("lowered_default")
@pytest.mark.parametrize("committed", [None, _setting(f"{LOWERED:g}")], ids=["none", "small"])
def test_the_trees_own_edit_to_its_config_changes_nothing(
    tmp_path: Path, committed: str | None
) -> None:
    """Known-bad: an agent that raises its own bar in the tree it is audited
    on (the working tree's pyproject.toml, staged by the audit's copy) is
    still judged under the limit committed at the baseline."""
    tree = _slow_tree(tmp_path, committed)
    (tree / "pyproject.toml").write_text(_setting(f"{ADMITS:g}"))
    tests = _finding(Auditor(tree).tier1(), "tests")
    assert tests.verdict == "fail"
    assert "hangs" in tests.detail


def test_a_true_hang_is_still_a_hang_under_the_configured_limit(tmp_path: Path) -> None:
    """The limit bounds; it does not waive. A test that sleeps ten minutes is
    reported as a hang after the project's 3 s, not the built-in 300 s."""
    tree = _slow_tree(tmp_path, _setting("3"), sleep=600)
    started = perf_counter()
    tests = _finding(Auditor(tree).tier1(), "tests")
    elapsed = perf_counter() - started
    assert tests.verdict == "fail"
    assert "hangs: no verdict within the time limit" in tests.detail
    assert elapsed < 120, elapsed


@pytest.mark.usefixtures("lowered_default")
def test_dead_code_is_rerun_under_the_projects_limit(tmp_path: Path) -> None:
    """The dead-code rerun reads a timeout as "the suite fails without them",
    which would pass dead code; under the project's limit it finds it."""
    tree = _slow_tree(tmp_path, _setting(f"{ADMITS:g}"))
    (tree / "n.py").write_text(FIXED_CODE + "\n\ndef _unused():\n    return 3\n")
    result = Auditor(tree).tier1()
    assert _finding(result, "tests").verdict == "pass"
    dead = _finding(result, "dead-code")
    assert dead.verdict == "fail", dead.detail
    assert "_unused" in dead.detail


FEES_BASE: Final = "def fee(x):\n    return x - 1\n"
FEES_NEW: Final = "def fee(x):\n    return x - 2\n"
FEES_TESTS: Final = (
    "import time\n\nfrom fees import fee\n\n\n"
    f"def test_fee_basic():\n    time.sleep({SLOW_S})\n    assert fee(10) == 9\n"
)


@pytest.mark.usefixtures("lowered_default")
def test_a_sanctioned_rewrite_is_checked_on_the_baseline_under_the_projects_limit(
    tmp_path: Path,
) -> None:
    """`green_on_baseline` reads a run that timed out as red, which would
    sanction a vacuous rewrite; under the project's limit it is refused."""
    tree = tmp_path / "tree"
    _commit(
        tree,
        {"fees.py": FEES_BASE, "test_fees.py": FEES_TESTS, "pyproject.toml": _setting("60")},
    )
    (tree / "fees.py").write_text(FEES_NEW)
    (tree / "test_fees.py").write_text(FEES_TESTS.replace("assert fee(10) == 9", "assert True"))
    config = AuditorConfig(sanctioned_test_rewrites=("test_fee_basic",))
    finding = _finding(Auditor(tree, config=config).tier1(), "assertion-preservation")
    assert finding.verdict == "fail"
    assert finding.detail.endswith(f"{GREEN_ON_BASELINE}test_fee_basic"), finding.detail


@pytest.mark.usefixtures("lowered_default")
def test_the_flat_audit_runs_under_the_projects_limit(tmp_path: Path) -> None:
    """`saddle audit`'s one-shot battery, tier 2 included: the new test's
    red-phase samples on the baseline must fail by assertion, not hang."""
    tree = _slow_tree(tmp_path, _setting(f"{ADMITS:g}"), new_test=True)
    checks = {c.name: c for c in audit_tree(tree).checks}
    assert checks["tests"].status == "pass", checks["tests"].detail
    assert checks["red-phase"].status == "pass", checks["red-phase"].detail


def test_a_limit_the_project_cannot_use_stops_the_audit(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Reported, never a silent default: the audit does not run at all."""
    tree = _slow_tree(tmp_path, _setting('"1800"'))
    with pytest.raises(AuditError, match="test-timeout = '1800' is not a number of seconds"):
        Auditor(tree).tier1()
    with pytest.raises(AuditError, match="test-timeout = '1800' is not a number of seconds"):
        audit_tree(tree)
    code = main(["audit", "--tiered", "--repo", str(tree), "--no-cache"])
    assert code == 2
    assert "error: cannot read the test time limit" in capsys.readouterr().err


# -- saddle run: node gates, sampled candidates, survivors, the merge suite -----------

SLICE_NODE: Final = {
    "id": "n1",
    "kind": "impl",
    "dependencies": [],
    "task_prompt": "Do n1.",
    "requirements": [
        {"id": "REQ-001", "statement": "REQ-001 holds.", "accepts": ["2"], "rejects": ["3"]}
    ],
    "execution_constraints": {
        "reasoning_budget": "low",
        "allowed_tools": ["read_file", "write_file", "run_tests", "lint"],
        "max_context_tokens": 8000,
    },
    "deterministic_gate": {
        "test_command": "pytest test_n.py",
        "changed_line_coverage_min": 100.0,
        "red_phase_required": True,
        "mutation_sample": {"scope": "changed-lines", "max_mutants": 100, "kill_threshold": 85.0},
    },
}
GOOD_DIFF: Final = (
    "diff --git a/n.py b/n.py\n--- /dev/null\n+++ b/n.py\n"
    "@@ -0,0 +1,2 @@\n+def f():\n+    return 2\n"
)
"""The worker envelope `slice` applies: one whole-file write section."""


def _slice_repo(root: Path, committed: str | None) -> None:
    """The node's slow test is committed red; the node's diff greens it."""
    test = _slow_test().replace("def test_f():", "def test_f():  # REQ-001")
    files = {"n.py": BASE_CODE, "test_n.py": test}
    if committed is not None:
        files["pyproject.toml"] = committed
    _commit(root, files, "baseline")


@pytest.mark.usefixtures("lowered_default")
def test_saddle_run_gates_and_merges_under_the_projects_limit(tmp_path: Path) -> None:
    """Every suite a `saddle run` starts -- the sampled candidates, the node's
    own gate and the unscoped merge suite -- is slower than the lowered
    built-in limit, so the run passes only if each one got the project's."""
    _slice_repo(tmp_path, _setting(f"{ADMITS:g}"))
    result = run_slice(
        "Fix f.",
        Dag.model_validate({"nodes": [SLICE_NODE]}),
        workdir=tmp_path,
        journal_path=tmp_path / "proofs.jsonl",
        propose=lambda node, failure, seed: DiffProposal(GOOD_DIFF, "return two"),
    )
    assert result.passed is True, result.transcript
    assert "merge-time full suite: exit 0" in (tmp_path / "proofs.jsonl").read_text()


@pytest.mark.usefixtures("lowered_default")
def test_saddle_run_without_the_setting_refuses_the_slow_suite(tmp_path: Path) -> None:
    _slice_repo(tmp_path, None)
    result = run_slice(
        "Fix f.",
        Dag.model_validate({"nodes": [SLICE_NODE]}),
        workdir=tmp_path,
        journal_path=tmp_path / "proofs.jsonl",
        propose=lambda node, failure, seed: DiffProposal(GOOD_DIFF, "return two"),
        merge_command=None,
    )
    assert result.passed is False
    assert "hangs: no verdict within the time limit" in result.transcript


@pytest.mark.usefixtures("lowered_default")
def test_a_survivor_candidate_runs_under_the_limit_it_is_given(tmp_path: Path) -> None:
    _slice_repo(tmp_path, None)
    node = Dag.model_validate({"nodes": [SLICE_NODE]}).nodes[0]
    other = tmp_path / "copy"
    _commit(other, {"n.py": FIXED_CODE, "test_n.py": _slow_test()})
    run = slice_module._candidate_runner(tmp_path, "HEAD", node, test_timeout=ADMITS)
    assert run(other, "test_n.py").exit_code == 0
    short = slice_module._candidate_runner(tmp_path, "HEAD", node, test_timeout=LOWERED)
    assert short(other, "test_n.py").exit_code == SHELL_TIMEOUT


def test_a_limit_the_project_cannot_use_stops_saddle_run_before_anything_is_sealed(
    tmp_path: Path,
) -> None:
    _slice_repo(tmp_path, _setting("0"))
    journal = tmp_path / "proofs.jsonl"
    with pytest.raises(ValueError, match="test-timeout = 0 is not a number of seconds"):
        run_slice(
            "Fix f.",
            Dag.model_validate({"nodes": [SLICE_NODE]}),
            workdir=tmp_path,
            journal_path=journal,
            propose=lambda node, failure, seed: DiffProposal(GOOD_DIFF, "return two"),
        )
    assert not journal.exists()
