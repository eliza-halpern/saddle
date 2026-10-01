"""`[tool.saddle] sandbox-expose`: the commands a project's suite needs in the audit's sandbox.

Contract: the audit's confined runs of a project's tests can run each command
the project names in `sandbox-expose` in the `pyproject.toml` committed at the
audit's baseline, read-only, and no command the tree under audit names for
itself. Why: the sandbox hides HOME, where `node` lives for a project whose
tests drive a browser; those tests skipped inside the audit and the packet
said nothing was left unproven.

Known-good: a command installed under a hidden prefix runs inside a real
sandbox when the baseline names it, from `audit_tree` and from `Auditor`.
Known-bad: the same suite with no setting, or with a setting only the working
tree has, cannot run it; an unusable value is refused by name.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path
from typing import Any

import pytest
from test_auditor import BASE_CODE, FIXED_CODE, _git, _init
from test_sandbox_expose import install

from saddle import auditor as auditor_module
from saddle import sandbox
from saddle.audit import AuditError, audit_tree
from saddle.auditor import Auditor, AuditorConfig
from saddle.evidence import SuiteLimitError, sandbox_expose
from saddle.impact import ImpactMemo
from saddle.sandbox import EXPOSE_ENV, also_exposing, default_expose

needs_sandbox = pytest.mark.skipif(
    sandbox.isolation_problem() is not None, reason="needs a bwrap that can start"
)

# The test shells out to `rt`, a command that exists only under a hidden prefix.
RUNS_RT = (
    "import subprocess\n"
    "from n import f\n\n\n"
    "def test_f():\n"
    "    ran = subprocess.run(['rt'], capture_output=True, text=True, check=False)\n"
    "    assert ran.returncode == 0, ran.stderr\n"
    "    assert f() == 2\n"
)


def _setting(value: str) -> str:
    return f"[tool.saddle]\nsandbox-expose = {value}\n"


def _shown(env: dict[str, str]) -> set[Path]:
    return {dest for _src, dest in default_expose(env)}


# -- the reader ---------------------------------------------------------------


def test_the_names_are_read_from_the_commit_not_the_tree(tmp_path: Path) -> None:
    _init(tmp_path, {"pyproject.toml": _setting('["node", "google-chrome"]')})
    assert sandbox_expose(tmp_path, "HEAD") == ("node", "google-chrome")
    (tmp_path / "pyproject.toml").write_text(_setting('["node", "ssh"]'))
    assert sandbox_expose(tmp_path, "HEAD") == ("node", "google-chrome")  # an edit grants nothing
    _git(tmp_path, "add", "-A")
    assert sandbox_expose(tmp_path, "HEAD") == ("node", "google-chrome")


@pytest.mark.parametrize(
    "files",
    [
        {"n.py": BASE_CODE},
        {"pyproject.toml": "[tool.saddle]\ntest-workers = 2\n"},
        {"pyproject.toml": _setting("[]")},
    ],
    ids=["no-file", "no-key", "empty-list"],
)
def test_no_names_means_nothing_is_added(tmp_path: Path, files: dict[str, str]) -> None:
    _init(tmp_path, files)
    assert sandbox_expose(tmp_path, "HEAD") == ()


@pytest.mark.parametrize(
    "value",
    ['"node"', '["node", 3]', '["/usr/bin/node"]', '["node,ssh"]', '[""]', '["../x"]', "[[]]"],
    ids=["string", "number", "path", "comma", "empty", "dotdot", "nested"],
)
def test_a_value_that_is_not_a_list_of_bare_names_is_refused_by_name(
    tmp_path: Path, value: str
) -> None:
    _init(tmp_path, {"pyproject.toml": _setting(value)})
    with pytest.raises(SuiteLimitError, match="sandbox-expose") as caught:
        sandbox_expose(tmp_path, "HEAD")
    assert "is not a list of bare command names" in str(caught.value)


def test_a_misspelt_key_is_still_refused_not_ignored(tmp_path: Path) -> None:
    _init(tmp_path, {"pyproject.toml": "[tool.saddle]\nsandbox_expose = ['node']\n"})
    with pytest.raises(SuiteLimitError, match="sandbox_expose") as caught:
        sandbox_expose(tmp_path, "HEAD")
    assert "sandbox-expose" in str(caught.value)  # the message names the key that was meant


# -- the sandbox's side ----------------------------------------------------------


def test_names_given_to_also_exposing_show_a_command_only_inside_the_block(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    prefix, _real, on_path = install(tmp_path)
    env = {"PATH": f"{on_path}{os.pathsep}{os.environ.get('PATH', '')}"}
    monkeypatch.delenv(EXPOSE_ENV, raising=False)
    assert prefix.resolve() not in _shown(env)
    with also_exposing(["rt"]):
        assert prefix.resolve() in _shown(env)
    assert prefix.resolve() not in _shown(env)


def test_the_setting_adds_to_the_environment_variable_and_does_not_replace_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    first, _real, on_path = install(tmp_path / "a")
    second_home = tmp_path / "b" / "hidden" / "other-1.0"
    (second_home / "bin").mkdir(parents=True)
    other = second_home / "bin" / "other"
    other.write_text("#!/bin/sh\nexit 0\n")
    other.chmod(0o755)
    (on_path / "other").symlink_to(other)
    env = {"PATH": f"{on_path}{os.pathsep}{os.environ.get('PATH', '')}"}
    monkeypatch.setenv(EXPOSE_ENV, "rt")
    with also_exposing(["other"]):
        shown = _shown(env)
    assert {first.resolve(), second_home.resolve()} <= shown


# -- a real sandbox -----------------------------------------------------------------


def _project(tmp_path: Path, *, declared: bool) -> tuple[Path, Path]:
    _prefix, _real, on_path = install(tmp_path / "rt")
    tree = tmp_path / "tree"
    files = {"n.py": BASE_CODE}
    if declared:
        files["pyproject.toml"] = _setting('["rt"]')
    _init(tree, files)
    (tree / "n.py").write_text(FIXED_CODE)
    (tree / "test_n.py").write_text(RUNS_RT)
    return tree, on_path


def _tests_verdict(tree: Path) -> str:
    return {f.gate: f for f in Auditor(tree).tier1().findings}["tests"].verdict


@needs_sandbox
def test_a_declared_command_runs_in_the_auditor_and_an_undeclared_one_does_not(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv(EXPOSE_ENV, raising=False)
    with_setting, on_path = _project(tmp_path / "yes", declared=True)
    without, _ = _project(tmp_path / "no", declared=False)
    monkeypatch.setenv("PATH", f"{on_path}{os.pathsep}{os.environ['PATH']}")
    assert _tests_verdict(with_setting) == "pass"
    assert _tests_verdict(without) == "fail"


@needs_sandbox
def test_the_audited_tree_cannot_grant_itself_a_command(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv(EXPOSE_ENV, raising=False)
    tree, on_path = _project(tmp_path, declared=False)
    (tree / "pyproject.toml").write_text(_setting('["rt"]'))  # written after the baseline
    monkeypatch.setenv("PATH", f"{on_path}{os.pathsep}{os.environ['PATH']}")
    assert _tests_verdict(tree) == "fail"


@needs_sandbox
def test_audit_tree_runs_a_declared_command_and_refuses_without_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv(EXPOSE_ENV, raising=False)
    with_setting, on_path = _project(tmp_path / "yes", declared=True)
    without, _ = _project(tmp_path / "no", declared=False)
    monkeypatch.setenv("PATH", f"{on_path}{os.pathsep}{os.environ['PATH']}")
    statuses = {c.name: c.status for c in audit_tree(with_setting).checks}
    assert statuses["tests"] == "pass"
    assert {c.name: c.status for c in audit_tree(without).checks}["tests"] == "fail"


@needs_sandbox
def test_a_command_the_sandbox_shows_cannot_write_where_the_sandbox_does_not_allow(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Shown read-only: the install prefix cannot be written from inside."""
    prefix, _real, on_path = install(tmp_path / "rt")
    work = tmp_path / "work"
    work.mkdir()
    monkeypatch.setenv("PATH", f"{on_path}{os.pathsep}{os.environ['PATH']}")
    with also_exposing(["rt"]):
        argv, env = sandbox.confine(
            ["sh", "-c", f"touch {prefix}/lib/x 2>&1; test ! -e {prefix}/lib/x"], work
        )
    ran = subprocess.run(argv, env=env, capture_output=True, text=True, check=False)
    assert ran.returncode == 0, ran.stdout + ran.stderr
    assert "Read-only file system" in ran.stdout
    assert not (prefix / "lib" / "x").exists()


# -- the map's own suite run ----------------------------------------------------------


def test_the_map_is_drawn_with_the_project_commands_shown_and_a_bad_value_refuses(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    seen: list[tuple[str, ...]] = []

    def spy(*_args: Any, **_kwargs: Any) -> Any:
        seen.append(sandbox._EXPOSED.get())
        raise StopIteration  # nothing past the first suite run is under test

    monkeypatch.setattr(auditor_module, "run_suite_capture", spy)
    tree = tmp_path / "tree"
    _init(tree, {"n.py": BASE_CODE, "pyproject.toml": _setting('["rt"]')})
    with pytest.raises(StopIteration):
        Auditor(tree, "HEAD", AuditorConfig(impact=ImpactMemo())).draw_map()
    assert seen == [("rt",)]
    assert sandbox._EXPOSED.get() == ()  # and the block was left
    bad = tmp_path / "bad"
    _init(bad, {"n.py": BASE_CODE, "pyproject.toml": _setting('["/x"]')})
    with pytest.raises(AuditError, match="sandbox-expose"):
        Auditor(bad, "HEAD", AuditorConfig(impact=ImpactMemo())).draw_map()


def test_an_audit_with_an_unusable_value_is_refused_by_name(tmp_path: Path) -> None:
    tree = tmp_path / "tree"
    _init(tree, {"n.py": BASE_CODE, "pyproject.toml": _setting('"node"')})
    (tree / "n.py").write_text(FIXED_CODE)
    with pytest.raises(AuditError, match="sandbox-expose"):
        audit_tree(tree)
    with pytest.raises(AuditError, match="sandbox-expose"):
        Auditor(tree).tier1()
