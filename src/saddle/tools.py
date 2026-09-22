"""Agent tools for the chat mode: files, search, and sandboxed commands.

Every path argument is resolved **inside the working directory** and refused
otherwise (`sandbox.resolve_within`). The workdir used to be a default `cwd`
and nothing more, so `read_file("../../.ssh/id_rsa")` worked; it is now a
boundary.

Commands run through `sandbox.Sandbox`, which uses `bwrap` when it is
available and says so when it is not. A command may run in the background so
a long build does not freeze the conversation: `run_command(background=true)`
returns a terminal id, `read_terminal` shows its output so far, and
`wait_for_terminal` blocks for a bounded time without killing it.

Every failure -- bad arguments, OS errors, refused paths, unknown tools --
becomes a result string beginning "error: " so the model can self-correct
rather than the turn dying.
"""

from __future__ import annotations

import difflib
import json
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Final

from saddle.edits import loose_spans
from saddle.sandbox import DEFAULT_TIMEOUT, OutsideRootError, Sandbox, resolve_within
from saddle.vllm import ToolCall

PREVIEWABLE: Final = frozenset({".png", ".jpg", ".jpeg", ".gif", ".webp", ".svg"})
"""Extensions the transcript will show rather than name. A model that draws
a diagram writes a file and tells you the filename; a filename is not a
picture, and checking it meant leaving the conversation."""


def preview_for(name: str, arguments: str, workdir: Path) -> str | None:
    """The workdir-relative path of an image this call just wrote, if any."""
    if name not in ("write_file", "edit_file"):
        return None
    try:
        args = json.loads(arguments) if arguments.strip() else {}
    except ValueError:
        return None
    raw = args.get("path") if isinstance(args, dict) else None
    if not isinstance(raw, str) or Path(raw).suffix.lower() not in PREVIEWABLE:
        return None
    try:
        target = resolve_within(workdir, raw)
    except OutsideRootError:
        return None
    return str(target.relative_to(workdir)) if target.is_file() else None


MAX_READ: Final = 200_000
MAX_MATCHES: Final = 60
MAX_DIFF: Final = 20_000


def _tool(
    name: str, description: str, properties: dict[str, Any], required: list[str]
) -> dict[str, Any]:
    return {
        "type": "function",
        "function": {
            "name": name,
            "description": description,
            "parameters": {
                "type": "object",
                "properties": properties,
                "required": required,
                "additionalProperties": False,
            },
        },
    }


TOOLS: Final[list[dict[str, Any]]] = [
    _tool(
        "read_file",
        "Read a UTF-8 text file under the working directory.",
        {"path": {"type": "string"}},
        ["path"],
    ),
    _tool(
        "write_file",
        "Write a UTF-8 text file under the working directory, creating parent directories.",
        {"path": {"type": "string"}, "content": {"type": "string"}},
        ["path", "content"],
    ),
    _tool(
        "edit_file",
        "Replace one exact snippet in a file under the working "
        "directory. `old` must appear exactly once; prefer this over "
        "write_file for existing files, which must otherwise be rewritten whole.",
        {"path": {"type": "string"}, "old": {"type": "string"}, "new": {"type": "string"}},
        ["path", "old", "new"],
    ),
    _tool(
        "list_dir",
        "List entries of a directory under the working directory.",
        {"path": {"type": "string"}},
        [],
    ),
    _tool(
        "search",
        "Search file contents under the working directory for a "
        "substring; returns path:line: text for each match.",
        {"query": {"type": "string"}, "glob": {"type": "string"}},
        ["query"],
    ),
    _tool(
        "run_command",
        "Run a shell command in the sandboxed working directory. "
        "Set background=true for long jobs: it returns a terminal id immediately, "
        "and you can read_terminal or wait_for_terminal afterwards.",
        {
            "command": {"type": "string"},
            "background": {"type": "boolean"},
            "timeout": {"type": "integer"},
        },
        ["command"],
    ),
    _tool(
        "read_terminal",
        "Show the output so far of a background terminal.",
        {"id": {"type": "string"}},
        ["id"],
    ),
    _tool(
        "wait_for_terminal",
        "Wait up to `timeout` seconds for a background "
        "terminal to finish. A timeout does not kill it; you may wait again.",
        {"id": {"type": "string"}, "timeout": {"type": "integer"}},
        ["id"],
    ),
]


@dataclass
class ToolContext:
    """Per-session state the tools share: the sandbox and its terminals."""

    workdir: Path
    sandbox: Sandbox | None = None
    uploads: list[str] = field(default_factory=list)
    on_output: Callable[[str, str], None] | None = None
    """Called with (terminal_id, chunk) as a command produces output, so a
    UI can show a build scrolling rather than a spinner."""

    def box(self) -> Sandbox:
        if self.sandbox is None:
            self.sandbox = Sandbox.for_workdir(self.workdir, on_output=self.on_output)
        return self.sandbox


class _BadArgumentError(ValueError):
    """An argument of the wrong type. Reported, never coerced.

    `str(args["path"])` would turn `{"path": 7}` into the file "7" and go
    looking for it, so a type error becomes a confusing "no such file".
    The model learns more from being told the argument was wrong.
    """


def _text(args: Mapping[str, Any], key: str, tool: str) -> str:
    value = args.get(key)
    if not isinstance(value, str):
        msg = f"{tool} needs a string {key} argument"
        raise _BadArgumentError(msg)
    return value


def _read_file(ctx: ToolContext, args: Mapping[str, Any]) -> str:
    name = _text(args, "path", "read_file")
    path = resolve_within(ctx.workdir, name)
    if not path.is_file():
        return f"error: cannot read {name!r}"
    data = path.read_text(encoding="utf-8", errors="replace")
    if len(data) > MAX_READ:
        return data[:MAX_READ] + f"\n[... truncated at {MAX_READ} characters ...]"
    return data


def _write_file(ctx: ToolContext, args: Mapping[str, Any]) -> str:
    name = _text(args, "path", "write_file")
    path = resolve_within(ctx.workdir, name)
    content = _text(args, "content", "write_file")
    before = path.read_text(encoding="utf-8", errors="replace") if path.is_file() else ""
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
    except OSError:
        return f"error: cannot write {name!r}"
    if not before:
        return f"created {name!r} ({len(content)} bytes)"
    return _diff(before, content, name)


def _diff(before: str, after: str, name: str) -> str:
    """A unified diff of a change, for the caller and for the UI to render.

    "wrote 'x.py'" says nothing about what changed. A diff is what a reader
    needs to decide whether to keep it, and it is small even when the file
    is not.
    """
    lines = list(
        difflib.unified_diff(
            before.splitlines(keepends=True),
            after.splitlines(keepends=True),
            fromfile=f"a/{name}",
            tofile=f"b/{name}",
            n=2,
        )
    )
    if not lines:
        return f"{name} unchanged"
    body = "".join(lines)
    if len(body) > MAX_DIFF:
        body = body[:MAX_DIFF] + f"\n[... diff truncated at {MAX_DIFF} characters ...]"
    return body


def _edit_file(ctx: ToolContext, args: Mapping[str, Any]) -> str:
    """Replace one exact occurrence. Ambiguity is refused, never guessed at.

    Rewriting a whole file to change three lines is expensive and, on a long
    file, is the generation shape this model degenerates on (F21.17). An
    `old` that matches twice is refused rather than applied to the first
    hit: the caller meant one of them and the tool cannot know which.
    """
    name = _text(args, "path", "edit_file")
    old_text = _text(args, "old", "edit_file")
    new_text = _text(args, "new", "edit_file")
    path = resolve_within(ctx.workdir, name)
    if not path.is_file():
        return f"error: cannot read {name!r}"
    before = path.read_text(encoding="utf-8", errors="replace")
    found = before.count(old_text)
    if found > 1:
        return (
            f"error: that snippet appears {found} times in {name!r}; "
            "include more surrounding context so it matches exactly once"
        )
    if found == 0:
        # A live draw reproduced a correct snippet but dropped one blank line,
        # and an exact count refused it as absent -- which sends the caller
        # hunting a typo that is not there. Blank lines and trailing spaces are
        # not what names a site; the significant lines are.
        spans = loose_spans(before, old_text)
        if len(spans) > 1:
            return (
                f"error: that snippet matches {len(spans)} places in {name!r} once blank "
                "lines are ignored; include more surrounding context"
            )
        if not spans:
            return f"error: that snippet does not appear in {name!r}"
        start, end = spans[0]
        lines = before.split("\n")
        after = "\n".join(lines[:start] + new_text.splitlines() + lines[end:])
    else:
        after = before.replace(old_text, new_text, 1)
    try:
        path.write_text(after, encoding="utf-8")
    except OSError:
        return f"error: cannot write {name!r}"
    return _diff(before, after, name)


def _list_dir(ctx: ToolContext, args: Mapping[str, Any]) -> str:
    raw = args.get("path")
    if raw is not None and not isinstance(raw, str):
        msg = "list_dir needs a string path argument"
        raise _BadArgumentError(msg)
    path = resolve_within(ctx.workdir, raw or ".")
    if not path.is_dir():
        return f"error: not a directory: {args.get('path') or '.'}"
    rows = []
    for entry in sorted(path.iterdir(), key=lambda p: (not p.is_dir(), p.name)):
        mark = "/" if entry.is_dir() else ""
        size = "" if entry.is_dir() else f"  {entry.stat().st_size}"
        rows.append(f"{entry.name}{mark}{size}")
    return "\n".join(rows) or "(empty)"


def _search(ctx: ToolContext, args: Mapping[str, Any]) -> str:
    query = _text(args, "query", "search")
    pattern = str(args.get("glob") or "**/*")
    root = ctx.workdir.resolve()
    hits: list[str] = []
    for candidate in root.glob(pattern):
        if not candidate.is_file() or len(hits) >= MAX_MATCHES:
            continue
        try:
            text = candidate.read_text(encoding="utf-8", errors="strict")
        except (OSError, UnicodeDecodeError):
            continue
        for number, line in enumerate(text.splitlines(), 1):
            if query in line:
                hits.append(f"{candidate.relative_to(root)}:{number}: {line.strip()[:160]}")
                if len(hits) >= MAX_MATCHES:
                    break
    if not hits:
        return f"no matches for {query!r}"
    more = "\n[... more matches not shown ...]" if len(hits) >= MAX_MATCHES else ""
    return "\n".join(hits) + more


def _run_command(ctx: ToolContext, args: Mapping[str, Any]) -> str:
    command = _text(args, "command", "run_command")
    box = ctx.box()
    if bool(args.get("background")):
        terminal = box.start(command)
        return (
            f"started terminal {terminal.id} (isolation: {box.isolation}). "
            "Use read_terminal or wait_for_terminal."
        )
    timeout = int(args.get("timeout") or DEFAULT_TIMEOUT)
    terminal = box.run(command, timeout=timeout)
    if terminal.running:
        return (
            f"still running after {timeout}s as terminal {terminal.id}; "
            f"output so far:\n{terminal.output()}"
        )
    return f"exit {terminal.exit_code}\n{terminal.output()}"


def _read_terminal(ctx: ToolContext, args: Mapping[str, Any]) -> str:
    terminal = ctx.box().terminals.get(str(args["id"]))
    if terminal is None:
        return f"error: no terminal {args['id']!r}"
    state = "running" if terminal.running else f"exited {terminal.exit_code}"
    return f"[{state}]\n{terminal.output()}"


def _wait_for_terminal(ctx: ToolContext, args: Mapping[str, Any]) -> str:
    box = ctx.box()
    if str(args["id"]) not in box.terminals:
        return f"error: no terminal {args['id']!r}"
    timeout = int(args.get("timeout") or 60)
    terminal = box.wait(str(args["id"]), timeout=timeout)
    if terminal.running:
        return (
            f"terminal {terminal.id} still running after {timeout}s "
            f"(not killed; wait again if you want)\n{terminal.output()}"
        )
    return f"exit {terminal.exit_code}\n{terminal.output()}"


# Naming the handler signature is what makes `handler(ctx, args)` a str
# rather than Any at the dispatch site below.
type _Handler = Callable[[ToolContext, Mapping[str, Any]], str]

_HANDLERS: Final[dict[str, _Handler]] = {
    "read_file": _read_file,
    "write_file": _write_file,
    "edit_file": _edit_file,
    "list_dir": _list_dir,
    "search": _search,
    "run_command": _run_command,
    "read_terminal": _read_terminal,
    "wait_for_terminal": _wait_for_terminal,
}


def execute_tool(call: ToolCall, *, workdir: Path, context: ToolContext | None = None) -> str:
    """Run one tool call; every failure becomes an "error: ..." string."""
    ctx = context or ToolContext(workdir=workdir)
    handler = _HANDLERS.get(call.name)
    if handler is None:
        return f"error: unknown tool {call.name!r}"
    try:
        args = json.loads(call.arguments) if call.arguments.strip() else {}
        if not isinstance(args, dict):
            return "error: arguments must be a JSON object"
    except ValueError as exc:
        return f"error: arguments are not valid JSON: {exc}"
    try:
        return handler(ctx, args)
    except (OutsideRootError, _BadArgumentError) as exc:
        return f"error: {exc}"
    except KeyError as exc:
        return f"error: missing argument {exc}"
    except (OSError, ValueError) as exc:
        return f"error: {type(exc).__name__}: {exc}"
