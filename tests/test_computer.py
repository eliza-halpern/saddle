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
        if argv[0] == "import":
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

    def closed(wanted: str, out: Path, env: Mapping[str, str], region: object = None) -> str:
        return "error: no window matches '0x1200005'"

    monkeypatch.setattr(screen, "capture", closed)
    result = act(ctx, window="video", action="key", keys="Return")
    assert result.startswith('done: press Return on 0x1200005 "Video Configuration"')
    assert "could not be shown afterwards (error: no window matches" in result
    assert result.endswith("The action was done.")
    assert ctx.attachments == []

    def junk(wanted: str, out: Path, env: Mapping[str, str], region: object = None) -> Window:
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
        ["xdotool", "type", "--clearmodifiers", "--", 'say "hi"\nnow'],
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
        ({"window": "video", "action": "key", "keys": "a b"}, "error: keys must be one key"),
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


def test_a_click_uses_the_compositors_pointer_when_it_offers_one() -> None:
    """Only the compositor's pointer is the session's real pointer. Known-good:
    it goes to the window's place plus the point, is read back there, presses."""
    desktop = FakeDesktop()
    real = FakePointer(desktop)
    waits: list[float] = []
    said = computer.perform(
        Action("click", x=10, y=20, double=True), VIDEO, {}, desktop, lambda env: real, waits.append
    )
    assert said is None
    assert real.calls == [
        ("move", "110", "120", "1280x720"),
        ("down", "left"),
        ("up", "left"),
        ("down", "left"),
        ("up", "left"),
    ]
    assert waits == [0.5]
    assert real.closed
    assert not any(c[1] in ("mousemove", "click") or "click" in c for c in desktop.actions)


def test_a_drag_moves_with_the_button_held_to_its_end() -> None:
    desktop = FakeDesktop()
    real = FakePointer(desktop)
    waits: list[float] = []
    computer.perform(
        Action("drag", x=10, y=5, to_x=310, to_y=105),
        VIDEO,
        {},
        desktop,
        lambda env: real,
        waits.append,
    )
    assert waits[:2] == [0.5, computer.HOLD_S]  # settle, then hold before moving
    assert real.calls[0] == ("move", "110", "105", "1280x720")
    assert real.calls[1] == ("down", "left")
    assert real.calls[-2] == ("move", "410", "205", "1280x720")  # the end: (100+310, 100+105)
    assert real.calls[-1] == ("up", "left")
    assert len([c for c in real.calls if c[0] == "move"]) == 1 + computer.DRAG_STEPS
    assert real.closed


def test_a_compositor_pointer_that_does_not_land_presses_nothing() -> None:
    desktop = FakeDesktop()
    real = FakePointer(desktop, lands=False)
    said = computer.perform(Action("click", x=10, y=20), VIDEO, {}, desktop, lambda env: real)
    assert said is not None
    assert said.startswith("error: nothing was clicked")
    assert [c[0] for c in real.calls] == ["move"]
    assert real.closed


def test_without_the_screens_size_the_click_goes_through_x() -> None:
    desktop = FakeDesktop()
    desktop.display_code = 1
    real = FakePointer(desktop)
    said = computer.perform(Action("click", x=10, y=20), VIDEO, {}, desktop, lambda env: real)
    assert said is None
    assert real.calls == []
    assert real.closed
    assert ["xdotool", "mousemove", "--window", "18874373", "10", "20"] in desktop.actions


def test_keys_and_scrolls_never_take_the_compositors_pointer() -> None:
    desktop = FakeDesktop()
    asked: list[str] = []

    def offer(env: object) -> FakePointer:
        asked.append("asked")
        return FakePointer(desktop)

    computer.perform(Action("key", keys="Return"), VIDEO, {}, desktop, offer)
    computer.perform(Action("scroll", x=1, y=1), VIDEO, {}, desktop, offer, lambda s: None)
    assert asked == []


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
    """Known-good: a region at (100, 50), 80x40, is shown four times larger; the
    model's (40, 20) on it is the window's (110, 55), and a drag's end converts too."""
    ctx = _ctx(tmp_path, monkeypatch)
    tools._screenshot(ctx, {"window": "video", "x": 100, "y": 50, "width": 80, "height": 40})
    said = act(ctx, window="video", action="click", x=40, y=20)
    assert said.startswith(
        'done: left click at (110, 55) on 0x1200005 "Video Configuration" 400x300 '
        "(x, y read on your zoomed screenshot)"
    )
    said = act(ctx, window="video", action="drag", x=40, y=20, to_x=80, to_y=40)
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
    ctx.last_look = "zoom"
    assert act(ctx, window="video", action="click", x=4, y=4).startswith(
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
