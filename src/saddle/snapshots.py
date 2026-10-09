"""Timed copies of a run's worktree, in the plain agent's snapshot format (#109 item 3).

A plain agent's run is copied at 30, 60, 90 and 120 minutes, so a run whose tree was
right before it was refused can still be scored: the copy at 120 minutes holds what the
tree held then. A saddle `auto` run had no such copy, so a refused `finish` left only the
final tree, and a correct-but-slow run read exactly like a wrong one. `saddle auto
--snapshot-marks` copies the tree at the same marks, in the same layout hashed the same
way, so the two kinds of run compare file for file.

A run with marks copies its worktree each time it is about to ask the model and once
again when it ends, for every mark its clock has reached that has no copy yet; a mark the
run does not live to is never taken. Elapsed seconds come from `AutoOptions.clock`, read
from the start of the run (`engine.RunBudget.elapsed`), and `AutoOptions.snapshot_marks_s`
is empty by default, which takes no copies at all.

Beside the run's journal, outside its worktree where the run's own tools cannot reach it:

    snapshots/m1800/tree/...    the copy of the worktree as it stood at 1800 seconds
    snapshots/m1800.json        its record: mark_s, taken_at_s, files, bytes,
                                content_sha256

`content_sha256` is sha256 over the copied files in sorted order of relative path, feeding
for each file its relative path as UTF-8, one zero byte, and the 32-byte sha256 digest of
that file's bytes. Every copy is also a journal span named `snapshot`
(`journal.SNAPSHOT_SPAN`) whose detail names the mark and its `content_sha256`, so the
hash of a tree the run no longer holds sits in the run's sealed chain and the journal
still verifies.
"""

from __future__ import annotations

import hashlib
import json
import os
from collections.abc import Iterator, Sequence
from dataclasses import dataclass, field
from fnmatch import fnmatch
from pathlib import Path
from typing import Final

from saddle.journal import SNAPSHOT_SPAN, append_span, build_span

SNAPSHOT_DIR: Final = "snapshots"
"""The folder of a run's timed copies, beside its journal: `<journal dir>/snapshots`."""

TREE_DIR: Final = "tree"
"""Inside one mark's folder, the name of the copy of the worktree: `m1800/tree`."""

MARK_PREFIX: Final = "m"
"""How one mark's folder and record are named after the seconds they stand for: `m1800`."""

SEPARATOR: Final = b"\x00"
"""What sits between one file's relative path and its digest in `content_sha256`."""

CHUNK_BYTES: Final = 1 << 20
"""How much of one file is read, hashed and written at a time."""

SKIP_NAMES: Final = (
    ".git",
    "__pycache__",
    ".pytest_cache",
    ".saddle",
    ".venv",
    "*.pyc",
    "*.egg-info",
    ".mutmut-cache",
    ".ruff_cache",
)
"""What a copy never carries: the shell-style patterns a plain agent's snapshotter skips
on, matched against every directory name and every file name along the way. `.git` matches
both the version store's directory and, in a linked worktree, the `gitdir:` file that
stands for it; `*.pyc` is the bytecode `__pycache__/` holds and `*.egg-info` what a built
package leaves behind, so those stay out even where no folder names them. Matched
case-sensitively, as a plain agent's patterns are."""


def mark_name(mark_s: int) -> str:
    """A mark's name in the layout: `m1800` for the 1800-second mark."""
    return f"{MARK_PREFIX}{mark_s}"


def skipped(name: str) -> bool:
    """Whether a directory or file called `name` is left out of a copy."""
    return any(fnmatch(name, pattern) for pattern in SKIP_NAMES)


def tree_files(root: Path) -> Iterator[Path]:
    """Every file under `root` a copy carries, in whatever order the tree presents them.

    A directory whose name is `skipped` is not descended into, so nothing under a `.venv`
    or a run's own `.saddle` state reaches the copy. `os.walk` does not follow a symlink to
    a directory, so nothing under one is copied; a symlink to a file is carried as that
    file's content, and an entry that is not a file at all -- a link whose target is gone --
    is left out rather than breaking the copy."""
    for here, dirs, names in os.walk(root):
        dirs[:] = sorted(d for d in dirs if not skipped(d))
        for name in names:
            if skipped(name):
                continue
            full = Path(here) / name
            if full.is_file():
                yield full


@dataclass(frozen=True)
class Snapshot:
    """One timed copy of a worktree: which mark it stands for and what it hashes to.

    `bytes_copied` is the record's `bytes` key: a plain agent names the total that, and
    `bytes` is a builtin here."""

    mark_s: int
    taken_at_s: float
    files: int
    bytes_copied: int
    content_sha256: str

    @property
    def name(self) -> str:
        """The copy's name in the layout: `m1800`."""
        return mark_name(self.mark_s)

    @property
    def detail(self) -> str:
        """The `snapshot` span's detail: the mark, when its copy was taken, its counts
        and, named, its `content_sha256`, so a reader of the ledger alone can tell which
        trees the run kept and what they held."""
        return (
            f"{self.name} taken at {self.taken_at_s:.1f}s: {self.files} file(s), "
            f"{self.bytes_copied} byte(s), content_sha256 {self.content_sha256}"
        )

    def record(self) -> dict[str, object]:
        """What `m1800.json` holds: a plain agent's five keys."""
        return {
            "mark_s": self.mark_s,
            "taken_at_s": self.taken_at_s,
            "files": self.files,
            "bytes": self.bytes_copied,
            "content_sha256": self.content_sha256,
        }


def copy_tree(src: Path, dest: Path) -> tuple[int, int, str]:
    """Copy `src` into `dest`, leaving out what `SKIP_NAMES` names.

    Returns (files copied, their total bytes, `content_sha256`). The hash is sha256 over
    the copied files in sorted order of relative path, feeding for each file its relative
    path as UTF-8, one zero byte, then the 32-byte sha256 digest of that file's bytes: two
    trees holding the same paths and the same bytes hash alike, and one changed byte makes
    a different hash. A `src` that carries no file copies nothing and hashes to the sha256
    of nothing."""
    root = src.resolve()
    parts = hashlib.sha256()
    files = 0
    size = 0
    for full in sorted(tree_files(root), key=lambda path: path.relative_to(root).as_posix()):
        rel = full.relative_to(root).as_posix()
        digest, written = _copy_one(full, dest / rel)
        parts.update(rel.encode("utf-8"))
        parts.update(SEPARATOR)
        parts.update(digest)
        files += 1
        size += written
    return files, size, parts.hexdigest()


def _copy_one(src: Path, dest: Path) -> tuple[bytes, int]:
    """Copy one file, making its parent folders; return (digest of its bytes, bytes)."""
    dest.parent.mkdir(parents=True, exist_ok=True)
    digest = hashlib.sha256()
    written = 0
    with src.open("rb") as read, dest.open("wb") as write:
        while True:
            chunk = read.read(CHUNK_BYTES)
            if not chunk:
                break
            digest.update(chunk)
            write.write(chunk)
            written += len(chunk)
    return digest.digest(), written


def write_record(folder: Path, snapshot: Snapshot) -> Path:
    """Write a copy's record as `folder/m1800.json` and return its path."""
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / f"{snapshot.name}.json"
    path.write_text(json.dumps(snapshot.record()), encoding="utf-8")
    return path


@dataclass
class Snapshots:
    """A run's marks, the copies it has already taken, and the spans they seal.

    `snapshot_due` is what an autonomous run calls before each request it makes to the
    model and once more when its loop ends: for every mark the elapsed seconds have
    reached that holds no copy yet, it copies the worktree to `snapshots/m1800/tree`,
    writes that copy's record beside it, and seals one `snapshot` span naming the mark and
    its `content_sha256`. A mark the run never reached is never taken, and a mark it has
    already taken is never taken again, however many times the run looks.

    A repeated mark is one mark (`1800,1800` gives copy `m1800` once), and marks are taken
    oldest first, so a single look that passes several marks copies them in order."""

    marks: Sequence[int]
    worktree: Path
    folder: Path
    journal: Path
    run_span: str
    node_id: str
    taken: list[Snapshot] = field(default_factory=list)

    def __post_init__(self) -> None:
        self.marks = sorted(set(self.marks))

    def taken_marks(self) -> list[int]:
        """The marks this run has copied, oldest first: a mark already here is not due."""
        return [snapshot.mark_s for snapshot in self.taken]

    def snapshot_due(self, elapsed_s: float) -> list[Snapshot]:
        """Copy the tree for each mark `elapsed_s` has reached that holds no copy yet.

        Returns the copies this call took, oldest mark first. `elapsed_s` is the run's own
        elapsed seconds -- `engine.RunBudget.elapsed`, which is `AutoOptions.clock` read
        from the run's start -- and each new copy's `taken_at_s` is that value. A mark
        `elapsed_s` has not reached is left for later, and one already copied is not
        copied again."""
        fresh: list[Snapshot] = []
        for mark in self.marks:
            if elapsed_s < mark or mark in self.taken_marks():
                continue
            snapshot = self._take(mark, elapsed_s)
            self.taken.append(snapshot)
            fresh.append(snapshot)
            self._seal(snapshot)
        return fresh

    def _take(self, mark_s: int, elapsed_s: float) -> Snapshot:
        """Copy the worktree to `folder/m1800/tree` and write its record."""
        files, size, content = copy_tree(self.worktree, self.folder / mark_name(mark_s) / TREE_DIR)
        snapshot = Snapshot(
            mark_s=mark_s,
            taken_at_s=elapsed_s,
            files=files,
            bytes_copied=size,
            content_sha256=content,
        )
        write_record(self.folder, snapshot)
        return snapshot

    def _seal(self, snapshot: Snapshot) -> None:
        """Seal one copy: a `snapshot` span naming the mark and its `content_sha256`,
        beside the run's other records and under its start span, so the hash of a tree the
        run no longer holds is in the sealed chain and the journal still verifies. An agent
        span like `compaction`: the run copies its own tree, the model never asks for it,
        so the chain does not hold it to the outcome's tool list."""
        append_span(
            self.journal,
            build_span(
                node_id=self.node_id,
                argv=[SNAPSHOT_SPAN, snapshot.name, snapshot.content_sha256],
                duration_ms=0,
                exit_code=0,
                detail=snapshot.detail,
                kind="agent",
                name=SNAPSHOT_SPAN,
                parent_id=self.run_span,
            ),
        )
