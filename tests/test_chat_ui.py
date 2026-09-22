"""The chat-mode substrate: labels, memory, the sandbox boundary, tools, sessions.

Each property is pinned by an instance it must accept and one it must
refuse. The refusals are the point: a boundary that has never been tested
against an escape is a boundary nobody has checked.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from saddle.engine import MIN_OUTPUT, TurnOptions
from saddle.labels import describe, label_for
from saddle.memory import KEEP_RECENT, compact, estimate_tokens
from saddle.sandbox import OutsideRootError, Sandbox, resolve_within
from saddle.sessions import SessionStore
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
    run = lambda c: execute_tool(c, workdir=root, context=ctx)  # noqa: E731
    assert "return 42" in run(call("read_file", path="pkg/a.py"))
    assert "pkg/a.py:1:" in run(call("search", query="hello"))
    assert "pkg/" in run(call("list_dir"))


def test_every_tool_failure_is_an_error_string_not_an_exception(
    workspace: tuple[Path, ToolContext],
) -> None:
    root, ctx = workspace
    run = lambda c: execute_tool(c, workdir=root, context=ctx)  # noqa: E731
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
    run = lambda c: execute_tool(c, workdir=root, context=ctx)  # noqa: E731
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
