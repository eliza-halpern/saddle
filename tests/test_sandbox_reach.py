"""What a sandboxed command can and cannot reach.

Each constraint is pinned from both sides (CLAUDE.md: "a constraint is verified
by a known-good instance, never by its existence"): a command the lanes need
still works, and a command that reaches past the workdir is stopped.

Probes are harmless. Secrets are dummies set by the test itself, planted files
live in a private temp dir under /var/tmp (outside the sandbox's private /tmp,
so the planted tree is a real sibling of the workdir), and the only thing a
successful escape does is touch a marker file in that same temp dir.
"""

from __future__ import annotations

import json
import os
import shlex
import shutil
import socket
import subprocess
import sys
import tempfile
import time
from collections.abc import Iterator
from pathlib import Path

import pytest

from saddle import evidence
from saddle import sandbox as sandbox_module
from saddle.sandbox import Sandbox
from saddle.tools import ToolContext, execute_tool
from saddle.vllm import ToolCall
from tests.test_auto import Scripted, auto, call, finish
from tests.test_auto import repo as repo  # fixture

HAS_BWRAP = shutil.which("bwrap") is not None
needs_bwrap = pytest.mark.skipif(not HAS_BWRAP, reason="bwrap is not installed")

SECRET_NAME = "SANDBOX_DUMMY_SECRET"
SECRET_VALUE = "dummy-sandbox-value-not-a-real-secret"
PY = shlex.quote(sys.executable)


@pytest.fixture
def outside() -> Iterator[Path]:
    """A private dir outside /tmp, standing in for the rest of the disk."""
    base = Path("/var/tmp")
    if not os.access(base, os.W_OK):  # pragma: no cover - every Linux box has it
        pytest.skip("/var/tmp is not writable")
    path = Path(tempfile.mkdtemp(prefix="sandbox-reach-", dir=base))
    yield path.resolve()
    shutil.rmtree(path, ignore_errors=True)


@pytest.fixture
def work(outside: Path) -> Path:
    tree = outside / "bench" / "runs" / "tree"
    tree.mkdir(parents=True)
    (tree / "calc.py").write_text("def add(a, b):\n    return a + b\n")
    (tree / "test_calc.py").write_text(
        "from calc import add\n\n\ndef test_add():\n    assert add(2, 2) == 4\n"
    )
    return tree


def sh(box: Sandbox, command: str, timeout: int = 60) -> tuple[int | None, str]:
    terminal = box.run(command, timeout=timeout)
    return terminal.exit_code, terminal.output()


def git(cwd: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(cwd), "-c", "user.name=t", "-c", "user.email=t@t", *args],
        capture_output=True,
        text=True,
        check=True,
    ).stdout


def boxes(work: Path) -> list[Sandbox]:
    found = [Sandbox.for_workdir(work, prefer_bwrap=False)]
    if HAS_BWRAP:
        found.append(Sandbox.for_workdir(work))
    return found


# -- known-good: what every lane must still be able to run --------------------


@needs_bwrap
def test_the_tools_a_lane_needs_still_run_under_bwrap(work: Path) -> None:
    git(work, "init", "-q")
    git(work, "add", "-A")
    git(work, "commit", "-q", "-m", "init")
    (work / "calc.py").write_text("def add(a, b):\n    return b + a\n")
    box = Sandbox.for_workdir(work)
    assert box.isolation == "bwrap"
    checks = {
        f"{PY} -m pytest -q -p no:cacheprovider test_calc.py": "1 passed",
        f"{PY} -c 'import calc; print(calc.add(2, 3))'": "5",
        f"{PY} -c 'import pytest, pathlib; print(pathlib.Path(pytest.__file__).is_file())'": (
            "True"
        ),
        "git status --short": "M calc.py",
        "git diff --stat": "1 file changed",
        "echo hi > scratch.txt && cat scratch.txt": "hi",
        "echo t > /tmp/t && cat /tmp/t": "t",
    }
    for command, expected in checks.items():
        code, out = sh(box, command)
        assert code == 0, (command, out)
        assert expected in out, (command, out)


@needs_bwrap
def test_ruff_on_path_still_runs_under_bwrap(work: Path) -> None:
    ruff = shutil.which("ruff")
    if ruff is None:  # pragma: no cover - the dev venv has it
        pytest.skip("ruff is not on PATH")
    code, out = sh(Sandbox.for_workdir(work), f"{shlex.quote(ruff)} check --no-cache calc.py")
    assert code == 0, out


@needs_bwrap
def test_git_works_in_an_auto_style_worktree_under_bwrap(outside: Path) -> None:
    repo = outside / "repo"
    repo.mkdir()
    (repo / "a.py").write_text("x = 1\n")
    git(repo, "init", "-q")
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", "init")
    tree = repo / ".saddle" / "worktrees" / "r1"
    git(repo, "worktree", "add", "-q", "-b", "run", str(tree), "HEAD")
    (tree / "a.py").write_text("x = 2\n")
    code, out = sh(Sandbox.for_workdir(tree), "git status --short && git diff")
    assert code == 0, out
    assert "+x = 2" in out


# -- secrets in the environment ---------------------------------------------


def test_a_secret_in_saddles_environment_does_not_reach_a_command(
    work: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(SECRET_NAME, SECRET_VALUE)
    monkeypatch.setenv("VLLM_API_KEY", SECRET_VALUE)
    for box in boxes(work):
        # only lines carrying the dummy value are printed, never the whole env
        grep = f"env | grep -F {shlex.quote(SECRET_VALUE)}"
        code, out = sh(box, f'{grep}; echo "[${{{SECRET_NAME}:-unset}}]"')
        assert code == 0
        assert SECRET_VALUE not in out, box.isolation
        assert "[unset]" in out
        # the known-good half: what a command does need is still there
        code, out = sh(box, 'echo "$PATH" && python3 -c "print(40 + 2)"')
        assert "42" in out, (box.isolation, out)


def test_the_extra_env_a_run_sets_still_arrives(work: Path) -> None:
    for prefer in (False, True):
        if prefer and not HAS_BWRAP:  # pragma: no cover
            continue
        box = Sandbox.for_workdir(work, prefer_bwrap=prefer, env={"PYTHONDONTWRITEBYTECODE": "1"})
        _, out = sh(box, 'echo "[$PYTHONDONTWRITEBYTECODE]"')
        assert "[1]" in out


# -- files outside the workdir ------------------------------------------------


@needs_bwrap
def test_a_key_file_in_home_is_hidden(
    work: Path, outside: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    home = outside / "home"
    (home / ".config" / "saddle").mkdir(parents=True)
    (home / ".config" / "saddle" / "env").write_text(f"VLLM_API_KEY={SECRET_VALUE}\n")
    (home / ".ssh").mkdir()
    (home / ".ssh" / "id_ed25519").write_text("dummy key\n")
    monkeypatch.setenv("HOME", str(home))
    box = Sandbox.for_workdir(work)
    _, out = sh(box, "cat $HOME/.config/saddle/env; ls -A $HOME/.ssh; echo done")
    assert SECRET_VALUE not in out
    assert "id_ed25519" not in out
    assert "done" in out


@needs_bwrap
def test_the_real_home_is_not_listable(work: Path) -> None:
    home = Path.home().resolve()
    keep = {work, Path(sys.prefix).resolve(), Path(sys.base_prefix).resolve()}
    private = [
        entry
        for entry in sorted(home.iterdir())
        if entry.name.startswith(".") and not any(k == entry or entry in k.parents for k in keep)
    ][:5]
    if not private:  # pragma: no cover - a home with no dotfiles
        pytest.skip("nothing private in HOME to look for")
    names = " ".join(shlex.quote(str(p)) for p in private)
    _, out = sh(Sandbox.for_workdir(work), f"for p in {names}; do test -e $p && echo SEEN $p; done")
    assert "SEEN" not in out, out


@needs_bwrap
def test_a_planted_answer_beside_the_workdir_is_hidden(work: Path, outside: Path) -> None:
    reference = outside / "bench" / "reference"
    reference.mkdir(parents=True)
    (reference / "solution.py").write_text("PLANTED_ANSWER = 1\n")
    box = Sandbox.for_workdir(work)
    probe = f"cat ../../reference/solution.py; find {outside} -name solution.py 2>/dev/null"
    _, out = sh(box, probe)
    assert "PLANTED_ANSWER" not in out
    assert str(reference) not in out


@needs_bwrap
def test_a_write_outside_the_workdir_fails_under_bwrap(work: Path, outside: Path) -> None:
    # The contract is the host file. Inside, the path may exist on the
    # sandbox's throwaway root, so the command's exit code is not the check.
    sh(Sandbox.for_workdir(work), f"mkdir -p {outside} && touch {outside}/escaped")
    assert not (outside / "escaped").exists()


@needs_bwrap
def test_a_user_site_pth_cannot_be_planted_under_bwrap(
    work: Path, outside: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    home = outside / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    plant = (
        f'{PY} -c "import os, site; d = site.getusersitepackages(); '
        "os.makedirs(d, exist_ok=True); open(os.path.join(d, 'zz.pth'), 'w').write('import os')\""
    )
    sh(Sandbox.for_workdir(work), plant)
    assert not list(home.rglob("*.pth"))


@needs_bwrap
def test_host_runtime_sockets_are_hidden(work: Path) -> None:
    runtime = Path(f"/run/user/{os.getuid()}")
    if not runtime.is_dir():  # pragma: no cover - a headless box with no user session
        pytest.skip("no user runtime dir on this box")
    _, out = sh(
        Sandbox.for_workdir(work),
        f"ls -A {runtime} 2>/dev/null | head -3; test -e /run/docker.sock && echo DOCKER",
    )
    assert out.strip() == "", out


def test_a_bwrap_that_cannot_be_executed_is_reported(outside: Path) -> None:
    assert sandbox_module.bwrap_works(str(outside / "no-such-bwrap")) is not None


@needs_bwrap
def test_a_gitdir_file_without_a_commondir_still_works(outside: Path) -> None:
    # a submodule-style `.git` file pointing at a whole repo's git dir
    real = outside / "real"
    real.mkdir()
    (real / "a.py").write_text("x = 1\n")
    git(real, "init", "-q")
    git(real, "add", "-A")
    git(real, "commit", "-q", "-m", "init")
    tree = outside / "tree"
    tree.mkdir()
    (tree / ".git").write_text(f"gitdir: {real / '.git'}\n")
    git(tree, "config", "core.worktree", str(tree))
    (tree / "a.py").write_text("x = 2\n")
    code, out = sh(Sandbox.for_workdir(tree), "git diff")
    assert code == 0, out
    assert "+x = 2" in out


@needs_bwrap
def test_a_git_file_that_is_not_a_gitdir_is_still_read_only(work: Path) -> None:
    (work / ".git").write_text("not a gitdir line\n")
    sh(Sandbox.for_workdir(work), "echo changed > .git")
    assert (work / ".git").read_text() == "not a gitdir line\n"


# -- the fallback without bwrap ----------------------------------------------


def test_a_lane_that_requires_isolation_refuses_without_bwrap(work: Path) -> None:
    with pytest.raises(sandbox_module.IsolationUnavailableError, match="bwrap"):
        Sandbox.for_workdir(work, prefer_bwrap=False, require_isolation=True)


def test_a_bwrap_that_cannot_start_is_not_trusted(
    work: Path, outside: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Ubuntu 24.04's AppArmor userns restriction makes bwrap exist and fail.
    fake = outside / "bin"
    fake.mkdir()
    (fake / "bwrap").write_text(
        "#!/bin/sh\necho 'bwrap: setting up uid map: Permission denied' >&2\nexit 1\n"
    )
    (fake / "bwrap").chmod(0o755)
    monkeypatch.setenv("PATH", f"{fake}:{os.environ['PATH']}")
    box = Sandbox.for_workdir(work)
    assert box.isolation == "none"
    with pytest.raises(sandbox_module.IsolationUnavailableError, match="uid map"):
        Sandbox.for_workdir(work, require_isolation=True)


# -- processes ----------------------------------------------------------------


def test_nothing_a_command_starts_outlives_the_call(work: Path, outside: Path) -> None:
    marker = outside / "survivor"
    for box in boxes(work):
        marker.unlink(missing_ok=True)
        detach = f"nohup sh -c 'sleep 1.5; touch {marker}' >/dev/null 2>&1 & echo started"
        code, out = sh(box, detach)
        assert code == 0
        assert "started" in out
        time.sleep(2.5)
        assert not marker.exists(), box.isolation


def test_killing_a_terminal_kills_its_children(work: Path, outside: Path) -> None:
    marker = outside / "orphan"
    for box in boxes(work):
        marker.unlink(missing_ok=True)
        terminal = box.start(f"(sleep 1.5; touch {marker}) & sleep 60")
        time.sleep(0.5)
        box.kill(terminal.id)
        time.sleep(2.5)
        assert not marker.exists(), box.isolation


# -- network ------------------------------------------------------------------


@pytest.fixture
def listener() -> Iterator[int]:
    """A throwaway TCP listener on the host's loopback (never the model port)."""
    server = socket.socket()
    server.bind(("127.0.0.1", 0))
    server.listen(4)
    yield server.getsockname()[1]
    server.close()


CONNECT = (
    "import socket, sys; s = socket.socket(); s.settimeout(2); "
    "print('CONNECT', s.connect_ex(('127.0.0.1', int(sys.argv[1]))))"
)


@needs_bwrap
def test_a_no_network_lane_cannot_reach_a_host_port(work: Path, listener: int) -> None:
    box = Sandbox.for_workdir(work, network="none")
    _, out = sh(box, f"{PY} -c {shlex.quote(CONNECT)} {listener}")
    assert "CONNECT 0" not in out, out
    # the known-good half: a test's own loopback server still works inside
    own = (
        "import socket; a = socket.socket(); a.bind(('127.0.0.1', 0)); a.listen(1); "
        "b = socket.socket(); print('OWN', b.connect_ex(a.getsockname()))"
    )
    _, out = sh(box, f"{PY} -c {shlex.quote(own)}")
    assert "OWN 0" in out, out


@needs_bwrap
def test_a_host_network_lane_still_reaches_a_host_port(work: Path, listener: int) -> None:
    _, out = sh(Sandbox.for_workdir(work), f"{PY} -c {shlex.quote(CONNECT)} {listener}")
    assert "CONNECT 0" in out, out


# -- the Task lane, through the real run_auto path ---------------------------


@needs_bwrap
def test_an_auto_run_command_has_no_host_network_and_no_secret(
    repo: Path, listener: int, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(SECRET_NAME, SECRET_VALUE)
    probe = f'{PY} -c {shlex.quote(CONNECT)} {listener}; echo "[${{{SECRET_NAME}:-unset}}]"'
    client = Scripted([[call("run_command", "c1", command=probe)], finish()])
    auto(repo, client)
    result = next(m["content"] for m in client.asked[1]["messages"] if m.get("role") == "tool")
    assert "CONNECT" in result, result  # the probe ran
    assert "CONNECT 0" not in result, result
    assert "[unset]" in result, result


def test_an_auto_run_refuses_to_start_without_isolation(
    repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(shutil, "which", lambda name, *a, **k: None)
    client = Scripted([[call("run_command", "c1", command="true")], finish()])
    with pytest.raises(sandbox_module.IsolationUnavailableError):
        auto(repo, client)
    assert client.asked == []  # nothing reached the model


# -- persistence through git: code the host runs later ------------------------


@needs_bwrap
def test_a_command_cannot_plant_a_git_hook_the_host_runs(work: Path, outside: Path) -> None:
    git(work, "init", "-q")
    git(work, "add", "-A")
    git(work, "commit", "-q", "-m", "init")
    marker = outside / "fsmonitor-ran"
    box = Sandbox.for_workdir(work)
    sh(box, f"git config core.fsmonitor {shlex.quote(f'touch {marker}; false')}")
    sh(box, f"printf '#!/bin/sh\\ntouch {marker}\\n' > .git/hooks/pre-commit")
    subprocess.run(["git", "-C", str(work), "status"], capture_output=True, check=False)
    assert not marker.exists()
    assert not (work / ".git" / "hooks" / "pre-commit").exists()


@needs_bwrap
def test_a_command_cannot_redirect_a_worktree_gitdir(outside: Path) -> None:
    repo = outside / "repo"
    repo.mkdir()
    (repo / "a.py").write_text("x = 1\n")
    git(repo, "init", "-q")
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", "init")
    tree = repo / ".saddle" / "worktrees" / "r1"
    git(repo, "worktree", "add", "-q", "-b", "run", str(tree), "HEAD")
    marker = outside / "redirect-ran"
    redirect = (
        "git init -q .evil && "
        f"git --git-dir=.evil/.git config core.fsmonitor {shlex.quote(f'touch {marker}; false')}"
        " && git --git-dir=.evil/.git config core.worktree $PWD"
        " && echo gitdir: $PWD/.evil/.git > .git"
    )
    sh(Sandbox.for_workdir(tree), redirect)
    # what run_auto does on the host after the model's last turn
    subprocess.run(["git", "-C", str(tree), "add", "-A"], capture_output=True, check=False)
    assert not marker.exists()
    assert (tree / ".git").read_text().startswith(f"gitdir: {repo}")


# -- the Ask lane's search tool ----------------------------------------------


def _search(work: Path, **arguments: str) -> str:
    call = ToolCall(id="s1", name="search", arguments=json.dumps(arguments))
    context = ToolContext(workdir=work, allowed=("read_file", "list_dir", "search"))
    return execute_tool(call, workdir=work, context=context)


def test_search_does_not_read_through_a_parent_glob(work: Path, outside: Path) -> None:
    (outside / "bench" / "answer.txt").write_text("PLANTED_ANSWER here\n")
    result = _search(work, query="PLANTED_ANSWER", glob="../../*")
    assert "PLANTED_ANSWER here" not in result
    # the known-good half: a match inside the tree is still found
    assert "calc.py:2" in _search(work, query="return a + b")


def test_search_does_not_follow_a_symlink_out_of_the_tree(work: Path, outside: Path) -> None:
    (outside / "secret.txt").write_text("PLANTED_SECRET line\n")
    (work / "notes.txt").symlink_to(outside / "secret.txt")
    assert "PLANTED_SECRET line" not in _search(work, query="PLANTED_SECRET")


def test_search_with_an_absolute_glob_is_an_error_not_a_crash(work: Path) -> None:
    assert _search(work, query="root", glob="/etc/*").startswith("error: ")


# -- gate runs execute model-written code too (later work) ----


@pytest.mark.xfail(strict=True, reason="later work: gates run on the host, unsandboxed")
def test_a_gate_run_cannot_read_a_planted_answer(work: Path, outside: Path) -> None:
    (outside / "bench" / "answer.txt").write_text("PLANTED_ANSWER\n")
    run = evidence.run_shell_capture(f"cat {outside}/bench/answer.txt", work)
    assert "PLANTED_ANSWER" not in run.stdout


def test_a_gate_timeout_kills_the_whole_process_group(work: Path, outside: Path) -> None:
    marker = outside / "gate-orphan"
    run = evidence.run_capture(
        ["bash", "-c", f"(sleep 1.5; touch {marker}) & sleep 60"], work, timeout=0.5
    )
    assert run.timed_out
    time.sleep(2.5)
    assert not marker.exists()


@pytest.mark.xfail(strict=True, reason="later work: no cgroup memory/pids cap yet")
def test_a_command_runs_in_its_own_capped_cgroup(work: Path) -> None:
    # A cap needs a cgroup of its own; sharing saddle's means a fork bomb or a
    # runaway allocation in a command is charged to saddle (and whatever else
    # shares its slice), so nothing can cap the command alone.
    for box in boxes(work):
        terminal = box.start("sleep 5")
        assert terminal.process is not None
        try:
            mine = Path("/proc/self/cgroup").read_text()
            theirs = Path(f"/proc/{terminal.process.pid}/cgroup").read_text()
        finally:
            box.kill(terminal.id)
        assert theirs != mine, box.isolation


# -- tools the gates need, where they live under HOME -------------------------


def _venv_with_tool(home: Path, tool: str) -> Path:
    """A real venv under a stand-in HOME holding a stand-in gate tool."""
    venv = home / "proj" / ".venv"
    subprocess.run([sys.executable, "-m", "venv", "--without-pip", str(venv)], check=True)
    script = venv / "bin" / tool
    script.write_text(
        f"#!{venv / 'bin' / 'python'}\nimport sys\nprint('{tool} from', sys.prefix)\n"
    )
    script.chmod(0o755)
    return venv


@needs_bwrap
def test_a_gate_tool_in_a_venv_under_home_still_runs(
    work: Path, outside: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    home = outside / "home"
    venv = _venv_with_tool(home, "ruff")
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("VIRTUAL_ENV", str(venv))
    monkeypatch.setenv("PATH", f"{venv / 'bin'}:/usr/bin:/bin")
    code, out = sh(Sandbox.for_workdir(work), "ruff")
    assert code == 0, out
    assert f"ruff from {venv}" in out


@needs_bwrap
def test_exposing_a_venv_under_home_hides_the_rest_of_home(
    work: Path, outside: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    home = outside / "home"
    venv = _venv_with_tool(home, "ruff")
    (home / "proj" / "secret.txt").write_text("PLANTED_BESIDE_VENV\n")
    (home / ".ssh").mkdir()
    (home / ".ssh" / "id_planted").write_text("PLANTED_KEY\n")
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("VIRTUAL_ENV", str(venv))
    monkeypatch.setenv("PATH", f"{venv / 'bin'}:/usr/bin:/bin")
    box = Sandbox.for_workdir(work)
    _, out = sh(box, f"cat {home}/proj/secret.txt {home}/.ssh/id_planted 2>/dev/null")
    assert "PLANTED" not in out, out
    _, listed = sh(box, f"ls -A {home} {home}/proj 2>/dev/null")
    assert "secret.txt" not in listed.split(), listed
    assert ".ssh" not in listed.split(), listed


@needs_bwrap
def test_a_gate_tool_linked_onto_path_from_a_venv_still_runs(
    work: Path, outside: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # the shape `uv tool install` / pipx leave: ~/.local/bin/<tool> -> a tool venv
    home = outside / "home"
    venv = _venv_with_tool(home, "mutmut")
    shims = home / ".local" / "bin"
    shims.mkdir(parents=True)
    (shims / "mutmut").symlink_to(venv / "bin" / "mutmut")
    (shims / "unrelated").write_text("#!/bin/sh\necho PLANTED_SHIM\n")
    (shims / "unrelated").chmod(0o755)
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.delenv("VIRTUAL_ENV", raising=False)
    monkeypatch.setenv("PATH", f"{shims}:/usr/bin:/bin")
    box = Sandbox.for_workdir(work)
    code, out = sh(box, "mutmut")
    assert code == 0, out
    assert f"mutmut from {venv}" in out
    _, out = sh(box, f"unrelated; cat {shims}/unrelated")
    assert "PLANTED_SHIM" not in out, out


@needs_bwrap
def test_the_real_gate_tools_run_by_bare_name_under_bwrap(work: Path) -> None:
    (work / "pyproject.toml").write_text('[tool.mutmut]\nsource_paths = ["calc.py"]\n')
    box = Sandbox.for_workdir(work)
    for command, expected in {
        "ruff --version": "ruff",
        "pytest --version": "pytest",
        "coverage --version": "Coverage",
        "mutmut --help": "Usage: mutmut",
    }.items():
        if shutil.which(command.split()[0]) is None:  # pragma: no cover - the dev venv has them
            pytest.skip(f"{command.split()[0]} is not on PATH")
        code, out = sh(box, command)
        assert code == 0, (command, out)
        assert expected in out, (command, out)


# -- host-side git: what saddle itself runs in a tree a command could touch ----


def plant_host_hooks(repo: Path, markers: Path) -> None:
    """Hooks and an fsmonitor in `repo`'s config, each touching a marker.

    Under bwrap a command cannot write these (the test above); without it, or
    in a repo that already had them, saddle's own git is the last line."""
    hooks = markers / "hooks"
    hooks.mkdir()
    for name in ("pre-commit", "post-commit", "post-checkout", "post-merge", "post-index-change"):
        (hooks / name).write_text(f"#!/bin/sh\ntouch {markers}/ran-{name}\n")
        (hooks / name).chmod(0o755)
    git(repo, "config", "core.hooksPath", str(hooks))
    git(repo, "config", "core.fsmonitor", f"touch {markers}/ran-fsmonitor; false")


@needs_bwrap
def test_saddles_own_git_around_a_run_runs_no_planted_hook(repo: Path, tmp_path: Path) -> None:
    markers = tmp_path / "markers"
    markers.mkdir()
    plant_host_hooks(repo, markers)
    (repo / "calc.py").write_text("def add(a, b):\n    return a + b\n")  # a change to commit
    result = auto(repo, Scripted([finish()]))
    assert sorted(p.name for p in markers.glob("ran-*")) == []
    # the known-good half: the run still set up its worktree and committed
    assert result.commit
    assert git(result.worktree, "log", "-1", "--format=%s").startswith("saddle auto r1")


def test_default_expose_skips_what_is_not_a_venv_and_a_venv_without_a_home(
    outside: Path,
) -> None:
    not_a_venv = outside / "plain"
    (not_a_venv / "bin").mkdir(parents=True)
    homeless = outside / "homeless"
    (homeless / "bin").mkdir(parents=True)
    (homeless / "pyvenv.cfg").write_text("include-system-site-packages = false\n")
    tool = homeless / "bin" / "pytest"
    tool.write_text("#!/bin/sh\n")
    tool.chmod(0o755)
    env = {"VIRTUAL_ENV": str(not_a_venv), "PATH": f"{homeless / 'bin'}"}
    assert sandbox_module.default_expose(env) == ((homeless, homeless),)
