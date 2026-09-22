"""Rewinding a conversation puts the files back.

The promise is narrow on purpose and the tests say where it stops: writes
made through the file tools come back, and anything a shell command did does
not. A rewind that quietly left half the previous attempt on disk would be
worse than one that never claimed to clean up.
"""

from __future__ import annotations

from pathlib import Path

from saddle.undo import UndoLog


def _log(tmp_path: Path) -> tuple[UndoLog, Path]:
    work = tmp_path / "work"
    work.mkdir()
    return UndoLog(tmp_path / "undo"), work


def test_a_file_a_turn_changed_comes_back(tmp_path: Path) -> None:
    log, work = _log(tmp_path)
    target = work / "a.py"
    target.write_text("original\n")

    log.begin(turn=1, start_index=0)
    log.before_write(target)
    target.write_text("the turn's version\n")

    result = log.restore_to(0)
    assert target.read_text() == "original\n"
    assert result.reverted == [str(target)]
    assert result.deleted == []


def test_a_file_a_turn_created_is_deleted_again(tmp_path: Path) -> None:
    log, work = _log(tmp_path)
    target = work / "new.py"

    log.begin(turn=1, start_index=0)
    log.before_write(target)          # recorded as "did not exist"
    target.write_text("invented\n")

    result = log.restore_to(0)
    assert not target.exists()
    assert result.deleted == [str(target)]


def test_repeated_edits_rewind_to_before_the_first(tmp_path: Path) -> None:
    # Once per turn, not once per write: a turn that edits a file four times
    # must come back to what was there before the first edit.
    log, work = _log(tmp_path)
    target = work / "a.py"
    target.write_text("v0\n")

    log.begin(turn=1, start_index=0)
    for version in ("v1", "v2", "v3", "v4"):
        log.before_write(target)
        target.write_text(version + "\n")

    log.restore_to(0)
    assert target.read_text() == "v0\n"


def test_only_turns_at_or_after_the_rewind_point_are_undone(tmp_path: Path) -> None:
    log, work = _log(tmp_path)
    kept, undone = work / "kept.py", work / "undone.py"
    kept.write_text("before turn 1\n")
    undone.write_text("before turn 2\n")

    log.begin(turn=1, start_index=0)
    log.before_write(kept)
    kept.write_text("turn 1 wrote this\n")

    log.begin(turn=2, start_index=4)
    log.before_write(undone)
    undone.write_text("turn 2 wrote this\n")

    log.restore_to(4)
    assert kept.read_text() == "turn 1 wrote this\n"     # earlier turn survives
    assert undone.read_text() == "before turn 2\n"


def test_a_file_several_turns_touched_lands_on_the_oldest_snapshot(
    tmp_path: Path,
) -> None:
    log, work = _log(tmp_path)
    target = work / "a.py"
    target.write_text("v0\n")

    for turn, start in ((1, 0), (2, 4), (3, 8)):
        log.begin(turn=turn, start_index=start)
        log.before_write(target)
        target.write_text(f"turn {turn}\n")

    log.restore_to(4)                       # undo turns 2 and 3
    assert target.read_text() == "turn 1\n"

    log.restore_to(0)                       # then turn 1 as well
    assert target.read_text() == "v0\n"


def test_rewinding_twice_does_not_restore_twice(tmp_path: Path) -> None:
    # The second rewind must be a no-op, not a second restore of a snapshot
    # that no longer describes anything.
    log, work = _log(tmp_path)
    target = work / "a.py"
    target.write_text("original\n")

    log.begin(turn=1, start_index=0)
    log.before_write(target)
    target.write_text("changed\n")

    assert log.restore_to(0).touched == 1
    target.write_text("what the user typed after the rewind\n")
    assert log.restore_to(0).touched == 0
    assert target.read_text() == "what the user typed after the rewind\n"


def test_a_write_outside_any_turn_is_not_recorded(tmp_path: Path) -> None:
    log, work = _log(tmp_path)
    target = work / "a.py"
    target.write_text("original\n")
    log.before_write(target)               # begin() was never called
    target.write_text("changed\n")
    assert log.restore_to(0).touched == 0


def test_an_unreadable_file_is_skipped_rather_than_failing_the_tool(
    tmp_path: Path,
) -> None:
    log, work = _log(tmp_path)
    target = work / "locked.py"
    target.write_text("secret\n")
    target.chmod(0o000)
    try:
        log.begin(turn=1, start_index=0)
        log.before_write(target)           # cannot be copied
    finally:
        target.chmod(0o644)
    # Nothing was promised, so nothing is claimed.
    assert log.restore_to(0).touched == 0


def test_a_missing_log_rewinds_to_nothing(tmp_path: Path) -> None:
    assert UndoLog(tmp_path / "never-written").restore_to(0).touched == 0


def test_a_torn_record_loses_one_line_not_the_log(tmp_path: Path) -> None:
    log, work = _log(tmp_path)
    target = work / "a.py"
    target.write_text("original\n")
    log.begin(turn=1, start_index=0)
    log.before_write(target)
    target.write_text("changed\n")

    with (log.root / "turns.jsonl").open("a", encoding="utf-8") as handle:
        handle.write("{half a record\n")

    assert log.restore_to(0).touched == 1
    assert target.read_text() == "original\n"


def test_a_restore_that_cannot_write_is_reported_not_swallowed(
    tmp_path: Path,
) -> None:
    log, work = _log(tmp_path)
    target = work / "a.py"
    target.write_text("original\n")

    log.begin(turn=1, start_index=0)
    log.before_write(target)
    target.write_text("changed\n")
    # Read-only on the file itself: a directory's write bit governs creating
    # and removing entries, not overwriting one that is already there.
    target.chmod(0o444)
    try:
        result = log.restore_to(0)
    finally:
        target.chmod(0o644)
    assert result.failed == [str(target)]
    assert result.reverted == []


# -- warning before touching anything -----------------------------------------

def test_a_rewind_can_be_previewed_without_happening(tmp_path: Path) -> None:
    # Rewinding edits the user's working directory. Doing that silently is
    # not acceptable, so the UI asks first -- and to ask, it has to know
    # which files before acting.
    log, work = _log(tmp_path)
    changed, created = work / "changed.py", work / "created.py"
    changed.write_text("original\n")

    log.begin(turn=1, start_index=0)
    log.before_write(changed)
    changed.write_text("turn wrote this\n")
    log.before_write(created)
    created.write_text("invented\n")

    preview = log.pending(0)
    assert preview.reverted == [str(changed)]
    assert preview.deleted == [str(created)]
    assert preview.touched == 2

    # ...and nothing moved.
    assert changed.read_text() == "turn wrote this\n"
    assert created.exists()

    # The preview matches what actually happens.
    done = log.restore_to(0)
    assert (done.reverted, done.deleted) == (preview.reverted, preview.deleted)


def test_a_preview_of_a_turn_that_touched_nothing_is_empty(tmp_path: Path) -> None:
    log, _work = _log(tmp_path)
    log.begin(turn=1, start_index=0)
    assert log.pending(0).touched == 0


def test_a_preview_only_covers_the_turns_being_undone(tmp_path: Path) -> None:
    log, work = _log(tmp_path)
    early, late = work / "early.py", work / "late.py"
    early.write_text("e\n")
    late.write_text("l\n")

    log.begin(turn=1, start_index=0)
    log.before_write(early)
    early.write_text("changed\n")
    log.begin(turn=2, start_index=4)
    log.before_write(late)
    late.write_text("changed\n")

    assert log.pending(4).reverted == [str(late)]


def test_a_file_edited_repeatedly_in_one_turn_is_copied_once(tmp_path: Path) -> None:
    """The once-per-turn guard is an optimisation, and this is what it buys.

    Removing it does not change what a rewind produces -- restores run
    newest-first and each overwrites the last, so the oldest snapshot wins
    either way. What it changes is cost: a turn that edits a 50MB file ten
    times would copy it ten times. So the property pinned here is the number
    of copies, not the final contents, which another test already covers.
    """
    log, work = _log(tmp_path)
    target = work / "big.py"
    target.write_text("v0\n")

    log.begin(turn=1, start_index=0)
    for version in range(6):
        log.before_write(target)
        target.write_text(f"v{version + 1}\n")

    blobs = list((log.root / "blobs").iterdir())
    assert len(blobs) == 1, f"{len(blobs)} copies of one file in one turn"

    # A later turn snapshots it again, because that is a different point.
    log.begin(turn=2, start_index=4)
    log.before_write(target)
    assert len(list((log.root / "blobs").iterdir())) == 2
