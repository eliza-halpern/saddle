"""The browser reaper's /proc reading survives any process name.

Contract: `descendants` finds every process whose parent chain reaches a pid,
whatever bytes a process name holds. Why: a process name that is not UTF-8
made the reaper raise at teardown under load, and saddle's own audit counted
it as a failing test of a correct change.
"""

from __future__ import annotations

from pathlib import Path

from conftest import _drives_a_browser, descendants


def stat(proc: Path, pid: int, comm: bytes, ppid: int) -> None:
    (proc / str(pid)).mkdir()
    (proc / str(pid) / "stat").write_bytes(b"%d (%s) S %d 1 1 0" % (pid, comm, ppid))


def test_descendants_reads_names_that_are_not_utf8_or_hold_parentheses(tmp_path: Path) -> None:
    stat(tmp_path, 10, b"pytest", 1)
    stat(tmp_path, 11, b"chrome \xc7 (gpu)", 10)
    stat(tmp_path, 12, b") 99 (", 11)
    stat(tmp_path, 13, b"other", 1)
    (tmp_path / "self").mkdir()
    assert sorted(descendants(10, tmp_path)) == [11, 12]


def test_a_module_on_the_chrome_helper_is_reaped_too(tmp_path: Path) -> None:
    helper = tmp_path / "test_new_ui.py"
    helper.write_text("from chrome_page import drive_page\n")
    driver = tmp_path / "test_old_ui.py"
    driver.write_text("CDP = 'fixtures/a_cdp.mjs'\n")
    plain = tmp_path / "test_plain.py"
    plain.write_text("import json\n")
    assert _drives_a_browser(str(helper))
    assert _drives_a_browser(str(driver))
    assert not _drives_a_browser(str(plain))
