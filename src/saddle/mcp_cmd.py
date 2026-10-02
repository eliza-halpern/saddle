"""`saddle mcp`: show the MCP allowlist and approve a server's tools (#139).

`saddle mcp list` reads the allowlist and says, per server, whether it is
approved. `saddle mcp approve NAME` starts the server in a sandbox with no
access to anything but an empty folder, shows the person every exposed tool
description the server gave, verbatim, and records the approval only on a
typed `y` or `yes`. There is deliberately no `--yes`: the point of the step is
that a person read the descriptions.
"""

from __future__ import annotations

import tempfile
from pathlib import Path
from typing import IO

from saddle.mcpclient import (
    Approvals,
    McpConfigError,
    McpError,
    McpHost,
    config_path,
    load_config,
    render_review,
)
from saddle.sandbox import Sandbox


def run_mcp(
    action: str,
    name: str | None,
    *,
    stdin: IO[str],
    stdout: IO[str],
    stderr: IO[str],
    root: Path | None = None,
) -> int:
    """`root` is the folder the reviewed server can see; none gives it an empty one."""
    try:
        config = load_config()
    except McpConfigError as exc:
        print(f"error: {exc}", file=stderr)
        return 1
    approvals = Approvals()
    if action == "list":
        if not config:
            print(f"no MCP servers are allowlisted (the allowlist is {config_path()})", file=stdout)
        for spec in config.values():
            print(
                f"{spec.name}: {spec.access}, tools {', '.join(spec.tools)}; "
                "approval is checked when the server starts",
                file=stdout,
            )
        return 0
    if name is None or name not in config:
        print(f"error: {name!r} is not in the MCP allowlist ({config_path()})", file=stderr)
        return 1
    with tempfile.TemporaryDirectory(prefix="saddle-mcp-") as empty:
        box = Sandbox.for_workdir(root or Path(empty))
        host = McpHost(config, approvals, lambda: box)
        try:
            spec, tools, approved = host.review(name)
        except McpError as exc:
            print(f"error: {exc}", file=stderr)
            return 1
        finally:
            host.close()
    print(render_review(spec, tools), file=stdout)
    if approved:
        print("\nalready approved exactly as shown.", file=stdout)
        return 0
    print("\napprove these tool descriptions? [y/N] ", end="", file=stdout, flush=True)
    if stdin.readline().strip().lower() not in ("y", "yes"):
        print("not approved; nothing was recorded.", file=stdout)
        return 1
    approvals.approve(spec, tools)
    print(f"approved {name}.", file=stdout)
    return 0
