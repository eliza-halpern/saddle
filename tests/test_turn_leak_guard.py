"""The suite fails a test that leaves the chat server's turn patched (#174).

`app_for` (test_chat_server) puts the engine's turn back when it exits. One
test monkeypatched `saddle.web.app.run_turn` inside it, so its monkeypatch,
undone after `app_for` had exited, put `app_for`'s echoing fake back. Every
later chat-turn test in that worker process then answered with the question
and ran no tool: six process-list tests saw no process, and five turn-death
tests saw no tool call, but only when the scheduler put them after it, so they
read as failures under load.

Known-bad: a turn left patched after a test fails that test, by name, at its
teardown, and the engine's turn is put back. Known-good: the engine's own turn
is not a leak.
"""

from __future__ import annotations

from typing import Any

import conftest
import pytest

import saddle.web.app as app
from saddle import engine


def scripted(*_a: Any, **_k: Any) -> Any:
    return iter(())


def test_the_engines_turn_is_not_a_leak(request: pytest.FixtureRequest) -> None:
    assert conftest.leaked_turn() is None
    teardown = conftest.pytest_runtest_teardown(request.node)
    next(teardown)
    with pytest.raises(StopIteration):
        next(teardown)


def test_a_turn_left_patched_fails_its_test_by_name_and_is_put_back(
    request: pytest.FixtureRequest,
) -> None:
    teardown = conftest.pytest_runtest_teardown(request.node)
    next(teardown)  # the test's own fixtures are torn down here
    app.run_turn = scripted
    assert conftest.leaked_turn() == "scripted"
    with pytest.raises(pytest.fail.Exception) as failed:
        next(teardown)
    assert str(failed.value) == f"{request.node.nodeid} left saddle.web.app.run_turn as scripted"
    assert app.run_turn is engine.run_turn
