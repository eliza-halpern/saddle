"""Shared pytest fixtures: hermetic cwd for every test.

Mutants that drop the `cwd` argument (e.g. `_git_ok`'s `run_argv(argv,
None)`) make git inherit pytest's cwd. Running each test from a
disposable directory keeps those side effects out of the checkout.
"""

from __future__ import annotations

import ctypes
import functools
import os
import re
import signal
import socket
import subprocess
import sys
from collections.abc import Iterator
from pathlib import Path

import pytest

from saddle import evidence
from saddle.journal import SpanRecorder

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

_PRODUCTION_SHOW_ALL = evidence.show_all_mutants
"""The real `show_all_mutants`, saved before `_stub_mutmut` patches it out.

`test_evidence._without_stubbed_mutmut` restores this so tests that run the
real mutmut engine exercise the real lookup, not the PATH-stub replay below.
"""


PR_SET_CHILD_SUBREAPER = 36


@functools.cache
def _subreaper() -> bool:
    """Make this test process adopt its orphaned descendants (Linux prctl).

    A browser test runs a `node` driver that starts Chrome. When the test
    times out, `subprocess` kills the driver and Chrome survives, reparented
    to init: the full gate leaked a Chrome per timeout, which slowed the next
    run into more timeouts (267 were found alive at once). As a subreaper this
    worker keeps them, so `_reap_browsers` can find and kill them, and only
    its own: a parallel worker's browsers are never its descendants."""
    try:
        libc = ctypes.CDLL(None, use_errno=True)
        return bool(libc.prctl(PR_SET_CHILD_SUBREAPER, 1, 0, 0, 0) == 0)
    except (OSError, AttributeError):
        return False


def descendants(pid: int, proc: Path = Path("/proc")) -> list[int]:
    """Every live process whose parent chain reaches `pid`, from /proc."""
    parent: dict[int, int] = {}
    for entry in proc.iterdir():
        if not entry.name.isdigit():
            continue
        try:
            stat = (entry / "stat").read_bytes()
        except OSError:
            continue
        # comm is any bytes a process names itself, spaces, parentheses and bytes that
        # are not UTF-8 included (one under load made this teardown raise and an audit
        # count a failure): read bytes, and take the ppid after the last ")".
        parent[int(entry.name)] = int(stat.rsplit(b")", 1)[1].split()[1])
    found: list[int] = []
    for child, up in parent.items():
        seen = {child}
        while up not in (0, 1, pid) and up in parent and up not in seen:
            seen.add(up)
            up = parent[up]
        if up == pid:
            found.append(child)
    return found


@functools.cache
def _drives_a_browser(path: str) -> bool:
    """A test module that runs a Chrome: through a `tests/fixtures/*_cdp.mjs` driver, or
    through `chrome_page` (whose modules name no driver)."""
    text = Path(path).read_text(encoding="utf-8")
    return "_cdp.mjs" in text or "chrome_page" in text


@pytest.fixture(autouse=True)
def _reap_browsers(request: pytest.FixtureRequest) -> Iterator[None]:
    """After a browser test, kill any process it left behind, by PID."""
    module = getattr(request.module, "__file__", None)
    if module is None or not _drives_a_browser(module) or not _subreaper():
        yield
        return
    yield
    for pid in descendants(os.getpid()):
        try:
            os.kill(pid, signal.SIGKILL)
        except ProcessLookupError:
            pass


@functools.cache
def black_hole_display() -> str:
    """An X display this process holds that accepts connections and never
    answers, as `:N`: an abstract socket, so no file is made and no real
    display is touched. One per test process, kept open for its life."""
    for number in range(90, 1000):
        hole = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        try:
            hole.bind(f"\0/tmp/.X11-unix/X{number}")
        except OSError:
            hole.close()
            continue
        hole.listen(64)
        _HOLES.append(hole)
        return f":{number}"
    taken = "no free X display number for the suite's black hole"
    raise RuntimeError(taken)


_HOLES: list[socket.socket] = []


@pytest.fixture(autouse=True)
def _no_real_display(monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep the suite off the person's screen: `DISPLAY` names a display that
    never answers and `WAYLAND_DISPLAY` is gone. A test that reaches for a
    display then hangs where it would have touched the desktop, so it fails
    instead of passing by luck. Test Chrome once inherited `DISPLAY`, and every
    Chrome test hung the day the desktop's X server stopped accepting. A test
    about displays sets its own."""
    monkeypatch.setenv("DISPLAY", black_hole_display())
    monkeypatch.delenv("WAYLAND_DISPLAY", raising=False)


@pytest.fixture(autouse=True)
def _empty_cwd(tmp_path_factory: pytest.TempPathFactory, monkeypatch: pytest.MonkeyPatch) -> None:
    """Run each test with cwd in a dedicated empty tmp dir."""
    monkeypatch.chdir(tmp_path_factory.mktemp("cwd"))


@pytest.fixture(autouse=True)
def _no_real_mcp_allowlist(
    tmp_path_factory: pytest.TempPathFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Keep the suite away from the operator's MCP allowlist and approvals
    (`mcpclient`): a test that attaches MCP to a session must never start a
    server the person configured, and every capability starts switched off."""
    nowhere = tmp_path_factory.mktemp("no-mcp")
    monkeypatch.setenv("SADDLE_MCP_CONFIG", str(nowhere / "mcp.json"))
    monkeypatch.setenv("SADDLE_MCP_APPROVALS", str(nowhere / "mcp-approved.json"))
    monkeypatch.setenv("SADDLE_CAPABILITIES_FILE", str(nowhere / "capabilities.json"))
    monkeypatch.delenv("SADDLE_CAPABILITIES", raising=False)
    monkeypatch.delenv("SADDLE_SEARCH_URL", raising=False)
    monkeypatch.setenv("SADDLE_BRAVE_ENV_FILE", str(nowhere / "brave.env"))
    monkeypatch.setenv("SADDLE_BRAVE_STATE", str(nowhere / "brave-budget.json"))
    monkeypatch.delenv("SADDLE_BRAVE_API_KEY", raising=False)
    monkeypatch.delenv("SADDLE_BRAVE_MONTHLY_QUOTA", raising=False)
    monkeypatch.setenv("SADDLE_BRAVE_ANSWERS_STATE", str(nowhere / "brave-answers.json"))
    monkeypatch.delenv("SADDLE_BRAVE_ANSWERS_API_KEY", raising=False)
    monkeypatch.delenv("SADDLE_BRAVE_ANSWERS_CAP", raising=False)


@pytest.fixture(autouse=True)
def _no_real_searx_pace(monkeypatch: pytest.MonkeyPatch) -> None:
    """Give each test its own process-wide SearXNG limiter on a clock its sleeps
    advance. The real one would make every search test sleep two real seconds,
    and would carry one test's requests into the next test's pace."""
    from saddle import searx

    now = [0.0]

    def sleep(seconds: float) -> None:
        now[0] += seconds

    monkeypatch.setattr(searx, "_SHARED", searx.SearxLimiter(clock=lambda: now[0], sleep=sleep))


@pytest.fixture(autouse=True)
def _no_real_api_key(monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep the suite away from the operator's actual key.

    `_api_key` falls back to ~/.config/saddle/env when nothing is exported,
    which is what makes `saddle chat` work without sourcing anything. It also
    means a test that deletes the environment variables still finds a live
    credential on a developer's machine. That is not hypothetical: the five
    "missing key" tests began passing a real key to `main`, and
    test_main_doctor_missing_key_reports reached an actual vLLM server and
    returned 0 instead of 1.

    So every test reads the key from a path that cannot exist, unless it
    points KEY_FILE somewhere itself. Tests must never depend on -- or
    spend -- a real credential.

    The server URL and model resolve the same way, so an exported
    SADDLE_BASE_URL or SADDLE_MODEL on the developer's machine is cleared
    too: a test that expects the built-in default must not see theirs.
    """
    from saddle import cli

    monkeypatch.setattr(cli, "KEY_FILE", "/nonexistent/saddle-test-env")
    monkeypatch.delenv(cli.BASE_URL_ENV, raising=False)
    monkeypatch.delenv(cli.MODEL_ENV, raising=False)


def _replay_show_all_mutants(
    scratch: Path, *, recorder: SpanRecorder | None = None
) -> dict[str, str]:
    """Stand-in for `evidence.show_all_mutants` under a PATH-stub `mutmut`.

    None of the ten PATH-stub `mutmut` scripts across the suite write a
    `mutants/` directory, so the production lookup subprocess -- which reads
    mutmut's own meta files -- would find nothing under them. This replays
    the old per-name loop instead: `mutmut results --all True`, then
    `mutmut show NAME` for every name the results line, through whichever
    `mutmut` is first on PATH. `subprocess.run` is used directly, not
    `evidence.run_capture`, so a test that patches `run_capture` never
    observes these calls.
    """
    results = subprocess.run(
        ["mutmut", "results", "--all", "True"], cwd=scratch, capture_output=True, text=True
    )
    names = re.findall(r"^\s*(\S+): ", results.stdout, re.MULTILINE)
    mapping: dict[str, str] = {}
    for name in names:
        shown = subprocess.run(
            ["mutmut", "show", name], cwd=scratch, capture_output=True, text=True
        )
        mapping[name] = shown.stdout
    return mapping


@pytest.fixture(autouse=True)
def _stub_mutmut(tmp_path_factory: pytest.TempPathFactory, monkeypatch: pytest.MonkeyPatch) -> None:
    """Hermetic mutmut: e2e tests exercise the collector without real runs.

    Reports MIN_SIGNIFICANT_MUTANTS killed mutants that locate to the
    line every worktree fixture changes (n.py:2), so a node with healthy
    tests passes the mutation gate for the stated reason.

    This stub used to emit one mutant with unparseable `show` output, so
    nothing was decided and the gate returned its fail-open "no mutants
    on changed lines" PASS -- the fixture's own docstring called the
    verdict vacuous. Roughly thirty tests across the suite depended on
    that path, which is how load-bearing the fail-open had become (#49).
    Tests needing other verdicts still override PATH.

    `show_all_mutants` is patched to `_replay_show_all_mutants`: the
    production lookup shells out to a real mutmut installation's meta files,
    which this stub (and the other nine PATH stubs across the suite) never
    creates.
    """
    stub_dir = tmp_path_factory.mktemp("mutmut-stub")
    script = stub_dir / "mutmut"
    results = "\n".join(f"  m{index}: killed" for index in range(1, 6))
    show = "--- n.py\n+++ n.py\n@@ -2 +2 @@\n-    return 2\n+    return 3\n"
    script.write_text(
        "#!/bin/sh\n"
        'case "$1" in\n'
        "  run) exit 0;;\n"
        f"  results) printf '%s\\n' '{results}';;\n"
        f"  show) printf '%s' '{show}';;\n"
        "esac\n"
    )
    script.chmod(0o755)
    monkeypatch.setenv("PATH", f"{stub_dir}{os.pathsep}{os.environ['PATH']}")
    monkeypatch.setattr(evidence, "show_all_mutants", _replay_show_all_mutants)


@pytest.fixture(autouse=True)
def _no_image_probe(request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch) -> None:
    """A chat turn asks the served model whether it reads images (one request,
    once per server); a scripted client would hand them its next rounds. The suite
    has no model server, so the answer is no unless a test says otherwise (the image
    tests run the real probe against their own scripted server)."""
    if request.module.__name__ in ("test_read_image", "test_image_limit"):
        return
    monkeypatch.setattr("saddle.engine.server_accepts_images", lambda _client: False)


@pytest.fixture(autouse=True)
def no_real_compositor_pointer(monkeypatch: pytest.MonkeyPatch) -> None:
    """No test drives the person's real pointer: the suite runs inside a desktop
    session, and `computer.session_pointer` would otherwise connect to its
    compositor (it did, once, before this guard). Every connection starts by
    finding the compositor's socket, so that is what is closed off; a test that
    wants the compositor path serves a fake one or injects a fake pointer."""
    monkeypatch.setattr("saddle.wlpointer.socket_path", lambda env: None)
