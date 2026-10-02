"""Full access gives a command the desktop session's variables (#133).

Known-good: in a full-access session a command sees each of the named
desktop variables saddle's own environment has set, and none it has not.

Known-bad: a sandboxed command (Edit without full access, which is what a Task
run's commands are too) sees none of them, and no variable outside the named
list reaches a full-access command: a model key in saddle's environment stays
out.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from saddle.sandbox import DESKTOP_ENV
from saddle.tools import ToolContext, execute_tool
from saddle.vllm import ToolCall

VALUES = {
    "DISPLAY": ":7",
    "WAYLAND_DISPLAY": "wayland-7",
    "XDG_RUNTIME_DIR": "/run/user/7777",
    "XDG_SESSION_TYPE": "wayland",
    "DBUS_SESSION_BUS_ADDRESS": "unix:path=/run/user/7777/bus",
    "PULSE_SERVER": "unix:/run/user/7777/pulse/native",
}
SECRET = "SADDLE_VLLM_API_KEY"


def seen_by_a_command(tmp_path: Path, *, full: bool) -> dict[str, str]:
    """Every variable a command sees, as the command's own Python reports them."""
    ctx = ToolContext(workdir=tmp_path, full_access=full)
    call = ToolCall(
        id="c1",
        name="run_command",
        arguments=json.dumps(
            {"command": "python3 -c 'import os,json;print(json.dumps(dict(os.environ)))'"}
        ),
    )
    result = execute_tool(call, workdir=tmp_path, context=ctx)
    return dict(json.loads(result.splitlines()[-1]))


@pytest.fixture
def desktop(monkeypatch: pytest.MonkeyPatch) -> dict[str, str]:
    for name, value in VALUES.items():
        monkeypatch.setenv(name, value)
    monkeypatch.setenv(SECRET, "k-secret-value")
    monkeypatch.setenv("SOME_OTHER_VARIABLE", "x")
    return dict(VALUES)


def test_the_named_list_is_the_six_the_issue_names() -> None:
    assert set(DESKTOP_ENV) == set(VALUES)


def test_a_full_access_command_sees_the_desktop_session(
    tmp_path: Path, desktop: dict[str, str]
) -> None:
    env = seen_by_a_command(tmp_path, full=True)
    assert {name: env.get(name) for name in desktop} == desktop


def test_a_variable_the_session_does_not_have_is_not_invented(
    tmp_path: Path, desktop: dict[str, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("PULSE_SERVER")
    env = seen_by_a_command(tmp_path, full=True)
    assert "PULSE_SERVER" not in env
    assert env["DISPLAY"] == ":7"


def test_a_sandboxed_command_sees_none_of_them(tmp_path: Path, desktop: dict[str, str]) -> None:
    env = seen_by_a_command(tmp_path, full=False)
    assert [name for name in desktop if name in env] == []


def test_nothing_outside_the_named_list_reaches_a_full_access_command(
    tmp_path: Path, desktop: dict[str, str]
) -> None:
    env = seen_by_a_command(tmp_path, full=True)
    assert SECRET not in env
    assert "SOME_OTHER_VARIABLE" not in env
