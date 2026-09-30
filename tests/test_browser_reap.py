"""The browser-test reaper in `conftest.py`: a process a test orphans is found and killed.

Known-good: a grandchild whose parent has exited (the shape a Chrome is left
in when its `node` driver is killed on a timeout) is still a descendant of this
worker, because the worker is a subreaper, and killing the descendants ends it.
Known-bad: a process that is not ours (this worker's own parent) is never
listed.
"""

from __future__ import annotations

import os
import subprocess

from conftest import _subreaper, descendants


def _alive(pid: int) -> bool:
    try:
        with open(f"/proc/{pid}/stat") as handle:
            return handle.read().rsplit(")", 1)[1].split()[0] != "Z"
    except OSError:
        return False


def test_an_orphaned_grandchild_stays_ours_and_is_reaped() -> None:
    assert _subreaper()
    # sh starts a long sleep in the background, prints its pid and exits at
    # once: the sleep is orphaned, as a Chrome is when its driver is killed.
    out = subprocess.run(
        ["sh", "-c", "sleep 300 >/dev/null 2>&1 & echo $!"],
        capture_output=True,
        text=True,
        check=True,
    )
    orphan = int(out.stdout.strip())
    try:
        assert orphan in descendants(os.getpid())
    finally:
        os.kill(orphan, 9)
    # Only a parent can reap: this raises ChildProcessError if the orphan had
    # gone to init instead of to this worker.
    os.waitpid(orphan, 0)
    assert not _alive(orphan)


def test_a_process_that_is_not_ours_is_never_listed() -> None:
    assert os.getppid() not in descendants(os.getpid())
    assert os.getpid() not in descendants(os.getpid())
