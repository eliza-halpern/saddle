"""The one place saddle touches the MCP Python SDK (`pip install saddle-harness[mcp]`).

Everything else in saddle imports `mcpclient`, which holds the allowlist,
approvals and process tracking and needs no SDK; this module is imported only
when a server is about to be started (`mcpclient.load_sdk`), so a saddle without
the extra installed imports, runs and passes its tests, and reports MCP as
unavailable with the reason instead of failing.
"""

from __future__ import annotations

import json
from contextlib import AbstractAsyncContextManager
from typing import Any, TextIO

import mcp_types as types
from mcp import Client
from mcp.client._transport import TransportStreams
from mcp.client.stdio import StdioServerParameters, stdio_client


class _Transport:
    """The SDK's stdio transport with the server's stderr kept, so a crash can
    say what the server last wrote."""

    def __init__(self, params: StdioServerParameters, errlog: TextIO) -> None:
        self._inner: AbstractAsyncContextManager[TransportStreams] = stdio_client(params, errlog)

    async def __aenter__(self) -> TransportStreams:
        return await self._inner.__aenter__()

    async def __aexit__(self, *exc: Any) -> bool | None:
        return await self._inner.__aexit__(*exc)


def client(argv: list[str], env: dict[str, str], cwd: str, errlog: TextIO) -> Client:
    """A client for the server `argv` starts (not yet entered): the initialize
    handshake, not the SDK's probing mode."""
    params = StdioServerParameters(command=argv[0], args=argv[1:], env=env, cwd=cwd)
    return Client(_Transport(params, errlog), mode="legacy")


async def list_all(session: Client) -> list[tuple[str, str, dict[str, Any]]]:
    """Every tool the server lists as (name, description, input schema), following
    its pages: a server that pages its list would otherwise look as though it
    offered only the first page."""
    listed: list[tuple[str, str, dict[str, Any]]] = []
    cursor: str | None = None
    while True:
        page = await session.list_tools(cursor=cursor)
        listed += [(t.name, t.description or "", dict(t.input_schema)) for t in page.tools]
        cursor = page.next_cursor
        if cursor is None:
            return listed


def render_result(name: str, result: types.CallToolResult) -> str:
    """A tool result as the text the model reads. Text blocks as they are;
    anything else is named and left out; a server's own error is an
    "error: " result."""
    parts: list[str] = []
    for block in result.content:
        if isinstance(block, types.TextContent):
            parts.append(block.text)
        else:
            parts.append(f"[{block.type} content from the server was not shown]")
    if not parts and result.structured_content is not None:
        parts.append(json.dumps(result.structured_content))
    text = "\n".join(parts) or "(the server returned no content)"
    if result.is_error:
        return f"error: MCP tool {name!r} reported an error: {text}"
    return text
