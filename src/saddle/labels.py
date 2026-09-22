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
}

MAX_OBJECT: Final = 72


def _object_of(name: str, args: dict[str, Any]) -> str:
    """The noun phrase a label points at: a path, a command, a query."""
    if name == "list_dir" and not args.get("path"):
        return "this folder"  # "Listed" alone reads as a sentence fragment
    if name in ("read_terminal", "wait_for_terminal") and args.get("id"):
        return f"terminal {args['id']}"
    for key in ("path", "file", "dir", "directory"):
        if isinstance(args.get(key), str):
            return args[key]
    if isinstance(args.get("command"), str):
        try:
            command = " ".join(shlex.split(args["command"])[:6])
        except ValueError:
            # An apostrophe is enough: shlex.split("echo don't") raises. A
            # label is decoration, so it falls back to the raw command rather
            # than taking the turn down with it.
            return args["command"][:80]
        return command or args["command"]
    if isinstance(args.get("query"), str):
        return f"{args['query']!r}"
    return ""


def _clip(text: str) -> str:
    return text if len(text) <= MAX_OBJECT else text[: MAX_OBJECT - 1] + "…"


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
