"""LANECHIP: Ask is the default lane and cannot write; Edit is opt-in.

Known-good: in an Edit session a scripted `edit_file` call changes the file,
and the tools offered to the model include every file and shell tool.
Known-bad: in an Ask session (the default for a new session) the same call
gets a refusal tool result, the file is byte-for-byte unchanged, and the
tools offered to the model are read-only.
"""

from __future__ import annotations

import json
import time
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from starlette.testclient import TestClient

from saddle.sessions import SessionStore
from saddle.vllm import ToolCall
from saddle.web.app import ChatServer, build_app

ORIGINAL = "def add(a, b):\n    return a - b\n"
EDIT = ToolCall(
    id="e1",
    name="edit_file",
    arguments=json.dumps({"path": "calc.py", "old": "a - b", "new": "a + b"}),
)


class Scripted:
    """Emits one tool call on the first round and plain text after it."""

    def __init__(self, first: list[Any], seen: list[list[str]]) -> None:
        self.first = list(first)
        self.seen = seen

    def __enter__(self) -> Scripted:
        return self

    def __exit__(self, *_: object) -> None:
        return None

    def max_model_len(self) -> int:
        return 200_000

    def stream_chat(self, messages: Any, **kwargs: Any) -> Iterator[Any]:
        self.seen.append([t["function"]["name"] for t in kwargs.get("tools") or []])
        first, self.first = self.first, []
        return iter(first)


def _server_of(app: Any) -> ChatServer:
    return next(
        cell.cell_contents
        for route in app.routes
        for cell in (getattr(getattr(route, "endpoint", None), "__closure__", None) or ())
        if isinstance(cell.cell_contents, ChatServer)
    )


def _turn(tmp_path: Path, mode: str | None) -> tuple[list[list[str]], list[dict[str, Any]]]:
    (tmp_path / "work").mkdir()
    target = tmp_path / "work" / "calc.py"
    target.write_text(ORIGINAL)
    store = SessionStore(tmp_path / "s")
    seen: list[list[str]] = []
    app = build_app(store, lambda: Scripted([EDIT], seen), default_workdir=tmp_path / "work")
    server = _server_of(app)
    with TestClient(app) as client:
        sid = client.post("/api/sessions", json={}).json()["id"]
        if mode is not None:
            assert client.patch(f"/api/sessions/{sid}", json={"mode": mode}).status_code == 200
        assert client.post(f"/api/sessions/{sid}/message", json={"text": "why?"}).status_code == 200
        deadline = time.monotonic() + 10
        while server._live(sid).busy and time.monotonic() < deadline:
            time.sleep(0.02)
    return seen, store.load_messages(sid)


def _tool_results(messages: list[dict[str, Any]]) -> list[str]:
    return [str(m["content"]) for m in messages if m.get("role") == "tool"]


def test_a_new_session_is_ask_and_an_edit_there_is_refused(tmp_path: Path) -> None:
    seen, messages = _turn(tmp_path, None)
    assert (tmp_path / "work" / "calc.py").read_text() == ORIGINAL
    [result] = _tool_results(messages)
    assert result.startswith("error: ")
    assert "Ask" in result
    assert "not changed" in result
    assert seen
    for offered in seen:
        assert "edit_file" not in offered
        assert "write_file" not in offered
        assert "run_command" not in offered
        assert "read_file" in offered


def test_an_edit_session_edits(tmp_path: Path) -> None:
    seen, messages = _turn(tmp_path, "edit")
    assert (tmp_path / "work" / "calc.py").read_text() == ORIGINAL.replace("a - b", "a + b")
    [result] = _tool_results(messages)
    assert not result.startswith("error: ")
    assert {"edit_file", "write_file", "run_command"} <= set(seen[0])


def test_a_new_session_defaults_to_ask(tmp_path: Path) -> None:
    store = SessionStore(tmp_path / "s")
    assert store.create(title="t", workdir=str(tmp_path)).mode == "ask"


@pytest.mark.parametrize("legacy", ["chat", None])
def test_a_session_saved_before_lanes_reads_as_ask(tmp_path: Path, legacy: str | None) -> None:
    store = SessionStore(tmp_path / "s")
    sid = store.create(title="old", workdir=str(tmp_path)).id
    meta = tmp_path / "s" / sid / "session.json"
    data = json.loads(meta.read_text())
    if legacy is None:
        del data["mode"]
    else:
        data["mode"] = legacy
    meta.write_text(json.dumps(data))
    assert store.get(sid).mode == "ask"


def test_the_lanes_are_exactly_ask_edit_and_task() -> None:
    from saddle.sessions import SESSION_MODES

    assert SESSION_MODES == ("ask", "edit", "task")


def test_ask_tools_are_read_only_and_edit_tools_are_all() -> None:
    from saddle.tools import TOOLS, tools_for_mode

    names = lambda mode: [t["function"]["name"] for t in tools_for_mode(mode)]  # noqa: E731
    assert names("ask") == ["read_file", "list_dir", "search"]
    assert names("edit") == [t["function"]["name"] for t in TOOLS]
    # Anything that is not exactly "edit" gets the read-only set.
    assert names("task") == names("ask") == names("chat")


def test_the_ask_guard_refuses_every_write_tool_and_lets_reads_through(tmp_path: Path) -> None:
    from saddle.tools import ASK_TOOLS, REFUSED, ToolContext, execute_tool

    (tmp_path / "a.txt").write_text("hello\n")
    ctx = ToolContext(workdir=tmp_path, allowed=tuple(t["function"]["name"] for t in ASK_TOOLS))
    for name, args in [
        ("write_file", {"path": "a.txt", "content": "x"}),
        ("edit_file", {"path": "a.txt", "old": "hello", "new": "bye"}),
        ("run_command", {"command": "echo x > a.txt"}),
        ("read_terminal", {"id": "t1"}),
        ("wait_for_terminal", {"id": "t1"}),
    ]:
        result = execute_tool(
            ToolCall(id="c", name=name, arguments=json.dumps(args)), workdir=tmp_path, context=ctx
        )
        assert result.startswith(REFUSED), name
    assert (tmp_path / "a.txt").read_text() == "hello\n"
    read = ToolCall(id="r", name="read_file", arguments=json.dumps({"path": "a.txt"}))
    assert "hello" in execute_tool(read, workdir=tmp_path, context=ctx)
    # An unknown tool is still reported as unknown, not as an Ask refusal.
    odd = ToolCall(id="u", name="nope", arguments="{}")
    assert "unknown tool" in execute_tool(odd, workdir=tmp_path, context=ctx)


def test_git_branch_names_the_checkout_or_nothing(tmp_path: Path) -> None:
    import subprocess

    from saddle.web.app import git_branch

    assert git_branch(tmp_path / "absent") == ""
    repo = tmp_path / "r"
    repo.mkdir()
    subprocess.run(["git", "init", "-q", "-b", "lanes", str(repo)], check=True)
    assert git_branch(repo) == ""  # no commit yet: HEAD names no branch git can resolve
    subprocess.run(
        [
            "git",
            "-C",
            str(repo),
            "-c",
            "user.name=t",
            "-c",
            "user.email=t@t",
            "commit",
            "-q",
            "--allow-empty",
            "-m",
            "i",
        ],
        check=True,
    )
    assert git_branch(repo) == "lanes"


def test_git_branch_is_empty_when_git_cannot_run(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import saddle.web.app as module

    def boom(*_a: Any, **_k: Any) -> Any:
        missing = "git"
        raise FileNotFoundError(missing)

    monkeypatch.setattr(module.subprocess, "run", boom)
    assert module.git_branch(tmp_path) == ""


# -- the browser --------------------------------------------------------------

import shutil  # noqa: E402
import subprocess  # noqa: E402

from test_ui3_mode import NoModel, serving  # noqa: E402

CDP = Path(__file__).parent / "fixtures" / "lanechip_cdp.mjs"
BROWSER = shutil.which("node") and shutil.which("google-chrome")
needs_browser = pytest.mark.skipif(not BROWSER, reason="needs node and google-chrome")


def _browser(base: str, sid: str, step: str) -> dict[str, Any]:
    out = subprocess.run(
        ["node", str(CDP), base, sid, step],
        capture_output=True,
        text=True,
        timeout=90,
        check=False,
    )
    assert out.returncode == 0, out.stderr
    return json.loads(out.stdout.strip().splitlines()[-1])


@needs_browser
def test_the_chip_shows_six_lanes_three_greyed_and_shift_tab_cycles_the_rest(
    tmp_path: Path,
) -> None:
    store = SessionStore(tmp_path / "s")
    app = build_app(store, NoModel, default_workdir=tmp_path)
    with serving(app) as base:
        sid = store.create(title="t", workdir=str(tmp_path)).id
        got = _browser(base, sid, "chip")
        final = store.get(sid).mode
    assert got["before"]["lane"] == "ask"
    assert got["before"]["chipText"] == "Ask"
    assert "read-only" in got["before"]["placeholder"]
    assert got["open"]["menuOpen"] is True
    lanes = [(o["lane"], o["disabled"]) for o in got["options"]]
    assert lanes == [
        ("ask", False),
        ("edit", False),
        ("task", False),
        ("feature", True),
        ("breadth", True),
        ("long", True),
    ]
    assert all(o["desc"] for o in got["options"])
    assert "unaudited, edits your folder" in got["options"][1]["desc"].lower()
    # Clicking the greyed lanes chose nothing and asked the server for nothing.
    assert got["afterDisabledClicks"]["lane"] == "ask"
    assert got["afterDisabledClicks"]["patches"] == []
    assert got["cycle"] == ["edit", "task", "ask", "edit"]
    assert got["after"]["patches"] == ["edit", "task", "ask", "edit"]
    assert final == "edit"


@needs_browser
def test_edit_chosen_from_the_menu_is_remembered_for_the_session(tmp_path: Path) -> None:
    store = SessionStore(tmp_path / "s")
    app = build_app(store, NoModel, default_workdir=tmp_path)
    with serving(app) as base:
        # Created first, so the page opens `sid` (the most recently updated).
        other = store.create(title="u", workdir=str(tmp_path)).id
        sid = store.create(title="t", workdir=str(tmp_path)).id
        got = _browser(base, sid, "menu-pick")
    assert got["afterEnter"]["lane"] == "edit"
    assert got["afterEnter"]["menuOpen"] is False
    assert "unaudited" in got["afterEnter"]["placeholder"]
    assert got["afterReload"]["lane"] == "edit"
    assert got["afterReload"]["chipText"] == "Edit"
    assert store.get(sid).mode == "edit"
    assert store.get(other).mode == "ask"


@needs_browser
def test_the_suggestion_is_shown_and_never_changes_the_lane_by_itself(tmp_path: Path) -> None:
    store = SessionStore(tmp_path / "s")
    app = build_app(store, NoModel, default_workdir=tmp_path)
    with serving(app) as base:
        sid = store.create(title="t", workdir=str(tmp_path)).id
        store.update(sid, mode="edit")
        got = _browser(base, sid, "heuristic")
    assert got["taskish"]["suggest"] == "looks like a task → Tab"
    assert got["taskish"]["lane"] == "edit"
    assert got["taskish"]["patches"] == []
    assert got["question"]["suggest"].startswith("looks like a question")
    assert got["question"]["lane"] == "edit"
    assert got["question"]["patches"] == []
    # Only the user's Tab takes it.
    assert got["afterTab"]["lane"] == "task"
    assert got["afterTab"]["patches"] == ["task"]


@needs_browser
def test_a_scripted_edit_in_ask_is_refused_on_screen_and_on_disk(tmp_path: Path) -> None:
    (tmp_path / "work").mkdir()
    target = tmp_path / "work" / "calc.py"
    target.write_text(ORIGINAL)
    store = SessionStore(tmp_path / "s")
    seen: list[list[str]] = []
    app = build_app(store, lambda: Scripted([EDIT], seen), default_workdir=tmp_path / "work")
    with serving(app) as base:
        sid = store.create(title="t", workdir=str(tmp_path / "work")).id
        got = _browser(base, sid, "ask-edit")
    assert got["before"]["lane"] == "ask"
    assert got["before"]["where"].startswith("work")
    assert target.read_text() == ORIGINAL
    assert "edit_file" not in seen[0]
    tool = got["tool"]
    assert tool["failed"] is True
    assert tool["open"] is True
    assert "not available in the Ask lane" in tool["text"]


@needs_browser
def test_the_task_lane_still_opens_the_strip_and_escape_closes_it(tmp_path: Path) -> None:
    store = SessionStore(tmp_path / "s")
    app = build_app(store, NoModel, default_workdir=tmp_path)
    with serving(app) as base:
        sid = store.create(title="t", workdir=str(tmp_path)).id
        store.update(sid, mode="task")
        got = _browser(base, sid, "task-strip")
    assert got["strip"]["stripVisible"] is True
    assert got["strip"]["focus"] == "tc-start"
    assert got["strip"]["taskPosts"] == 0
    assert got["afterEscape"]["stripVisible"] is False
    assert got["afterEscape"]["focus"] == "input"
