"""Full access lets the file tools reach outside the folder (#135).

Known-good: in a full-access Edit session `write_file`, `edit_file`,
`read_file` and `list_dir` take absolute, `~` and `..` paths; each outside
write backs the original up first and lands in the session's side-effect
record; the tools' own descriptions say so before any call is made.

Known-bad: without full access (a sandboxed session, a task run, a full-access
context with no record to put backups in) the same call is refused as it always
was, writes nothing and records nothing; a task's tool list is the plain one.
"""

from __future__ import annotations

import ast
import json
from pathlib import Path
from typing import Any

import pytest
from test_chat_server import engine_app

from saddle.sessions import FULL_ACCESS_CONFIRM, SessionStore
from saddle.sideeffects import SideEffects
from saddle.tools import (
    ASK_TOOLS,
    OUTSIDE_DESCRIPTIONS,
    TOOLS,
    ToolContext,
    execute_tool,
    scope_turn,
    tools_for_mode,
)
from saddle.vllm import StreamToken, ToolCall


@pytest.fixture
def home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    place = tmp_path / "home"
    place.mkdir()
    monkeypatch.setenv("HOME", str(place))
    return place


@pytest.fixture
def folder(tmp_path: Path) -> Path:
    place = tmp_path / "folder"
    place.mkdir()
    return place


def call(ctx: ToolContext, name: str, **args: Any) -> str:
    return execute_tool(
        ToolCall(id="c", name=name, arguments=json.dumps(args)), workdir=ctx.workdir, context=ctx
    )


def full(folder: Path, tmp_path: Path) -> ToolContext:
    return ToolContext(workdir=folder, full_access=True, effects=SideEffects(tmp_path / "rec"))


def test_an_absolute_write_outside_the_folder_is_backed_up_recorded_and_undoable(
    tmp_path: Path, home: Path, folder: Path
) -> None:
    config = home / ".config" / "app.conf"
    config.parent.mkdir()
    original = b"mode=dark\r\n\xff"
    config.write_bytes(original)
    ctx = full(folder, tmp_path)
    result = call(ctx, "write_file", path=str(config), content="mode=light\n")
    assert config.read_text() == "mode=light\n"
    assert "mode=light" in result
    assert ctx.effects is not None
    (row,) = ctx.effects.view()["files"]
    assert (row["path"], row["change"], row["backup"], row["via"]) == (
        str(config),
        "changed",
        "backed up",
        "write_file",
    )
    ctx.effects.undo(delete_created=False)
    assert config.read_bytes() == original


def test_edit_file_reaches_a_tilde_path_and_a_new_file_is_recorded_created(
    tmp_path: Path, home: Path, folder: Path
) -> None:
    (home / "rc").write_text("a=1\nb=2\n")
    ctx = full(folder, tmp_path)
    assert "a=9" in call(ctx, "edit_file", path="~/rc", old="a=1", new="a=9")
    assert (home / "rc").read_text() == "a=9\nb=2\n"
    call(ctx, "write_file", path="$HOME/apps/new.desktop", content="[Desktop Entry]\n")
    assert ctx.effects is not None
    rows = {Path(f["path"]).name: (f["change"], f["via"]) for f in ctx.effects.view()["files"]}
    assert rows == {"rc": ("changed", "edit_file"), "new.desktop": ("created", "write_file")}


def test_a_dotdot_path_leaves_the_folder_in_full_access_and_is_refused_otherwise(
    tmp_path: Path, folder: Path
) -> None:
    ctx = full(folder, tmp_path)
    call(ctx, "write_file", path="../sibling.txt", content="out")
    assert (tmp_path / "sibling.txt").read_text() == "out"
    plain = ToolContext(workdir=folder)
    refused = call(plain, "write_file", path="../other.txt", content="out")
    assert refused.startswith("error:")
    assert not (tmp_path / "other.txt").exists()


@pytest.mark.parametrize("which", ["sandboxed", "no record", "task"])
def test_without_full_access_and_a_record_an_outside_write_is_refused_and_leaves_nothing(
    which: str, tmp_path: Path, home: Path, folder: Path
) -> None:
    target = home / "victim.txt"
    target.write_text("untouched")
    record = SideEffects(tmp_path / "rec")
    ctx = {
        "sandboxed": ToolContext(workdir=folder, effects=record),
        "no record": ToolContext(workdir=folder, full_access=True),
        "task": ToolContext(workdir=folder, protected_tests=("tests",), syntax_guard=True),
    }[which]
    for name, args in (
        ("write_file", {"path": str(target), "content": "x"}),
        ("edit_file", {"path": str(target), "old": "untouched", "new": "x"}),
        ("read_file", {"path": str(target)}),
        ("list_dir", {"path": str(home)}),
    ):
        assert call(ctx, name, **args).startswith("error:"), (which, name)
    assert target.read_text() == "untouched"
    assert not (tmp_path / "rec").exists()


def test_reads_outside_the_folder_work_in_full_access_and_search_stays_inside(
    tmp_path: Path, home: Path, folder: Path
) -> None:
    (home / "notes.txt").write_text("outside text")
    (folder / "inside.txt").write_text("needle inside")
    (home / "other.txt").write_text("needle outside")
    ctx = full(folder, tmp_path)
    assert call(ctx, "read_file", path=str(home / "notes.txt")) == "outside text"
    assert "notes.txt" in call(ctx, "list_dir", path="~")
    found = call(ctx, "search", query="needle", glob=str(home / "*.txt"))
    assert found.startswith("error:")
    assert "outside" not in call(ctx, "search", query="needle")
    assert not (tmp_path / "rec").exists()  # reading records nothing


def test_a_write_that_changes_nothing_or_fails_leaves_no_record(
    tmp_path: Path, home: Path, folder: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    target = home / "same.txt"
    target.write_text("same")
    ctx = full(folder, tmp_path)
    call(ctx, "write_file", path=str(target), content="same")
    assert ctx.effects is not None
    assert ctx.effects.view()["empty"] is True
    real = Path.write_text

    def refuse(self: Path, *args: Any, **kwargs: Any) -> int:
        if self == target:
            raise OSError
        return real(self, *args, **kwargs)

    monkeypatch.setattr(Path, "write_text", refuse)
    assert call(ctx, "write_file", path=str(target), content="new").startswith("error: cannot")
    assert call(ctx, "edit_file", path=str(target), old="same", new="new").startswith(
        "error: cannot"
    )
    assert ctx.effects.view()["empty"] is True


def test_a_write_inside_the_folder_still_goes_to_the_folders_undo_not_the_record(
    tmp_path: Path, folder: Path
) -> None:
    from saddle.undo import UndoLog

    ctx = full(folder, tmp_path)
    ctx.undo = UndoLog(tmp_path / "undo")
    ctx.undo.begin(1, 0)
    call(ctx, "write_file", path="inside.txt", content="in")
    assert ctx.effects is not None
    assert ctx.effects.view()["empty"] is True
    assert ctx.undo.versions() == {"c": ctx.undo.versions()["c"]}


# --- the rule is stated before it can refuse ------------------------------


def described(tools: list[dict[str, Any]]) -> dict[str, str]:
    return {t["function"]["name"]: t["function"]["description"] for t in tools}


def test_the_full_access_file_tools_say_they_reach_outside_and_are_backed_up(
    tmp_path: Path, folder: Path
) -> None:
    offered = described(scope_turn(full(folder, tmp_path), "edit"))
    for name in ("write_file", "edit_file"):
        text = offered[name]
        assert "outside the working directory" in text
        assert "backed up" in text
        assert "undo" in text
    for name in ("read_file", "list_dir"):
        assert "outside the working directory" in offered[name]
    assert "under the working directory only" in offered["search"]
    assert set(OUTSIDE_DESCRIPTIONS) <= set(offered)


def test_without_the_record_the_tools_are_exactly_the_plain_ones(
    tmp_path: Path, folder: Path
) -> None:
    for ctx in (
        ToolContext(workdir=folder),
        ToolContext(workdir=folder, full_access=True),
        ToolContext(workdir=folder, effects=SideEffects(tmp_path / "r")),
    ):
        assert scope_turn(ctx, "edit") == TOOLS
    assert scope_turn(full(folder, tmp_path), "ask") == ASK_TOOLS
    assert tools_for_mode("edit") == TOOLS


def test_a_task_run_builds_its_context_without_full_access_or_a_record() -> None:
    """The consumer, read: `auto.run_auto` names neither, so a task run's file
    tools stay inside its worktree and it keeps no record."""
    source = Path(__file__).parent.parent / "src" / "saddle" / "auto.py"
    calls = [
        node
        for node in ast.walk(ast.parse(source.read_text()))
        if isinstance(node, ast.Call) and getattr(node.func, "id", "") == "ToolContext"
    ]
    assert calls
    for node in calls:
        assert {"full_access", "effects"}.isdisjoint(k.arg for k in node.keywords)


# --- through the server ---------------------------------------------------


def test_a_full_access_chat_turn_writes_outside_and_the_session_keeps_the_record(
    tmp_path: Path, home: Path, folder: Path
) -> None:
    from test_chat_server import _server_of

    target = home / ".config" / "tool.toml"
    target.parent.mkdir()
    target.write_text("x = 1\n")
    rounds: list[list[Any]] = [
        [
            ToolCall(
                id="w",
                name="write_file",
                arguments=json.dumps({"path": str(target), "content": "x = 2\n"}),
            )
        ],
        [StreamToken(stream="content", text="done")],
    ]
    store = SessionStore(tmp_path / "s")
    with engine_app(store, folder, rounds) as (client, app):
        sid = client.post("/api/sessions", json={"workdir": str(folder)}).json()["id"]
        client.patch(f"/api/sessions/{sid}", json={"mode": "edit"})
        server = _server_of(app)
        server._run(sid, "sandboxed first")
        assert target.read_text() == "x = 1\n"  # refused: not in full access yet
        client.post(
            f"/api/sessions/{sid}/full-access", json={"on": True, "confirm": FULL_ACCESS_CONFIRM}
        )
        server._run(sid, "now with full access")
    assert target.read_text() == "x = 2\n"
    record = SideEffects(store.outside_dir(sid))
    (row,) = record.view()["files"]
    assert (row["path"], row["change"]) == (str(target), "changed")
    record.undo(delete_created=False)
    assert target.read_text() == "x = 1\n"
