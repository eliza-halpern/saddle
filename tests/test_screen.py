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
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path
from typing import Any, Final

import pytest

from saddle import screen, tools
from saddle.procs import ProcessLedger
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
    assert found[1] == Window("0xc00004", "Harry Potter", 640, 480, 4, 30)


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
    assert got == Window("0xc0000a", "Critical Error", 277, 141, 501, 302)
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


@pytest.mark.usefixtures("display")
def test_with_screenshot_offered_no_tool_says_to_ask_the_person_what_is_on_screen(
    tmp_path: Path,
) -> None:
    """Live 9b: run_command said "ask them what is on their screen" while
    screenshot said "instead of asking the person". Known-bad: both texts in
    one request. Known-good: with screenshot offered, run_command sends only
    what a picture cannot show to the person; without it, nothing changes."""
    ctx = _ctx(tmp_path)
    ctx.processes = ProcessLedger()  # a chat session: its facts are stated
    stated = tools.state_session_facts(tools_for_mode("edit", processes=True), ctx, "edit")

    def run_text(found: list[dict[str, Any]]) -> str:
        return str(
            next(t for t in found if t["function"]["name"] == "run_command")["function"][
                "description"
            ]
        )

    assert tools.FACT_ASK_THEM in run_text(stated)
    seeing = run_text(offer_screenshot(stated, ctx, lambda: True))
    assert tools.FACT_ASK_THEM not in seeing
    assert tools.FACT_ASK_THEM_SEEING in seeing
    blind = run_text(offer_screenshot(stated, ctx, lambda: False))
    assert tools.FACT_ASK_THEM in blind


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

    def junk(wanted: str, out: Path, env: Mapping[str, str], **_: object) -> None:
        out.write_text("not a picture")  # what a broken import could leave

    monkeypatch.setattr(screen, "capture", junk)
    assert tools._screenshot(ctx, {}) == "error: the capture was not an image"


def test_the_x_tools_get_the_desktop_variables(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(tools, "desktop_env", lambda: {"DISPLAY": ":1"})
    monkeypatch.setenv("XAUTHORITY", "/run/user/1000/xauth")
    env = tools._screen_env()
    assert env["DISPLAY"] == ":1"
    assert env["XAUTHORITY"] == "/run/user/1000/xauth"


def test_the_x_tools_read_titles_as_utf8(monkeypatch: pytest.MonkeyPatch) -> None:
    """Live: without a locale, a title holding an em dash came back as
    "(failure in conversion from UTF8_STRING to ANSI_X3.4-1968)"."""
    monkeypatch.setattr(tools, "desktop_env", lambda: {"DISPLAY": ":1"})
    monkeypatch.delenv("LC_ALL", raising=False)
    assert tools._screen_env()["LC_ALL"] == "C.UTF-8"


# -- zoom: part of a window, enlarged ------------------------------------------------


def test_a_zoom_crops_the_window_and_enlarges_it(tmp_path: Path) -> None:
    """Live: a 1600x900 picture left "O" and "o" indistinguishable. Known-good:
    the region is cut from the window at full size and scaled up to ZOOM_MAX."""
    calls: list[list[str]] = []

    def run(argv: Sequence[str], env: Mapping[str, str]) -> tuple[int, str]:
        calls.append(list(argv))
        if argv[0] == "import":
            Path(argv[-1].removeprefix("png:")).write_bytes(b"png")
        return 0, TREE

    out = tmp_path / "z.png"
    got = screen.capture("critical", out, {}, run, region=(100, 50, 80, 40))
    assert isinstance(got, Window)
    assert calls[-1] == [
        "import", "-window", got.id, "-crop", "80x40+100+50", "+repage",
        "-resize", "320x160!", f"png:{out}",
    ]  # fmt: skip
    assert screen.zoom_size(1000, 10) == (1600, 16, 1.6)  # a wide strip stays within MAX_SIDE


@pytest.mark.parametrize(
    ("wanted", "region", "refusal"),
    [
        ("", (0, 0, 10, 10), "error: a region is part of a window"),
        ("critical", (-1, 0, 10, 10), "error: a region needs x, y of 0 or more"),
        ("critical", (0, 0, 0, 10), "error: a region needs x, y of 0 or more"),
        ("critical", (0, 0, 10, 0), "error: a region needs x, y of 0 or more"),
        ("critical", (0, -1, 10, 10), "error: a region needs x, y of 0 or more"),
        ("critical", (267, 0, 11, 10), "error: the region (267, 0, 11x10) goes past"),
        ("critical", (0, 136, 10, 6), "error: the region (0, 136, 10x6) goes past"),
    ],
)
def test_a_zoom_outside_its_window_is_refused(
    tmp_path: Path, wanted: str, region: tuple[int, int, int, int], refusal: str
) -> None:
    def run(argv: Sequence[str], env: Mapping[str, str]) -> tuple[int, str]:
        return 0, TREE

    said = screen.capture(wanted, tmp_path / "z.png", {}, run, region=region)
    assert isinstance(said, str)
    assert said.startswith(refusal)
    inside = screen.capture("critical", tmp_path / "z.png", {}, run, region=(267, 131, 10, 10))
    assert inside == "error: the window could not be captured (import exited 0)"  # it got as far


# -- a window on top is taken from the screen, menus included --------------------------


def _recorder(calls: list[list[str]]) -> screen.Run:
    def run(argv: Sequence[str], env: Mapping[str, str]) -> tuple[int, str]:
        calls.append(list(argv))
        if argv[0] in ("grim", "import", "convert"):
            Path(argv[-1].removeprefix("png:")).write_bytes(b"png")
        return 0, TREE

    return run


WAYLAND: Final = {"WAYLAND_DISPLAY": "wayland-0"}


def _grim(name: str) -> str | None:
    return f"/usr/bin/{name}"


def test_a_window_on_top_is_taken_from_the_screen_at_its_own_size(tmp_path: Path) -> None:
    """Live: a menu is a window of its own, so the Writer window's picture showed
    "Format" highlighted and no menu. Known-good: the window's place on the
    screen is captured (what the person sees there), scaled to its own pixels."""
    calls: list[list[str]] = []
    out = tmp_path / "w.png"
    got = capture("critical", out, WAYLAND, _recorder(calls), on_top=lambda w: True, which=_grim)
    assert isinstance(got, Window)
    assert calls[1:] == [
        ["grim", "-t", "png", "-g", "501,302 277x141", str(out)],
        ["convert", str(out), "-resize", "277x141!", str(out)],
        ["convert", str(out), "-resize", "1600x1600>", str(out)],
    ]


def test_a_zoom_on_top_is_cut_from_the_screen(tmp_path: Path) -> None:
    calls: list[list[str]] = []
    out = tmp_path / "z.png"
    region = (100, 50, 80, 40)
    capture("critical", out, WAYLAND, _recorder(calls), region, lambda w: True, _grim)
    assert calls[1:] == [
        ["grim", "-t", "png", "-g", "601,352 80x40", str(out)],
        ["convert", str(out), "-resize", "320x160!", str(out)],
    ]


@pytest.mark.parametrize(
    ("env", "on_top", "which"),
    [
        (WAYLAND, False, _grim),  # covered: the screen there shows the cover
        ({}, True, _grim),  # plain X11: no compositor capture
        (WAYLAND, True, lambda name: None),  # grim not installed
    ],
)
def test_otherwise_the_window_is_read_from_x(
    tmp_path: Path,
    env: dict[str, str],
    on_top: bool,
    which: Callable[[str], str | None],
) -> None:
    calls: list[list[str]] = []
    out = tmp_path / "w.png"
    capture("critical", out, env, _recorder(calls), on_top=lambda w: on_top, which=which)
    assert [c[0] for c in calls[1:]] == ["import"]


def test_without_xdotool_no_window_counts_as_on_top(monkeypatch: pytest.MonkeyPatch) -> None:
    def missing(argv: Sequence[str], env: Mapping[str, str]) -> tuple[int, str]:
        raise FileNotFoundError(argv[0])

    monkeypatch.setattr(screen, "run_x", missing)
    assert tools._on_top({})(Window("0x10", "x", 20, 20)) is False


def test_a_framed_window_s_place_is_where_it_is_on_the_screen() -> None:
    """A window manager that frames windows puts the program's window inside its
    frame: xwininfo's first place is within the frame, the second on the screen,
    and a capture of the window's place on the screen needs the second."""
    tree = '     0x1e00007 "Notes": ("notes" "Notes")  800x600+0+24  +300+224\n'
    assert parse_windows(tree) == [Window("0x1e00007", "Notes", 800, 600, 300, 224)]
