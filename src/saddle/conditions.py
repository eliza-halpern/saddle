"""What a chat session runs with, said at start and loudly whenever it changes.

A run that began with an opt-in capability silently off (images, the check tool,
the reader's browser) went unnoticed until it was under way, more than once. So
the conditions a chat session runs under are stated, not left to be inferred:

- `startup_lines`: what `saddle chat` and `saddle up` print when they start, one
  line per capability (`on`, `off`, or `unavailable: <reason>`), the model, the
  reasoning effort and whether past reasoning is sent back. A capability that is
  off but would work says how to turn it on; one switched on that cannot work is
  an ERROR line. Neither stops the start.
- `snapshot` and `changes`: the conditions as one comparable value, and what
  differs between two of them. The web chat records a session's snapshots
  (`SessionStore.record_conditions`), shows the current one in a strip that never
  leaves the page, and draws a line in the transcript where they changed.

Reasoning is off exactly when the effort is `none`; keeping reasoning means each
round's reasoning goes back to the model on its assistant message
(`TurnOptions.keep_reasoning`).
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import replace
from typing import Any, Final

import httpx

from saddle import capabilities

Row = dict[str, Any]
"""One capability as the strip and the report read it: `name`, `state` (on, off,
unavailable), `reason`, and `available` (off rows only: True when switching it
on would work, False when it would not, None when that was not checked)."""

Probe = Callable[[], list[Row]]

REASONING_OFF: Final = "none"
"""The effort that turns reasoning off."""

ITEMS: Final = ("reasoning", "effort", "keep_reasoning")
"""The reasoning items of a snapshot, in the order the strip and a change name them.
The capabilities follow, in `capabilities.NAMES` order."""


def capability_rows(
    switches: capabilities.Switches | None = None,
    http: httpx.Client | None = None,
    reads: Callable[[], bool | None] | None = None,
    *,
    availability: bool = True,
) -> list[Row]:
    """Each capability's state, and for one that is off whether it would work:
    the same check `status` makes, asked as if it were switched on. Starts no
    server; asks what `status` asks. `availability=False` skips that second look
    and leaves `available` as None."""
    on = switches if switches is not None else capabilities.load()
    rows = capabilities.status(on, http, reads)
    off = [row.name for row in rows if row.state == "off"]
    trial: dict[str, capabilities.Status] = {}
    if availability and off:
        tried = capabilities.status(replace(on, **dict.fromkeys(off, True)), http, reads)
        trial = {row.name: row for row in tried}
    found: list[Row] = []
    for row in rows:
        shown: Row = {**row.as_json(), "available": None}
        if row.state == "off" and row.name in trial:
            would = trial[row.name]
            shown["available"] = would.state == "on"
            shown["reason"] = "" if would.state == "on" else would.reason
        found.append(shown)
    return found


def reasoning_on(effort: str) -> bool:
    return effort != REASONING_OFF


def snapshot(rows: Sequence[Mapping[str, Any]], effort: str, keep: bool) -> dict[str, Any]:
    """The conditions as one value two of which compare: the reasoning items and
    each capability's state. A reason or an availability is not a condition: it
    says why, and the strip shows it, but a change in it is no change of state."""
    return {
        "reasoning": "on" if reasoning_on(effort) else "off",
        "effort": effort,
        "keep_reasoning": "on" if keep else "off",
        "capabilities": {str(row["name"]): str(row["state"]) for row in rows},
    }


def changes(before: Mapping[str, Any], after: Mapping[str, Any]) -> list[dict[str, str]]:
    """Each item whose value differs, as {item, before, after}: the reasoning
    items first, then the capabilities in `capabilities.NAMES` order. A capability
    present on one side only reads as `absent` on the other."""
    found = [
        {"item": item, "before": str(before.get(item)), "after": str(after.get(item))}
        for item in ITEMS
        if before.get(item) != after.get(item)
    ]
    old: Mapping[str, Any] = before.get("capabilities") or {}
    new: Mapping[str, Any] = after.get("capabilities") or {}
    order = [*capabilities.NAMES, *sorted((set(old) | set(new)) - set(capabilities.NAMES))]
    for name in order:
        if (name in old or name in new) and old.get(name) != new.get(name):
            found.append(
                {
                    "item": name,
                    "before": str(old.get(name, "absent")),
                    "after": str(new.get(name, "absent")),
                }
            )
    return found


def change_text(found: Sequence[Mapping[str, str]]) -> str:
    """`effort medium → xhigh; images off → on`: what a transcript line says."""
    return "; ".join(f"{c['item']} {c['before']} → {c['after']}" for c in found)


def enable_hint(name: str) -> str:
    return (
        f"turn it on with `saddle capabilities enable {name}` "
        f"(or ${capabilities.OVERRIDE_ENV}={name} for one process)"
    )


def capability_line(row: Mapping[str, Any]) -> str:
    """One capability's line of the start report.

    on: `  images     on`, with the note `status` gives when there is one.
    off, and it would work: `! browser    off -- available: turn it on with ...`.
    off otherwise: `  mcp        off`.
    switched on and unavailable: `ERROR ocr  unavailable: <reason> (it is switched on)`."""
    name, state = str(row["name"]), str(row["state"])
    reason = str(row.get("reason") or "")
    if state == "unavailable":
        return f"ERROR {name:<10} unavailable: {reason} (it is switched on)"
    if state == "off" and row.get("available") is True:
        return f"!     {name:<10} off -- available: {enable_hint(name)}"
    if state == "on" and reason:
        return f"      {name:<10} on ({reason})"
    return f"      {name:<10} {state}"


def startup_lines(
    *,
    model: str,
    effort: str,
    keep: bool,
    rows: Sequence[Mapping[str, Any]],
    effort_note: str = "",
) -> list[str]:
    """What a chat prints when it starts: the model, the reasoning settings, then
    one line per capability (`capability_line`)."""
    shown = f"effort {effort}" + (f" ({effort_note})" if effort_note else "")
    lines = [
        f"model: {model}",
        f"reasoning: {'on' if reasoning_on(effort) else 'off'}, {shown}",
        "keep reasoning: "
        + (
            "on (each round's reasoning is sent back to the model)"
            if keep
            else "off (past reasoning is not sent back to the model)"
        ),
        "capabilities:",
    ]
    lines.extend(capability_line(row) for row in rows)
    return lines
