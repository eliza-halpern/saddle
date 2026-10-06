"""The chat-mode substrate: labels, memory, the sandbox boundary, tools, sessions.

Each property is pinned by an instance it must accept and one it must
refuse. The refusals are the point: a boundary that has never been tested
against an escape is a boundary nobody has checked.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

from saddle.engine import MIN_OUTPUT, TurnOptions, _user_message
from saddle.labels import describe, label_for
from saddle.memory import KEEP_RECENT, compact, estimate_tokens
from saddle.sandbox import OutsideRootError, Sandbox, resolve_within
from saddle.sessions import BUILTIN_PERSONAS, SessionStore
from saddle.tools import ToolContext, execute_tool
from saddle.vllm import ToolCall


def call(name: str, **kwargs: object) -> ToolCall:
    return ToolCall(id="t1", name=name, arguments=json.dumps(kwargs))


# -- labels -------------------------------------------------------------------


def test_a_tool_call_reads_as_english_in_all_three_tenses() -> None:
    present, past, failed = describe("read_file", '{"path": "pyproject.toml"}')
    assert present == "Reading pyproject.toml"
    assert past == "Read pyproject.toml"
    assert failed == "Failed to read pyproject.toml"


def test_malformed_arguments_still_label_and_never_raise() -> None:
    present, _, failed = describe("read_file", "{not json")
    assert present == "Reading"
    assert "read" in failed


def test_an_unlabelled_tool_degrades_readably_rather_than_to_json() -> None:
    assert describe("some_new_tool", '{"path": "x.txt"}')[0] == "Calling some_new_tool x.txt"


def test_state_selects_the_tense() -> None:
    args = '{"path":"x"}'
    assert label_for("read_file", args, ok=None) == "Reading x"
    assert label_for("read_file", args, ok=True) == "Read x"
    assert label_for("read_file", args, ok=False) == "Failed to read x"


# -- memory -------------------------------------------------------------------


def test_a_short_conversation_is_left_alone() -> None:
    messages = [{"role": "user", "content": "hi"}]
    assert compact(messages, limit_tokens=1000) == (0, "")
    assert messages == [{"role": "user", "content": "hi"}]


def test_compaction_never_drops_the_system_prompt_or_the_recent_tail() -> None:
    messages = [{"role": "system", "content": "you are saddle"}]
    for index in range(30):
        messages.append({"role": "user", "content": f"q{index} " + "x" * 200})
        messages.append({"role": "assistant", "content": f"a{index} " + "y" * 200})
    dropped, _ = compact(messages, limit_tokens=500)
    assert dropped > 0
    assert messages[0]["content"] == "you are saddle"
    assert messages[-1]["content"].startswith("a29")


def test_compaction_announces_itself_in_band() -> None:
    messages = [{"role": "user", "content": "x" * 4000} for _ in range(20)]
    compact(messages, limit_tokens=100)
    assert any("compacted" in (m.get("content") or "") for m in messages)


def test_a_huge_tool_result_is_elided_head_and_tail_before_anything_is_dropped() -> None:
    messages: list[dict[str, object]] = [{"role": "system", "content": "s"}]
    messages.append({"role": "tool", "tool_call_id": "1", "content": "Z" * 40_000})
    messages += [{"role": "user", "content": f"recent {i}"} for i in range(KEEP_RECENT)]
    compact(messages, limit_tokens=2000)
    body = str(messages[1]["content"])
    assert "elided by compaction" in body
    assert body.startswith("Z")
    assert body.endswith("Z")


def test_compaction_stops_rather_than_emptying_a_list_of_protected_messages() -> None:
    messages = [{"role": "system", "content": "S" * 100_000}]
    assert compact(messages, limit_tokens=10) == (0, "large tool results elided")
    assert len(messages) == 1
    assert estimate_tokens(messages) > 10


def test_compaction_names_a_dropped_text_turn_by_its_first_line() -> None:
    # The text-only twin of the image test below (its known-good pair):
    # a dropped plain-text user turn is named in the summary, and the note
    # takes the slot right after the system prompt.
    messages: list[dict[str, object]] = [{"role": "system", "content": "you are saddle"}]
    messages.append({"role": "user", "content": "first question " + "x" * 400})
    for index in range(12):
        messages.append({"role": "assistant", "content": "a" * 400})
        messages.append({"role": "user", "content": f"question {index} " + "q" * 400})

    limit = estimate_tokens(messages) - 250
    dropped, summary = compact(messages, limit_tokens=limit)

    assert dropped == 4  # three left the note over the limit (#122): the note is reserved now
    assert estimate_tokens(messages) <= limit
    assert summary.startswith("4 earlier message(s) compacted: first question")
    assert "Earlier conversation compacted" in str(messages[1]["content"])


def test_compaction_survives_an_image_in_the_dropped_turn(tmp_path: Path) -> None:
    # The first user turn is built by the production `_user_message`, which
    # makes an uploaded image a list of content parts, not a hand-written
    # dict: `.strip()` on that list is what raised AttributeError.
    shot = tmp_path / "shot.png"
    shot.write_bytes(b"\x89PNG\r\n\x1a\n")
    messages: list[dict[str, object]] = [{"role": "system", "content": "you are saddle"}]
    messages.append(_user_message("look at this screenshot", [shot]))
    for index in range(12):
        messages.append({"role": "assistant", "content": "a" * 400})
        messages.append({"role": "user", "content": f"question {index} " + "q" * 400})
    before = len(messages)
    limit = estimate_tokens(messages) - 250

    dropped, summary = compact(messages, limit_tokens=limit)  # must not raise

    # The image part costs IMAGE_TOKENS regardless of the fixture, so how
    # many messages go is computed here, not hard-coded.
    assert dropped == before - len(messages) + 1  # +1: the note that was inserted
    assert summary.split(": ", 1)[1].startswith("look at this screenshot")
    note = next(m for m in messages if "Earlier conversation compacted" in str(m.get("content")))
    assert note is not None


# -- the sandbox boundary -----------------------------------------------------


def test_a_path_inside_the_root_resolves(tmp_path: Path) -> None:
    (tmp_path / "a.txt").write_text("hi")
    assert resolve_within(tmp_path, "a.txt").name == "a.txt"


def test_a_parent_escape_is_refused(tmp_path: Path) -> None:
    with pytest.raises(OutsideRootError):
        resolve_within(tmp_path, "../../etc/passwd")


def test_a_symlink_pointing_out_of_the_tree_is_refused(tmp_path: Path) -> None:
    outside = tmp_path.parent / "outside_secret.txt"
    outside.write_text("secret")
    root = tmp_path / "root"
    root.mkdir()
    (root / "link").symlink_to(outside)
    with pytest.raises(OutsideRootError):
        resolve_within(root, "link")


def test_a_command_runs_and_reports_its_exit_code(tmp_path: Path) -> None:
    box = Sandbox.for_workdir(tmp_path)
    assert box.run("exit 7", timeout=30).exit_code == 7


def test_a_short_wait_does_not_kill_the_command(tmp_path: Path) -> None:
    box = Sandbox.for_workdir(tmp_path)
    terminal = box.start("sleep 1.5; echo done")
    assert box.wait(terminal.id, timeout=0.2).running
    finished = box.wait(terminal.id, timeout=20)
    assert not finished.running
    assert "done" in finished.output()


def test_runaway_output_is_capped_head_and_tail(tmp_path: Path) -> None:
    box = Sandbox.for_workdir(tmp_path)
    output = box.run("head -c 900000 /dev/zero | tr '\\0' 'x'", timeout=60).output()
    assert len(output) < 500_000
    assert "elided" in output


# -- tools --------------------------------------------------------------------


@pytest.fixture
def workspace(tmp_path: Path) -> tuple[Path, ToolContext]:
    (tmp_path / "pkg").mkdir()
    (tmp_path / "pkg" / "a.py").write_text("def hello():\n    return 42\n")
    return tmp_path, ToolContext(workdir=tmp_path)


def test_tools_read_search_and_list(workspace: tuple[Path, ToolContext]) -> None:
    root, ctx = workspace

    def run(c: ToolCall) -> str:
        return execute_tool(c, workdir=root, context=ctx)

    assert "return 42" in run(call("read_file", path="pkg/a.py"))
    assert "pkg/a.py:1:" in run(call("search", query="hello"))
    assert "pkg/" in run(call("list_dir"))


def test_every_tool_failure_is_an_error_string_not_an_exception(
    workspace: tuple[Path, ToolContext],
) -> None:
    root, ctx = workspace

    def run(c: ToolCall) -> str:
        return execute_tool(c, workdir=root, context=ctx)

    assert run(call("read_file", path="../../etc/passwd")).startswith("error:")
    assert run(call("write_file", path="/etc/evil", content="x")).startswith("error:")
    assert run(call("read_file")) == "error: read_file needs a string path argument"
    assert run(ToolCall(id="t", name="read_file", arguments="{bad")).startswith("error: arguments")
    assert run(call("nope")).startswith("error: unknown tool")
    assert run(call("read_terminal", id="nope")).startswith("error: no terminal")


def test_a_background_command_returns_immediately_and_can_be_waited_on(
    workspace: tuple[Path, ToolContext],
) -> None:
    root, ctx = workspace

    def run(c: ToolCall) -> str:
        return execute_tool(c, workdir=root, context=ctx)

    started = run(call("run_command", command="sleep 1.5; echo late", background=True))
    assert "started terminal" in started
    terminal_id = started.split("terminal ")[1].split()[0]
    waited = run(call("wait_for_terminal", id=terminal_id, timeout=1))
    assert "still running" in waited
    assert "not killed" in waited
    finished = run(call("wait_for_terminal", id=terminal_id, timeout=20))
    assert finished.startswith("exit 0")
    assert "late" in finished


# -- sessions -----------------------------------------------------------------


def test_a_session_round_trips_with_its_messages(tmp_path: Path) -> None:
    store = SessionStore(tmp_path)
    session = store.create(title="First", workdir=str(tmp_path))
    store.save_messages(session.id, [{"role": "user", "content": "hi"}])
    assert store.get(session.id).title == "First"
    assert store.load_messages(session.id) == [{"role": "user", "content": "hi"}]


def test_a_torn_message_tail_loses_one_message_not_the_session(tmp_path: Path) -> None:
    store = SessionStore(tmp_path)
    session = store.create(workdir=str(tmp_path))
    store.messages_path(session.id).write_text('{"role":"user","content":"ok"}\n{"role":"as')
    assert store.load_messages(session.id) == [{"role": "user", "content": "ok"}]


def test_an_explicit_system_prompt_overrides_the_persona(tmp_path: Path) -> None:
    store = SessionStore(tmp_path)
    session = store.create(workdir=str(tmp_path), persona="engineer")
    assert store.get(session.id).prompt_text().startswith("You are a careful")
    store.update(session.id, system_prompt="custom")
    assert store.get(session.id).prompt_text() == "custom"


def test_the_reviewer_persona_is_what_the_model_is_sent_until_the_user_edits_it(
    tmp_path: Path,
) -> None:
    # Structural only: which text reaches the model. What the text makes a
    # model do is measured by replaying a fixed review case, not asserted
    # on the wording here.
    store = SessionStore(tmp_path)
    session = store.create(workdir=str(tmp_path), persona="reviewer")
    builtin = BUILTIN_PERSONAS["reviewer"]
    assert builtin.strip()
    assert store.get(session.id).prompt_text() == builtin
    assert store.get(session.id).prompt_text(store.personas()) == builtin
    store.save_persona("reviewer", "my own reviewer")
    assert store.get(session.id).prompt_text(store.personas()) == "my own reviewer"
    # Deleting the user's entry restores the builtin.
    store.delete_persona("reviewer")
    assert store.get(session.id).prompt_text(store.personas()) == builtin


def test_a_traversing_session_id_is_refused(tmp_path: Path) -> None:
    store = SessionStore(tmp_path)
    with pytest.raises(ValueError, match="invalid session id"):
        store._dir("../../etc")


# -- the reply budget ---------------------------------------------------------


def test_a_fresh_turn_gets_nearly_the_whole_window_not_a_flat_cap() -> None:
    options = TurnOptions(context_tokens=175_000)
    assert options.budget([]) > 150_000


def test_the_budget_shrinks_as_the_conversation_fills() -> None:
    options = TurnOptions(context_tokens=175_000)
    empty = options.budget([])
    full = options.budget([{"role": "user", "content": "x" * 400_000}])
    assert full < empty


def test_the_budget_never_drops_below_a_usable_floor() -> None:
    options = TurnOptions(context_tokens=10_000)
    assert options.budget([{"role": "user", "content": "x" * 10_000_000}]) >= MIN_OUTPUT


def test_an_explicit_max_tokens_still_wins() -> None:
    assert TurnOptions(max_tokens=4096).budget([]) == 4096


# -- live terminal output -----------------------------------------------------


def test_background_output_is_pushed_to_a_listener_as_it_arrives(tmp_path: Path) -> None:
    seen: list[tuple[str, str]] = []
    box = Sandbox.for_workdir(tmp_path, on_output=lambda tid, chunk: seen.append((tid, chunk)))
    terminal = box.start("echo one; echo two; echo three")
    box.wait(terminal.id, timeout=20)
    assert [chunk for _, chunk in seen] == ["one\n", "two\n", "three\n"]
    assert {tid for tid, _ in seen} == {terminal.id}


def test_a_broken_listener_does_not_stop_the_command(tmp_path: Path) -> None:
    def explode(_tid: str, _chunk: str) -> None:
        msg = "listener is broken"
        raise RuntimeError(msg)

    box = Sandbox.for_workdir(tmp_path, on_output=explode)
    terminal = box.wait(box.start("echo still ran").id, timeout=20)
    assert terminal.exit_code == 0
    assert "still ran" in terminal.output()


# -- edit_file ----------------------------------------------------------------


def test_edit_file_replaces_one_snippet_and_returns_a_diff(tmp_path: Path) -> None:
    target = tmp_path / "m.py"
    target.write_text("def hello():\n    return 1\n")
    result = execute_tool(
        ToolCall(
            id="t",
            name="edit_file",
            arguments=json.dumps({"path": "m.py", "old": "return 1", "new": "return 42"}),
        ),
        workdir=tmp_path,
        context=ToolContext(workdir=tmp_path),
    )
    assert target.read_text() == "def hello():\n    return 42\n"
    assert result.startswith("--- a/m.py")
    assert "+    return 42" in result
    assert "-    return 1" in result


def test_an_ambiguous_edit_is_refused_rather_than_applied_to_the_first_hit(
    tmp_path: Path,
) -> None:
    target = tmp_path / "m.py"
    target.write_text("x = 1\ny = 1\n")
    before = target.read_text()
    result = execute_tool(
        ToolCall(
            id="t",
            name="edit_file",
            arguments=json.dumps({"path": "m.py", "old": "= 1", "new": "= 2"}),
        ),
        workdir=tmp_path,
        context=ToolContext(workdir=tmp_path),
    )
    assert "appears 2 times" in result
    assert target.read_text() == before


def test_an_edit_whose_snippet_is_absent_is_an_error(tmp_path: Path) -> None:
    (tmp_path / "m.py").write_text("x = 1\n")
    result = execute_tool(
        ToolCall(
            id="t",
            name="edit_file",
            arguments=json.dumps({"path": "m.py", "old": "nope", "new": "x"}),
        ),
        workdir=tmp_path,
        context=ToolContext(workdir=tmp_path),
    )
    assert "does not appear" in result


def _edit(tmp_path: Path, old: str, new: str, name: str = "m.py") -> str:
    return execute_tool(
        ToolCall(
            id="t",
            name="edit_file",
            arguments=json.dumps({"path": name, "old": old, "new": new}),
        ),
        workdir=tmp_path,
        context=ToolContext(workdir=tmp_path),
    )


def test_a_snippet_that_drops_a_blank_line_still_names_its_one_site(tmp_path: Path) -> None:
    """The known-good half: a correct edit an exact count refused.

    This `old` is a live draw from the local model against this exact file.
    It reproduced every significant line correctly and omitted the blank line
    before `def deposit`, and `before.count(old)` was therefore 0 -- reported
    as "does not appear", which sends the caller looking for a typo that is
    not there. Reproducing blank lines byte for byte was never the contract;
    naming one site is.
    """
    fixtures = Path(__file__).parent / "fixtures"
    target = tmp_path / "accounts.py"
    target.write_text((fixtures / "edit_target_accounts.txt").read_text())
    block = (fixtures / "edit_blank_line_slip.txt").read_text()
    lines = block.split("\n")
    old = "\n".join(line[1:] for line in lines if line.startswith("-"))
    new = "\n".join(line[1:] for line in lines if line.startswith("+"))
    body = target.read_text()
    assert body.count(old) == 0  # the exact count that refused it
    assert "    return self._balance\n\n    def deposit" in body  # the dropped line

    result = _edit(tmp_path, old, new, name="accounts.py")

    assert not result.startswith("error:"), result
    after = target.read_text()
    assert "def currencies(self):" in after  # the method the draw was adding
    assert after.count("def deposit(self, amount):") == 1  # not duplicated
    assert body.replace(old.replace("    def deposit", "\n    def deposit"), new) == after


def test_a_snippet_that_loosely_matches_two_sites_is_still_refused(tmp_path: Path) -> None:
    """The known-bad half: loosening the match must not loosen uniqueness.

    Neither site matches exactly -- each has a blank line the snippet omits --
    so both are reachable only through the loose path. The edit must be
    refused, and refused with its own wording: "does not appear" would send
    the caller hunting a typo when the real problem is that it appears twice.
    """
    target = tmp_path / "m.py"
    target.write_text(
        "def a():\n    x = 1\n\n    return x\n\n\ndef b():\n    x = 1\n\n    return x\n"
    )
    before = target.read_text()

    result = _edit(tmp_path, "    x = 1\n    return x", "    return 2")

    assert "matches 2 places" in result
    assert target.read_text() == before


def test_an_exact_match_is_used_even_where_a_loose_one_would_be_ambiguous(
    tmp_path: Path,
) -> None:
    """Exactness still wins: the loose path is a fallback, not a re-ranking.

    `old` occurs exactly once, and loosely twice. Falling through to the loose
    matcher would refuse an edit that names its site precisely.
    """
    target = tmp_path / "m.py"
    target.write_text(
        "def a():\n    x = 1\n    return x\n\n\ndef b():\n    x = 1\n\n    return x\n"
    )

    result = _edit(tmp_path, "    x = 1\n    return x", "    return 2")

    assert not result.startswith("error:"), result
    assert target.read_text() == "def a():\n    return 2\n\n\ndef b():\n    x = 1\n\n    return x\n"


def test_overwriting_an_existing_file_reports_a_diff_not_just_a_filename(
    tmp_path: Path,
) -> None:
    (tmp_path / "m.py").write_text("a\nb\n")
    result = execute_tool(
        ToolCall(
            id="t", name="write_file", arguments=json.dumps({"path": "m.py", "content": "a\nc\n"})
        ),
        workdir=tmp_path,
        context=ToolContext(workdir=tmp_path),
    )
    assert result.startswith("--- a/m.py")
    assert "+c" in result


def test_a_brand_new_file_reports_creation_rather_than_a_diff_against_nothing(
    tmp_path: Path,
) -> None:
    result = execute_tool(
        ToolCall(
            id="t",
            name="write_file",
            arguments=json.dumps({"path": "new.py", "content": "x = 1\n"}),
        ),
        workdir=tmp_path,
        context=ToolContext(workdir=tmp_path),
    )
    assert result.startswith("created 'new.py'")


def test_markdown_renderer_suite_passes() -> None:
    """The browser-side renderer has its own tests; run them with the rest.

    A test suite that needs a separate command is one nobody runs. Node
    carries the runner, so this costs a subprocess and keeps the transcript
    renderer covered by `pytest -q` like everything else.
    """
    node = shutil.which("node")
    if node is None:
        pytest.skip("node is not installed")
    suite = Path(__file__).parent / "markdown.test.js"
    result = subprocess.run(
        [node, "--test", str(suite)],
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr


# -- labels: the noun phrase a row points at ----------------------------------


def test_listing_the_working_folder_reads_as_a_sentence_not_a_fragment() -> None:
    # "Listed" alone is a fragment; the row has to say what was listed.
    assert describe("list_dir", "{}")[1] == "Listed this folder"
    assert describe("list_dir", '{"path": "src"}')[1] == "Listed src"


def test_a_terminal_is_named_by_its_id() -> None:
    assert describe("read_terminal", '{"id": "9f2a"}')[0] == "Reading terminal 9f2a"
    assert describe("wait_for_terminal", '{"id": "9f2a"}')[1] == "Waited for terminal 9f2a"


def test_a_command_is_summarised_by_its_head_not_its_whole_line() -> None:
    long_command = "pytest -q tests/ -x --no-cov --tb=short -p no:randomly --maxfail=1"
    present = describe("run_command", json.dumps({"command": long_command}))[0]
    assert present == "Running pytest -q tests/ -x --no-cov --tb=short"


def test_an_unquotable_command_still_labels_rather_than_raising() -> None:
    # shlex.split raises on an unbalanced quote, and an apostrophe is enough
    # to make one: `echo don't` crashed the label, which propagated out of
    # run_turn and ended the whole turn over a decoration.
    assert describe("run_command", json.dumps({"command": "echo don't"}))[0] == (
        "Running echo don't"
    )
    assert describe("run_command", json.dumps({"command": 'grep "TODO'}))[1] == ('Ran grep "TODO')


def test_a_search_shows_the_query_quoted_so_whitespace_is_visible() -> None:
    assert describe("search", '{"query": "def  run"}')[0] == ("Searching for 'def  run'")


def test_arguments_that_are_not_an_object_are_treated_as_absent() -> None:
    # The model can emit a bare list or string; a label must never raise.
    assert describe("read_file", "[1, 2, 3]")[0] == "Reading"
    assert describe("read_file", '"just a string"')[0] == "Reading"
    assert describe("read_file", "")[0] == "Reading"


# -- memory: the cheap paths --------------------------------------------------


def test_a_short_tool_result_is_returned_unchanged() -> None:
    from saddle.memory import _truncate_result

    assert _truncate_result("short") == "short"


def test_an_image_is_charged_a_flat_rate_not_its_base64_length() -> None:
    # A photo is megabytes of base64; charging it by length would evict the
    # entire conversation around it.
    from saddle.memory import IMAGE_TOKENS

    huge = "A" * 400_000
    message = [
        {
            "role": "user",
            "content": [
                {"type": "text", "text": "look"},
                {"type": "image_url", "image_url": {"url": f"data:image/png;base64,{huge}"}},
            ],
        }
    ]
    assert estimate_tokens(message) == 1 + IMAGE_TOKENS


def test_tool_calls_are_counted_against_the_window_too() -> None:
    plain = [{"role": "assistant", "content": "hi"}]
    with_calls = [
        {
            "role": "assistant",
            "content": "hi",
            "tool_calls": [
                {"id": "1", "function": {"name": "read_file", "arguments": '{"path": "x"}'}}
            ],
        }
    ]
    assert estimate_tokens(with_calls) > estimate_tokens(plain)


def test_a_recent_tool_result_is_not_elided_however_large() -> None:
    # Stage 1 only touches results old enough to be out of the recent tail.
    messages = [{"role": "system", "content": "s"}]
    messages += [{"role": "user", "content": "x" * 8_000} for _ in range(KEEP_RECENT - 1)]
    messages.append({"role": "tool", "tool_call_id": "1", "content": "Z" * 40_000})
    compact(messages, limit_tokens=100)
    assert messages[-1]["content"] == "Z" * 40_000


def test_a_dropped_message_with_no_text_contributes_no_topic() -> None:
    # Whitespace-only content still costs tokens, so it can force compaction
    # while having no first line to name. An earlier version of this test used
    # short blank messages, which never reached the limit at all: compact
    # returned (0, "") and the assertion passed having tested nothing.
    messages = [{"role": "user", "content": " " * 8_000} for _ in range(10)]
    messages += [{"role": "user", "content": "recent"} for _ in range(KEEP_RECENT)]
    dropped, summary = compact(messages, limit_tokens=100)
    assert dropped > 0
    assert summary == f"{dropped} earlier message(s) compacted"
    assert ":" not in summary  # no topics to list


def test_a_short_tool_result_is_left_alone_while_a_huge_one_is_elided() -> None:
    # Stage 1 walks every old tool result; only the oversized ones are cut.
    messages: list[dict[str, object]] = [{"role": "system", "content": "s"}]
    messages.append({"role": "tool", "tool_call_id": "small", "content": "brief"})
    messages.append({"role": "tool", "tool_call_id": "big", "content": "Z" * 40_000})
    messages += [{"role": "user", "content": f"recent {i}"} for i in range(KEEP_RECENT)]
    compact(messages, limit_tokens=2_000)
    assert messages[1]["content"] == "brief"
    assert "elided by compaction" in str(messages[2]["content"])


# -- sessions -----------------------------------------------------------------


def test_a_half_written_session_is_skipped_rather_than_breaking_the_list(
    tmp_path: Path,
) -> None:
    store = SessionStore(tmp_path)
    good = store.create(title="Good")
    broken = tmp_path / "broken"
    broken.mkdir()
    (broken / "session.json").write_text("{not json")
    surprising = tmp_path / "surprising"
    surprising.mkdir()
    (surprising / "session.json").write_text('{"unexpected_field": 1}')

    assert [s.id for s in store.list()] == [good.id]


def test_a_directory_with_no_metadata_is_not_a_session(tmp_path: Path) -> None:
    store = SessionStore(tmp_path)
    (tmp_path / "uploads-only").mkdir()
    assert store.list() == []


def test_a_torn_line_in_the_middle_of_a_transcript_is_skipped(tmp_path: Path) -> None:
    store = SessionStore(tmp_path)
    session = store.create()
    store.messages_path(session.id).write_text(
        '{"role": "user", "content": "one"}\n'
        "{half a line\n"
        "\n"
        '{"role": "assistant", "content": "two"}\n'
    )
    assert [m["content"] for m in store.load_messages(session.id)] == ["one", "two"]


# -- the last corners ---------------------------------------------------------


def test_without_bwrap_a_command_still_runs_unwrapped(tmp_path: Path) -> None:
    # Isolation is best-effort: on a box with no bwrap the tool still works,
    # it just is not sandboxed. Silently doing nothing would be worse.
    box = Sandbox.for_workdir(tmp_path, prefer_bwrap=False)
    assert box.isolation == "none"
    assert box._argv("echo hi") == ["bash", "-lc", "echo hi"]
    terminal = box.run("echo hi")
    box.wait(terminal.id, timeout=20)
    assert "hi" in terminal.output()
    assert terminal.exit_code == 0


def test_a_command_that_cannot_start_is_reported_not_raised(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import subprocess

    def refuse(*_a: object, **_k: object) -> None:
        message = "too many open files"
        raise OSError(message)

    box = Sandbox.for_workdir(tmp_path, prefer_bwrap=False)
    monkeypatch.setattr(subprocess, "Popen", refuse)
    terminal = box.run("echo hi")
    assert terminal.exit_code == 127
    assert "could not start command" in terminal.output()
    assert box.terminals[terminal.id] is terminal


def test_killing_a_finished_terminal_is_not_an_error(tmp_path: Path) -> None:
    box = Sandbox.for_workdir(tmp_path, prefer_bwrap=False)
    terminal = box.run("true")
    box.wait(terminal.id, timeout=20)
    assert box.kill(terminal.id) is terminal  # already done, nothing to do


def test_an_unwritable_target_is_an_error_not_a_crash(tmp_path: Path) -> None:
    from saddle.tools import ToolContext as Ctx

    target = tmp_path / "readonly.py"
    target.write_text("x = 1\n")
    target.chmod(0o444)
    try:
        out = execute_tool(
            call("edit_file", path="readonly.py", old="x = 1", new="x = 2"),
            workdir=tmp_path,
            context=Ctx(workdir=tmp_path),
        )
    finally:
        target.chmod(0o644)
    assert out == "error: cannot write 'readonly.py'"


def test_a_handler_that_raises_an_os_error_becomes_an_error_string(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The dispatcher's last net: no tool failure may escape as an exception,
    # because the turn above it would die over one bad call.
    import saddle.tools as tools_module

    def explode(*_a: object, **_k: object) -> str:
        message = "disk went away"
        raise OSError(message)

    monkeypatch.setitem(tools_module._HANDLERS, "read_file", explode)
    out = execute_tool(call("read_file", path="x"), workdir=tmp_path)
    assert out == "error: OSError: disk went away"


def test_an_unknown_field_cannot_be_patched_onto_a_session(tmp_path: Path) -> None:
    # PATCH bodies come from the browser; a client may not attach arbitrary
    # attributes to a session, and a null may not blank a real one.
    store = SessionStore(tmp_path)
    session = store.create(title="Kept")
    updated = store.update(session.id, nonsense="x", title=None, persona="reviewer")
    assert not hasattr(updated, "nonsense")
    assert updated.title == "Kept"
    assert updated.persona == "reviewer"


def test_waiting_on_a_terminal_with_no_process_times_out_rather_than_hanging(
    tmp_path: Path,
) -> None:
    from saddle.sandbox import Terminal

    box = Sandbox.for_workdir(tmp_path, prefer_bwrap=False)
    orphan = Terminal(id="orphan", command="never started", started=0.0)
    box.terminals["orphan"] = orphan
    assert orphan.running is True  # no process, no exit code
    assert box.wait("orphan", timeout=0.3) is orphan
    assert orphan.running is True  # and the wait gave up, not the terminal


def test_a_transcript_with_no_file_yet_is_empty_not_an_error(tmp_path: Path) -> None:
    store = SessionStore(tmp_path)
    session = store.create()
    store.messages_path(session.id).unlink(missing_ok=True)
    assert store.load_messages(session.id) == []


# -- stylesheet invariants ----------------------------------------------------


def _css_rules() -> list[tuple[str, str]]:
    """(selector, declarations) for every rule in the stylesheet."""
    import re

    # Every test runs from a disposable cwd (conftest), so this is anchored
    # to the test file rather than to wherever pytest was started.
    root = Path(__file__).resolve().parents[1]
    css = (root / "src/saddle/web/static/app.css").read_text(encoding="utf-8")
    css = re.sub(r"/\*.*?\*/", "", css, flags=re.DOTALL)
    return [
        (selector.strip(), body)
        for selector, body in re.findall(r"([^{}]+)\{([^{}]*)\}", css)
        if not selector.strip().startswith("@")
    ]


def test_an_absolutely_positioned_pseudo_has_a_positioned_parent() -> None:
    """Otherwise it escapes to the corner of the page.

    `#meter::before` -- the context meter's track -- was `position: absolute;
    left: 0` under a statically positioned `#meter`, so it resolved against
    the initial containing block and painted its 54x4 capsule across the
    top-left of the *page*, straight through the wordmark. It survived being
    blamed on background-clip and then on a missing glyph, because it scales
    with neither and sits nowhere near the element that owns it.
    """
    positioned = {"relative", "absolute", "fixed", "sticky"}
    rules = _css_rules()

    def is_positioned(base: str) -> bool:
        for selector, body in rules:
            if base not in [s.strip() for s in selector.split(",")]:
                continue
            for declaration in body.split(";"):
                name, _, value = declaration.partition(":")
                if name.strip() == "position" and value.strip() in positioned:
                    return True
        return False

    escapees = []
    for selector, body in rules:
        for one in (s.strip() for s in selector.split(",")):
            if "::before" not in one and "::after" not in one:
                continue
            decls = {
                d.split(":", 1)[0].strip(): d.split(":", 1)[1].strip()
                for d in body.split(";")
                if ":" in d
            }
            if decls.get("position") != "absolute":
                continue
            base = one.split("::")[0].strip()
            if base and not is_positioned(base):
                escapees.append(one)

    assert escapees == [], (
        f"absolutely positioned pseudo-elements with no positioned parent: {escapees}"
    )


def test_the_invariant_can_see_a_violation() -> None:
    # The rule above is only worth having if it fails on the shape it exists
    # to catch, so prove it does rather than trusting an empty list.
    import re

    broken = "#thing { color: red }\n#thing::before { position: absolute; left: 0 }\n"
    rules = [(sel.strip(), body) for sel, body in re.findall(r"([^{}]+)\{([^{}]*)\}", broken)]
    base_positions = {
        sel: {
            d.split(":", 1)[0].strip(): d.split(":", 1)[1].strip()
            for d in body.split(";")
            if ":" in d
        }.get("position")
        for sel, body in rules
    }
    assert base_positions["#thing"] is None
    assert base_positions["#thing::before"] == "absolute"


@pytest.mark.parametrize(
    ("name", "arguments", "labels"),
    [
        (
            "screenshot",
            "{}",
            (
                "Taking a screenshot of the screen",
                "Took a screenshot of the screen",
                "Failed to take a screenshot of the screen",
            ),
        ),
        (
            "screenshot",
            '{"window": "Video Configuration"}',
            (
                'Taking a screenshot of "Video Configuration"',
                'Took a screenshot of "Video Configuration"',
                'Failed to take a screenshot of "Video Configuration"',
            ),
        ),
        (
            "screenshot",
            '{"window": "list"}',
            ("Listing open windows", "Listed open windows", "Failed to list open windows"),
        ),
        (
            "computer",
            '{"action": "click", "window": "Video Configuration", "x": 210, "y": 270}',
            (
                'Clicking (210, 270) in "Video Configuration"',
                'Clicked (210, 270) in "Video Configuration"',
                'Failed to click (210, 270) in "Video Configuration"',
            ),
        ),
        (
            "computer",
            '{"action": "key", "window": "Harry Potter", "keys": "alt+Return"}',
            (
                'Pressing alt+Return in "Harry Potter"',
                'Pressed alt+Return in "Harry Potter"',
                'Failed to press alt+Return in "Harry Potter"',
            ),
        ),
        (
            "computer",
            '{"action": "type", "window": "Notes", "text": "say hi"}',
            (
                'Typing 6 characters in "Notes"',
                'Typed 6 characters in "Notes"',
                'Failed to type 6 characters in "Notes"',
            ),
        ),
        (
            "computer",
            '{"action": "focus", "window": "Harry Potter"}',
            ('Focusing "Harry Potter"', 'Focused "Harry Potter"', 'Failed to focus "Harry Potter"'),
        ),
        (
            "computer",
            '{"action": "scroll", "window": "Game", "direction": "up", "amount": 2}',
            (
                'Scrolling up 2 in "Game"',
                'Scrolled up 2 in "Game"',
                'Failed to scroll up 2 in "Game"',
            ),
        ),
        (
            "computer",
            '{"action": "drag", "window": "Notes", "x": 5, "y": 6, "to_x": 300, "to_y": 40}',
            (
                'Dragging from (5, 6) to (300, 40) in "Notes"',
                'Dragged from (5, 6) to (300, 40) in "Notes"',
                'Failed to drag from (5, 6) to (300, 40) in "Notes"',
            ),
        ),
        (
            "computer",
            '{"action": "scroll", "window": "Game"}',
            (
                'Scrolling down 3 in "Game"',
                'Scrolled down 3 in "Game"',
                'Failed to scroll down 3 in "Game"',
            ),
        ),
    ],
)
def test_screen_actions_are_labelled_by_what_they_do_to_which_window(
    name: str, arguments: str, labels: tuple[str, str, str]
) -> None:
    """A live run's page showed only "Called computer": the person could not see
    what was clicked or typed where. Typed text stays out of the headline."""
    assert describe(name, arguments) == labels


def test_a_malformed_screen_action_still_reads_as_english() -> None:
    assert describe("computer", "{not json")[1] == "Acted on the screen"
    assert describe("computer", '{"action": "dance", "window": "x"}')[1] == "Acted on the screen"
