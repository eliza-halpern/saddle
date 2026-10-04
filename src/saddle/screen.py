"""The `screenshot` tool: what is on the person's screen, shown to the model.

A model that cannot see a window cannot tell an error dialog from a game's
menu: in a live setup run the game opened a 640x480 window that only the
person could see, and a "Critical Error" dialog that the model could only
name by its title. The tool captures one window, or the whole screen, and
hands the picture to the model through the same path as an image file
(`tools._read_image`).

It is offered only when it can work and is allowed: the served model reads
images, the session has full access (a sandboxed command has no display, and
the screen holds every other program the person has open), and an X display
with `xwininfo` and ImageMagick's `import` is there. Pure parsing and choosing
live here; running the X tools is injected (`Run`) so tests need no display.
"""

from __future__ import annotations

import itertools
import re
import shutil
import subprocess
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Final

Run = Callable[[Sequence[str], Mapping[str, str]], tuple[int, str]]
"""Runs argv with the desktop environment; the exit code and stdout."""

NEEDED: Final = ("xwininfo", "import")
"""The X tools the capture runs; without them the tool is not offered."""

MIN_SIDE: Final = 16
"""Windows narrower or shorter than this are helpers (input methods, 1x1
anchors), never something the person sees."""

MAX_SIDE: Final = 1600
"""A capture is scaled down to fit this, so a 4K screen stays under the image
limit the model is shown."""

LIST: Final = "list"

_LINE: Final = re.compile(
    r'^\s*(0x[0-9a-fA-F]+) (?:"(.*)"|\(has no name\)): \(.*?\)\s+(\d+)x(\d+)\+(-?\d+)\+(-?\d+)'
    r"(?:\s+\+(-?\d+)\+(-?\d+))?"
)


@dataclass(frozen=True)
class Window:
    """One named window the person could see."""

    id: str
    title: str
    width: int
    height: int
    x: int = 0
    y: int = 0
    """Where the window sits on the screen (xwininfo's absolute place)."""

    def describe(self) -> str:
        return f'{self.id} "{self.title}" {self.width}x{self.height}'


def available(env: Mapping[str, str], which: Callable[[str], str | None] = shutil.which) -> bool:
    """An X display is set and the capture tools are installed."""
    return bool(env.get("DISPLAY")) and all(which(tool) for tool in NEEDED)


def parse_windows(tree: str, *, named: bool = True) -> list[Window]:
    """The named (or, with `named=False`, also unnamed) visible-sized windows
    in `xwininfo -root -tree` output, in its order (stacking order, the top
    window first), each id once."""
    found: dict[str, Window] = {}
    for line in tree.splitlines():
        match = _LINE.match(line)
        if match is None:
            continue
        wid, title, width, height = match[1], match[2] or "", int(match[3]), int(match[4])
        x, y = int(match[7] or match[5]), int(match[8] or match[6])
        if (title or not named) and width >= MIN_SIDE and height >= MIN_SIDE and wid not in found:
            found[wid] = Window(wid, title, width, height, x, y)
    return list(found.values())


def hidden(window: Window, env: Mapping[str, str], run: Run) -> bool:
    """Whether X reports `window` as not shown (unmapped or unviewable).
    LibreOffice keeps a closed menu as such a window, still in the tree; live,
    the model tried to focus one. A window whose state cannot be read counts
    as shown, so nothing is left out on a failed query."""
    code, info = run(["xwininfo", "-id", window.id], env)
    return code == 0 and ("IsUnMapped" in info or "IsUnviewable" in info)


def shown_windows(tree: str, env: Mapping[str, str], run: Run) -> list[Window]:
    """The named windows in `tree` that are on the screen (`hidden`)."""
    return [w for w in parse_windows(tree) if not hidden(w, env, run)]


def choose(windows: Sequence[Window], wanted: str) -> Window | str:
    """The window `wanted` names: its id exactly, else the one window whose
    title contains it (ignoring case). A refusal naming the windows otherwise."""
    for window in windows:
        if window.id.lower() == wanted.lower():
            return window
    matches = [w for w in windows if wanted.lower() in w.title.lower()]
    if len(matches) == 1:
        return matches[0]
    shown = "; ".join(w.describe() for w in (matches or windows)) or "none"
    if matches:
        return f"error: {len(matches)} windows match {wanted!r}; name one by id: {shown}"
    return f"error: no window matches {wanted!r}; the windows are: {shown}"


def run_x(argv: Sequence[str], env: Mapping[str, str]) -> tuple[int, str]:
    """`Run` for the real desktop: a short X client call."""
    done = subprocess.run(
        list(argv), env=dict(env), capture_output=True, text=True, timeout=20, check=False
    )
    return done.returncode, done.stdout


ZOOM_MAX: Final = 4
"""How much a zoomed region is enlarged at most: live, text in a 1600x900
whole-screen picture was too small for the model to tell "O" from "o"."""


@dataclass(frozen=True)
class Zoom:
    """A region of a window, as `capture` enlarged it: where it starts in the
    window and how many picture pixels one window pixel became."""

    window: str
    x: int
    y: int
    scale: float
    width: int | None = None
    height: int | None = None
    """The picture's extent in window pixels when it reaches past the window
    (a menu over it): points anywhere on it may be acted on."""
    label: str = "zoomed"
    """How the result names the picture the point was read on."""


def zoom_size(width: int, height: int) -> tuple[int, int, float]:
    """The enlarged picture's size and its scale: up to ZOOM_MAX, within MAX_SIDE."""
    scale = min(float(ZOOM_MAX), MAX_SIDE / max(width, height))
    return round(width * scale), round(height * scale), scale


def capture(
    wanted: str,
    out: Path,
    env: Mapping[str, str],
    run: Run | None = None,
    region: tuple[int, int, int, int] | None = None,
    on_top: Callable[[Window], bool] | None = None,
    which: Callable[[str], str | None] | None = None,
    area: list[tuple[int, int, int, int]] | None = None,
) -> Window | str | None:
    """Capture what `wanted` names into `out` (PNG). An empty `wanted` is the
    whole screen (None); `list` returns the window list as text; `region`
    (x, y, width, height in the window's pixels) captures that part of the
    window, enlarged (`zoom_size`); a failure is an "error: ..." string.

    A window `on_top` says is uncovered is taken from the screen itself under
    Wayland (`grim`), scaled to the window's own pixels: a menu or dropdown is a
    window of its own, so a picture of the window alone left it out (live, the
    model saw "Format" highlighted and no menu). A covered window is read from X,
    which shows the window and not what covers it."""
    run = run or run_x
    code, tree = run(["xwininfo", "-root", "-tree"], env)
    if code != 0:
        return "error: the window list could not be read (xwininfo failed)"
    windows = shown_windows(tree, env, run)
    if wanted.strip().lower() == LIST:
        listed = "\n".join(w.describe() for w in windows)
        if not listed:
            return "no X11 windows are open (native Wayland windows are not listed)"
        return f"windows on the screen:\n{listed}"
    target: Window | None = None
    if wanted.strip():
        chosen = choose(windows, wanted.strip())
        if isinstance(chosen, str):
            return chosen
        target = chosen
    seen = (
        target is not None
        and on_top is not None
        and bool(env.get("WAYLAND_DISPLAY"))
        and (which or shutil.which)("grim") is not None
        and on_top(target)
    )
    if region is not None:
        if target is None:
            return "error: a region is part of a window: name the window too"
        x, y, width, height = region
        if width < 1 or height < 1 or x < 0 or y < 0:
            return "error: a region needs x, y of 0 or more and a width and height of 1 or more"
        if x >= target.width or y >= target.height:
            return (
                f"error: the region ({x}, {y}, {width}x{height}) starts past "
                f"{target.describe()}; give a point inside the window"
            )
        # A region reaching past the window is cut at its edge (live, one 2 px
        # over was refused and cost a round); `area` says what was captured.
        width, height = min(width, target.width - x), min(height, target.height - y)
        if area is not None:
            area.append((x, y, width, height))
        wide, high, _ = zoom_size(width, height)
        if seen:
            steps = _from_screen(target.x + x, target.y + y, width, height, out, f"{wide}x{high}!")
        else:
            crop = f"{width}x{height}+{x}+{y}"
            steps = [
                [
                    "import",
                    "-window",
                    target.id,
                    "-crop",
                    crop,
                    "+repage",
                    "-resize",
                    f"{wide}x{high}!",
                    f"png:{out}",
                ]
            ]
    elif target is not None and seen:

        def shown(window: Window) -> bool:
            return "IsViewable" in run(["xwininfo", "-id", window.id], env)[1]

        left, top, right, bottom = _with_what_is_over(
            target, parse_windows(tree, named=False), shown
        )
        wide, high = right - left, bottom - top
        steps = _from_screen(left, top, wide, high, out, f"{wide}x{high}!")
        steps.append(["convert", str(out), "-resize", f"{MAX_SIDE}x{MAX_SIDE}>", str(out)])
        if area is not None:
            area.append((left - target.x, top - target.y, wide, high))
    else:
        steps = [_window_argv(target.id, out)] if target is not None else screen_argv(out, env)
    for argv in steps:
        code, _ = run(argv, env)
        if code != 0 or not out.is_file():
            what = "the window" if target is not None else "the screen"
            return f"error: {what} could not be captured ({argv[0]} exited {code})"
    return target


def _with_what_is_over(
    target: Window, windows: Sequence[Window], shown: Callable[[Window], bool]
) -> tuple[int, int, int, int]:
    """The screen area (left, top, right, bottom) of `target` and every window
    stacked over it (listed before it) that touches it and is `shown`: an open
    menu is a window of its own, and a tall one starts above the window it
    belongs to. A closed menu is often kept, unmapped, and still listed."""
    left, top = target.x, target.y
    right, bottom = target.x + target.width, target.y + target.height
    for window in itertools.takewhile(lambda w: w.id != target.id, windows):
        touches = (
            window.x < target.x + target.width
            and target.x < window.x + window.width
            and window.y < target.y + target.height
            and target.y < window.y + window.height
        )
        if touches and shown(window):
            left, top = min(left, window.x), min(top, window.y)
            right = max(right, window.x + window.width)
            bottom = max(bottom, window.y + window.height)
    return max(left, 0), max(top, 0), right, bottom


def _from_screen(x: int, y: int, width: int, height: int, out: Path, size: str) -> list[list[str]]:
    """Capture a part of the screen through the compositor, then scale it to
    `size` (the compositor captures at the output's scale, 2x on a HiDPI one)."""
    grab = ["grim", "-t", "png", "-g", f"{x},{y} {width}x{height}", str(out)]
    return [grab, ["convert", str(out), "-resize", size, str(out)]]


def _window_argv(window_id: str, out: Path) -> list[str]:
    fit = f"{MAX_SIDE}x{MAX_SIDE}>"
    return ["import", "-window", window_id, "-resize", fit, f"png:{out}"]


def screen_argv(
    out: Path, env: Mapping[str, str], which: Callable[[str], str | None] = shutil.which
) -> list[list[str]]:
    """The commands that capture the whole screen into `out`, scaled to fit.

    Under Wayland the X root window is Xwayland's, which `import` cannot read,
    so the compositor's own capture (`grim`) is used when it is installed and
    ImageMagick scales the result; on plain X11 `import` reads the root."""
    fit = f"{MAX_SIDE}x{MAX_SIDE}>"
    if env.get("WAYLAND_DISPLAY") and which("grim"):
        return [["grim", "-t", "png", str(out)], ["convert", str(out), "-resize", fit, str(out)]]
    return [_window_argv("root", out)]
