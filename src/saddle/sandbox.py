"""Command execution with a real workdir boundary and background terminals.

Two things the old `run_command` did not do.

**The workdir is a boundary, not a default.** It used to set `cwd` and
nothing else, so `read_file("../../.ssh/id_rsa")` was a normal, working call.
Every path a tool touches is now resolved and required to stay inside the
root, symlinks included -- `Path.resolve()` first, then a containment check,
because a symlink out of the tree resolves out of the tree.

**A command can outlive the turn.** A build or a test suite takes minutes;
blocking the whole conversation on it is what makes an agent feel dead. A
command may be started in the background, its output read while it runs, and
waited on later -- which is what lets the model start work, say what it did,
and come back to the result.

**A command sees only what it needs.** Under `bwrap` the root is built up
from the system directories and the interpreter, read-only; HOME and /tmp
are empty; the workdir is the one writable place, and its `.git` is
read-only again, because hooks and config there are code the host runs
later. The environment is an allowlist, each command gets its own session
and process group, and a lane can take the network away. A `bwrap` that is
installed but cannot start is not trusted, and a lane that needs isolation
refuses to run without it. Where isolation is absent, commands run as the
invoking user and `Sandbox.isolation` says "none" rather than implying
protection that is not there. The gates' subprocesses on the tree's code
(its tests, `mutmut run`) use the same boundary through `confine`.
"""

from __future__ import annotations

import atexit
import functools
import os
import re
import shlex
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
import uuid
from collections.abc import Callable, Iterator, Mapping, Sequence
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from pathlib import Path
from time import monotonic
from time import time as wall_time
from typing import Final, Literal

from saddle import memcap
from saddle.procs import ProcessLedger, Tracked

DEFAULT_TIMEOUT: Final = 120
MAX_CAPTURE: Final = 400_000
"""Per-terminal output cap. Beyond this the head and tail are kept: an
unbounded buffer is how a `yes` loop takes the whole session down."""


class OutsideRootError(ValueError):
    """A path escaped the workdir. Raised before anything is opened."""


class IsolationUnavailableError(RuntimeError):
    """A lane that needs isolation asked for it on a box that cannot give it."""


Network = Literal["host", "none"]

ENV_KEEP: Final = (
    "PATH",
    "HOME",
    "USER",
    "LOGNAME",
    "SHELL",
    "LANG",
    "LANGUAGE",
    "TZ",
    "VIRTUAL_ENV",
    "PYTHONPATH",
    "PYTHONDONTWRITEBYTECODE",
)
"""The only variables of saddle's own environment a command sees, plus any
`LC_*`. An allowlist rather than a `*KEY*`/`*TOKEN*` denylist: a key can be
named anything, and saddle's process holds the model key when it is exported."""


DESKTOP_ENV: Final = (
    "DISPLAY",
    "WAYLAND_DISPLAY",
    "XDG_RUNTIME_DIR",
    "XDG_SESSION_TYPE",
    "DBUS_SESSION_BUS_ADDRESS",
    "PULSE_SERVER",
)
"""The desktop session's variables: where a window opens, the runtime directory,
the session bus and the audio server. Not in `ENV_KEEP`: a sandboxed command
has no display or bus to reach. A session's full access (`desktop_env`) is the
only way they reach a command, so a program it starts can open a window."""


def desktop_env() -> dict[str, str]:
    """The `DESKTOP_ENV` variables saddle's own environment has set."""
    return {name: os.environ[name] for name in DESKTOP_ENV if name in os.environ}


def command_env(extra: Mapping[str, str]) -> dict[str, str]:
    """The environment a command runs with: the allowlist, then `extra`."""
    kept = {
        name: value
        for name, value in os.environ.items()
        if name in ENV_KEEP or name.startswith("LC_")
    }
    return {**kept, "TERM": "dumb", "NO_COLOR": "1", **extra}


def tool_dir() -> Path | None:
    """The directory of the interpreter running saddle, or None when Python
    cannot name it (`sys.executable` is empty).

    In a virtual environment this is the venv's `bin`, where the installer
    put the gate tools saddle depends on (`pytest`, `coverage`, `ruff`,
    `mutmut`) beside the `python` they run on. It is inside `sys.prefix`,
    which `bwrap_argv` already shows read-only."""
    return Path(sys.executable).parent if sys.executable else None


PROJECT_VENVS: Final = (".venv", "venv")
"""Where a project keeps its own virtualenv, relative to its folder, in the
order they are tried. A directory counts only with a `pyvenv.cfg`."""

GATE_ENV_TOOLS: Final = ("python", "pytest", "coverage", "mutmut")
"""What a project venv must hold, in its `bin`, for the gates to run on it:
the test command's `python` (which also runs `-m pytest` and `-m coverage`)
and `mutmut`, which runs the tests in its own process. A venv missing one
would run that gate on a different interpreter, without the project's
packages, and refuse correct work."""

_PROJECT_ENV: ContextVar[Path | None] = ContextVar("saddle_project_env", default=None)


def project_env(folder: Path, environ: Mapping[str, str] | None = None) -> Path | None:
    """The project's own virtualenv for work on `folder`, or None.

    `folder` is the user's folder, never a run's worktree: a worktree holds
    tracked files only, so an untracked `.venv` is not in it. Tried in
    order: each of `PROJECT_VENVS` under `folder`, then an active
    `VIRTUAL_ENV` (from `environ`, default saddle's own environment) unless
    that is saddle's own environment, which is no project's."""
    for name in PROJECT_VENVS:
        candidate = folder / name
        if (candidate / "pyvenv.cfg").is_file():
            return candidate.resolve()
    active = (os.environ if environ is None else environ).get("VIRTUAL_ENV", "")
    if active and (Path(active) / "pyvenv.cfg").is_file():
        real = Path(active).resolve()
        if real != Path(sys.prefix).resolve():
            return real
    return None


def project_env_problem(env: Path) -> str | None:
    """None when the gates can run on `env`, else a setup error naming what is missing."""
    missing = [name for name in GATE_ENV_TOOLS if not (env / "bin" / name).exists()]
    if not missing:
        return None
    return (
        f"setup: the project's virtualenv {env} has no {', '.join(missing)}; the auditor "
        "runs the tests on it, so install them into it "
        f"({env / 'bin' / 'python'} -m pip install {' '.join(missing)})"
        if "python" not in missing
        else f"setup: the project's virtualenv {env} has no python in {env / 'bin'}"
    )


@contextmanager
def using_project_env(env: Path | None) -> Iterator[None]:
    """Within the block (this thread, this context), the gates run on `env`:
    `gate_path` puts its `bin` first and `confine` shows it read-only."""
    token = _PROJECT_ENV.set(env)
    try:
        yield
    finally:
        _PROJECT_ENV.reset(token)


def project_command_env(env: Path | None, path: str | None = None) -> dict[str, str]:
    """Extra environment for the model's commands on a project with venv
    `env`: its `bin` first on `path` (default saddle's PATH), and
    `VIRTUAL_ENV` naming it, which `default_expose` shows read-only.
    Empty when there is no project venv: PATH is then left as it was."""
    if env is None:
        return {}
    rest = os.environ.get("PATH", "") if path is None else path
    bin_dir = str(env / "bin")
    entries = [entry for entry in rest.split(os.pathsep) if entry and entry != bin_dir]
    return {"PATH": os.pathsep.join([bin_dir, *entries]), "VIRTUAL_ENV": str(env)}


def gate_path(path: str) -> str:
    """`path`, with the project venv's `bin` first when one is in use
    (`using_project_env`), then `tool_dir()` as the last entry when `path`
    lacks it.

    The gates run their tools by bare name. A tool on the user's PATH wins:
    it belongs to the environment the project's tests were written for,
    with the project's own dependencies beside it. A `uv tool` or `pipx`
    install puts only the `saddle` script on PATH, so a tool the user has
    nowhere else is found beside saddle's interpreter instead of not at all
    (which read as "tests failed", a refusal of correct work). Only the
    gates use this; a command the model runs (`command_env`) never sees
    saddle's own directory."""
    env = _PROJECT_ENV.get()
    if env is not None:
        path = project_command_env(env, path)["PATH"]
    fallback = tool_dir()
    entries = [entry for entry in path.split(os.pathsep) if entry]
    shim = _python3_shim(entries)
    tail = [str(shim)] if shim is not None else []
    if fallback is None or any(Path(entry) == fallback for entry in entries):
        return os.pathsep.join([*entries, *tail]) if tail else path
    return os.pathsep.join([*entries, *tail, str(fallback)])


GATE_MODULES: Final = ("pytest", "coverage", "mutmut")
"""What a `python3` must import, under the gates' own confinement, to run
the gates in place of a missing `python`."""

PROBE_TIMEOUT_S: Final = 60.0

_SHIMS: dict[Path, str] = {}
"""Shim directory -> the `python3` its `python` runs (`_shim_for`)."""


def _python3_shim(entries: Sequence[str]) -> Path | None:
    """A directory whose `python` runs the `python3` on `entries`, when
    `entries` has no `python` and that `python3` can run the gates; else None.

    Ubuntu ships `python3` without `python` unless python-is-python3 is
    installed, and the test command is `python -m pytest`. Without this the
    gates fell back to saddle's own interpreter, which has none of the
    project's packages, and refused correct work. That `python3` is used
    only when it imports every one of `GATE_MODULES` inside the sandbox and
    a `mutmut` is on `entries` too (mutmut runs the tests in its own
    process): one that cannot would refuse correct work the same way, so
    saddle's own stays the fallback. Nothing on the user's system is
    changed: the shim is a script in a directory saddle owns."""
    joined = os.pathsep.join(entries)
    if shutil.which("python", path=joined) is not None:
        return None
    found = shutil.which("python3", path=joined)
    if found is None or shutil.which("mutmut", path=joined) is None:
        return None
    python3 = str(Path(found).absolute())
    return _shim_for(python3) if _runs_gates(python3) else None


@functools.cache
def _runs_gates(python3: str) -> bool:
    """`python3` imports all of `GATE_MODULES`, run confined as a gate runs
    (so a package the sandbox hides counts as missing). Asked once per
    saddle process: install the tools, then restart saddle."""
    # Its own directory first, so `default_expose` shows the venv it lives
    # in; then saddle's PATH, where `bwrap` itself is found.
    env = command_env(
        {"PATH": os.pathsep.join([str(Path(python3).parent), os.environ.get("PATH", "")])}
    )
    argv = [python3, "-c", f"import {', '.join(GATE_MODULES)}"]
    with tempfile.TemporaryDirectory(prefix="saddle-probe-") as root:
        wrapped, lent = _confined(argv, Path(root), env)
        try:
            done = subprocess.run(
                wrapped,
                cwd=root,
                env=lent,
                capture_output=True,
                timeout=PROBE_TIMEOUT_S,
                check=False,
            )
        except subprocess.TimeoutExpired:
            return False
    return done.returncode == 0


@functools.cache
def _shim_for(python3: str) -> Path:
    """A private directory holding one script, `python`, that execs `python3`.

    A script, not a link: a venv's interpreter finds its `pyvenv.cfg` next
    to the path it was started by, so a link elsewhere would lose the venv."""
    where = Path(tempfile.mkdtemp(prefix="saddle-python-")).resolve()
    script = where / "python"
    script.write_text(f'#!/bin/sh\nexec {shlex.quote(python3)} "$@"\n', encoding="utf-8")
    script.chmod(0o755)
    atexit.register(shutil.rmtree, where, True)
    _SHIMS[where] = python3
    return where


def gate_environment() -> str:
    """Which environment the gates run the tests on, in words, for the
    run's sealed start record: the project venv in use, else the `python`
    `gate_path` finds and where it came from. No `;` (the record's field
    separator) survives unescaped."""
    env = _PROJECT_ENV.get()
    if env is not None:
        said = f"the project's virtualenv {env}"
    else:
        found = shutil.which("python", path=gate_path(os.environ.get("PATH", "")))
        fallback = tool_dir()
        shimmed = _SHIMS.get(Path(found).parent) if found is not None else None
        if found is None:
            said = "no python found"
        elif shimmed is not None:
            said = f"{shimmed} from PATH (there is no `python` on PATH)"
        elif fallback is not None and Path(found).parent == fallback:
            said = f"saddle's own interpreter {found}"
        else:
            said = f"{found} from PATH"
    return said.replace(";", "%3B")


def environment_key() -> str:
    """What the gates' environment holds, for the auditor's cache key.

    `gate_environment`'s words, plus, when a project venv is in use, the
    names in its site-packages and in those of every venv it layers on. An
    approved install (`installs`) adds a `.dist-info` there, so a verdict
    cached before it is not reused after it on an unchanged tree."""
    parts = [gate_environment()]
    env = _PROJECT_ENV.get()
    seen: set[Path] = set()
    while env is not None and env not in seen:
        seen.add(env)
        for site in sorted(env.glob("lib/python*/site-packages")):
            parts.append(f"{site}: {' '.join(sorted(p.name for p in site.iterdir()))}")
        env = layers_on(env)
    return "\n".join(parts)


def gate_env() -> dict[str, str]:
    """saddle's own environment with `gate_path` as PATH, for the gate tool
    runs that are not confined (`ruff`, the version probe)."""
    return {**os.environ, "PATH": gate_path(os.environ.get("PATH", ""))}


HOST_GIT_GUARD: Final = ("-c", "core.fsmonitor=", "-c", "core.hooksPath=/dev/null")
"""Options for every git command saddle itself runs in a tree a command
could have written: no fsmonitor, no hooks. Git runs both as programs, from
config, so a hook or an fsmonitor written into `.git` is code the host
executes the next time saddle stages or commits. Under bwrap a command cannot
write `.git` at all; this is the second layer, for a run without bwrap and a
repo that already carried such config. saddle's own commits, worktrees and
merges need no hook, so nothing a run relies on is lost."""


SYSTEM_DIRS: Final = (
    "/usr",
    "/bin",
    "/sbin",
    "/lib",
    "/lib32",
    "/lib64",
    "/libx32",
    "/etc",
    "/opt",
    "/sys",
)
"""What a command may read besides its workdir and the interpreter. Built up
from an empty root rather than `--ro-bind / /` minus a list: a deny list of
places misses the one nobody thought of (a sibling answer dir, /var/tmp,
another agent's scratch, /run/user sockets)."""

RESOLVER_DIR: Final = "/run/systemd/resolve"
"""Where /etc/resolv.conf points on systemd boxes; needed only with network."""


GATE_TOOLS: Final = ("python", "python3", "pytest", "ruff", "coverage", "mutmut")
"""Names a command runs bare that saddle's own gates also run. Where one of
them lives in a venv outside the system dirs (a project's `.venv`, a
`uv tool` or `pipx` install under HOME), that venv is shown read-only."""


def _venv_root(tool: Path) -> Path | None:
    """The venv holding `tool` (`<venv>/bin/<tool>`), if it is one."""
    root = tool.parent.parent
    return root if (root / "pyvenv.cfg").is_file() else None


def _base_prefix(venv: Path) -> Path | None:
    """Where the venv's interpreter comes from (`home` in `pyvenv.cfg`)."""
    for line in (venv / "pyvenv.cfg").read_text(encoding="utf-8", errors="replace").splitlines():
        key, sep, value = line.partition("=")
        if sep and key.strip() == "home" and value.strip():
            return Path(value.strip()).resolve().parent
    return None


LAYERS_ON: Final = "saddle-layers-on"
"""The `pyvenv.cfg` key of a run's install overlay (`installs.Installs`): the
project venv it layers on, whose site-packages its own `.pth` adds."""


def layers_on(venv: Path) -> Path | None:
    """The venv `venv` layers on (its `LAYERS_ON` key), when that is a venv."""
    try:
        text = (venv / "pyvenv.cfg").read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None
    for line in text.splitlines():
        key, sep, value = line.partition("=")
        if sep and key.strip() == LAYERS_ON and value.strip():
            under = Path(value.strip())
            return under.resolve() if (under / "pyvenv.cfg").is_file() else None
    return None


CONFINED_ENV: Final = "SADDLE_CONFINED"
"""Set to `1` in every `confine`d gate run (the audit's suite, mutation, gate
stages), so a test can tell it runs inside an audit's sandbox: a test that runs
a whole audit of its own there nests one confinement in another, which cannot
reproduce the outer run, and says so by skipping (the outer audit then lists it
as not proven)."""

EXPOSE_ENV: Final = "SADDLE_SANDBOX_EXPOSE"
"""Comma-separated command names a sandboxed command may also run, beyond the
gate tools: each is found on saddle's own PATH, and its install directory is
shown read-only (`exposed_commands`). Unset or empty, nothing more is shown.
A project names its own under `[tool.saddle] sandbox-expose`
(`also_exposing`), which adds to these. For a project whose tests need a
tool installed under HOME -- `node` and a browser for web tests, say --
which the sandbox otherwise hides."""


_EXPOSED: ContextVar[tuple[str, ...]] = ContextVar("saddle_exposed", default=())


@contextmanager
def also_exposing(names: Sequence[str]) -> Iterator[None]:
    """Within the block (this thread, this context), `default_expose` also shows
    each command in `names`, as `EXPOSE_ENV` would.

    The names are a project's own (`evidence.sandbox_expose`, read at the
    audit's baseline), so a gate run for that project can run its `node` and
    browser without the person who launched saddle exporting anything. Carried
    like `using_project_env`, so a worker thread started under
    `contextvars.copy_context` sees it too."""
    token = _EXPOSED.set(tuple(names))
    try:
        yield
    finally:
        _EXPOSED.reset(token)


_SHOWN: ContextVar[tuple[Path, ...]] = ContextVar("saddle_shown", default=())


@contextmanager
def also_showing(paths: Sequence[Path]) -> Iterator[None]:
    """Within the block (this thread, this context), every `confine`d run also
    shows each directory in `paths` read-only at its own path, as its `shown`
    argument does.

    The audit shows the checkout's git-ignored dependency directories, which its
    staged copy leaves out, so the suite it runs sees what the project's own gate
    sees. Carried like `also_exposing`."""
    token = _SHOWN.set(tuple(paths))
    try:
        yield
    finally:
        _SHOWN.reset(token)


def exposed_commands(path: str, names: str) -> dict[Path, Path]:
    """Read-only `{destination: source}` binds for each command in `names`.

    The command's resolved file is shown at the spelling found on `path` (so a
    link in a hidden `~/.local/bin` still runs), and the directory it is
    installed in is shown whole -- the parent of its `bin/` when it sits in one,
    else its own directory -- because a runtime needs its libraries beside it;
    but never a prefix many tools share (`_shared_prefixes`), whose tool shows
    its file alone.
    A name not found is skipped: the command then fails inside the sandbox as
    it would have, rather than saddle refusing to start."""
    binds: dict[Path, Path] = {}
    for name in (n.strip() for n in names.split(",")):
        found = shutil.which(name, path=path) if name else None
        if found is None:
            continue
        spelled, landed = Path(found).absolute(), Path(found).resolve()
        home = landed.parent.parent if landed.parent.name == "bin" else landed.parent
        if home in _shared_prefixes():
            # A binary installed straight into ~/.local/bin: the prefix is everyone's,
            # so the file alone is shown, never the rest of ~/.local.
            binds[landed] = landed
        else:
            binds[home] = home
        if spelled != landed:
            binds[spelled] = landed
    return binds


def _shared_prefixes() -> frozenset[Path]:
    """Install prefixes many tools share under HOME. Showing one whole for a single
    tool would show the others' files too, and a later bind of a file inside it
    (another named tool's link in `~/.local/bin`) could not be made, so bwrap
    would not start at all."""
    home = Path.home().resolve()
    return frozenset({home, home / ".local"})


def default_expose(env: Mapping[str, str]) -> tuple[tuple[Path, Path], ...]:
    """Read-only `(source, destination)` binds for the gate tools `env` finds.

    Minimal on purpose: each venv a gate tool (or `VIRTUAL_ENV`) lives in,
    the interpreter that venv was made from, and the tool's own PATH entry
    when that is a link into the venv; and, for a `python` or `python3`
    that is no venv's, that interpreter's user site-packages
    (`user_site`); and, for a run's install overlay, the venv it layers
    on (`layers_on`). Nothing else under HOME: a sibling of the venv, the rest
    of `~/.local` and `~/.local/bin`, and dotfiles all stay hidden."""
    binds: dict[Path, Path] = {}
    venvs: list[Path] = []
    if env.get("VIRTUAL_ENV"):
        venvs.append(Path(env["VIRTUAL_ENV"]))
    for name in GATE_TOOLS:
        found = shutil.which(name, path=env.get("PATH", ""))
        if found is None:
            continue
        spelled, landed = Path(found).absolute(), Path(found).resolve()
        for tool in (spelled, landed):
            venv = _venv_root(tool)
            if venv is not None:
                venvs.append(venv)
        if spelled != landed and _venv_root(landed) is not None:
            binds[spelled] = landed
        if (
            name in ("python", "python3")
            and _venv_root(spelled) is None
            and _venv_root(landed) is None
        ):
            site = user_site(landed)
            if site is not None:
                binds[site] = site
    named = ",".join([os.environ.get(EXPOSE_ENV, ""), *_EXPOSED.get()])
    binds.update(exposed_commands(env.get("PATH", ""), named))
    for venv in venvs:
        if not (venv / "pyvenv.cfg").is_file():
            continue
        real = venv.resolve()
        if real in binds:
            continue
        binds[real] = real
        base = _base_prefix(real)
        if base is not None:
            binds[base] = base
        under = layers_on(real)
        if under is not None:
            venvs.append(under)  # an overlay's packages include the project venv's
    return tuple((source, dest) for dest, source in sorted(binds.items()) if not _is_system(dest))


def user_site(interpreter: Path) -> Path | None:
    """`~/.local/lib/pythonX.Y/site-packages` for an interpreter named
    `pythonX.Y` (as `/usr/bin/python3` resolves), when that directory exists.

    Shown read-only, and it alone of HOME, because `pip install --user` is
    how a system Python gets pytest on Debian and Ubuntu: hidden, the gates
    run a different environment from the user's own `python3 -m pytest`
    and refuse correct work. It holds installed code, not credentials;
    what it points at elsewhere under HOME (an editable install's `.pth`)
    stays hidden. A venv's interpreter is never given it: a venv ignores
    the user site."""
    match = re.fullmatch(r"python(\d+\.\d+)", interpreter.name)
    if match is None:
        return None
    site = Path.home() / ".local" / "lib" / f"python{match[1]}" / "site-packages"
    return site if site.is_dir() else None


def _is_system(path: Path) -> bool:
    """Already visible: under one of `SYSTEM_DIRS`."""
    return any(path == Path(d) or Path(d) in path.parents for d in SYSTEM_DIRS)


@functools.cache
def bwrap_works(bwrap: str) -> str | None:
    """None when `bwrap` can build a sandbox here, else its error text.

    An installed bwrap is not a working one: where unprivileged user
    namespaces are restricted (Ubuntu 24.04's AppArmor default) it exists and
    fails on every call, and trusting `which` labels that "bwrap"."""
    try:
        done = subprocess.run(
            [bwrap, "--ro-bind", "/", "/", "--unshare-all", "true"],
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return str(exc)
    return None if done.returncode == 0 else (done.stderr.strip() or f"exit {done.returncode}")


def resolve_within(root: Path, candidate: str | Path) -> Path:
    """Resolve `candidate` under `root`, or raise `OutsideRoot`.

    Resolution happens first so that a symlink pointing out of the tree is
    caught by where it *lands*, not by how it is spelled.
    """
    base = root.resolve()
    target = (base / Path(candidate)).resolve()
    if target != base and base not in target.parents:
        msg = f"{candidate!r} resolves outside the working directory"
        raise OutsideRootError(msg)
    return target


def isolation_problem() -> str | None:
    """None when this box's `bwrap` can build a sandbox, else why not."""
    found = shutil.which("bwrap")
    return "bwrap is not installed" if found is None else bwrap_works(found)


def _git_binds(root: Path) -> list[str]:
    """Read-only binds that keep git usable and its metadata untouchable.

    A writable `.git` is code the host runs later: a hook, a
    `core.fsmonitor`, or a worktree's `.git` file pointed at a repo the
    command made. So `.git` is read-only, and a worktree's real gitdir
    (outside the workdir, hidden otherwise) is shown read-only too."""
    dot = root / ".git"
    if dot.is_dir():
        return ["--ro-bind", str(dot), str(dot)]
    if not dot.is_file():
        return []
    binds = ["--ro-bind", str(dot), str(dot)]
    text = dot.read_text(encoding="utf-8", errors="replace").strip()
    if text.startswith("gitdir:"):
        gitdir = (root / text.removeprefix("gitdir:").strip()).resolve()
        common = gitdir / "commondir"
        shown = [gitdir]
        if common.is_file():
            shown.append((gitdir / common.read_text(encoding="utf-8").strip()).resolve())
        for path in shown:
            binds += ["--ro-bind-try", str(path), str(path)]
    return binds


def bwrap_argv(
    root: Path,
    argv: Sequence[str],
    *,
    network: Network,
    expose: Sequence[tuple[Path, Path]],
    writable: Sequence[Path] = (),
    tmp: Path | None = None,
    mounted: Sequence[tuple[Path, Path]] = (),
) -> list[str]:
    """`argv` wrapped in bwrap: `root` (and each of `writable`) is the only
    writable place, and nothing else outside the system dirs, the
    interpreter and `expose` exists. `/tmp` is empty, or `tmp` when given
    (`Sandbox.tmp`); never the host's. `mounted` are read-only `(source, dest)`
    binds inside `root`, made after it so it does not hide them."""
    wrapped = ["bwrap"]
    for name in SYSTEM_DIRS:
        path = Path(name)
        if path.is_symlink():
            wrapped += ["--symlink", os.readlink(path), name]
        else:
            wrapped += ["--ro-bind-try", name, name]
    wrapped += ["--dev", "/dev", "--proc", "/proc"]
    wrapped += ["--tmpfs", "/tmp"] if tmp is None else ["--bind", str(tmp), "/tmp"]
    wrapped += ["--tmpfs", str(Path.home())]
    if network == "host":
        wrapped += ["--ro-bind-try", RESOLVER_DIR, RESOLVER_DIR]
    for prefix in sorted({Path(sys.prefix).resolve(), Path(sys.base_prefix).resolve()}):
        wrapped += ["--ro-bind", str(prefix), str(prefix)]
    for source, dest in expose:
        wrapped += ["--ro-bind", str(source), str(dest)]
    # Order matters: later mounts shadow earlier ones, so the writable
    # workdir comes after the tmpfs mounts (a workdir under /tmp or HOME
    # would vanish behind them), and the read-only .git after the workdir.
    for path in writable:
        wrapped += ["--bind", str(path), str(path)]
    wrapped += ["--bind", str(root), str(root), *_git_binds(root)]
    for source, dest in mounted:
        wrapped += ["--ro-bind", str(source), str(dest)]
    wrapped += ["--unshare-all"]
    if network == "host":
        wrapped += ["--share-net"]
    return [*wrapped, "--new-session", "--die-with-parent", "--chdir", str(root), *argv]


GATE_NETWORK: Final[Network] = "none"
"""A gate runs the tree's own tests, which the agent may have written, and
nothing a gate measures needs the host network; a test's own loopback
server still works in the empty namespace."""


def confine(
    argv: Sequence[str],
    root: Path,
    *,
    writable: Sequence[Path] = (),
    extra_env: Mapping[str, str] | None = None,
    shown: Sequence[Path] = (),
) -> tuple[list[str], dict[str, str]]:
    """The argv and environment that run a gate's `argv` on the tree at `root`.

    The same boundary a command gets (`Sandbox`), for the subprocesses the
    gates start on the tree's code (its tests under pytest and coverage,
    `mutmut run`): `root` writable, `writable` (a scratch dir the gate
    reads back) writable too, the gate-tool venvs read-only, no network,
    and the scrubbed environment. The fallback matches
    `Sandbox.for_workdir`'s default: where bwrap cannot start, the argv
    runs unwrapped, with the environment still scrubbed.

    PATH is `gate_path`'s: the project venv's `bin` when one is in use
    (`using_project_env`, which also sets `VIRTUAL_ENV` to it), the user's
    tools, then saddle's own as the fallback. `default_expose` shows the
    venv each tool it finds lives in read-only; saddle's own is inside
    `sys.prefix`, shown regardless.
    `extra_env` is laid over the scrubbed environment last. `shown` are
    directories outside `root` that the run may read and not write, each at
    its own path: a tool whose files live beside the project (the browser
    files' `node_modules`, which the staged copy leaves out)."""
    project = _PROJECT_ENV.get()
    venv = {"VIRTUAL_ENV": str(project)} if project is not None else {}
    base = {"PATH": gate_path(os.environ.get("PATH", "")), CONFINED_ENV: "1", **venv}
    env = command_env({**base, **(extra_env or {})})
    return _confined(argv, root, env, writable, (*shown, *_SHOWN.get()))


def _confined(
    argv: Sequence[str],
    root: Path,
    env: dict[str, str],
    writable: Sequence[Path] = (),
    shown: Sequence[Path] = (),
) -> tuple[list[str], dict[str, str]]:
    """`confine` for an environment already built: `argv` under bwrap with
    `root` writable, or unwrapped where bwrap cannot start."""
    if isolation_problem() is not None:
        return list(argv), env
    real = root.resolve()
    extra = [path.resolve() for path in writable]
    expose = default_expose(env)
    # A `_shim_for` script is a bare `python` on PATH outside any venv, so
    # `_bare_tools` shows it like any other such tool.
    wrapped = bwrap_argv(
        real,
        argv,
        network=GATE_NETWORK,
        expose=(*expose, *_bare_tools(env, expose), *((p.resolve(), p.resolve()) for p in shown)),
        writable=extra,
    )
    return wrapped, env


def _bare_tools(
    env: Mapping[str, str], expose: Sequence[tuple[Path, Path]]
) -> tuple[tuple[Path, Path], ...]:
    """Read-only binds for gate tools on PATH that no venv bind already shows.

    A gate runs `mutmut` or `pytest` by bare name, and the host resolves
    that name on PATH; the confined run must find the same program, not a
    different one further along PATH. Only the file itself is shown, never
    the directory it sits in."""
    shown = [dest for _, dest in expose]
    binds: list[tuple[Path, Path]] = []
    for name in GATE_TOOLS:
        found = shutil.which(name, path=env.get("PATH", ""))
        if found is None:
            continue
        spelled = Path(found).absolute()
        if _is_system(spelled) or any(d == spelled or d in spelled.parents for d in shown):
            continue
        binds.append((spelled.resolve(), spelled))
    return tuple(binds)


@dataclass
class Terminal:
    """One command, possibly still running."""

    id: str
    command: str
    started: float
    process: subprocess.Popen[str] | None = None
    chunks: list[str] = field(default_factory=list)
    exit_code: int | None = None
    truncated: int = 0
    _lock: threading.Lock = field(default_factory=threading.Lock)

    @property
    def running(self) -> bool:
        return self.exit_code is None

    def output(self) -> str:
        with self._lock:
            text = "".join(self.chunks)
        if self.truncated:
            head = text[: MAX_CAPTURE // 2]
            tail = text[-MAX_CAPTURE // 2 :]
            return f"{head}\n[... {self.truncated} characters elided ...]\n{tail}"
        return text

    def _append(self, chunk: str) -> None:
        with self._lock:
            self.chunks.append(chunk)
            size = sum(len(c) for c in self.chunks)
            if size > MAX_CAPTURE:
                joined = "".join(self.chunks)
                self.truncated += len(joined) - MAX_CAPTURE
                self.chunks = [joined[: MAX_CAPTURE // 2], joined[-MAX_CAPTURE // 2 :]]


@dataclass
class Sandbox:
    """Command execution rooted at `root`.

    `on_output(terminal_id, chunk)` is called from the reader thread as
    output arrives, so a caller can stream it somewhere. A background
    command outlives the tool call that started it, so its output cannot
    be returned from that call -- it has to be pushed.
    """

    root: Path
    isolation: Literal["bwrap", "none"] = "none"
    terminals: dict[str, Terminal] = field(default_factory=dict)
    on_output: Callable[[str, str], None] | None = None
    env: Mapping[str, str] = field(default_factory=dict)
    """Extra environment for every command, over the scrubbed allowlist."""
    network: Network = "host"
    """"none" gives a command its own empty network namespace (loopback only).
    saddle talks to the model itself, so no command needs the host network to
    do the task; only installs do."""
    expose: tuple[tuple[Path, Path], ...] = ()
    """Read-only `(source, destination)` binds beyond the system dirs and the
    interpreter: the venvs the gate tools live in (`default_expose`)."""
    memory_max: int = memcap.DEFAULT_MEMORY_MAX
    """Hard memory cap in bytes for each command and everything it starts
    (`memcap`); `for_workdir` sets it from `SADDLE_MEMORY_MAX`. There is no
    uncapped setting."""
    mounted: tuple[tuple[Path, Path], ...] = ()
    """Read-only `(source, dest)` binds inside `root` (`bwrap_argv`): an autonomous
    run's worktree sees the checkout's `node_modules` there, as a directory git
    ignores, so the model can run the project's JavaScript tools. A link would be
    a file to git, and land in the run's commit."""
    ledger: ProcessLedger | None = None
    """Where every command's scope is recorded (`procs`), so what it leaves
    running can be listed and stopped. Only a chat session sets it; a task's
    box records nothing."""
    tmp: Path | None = None
    """A private directory every command sees as `/tmp`, so what one command
    leaves there the next can read; None gives each command an empty `/tmp`.
    An autonomous run keeps its scratch there, outside the audited worktree:
    a watched run lost its own fix when a copy it saved in `/tmp` was gone
    by the next command."""

    @classmethod
    def for_workdir(
        cls,
        root: Path,
        *,
        prefer_bwrap: bool = True,
        on_output: Callable[[str, str], None] | None = None,
        env: Mapping[str, str] | None = None,
        require_isolation: bool = False,
        network: Network = "host",
        tmp: Path | None = None,
        unsandboxed: bool = False,
        mounted: Sequence[tuple[Path, Path]] = (),
        ledger: ProcessLedger | None = None,
    ) -> Sandbox:
        """A sandbox for `root`; with `require_isolation`, never an unisolated one.

        `unsandboxed` is a person's deliberate choice (a session's full access):
        commands run as them, with no isolation. A lane that requires isolation
        can never be given it."""
        if unsandboxed and require_isolation:
            msg = "this lane requires isolation, so it cannot run unsandboxed"
            raise ValueError(msg)
        problem = isolation_problem() if prefer_bwrap else "bwrap is not installed"
        if problem is not None and require_isolation:
            msg = f"this lane needs isolation and bwrap cannot provide it: {problem}"
            raise IsolationUnavailableError(msg)
        return cls(
            root=root.resolve(),
            isolation="bwrap" if problem is None and not unsandboxed else "none",
            on_output=on_output,
            env=dict(env or {}),
            network=network,
            expose=default_expose(command_env(env or {})),
            memory_max=memcap.memory_max(),
            tmp=tmp,
            mounted=tuple(mounted),
            ledger=ledger,
        )

    def _argv(self, command: str) -> list[str]:
        """The argv actually executed, wrapped for isolation when available."""
        if self.isolation != "bwrap":
            return ["bash", "-lc", command]
        return bwrap_argv(
            self.root,
            ["bash", "-lc", command],
            network=self.network,
            expose=self.expose,
            tmp=self.tmp,
            mounted=self.mounted,
        )

    def run(self, command: str, *, timeout: int = DEFAULT_TIMEOUT) -> Terminal:
        """Run to completion (or timeout) and return the finished terminal."""
        terminal = self.start(command)
        self.wait(terminal.id, timeout=timeout)
        return terminal

    def start(self, command: str) -> Terminal:
        """Start a command in the background; returns immediately."""
        terminal = Terminal(id=uuid.uuid4().hex[:8], command=command, started=monotonic())
        cap = memcap.cap(self.memory_max)
        argv, env = cap.wrap(self._argv(command), command_env(self.env))
        try:
            process = subprocess.Popen(
                argv,
                cwd=str(self.root),
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                bufsize=1,
                env=env,
                start_new_session=True,
            )
        except OSError as exc:
            terminal.exit_code = 127
            terminal._append(f"error: could not start command: {exc}")
            self.terminals[terminal.id] = terminal
            return terminal
        terminal.process = process
        if self.ledger is not None:
            self.ledger.record(
                Tracked(
                    unit=cap.unit,
                    terminal=terminal.id,
                    command=command,
                    started=wall_time(),
                    pgid=process.pid,
                    process=process,
                )
            )

        def pump() -> None:
            assert process.stdout is not None
            for line in process.stdout:
                terminal._append(line)
                if self.on_output is not None:
                    try:
                        self.on_output(terminal.id, line)
                    except Exception:  # a broken listener must not stop the command
                        self.on_output = None
            code = process.wait()
            _kill_group(process.pid)  # nothing it started outlives it
            if cap.oom_killed():
                terminal._append(f"\n{cap.reason()}\n")
            terminal.exit_code = code

        threading.Thread(target=pump, daemon=True).start()
        self.terminals[terminal.id] = terminal
        return terminal

    def wait(self, terminal_id: str, *, timeout: float = DEFAULT_TIMEOUT) -> Terminal:
        """Block until the terminal finishes or `timeout` elapses.

        A timeout is not a kill: the command keeps running and can be waited
        on again. That is what makes "start it, tell the user, come back"
        work instead of forcing every long job to finish inside one turn.
        """
        terminal = self.terminals[terminal_id]
        deadline = monotonic() + timeout
        while terminal.running and monotonic() < deadline:
            if terminal.process is not None:
                try:
                    terminal.process.wait(timeout=min(0.2, max(0.0, deadline - monotonic())))
                except subprocess.TimeoutExpired:
                    continue
        return terminal

    def kill(self, terminal_id: str) -> Terminal:
        terminal = self.terminals[terminal_id]
        if terminal.process is not None and terminal.running:
            _kill_group(terminal.process.pid)
            terminal.process.wait(timeout=5)
        return terminal


def _kill_group(pgid: int) -> None:
    """SIGKILL a command's whole process group (its own session: pgid == pid).

    Killing the shell alone leaves its children running; without bwrap's PID
    namespace nothing else reaps them. A child that calls setsid itself
    escapes this, which is one reason unisolated runs are labelled "none"."""
    try:
        os.killpg(pgid, signal.SIGKILL)
    except (ProcessLookupError, PermissionError):
        pass
