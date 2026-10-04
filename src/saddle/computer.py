"""The `computer` tool: act on a window the model can see with `screenshot`.

In a live setup run the model saw a game's first-run "Video Configuration"
dialog and the game's window through `screenshot`, but could not press
"Start Game" or raise the window, and spent many minutes working around it.
This tool focuses a window, clicks in it, presses a key or combination, types
text, or scrolls, through `xdotool` on the X display (Xwayland included, so
Wine programs are reachable; native Wayland windows are not).

Who it may touch: a window whose owning process (`xdotool getwindowpid`, the
window's `_NET_WM_PID`) is one of this session's own processes is acted on
freely; any other window is put to the person first (`tools._computer`).
Pure parsing and the xdotool argv live here; running X programs is injected
(`screen.Run`) so tests need no display.
"""

from __future__ import annotations

import re
import shutil
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any, Final

from saddle import screen
from saddle.screen import Run, Window

NEEDED: Final = ("xdotool",)
"""The X tool the actions run; without it the tool is not offered."""

ACTIONS: Final = ("focus", "click", "key", "type", "scroll")
BUTTONS: Final = {"left": "1", "right": "3"}
WHEEL: Final = {"up": "4", "down": "5"}
MAX_SCROLL: Final = 20
"""Wheel steps in one scroll; more is a mistake, not a scroll."""

DEFAULT_SCROLL: Final = 3

_KEY: Final = re.compile(r"[A-Za-z0-9_]+(\+[A-Za-z0-9_]+)*")
"""One key or combination in xdotool's keysym syntax ("Return", "alt+Return",
"ctrl+s"); matched whole, so nothing that reads as an option or a second key
gets through."""


@dataclass(frozen=True)
class Action:
    """One checked action, before a window is chosen."""

    kind: str
    x: int | None = None
    y: int | None = None
    button: str = "left"
    double: bool = False
    keys: str = ""
    text: str = ""
    direction: str = "down"
    amount: int = DEFAULT_SCROLL

    def describe(self) -> str:
        if self.kind == "click":
            twice = "double-" if self.double else ""
            return f"{self.button} {twice}click at ({self.x}, {self.y})"
        if self.kind == "key":
            return f"press {self.keys}"
        if self.kind == "type":
            return f"type {len(self.text)} characters"
        if self.kind == "scroll":
            return f"scroll {self.direction} {self.amount}"
        return "raise and focus it"


def available(env: Mapping[str, str], which: Callable[[str], str | None] | None = None) -> bool:
    """An X display is set and xdotool is installed (`shutil.which`, looked up
    when asked)."""
    which = which or shutil.which
    return bool(env.get("DISPLAY")) and all(which(tool) for tool in NEEDED)


def _whole(args: Mapping[str, Any], key: str) -> int | None:
    """An optional whole number; a numeric string counts. Anything else raises
    ValueError: a 3.5 or a "left" is reported, never rounded or dropped."""
    value = args.get(key)
    if isinstance(value, str) and re.fullmatch(r"-?\d+", value.strip()):
        value = int(value)
    if value is None or (isinstance(value, int) and not isinstance(value, bool)):
        return value
    raise ValueError(key)


def parse(args: Mapping[str, Any]) -> Action | str:
    """The action `args` describe, or an "error: ..." naming what is wrong."""
    try:
        return _parse(args)
    except ValueError as exc:
        return f"error: {exc} must be a whole number"


def _parse(args: Mapping[str, Any]) -> Action | str:
    kind = args.get("action")
    if kind not in ACTIONS:
        return f"error: action must be one of {', '.join(ACTIONS)}"
    if kind == "focus":
        return Action("focus")
    if kind == "key":
        keys = args.get("keys")
        if not isinstance(keys, str) or _KEY.fullmatch(keys) is None:
            return (
                "error: keys must be one key or combination in xdotool syntax, such as "
                "Return, alt+Return or ctrl+s; for several keys, call computer once per key"
            )
        return Action("key", keys=keys)
    if kind == "type":
        text = args.get("text")
        if not isinstance(text, str) or not text:
            return "error: type needs a non-empty string text"
        return Action("type", text=text)
    x, y = _whole(args, "x"), _whole(args, "y")
    if kind == "scroll":
        direction = args.get("direction", "down")
        amount = _whole(args, "amount")
        amount = DEFAULT_SCROLL if amount is None else amount
        if direction not in WHEEL or not 1 <= amount <= MAX_SCROLL:
            return f"error: scroll needs direction up or down and an amount from 1 to {MAX_SCROLL}"
        if (x is None) != (y is None):
            return "error: scroll takes both x and y, or neither (the window's middle)"
        return Action("scroll", x=x, y=y, direction=direction, amount=amount)
    button = args.get("button", "left")
    double = args.get("double", False)
    if x is None or y is None:
        return "error: click needs x and y, in pixels from the window's top-left corner"
    if button not in BUTTONS or not isinstance(double, bool):
        return "error: click takes button left or right and double true or false"
    return Action("click", x=x, y=y, button=button, double=double)


def placed(action: Action, window: Window) -> Action | str:
    """`action` with its point inside `window` (a scroll without one aims at
    the middle), or an "error: ..." when the point is outside it."""
    if action.kind not in ("click", "scroll"):
        return action
    if action.x is None or action.y is None:
        return Action(
            "scroll",
            x=window.width // 2,
            y=window.height // 2,
            direction=action.direction,
            amount=action.amount,
        )
    if not (0 <= action.x < window.width and 0 <= action.y < window.height):
        return (
            f"error: ({action.x}, {action.y}) is outside {window.describe()}; x and y count "
            "pixels from the window's top-left corner, as its screenshot shows them"
        )
    return action


def argv(action: Action, window: Window) -> list[list[str]]:
    """The xdotool commands that perform `action` on `window` (point placed)."""
    xid = str(int(window.id, 16))
    if action.kind == "focus":
        return [["xdotool", "windowactivate", xid], ["xdotool", "windowfocus", xid]]
    if action.kind == "key":
        return [["xdotool", "key", "--window", xid, action.keys]]
    if action.kind == "type":
        return [["xdotool", "type", "--window", xid, "--", action.text]]
    move = ["xdotool", "mousemove", "--window", xid, str(action.x), str(action.y), "click"]
    if action.kind == "scroll":
        return [[*move, "--repeat", str(action.amount), WHEEL[action.direction]]]
    twice = ["--repeat", "2"] if action.double else []
    return [[*move, *twice, BUTTONS[action.button]]]


def find(wanted: str, env: Mapping[str, str], run: Run | None = None) -> Window | str:
    """The window `wanted` names (`screen.choose`), or an "error: ..."."""
    run = run or screen.run_x
    code, tree = run(["xwininfo", "-root", "-tree"], env)
    if code != 0:
        return "error: the window list could not be read (xwininfo failed)"
    return screen.choose(screen.parse_windows(tree), wanted)


def owner(window: Window, env: Mapping[str, str], run: Run | None = None) -> int | None:
    """The PID that owns `window` (its `_NET_WM_PID`), or None when it has none."""
    run = run or screen.run_x
    code, out = run(["xdotool", "getwindowpid", str(int(window.id, 16))], env)
    text = out.strip()
    return int(text) if code == 0 and text.isdigit() else None


def perform(
    action: Action, window: Window, env: Mapping[str, str], run: Run | None = None
) -> str | None:
    """Run `action` on `window`; None when it was done, else an "error: ...".

    Focus is done when either step works: a compositor that refuses to
    activate a window can still give it the keyboard."""
    run = run or screen.run_x
    codes = [run(step, env)[0] for step in argv(action, window)]
    if action.kind == "focus" and 0 in codes:
        return None
    if all(code == 0 for code in codes):
        return None
    return f"error: {action.describe()} on {window.describe()} failed (xdotool exited {max(codes)})"
