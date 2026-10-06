"""The tools the model is told it has are the ones its sandbox can run.

A watched run spent hours hand-rolling page scripts before it found the box
had a browser; another was told nothing about `uv`, which its commands could
not start although the host had it. The prompt now says, per tool, whether a
confined command can run it, by the sandbox's own exposure rules.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest
from test_auto import Scripted, auto, finish, git
from test_run_env import _repo, _system

from saddle import auto as auto_module
from saddle import sandbox
from saddle.auto import JS_MUTATION_PROMPT, detected_tools
from saddle.jsevidence import STRYKER_PACKAGE


def _tool(home: Path, name: str) -> Path:
    """A runnable `name` in `home/bin`, a directory no system bind shows."""
    bin_dir = home / "bin"
    bin_dir.mkdir(parents=True, exist_ok=True)
    tool = bin_dir / name
    tool.write_text("#!/bin/sh\necho ran\n")
    tool.chmod(0o755)
    return tool


def test_a_tool_on_the_host_path_but_hidden_from_the_sandbox_is_not_reachable(
    tmp_path: Path,
) -> None:
    _tool(tmp_path / "hidden", "faketool")
    env = {"PATH": f"{tmp_path / 'hidden' / 'bin'}:/usr/bin:/bin"}
    assert sandbox.reachable(["faketool", "sh"], env) == ["sh"]  # known-bad: host which says yes
    with sandbox.also_exposing(["faketool"]):
        assert sandbox.reachable(["faketool", "sh"], env) == ["faketool", "sh"]  # known-good


def test_reachable_agrees_with_a_confined_command(tmp_path: Path) -> None:
    """The probe's answer is what `command -v` says inside the sandbox."""
    _tool(tmp_path / "hidden", "faketool")
    work = tmp_path / "work"  # the confined tree; the tool is outside it
    work.mkdir()
    path = f"{tmp_path / 'hidden' / 'bin'}:/usr/bin:/bin"
    script = "command -v faketool >/dev/null && echo yes || echo no"
    for exposed, want in (((), "no"), (("faketool",), "yes")):
        with sandbox.also_exposing(exposed):
            argv, env = sandbox.confine(["sh", "-c", script], work, extra_env={"PATH": path})
            said = subprocess.run(argv, env=env, capture_output=True, text=True).stdout.strip()
            assert said == want
            assert (sandbox.reachable(["faketool"], {"PATH": path}) == ["faketool"]) is (
                want == "yes"
            )


def test_the_prompt_names_what_is_there_and_what_is_not(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(auto_module, "DETECTED_TOOLS", ("sh", "no-such-tool-5e1d"))
    said = detected_tools({"PATH": os.environ["PATH"]})
    assert said == " On your PATH: `sh`. Not on it: `no-such-tool-5e1d`."
    monkeypatch.setattr(auto_module, "DETECTED_TOOLS", ("no-such-tool-5e1d",))
    assert detected_tools({"PATH": os.environ["PATH"]}) == (
        " On your PATH: none of `no-such-tool-5e1d`."
    )


def test_a_run_is_told_its_tools(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(auto_module, "DETECTED_TOOLS", ("sh", "no-such-tool-5e1d"))
    client = Scripted([finish()])
    auto(_repo(tmp_path / "repo", src=False), client)
    assert "On your PATH: `sh`. Not on it: `no-such-tool-5e1d`." in _system(client)


def test_a_run_is_told_what_js_mutation_counts_only_where_it_applies(tmp_path: Path) -> None:
    plain = Scripted([finish()])
    auto(_repo(tmp_path / "plain", src=False), plain)
    assert "StrykerJS" not in _system(plain)  # known-bad: no JS gate, no claim about one

    root = _repo(tmp_path / "js", src=False)
    (root / STRYKER_PACKAGE).parent.mkdir(parents=True)
    (root / STRYKER_PACKAGE).write_text("")
    (root / "tests" / "page.test.js").write_text("")
    subprocess.run(["git", "-C", str(root), "add", "tests/page.test.js"], check=True)
    subprocess.run(
        [
            "git",
            "-C",
            str(root),
            "-c",
            "user.name=t",
            "-c",
            "user.email=t@t",
            "commit",
            "-qm",
            "js",
        ],
        check=True,
    )
    js = Scripted([finish()])
    auto(root, js)
    assert JS_MUTATION_PROMPT.format(files="`tests/page.test.js`") in _system(js)


def test_a_run_counts_the_tools_its_project_exposes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`[tool.saddle] sandbox-expose` shows a tool to the model's commands, so
    the prompt must count it; read without that exposure it is hidden."""
    _tool(tmp_path / "hidden", "faketool")
    monkeypatch.setenv("PATH", f"{tmp_path / 'hidden' / 'bin'}:{os.environ['PATH']}")
    monkeypatch.setattr(auto_module, "DETECTED_TOOLS", ("faketool",))
    root = _repo(tmp_path / "repo", src=False)
    (root / "pyproject.toml").write_text('[tool.saddle]\nsandbox-expose = ["faketool"]\n')
    git(root, "add", "pyproject.toml")
    git(root, "commit", "-qm", "expose")
    client = Scripted([finish()])
    auto(root, client)
    assert "On your PATH: `faketool`." in _system(client)
