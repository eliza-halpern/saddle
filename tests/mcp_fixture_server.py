"""A real, minimal MCP server over stdio, for the client's tests.

Started as `python mcp_fixture_server.py [--describe TEXT] [--describe-file F]
[--log FILE]`. Tools: `echo` (returns its text; its description is `--describe`,
or the contents of `--describe-file` when that file exists, so a test can change
what a server says about itself without changing its command), `add` (a sum),
`secret` (never allowlisted by the tests), `crash` (the process dies mid-call),
`hang` (never answers), `fail` (the tool itself reports an error), `sees`
(whether a path exists for the server) and `env` (one environment variable).
Each call is appended to the `--log` file so a test can tell a call that reached
the server from one the client refused.
"""

from __future__ import annotations

import argparse
import os
import time
from pathlib import Path

from mcp.server.mcpserver import MCPServer

parser = argparse.ArgumentParser()
parser.add_argument("--describe", default="Return the text unchanged.")
parser.add_argument("--describe-file", default="")
parser.add_argument("--log", default="")
parser.add_argument("--pin", default="", help="the version a test pins; unused here")
args = parser.parse_args()
if args.describe_file and Path(args.describe_file).exists():
    args.describe = Path(args.describe_file).read_text(encoding="utf-8")

server = MCPServer("fixture")


def record(line: str) -> None:
    if args.log:
        with Path(args.log).open("a", encoding="utf-8") as handle:
            handle.write(line + "\n")


@server.tool(name="echo", description=args.describe)
def echo(text: str) -> str:
    record(f"echo {text}")
    return text


@server.tool(name="add", description="Add two integers.")
def add(a: int, b: int) -> int:
    record(f"add {a} {b}")
    return a + b


@server.tool(name="secret", description="Not on any allowlist.")
def secret() -> str:
    record("secret")
    return "the secret"


@server.tool(name="crash", description="Exit the server process mid-call.")
def crash() -> str:
    record("crash")
    print("fixture server: about to die", flush=True, file=__import__("sys").stderr)
    os._exit(3)


@server.tool(name="hang", description="Never answer.")
def hang() -> str:
    record("hang")
    time.sleep(600)
    return "late"


@server.tool(name="fail", description="Report a tool error.")
def fail() -> str:
    record("fail")
    msg = "the fixture tool failed on purpose"
    raise ValueError(msg)


@server.tool(name="sees", description="Whether a path exists for this server.")
def sees(path: str) -> str:
    return "yes" if Path(path).exists() else "no"


@server.tool(name="env", description="One environment variable of this server.")
def env(name: str) -> str:
    return os.environ.get(name, "(unset)")


if __name__ == "__main__":
    server.run("stdio")
