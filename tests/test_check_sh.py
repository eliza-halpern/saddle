"""`check.sh` exits non-zero when any stage of it fails, and zero when none does.

The contract, each half both ways: a clean tree passes `check.sh`; a lint
error, a formatting violation, a type error, a failing test and an uncovered
line each make it exit non-zero; and for every other stage the script runs
(the npm install guard, ShellCheck, and whatever is added later), that
stage's tool failing makes it exit non-zero while every one of those stages
runs on a clean tree. A stage joined to `|| true`, placed after an `exit`, or
guarded by a condition that never holds fails one of these.

Running the real `check.sh` on the real tree takes many minutes, so the real
script is copied, unmodified, into a miniature project (one module, one test,
the same ruff, mypy, pytest and coverage settings shape) and run there. What
is replaced, and why that still tests `check.sh`'s own logic: `uv` is a stub
that drops `run` and its options and execs the tool from this virtual
environment, because the script's logic is its `set -e` chain and its stage
order, not uv's resolution, and uv would resolve over the network. Every tool
other than ruff, mypy and pytest is a stub that logs its command line and
exits 0, or exits 1 when `STUB_FAIL` names its stage. Ruff, mypy and pytest
are real, so the three broken trees fail for real reasons, which the tests
read from the output.

The stages are derived by parsing `check.sh`'s command lines
(`command_lines`), so a stage added later is covered by the per-stage test
automatically; a construct the parser does not understand fails
`test_every_check_sh_line_is_understood`, which asks for a decision.
"""

from __future__ import annotations

import os
import re
import shlex
import shutil
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

import pytest
from test_shell_scripts import ROOT, command_lines, needs_checkout

VENV_BIN = Path(sys.executable).parent
CHECK_SH = ROOT / "check.sh"
IN_CHECKOUT = CHECK_SH.is_file()

# Tools the miniature runs for real; every other tool a stage names is stubbed.
REAL_TOOLS = frozenset({"ruff", "mypy", "pytest"})
# Lines that are shell structure or setup, not stages.
STRUCTURE = frozenset({"set", "cd", "fi", "then", "else", "do", "done", "exit", "export", "true"})
CONTROL = frozenset({"for", "while", "until", "case", "function", "select"})
OPERATOR_CHARS = frozenset(";&|()")


@dataclass(frozen=True)
class Stage:
    """One command check.sh runs: the executable on PATH, a word identifying it, its arguments."""

    exe: str
    key: str
    args: tuple[str, ...]
    understood: bool = True

    @property
    def real(self) -> bool:
        python_c = self.exe == "python" and self.args[:1] == ("-c",)
        return self.exe in REAL_TOOLS or python_c


def _words(line: str) -> list[str]:
    lexer = shlex.shlex(line, posix=True, punctuation_chars=True)
    lexer.whitespace_split = True
    lexer.commenters = "#"
    return list(lexer)


def _segments(words: list[str]) -> list[list[str]]:
    segments: list[list[str]] = [[]]
    for word in words:
        if set(word) <= OPERATOR_CHARS:
            segments.append([])
        else:
            segments[-1].append(word)
    return [s for s in segments if s]


def _after_options(words: list[str]) -> list[str]:
    """`words` from the first one that is not an option (the tool a launcher runs)."""
    return next(words[i:] for i, w in enumerate(words) if not w.startswith("-"))


def _stage(words: list[str]) -> Stage | None:
    """The stage a command stands for, or None for shell structure."""
    while words and re.fullmatch(r"\w+=\S*", words[0]):
        words = words[1:]  # environment prefix
    if not words or words[0] in STRUCTURE or words[0] in {"if", "elif", "[", "[["}:
        return None
    first, rest = words[0], words[1:]
    if first in CONTROL:
        return Stage(first, first, tuple(rest), understood=False)
    if first == "uv" and rest[:1] == ["run"]:
        tool = _after_options(rest[1:])
        return Stage(tool[0], tool[0], tuple(tool[1:]))
    if first == "uv":
        return Stage("uv", rest[0], tuple(rest[1:]))
    if first == "npx":
        tool = _after_options(rest)
        return Stage("npx", tool[0], tuple(tool[1:]))
    return Stage(first, first, tuple(rest))


def check_sh_stages(script: str) -> list[Stage]:
    stages: list[Stage] = []
    for line in command_lines(script):
        # `name=$(command)`: the command is the stage.
        assigned = re.fullmatch(r"\w+=\$\((.*)\)", line)
        for segment in _segments(_words(assigned.group(1) if assigned else line)):
            stage = _stage(segment)
            if stage is not None:
                stages.append(stage)
    return stages


def stubbed_stages(script: str) -> list[Stage]:
    seen: dict[str, Stage] = {}
    for stage in check_sh_stages(script):
        if stage.understood and not stage.real:
            seen.setdefault(stage.key, stage)
    return list(seen.values())


REAL_STUBS = stubbed_stages(CHECK_SH.read_text()) if IN_CHECKOUT else []


# -- the miniature project ---------------------------------------------------

PYPROJECT = """\
[project]
name = "mini"
version = "0"
requires-python = ">=3.12"

[tool.ruff]
line-length = 100

[tool.ruff.lint]
select = ["E", "F", "I"]

[tool.mypy]
strict = true
mypy_path = "src"

[tool.pytest.ini_options]
testpaths = ["tests"]
pythonpath = ["src"]
addopts = "--cov=mini --cov-report=term-missing --cov-fail-under=100"

[tool.coverage.run]
source = ["mini"]
branch = true

[tool.saddle]
test-workers = 1
"""
MODULE = "def add(a: int, b: int) -> int:\n    return a + b\n"
TEST = "from mini import add\n\n\ndef test_add() -> None:\n    assert add(1, 2) == 3\n"

# One broken tree per real stage: the file to rewrite, its new text, and what the
# real tool must say, so the exit is non-zero for the stage's own reason.
BROKEN = {
    "a lint error": ("src/mini.py", "import os\n\n\n" + MODULE, "F401"),
    "a formatting violation": (
        "src/mini.py",
        "def add(a: int, b: int) -> int:\n    return a+b\n",
        "would be reformatted",
    ),
    "a type error": (
        "src/mini.py",
        "def add(a: int, b: int) -> int:\n    return str(a)\n",
        "Incompatible return value",
    ),
    "a failing test": (
        "tests/test_mini.py",
        TEST.replace("== 3", "== 4"),
        "1 failed",
    ),
    "an uncovered line": (
        "src/mini.py",
        MODULE + "\n\ndef sub(a: int, b: int) -> int:\n    return a - b\n",
        "Required test coverage of 100% not reached",
    ),
}

STUB = """\
#!/bin/sh
# Logs its command line; fails when STUB_FAIL names one of the words on it.
name=${STUB_NAME:-$(basename "$0")}
line="$name $*"
echo "$line" >> "$STUB_LOG"
for word in $line; do
    [ "$word" = "${STUB_FAIL:-}" ] && exit 1
done
exit 0
"""
UV_STUB = """\
#!/bin/sh
# `uv run [options] TOOL ...` runs TOOL from PATH; any other subcommand is a stage named uv.
if [ "$1" != run ]; then
    STUB_NAME=uv exec "$(dirname "$0")/stage" "$@"
fi
shift
while [ "${1#-}" != "$1" ]; do shift; done
exec "$@"
"""
# `python -c` (check.sh's own reading of pyproject.toml) is the real interpreter;
# any other python command is a stage.
PYTHON_STUB = f"""\
#!/bin/sh
if [ "$1" = "-c" ]; then exec {VENV_BIN}/python "$@"; fi
STUB_NAME=python exec "$(dirname "$0")/stage" "$@"
"""


def write_executable(path: Path, text: str) -> None:
    path.write_text(text)
    path.chmod(0o755)


def build_stubs(directory: Path, script: str) -> None:
    """A stub for every executable `check.sh` names that is not run for real."""
    directory.mkdir()
    write_executable(directory / "stage", STUB)
    write_executable(directory / "uv", UV_STUB)
    for stage in check_sh_stages(script):
        if stage.real or not stage.understood or (directory / stage.exe).exists():
            continue
        if stage.exe == "python":
            write_executable(directory / "python", PYTHON_STUB)
        else:
            shutil.copy(directory / "stage", directory / stage.exe)


def build_project(root: Path, stubs: Path, script: str) -> None:
    """The miniature, with the real `check.sh` beside it, and the stubs outside it."""
    (root / "src").mkdir(parents=True)
    (root / "tests").mkdir()
    (root / "pyproject.toml").write_text(PYPROJECT)
    (root / "src" / "mini.py").write_text(MODULE)
    (root / "tests" / "test_mini.py").write_text(TEST)
    shutil.copy(CHECK_SH, root / "check.sh")
    build_stubs(stubs, script)


def run_check(
    root: Path, stubs: Path, log: Path, *, fail: str = ""
) -> tuple[subprocess.CompletedProcess[str], list[str]]:
    """The copied check.sh in a closed environment (no coverage or pytest state inherited)."""
    log.write_text("")
    done = subprocess.run(
        ["bash", str(root / "check.sh")],
        cwd=root,
        capture_output=True,
        text=True,
        check=False,
        env={
            "PATH": f"{stubs}:{VENV_BIN}:{os.environ['PATH']}",
            "HOME": str(root.parent),
            "LANG": "C.UTF-8",
            "NO_COLOR": "1",
            "STUB_LOG": str(log),
            "STUB_FAIL": fail,
        },
        timeout=120,
    )
    return done, log.read_text().splitlines()


@pytest.fixture(scope="module")
def template(tmp_path_factory: pytest.TempPathFactory) -> tuple[Path, Path, Path]:
    """A miniature already checked once, so each variant copies warm tool caches."""
    base = tmp_path_factory.mktemp("mini")
    root, stubs, log = base / "project", base / "stubs", base / "stub.log"
    build_project(root, stubs, CHECK_SH.read_text())
    done, _ = run_check(root, stubs, log)
    assert done.returncode == 0, done.stdout + done.stderr
    return root, stubs, log


def variant(template: tuple[Path, Path, Path], tmp_path: Path) -> tuple[Path, Path, Path]:
    root, stubs, _ = template
    copy = tmp_path / "project"
    shutil.copytree(root, copy, ignore=shutil.ignore_patterns(".coverage*", "__pycache__"))
    return copy, stubs, tmp_path / "stub.log"


# -- the tests ---------------------------------------------------------------


@needs_checkout
def test_every_check_sh_line_is_understood() -> None:
    """A construct this file cannot classify needs a decision about how to test it."""
    stages = check_sh_stages(CHECK_SH.read_text())
    unknown = [s for s in stages if not s.understood]
    assert not unknown, f"decide how to test these check.sh constructs: {unknown}"
    exes = {s.exe for s in stages}
    assert REAL_TOOLS <= exes, f"check.sh no longer runs {REAL_TOOLS - exes}"


@needs_checkout
def test_the_stages_the_per_stage_test_runs_over_are_found() -> None:
    """Parametrizing over an empty list would run no per-stage test and say nothing."""
    assert {stage.key for stage in REAL_STUBS} >= {"npm", "shellcheck"}


def test_the_stage_parser_reads_the_shapes_check_sh_uses() -> None:
    script = (
        "set -e\n"
        "if [ ! -f x ]; then\n"
        "    npm ci --ignore-scripts\n"
        "fi\n"
        "uv run ruff check .\n"
        "uv run --frozen mypy src \\\n    tests # note\n"
        "w=$(uv run python -c 'print(1)')\n"
        "npx --no-install eslint a.js || true\n"
        "uv lock --check\n"
        "FOO=1 uv run python tools/x.py && exit 0\n"
    )
    got = [(s.exe, s.key, s.real, s.understood) for s in check_sh_stages(script)]
    assert got == [
        ("npm", "npm", False, True),
        ("ruff", "ruff", True, True),
        ("mypy", "mypy", True, True),
        ("python", "python", True, True),
        ("npx", "eslint", False, True),
        ("uv", "lock", False, True),
        ("python", "python", False, True),
    ]
    assert [s.key for s in check_sh_stages("for f in a b; do cat $f; done\n")] == ["for"]
    assert [s.understood for s in check_sh_stages("for f in a; do\n")] == [False]


@needs_checkout
def test_a_clean_tree_passes_and_every_stubbed_stage_runs(
    template: tuple[Path, Path, Path], tmp_path: Path
) -> None:
    root, stubs, log = variant(template, tmp_path)
    done, calls = run_check(root, stubs, log)
    assert done.returncode == 0, done.stdout + done.stderr
    assert "1 passed" in done.stdout
    for stage in REAL_STUBS:
        assert any(stage.key in call.split() for call in calls), (stage.key, calls)


@needs_checkout
@pytest.mark.parametrize("what", sorted(BROKEN))
def test_a_broken_tree_makes_check_sh_exit_non_zero(
    template: tuple[Path, Path, Path], tmp_path: Path, what: str
) -> None:
    root, stubs, log = variant(template, tmp_path)
    name, text, expected = BROKEN[what]
    (root / name).write_text(text)
    done, _ = run_check(root, stubs, log)
    assert done.returncode != 0, done.stdout
    assert expected in done.stdout + done.stderr


@needs_checkout
@pytest.mark.parametrize("stage", REAL_STUBS, ids=[s.key for s in REAL_STUBS])
def test_a_failing_stage_makes_check_sh_exit_non_zero(
    template: tuple[Path, Path, Path], tmp_path: Path, stage: Stage
) -> None:
    root, stubs, log = variant(template, tmp_path)
    done, calls = run_check(root, stubs, log, fail=stage.key)
    assert any(stage.key in call.split() for call in calls), (stage.key, calls)
    assert done.returncode != 0, f"check.sh ignored a failing {stage.key}: {done.stdout}"
