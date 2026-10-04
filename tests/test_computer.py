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
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any, cast

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


class FakeDesktop:
    """xwininfo lists `TREE`; getwindowpid answers from `OWNERS`; import writes
    a PNG; every other xdotool call exits `act_code` (or per subcommand)."""

    def __init__(self, act_codes: Mapping[str, int] | None = None, *, tree_code: int = 0) -> None:
        self.act_codes = dict(act_codes or {})
        self.tree_code = tree_code
        self.calls: list[list[str]] = []

    def __call__(self, argv: Sequence[str], env: Mapping[str, str]) -> tuple[int, str]:
        self.calls.append(list(argv))
        if argv[0] == "xwininfo":
            return self.tree_code, TREE
        if argv[0] == "import":
            Path(argv[-1].removeprefix("png:")).write_bytes(_png())
            return 0, ""
        if argv[1] == "getwindowpid":
            pid = OWNERS.get(argv[2])
            return (0, f"{pid}\n") if pid is not None else (1, "")
        return self.act_codes.get(argv[1], 0), ""

    @property
    def actions(self) -> list[list[str]]:
        """The xdotool calls that change something (not the owner lookups)."""
        return [c for c in self.calls if c[0] == "xdotool" and c[1] != "getwindowpid"]


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


@pytest.mark.parametrize(
    ("arguments", "argv"),
    [
        (
            {"action": "focus"},
            [["xdotool", "windowactivate", "18874373"], ["xdotool", "windowfocus", "18874373"]],
        ),
        (
            {"action": "click", "x": 210, "y": 270},
            [["xdotool", "mousemove", "--window", "18874373", "210", "270", "click", "1"]],
        ),
        (
            {"action": "click", "x": "5", "y": 6, "button": "right", "double": True},
            [
                [
                    "xdotool",
                    "mousemove",
                    "--window",
                    "18874373",
                    "5",
                    "6",
                    "click",
                    "--repeat",
                    "2",
                    "3",
                ]
            ],
        ),
        (
            {"action": "key", "keys": "alt+Return"},
            [["xdotool", "key", "--window", "18874373", "alt+Return"]],
        ),
        (
            {"action": "type", "text": "-n ok"},
            [["xdotool", "type", "--window", "18874373", "--", "-n ok"]],
        ),
        (
            {"action": "scroll"},
            [
                [
                    "xdotool",
                    "mousemove",
                    "--window",
                    "18874373",
                    "200",
                    "150",
                    "click",
                    "--repeat",
                    "3",
                    "5",
                ]
            ],
        ),
        (
            {"action": "scroll", "direction": "up", "amount": 2, "x": 1, "y": 2},
            [
                [
                    "xdotool",
                    "mousemove",
                    "--window",
                    "18874373",
                    "1",
                    "2",
                    "click",
                    "--repeat",
                    "2",
                    "4",
                ]
            ],
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

    def closed(wanted: str, out: Path, env: Mapping[str, str]) -> str:
        return "error: no window matches '0x1200005'"

    monkeypatch.setattr(screen, "capture", closed)
    result = act(ctx, window="video", action="key", keys="Return")
    assert result.startswith('done: press Return on 0x1200005 "Video Configuration"')
    assert "could not be shown afterwards (error: no window matches" in result
    assert result.endswith("The action was done.")
    assert ctx.attachments == []

    def junk(wanted: str, out: Path, env: Mapping[str, str]) -> Window:
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
        ["xdotool", "type", "--window", "20971522", "--", 'say "hi"\nnow'],
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
        ({"window": "video", "action": "drag"}, "error: action must be one of"),
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
    monkeypatch.setattr(screen, "run_x", FakeDesktop({"mousemove": 1}))
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
    assert fake.actions == [["xdotool", "key", "--window", "18874377", "Return"]]
