"""Human labels for a tool call, in three tenses.

The UI shows a tool call as a clickable row whose text changes with its
state: present tense while it runs, past tense when it succeeds, and
"Failed to ..." when it does not. One function owns all three so they cannot
drift into describing different things, and so a tool added without a label
degrades to something readable rather than to a raw JSON blob.

    Reading pyproject.toml        ->  Read pyproject.toml
                                  ->  Failed to read pyproject.toml
"""

from __future__ import annotations

import json
import shlex
from typing import Any, Final

# (present participle, past participle) per tool. The subject is implied.
_TENSES: Final[dict[str, tuple[str, str]]] = {
    "read_file": ("Reading", "Read"),
    "write_file": ("Writing", "Wrote"),
    "edit_file": ("Editing", "Edited"),
    "run_command": ("Running", "Ran"),
    "list_dir": ("Listing", "Listed"),
    "search": ("Searching for", "Searched for"),
    "view_image": ("Viewing", "Viewed"),
    "read_terminal": ("Reading", "Read"),
    "wait_for_terminal": ("Waiting for", "Waited for"),
    "processes": ("Checking", "Checked"),
}

MAX_OBJECT: Final = 72


def _object_of(name: str, args: dict[str, Any]) -> str:
    """The noun phrase a label points at: a path, a command, a query."""
    if name == "list_dir" and not args.get("path"):
        return "this folder"  # "Listed" alone reads as a sentence fragment
    if name == "processes":
        return "session processes"
    if name in ("read_terminal", "wait_for_terminal") and args.get("id"):
        return f"terminal {args['id']}"
    for key in ("path", "file", "dir", "directory"):
        value = args.get(key)
        if isinstance(value, str):
            return value
    command_arg = args.get("command")
    if isinstance(command_arg, str):
        try:
            command = " ".join(shlex.split(command_arg)[:6])
        except ValueError:
            # An apostrophe is enough: shlex.split("echo don't") raises. A
            # label is decoration, so it falls back to the raw command rather
            # than taking the turn down with it.
            return command_arg[:80]
        return command or command_arg
    query = args.get("query")
    if isinstance(query, str):
        return f"{query!r}"
    return ""


def _clip(text: str) -> str:
    return text if len(text) <= MAX_OBJECT else text[: MAX_OBJECT - 1] + "…"


_SCREEN_ACTS: Final[dict[str, tuple[str, str, str]]] = {
    "focus": ("Focusing", "Focused", "focus"),
    "click": ("Clicking", "Clicked", "click"),
    "key": ("Pressing", "Pressed", "press"),
    "type": ("Typing", "Typed", "type"),
    "scroll": ("Scrolling", "Scrolled", "scroll"),
    "drag": ("Dragging", "Dragged", "drag"),
}
"""(present, past, failed stem) per `computer` action: what it does on screen."""


def _screen_labels(name: str, args: dict[str, Any]) -> tuple[str, str, str]:
    """Labels for `screenshot` and `computer`, which act on the person's screen:
    the action and the window, never the typed text (it is in the details)."""
    window = args.get("window")
    target = f'"{_clip(window)}"' if isinstance(window, str) and window.strip() else ""
    if name == "screenshot":
        if isinstance(window, str) and window.strip().lower() == "list":
            return "Listing open windows", "Listed open windows", "Failed to list open windows"
        shot = target or "the screen"
        return (
            f"Taking a screenshot of {shot}",
            f"Took a screenshot of {shot}",
            f"Failed to take a screenshot of {shot}",
        )
    act = _SCREEN_ACTS.get(str(args.get("action")))
    if act is None or not target:
        return "Acting on the screen", "Acted on the screen", "Failed to act on the screen"
    kind = str(args.get("action"))
    if kind == "focus":
        what = target
    elif kind == "click":
        what = f"({args.get('x')}, {args.get('y')}) in {target}"
    elif kind == "key":
        what = f"{args.get('keys')} in {target}"
    elif kind == "type":
        text = args.get("text")
        what = f"{len(text) if isinstance(text, str) else 0} characters in {target}"
    elif kind == "drag":
        what = (
            f"from ({args.get('x')}, {args.get('y')}) to "
            f"({args.get('to_x')}, {args.get('to_y')}) in {target}"
        )
    else:
        what = f"{args.get('direction') or 'down'} {args.get('amount') or 3} in {target}"
    present, past, stem = act
    return f"{present} {what}", f"{past} {what}", f"Failed to {stem} {what}"


def describe(name: str, arguments: str) -> tuple[str, str, str]:
    """(present, past, failed) labels for one call.

    `arguments` is the raw JSON the model emitted, which may be malformed --
    that is a normal thing for a model to do and must not raise here. An
    unparseable call still gets a readable label naming the tool.
    """
    try:
        parsed = json.loads(arguments) if arguments.strip() else {}
        if not isinstance(parsed, dict):
            parsed = {}
    except ValueError:
        parsed = {}
    if name in ("screenshot", "computer"):
        return _screen_labels(name, parsed)
    present_verb, past_verb = _TENSES.get(name, (f"Calling {name}", f"Called {name}"))
    obj = _clip(_object_of(name, parsed))
    if not obj:
        # No recognised object: the verb alone still reads as English.
        return present_verb, past_verb, f"Failed: {past_verb.lower()} {name}"
    present = f"{present_verb} {obj}"
    past = f"{past_verb} {obj}"
    # "Reading" -> "read" but "Writing" -> "writ" under string surgery, so
    # the stems are a table rather than a rule.
    stems = {
        "Reading": "read",
        "Writing": "write",
        "Editing": "edit",
        "Running": "run",
        "Listing": "list",
        "Searching for": "search for",
        "Viewing": "view",
        "Waiting for": "wait for",
    }
    stem = stems.get(present_verb, present_verb.lower())
    failed = f"Failed to {stem} {obj}"
    return present, past, failed


def label_for(name: str, arguments: str, *, ok: bool | None) -> str:
    """The label for a call in its current state. `ok is None` means running."""
    present, past, failed = describe(name, arguments)
    if ok is None:
        return present
    return past if ok else failed
