"""The side-effect record: backups, the record view, Undo, and command reading (#137).

Known-good: a config file changed outside the folder is backed up first, shows
as changed, and Undo puts back its exact bytes; a download shows with its host
and size; a file the session created shows as created and is removed only with
the person's confirmation.

Known-bad: a command whose effects cannot be read from its words (a script, a
path through a variable, an unparseable line) appears under "not tracked" with
its reason, never nowhere; a path the session left alone appears as nothing,
and its stale backup is not left to overwrite later work.
"""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from saddle import sideeffects
from saddle.sideeffects import LIMITS, SideEffects, plan_command


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


def test_a_changed_config_is_backed_up_shown_and_restored_byte_for_byte(
    tmp_path: Path, home: Path
) -> None:
    config = home / "app.conf"
    original = b"colour=blue\r\n\xff\xfe binary-ish\n"
    config.write_bytes(original)
    record = SideEffects(tmp_path / "rec")
    assert record.before_file(config, via="edit_file") is True
    config.write_bytes(b"colour=red\n")
    view = record.view()
    assert [(f["path"], f["change"], f["backup"]) for f in view["files"]] == [
        (str(config), "changed", "backed up")
    ]
    assert view["can_restore"] == 1
    done = record.undo(delete_created=False)
    assert done["restored"] == [str(config)]
    assert config.read_bytes() == original
    assert record.view()["files"] == []
    assert record.view()["undone"][0]["restored"] == [str(config)]


def test_the_first_original_is_the_one_kept(tmp_path: Path, home: Path) -> None:
    target = home / "a.txt"
    target.write_text("one")
    record = SideEffects(tmp_path / "rec")
    assert record.before_file(target, via="write_file")
    target.write_text("two")
    assert record.before_file(target, via="write_file") is False
    target.write_text("three")
    record.undo(delete_created=False)
    assert target.read_text() == "one"


def test_a_deleted_file_is_listed_deleted_and_comes_back(tmp_path: Path, home: Path) -> None:
    target = home / "gone.txt"
    target.write_text("keep me")
    record = SideEffects(tmp_path / "rec")
    record.before_file(target, via="run_command", command="rm ~/gone.txt")
    target.unlink()
    assert record.view()["files"][0]["change"] == "deleted"
    record.undo(delete_created=False)
    assert target.read_text() == "keep me"


def test_a_created_file_is_removed_only_when_the_person_confirms(
    tmp_path: Path, home: Path
) -> None:
    target = home / "new.txt"
    record = SideEffects(tmp_path / "rec")
    record.before_file(target, via="write_file")
    target.write_text("made by the session")
    view = record.view()
    assert view["files"][0]["change"] == "created"
    assert (view["can_restore"], view["can_delete"]) == (0, 1)
    kept = record.undo(delete_created=False)
    assert (kept["kept"], kept["deleted"]) == ([str(target)], [])
    assert target.exists()
    gone = record.undo(delete_created=True)
    assert gone["deleted"] == [str(target)]
    assert not target.exists()


def test_a_path_the_session_left_alone_shows_nothing_and_keeps_no_backup(
    tmp_path: Path, home: Path
) -> None:
    target = home / "same.txt"
    target.write_text("same")
    record = SideEffects(tmp_path / "rec")
    record.before_file(target, via="write_file")
    target.write_text("same")  # written, but to the same bytes
    assert record.view()["files"] == []
    record.settle_files({target})
    assert not list((tmp_path / "rec" / "blobs").iterdir())
    # the person edits it afterwards; a later Undo must not touch it
    target.write_text("the person's edit")
    record.undo(delete_created=True)
    assert target.read_text() == "the person's edit"


def test_a_file_too_big_to_back_up_is_listed_and_undo_says_it_cannot(
    tmp_path: Path, home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(sideeffects, "MAX_FILE_BYTES", 4)
    target = home / "big.bin"
    target.write_bytes(b"0123456789")
    record = SideEffects(tmp_path / "rec")
    record.before_file(target, via="write_file")
    target.write_bytes(b"x")
    view = record.view()
    assert view["files"][0]["backup"] == "too large to back up"
    assert view["can_restore"] == 0
    assert record.undo(delete_created=False)["failed"] == [str(target)]
    assert target.read_bytes() == b"x"


def test_a_created_directory_is_removed_with_its_files_when_confirmed(
    tmp_path: Path, home: Path
) -> None:
    app = home / "apps" / "demo"
    record = SideEffects(tmp_path / "rec")
    watch = record.watch(f"mkdir -p {app} && echo hi > {app}/run.sh", tmp_path / "w")
    (app).mkdir(parents=True)
    (app / "run.sh").write_text("hi")
    record.settle(watch, exit_code=0)
    paths = {f["path"] for f in record.view()["files"]}
    assert str(app / "run.sh") in paths
    record.undo(delete_created=True)
    assert not (app / "run.sh").exists()
    assert not app.exists()
    assert (home / "apps").exists()  # a directory the session did not create stays


def test_a_file_that_cannot_be_copied_is_recorded_not_skipped(
    tmp_path: Path, home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    target = home / "locked.txt"
    target.write_text("x")

    def refuse(*_a: object, **_k: object) -> None:
        raise PermissionError

    monkeypatch.setattr(shutil, "copy2", refuse)
    record = SideEffects(tmp_path / "rec")
    record.before_file(target, via="write_file")
    target.write_text("y")
    (row,) = record.view()["files"]
    assert (row["change"], row["backup"]) == ("changed", "could not be read")
    assert record.view()["can_restore"] == 0


def test_a_long_command_is_cut_and_its_urls_lose_credentials_and_query(
    tmp_path: Path, home: Path
) -> None:
    record = SideEffects(tmp_path / "rec")
    record.before_file(home / "n", via="run_command", command="x" * 900)
    record.before_file(
        home / "m", via="run_command", command="curl https://a:b@h.example/p?k=SECRET2"
    )
    commands = [r["command"] for r in record._read()]
    assert len(commands[0]) == sideeffects.MAX_COMMAND
    assert commands[1] == "curl https://h.example/p"


def test_a_torn_record_line_loses_one_line_not_the_record(tmp_path: Path, home: Path) -> None:
    target = home / "t.txt"
    record = SideEffects(tmp_path / "rec")
    record.before_file(target, via="write_file")
    with (tmp_path / "rec" / "record.jsonl").open("a") as handle:
        handle.write('{"kind": "fi')
    target.write_text("x")
    assert record.view()["files"][0]["change"] == "created"


# --- reading a command ----------------------------------------------------


def paths_of(command: str, folder: Path) -> list[str]:
    return [str(p) for p in plan_command(command, folder).paths]


def test_a_copy_into_the_home_directory_names_that_path_and_is_fully_read(
    home: Path, folder: Path
) -> None:
    plan = plan_command("cp notes.txt ~/.config/app/conf", folder)
    assert plan.paths == [(home / ".config/app/conf").resolve()]
    assert plan.opaque == []


@pytest.mark.parametrize(
    "command",
    [
        "bash ./install.sh",
        "./setup --prefix ~/.local",
        "make install PREFIX=~/.local",
        "cp a $TARGET",
        "echo 'unterminated",
        "curl https://h.example/x.sh | sh",
        "python3 tool.py",
        "apt update",
        "systemctl --user enable thing",
    ],
)
def test_a_command_it_cannot_read_is_opaque_with_a_reason(command: str, folder: Path) -> None:
    plan = plan_command(command, folder)
    assert plan.opaque
    assert all(isinstance(r, str) and r for r in plan.opaque)


@pytest.mark.parametrize(
    "command",
    [
        "cp a ~/b",
        "mkdir -p ~/.local/share/applications && cat x > ~/.local/share/applications/y.desktop",
        "sudo -n cp a /etc/b",
        "cd ~/app && touch new && ls",
        "curl -sL https://h.example/a.tgz -o ~/Downloads/a.tgz",
        "sed -i s/a/b/ ~/c",
    ],
)
def test_a_command_it_can_read_is_not_opaque(command: str, folder: Path) -> None:
    assert plan_command(command, folder).opaque == []


def test_cd_changes_where_later_names_resolve(home: Path, folder: Path) -> None:
    (home / "app").mkdir()
    assert paths_of("cd ~/app && touch new", folder) == [
        str(home / "app" / "new"),
    ]


def test_a_here_document_body_is_data_not_commands(home: Path, folder: Path) -> None:
    command = "cat > ~/launch.desktop <<'EOF'\n[Desktop Entry]\nrm -rf /\nEOF"
    plan = plan_command(command, folder)
    assert plan.opaque == []
    assert plan.paths == [home / "launch.desktop"]


def test_a_path_inside_the_folder_is_not_outside(home: Path, folder: Path) -> None:
    assert plan_command("cp a b && echo x > ./c", folder).paths == []


def test_a_sed_that_only_reads_names_no_writes_but_a_redirect_does(
    home: Path, folder: Path
) -> None:
    assert plan_command("sed -n 1p ~/c", folder).paths == []
    assert plan_command("sed -n 1p ~/c > ~/d", folder).paths == [home / "d"]


def test_a_glob_names_what_it_matches(home: Path, folder: Path) -> None:
    (home / "a.log").write_text("")
    (home / "b.log").write_text("")
    assert paths_of("rm ~/*.log", folder) == [str(home / "a.log"), str(home / "b.log")]


@pytest.mark.parametrize(
    ("command", "manager", "action", "packages"),
    [
        ("sudo apt-get install -y foo bar", "apt-get", "install", ["foo", "bar"]),
        ("apt remove foo", "apt", "remove", ["foo"]),
        ("dnf install -y baz", "dnf", "install", ["baz"]),
        ("sudo pacman -S qux", "pacman", "install", ["qux"]),
        ("pacman -Rns qux", "pacman", "remove", ["qux"]),
        ("flatpak install flathub org.x.App", "flatpak", "install", ["flathub", "org.x.App"]),
        ("pip install --user thing", "pip", "install", ["thing"]),
        ("python3 -m pip uninstall -y thing", "pip", "remove", ["thing"]),
        ("npm install -g tool", "npm", "install", ["tool"]),
    ],
)
def test_a_package_manager_call_is_read_into_manager_action_and_packages(
    command: str, manager: str, action: str, packages: list[str], folder: Path
) -> None:
    assert plan_command(command, folder).packages == [
        {"manager": manager, "action": action, "packages": packages}
    ]


def test_npm_without_global_is_the_folders_own_business(folder: Path) -> None:
    assert plan_command("npm install left-pad", folder).packages == []


# --- downloads ------------------------------------------------------------


def test_a_download_shows_its_host_and_size_and_drops_credentials_and_query(
    tmp_path: Path, home: Path, folder: Path
) -> None:
    record = SideEffects(tmp_path / "rec")
    command = f"curl -L 'https://user:pw@dl.example.org/pkg/a.tar.gz?token=SECRET' -o {home}/a.tgz"
    watch = record.watch(command, folder)
    (home / "a.tgz").write_bytes(b"x" * 1234)
    record.settle(watch, exit_code=0)
    (download,) = record.view()["downloads"]
    assert download["source"] == "https://dl.example.org/pkg/a.tar.gz"
    assert (download["host"], download["size"], download["path"]) == (
        "dl.example.org",
        1234,
        str(home / "a.tgz"),
    )
    assert "SECRET" not in str(record.view())
    assert "pw" not in download["source"]
    assert record.view()["files"][0]["change"] == "created"


@pytest.mark.parametrize(
    ("command", "name"),
    [
        ("curl -O https://h.example/p/x.zip", "x.zip"),
        ("wget https://h.example/p/y.zip", "y.zip"),
        ("wget -P ~/dl https://h.example/p/z.zip", "z.zip"),
        ("curl https://h.example/p/", None),
    ],
)
def test_where_a_download_lands_is_read_from_the_flags(
    command: str, name: str | None, folder: Path, home: Path
) -> None:
    (found,) = plan_command(command, folder).downloads
    assert (found[1].name if found[1] else None) == name


# --- commands: watching them ---------------------------------------------


def test_a_command_that_edits_a_named_config_is_backed_up_before_and_restorable(
    tmp_path: Path, home: Path, folder: Path
) -> None:
    config = home / ".config" / "app.toml"
    config.parent.mkdir()
    config.write_bytes(b"a = 1\n")
    record = SideEffects(tmp_path / "rec")
    watch = record.watch(f"sed -i s/1/2/ {config}", folder)
    config.write_bytes(b"a = 2\n")
    record.settle(watch, exit_code=0)
    (row,) = record.view()["files"]
    assert (row["change"], row["via"]) == ("changed", "run_command")
    record.undo(delete_created=False)
    assert config.read_bytes() == b"a = 1\n"


def test_a_command_that_changes_nothing_it_names_leaves_no_trace(
    tmp_path: Path, home: Path, folder: Path
) -> None:
    (home / "r.txt").write_text("r")
    record = SideEffects(tmp_path / "rec")
    watch = record.watch(f"cat {home}/r.txt", folder)
    record.settle(watch, exit_code=0)
    assert record.view()["empty"] is True
    assert record._read() == []


def test_files_a_command_creates_in_a_named_directory_are_found(
    tmp_path: Path, home: Path, folder: Path
) -> None:
    out = home / "unpacked"
    out.mkdir()
    (out / "old.txt").write_text("old")
    record = SideEffects(tmp_path / "rec")
    watch = record.watch(f"tar -xf a.tar -C {out}", folder)
    (out / "fresh.txt").write_text("fresh")
    (out / "old.txt").write_text("changed")
    record.settle(watch, exit_code=0)
    changes = {Path(f["path"]).name: f["change"] for f in record.view()["files"]}
    assert changes == {"fresh.txt": "created", "old.txt": "changed"}


def test_an_unattributable_command_is_listed_not_tracked_with_its_reason(
    tmp_path: Path, home: Path, folder: Path
) -> None:
    record = SideEffects(tmp_path / "rec")
    watch = record.watch("bash ./install.sh --prefix ~/.local", folder)
    record.settle(watch, exit_code=0)
    view = record.view()
    (row,) = view["not_tracked"]
    assert row["command"] == "bash ./install.sh --prefix ~/.local"
    assert "bash" in row["reasons"][0]
    assert view["empty"] is False
    assert view["limits"] == LIMITS


def test_a_background_command_is_listed_not_tracked_even_when_it_is_fully_read(
    tmp_path: Path, home: Path, folder: Path
) -> None:
    record = SideEffects(tmp_path / "rec")
    watch = record.watch(f"cp a {home}/b", folder)
    record.settle(watch, exit_code=None, background=True)
    (row,) = record.view()["not_tracked"]
    assert "background" in row["reasons"][0]


def test_a_package_install_is_listed_with_its_note_and_is_not_undoable(
    tmp_path: Path, home: Path, folder: Path
) -> None:
    record = SideEffects(tmp_path / "rec")
    watch = record.watch("sudo apt-get install -y foo", folder)
    record.settle(watch, exit_code=0)
    view = record.view()
    assert [(p["manager"], p["action"], p["packages"]) for p in view["packages"]] == [
        ("apt-get", "install", ["foo"])
    ]
    assert "not listed" in view["not_tracked"][0]["reasons"][0]
    assert record.undo(delete_created=True) == {
        "restored": [],
        "deleted": [],
        "kept": [],
        "failed": [],
    }


# --- the odd corners, each with the instance that reaches it ---------------


def test_home_is_read_in_every_spelling(home: Path, folder: Path) -> None:
    for spelling in ("~", "~/x", "$HOME/x", "${HOME}/x"):
        assert plan_command(f"touch {spelling}", folder).paths, spelling
    assert plan_command("touch ~", folder).paths == [home]


@pytest.mark.parametrize(
    ("command", "name"),
    [
        ("curl --output ~/a.bin https://h.example/a", "a.bin"),
        ("curl --output=~/b.bin https://h.example/a", "b.bin"),
        ("wget -O ~/c.bin https://h.example/a", "c.bin"),
        ("wget --output-document=~/d.bin https://h.example/a", "d.bin"),
        ("wget --directory-prefix=~/dl https://h.example/e.bin", "e.bin"),
        ("wget --directory-prefix ~/dl https://h.example/f.bin", "f.bin"),
    ],
)
def test_the_long_flag_spellings_of_a_download_are_read_too(
    command: str, name: str, home: Path, folder: Path
) -> None:
    plan = plan_command(command, folder)
    (found,) = plan.downloads
    assert found[1] is not None
    assert found[1].name == name
    assert found[1] in plan.paths


@pytest.mark.parametrize(
    "command",
    [
        "apt list --installed",
        "apt",
        "pip install --target ~/vendor thing",
        "python3 -m pip install -r requirements.txt",
        "pacman -Q",
    ],
)
def test_a_package_manager_call_that_names_no_package_is_not_a_package_record(
    command: str, folder: Path
) -> None:
    assert plan_command(command, folder).packages == []


def test_a_package_call_with_nothing_to_read_is_still_opaque_unless_bare(folder: Path) -> None:
    assert plan_command("apt list", folder).opaque
    assert plan_command("pip install -r requirements.txt", folder).opaque
    assert plan_command("python3 -m pip install -r requirements.txt", folder).opaque
    assert plan_command("apt", folder).opaque == []


def test_dash_c_and_dash_d_name_the_directory_a_tool_writes_into(home: Path, folder: Path) -> None:
    assert plan_command("unzip a.zip -d ~/z", folder).paths == [home / "z"]
    assert plan_command("tar --directory=~/t -xf a.tar", folder).paths == [home / "t"]


def test_a_sudo_wrapper_with_flags_is_looked_through(home: Path, folder: Path) -> None:
    plan = plan_command("sudo -n -u root cp a ~/b", folder)
    assert plan.paths == [home / "b"]


def test_a_command_that_is_only_an_assignment_names_nothing(folder: Path) -> None:
    plan = plan_command("A=1", folder)
    assert (plan.paths, plan.opaque) == ([], [])


def test_an_opaque_programs_named_paths_are_still_watched(home: Path, folder: Path) -> None:
    (home / "target").write_text("x")
    plan = plan_command("./install.sh ~/target --force", folder)
    assert plan.paths == [home / "target"]
    assert plan.opaque


def test_a_directory_with_too_many_entries_says_it_was_only_partly_watched(
    tmp_path: Path, home: Path, folder: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(sideeffects, "MAX_WATCHED", 3)
    big = home / "big"
    big.mkdir()
    for index in range(5):
        (big / f"{index}.txt").write_text("x")
    record = SideEffects(tmp_path / "rec")
    watch = record.watch(f"rm -r {big}", folder)
    record.settle(watch, exit_code=0)
    (row,) = record.view()["not_tracked"]
    assert "only some were watched" in row["reasons"][0]


def test_a_directory_walk_stops_at_the_cap(
    tmp_path: Path, home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(sideeffects, "MAX_WATCHED", 2)
    for index in range(4):
        (home / f"{index}.txt").write_text("x")
    assert len(SideEffects._walk(home)) == 2


def test_a_file_that_vanishes_before_it_is_read_does_not_break_the_record(
    tmp_path: Path, home: Path
) -> None:
    ghost = home / "ghost.txt"
    record = SideEffects(tmp_path / "rec")
    record.before_file(ghost, via="write_file")
    record.settle_files({ghost})
    assert record._read() == []


def test_a_restore_that_cannot_write_is_reported_failed(
    tmp_path: Path, home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    target = home / "f.txt"
    target.write_text("old")
    record = SideEffects(tmp_path / "rec")
    record.before_file(target, via="write_file")
    target.write_text("new")

    def refuse(*_a: object, **_k: object) -> None:
        raise OSError

    monkeypatch.setattr(shutil, "copy2", refuse)
    assert record.undo(delete_created=False)["failed"] == [str(target)]
    assert target.read_text() == "new"


def test_a_directory_the_session_made_that_is_not_empty_is_kept_and_said(
    tmp_path: Path, home: Path, folder: Path
) -> None:
    app = home / "keepdir"
    record = SideEffects(tmp_path / "rec")
    watch = record.watch(f"mkdir {app}", folder)
    app.mkdir()
    (app / "theirs.txt").write_text("the person's file, made later")
    record.settle(watch, exit_code=0)
    (record.root / "record.jsonl").write_text(
        "\n".join(
            r for r in (record.root / "record.jsonl").read_text().splitlines() if "theirs" not in r
        )
        + "\n"
    )
    done = record.undo(delete_created=True)
    assert done["kept"] == [str(app)]
    assert (app / "theirs.txt").exists()


def test_a_command_with_no_url_downloads_nothing(folder: Path) -> None:
    assert plan_command("curl --version", folder).downloads == []


def test_a_download_to_the_terminal_has_a_source_and_no_file(
    tmp_path: Path, home: Path, folder: Path
) -> None:
    record = SideEffects(tmp_path / "rec")
    watch = record.watch("curl -s https://h.example/p/", folder)
    record.settle(watch, exit_code=0)
    (download,) = record.view()["downloads"]
    assert (download["path"], download["size"]) == (None, None)


def test_the_same_reason_is_given_once_however_often_the_program_runs(folder: Path) -> None:
    assert len(plan_command("bash a; bash b && bash c", folder).opaque) == 1


def test_an_opaque_program_with_a_variable_argument_says_it_once_not_per_word(
    home: Path, folder: Path
) -> None:
    plan = plan_command("./tool $X ~/y", folder)
    assert plan.paths == [home / "y"]
    assert len(plan.opaque) == 1


def test_a_known_reader_names_no_paths(home: Path, folder: Path) -> None:
    assert plan_command("grep needle ~/f", folder).paths == []


def test_a_flag_at_the_end_with_no_value_names_nothing(folder: Path) -> None:
    assert plan_command("tar -xf a.tar -C", folder).paths == []


def test_a_lost_backup_reads_as_changed_not_as_a_crash(tmp_path: Path, home: Path) -> None:
    target = home / "f.txt"
    target.write_text("old")
    record = SideEffects(tmp_path / "rec")
    record.before_file(target, via="write_file")
    for blob in (tmp_path / "rec" / "blobs").iterdir():
        blob.unlink()
    assert record.view()["files"][0]["change"] == "changed"


def test_a_record_for_a_file_the_session_never_changed_is_not_undone(
    tmp_path: Path, home: Path
) -> None:
    target = home / "f.txt"
    target.write_text("old")
    record = SideEffects(tmp_path / "rec")
    record.before_file(target, via="write_file")
    target.write_text("the person's later edit")
    (
        tmp_path / "rec" / "blobs" / next(p.name for p in (tmp_path / "rec" / "blobs").iterdir())
    ).write_text("the person's later edit")
    assert record.view()["files"] == []
    assert record.undo(delete_created=True) == {
        "restored": [],
        "deleted": [],
        "kept": [],
        "failed": [],
    }
    assert target.read_text() == "the person's later edit"


def test_files_past_the_backup_cap_are_still_watched_and_say_they_are_not_restorable(
    tmp_path: Path, home: Path, folder: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(sideeffects, "MAX_BACKUPS", 1)
    big = home / "many"
    big.mkdir()
    for name in ("a", "b", "c"):
        (big / name).write_text("x")
    record = SideEffects(tmp_path / "rec")
    watch = record.watch(f"tar -xf a.tar -C {big}", folder)
    for name in ("a", "b", "c"):
        (big / name).write_text("yy")
    record.settle(watch, exit_code=0)
    view = record.view()
    assert len(view["files"]) == 3
    assert [f["backup"] for f in view["files"]].count("backed up") == 1
    assert view["can_restore"] == 1
    # a file past the cap that was not changed shows nothing
    watch = SideEffects(tmp_path / "rec2").watch(f"cat {big}/a", folder)
    assert watch.plan.opaque == []
