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

from saddle.edits import first_divergence, loose_spans
from saddle.sandbox import (
    DEFAULT_TIMEOUT,
    OutsideRootError,
    Sandbox,
    project_command_env,
    project_env,
    resolve_within,
)
from saddle.undo import UndoLog
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


READ_LINES: Final = 400
READ_TOKENS: Final = 12_000
"""At most this many tokens (the model's own count) in one `read_file`
window; a window over it is cut to fewer lines, and one line over it alone
is refused with where to look instead."""
"""How many lines one `read_file` returns unless asked for a window. A whole
module in one read (a dogfood run read a 2,600-line file at once, about 30k
tokens) filled half the context by minute five and forced compaction, and
the model re-read what compaction had shortened."""
MAX_MATCHES: Final = 60
MAX_DIFF: Final = 20_000

MAX_BODY: Final = 20_000
"""How much of a newly written file is shown back. Same order as a diff: a
transcript that quietly swallows a 200KB generated file is no better than
one that prints all of it."""


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
        "Read a UTF-8 text file under the working directory. A file longer than "
        f"{READ_LINES} lines comes back {READ_LINES} lines at a time, headed by "
        "which lines they are; pass offset (the first line, from 1) and limit (how "
        "many lines) to read another part.",
        {
            "path": {"type": "string"},
            "offset": {"type": "integer", "minimum": 1},
            "limit": {"type": "integer", "minimum": 1},
        },
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

_ARGUMENTS: Final[dict[str, frozenset[str]]] = {
    tool["function"]["name"]: frozenset(tool["function"]["parameters"]["properties"])
    for tool in TOOLS
}
"""Each tool's declared arguments. Anything else is refused by name, not
dropped: `read_file` once ignored an `offset` and `limit` its schema did not
declare, and returned whole files to a model that had asked for 120 lines."""

READ_ONLY_TOOLS: Final = ("read_file", "list_dir", "search")
"""What the Ask lane may call: nothing that writes a file or runs a command.

`run_command` is out entirely rather than filtered: a shell can write
anywhere the sandbox can, and no read-only subset of it is checkable here.
The terminal tools go with it, since only `run_command` starts a terminal."""

ASK_TOOLS: Final[list[dict[str, Any]]] = [
    tool for tool in TOOLS if tool["function"]["name"] in READ_ONLY_TOOLS
]
"""The schemas an Ask turn offers the model."""


def tools_for_mode(mode: str) -> list[dict[str, Any]]:
    """The tool schemas a chat turn in `mode` offers: read-only unless Edit."""
    return list(TOOLS) if mode == "edit" else list(ASK_TOOLS)


def scope_turn(context: ToolContext, mode: str) -> list[dict[str, Any]]:
    """Scope one chat turn to its lane: the schemas to offer, and the refusal.

    The one lane policy for a chat turn, shared by the web chat
    (`ChatServer._run`) and the terminal chat (`saddle up`, `chat._run_turn`)
    so the two cannot drift: both offer `tools_for_mode(mode)` and both set
    `context.allowed` to exactly those names, so a call to any other tool is
    refused before it runs. Set on every turn, because a context outlives a
    lane change."""
    tools = tools_for_mode(mode)
    context.allowed = tuple(t["function"]["name"] for t in tools)
    return tools


FINISH_TOOL: Final = "finish"
"""The only way an autonomous run ends "finished" (engine.AutoRun).

The engine handles it, not `_HANDLERS`: it ends the run rather than
touching the tree, and it is offered only when a run is autonomous. What
the model writes in it is sealed labelled as narrative, never evidence --
the page's "its narrative can't assert a result"."""

FINISH_SCHEMA: Final[dict[str, Any]] = _tool(
    FINISH_TOOL,
    "Finish the task. Call this once, when you are done, with a short account "
    "of what you changed and why. The account is kept as your narrative; it is "
    "not treated as proof that anything works.",
    {"summary": {"type": "string"}},
    ["summary"],
)

CHECK_TOOL: Final = "check"
"""The model's pull on the auditor (`saddle auto --check-tool`, `feed.AuditFeed.check`).

Like `finish`, the engine handles it, and it is offered only when the run
was started with the flag; without it the name is not in the tool list."""

CHECK_SCHEMA: Final[dict[str, Any]] = _tool(
    CHECK_TOOL,
    "Run the audit's fast checks (syntax, ruff and imports on each changed Python "
    "file, then the tests, changed-line coverage, dead code, public deletions and "
    "assertion preservation) on the tree as it is now, and return the findings "
    "exactly as a refused finish would show them. finish runs these same checks "
    "plus mutation testing, so a passing check does not guarantee finish passes. "
    "A check on a tree unchanged since the last check is refused.",
    {},
    [],
)

DISPUTE_TOOL: Final = "dispute"
"""The model's ripcord (`engine._dispute`): the task's premise is false.

Like `finish`, the engine handles it, and it is offered only when a run is
autonomous. It never passes anything: the run ends "needs you" with the
claim, the finding and the evidence commands' output, rerun by saddle."""

DISPUTE_SCHEMA: Final[dict[str, Any]] = _tool(
    DISPUTE_TOOL,
    "End the run because the task's premise is false: the bug it describes cannot "
    "happen, or is already fixed. Use it as soon as your probe or your reading shows "
    "the task's claim cannot hold on the current code, even if you could build "
    "something that satisfies the task's wording. Give the claim the task makes, what "
    "you found, and "
    "one to five shell commands whose output shows it (a script you wrote in /tmp is "
    "fine). Saddle reruns each command and seals its output; a person reviews it. "
    "This is not a pass and not a finish. A dispute with no command, or with a command "
    "that cannot run, is refused.",
    {
        "claim": {"type": "string"},
        "finding": {"type": "string"},
        "evidence": {"type": "array", "items": {"type": "string"}},
    },
    ["claim", "finding", "evidence"],
)

PREMISE_TOOL: Final = "premise_check"
"""`saddle auto --premise-check`: before its first edit the model shows the
problem the task describes (`engine._premise`). Saddle reruns the commands and
hands their output back with one question: does this show the problem?"""

PREMISE_SCHEMA: Final[dict[str, Any]] = _tool(
    PREMISE_TOOL,
    "Before your first edit: show that the problem the task describes exists on the "
    "current code. Give the claim you are checking and one to five shell commands "
    "(a failing test, or a script you wrote in /tmp) whose output shows it. Saddle "
    "reruns them and shows you their output. Edits are refused until you call this.",
    {
        "claim": {"type": "string"},
        "commands": {"type": "array", "items": {"type": "string"}},
    },
    ["claim", "commands"],
)

INSTALL_TOOL: Final = "install"
"""The model's request for a package (`saddle auto --allow-installs`, `installs`).

Like `finish`, the engine handles it, and it is offered only when the run
was started with installs allowed; without that the name is not in the
tool list. The user approves or refuses every call."""

INSTALL_SCHEMA: Final[dict[str, Any]] = _tool(
    INSTALL_TOOL,
    "Ask the user to install Python packages this task needs into this run's own "
    "environment, from the user's local wheel folder (there is no network). The user "
    "approves or refuses each request. Give plain requirements, a name and an optional "
    "version such as pkg==1.2, and say why the task needs them. A package with no wheel "
    "in the folder is refused as missing.",
    {
        "packages": {"type": "array", "items": {"type": "string"}},
        "reason": {"type": "string"},
    },
    ["packages", "reason"],
)

REFUSED: Final = "error: refused by the tier-0 guard: "
"""Prefix of a result the tier-0 guard produced. Still an "error: " result,
so the model reads it the way it reads every other failure; the engine
tells a refusal apart by this prefix and records it as one."""

TEST_FILE_NAMES: Final = ("conftest.py",)
"""Files that are tests wherever they sit, besides `test_*.py`/`*_test.py`."""


def is_test_path(relative: str, test_roots: tuple[str, ...]) -> bool:
    """Whether a workdir-relative POSIX path is a test file for the guard.

    Under a configured test root (`tests/` by default, or the repo's pytest
    `testpaths`), or named like a pytest test module anywhere.
    """
    for raw in test_roots:
        root = raw.strip("/")
        if root and (relative == root or relative.startswith(root + "/")):
            return True
    base = relative.rsplit("/", 1)[-1]
    return (
        base in TEST_FILE_NAMES
        or (base.startswith("test_") and base.endswith(".py"))
        or base.endswith("_test.py")
    )


def _parses(text: str, name: str) -> SyntaxError | None:
    try:
        compile(text, name, "exec", dont_inherit=True)
    except SyntaxError as exc:  # a NUL byte is one too, since Python 3.12
        return exc
    return None


@dataclass
class ToolContext:
    """Per-session state the tools share: the sandbox and its terminals."""

    workdir: Path
    sandbox: Sandbox | None = None
    uploads: list[str] = field(default_factory=list)
    on_output: Callable[[str, str], None] | None = None
    """Called with (terminal_id, chunk) as a command produces output, so a
    UI can show a build scrolling rather than a spinner."""
    undo: UndoLog | None = None
    """Snapshots a file before this turn first changes it, so retrying or
    editing a message can put the workdir back. Only the file tools are
    recorded -- `run_command` can do anything and is not reversible."""

    def snapshot(self, path: Path) -> None:
        if self.undo is not None:
            self.undo.before_write(path)

    call_id: str | None = None
    time_left: Callable[[], float] | None = None
    """Seconds left in an autonomous run's time budget; None in chat. A
    command's timeout and a wait are capped at it (`_bounded`): the budget is
    checked between model rounds, so one long command could otherwise run far
    past it (a `timeout 3500` suite started near the end of a 60-min run did)."""
    count_tokens: Callable[[str], int | None] | None = None
    """The model's own tokenizer, asked through the server, for a tool that
    must keep its result inside the context (`read_file`). None where no
    server is at hand: the tool then limits by lines alone."""
    """Which tool call is running, so a kept version can be found again from
    a stored transcript."""

    def keep_version(self, path: Path) -> None:
        """Record the file as this call left it, for the transcript to show."""
        if self.undo is not None:
            self.undo.after_write(path, self.call_id)

    allowed: tuple[str, ...] | None = None
    """When set, a call to any tool not named here is refused before it runs
    (the Ask lane). Offering fewer schemas is not enough on its own: a model
    can still emit a call to a tool it was not offered. None allows all."""

    protected_tests: tuple[str, ...] | None = None
    """Tier-0 guard (the page's "test files read-only during
    implementation"): when set, `write_file` and `edit_file` refuse any path
    `is_test_path` names under these roots. None, the chat default, guards
    nothing."""
    syntax_guard: bool = False
    """Tier-0 guard: refuse a write that leaves a `.py` file unparseable,
    unless the file was already unparseable before it."""

    def guard(self, path: Path, name: str, before: str | None, after: str) -> str | None:
        """A refusal result for this write, or None to let it through."""
        if self.protected_tests is not None:
            relative = path.relative_to(self.workdir.resolve()).as_posix()
            if is_test_path(relative, self.protected_tests):
                return (
                    f"{REFUSED}{name!r} is a test file, and tests are read-only in "
                    "this run; change the implementation instead. The file was not changed."
                )
        if self.syntax_guard and path.suffix == ".py":
            broken = _parses(after, name)
            if broken is not None and (before is None or _parses(before, name) is None):
                return (
                    f"{REFUSED}that change leaves {name!r} with a SyntaxError: "
                    f"{broken.msg} (line {broken.lineno}). The file was not changed."
                )
        return None

    def box(self) -> Sandbox:
        """The chat lanes' sandbox on the user's folder, with the folder's own
        virtualenv first on PATH when it has one (`sandbox.project_env`)."""
        if self.sandbox is None:
            self.sandbox = Sandbox.for_workdir(
                self.workdir,
                on_output=self.on_output,
                env=project_command_env(project_env(self.workdir)),
            )
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


def _line_number(args: Mapping[str, Any], key: str) -> int | None:
    """An optional whole-number argument of at least 1; a numeric string counts
    (models send "280" as often as 280)."""
    value = args.get(key)
    if value is None:
        return None
    if isinstance(value, str) and value.strip().isdigit():
        value = int(value)
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        msg = f"read_file {key} must be a whole number of at least 1"
        raise _BadArgumentError(msg)
    return value


def _read_file(ctx: ToolContext, args: Mapping[str, Any]) -> str:
    """The file, or a window of it by lines. A file of at most `READ_LINES`
    lines and `READ_TOKENS` tokens read without `offset`/`limit` comes back
    whole and unchanged; any other read is headed by the lines it holds and
    how to read on."""
    name = _text(args, "path", "read_file")
    offset = _line_number(args, "offset")
    limit = _line_number(args, "limit")
    path = resolve_within(ctx.workdir, name)
    if not path.is_file():
        return f"error: cannot read {name!r}"
    data = path.read_text(encoding="utf-8", errors="replace")
    lines = data.splitlines(keepends=True)
    total = len(lines)
    if offset is None and limit is None and total <= READ_LINES:
        whole = ctx.count_tokens(data) if ctx.count_tokens is not None else None
        if whole is None or whole <= READ_TOKENS:
            return data
    start = offset or 1
    if start > max(total, 1):
        return f"error: offset {start} is past the end of {name} ({total} lines)"
    end = min(start - 1 + (limit or READ_LINES), total)
    body = "".join(lines[start - 1 : end])
    count = ctx.count_tokens
    tokens = count(body) if count is not None else None
    if count is not None and tokens is not None and tokens > READ_TOKENS:
        first = count(lines[start - 1])
        if first is not None and first > READ_TOKENS:
            return (
                f"error: line {start} of {name} alone is {first} tokens, over the "
                f"{READ_TOKENS}-token limit for one read; look into it with search "
                "or run_command instead"
            )
        # The largest window from `start` that fits, by halving: a few counts,
        # and never a window over the limit (a proportional cut undershoots
        # when every count carries a fixed overhead).
        fits, over = start, end
        while over - fits > 1:
            mid = (fits + over) // 2
            size = count("".join(lines[start - 1 : mid]))
            if size is None or size <= READ_TOKENS:
                fits = mid
            else:
                over = mid
        end = fits
        body = "".join(lines[start - 1 : end])
    head = f"[{name}: lines {start}-{end} of {total}]\n"
    more = (
        f"\n[{total - end} more lines: read_file with offset={end + 1} to read on]"
        if end < total
        else ""
    )
    return head + body + more


def _write_file(ctx: ToolContext, args: Mapping[str, Any]) -> str:
    name = _text(args, "path", "write_file")
    path = resolve_within(ctx.workdir, name)
    content = _text(args, "content", "write_file")
    before = path.read_text(encoding="utf-8", errors="replace") if path.is_file() else ""
    refused = ctx.guard(path, name, before if path.is_file() else None, content)
    if refused is not None:
        return refused
    ctx.snapshot(path)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
    except OSError:
        return f"error: cannot write {name!r}"
    ctx.keep_version(path)
    if not before:
        # The file itself, not just how big it is. A byte count says nothing
        # about what was written, and a new file is exactly when there is no
        # diff to read instead. Capped like a diff is, and carried in the
        # result rather than beside it so a reloaded transcript shows the
        # same thing the live one did.
        body = content
        if len(body) > MAX_BODY:
            body = body[:MAX_BODY] + f"\n[... truncated at {MAX_BODY} characters ...]"
        return f"created {name!r} ({len(content)} bytes)\n{body}"
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
    file, is the generation shape this model degenerates on. An
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
            # Say which line of the snippet the file lacks (`edits.first_divergence`,
            # the same words the worker's refusal uses): "does not appear" alone
            # gives a person or model nothing to fix on a long block.
            where = first_divergence(before, old_text)
            return f"error: that snippet does not appear in {name!r}.{where}"
        start, end = spans[0]
        lines = before.split("\n")
        after = "\n".join(lines[:start] + new_text.splitlines() + lines[end:])
    else:
        after = before.replace(old_text, new_text, 1)
    refused = ctx.guard(path, name, before, after)
    if refused is not None:
        return refused
    ctx.snapshot(path)
    try:
        path.write_text(after, encoding="utf-8")
    except OSError:
        return f"error: cannot write {name!r}"
    ctx.keep_version(path)
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
    if Path(pattern).is_absolute() or ".." in Path(pattern).parts:
        return f"error: glob {pattern!r} must stay inside the working directory"
    root = ctx.workdir.resolve()
    hits: list[str] = []
    for candidate in root.glob(pattern):
        if not candidate.is_file() or len(hits) >= MAX_MATCHES:
            continue
        try:  # a symlink out of the tree is refused here as read_file refuses it
            resolve_within(root, candidate)
        except OutsideRootError:
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


def _bounded(ctx: ToolContext, asked: int) -> tuple[int, str]:
    """`asked` seconds, capped at the run's time left, and a note when capped."""
    if ctx.time_left is None:
        return asked, ""
    left = max(1, int(ctx.time_left()))
    if left >= asked:
        return asked, ""
    return left, f" (capped at the {left}s left in this run's time budget)"


def _run_command(ctx: ToolContext, args: Mapping[str, Any]) -> str:
    command = _text(args, "command", "run_command")
    box = ctx.box()
    if bool(args.get("background")):
        terminal = box.start(command)
        return (
            f"started terminal {terminal.id} (isolation: {box.isolation}). "
            "Use read_terminal or wait_for_terminal."
        )
    timeout, capped = _bounded(ctx, int(args.get("timeout") or DEFAULT_TIMEOUT))
    terminal = box.run(command, timeout=timeout)
    if terminal.running:
        return (
            f"still running after {timeout}s{capped} as terminal {terminal.id}; "
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
    timeout, capped = _bounded(ctx, int(args.get("timeout") or 60))
    terminal = box.wait(str(args["id"]), timeout=timeout)
    if terminal.running:
        return (
            f"terminal {terminal.id} still running after {timeout}s{capped} "
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
    if handler is not None and ctx.allowed is not None and call.name not in ctx.allowed:
        return (
            f"{REFUSED}{call.name!r} is not available in the Ask lane, which is "
            "read-only (read_file, list_dir, search). Nothing was run and the folder was "
            "not changed. Answer from what you can read, or tell the user to switch "
            "the lane to Edit or Task if the job needs changes."
        )
    if handler is None:
        return f"error: unknown tool {call.name!r}"
    try:
        args = json.loads(call.arguments) if call.arguments.strip() else {}
        if not isinstance(args, dict):
            return "error: arguments must be a JSON object"
    except ValueError as exc:
        return f"error: arguments are not valid JSON: {exc}"
    allowed = _ARGUMENTS.get(call.name)
    extra = sorted(set(args) - allowed) if allowed is not None else []
    if extra:
        return (
            f"error: {call.name} does not take {', '.join(extra)}; "
            f"its arguments are {', '.join(sorted(allowed or ()))}"
        )
    ctx.call_id = call.id
    try:
        return handler(ctx, args)
    except (OutsideRootError, _BadArgumentError) as exc:
        return f"error: {exc}"
    except KeyError as exc:
        return f"error: missing argument {exc}"
    except (OSError, ValueError) as exc:
        return f"error: {type(exc).__name__}: {exc}"
