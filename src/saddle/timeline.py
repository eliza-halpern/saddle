"""Rich live timeline for `saddle up` (Ocean palette).

One scrolling timeline instead of panes: dim rules between turns, a cyan
`you>` prompt, thinking in italic dim slate-blue, assistant talk as
bright-white Markdown (code fences highlighted), tool calls in magenta
with green/red results. A per-turn Live display refreshes token by
token on a terminal; on a pipe Rich emits each turn once, complete.
"""

from __future__ import annotations

from collections.abc import Callable, Iterator
from contextlib import contextmanager
from time import monotonic
from typing import Final

from rich.console import Console, ConsoleOptions, Group, RenderableType, RenderResult
from rich.live import Live
from rich.markdown import Markdown
from rich.styled import Styled
from rich.text import Text

from saddle.vllm import ToolCall

DOTS_LIT_STYLE: Final = "bold red"
DOTS_UNLIT_STYLE: Final = "dim red"
SPINNER_DOT_SECONDS: Final = 0.3
THINKING_STYLE: Final = "italic dim #7ba7cc"
TALK_STYLE: Final = "bright_white"
TALK_LABEL: Final = "saddle> "
TALK_LABEL_STYLE: Final = "bold #5ab0fa"
PROMPT_STYLE: Final = "bold cyan"
RULE_STYLE: Final = "dim"
TOOL_STYLE: Final = "magenta"
OK_STYLE: Final = "green"
ERR_STYLE: Final = "red"

TOOL_ARGS_CHARS: Final = 500
TOOL_RESULT_CHARS: Final = 2000
USER_TURN_CHARS: Final = 2000
LIVE_VIEW_CHARS: Final = 3000
REFRESH_MIN_SECONDS: Final = 0.1


def _shorten(text: str, limit: int) -> str:
    """Cap text at limit chars with a remainder marker."""
    if len(text) <= limit:
        return text
    return text[:limit] + f"\n…[{len(text) - limit} chars truncated]"


class _SpinnerLine:
    """Three red dots, lighting up one at a time against the clock."""

    def __init__(self, clock: Callable[[], float], started: float) -> None:
        self._clock = clock
        self._started = started

    def __rich_console__(self, console: Console, options: ConsoleOptions) -> RenderResult:
        lit = int((self._clock() - self._started) / SPINNER_DOT_SECONDS) % 4
        line = Text()
        for i in range(3):
            if i > 0:
                line.append(" ")
            if i < lit:
                line.append("●", style=DOTS_LIT_STYLE)
            else:
                line.append("○", style=DOTS_UNLIT_STYLE)
        yield line


class Timeline:
    """Per-turn Live timeline over one console (Ocean palette)."""

    def __init__(self, console: Console, *, clock: Callable[[], float] = monotonic) -> None:
        self._console = console
        self._items: list[tuple[str, str]] = []
        self._live: Live | None = None
        self._clock = clock
        self._turn_started = clock()
        # Set per turn in live_turn; _refresh only reads it mid-turn.
        self._last_render: float

    @contextmanager
    def live_turn(self) -> Iterator[None]:
        """Show this turn's blocks live; persist them when the turn ends."""
        self._items = []
        self._turn_started = self._clock()
        self._last_render = float("-inf")
        # auto_refresh ticks TTY redraws so the spinner runs through
        # stalls (content updates still come from manual refresh below);
        # headless output is identical either way: unkillable, hence pragma.
        try:
            with Live(
                self._render_live(),
                console=self._console,
                auto_refresh=True,  # pragma: no mutate
            ) as live:
                self._live = live
                try:
                    yield
                finally:
                    # Settle the persisted frame without the spinner line.
                    live.update(self._render())
                    live.refresh()
                    self._live = None
        finally:
            # Live leaves the cursor after the last line; terminate it so
            # the next prompt starts fresh (nothing to end on empty turns).
            if self._items:
                self._console.print()

    def token(self, stream: str, text: str) -> None:
        """Append one token to the open block, refreshing the live turn."""
        if stream not in ("reasoning", "content"):
            msg = f"unknown token stream {stream!r}"
            raise ValueError(msg)
        kind = "thinking" if stream == "reasoning" else "talk"
        if self._items and self._items[-1][0] == kind:
            self._items[-1] = (kind, self._items[-1][1] + text)
        else:
            self._items.append((kind, text))
        self._refresh()

    def tool_call(self, call: ToolCall) -> None:
        """Show a tool call starting (magenta `$` line)."""
        args = _shorten(call.arguments, TOOL_ARGS_CHARS)
        self._items.append(("tool", f"$ {call.name} {args}"))
        self._refresh()

    def tool_result(self, result: str, exit_code: int) -> None:
        """Show a finished tool result (green ok, red failure)."""
        kind = "err" if exit_code != 0 else "ok"
        self._items.append((kind, _shorten(result, TOOL_RESULT_CHARS)))
        self._refresh()

    def show_prompt(self) -> None:
        """Rule plus cyan prompt; the terminal echoes the typed turn."""
        self._console.rule(style=RULE_STYLE)
        # end="": Rich treats None identically, so the None mutant is equivalent.
        self._console.print(Text("you> ", style=PROMPT_STYLE), end="")  # pragma: no mutate

    def user_turn(self, text: str) -> None:
        """Print the user's turn, literal (no Markdown), truncated if huge."""
        shown = _shorten(text, USER_TURN_CHARS)
        self._console.print(Text.assemble(("you> ", PROMPT_STYLE), (shown, TALK_STYLE)))

    def show_rule(self) -> None:
        """Dim rule; sits under the user's sent line in every session."""
        self._console.rule(style=RULE_STYLE)

    def show_error(self, message: str) -> None:
        """Red error line: a timeline item mid-turn, direct print otherwise."""
        if self._live is not None:
            self._items.append(("err", f"error: {message}"))
            self._refresh()
        else:
            self._console.print(Text(f"error: {message}", style=ERR_STYLE))

    def _refresh(self) -> None:
        """Re-render the live turn, at most every tenth of a second."""
        if self._live is None:
            return
        now = self._clock()
        if now - self._last_render < REFRESH_MIN_SECONDS:
            return
        self._last_render = now
        self._live.update(self._render_live())
        self._live.refresh()

    def _render_live(self) -> Group:
        """This turn's items, capped for speed, plus the spinner while generating."""
        return Group(
            *(self._live_item(kind, text) for kind, text in self._items),
            _SpinnerLine(self._clock, self._turn_started),
        )

    @staticmethod
    def _live_item(kind: str, text: str) -> RenderableType:
        """One item for the live view; long text shows a capped tail plus marker."""
        if len(text) <= LIVE_VIEW_CHARS:
            return Timeline._render_item(kind, text)
        tail = text[-LIVE_VIEW_CHARS:]
        return Group(
            Text("…[earlier output hidden in live view]", style=RULE_STYLE),
            Timeline._render_item(kind, tail),
        )

    def _render(self) -> Group:
        """This turn's items as renderables."""
        return Group(*(self._render_item(kind, text) for kind, text in self._items))

    @staticmethod
    def _render_item(kind: str, text: str) -> RenderableType:
        """One timeline item in Ocean colors."""
        if kind == "thinking":
            return Text(f"thinking: {text}", style=THINKING_STYLE)
        if kind == "talk":
            return Group(
                Text(TALK_LABEL, style=TALK_LABEL_STYLE),
                Styled(Markdown(text), TALK_STYLE),
            )
        if kind == "tool":
            return Text(text, style=TOOL_STYLE)
        if kind == "ok":
            return Text(text, style=OK_STYLE)
        if kind == "err":
            return Text(text, style=ERR_STYLE)
        msg = f"unknown timeline kind {kind!r}"
        raise ValueError(msg)
