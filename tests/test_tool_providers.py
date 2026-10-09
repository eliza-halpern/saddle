"""A package installed in the harness's environment can add a tool to a run.

Contract: a ToolProvider registered under the "saddle.tools" entry-point group
adds its tool -- schema, handler and prompt line -- to a run, discovered from
installed distributions (never a run's worktree). execute_tool runs its handler
and validates its arguments like any built-in. A provider whose name shadows a
built-in, and an entry that is not a ToolProvider, are ignored. With none
installed nothing changes: the tool list, prompt and records are as before.

Known-good: a fake provider is called and its args are checked. Known-bad: a
name-shadowing provider and a non-provider entry are dropped; the un-patched
environment installs no providers, so the defaults hold.
"""

from __future__ import annotations

import importlib.metadata
from collections.abc import Iterator, Mapping
from pathlib import Path
from typing import Any

import pytest

from saddle.tools import (
    ToolContext,
    ToolProvider,
    execute_tool,
    provider_prompt,
    provider_schemas,
    tool_providers,
)
from saddle.vllm import ToolCall


class _FakeEntry:
    """An importlib.metadata entry point whose load() returns a fixed object."""

    def __init__(self, obj: object) -> None:
        self._obj = obj

    def load(self) -> object:
        return self._obj


def _schema(name: str) -> dict[str, Any]:
    return {
        "type": "function",
        "function": {
            "name": name,
            "description": "search a set of documents",
            "parameters": {
                "type": "object",
                "properties": {"query": {"type": "string"}},
                "required": ["query"],
            },
        },
    }


def _provider(name: str = "doc_search") -> ToolProvider:
    def handler(ctx: ToolContext, args: Mapping[str, Any]) -> str:
        return f"library says: {args['query']}"

    return ToolProvider(
        name=name,
        schema=_schema(name),
        handler=handler,
        prompt_line=f"A {name} tool is available; cite it before changing a gate.",
    )


def _install(monkeypatch: pytest.MonkeyPatch, *objs: object) -> None:
    def entry_points(*, group: str) -> list[_FakeEntry]:
        return [_FakeEntry(o) for o in objs] if group == "saddle.tools" else []

    monkeypatch.setattr(importlib.metadata, "entry_points", entry_points)
    tool_providers.cache_clear()


@pytest.fixture(autouse=True)
def _clear_cache() -> Iterator[None]:
    tool_providers.cache_clear()  # start from the real (empty) environment
    yield
    tool_providers.cache_clear()  # never leak a fake into another test


def test_no_providers_are_installed_by_default() -> None:
    assert tool_providers() == ()
    assert provider_schemas() == []
    assert provider_prompt() == ""


def test_a_provider_adds_a_tool_a_run_can_call(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _install(monkeypatch, _provider())
    assert [p.name for p in tool_providers()] == ["doc_search"]
    assert provider_schemas()[0]["function"]["name"] == "doc_search"
    assert "doc_search" in provider_prompt()
    out = execute_tool(
        ToolCall(id="1", name="doc_search", arguments='{"query": "diff grammar"}'),
        workdir=tmp_path,
    )
    assert out == "library says: diff grammar"


def test_a_provider_tools_arguments_are_validated(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _install(monkeypatch, _provider())
    out = execute_tool(
        ToolCall(id="2", name="doc_search", arguments='{"nope": 1}'),
        workdir=tmp_path,
    )
    assert "does not take nope" in out


def test_a_provider_cannot_shadow_a_builtin_tool(monkeypatch: pytest.MonkeyPatch) -> None:
    _install(monkeypatch, _provider("read_file"))
    assert tool_providers() == ()  # read_file is a built-in, so the provider is dropped


def test_an_entry_that_is_not_a_provider_is_ignored(monkeypatch: pytest.MonkeyPatch) -> None:
    _install(monkeypatch, "not a provider at all")
    assert tool_providers() == ()
