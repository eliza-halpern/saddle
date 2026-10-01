"""ShellCheck covers every shell script the repository tracks, and `ci-mutate.sh` keeps its shape.

The contract, each half both ways:

- the files `check.sh` hands to shellcheck are exactly the tracked files that
  are shell scripts, found by `.sh`/`.bash` extension or by shell shebang, so
  the hooks (which have no extension) cannot be forgotten and a new script
  cannot be missed; a script shellcheck objects to makes it exit non-zero and a
  clean one makes it exit zero; every tracked script passes it today;
- `ci-mutate.sh` selects the modules a diff touches for its guaranteed phase,
  adds a rotation sample from the rest (never the package marker), and fails on
  a survivor, on a failed mutation run, and not at all when no Python changed.

`ci-mutate.sh` is driven here against a temporary git repo with a stub `uv`
that logs its arguments: the script's own logic is what is under test, not
mutmut.
"""

from __future__ import annotations

import os
import re
import shlex
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
VENV_BIN = Path(sys.executable).parent
SHELL_SHEBANG = re.compile(
    rb"^#!\s*(?:/usr/bin/env\s+(?:-S\s+)?)?(?:\S*/)?(?:ba|da|k|z|a)?sh(?:\s|$)"
)

# Not a repository checkout (an sdist, mutmut's work copy): nothing to enumerate.
IN_CHECKOUT = (ROOT / ".git").exists() and (ROOT / "check.sh").is_file()
needs_checkout = pytest.mark.skipif(not IN_CHECKOUT, reason="not a git checkout of the repository")


def tracked_files() -> list[str]:
    out = subprocess.run(
        ["git", "-C", str(ROOT), "ls-files", "-z"], capture_output=True, check=True
    ).stdout
    return [name for name in out.decode().split("\0") if name]


def is_shell_script(path: Path, name: str) -> bool:
    """A `.sh`/`.bash` file, or any file whose first line is a shell shebang."""
    if name.endswith((".sh", ".bash")):
        return True
    if path.is_dir():
        return False
    with path.open("rb") as fh:
        return SHELL_SHEBANG.match(fh.readline(200)) is not None


def tracked_shell_scripts() -> list[str]:
    return sorted(name for name in tracked_files() if is_shell_script(ROOT / name, name))


def command_lines(script: str) -> list[str]:
    """The logical command lines of a shell script: continuations joined, comments dropped."""
    lines: list[str] = []
    pending = ""
    for raw in script.splitlines():
        text = pending + raw.strip()
        pending = ""
        if text.endswith("\\"):
            pending = text[:-1].rstrip() + " "
            continue
        if text and not text.startswith("#"):
            lines.append(text)
    return lines


def shellcheck_targets(script: str) -> list[str]:
    """The files named by the single `shellcheck` command in `script`."""
    found = [
        shlex.split(line, comments=True)
        for line in command_lines(script)
        if re.search(r"(?:^|\s)shellcheck(?:\s|$)", line)
    ]
    assert len(found) == 1, f"expected exactly one shellcheck command, found {found}"
    words = found[0]
    return sorted(w for w in words[words.index("shellcheck") + 1 :] if not w.startswith("-"))


def run_shellcheck(*files: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [str(VENV_BIN / "shellcheck"), *map(str, files)],
        capture_output=True,
        text=True,
        check=False,
    )


# -- ShellCheck over every tracked shell script ------------------------------


@needs_checkout
def test_shellcheck_is_run_on_exactly_the_tracked_shell_scripts() -> None:
    targets = shellcheck_targets((ROOT / "check.sh").read_text())
    scripts = tracked_shell_scripts()
    assert len(scripts) >= 5, scripts  # check.sh, ci-mutate.sh and the three hooks
    assert targets == scripts


def test_the_shell_script_finder_sees_a_hook_without_an_extension(tmp_path: Path) -> None:
    hook = tmp_path / "pre-x"
    hook.write_text("#!/bin/sh\ntrue\n")
    py = tmp_path / "tool"
    py.write_text("#!/usr/bin/env python3\nprint(1)\n")
    env_bash = tmp_path / "other"
    env_bash.write_text("#!/usr/bin/env bash\ntrue\n")
    assert is_shell_script(hook, "pre-x")
    assert is_shell_script(env_bash, "other")
    assert is_shell_script(tmp_path / "gone.sh", "gone.sh")
    assert not is_shell_script(py, "tool")
    assert not is_shell_script(tmp_path, "dir")


def test_shellcheck_targets_reads_a_continued_command_and_refuses_none_or_two() -> None:
    assert shellcheck_targets("uv run shellcheck -x \\\n    b.sh a.sh # why\n") == ["a.sh", "b.sh"]
    with pytest.raises(AssertionError, match="exactly one"):
        shellcheck_targets("# shellcheck a.sh\ntrue\n")
    with pytest.raises(AssertionError, match="exactly one"):
        shellcheck_targets("shellcheck a.sh\nshellcheck b.sh\n")


def test_shellcheck_exits_non_zero_on_a_bad_script_and_zero_on_a_good_one(tmp_path: Path) -> None:
    bad = tmp_path / "bad.sh"
    bad.write_text('#!/bin/sh\ncd "$1"\necho $2\n')
    good = tmp_path / "good.sh"
    good.write_text("#!/bin/sh\nprintf '%s\\n' \"$1\"\n")
    refused = run_shellcheck(bad)
    assert refused.returncode != 0
    assert "SC2164" in refused.stdout
    assert run_shellcheck(good).returncode == 0


@needs_checkout
@pytest.mark.parametrize("name", tracked_shell_scripts() if IN_CHECKOUT else [])
def test_every_tracked_shell_script_passes_shellcheck(name: str) -> None:
    done = run_shellcheck(ROOT / name)
    assert done.returncode == 0, done.stdout


# -- ci-mutate.sh ------------------------------------------------------------

UV_STUB = """#!/bin/sh
echo "$*" >> "$UV_LOG"
case "$*" in
    *"mutmut results"*) printf '%s' "${MUT_RESULTS:-}"; exit 0 ;;
    *"mutmut run"*) exit "${MUT_RUN_EXIT:-0}" ;;
esac
exit 0
"""


def git(repo: Path, *args: str) -> None:
    subprocess.run(
        ["git", "-C", str(repo), "-c", "user.name=t", "-c", "user.email=t@example.com",
         "-c", "core.hooksPath=/dev/null", "-c", "commit.gpgsign=false", *args],
        check=True,
        capture_output=True,
    )  # fmt: skip


def mutate_repo(tmp_path: Path) -> Path:
    """A repo of src/saddle {__init__, a, b, c} whose last commit touched a and test_b_extra."""
    repo = tmp_path / "repo"
    (repo / "src" / "saddle").mkdir(parents=True)
    (repo / "tests").mkdir()
    for name in ("__init__", "a", "b", "c"):
        (repo / "src" / "saddle" / f"{name}.py").write_text("X = 1\n")
    (repo / "README.md").write_text("one\n")
    shutil.copy(ROOT / "ci-mutate.sh", repo / "ci-mutate.sh")
    git(repo, "init", "-q", "-b", "main")
    git(repo, "add", ".")
    git(repo, "commit", "-q", "-m", "base")
    return repo


def commit_change(repo: Path, *names: str) -> None:
    for name in names:
        path = repo / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(path.read_text() + "# more\n" if path.exists() else "# new\n")
    git(repo, "add", ".")
    git(repo, "commit", "-q", "-m", "change")


def run_mutate(
    repo: Path, tmp_path: Path, *args: str, **env: str
) -> tuple[subprocess.CompletedProcess[str], list[str]]:
    stubs = tmp_path / "stubs"
    stubs.mkdir(exist_ok=True)
    uv = stubs / "uv"
    uv.write_text(UV_STUB)
    uv.chmod(0o755)
    log = tmp_path / "uv.log"
    log.write_text("")
    done = subprocess.run(
        ["bash", str(repo / "ci-mutate.sh"), *args],
        capture_output=True,
        text=True,
        check=False,
        env={
            "PATH": f"{stubs}:{os.environ['PATH']}",
            "HOME": str(tmp_path),
            "UV_LOG": str(log),
            **env,
        },
    )
    return done, log.read_text().splitlines()


def test_ci_mutate_maps_the_diff_to_phase_one_and_samples_the_rest(tmp_path: Path) -> None:
    repo = mutate_repo(tmp_path)
    commit_change(repo, "src/saddle/a.py", "tests/test_b_extra.py")
    done, calls = run_mutate(repo, tmp_path, "HEAD~1")
    assert done.returncode == 0, done.stdout + done.stderr
    assert "phase 1: diff modules: a b" in done.stdout
    assert "OK: no surviving mutants in scope" in done.stdout
    # The rest is `c` and the package marker; only `c` may be sampled.
    assert "phase 2: rotation sample: c" in done.stdout
    assert calls[0] == "run --frozen mutmut run saddle.a* saddle.b*"
    assert calls[1] == "run --frozen mutmut run saddle.c*"
    assert not any("__init__" in call for call in calls)


def test_ci_mutate_fails_on_a_survivor_and_on_a_failed_run(tmp_path: Path) -> None:
    repo = mutate_repo(tmp_path)
    commit_change(repo, "src/saddle/a.py")
    survived, _ = run_mutate(
        repo, tmp_path, "HEAD~1", MUT_RESULTS="saddle.a.x_f__mutmut_1: survived\n"
    )
    assert survived.returncode == 1
    assert "FAIL: surviving mutants found" in survived.stdout
    broken, _ = run_mutate(repo, tmp_path, "HEAD~1", MUT_RUN_EXIT="2")
    assert broken.returncode == 1
    assert "mutation run failed (exit 2), not a timeout" in broken.stdout


def test_ci_mutate_skips_when_no_python_module_changed(tmp_path: Path) -> None:
    repo = mutate_repo(tmp_path)
    commit_change(repo, "README.md")
    done, calls = run_mutate(repo, tmp_path, "HEAD~1")
    assert done.returncode == 0
    assert "no Python modules changed; skipping mutation" in done.stdout
    assert calls == []


def test_ci_mutate_without_a_base_runs_everything(tmp_path: Path) -> None:
    repo = mutate_repo(tmp_path)
    done, calls = run_mutate(repo, tmp_path, "0000000")
    assert done.returncode == 0
    assert "no diff base; running full mutation" in done.stdout
    assert calls[0] == "run --frozen mutmut run"
