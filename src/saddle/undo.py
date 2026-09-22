"""What a turn changed on disk, and how to put it back.

Sampling makes a retry worth having, and a retry that leaves the previous
attempt's files behind is not a retry -- it is two attempts layered on top
of each other. So every write a turn makes through the file tools is
recorded here first, and rewinding a conversation puts those files back the
way they were before the turn ran.

What is reversible, exactly:

- `write_file` and `edit_file`. The file's previous bytes are copied aside
  before the first change a turn makes to it, and restored on rewind. A file
  the turn *created* is deleted again.
- Nothing else. A shell command can do anything -- `rm -rf`, `sed -i`, a
  build that writes a thousand artefacts -- and no log kept here could
  reverse it honestly. `run_command` is not tracked, and rewinding says so
  rather than implying the workdir is clean.

The record is per turn and append-only, so rewinding to a point undoes every
turn from there forward, newest first: a file changed by three turns comes
back to what it was before the earliest of them.
"""

from __future__ import annotations

import json
import shutil
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Final

MANIFEST: Final = "turns.jsonl"
BLOBS: Final = "blobs"


@dataclass(frozen=True)
class Restored:
    """What a rewind actually did, in the words the UI shows."""

    reverted: list[str]
    deleted: list[str]
    failed: list[str]

    @property
    def touched(self) -> int:
        return len(self.reverted) + len(self.deleted)


def _tidy(reverted: list[str], deleted: list[str], failed: list[str]) -> Restored:
    """One entry per file: several turns may have written the same one."""
    seen = list(dict.fromkeys(reverted))
    return Restored(
        reverted=seen,
        deleted=[p for p in dict.fromkeys(deleted) if p not in set(seen)],
        failed=list(dict.fromkeys(failed)),
    )


class UndoLog:
    """Per-turn snapshots of the files a session's tools wrote."""

    def __init__(self, root: Path) -> None:
        self.root = Path(root)
        self.turn: int | None = None
        self._seen: set[tuple[int, str]] = set()

    # -- recording ---------------------------------------------------------

    def begin(self, turn: int, start_index: int) -> None:
        """Open a turn, remembering where its messages start.

        `start_index` is what lets a rewind to a message find the turns that
        produced everything after it.
        """
        self.turn = turn
        self._append({"kind": "turn", "turn": turn, "start_index": start_index})

    def before_write(self, path: Path) -> None:
        """Snapshot `path` as it is now, once per turn.

        Once per turn, not once per write: a turn that edits the same file
        four times should rewind to what was there before the first edit,
        not before the fourth.
        """
        if self.turn is None:
            return
        key = (self.turn, str(path))
        if key in self._seen:
            return
        self._seen.add(key)
        try:
            existed = path.is_file()
            blob = None
            if existed:
                blob = uuid.uuid4().hex
                target = self.root / BLOBS / blob
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(path, target)
        except OSError:
            # A file that cannot be read cannot be promised back, and
            # failing the tool call over it would be worse.
            return
        self._append(
            {"kind": "write", "turn": self.turn, "path": str(path),
             "existed": existed, "blob": blob}
        )

    def after_write(self, path: Path, call: str | None = None) -> str | None:
        """Keep the file as this turn left it, and return its version id.

        A preview in an older message should show what that message
        produced, not whatever the file happens to say now. Serving by path
        alone meant a frog drawn in turn one and edited in turn three showed
        the turn-three frog twice, and the conversation stopped making sense.

        Unlike `before_write` this runs on every write: the last one is the
        version the turn actually left behind.
        """
        if self.turn is None:
            return None
        try:
            blob = uuid.uuid4().hex
            target = self.root / BLOBS / blob
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(path, target)
        except OSError:
            return None
        self._append({
            "kind": "version", "turn": self.turn, "call": call,
            "path": str(path), "blob": blob,
        })
        return blob

    def versions(self) -> dict[str, str]:
        """Blob per tool call, latest write of each wins.

        Keyed by the call rather than the turn because that is what a stored
        transcript has: a reloaded message can find the picture it made
        without knowing which turn it belonged to.
        """
        out: dict[str, str] = {}
        for record in self._read():
            if record.get("kind") == "version" and record.get("call"):
                out[str(record["call"])] = str(record["blob"])
        return out

    def blob_path(self, blob: str) -> Path:
        return self.root / BLOBS / blob

    # -- rewinding ---------------------------------------------------------

    def _plan(self, index: int) -> tuple[list[dict[str, Any]], set[int]]:
        """The write records a rewind to `index` would act on, newest first."""
        records = self._read()
        starts = {
            r["turn"]: r["start_index"] for r in records if r.get("kind") == "turn"
        }
        undoing = {turn for turn, start in starts.items() if start >= index}
        # Newest first, so a file several turns touched lands on the oldest
        # snapshot rather than an intermediate one.
        return [
            r for r in reversed(records)
            if r.get("kind") == "write" and r.get("turn") in undoing
        ], undoing

    def pending(self, index: int) -> Restored:
        """What a rewind would change, without changing it.

        Rewinding edits the user's working directory. Doing that silently is
        not acceptable, so the UI asks first -- and to ask it has to be able
        to say which files, which means knowing before acting.
        """
        reverted: list[str] = []
        deleted: list[str] = []
        for record in self._plan(index)[0]:
            (reverted if record.get("existed") else deleted).append(record["path"])
        return _tidy(reverted, deleted, [])

    def restore_to(self, index: int) -> Restored:
        """Undo every turn whose messages start at or after `index`."""
        plan, undoing = self._plan(index)
        reverted: list[str] = []
        deleted: list[str] = []
        failed: list[str] = []
        for record in plan:
            path = Path(record["path"])
            try:
                if record.get("existed"):
                    blob = self.root / BLOBS / str(record.get("blob"))
                    path.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(blob, path)
                    reverted.append(str(path))
                else:
                    path.unlink(missing_ok=True)
                    deleted.append(str(path))
            except OSError:
                failed.append(str(path))
        self._forget(undoing)
        return _tidy(reverted, deleted, failed)

    # -- storage -----------------------------------------------------------

    def _path(self) -> Path:
        return self.root / MANIFEST

    def _append(self, record: dict[str, Any]) -> None:
        self.root.mkdir(parents=True, exist_ok=True)
        with self._path().open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(record, ensure_ascii=False) + "\n")

    def _read(self) -> list[dict[str, Any]]:
        try:
            lines = self._path().read_text(encoding="utf-8").splitlines()
        except OSError:
            return []
        out = []
        for line in lines:
            if not line.strip():
                continue
            try:
                out.append(json.loads(line))
            except ValueError:
                continue  # a torn tail loses one record, not the log
        return out

    def _forget(self, turns: set[int]) -> None:
        """Drop undone turns, so rewinding twice does not restore twice."""
        kept = [r for r in self._read() if r.get("turn") not in turns]
        self.root.mkdir(parents=True, exist_ok=True)
        self._path().write_text(
            "".join(json.dumps(r, ensure_ascii=False) + "\n" for r in kept),
            encoding="utf-8",
        )
        self._seen = {key for key in self._seen if key[0] not in turns}
