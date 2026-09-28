"""Approved installs for a Task run: a throwaway overlay on the project venv.

A task can need a package the project's virtualenv lacks. The model may
ask for one with the `install` tool (offered only with `--allow-installs`);
the user approves or refuses each request through the run's own question
seam (`engine._ask`, the one the budget and test-edit questions use). A
run nobody can answer (`saddle auto` in a terminal) takes the default,
Refuse, sealed as unanswered.

What an approved install may touch, and what it may not:

- **Only a local wheel folder.** The user fills it; saddle never downloads.
  pip runs with `--no-index --find-links <folder> --only-binary=:all:`, so
  no index is read and no sdist is built (building runs a package's own
  code). It runs inside bwrap with no network namespace but its own, so an
  attempt to reach the network fails rather than being trusted to a flag.
- **Only the run's overlay.** The overlay is a venv under the run's own
  state (`.saddle/runs/<run-id>/overlay`), made by the project's own
  interpreter, whose `.pth` adds the project venv's site-packages with
  `site.addsitedir` (so the project's own `.pth` files, an editable install
  of the project included, still apply). The project venv is shown to the
  installer read-only and is never written; the overlay is the one
  writable place. It is removed when the run ends.

Why not `--system-site-packages`: made from a venv's interpreter, that
flag layers on the *base* interpreter's site-packages, not on the project
venv's, so the project's packages would vanish.

After the first approved install, the run uses the overlay as its project
environment: the gates (`sandbox.using_project_env`), the model's commands
(`sandbox.project_command_env`), and what the sandbox shows read-only
(`sandbox.default_expose`, which follows the overlay's `saddle-layers-on`
key to the project venv). The auditor's cache key carries the environment's
contents (`sandbox.environment_key`), so a verdict from before an install
is not reused after it.

A requested package the folder has no wheel for is reported as missing
before anything is asked, and every requested name is looked up in the
overlay after pip exits: one that is not there is reported missing, never
installed.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import subprocess
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Final

from saddle import sandbox

DEFAULT_WHEEL_DIR: Final = "~/.local/share/saddle/wheels"
WHEEL_DIR_ENV: Final = "SADDLE_WHEEL_DIR"
"""Where approved installs come from: `--wheel-dir`, else this variable
(exported, or a line in the key file), else `DEFAULT_WHEEL_DIR`
(`cli.resolve_setting`)."""

INSTALL: Final = "Install"
REFUSE_INSTALL: Final = "Refuse"
"""The install question's options; the second is the default."""

INSTALL_REFUSED: Final = "error: install refused: "
"""Prefix of an install call's result when nothing was tried: a bad request,
a missing wheel, a missing or empty folder, or the user said no."""

INSTALL_FAILED: Final = "error: install failed: "
"""Prefix when pip ran but a requested package is not there afterwards, or
pip failed; the text says what, if anything, did land in the overlay."""

INSTALL_TIMEOUT_S: Final = 300.0
"""A wheel install from a local folder takes seconds; this bounds a hung one."""

_REQUIREMENT: Final = re.compile(
    r"(?P<name>[A-Za-z0-9](?:[A-Za-z0-9._-]*[A-Za-z0-9])?)"
    r"(?:(?:===|==|!=|~=|<=|>=|<|>)[A-Za-z0-9.*+!_-]+"
    r"(?:,(?:===|==|!=|~=|<=|>=|<|>)[A-Za-z0-9.*+!_-]+)*)?"
)
"""A requirement the tool accepts, whole (`re.fullmatch`): a distribution
name and an optional version specifier. No extras, markers, URLs, paths or
options: nothing that could name a source other than the wheel folder."""

_PIP_PROBE: Final = (
    "import ensurepip, importlib.util, json, pathlib\n"
    "wheel = ''\n"
    "try:\n"
    "    pkg = ensurepip._get_packages()['pip']\n"
    "    wheel = pkg.wheel_path or str(pathlib.Path(ensurepip.__file__).parent"
    " / '_bundled' / pkg.wheel_name)\n"
    "except Exception:\n"
    "    pass\n"
    "print(json.dumps({'bundled': wheel if wheel and pathlib.Path(wheel).is_file() else '',"
    " 'pip': importlib.util.find_spec('pip') is not None}))\n"
)
"""Where the overlay's interpreter can get pip from, without a network: the
wheel its standard library bundles for `ensurepip`, else a pip the project
venv already has."""

_LOOKUP: Final = (
    "import importlib.metadata as m, json, sys\n"
    "out = {}\n"
    "for name in sys.argv[1:]:\n"
    "    try:\n"
    "        d = m.distribution(name)\n"
    "        out[name] = [d.version, str(d.locate_file(''))]\n"
    "    except m.PackageNotFoundError:\n"
    "        out[name] = None\n"
    "print(json.dumps(out))\n"
)
"""Each requested name's installed version and where it lives, or None."""


@dataclass(frozen=True)
class WheelFolder:
    """The configured wheel folder and where the setting came from."""

    path: Path
    source: str

    def problem(self) -> str | None:
        """None when the folder holds at least one wheel, else why not, naming it."""
        where = f"the wheel folder {self.path} ({self.source})"
        if not self.path.exists():
            return f"{where} does not exist; create it and put the wheels to install there"
        if not self.path.is_dir():
            return f"{where} is not a directory"
        if not self.wheels():
            return f"{where} holds no .whl files; put the wheels to install there"
        return None

    def wheels(self) -> list[Path]:
        return sorted(p for p in self.path.iterdir() if p.suffix == ".whl" and p.is_file())


def normalize(name: str) -> str:
    """A distribution name as wheel filenames spell it (PEP 503, then `_`)."""
    return re.sub(r"[-_.]+", "_", name).lower()


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


@dataclass(frozen=True)
class Plan:
    """One install request that passed every check but the user's approval."""

    requirements: tuple[str, ...]
    names: tuple[str, ...]
    wheels: tuple[Path, ...]
    reason: str

    def question(self, folder: WheelFolder, overlay: Path, project: Path) -> str:
        files = ", ".join(w.name for w in self.wheels)
        why = f" Its reason: {self.reason}" if self.reason else ""
        return (
            f"The model asks to install {', '.join(self.requirements)} from the wheel "
            f"folder {folder.path} ({files}) into this run's own environment {overlay}, "
            f"which layers on {project}. The project's virtualenv is not changed, "
            f"and the environment is removed when the run ends.{why} Install?"
        )


@dataclass
class Installs:
    """A run's install seam: `plan` checks a request, `install` carries it out.

    `on_ready` is called with the overlay after an install put something in
    it, so the run switches its gates and commands to it."""

    folder: WheelFolder
    project: Path
    overlay: Path
    on_ready: Callable[[Path], None] = lambda _overlay: None
    installed: list[str] = field(default_factory=list)
    """`name==version` of everything installed into the overlay, in order."""
    asked: int = 0
    _pip_wheel: Path | None = None
    """The bundled pip wheel `_pip` found, shown read-only to the installer."""

    def plan(self, arguments: str) -> Plan | str:
        """The request in `arguments`, checked; or the refusal text."""
        try:
            parsed = json.loads(arguments)
        except ValueError:
            parsed = None
        packages = parsed.get("packages") if isinstance(parsed, dict) else None
        if (
            not isinstance(packages, list)
            or not packages
            or not all(isinstance(p, str) for p in packages)
        ):
            return f"{INSTALL_REFUSED}give `packages`, a non-empty list of requirement strings"
        reason = parsed.get("reason", "") if isinstance(parsed, dict) else ""
        bad = [p for p in packages if _REQUIREMENT.fullmatch(p.strip()) is None]
        if bad:
            return (
                f"{INSTALL_REFUSED}not a plain requirement (a name and an optional version "
                f"such as ==1.2): {', '.join(bad)}"
            )
        problem = self.folder.problem()
        if problem is not None:
            return f"{INSTALL_REFUSED}{problem}. Nothing was installed."
        requirements = tuple(p.strip() for p in packages)
        names = tuple(_name(r) for r in requirements)
        wheels = self.folder.wheels()
        missing = [n for n in names if not any(_wheel_name(w) == normalize(n) for w in wheels)]
        if missing:
            return (
                f"{INSTALL_REFUSED}missing from the wheel folder {self.folder.path}: "
                f"{', '.join(missing)}. Nothing was installed. Only the user can add a "
                "wheel there."
            )
        matched = tuple(w for w in wheels if _wheel_name(w) in {normalize(n) for n in names})
        return Plan(requirements, names, matched, reason if isinstance(reason, str) else "")

    def question(self, plan: Plan) -> tuple[str, str]:
        """The question's id and text for `plan`; each request is asked anew."""
        self.asked += 1
        return f"install-{self.asked}", plan.question(self.folder, self.overlay, self.project)

    def install(self, plan: Plan) -> str:
        """Install an approved `plan` into the overlay; say what is where now."""
        try:
            self._ensure_overlay()
            pip = self._pip()
        except InstallError as exc:
            return f"{INSTALL_REFUSED}{exc}. Nothing was installed."
        argv = [
            *pip,
            "--isolated",
            "--disable-pip-version-check",
            "--no-cache-dir",
            "install",
            "--no-input",
            "--no-index",
            "--find-links",
            str(self.folder.path),
            "--only-binary=:all:",
            *plan.requirements,
        ]
        done = self._run(argv)
        found = self._lookup(plan.names)
        if isinstance(found, str):
            return (
                f"{INSTALL_FAILED}pip exited {done.returncode}, and looking up what is "
                f"installed failed ({found}), so nothing is reported installed and the "
                "run's environment is unchanged."
            )
        site = self.overlay.resolve()
        into = {n: v for n, (v, where) in found.items() if site in where.parents}
        already = {n: (v, where) for n, (v, where) in found.items() if n not in into}
        missing = [n for n in plan.names if n not in found]
        lines: list[str] = []
        if done.returncode != 0:
            tail = "\n".join(done.stdout.strip().splitlines()[-15:])
            lines.append(f"pip exited {done.returncode}:\n{tail}")
        if into:
            self.installed.extend(f"{n}=={v}" for n, v in into.items())
            self.on_ready(self.overlay)
            shas = "; ".join(f"{w.name} sha256 {_sha256(w)}" for w in plan.wheels)
            lines.append(
                "installed into this run's environment (the project's virtualenv is "
                f"unchanged): {', '.join(f'{n} {v}' for n, v in into.items())}. "
                f"The tests and your commands now use it. Wheels: {shas}"
            )
        if already:
            lines.append(
                "already there before this run, not installed: "
                + ", ".join(f"{n} {v} (in {where})" for n, (v, where) in already.items())
            )
        if missing:
            lines.append(f"missing, not installed: {', '.join(missing)}")
        text = "\n".join(lines)
        return text if done.returncode == 0 and not missing else f"{INSTALL_FAILED}{text}"

    # -- the overlay -----------------------------------------------------------

    def _ensure_overlay(self) -> None:
        """Make the overlay once: a venv of the project's interpreter over it."""
        if (self.overlay / "pyvenv.cfg").is_file():
            return
        problem = sandbox.isolation_problem()
        if problem is not None:
            msg = f"installs need bwrap, and it cannot start here: {problem}"
            raise InstallError(msg)
        python = self.project / "bin" / "python"
        self.overlay.mkdir(parents=True, exist_ok=True)
        made = self._run(
            [str(python), "-m", "venv", "--without-pip", str(self.overlay)], use=self.project
        )
        if made.returncode != 0:
            msg = f"could not make the run's environment: {made.stdout.strip()}"
            raise InstallError(msg)
        found = self._run(
            [
                str(python),
                "-c",
                "import sysconfig; print(sysconfig.get_path('purelib'))",
            ],
            use=self.project,
        ).stdout.strip()
        own = self._run(
            [
                str(self.overlay / "bin" / "python"),
                "-c",
                "import sysconfig; print(sysconfig.get_path('purelib'))",
            ]
        ).stdout.strip()
        (Path(own) / "_saddle_layers_on.pth").write_text(
            f"import site; site.addsitedir({found!r})\n", encoding="utf-8"
        )
        with (self.overlay / "pyvenv.cfg").open("a", encoding="utf-8") as cfg:
            cfg.write(f"{sandbox.LAYERS_ON} = {self.project}\n")
        self._copy_scripts()

    def _copy_scripts(self) -> None:
        """The project's console scripts, re-pointed at the overlay's python.

        The gates run `pytest`, `coverage` and `mutmut` by name from the
        environment's `bin`; the project's copies would start the project's
        interpreter, which never sees the overlay."""
        old = str(self.project / "bin") + "/"
        new = str(self.overlay / "bin") + "/"
        for script in sorted((self.project / "bin").iterdir()):
            target = self.overlay / "bin" / script.name
            if target.exists() or target.is_symlink() or script.is_symlink():
                continue  # the overlay's own (python, activate) or a link
            head = script.read_bytes() if script.is_file() else b""
            if not head.startswith(b"#!"):
                continue  # not a script: a binary, a directory, Activate.ps1
            target.write_bytes(head.replace(old.encode(), new.encode()))
            target.chmod(script.stat().st_mode)

    def _pip(self) -> list[str]:
        """The argv that runs pip on the overlay's interpreter."""
        probe = self._run([str(self.overlay / "bin" / "python"), "-c", _PIP_PROBE])
        try:
            found = json.loads(probe.stdout.strip().splitlines()[-1])
        except (ValueError, IndexError):
            said = probe.stdout.strip() or f"exit {probe.returncode}, no output"
            msg = f"looking for a pip to install with failed ({said})"
            raise InstallError(msg) from None
        python = str(self.overlay / "bin" / "python")
        if found["bundled"]:
            self._pip_wheel = Path(found["bundled"])
            return [python, f"{found['bundled']}/pip"]
        if found["pip"]:
            return [python, "-m", "pip"]
        msg = (
            "no pip to install with: the project's interpreter bundles no pip wheel and "
            f"the project's virtualenv {self.project} has no pip"
        )
        raise InstallError(msg)

    def _lookup(self, names: Sequence[str]) -> dict[str, tuple[str, Path]] | str:
        """Each of `names` the overlay's interpreter finds: its version and
        the directory it is installed in; a name it does not find is absent.
        A lookup that failed outright is its output, never "nothing found"."""
        done = self._run([str(self.overlay / "bin" / "python"), "-c", _LOOKUP, *names])
        try:
            found = json.loads(done.stdout.strip().splitlines()[-1])
        except (ValueError, IndexError):
            return done.stdout.strip() or f"exit {done.returncode}, no output"
        return {
            n: (str(found[n][0]), Path(found[n][1]).resolve())
            for n in names
            if isinstance(found, dict) and isinstance(found.get(n), list)
        }

    def _run(
        self, argv: Sequence[str], *, use: Path | None = None
    ) -> subprocess.CompletedProcess[str]:
        """`argv` under bwrap: no network, the overlay the only writable place.

        `use` is the venv whose `bin` goes first on PATH (default the
        overlay); the wheel folder and pip's wheel are shown read-only."""
        venv = use or self.overlay
        env = sandbox.command_env(
            {
                "PATH": os.pathsep.join([str(venv / "bin"), os.environ.get("PATH", "")]),
                "VIRTUAL_ENV": str(venv),
                "PYTHONDONTWRITEBYTECODE": "1",
            }
        )
        expose = list(sandbox.default_expose(env))
        expose += [(self.project.resolve(), self.project.resolve())]
        expose += [(self.folder.path.resolve(), self.folder.path.resolve())]
        if self._pip_wheel is not None:
            expose += [(self._pip_wheel, self._pip_wheel)]
        wrapped = sandbox.bwrap_argv(self.overlay.resolve(), argv, network="none", expose=expose)
        try:
            return subprocess.run(
                wrapped,
                cwd=self.overlay,
                env=env,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                timeout=INSTALL_TIMEOUT_S,
                check=False,
            )
        except subprocess.TimeoutExpired:
            said = f"timed out after {INSTALL_TIMEOUT_S:.0f}s"
            return subprocess.CompletedProcess(list(argv), 124, said, "")

    def remove(self) -> None:
        """The run is over: its overlay goes with it."""
        shutil.rmtree(self.overlay, ignore_errors=True)


class InstallError(RuntimeError):
    """The overlay could not be made or has no pip; nothing was installed."""


def _name(requirement: str) -> str:
    match = _REQUIREMENT.fullmatch(requirement)
    assert match is not None
    return match["name"]


def _wheel_name(wheel: Path) -> str:
    """The distribution part of a wheel's filename, normalized."""
    return normalize(wheel.name.split("-", 1)[0])
