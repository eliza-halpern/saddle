"""Evidence collectors for Tier-1 gates (ARCHITECTURE.md §3 Phase 3 Tier 1).

Thin subprocess wrappers and pure parsers that turn a worktree into the
`Tier1Inputs` evidence bundle. Every collector is straight-line where
possible so fixtures stay fast and branch coverage stays cheap.
"""

from __future__ import annotations

import ast
import contextlib
import hashlib
import io
import json
import os
import re
import shlex
import shutil
import signal
import subprocess
import sys
import tarfile
import tempfile
import tokenize
import tomllib
from collections.abc import Collection, Mapping, Sequence
from dataclasses import dataclass, field
from io import BytesIO
from pathlib import Path, PurePath
from time import perf_counter
from typing import Any, Final

import coverage

from saddle import memcap, sandbox
from saddle.gates import SHELL_TIMEOUT, TOOL_UNAVAILABLE, RuffFinding
from saddle.journal import SpanRecorder

_HUNK = re.compile(r"^@@ -\d+(?:,\d+)? \+(\d+)(?:,(\d+))? @@")
_MUTANT_VERDICT = re.compile(r"^\s*(\S+): (.+?)\s*$")
"""Any status to end of line: mutmut 3.8's `status_by_exit_code`
emits ten distinct strings (killed, survived, no tests, check was
interrupted by user, not checked, skipped, suspicious, timeout, caught
by type check, segfault), and the contract is that every one of them
enters the population except `not checked`. The old alternation
recognized only four and silently dropped the other six -- a "no tests"
mutant simply vanished from both `total` and `killed` instead of
counting as a survivor."""
_MUTANT_NAME = re.compile(
    r"^(?:\w+\.)+x(?:_(?P<func>.+?)|ǁ(?P<cls>\w+)ǁ(?P<method>.+?))__mutmut_\d+$"
)
"""mutmut's two mangled-name shapes, parsed locally so saddle never
imports mutmut into its own process: `<dotted.module>.x_<func>__mutmut_<n>`
for a function, `<dotted.module>.xǁ<Class>ǁ<method>__mutmut_<n>` for a
method (ǁ is U+01C1). Checked against mutmut's own
`orig_function_and_class_names_from_key` as the test oracle
(test_mutant_name_matches_mutmut_names_from_key), not used at runtime. A
name that does not match this shape is a hand-built test stub and takes
`_mutant_lines`'s old whole-file fallback."""
_MUTATION_TIMEOUT_S = 600

DEFAULT_TEST_TIMEOUT_S: Final = 300.0
"""Wall-clock ceiling for one run of a project's test command when the
project sets none (`suite_limit`). The benchmark's suites run in seconds,
so this bounds non-termination without failing them. A suite that is
slow and sound -- saddle's own takes about twenty minutes -- sets its
own limit instead: `[tool.saddle] test-timeout` in its `pyproject.toml`."""

SUITE_LIMIT_FILE: Final = "pyproject.toml"
SUITE_LIMIT_KEY: Final = "test-timeout"
"""The project's own test time limit, in seconds: `test-timeout` in the
`[tool.saddle]` table of its `pyproject.toml` (`suite_limit`)."""

SUITE_LIMIT_MAX_S: Final = 86400.0
"""The longest limit a project may set: a day. A hang must still end in a
verdict, and far past this the wait itself fails: `Popen.communicate`
raises `OverflowError` for a timeout of 2.2e6 s (about 25 days), which
would read as a crashed audit instead of a hang."""

SUITE_WORKERS_KEY: Final = "test-workers"
"""How many pytest-xdist workers the audit runs the project's suite on:
`test-workers` in the `[tool.saddle]` table of its `pyproject.toml`
(`suite_workers`). Absent or 1, the suite runs serially, as before."""

SUITE_WORKERS_MAX: Final = 64
"""The most workers a project may ask for. Every worker is a Python process
under the run's one memory cap, so a typo such as 800 must be refused by
name rather than start 800 interpreters."""

SADDLE_KEYS: Final = (SUITE_LIMIT_KEY, SUITE_WORKERS_KEY)
"""Every key saddle reads in `[tool.saddle]`. Any other key there is refused
(`_committed_saddle_table`): a typo must not read as "not set"."""

TEST_MEMORY_LIMIT_BYTES: Final = memcap.DEFAULT_MEMORY_MAX
"""Default memory cap for every subprocess that executes the audited tree's
code: the declared test command and `mutmut run` (`SADDLE_MEMORY_MAX`
overrides it; `tree_memory_limit` reads both). 6 GiB, the benchmark oracle
harness's cap, set after a 57 GB OOM; an uncapped audit of one benchmark tree
grew to 20.3 GB before the kernel killed it. Code past the cap is killed in
its own cgroup (or gets `MemoryError` under the address-space fallback), and
its tests fail. `git`, `ruff` and `coverage` bookkeeping calls are not
capped."""


def tree_memory_limit() -> int:
    """The cap for a command that runs the tree's code, read at each call."""
    return memcap.memory_max(TEST_MEMORY_LIMIT_BYTES)


def _record(
    recorder: SpanRecorder | None,
    argv: Sequence[str],
    start: float,
    proc: subprocess.CompletedProcess[str] | subprocess.CompletedProcess[bytes],
    *,
    name: str | None = None,
) -> None:
    """Journal one completed invocation; no recorder means no span."""
    if recorder is None:
        return
    err = proc.stderr
    detail = err.decode(errors="replace") if isinstance(err, bytes) else err
    recorder.record(
        argv=list(argv),
        duration_ms=int((perf_counter() - start) * 1000),
        exit_code=proc.returncode,
        detail=detail,
        name=name,
    )


def _partial(stream: str | bytes | None) -> str:
    """Decode whatever a killed process managed to emit before dying."""
    if stream is None:
        return ""
    return stream.decode(errors="replace") if isinstance(stream, bytes) else stream


def _record_timeout(
    recorder: SpanRecorder | None,
    argv: Sequence[str],
    start: float,
    expired: subprocess.TimeoutExpired,
) -> None:
    """Journal a killed invocation; the span must not silently vanish."""
    if recorder is None:
        return
    recorder.record(
        argv=list(argv),
        duration_ms=int((perf_counter() - start) * 1000),
        exit_code=SHELL_TIMEOUT,
        detail=f"timed out after {expired.timeout}s: {_partial(expired.stderr)}",
    )


def _record_unavailable(
    recorder: SpanRecorder | None, argv: Sequence[str], start: float, exc: OSError
) -> None:
    """Journal a tool that never launched; the reason must reach the run."""
    if recorder is None:
        return
    recorder.record(
        argv=list(argv),
        duration_ms=int((perf_counter() - start) * 1000),
        exit_code=TOOL_UNAVAILABLE,
        detail=str(exc),
    )


def run_argv(
    argv: Sequence[str],
    cwd: Path,
    *,
    recorder: SpanRecorder | None = None,
    timeout: float | None = None,
) -> int:
    """Run `argv` in `cwd`; return its exit code, capturing output.

    A `timeout` bounds the run: exceeding it yields `SHELL_TIMEOUT`
    rather than blocking the gate runner forever.
    """
    start = perf_counter()
    try:
        proc = subprocess.run(
            argv, cwd=cwd, capture_output=True, timeout=timeout, env=sandbox.gate_env()
        )
    except subprocess.TimeoutExpired as expired:
        _record_timeout(recorder, argv, start, expired)
        return SHELL_TIMEOUT
    except OSError as exc:
        _record_unavailable(recorder, argv, start, exc)
        return TOOL_UNAVAILABLE
    _record(recorder, argv, start, proc)
    return proc.returncode


# The lint rules the ruff gate enforces: defects, not style. Round 3e
# was gated by ruff 0.16.7's *default* rule set -- no config
# existed in the workdir or above it -- and UP031 failed a node for the
# `%`-formatting its own baseline uses in 4 of 5 modules; a `pip install
# -U ruff` would have changed the verdict. Every ruff call runs
# `--isolated`, so neither the graded repo's config nor the machine's
# reaches the gate, and `ruff check` selects exactly these: pyflakes (F),
# the E4/E7/E9 subset ruff itself ships by default, and bugbear (B).
RUFF_RULES: Final = ("F", "E4", "E7", "E9", "B")


def ruff_argv(command: str, *args: str) -> list[str]:
    """`ruff <command>` as the gate runs it: isolated, and for `check`, saddle's rules."""
    argv = ["ruff", command, "--isolated"]
    if command == "check":
        argv.extend(["--select", ",".join(RUFF_RULES)])
    argv.extend(args)
    return argv


def ruff_version() -> str:
    """The installed ruff's version (`ruff --version`), or "unavailable"."""
    try:
        proc = subprocess.run(
            ["ruff", "--version"],
            capture_output=True,
            text=True,
            check=False,
            env=sandbox.gate_env(),
        )
    except OSError:
        return "unavailable"
    words = proc.stdout.split()
    return words[1] if proc.returncode == 0 and len(words) >= 2 else "unavailable"


def ruff_findings(
    workdir: Path, files: Sequence[str], *, recorder: SpanRecorder | None = None
) -> tuple[CapturedRun, list[RuffFinding]]:
    """Run `ruff check --output-format json` on `files` in `workdir`.

    Returns the run (exit code as ruff gave it; stdout replaced by one
    human line per finding, `path:row:col: CODE message`, so a repair
    brief reads findings rather than JSON) and the parsed findings, each
    carrying the stripped source line it sits on. Unparseable output
    yields no findings and leaves the exit code to say the tool failed.
    """
    argv = ruff_argv("check", "--output-format", "json", *files)
    run = run_capture(argv, workdir, recorder=recorder)
    findings: list[RuffFinding] = []
    try:
        raw = json.loads(run.stdout) if run.stdout.strip() else []
    except ValueError:
        raw = []
    root = os.path.realpath(workdir)
    for item in raw if isinstance(raw, list) else []:
        if not isinstance(item, dict):
            continue
        raw_location = item.get("location")
        location: dict[str, Any] = raw_location if isinstance(raw_location, dict) else {}
        row = int(location.get("row", 0) or 0)
        path = os.path.relpath(os.path.realpath(str(item.get("filename", ""))), root)
        line = _source_line(workdir / path, row)
        findings.append(
            RuffFinding(
                code=str(item.get("code") or "?"),
                path=path,
                row=row,
                message=str(item.get("message") or ""),
                line=line,
                column=int(location.get("column", 0) or 0),
            )
        )
    rendered = "\n".join(f"{f.path}:{f.row}:{f.column}: {f.code} {f.message}" for f in findings)
    shown = CapturedRun(
        argv=("ruff", "check", *files),
        exit_code=run.exit_code,
        stdout=rendered,
        stderr=run.stderr,
        timed_out=run.timed_out,
    )
    return shown, findings


def _source_line(path: Path, row: int) -> str:
    """The stripped text of line `row` (1-based) of `path`, or empty."""
    try:
        lines = path.read_text().splitlines()
    except (OSError, UnicodeDecodeError):
        return ""
    return lines[row - 1].strip() if 0 < row <= len(lines) else ""


def run_stdin(
    argv: Sequence[str], cwd: Path, text: str, *, recorder: SpanRecorder | None = None
) -> int:
    """Run `argv` with `text` on stdin; return its exit code."""
    return run_stdin_capture(argv, cwd, text, recorder=recorder)[0]


def run_stdin_capture(
    argv: Sequence[str], cwd: Path, text: str, *, recorder: SpanRecorder | None = None
) -> tuple[int, str]:
    """Run `argv` with `text` on stdin; return its exit code and stderr."""
    start = perf_counter()
    proc = subprocess.run(argv, input=text, cwd=cwd, capture_output=True, text=True)
    _record(recorder, argv, start, proc)
    return proc.returncode, proc.stderr


def run_shell(
    command: str,
    cwd: Path,
    *,
    recorder: SpanRecorder | None = None,
    timeout: float | None = DEFAULT_TEST_TIMEOUT_S,
) -> int:
    """Run a `test_command` string via shlex splitting (never a shell).

    The default `timeout` is the built-in one; a gate running a project's
    tests passes that project's `suite_limit` instead."""
    return run_argv(shlex.split(command), cwd, recorder=recorder, timeout=timeout)


@dataclass(frozen=True)
class CapturedRun:
    """One completed invocation with its output, for recovery prompts."""

    argv: tuple[str, ...]
    exit_code: int
    stdout: str
    stderr: str
    timed_out: bool = False


def _run_as_group(
    argv: Sequence[str], cwd: Path, timeout: float | None, env: Mapping[str, str] | None = None
) -> subprocess.CompletedProcess[str]:
    """`subprocess.run`, except a timeout kills the whole process group.

    `subprocess.run` kills only the direct child, so a test that forked a
    helper left it running after the gate had moved on."""
    with subprocess.Popen(
        argv,
        cwd=cwd,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        start_new_session=True,
        env=env,
    ) as proc:
        try:
            stdout, stderr = proc.communicate(timeout=timeout)
        except subprocess.TimeoutExpired:
            with contextlib.suppress(ProcessLookupError):
                os.killpg(proc.pid, signal.SIGKILL)
            proc.wait()  # like subprocess.run on POSIX: keep the partial bytes
            raise
    return subprocess.CompletedProcess(list(argv), proc.returncode, stdout, stderr)


def run_capture(
    argv: Sequence[str],
    cwd: Path,
    *,
    recorder: SpanRecorder | None = None,
    timeout: float | None = None,
    memory_limit: int | None = None,
    writable: Sequence[Path] = (),
    extra_env: Mapping[str, str] | None = None,
) -> CapturedRun:
    """Run `argv` in `cwd`; journal its span and return exit plus output.

    On timeout the partial output is preserved and `timed_out` is set, so
    recovery prompts still see how far the run got before it stalled.

    `memory_limit` puts the command, and everything it starts, under a hard
    cap (`memcap`): a cgroup scope of its own where the box has a user
    systemd manager, else a `prlimit` address-space ceiling. Both are argv
    prefixes, not `preexec_fn`, which is unsafe once threads exist (`slice`
    runs a `ThreadPoolExecutor`). A command the cap killed exits nonzero with
    the reason as the last line of its stderr and the first of its span, so
    it reads as a failure with a cause, never as a hang or a crash of saddle.
    The span and the result record `argv` without the prefix, so journals
    and cache keys read as the command that was asked for.

    A `memory_limit` marks a command that runs the tree's code, so it is
    also confined (`sandbox.confine`): `cwd` and `writable` are the only
    places it can write, it sees nothing else outside the system dirs and
    the gate-tool venvs, it has no network, and its environment is the
    scrubbed allowlist.
    """
    start = perf_counter()
    cap = None if memory_limit is None else memcap.cap(memory_limit)
    try:
        if cap is None:
            proc = _run_as_group(argv, cwd, timeout, {**sandbox.gate_env(), **(extra_env or {})})
        else:
            # A prefix would turn a missing program into its own exit status
            # (`systemd-run` exits 1, which reads as "tests failed").
            if shutil.which(argv[0], path=sandbox.gate_path(os.environ.get("PATH", ""))) is None:
                raise FileNotFoundError(2, "No such file or directory", argv[0])
            confined, env = sandbox.confine(argv, cwd, writable=writable, extra_env=extra_env)
            launched, lent = cap.wrap(confined, env)
            proc = _run_as_group(launched, cwd, timeout, lent)
    except subprocess.TimeoutExpired as expired:
        _record_timeout(recorder, argv, start, expired)
        return CapturedRun(
            argv=tuple(argv),
            exit_code=SHELL_TIMEOUT,
            stdout=_partial(expired.stdout),
            stderr=_partial(expired.stderr),
            timed_out=True,
        )
    except OSError as exc:
        _record_unavailable(recorder, argv, start, exc)
        return CapturedRun(argv=tuple(argv), exit_code=TOOL_UNAVAILABLE, stdout="", stderr=str(exc))
    if cap is not None and cap.oom_killed():
        reason = cap.reason()
        _record(recorder, argv, start, replace_stderr(proc, f"{reason}\n{proc.stderr}"))
        proc = replace_stderr(proc, f"{proc.stderr.rstrip()}\n{reason}\n".lstrip("\n"))
    else:
        _record(recorder, argv, start, proc)
    return CapturedRun(
        argv=tuple(argv), exit_code=proc.returncode, stdout=proc.stdout, stderr=proc.stderr
    )


def replace_stderr(
    proc: subprocess.CompletedProcess[str], stderr: str
) -> subprocess.CompletedProcess[str]:
    """`proc` with its stderr replaced."""
    return subprocess.CompletedProcess(proc.args, proc.returncode, proc.stdout, stderr)


def run_shell_capture(
    command: str,
    cwd: Path,
    *,
    recorder: SpanRecorder | None = None,
    timeout: float | None = DEFAULT_TEST_TIMEOUT_S,
    extra_env: Mapping[str, str] | None = None,
) -> CapturedRun:
    """Run a `test_command` string via shlex splitting, capturing output, under
    the memory cap (`tree_memory_limit`): it executes the tree's code, and
    imports the tree's own package (`src_layout_env`). The default `timeout`
    is the built-in one; a gate passes the project's `suite_limit` instead.
    `extra_env` is laid over that environment (a parallel suite's data file)."""
    return run_capture(
        shlex.split(command),
        cwd,
        recorder=recorder,
        timeout=timeout,
        memory_limit=tree_memory_limit(),
        extra_env={**src_layout_env(cwd), **(extra_env or {})},
    )


class SuiteLimitError(ValueError):
    """The project sets a test time limit or worker count saddle cannot use."""


@dataclass(frozen=True)
class SuiteLimit:
    """How long one run of a project's test command may take, and where that came from."""

    seconds: float
    source: str


def _committed_saddle_table(tree: Path, rev: str, what: str) -> tuple[dict[str, Any] | None, str]:
    """`[tool.saddle]` of the `pyproject.toml` committed at `rev`, and where it was read.

    The file is the one in `tree`'s own directory, read from the commit,
    never from the working tree or the index. The table is None when that
    commit has no such file, and empty when the file has no table. A file
    that cannot be read, a table that is not one, or a key outside
    `SADDLE_KEYS` raises `SuiteLimitError`, its message starting "cannot
    read `what`" and naming the commit.
    """
    found = run_capture(["git", "rev-parse", "--verify", "--quiet", f"{rev}^{{commit}}"], tree)
    sha = found.stdout.strip()
    if found.exit_code != 0 or not sha:
        msg = f"cannot read {what}: {rev!r} is not a commit in {tree}"
        raise SuiteLimitError(msg)
    where = f"{SUITE_LIMIT_FILE} at {sha[:12]}"
    blob = f"{sha}:./{SUITE_LIMIT_FILE}"
    if run_capture(["git", "cat-file", "-e", blob], tree).exit_code != 0:
        return None, where
    shown = run_capture(["git", "cat-file", "blob", blob], tree)
    if shown.exit_code != 0:
        msg = f"cannot read {what}: {where} is not a file: {shown.stderr.strip()}"
        raise SuiteLimitError(msg)
    try:
        data = tomllib.loads(shown.stdout)
    except tomllib.TOMLDecodeError as exc:
        msg = f"cannot read {what}: {where} is not TOML ({exc})"
        raise SuiteLimitError(msg) from exc
    tool = data.get("tool")
    table = tool.get("saddle", {}) if isinstance(tool, dict) else {}
    if not isinstance(table, dict):
        msg = f"cannot read {what}: {where}: [tool.saddle] is not a table"
        raise SuiteLimitError(msg)
    unknown = sorted(set(table) - set(SADDLE_KEYS))
    if unknown:
        msg = (
            f"cannot read {what}: {where}: [tool.saddle] holds {', '.join(unknown)}; "
            f"the keys saddle reads there are {' and '.join(SADDLE_KEYS)}"
        )
        raise SuiteLimitError(msg)
    return table, where


def suite_limit(tree: Path, rev: str) -> SuiteLimit:
    """The project's test time limit as committed at `rev`, never as `tree` has it now.

    `test-timeout = <seconds>` in the `[tool.saddle]` table of the
    `pyproject.toml` in `tree`'s own directory sets it; with no file, no
    table or no key it is `DEFAULT_TEST_TIMEOUT_S`. Every run of the
    project's tests under a gate takes this limit: `runner.run_node_gate`'s
    suite, red-phase and dead-code runs, `auditor.green_on_baseline`, and
    `slice`'s candidate and merge-time runs.

    `rev` is where the task started (the audit's baseline, a `saddle run`'s
    `HEAD`), so the tree under audit cannot lift its own bar: an edit to its
    `pyproject.toml`, staged or not, changes nothing for the run it is part
    of. A value that cannot be used -- not TOML, not a table, not a number
    of seconds in (0, `SUITE_LIMIT_MAX_S`], or a key in the table saddle
    does not read -- raises `SuiteLimitError` naming the commit and the
    value: a typo must not read as "no limit set" and leave the default in
    force unannounced.
    """
    table, where = _committed_saddle_table(tree, rev, "the test time limit")
    default = f"built-in default {DEFAULT_TEST_TIMEOUT_S:g} s"
    if table is None:
        return SuiteLimit(DEFAULT_TEST_TIMEOUT_S, f"{default} (no {where})")
    if SUITE_LIMIT_KEY not in table:
        return SuiteLimit(DEFAULT_TEST_TIMEOUT_S, f"{default} (no {SUITE_LIMIT_KEY} in {where})")
    value = table[SUITE_LIMIT_KEY]
    # The comparison alone rejects nan and both infinities, and compares a
    # huge TOML integer exactly (tomllib keeps it whole; `math.isfinite`
    # would raise OverflowError on it).
    if (
        isinstance(value, bool)
        or not isinstance(value, int | float)
        or not 0 < value <= SUITE_LIMIT_MAX_S
    ):
        msg = (
            f"cannot read the test time limit: {where}: {SUITE_LIMIT_KEY} = {value!r} "
            f"is not a number of seconds above 0 and at most {SUITE_LIMIT_MAX_S:g}"
        )
        raise SuiteLimitError(msg)
    return SuiteLimit(float(value), f"[tool.saddle] {SUITE_LIMIT_KEY} in {where}")


@dataclass(frozen=True)
class SuiteWorkers:
    """How many workers the audit runs a project's suite on, and where that came from."""

    count: int
    source: str


def suite_workers(tree: Path, rev: str) -> SuiteWorkers:
    """The project's test worker count as committed at `rev`, never as `tree` has it now.

    `test-workers = <N>` in the `[tool.saddle]` table of the
    `pyproject.toml` in `tree`'s own directory sets it; with no file, no
    table or no key it is 1, a serial run. Read exactly where and how
    `suite_limit` reads `test-timeout`, so the tree under audit cannot
    choose how its own suite is run. A value that is not a whole number
    from 1 to `SUITE_WORKERS_MAX` (a bool, a float, a string such as
    "auto") raises `SuiteLimitError` naming the commit and the value.
    Whether N workers are then used is `suite_run`'s question.
    """
    table, where = _committed_saddle_table(tree, rev, "the test worker count")
    if table is None:
        return SuiteWorkers(1, f"serial (no {where})")
    if SUITE_WORKERS_KEY not in table:
        return SuiteWorkers(1, f"serial (no {SUITE_WORKERS_KEY} in {where})")
    value = table[SUITE_WORKERS_KEY]
    if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= SUITE_WORKERS_MAX:
        msg = (
            f"cannot read the test worker count: {where}: {SUITE_WORKERS_KEY} = {value!r} "
            f"is not a whole number of workers from 1 to {SUITE_WORKERS_MAX}"
        )
        raise SuiteLimitError(msg)
    return SuiteWorkers(value, f"[tool.saddle] {SUITE_WORKERS_KEY} in {where}")


RAN_SERIALLY: Final = "ran serially"
"""How a note about a serial run ends (`SuiteRun.note`)."""

WORKERS_BASIS: Final = "test-workers="
"""The `basis` field of a `tests` check whose suite ran on that many
pytest-xdist workers (`runner.run_node_gate`)."""

WORKERS_PROBE_MODULE: Final = "saddle_workers_probe"
_WORKERS_PROBE_MARK: Final = "saddle-workers-probe "
_WORKERS_PROBE_SOURCE: Final = f'''"""saddle's test-workers probe.

Which of the plugins a parallel audit needs pytest loads under the
project's own configuration. It prints one line and ends the run before
anything is collected."""

import json

import pytest


@pytest.hookimpl(tryfirst=True)
def pytest_cmdline_main(config):
    plugins = config.pluginmanager
    report = {{
        "xdist": plugins.hasplugin("xdist"),
        "xdist_blocked": plugins.is_blocked("xdist"),
        "cov": plugins.hasplugin("pytest_cov"),
        "cov_blocked": plugins.is_blocked("pytest_cov"),
        "no_cov": bool(getattr(config.option, "no_cov", False)),
        "cov_source": bool(getattr(config.option, "cov_source", None)),
    }}
    print({_WORKERS_PROBE_MARK!r} + json.dumps(report), flush=True)
    return 0
'''
"""A pytest plugin, loaded with `-p` from a scratch directory, that answers
`suite_run`'s questions with pytest's own view: its plugin manager after
the project's `addopts` and the test command's own options are applied.
Asking pytest, not the filesystem: an installed pytest-xdist that the
project disables with `-p no:xdist` would make `-n` a usage error, and
`pytest -VV` still lists xdist's looponfail plugin in that case."""

WORKERS_PROBE_TIMEOUT_S: Final = 120.0
"""How long the probe may take. It starts pytest and imports the project's
initial conftests, and collects nothing."""


@dataclass(frozen=True)
class SuiteRun:
    """How a gate runs a project's suite (`suite_run` decides).

    The default is the serial run exactly as it always was: the test
    command under `coverage run` (`under_coverage`). `project_cov` says the
    project's own pytest options start pytest-cov. Inside `coverage run`
    that pytest-cov takes the tracer over and `coverage run` records
    nothing, so such a suite is recorded by the project's pytest-cov itself:
    the test command with `COVERAGE_FILE` naming the gate's data file; its
    sources and report stand.

    From 2 `workers` up it is the test command with `-n workers`
    (pytest-xdist), also recorded by pytest-cov into the gate's data file,
    which pytest-cov combines from the controller and every worker. When
    the project's options do not start pytest-cov, `--cov` with no source
    measures what `coverage run` measures (the coverage config's `source`,
    else everything).

    Whenever pytest-cov runs, the gate adds `--cov-fail-under=0`: no
    coverage total decides the tests, red-phase or dead-code checks, as
    none does under `coverage run`. Coverage is the coverage check's, over
    the changed lines. A project's own total cannot be met in the gate's
    sandbox whenever its suite skips tests there (saddle's own skips the
    ones that need a user systemd manager), and it failed every run.

    `note` says why a project that set `test-workers` got a serial run; it
    is appended to the `tests` finding, never dropped.
    """

    workers: int = 1
    project_cov: bool = False
    note: str = ""

    @property
    def parallel(self) -> bool:
        return self.workers >= 2

    @property
    def by_pytest_cov(self) -> bool:
        """Whether pytest-cov, not `coverage run`, records the run."""
        return self.parallel or self.project_cov

    def covered(self, test_command: str, data_file: str) -> tuple[str, dict[str, str]]:
        """The command that runs the suite recording into `data_file`, and its extra env."""
        if not self.by_pytest_cov:
            return under_coverage(test_command, data_file), {}
        extra = ["-n", str(self.workers)] if self.parallel else []
        if not self.project_cov:
            extra += ["--cov", "--cov-report="]
        extra += ["--cov-fail-under=0"]
        command = shlex.join([*shlex.split(test_command), *extra])
        return command, {"COVERAGE_FILE": os.path.abspath(data_file)}

    def plain(self, test_command: str) -> str:
        """The command that runs the suite with no coverage asked for (dead-code reruns).

        The project's own pytest-cov still runs if its options start it, and
        its total must not decide this run either: a rerun failed on the total
        reads as "the suite fails without them" and passes dead code."""
        extra = ["-n", str(self.workers)] if self.parallel else []
        if self.project_cov:
            extra += ["--cov-fail-under=0"]
        if not extra:
            return test_command
        return shlex.join([*shlex.split(test_command), *extra])


PYTEST_CONFIG_FILES: Final = ("pytest.ini", ".pytest.ini", "pyproject.toml", "tox.ini", "setup.cfg")
"""The files at a tree's root pytest reads its options from (`_may_start_pytest_cov`)."""


def _starts_pytest(argv: Sequence[str]) -> bool:
    """`pytest ...` or `<python> -m pytest ...`: a command options can be appended to."""
    program = PurePath(next(iter(argv), "")).name
    if program in ("pytest", "py.test"):
        return True
    return re.fullmatch(r"python[\d.]*", program) is not None and list(argv[1:3]) == [
        "-m",
        "pytest",
    ]


def _may_start_pytest_cov(tree: Path, test_command: str) -> bool:
    """Whether the test command, or a pytest config file at `tree`'s root,
    mentions `--cov`: a filter, so a serial run of a project whose options
    cannot start pytest-cov asks pytest nothing and runs exactly as before.
    `suite_run` asks pytest itself whenever this says yes."""
    if "--cov" in test_command:
        return True
    for name in PYTEST_CONFIG_FILES:
        try:
            if "--cov" in (tree / name).read_text(errors="replace"):
                return True
        except OSError:
            continue
    return False


def _probe_plugins(
    tree: Path, argv: Sequence[str], recorder: SpanRecorder | None
) -> dict[str, Any] | str:
    """pytest's own report on its plugins (`_WORKERS_PROBE_SOURCE`), or why there is none."""
    with tempfile.TemporaryDirectory(prefix="saddle-workers-probe-") as scratch:
        (Path(scratch) / f"{WORKERS_PROBE_MODULE}.py").write_text(_WORKERS_PROBE_SOURCE)
        inherited = src_layout_env(tree).get("PYTHONPATH", os.environ.get("PYTHONPATH", ""))
        probe = run_capture(
            [*argv, "-p", WORKERS_PROBE_MODULE, "-p", "no:cacheprovider"],
            tree,
            recorder=recorder,
            timeout=WORKERS_PROBE_TIMEOUT_S,
            memory_limit=tree_memory_limit(),
            writable=(Path(scratch),),
            extra_env={"PYTHONPATH": os.pathsep.join(filter(None, [scratch, inherited]))},
        )
    said = [
        line.removeprefix(_WORKERS_PROBE_MARK)
        for line in probe.stdout.splitlines()
        if line.startswith(_WORKERS_PROBE_MARK)
    ]
    try:
        report = json.loads(said[-1]) if said and probe.exit_code == 0 else None
    except ValueError:
        report = None
    if isinstance(report, dict):
        return report
    return "timed out" if probe.timed_out else f"exit {probe.exit_code}"


def suite_run(
    tree: Path,
    test_command: str,
    workers: int,
    *,
    recorder: SpanRecorder | None = None,
) -> SuiteRun:
    """How to run `test_command`'s suite in `tree` when the project asks for `workers`.

    Parallel only when the test command starts pytest and pytest, started
    exactly as the command starts it in the gate's sandbox and environment,
    loads pytest-xdist and pytest-cov with neither disabled by the project's
    options (`_WORKERS_PROBE_SOURCE`). Every other case is serial, and when
    `workers` asked for more than one, `SuiteRun.note` says why: a missing
    plugin must never be silent. A probe that does not answer is such a
    case too, reported with its exit code.

    Serial or not, the same report says whether the project's own options
    start pytest-cov (`SuiteRun.project_cov`). A serial run asks only when
    `_may_start_pytest_cov` says they might; otherwise it asks nothing.
    """
    head = f"{SUITE_WORKERS_KEY} = {workers} set but"
    argv = shlex.split(test_command)
    if not _starts_pytest(argv):
        note = f"{head} the test command does not start pytest: {RAN_SERIALLY}"
        return SuiteRun(note=note if workers >= 2 else "")
    if workers < 2 and not _may_start_pytest_cov(tree, test_command):
        return SuiteRun()
    report = _probe_plugins(tree, argv, recorder)
    if isinstance(report, str):
        note = f"{head} pytest did not say which plugins it loads ({report}): {RAN_SERIALLY}"
        return SuiteRun(note=note if workers >= 2 else "")
    project_cov = bool(report.get("cov") and report.get("cov_source") and not report.get("no_cov"))
    if workers < 2:
        return SuiteRun(project_cov=project_cov)
    missing = []
    for plugin, name in (("xdist", "pytest-xdist"), ("cov", "pytest-cov")):
        if not report.get(plugin):
            blocked = report.get(f"{plugin}_blocked")
            missing.append(
                f"the project's pytest options disable {name}"
                if blocked
                else f"{name} is not installed"
            )
    if report.get("cov") and report.get("no_cov"):
        missing.append("the project's pytest options disable pytest-cov (--no-cov)")
    if missing:
        note = f"{head} {' and '.join(missing)}: {RAN_SERIALLY}"
        return SuiteRun(project_cov=project_cov, note=note)
    return SuiteRun(workers=workers, project_cov=project_cov)


def run_suite_capture(
    run: SuiteRun,
    test_command: str,
    cwd: Path,
    data_file: str,
    *,
    recorder: SpanRecorder | None = None,
    timeout: float | None,
) -> CapturedRun:
    """Run the suite as `run` says, recording coverage into `data_file`.

    A run pytest-cov records first removes `data_file` and any
    `data_file.*` left in `cwd`: pytest-cov combines every file of that name
    and, under a project's `--cov-append`, keeps the old data too, so one it
    did not write (a copy of the tree carries whatever the tree holds) must
    not add lines no test ran. The serial `coverage run` is
    `run_shell_capture` of `under_coverage`, unchanged."""
    command, env = run.covered(test_command, data_file)
    if run.by_pytest_cov:
        target = Path(data_file)
        for stale in list(target.parent.iterdir()):
            if stale.name == target.name or stale.name.startswith(f"{target.name}."):
                stale.unlink()
    return run_shell_capture(command, cwd, recorder=recorder, timeout=timeout, extra_env=env)


def src_layout_env(tree: Path) -> dict[str, str]:
    """`PYTHONPATH` with `<tree>/src` first when `tree` has a `src/`, else nothing.

    A `src`-layout project's tests import its package through an install,
    usually an editable one. In the gate that install is either absent
    (the sandbox hides the checkout it points at, so every test errors) or
    another copy (saddle auditing saddle: the tests ran against the
    maintainer's checkout, not the tree audited). With `src` first, `import
    <package>` finds the audited tree's copy. Not for `mutmut run`, which
    puts its mutated `src` first and removes the original from `sys.path`
    itself."""
    src = tree / "src"
    if not src.is_dir():
        return {}
    inherited = os.environ.get("PYTHONPATH", "")
    return {"PYTHONPATH": os.pathsep.join([str(src.resolve()), *filter(None, [inherited])])}


def drop_test_caches(root: Path) -> None:
    """Remove Python and pytest caches so gates evaluate current sources.

    Fix-forward retries rewrite test files seconds apart; an unchanged
    size within the same mtime second would otherwise validate a stale
    .pyc and run yesterday's tests. Targets materialize before removal
    so the tree never mutates mid-walk.
    """
    for cache in list(root.rglob("__pycache__")):
        shutil.rmtree(cache, ignore_errors=True)
    for cache in list(root.rglob(".pytest_cache")):
        shutil.rmtree(cache, ignore_errors=True)
    for stale in list(root.rglob("*.pyc")):
        # TOCTOU-only: rglob yields existing paths, so the flag never fires in tests.
        stale.unlink(missing_ok=True)  # pragma: no mutate


def changed_lines(diff: str) -> set[tuple[str, int]]:
    """Added-line identities from `git diff -U0` text; deletions add none."""
    changed: set[tuple[str, int]] = set()
    path: str | None = None
    for line in diff.splitlines():
        if line.startswith("+++ "):
            target = line.removeprefix("+++ ").strip()
            path = None if target == "/dev/null" else target.removeprefix("b/")
        elif path is not None and (match := _HUNK.match(line)):
            start = int(match.group(1))
            count = int(match.group(2)) if match.group(2) is not None else 1
            changed.update((path, number) for number in range(start, start + count))
    return changed


def git_ls_files(cwd: Path) -> list[str]:
    """Tracked worktree-relative paths at `cwd` for worker prompts."""
    proc = subprocess.run(
        ["git", "-C", str(cwd), "ls-files"],
        capture_output=True,
        text=True,
    )
    if proc.returncode != 0:
        msg = f"git ls-files failed: {proc.stderr.strip()}"
        raise RuntimeError(msg)
    return proc.stdout.splitlines()


def git_changed_files(cwd: Path, ref: str, *, recorder: SpanRecorder | None = None) -> list[str]:
    """Worktree-relative paths differing from `ref`, for scoping autofixes.

    `--no-renames`: with git's default rename detection a staged
    `git mv n.py m.py` prints only `m.py`, so the path a node deleted
    never reached target-scope and a `target_files` list naming the new
    path alone let the rename through. Both paths are the node's.
    """
    argv = ["git", "-C", str(cwd), "diff", "--no-renames", "--name-only", ref, "--", "."]
    start = perf_counter()
    proc = subprocess.run(argv, capture_output=True, text=True)
    _record(recorder, argv, start, proc)
    if proc.returncode != 0:
        msg = f"git diff --name-only against {ref!r} failed: {proc.stderr.strip()}"
        raise RuntimeError(msg)
    return proc.stdout.splitlines()


def git_added_files(cwd: Path, ref: str, *, recorder: SpanRecorder | None = None) -> list[str]:
    """Worktree-relative paths that do not exist at `ref` (renames count as added).

    Only staged adds count. A worker introduces files through its diff
    alone, and `_apply_diff` applies with `--index`, so every file a node
    creates is staged. Untracked files in the worktree are the harness's
    own artefacts -- `.saddle/proofs.jsonl`, `.coverage.tier1`, bytecode --
    and listing them would fail every `refactor` node on its first retry.
    """
    argv = [
        "git",
        "-C",
        str(cwd),
        "diff",
        "--no-renames",
        "--diff-filter=A",
        "--name-only",
        ref,
        "--",
        ".",
    ]
    start = perf_counter()
    proc = subprocess.run(argv, capture_output=True, text=True)
    _record(recorder, argv, start, proc)
    if proc.returncode != 0:
        msg = f"git diff --diff-filter=A against {ref!r} failed: {proc.stderr.strip()}"
        raise RuntimeError(msg)
    return proc.stdout.splitlines()


BASELINE_REF_PREFIX: Final = "refs/saddle/baseline/"
"""Namespace for per-node baseline commits. Outside `refs/heads` and
`refs/tags`, so writing one moves no branch, no tag and not `HEAD`."""


PROVEN_REF_PREFIX: Final = "refs/saddle/proven/"
"""Namespace for the tree each proof was sealed against. Same
properties as `BASELINE_REF_PREFIX`: writing one moves no branch, no tag
and not `HEAD`."""


ATTEMPT_REF_PREFIX: Final = "refs/saddle/attempt/"
"""Namespace for the tree each attempt was graded on. Same
properties as `BASELINE_REF_PREFIX`: writing one moves no branch, no tag
and not `HEAD`."""


def _ref_slug(node_id: str) -> str:
    """`node_id` as a git-legal ref component, else its sha256 prefix.

    Node ids come from the planner and nothing in the DAG schema makes
    them ref-safe: a space, a `..`, a trailing `.lock` would fail
    `update-ref` and take the node down for a naming reason. git's own
    parser decides -- `check-ref-format` is the one `update-ref` uses --
    and the digest fallback is a pure function of the id, so attempts
    2..N of the same node resolve the same ref.
    """
    proc = subprocess.run(
        ["git", "check-ref-format", f"{BASELINE_REF_PREFIX}{node_id}"],
        capture_output=True,
        text=True,
    )
    if proc.returncode == 0:
        return node_id
    return hashlib.sha256(node_id.encode()).hexdigest()[:16]


def proven_ref(node_id: str) -> str:
    """The ref holding the tree `node_id`'s proof was sealed against.

    Same slug rule as the baseline refs: `check-ref-format` decides a ref
    component by component, so an id git accepts under one
    `refs/saddle/<word>/` prefix it accepts under the other.
    """
    return f"{PROVEN_REF_PREFIX}{_ref_slug(node_id)}"


def attempt_ref(node_id: str, attempt: int) -> str:
    """The ref holding the tree attempt `attempt` of `node_id` was graded on.

    A failed attempt used to leave no ref at all: the baseline ref is the
    pre-node tree and a proven ref exists only for a pass, so `git gc`
    pruned the only copy of what the gates actually judged. Session 41
    had to rebuild round 3d's attempt trees out of `lost-found` dangling
    blobs to re-score them, and two of those blobs differed from
    each other only by autofix.

    The attempt number is its own ref component, so attempts 1..N of one
    node are N refs and not one that the last attempt overwrites. The
    slug rule is `proven_ref`'s: `check-ref-format` decides a ref
    component at a time, and a decimal attempt number is legal in every
    component position.
    """
    return f"{ATTEMPT_REF_PREFIX}{_ref_slug(node_id)}/{attempt}"


# The identity every commit saddle creates is authored under.
# `commit-tree` takes no identity of its own, so it falls back to git's
# auto-derived `user@host` -- which is not a fallback at all when the host
# has no domain: `unable to auto-detect email address (got
# 'user@host.(none)')`, exit 128. Round 3h died on that at its first
# node, 16 ms into the slice, and the transcript reported a node with no
# proof and no gate naming it, because no gate ran. `_ensure_repo` already
# committed the baseline under this name; the attempt snapshots did
# not, so a run's survival depended on the operator's git config and on
# DNS. Nothing here is a user identity: a run's refs are saddle's own.
SADDLE_COMMIT_IDENTITY: Final = (
    "-c",
    "user.name=saddle",
    "-c",
    "user.email=saddle@local",
)


def snapshot_tree(cwd: Path, ref: str | None, *, recorder: SpanRecorder | None = None) -> str:
    """Stage the tracked files at `cwd`; return their `git write-tree` id.

    `git add -u` stages tracked files only (no pathspec: git 2.45 and later
    reject `-- .` when nothing is tracked yet, as after `commit
    --allow-empty`; 2.43 did not, which is why CI was red while check.sh was
    green), so `.coverage.tier1`,
    `.saddle/` and bytecode stay out of the tree, while a file `git apply
    --index` added is already tracked and does go in. The id is a pure
    function of that content and nothing else: the same tracked tree
    hashes the same before and after it is committed, which is what lets
    a resume compare the worktree it was handed against the tree a proof
    was sealed on (`ref=None`: stage and hash, publish nothing).

    With a `ref`, the tree is also named -- `commit-tree -p HEAD` then
    `update-ref`, outside `refs/heads` and `refs/tags`, so `HEAD`, every
    branch and the set of paths the index tracks are unchanged and the
    worker's worktree is the worktree it had. `git diff`, `git diff
    --diff-filter=A` and `git archive` take the result exactly as they
    take `HEAD`.
    """

    def git(*args: str) -> str:
        argv = ["git", "-C", str(cwd), *args]
        start = perf_counter()
        proc = subprocess.run(argv, capture_output=True, text=True)
        _record(recorder, argv, start, proc)
        if proc.returncode != 0:
            msg = f"{' '.join(argv)} failed: {proc.stderr.strip()}"
            raise RuntimeError(msg)
        return proc.stdout.strip()

    git("add", "-u")
    tree = git("write-tree")
    if ref is not None:
        commit = git(
            *SADDLE_COMMIT_IDENTITY,
            "commit-tree",
            tree,
            "-p",
            "HEAD",
            "-m",
            f"saddle snapshot {ref}",
        )
        git("update-ref", ref, commit)
    return tree


def snapshot_baseline(cwd: Path, node_id: str, *, recorder: SpanRecorder | None = None) -> str:
    """Freeze the worktree at `cwd` as a commit; return the ref naming it.

    A node's gates must diff the node's own work, and `HEAD` is not that
    ref once a slice has more than one node: `_run_node` applies,
    autofixes, gates and seals but never commits, so every earlier node's
    proven edit is still staged when the next node runs. Against `HEAD`
    the second node's `target-scope` names the first node's files, its
    `coverage` demands lines it never wrote, and `red-phase` compares
    against a tree two nodes old.

    `snapshot_tree` does the work and states what it leaves untouched;
    this one only decides the ref.
    """
    ref = f"{BASELINE_REF_PREFIX}{_ref_slug(node_id)}"
    snapshot_tree(cwd, ref, recorder=recorder)
    return ref


def git_diff(cwd: Path, ref: str, *, recorder: SpanRecorder | None = None) -> str:
    """Zero-context diff of the worktree at `cwd` against git `ref`."""
    argv = ["git", "-C", str(cwd), "diff", "-U0", ref, "--", "."]
    start = perf_counter()
    proc = subprocess.run(
        argv,
        capture_output=True,
        text=True,
    )
    _record(recorder, argv, start, proc)
    if proc.returncode != 0:
        msg = f"git diff against {ref!r} failed: {proc.stderr.strip()}"
        raise RuntimeError(msg)
    return proc.stdout


def materialize_baseline(
    cwd: Path, ref: str, dest: Path, *, recorder: SpanRecorder | None = None
) -> None:
    """Extract tracked files at git `ref` into `dest` (empty when the tree is empty)."""
    argv = ["git", "-C", str(cwd), "archive", ref]
    start = perf_counter()
    proc = subprocess.run(
        argv,
        capture_output=True,
    )
    _record(recorder, argv, start, proc)
    if proc.returncode != 0:
        msg = f"git archive of {ref!r} failed: {proc.stderr.decode().strip()}"
        raise RuntimeError(msg)
    dest.mkdir(parents=True, exist_ok=True)
    try:
        with tarfile.open(fileobj=BytesIO(proc.stdout)) as tar:
            tar.extractall(dest, filter="data")
    except tarfile.ReadError:
        # `git archive` of an empty tree is not a readable tar: nothing to extract.
        return


def restore_baseline(cwd: Path, ref: str, *, recorder: SpanRecorder | None = None) -> None:
    """Put the worktree and index at `cwd` back to git `ref`.

    A node that gave up leaves its applied diffs staged; a replacement
    node would snapshot them as its own baseline and the merge suite
    would run over them. One `git restore --source <ref> --staged
    --worktree -- .` drops edits and staged adds alike (probed: a file
    `git apply --index` added since the ref is gone from disk after it).
    The span is named `restore-baseline` so the journal shows the tree
    was reset. Raises `RuntimeError` naming the ref if anything still
    differs from it afterwards; `HEAD` and every branch stand.
    """
    argv = ["git", "-C", str(cwd), "restore", "--source", ref, "--staged", "--worktree", "--", "."]
    start = perf_counter()
    proc = subprocess.run(argv, capture_output=True, text=True)
    _record(recorder, argv, start, proc, name="restore-baseline")
    if proc.returncode != 0:
        msg = f"git restore to {ref!r} failed: {proc.stderr.strip()}"
        raise RuntimeError(msg)
    leftover = git_changed_files(cwd, ref, recorder=recorder) + git_added_files(
        cwd, ref, recorder=recorder
    )
    if leftover:
        paths = ", ".join(sorted(set(leftover)))
        msg = f"worktree still differs from {ref!r} after restore: {paths}"
        raise RuntimeError(msg)


def under_coverage(test_command: str, data_file: str) -> str:
    """Wrap a pytest scope command so the run records into `data_file`."""
    return test_command.replace("pytest", f"coverage run --data-file={data_file} -m pytest", 1)


def pytest_scope(test_command: str) -> tuple[str, ...]:
    """The arguments a declared `test_command` hands to pytest, in order.

    Everything after the first `pytest` token, so the mutation gate's
    engine collects the same tests the tests gate ran. A
    command that never names pytest yields nothing, and the engine runs
    its whole tree as before.
    """
    argv = shlex.split(test_command)
    if "pytest" not in argv:
        return ()
    return tuple(argv[argv.index("pytest") + 1 :])


def scoped_targets(targets: Collection[str], scope: Collection[str]) -> tuple[str, ...]:
    """`targets` the declared pytest `scope` would itself collect.

    The property oracle runs its targets ALONE against the node's
    mutants, and mutmut baselines by running that selection, so one
    module the node's scope excludes decides the whole oracle. In a TDD
    plan such a module is red by construction -- the test node writes
    every module's specification up front and the impl node that greens
    it has not landed -- and the oracle then reports "no mutants
    sampled", which no diff the node can write will change.

    An empty `scope` is pytest's whole tree, so nothing is filtered.
    Flags are not selectors and are ignored; a `path::name` selector
    scopes by its path. What this admits, stated plainly: a node whose
    only property-bearing module lies outside its scope gets no oracle
    at all, the same way the mutation gate already runs only the node's
    declared tests.
    """
    roots = tuple(
        PurePath(item.split("::", 1)[0]).as_posix().rstrip("/")
        for item in scope
        if not item.startswith("-")
    )
    if not roots:
        return tuple(targets)
    kept = []
    for target in targets:
        path = PurePath(target).as_posix()
        if any(path == root or path.startswith(root + "/") for root in roots):
            kept.append(target)
    return tuple(kept)


type MutantDetail = tuple[str, str, str]  # (name, status, mutmut show text)
type SurvivorDetail = tuple[str, str, str, int, str, bool]
"""(name, status, path, line, mutation text, message-only) of one survivor."""


@dataclass(frozen=True)
class MutationOutcome:
    """Sampled kill-rate evidence over changed-line mutants."""

    killed: int
    total: int
    generated: int
    survivors: tuple[str, ...]
    # Mutants whose only change is inside string literals: no test
    # derived from a requirement can kill one without pinning wording, so
    # they leave the population and are counted here instead.
    text_only: int = 0
    # Where each survivor sits: the changed lines its removed hunk
    # lines matched, spelled as the caller spelled `changed`, so a recovery
    # can name the enclosing function without re-running the engine.
    survivor_lines: tuple[tuple[str, int], ...] = ()
    # How many sampled mutants decided "no tests": no test executes
    # the mutated function at all (mutmut's own exit codes 33 and 5). Their
    # names are already in `survivors` and their lines in `survivor_lines`,
    # so a repair round can target them; this is only the count for the
    # gate's own detail string.
    untested: int = 0
    # Every scored mutant's status string, counted after the
    # text-only exclusion and the `not checked` drop, so the counts
    # always sum to `total`. Nothing in `gates` reads this; it is
    # calibration evidence for the SIGKILL/SIGSEGV question (mutmut 3.8
    # maps both to "segfault", and can escalate a timeout to it) -- a
    # question the raw status distribution can answer without decoding
    # exit codes. An engine failure (`total == 0`) leaves it empty.
    statuses: tuple[tuple[str, int], ...] = ()
    # One row per survivor: (name, status, path as the caller
    # spelled it, the first changed line it locates to, the mutation as
    # mutmut shows it -- its removed and added hunk lines only -- and
    # whether `message_only_mutant` holds). The same population as
    # `survivors`, in name order; only `gates.check_mutation_shortlist`
    # (`--tier2 shortlist`) reads it. Not compared and not in the audit's
    # JSON (`audit.AuditResult.to_dict`), so `--tier2 score` is unchanged.
    survivor_details: tuple[SurvivorDetail, ...] = field(default=(), compare=False)
    mutant_detail: tuple[MutantDetail, ...] = field(default=(), compare=False)
    """(name, status, mutmut show text) for EVERY scored mutant, killed ones
    included, in name order; a mutant mutmut never scored (`not checked`) or
    that is not in `total` has none. Recording only: no verdict reads it."""


def mutation_text(show_output: str) -> str:
    """The `-`/`+` hunk lines of a `mutmut show` diff, headers dropped."""
    return "\n".join(
        line
        for line in show_output.splitlines()
        if line[:1] in "-+" and not line.startswith(("--- ", "+++ "))
    )


_LOG_METHODS: Final = frozenset(
    {"debug", "info", "warning", "warn", "error", "exception", "critical", "fatal", "log"}
)
_EXCEPTION_SUFFIXES: Final = ("Error", "Exception", "Warning")


def _callee(call: ast.Call) -> tuple[str, str]:
    """(receiver name, called name) of a call: `log.info` -> ("log", "info")."""
    func = call.func
    if isinstance(func, ast.Name):
        return "", func.id
    if isinstance(func, ast.Attribute):
        owner = func.value
        receiver = (
            owner.id
            if isinstance(owner, ast.Name)
            else owner.attr
            if isinstance(owner, ast.Attribute)
            else ""
        )
        return receiver, func.attr
    return "", ""


def _is_message_call(call: ast.Call, parent: ast.AST | None) -> bool:
    """A call whose arguments are only a message: an exception constructor
    (raised, or named `*Error`/`*Exception`/`*Warning`) or a logging call."""
    receiver, name = _callee(call)
    if isinstance(parent, ast.Raise) and parent.exc is call:
        return True
    if name.endswith(_EXCEPTION_SUFFIXES):
        return True
    return name in _LOG_METHODS and "log" in receiver.lower()


def _differences(
    old: ast.AST, new: ast.AST, trail: tuple[tuple[ast.AST, str], ...] = ()
) -> list[tuple[tuple[ast.AST, str], ...]]:
    """Where two trees differ, each as the trail of (old ancestor, field) down to it."""
    if type(old) is not type(new):
        return [trail]
    found: list[tuple[tuple[ast.AST, str], ...]] = []
    for name, left in ast.iter_fields(old):
        if name in ("ctx", "type_comment"):
            continue
        right = getattr(new, name, None)
        step = (*trail, (old, name))
        if isinstance(left, ast.AST) and isinstance(right, ast.AST):
            found.extend(_differences(left, right, step))
        elif isinstance(left, list) and isinstance(right, list) and len(left) == len(right):
            for a, b in zip(left, right, strict=True):
                if isinstance(a, ast.AST) and isinstance(b, ast.AST):
                    found.extend(_differences(a, b, step))
                elif a != b:
                    found.append(step)
        elif left != right:
            found.append(step)
    return found


def _in_message_argument(trail: tuple[tuple[ast.AST, str], ...]) -> bool:
    """The difference sits inside an argument of a message call (`_is_message_call`)."""
    for index, (node, name) in enumerate(trail):
        if isinstance(node, ast.Call) and name in ("args", "keywords"):
            parent = trail[index - 1][0] if index else None
            if _is_message_call(node, parent):
                return True
    return False


def _mutated_source(show_output: str, source: str, mutant_name: str) -> str | None:
    """`source` with the mutant's hunk applied, or None when it cannot be placed.

    The removed lines are found as one contiguous block inside the def
    mutmut mutated (`_mutant_def`), at that def's indentation added back
    (mutmut shows the def at column 0, as `_mutant_lines` explains); a
    name of neither production shape searches the whole file as written.
    """
    removed = [
        line[1:]
        for line in show_output.splitlines()
        if line.startswith("-") and not line.startswith("--- ")
    ]
    added = [
        line[1:]
        for line in show_output.splitlines()
        if line.startswith("+") and not line.startswith("+++ ")
    ]
    if not removed:
        return None
    lines = source.splitlines()
    start, end, indent = 1, len(lines), ""
    match = _MUTANT_NAME.match(mutant_name)
    if match is not None:
        try:
            node = _mutant_def(ast.parse(source), match)
        except SyntaxError:
            return None
        if node is None:
            return None
        start = node.decorator_list[0].lineno if node.decorator_list else node.lineno
        end = min(node.end_lineno or node.lineno, len(lines))
        found = re.match(r"[ \t]*", lines[node.lineno - 1])
        indent = found.group() if found else ""
    block = [indent + text for text in removed]
    for first in range(start - 1, end - len(block) + 1):
        if lines[first : first + len(block)] == block:
            mutated = [*lines[:first], *(indent + t for t in added), *lines[first + len(block) :]]
            return "\n".join(mutated) + "\n"
    return None


def message_only_mutant(show_output: str, source: str, mutant_name: str) -> bool:
    """Every AST difference the mutant makes is inside a message argument.

    Seen in a calibration read of the E-t8 s5 tree: a mutant that swaps the argument
    of a raised exception or of a logging call -- `ValueError(f"...")` to
    `ValueError(None)`, `KeyError(sku)` to `KeyError(None)` -- changes only
    what a message says. Decided by comparing the module's AST before and
    after the hunk, never by matching text: a mutant that also changes a
    comparison, a call's target or anything outside those arguments is not
    message-only. A hunk that cannot be placed or parsed is not either.
    """
    mutated = _mutated_source(show_output, source, mutant_name)
    if mutated is None:
        return False
    try:
        before, after = ast.parse(source), ast.parse(mutated)
    except SyntaxError:
        return False
    found = _differences(before, after)
    return bool(found) and all(_in_message_argument(trail) for trail in found)


def _is_given(decorator: ast.expr) -> bool:
    node = decorator.func if isinstance(decorator, ast.Call) else decorator
    name = (
        node.id
        if isinstance(node, ast.Name)
        else node.attr
        if isinstance(node, ast.Attribute)
        else ""
    )
    return name == "given"


def _imported_names(tree: ast.AST) -> set[str]:
    """Dotted names a module imports: `a.b` and, for `from a import b`, `a` and `a.b`."""
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            module = node.module or ""
            if module:
                names.add(module)
            names.update(f"{module}.{alias.name}" if module else alias.name for alias in node.names)
    return names


def property_modules(
    test_sources: Mapping[str, str], changed_files: Collection[str]
) -> dict[str, str]:
    """Test modules that drive a `@given` property over a changed module.

    A module counts when its AST carries a `given` decorator and one of
    its imports names a changed file: the dotted name, as a path, equals
    the changed file's path without `.py` (a package's `__init__.py` is
    its directory) or ends it at a `/` boundary. These are the modules
    the property oracle runs alone against the node's mutants.
    """
    targets: set[str] = set()
    for path in changed_files:
        posix = PurePath(path).as_posix().removesuffix(".py")
        targets.add(posix.removesuffix("/__init__") if posix.endswith("/__init__") else posix)
    matched: dict[str, str] = {}
    for path in sorted(test_sources):
        source = test_sources[path]
        try:
            tree = ast.parse(source)
        except SyntaxError:
            continue
        functions = (
            n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef | ast.AsyncFunctionDef)
        )
        if not any(_is_given(d) for f in functions for d in f.decorator_list):
            continue
        for name in _imported_names(tree):
            as_path = name.replace(".", "/")
            if any(t == as_path or t.endswith("/" + as_path) for t in targets):
                matched[path] = source
                break
    return matched


def _parse_mutant_verdicts(text: str) -> dict[str, str]:
    """Mutant name to verdict from `mutmut results --all True` output.

    Every decided status, not only killed/survived/timeout/not-checked,
    because a status this parser cannot match is a status that silently
    leaves both `total` and `killed` in `mutation_sample`.
    """
    verdicts = {}
    for line in text.splitlines():
        match = _MUTANT_VERDICT.match(line)
        if match:
            verdicts[match.group(1)] = match.group(2)
    return verdicts


def _mutant_path(show_output: str) -> str | None:
    """Repo-relative path from `mutmut show`, else None."""
    for line in show_output.splitlines():
        if line.startswith("+++ "):
            return line[4:].strip().removeprefix("b/")
    return None


_FSTRING_TEXT: Final = frozenset(
    getattr(tokenize, n) for n in ("FSTRING_MIDDLE",) if hasattr(tokenize, n)
)


def _tokens_modulo_strings(line: str) -> list[tuple[int, str]] | None:
    """A line's tokens with every string literal's text blanked, or None when
    the line does not tokenize on its own (a multi-line construct)."""
    try:
        tokens = list(tokenize.generate_tokens(io.StringIO(line + "\n").readline))
    except (tokenize.TokenError, SyntaxError):
        return None
    out: list[tuple[int, str]] = []
    for tok in tokens:
        if tok.type in (tokenize.NL, tokenize.NEWLINE, tokenize.ENDMARKER, tokenize.COMMENT):
            continue
        if tok.type in _FSTRING_TEXT:
            # An f-string's literal text arrives as a variable number of
            # middle tokens (3.12+); it is string content, so it is dropped
            # rather than blanked, and the expressions between stay.
            continue
        out.append((tok.type, "" if tok.type == tokenize.STRING else tok.string))
    return out


def text_only_mutant(show_output: str) -> bool:
    """Whether a `mutmut show` diff changes nothing but string-literal text.

    Round 3d's n2 was asked to kill 66 survivors of which 34 edited only a
    message (`"cannot convert"` to `"XXcannot convertXX"`). Pairwise:
    the removed and added lines must tokenize identically once string
    contents are blanked, and at least one string must differ. Anything that
    does not tokenize line by line stays in the population (fail closed).

    This is a shape test, not a semantic one, and it cannot be more: whether
    a string's contents are constrained is a property of the task, not of
    the code. t5's rule 1 requires the `ValueError` message to name all
    three currencies, and a string can equally be a currency code or a
    `Decimal` exponent -- round 3h classified `currency == "XXJPYXX"` and
    `Decimal("XX1XX")` as text-only, and the suite killed both.
    So `mutation_sample` consults this only for SURVIVORS, where an
    exclusion costs nothing it could have learned; a killed mutant of any
    shape is evidence the suite discriminates and stays in the population.
    """
    removed = [
        line[1:]
        for line in show_output.splitlines()
        if line.startswith("-") and not line.startswith("--- ")
    ]
    added = [
        line[1:]
        for line in show_output.splitlines()
        if line.startswith("+") and not line.startswith("+++ ")
    ]
    if not removed or len(removed) != len(added):
        return False
    differs = False
    for old, new in zip(removed, added, strict=True):
        if old == new:
            continue
        a, b = _tokens_modulo_strings(old), _tokens_modulo_strings(new)
        if a is None or b is None or a != b:
            return False
        differs = True
    return differs


def _mutant_def(
    tree: ast.Module, match: re.Match[str]
) -> ast.FunctionDef | ast.AsyncFunctionDef | None:
    """The `def` mutmut mutated, from `_MUTANT_NAME`'s parse of `mutant_name`.

    A method resolves only inside its own top-level `ClassDef`'s direct
    children (name alone is not enough -- two classes, or a
    module-level function sharing a method's name, must not collide). A
    function resolves only among top-level `def`s, matching the spec's
    "the top-level def named <func>".
    """
    cls_name = match.group("cls")
    func_name = match.group("func") or match.group("method")
    scope: list[ast.stmt] = tree.body
    if cls_name is not None:
        class_node = next(
            (n for n in tree.body if isinstance(n, ast.ClassDef) and n.name == cls_name),
            None,
        )
        if class_node is None:
            return None
        scope = class_node.body
    for node in scope:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == func_name:
            return node
    return None


def _statement_start(tree: ast.Module, line: int) -> int | None:
    """First line of the innermost `ast.stmt`/`ast.excepthandler` in `tree`
    whose `[lineno, end_lineno]` contains `line`; `None` if none does.

    This is the coordinate system `statement_lines` (and so `changed`)
    uses, built by taking the enclosing statement with the smallest span --
    a nested statement's range is always a subset of its parents', so the
    smallest one containing `line` is the innermost. Walking the whole
    module rather than just the resolved def's own subtree is deliberate:
    the def's own search range is what keeps a duplicate
    line elsewhere in the file out of `matched` in the first place, and
    this function must not independently re-derive that scoping, or a
    mutant that widens the range to the whole file stops being
    observable -- it would land back on a real statement (the duplicate's
    own) instead of the resolved def's, silently passing.
    """
    best: ast.stmt | ast.excepthandler | None = None
    best_span = -1
    for child in ast.walk(tree):
        if not isinstance(child, (ast.stmt, ast.excepthandler)):
            continue
        end = child.end_lineno or child.lineno
        if not (child.lineno <= line <= end):
            continue
        span = end - child.lineno
        if best is None or span < best_span:
            best, best_span = child, span
    return best.lineno if best is not None else None


def changed_statements(workdir: Path, diff: str) -> set[tuple[str, int]]:
    """First line of every statement with at least one of its own lines changed.

    A line belongs to a statement if it is non-blank, not a comment, and
    inside the statement's span; a decorator line belongs to the `def` or
    `class` it decorates. Docstrings stay exempt (`statement_lines`), and the
    spelling is `(str(workdir / rel), line)`, the one `changed` has always
    used. Only `.py` files that exist under `workdir` and parse contribute.
    Before continuation lines counted, a changed line that was not a statement's first line,
    such as the message of a multi-line `raise`, vanished, and a diff confined
    to such lines passed coverage with "no changed lines".
    """
    by_path: dict[str, set[int]] = {}
    for rel, number in changed_lines(diff):
        by_path.setdefault(rel, set()).add(number)
    found: set[tuple[str, int]] = set()
    for rel, numbers in by_path.items():
        path = workdir / rel
        if path.suffix != ".py" or not path.is_file():
            continue
        source = path.read_text()
        try:
            tree = ast.parse(source)
        except SyntaxError:
            continue
        text = source.splitlines()
        decorated = [
            (decorator.lineno, decorator.end_lineno or decorator.lineno, node.lineno)
            for node in ast.walk(tree)
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))
            for decorator in node.decorator_list
        ]
        executable = statement_lines(source)
        for number in numbers:
            stripped = text[number - 1].strip()
            if not stripped or stripped.startswith("#"):
                continue
            start = next(
                (owner for first, last, owner in decorated if first <= number <= last),
                _statement_start(tree, number),
            )
            if start in executable:
                found.add((str(workdir / rel), start))
    return found


def _mutant_lines(show_output: str, source: str, mutant_name: str) -> set[int]:
    """Statement-start line numbers the mutant's removed (`-`) hunk lines locate to.

    Scoped to the function mutmut actually mutated: a
    method mutant on a changed line enters the population, a mutant on a
    continuation line of a changed statement enters it, and a mutant whose
    text merely repeats a changed line elsewhere in the file does not.

    `mutant_name`'s two production shapes (`_MUTANT_NAME`) resolve a `def`
    with `ast` (`_mutant_def`). Only lines inside that def's own range --
    from its first decorator line (or the `def` line) to `end_lineno` --
    can match, at the def's own indentation added back (mutmut renders
    the extracted function at column 0, so every line including a
    continuation loses that one level of dedent -- verified against a
    real method mutant on a continuation line). The unreindented
    form is gone (scope narrowed): it matched a
    multi-line string's dedented content line against the wrong
    statement, and a no-effect check against real mutmut on the s1/s3/s4
    trees found no mutant whose outcome depended on it. Restricting
    `matched` to the def's own range is the only thing standing between a
    duplicate line elsewhere in the file and a wrong attribution:
    `_statement_start` looks up the innermost statement over the
    *whole* module, not just this def's subtree, so a matched line is
    trusted to belong to this def only because the range already
    confined it there. A matched line the whole module covers by no
    statement at all -- a decorator, since `ast.FunctionDef.lineno` is
    the `def` line and never the decorator's -- falls back to the def's
    own line, `statement_lines`'s coordinate for the whole decorated
    function.

    A name that does not match either shape is a hand-built test stub
    (`m1`, `m_hit1`, ...): production names always parse, so this keeps
    the old whole-file exact match, with no statement mapping, exactly as
    it was before this function took `mutant_name`.
    """
    removed = [
        line[1:]
        for line in show_output.splitlines()
        if line.startswith("-") and not line.startswith("--- ")
    ]
    if not removed:
        return set()
    match = _MUTANT_NAME.match(mutant_name)
    if match is None:
        numbered = list(enumerate(source.splitlines(), start=1))
        return {lineno for lineno, text in numbered for snippet in removed if text == snippet}
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return set()
    node = _mutant_def(tree, match)
    if node is None:
        return set()
    lines = source.splitlines()
    start = node.decorator_list[0].lineno if node.decorator_list else node.lineno
    end = min(node.end_lineno or node.lineno, len(lines))
    indent_match = re.match(r"[ \t]*", lines[node.lineno - 1])
    indent = indent_match.group() if indent_match else ""
    matched = {
        lineno
        for lineno in range(start, end + 1)
        for snippet in removed
        if lines[lineno - 1] == indent + snippet
    }
    return {(_statement_start(tree, lineno) or node.lineno) for lineno in matched}


def _mutmut_scratch_config(
    sources: list[str], run_tests: Collection[str] = (), also_copy: Sequence[str] = ()
) -> str:
    """Minimal mutmut config: per-file sources (a `.` root nests mutants/).

    `run_tests` are pytest arguments appended after the fixed flags, so
    only what they collect runs against each mutant; empty means the
    whole scratch tree, as before. A kill scored by a narrowed set
    belongs to that set, which the unrestricted run cannot say.
    A sequence keeps its order (a declared scope's `-k expr` must stay a
    pair); an unordered collection is sorted for a stable file.
    """
    quoted = ", ".join(json.dumps(source) for source in sources)
    ordered = list(run_tests) if isinstance(run_tests, Sequence) else sorted(run_tests)
    args = ["-q", "-x", "-p", "no:cacheprovider", *ordered]
    joined = ", ".join(json.dumps(arg) for arg in args)
    copied = (
        "also_copy = [" + ", ".join(json.dumps(path) for path in also_copy) + "]\n"
        if also_copy
        else ""
    )
    return f"[tool.mutmut]\nsource_paths = [{quoted}]\npytest_add_cli_args = [{joined}]\n{copied}"


class MutantLookupError(RuntimeError):
    """The batched mutant-lookup subprocess failed or gave unparseable output.

    A lookup failure must be named, never silently read as "no mutants"
    (the rule for `mutmut run`, extended to this lookup).
    """


# First line is the marker the journal is grepped for: argv for this
# call is `[sys.executable, "-c", _MUTANT_LOOKUP_SCRIPT]`, so the marker
# lands in the recorded span's argv and a census can count "one lookup
# subprocess" instead of one `mutmut show` per mutant. Reads every mutant's
# diff in one process: `SourceFileMutationData` and `get_diff_for_mutant`
# are the same functions `mutmut show NAME` calls
# (`mutmut/__main__.py:show`, `mutmut/mutation/diff_apply.py`), so walking
# `walk_mutatable_files()` once and loading each file's meta once
# reproduces `mutmut show`'s stdout byte for byte (measured 2026-09-22,
# `lookup_equiv.py`: 480/480, 535/535, 541/541 across three sealed trees).
# The first file to name a key wins, matching `find_mutant`. A name whose
# diff raises keeps only the header line, exactly what `show` printed to
# stdout before its traceback -- today's silent per-name skip, preserved.
_MUTANT_LOOKUP_SCRIPT: Final = r"""# saddle-mutant-lookup
import json
import sys

from mutmut.mutation.data import SourceFileMutationData
from mutmut.mutation.diff_apply import get_diff_for_mutant
from mutmut.stats import status_by_exit_code
from mutmut.utils.file_utils import walk_mutatable_files

mapping: dict[str, str] = {}
for path in walk_mutatable_files():
    data = SourceFileMutationData(path=path)
    data.load()
    for name, exit_code in data.exit_code_by_key.items():
        if name in mapping:
            continue
        header = f"# {name}: {status_by_exit_code[exit_code]}\n"
        try:
            diff = get_diff_for_mutant(name, path=data.path)
        except Exception:
            mapping[name] = header
        else:
            mapping[name] = header + diff + "\n"
json.dump(mapping, sys.stdout)
"""


def _lookup_failure(run: CapturedRun) -> MutantLookupError:
    """One line: the lookup subprocess's exit code and its last stderr line."""
    lines = run.stderr.strip().splitlines()
    last = lines[-1] if lines else "no output"
    msg = f"exit {run.exit_code}: {last}"
    return MutantLookupError(msg)


def show_all_mutants(scratch: Path, *, recorder: SpanRecorder | None = None) -> dict[str, str]:
    """`mutmut show NAME`'s stdout for every mutant, in one subprocess.

    Runs `_MUTANT_LOOKUP_SCRIPT` with `cwd=scratch`: mutmut's `config()` is a
    process-global cache read from `./pyproject.toml` and
    `read_mutants_module` opens `Path("mutants") / path` relative to the
    working directory, so the lookup must run as a subprocess rooted at
    `scratch` rather than inside saddle's own process. Raises
    `MutantLookupError` on a non-zero exit or unparseable stdout.
    """
    run = run_capture([sys.executable, "-c", _MUTANT_LOOKUP_SCRIPT], scratch, recorder=recorder)
    if run.exit_code != 0:
        raise _lookup_failure(run)
    try:
        mapping = json.loads(run.stdout)
    except ValueError:
        raise _lookup_failure(run) from None
    if not isinstance(mapping, dict) or not all(isinstance(v, str) for v in mapping.values()):
        raise _lookup_failure(run)
    return mapping


def mutation_sample(
    workdir: Path,
    changed: Collection[tuple[str, int]],
    max_mutants: int,
    *,
    test_files: Collection[str],
    run_tests: Collection[str] = (),
    suite_passed: bool = True,
    timeout_s: int = _MUTATION_TIMEOUT_S,
    recorder: SpanRecorder | None = None,
) -> MutationOutcome:
    """Kill-rate over every decided mutant on a changed line.

    Runs in a scratch copy (mutmut writes mutants/ into cwd) under a time
    budget; the verdict covers every changed-line mutant mutmut decided
    and degrades to decided mutants when the budget binds first (a real
    timeout, not a truncation: see `max_mutants` below). `max_mutants` is
    kept as a parameter only because the plan schema and its callers
    still pass it; it no longer samples or truncates the population
    (every scoped mutant is scored), so no verdict depends on which mutants sort first by
    name. `test_files` are excluded from mutation scope (mutating tests
    pollutes the rate);
    `run_tests` restricts which tests pytest collects against each mutant
    and leaves the scope alone -- the two are different sets (a
    session read the first as the second and built a vacuous oracle).
    Text-only mutants are excluded only when they did NOT kill (widened
    from "survived" alone to every not-killed status).
    Timeouts count as killed (behavior changed), and missing mutmut
    fails closed. Every decided mutant on a changed line enters the
    population: `killed` and `timeout` are the only killed
    statuses, `not checked` stays undecided, and everything else --
    `no tests` included -- is a survivor; `no tests` ones are also
    counted in `MutationOutcome.untested`.
    """
    if not changed:
        return MutationOutcome(killed=0, total=0, generated=0, survivors=())
    if shutil.which("mutmut", path=sandbox.gate_path(os.environ.get("PATH", ""))) is None:
        return MutationOutcome(killed=0, total=0, generated=0, survivors=("mutmut not on PATH",))
    root = os.path.realpath(workdir)
    by_line: dict[str, set[int]] = {}
    spelled: dict[str, str] = {}
    for path, line in changed:
        key = os.path.relpath(os.path.realpath(path), root)
        by_line.setdefault(key, set()).add(line)
        spelled.setdefault(key, path)
    tests = set(test_files)
    with tempfile.TemporaryDirectory(prefix="saddle-mutation-") as tmp:
        scratch = Path(tmp)
        for source in sorted(workdir.rglob("*.py")):
            if "__pycache__" in source.parts:
                continue
            dest = scratch / source.relative_to(workdir)
            dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, dest)
        # Only the files holding a changed line: a mutant on a changed line
        # lives in one of them, so the verdict is the same, and mutmut is not
        # asked to generate and run the whole tree (on saddle's own repo that
        # never finished: one module alone took 431 s to generate).
        # The rest are copied beside them unmutated (mutmut's work area holds
        # only what it is told about, and the tests import them).
        touched = {Path(key).as_posix() for key in by_line}
        every = sorted(
            path.relative_to(scratch).as_posix()
            for path in scratch.rglob("*.py")
            if path.relative_to(scratch).as_posix() not in tests
        )
        production = [path for path in every if path in touched]
        untouched = [path for path in every if path not in touched]
        if not production:
            return MutationOutcome(killed=0, total=0, generated=0, survivors=())
        (scratch / "pyproject.toml").write_text(
            _mutmut_scratch_config(production, run_tests, untouched)
        )
        ran = run_capture(
            ["timeout", str(timeout_s), "mutmut", "run"],
            scratch,
            recorder=recorder,
            memory_limit=tree_memory_limit(),
        )
        # `mutmut run` exits 0 even when mutants survive, so any other exit
        # is the tool failing, not a verdict: the smoke run's mutmut
        # 3.8 refused a package named `src` and exited 1 in 658 ms, and the
        # gate read "no mutants decided" -- the absence of a verdict, not
        # the tool. SHELL_TIMEOUT is the budget binding and keeps its path.
        if ran.exit_code not in (0, SHELL_TIMEOUT):
            output = (ran.stderr.strip() or ran.stdout.strip()).splitlines()
            last = output[-1].strip() if output else "no output"
            # The same exit covers two causes and only the caller can tell
            # them apart: mutmut baselines by running the suite, so
            # a red suite fails collection exactly as a broken engine does.
            # `suite_passed` is the tests gate's own verdict on this tree;
            # when it is False the suite is the cause and the engine is not.
            cause = f"mutmut run exited {ran.exit_code}: {last}"
            return MutationOutcome(
                killed=0,
                total=0,
                generated=0,
                survivors=(cause if suite_passed else f"suite is red: {cause}",),
            )
        results = run_capture(["mutmut", "results", "--all", "True"], scratch, recorder=recorder)
        verdicts = _parse_mutant_verdicts(results.stdout)
        try:
            shows = show_all_mutants(scratch, recorder=recorder)
        except MutantLookupError as exc:
            # A lookup failure is named, never read as "no mutants" (the
            # rule for `mutmut run`, extended to the batched lookup).
            msg = f"mutant lookup failed: {exc}"
            return MutationOutcome(killed=0, total=0, generated=0, survivors=(msg,))
        scoped: list[tuple[str, str, str, set[int]]] = []
        texts: dict[str, str] = {}
        message_only: dict[str, bool] = {}
        shown: dict[str, str] = {}
        undecided = 0
        text_only = 0
        for name in sorted(verdicts):
            verdict = verdicts[name]
            if verdict == "not checked":
                undecided += 1
                continue
            shown_stdout = shows.get(name, "")
            rel = _mutant_path(shown_stdout)
            if rel is None:
                continue
            posix_rel = rel.replace(os.sep, "/")
            if posix_rel in tests:
                continue
            key = os.path.relpath(os.path.realpath(scratch / rel), os.path.realpath(scratch))
            lines = by_line.get(key)
            if lines is None:
                continue
            target = scratch / rel
            if not target.is_file():
                continue
            hit = _mutant_lines(shown_stdout, target.read_text(), name) & lines
            if not hit:
                continue
            # Only a mutant that did NOT kill is excluded as
            # text-only (widened from "survived" alone to every
            # not-killed status, since the text-only argument -- no spec-derived
            # test can kill a message-only mutant without pinning wording
            # -- does not depend on whether a test currently runs the
            # function). The tokenizer cannot tell a message from a
            # currency code or a `Decimal` exponent, so round 3h dropped
            # `currency == "XXJPYXX"` and `Decimal("XX1XX")` -- real
            # behaviour changes the suite killed -- from both sides of the
            # ratio. A kill is evidence the suite discriminates; the
            # verdict decides, not the shape.
            if verdict not in ("killed", "timeout") and text_only_mutant(shown_stdout):
                text_only += 1
                continue
            scoped.append((name, verdict, key, hit))
            texts[name] = mutation_text(shown_stdout)
            shown[name] = shown_stdout
            if verdict not in ("killed", "timeout"):
                message_only[name] = message_only_mutant(shown_stdout, target.read_text(), name)
    sample = scoped
    killed = sum(1 for _, verdict, _, _ in sample if verdict in ("killed", "timeout"))
    # Every not-killed status is a survivor, not only
    # "survived": `no tests`, `suspicious`, `segfault` and the rest all
    # count against the node exactly as a survived mutant does.
    survivors = tuple(
        name for name, verdict, _, _ in sample if verdict not in ("killed", "timeout")
    )
    survivor_lines = sorted(
        {
            (spelled[key], line)
            for _, verdict, key, hit in sample
            if verdict not in ("killed", "timeout")
            for line in hit
        }
    )
    details = tuple(
        (name, verdict, spelled[key], min(hit), texts[name], message_only[name])
        for name, verdict, key, hit in sample
        if verdict not in ("killed", "timeout")
    )
    untested = sum(1 for _, verdict, _, _ in sample if verdict == "no tests")
    status_tally: dict[str, int] = {}
    for _, verdict, _, _ in sample:
        status_tally[verdict] = status_tally.get(verdict, 0) + 1
    return MutationOutcome(
        killed=killed,
        total=len(sample),
        generated=len(scoped) + undecided,
        survivors=survivors,
        text_only=text_only,
        survivor_lines=tuple(survivor_lines),
        untested=untested,
        statuses=tuple(sorted(status_tally.items())),
        survivor_details=details,
        mutant_detail=tuple((name, verdict, shown[name]) for name, verdict, _, _ in sample),
    )


def covered_lines(data_file: str, files: Collection[str]) -> set[tuple[str, int]]:
    """Executed lines per file from a coverage data file (empty when unreadable).

    Data keys are absolute while callers may pass workdir-relative paths, so
    the join normalizes both sides; emitted tuples keep the caller's spelling.
    """
    cov = coverage.Coverage(data_file=data_file, config_file=False)
    try:
        cov.load()
    except coverage.CoverageException:
        return set()
    data = cov.get_data()
    by_realpath = {os.path.realpath(measured): measured for measured in data.measured_files()}
    covered: set[tuple[str, int]] = set()
    for filename in files:
        measured = by_realpath.get(os.path.realpath(filename))
        if measured is None:
            continue
        for number in data.lines(measured) or ():
            covered.add((filename, number))
    return covered


def _docstring_lines(tree: ast.Module) -> set[int]:
    """First lines of docstrings: leading strings of modules, classes, functions."""
    found = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
            first = node.body[0] if node.body else None
            if (
                isinstance(first, ast.Expr)
                and isinstance(first.value, ast.Constant)
                and isinstance(first.value.value, str)
            ):
                found.add(first.lineno)
    return found


def statement_lines(source: str) -> set[int]:
    """Executable first-lines of `source`; unparseable source yields none.

    Conservative approximation of what coverage can execute (blanks,
    comments, and docstrings are never statements; `case` lines carry
    no position of their own and stay exempt). A syntax error means the
    syntax gate fails the node anyway, so there is nothing coverable to
    require here.
    """
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return set()
    lines = {
        node.lineno for node in ast.walk(tree) if isinstance(node, (ast.stmt, ast.excepthandler))
    }
    return lines - _docstring_lines(tree)
