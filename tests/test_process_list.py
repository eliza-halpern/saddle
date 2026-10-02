"""A session's process list (#136): what its commands start is listed, stoppable,
and stopped when access ends.

Known-good: a program started detached (`setsid`, so no longer in its
command's process group) is listed after the command has returned, survives
the turn, and is stopped when full access is revoked, the lane is left, the
session is deleted, or the person (or the model, through its tool) stops it,
with what was stopped reported.

Known-bad: an unrelated process with the very same command line, started
outside the session, is never listed and never touched, by `stop`, `stop_all`
or the web API; a process group id the session did not start is refused.

Tracking: found by cgroup where a user systemd manager is reachable; without
one it falls back to the command's process group, says so, and does not see a
program that called `setsid`.
"""

from __future__ import annotations

import json
import os
import signal
import subprocess
import time
import uuid
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest
from test_chat_server import _server_of, engine_app
from test_full_access import call, run
from test_memcap import needs_cgroup

from saddle import memcap
from saddle.procs import GRACE_S, ProcessLedger
from saddle.sessions import FULL_ACCESS_CONFIRM, SessionStore
from saddle.tools import (
    PROCESSES_SCHEMA,
    TOOLS,
    ToolContext,
    execute_tool,
    scope_turn,
    tools_for_mode,
)
from saddle.vllm import StreamToken, ToolCall


def marker() -> str:
    """A sleep length no other process has: its command line names this test."""
    return f"{3000 + uuid.uuid4().int % 100000}.{uuid.uuid4().int % 1000:03d}"


def alive(pid: int) -> bool:
    try:
        stat = Path(f"/proc/{pid}/stat").read_text()
    except OSError:
        return False
    return stat[stat.rindex(")") + 2] != "Z"


def pids_running(sleep: str) -> list[int]:
    """Live `sleep <marker>` processes, whoever started them."""
    found = []
    for name in os.listdir("/proc"):
        if name.isdigit():
            try:
                argv = Path(f"/proc/{name}/cmdline").read_bytes().split(b"\0")
            except OSError:
                continue
            if argv[:2] == [b"sleep", sleep.encode()] and alive(int(name)):
                found.append(int(name))
    return found


def wait_for(sleep: str, count: int) -> list[int]:
    for _ in range(100):
        found = pids_running(sleep)
        if len(found) == count:
            return found
        time.sleep(0.05)
    message = f"{sleep}: wanted {count} process(es), saw {pids_running(sleep)}"
    raise AssertionError(message)


def detach(sleep: str) -> str:
    """A command that starts `sleep` detached and returns. The half second lets
    `setsid` leave the command's process group before the command ends: the
    end-of-command cleanup (`Sandbox.start`) kills that group, and a program
    that has not left it yet goes with it."""
    return f"setsid sleep {sleep} >/dev/null 2>&1 & sleep 0.5"


@pytest.fixture
def full(tmp_path: Path) -> Iterator[ToolContext]:
    ctx = ToolContext(workdir=tmp_path, full_access=True, processes=ProcessLedger())
    yield ctx
    ctx.stop_processes()


@dataclass
class Stranger:
    """The unrelated process: same program, same arguments, not the session's."""

    process: subprocess.Popen[bytes]
    sleep: str

    @property
    def pid(self) -> int:
        return self.process.pid

    def poll(self) -> int | None:
        return self.process.poll()


@pytest.fixture
def stranger() -> Iterator[Stranger]:
    sleep = marker()
    process = subprocess.Popen(["sleep", sleep], start_new_session=True)
    yield Stranger(process, sleep)
    process.kill()
    process.wait()


def processes_of(ctx: ToolContext) -> ProcessLedger:
    assert ctx.processes is not None
    return ctx.processes


# -- tracking ---------------------------------------------------------------


@needs_cgroup
def test_a_detached_program_is_listed_survives_the_turn_and_is_stopped_on_revoke(
    full: ToolContext,
) -> None:
    sleep = marker()
    before = time.time()
    result = run(full, "run_command", command=detach(sleep))
    assert "exit 0" in result  # the command returned; the program did not go with it
    (pid,) = wait_for(sleep, 1)
    time.sleep(0.5)  # past the end-of-command cleanup of the command's own group
    assert alive(pid)
    (entry,) = processes_of(full).entries()
    assert entry.command == f"sleep {sleep}"
    assert (entry.pid, entry.id) == (pid, pid)  # its own process group
    assert entry.ran == detach(sleep)
    assert before - 2 <= entry.started <= time.time() + 1
    stopped = full.revoke_full_access()
    assert [e.id for e in stopped] == [pid]
    assert wait_for(sleep, 0) == []


@needs_cgroup
def test_an_unrelated_process_with_the_same_command_is_not_listed_or_touched(
    full: ToolContext, stranger: Stranger
) -> None:
    sleep = stranger.sleep
    run(full, "run_command", command=detach(sleep))
    ours = [pid for pid in wait_for_two(sleep) if pid != stranger.pid]
    assert len(ours) == 1
    (entry,) = processes_of(full).entries()
    assert entry.pid == ours[0]
    full.stop_processes()
    wait_gone(ours[0])
    assert stranger.poll() is None  # untouched
    assert processes_of(full).entries() == []


def wait_for_two(sleep: str) -> list[int]:
    return wait_for(sleep, 2)


def wait_gone(pid: int) -> None:
    for _ in range(100):
        if not alive(pid):
            return
        time.sleep(0.05)
    message = f"{pid} is still running"
    raise AssertionError(message)


def test_without_a_user_systemd_manager_the_command_group_is_tracked_and_the_list_says_so(
    full: ToolContext, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(memcap, "cgroup_problem", lambda: "no user manager here")
    assert processes_of(full).tracking == "process group"
    sleep = marker()
    run(full, "run_command", command=f"sleep {sleep}", background=True)
    (pid,) = wait_for(sleep, 1)
    (entry,) = processes_of(full).entries()
    assert entry.ran == f"sleep {sleep}"
    detached = marker()
    run(full, "run_command", command=detach(detached))
    (gone,) = wait_for(detached, 1)
    try:
        assert [e.ran for e in processes_of(full).entries()] == [f"sleep {sleep}"]  # setsid escapes
    finally:
        os.kill(gone, signal.SIGKILL)
    stopped = full.stop_processes()
    assert [e.pid for e in stopped] == [pid]
    wait_gone(pid)


@needs_cgroup
def test_the_list_is_found_again_from_disk_after_a_restart(
    tmp_path: Path,
) -> None:
    path = tmp_path / "state" / "processes.json"
    first = ToolContext(workdir=tmp_path, full_access=True, processes=ProcessLedger(path))
    sleep = marker()
    run(first, "run_command", command=detach(sleep))
    (pid,) = wait_for(sleep, 1)
    again = ProcessLedger(path)  # a new server process
    try:
        assert [e.pid for e in again.entries()] == [pid]
    finally:
        assert [e.pid for e in again.stop_all()] == [pid]
    wait_gone(pid)
    assert ProcessLedger(path).entries() == []  # and forgets what has ended


def test_a_damaged_list_file_is_an_empty_list(tmp_path: Path) -> None:
    path = tmp_path / "processes.json"
    path.write_text("{not json")
    assert ProcessLedger(path).entries() == []
    path.write_text('[{"unit": "x"}]')
    assert ProcessLedger(path).entries() == []


# -- the model's tool: its own processes only ----------------------------------------------


@needs_cgroup
def test_the_tool_lists_and_stops_the_sessions_own_processes_only(
    full: ToolContext, stranger: Stranger
) -> None:
    assert run(full, "processes", action="list") == "no processes are running from this session"
    sleep = stranger.sleep
    run(full, "run_command", command=detach(sleep))
    (ours,) = [pid for pid in wait_for_two(sleep) if pid != stranger.pid]
    listed = run(full, "processes", action="list")
    assert f"group {ours}: sleep {sleep}" in listed
    assert str(stranger.pid) not in listed
    refused = run(full, "processes", action="stop", id=stranger.pid)
    assert refused.startswith("error:")
    assert "not one of this session's" in refused
    assert stranger.poll() is None
    stopped = run(full, "processes", action="stop", id=ours)
    assert stopped.startswith("stopped:")
    assert f"group {ours}" in stopped
    wait_gone(ours)
    assert stranger.poll() is None
    assert run(full, "processes", action="stop_all") == "nothing was running"


@needs_cgroup
def test_stop_all_through_the_tool_names_what_it_stopped(full: ToolContext) -> None:
    sleeps = [marker(), marker()]
    for sleep in sleeps:
        run(full, "run_command", command=detach(sleep))
    pids = [wait_for(sleep, 1)[0] for sleep in sleeps]
    said = run(full, "processes", action="stop_all")
    assert said.startswith("stopped:")
    assert all(f"group {pid}" in said for pid in pids)
    for pid in pids:
        wait_gone(pid)


@pytest.mark.parametrize(
    ("arguments", "needle"),
    [
        ({"action": "stop"}, "needs an integer id"),
        ({"action": "stop", "id": True}, "needs an integer id"),
        ({"action": "stop", "id": "7"}, "needs an integer id"),
        ({"action": "restart"}, "action must be"),
    ],
)
def test_the_tool_refuses_a_malformed_call(
    full: ToolContext, arguments: dict[str, Any], needle: str
) -> None:
    assert needle in run(full, "processes", **arguments)


def test_the_tool_does_not_exist_for_a_context_without_a_process_list(tmp_path: Path) -> None:
    ctx = ToolContext(workdir=tmp_path)
    assert run(ctx, "processes", action="list") == "error: unknown tool 'processes'"


# -- lanes: the tool is Edit's alone; a task run's tools are exactly as before ---------------


def names(tools: list[dict[str, Any]]) -> list[str]:
    return [t["function"]["name"] for t in tools]


def test_only_an_edit_session_with_a_process_list_is_offered_the_tool(tmp_path: Path) -> None:
    kept = ToolContext(workdir=tmp_path, processes=ProcessLedger())
    bare = ToolContext(workdir=tmp_path)
    assert "processes" in names(scope_turn(kept, "edit"))
    assert kept.allowed is not None
    assert "processes" in kept.allowed
    assert "processes" not in names(scope_turn(kept, "ask"))
    assert "processes" not in names(scope_turn(bare, "edit"))
    assert names(tools_for_mode("edit")) == names(TOOLS)
    assert PROCESSES_SCHEMA not in TOOLS


def test_the_ask_lane_cannot_call_the_tool_even_if_it_emits_the_call(tmp_path: Path) -> None:
    ctx = ToolContext(workdir=tmp_path, processes=ProcessLedger())
    scope_turn(ctx, "ask")
    out = execute_tool(call("processes", action="list"), workdir=tmp_path, context=ctx)
    assert out.startswith("error: refused by the tier-0 guard")


def test_a_task_run_is_offered_no_process_tool_and_its_box_records_nothing(
    tmp_path: Path,
) -> None:
    """The tool list `run_auto` builds starts from `TOOLS`, and its sandbox has no
    ledger: the Task lane's behaviour is exactly what it was."""
    from saddle.sandbox import Sandbox

    assert "processes" not in names(TOOLS)
    box = Sandbox.for_workdir(tmp_path)
    assert box.ledger is None
    box.run("true")
    assert ToolContext(workdir=tmp_path).processes is None


# -- ending access: revoke, leave Edit, delete -------------------------------------------


def _start_detached(sleep: str) -> list[list[Any]]:
    return [
        [
            ToolCall(
                id="d",
                name="run_command",
                arguments=json.dumps({"command": detach(sleep)}),
            )
        ],
        [StreamToken(stream="content", text="started it")],
    ]


@needs_cgroup
@pytest.mark.parametrize("ending", ["revoke", "leave-edit", "delete", "delete-now"])
def test_a_detached_program_is_stopped_when_access_ends_and_the_response_says_so(
    tmp_path: Path, ending: str
) -> None:
    store = SessionStore(tmp_path / "s")
    sleep = marker()
    with engine_app(store, tmp_path, _start_detached(sleep)) as (client, app):
        sid = client.post("/api/sessions", json={"workdir": str(tmp_path)}).json()["id"]
        url = f"/api/sessions/{sid}"
        client.patch(url, json={"mode": "edit"})
        client.post(f"{url}/full-access", json={"on": True, "confirm": FULL_ACCESS_CONFIRM})
        _server_of(app)._run(sid, "start it")
        (pid,) = wait_for(sleep, 1)
        time.sleep(0.5)
        listed = client.get(f"{url}/processes").json()
        assert listed["tracking"] == "cgroup"
        assert [p["pid"] for p in listed["processes"]] == [pid]
        assert listed["processes"][0]["pgid"] == pid
        try:
            reply = {
                "revoke": lambda: client.post(f"{url}/full-access", json={"on": False}),
                "leave-edit": lambda: client.patch(url, json={"mode": "ask"}),
                "delete": lambda: client.delete(url),
                "delete-now": lambda: _delete_now(store, sid, client, url),
            }[ending]().json()
            assert [p["pid"] for p in reply["stopped"]] == [pid], reply
            wait_gone(pid)
        finally:
            if alive(pid):
                os.kill(pid, signal.SIGKILL)


def _delete_now(store: SessionStore, sid: str, client: Any, url: str) -> Any:
    """Removal after the undo window, for a session hidden some other way (the
    page's own trash call has already stopped the processes)."""
    store.trash(sid)
    return client.delete(f"{url}?now=1")


@needs_cgroup
def test_leaving_edit_without_full_access_stops_the_session_commands_too(
    tmp_path: Path,
) -> None:
    store = SessionStore(tmp_path / "s")
    sleep = marker()
    rounds: list[list[Any]] = [
        [
            ToolCall(
                id="d",
                name="run_command",
                arguments=json.dumps({"command": f"sleep {sleep}", "background": True}),
            )
        ],
        [StreamToken(stream="content", text="started it")],
    ]
    with engine_app(store, tmp_path, rounds) as (client, app):
        sid = client.post("/api/sessions", json={"workdir": str(tmp_path)}).json()["id"]
        url = f"/api/sessions/{sid}"
        client.patch(url, json={"mode": "edit"})
        _server_of(app)._run(sid, "start it")
        (pid,) = wait_for(sleep, 1)
        # Other changes, and a lane change that stays in Edit, stop nothing.
        assert client.patch(url, json={"title": "x"}).json()["stopped"] == []
        assert alive(pid)
        reply = client.patch(url, json={"mode": "task"}).json()
        assert len(reply["stopped"]) == 1
        assert reply["stopped"][0]["ran"] == f"sleep {sleep}"
        wait_gone(pid)


@needs_cgroup
def test_the_web_api_stops_one_or_all_and_refuses_a_stranger(
    tmp_path: Path, stranger: Stranger
) -> None:
    store = SessionStore(tmp_path / "s")
    sleep = marker()
    other = marker()
    with engine_app(store, tmp_path, _start_detached(sleep)) as (client, app):
        sid = client.post("/api/sessions", json={"workdir": str(tmp_path)}).json()["id"]
        url = f"/api/sessions/{sid}"
        client.patch(url, json={"mode": "edit"})
        client.post(f"{url}/full-access", json={"on": True, "confirm": FULL_ACCESS_CONFIRM})
        server = _server_of(app)
        server._run(sid, "start one")
        (first,) = wait_for(sleep, 1)
        context = server.live[sid].context
        assert context is not None
        run(context, "run_command", command=detach(other))
        (second,) = wait_for(other, 1)
        refused = client.post(f"{url}/processes/stop", json={"id": stranger.pid})
        assert refused.status_code == 404
        assert stranger.poll() is None
        for bad in ({}, {"id": "x"}, {"id": True}, {"all": False}):
            assert client.post(f"{url}/processes/stop", json=bad).status_code == 404
        assert alive(first)
        assert alive(second)
        one = client.post(f"{url}/processes/stop", json={"id": first}).json()
        assert [p["pid"] for p in one["stopped"]] == [first]
        wait_gone(first)
        assert alive(second)
        everything = client.post(f"{url}/processes/stop", json={"all": True}).json()
        assert [p["pid"] for p in everything["stopped"]] == [second]
        wait_gone(second)
        assert client.get(f"{url}/processes").json()["processes"] == []
        assert stranger.poll() is None


def test_grace_is_short_enough_for_a_page_button() -> None:
    assert GRACE_S <= 2


# -- the corners ------------------------------------------------------------------------------


def test_the_tool_call_has_a_label_in_every_tense() -> None:
    from saddle.labels import describe

    present, past, _failed = describe("processes", '{"action": "list"}')
    assert (present, past) == ("Checking session processes", "Checked session processes")


def test_a_process_that_is_gone_has_no_command_line() -> None:
    from saddle.procs import _cmdline

    gone = subprocess.Popen(["true"])
    gone.wait()
    assert _cmdline(gone.pid) == ""


def test_a_list_that_cannot_be_written_is_still_kept_in_memory(tmp_path: Path) -> None:
    blocker = tmp_path / "file"
    blocker.write_text("not a directory")
    ledger = ProcessLedger(blocker / "processes.json")  # its parent is a file
    sleep = marker()
    ctx = ToolContext(workdir=tmp_path, full_access=True, processes=ledger)
    try:
        run(ctx, "run_command", command=f"sleep {sleep}", background=True)
        wait_for(sleep, 1)
        assert len(ledger.entries()) == 1
    finally:
        ctx.stop_processes()


def test_a_command_with_a_shell_around_it_is_shown_as_the_command_it_was(
    full: ToolContext,
) -> None:
    sleep = marker()
    command = f"sleep {sleep}; true"  # the shell cannot exec its last command, so it stays
    run(full, "run_command", command=command, background=True)
    wait_for(sleep, 1)
    (entry,) = processes_of(full).entries()
    assert (entry.command, entry.processes) == (command, 2)  # the shell and its sleep


def test_termination_survives_a_process_that_is_gone_or_not_ours_and_gives_up_politely(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from saddle import procs

    gone = subprocess.Popen(["true"])
    gone.wait()
    sequence = iter([[gone.pid, 1], []])  # one that exited, one we may not signal (init)
    ProcessLedger._terminate(lambda: next(sequence))
    stubborn = subprocess.Popen(["sleep", marker()])
    monkeypatch.setattr(procs, "GRACE_S", 0.0)
    monkeypatch.setattr(procs, "KILL_S", 0.0)
    try:
        ProcessLedger._terminate(lambda: [stubborn.pid])  # always found: both waits run out
        assert stubborn.wait(timeout=5) is not None
    finally:
        stubborn.kill()


# -- a program a command backgrounds outlives it, in full access only (#136) --------------------

BACKGROUNDED = {
    "bare-ampersand": "sleep {s} &",
    "nohup": "nohup sleep {s} &",
    "disown": "sleep {s} & disown",
}


@needs_cgroup
@pytest.mark.parametrize("how", list(BACKGROUNDED))
def test_in_full_access_a_backgrounded_program_outlives_its_command_is_listed_and_stopped(
    full: ToolContext, how: str
) -> None:
    sleep = marker()
    command = BACKGROUNDED[how].format(s=sleep)
    started = time.monotonic()
    result = run(full, "run_command", command=command, timeout=30)
    assert "exit 0" in result  # the command ended; it did not wait for the program
    assert time.monotonic() - started < 10
    (pid,) = wait_for(sleep, 1)
    time.sleep(0.5)
    assert alive(pid)  # the known-bad this admits: it outlived its command
    (entry,) = processes_of(full).entries()
    assert (entry.pid, entry.ran) == (pid, command)
    stopped = full.revoke_full_access()
    assert [e.pid for e in stopped] == [pid]
    wait_gone(pid)


@pytest.mark.parametrize("how", list(BACKGROUNDED))
def test_a_sandboxed_command_still_ends_every_program_it_backgrounded(
    tmp_path: Path, how: str
) -> None:
    ctx = ToolContext(workdir=tmp_path, processes=ProcessLedger())  # Edit, no full access
    sleep = marker()
    run(ctx, "run_command", command=BACKGROUNDED[how].format(s=sleep) + " sleep 0.5")
    assert ctx.box().outlive is False
    time.sleep(0.5)
    assert pids_running(sleep) == []
    ctx.stop_processes()


def test_a_box_without_a_process_list_never_lets_a_program_outlive_its_command(
    tmp_path: Path,
) -> None:
    from saddle.sandbox import Sandbox

    for unsandboxed in (True, False):
        box = Sandbox.for_workdir(tmp_path, unsandboxed=unsandboxed)
        assert box.outlive is False
        sleep = marker()
        box.run(f"sleep {sleep} >/dev/null 2>&1 & sleep 0.5")
        time.sleep(0.3)
        assert pids_running(sleep) == [], unsandboxed
