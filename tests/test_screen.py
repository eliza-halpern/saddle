"""The `screenshot` tool: the person's screen, shown to a model that can see.

In a live setup run the model could name an error dialog only by its title,
and a game window drew its splash screen where only the person could see it.
The contract: with full access, a display, the capture tools and a model
that reads images, the Edit lane is offered `screenshot`; it captures one
window (by id or part of its title) or the whole screen and shows it through
the image path `read_file` uses. Without any one of those it is not offered.
"""

from __future__ import annotations

import struct
import zlib
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

import pytest

from saddle import screen, tools
from saddle.screen import Window, available, capture, choose, parse_windows
from saddle.tools import (
    SCREENSHOT_TOOL,
    ToolContext,
    offer_screenshot,
    tools_for_mode,
)
from saddle.vllm import VllmError

TREE = """
xwininfo: Window id: 0x5c5 (the root window) (has no name)

  Root window id: 0x5c5 (the root window) (has no name)
  Parent window id: 0x0 (none)
     4 children:
     0xc0000a "Critical Error": ("hp.exe" "hp.exe")  277x141+501+302  +501+302
     0xc00004 "Harry Potter": ("hp.exe" "hp.exe")  640x480+4+30  +4+30
        1 child:
        0xc00009 "DXGI device window": ("hp.exe" "hp.exe")  113x2+3+29  +3+29
     0xc00003 (has no name): ("hp.exe" "hp.exe")  1x1+0+0  +0+0
     0xc00002 "Default IME": ("hp.exe" "hp.exe")  1x1+0+0  +0+0
     0x1a00007 "Harry Potter (Running)": ("hp.exe" "hp.exe")  504x222+4+36  +4+36
"""


def _png(width: int = 4, height: int = 3) -> bytes:
    def chunk(kind: bytes, body: bytes) -> bytes:
        return (
            struct.pack(">I", len(body)) + kind + body + struct.pack(">I", zlib.crc32(kind + body))
        )

    raw = b"".join(b"\x00" + b"\x00\x00\x00" * width for _ in range(height))
    header = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)
    return (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", header)
        + chunk(b"IDAT", zlib.compress(raw))
        + chunk(b"IEND", b"")
    )


class FakeX:
    """xwininfo answers with `tree`; import writes a PNG (or fails)."""

    def __init__(self, tree: str = TREE, *, tree_code: int = 0, shot_code: int = 0) -> None:
        self.tree, self.tree_code, self.shot_code = tree, tree_code, shot_code
        self.calls: list[list[str]] = []

    def __call__(self, argv: Sequence[str], env: Mapping[str, str]) -> tuple[int, str]:
        self.calls.append(list(argv))
        if argv[0] == "xwininfo":
            return self.tree_code, self.tree
        if self.shot_code == 0 and argv[0] in ("import", "grim"):
            Path(argv[-1].removeprefix("png:")).write_bytes(_png())
        return self.shot_code, ""


# -- pure parts --------------------------------------------------------------------


def test_the_window_list_keeps_named_windows_a_person_could_see() -> None:
    found = parse_windows(TREE)
    assert [w.title for w in found] == ["Critical Error", "Harry Potter", "Harry Potter (Running)"]
    assert found[1] == Window("0xc00004", "Harry Potter", 640, 480)


def test_a_window_is_chosen_by_id_or_by_the_one_title_that_contains_the_words() -> None:
    windows = parse_windows(TREE)
    assert choose(windows, "0xC00004") == windows[1]
    assert choose(windows, "critical") == windows[0]
    both = choose(windows, "harry potter")
    assert isinstance(both, str)
    assert both.startswith("error: 2 windows match 'harry potter'; name one by id:")
    none = choose(windows, "steam")
    assert isinstance(none, str)
    assert none.startswith("error: no window matches 'steam'; the windows are: 0xc0000a")
    assert choose([], "x") == "error: no window matches 'x'; the windows are: none"


def test_the_tool_needs_a_display_and_both_capture_tools() -> None:
    def has(*tools_found: str) -> Any:
        return lambda name: f"/usr/bin/{name}" if name in tools_found else None

    assert available({"DISPLAY": ":1"}, has("xwininfo", "import"))
    assert not available({}, has("xwininfo", "import"))
    assert not available({"DISPLAY": ""}, has("xwininfo", "import"))
    assert not available({"DISPLAY": ":1"}, has("xwininfo"))


# -- capture -------------------------------------------------------------------------


def test_a_named_window_is_captured_scaled_to_fit(tmp_path: Path) -> None:
    x = FakeX()
    out = tmp_path / "s.png"
    got = capture("critical", out, {"DISPLAY": ":1"}, x)
    assert got == Window("0xc0000a", "Critical Error", 277, 141)
    assert x.calls[-1] == ["import", "-window", "0xc0000a", "-resize", "1600x1600>", f"png:{out}"]
    assert out.read_bytes().startswith(b"\x89PNG")


def test_the_whole_screen_uses_the_compositors_capture_under_wayland(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Xwayland's root window cannot be read by `import` (found live)."""
    out = tmp_path / "s.png"
    has_grim = {"WAYLAND_DISPLAY": "wayland-0", "DISPLAY": ":1"}
    assert screen.screen_argv(out, has_grim, lambda name: "/usr/bin/grim") == [
        ["grim", "-t", "png", str(out)],
        ["convert", str(out), "-resize", "1600x1600>", str(out)],
    ]
    no_grim = screen.screen_argv(out, has_grim, lambda name: None)
    assert no_grim == [["import", "-window", "root", "-resize", "1600x1600>", f"png:{out}"]]
    assert screen.screen_argv(out, {"DISPLAY": ":1"}, lambda name: "/usr/bin/grim") == no_grim
    x = FakeX()
    env = {"WAYLAND_DISPLAY": "wayland-0"}
    monkeypatch.setattr("saddle.screen.shutil.which", lambda name: f"/usr/bin/{name}")
    assert capture("", out, env, x) is None
    assert [c[0] for c in x.calls] == ["xwininfo", "grim", "convert"]


def test_no_window_is_the_whole_screen_and_list_is_the_window_list(tmp_path: Path) -> None:
    x = FakeX()
    assert capture("", tmp_path / "s.png", {}, x) is None
    assert x.calls[-1][:3] == ["import", "-window", "root"]
    listed = capture(" List ", tmp_path / "l.png", {}, x)
    assert isinstance(listed, str)
    assert listed.splitlines()[1] == '0xc0000a "Critical Error" 277x141'
    assert capture("list", tmp_path / "e.png", {}, FakeX("")) == (
        "no X11 windows are open (native Wayland windows are not listed)"
    )


def test_a_capture_that_fails_says_so_and_never_shows_a_stale_file(tmp_path: Path) -> None:
    assert capture("", tmp_path / "a.png", {}, FakeX(tree_code=1)) == (
        "error: the window list could not be read (xwininfo failed)"
    )
    assert capture("critical", tmp_path / "b.png", {}, FakeX(shot_code=1)) == (
        "error: the window could not be captured (import exited 1)"
    )
    assert capture("", tmp_path / "c.png", {}, FakeX(shot_code=1)) == (
        "error: the screen could not be captured (import exited 1)"
    )
    missing = capture("steam", tmp_path / "d.png", {}, FakeX())
    assert isinstance(missing, str)
    assert missing.startswith("error: no window")


def test_the_real_runner_returns_the_exit_code_and_stdout() -> None:
    assert screen.run_x(["sh", "-c", "echo hi; exit 3"], {"PATH": "/usr/bin:/bin"}) == (3, "hi\n")


# -- offered only when it can work ---------------------------------------------------


@pytest.fixture
def display(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(tools, "desktop_env", lambda: {"DISPLAY": ":1"})
    monkeypatch.setattr("saddle.screen.shutil.which", lambda name: f"/usr/bin/{name}")


def _ctx(tmp_path: Path, *, full: bool = True) -> ToolContext:
    ctx = ToolContext(workdir=tmp_path, full_access=full)
    ctx.allowed = tuple(t["function"]["name"] for t in tools_for_mode("edit"))
    return ctx


def _names(found: list[dict[str, Any]]) -> list[str]:
    return [t["function"]["name"] for t in found]


@pytest.mark.usefixtures("display")
def test_full_access_a_display_and_sight_offer_the_tool(tmp_path: Path) -> None:
    ctx = _ctx(tmp_path)
    offered = offer_screenshot(tools_for_mode("edit"), ctx, lambda: True)
    assert _names(offered)[-1] == SCREENSHOT_TOOL
    assert ctx.allowed is not None
    assert SCREENSHOT_TOOL in ctx.allowed
    unrestricted = ToolContext(workdir=tmp_path, full_access=True)
    offer_screenshot(tools_for_mode("edit"), unrestricted, lambda: True)
    assert unrestricted.allowed is None  # no list: every tool was already allowed


@pytest.mark.usefixtures("display")
def test_missing_any_condition_offers_nothing(tmp_path: Path) -> None:
    edit = tools_for_mode("edit")

    def blind() -> bool:
        message = "probe failed"
        raise VllmError(message)

    for ctx, offered_tools, sees in [
        (_ctx(tmp_path, full=False), edit, lambda: True),  # sandboxed: no display
        (_ctx(tmp_path), tools_for_mode("ask"), lambda: True),  # Ask lane
        (_ctx(tmp_path), edit, lambda: False),  # the model cannot see
        (_ctx(tmp_path), edit, blind),  # unknown is not a promise
    ]:
        assert SCREENSHOT_TOOL not in _names(offer_screenshot(offered_tools, ctx, sees))
        assert ctx.allowed is not None
        assert SCREENSHOT_TOOL not in ctx.allowed


def test_no_display_offers_nothing(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(tools, "desktop_env", dict)
    ctx = _ctx(tmp_path)
    assert SCREENSHOT_TOOL not in _names(
        offer_screenshot(tools_for_mode("edit"), ctx, lambda: True)
    )


# -- the tool call -------------------------------------------------------------------


def test_a_screenshot_is_shown_to_the_model_like_an_image_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    x = FakeX()
    monkeypatch.setattr(screen, "run_x", x)
    monkeypatch.setattr(tools, "desktop_env", lambda: {"DISPLAY": ":1"})
    ctx = _ctx(tmp_path)
    ctx.allowed = (*(ctx.allowed or ()), SCREENSHOT_TOOL)
    ctx.accepts_images = lambda: True
    ctx.call_id = "c1"
    result = tools._screenshot(ctx, {"window": "critical"})
    assert result.startswith('screenshot of 0xc0000a "Critical Error" 277x141: PNG image, 4x3')
    assert result.endswith("The image follows in the next message.")
    ((call_id, _name, url),) = ctx.attachments
    assert (call_id, url[:22]) == ("c1", "data:image/png;base64,")
    whole = tools._screenshot(ctx, {})
    assert whole.startswith("screenshot of the screen: PNG image")
    assert x.calls[-1][2] == "root"


def test_a_screenshot_call_that_cannot_be_shown_says_why(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ctx = _ctx(tmp_path)
    assert tools._screenshot(ctx, {"window": 3}).startswith("error: window must be a string")
    monkeypatch.setattr(screen, "run_x", FakeX(tree_code=1))
    assert tools._screenshot(ctx, {}).startswith("error: the window list could not be read")

    def junk(wanted: str, out: Path, env: Mapping[str, str]) -> None:
        out.write_text("not a picture")  # what a broken import could leave

    monkeypatch.setattr(screen, "capture", junk)
    assert tools._screenshot(ctx, {}) == "error: the capture was not an image"


def test_the_x_tools_get_the_desktop_variables(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(tools, "desktop_env", lambda: {"DISPLAY": ":1"})
    monkeypatch.setenv("XAUTHORITY", "/run/user/1000/xauth")
    env = tools._screen_env()
    assert env["DISPLAY"] == ":1"
    assert env["XAUTHORITY"] == "/run/user/1000/xauth"
