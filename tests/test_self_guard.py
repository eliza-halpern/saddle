"""The self-guard: a run on saddle's own source cannot finish on its judges.

When saddle works on its own repository, a run could change the code that
judges runs. A run whose tree changes a guarded path (`auto.GUARDED_PATHS`: the gates,
evidence, auditor and audit modules; the feed and the turn engine, which
decide whether a finish is accepted; the sandbox and the memory cap, which
confine the auditor's runs; their tests, and the suite's conftest) ends
`stopped`, reason "needs you: ...", naming the paths, never `finished`. Each
half both ways: a guarded edit stops, an unrelated module or a near-miss of a
guarded name finishes; the guard is armed only on saddle's own source, so
another repo with a `gates.py` is unaffected; and it is armed at the
baseline, so a run cannot disarm it.
"""

from __future__ import annotations

import io
import json
import subprocess
from pathlib import Path
from typing import Any, cast

import pytest
from test_auto import Scripted, call, finish, namespace, sidecar
from test_feed import CHECK, FakeAuditor, Reactive, StopsAfter, Surfaces

from saddle import cli
from saddle.auto import GUARDED_MODULES, GUARDED_PATHS, SELF_PACKAGE, AutoOptions, run_auto
from saddle.engine import GUARDED_STOP
from saddle.journal import read_spans, verify_journal
from saddle.packet import compile_packet
from saddle.vllm import VllmClient

BUGGY = "def add(a, b):\n    return a - b\n"

FINISH_PATH_JUDGES = [
    "src/saddle/feed.py",
    "src/saddle/engine.py",
    "src/saddle/memcap.py",
    "src/saddle/sandbox.py",
    "tests/test_feed.py",
    "tests/test_feed_covtext.py",
    "tests/test_feed_said_once.py",
    "tests/test_feed_tally.py",
    "tests/test_chat_engine.py",
    "tests/test_memcap.py",
    "tests/test_sandbox_reach.py",
]
"""What decides whether a finish is accepted (the feed and the turn engine)
and what confines and caps the auditor's runs (sandbox, memory cap), with the
test files that pin each."""

GUARD_ITSELF = [
    "src/saddle/auto.py",
    "tests/test_auto.py",
    "tests/test_self_guard.py",
]
"""The module that defines the guard and its lists, and the tests that pin
them: a run that could shorten the list must not finish on it either."""

NEAR_MISSES = [
    "src/saddle/feeds.py",
    "src/saddle/engine_notes.py",
    "src/saddle/sandbox_util.py",
    "src/saddle/autos.py",
    "tests/test_feed_extra.py",
    "tests/test_engine.py",
]
"""Named files, never a pattern: paths that share a guarded module's stem but
are not it, and must still finish."""


def git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(repo), "-c", "user.name=t", "-c", "user.email=t@t", *args],
        check=True,
        capture_output=True,
        text=True,
    ).stdout


def _commit(root: Path, files: dict[str, str]) -> Path:
    for rel, text in files.items():
        (root / rel).parent.mkdir(parents=True, exist_ok=True)
        (root / rel).write_text(text)
    git(root, "init", "-q", "-b", "main")
    git(root, "add", "-A")
    git(root, "commit", "-q", "-m", "init")
    return root


@pytest.fixture
def saddle_repo(tmp_path: Path) -> Path:
    """A checkout shaped like saddle's own source."""
    return _commit(
        tmp_path / "saddle",
        {
            "calc.py": BUGGY,
            SELF_PACKAGE: "",
            "src/saddle/gates.py": "LIMIT = 1\n",
            "src/saddle/mutant_text.py": "WIDTH = 1\n",
            "tests/test_gates.py": "def test_limit():\n    assert 1\n",
            "tests/conftest.py": "",
            **dict.fromkeys(FINISH_PATH_JUDGES + GUARD_ITSELF + NEAR_MISSES, "LIMIT = 1\n"),
        },
    )


@pytest.fixture
def other_repo(tmp_path: Path) -> Path:
    """Not saddle: it has a `gates.py` (and its test) of its own."""
    return _commit(
        tmp_path / "other",
        {
            "calc.py": BUGGY,
            "gates.py": "LIMIT = 1\n",
            "src/other/__init__.py": "",
            "src/other/gates.py": "LIMIT = 1\n",
            "tests/test_gates.py": "def test_limit():\n    assert 1\n",
        },
    )


def _edit(path: str, call_id: str = "e1") -> list[Any]:
    return [call("edit_file", call_id, path=path, old="1\n", new="2\n")]


def _run(repo: Path, client: Any, **kwargs: Any) -> Any:
    kwargs.setdefault("arm", "E")
    options = AutoOptions(task="t", repo=repo, run_id=kwargs.pop("run_id", "g1"), **kwargs)
    return run_auto(options, cast(VllmClient, client))


def test_the_guarded_list_is_the_judges_the_finish_path_the_confinement_and_their_tests() -> None:
    assert GUARDED_MODULES == (
        "gates",
        "evidence",
        "auditor",
        "audit",
        "feed",
        "engine",
        "memcap",
        "sandbox",
        "auto",
    )
    assert {
        "src/saddle/gates.py",
        "src/saddle/evidence.py",
        "src/saddle/auditor.py",
        "src/saddle/audit.py",
        "tests/test_gates.py",
        "tests/test_evidence.py",
        "tests/test_auditor.py",
        "tests/test_audit.py",
        "tests/conftest.py",
        *FINISH_PATH_JUDGES,
        *GUARD_ITSELF,
    } == GUARDED_PATHS


def test_every_guarded_path_exists_in_saddle_itself() -> None:
    """Known-good for the list: it names files this repository has, so a
    rename cannot leave the guard naming a path nothing ever touches."""
    root = Path(__file__).resolve().parent.parent
    assert (root / SELF_PACKAGE).is_file()
    assert sorted(p for p in GUARDED_PATHS if not (root / p).is_file()) == []


# -- known-bad: a guarded edit cannot finish ------------------------------------


def test_a_run_that_edits_gates_py_stops_needing_you(saddle_repo: Path) -> None:
    result = _run(saddle_repo, Scripted([_edit("src/saddle/gates.py"), finish()]))
    assert result.outcome == "stopped"
    assert result.reason == GUARDED_STOP.format(paths="src/saddle/gates.py")
    assert result.reason.startswith("needs you: ")
    evidence = sidecar(result)
    assert evidence["outcome"] == "stopped"
    assert evidence["self_guard"] is True
    assert evidence["guarded_paths"] == ["src/saddle/gates.py"]
    assert evidence["narrative"] == "fixed add"  # the model's account is kept
    span = [s for s in read_spans(result.journal) if s.name.startswith("auto:")][-1]
    assert span.argv == ["auto:stopped"]
    assert span.exit_code == 3
    assert verify_journal(result.journal) == []
    packet = compile_packet(result.journal, run_id="g1")
    assert packet.verdict == "stopped"
    assert "src/saddle/gates.py" in packet.verdict_text
    assert "a person must review it" in packet.verdict_text
    assert f"stopped ({result.reason})" in git(
        saddle_repo, "log", "-1", "--format=%B", result.branch
    )
    # the edit itself is kept on the branch for the person to review
    assert git(saddle_repo, "show", f"{result.branch}:src/saddle/gates.py") == "LIMIT = 2\n"


def test_a_guarded_test_or_the_conftest_stops_it_too_naming_each(saddle_repo: Path) -> None:
    client = Scripted(
        [
            _edit("tests/test_gates.py", "e1"),
            [call("write_file", "w1", path="tests/conftest.py", content="X = 1\n")],
            _edit("src/saddle/mutant_text.py", "e2"),
            finish(),
        ]
    )
    result = _run(saddle_repo, client, allow_test_edits=True)
    assert result.outcome == "stopped"
    assert sidecar(result)["guarded_paths"] == ["tests/conftest.py", "tests/test_gates.py"]
    assert "(tests/conftest.py, tests/test_gates.py)" in result.reason


def test_the_cli_exits_stopped_on_a_guarded_run(saddle_repo: Path) -> None:
    out = io.StringIO()
    client = Scripted([_edit("src/saddle/gates.py"), finish()])
    code = cli.run_auto_command(namespace(saddle_repo), cast(VllmClient, client), stdout=out)
    assert code == cli.AUTO_STOPPED
    assert "needs you: " in out.getvalue()


def test_the_guard_is_armed_at_the_baseline_so_a_run_cannot_disarm_it(saddle_repo: Path) -> None:
    client = Scripted(
        [
            [call("run_command", "r1", command=f"rm {SELF_PACKAGE}")],
            _edit("src/saddle/gates.py"),
            finish(),
        ]
    )
    result = _run(saddle_repo, client)
    assert not (result.worktree / SELF_PACKAGE).exists()
    assert result.outcome == "stopped"
    assert sidecar(result)["guarded_paths"] == ["src/saddle/gates.py"]


def test_a_surfaced_accept_that_stands_is_held_as_well(saddle_repo: Path) -> None:
    """The other route to `finished`: an accept with not-proven findings that
    stands when the model then stops (engine.run_turn's promotion)."""
    fix = call("edit_file", "e1", path="calc.py", old="a - b", new="a + b")
    client = StopsAfter(
        [[*_edit("src/saddle/gates.py", "e2"), fix], [call("finish", "f1", summary="d")]]
    )
    result = _run(saddle_repo, client, arm="E+A+F", auditor_factory=lambda *a: Surfaces())
    assert result.outcome == "stopped"
    assert result.reason.startswith("needs you: ")
    assert sidecar(result)["guarded_paths"] == ["src/saddle/gates.py"]


def test_an_audited_accept_on_a_guarded_path_is_held(saddle_repo: Path) -> None:
    fix = call("edit_file", "e1", path="calc.py", old="a - b", new="a + b")
    client = Reactive(
        [[*_edit("src/saddle/gates.py", "e2"), fix], [CHECK], [call("finish", "f1", summary="d")]]
    )
    result = _run(saddle_repo, client, arm="E+A+F", auditor_factory=lambda *a: FakeAuditor())
    assert (result.outcome, sidecar(result)["audit"]["passed"]) == ("stopped", True)


@pytest.mark.parametrize("path", FINISH_PATH_JUDGES)
def test_a_run_that_edits_the_finish_path_or_the_confinement_stops_needing_you(
    saddle_repo: Path, path: str
) -> None:
    result = _run(saddle_repo, Scripted([_edit(path), finish()]), allow_test_edits=True)
    assert result.outcome == "stopped"
    assert result.reason == GUARDED_STOP.format(paths=path)
    assert sidecar(result)["guarded_paths"] == [path]


# -- known-good: what the guard must not stop ------------------------------------


@pytest.mark.parametrize("path", GUARD_ITSELF)
def test_a_run_that_edits_the_guard_itself_stops_needing_you(saddle_repo: Path, path: str) -> None:
    result = _run(saddle_repo, Scripted([_edit(path), finish()]), allow_test_edits=True)
    assert result.outcome == "stopped"
    assert result.reason == GUARDED_STOP.format(paths=path)


@pytest.mark.parametrize("path", NEAR_MISSES)
def test_a_run_on_a_near_miss_of_a_guarded_name_finishes(saddle_repo: Path, path: str) -> None:
    result = _run(saddle_repo, Scripted([_edit(path), finish()]), allow_test_edits=True)
    assert (result.outcome, result.reason) == ("finished", "finish called")
    assert "guarded_paths" not in sidecar(result)


def test_a_run_on_an_unrelated_saddle_module_finishes(saddle_repo: Path) -> None:
    result = _run(saddle_repo, Scripted([_edit("src/saddle/mutant_text.py"), finish()]))
    assert (result.outcome, result.reason) == ("finished", "finish called")
    evidence = sidecar(result)
    assert evidence["self_guard"] is True
    assert "guarded_paths" not in evidence


def test_a_repo_that_is_not_saddle_is_unaffected_by_its_own_gates_py(other_repo: Path) -> None:
    client = Scripted(
        [
            _edit("gates.py", "e1"),
            _edit("src/other/gates.py", "e2"),
            _edit("tests/test_gates.py", "e3"),
            finish(),
        ]
    )
    result = _run(other_repo, client, allow_test_edits=True)
    assert (result.outcome, result.reason) == ("finished", "finish called")
    evidence = sidecar(result)
    assert evidence["files_changed"] == ["gates.py", "src/other/gates.py", "tests/test_gates.py"]
    assert "self_guard" not in evidence
    assert "guarded_paths" not in evidence


def test_a_stop_is_not_relabelled_by_the_guard(saddle_repo: Path) -> None:
    """Only a finish is held: a budget stop keeps its own reason."""
    result = _run(saddle_repo, Scripted([_edit("src/saddle/gates.py")], tail=[]), token_budget=2)
    assert result.outcome == "stopped"
    assert result.reason.startswith("token budget exhausted")
    assert "guarded_paths" not in sidecar(result)
    assert json.dumps(sidecar(result)["files_changed"]) == '["src/saddle/gates.py"]'
