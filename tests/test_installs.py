"""Approved installs: a run's own overlay on the project venv, from a local
wheel folder, with the user's approval each time (#113).

The wheels here are built in a temp dir by `make_wheel` (a zip, written
directly: no build backend, no network). The project venvs are real
(`test_project_env.make_venv`). pip is the one the interpreter bundles for
`ensurepip`, run inside bwrap with no network.
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import json
import os
import socket
import subprocess
import sys
import zipfile
from pathlib import Path
from typing import Any, cast

import pytest
from test_auto import Scripted, call, finish, git
from test_project_env import BUGGY, FIXED, make_venv

from saddle import cli, installs, sandbox
from saddle.auditor import Auditor
from saddle.auto import AutoError, AutoOptions, AutoResult, run_auto
from saddle.events import Question
from saddle.installs import INSTALL_FAILED, INSTALL_REFUSED, Installs, WheelFolder
from saddle.journal import read_spans
from saddle.packet import compile_packet
from saddle.vllm import VllmClient

pytestmark = pytest.mark.skipif(
    sandbox.isolation_problem() is not None, reason="installs run under bwrap"
)

NEEDS_EXTRA = (
    "from needsinstall import TWO\n\nfrom n import f\n\n\ndef test_f():\n    assert f() == TWO\n"
)
PROBE = "python -c 'import needsinstall; print(needsinstall.__file__)'"


def make_wheel(folder: Path, name: str = "needsinstall", version: str = "1.0") -> Path:
    """A pure-Python wheel of `name`, whose module holds `TWO = 2`."""
    folder.mkdir(parents=True, exist_ok=True)
    info = f"{name}-{version}.dist-info"
    files = {
        f"{name}/__init__.py": "TWO = 2\n",
        f"{info}/METADATA": f"Metadata-Version: 2.1\nName: {name}\nVersion: {version}\n",
        f"{info}/WHEEL": "Wheel-Version: 1.0\nGenerator: test\nRoot-Is-Purelib: true\n"
        "Tag: py3-none-any\n",
    }
    record = []
    for path, text in files.items():
        digest = base64.urlsafe_b64encode(hashlib.sha256(text.encode()).digest()).rstrip(b"=")
        record.append(f"{path},sha256={digest.decode()},{len(text.encode())}")
    record.append(f"{info}/RECORD,,")
    wheel = folder / f"{name}-{version}-py3-none-any.whl"
    with zipfile.ZipFile(wheel, "w") as z:
        for path, text in files.items():
            z.writestr(path, text)
        z.writestr(f"{info}/RECORD", "\n".join(record) + "\n")
    return wheel


def tree_digest(root: Path) -> dict[str, str]:
    """Every file, link and directory under `root`, by content or target."""
    out: dict[str, str] = {}
    for here, dirs, files in os.walk(root):
        for name in dirs + files:
            path = Path(here) / name
            rel = str(path.relative_to(root))
            if path.is_symlink():
                out[rel] = "link " + os.readlink(path)
            elif path.is_file():
                out[rel] = hashlib.sha256(path.read_bytes()).hexdigest()
            else:
                out[rel] = "dir"
    return out


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    """A repo whose test needs `needsinstall`, which its `.venv` lacks."""
    root = tmp_path / "repo"
    (root / "tests").mkdir(parents=True)
    (root / "n.py").write_text(BUGGY)
    (root / "tests" / "test_n.py").write_text(NEEDS_EXTRA)
    (root / ".gitignore").write_text(".venv/\n")
    git(root, "init", "-q", "-b", "main")
    git(root, "add", "-A")
    git(root, "commit", "-q", "-m", "init")
    make_venv(root / ".venv")
    return root


@pytest.fixture
def wheels(tmp_path: Path) -> WheelFolder:
    make_wheel(tmp_path / "wheels")
    return WheelFolder(tmp_path / "wheels", "from flag --wheel-dir")


def _run(
    repo: Path,
    client: Scripted,
    wheels: WheelFolder | None,
    answer: Any = None,
    **kwargs: Any,
) -> AutoResult:
    kwargs.setdefault("run_id", "r1")
    options = AutoOptions(task="t", repo=repo, wheels=wheels, **kwargs)
    return run_auto(options, cast(VllmClient, client), answer=answer)


def _tool(result: AutoResult, name: str) -> list[Any]:
    return [s for s in read_spans(result.journal) if s.argv and s.argv[0] == name]


def _ask(pkg: str = "needsinstall", call_id: str = "i1") -> list[Any]:
    return [call("install", call_id, packages=[pkg], reason="the test imports it")]


# -- the run: known-good and known-bad ------------------------------------------


def test_an_approved_install_lets_the_gates_and_commands_see_the_package(
    repo: Path, wheels: WheelFolder
) -> None:
    """Red before: there was no install tool, so the tests could not import
    the package and finish stayed refused. The first finish is refused on
    the same tree the second accepts: the cached verdict from before the
    install is not reused after it."""
    venv = (repo / ".venv").resolve()
    before = tree_digest(venv)
    asked: list[Question] = []

    def answer(question: Question) -> str:
        asked.append(question)
        return "Install"

    fix = call("write_file", "w1", path="n.py", content=FIXED)
    probe = call("run_command", "r1", command=PROBE)
    result = _run(
        repo,
        Scripted([[fix], finish(), _ask(), [probe], finish(call_id="f2")]),
        wheels,
        answer=answer,
        finish_refusal_cap=3,
    )
    assert (result.outcome, result.reason) == ("finished", "finish called"), result.reason
    finishes = _tool(result, "finish")
    assert finishes[0].exit_code != 0  # refused: the tests could not import it
    # flip: #131 -- before the install no test ran `f`, and a line in a
    # definition the baseline had was spared; now a plain finish audit names it
    # and the run asks to allow test edits first. The install ask is unchanged.
    edits, question = asked
    assert "needs a test that covers n.py:2" in edits.text
    assert "needsinstall" in question.text
    assert str(wheels.path) in question.text
    assert question.options == ["Install", "Refuse"]
    [done] = _tool(result, "install")
    assert done.exit_code == 0, done.detail
    assert "installed into this run's environment" in done.detail
    assert "needsinstall 1.0" in done.detail
    overlay = result.journal.parent / "overlay"
    [ran] = _tool(result, "run_command")
    assert "exit 0" in ran.detail, ran.detail
    assert str(overlay.resolve()) in ran.detail
    # The project venv is byte-identical, and the overlay is gone.
    assert tree_digest(venv) == before
    assert not overlay.exists()
    tests = next(r for r in compile_packet(result.journal, run_id="r1").rows if r.key == "tests")
    assert tests.status == "proven"
    assert "with 1 approved install(s) layered on it" in tests.text
    assert done.record_hash in tests.cites
    # What this admits, on the record: the accepted branch needs a package the
    # project venv still lacks, so its tests fail there until the user installs it.
    alone = subprocess.run(
        [str(venv / "bin" / "python"), "-m", "pytest", "-q", "-p", "no:cacheprovider"],
        cwd=result.worktree,
        capture_output=True,
        text=True,
        check=False,
    )
    assert alone.returncode != 0
    assert "No module named 'needsinstall'" in alone.stdout


@pytest.mark.parametrize("answer", [None, lambda _q: "Refuse"], ids=["headless", "refused"])
def test_without_approval_nothing_is_installed(
    repo: Path, wheels: WheelFolder, answer: Any
) -> None:
    """Known-bad: a headless run (no one to answer) and a user's Refuse both
    leave the package out; the tests still cannot import it."""
    fix = call("write_file", "w1", path="n.py", content=FIXED)
    result = _run(repo, Scripted([[fix], _ask(), finish()]), wheels, answer=answer)
    assert result.outcome == "stopped"
    [refused] = _tool(result, "install")
    assert refused.detail.startswith(f"{INSTALL_REFUSED}the user did not approve it")
    assert not (result.journal.parent / "overlay").exists()


def test_a_headless_refusal_is_sealed_as_unanswered(repo: Path, wheels: WheelFolder) -> None:
    result = _run(repo, Scripted([_ask(), finish()]), wheels, arm="E")
    answered = [s for s in read_spans(result.journal) if s.name == "answer"]
    assert [s.argv for s in answered] == [["answer", "Refuse", "unanswered"]]


def test_a_missing_wheel_is_reported_missing_and_nothing_is_asked(
    repo: Path, wheels: WheelFolder
) -> None:
    asked: list[Question] = []

    def answer(question: Question) -> str:
        asked.append(question)
        return "Install"

    result = _run(repo, Scripted([_ask("notthere"), finish()]), wheels, answer=answer, arm="E")
    [refused] = _tool(result, "install")
    assert refused.detail.startswith(f"{INSTALL_REFUSED}missing from the wheel folder")
    assert "notthere" in refused.detail
    assert "Nothing was installed" in refused.detail
    assert asked == []


def test_without_allow_installs_the_tool_is_not_offered(repo: Path) -> None:
    client = Scripted([finish()])
    _run(repo, client, None, arm="E")
    names = [t["function"]["name"] for t in client.asked[0]["tools"]]
    assert "install" not in names
    client = Scripted([finish()])
    make_wheel(repo.parent / "w")
    _run(repo, client, WheelFolder(repo.parent / "w", "x"), arm="E", run_id="r2")
    assert "install" in [t["function"]["name"] for t in client.asked[0]["tools"]]


# -- the run does not start without what installs need --------------------------


def test_a_missing_or_empty_wheel_folder_stops_the_run_before_it_starts(
    repo: Path, tmp_path: Path
) -> None:
    gone = WheelFolder(tmp_path / "nowhere", "from environment SADDLE_WHEEL_DIR")
    with pytest.raises(AutoError, match=r"nowhere \(from environment SADDLE_WHEEL_DIR\) does"):
        _run(repo, Scripted([finish()]), gone)
    (tmp_path / "empty").mkdir()
    (tmp_path / "empty" / "pkg-1.0.tar.gz").write_text("an sdist is not a wheel")
    with pytest.raises(AutoError, match=r"holds no \.whl files"):
        _run(repo, Scripted([finish()]), WheelFolder(tmp_path / "empty", "built-in default"))
    (tmp_path / "file").write_text("")
    with pytest.raises(AutoError, match=r"is not a directory"):
        _run(repo, Scripted([finish()]), WheelFolder(tmp_path / "file", "x"))
    assert not (repo / ".saddle").exists()


def test_installs_need_a_project_venv_to_layer_on(tmp_path: Path, wheels: WheelFolder) -> None:
    root = tmp_path / "plain"
    root.mkdir()
    (root / "n.py").write_text(BUGGY)
    git(root, "init", "-q", "-b", "main")
    git(root, "add", "-A")
    git(root, "commit", "-q", "-m", "init")
    with pytest.raises(AutoError, match=r"layered on the project's virtualenv, and there is none"):
        _run(root, Scripted([finish()]), wheels, arm="E")


# -- the seam: requests, folder, network ----------------------------------------


def _seam(repo: Path, folder: WheelFolder) -> Installs:
    return Installs(
        folder=folder, project=(repo / ".venv").resolve(), overlay=repo.parent / "overlay"
    )


@pytest.mark.parametrize(
    "good", ["needsinstall", "needsinstall==1.0", "needsinstall>=1,<2", "NeedsInstall"]
)
def test_a_plain_requirement_is_accepted(repo: Path, wheels: WheelFolder, good: str) -> None:
    plan = _seam(repo, wheels).plan(json.dumps({"packages": [good], "reason": "r"}))
    assert isinstance(plan, installs.Plan), plan
    assert [w.name for w in plan.wheels] == ["needsinstall-1.0-py3-none-any.whl"]


@pytest.mark.parametrize(
    "bad",
    [
        "--index-url=https://example.invalid/simple",
        "needsinstall @ https://example.invalid/n.whl",
        "../wheels/needsinstall-1.0-py3-none-any.whl",
        "needsinstall[extra]",
        "needsinstall; python_version>'3'",
        "-e .",
        "",
    ],
)
def test_anything_but_a_plain_requirement_is_refused(
    repo: Path, wheels: WheelFolder, bad: str
) -> None:
    said = _seam(repo, wheels).plan(json.dumps({"packages": [bad], "reason": "r"}))
    assert isinstance(said, str)
    assert said.startswith(f"{INSTALL_REFUSED}not a plain requirement")


@pytest.mark.parametrize("arguments", ["not json", "{}", '{"packages": []}', '{"packages": [1]}'])
def test_a_malformed_request_is_refused(repo: Path, wheels: WheelFolder, arguments: str) -> None:
    said = _seam(repo, wheels).plan(arguments)
    assert isinstance(said, str)
    assert said.startswith(f"{INSTALL_REFUSED}give `packages`")


def test_a_folder_emptied_during_the_run_is_a_refusal_naming_it(
    repo: Path, wheels: WheelFolder
) -> None:
    for wheel in wheels.wheels():
        wheel.unlink()
    said = _seam(repo, wheels).plan(json.dumps({"packages": ["needsinstall"]}))
    assert isinstance(said, str)
    assert f"the wheel folder {wheels.path} (from flag --wheel-dir) holds no .whl" in said


def test_the_installer_has_no_network(repo: Path, wheels: WheelFolder) -> None:
    """A host listener the test itself reaches is unreachable from the
    installer's sandbox: an attempt to use the network fails."""
    seam = _seam(repo, wheels)
    seam.overlay.mkdir()
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        listener.listen(1)
        port = listener.getsockname()[1]
        socket.create_connection(("127.0.0.1", port), timeout=5).close()  # reachable here
        code = (
            "import socket\n"
            "try:\n"
            f"    socket.create_connection(('127.0.0.1', {port}), timeout=5)\n"
            "    print('reached')\n"
            "except OSError:\n"
            "    print('refused')\n"
        )
        done = seam._run([sys.executable, "-c", code], use=seam.project)
    assert done.stdout.strip() == "refused", done.stdout


def test_a_package_already_in_the_project_venv_is_not_reinstalled(
    repo: Path, tmp_path: Path
) -> None:
    make_wheel(tmp_path / "w", name="onlyhere", version="9.9")
    seam = _seam(repo, WheelFolder(tmp_path / "w", "x"))
    purelib = next((repo / ".venv").glob("lib/python*/site-packages"))
    info = purelib / "onlyhere-1.0.dist-info"
    info.mkdir()
    (info / "METADATA").write_text("Metadata-Version: 2.1\nName: onlyhere\nVersion: 1.0\n")
    plan = seam.plan(json.dumps({"packages": ["onlyhere"]}))
    assert isinstance(plan, installs.Plan)
    said = seam.install(plan)
    assert said.startswith("already there before this run, not installed: onlyhere 1.0"), said
    assert seam.installed == []


def test_a_failed_pip_is_reported_failed_never_installed(repo: Path, tmp_path: Path) -> None:
    broken = tmp_path / "w" / "needsinstall-1.0-py3-none-any.whl"
    broken.parent.mkdir()
    broken.write_text("not a zip")
    seam = _seam(repo, WheelFolder(broken.parent, "x"))
    plan = seam.plan(json.dumps({"packages": ["needsinstall"]}))
    assert isinstance(plan, installs.Plan)
    said = seam.install(plan)
    assert said.startswith(INSTALL_FAILED)
    assert "missing, not installed: needsinstall" in said
    assert seam.installed == []


def test_a_lookup_that_fails_is_reported_as_failed(
    repo: Path, wheels: WheelFolder, monkeypatch: pytest.MonkeyPatch
) -> None:
    seam = _seam(repo, wheels)
    monkeypatch.setattr(installs, "_LOOKUP", "raise SystemExit('lookup broke')")
    plan = seam.plan(json.dumps({"packages": ["needsinstall"]}))
    assert isinstance(plan, installs.Plan)
    said = seam.install(plan)
    assert said.startswith(INSTALL_FAILED)
    assert "looking up what is installed failed (lookup broke)" in said
    assert seam.installed == []


# -- what the gates and the sandbox see -------------------------------------------


def test_the_overlay_shows_the_project_venv_read_only(repo: Path, wheels: WheelFolder) -> None:
    seam = _seam(repo, wheels)
    plan = seam.plan(json.dumps({"packages": ["needsinstall"]}))
    assert isinstance(plan, installs.Plan)
    seam.install(plan)
    shown = [d for _, d in sandbox.default_expose({"VIRTUAL_ENV": str(seam.overlay)})]
    assert seam.overlay.resolve() in shown
    assert seam.project in shown
    assert sandbox.project_env_problem(seam.overlay) is None
    # Known-bad: a layers-on key naming a directory that is no venv shows nothing.
    cfg = seam.overlay / "pyvenv.cfg"
    cfg.write_text(cfg.read_text().replace(str(seam.project), str(repo.parent)))
    shown = [d for _, d in sandbox.default_expose({"VIRTUAL_ENV": str(seam.overlay)})]
    assert repo.parent.resolve() not in shown
    assert sandbox.layers_on(repo.parent / "nowhere") is None


def test_the_environment_key_changes_when_an_install_lands(repo: Path, tmp_path: Path) -> None:
    venv = (repo / ".venv").resolve()
    with sandbox.using_project_env(venv):
        first = sandbox.environment_key()
        assert sandbox.environment_key() == first
        site = next(venv.glob("lib/python*/site-packages"))
        (site / "new-1.0.dist-info").mkdir()
        assert sandbox.environment_key() != first
        key = Auditor(repo)._key(1, "tree")
    (site / "new-1.0.dist-info").rmdir()
    with sandbox.using_project_env(venv):
        assert Auditor(repo)._key(1, "tree") != key


# -- the setting --------------------------------------------------------------------


def _args(*argv: str) -> argparse.Namespace:
    return cli.build_parser().parse_args(["auto", "t", *argv])


def test_the_wheel_folder_resolves_flag_env_file_default(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(cli, "KEY_FILE", str(tmp_path / "env"))
    monkeypatch.delenv("SADDLE_WHEEL_DIR", raising=False)
    assert cli.wheel_folder(_args()) is None
    default = cli.wheel_folder(_args("--allow-installs"))
    assert default == WheelFolder(Path(installs.DEFAULT_WHEEL_DIR).expanduser(), "built-in default")
    (tmp_path / "env").write_text(f"SADDLE_WHEEL_DIR={tmp_path / 'f'}\n")
    assert cli.wheel_folder(_args("--allow-installs")) == WheelFolder(
        tmp_path / "f", f"from file {tmp_path / 'env'}"
    )
    monkeypatch.setenv("SADDLE_WHEEL_DIR", str(tmp_path / "e"))
    assert cli.wheel_folder(_args("--allow-installs")) == WheelFolder(
        tmp_path / "e", "from environment SADDLE_WHEEL_DIR"
    )
    assert cli.wheel_folder(_args("--allow-installs", "--wheel-dir", "/w")) == WheelFolder(
        Path("/w"), "from flag --wheel-dir"
    )
    web = cli.build_parser().parse_args(["web", "--allow-installs", "--wheel-dir", "/w"])
    assert cli.wheel_folder(web) == WheelFolder(Path("/w"), "from flag --wheel-dir")


# -- the installer's own failures -----------------------------------------------------


def _planned(seam: Installs, name: str = "needsinstall") -> installs.Plan:
    plan = seam.plan(json.dumps({"packages": [name]}))
    assert isinstance(plan, installs.Plan), plan
    return plan


def test_a_second_install_goes_into_the_same_overlay(repo: Path, wheels: WheelFolder) -> None:
    make_wheel(wheels.path, name="second", version="2.0")
    seam = _seam(repo, wheels)
    (repo / ".venv" / "bin" / "native").write_bytes(b"\x7fELF not a script")
    assert "needsinstall 1.0" in seam.install(_planned(seam))
    with sandbox.using_project_env(seam.overlay):
        key = sandbox.environment_key()
    assert "second 2.0" in seam.install(_planned(seam, "second"))
    with sandbox.using_project_env(seam.overlay):
        assert sandbox.environment_key() != key
    assert seam.installed == ["needsinstall==1.0", "second==2.0"]
    assert not (seam.overlay / "bin" / "native").exists()  # only scripts are re-pointed
    pytest_script = (seam.overlay / "bin" / "pytest").read_text()
    assert pytest_script.startswith(f"#!{seam.overlay / 'bin' / 'python'}\n")


def test_without_bwrap_nothing_is_installed(
    repo: Path, wheels: WheelFolder, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(sandbox, "isolation_problem", lambda: "bwrap is not installed")
    seam = _seam(repo, wheels)
    said = seam.install(_planned(seam))
    assert said == (
        f"{INSTALL_REFUSED}installs need bwrap, and it cannot start here: bwrap is not "
        "installed. Nothing was installed."
    )


def test_an_overlay_that_cannot_be_made_is_a_refusal(tmp_path: Path, wheels: WheelFolder) -> None:
    broken = tmp_path / "broken"
    (broken / "bin").mkdir(parents=True)
    (broken / "pyvenv.cfg").write_text("home = /usr/bin\n")
    (broken / "bin" / "python").write_text("#!/bin/sh\necho no venv here\nexit 3\n")
    (broken / "bin" / "python").chmod(0o755)
    seam = Installs(folder=wheels, project=broken.resolve(), overlay=tmp_path / "overlay")
    said = seam.install(_planned(seam))
    assert said.startswith(f"{INSTALL_REFUSED}could not make the run's environment: no venv")


@pytest.mark.parametrize(
    ("probe", "said"),
    [
        ('print(\'{"bundled": "", "pip": false}\')', "no pip to install with"),
        ("raise SystemExit('probe broke')", "looking for a pip to install with failed (probe"),
    ],
)
def test_no_pip_is_a_refusal_naming_why(
    repo: Path, wheels: WheelFolder, monkeypatch: pytest.MonkeyPatch, probe: str, said: str
) -> None:
    monkeypatch.setattr(installs, "_PIP_PROBE", probe)
    seam = _seam(repo, wheels)
    assert seam.install(_planned(seam)).startswith(f"{INSTALL_REFUSED}{said}")
    assert seam.installed == []


def test_the_projects_own_pip_is_used_without_a_bundled_one(
    tmp_path: Path, wheels: WheelFolder, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A venv made with pip (offline: ensurepip's own wheel), and a probe
    that finds no bundled wheel: `python -m pip` installs from the folder."""
    project = tmp_path / "withpip"
    subprocess.run([sys.executable, "-m", "venv", str(project)], check=True, capture_output=True)
    hidden = "import json; print(json.dumps({'bundled': '', 'pip': True}))"
    monkeypatch.setattr(installs, "_PIP_PROBE", hidden)
    seam = Installs(folder=wheels, project=project.resolve(), overlay=tmp_path / "overlay")
    before = tree_digest(project)
    said = seam.install(_planned(seam))
    assert "installed into this run's environment" in said, said
    assert tree_digest(project) == before


def test_a_hung_install_step_times_out(
    repo: Path, wheels: WheelFolder, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(installs, "INSTALL_TIMEOUT_S", 0.5)
    seam = _seam(repo, wheels)
    seam.overlay.mkdir()
    done = seam._run([sys.executable, "-c", "import time; time.sleep(30)"], use=seam.project)
    assert (done.returncode, done.stdout) == (124, "timed out after 0s")


def test_in_arm_e_an_approved_install_reaches_the_models_commands(
    repo: Path, wheels: WheelFolder
) -> None:
    """Arm E has no auditor to switch, but its commands still use the overlay."""
    probe = call("run_command", "r1", command=PROBE)
    result = _run(
        repo,
        Scripted([[probe], _ask(), [probe], finish()]),
        wheels,
        answer=lambda _q: "Install",
        arm="E",
    )
    before, after = _tool(result, "run_command")
    assert "No module named 'needsinstall'" in before.detail, before.detail
    assert "exit 0" in after.detail, after.detail
    assert str((result.journal.parent / "overlay").resolve()) in after.detail
