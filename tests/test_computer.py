"""The `computer` tool: act on a window the model sees with `screenshot`.

In a live run the model saw a game's first-run "Video Configuration" dialog
through `screenshot` but could not press "Start Game" or raise the game's
window. The contract: with the Edit lane, full access, `screenshot` offered,
an X display and xdotool, `computer` is offered; without any one of those it
is not. A window owned by one of the session's processes is acted on without
asking; any other window is put to the person first, and left alone when they
decline or nobody has the page open. Every action that ran returns a fresh
picture of the window.

No test here reaches the real display: every X program runs through a fake
`screen.run_x` that records its argv.
"""

from __future__ import annotations

import json
import re
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any, Final, cast

import pytest
from test_screen import _png

from saddle import computer, engine, screen, tools
from saddle.computer import Action
from saddle.engine import TurnOptions, run_turn
from saddle.procs import ProcessLedger
from saddle.research import NOBODY_WATCHING
from saddle.screen import Window
from saddle.tools import (
    COMPUTER_TITLE,
    COMPUTER_TOOL,
    FACT_ASK_THEM_ACTING,
    FACT_ASK_THEM_SEEING,
    SCREENSHOT_TOOL,
    ToolContext,
    execute_tool,
    offer_computer,
    offer_screenshot,
    tools_for_mode,
)
from saddle.vllm import StreamToken, ToolCall, VllmClient

TREE = """
  Root window id: 0x5c5 (the root window) (has no name)
     4 children:
     0x1200005 "Video Configuration": ("game.exe" "game.exe")  400x300+100+100  +100+100
     0x1200009 "Game": ("game.exe" "game.exe")  640x480+0+0  +0+0
     0x1400002 "Notes - Editor": ("editor" "Editor")  1200x800+0+0  +0+0
     0x1400003 "Mail - Editor": ("editor" "Editor")  900x700+0+0  +0+0
"""

OWN = 4242
OWNERS = {"18874373": OWN, "18874377": OWN, "20971522": 777}
"""Window (decimal id, as xdotool is given it) -> its _NET_WM_PID; the mail
window has none."""


QUERIES: Final = ("getwindowpid", "getactivewindow", "getwindowgeometry", "getmouselocation")


class FakeDesktop:
    """xwininfo lists `TREE`; getwindowpid answers from `OWNERS`; import writes
    a PNG; windowactivate moves the focus (unless `focus_sticks` is False, a
    compositor that keeps it), mousemove the pointer (unless `pointer_moves` is
    False, as Xwayland ignored a move for a window that was not active); every
    other xdotool call exits `act_code` (or per subcommand)."""

    def __init__(
        self,
        act_codes: Mapping[str, int] | None = None,
        *,
        tree_code: int = 0,
        focus_sticks: bool = True,
        pointer_moves: bool = True,
    ) -> None:
        self.act_codes = dict(act_codes or {})
        self.tree_code = tree_code
        self.focus_sticks, self.pointer_moves = focus_sticks, pointer_moves
        self.display_code = 0
        self.active, self.pointer = "1", (715, 438)
        self.calls: list[list[str]] = []

    def _origin(self, xid: str) -> tuple[int, int]:
        for window in re.finditer(r"(0x[0-9a-f]+) .*?\d+x\d+\+(-?\d+)\+(-?\d+)", TREE):
            if str(int(window[1], 16)) == xid:
                return int(window[2]), int(window[3])
        return 0, 0

    def __call__(self, argv: Sequence[str], env: Mapping[str, str]) -> tuple[int, str]:
        self.calls.append(list(argv))
        if argv[0] == "xwininfo":
            return self.tree_code, TREE
        if argv[0] in ("import", "grim", "convert"):
            Path(argv[-1].removeprefix("png:")).write_bytes(_png())
            return 0, ""
        verb = argv[1]
        if verb == "getwindowpid":
            pid = OWNERS.get(argv[2])
            return (0, f"{pid}\n") if pid is not None else (1, "")
        if verb == "getactivewindow":
            return 0, f"{self.active}\n"
        if verb == "getwindowgeometry":
            x, y = self._origin(argv[3])
            return 0, f"WINDOW={argv[3]}\nX={x}\nY={y}\nWIDTH=1\nHEIGHT=1\nSCREEN=0\n"
        if verb == "getdisplaygeometry":
            return self.display_code, "1280 720\n"
        if verb == "getmouselocation":
            return 0, f"X={self.pointer[0]}\nY={self.pointer[1]}\nSCREEN=0\nWINDOW=1\n"
        code = max(self.act_codes.get(part, 0) for part in argv[1:])  # a chain fails as a whole
        if verb == "windowactivate" and code == 0 and self.focus_sticks:
            self.active = argv[2]
        if verb == "mousemove" and code == 0 and self.pointer_moves:
            if argv[2] == "--":  # the screen's own place
                self.pointer = (int(argv[3]), int(argv[4]))
            else:
                x, y = self._origin(argv[3])
                self.pointer = (x + int(argv[4]), y + int(argv[5]))
        return code, ""

    @property
    def actions(self) -> list[list[str]]:
        """The xdotool calls that change something (not the lookups)."""
        return [c for c in self.calls if c[0] == "xdotool" and c[1] not in QUERIES]


class Asker:
    """`ToolContext.approve`: records each question, answers `yes`."""

    def __init__(self, *, yes: bool) -> None:
        self.yes = yes
        self.asked: list[tuple[str, list[str]]] = []

    def __call__(self, title: str, lines: list[str]) -> bool:
        self.asked.append((title, lines))
        return self.yes


@pytest.fixture
def desktop(monkeypatch: pytest.MonkeyPatch) -> FakeDesktop:
    fake = FakeDesktop()
    monkeypatch.setattr(screen, "run_x", fake)
    monkeypatch.setattr("saddle.tools.time.sleep", lambda s: None)  # AFTER_SETTLE_S
    monkeypatch.setattr(tools, "desktop_env", lambda: {"DISPLAY": ":1"})
    return fake


def _ctx(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, approve: Asker | None = None
) -> ToolContext:
    ledger = ProcessLedger()
    monkeypatch.setattr(ledger, "pids", lambda: frozenset({OWN, 1}))
    ctx = ToolContext(workdir=tmp_path, full_access=True, processes=ledger, approve=approve)
    ctx.accepts_images = lambda: True
    ctx.call_id = "c1"
    return ctx


def act(ctx: ToolContext, **arguments: Any) -> str:
    return execute_tool(
        ToolCall(id="c1", name=COMPUTER_TOOL, arguments=json.dumps(arguments)),
        workdir=ctx.workdir,
        context=ctx,
    )


# -- offered only when it can work and is allowed ------------------------------------


@pytest.fixture
def xdotool(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(tools, "desktop_env", lambda: {"DISPLAY": ":1"})
    monkeypatch.setattr("saddle.screen.shutil.which", lambda name: f"/usr/bin/{name}")
    monkeypatch.setattr("saddle.computer.shutil.which", lambda name: f"/usr/bin/{name}")


def _names(found: list[dict[str, Any]]) -> list[str]:
    return [t["function"]["name"] for t in found]


def _seeing(ctx: ToolContext, mode: str = "edit") -> list[dict[str, Any]]:
    """The turn's tools after the screenshot offer, as the engine builds them."""
    ctx.processes = ctx.processes or ProcessLedger()
    offered = tools_for_mode(mode, processes=True)
    ctx.allowed = tuple(_names(offered))
    stated = tools.state_session_facts(offered, ctx, mode)
    return offer_screenshot(stated, ctx, lambda: True)


def _run_text(found: list[dict[str, Any]]) -> str:
    return str(
        next(t for t in found if t["function"]["name"] == "run_command")["function"]["description"]
    )


@pytest.mark.usefixtures("xdotool")
def test_every_condition_met_offers_the_tool_and_run_command_says_it_can_act(
    tmp_path: Path,
) -> None:
    ctx = ToolContext(workdir=tmp_path, full_access=True)
    seeing = _seeing(ctx)
    assert FACT_ASK_THEM_SEEING in _run_text(seeing)
    offered = offer_computer(seeing, ctx)
    assert _names(offered)[-1] == COMPUTER_TOOL
    assert ctx.allowed is not None
    assert COMPUTER_TOOL in ctx.allowed
    text = _run_text(offered)
    assert FACT_ASK_THEM_ACTING in text
    assert FACT_ASK_THEM_SEEING not in text  # never two opposite instructions
    unrestricted = ToolContext(workdir=tmp_path, full_access=True)
    offer_computer(seeing, unrestricted)
    assert unrestricted.allowed is None  # no list: every tool was already allowed


@pytest.mark.usefixtures("xdotool")
def test_missing_any_condition_offers_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    full = ToolContext(workdir=tmp_path, full_access=True)
    seeing = _seeing(full)
    without_screenshot = [t for t in seeing if t["function"]["name"] != SCREENSHOT_TOOL]
    without_run = [t for t in seeing if t["function"]["name"] != "run_command"]
    sandboxed = ToolContext(workdir=tmp_path, full_access=False)
    for ctx, offered in [
        (ToolContext(workdir=tmp_path, full_access=True), without_screenshot),  # cannot see
        (ToolContext(workdir=tmp_path, full_access=True), without_run),  # not the Edit lane
        (sandboxed, seeing),  # no full access
    ]:
        ctx.allowed = ()
        assert COMPUTER_TOOL not in _names(offer_computer(offered, ctx))
        assert ctx.allowed == ()
    ask = ToolContext(workdir=tmp_path, full_access=True)
    assert COMPUTER_TOOL not in _names(offer_computer(_seeing(ask, "ask"), ask))
    monkeypatch.setattr("saddle.computer.shutil.which", lambda name: None)  # no xdotool
    full.allowed = ()
    assert COMPUTER_TOOL not in _names(offer_computer(seeing, full))
    monkeypatch.setattr("saddle.computer.shutil.which", lambda name: f"/usr/bin/{name}")
    monkeypatch.setattr(tools, "desktop_env", dict)  # no display
    assert COMPUTER_TOOL not in _names(offer_computer(seeing, full))
    assert full.allowed == ()


def test_the_tool_needs_a_display_and_xdotool() -> None:
    assert computer.available({"DISPLAY": ":1"}, lambda name: f"/usr/bin/{name}")
    assert not computer.available({"DISPLAY": ""}, lambda name: f"/usr/bin/{name}")
    assert not computer.available({"DISPLAY": ":1"}, lambda name: None)


class FakeClient:
    def __init__(self) -> None:
        self.asked: list[dict[str, Any]] = []

    def stream_chat(self, messages: Any, **kwargs: Any) -> Any:
        self.asked.append(dict(kwargs))
        return iter([StreamToken(stream="content", text="ok")])


@pytest.mark.usefixtures("xdotool")
def test_a_chat_turn_offers_it_and_a_task_runs_tool_list_is_unchanged(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(engine, "server_accepts_images", lambda client: True)
    edit = tools_for_mode("edit")
    chat = ToolContext(workdir=tmp_path, full_access=True, images=True)
    client = FakeClient()
    options = TurnOptions(workdir=tmp_path, journal=tmp_path / "j.jsonl", tools=edit)
    list(run_turn(cast(VllmClient, client), [], "hi", options, turn=1, context=chat))
    assert _names(client.asked[0]["tools"])[-2:] == [SCREENSHOT_TOOL, COMPUTER_TOOL]
    task = ToolContext(workdir=tmp_path)  # a task run: no images, no full access
    client = FakeClient()
    task_options = TurnOptions(workdir=tmp_path, journal=tmp_path / "t.jsonl")
    list(run_turn(cast(VllmClient, client), [], "hi", task_options, turn=1, context=task))
    assert client.asked[0]["tools"] == list(tools.TOOLS)


# -- the session's own windows -------------------------------------------------------


ACTIVATE = ["xdotool", "windowactivate", "18874373"]


def _move(x: int, y: int) -> list[str]:
    return ["xdotool", "mousemove", "--window", "18874373", str(x), str(y)]


@pytest.mark.parametrize(
    ("arguments", "argv"),
    [
        (
            {"action": "focus"},
            [ACTIVATE, ["xdotool", "windowfocus", "18874373"]],
        ),
        (
            {"action": "click", "x": 210, "y": 270},
            [ACTIVATE, _move(210, 270), ["xdotool", "sleep", "0.5", "click", "1"]],
        ),
        (
            {"action": "click", "x": "5", "y": 6, "button": "right", "double": True},
            [ACTIVATE, _move(5, 6), ["xdotool", "sleep", "0.5", "click", "--repeat", "2", "3"]],
        ),
        (
            {"action": "key", "keys": "alt+Return"},
            [ACTIVATE, ["xdotool", "key", "--clearmodifiers", "alt+Return"]],
        ),
        (
            {"action": "type", "text": "-n ok"},
            [ACTIVATE, ["xdotool", "type", "--clearmodifiers", "--", "-n ok"]],
        ),
        (
            {"action": "scroll"},
            [ACTIVATE, _move(200, 150), ["xdotool", "sleep", "0.5", "click", "--repeat", "3", "5"]],
        ),
        (
            {"action": "scroll", "direction": "up", "amount": 2, "x": 1, "y": 2},
            [ACTIVATE, _move(1, 2), ["xdotool", "sleep", "0.5", "click", "--repeat", "2", "4"]],
        ),
    ],
)
def test_an_own_window_is_acted_on_without_asking(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    desktop: FakeDesktop,
    arguments: dict[str, Any],
    argv: list[list[str]],
) -> None:
    asker = Asker(yes=False)
    ctx = _ctx(tmp_path, monkeypatch, asker)
    result = act(ctx, window="video", **arguments)
    assert result.startswith("done: ")
    assert desktop.actions == argv
    assert ["xdotool", "getwindowpid", "18874373"] in desktop.calls
    assert asker.asked == []


def test_the_result_carries_a_fresh_picture_of_the_window(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, desktop: FakeDesktop
) -> None:
    ctx = _ctx(tmp_path, monkeypatch)
    result = act(ctx, window="0x1200005", action="click", x=10, y=20)
    assert result.startswith('done: left click at (10, 20) on 0x1200005 "Video Configuration"')
    assert 'screenshot of 0x1200005 "Video Configuration" 400x300 afterwards: PNG' in result
    assert result.endswith("The image follows in the next message.")
    assert desktop.calls[-1][:3] == ["import", "-window", "0x1200005"]
    assert desktop.calls.index(desktop.actions[0]) < len(desktop.calls) - 1  # after the click
    ((call_id, _name, url),) = ctx.attachments
    assert (call_id, url[:22]) == ("c1", "data:image/png;base64,")


def test_a_picture_that_cannot_be_taken_afterwards_is_said_and_the_action_still_counts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, desktop: FakeDesktop
) -> None:
    ctx = _ctx(tmp_path, monkeypatch)

    def closed(wanted: str, out: Path, env: Mapping[str, str], **_: object) -> str:
        return "error: no window matches '0x1200005'"

    monkeypatch.setattr(screen, "capture", closed)
    result = act(ctx, window="video", action="key", keys="Return")
    assert result.startswith('done: press Return on 0x1200005 "Video Configuration"')
    assert "could not be shown afterwards (error: no window matches" in result
    assert result.endswith("The action was done.")
    assert ctx.attachments == []

    def junk(wanted: str, out: Path, env: Mapping[str, str], **_: object) -> Window:
        out.write_text("not a picture")
        return Window("0x1200005", "Video Configuration", 400, 300)

    monkeypatch.setattr(screen, "capture", junk)
    assert act(ctx, window="video", action="focus").endswith(
        "could not be shown afterwards (the capture was not an image)."
    )


# -- any other window ---------------------------------------------------------------


def test_another_window_is_asked_about_and_acted_on_only_when_approved(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, desktop: FakeDesktop
) -> None:
    asker = Asker(yes=True)
    ctx = _ctx(tmp_path, monkeypatch, asker)
    result = act(ctx, window="notes", action="type", text='say "hi"\nnow')
    assert result.startswith("done: type 12 characters on 0x1400002")
    ((title, lines),) = asker.asked
    assert title == COMPUTER_TITLE
    assert lines == [
        'window: "Notes - Editor" (0x1400002), not opened by this session\'s commands',
        "action: type 12 characters",
        'text: "say \\"hi\\"\\nnow"',  # the exact text, newline and quotes visible
    ]
    assert desktop.actions == [
        ["xdotool", "windowactivate", "20971522"],
        ["xdotool", "type", "--clearmodifiers", "--", 'say "hi"'],
        ["xdotool", "key", "--clearmodifiers", "Return"],
        ["xdotool", "type", "--clearmodifiers", "--", "now"],
    ]


@pytest.mark.parametrize(
    ("asker", "watched", "why"),
    [
        (Asker(yes=False), True, "the person did not approve it"),
        (Asker(yes=False), False, NOBODY_WATCHING),
        (None, None, "the person did not approve it"),
    ],
)
def test_another_window_is_left_alone_when_declined_or_nobody_is_watching(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    desktop: FakeDesktop,
    asker: Asker | None,
    watched: bool | None,
    why: str,
) -> None:
    ctx = _ctx(tmp_path, monkeypatch, asker)
    if watched is not None:
        ctx.watched = lambda: watched
    result = act(ctx, window="mail", action="click", x=1, y=1)  # a window with no owner PID
    assert result == (
        'error: nothing was done: 0x1400003 "Mail - Editor" 900x700 is not a window this '
        f"session's commands opened, and {why}. Act on a window your commands opened, "
        "or ask the person."
    )
    assert desktop.actions == []
    if asker is not None:
        ((_title, lines),) = asker.asked
        assert lines[1] == "action: left click at (1, 1)"


def test_a_session_without_a_process_list_owns_no_window(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, desktop: FakeDesktop
) -> None:
    ctx = _ctx(tmp_path, monkeypatch, Asker(yes=False))
    ctx.processes = None
    assert act(ctx, window="video", action="focus").startswith("error: nothing was done")
    assert desktop.actions == []


# -- refusals -----------------------------------------------------------------------


def test_an_unknown_or_ambiguous_window_is_refused_naming_the_windows(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, desktop: FakeDesktop
) -> None:
    ctx = _ctx(tmp_path, monkeypatch)
    both = act(ctx, window="editor", action="focus")
    assert both.startswith("error: 2 windows match 'editor'; name one by id: 0x1400002")
    assert "0x1400003" in both
    none = act(ctx, window="steam", action="focus")
    assert none.startswith("error: no window matches 'steam'; the windows are: 0x1200005")
    assert desktop.actions == []
    monkeypatch.setattr(screen, "run_x", FakeDesktop(tree_code=1))
    assert act(ctx, window="video", action="focus") == (
        "error: the window list could not be read (xwininfo failed)"
    )


@pytest.mark.parametrize(
    ("arguments", "start"),
    [
        ({"window": "video"}, "error: action must be one of focus, click, key, type, scroll"),
        ({"window": "video", "action": "dance"}, "error: action must be one of"),
        ({"action": "focus"}, "error: computer needs a window"),
        ({"window": " ", "action": "focus"}, "error: computer needs a window"),
        ({"window": 3, "action": "focus"}, "error: computer needs a window"),
        ({"window": "video", "action": "key", "keys": "a -b"}, "error: keys must be one key"),
        ({"window": "video", "action": "key", "keys": "-window"}, "error: keys must be one key"),
        ({"window": "video", "action": "key"}, "error: keys must be one key"),
        ({"window": "video", "action": "type", "text": ""}, "error: type needs a non-empty"),
        ({"window": "video", "action": "type", "text": 5}, "error: type needs a non-empty"),
        ({"window": "video", "action": "click", "x": 3}, "error: click needs x and y"),
        ({"window": "video", "action": "click", "x": 3.5, "y": 1}, "error: x must be a whole"),
        ({"window": "video", "action": "click", "x": True, "y": 1}, "error: x must be a whole"),
        ({"window": "video", "action": "click", "x": 1, "y": "top"}, "error: y must be a whole"),
        (
            {"window": "video", "action": "click", "x": 1, "y": 1, "button": "middle"},
            "error: click takes button left or right",
        ),
        (
            {"window": "video", "action": "click", "x": 1, "y": 1, "double": "yes"},
            "error: click takes button left or right",
        ),
        (
            {"window": "video", "action": "click", "x": 400, "y": 1},
            'error: (400, 1) is outside 0x1200005 "Video Configuration" 400x300',
        ),
        ({"window": "video", "action": "click", "x": "-1", "y": 1}, "error: (-1, 1) is outside"),
        ({"window": "video", "action": "click", "x": 1, "y": 300}, "error: (1, 300) is outside"),
        ({"window": "video", "action": "scroll", "x": 1, "y": -1}, "error: (1, -1) is outside"),
        ({"window": "video", "action": "scroll", "amount": 0}, "error: scroll needs direction"),
        ({"window": "video", "action": "scroll", "amount": 21}, "error: scroll needs direction"),
        ({"window": "video", "action": "scroll", "direction": "left"}, "error: scroll needs"),
        ({"window": "video", "action": "scroll", "x": 5}, "error: scroll takes both x and y"),
        ({"window": "video", "action": "focus", "where": 1}, "error: computer does not take where"),
    ],
)
def test_bad_arguments_are_refused_and_nothing_is_done(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    desktop: FakeDesktop,
    arguments: dict[str, Any],
    start: str,
) -> None:
    result = act(_ctx(tmp_path, monkeypatch, Asker(yes=True)), **arguments)
    assert result.startswith(start), result
    assert desktop.actions == []


def test_without_full_access_nothing_is_done(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, desktop: FakeDesktop
) -> None:
    """A task run never sets full access: a `computer` call it emits does nothing."""
    ctx = _ctx(tmp_path, monkeypatch, Asker(yes=True))
    ctx.full_access = False
    assert act(ctx, window="video", action="focus").startswith(
        "error: computer acts on the person's screen only in a full-access Edit session"
    )
    assert desktop.calls == []


def test_an_action_xdotool_fails_is_reported(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ctx = _ctx(tmp_path, monkeypatch)
    monkeypatch.setattr(screen, "run_x", FakeDesktop({"click": 1}))
    assert act(ctx, window="video", action="click", x=1, y=1) == (
        'error: left click at (1, 1) on 0x1200005 "Video Configuration" 400x300 failed '
        "(xdotool exited 1)"
    )
    # A compositor that will not raise a window can still focus it.
    monkeypatch.setattr(screen, "run_x", FakeDesktop({"windowactivate": 1}))
    assert act(ctx, window="video", action="focus").startswith(
        'done: raise and focus it on 0x1200005 "Video Configuration"'
    )
    monkeypatch.setattr(screen, "run_x", FakeDesktop({"windowactivate": 1, "windowfocus": 1}))
    assert act(ctx, window="video", action="focus").startswith(
        "error: raise and focus it on 0x1200005"
    )


def test_the_pure_parts_without_a_runner_use_the_real_one(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake = FakeDesktop()
    monkeypatch.setattr(screen, "run_x", fake)
    window = computer.find("game", {})
    assert window == Window("0x1200009", "Game", 640, 480)
    assert computer.owner(window, {}) == OWN
    assert computer.owner(Window("0x1400003", "Mail - Editor", 900, 700), {}) is None
    assert computer.perform(Action("key", keys="Return"), window, {}) is None
    assert fake.actions == [
        ["xdotool", "windowactivate", "18874377"],
        ["xdotool", "key", "--clearmodifiers", "Return"],
    ]


# -- input goes where the focus and the pointer are (F43) ---------------------------


def test_a_click_the_desktop_will_not_aim_is_not_sent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Live (labwc, Xwayland), a pointer move sent before the window was active was
    ignored, and a click would have landed
    wherever the person's pointer was. Known-bad: the click is sent anyway.
    Known-good: nothing is clicked, the pointer's place is named, keys are offered."""
    blocked = FakeDesktop(pointer_moves=False)
    monkeypatch.setattr(screen, "run_x", blocked)
    said = act(_ctx(tmp_path, monkeypatch), window="video", action="click", x=10, y=20)
    assert said == computer.NO_POINTER.format(
        window='0x1200005 "Video Configuration" 400x300', where="(715, 438)"
    )
    assert not any("click" in c for c in blocked.actions)
    moved = FakeDesktop()
    monkeypatch.setattr(screen, "run_x", moved)
    assert act(_ctx(tmp_path, monkeypatch), window="video", action="click", x=10, y=20).startswith(
        "done: left click at (10, 20)"
    )
    assert moved.pointer == (110, 120)  # the window's origin (100, 100) plus the point


def test_keys_are_not_sent_unless_the_window_has_the_focus(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Known-bad: keys sent while another window (the person's mail) has the focus."""
    stuck = FakeDesktop(focus_sticks=False)
    monkeypatch.setattr(screen, "run_x", stuck)
    for arguments in ({"action": "key", "keys": "Return"}, {"action": "type", "text": "hi"}):
        said = act(_ctx(tmp_path, monkeypatch), window="video", **arguments)
        assert said == computer.NOT_FOCUSED.format(window='0x1200005 "Video Configuration" 400x300')
    assert [c for c in stuck.actions if c[1] in ("key", "type")] == []


def test_an_unreadable_pointer_place_is_not_a_click(monkeypatch: pytest.MonkeyPatch) -> None:
    class Silent(FakeDesktop):
        def __call__(self, argv: Sequence[str], env: Mapping[str, str]) -> tuple[int, str]:
            if argv[:2] == ["xdotool", "getmouselocation"]:
                self.calls.append(list(argv))
                return 1, ""
            return super().__call__(argv, env)

    silent = Silent()
    window = Window("0x1200005", "Video Configuration", 400, 300)
    said = computer.perform(Action("click", x=1, y=1), window, {}, silent)
    assert said is not None
    assert "it stayed at an unknown place" in said


def test_shell_output_keeps_the_numbers_negative_ones_included() -> None:
    assert computer._shell("X=12\nNAME=game\nY=-3\n") == {"X": 12, "Y": -3}


# -- clicks measured on a whole-screen screenshot -----------------------------------


def test_a_point_measured_on_the_whole_screen_is_placed_in_the_window(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, desktop: FakeDesktop
) -> None:
    """Live: the model aimed with whole-screen pixels (a 1600x900 picture) at a
    524x411 dialog and was refused. Known-good: space=screen scales the point to
    the display (1280x720 here) and subtracts the window's place (100, 100)."""
    ctx = _ctx(tmp_path, monkeypatch)
    ctx.screen_capture = (1600, 900)
    said = act(ctx, window="video", action="click", x=300, y=250, space="screen")
    assert said.startswith('done: left click at (140, 100) on 0x1200005 "Video Configuration"')
    assert ["xdotool", "mousemove", "--window", "18874373", "140", "100"] in desktop.actions


def test_screen_points_need_a_whole_screen_screenshot_and_a_readable_screen(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, desktop: FakeDesktop
) -> None:
    ctx = _ctx(tmp_path, monkeypatch)
    assert act(ctx, window="video", action="click", x=300, y=250, space="screen") == (
        "error: space=screen needs a whole-screen screenshot to measure on; take one "
        "(screenshot with no window), then give x, y as it shows them"
    )
    ctx.screen_capture = (1600, 900)
    assert act(ctx, window="video", action="click", x=1, y=1, space="edge").startswith(
        "error: space must be window"
    )
    desktop.display_code = 1
    assert act(ctx, window="video", action="click", x=1, y=1, space="screen").startswith(
        "error: the screen's size could not be read"
    )
    assert [c for c in desktop.actions if c[1] in ("mousemove", "click")] == []


def test_a_window_whose_place_is_unknown_is_not_clicked_from_screen_points(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class Placeless(FakeDesktop):
        def __call__(self, argv: Sequence[str], env: Mapping[str, str]) -> tuple[int, str]:
            if argv[:2] == ["xdotool", "getwindowgeometry"]:
                return 1, ""
            return super().__call__(argv, env)

    window = Window("0x1200005", "Video Configuration", 400, 300)
    said = computer.to_window(Action("click", x=5, y=5), window, (1600, 900), {}, Placeless())
    assert isinstance(said, str)
    assert said.startswith('error: where 0x1200005 "Video Configuration" 400x300 sits')
    scroll = Action("scroll")  # no point: it aims at the middle, nothing to convert
    assert computer.to_window(scroll, window, None, {}, Placeless()) == scroll


def test_a_point_outside_the_window_suggests_screen_space(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, desktop: FakeDesktop
) -> None:
    said = act(_ctx(tmp_path, monkeypatch), window="video", action="click", x=579, y=698)
    assert said.endswith("If you measured them on a whole-screen screenshot, pass space=screen")


def test_a_whole_screen_screenshot_is_remembered_for_screen_points(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, desktop: FakeDesktop
) -> None:
    ctx = _ctx(tmp_path, monkeypatch)
    tools._screenshot(ctx, {"window": "video"})
    assert ctx.screen_capture is None  # a window's picture is not the screen
    tools._screenshot(ctx, {})
    assert ctx.screen_capture == (4, 3)  # the fake capture's size


# -- drag -------------------------------------------------------------------------


def test_a_drag_presses_inside_the_window_moves_and_lets_go(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, desktop: FakeDesktop
) -> None:
    """The person asked for a model to move a window with the mouse. Known-good:
    the press point is verified inside the window, the end may be anywhere."""
    said = act(
        _ctx(tmp_path, monkeypatch), window="video", action="drag", x=50, y=10, to_x=650, to_y=210
    )
    assert said.startswith("done: drag from (50, 10) to (650, 210) on 0x1200005")
    assert desktop.actions[-1] == [
        "xdotool",
        "sleep",
        "0.5",
        "mousedown",
        "1",
        "mousemove_relative",
        "--",
        "600",
        "200",
        "sleep",
        "0.5",
        "mouseup",
        "1",
    ]


def test_a_drag_needs_both_points_and_a_start_inside_the_window(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, desktop: FakeDesktop
) -> None:
    ctx = _ctx(tmp_path, monkeypatch)
    assert act(ctx, window="video", action="drag", x=5, y=5).startswith(
        "error: drag needs x, y (where to press, inside the window) and to_x, to_y"
    )
    assert act(ctx, window="video", action="drag", x=900, y=5, to_x=0, to_y=0).startswith(
        "error: (900, 5) is outside"
    )
    assert desktop.actions == []


def test_a_drag_measured_on_the_whole_screen_converts_both_points(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, desktop: FakeDesktop
) -> None:
    ctx = _ctx(tmp_path, monkeypatch)
    ctx.screen_capture = (1600, 900)
    said = act(ctx, window="video", action="drag", x=300, y=250, to_x=500, to_y=500, space="screen")
    # (300, 250) -> (240, 200) on the 1280x720 display -> (140, 100) in the window;
    # (500, 500) -> (400, 400) -> (300, 300).
    assert said.startswith("done: drag from (140, 100) to (300, 300)")


# -- the compositor's own pointer ---------------------------------------------------


class FakePointer:
    """A compositor pointer that moves the fake desktop's pointer (unless
    `lands` is False) and records presses."""

    def __init__(self, desktop: FakeDesktop, *, lands: bool = True) -> None:
        self.desktop, self.lands = desktop, lands
        self.calls: list[tuple[str, ...]] = []
        self.closed = False

    def move(self, x: int, y: int, extent: tuple[int, int]) -> None:
        self.calls.append(("move", str(x), str(y), f"{extent[0]}x{extent[1]}"))
        if self.lands:
            self.desktop.pointer = (x, y)

    def button(self, button: str, *, pressed: bool) -> None:
        self.calls.append(("down" if pressed else "up", button))

    def close(self) -> None:
        self.closed = True


VIDEO = Window("0x1200005", "Video Configuration", 400, 300)


def test_a_click_inside_the_window_goes_through_x_even_with_a_compositor_pointer() -> None:
    """Measured on labwc with Writer: "Insert Table..." in the Table menu,
    clicked through the compositor's pointer, 0 of 9 (a glide, a slower
    glide, a re-entry, one device for both clicks); through X 3 of 3, and
    through X also 4 of 4 with the first click arriving from a native
    window and 4 of 4 on "Bold" in a submenu. Known-good: a click inside the
    window is moved and pressed by X; the compositor's pointer is not used."""
    desktop = FakeDesktop()
    real = FakePointer(desktop)
    said = computer.perform(
        Action("click", x=10, y=20, double=True),
        VIDEO,
        {},
        desktop,
        lambda env: real,
        lambda s: None,
    )
    assert said is None
    assert real.calls == [("move", "110", "120", "1280x720")]  # the cursor shown, not pressed
    assert ["xdotool", "mousemove", "--window", "18874373", "10", "20"] in desktop.actions
    assert desktop.actions[-1][-4:] == ["click", "--repeat", "2", "1"]


def test_a_drag_from_the_title_bar_moves_with_the_button_held_to_its_end() -> None:
    """A drag that starts outside the window's own area (its title bar, drawn
    by the compositor) is the one thing X cannot do: it goes through the
    compositor's pointer."""
    desktop = FakeDesktop()
    real = FakePointer(desktop)
    waits: list[float] = []
    computer.perform(
        Action("drag", x=10, y=-10, to_x=310, to_y=90),
        VIDEO,
        {},
        desktop,
        lambda env: real,
        waits.append,
    )
    glide = computer.GLIDE_STEPS
    assert waits[: glide + 2] == [computer.GLIDE_S] * glide + [0.5, computer.HOLD_S]
    assert real.calls[glide - 1] == ("move", "110", "90", "1280x720")
    assert real.calls[glide] == ("down", "left")
    assert real.calls[-2] == ("move", "410", "190", "1280x720")  # the end: (100+310, 100+90)
    assert real.calls[-1] == ("up", "left")
    assert len([c for c in real.calls if c[0] == "move"]) == glide + computer.DRAG_STEPS
    assert real.closed


def test_a_compositor_pointer_that_does_not_land_presses_nothing() -> None:
    desktop = FakeDesktop()
    real = FakePointer(desktop, lands=False)
    said = computer.perform(
        Action("drag", x=10, y=-10, to_x=50, to_y=40),
        VIDEO,
        {},
        desktop,
        lambda env: real,
        lambda s: None,
    )
    assert said is not None
    assert said.startswith("error: nothing was clicked")
    assert [c[0] for c in real.calls] == ["move"] * computer.GLIDE_STEPS
    assert real.closed


def test_without_the_screens_size_a_title_bar_drag_goes_through_x() -> None:
    desktop = FakeDesktop()
    desktop.display_code = 1
    real = FakePointer(desktop)
    said = computer.perform(
        Action("drag", x=10, y=-10, to_x=50, to_y=40),
        VIDEO,
        {},
        desktop,
        lambda env: real,
        lambda s: None,
    )
    assert said is None
    assert real.calls == []
    assert real.closed
    assert ["xdotool", "mousemove", "--", "110", "90"] in desktop.actions


def test_keys_and_scrolls_never_take_the_compositors_pointer() -> None:
    desktop = FakeDesktop()
    asked: list[str] = []

    def offer(env: object) -> FakePointer:
        asked.append("asked")
        return FakePointer(desktop)

    computer.perform(Action("key", keys="Return"), VIDEO, {}, desktop, offer)
    assert asked == []  # a key has no point: the pointer is not even asked for
    real = FakePointer(desktop)
    computer.perform(
        Action("scroll", x=1, y=1), VIDEO, {}, desktop, lambda env: real, lambda s: None
    )
    assert [c[0] for c in real.calls] == ["move"]  # shown where the wheel turns, never pressed


def test_no_compositor_pointer_in_the_suite_means_x(monkeypatch: pytest.MonkeyPatch) -> None:
    """The real `session_pointer`, under the suite's guard (no session socket)."""
    assert computer.session_pointer({}) is None


# -- the latest look decides where a point was measured ------------------------------


def test_after_a_whole_screen_look_a_point_is_read_on_it_unasked(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, desktop: FakeDesktop
) -> None:
    """Live: the model looked at the whole screen and dragged with its pixels,
    without space=screen, and was refused; it then spent minutes working the
    mapping out. Known-good: with no space given, the latest screenshot decides,
    and the result says how the point was read."""
    ctx = _ctx(tmp_path, monkeypatch)
    tools._screenshot(ctx, {})  # the whole screen (a 4x3 fake picture)
    ctx.screen_capture = (1600, 900)  # as a real 1600x900 capture would record
    said = act(ctx, window="video", action="click", x=300, y=250)
    assert said.startswith(
        'done: left click at (140, 100) on 0x1200005 "Video Configuration" 400x300 '
        "(x, y read on your whole-screen screenshot)"
    )
    tools._screenshot(ctx, {"window": "video"})  # now the window
    said = act(ctx, window="video", action="click", x=30, y=40)
    assert said.startswith('done: left click at (30, 40) on 0x1200005 "Video Configuration"')
    assert "whole-screen" not in said.split(".")[0]


def test_an_explicit_space_overrides_the_latest_look(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, desktop: FakeDesktop
) -> None:
    ctx = _ctx(tmp_path, monkeypatch)
    tools._screenshot(ctx, {})
    said = act(ctx, window="video", action="click", x=30, y=40, space="window")
    assert said.startswith("done: left click at (30, 40) on 0x1200005")


# -- points measured on a zoomed screenshot -----------------------------------------


def test_after_a_zoom_a_point_is_read_on_the_enlarged_picture(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, desktop: FakeDesktop
) -> None:
    """Known-good: a region at (100, 50), 80x40, is shown four times larger; with
    space=zoom the model's (40, 20) on it is the window's (110, 55), and a
    drag's end converts too. Known-bad (live, rung 1): with no space, a point
    after a zoom is the window's own; a click meant for the menu bar was read
    on a zoom of the title and landed in the page."""
    ctx = _ctx(tmp_path, monkeypatch)
    tools._screenshot(ctx, {"window": "video", "x": 100, "y": 50, "width": 80, "height": 40})
    said = act(ctx, window="video", action="click", x=40, y=20)
    assert said.startswith('done: left click at (40, 20) on 0x1200005 "Video Configuration"')
    tools._screenshot(ctx, {"window": "video", "x": 100, "y": 50, "width": 80, "height": 40})
    said = act(ctx, window="video", action="click", x=40, y=20, space="zoom")
    assert said.startswith(
        'done: left click at (110, 55) on 0x1200005 "Video Configuration" 400x300 '
        "(x, y read on your zoomed screenshot)"
    )
    tools._screenshot(ctx, {"window": "video", "x": 100, "y": 50, "width": 80, "height": 40})
    said = act(ctx, window="video", action="drag", x=40, y=20, to_x=80, to_y=40, space="zoom")
    assert said.startswith("done: drag from (110, 55) to (120, 60) on 0x1200005")
    said = act(ctx, window="video", action="key", keys="ctrl+b")
    assert "zoomed screenshot" not in said  # a key has no point to read


def test_a_zoom_of_another_window_does_not_place_a_point(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, desktop: FakeDesktop
) -> None:
    ctx = _ctx(tmp_path, monkeypatch)
    assert act(ctx, window="video", action="click", x=4, y=4, space="zoom").startswith(
        "error: your latest zoomed screenshot was not of 0x1200005"
    )
    ctx.zoom = screen.Zoom("0x99", 0, 0, 4.0)
    ctx.last_look = "zoom"  # a fresh zoom, of another window
    assert act(ctx, window="video", action="click", x=4, y=4, space="zoom").startswith(
        "error: your latest zoomed screenshot was not of 0x1200005"
    )
    assert [c for c in desktop.actions if c[1] in ("mousemove", "click")] == []


def test_a_zoom_needs_all_four_numbers(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, desktop: FakeDesktop
) -> None:
    ctx = _ctx(tmp_path, monkeypatch)
    half = {"window": "video", "x": 1, "y": 1, "width": 10}
    assert tools._screenshot(ctx, half).startswith("error: a zoom needs all of x, y, width")
    flag = {"window": "video", "x": 1, "y": 1, "width": 10, "height": True}
    assert tools._screenshot(ctx, flag).startswith("error: a zoom needs all of x, y, width")
    assert ctx.last_look is None
    assert ctx.zoom is None


def test_a_whole_window_look_after_a_zoom_reads_window_pixels_again(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, desktop: FakeDesktop
) -> None:
    ctx = _ctx(tmp_path, monkeypatch)
    tools._screenshot(ctx, {"window": "video", "x": 100, "y": 50, "width": 80, "height": 40})
    tools._screenshot(ctx, {"window": "video"})
    said = act(ctx, window="video", action="click", x=40, y=20)
    assert said.startswith('done: left click at (40, 20) on 0x1200005 "Video Configuration"')


def test_the_picture_after_an_action_is_the_latest_look(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, desktop: FakeDesktop
) -> None:
    """The tool says to give x, y as the latest screenshot shows them, and the
    window's picture comes back after every action. Known-bad: after a zoom and
    a click, the next point was still read on the zoom although the model's
    newest picture was the whole window."""
    ctx = _ctx(tmp_path, monkeypatch)
    tools._screenshot(ctx, {"window": "video", "x": 100, "y": 50, "width": 80, "height": 40})
    act(ctx, window="video", action="click", x=40, y=20)  # read on the zoom: (110, 55)
    said = act(ctx, window="video", action="click", x=40, y=20)
    assert said.startswith('done: left click at (40, 20) on 0x1200005 "Video Configuration"')
    tools._screenshot(ctx, {})
    ctx.screen_capture = (1600, 900)
    act(ctx, window="video", action="key", keys="Return")  # its picture is the window's too
    said = act(ctx, window="video", action="click", x=30, y=40)
    assert said.startswith('done: left click at (30, 40) on 0x1200005 "Video Configuration"')


def test_typed_lines_are_parted_by_a_real_return(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, desktop: FakeDesktop
) -> None:
    """Live: "Owls\nOwls are ..." typed into Writer as "OwlsOwls are ...", the
    newline lost. Known-good: each line is typed, and Return is pressed as a
    key between lines, an empty line included."""
    said = act(_ctx(tmp_path, monkeypatch), window="video", action="type", text="A\r\n\nB")
    assert said.startswith("done: type 5 characters")
    assert [c[1:] for c in desktop.actions[1:]] == [
        ["type", "--clearmodifiers", "--", "A"],
        ["key", "--clearmodifiers", "Return"],
        ["key", "--clearmodifiers", "Return"],
        ["type", "--clearmodifiers", "--", "B"],
    ]


def test_a_failed_line_stops_the_typing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, desktop: FakeDesktop
) -> None:
    desktop.act_codes["key"] = 1
    said = act(_ctx(tmp_path, monkeypatch), window="video", action="type", text="A\nB")
    assert said.startswith("error: type 3 characters on 0x1200005")
    assert [c[1] for c in desktop.actions[1:]] == ["type", "key"]  # B was never typed


# -- the picture after an action shows what is on top of the window -------------------


@pytest.mark.parametrize(("focus_sticks", "taken_with"), [(True, "grim"), (False, "import")])
def test_the_picture_after_an_action_shows_the_menu_it_opened(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    focus_sticks: bool,
    taken_with: str,
) -> None:
    """Live: a click on "Format" opened its menu as a window of its own, and the
    picture that came back (the Writer window alone) showed no menu. Known-good:
    the window has the focus, so its place on the screen is captured. Known-bad
    stays read from X: a window that did not get the focus may be covered."""
    fake = FakeDesktop(focus_sticks=focus_sticks)
    monkeypatch.setattr(screen, "run_x", fake)
    monkeypatch.setattr(tools, "desktop_env", lambda: {"DISPLAY": ":1", "WAYLAND_DISPLAY": "w-0"})
    monkeypatch.setattr("saddle.screen.shutil.which", lambda name: f"/usr/bin/{name}")
    said = act(_ctx(tmp_path, monkeypatch), window="video", action="click", x=30, y=40)
    assert said.startswith("done: left click at (30, 40)")
    pictures = [c for c in fake.calls if c[0] in ("grim", "import")]
    assert [c[0] for c in pictures] == [taken_with]
    if taken_with == "grim":
        assert pictures[0][4] == "100,100 400x300"


def test_a_scroll_with_no_point_after_a_zoom_aims_at_the_middle(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, desktop: FakeDesktop
) -> None:
    ctx = _ctx(tmp_path, monkeypatch)
    tools._screenshot(ctx, {"window": "video", "x": 100, "y": 50, "width": 80, "height": 40})
    said = act(ctx, window="video", action="scroll", direction="down", space="zoom")
    assert said.startswith("done: scroll down 3 on 0x1200005")
    assert ["xdotool", "mousemove", "--window", "18874373", "200", "150"] in desktop.actions


def test_the_picture_after_an_action_waits_for_the_screen_to_settle(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, desktop: FakeDesktop
) -> None:
    """Measured on labwc with Writer: a picture taken straight after a click on
    "Format" showed the menu 1 time of 4 (the menu not drawn yet), after 0.1 s
    4 of 4. Live, the stale picture read as a missed click, and the model's
    next click closed the menu it had opened. Known-good: the wait comes after
    the action and before the capture."""
    order: list[str] = []
    monkeypatch.setattr("saddle.tools.time.sleep", lambda s: order.append(f"sleep {s}"))
    original = desktop.__call__

    def spy(argv: Sequence[str], env: Mapping[str, str]) -> tuple[int, str]:
        if argv[0] == "import":
            order.append("capture")
        elif argv[0] == "xdotool" and "click" in argv:
            order.append("click")
        return original(argv, env)

    monkeypatch.setattr(screen, "run_x", spy)
    act(_ctx(tmp_path, monkeypatch), window="video", action="click", x=30, y=40)
    assert order[-3:] == ["click", f"sleep {tools.AFTER_SETTLE_S}", "capture"]
    assert tools.AFTER_SETTLE_S >= 0.1


class MenuDesktop(FakeDesktop):
    """The video dialog with an open menu (unnamed, stacked over it) that
    starts 40 px above the dialog's top edge."""

    def __call__(self, argv: Sequence[str], env: Mapping[str, str]) -> tuple[int, str]:
        if list(argv[:2]) == ["xwininfo", "-id"]:
            return 0, "  Map State: IsViewable\n"
        if argv[0] == "xwininfo":
            menu = "     0x1300001 (has no name): ()  150x300+150+60  +150+60\n"
            return 0, TREE.replace('     0x1200005 "Video', menu + '     0x1200005 "Video', 1)
        return super().__call__(argv, env)


def test_a_menu_reaching_past_the_window_is_seen_and_clicked(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Live (rung 1): Writer's tall Format menu opened from 34 px above the
    window, its first rows were cut from the picture, and the model concluded
    "Bold" was off screen. Known-good: the picture widens to the menu, says
    where it starts, and a point read on it lands on the menu (above the
    window, at its place on the screen). Known-bad: a point past the widened
    picture is still refused."""
    fake = MenuDesktop()
    monkeypatch.setattr(screen, "run_x", fake)
    monkeypatch.setattr(tools, "desktop_env", lambda: {"DISPLAY": ":1", "WAYLAND_DISPLAY": "w-0"})
    monkeypatch.setattr("saddle.screen.shutil.which", lambda name: f"/usr/bin/{name}")
    monkeypatch.setattr("saddle.tools.time.sleep", lambda s: None)
    ctx = _ctx(tmp_path, monkeypatch)
    said = act(ctx, window="video", action="click", x=60, y=10)
    assert "widened to 400x340" in said
    assert "starts at (0, -40) of the window" in said
    assert ["grim", "-t", "png", "-g", "100,60 400x340"] == [
        c for c in fake.calls if c[0] == "grim"
    ][-1][:5]
    said = act(ctx, window="video", action="click", x=200, y=10)  # on the menu, above the window
    assert said.startswith("done: left click at (200, -30) on 0x1200005")
    assert "(x, y read on your latest screenshot)" in said
    assert ["xdotool", "mousemove", "--", "300", "70"] in fake.actions
    refused = act(ctx, window="video", action="click", x=200, y=345)
    assert refused.startswith("error: (200, 305) is outside")


# -- the shapes a model reaches for ---------------------------------------------------


@pytest.mark.parametrize(
    ("arguments", "meant"),
    [
        ({"action": "double_click", "x": 1, "y": 2}, Action("click", x=1, y=2, double=True)),
        ({"action": "double", "x": 1, "y": 2}, Action("click", x=1, y=2, double=True)),
        ({"action": "right_click", "x": 1, "y": 2}, Action("click", x=1, y=2, button="right")),
        (
            {"action": "click", "x": 1, "y": 2, "double": "True"},
            Action("click", x=1, y=2, double=True),
        ),
        ({"action": "click", "x": 1, "y": 2, "double": "false"}, Action("click", x=1, y=2)),
    ],
)
def test_the_shapes_a_model_reaches_for_mean_what_they_say(
    arguments: dict[str, Any], meant: Action
) -> None:
    """Live (rungs 1 and 3): action "double" and "double_click", and double
    "True" as a string, were refused, four calls in one minute. Known-good:
    each is read as the click it names. Known-bad stays refused."""
    assert computer.parse(arguments) == meant
    assert computer.parse({"action": "click", "x": 1, "y": 2, "double": "twice"}) == (
        "error: click takes button left or right and double true or false"
    )
    unknown = computer.parse({"action": "triple_click", "x": 1, "y": 2})
    assert str(unknown).startswith("error: action")


def test_with_no_window_named_the_last_one_looked_at_is_meant(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, desktop: FakeDesktop
) -> None:
    """Live (rung 1): two type calls without a window were refused while the
    model had just looked at Writer. Known-good: the window last looked at or
    acted on is used, and the result names it. Known-bad: with no such window
    the call is still refused."""
    ctx = _ctx(tmp_path, monkeypatch)
    assert act(ctx, action="type", text="hi").startswith("error: computer needs a window")
    tools._screenshot(ctx, {"window": "video"})
    said = act(ctx, action="type", text="hi")
    assert said.startswith('done: type 2 characters on 0x1200005 "Video Configuration" 400x300')
    assert "(no window named: the one you last looked at or acted on)" in said


def test_a_window_acted_on_is_the_one_meant_next(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, desktop: FakeDesktop
) -> None:
    ctx = _ctx(tmp_path, monkeypatch)
    act(ctx, window="video", action="key", keys="Return")  # named, no screenshot before
    said = act(ctx, action="key", keys="Tab")
    assert said.startswith('done: press Tab on 0x1200005 "Video Configuration" 400x300')


def test_space_window_reads_points_on_the_window_s_widened_picture(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Live (rung 1, try 3): on a picture widened 34 px above Writer for its
    Format menu, the model passed space=window and its click on "Character..."
    landed 34 px off. Known-good: space=window means the window's latest
    picture, widened or not."""
    fake = MenuDesktop()
    monkeypatch.setattr(screen, "run_x", fake)
    monkeypatch.setattr(tools, "desktop_env", lambda: {"DISPLAY": ":1", "WAYLAND_DISPLAY": "w-0"})
    monkeypatch.setattr("saddle.screen.shutil.which", lambda name: f"/usr/bin/{name}")
    monkeypatch.setattr("saddle.tools.time.sleep", lambda s: None)
    ctx = _ctx(tmp_path, monkeypatch)
    act(ctx, window="video", action="click", x=60, y=10)  # opens the menu: picture widened
    said = act(ctx, window="video", action="click", x=200, y=100, space="window")
    assert said.startswith("done: left click at (200, 60) on 0x1200005")


def test_a_widened_picture_of_one_window_does_not_move_points_on_another(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake = MenuDesktop()
    monkeypatch.setattr(screen, "run_x", fake)
    monkeypatch.setattr(tools, "desktop_env", lambda: {"DISPLAY": ":1", "WAYLAND_DISPLAY": "w-0"})
    monkeypatch.setattr("saddle.screen.shutil.which", lambda name: f"/usr/bin/{name}")
    monkeypatch.setattr("saddle.tools.time.sleep", lambda s: None)
    ctx = _ctx(tmp_path, monkeypatch, Asker(yes=True))
    act(ctx, window="video", action="click", x=60, y=10)  # video's picture widened
    said = act(ctx, window="notes", action="click", x=5, y=5)
    assert said.startswith('done: left click at (5, 5) on 0x1400002 "Notes - Editor"')


def test_screen_points_are_read_only_right_after_a_whole_screen_look(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, desktop: FakeDesktop
) -> None:
    """Live (rung 1, try 4): the model read a point off the window's picture
    (widened by a menu, so it looked like the whole screen) and passed
    space=screen; saddle scaled it with an older whole-screen picture's size,
    the click landed at (169, 0) and changed the document. Known-good: right
    after a whole-screen screenshot, space=screen converts. Known-bad: once
    a window's picture is newer, space=screen is refused with what to do."""
    ctx = _ctx(tmp_path, monkeypatch)
    tools._screenshot(ctx, {})
    ctx.screen_capture = (1600, 900)
    said = act(ctx, window="video", action="click", x=300, y=250, space="screen")
    assert said.startswith("done: left click at (140, 100)")  # its picture is the window's now
    said = act(ctx, window="video", action="click", x=300, y=250, space="screen")
    assert said.startswith(
        "error: space=screen reads x, y on a whole-screen screenshot, but your latest "
        'picture is of 0x1200005 "Video Configuration" 400x300'
    )


def test_a_zoom_with_no_window_named_is_of_the_last_one(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, desktop: FakeDesktop
) -> None:
    """Live (rung 1, try 4): a zoom without a window was refused right after a
    look at Writer. Known-good: the window last looked at or acted on is
    zoomed. Known-bad: with none, the refusal stands."""
    ctx = _ctx(tmp_path, monkeypatch)
    region = {"x": 10, "y": 10, "width": 40, "height": 20}
    assert tools._screenshot(ctx, region).startswith("error: a region is part of a window")
    tools._screenshot(ctx, {"window": "video"})
    said = tools._screenshot(ctx, region)
    assert "screenshot of 0x1200005" in said
    assert ctx.zoom == screen.Zoom("0x1200005", 10, 10, 4.0)


def test_a_zoom_is_aimed_on_only_while_it_is_the_latest_picture(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, desktop: FakeDesktop
) -> None:
    """Live (rung 1, try 7): the model zoomed on the title, opened the Format
    menu (the click's picture, widened, became the latest), then passed
    space=zoom for "Text"; the title's zoom was applied and the click landed
    in the page at (307, 228). Known-good: right after the zoom, space=zoom
    converts. Known-bad: once a newer picture came back, it is refused."""
    ctx = _ctx(tmp_path, monkeypatch)
    tools._screenshot(ctx, {"window": "video", "x": 100, "y": 50, "width": 80, "height": 40})
    said = act(ctx, window="video", action="click", x=40, y=20, space="zoom")
    assert said.startswith("done: left click at (110, 55)")
    said = act(ctx, window="video", action="click", x=40, y=20, space="zoom")
    assert said.startswith(
        "error: space=zoom reads x, y on a zoomed screenshot, but your latest picture "
        'is of 0x1200005 "Video Configuration" 400x300 as a whole'
    )


def test_the_compositor_pointer_glides_from_where_it_is() -> None:
    """A fresh virtual pointer's single jump was not seen as the pointer
    arriving (measured on a submenu item, 0 of 6, 6 of 6 with a glide). The
    compositor's pointer now serves title-bar drags; it still glides there.
    Known-good: the moves start next to the pointer's place and end on the
    press point, which only a later move reaches."""
    desktop = FakeDesktop()  # the pointer rests at (715, 438)
    real = FakePointer(desktop)
    computer.perform(
        Action("drag", x=10, y=-10, to_x=50, to_y=40),
        VIDEO,
        {},
        desktop,
        lambda env: real,
        lambda s: None,
    )
    moves = [(int(c[1]), int(c[2])) for c in real.calls if c[0] == "move"][: computer.GLIDE_STEPS]
    assert moves[0] == (715 + (110 - 715) // 8, 438 + (90 - 438) // 8)  # near the start
    assert moves[-1] == (110, 90)
    assert (110, 90) not in moves[:-1]


def test_a_short_key_sequence_is_pressed_in_order(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, desktop: FakeDesktop
) -> None:
    """Live (rung 1, try 9): keys "shift+Right shift+Right shift+Right
    shift+Right" was refused (one key per call). Known-good: up to MAX_KEYS
    keys, each in key syntax, go to xdotool in order. Known-bad: a key that
    reads as an option, or too many keys, is still refused."""
    said = act(_ctx(tmp_path, monkeypatch), window="video", action="key", keys="shift+Right Tab")
    assert said.startswith("done: press shift+Right Tab on 0x1200005")
    assert desktop.actions[-1] == ["xdotool", "key", "--clearmodifiers", "shift+Right", "Tab"]
    assert str(computer.parse({"action": "key", "keys": "Tab --window 1"})).startswith(
        "error: keys"
    )
    many = " ".join(["Right"] * (computer.MAX_KEYS + 1))
    assert str(computer.parse({"action": "key", "keys": many})).startswith("error: keys")


def test_a_list_flag_lists_the_windows(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, desktop: FakeDesktop
) -> None:
    """Live (rung 1, try 9): screenshot {"list": "True"} was taken as a
    whole-screen picture, three times, when the window list was asked for."""
    ctx = _ctx(tmp_path, monkeypatch)
    for flag in (True, "True", "true"):
        assert tools._screenshot(ctx, {"list": flag}).startswith("windows on the screen:")
    assert "windows on the screen" not in tools._screenshot(ctx, {"list": "false"})


def test_a_hidden_window_is_not_found_to_act_on(desktop: FakeDesktop) -> None:
    class Hiding(FakeDesktop):
        def __call__(self, argv: Sequence[str], env: Mapping[str, str]) -> tuple[int, str]:
            if list(argv[:3]) == ["xwininfo", "-id", "0x1200005"]:
                return 0, "  Map State: IsUnMapped\n"
            return super().__call__(argv, env)

    said = computer.find("video", {}, Hiding())
    assert str(said).startswith("error: no window matches 'video'")


def test_a_drag_inside_the_window_goes_through_x() -> None:
    """Drawing and selecting are drags inside the window: X input, like clicks
    there. Checked live: an X drag across "Owls " in Writer selected exactly it."""
    desktop = FakeDesktop()
    real = FakePointer(desktop)
    said = computer.perform(
        Action("drag", x=10, y=5, to_x=60, to_y=5),
        VIDEO,
        {},
        desktop,
        lambda env: real,
        lambda s: None,
    )
    assert said is None
    assert [c[0] for c in real.calls] == ["move"]
    assert "mousedown" in desktop.actions[-1]


def test_the_drawn_cursor_is_moved_to_where_x_presses() -> None:
    """Checked on labwc: with X's pointer moved to (300, 300) and the
    compositor's left at (900, 500), the picture drew the arrow at
    (900, 500). Live (rung 2), pictures showed the arrow where an earlier
    action left it and the model reasoned from it about where it had
    clicked. Known-good: before X presses, the compositor's pointer is moved
    (no press) to the same screen point, and closed."""
    desktop = FakeDesktop()
    real = FakePointer(desktop)
    computer.perform(
        Action("click", x=30, y=40), VIDEO, {}, desktop, lambda env: real, lambda s: None
    )
    assert real.calls == [("move", "130", "140", "1280x720")]
    assert real.closed
    unknown = FakeDesktop()
    unknown.display_code = 1  # no screen size: nothing to move it by
    quiet = FakePointer(unknown)
    computer.perform(
        Action("click", x=30, y=40), VIDEO, {}, unknown, lambda env: quiet, lambda s: None
    )
    assert quiet.calls == []
    assert quiet.closed


def test_screenshot_refuses_what_it_does_not_read(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, desktop: FakeDesktop
) -> None:
    """Live (rung 2): {"action": "list"} was silently a whole-screen picture,
    three times. Known-good: action list lists; an argument screenshot does not
    read is refused by name, nothing captured."""
    ctx = _ctx(tmp_path, monkeypatch)
    assert tools._screenshot(ctx, {"action": "list"}).startswith("windows on the screen:")
    said = tools._screenshot(ctx, {"windw": "video"})
    assert said.startswith("error: screenshot does not take windw")
    assert tools._screenshot(ctx, {"action": "click"}).startswith("error: screenshot's action")


def test_a_cut_zoom_converts_points_with_its_real_size(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, desktop: FakeDesktop
) -> None:
    """A zoom cut at the window's edge is scaled by what was captured: a
    1000x40 region from x=380 of the 400-wide dialog is 20x40, shown x4 (the
    uncut request would have been x1.6)."""
    ctx = _ctx(tmp_path, monkeypatch)
    tools._screenshot(ctx, {"window": "video", "x": 380, "y": 50, "width": 1000, "height": 40})
    assert ctx.zoom == screen.Zoom("0x1200005", 380, 50, 4.0)
