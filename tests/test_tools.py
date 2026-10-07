"""Tool behaviour, under the contract the chat UI needs.

This file was rewritten when the workdir became a **boundary** rather than a
default `cwd` (`sandbox.resolve_within`) and `run_command` moved into the
sandbox. Three parts of the old contract changed deliberately, and each is
pinned here in its new form rather than dropped:

- stdout and stderr are **merged**, because a live terminal shows one
  interleaved stream and splitting them reorders the output a user watches;
- a `run_command` timeout **no longer kills** the command: it reports the
  terminal id so the work can be waited on again, which is what makes a long
  build survive a turn;
- the default timeout is 120s, not 60.

Everything else the old file pinned is still pinned: non-string arguments
are refused rather than coerced, a missing file, a directory read and an
unknown tool are error strings, and every handler is reachable through
`execute_tool`.
"""

from __future__ import annotations

import json
from pathlib import Path

from saddle import tools
from saddle.tools import ToolContext, execute_tool
from saddle.vllm import ToolCall


def run(name: str, workdir: Path, **kwargs: object) -> str:
    call = ToolCall(id="t1", name=name, arguments=json.dumps(kwargs))
    return execute_tool(call, workdir=workdir, context=ToolContext(workdir=workdir))


# -- read_file ----------------------------------------------------------------


def test_read_file_returns_content(tmp_path: Path) -> None:
    (tmp_path / "note.txt").write_text("hello\n")
    assert run("read_file", tmp_path, path="note.txt") == "hello\n"


def test_read_file_rejects_a_non_string_path(tmp_path: Path) -> None:
    assert run("read_file", tmp_path, path=7) == "error: read_file needs a string path argument"
    assert run("read_file", tmp_path) == "error: read_file needs a string path argument"


def test_read_file_missing_file_is_an_error_string(tmp_path: Path) -> None:
    assert run("read_file", tmp_path, path="missing.txt").startswith("error:")


def test_read_file_directory_is_an_error_string(tmp_path: Path) -> None:
    (tmp_path / "sub").mkdir()
    assert run("read_file", tmp_path, path="sub").startswith("error:")


# -- write_file ---------------------------------------------------------------


def test_write_file_creates_parents_and_reports(tmp_path: Path) -> None:
    result = run("write_file", tmp_path, path="sub/deep/note.txt", content="hello\n")
    assert (tmp_path / "sub" / "deep" / "note.txt").read_text() == "hello\n"
    assert "sub/deep/note.txt" in result


def test_write_file_rejects_non_string_arguments(tmp_path: Path) -> None:
    assert run("write_file", tmp_path, path="a.txt", content=7) == (
        "error: write_file needs a string content argument"
    )
    assert run("write_file", tmp_path) == "error: write_file needs a string path argument"


def test_write_file_directory_is_an_error_string(tmp_path: Path) -> None:
    (tmp_path / "sub").mkdir()
    assert run("write_file", tmp_path, path="sub", content="x").startswith("error:")


# -- the workdir is a boundary, not a default ---------------------------------


def test_reads_and_writes_outside_the_workdir_are_refused(tmp_path: Path) -> None:
    (tmp_path.parent / "secret_outside.txt").write_text("secret")
    assert run("read_file", tmp_path, path="../secret_outside.txt").startswith("error:")
    assert run("write_file", tmp_path, path="/etc/evil", content="x").startswith("error:")


# -- run_command --------------------------------------------------------------


def test_run_command_reports_exit_and_merged_output(tmp_path: Path) -> None:
    result = run("run_command", tmp_path, command="echo hi")
    assert result.startswith("exit 0")
    assert "hi" in result
    failed = run("run_command", tmp_path, command="echo oops >&2; exit 3")
    assert failed.startswith("exit 3")
    assert "oops" in failed


def test_run_command_runs_in_the_workdir(tmp_path: Path) -> None:
    (tmp_path / "marker.txt").write_text("here\n")
    assert "here" in run("run_command", tmp_path, command="cat marker.txt")


def test_run_command_rejects_a_non_string_command(tmp_path: Path) -> None:
    assert run("run_command", tmp_path, command=7) == (
        "error: run_command needs a string command argument"
    )
    assert run("run_command", tmp_path) == "error: run_command needs a string command argument"


def test_a_run_command_timeout_reports_the_terminal_rather_than_killing_it(
    tmp_path: Path,
) -> None:
    result = run("run_command", tmp_path, command="sleep 4; echo done", timeout=1)
    assert "still running" in result
    assert "terminal" in result


# -- dispatch -----------------------------------------------------------------


def test_execute_tool_dispatches_every_declared_tool(tmp_path: Path) -> None:
    (tmp_path / "note.txt").write_text("hello\n")
    context = ToolContext(workdir=tmp_path)

    def call(name: str, **kwargs: object) -> str:
        return execute_tool(
            ToolCall(id="t", name=name, arguments=json.dumps(kwargs)),
            workdir=tmp_path,
            context=context,
        )

    assert call("read_file", path="note.txt") == "hello\n"
    assert "out.txt" in call("write_file", path="out.txt", content="hi")
    assert "note.txt" in call("list_dir")
    assert "note.txt:1:" in call("search", query="hello")
    started = call("run_command", command="echo x", background=True)
    assert "started terminal" in started
    terminal_id = started.split("terminal ")[1].split()[0]
    assert "x" in call("wait_for_terminal", id=terminal_id, timeout=20)
    assert "[" in call("read_terminal", id=terminal_id)


def test_execute_tool_rejects_bad_arguments(tmp_path: Path) -> None:
    result = execute_tool(
        ToolCall(id="t", name="read_file", arguments="{not json"), workdir=tmp_path
    )
    assert result.startswith("error: arguments are not valid JSON")


def test_execute_tool_rejects_an_unknown_tool(tmp_path: Path) -> None:
    result = execute_tool(ToolCall(id="t", name="nope", arguments="{}"), workdir=tmp_path)
    assert result == "error: unknown tool 'nope'"


# -- limits: what a tool does when the answer is too big ----------------------


def _words(text: str) -> int:
    """A stand-in tokenizer for the tests: one token per whitespace-split word."""
    return len(text.split())


def read(workdir: Path, counter: object = _words, **kwargs: object) -> str:
    """`read_file` with a token counter in its context, as a run has."""
    ctx = ToolContext(workdir=workdir, count_tokens=counter)  # type: ignore[arg-type]
    call = ToolCall(id="c1", name="read_file", arguments=json.dumps(kwargs))
    return execute_tool(call, workdir=workdir, context=ctx)


def test_an_enormous_one_line_file_is_refused_rather_than_flooding_the_window(
    tmp_path: Path,
) -> None:
    """flip: was `test_an_enormous_file_is_truncated_rather_than_flooding_the_window`,
    which cut the text at a character count. The protection it pinned (one
    read cannot flood the context) is kept, measured in the model's own
    tokens: a single line over `READ_TOKENS` is refused with where to look."""
    from saddle.tools import READ_TOKENS

    (tmp_path / "big.txt").write_text("w " * (READ_TOKENS + 5))
    out = read(tmp_path, path="big.txt")
    assert out.startswith("error: line 1 of big.txt alone is ")
    assert f"over the {READ_TOKENS}-token limit" in out


def test_a_window_over_the_token_limit_is_cut_to_fewer_lines(tmp_path: Path) -> None:
    from saddle.tools import READ_TOKENS

    line = "w " * 100 + "\n"  # 100 tokens a line under the stand-in
    (tmp_path / "wide.txt").write_text(line * 300)
    out = read(tmp_path, path="wide.txt")
    head = out.splitlines()[0]
    shown = int(head.split("lines 1-", 1)[1].split(" ", 1)[0])
    assert 1 <= shown * 100 <= READ_TOKENS < 300 * 100
    assert out.endswith(f"read_file with offset={shown + 1} to read on]")


def test_a_short_file_read_whole_is_unchanged(tmp_path: Path) -> None:
    """Known-good: at most `READ_LINES` lines and no window -> the bytes, as before."""
    text = "".join(f"line {i}\n" for i in range(1, 51))
    (tmp_path / "s.py").write_text(text)
    assert read(tmp_path, path="s.py") == text
    assert run("read_file", tmp_path, path="s.py") == text


def test_a_long_file_comes_back_one_window_at_a_time(tmp_path: Path) -> None:
    """Red before: a 2,600-line module came back whole (about 30k tokens in
    one result), filling half the context by minute five of a dogfood run."""
    from saddle.tools import READ_LINES

    total = READ_LINES + 150
    (tmp_path / "long.py").write_text("".join(f"x{i} = {i}\n" for i in range(1, total + 1)))
    out = run("read_file", tmp_path, path="long.py")
    lines = out.splitlines()
    assert lines[0] == f"[long.py: lines 1-{READ_LINES} of {total}]"
    assert lines[1] == "x1 = 1"
    assert lines[READ_LINES] == f"x{READ_LINES} = {READ_LINES}"
    assert lines[-1] == f"[150 more lines: read_file with offset={READ_LINES + 1} to read on]"


def test_offset_and_limit_read_exactly_that_window(tmp_path: Path) -> None:
    """Red before: offset and limit were silently dropped and the whole file
    came back from line 1 (a model asked for lines 180-299 and got 612)."""
    (tmp_path / "t.py").write_text("".join(f"v{i}\n" for i in range(1, 613)))
    out = run("read_file", tmp_path, path="t.py", offset=180, limit=120)
    assert out.splitlines()[0] == "[t.py: lines 180-299 of 612]"
    assert out.splitlines()[1] == "v180"
    assert out.splitlines()[120] == "v299"
    assert out.endswith("[313 more lines: read_file with offset=300 to read on]")
    as_text = run("read_file", tmp_path, path="t.py", offset="180", limit="120")
    assert as_text == out  # numbers sent as strings are read the same
    tail = run("read_file", tmp_path, path="t.py", offset=600)
    assert tail.splitlines()[0] == "[t.py: lines 600-612 of 612]"
    assert "more lines" not in tail


def test_a_window_past_the_end_or_not_a_line_number_is_an_error(tmp_path: Path) -> None:
    (tmp_path / "t.py").write_text("a\nb\n")
    assert run("read_file", tmp_path, path="t.py", offset=9) == (
        "error: offset 9 is past the end of t.py (2 lines)"
    )
    for bad in (0, -3, "abc", 1.5, True):
        out = run("read_file", tmp_path, path="t.py", limit=bad)
        assert out == "error: read_file limit must be a whole number of at least 1", bad


def test_an_argument_a_tool_does_not_declare_is_refused_by_name(tmp_path: Path) -> None:
    """Red before: unknown arguments were dropped without a word."""
    (tmp_path / "t.py").write_text("a\n")
    assert run("read_file", tmp_path, path="t.py", start_line=3) == (
        "error: read_file does not take start_line; its arguments are limit, offset, path"
    )
    assert run("list_dir", tmp_path, path=".", recursive=True).startswith(
        "error: list_dir does not take recursive; its arguments are "
    )


def test_an_enormous_new_file_is_shown_truncated_but_counted_in_full(
    tmp_path: Path,
) -> None:
    # A created file's result carries the file itself, capped like a diff;
    # the byte count still says how much was actually written.
    from saddle.tools import MAX_BODY

    content = "x" * (MAX_BODY + 500)
    out = run("write_file", tmp_path, path="big.txt", content=content)
    assert (tmp_path / "big.txt").read_text() == content
    assert out == (
        f"created 'big.txt' ({MAX_BODY + 500} bytes)\n"
        + "x" * MAX_BODY
        + f"\n[... truncated at {MAX_BODY} characters ...]"
    )


def test_an_enormous_diff_is_truncated(tmp_path: Path) -> None:
    from saddle.tools import MAX_DIFF

    (tmp_path / "big.py").write_text("\n".join(f"old line {i}" for i in range(4_000)))
    out = run(
        "write_file",
        tmp_path,
        path="big.py",
        content="\n".join(f"new line {i}" for i in range(4_000)),
    )
    assert out.endswith(f"\n[... diff truncated at {MAX_DIFF} characters ...]")


def test_a_search_stops_at_its_cap_and_says_there_are_more(tmp_path: Path) -> None:
    from saddle.tools import MAX_MATCHES

    (tmp_path / "many.txt").write_text("\n".join("needle" for _ in range(MAX_MATCHES + 50)))
    out = run("search", tmp_path, query="needle")
    assert out.endswith("\n[... more matches not shown ...]")
    assert len(out.splitlines()) == MAX_MATCHES + 1


def test_a_search_with_no_matches_says_so_rather_than_returning_nothing(
    tmp_path: Path,
) -> None:
    (tmp_path / "a.txt").write_text("something else\n")
    assert run("search", tmp_path, query="zzz-absent") == "no matches for 'zzz-absent'"


def test_a_glob_ending_in_double_star_searches_the_files_under_it(tmp_path: Path) -> None:
    (tmp_path / "web" / "static").mkdir(parents=True)
    (tmp_path / "web" / "static" / "app.js").write_text("const status = 1;\n")
    out = run("search", tmp_path, query="status", glob="web/**")
    assert out == "web/static/app.js:1: const status = 1;"


def test_a_glob_that_matches_no_files_is_an_error_not_no_matches(tmp_path: Path) -> None:
    (tmp_path / "a.py").write_text("status\n")
    out = run("search", tmp_path, query="status", glob="nowhere/*.py")
    assert out == (
        "error: glob 'nowhere/*.py' matched no files, so nothing was searched. "
        "Name files, as in src/**/*.py or src/**/*."
    )
    assert run("search", tmp_path, query="absent", glob="*.py") == "no matches for 'absent'"


def test_a_binary_file_is_skipped_by_search_rather_than_ending_it(
    tmp_path: Path,
) -> None:
    (tmp_path / "blob.bin").write_bytes(b"\xff\xfe\x00needle\x00")
    (tmp_path / "ok.txt").write_text("needle here")
    out = run("search", tmp_path, query="needle")
    assert "ok.txt" in out
    assert "blob.bin" not in out


def test_a_write_that_changes_nothing_says_so_rather_than_showing_an_empty_diff(
    tmp_path: Path,
) -> None:
    (tmp_path / "same.py").write_text("unchanged\n")
    assert run("write_file", tmp_path, path="same.py", content="unchanged\n") == (
        "same.py unchanged"
    )


# -- refusals -----------------------------------------------------------------


def test_editing_a_file_that_is_not_there_is_an_error(tmp_path: Path) -> None:
    assert run("edit_file", tmp_path, path="absent.py", old="a", new="b") == (
        "error: cannot read 'absent.py'"
    )


def test_a_near_miss_edit_names_the_line_that_diverged_and_the_files_nearest(
    tmp_path: Path,
) -> None:
    """Known-good: the refusal says which line of the snippet the file lacks and
    which line it holds instead, each in its own role, and changes nothing."""
    source = "def add(a, b):\n    return a - b\n"
    (tmp_path / "calc.py").write_text(source)
    out = run(
        "edit_file",
        tmp_path,
        path="calc.py",
        old="def add(a, b):\n    return a - c\n",
        new="def add(a, b):\n    return a + b\n",
    )
    assert out.startswith("error: that snippet does not appear in 'calc.py'.")
    assert "The snippet's line 2, '    return a - c', is not in the file" in out
    assert "the file's closest line is '    return a - b'" in out  # the file's role
    assert (tmp_path / "calc.py").read_text() == source


def test_a_snippet_with_nothing_close_says_so_and_names_no_nearest_line(tmp_path: Path) -> None:
    """Known-bad guard: with no line within reach, no 'closest line' is offered,
    which would point the caller at code that is not what they meant."""
    source = "def add(a, b):\n    return a - b\n"
    (tmp_path / "calc.py").write_text(source)
    out = run(
        "edit_file", tmp_path, path="calc.py", old="zzqq_unrelated_words_here\n", new="x = 1\n"
    )
    assert "The snippet's line 1, 'zzqq_unrelated_words_here', is not in the file" in out
    assert "no line of the file is close to it" in out
    assert "closest line" not in out
    assert (tmp_path / "calc.py").read_text() == source


def test_the_edit_is_still_applied_or_refused_on_exactly_the_same_inputs(tmp_path: Path) -> None:
    """The divergence text only explains a refusal; it never turns one into an edit
    or an edit into a refusal. An exact snippet applies, a snippet that differs only
    by a dropped blank line still applies, and one that matches twice is refused."""
    (tmp_path / "a.py").write_text("x = 1\n\ny = 2\n")
    assert "error" not in run("edit_file", tmp_path, path="a.py", old="x = 1\n", new="x = 9\n")
    # blank line dropped between the two significant lines: matched loosely, applied
    out = run("edit_file", tmp_path, path="a.py", old="x = 9\ny = 2\n", new="x = 9\ny = 3\n")
    assert not out.startswith("error")
    assert (tmp_path / "a.py").read_text() == "x = 9\ny = 3\n"
    (tmp_path / "b.py").write_text("v = 1\nv = 1\n")
    twice = run("edit_file", tmp_path, path="b.py", old="v = 1\n", new="v = 2\n")
    assert twice.startswith("error: that snippet appears 2 times")
    assert (tmp_path / "b.py").read_text() == "v = 1\nv = 1\n"


def test_edit_file_rejects_non_string_snippets(tmp_path: Path) -> None:
    (tmp_path / "a.py").write_text("x = 1\n")
    assert run("edit_file", tmp_path, path="a.py", old=1, new="b").startswith("error: ")
    assert run("edit_file", tmp_path, path="a.py", old="x", new=2).startswith("error: ")
    assert (tmp_path / "a.py").read_text() == "x = 1\n"


def test_listing_something_that_is_not_a_directory_is_an_error(tmp_path: Path) -> None:
    (tmp_path / "a.txt").write_text("x")
    assert run("list_dir", tmp_path, path="a.txt") == "error: not a directory: a.txt"


def test_list_dir_rejects_a_non_string_path_rather_than_stringifying_it(
    tmp_path: Path,
) -> None:
    assert run("list_dir", tmp_path, path=7).startswith("error: ")
    assert not (tmp_path / "7").exists()


def test_arguments_that_are_not_an_object_are_an_error(tmp_path: Path) -> None:
    result = execute_tool(ToolCall(id="t", name="read_file", arguments="[1, 2]"), workdir=tmp_path)
    assert result == "error: arguments must be a JSON object"


def test_empty_arguments_are_an_empty_object_not_a_crash(tmp_path: Path) -> None:
    result = execute_tool(ToolCall(id="t", name="list_dir", arguments="   "), workdir=tmp_path)
    assert not result.startswith("error: ")


def test_a_missing_required_argument_names_it(tmp_path: Path) -> None:
    result = execute_tool(ToolCall(id="t", name="read_terminal", arguments="{}"), workdir=tmp_path)
    assert result.startswith("error: missing argument")


def test_a_terminal_that_does_not_exist_is_an_error_in_both_tools(
    tmp_path: Path,
) -> None:
    assert run("wait_for_terminal", tmp_path, id="nope") == "error: no terminal 'nope'"
    assert run("read_terminal", tmp_path, id="nope").startswith("error: ")


def test_waiting_on_a_command_that_is_still_running_reports_it_rather_than_lying(
    tmp_path: Path,
) -> None:
    context = ToolContext(workdir=tmp_path)

    def call(name: str, **kwargs: object) -> str:
        return execute_tool(
            ToolCall(id="t", name=name, arguments=json.dumps(kwargs)),
            workdir=tmp_path,
            context=context,
        )

    started = call("run_command", command="sleep 30", background=True)
    terminal_id = started.split("terminal ")[1].split()[0]
    out = call("wait_for_terminal", id=terminal_id, timeout=1)
    assert "still running" in out
    context.box().kill(terminal_id)


def test_a_window_is_the_largest_that_fits_even_with_a_fixed_overhead(tmp_path: Path) -> None:
    """Red before: with a per-call overhead (as a chat template adds) a
    proportional cut undershoots every time, and the old loop gave up after
    four cuts and returned a window still over the limit."""
    from saddle.tools import READ_TOKENS

    def overhead(text: str) -> int:
        return (READ_TOKENS - 10) + text.count("\n")  # 10 lines fit, 11 do not

    calls: list[int] = []

    def counted(text: str) -> int:
        calls.append(1)
        return overhead(text)

    (tmp_path / "o.txt").write_text("x\n" * 300)
    out = read(tmp_path, counter=counted, path="o.txt")
    assert out.splitlines()[0] == "[o.txt: lines 1-10 of 300]"
    assert len(calls) <= 12  # a halving search, not one count per shrink
    assert out.endswith("[290 more lines: read_file with offset=11 to read on]")


# -- a command's output fits the context ---------------------------------------------


def test_a_huge_command_output_reaches_the_model_as_its_head_and_tail(tmp_path: Path) -> None:
    """Live (rung 1, try 6): `ls -la /tmp` returned 404 KB, about 100,000
    tokens, whole; the next request overflowed the window, the server sent
    nothing and the turn ended. Known-good: with no tokenizer at hand, at most
    READ_LINES lines reach the model, the first and the last, with a line
    saying how many went and how to get them. Known-bad: short output is
    unchanged."""
    result = run("run_command", tmp_path, command="seq 1 20000")
    body = result.splitlines()
    assert "1" in body[:3]
    assert body[-1] == "20000"
    assert sum(line.isdigit() for line in body) == tools.READ_LINES
    assert "19600 lines elided" in result
    assert "write the output to a file" in result
    assert run("run_command", tmp_path, command="seq 1 5").endswith("1\n2\n3\n4\n5\n")


def test_with_a_tokenizer_the_output_is_cut_to_the_token_limit() -> None:
    """Counted in tokens when the server's counter answers (here, one per word)."""
    ctx = ToolContext(workdir=Path("."))
    ctx.count_tokens = lambda text: len(text.split())
    text = "".join(f"w{i} x x x x x x x x x\n" for i in range(5000))  # 50,000 tokens
    fitted = tools._fit_output(ctx, text)
    assert len(fitted.split()) <= tools.READ_TOKENS + 40  # the note's own words
    assert fitted.startswith("w0 ")
    assert "w4999 " in fitted
    assert "tokens" in fitted.split("lines elided")[1].split("\n")[0]


def test_a_background_command_s_huge_output_is_cut_too(tmp_path: Path) -> None:
    ctx = ToolContext(workdir=tmp_path)

    def call(name: str, **kwargs: object) -> str:
        return execute_tool(
            ToolCall(id="t1", name=name, arguments=json.dumps(kwargs)),
            workdir=tmp_path,
            context=ctx,
        )

    started = call("run_command", command="seq 1 20000", background=True)
    terminal = started.split("terminal ")[1].split(" ")[0]
    waited = call("wait_for_terminal", id=terminal, timeout=30)
    assert "19600 lines elided" in waited
    assert "19600 lines elided" in call("read_terminal", id=terminal)


def test_a_dangling_link_is_listed_not_a_crash(tmp_path: Path) -> None:
    """Live (rung 2): list_dir of a home folder holding a link to a removed
    Steam path failed whole with FileNotFoundError. Known-good: the other
    entries are listed and the link is shown as broken."""
    (tmp_path / "real.txt").write_text("hi")
    (tmp_path / "gone").symlink_to(tmp_path / "missing" / "steam")
    listed = run("list_dir", tmp_path, path=".")
    assert "real.txt  2" in listed
    assert "gone -> (missing)" in listed
