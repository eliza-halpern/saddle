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
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass, replace
from typing import Any, Final, Protocol

from saddle import screen
from saddle.screen import Run, Window
from saddle.wlpointer import WaylandError, WaylandPointer

NEEDED: Final = ("xdotool",)
"""The X tool the actions run; without it the tool is not offered."""

ACTIONS: Final = ("focus", "click", "key", "type", "scroll", "drag", "move")
BUTTONS: Final = {"left": "1", "right": "3"}
WHEEL: Final = {"up": "4", "down": "5"}
MAX_SCROLL: Final = 20
"""Wheel steps in one scroll; more is a mistake, not a scroll."""

DEFAULT_SCROLL: Final = 3

_KEY: Final = re.compile(r"[A-Za-z0-9_]+(\+[A-Za-z0-9_]+)*")
"""One key or combination in xdotool's keysym syntax ("Return", "alt+Return",
"ctrl+s"); matched whole, so nothing that reads as an option gets through."""
MAX_KEYS: Final = 8
"""Keys one `key` action may press in order, space-separated. Live, four
shift+Right in one call were refused and cost the model a round."""


@dataclass(frozen=True)
class Action:
    """One checked action, before a window is chosen."""

    kind: str
    x: int | None = None
    y: int | None = None
    button: str = "left"
    double: bool = False
    triple: bool = False
    hold: str = ""
    """Modifier keys held through a click, scroll or drag ("ctrl", "shift+ctrl")."""
    keys: str = ""
    text: str = ""
    direction: str = "down"
    amount: int = DEFAULT_SCROLL
    to_x: int | None = None
    to_y: int | None = None
    dx: int | None = None
    dy: int | None = None
    click: bool = False
    """For a move: press the left button where the pointer ends up."""

    def describe(self) -> str:
        held = f" holding {self.hold}" if self.hold else ""
        if self.kind == "click":
            twice = "triple-" if self.triple else "double-" if self.double else ""
            return f"{self.button} {twice}click at ({self.x}, {self.y}){held}"
        if self.kind == "key":
            return f"press {self.keys}"
        if self.kind == "type":
            return f"type {len(self.text)} characters"
        if self.kind == "scroll":
            return f"scroll {self.direction} {self.amount}{held}"
        if self.kind == "drag":
            return f"drag from ({self.x}, {self.y}) to ({self.to_x}, {self.to_y}){held}"
        if self.kind == "move":
            then = " and left click there" if self.click else ""
            return f"move the pointer by ({self.dx}, {self.dy}){then}"
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


ALIASES: Final[Mapping[str, Mapping[str, Any]]] = {
    "double_click": {"action": "click", "double": True},
    "double": {"action": "click", "double": True},
    "right_click": {"action": "click", "button": "right"},
    "left_click": {"action": "click", "button": "left"},
    "triple_click": {"action": "click", "triple": True},
    "triple": {"action": "click", "triple": True},
}
"""Action names a model reaches for, read as the click they name. Live, action
"double" and "double_click" were refused again and again in one run."""


def _flag(value: Any) -> Any:
    """A true/false flag, also when it came as the string "true" or "false"."""
    if isinstance(value, str) and value.strip().lower() in ("true", "false"):
        return value.strip().lower() == "true"
    return value


MODIFIERS: Final = ("ctrl", "shift", "alt", "super")
"""Keys `hold` may name: live, a drawing tool's constrained drag needed Ctrl
held, and the computer tool could not hold it."""


def _hold(args: Mapping[str, Any]) -> str | None:
    """The modifiers `args` asks to hold, "+"-joined, or None when they are not
    modifiers."""
    raw = args.get("hold", "")
    if not isinstance(raw, str):
        return None
    parts = [p.strip().lower() for p in raw.replace(" ", "+").split("+") if p.strip()]
    return "+".join(parts) if all(p in MODIFIERS for p in parts) else None


def _parse(args: Mapping[str, Any]) -> Action | str:
    parsed = _parse_kind(args)
    if isinstance(parsed, str) or parsed.kind not in ("click", "scroll", "drag"):
        return parsed
    hold = _hold(args)
    if hold is None:
        return f"error: hold takes modifier keys joined by +, from {', '.join(MODIFIERS)}"
    return replace(parsed, hold=hold)


def _parse_kind(args: Mapping[str, Any]) -> Action | str:
    kind = args.get("action")
    if isinstance(kind, str) and kind in ALIASES:
        args = {**args, **ALIASES[kind]}
        kind = args["action"]
    if kind not in ACTIONS:
        return f"error: action must be one of {', '.join(ACTIONS)}"
    if kind == "focus":
        return Action("focus")
    if kind == "key":
        keys = args.get("keys")
        parts = keys.split() if isinstance(keys, str) else []
        if not 1 <= len(parts) <= MAX_KEYS or any(_KEY.fullmatch(p) is None for p in parts):
            return (
                "error: keys must be one key or combination in xdotool syntax, such as "
                f"Return, alt+Return or ctrl+s, or up to {MAX_KEYS} of them separated by "
                "spaces, pressed in order"
            )
        for part in parts:
            held = part.split("+")
            twice = [k for i, k in enumerate(held) if k in held[:i]]
            if twice:
                spaced = " ".join(held)
                return (
                    f"error: {part} holds {twice[0]} together with itself (+ joins keys "
                    f"held at once); to press keys one after another, separate them with "
                    f"spaces, as in {spaced}. Nothing was pressed."
                )
        keys = " ".join(parts)
        return Action("key", keys=keys)
    if kind == "type":
        text = args.get("text")
        if not isinstance(text, str) or not text:
            return "error: type needs a non-empty string text"
        return Action("type", text=text)
    if kind == "move":
        dx, dy = _whole(args, "dx"), _whole(args, "dy")
        click = _flag(args.get("click", False))
        if dx is None or dy is None or not isinstance(click, bool):
            return (
                "error: move needs dx and dy, the pixels to move the pointer by from where "
                "it is (right and down are positive), and click true or false"
            )
        return Action("move", dx=dx, dy=dy, click=click)
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
    if kind == "drag":
        to_x, to_y = _whole(args, "to_x"), _whole(args, "to_y")
        if x is None or y is None or to_x is None or to_y is None:
            return (
                "error: drag needs x, y (where to press, inside the window) and to_x, to_y "
                "(where to let go), in the window's pixels"
            )
        return Action("drag", x=x, y=y, to_x=to_x, to_y=to_y)
    button = args.get("button", "left")
    double = _flag(args.get("double", False))
    triple = _flag(args.get("triple", False))
    if x is None or y is None:
        return "error: click needs x and y, in pixels from the window's top-left corner"
    if button not in BUTTONS or not isinstance(double, bool) or not isinstance(triple, bool):
        return "error: click takes button left or right and double or triple true or false"
    return Action("click", x=x, y=y, button=button, double=double, triple=triple)


def placed(
    action: Action, window: Window, bounds: tuple[int, int, int, int] | None = None
) -> Action | str:
    """`action` with its point inside `window`, or inside `bounds` (left, top,
    right, bottom in window pixels: a picture that also showed a menu over the
    window); a scroll without one aims at the middle; an "error: ..." when the
    point is outside."""
    if action.kind not in ("click", "scroll", "drag"):
        return action
    if action.x is None or action.y is None:
        return Action(
            "scroll",
            x=window.width // 2,
            y=window.height // 2,
            direction=action.direction,
            amount=action.amount,
        )
    left, top, right, bottom = bounds or (0, 0, window.width, window.height)
    if not (left <= action.x < right and top <= action.y < bottom):
        return (
            f"error: ({action.x}, {action.y}) is outside {window.describe()}; x and y count "
            "pixels from the window's top-left corner, as its screenshot shows them. If you "
            "measured them on a whole-screen screenshot, pass space=screen"
        )
    return action


def typing(action: Action) -> list[list[str]]:
    """The commands that type `action.text`: each line typed, and Return pressed
    as a key between lines. Live, a newline inside `xdotool type` reached Writer
    as nothing ("Owls\\nOwls are" became "OwlsOwls are")."""
    argvs: list[list[str]] = []
    for number, line in enumerate(action.text.replace("\r\n", "\n").split("\n")):
        if number:
            argvs.append(["xdotool", "key", "--clearmodifiers", "Return"])
        if line:
            argvs.append(steps(replace(action, text=line)))
    return argvs


def steps(action: Action) -> list[str]:
    """The xdotool command that performs `action` once its target is verified:
    keys and text go to the focused window as real (XTest) input, clicks and
    scrolls at the pointer. `key --window` (XSendEvent) is not used: programs,
    Wine among them, often ignore synthetic events."""
    if action.kind == "key":
        return ["xdotool", "key", "--clearmodifiers", *action.keys.split()]
    if action.kind == "type":
        return ["xdotool", "type", "--clearmodifiers", "--", action.text]
    if action.kind == "move":
        # Relative motion, as a game that has captured the mouse reads it: such a
        # game draws its own cursor and moves it only by the motion it is sent,
        # so an absolute jump lands nowhere it can see.
        moved = ["xdotool", "mousemove_relative", "--sync", "--", str(action.dx), str(action.dy)]
        return [*moved, "sleep", SETTLE_S, "click", "1"] if action.click else moved
    settle = ["xdotool", "sleep", SETTLE_S]
    mods = action.hold.split("+") if action.hold else []
    if mods:  # pressed after the settle, let go after the press, in one call
        settle = [*settle, "keydown", *mods]
    release = ["keyup", *mods] if mods else []
    return [*_press_steps(action, settle), *release]


def _press_steps(action: Action, settle: list[str]) -> list[str]:
    if action.kind == "drag":
        # In steps, not one jump: measured in GIMP, a rectangle select dragged
        # in one move selected nothing (3 of 3), in 20 steps it selected the
        # rectangle (3 of 3). Each step is the share of the distance still due,
        # so the steps add up to it exactly.
        dx = (action.to_x or 0) - (action.x or 0)
        dy = (action.to_y or 0) - (action.y or 0)
        moves: list[str] = []
        for step in range(1, DRAG_STEPS + 1):
            sx = dx * step // DRAG_STEPS - dx * (step - 1) // DRAG_STEPS
            sy = dy * step // DRAG_STEPS - dy * (step - 1) // DRAG_STEPS
            moves += ["mousemove_relative", "--", str(sx), str(sy), "sleep", DRAG_STEP_S]
        return [*settle, "mousedown", "1", *moves, "sleep", SETTLE_S, "mouseup", "1"]
    if action.kind == "scroll":
        return [*settle, "click", "--repeat", str(action.amount), WHEEL[action.direction]]
    twice = ["--repeat", "3" if action.triple else "2"] if action.double or action.triple else []
    return [*settle, "click", *twice, BUTTONS[action.button]]


DRAG_STEP_S: Final = "0.02"
"""The pause after each step of a drag, so the program sees separate moves."""
SETTLE_S: Final = "0.5"
"""Seconds between the pointer arriving and the press: live (labwc, Xwayland, a
GTK dialog), a press 0.2 s after the move was not taken; 0.5 s and 1.0 s were."""
NOT_FOCUSED: Final = (
    "error: nothing was sent: {window} could not be given the keyboard (another window "
    "kept the focus), and keys sent now would reach that window instead"
)
NO_POINTER: Final = (
    "error: nothing was clicked: this desktop did not let saddle move the pointer onto "
    "{window} (it stayed at {where}), and a click now would land wherever the pointer is. "
    "Use the keyboard instead: action=key with Tab, space, Return or the arrow keys, "
    "or, in a program that draws its own cursor, action=move"
)
NOT_ACTIVE: Final = (
    "error: nothing was moved: {window} could not be made the active window (another "
    "window kept it), and pointer motion or a click now would reach that window instead"
)
POINTER_SLACK: Final = 2
"""Pixels the pointer may be off the aimed point and still count as on it."""


def _shell(text: str) -> dict[str, int]:
    """`xdotool ... --shell` output (`X=12` lines) as numbers."""
    found: dict[str, int] = {}
    for line in text.splitlines():
        name, _, value = line.partition("=")
        if value.strip().lstrip("-").isdigit():
            found[name.strip()] = int(value)
    return found


def find(wanted: str, env: Mapping[str, str], run: Run | None = None) -> Window | str:
    """The window `wanted` names (`screen.choose`), or an "error: ..."."""
    run = run or screen.run_x
    code, tree = run(["xwininfo", "-root", "-tree"], env)
    if code != 0:
        return "error: the window list could not be read (xwininfo failed)"
    return screen.choose(screen.shown_windows(tree, env, run), wanted)


def owner(window: Window, env: Mapping[str, str], run: Run | None = None) -> int | None:
    """The PID that owns `window` (its `_NET_WM_PID`), or None when it has none."""
    run = run or screen.run_x
    code, out = run(["xdotool", "getwindowpid", str(int(window.id, 16))], env)
    text = out.strip()
    return int(text) if code == 0 and text.isdigit() else None


class Pointer(Protocol):
    """What a click or drag needs from a pointer the compositor drives
    (`wlpointer.WaylandPointer`)."""

    def move(self, x: int, y: int, extent: tuple[int, int]) -> None: ...
    def button(self, button: str, *, pressed: bool) -> None: ...
    def close(self) -> None: ...


def session_pointer(env: Mapping[str, str]) -> Pointer | None:
    """The compositor's own pointer when it offers one (a wlroots compositor:
    labwc, sway, a headless one), else None and clicks go through X."""
    try:
        return WaylandPointer.connect(env)
    except (OSError, WaylandError):
        return None


HOLD_S: Final = 0.2
"""Seconds the button is held before a drag moves: live, a drag that moved at
once after the press left the window where it was; one that waited moved it."""
DRAG_STEPS: Final = 10
"""Moves between a drag's press and release: a compositor starts moving a window
only once the pointer has travelled a little with the button down."""


def perform(
    action: Action,
    window: Window,
    env: Mapping[str, str],
    run: Run | None = None,
    pointer: Callable[[Mapping[str, str]], Pointer | None] | None = None,
    sleep: Callable[[float], None] = time.sleep,
) -> str | None:
    """Run `action` on `window`; None when it was done, else an "error: ...".

    No Wayland or X method aims input at a window: input goes where the focus
    or the pointer is. So keys go only after the window is verified to have the
    focus, and a click only after the pointer is read back on the aimed point;
    otherwise nothing is sent (F43: live, a pointer move sent while the window
    was not active was silently ignored, and the click would have gone astray).
    Clicks and drags use the compositor's own pointer when it offers one
    (`session_pointer`): only that can move a window. Focus is done when either
    step works: a compositor that refuses to activate a window can still give
    it the keyboard."""
    run = run or screen.run_x
    xid = str(int(window.id, 16))
    if action.kind == "focus":
        codes = [run(["xdotool", verb, xid], env)[0] for verb in ("windowactivate", "windowfocus")]
        if 0 in codes:
            return None
        failed = max(codes)
        return f"error: {action.describe()} on {window.describe()} failed (xdotool exited {failed})"
    run(["xdotool", "windowactivate", xid], env)
    if action.kind in ("key", "type", "move"):
        # A move is not read back: a program that captured the mouse keeps the X
        # pointer where it likes, so the check a click gets would refuse every move.
        code, active = run(["xdotool", "getactivewindow"], env)
        if code != 0 or active.strip() != xid:
            refusal = NOT_ACTIVE if action.kind == "move" else NOT_FOCUSED
            return refusal.format(window=window.describe())
        for argv in typing(action) if action.kind == "type" else [steps(action)]:
            code = run(argv, env)[0]
            if code != 0:
                return _ran(code, action, window)
        return None
    _, geometry = run(["xdotool", "getwindowgeometry", "--shell", xid], env)
    frame = _shell(geometry)
    aimed = (frame.get("X", 0) + (action.x or 0), frame.get("Y", 0) + (action.y or 0))
    # X input for everything inside the window; the compositor's pointer only
    # for a drag that starts outside it (a title bar), which X cannot do.
    # Measured on labwc with Writer: clicks on menu items through X 11 of 11
    # (first-level and submenu, a first click arriving from a native window
    # included); through the compositor's pointer 0 of 9 on "Insert Table...".
    inside = 0 <= (action.x or 0) < window.width and 0 <= (action.y or 0) < window.height
    by_compositor = action.kind == "drag" and not inside
    if by_compositor and action.hold:
        return (
            "error: a key can be held only through a drag that starts inside the window; "
            "a title-bar drag goes through the compositor. Nothing was done."
        )
    real = (pointer or session_pointer)(env) if by_compositor else None
    extent = _display(run, env) if real is not None else None
    if real is not None and extent is not None:
        _, before = run(["xdotool", "getmouselocation", "--shell"], env)
        start = _shell(before)
        _glide(real, (start.get("X", aimed[0]), start.get("Y", aimed[1])), aimed, extent, sleep)
    else:
        if real is not None:
            real.close()
            real = None
        _show_cursor_at(aimed, env, run, pointer)
        x, y = action.x or 0, action.y or 0
        if 0 <= x < window.width and 0 <= y < window.height:
            run(["xdotool", "mousemove", "--window", xid, str(x), str(y)], env)
        else:  # on a menu over the window, past its edge: the screen's own place
            run(["xdotool", "mousemove", "--", str(aimed[0]), str(aimed[1])], env)
    _, location = run(["xdotool", "getmouselocation", "--shell"], env)
    seen = _shell(location)
    here = (seen.get("X", -1), seen.get("Y", -1))
    if any(abs(a - b) > POINTER_SLACK for a, b in zip(aimed, here, strict=True)):
        if real is not None:
            real.close()
        where = f"({here[0]}, {here[1]})" if "X" in seen else "an unknown place"
        return NO_POINTER.format(window=window.describe(), where=where)
    if real is None or extent is None:
        return _ran(run(steps(action), env)[0], action, window)
    try:
        _press(real, action, aimed, extent, frame, sleep)
    finally:
        real.close()
    return None


GLIDE_STEPS: Final = 8
GLIDE_S: Final = 0.02
"""How the compositor pointer travels to a click: from where it is, in this
many moves this far apart. Measured on labwc with Writer: a click on "Bold"
in an open submenu after a single jump onto it applied bold 0 times of 6
(and 0 of 3 holding the button, 0 of 6 with a wiggle on the item); after the
pointer left the submenu and came back in, 9 of 9. A fresh virtual pointer's
first move is not seen as the pointer arriving; a glide arrives on a later one."""


def _glide(
    real: Pointer,
    start: tuple[int, int],
    end: tuple[int, int],
    extent: tuple[int, int],
    sleep: Callable[[float], None],
) -> None:
    for step in range(1, GLIDE_STEPS + 1):
        x = start[0] + (end[0] - start[0]) * step // GLIDE_STEPS
        y = start[1] + (end[1] - start[1]) * step // GLIDE_STEPS
        real.move(x, y, extent)
        sleep(GLIDE_S)


def _show_cursor_at(
    aimed: tuple[int, int],
    env: Mapping[str, str],
    run: Run,
    pointer: Callable[[Mapping[str, str]], Pointer | None] | None,
) -> None:
    """Move the compositor's pointer (no press) to where X is about to press.
    The cursor drawn in a picture is the compositor's, and X moves do not move
    it: live, pictures showed the arrow where an earlier action left it, and
    the model reasoned from that arrow about where its click had landed."""
    shown = (pointer or session_pointer)(env)
    if shown is None:
        return
    try:
        extent = _display(run, env)
        if extent is not None:
            shown.move(*aimed, extent)
    finally:
        shown.close()


def _ran(code: int, action: Action, window: Window) -> str | None:
    if code == 0:
        return None
    return f"error: {action.describe()} on {window.describe()} failed (xdotool exited {code})"


def _display(run: Run, env: Mapping[str, str]) -> tuple[int, int] | None:
    """The screen's size in the coordinates windows are placed in, or None."""
    return screen.display_size(run, env)


def _press(
    real: Pointer,
    action: Action,
    aimed: tuple[int, int],
    extent: tuple[int, int],
    frame: Mapping[str, int],
    sleep: Callable[[float], None],
) -> None:
    """The drag itself (the one action the compositor's pointer serves: from a
    title bar), on the pointer already at `aimed`."""
    sleep(float(SETTLE_S))
    end = (frame.get("X", 0) + (action.to_x or 0), frame.get("Y", 0) + (action.to_y or 0))
    real.button("left", pressed=True)
    sleep(HOLD_S)
    for step in range(1, DRAG_STEPS + 1):
        x = aimed[0] + (end[0] - aimed[0]) * step // DRAG_STEPS
        y = aimed[1] + (end[1] - aimed[1]) * step // DRAG_STEPS
        real.move(x, y, extent)
        sleep(0.03)
    sleep(float(SETTLE_S))
    real.button("left", pressed=False)


SPACES: Final = ("window", "screen", "zoom")
"""Where x, y were measured: on a screenshot of the window (its own pixels), or
on the last whole-screen screenshot (scaled, from the screen's corner)."""


def to_window(
    action: Action,
    window: Window,
    capture: tuple[int, int] | None,
    env: Mapping[str, str],
    run: Run | None = None,
) -> Action | str:
    """`action` with x, y measured on the last whole-screen screenshot (`capture`,
    its width and height in pixels) turned into the window's own pixels: scaled
    to the display's real size, less the window's position. Live, a model aimed
    a click with whole-screen pixels (1600x900, scaled) at a 524x411 dialog and
    was refused as outside it."""
    if action.x is None or action.y is None:
        return action
    if capture is None:
        return (
            "error: space=screen needs a whole-screen screenshot to measure on; take one "
            "(screenshot with no window), then give x, y as it shows them"
        )
    run = run or screen.run_x
    code, size = run(["xdotool", "getdisplaygeometry"], env)
    parts = size.split()
    if code != 0 or len(parts) != 2 or not all(part.isdigit() for part in parts):
        return (
            "error: the screen's size could not be read, so the point cannot be placed; "
            "take a screenshot of the window and use its pixels"
        )
    _, geometry = run(["xdotool", "getwindowgeometry", "--shell", str(int(window.id, 16))], env)
    frame = _shell(geometry)
    if "X" not in frame or "Y" not in frame:
        return (
            f"error: where {window.describe()} sits on the screen could not be read; take a "
            "screenshot of the window and use its pixels"
        )
    width, height = int(parts[0]), int(parts[1])
    x = round(action.x * width / capture[0]) - frame["X"]
    y = round(action.y * height / capture[1]) - frame["Y"]
    if action.to_x is not None and action.to_y is not None:
        to_x = round(action.to_x * width / capture[0]) - frame["X"]
        to_y = round(action.to_y * height / capture[1]) - frame["Y"]
        return replace(action, x=x, y=y, to_x=to_x, to_y=to_y)
    return replace(action, x=x, y=y)


def from_zoom(action: Action, zoom: screen.Zoom | None, window: Window) -> Action | str:
    """`action` with x, y measured on the latest zoomed screenshot turned into
    the window's own pixels: the zoom's origin plus the point over its scale."""
    if zoom is None or zoom.window != window.id:
        return (
            f"error: your latest zoomed screenshot was not of {window.describe()}; zoom into "
            "that window first, or give x, y in its own pixels with space=window"
        )
    if action.x is None or action.y is None:
        return action
    x, y = zoom.x + round(action.x / zoom.scale), zoom.y + round(action.y / zoom.scale)
    if action.to_x is not None and action.to_y is not None:
        to_x = zoom.x + round(action.to_x / zoom.scale)
        to_y = zoom.y + round(action.to_y / zoom.scale)
        return replace(action, x=x, y=y, to_x=to_x, to_y=to_y)
    return replace(action, x=x, y=y)
