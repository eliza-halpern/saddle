"""A full-access command's outside effects reach the session record (#137).

Known-good: a session that edits a config file outside its folder with `sed -i`
and downloads one archive with `curl` shows both in the record, and Undo puts
the config back byte for byte.

Known-bad: a command whose effects its arguments do not name (a script that
writes where it likes) is listed as not tracked, and the file it wrote is not
claimed; a sandboxed session's commands are not watched at all.
"""

from __future__ import annotations

import functools
import http.server
import json
import shutil
import threading
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from saddle.sideeffects import SideEffects
from saddle.tools import ToolContext, execute_tool
from saddle.vllm import ToolCall


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


@pytest.fixture
def server(tmp_path: Path) -> Iterator[str]:
    """A local HTTP server holding one 3000-byte archive."""
    root = tmp_path / "www"
    root.mkdir()
    (root / "pkg.tgz").write_bytes(b"z" * 3000)
    handler = functools.partial(http.server.SimpleHTTPRequestHandler, directory=str(root))
    httpd = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{httpd.server_address[1]}"
    httpd.shutdown()
    httpd.server_close()


def run(ctx: ToolContext, command: str, **extra: Any) -> str:
    call = ToolCall(id="c", name="run_command", arguments=json.dumps({"command": command, **extra}))
    return execute_tool(call, workdir=ctx.workdir, context=ctx)


def full(folder: Path, tmp_path: Path) -> ToolContext:
    return ToolContext(workdir=folder, full_access=True, effects=SideEffects(tmp_path / "rec"))


@pytest.mark.skipif(shutil.which("curl") is None, reason="needs curl")
def test_a_config_edit_and_a_download_both_show_and_undo_restores_the_config(
    tmp_path: Path, home: Path, folder: Path, server: str
) -> None:
    config = home / ".config" / "app.conf"
    config.parent.mkdir()
    original = b"theme=dark\r\nkeep=\xff\n"
    config.write_bytes(original)
    ctx = full(folder, tmp_path)
    edited = run(ctx, f"sed -i s/dark/light/ {config}")
    assert edited.endswith("exit 0\n")
    run(ctx, f"mkdir -p {home}/Downloads && curl -s -o {home}/Downloads/pkg.tgz {server}/pkg.tgz")
    assert ctx.effects is not None
    view = ctx.effects.view()
    changes = {Path(f["path"]).name: f["change"] for f in view["files"]}
    assert changes == {"app.conf": "changed", "Downloads": "created", "pkg.tgz": "created"}
    (download,) = view["downloads"]
    assert (download["source"], download["host"], download["size"]) == (
        f"{server}/pkg.tgz",
        "127.0.0.1",
        3000,
    )
    assert download["path"] == str(home / "Downloads" / "pkg.tgz")
    assert view["not_tracked"] == []
    ctx.effects.undo(delete_created=False)
    assert config.read_bytes() == original
    assert (home / "Downloads" / "pkg.tgz").exists()  # created files wait for confirmation
    ctx.effects.undo(delete_created=True)
    assert not (home / "Downloads").exists()


def test_a_script_that_writes_where_it_likes_is_not_tracked_and_its_file_is_not_claimed(
    tmp_path: Path, home: Path, folder: Path
) -> None:
    script = folder / "install.sh"
    script.write_text(f"#!/bin/sh\necho hi > {home}/ghost.txt\n")
    ctx = full(folder, tmp_path)
    run(ctx, "bash install.sh")
    assert (home / "ghost.txt").exists()
    assert ctx.effects is not None
    view = ctx.effects.view()
    assert view["files"] == []
    (row,) = view["not_tracked"]
    assert row["command"] == "bash install.sh"
    assert "bash" in row["reasons"][0]
    assert view["empty"] is False


def test_a_sandboxed_sessions_commands_are_not_watched(tmp_path: Path, folder: Path) -> None:
    record = SideEffects(tmp_path / "rec")
    ctx = ToolContext(workdir=folder, effects=record)
    run(ctx, "bash -c 'echo hi'")
    assert not (tmp_path / "rec").exists()


def test_a_command_that_outlives_its_timeout_or_runs_in_the_background_says_so(
    tmp_path: Path, home: Path, folder: Path
) -> None:
    ctx = full(folder, tmp_path)
    try:
        run(ctx, f"touch {home}/late.txt && sleep 30", timeout=1)
        run(ctx, f"touch {home}/bg.txt", background=True)
        assert ctx.effects is not None
        reasons = [r["reasons"][0] for r in ctx.effects.view()["not_tracked"]]
        assert len(reasons) == 2
        assert all("background" in reason for reason in reasons)
        assert {Path(f["path"]).name for f in ctx.effects.view()["files"]} >= {"late.txt"}
    finally:
        ctx.stop_processes()


def test_a_watch_that_fails_never_stops_the_command_and_is_listed_not_tracked(
    tmp_path: Path, home: Path, folder: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ctx = full(folder, tmp_path)
    assert ctx.effects is not None

    def broken(*_a: object, **_k: object) -> None:
        raise OSError

    monkeypatch.setattr(ctx.effects, "watch", broken)
    assert "exit 0" in run(ctx, f"touch {home}/x")
    (row,) = ctx.effects.view()["not_tracked"]
    assert "could not be watched" in row["reasons"][0]


def test_a_settle_that_fails_is_listed_not_tracked_too(
    tmp_path: Path, home: Path, folder: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ctx = full(folder, tmp_path)
    assert ctx.effects is not None

    def broken(*_a: object, **_k: object) -> None:
        raise OSError

    monkeypatch.setattr(ctx.effects, "settle", broken)
    assert "exit 0" in run(ctx, f"touch {home}/x")
    (row,) = ctx.effects.view()["not_tracked"]
    assert "could not be read" in row["reasons"][0]
