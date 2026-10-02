"""Shared helpers for the MCP and research tests."""

from __future__ import annotations

import json
import shutil
import sys
from pathlib import Path
from typing import Any

from saddle.sandbox import isolation_problem

FIXTURE = Path(__file__).with_name("mcp_fixture_server.py")
BWRAP = isolation_problem() is None


def server_entry(
    workdir: Path,
    *,
    tools: list[str],
    access: str = "acting",
    extra: list[str] | None = None,
) -> dict[str, Any]:
    """An allowlist entry for the fixture server, which lives in `workdir` so a
    sandboxed session can run it."""
    script = workdir / "srv.py"
    if not script.exists():
        shutil.copy(FIXTURE, script)
    return {
        "command": [
            sys.executable,
            str(script),
            "--log",
            str(workdir / "calls.log"),
            "--pin",
            "1.0.0",
            *(extra or []),
        ],
        "version": "1.0.0",
        "access": access,
        "tools": tools,
    }


def write_config(path: Path, servers: dict[str, Any]) -> Path:
    path.write_text(json.dumps({"servers": servers}), encoding="utf-8")
    return path


def mcp_names(tools: list[dict[str, Any]]) -> list[str]:
    return [t["function"]["name"] for t in tools if t["function"]["name"].startswith("mcp__")]


def calls(workdir: Path) -> list[str]:
    log = workdir / "calls.log"
    return log.read_text(encoding="utf-8").splitlines() if log.exists() else []


def one_server(**changes: Any) -> dict[str, Any]:
    entry: dict[str, Any] = {
        "command": ["uvx", "some-server==1.2.3"],
        "version": "1.2.3",
        "access": "acting",
        "tools": ["read"],
    }
    entry.update(changes)
    return {"srv": entry}
