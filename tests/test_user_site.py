"""The sandbox shows a system Python's user site-packages, read-only, and
nothing else of HOME (#113; README, "What is confined").

`pip install --user` is how pytest reaches a system `python3` on Debian and
Ubuntu. With HOME hidden, the gates ran a different environment from the
user's own `python3 -m pytest` and refused correct work. HOME here is a
temp dir planted with that site-packages and with things that must stay
hidden beside it.
"""

from __future__ import annotations

import os
import re
import subprocess
import sys
from pathlib import Path

import pytest

from saddle import evidence, sandbox
from saddle.sandbox import Sandbox

SYSTEM = f"/usr/bin{os.pathsep}/bin"
SYSTEM_PYTHON = Path("/usr/bin/python3").resolve()


@pytest.fixture
def home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A HOME with a user site holding `userpkg`, and neighbours to hide."""
    if sandbox.isolation_problem() is not None:
        pytest.skip("bwrap cannot start here")
    if re.fullmatch(r"python\d+\.\d+", SYSTEM_PYTHON.name) is None:
        pytest.skip("/usr/bin/python3 is not a pythonX.Y")
    root = tmp_path / "home"
    lib = root / ".local" / "lib" / SYSTEM_PYTHON.name
    (lib / "site-packages" / "userpkg").mkdir(parents=True)
    (lib / "site-packages" / "userpkg" / "__init__.py").write_text("WHERE = 'user site'\n")
    (lib / "beside-site.txt").write_text("hidden\n")
    (root / ".local" / "share").mkdir()
    (root / ".local" / "share" / "state.txt").write_text("hidden\n")
    (root / ".local" / "bin").mkdir()
    (root / ".ssh").mkdir()
    (root / ".ssh" / "id_dummy").write_text("not a real key\n")
    (root / "notes.txt").write_text("hidden\n")
    monkeypatch.setenv("HOME", str(root))
    monkeypatch.setenv("PATH", SYSTEM)
    monkeypatch.delenv("VIRTUAL_ENV", raising=False)
    return root


PROBE = (
    "python3 -c 'import userpkg; print(userpkg.WHERE)'; "
    'find "$HOME" -type f | sort; '
    'cat "$HOME/notes.txt" "$HOME/.ssh/id_dummy" 2>&1 | head -2; '
    f'touch "$HOME/.local/lib/{SYSTEM_PYTHON.name}/site-packages/written" 2>&1 | tail -1'
)


def test_a_command_sees_the_user_site_and_nothing_else_of_home(home: Path, tmp_path: Path) -> None:
    """Red before: `import userpkg` failed (HOME empty). The rest of HOME is
    as hidden as it was: the only file under it is the site-packages' own,
    the planted neighbours (a key, notes, `~/.local/share`, a file beside
    site-packages) are absent, and the site-packages is read-only."""
    work = tmp_path / "work"
    work.mkdir()
    box = Sandbox.for_workdir(work, network="none", require_isolation=True)
    out = box.run(PROBE, timeout=60).output().splitlines()
    lib = home / ".local" / "lib" / SYSTEM_PYTHON.name
    site = lib / "site-packages"
    assert out[0] == "user site"
    listed = [line for line in out[1:] if line.startswith(str(home))]
    assert listed == [str(site / "userpkg" / "__init__.py")]
    assert "No such file or directory" in out[-3]
    assert "No such file or directory" in out[-2]
    assert "Read-only file system" in out[-1]
    assert not (site / "written").exists()


def test_a_gate_run_imports_from_the_user_site(home: Path, tmp_path: Path) -> None:
    work = tmp_path / "work"
    work.mkdir()
    run = evidence.run_capture(
        ["python3", "-c", "import userpkg; print(userpkg.WHERE)"],
        work,
        memory_limit=evidence.tree_memory_limit(),
    )
    assert (run.exit_code, run.stdout.strip()) == (0, "user site"), run.stderr


def test_a_venvs_python_is_not_given_the_user_site(home: Path, tmp_path: Path) -> None:
    """Known-bad for "always": a venv ignores the user site, so showing it
    to one would widen what a run sees for nothing."""
    venv = tmp_path / "v"
    subprocess.run([sys.executable, "-m", "venv", "--without-pip", str(venv)], check=True)
    env = {"PATH": f"{venv / 'bin'}{os.pathsep}{SYSTEM}"}
    shown = [dest for _, dest in sandbox.default_expose(env)]
    site = home / ".local" / "lib" / SYSTEM_PYTHON.name / "site-packages"
    assert site not in shown
    assert site in [dest for _, dest in sandbox.default_expose({"PATH": SYSTEM})]


def test_user_site_needs_a_versioned_name_and_an_existing_dir(home: Path) -> None:
    assert sandbox.user_site(Path("/usr/bin/python3")) is None
    assert sandbox.user_site(Path("/usr/bin/python3.99")) is None
    assert sandbox.user_site(SYSTEM_PYTHON) == (
        home / ".local" / "lib" / SYSTEM_PYTHON.name / "site-packages"
    )
