"""`SADDLE_SANDBOX_EXPOSE`: commands a sandboxed command may also run.

Known-good: a named command installed under a hidden prefix (a `bin/` inside
HOME, reached through a link on PATH) has its install directory shown
read-only and its link resolved, and `default_expose` carries both. Known-bad:
unset or empty shows nothing more, and a name not on PATH is skipped rather
than refusing the sandbox.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from saddle import sandbox
from saddle.sandbox import EXPOSE_ENV, default_expose, exposed_commands


def install(tmp_path: Path) -> tuple[Path, Path, Path]:
    """A runtime at <prefix>/bin/rt with a lib beside it, linked from a PATH dir."""
    prefix = tmp_path / "hidden" / "rt-1.0"
    (prefix / "bin").mkdir(parents=True)
    (prefix / "lib").mkdir()
    real = prefix / "bin" / "rt"
    real.write_text("#!/bin/sh\nexit 0\n")
    real.chmod(0o755)
    on_path = tmp_path / "local-bin"
    on_path.mkdir()
    (on_path / "rt").symlink_to(real)
    return prefix, real, on_path


def test_a_named_command_shows_its_install_prefix_and_its_link(tmp_path: Path) -> None:
    prefix, real, on_path = install(tmp_path)
    binds = exposed_commands(str(on_path), "rt")
    assert binds[prefix.resolve()] == prefix.resolve()  # the lib beside bin/ comes too
    assert binds[(on_path / "rt").absolute()] == real.resolve()  # the link still runs


def test_a_command_not_in_a_bin_dir_shows_its_own_directory(tmp_path: Path) -> None:
    home = tmp_path / "opt-like" / "browser"
    home.mkdir(parents=True)
    exe = home / "browser"
    exe.write_text("#!/bin/sh\nexit 0\n")
    exe.chmod(0o755)
    assert exposed_commands(str(home), "browser") == {home.resolve(): home.resolve()}


def test_unset_empty_or_unknown_names_show_nothing(tmp_path: Path) -> None:
    _prefix, _real, on_path = install(tmp_path)
    assert exposed_commands(str(on_path), "") == {}
    assert exposed_commands(str(on_path), " , ") == {}
    assert exposed_commands(str(on_path), "not-installed") == {}


def test_default_expose_carries_the_setting_and_only_with_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    prefix, _real, on_path = install(tmp_path)
    env = {"PATH": f"{on_path}{os.pathsep}{os.environ.get('PATH', '')}"}
    monkeypatch.delenv(EXPOSE_ENV, raising=False)
    without = {dest for _src, dest in default_expose(env)}
    monkeypatch.setenv(EXPOSE_ENV, "rt")
    shown = {dest for _src, dest in default_expose(env)}
    assert prefix.resolve() not in without
    assert prefix.resolve() in shown
    assert sandbox.EXPOSE_ENV not in sandbox.ENV_KEEP  # the setting never reaches a command


def test_a_binary_straight_in_local_bin_shows_itself_not_the_rest_of_local(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # `uv` lives at ~/.local/bin/uv: the parent of its bin/ is ~/.local, which holds
    # every other user-installed tool. Shown whole, a second named tool's link in
    # ~/.local/bin could not be bound inside it, and bwrap refused to start.
    home = tmp_path / "home"
    local_bin = home / ".local" / "bin"
    local_bin.mkdir(parents=True)
    (home / ".local" / "share" / "secret").mkdir(parents=True)
    uv = local_bin / "uv"
    uv.write_text("#!/bin/sh\nexit 0\n")
    uv.chmod(0o755)
    monkeypatch.setenv("HOME", str(home))
    assert exposed_commands(str(local_bin), "uv") == {uv.resolve(): uv.resolve()}
    prefix, _real, on_path = install(tmp_path)
    both = exposed_commands(f"{local_bin}{os.pathsep}{on_path}", "uv,rt")
    assert (home / ".local").resolve() not in both
    assert both[prefix.resolve()] == prefix.resolve()
