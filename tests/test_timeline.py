"""Tests for saddle.timeline: the `saddle up` Rich timeline."""

from __future__ import annotations

import io

import pytest
from rich.console import Console

from saddle.timeline import (
    DOTS_LIT_STYLE,
    DOTS_UNLIT_STYLE,
    ERR_STYLE,
    LIVE_VIEW_CHARS,
    OK_STYLE,
    PROMPT_STYLE,
    REFRESH_MIN_SECONDS,
    RULE_STYLE,
    SPINNER_DOT_SECONDS,
    TALK_LABEL,
    TALK_LABEL_STYLE,
    TALK_STYLE,
    THINKING_STYLE,
    TOOL_ARGS_CHARS,
    TOOL_RESULT_CHARS,
    TOOL_STYLE,
    USER_TURN_CHARS,
    Timeline,
    _shorten,
    _SpinnerLine,
)
from saddle.vllm import ToolCall


def _console(out: io.StringIO, *, ansi: bool = False) -> Console:
    return Console(
        file=out,
        width=80,
        force_terminal=ansi,
        color_system="truecolor" if ansi else None,
    )


class _FakeClock:
    def __init__(self) -> None:
        self.now = 100.0

    def __call__(self) -> float:
        return self.now


def _token_then_boom(timeline: Timeline) -> None:
    timeline.token("content", "Hi ")
    msg = "x"
    raise ValueError(msg)


def test_shorten_passes_short_text_through() -> None:
    assert _shorten("hello", 500) == "hello"
    assert _shorten("x" * 500, 500) == "x" * 500
    assert _shorten("", 500) == ""


def test_shorten_truncates_with_remainder_marker() -> None:
    assert _shorten("x" * 501, 500) == "x" * 500 + "\n…[1 chars truncated]"
    assert _shorten("y" * 2001, 2000) == "y" * 2000 + "\n…[1 chars truncated]"
    assert _shorten("z" * 2500, 2000) == "z" * 2000 + "\n…[500 chars truncated]"


def test_ocean_palette_pins_styles_and_limits() -> None:
    assert DOTS_LIT_STYLE == "bold red"
    assert DOTS_UNLIT_STYLE == "dim red"
    assert SPINNER_DOT_SECONDS == 0.3
    assert THINKING_STYLE == "italic dim #7ba7cc"
    assert TALK_STYLE == "bright_white"
    assert TALK_LABEL == "saddle> "
    assert TALK_LABEL_STYLE == "bold #5ab0fa"
    assert PROMPT_STYLE == "bold cyan"
    assert RULE_STYLE == "dim"
    assert TOOL_STYLE == "magenta"
    assert OK_STYLE == "green"
    assert ERR_STYLE == "red"
    assert TOOL_ARGS_CHARS == 500
    assert TOOL_RESULT_CHARS == 2000
    assert USER_TURN_CHARS == 2000
    assert LIVE_VIEW_CHARS == 3000
    assert REFRESH_MIN_SECONDS == 0.1


def test_token_streaming_groups_blocks_by_stream() -> None:
    out = io.StringIO()
    timeline = Timeline(_console(out))

    with timeline.live_turn():
        timeline.token("reasoning", "Let me ")
        timeline.token("reasoning", "think. ")
        timeline.token("content", "Hi ")
        timeline.token("content", "there!")

    assert out.getvalue() == (
        "thinking: Let me think. \n" + "saddle> \n" + "Hi there!" + " " * 71 + "\n"
    )


def test_talk_renders_markdown_code_fence() -> None:
    out = io.StringIO()
    timeline = Timeline(_console(out))

    with timeline.live_turn():
        timeline.token("content", "Run:\n\n```sh\necho hi\n```\n")

    assert out.getvalue() == (
        "saddle> \n"
        + "Run:"
        + " " * 76
        + "\n\n"
        + " " * 80
        + "\n echo hi"
        + " " * 72
        + "\n"
        + " " * 80
        + "\n"
    )


def test_talk_label_ansi_bold_ocean_blue() -> None:
    out = io.StringIO()
    console = _console(out, ansi=True)
    timeline = Timeline(console)
    timeline.token("content", "Hi!")

    console.print(timeline._render())

    assert out.getvalue() == (
        "\x1b[1;38;2;90;176;250msaddle> \x1b[0m\n" + "\x1b[97mHi!" + " " * 77 + "\x1b[0m\n"
    )


def test_user_turn_prints_literal_text() -> None:
    out = io.StringIO()
    Timeline(_console(out)).user_turn("run *this* `code`")

    assert out.getvalue() == "you> run *this* `code`\n"


def test_user_turn_ansi_styles() -> None:
    out = io.StringIO()
    Timeline(_console(out, ansi=True)).user_turn("hello")

    assert out.getvalue() == "\x1b[1;36myou> \x1b[0m\x1b[97mhello\x1b[0m\n"


def test_user_turn_truncates_with_remainder_marker() -> None:
    out = io.StringIO()
    Timeline(_console(out)).user_turn("x" * 2500)

    # count-based: Rich wraps the long line, so exact layout is not pinned.
    captured = out.getvalue()
    assert captured.startswith("you> ")
    assert captured.count("x") == 2000
    assert captured.count("…[500 chars truncated]") == 1


def test_show_rule_prints_dim_rule() -> None:
    out = io.StringIO()
    Timeline(_console(out)).show_rule()

    assert out.getvalue() == "─" * 80 + "\n"


def test_show_rule_ansi_dim() -> None:
    out = io.StringIO()
    Timeline(_console(out, ansi=True)).show_rule()

    assert out.getvalue() == "\x1b[2m" + "─" * 80 + "\x1b[0m\n"


def test_dots_light_up_one_at_a_time() -> None:
    clock = _FakeClock()
    spinner = _SpinnerLine(clock, 100.0)
    frames: list[str] = []

    for offset in (0.0, 0.35, 0.65, 0.95, 1.25):
        clock.now = 100.0 + offset
        frame_out = io.StringIO()
        _console(frame_out).print(spinner)
        frames.append(frame_out.getvalue())

    assert frames == ["○ ○ ○\n", "● ○ ○\n", "● ● ○\n", "● ● ●\n", "○ ○ ○\n"]


def test_dots_ansi_red_lit_and_dim() -> None:
    clock = _FakeClock()
    clock.now = 100.35
    out = io.StringIO()

    _console(out, ansi=True).print(_SpinnerLine(clock, 100.0))

    assert out.getvalue() == "\x1b[1;31m●\x1b[0m \x1b[2;31m○\x1b[0m \x1b[2;31m○\x1b[0m\n"


def test_render_live_appends_dots_line() -> None:
    out = io.StringIO()
    console = _console(out)
    clock = _FakeClock()
    timeline = Timeline(console, clock=clock)

    with timeline.live_turn():
        timeline.token("content", "Hi!")
        clock.now = 100.35
        console.print(timeline._render_live())

    assert out.getvalue() == (
        "saddle> \n" + "Hi!" + " " * 77 + "\n" + "● ○ ○\n" + "saddle> \n" + "Hi!" + " " * 77 + "\n"
    )


def test_refresh_updates_live_render_with_dots() -> None:
    out = io.StringIO()
    clock = _FakeClock()
    timeline = Timeline(_console(io.StringIO()), clock=clock)

    with timeline.live_turn():
        timeline.token("content", "Hi!")
        clock.now = 100.35
        live = timeline._live
        assert live is not None
        _console(out).print(live.renderable)

    assert out.getvalue() == "saddle> \n" + "Hi!" + " " * 77 + "\n" + "● ○ ○\n"


def test_render_live_before_first_turn_is_unlit() -> None:
    out = io.StringIO()
    timeline = Timeline(_console(io.StringIO()), clock=_FakeClock())

    _console(out).print(timeline._render_live())

    assert out.getvalue() == "○ ○ ○\n"


def test_second_turn_restarts_spinner_clock() -> None:
    clock = _FakeClock()
    timeline = Timeline(_console(io.StringIO()), clock=clock)

    with timeline.live_turn():
        pass
    clock.now = 100.3

    with timeline.live_turn():
        out = io.StringIO()
        _console(out).print(timeline._render_live())

    assert out.getvalue() == "○ ○ ○\n"


def test_live_item_passes_short_text_through() -> None:
    out = io.StringIO()
    timeline = Timeline(_console(io.StringIO()), clock=_FakeClock())

    with timeline.live_turn():
        timeline.token("reasoning", "x" * 3000)
        _console(out).print(timeline._render_live())

    captured = out.getvalue()
    assert "…[earlier output hidden in live view]" not in captured
    assert captured.count("x") == 3000


def test_live_item_caps_long_text_to_tail() -> None:
    out = io.StringIO()
    timeline = Timeline(_console(io.StringIO()), clock=_FakeClock())

    with timeline.live_turn():
        timeline.token("reasoning", "HEAD" + "m" * 2993 + "TAIL")
        _console(out).print(timeline._render_live())

    captured = out.getvalue()
    assert captured.startswith("…[earlier output hidden in live view]\n")
    assert "TAIL" in captured
    assert "HEAD" not in captured
    assert captured.count("m") == 2993


def test_live_item_marker_ansi_dim() -> None:
    out = io.StringIO()
    timeline = Timeline(_console(io.StringIO()), clock=_FakeClock())

    with timeline.live_turn():
        timeline.token("reasoning", "y" * 3001)
        _console(out, ansi=True).print(timeline._render_live())

    assert out.getvalue().startswith("\x1b[2m…[earlier output hidden in live view]\x1b[0m\n")


def test_settled_frame_keeps_full_text() -> None:
    out = io.StringIO()
    timeline = Timeline(_console(out), clock=_FakeClock())

    with timeline.live_turn():
        timeline.token("reasoning", "HEAD" + "m" * 2993 + "TAIL")

    captured = out.getvalue()
    assert "HEAD" in captured
    assert "TAIL" in captured
    assert "…[earlier output hidden in live view]" not in captured


def test_refresh_throttles_rapid_tokens() -> None:
    clock = _FakeClock()
    clock.now = 0.0
    timeline = Timeline(_console(io.StringIO()), clock=clock)

    def shown() -> str:
        live = timeline._live
        assert live is not None
        out = io.StringIO()
        _console(out).print(live.renderable)
        return out.getvalue()

    with timeline.live_turn():
        timeline.token("content", "a")  # gap inf: renders
        assert "ab" not in shown()
        timeline.token("content", "b")  # gap 0: skipped
        assert "ab" not in shown()
        clock.now = 0.1  # gap exactly 0.1: renders
        timeline.token("content", "c")
        assert "abc" in shown()
        clock.now = 0.15  # gap 0.05: skipped
        timeline.token("content", "d")
        assert "abcd" not in shown()
        clock.now = 0.25  # gap 0.15: renders
        timeline.token("content", "e")
        assert "abcde" in shown()


def test_second_turn_resets_refresh_throttle() -> None:
    clock = _FakeClock()
    clock.now = 50.0
    timeline = Timeline(_console(io.StringIO()), clock=clock)

    with timeline.live_turn():
        timeline.token("content", "one")
    with timeline.live_turn():
        timeline.token("content", "two")
        live = timeline._live
        assert live is not None
        out = io.StringIO()
        _console(out).print(live.renderable)

    captured = out.getvalue()
    assert "two" in captured
    assert "one" not in captured


def test_live_turn_final_frame_has_no_dots() -> None:
    out = io.StringIO()
    timeline = Timeline(_console(out))

    with timeline.live_turn():
        timeline.token("reasoning", "Hmm. ")
        timeline.token("content", "Hi!")
        timeline.tool_call(ToolCall(id="1", name="add", arguments="{}"))
        timeline.tool_result("3", 0)

    captured = out.getvalue()
    assert "●" not in captured
    assert "○" not in captured


def test_tool_call_and_result_entries() -> None:
    out = io.StringIO()
    timeline = Timeline(_console(out))

    with timeline.live_turn():
        timeline.tool_call(ToolCall(id="1", name="read_file", arguments='{"path": "x"}'))
        timeline.tool_result("hello\n", 0)
        timeline.tool_call(ToolCall(id="2", name="read_file", arguments='{"path": "y"}'))
        timeline.tool_result("error: cannot read 'y'", 1)

    assert out.getvalue() == (
        '$ read_file {"path": "x"}\nhello\n\n$ read_file {"path": "y"}\nerror: cannot read \'y\'\n'
    )


def test_tool_entries_truncate_at_configured_limits() -> None:
    out = io.StringIO()
    timeline = Timeline(_console(out))

    with timeline.live_turn():
        timeline.tool_call(ToolCall(id="1", name="run", arguments="x" * 501))
        timeline.tool_call(ToolCall(id="2", name="run", arguments="y" * 500))
        timeline.tool_result("z" * 2001, 0)

    # x/y/z are absent from the marker, so counts pin exact kept lengths
    # without depending on Rich's line wrapping.
    captured = out.getvalue()
    assert captured.count("x") == 500
    assert captured.count("y") == 500
    assert captured.count("z") == 2000
    assert captured.count("…[1 chars truncated]") == 2


def test_show_prompt_rule_and_cyan() -> None:
    out = io.StringIO()
    timeline = Timeline(_console(out))

    timeline.show_prompt()

    assert out.getvalue() == "─" * 80 + "\n" + "you> "


def test_show_prompt_ansi_styles() -> None:
    out = io.StringIO()
    timeline = Timeline(_console(out, ansi=True))

    timeline.show_prompt()

    assert out.getvalue() == ("\x1b[2m" + "─" * 80 + "\x1b[0m\n" + "\x1b[1;36myou> \x1b[0m")


def test_show_error_red() -> None:
    out = io.StringIO()
    timeline = Timeline(_console(out))

    timeline.show_error("boom")

    assert out.getvalue() == "error: boom\n"


def test_show_error_ansi_red() -> None:
    out = io.StringIO()
    timeline = Timeline(_console(out, ansi=True))

    timeline.show_error("boom")

    assert out.getvalue() == "\x1b[31merror: boom\x1b[0m\n"


def test_show_error_mid_turn_stays_in_order() -> None:
    out = io.StringIO()
    timeline = Timeline(_console(out))

    with timeline.live_turn():
        timeline.token("content", "Hi!")
        timeline.show_error("boom")

    assert out.getvalue() == "saddle> \n" + "Hi!" + " " * 77 + "\n" + "error: boom\n"


def test_token_outside_turn_is_dropped() -> None:
    out = io.StringIO()
    timeline = Timeline(_console(out))

    timeline.token("content", "Hi!")

    assert out.getvalue() == ""


def test_empty_turn_renders_nothing() -> None:
    out = io.StringIO()
    timeline = Timeline(_console(out))

    with timeline.live_turn():
        pass

    assert out.getvalue() == ""


def test_live_turn_terminates_partial_output_on_error() -> None:
    out = io.StringIO()
    timeline = Timeline(_console(out))

    with pytest.raises(ValueError, match=r"^x$"), timeline.live_turn():
        _token_then_boom(timeline)

    assert out.getvalue() == "saddle> \n" + "Hi" + " " * 78 + "\n"


def test_render_item_rejects_unknown_kind() -> None:
    with pytest.raises(ValueError, match=r"^unknown timeline kind 'bogus'$") as exc_info:
        Timeline._render_item("bogus", "t")

    assert str(exc_info.value) == "unknown timeline kind 'bogus'"


def test_token_rejects_unknown_stream() -> None:
    out = io.StringIO()
    timeline = Timeline(_console(out))

    with pytest.raises(ValueError, match=r"^unknown token stream 'bogus'$") as exc_info:
        timeline.token("bogus", "Hi!")

    assert str(exc_info.value) == "unknown token stream 'bogus'"
    assert out.getvalue() == ""


def test_ansi_styles_render_ocean_codes() -> None:
    out = io.StringIO()
    console = _console(out, ansi=True)
    timeline = Timeline(console)
    timeline.token("reasoning", "Hmm. ")
    timeline.token("content", "Hi!")
    timeline.tool_call(ToolCall(id="1", name="add", arguments="{}"))
    timeline.tool_result("3", 0)
    timeline.tool_result("bad", 2)

    console.print(timeline._render())

    assert out.getvalue() == (
        "\x1b[2;3;38;2;123;167;204mthinking: Hmm. \x1b[0m\n"
        "\x1b[1;38;2;90;176;250msaddle> \x1b[0m\n"
        "\x1b[97mHi!" + " " * 77 + "\x1b[0m\n"
        "\x1b[35m$ add {}\x1b[0m\n"
        "\x1b[32m3\x1b[0m\n"
        "\x1b[31mbad\x1b[0m\n"
    )
