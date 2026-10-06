"""What a user installs: the sdist and the wheel, built from this tree.

The rest of the suite runs inside the development environment, where every
tool is on PATH and every file is on disk, so it cannot see a release that
leaves a file or a dependency behind. These tests build the distributions
and look at them from the outside.
"""

from __future__ import annotations

import importlib.metadata
import os
import re
import shutil
import subprocess
import sys
import tarfile
import tomllib
from pathlib import Path

import pytest
from uv_offline import run_uv

from saddle.audit import SURFACE_TOOLS

REPO = Path(__file__).resolve().parents[1]

pytestmark = pytest.mark.packaging


def _uv() -> str:
    found = shutil.which("uv")
    if found is None:
        pytest.skip("uv is not on PATH; the packaging tests build with it")
    return found


def _tracked() -> set[str]:
    if not (REPO / ".git").exists():
        pytest.skip("not a git checkout: nothing to compare the sdist with")
    listed = subprocess.run(
        ["git", "ls-files", "-z"], cwd=REPO, capture_output=True, text=True, check=True
    )
    return {name for name in listed.stdout.split("\0") if name}


def test_the_sdist_carries_every_tracked_file(tmp_path: Path) -> None:
    """Contract: the sdist holds every file git tracks. A tracked fixture a
    test reads, dropped by an ignore pattern, fails that test from the sdist."""
    tracked = _tracked()
    _uv_run([_uv(), "build", "--sdist", "--offline", "--out-dir", str(tmp_path), str(REPO)])
    (sdist,) = tmp_path.glob("*.tar.gz")
    with tarfile.open(sdist) as archive:
        members = {m.name.partition("/")[2] for m in archive.getmembers() if m.isfile()}
    assert sorted(tracked - members) == []


def _declared() -> set[str]:
    """The distribution names `[project].dependencies` declares, lowercased."""
    project = tomllib.loads((REPO / "pyproject.toml").read_text())["project"]
    return {
        re.split(r"[<>=!~;\[ ]", spec, maxsplit=1)[0].lower() for spec in project["dependencies"]
    }


def test_every_gate_tool_is_a_runtime_dependency() -> None:
    """Contract: an install with only the declared dependencies has every tool
    the gates run or `gate_surface` reads. A tool left in the dev group is
    absent from a `uv tool install`, and the audit cannot run."""
    gate_tools = {*SURFACE_TOOLS, "pytest"}
    assert sorted(gate_tools - _declared()) == []


# -- the installed wheel audits, with its bin/ absent from PATH ---------------
#
# A user who runs `uv tool install saddle-harness` or `pipx install` gets a
# venv whose bin/ is not on PATH; only the `saddle` script is linked onto it.
# These tests build that shape from this tree: the wheel alone, its declared
# dependencies alone (no dev group), from the uv cache (`run_uv`), and a PATH
# made of the system directories alone (`system_path`).

GATE_NAMES = frozenset({"python", "pytest", "coverage", "ruff", "mutmut"})

BASE_CALC = "def add(a, b):\n    return a + b\n"
BASE_TEST = "from calc import add\n\n\ndef test_add():\n    assert add(1, 2) == 3\n"
SUB = "\n\ndef sub(a, b):\n    return {body}\n"
SUB_TEST = (
    "\n\ndef test_sub():\n    from calc import sub\n\n"
    "    assert sub(3, 1) == 2\n    assert sub(1, 3) == -2\n"
)
PROPERTY_TEST = (
    "from hypothesis import given\nfrom hypothesis import strategies as st\n\n"
    "from calc import sub\n\n\n"
    "@given(st.integers(), st.integers())\n"
    "def test_sub_undoes_add(a, b):\n    assert sub(a + b, b) == a\n"
)


def _run(argv: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
    done: subprocess.CompletedProcess[str] = subprocess.run(  # type: ignore[call-overload]
        argv, capture_output=True, text=True, check=True, **kwargs
    )
    return done


def _uv_run(argv: list[str]) -> None:
    """`argv`, a uv command, from uv's cache or online when the cache falls short
    (`run_uv`); a failure shows uv's own words."""
    done = run_uv(argv)
    assert done.returncode == 0, f"{' '.join(argv)}\n{done.stderr}"


def _venv(uv: str, where: Path, *requirements: str) -> Path:
    """A fresh venv at `where` with `requirements` installed offline; its bin/."""
    _uv_run([uv, "venv", "--offline", "--quiet", "--python", sys.executable, str(where)])
    python = where / "bin" / "python"
    _uv_run([uv, "pip", "install", "--offline", "--quiet", "--python", str(python), *requirements])
    return where / "bin"


@pytest.fixture(scope="module")
def installed(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """bin/ of a clean venv holding the wheel built from this tree."""
    uv = _uv()
    out = tmp_path_factory.mktemp("wheel")
    _uv_run([uv, "build", "--wheel", "--offline", "--out-dir", str(out), str(REPO)])
    (wheel,) = out.glob("*.whl")
    return _venv(uv, tmp_path_factory.mktemp("installed") / "venv", str(wheel))


@pytest.fixture(scope="module")
def system_path() -> str:
    """The system directories alone, as a `uv tool` user's PATH reaches them.

    A host whose /usr/bin carries a gate tool (a `python` from
    python-is-python3, say) would have that copy win over saddle's, by
    design: the user's PATH comes first. Such a host cannot show this
    shape, so the tests skip there and say why. A directory of links that
    hid those names does not work instead: inside the sandbox only the
    system directories exist, so the links would lead nowhere."""
    path = os.pathsep.join(["/usr/bin", "/bin"])
    present = sorted(name for name in GATE_NAMES if shutil.which(name, path=path))
    if present:
        pytest.skip(f"this host's system PATH has gate tools: {', '.join(present)}")
    return path


def _tree(root: Path, body: str, extra: dict[str, str] | None = None) -> Path:
    """A repo whose baseline has `add`; the working tree adds `sub` with `body` and tests."""
    root.mkdir()
    (root / "calc.py").write_text(BASE_CALC)
    (root / "test_calc.py").write_text(BASE_TEST)
    ident = ["-c", "user.name=t", "-c", "user.email=t@example.invalid"]
    _run(["git", "init", "-q"], cwd=root)
    _run(["git", "add", "-A"], cwd=root)
    _run(["git", *ident, "commit", "-q", "-m", "base"], cwd=root)
    (root / "calc.py").write_text(BASE_CALC + SUB.format(body=body))
    (root / "test_calc.py").write_text(BASE_TEST + SUB_TEST)
    for name, text in (extra or {}).items():
        (root / name).write_text(text)
    return root


def _audit(saddle_bin: Path, tree: Path, path: str) -> subprocess.CompletedProcess[str]:
    """`saddle audit --tiered --no-cache` as the installed script, under `path`."""
    env = {
        name: value
        for name, value in os.environ.items()
        if not name.startswith(("VIRTUAL_ENV", "PYTHON", "COV_", "UV_"))
    }
    env["PATH"] = path
    assert shutil.which("saddle", path=path) is None
    return subprocess.run(
        [str(saddle_bin / "saddle"), "audit", "--repo", str(tree), "--tiered", "--no-cache"],
        capture_output=True,
        text=True,
        cwd=tree,
        env=env,
        check=False,
    )


def _verdict(done: subprocess.CompletedProcess[str]) -> str:
    lines = [line for line in done.stdout.splitlines() if line.startswith("verdict: ")]
    assert lines, f"exit {done.returncode}\n{done.stdout}\n{done.stderr}"
    return lines[-1]


def test_the_installed_wheel_accepts_a_correct_change(
    installed: Path, system_path: str, tmp_path: Path
) -> None:
    """Known-good. Red at 49502a7: PackageNotFoundError for mutmut, then,
    with the tools installed but not on PATH, `verdict: refuse`."""
    done = _audit(installed, _tree(tmp_path / "good", "a - b"), system_path)
    assert (_verdict(done), done.returncode) == ("verdict: accept", 0), done.stdout


def test_the_installed_wheel_refuses_a_wrong_change(
    installed: Path, system_path: str, tmp_path: Path
) -> None:
    """Known-bad: `sub` adds, its test fails."""
    done = _audit(installed, _tree(tmp_path / "bad", "a + b"), system_path)
    assert (_verdict(done), done.returncode) == ("verdict: refuse", 1), done.stdout
    # Refused because the test ran and failed, not because a tool was missing.
    tests = [line for line in done.stdout.splitlines() if line.split()[:2] == ["fail", "tests"]]
    assert tests, done.stdout
    # The line may go on to name the failing tests (e786922).
    assert all("'python -m pytest -q' exited 1" in line for line in tests), tests


def test_the_projects_own_environment_on_path_runs_its_tests(
    installed: Path, system_path: str, tmp_path_factory: pytest.TempPathFactory
) -> None:
    """Known-good: the project's tests import hypothesis, which saddle's venv
    lacks and the project's environment, first on PATH, has. The gates run
    the project's python, so the change is accepted."""
    project = _venv(
        _uv(),
        tmp_path_factory.mktemp("project") / "venv",
        *(
            f"{name}=={importlib.metadata.version(name)}"
            for name in ("pytest", "coverage", "mutmut", "hypothesis")
        ),
    )
    with pytest.raises(subprocess.CalledProcessError):
        _run([str(installed / "python"), "-c", "import hypothesis"])
    tree = _tree(tmp_path_factory.mktemp("tree") / "prop", "a - b", {"test_prop.py": PROPERTY_TEST})
    done = _audit(installed, tree, f"{project}{os.pathsep}{system_path}")
    assert (_verdict(done), done.returncode) == ("verdict: accept", 0), done.stdout
