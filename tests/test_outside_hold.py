"""A full-access command that can destroy what saddle cannot back up is held (#124, #137).

Known-good: `BASE="$HOME/hp1"; rm -rf "$BASE"` (a variable set earlier in the
same command) is resolved, backed up and recorded, runs without asking, and Undo
puts the files back; commands made only of read-only programs leave no record.

Known-bad: `rm -rf "$(cat path)"`, an unset variable, a glob over a tree too big
to back up, `find -delete` over one, `pkill -f` and `xargs rm` are put to the
person and are not run when declined, or when nobody can answer; a read-only
looking command with a write (`cat x > ~/y`, `tee`, `find -delete`) is still
recorded; `/dev/null` is never a created file.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from saddle.sideeffects import SideEffects, expand_home, is_special_path, plan_command
from saddle.tools import HELD_TITLE, ToolContext, execute_tool
from saddle.vllm import ToolCall


@pytest.fixture
def home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    place = tmp_path / "home"
    place.mkdir()
    monkeypatch.setenv("HOME", str(place))
    return place


@pytest.fixture
def folder(tmp_path: Path) -> Path:
    place = tmp_path / "folder"
    place.mkdir()
    return place


class Asked:
    """An approval callback that records what the person was shown."""

    def __init__(self, answer: bool) -> None:
        self.answer = answer
        self.shown: list[tuple[str, list[str]]] = []

    def __call__(self, title: str, lines: list[str]) -> bool:
        self.shown.append((title, lines))
        return self.answer


def session(folder: Path, tmp_path: Path, asked: Asked | None) -> ToolContext:
    return ToolContext(
        workdir=folder,
        full_access=True,
        effects=SideEffects(tmp_path / "rec"),
        approve=asked,
    )


def run(ctx: ToolContext, command: str) -> str:
    call = ToolCall(id="c", name="run_command", arguments=json.dumps({"command": command}))
    return execute_tool(call, workdir=ctx.workdir, context=ctx)


def small_tree(home: Path) -> Path:
    tree = home / "hp1"
    (tree / "sub").mkdir(parents=True)
    (tree / "a.txt").write_text("alpha")
    (tree / "sub" / "b.txt").write_text("beta")
    return tree


def big_tree(home: Path, count: int = 250) -> Path:
    tree = home / "big"
    tree.mkdir()
    for number in range(count):
        (tree / f"f{number}.txt").write_text("x")
    return tree


# --- F1: destructive commands ----------------------------------------------


def test_a_variable_set_earlier_in_the_command_is_resolved_backed_up_and_undone(
    tmp_path: Path, home: Path, folder: Path
) -> None:
    tree = small_tree(home)
    asked = Asked(False)
    ctx = session(folder, tmp_path, asked)
    out = run(ctx, 'BASE="$HOME/hp1"\nrm -rf "$BASE"')
    assert out.endswith("exit 0\n")
    assert not tree.exists()
    assert asked.shown == []
    view = ctx.effects.view() if ctx.effects else {}
    assert {Path(f["path"]).name: f["change"] for f in view["files"]} == {
        "a.txt": "deleted",
        "b.txt": "deleted",
    }
    assert ctx.effects is not None
    assert sorted(ctx.effects.undo(delete_created=False)["restored"]) == sorted(
        [str(tree / "a.txt"), str(tree / "sub" / "b.txt")]
    )
    assert (tree / "sub" / "b.txt").read_text() == "beta"


def test_a_target_through_a_substitution_is_held_shown_and_not_run_when_declined(
    tmp_path: Path, home: Path, folder: Path
) -> None:
    tree = small_tree(home)
    (folder / "path").write_text(str(tree))
    asked = Asked(False)
    ctx = session(folder, tmp_path, asked)
    out = run(ctx, 'rm -rf "$(cat path)"')
    assert tree.exists()
    assert out.startswith("error: this command was not run")
    ((title, lines),) = asked.shown
    assert title == HELD_TITLE
    assert lines[0] == 'command: rm -rf "$(cat path)"'
    assert any("command substitution" in line for line in lines)


def test_an_approved_held_command_runs_and_is_listed_as_not_fully_backed_up(
    tmp_path: Path, home: Path, folder: Path
) -> None:
    tree = small_tree(home)
    (folder / "path").write_text(str(tree))
    ctx = session(folder, tmp_path, Asked(True))
    assert run(ctx, 'rm -rf "$(cat path)"').endswith("exit 0\n")
    assert not tree.exists()
    assert ctx.effects is not None
    (row,) = ctx.effects.view()["not_tracked"]
    assert any("approved by the person" in r for r in row["reasons"])


def test_nobody_to_ask_is_a_no(tmp_path: Path, home: Path, folder: Path) -> None:
    tree = small_tree(home)
    ctx = session(folder, tmp_path, None)
    assert run(ctx, f'X="{tree}/$UNSET"; rm -rf $X').startswith("error: this command was not run")
    assert (tree / "a.txt").exists()


def test_a_glob_over_a_tree_too_big_to_back_up_is_held(
    tmp_path: Path, home: Path, folder: Path
) -> None:
    tree = big_tree(home)
    asked = Asked(False)
    out = run(session(folder, tmp_path, asked), f"rm -rf {tree}/*")
    assert out.startswith("error: this command was not run")
    assert len(list(tree.iterdir())) == 250
    assert "more than the" in "\n".join(asked.shown[0][1])


def test_a_file_over_the_backup_limit_is_held(tmp_path: Path, home: Path, folder: Path) -> None:
    (home / "huge.bin").write_bytes(b"0" * (8 * 1024 * 1024 + 1))
    asked = Asked(False)
    run(session(folder, tmp_path, asked), "rm -rf ~/huge.bin")
    assert (home / "huge.bin").exists()
    assert "too large to back up" in "\n".join(asked.shown[0][1])


@pytest.mark.parametrize(
    "command",
    [
        "find ~/big -delete",
        "find ~/big -name '*.txt' -exec rm {} +",
        "mv ~/big ~/hp9",
        "rsync -a --delete ~/hp1/ ~/big/",
        "echo x > ~/big/f1.txt\nrm -rf ~/big",
        "cd $NOWHERE && rm -rf build",
        "bash -c 'rm -rf $NOWHERE'",
        "ls | xargs rm -rf",
    ],
)
def test_each_destructive_form_over_something_unbackable_is_held(
    command: str, tmp_path: Path, home: Path, folder: Path
) -> None:
    big_tree(home)
    ctx = session(folder, tmp_path, Asked(False))
    assert ctx.effects is not None
    reasons = ctx.effects.hold(command, folder)
    assert reasons, command


@pytest.mark.parametrize(
    "command",
    [
        "rm -rf ~/hp1",
        "rm -f ~/hp1/a.txt",
        "rm -rf build",
        "rm -rf $HOME/hp1/sub",
        "mv ~/hp1/a.txt ~/hp1/c.txt",
        "NAME=hp1; rm -r ~/$NAME",
        "cat x > ~/out.txt",
        "echo hi > /dev/null",
        "rm $UNKNOWN",
        "export D=~/hp1 && rm -rf ${D}/sub",
    ],
)
def test_a_destructive_command_with_concrete_small_targets_is_not_held(
    command: str, tmp_path: Path, home: Path, folder: Path
) -> None:
    small_tree(home)
    effects = SideEffects(tmp_path / "rec")
    assert effects.hold(command, folder) == []


def test_pkill_and_killall_are_held_and_point_at_the_processes_tool(
    tmp_path: Path, home: Path, folder: Path
) -> None:
    asked = Asked(False)
    out = run(session(folder, tmp_path, asked), "pkill -f definitely-not-a-process-xyz")
    assert out.startswith("error: this command was not run")
    text = "\n".join(asked.shown[0][1])
    assert "other programs" in text
    assert "processes tool" in text
    effects = SideEffects(tmp_path / "rec2")
    assert effects.hold("killall -9 firefox", folder)
    assert effects.hold("kill 12345", folder) == []


def test_a_variable_set_in_the_command_is_read_and_a_cd_is_followed(
    home: Path, folder: Path
) -> None:
    small_tree(home)
    plan = plan_command("BASE=~/hp1; cd $BASE && rm -rf sub", folder)
    assert plan.targets == [(home / "hp1" / "sub").resolve()]
    assert plan.unresolved == []


# --- F2: read-only commands -------------------------------------------------


@pytest.mark.parametrize(
    "command",
    [
        "cat ~/a.txt",
        "ls -la ~ | grep conf | head -5",
        "find ~ -name '*.toml' -type f",
        "for f in ~/*.txt; do wc -l $f; done",
        "du -sh ~ 2>/dev/null; stat ~/a.txt && which python3",
        "if test -f ~/a.txt; then echo yes; fi",
        "echo $HOME; file ~/a.txt 2>&1",
    ],
)
def test_a_command_of_read_only_programs_is_not_recorded(
    command: str, tmp_path: Path, home: Path, folder: Path
) -> None:
    (home / "a.txt").write_text("a")
    ctx = session(folder, tmp_path, None)
    run(ctx, command)
    assert ctx.effects is not None
    assert ctx.effects.view()["empty"] is True
    assert ctx.effects._read() == []
    assert plan_command(command, folder).read_only is True


@pytest.mark.parametrize(
    "command",
    [
        "cat x > ~/y",
        "echo hi >> ~/y",
        "cat x | tee ~/y",
        "find ~ -name a -delete",
        "find ~ -exec touch {} +",
        "sort -o ~/y x",
        "echo $(touch ~/y)",
        "cat <(touch ~/y)",
        "ls; python3 -c 'pass'",
    ],
)
def test_a_read_only_looking_command_with_a_write_is_still_recorded(
    command: str, folder: Path
) -> None:
    plan = plan_command(command, folder)
    assert plan.read_only is False


def test_a_redirected_read_only_command_still_records_the_file_it_creates(
    tmp_path: Path, home: Path, folder: Path
) -> None:
    ctx = session(folder, tmp_path, None)
    run(ctx, "echo hi > ~/y.txt")
    assert ctx.effects is not None
    ((row,),) = [ctx.effects.view()["files"]]
    assert (Path(row["path"]).name, row["change"]) == ("y.txt", "created")


def test_a_background_read_only_command_is_not_listed_as_not_tracked(
    tmp_path: Path, home: Path, folder: Path
) -> None:
    ctx = session(folder, tmp_path, None)
    call = ToolCall(
        id="c", name="run_command", arguments=json.dumps({"command": "sleep 0", "background": True})
    )
    execute_tool(call, workdir=folder, context=ctx)
    assert ctx.effects is not None
    assert ctx.effects.view()["empty"] is True


# --- F3: devices --------------------------------------------------------------


def test_dev_null_is_never_a_created_file(tmp_path: Path, home: Path, folder: Path) -> None:
    ctx = session(folder, tmp_path, None)
    run(ctx, "printf x > /dev/null; printf y > /dev/stdout | cat > /dev/null 2> /dev/null")
    run(ctx, "touch /dev/null ~/real.txt")
    assert ctx.effects is not None
    assert [Path(f["path"]).name for f in ctx.effects.view()["files"]] == ["real.txt"]
    assert ctx.effects.before_file(Path("/dev/null"), via="write_file") is False
    assert all(is_special_path(Path(p)) for p in ("/dev/null", "/proc/1/status", "/sys/kernel"))
    assert not is_special_path(Path("/devices/x"))
    assert not is_special_path(Path("/"))


# --- reading the command text ---------------------------------------------------


def test_a_script_is_read_line_by_line_with_quotes_comments_and_continuations(
    home: Path, folder: Path
) -> None:
    small_tree(home)
    script = (
        "# set up\n"
        'echo "a \\" quoted\nline" # not a command: rm -rf ~/nothing\n'
        "BASE=~/hp1 \\\n"
        "  \n"
        "echo a\\ b\n"
        "rm -rf $BASE/sub\n"
    )
    plan = plan_command(script, folder)
    assert plan.targets == [(home / "hp1" / "sub").resolve()]
    assert plan.unresolved == []


def test_a_tilde_user_and_a_brace_list_are_not_guessed_at(home: Path, folder: Path) -> None:
    plan = plan_command("rm -rf ~other/x {a,b}/c", folder)
    assert len(plan.unresolved) == 2
    assert "another user's home" in plan.unresolved[0]
    assert "brace expansion" in plan.unresolved[1]


def test_a_cd_that_cannot_be_read_makes_later_relative_names_unreadable(
    home: Path, folder: Path
) -> None:
    (home / "hp1").mkdir()
    for moved in ("cd -", "pushd ~/hp1", "cd $ELSEWHERE"):
        plan = plan_command(f"{moved} && rm -rf sub", folder)
        assert len(plan.unresolved) == 1, moved
    recovered = plan_command("cd $ELSEWHERE; cd ~/hp1 && rm -rf sub", folder)
    assert recovered.unresolved == []
    assert recovered.targets == [(home / "hp1" / "sub").resolve()]
    assert plan_command("cd && rm -rf hp1", folder).targets == [(home / "hp1").resolve()]


def test_input_redirects_and_a_shell_c_string_that_cannot_be_split_are_read(
    home: Path, folder: Path
) -> None:
    assert plan_command("sort < ~/in.txt | head -3", folder).read_only is True
    assert plan_command("cat <<< hello", folder).read_only is True
    plan = plan_command("bash -c 'echo \"unterminated'", folder)
    assert plan.opaque


def test_mv_with_a_target_directory_flag_names_that_directory(home: Path, folder: Path) -> None:
    small_tree(home)
    plan = plan_command("mv -t ~/hp1 ~/x", folder)
    assert (home / "hp1").resolve() in plan.targets


def test_a_tree_of_two_thousand_files_is_too_many_to_back_up(
    tmp_path: Path, home: Path, folder: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("saddle.sideeffects.MAX_WATCHED", 5)
    tree = big_tree(home, count=6)
    reasons = SideEffects(tmp_path / "rec").hold(f"rm -rf {tree}", folder)
    assert reasons == [f"{tree} holds 5 or more files, too many to back up"]


def test_a_target_that_cannot_be_read_holds_the_command(
    tmp_path: Path, home: Path, folder: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    tree = small_tree(home)

    def refuse(*_a: object, **_k: object) -> None:
        raise PermissionError

    monkeypatch.setattr(SideEffects, "_walk", staticmethod(refuse))
    (reason,) = SideEffects(tmp_path / "rec").hold(f"rm -rf {tree}", folder)
    assert "could not be read to back it up (PermissionError)" in reason


def test_the_single_target_destroyers_name_their_arguments(home: Path, folder: Path) -> None:
    small_tree(home)
    for command in ("rmdir ~/hp1/sub", "shred ~/hp1/a.txt", "truncate -s 0 ~/hp1/a.txt"):
        assert (home / "hp1").resolve() in [
            p.parent.resolve() for p in plan_command(command, folder).targets
        ], command


def test_home_is_expanded_in_every_spelling_for_the_file_tools(home: Path) -> None:
    assert expand_home("~/x") == f"{home}/x"
    assert expand_home("$HOME/x") == f"{home}/x"
    assert expand_home("${HOME}/x") == f"{home}/x"
    assert expand_home("/etc/x") == "/etc/x"
