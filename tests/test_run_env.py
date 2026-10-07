"""What a task run's model is told about, and given as, its environment.

Dogfood run on saddle's own repo (src layout): the model spent seven rounds
finding a Python that could import the project, because its commands lacked
the `src/`-first `PYTHONPATH` the gates already use (`evidence.src_layout_env`)
and its prompt never said where it was or how the audit runs the tests.
"""

from __future__ import annotations

import json
import shutil
from pathlib import Path
from typing import Any

import pytest
from test_auto import Scripted, auto, call, finish, git
from test_project_env import make_venv

from saddle.audit import AUDIT_TEST_COMMAND
from saddle.auto import EVIDENCE_DIR, keep_evidence, page_coverage
from saddle.journal import read_spans
from saddle.jsevidence import COVERAGE_SCOPE
from saddle.tools import CHECK_SCHEMA, DISPUTE_TOOL, FACT_CD, FACT_RUN_TMP, TOOLS

PKG = "VALUE = 7\n"
FLAT = "def f():\n    return 2\n"
PROBE = 'python3 -c "import pkg; print(pkg.__file__)"'
PYPATH = "python3 -c \"import os; print('PP=' + os.environ.get('PYTHONPATH', ''))\""


def _repo(root: Path, *, src: bool) -> Path:
    (root / "tests").mkdir(parents=True)
    if src:
        (root / "src" / "pkg").mkdir(parents=True)
        (root / "src" / "pkg" / "__init__.py").write_text(PKG)
    else:
        (root / "n.py").write_text(FLAT)
    (root / "tests" / "test_x.py").write_text("def test_x():\n    assert True\n")
    (root / ".gitignore").write_text(".venv/\n")
    git(root, "init", "-q", "-b", "main")
    git(root, "add", "-A")
    git(root, "commit", "-q", "-m", "init")
    return root


def _command_details(result: Any) -> list[str]:
    return [s.detail for s in read_spans(result.journal) if s.name == "run_command"]


def _system(client: Scripted) -> str:
    return str(client.asked[0]["messages"][0]["content"])


def test_the_models_commands_import_the_src_layout_package_from_the_worktree(
    tmp_path: Path,
) -> None:
    """Red before: `import pkg` failed (ModuleNotFoundError) in the model's
    commands, though the gates import it from the same worktree."""
    repo = _repo(tmp_path / "repo", src=True)
    result = auto(repo, Scripted([[call("run_command", "r1", command=PROBE)], finish()]))
    detail = _command_details(result)[0]
    assert "exit 0" in detail, detail
    assert "/.saddle/worktrees/" in detail, detail
    assert "/src/pkg/__init__.py" in detail, detail


def test_a_flat_project_gets_no_src_entry_on_its_import_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Known-good half: without `src/` nothing is added. Nothing is
    inherited either: this suite may itself run with a `src` entry on its
    path (the audit runs saddle's own tests that way)."""
    monkeypatch.delenv("PYTHONPATH", raising=False)
    repo = _repo(tmp_path / "repo", src=False)
    result = auto(repo, Scripted([[call("run_command", "r1", command=PYPATH)], finish()]))
    detail = _command_details(result)[0]
    assert "exit 0" in detail, detail
    assert detail.split("PP=", 1)[1].strip() == "", detail


def test_the_prompt_states_the_worktree_the_python_and_the_audits_test_command(
    tmp_path: Path,
) -> None:
    """Red before: the system prompt said none of these."""
    repo = _repo(tmp_path / "repo", src=True)
    make_venv(repo / ".venv")
    client = Scripted([finish()])
    auto(repo, client)
    system = _system(client)
    assert "worktree" in system
    assert f"`{AUDIT_TEST_COMMAND}`" in system
    assert "project's own environment" in system
    assert "src/" in system
    assert "PYTHONPATH" in system


def test_the_prompt_says_so_when_there_is_no_project_venv_and_no_src(tmp_path: Path) -> None:
    repo = _repo(tmp_path / "repo", src=False)
    client = Scripted([finish()])
    auto(repo, client)
    system = _system(client)
    assert f"`{AUDIT_TEST_COMMAND}`" in system
    assert "project's own environment" not in system
    assert "no virtual environment of its own" in system
    assert "PYTHONPATH" not in system


def test_the_prompt_states_the_runs_budgets_as_given(tmp_path: Path) -> None:
    """Red before: the model was never told its time or token budget. Two
    budgets, two sentences: the numbers come from the run, not the text."""
    short = Scripted([finish()])
    auto(_repo(tmp_path / "a", src=False), short, time_budget_s=600.0, token_budget=5_000)
    long = Scripted([finish()])
    auto(_repo(tmp_path / "b", src=False), long, time_budget_s=5_400.0, token_budget=120_000)
    assert "This run has 10 minutes and 5,000 generated tokens" in _system(short)
    assert "This run has 90 minutes and 120,000 generated tokens" in _system(long)
    assert "run the test files that cover your change first" in _system(short)


def test_a_budget_under_a_minute_still_reads_as_one_minute(tmp_path: Path) -> None:
    repo = _repo(tmp_path / "repo", src=False)
    client = Scripted([finish()])
    auto(repo, client, time_budget_s=20.0)
    assert "This run has 1 minute and " in _system(client)


def test_a_run_with_no_limits_is_told_it_has_none(tmp_path: Path) -> None:
    client = Scripted([finish()])
    auto(_repo(tmp_path / "repo", src=False), client)  # the defaults: NO_LIMIT both
    system = _system(client)
    assert "This run has no time or token limit; it ends when you call finish." in system
    assert "1 minute" not in system  # known-bad: the deadline every default run was told
    assert "0 generated tokens" not in system
    assert "leave room to call finish" not in system


def test_one_limit_is_named_and_the_other_said_to_be_absent(tmp_path: Path) -> None:
    timed = Scripted([finish()])
    auto(_repo(tmp_path / "a", src=False), timed, time_budget_s=600.0)
    counted = Scripted([finish()])
    auto(_repo(tmp_path / "b", src=False), counted, token_budget=5_000)
    assert "This run has 10 minutes and no token limit; it stops there" in _system(timed)
    assert "This run has 5,000 generated tokens and no time limit; it stops there" in (
        _system(counted)
    )


def test_the_prompt_warns_about_coverage_in_addopts_only_when_it_is_there(
    tmp_path: Path,
) -> None:
    """Red before: a one-file test run on a repo whose addopts carry --cov
    failed on package coverage, and nothing said why. Instances: pyproject
    addopts (string and list), pytest.ini addopts, and a repo without."""
    cases = {
        "toml": ("pyproject.toml", '[tool.pytest.ini_options]\naddopts = "--cov=pkg"\n'),
        "list": ("pyproject.toml", '[tool.pytest.ini_options]\naddopts = ["-q", "--cov=pkg"]\n'),
        "ini": ("pytest.ini", "[pytest]\naddopts = --cov=pkg\n"),
        "none": ("pyproject.toml", '[tool.pytest.ini_options]\naddopts = "-q"\n'),
        "broken": ("pyproject.toml", "[tool.pytest.ini_options\naddopts = --cov\n"),
    }
    said: dict[str, bool] = {}
    for name, (config, text) in cases.items():
        root = tmp_path / name
        root.mkdir()
        (root / config).write_text(text)
        (root / "tests").mkdir()
        (root / "n.py").write_text(FLAT)
        (root / "tests" / "test_x.py").write_text("def test_x():\n    assert True\n")
        git(root, "init", "-q", "-b", "main")
        git(root, "add", "-A")
        git(root, "commit", "-q", "-m", "init")
        client = Scripted([finish()])
        auto(root, client)
        said[name] = "--no-cov" in _system(client)
    assert said == {"toml": True, "list": True, "ini": True, "none": False, "broken": False}


def test_the_prompt_names_the_worktree_and_not_the_checkout_it_hides(tmp_path: Path) -> None:
    """Red before: the prompt printed the project venv's absolute path, inside
    the main checkout; a watched run `cd`'d there, found it hidden by the
    sandbox, and lost two rounds asking where it was."""
    repo = _repo(tmp_path / "repo", src=True)
    make_venv(repo / ".venv")
    client = Scripted([finish()])
    auto(repo, client)
    system = _system(client)
    worktree = system.split("Your working directory is ", 1)[1].split(",", 1)[0]
    assert "/.saddle/worktrees/" in worktree
    assert Path(worktree).is_absolute()
    assert str((repo / ".venv").resolve()) not in system
    assert "not visible" in system


def test_the_prompt_explains_audit_notes_only_when_the_run_delivers_them(
    tmp_path: Path,
) -> None:
    """Red before: arm E+A+F appends audit results to tool results and the
    model was never told; a watched run guessed they came from the repo."""
    said: dict[str, bool] = {}
    for arm in ("E+A+F", "E+A", "E"):
        client = Scripted([finish()])
        auto(_repo(tmp_path / arm.replace("+", "p"), src=False), client, arm=arm)
        said[arm] = "audit checkpoint N on tree <id>, then PASS or FAIL" in _system(client)
    assert said == {"E+A+F": True, "E+A": False, "E": False}


def test_a_file_one_command_leaves_in_tmp_is_there_for_the_next(tmp_path: Path) -> None:
    """Red before: each command got an empty /tmp, and a watched run lost its
    fix when the copies it had saved there were gone by the next command.
    The run's /tmp sits beside its ledger and goes when the run ends."""
    repo = _repo(tmp_path / "repo", src=False)
    client = Scripted(
        [
            [call("run_command", "r1", command="echo kept > /tmp/scratch.txt")],
            [call("run_command", "r2", command="cat /tmp/scratch.txt")],
            finish(),
        ]
    )
    result = auto(repo, client)
    second = _command_details(result)[1]
    assert "exit 0" in second, second
    assert "kept" in second, second
    assert not (result.journal.parent / "tmp").exists()


def test_the_prompt_says_git_is_read_only_who_commits_and_where_scratch_goes(
    tmp_path: Path,
) -> None:
    """Red before: a watched run's `git checkout` hit the read-only git
    directory, its copies in /tmp were gone by the next command, and nothing
    said saddle makes the commit from the finish summary."""
    client = Scripted([finish()])
    auto(_repo(tmp_path / "repo", src=False), client)
    system = _system(client)
    assert "Git is read-only there" in system
    assert "your finish summary in the commit message" in system
    assert "/tmp is private to this run and lasts for all of it" in system


def test_the_prompt_says_the_finish_audit_runs_new_tests_on_the_old_code(tmp_path: Path) -> None:
    """Red before: a watched run undid its own fix to show its new tests
    failing on the old code, a check the finish audit makes (red-phase).
    Said only where that audit's findings reach the model (arm E+A+F)."""
    said: dict[str, bool] = {}
    for arm in ("E+A+F", "E"):
        client = Scripted([finish()])
        auto(_repo(tmp_path / arm.replace("+", "p"), src=False), client, arm=arm)
        wording = "against the original code, where at least one of them must fail"
        said[arm] = wording in _system(client)
    assert said == {"E+A+F": True, "E": False}


def test_the_prompt_holds_each_rewritten_test_to_failing_on_the_old_code(
    tmp_path: Path,
) -> None:
    """Red before: the prompt said the finish audit runs "your new and changed
    tests" on the original code and "others may pass there", but the audit
    refuses each rewritten test that passes there
    (test_sanction_red.py::test_a_rewrite_that_passes_on_both_trees_is_not_sanctioned).
    "May pass" covers only new tests; the rewrite rule is stated."""
    client = Scripted([finish()])
    auto(_repo(tmp_path / "repo", src=False), client, arm="E+A+F")
    system = _system(client)
    assert "new and changed tests" not in system
    assert "other new tests may pass there" in system
    assert "each rewritten test must fail on the original code" in system


def test_the_prompt_says_a_pinned_test_rewrite_is_put_to_a_person(tmp_path: Path) -> None:
    """Red before: a watched run had to rewrite two tests that pinned the
    masking its task removed, found the rewrite refused, and went looking
    through the harness for a way to finish. Said where audits reach the model."""
    said: dict[str, bool] = {}
    for arm in ("E+A+F", "E"):
        client = Scripted([finish()])
        auto(_repo(tmp_path / arm.replace("+", "p"), src=False), client, arm=arm)
        said[arm] = "asks a person to approve it when the run ends" in _system(client)
    assert said == {"E+A+F": True, "E": False}


def test_a_resumed_run_continues_the_recorded_conversation_in_its_own_worktree(
    tmp_path: Path,
) -> None:
    """Dev resume: the recorded messages go to the model as they were, behind
    this harness's system prompt, with the recorded worktree path rewritten
    to this run's, and the recorded files applied before the model continues."""
    repo = _repo(tmp_path / "repo", src=False)
    old = "/old/run/.saddle/worktrees/abc123"
    recorded = tmp_path / "request.json"
    recorded.write_text(
        json.dumps(
            {
                "messages": [
                    {
                        "role": "system",
                        "content": f"Old prompt. Your working directory is {old}, x",
                    },
                    {"role": "user", "content": "make add add"},
                    {
                        "role": "assistant",
                        "content": "",
                        "tool_calls": [
                            {
                                "id": "r0",
                                "type": "function",
                                "function": {
                                    "name": "run_command",
                                    "arguments": json.dumps({"command": f"cd {old} && cat n.py"}),
                                },
                            }
                        ],
                    },
                    {"role": "tool", "tool_call_id": "r0", "content": "exit 0"},
                ]
            }
        )
    )
    patch = tmp_path / "state.diff"
    patch.write_text(
        "diff --git a/n.py b/n.py\n--- a/n.py\n+++ b/n.py\n@@ -1,2 +1,2 @@\n"
        " def f():\n-    return 2\n+    return 3\n"
    )
    kept = tmp_path / "kept_tmp"
    kept.mkdir()
    (kept / "note.txt").write_text("from before\n")
    (kept / "pytest-current").symlink_to(tmp_path / "gone")  # dangling, as pytest leaves it
    client = Scripted([[call("run_command", "r1", command="cat n.py /tmp/note.txt")], finish()])
    result = auto(repo, client, resume_messages=recorded, resume_patch=patch, resume_tmp=kept)
    sent = client.asked[0]["messages"]
    system = str(sent[0]["content"])
    assert system.startswith("You are working alone")
    worktree = system.split("Your working directory is ", 1)[1].split(",", 1)[0]
    assert [m["role"] for m in sent[1:]] == ["user", "assistant", "tool"]
    assert old not in json.dumps(sent)
    assert f"cd {worktree} && cat n.py" in sent[2]["tool_calls"][0]["function"]["arguments"]
    assert "return 3" in _command_details(result)[0]
    assert "from before" in _command_details(result)[0]


def test_the_prompt_says_how_many_workers_the_audit_runs_the_suite_on(tmp_path: Path) -> None:
    """Red before: a resumed watched run ran saddle's whole suite serially
    (about twenty minutes) while the audit uses eight workers, because
    nothing said so. Said only when the project sets `test-workers` and its
    environment has pytest-xdist to honour `-n`."""
    said: dict[str, bool] = {}
    for name, workers, xdist in (("both", 8, True), ("no-xdist", 8, False), ("serial", 1, True)):
        repo = _repo(tmp_path / name, src=False)
        (repo / "pyproject.toml").write_text(f"[tool.saddle]\ntest-workers = {workers}\n")
        git(repo, "add", "-A")
        git(repo, "commit", "-q", "-m", "workers")
        make_venv(repo / ".venv")
        if xdist:
            (next((repo / ".venv").glob("lib/python*/site-packages")) / "xdist").mkdir()
        client = Scripted([finish()])
        auto(repo, client)
        said[name] = f"pass `-n {workers}` too" in _system(client)
    assert said == {"both": True, "no-xdist": False, "serial": False}


NODE_PROBE = (
    "node_modules/.bin/fmt; touch node_modules/new 2>&1 || echo refused-write; "
    "echo n.py changed > n.py; git status --porcelain"
)


def test_the_models_commands_run_the_checkouts_node_tools_and_git_does_not_see_them(
    tmp_path: Path,
) -> None:
    """Red before: a watched run's JavaScript fix failed the prettier stage the
    finish audit runs, and the model could not run prettier: its worktree held
    tracked files only ("I can't run c8 here because there's no node_modules")."""
    repo = _repo(tmp_path / "repo", src=False)
    (repo / ".gitignore").write_text(".venv/\nnode_modules/\n")
    git(repo, "commit", "-qam", "ignore node_modules")
    tool = repo / "node_modules" / ".bin" / "fmt"
    tool.parent.mkdir(parents=True)
    tool.write_text("#!/bin/sh\necho formatted-by-the-checkouts-tool\n")
    tool.chmod(0o755)
    client = Scripted([[call("run_command", "r1", command=NODE_PROBE)], finish()])
    result = auto(repo, client)
    detail = _command_details(result)[0]
    assert "formatted-by-the-checkouts-tool" in detail, detail
    assert "refused-write" in detail, detail
    assert "?? node_modules" not in detail, detail
    assert " M n.py" in detail, detail
    assert "node_modules/.bin/<tool>" in _system(client)
    committed = git(repo, "show", "--name-only", "--format=", result.branch)
    assert committed.split() == ["n.py"]
    assert not (repo / "node_modules" / "new").exists()


def test_without_node_modules_nothing_is_mounted_or_said(tmp_path: Path) -> None:
    client = Scripted([finish()])
    auto(_repo(tmp_path / "repo", src=False), client)
    assert "node_modules/.bin/<tool>" not in _system(client)


NODE_ON_PATH = shutil.which("node")
node_hidden_by_the_sandbox = pytest.mark.skipif(
    NODE_ON_PATH is None or not Path(NODE_ON_PATH).resolve().is_relative_to(Path.home()),
    reason="needs a node installed under HOME, which the sandbox hides unless it is named",
)


@node_hidden_by_the_sandbox
def test_the_commands_the_project_names_reach_the_models_sandbox_too(tmp_path: Path) -> None:
    """Red before: the project's `sandbox-expose` reached the audit's sandbox only.
    A watched run's commands had no `node` ('node: No such file or directory' in
    13 tests), and its node_modules/.bin tools, `#!/usr/bin/env node` scripts,
    could not start."""
    repo = _repo(tmp_path / "repo", src=False)
    (repo / ".gitignore").write_text(".venv/\nnode_modules/\n")
    (repo / "pyproject.toml").write_text('[tool.saddle]\nsandbox-expose = ["node"]\n')
    git(repo, "add", "-A")
    git(repo, "commit", "-qm", "expose node")
    tool = repo / "node_modules" / ".bin" / "fmt"
    tool.parent.mkdir(parents=True)
    tool.write_text("#!/usr/bin/env node\nconsole.log('formatted by node ' + typeof process);\n")
    tool.chmod(0o755)
    command = "node -e 'console.log(1 + 1)'; node_modules/.bin/fmt"
    result = auto(repo, Scripted([[call("run_command", "r1", command=command)], finish()]))
    detail = _command_details(result)[0]
    assert "exit 0" in detail, detail
    assert "2\nformatted by node object" in detail, detail


def test_the_finish_audit_of_a_run_finds_the_checkouts_packages(tmp_path: Path) -> None:
    """Red before: a watched run's finish audit took node tools from its worktree,
    whose node_modules is only a mount point: no c8, no typescript, and 17 tests
    that needed node_modules failed, refusing a correct change."""
    repo = _repo(tmp_path / "repo", src=False)
    (repo / ".gitignore").write_text(".venv/\nnode_modules/\n")
    (repo / "tests" / "test_x.py").write_text(
        "from pathlib import Path\n\nfrom n import f\n\n\n"
        "def test_x():\n"
        '    assert Path("node_modules/pkg/data.txt").read_text() == "ok"\n'
        "    assert f() == 2\n"
    )
    git(repo, "add", "-A")
    git(repo, "commit", "-qm", "a test that reads node_modules")
    (repo / "node_modules" / "pkg").mkdir(parents=True)
    (repo / "node_modules" / "pkg" / "data.txt").write_text("ok")
    # A data file: no Python line changes, so nothing but the suite's tier-1 run
    # (which reads node_modules) can refuse the finish. The suite's own mutmut is
    # a stub here, and would refuse a Python edit for having no mutants.
    edit = call("write_file", "w1", path="notes.txt", content="node tools reach the audit\n")
    result = auto(repo, Scripted([[edit], finish()]), arm="E+A+F")
    assert result.outcome == "finished", result


def _with_pyproject(root: Path, saddle_table: str) -> Path:
    (root / "pyproject.toml").write_text(f"[tool.saddle]\n{saddle_table}\n")
    git(root, "add", "-A")
    git(root, "commit", "-q", "-m", "pyproject")
    return root


def test_the_projects_own_audit_checks_are_named_before_they_can_refuse(tmp_path: Path) -> None:
    """Red before: the static check and gate stages reached the model only as
    refusals, one finish spent per stage it had not been told of."""
    root = _with_pyproject(
        _repo(tmp_path / "repo", src=False),
        'static-check = ["mypy", "--strict", "n.py"]\ngate-checks = [["eslint", "."]]',
    )
    client = Scripted([finish()])
    auto(root, client)
    system = _system(client)
    assert "the finish audit runs the project's own checks" in system
    assert "`mypy --strict n.py`; `eslint .`" in system
    # Known good: a project that configures none is told of none.
    quiet = Scripted([finish()])
    auto(_repo(tmp_path / "plain", src=False), quiet)
    assert "the project's own checks" not in _system(quiet)


def test_audit_settings_that_cannot_be_read_are_said_not_left_out(tmp_path: Path) -> None:
    from saddle.auto import audit_stages

    root = _with_pyproject(_repo(tmp_path / "repo", src=False), 'static-check = "mypy"')
    said = audit_stages(root)
    assert said.startswith("The project's audit settings could not be read")
    assert "static-check = 'mypy'" in said


def test_the_feed_prompt_states_the_flip_form_and_the_runs_own_refusal_cap(
    tmp_path: Path,
) -> None:
    client = Scripted([finish()])
    auto(_repo(tmp_path / "repo", src=False), client, arm="E+A+F", finish_refusal_cap=5)
    system = _system(client)
    assert "`flip: <exact test name> -- <evidence>`" in system
    assert "If finish is refused 5 times in a row with the same findings" in system
    off = Scripted([finish()])
    auto(_repo(tmp_path / "off", src=False), off, arm="E")
    assert "refused 5 times" not in _system(off)


def test_the_prompt_says_how_to_undo_an_edit_git_cannot_restore(tmp_path: Path) -> None:
    client = Scripted([finish()])
    auto(_repo(tmp_path / "repo", src=False), client)
    assert "copy the file to /tmp first and copy it back afterwards" in _system(client)


def test_what_the_model_saves_in_tmp_evidence_outlives_the_run_and_is_listed(
    tmp_path: Path,
) -> None:
    """Red before: everything in the run's /tmp went when the run ended, so a run
    asked for screenshots had nowhere to leave them."""
    repo = _repo(tmp_path / "repo", src=False)
    save = (
        "mkdir -p /tmp/evidence/shots && printf png > /tmp/evidence/shots/after.png"
        " && printf x > /tmp/scratch.txt && ln -s /etc/hostname /tmp/evidence/link"
    )
    client = Scripted([[call("run_command", "r1", command=save)], finish()])
    result = auto(repo, client)
    kept = result.journal.parent / EVIDENCE_DIR
    assert (kept / "shots" / "after.png").read_text() == "png"
    assert not (result.journal.parent / "tmp").exists()  # the rest of /tmp still goes
    message = git(result.worktree, "log", "-1", "--format=%B")
    where = kept.relative_to(repo).as_posix()
    assert f"kept in {where}: shots/after.png\n" in message
    assert "link" not in message.split(where)[1].splitlines()[0]  # a link is not a file
    assert "after.png" not in git(result.worktree, "show", "--stat", "--format=", "HEAD")
    assert "is kept beside the run's ledger" in _system(client)
    # Known good: nothing saved there, nothing kept and nothing said.
    quiet = auto(_repo(tmp_path / "bare", src=False), Scripted([finish()]))
    assert not (quiet.journal.parent / EVIDENCE_DIR).exists()
    assert "Files the model saved" not in git(quiet.worktree, "log", "-1", "--format=%B")


def test_evidence_that_is_not_a_folder_keeps_nothing(tmp_path: Path) -> None:
    run_tmp, run_dir = tmp_path / "tmp", tmp_path / "run"
    run_tmp.mkdir()
    run_dir.mkdir()
    assert keep_evidence(run_tmp, run_dir) == []
    (tmp_path / "elsewhere").mkdir()
    (tmp_path / "elsewhere" / "secret").write_text("s")
    (run_tmp / EVIDENCE_DIR).symlink_to(tmp_path / "elsewhere")
    assert keep_evidence(run_tmp, run_dir) == []
    assert not (run_dir / EVIDENCE_DIR).exists()


def test_a_task_partly_done_already_is_finished_in_part_not_disputed(tmp_path: Path) -> None:
    client = Scripted([finish()])
    auto(_repo(tmp_path / "repo", src=False), client)
    system = _system(client)
    assert "call dispute only when no part of the task is left to do" in system
    dispute = next(t for t in client.asked[0]["tools"] if t["function"]["name"] == DISPUTE_TOOL)
    assert (
        "When only some of the task's parts are false or already done, do not dispute"
        in (dispute["function"]["description"])
    )


def test_read_only_tests_say_what_to_do_when_a_change_needs_one(tmp_path: Path) -> None:
    client = Scripted([finish()])
    auto(_repo(tmp_path / "repo", src=False), client)
    assert "call blocked and name the test it needs" in _system(client)
    editable = Scripted([finish()])
    auto(_repo(tmp_path / "open", src=False), editable, allow_test_edits=True)
    assert "name the test it needs" not in _system(editable)


def test_page_coverage_rules_are_said_only_where_chrome_tests_measure_pages(
    tmp_path: Path,
) -> None:
    repo = _repo(tmp_path / "repo", src=False)
    assert page_coverage(repo) == ""
    scope = repo / COVERAGE_SCOPE
    scope.parent.mkdir(parents=True)
    scope.write_text(
        json.dumps(
            {
                "measured": [],
                "not_measured": {},
                "chrome_measured": {"static/p.js": 1},
                "chrome_tests": ["tests/test_page.py"],
            }
        )
    )
    said = page_coverage(repo)
    assert "`static/p.js`" in said
    assert "`tests/test_page.py`" in said
    assert "if any skips" in said
    scope.write_text(json.dumps({"measured": [], "not_measured": {}, "chrome_tests": ["t.py"]}))
    assert page_coverage(repo) == ""  # Chrome tests, but no page measured by them


def test_the_check_tool_says_it_runs_the_projects_own_checks() -> None:
    said = CHECK_SCHEMA["function"]["description"]
    assert "the project's own audit checks" in said


def test_an_autonomous_runs_run_command_says_what_lasts_between_commands(
    tmp_path: Path,
) -> None:
    """#176: a run 477 rounds in was unsure whether /tmp outlived a command.
    The run_command it is offered says /tmp lasts and `cd` and exported
    variables do not (`test_a_run_tmp_lasts_across_commands_and_is_never_the_hosts`
    holds the sandbox to the first; a fresh shell per command to the rest)."""
    root = _repo(tmp_path / "repo", src=False)
    client = Scripted([finish()])
    auto(root, client)
    offered = client.asked[0]["tools"]
    (run_command,) = [t for t in offered if t["function"]["name"] == "run_command"]
    described = run_command["function"]["description"]
    assert FACT_RUN_TMP in described
    assert FACT_CD not in described  # a chat session's sentence (test_task_freeze)
    assert FACT_RUN_TMP not in str(TOOLS)  # stated for the run, not for every caller
