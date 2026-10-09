"""A run keeps a timed copy of its own worktree at each mark it reaches (#109 item 3).

A plain agent's runs are snapshotted at 30, 60, 90 and 120 minutes, so a run whose tree was
correct before it was refused can still be measured: the copy at its mark holds what the tree held
then. A saddle `auto` run had no such copy, so a refused `finish` left only the final tree, and a
correct-but-slow run read exactly like a wrong one. `saddle auto --snapshot-marks 1800,3600,5400,
7200` copies the worktree at the marks the run's own clock reaches, beside the run's journal, in
the plain agent's layout and with its hash, so the two kinds of run compare file for file.

Every half of the contract, both ways: a run that lives to a mark holds a copy of its tree as it
stood at that mark, with its record beside it and its `content_sha256` sealed in a `snapshot`
span; a mark the run did not live to has no copy, no record and no span; a run given no marks
writes no `snapshots` folder and seals no `snapshot` span at all.
"""

from __future__ import annotations

import hashlib
import json
import os
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest
from test_auto import BUGGY, Scripted, auto, call, finish, git, repo  # noqa: F401  (fixture)

from saddle import cli
from saddle.auto import AutoOptions, AutoResult, changed_files
from saddle.journal import SNAPSHOT_SPAN, append_span, build_span, read_spans, verify_journal
from saddle.snapshots import (
    SEPARATOR,
    SKIP_NAMES,
    SNAPSHOT_DIR,
    TREE_DIR,
    Snapshot,
    Snapshots,
    copy_tree,
    mark_name,
    skipped,
    tree_files,
    write_record,
)

FIXED_TREE = "def add(a, b):\n    return a + b\n"
"""The fixed `add`, so a test can say what the tree one copy holds is worth."""

PLAIN_MARKS = (1800, 3600, 5400, 7200)
"""The marks a plain agent's runs are snapshotted at: 30, 60, 90 and 120 minutes."""

PLAIN_SKIPS = (
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
"""What a plain agent's snapshotter leaves out, written out here from the format rather than read
back out of `saddle.snapshots`, so the two lists can be compared."""

SKIPPED_PATHS = (
    ".git/HEAD",
    ".git/objects/ab/cde",
    "src/.git/config",
    "__pycache__/calc.cpython-312.pyc",
    ".pytest_cache/v/cache/nodeids",
    ".scaffold/.saddle/runs/r1/proofs.jsonl",
    ".venv/lib/mod.py",
    "sitecustomize.pyc",
    "src/pkg/build.egg-info/PKG-INFO",
    ".mutmut-cache/state.json",
    ".ruff_cache/CACHEDIR.TAG",
)
"""A path of every kind a plain agent leaves out: a directory of its list, that directory deep in a
tree, and a file matching one of the list's `*` patterns."""

KEEP = b"keep = 1\n"
"""One file a copy is asked to keep, so that byte totals in these tests read small."""

BLOCKER = {"reason": "which of two configs is live\nmore", "tried": "read both\nran the tests."}
"""A run the model hands back to a person, taken from test_auto's blocked-run tests."""


def tree_hash(contents: dict[str, bytes]) -> str:
    """What a plain agent's `content_sha256` is for a tree, written out here from the format rather
    than read back out of `saddle.snapshots`: sha256 over the copied files in sorted order of
    relative path, feeding for each file its relative path as UTF-8, one zero byte, and the 32-byte
    sha256 digest of that file's bytes."""
    parts = hashlib.sha256()
    for rel in sorted(contents):
        parts.update(rel.encode("utf-8"))
        parts.update(b"\x00")
        parts.update(hashlib.sha256(contents[rel]).digest())
    return parts.hexdigest()


def build(root: Path, contents: dict[str, bytes]) -> dict[str, bytes]:
    """Write `contents` (relative path -> bytes) under `root`, making its folders."""
    for rel, body in contents.items():
        target = root / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(body)
    return contents


def kept(copy: Path) -> dict[str, bytes]:
    """What a copy holds, as relative path -> bytes."""
    return {
        str(p.relative_to(copy).as_posix()): p.read_bytes() for p in copy.rglob("*") if p.is_file()
    }


def record_of(folder: Path, mark: int) -> dict[str, Any]:
    """What one mark's record `folder/m<mark>.json` holds."""
    record: dict[str, Any] = json.loads((folder / f"{mark_name(mark)}.json").read_text())
    return record


def marks_held(folder: Path) -> list[int]:
    """The marks `folder` holds a copy and a record for, oldest first."""
    names = [p.name for p in folder.glob("*.json")]
    return sorted(int(name[: -len(".json")].removeprefix("m")) for name in names)


def tree_under(root: Path) -> list[str]:
    """The relative paths a copy of `root` would carry, sorted: what the copy is measured on."""
    return sorted(str(p.relative_to(root).as_posix()) for p in tree_files(root))


def walked_under(root: Path) -> list[str]:
    """The relative paths `os.walk` reaches, in the order it reaches them: a directory's own files
    come before anything in its subfolders, which is not the order the hash uses."""
    seen: list[str] = []
    for directory, _folders, names in os.walk(root):
        seen.extend(str(Path(directory, name).relative_to(root).as_posix()) for name in names)
    return seen


def holder(tmp_path: Path, marks: tuple[int, ...], tree: Path | None = None) -> Snapshots:
    """A mark-keeper whose copies, records and spans land under `tmp_path/snapshots`, with a start
    span in its journal so the spans it seals have the parent they cite."""
    if tree is None:
        build(tmp_path / "tree", {"a.py": b"a = 1\n"})
        tree = tmp_path / "tree"
    journal = tmp_path / "proofs.jsonl"
    start = build_span(
        node_id="chat#1",
        argv=["holder:start", "make add add"],
        duration_ms=0,
        exit_code=0,
        detail="the start span the copies cite",
        name="holder:start",
    )
    append_span(journal, start)
    return Snapshots(
        marks=marks,
        worktree=tree,
        folder=tmp_path / SNAPSHOT_DIR,
        journal=journal,
        run_span=start.span_id,
        node_id="chat#1",
    )


def snapped(snaps: Snapshots, elapsed_s: float) -> list[str]:
    """The names of the copies one look at the elapsed seconds takes."""
    return [snapshot.name for snapshot in snaps.snapshot_due(elapsed_s)]


def sealed_marks(journal: Path) -> list[str]:
    """The marks the `snapshot` spans in `journal` name, in the order they were sealed."""
    return [span.argv[1] for span in read_spans(journal) if span.name == SNAPSHOT_SPAN]


def fed(names: list[str], contents: dict[str, bytes], seam: bytes = SEPARATOR) -> str:
    """The hash one order of `names` produces over `contents`, with `seam` between each name and the
    digest of its bytes: what the sorted order and one zero byte are each worth."""
    parts = hashlib.sha256()
    for rel in names:
        parts.update(rel.encode("utf-8"))
        parts.update(seam)
        parts.update(hashlib.sha256(contents[rel]).digest())
    return parts.hexdigest()


# -- the copy, its record and its hash ------------------------------------------


def test_a_copy_holds_every_file_of_the_tree_and_hashes_it_as_the_plain_agent_does(
    tmp_path: Path,
) -> None:
    """Known-good: every file is carried byte for byte under its own relative path, and `copy_tree`
    says how many files it carried, their total bytes and the plain agent's hash."""
    contents = build(
        tmp_path / "tree",
        {
            "z.py": b"z = 26\n",
            "pkg/a.py": b"def f():\n    return 1\n",
            "pkg/sub/deep.md": b"# deep\n",
            "empty.txt": b"",
        },
    )
    files, size, digest = copy_tree(tmp_path / "tree", tmp_path / "copy")
    assert kept(tmp_path / "copy") == contents
    assert (files, size) == (4, sum(len(body) for body in contents.values()))
    assert digest == tree_hash(contents)


def test_a_symlink_is_left_out_as_the_plain_agent_leaves_it_out(tmp_path: Path) -> None:
    """Known-bad, found in review: a symlinked file was carried as its target's content,
    where the plain agent's snapshotter skips every symlink. A tree with `CLAUDE.md ->
    README.md` then held three files against the plain agent's two, and the hashes differed.
    Known-good: the copy, its counts and its hash are those of the tree without its links."""
    contents = build(tmp_path / "tree", {"README.md": b"hello\n", "pkg/a.py": b"x = 1\n"})
    (tmp_path / "tree" / "CLAUDE.md").symlink_to("README.md")
    (tmp_path / "tree" / "linked").symlink_to("pkg", target_is_directory=True)
    files, size, digest = copy_tree(tmp_path / "tree", tmp_path / "copy")
    assert kept(tmp_path / "copy") == contents
    assert (files, size) == (2, sum(len(body) for body in contents.values()))
    assert digest == tree_hash(contents)


def test_the_hash_is_over_the_files_in_sorted_order_of_relative_path(tmp_path: Path) -> None:
    """Known-bad (the order the tree is walked in, a rotation of the sorted order, or its reverse):
    the plain agent's hash is fed in sorted order of relative path, and any other order gives a
    different digest, so saddle's marks and a plain agent's marks would never match."""
    contents = build(
        tmp_path / "tree",
        {"z.py": b"z\n", "m/deep.py": b"deep\n", "a/other.py": b"other\n"},
    )
    _, _, digest = copy_tree(tmp_path / "tree", tmp_path / "copy")
    walked = walked_under(tmp_path / "tree")
    order = sorted(walked)
    assert walked[0] == "z.py"  # a walked directory's own file, not the alphabetically first name
    assert walked != order
    assert digest == fed(order, contents) == tree_hash(contents)
    assert digest != fed(walked, contents)  # the copy does not hash in the order it walks
    assert digest != fed(order[1:] + order[:1], contents)  # a rotation of the right order
    assert digest != fed(list(reversed(order)), contents)  # its reverse


@pytest.mark.parametrize(
    ("one", "other"),
    [
        # the same bytes under a different name, and the same name under different bytes
        ({"a.py": b"same\n"}, {"b.py": b"same\n"}),
        ({"a.py": FIXED_TREE.encode()}, {"a.py": BUGGY.encode()}),
        # one added file whose own content is already in the tree, and one path made deeper
        ({"a.py": b"x\n"}, {"a.py": b"x\n", "extra.py": b"x\n"}),
        ({"a.py": b"x\n", "pkg/b.py": b"x\n"}, {"a.py": b"x\n", "pkg/sub/b.py": b"x\n"}),
    ],
    ids=["renamed", "changed-byte", "added-file", "deeper-path"],
)
def test_two_different_trees_hash_apart(
    one: dict[str, bytes], other: dict[str, bytes], tmp_path: Path
) -> None:
    """Known-bad (a hash of content only, or of names and sizes only): the tree that was right and
    the tree that was wrong would read alike, which is the whole reason a copy is taken."""
    build(tmp_path / "first", one)
    build(tmp_path / "second", other)
    _, _, first = copy_tree(tmp_path / "first", tmp_path / "copy1")
    _, _, second = copy_tree(tmp_path / "second", tmp_path / "copy2")
    assert first != second


def test_the_path_and_the_digest_are_separated_by_one_zero_byte(tmp_path: Path) -> None:
    """Known-bad (no byte between them, or two of them): the names `a` and `ab`, fed straight into
    the digest bytes of contents that read as names, hash to a different digest each way, so a plain
    agent's snapshot of the very same tree would not match saddle's."""
    names = ["a", "ab"]
    entangled = build(tmp_path / "tree", {"a": b"ab", "ab": b"a"})
    _, _, digest = copy_tree(tmp_path / "tree", tmp_path / "copy")
    assert digest != fed(names, entangled, seam=b"")
    assert digest != fed(names, entangled, seam=b"\x00\x00")
    assert digest == fed(names, entangled) == tree_hash(entangled)
    assert SEPARATOR == b"\x00"


def test_a_file_is_copied_whatever_its_size(tmp_path: Path) -> None:
    """Known-good, both ways a chunked copy goes wrong: a file smaller than one read and a file
    larger than one each arrive whole, are counted whole, and hash for their whole bytes."""
    big = bytes(range(256)) * 6000  # past one chunk-sized read
    contents = build(tmp_path / "tree", {"small.txt": b"small\n", "big.bin": big})
    files, size, digest = copy_tree(tmp_path / "tree", tmp_path / "copy")
    assert kept(tmp_path / "copy") == contents
    assert (files, size) == (2, len(contents["small.txt"]) + len(big))
    assert digest == tree_hash(contents)


def test_a_copy_writes_the_plain_agents_record_beside_its_tree(tmp_path: Path) -> None:
    """Known-good: `snapshots/m1800.json` holds `mark_s`, `taken_at_s`, `files`, `bytes` and
    `content_sha256`, measuring the copy at `snapshots/m1800/tree`. Known-bad (one key missing, or
    the byte total named another way): a reader comparing saddle's mark with a plain agent's has
    nothing to compare."""
    tree = tmp_path / "tree"
    contents = build(tree, {"a.py": b"a = 1\n", "pkg/b.py": b"b = 22\n"})
    snaps = holder(tmp_path, (1800,), tree=tree)
    assert snapped(snaps, 1800.0) == [mark_name(1800)]
    folder = tmp_path / SNAPSHOT_DIR
    assert sorted(p.name for p in folder.iterdir()) == ["m1800", "m1800.json"]
    assert kept(folder / "m1800" / TREE_DIR) == contents
    assert record_of(folder, 1800) == {
        "mark_s": 1800,
        "taken_at_s": 1800.0,
        "files": 2,
        "bytes": 13,
        "content_sha256": tree_hash(contents),
    }


def test_the_record_stands_on_its_own(tmp_path: Path) -> None:
    """`Snapshot` and `write_record` are the layout, held apart from the run so it can be read out:
    `m1800.json` in the folder that names the mark, one key per field, `bytes` for the byte total,
    and a span detail naming the mark, its seconds, its counts and its hash."""
    digest = "ab" * 32
    snap = Snapshot(mark_s=1800, taken_at_s=1830.5, files=3, bytes_copied=61, content_sha256=digest)
    assert write_record(tmp_path / "later", snap) == tmp_path / "later" / "m1800.json"
    assert record_of(tmp_path / "later", 1800) == {
        "mark_s": 1800,
        "taken_at_s": 1830.5,
        "files": 3,
        "bytes": 61,
        "content_sha256": digest,
    }
    assert snap.name == "m1800"
    assert mark_name(1800) == "m1800"
    assert snap.detail.count(digest) == 1
    assert snap.detail == f"m1800 taken at 1830.5s: 3 file(s), 61 byte(s), content_sha256 {digest}"


# -- the two lists, compared ----------------------------------------------------


def test_a_run_leaves_out_what_a_plain_agent_leaves_out(tmp_path: Path) -> None:
    """The comparison #109 item 3 exists for, both ways: a tree holding a run's own leftovers
    beside its sources copies to exactly the plain agent's set of files, counts and hashes as a
    plain agent's hash of that same set, and saddle's skip list is that list."""
    tree = tmp_path / "tree"
    sources = {
        "calc.py": BUGGY.encode(),
        "tests/test_calc.py": b"from calc import add\n",
        "notes.py": b"# a source file\n",
    }
    leftovers = {
        "notes.pyc": b"a source file that happens to end in .pyc\n",
        ".git/HEAD": b"ref: refs/heads/main\n",
        "__pycache__/calc.cpython-312.pyc": b"bytecode\n",
        ".pytest_cache/v/cache/lastfailed": b"{}\n",
        ".saddle/runs/r1/proofs.jsonl": b"{}\n",
        ".venv/lib/mod.py": b"installed\n",
        "build.egg-info/PKG-INFO": b"metadata\n",
        ".mutmut-cache/state.json": b"{}\n",
        ".ruff_cache/CACHEDIR.TAG": b"tag\n",
    }
    build(tree, sources)
    build(tree, leftovers)
    files, size, digest = copy_tree(tree, tmp_path / "copy")
    assert kept(tmp_path / "copy") == sources
    assert (files, size) == (len(sources), sum(len(body) for body in sources.values()))
    assert digest == tree_hash(sources)  # a plain agent's hash of the tree it would have copied
    assert sorted(SKIP_NAMES) == sorted(PLAIN_SKIPS)


@pytest.mark.parametrize("name", SKIPPED_PATHS)
def test_every_name_a_plain_agent_leaves_out_is_left_out_however_deep_it_sits(
    name: str, tmp_path: Path
) -> None:
    """Known-bad (one of these patterns missing from the skip list): a run's version store,
    bytecode, test cache, own `.saddle` state, virtualenv or built metadata would be copied, counted
    and hashed, so the record would measure a tree that is not the run's sources. Both forms count:
    a directory the list names, wherever it sits, and a file matching one of its `*` patterns."""
    contents = build(tmp_path / "tree", {"keep.py": KEEP})
    build(tmp_path / "tree", {name: b"leftover\n"})
    files, size, digest = copy_tree(tmp_path / "tree", tmp_path / "copy")
    assert kept(tmp_path / "copy") == contents
    assert (files, size, digest) == (1, len(KEEP), tree_hash(contents))
    assert tree_under(tmp_path / "tree") == ["keep.py"]


@pytest.mark.parametrize("name", SKIPPED_PATHS)
def test_the_skip_list_names_each_of_the_plain_agents_names(name: str) -> None:
    """Each path above is left out because one of its names matches a pattern, a directory name as
    well as a file name: `.git` deep in a tree, and a name that only ends in `.pyc` or `.egg-info`.
    Known-bad (a list matched against the whole relative path, or against file names only) leaves
    every one of them in the copy."""
    assert any(skipped(part) for part in name.split("/"))


def test_a_name_the_skip_list_does_not_name_is_part_of_the_tree(tmp_path: Path) -> None:
    """Known-good, and the known-bad of a skip list grown past the plain agent's (a match by prefix
    or substring instead of the whole name): a directory named `git`, a folder named `pycache`, a
    `node_modules` folder and a `.pyc`-lookalike file are the tree's own content, and a copy that
    dropped them would not match a plain agent's copy file for file."""
    contents = build(
        tmp_path / "tree",
        {
            "keep.py": KEEP,
            "git/HEAD": b"not a version store\n",
            "node_modules/pkg/index.js": b"module.exports = 1\n",
            "pycache/mod.py": b"compiled by hand\n",
            "notes.pycx": b"not bytecode\n",
        },
    )
    files, _, digest = copy_tree(tmp_path / "tree", tmp_path / "copy")
    assert kept(tmp_path / "copy") == contents
    assert (files, digest) == (5, tree_hash(contents))
    assert tree_under(tmp_path / "tree") == sorted(contents)


def test_a_name_that_is_not_exactly_one_of_the_skip_lists_names_is_kept(tmp_path: Path) -> None:
    """Known-bad (a skip test that matches a name by prefix or substring rather than whole names):
    `git`, `pycache`, `node_modules` and `notes.pycx` would all be dropped, and the copy would be
    smaller than the plain agent's copy of the same tree."""
    for name in ("keep.py", "git", "pycache", "node_modules", "notes.pycx", "index.js"):
        assert not skipped(name)
    for name in (".git", "build.egg-info", "stale.pyc", ".venv", ".ruff_cache"):
        assert skipped(name)


def test_a_directory_the_skip_list_names_is_not_walked_at_all(tmp_path: Path) -> None:
    """Known-bad (a skip list applied to file names only, or one that still walks the directory it
    skips): every file under a skipped directory would be reached, copied, counted and hashed. The
    directory is left out whole, however deep it goes."""
    contents = build(
        tmp_path / "tree",
        {
            "keep.py": KEEP,
            ".venv/lib/a.py": b"1\n",
            ".venv/lib/sub/b.py": b"22\n",
            "nest/.git/HEAD": b"3\n",
        },
    )
    files, size, digest = copy_tree(tmp_path / "tree", tmp_path / "copy" / "sub")
    assert kept(tmp_path / "copy" / "sub") == {"keep.py": contents["keep.py"]}
    assert (files, size, digest) == (1, len(KEEP), tree_hash({"keep.py": contents["keep.py"]}))
    assert tree_under(tmp_path / "tree") == ["keep.py"]


def test_an_empty_tree_copies_to_an_empty_tree(tmp_path: Path) -> None:
    """Known-good (a tree holding only skipped names): the copy is made, holds nothing, and hashes
    to the sha256 of nothing. Known-bad (no copy and no record for such a tree): a run whose tree
    held only leftovers would leave a mark with nothing beside it to check."""
    build(tmp_path / "tree", {".git/HEAD": b"ref\n", "__pycache__/a.pyc": b"x"})
    files, size, digest = copy_tree(tmp_path / "tree", tmp_path / "copy")
    assert (files, size, digest) == (0, 0, hashlib.sha256(b"").hexdigest())
    assert kept(tmp_path / "copy") == {}
    assert tree_under(tmp_path / "tree") == []
    snaps = holder(tmp_path, (60,), tree=tmp_path / "tree")
    assert snapped(snaps, 60.0) == ["m60"]
    assert kept(tmp_path / SNAPSHOT_DIR / "m60" / TREE_DIR) == {}
    assert record_of(tmp_path / SNAPSHOT_DIR, 60) == {
        "mark_s": 60,
        "taken_at_s": 60.0,
        "files": 0,
        "bytes": 0,
        "content_sha256": hashlib.sha256(b"").hexdigest(),
    }


def test_a_dangling_link_is_left_out_rather_than_breaking_the_copy(tmp_path: Path) -> None:
    """Known-bad (the copy trusting a name that is not a file): a link whose target is gone would
    raise, and a run would end with no copy of its tree at a mark it lived to."""
    contents = build(tmp_path / "tree", {"keep.py": KEEP})
    (tmp_path / "tree" / "gone.py").symlink_to(tmp_path / "nowhere.py")
    files, _, digest = copy_tree(tmp_path / "tree", tmp_path / "copy")
    assert (files, digest) == (1, tree_hash(contents))
    assert tree_under(tmp_path / "tree") == ["keep.py"]


# -- which marks one run takes --------------------------------------------------


def test_a_run_copies_each_reached_mark_once_however_often_it_looks(tmp_path: Path) -> None:
    """Known-good: a mark becomes due when the elapsed seconds reach it, and a look after that
    takes nothing. Known-bad (a fresh copy at every look once a mark is reached): a run looks at its
    tree before each of many requests, so one mark would be copied many times over. Known-bad (a
    copy taken from the tree as it stands at a later look): the copy keeps the tree as it stood at
    the look that took it, which is what makes a mark worth reading."""
    snaps = holder(tmp_path, (60,))
    tree = snaps.worktree
    early = kept(tree)
    assert snapped(snaps, 59.0) == []
    assert snapped(snaps, 60.0) == ["m60"]
    build(tree, {"a.py": b"a = 2\n"})  # the tree moves after the copy was taken
    for elapsed in (61.0, 120.0, 9000.0):
        assert snapped(snaps, elapsed) == []
    folder = tmp_path / SNAPSHOT_DIR
    assert marks_held(folder) == [60]
    assert kept(folder / "m60" / TREE_DIR) == early
    assert record_of(folder, 60) == {
        "mark_s": 60,
        "taken_at_s": 60.0,
        "files": 1,
        "bytes": 6,
        "content_sha256": tree_hash(early),
    }


def test_one_look_copies_every_mark_it_passed_in_order(tmp_path: Path) -> None:
    """Known-bad (only the mark the run happens to land on, or the marks in the order the caller
    wrote them, or one mark taken twice by two looks): a run whose clock passed 5400s and 1800s
    between two looks must keep both trees, oldest first, each record dated with the seconds of the
    look that took it."""
    snaps = holder(tmp_path, (5400, 1800, 1800, 3600))
    assert list(snaps.marks) == [1800, 3600, 5400]
    assert snapped(snaps, 1799.9) == []
    assert snapped(snaps, 3601.0) == ["m1800", "m3600"]
    assert snapped(snaps, 5401.0) == ["m5400"]
    assert snapped(snaps, 9000.0) == []
    folder = tmp_path / SNAPSHOT_DIR
    assert marks_held(folder) == [1800, 3600, 5400]
    taken_at = [record_of(folder, mark)["taken_at_s"] for mark in (1800, 3600, 5400)]
    assert taken_at == [3601.0, 3601.0, 5401.0]


def test_a_mark_the_run_did_not_live_to_is_never_copied(tmp_path: Path) -> None:
    """Known-bad (a copy for every mark asked for, lived to or not): a run whose clock reached
    1800s and stopped would hold a copy dated 7200s, and a reader scoring it against a plain
    agent's 120-minute snapshot would be reading a tree that never existed."""
    snaps = holder(tmp_path, (1800, 7200))
    assert snapped(snaps, 1800.0) == ["m1800"]
    assert snapped(snaps, 7199.9) == []
    folder = tmp_path / SNAPSHOT_DIR
    assert marks_held(folder) == [1800]
    assert not (folder / "m7200").exists()
    assert not (folder / "m7200.json").exists()
    assert record_of(folder, 1800)["mark_s"] == 1800


def test_no_snapshots_are_made_when_the_run_wants_none(tmp_path: Path) -> None:
    """Known-good (the default, `AutoOptions.snapshot_marks_s` is empty): nothing copied and
    nothing sealed, however far the clock runs. Known-bad (a folder made even with no marks): a run
    that asked for no copies would leave a reader looking for them, and the comparison with a plain
    agent's run would start out unequal."""
    assert AutoOptions(task="t", repo=Path("."), run_id="r1").snapshot_marks_s == ()
    snaps = holder(tmp_path, ())
    assert snapped(snaps, 7200.0) == []
    assert not (tmp_path / SNAPSHOT_DIR).exists()
    assert sealed_marks(tmp_path / "proofs.jsonl") == []


# -- the span a copy seals ------------------------------------------------------


def test_each_copy_seals_a_span_naming_its_mark_and_its_hash(tmp_path: Path) -> None:
    """Known-good: one `snapshot` span per copy, under the run's start span, whose argv and detail
    name the mark and its `content_sha256`, so the hash of a tree the run no longer holds sits in
    the sealed chain and `verify_journal` still finds nothing. Known-bad (no span, or a span that
    names no hash): the copy exists on disk only, and a run refused at `finish` leaves nothing in
    its own ledger to measure."""
    tree = tmp_path / "tree"
    right = build(tree, {"fix.py": FIXED_TREE.encode()})
    snaps = holder(tmp_path, (60, 120), tree=tree)
    taken = snapped(snaps, 120.0)
    build(tree, {"fix.py": BUGGY.encode()})
    assert taken == ["m60", "m120"]

    journal = tmp_path / "proofs.jsonl"
    spans = [span for span in read_spans(journal) if span.name == SNAPSHOT_SPAN]
    assert sealed_marks(journal) == ["m60", "m120"]
    assert [span.kind for span in spans] == ["agent", "agent"]
    assert all(len(span.span_id) == 32 for span in spans)  # an id a later span can cite
    assert [span.parent_id for span in spans] == [spans[0].parent_id, spans[0].parent_id]
    assert all(span.node_id == "chat#1" for span in spans)
    for span in spans:
        assert span.argv == ["snapshot", span.argv[1], span.argv[2]]
        assert span.argv[1] in span.detail
        assert span.argv[2] in span.detail
    assert spans[0].record_hash != spans[1].record_hash
    assert [span.argv[2] for span in spans] == [tree_hash(right), tree_hash(right)]
    assert verify_journal(journal) == []


def test_two_copies_of_a_changed_tree_seal_two_different_trees(tmp_path: Path) -> None:
    """Known-bad (every copy made from the tree of the last look, so one hash for all of them): the
    sealed chain has to let a reader tell the 60-second tree from the 120-second one, which is how a
    run that was right at 60s and wrong at 120s is told apart from one wrong throughout."""
    tree = tmp_path / "tree"
    right = build(tree, {"fix.py": FIXED_TREE.encode()})
    snaps = holder(tmp_path, (60, 120), tree=tree)
    snapped(snaps, 60.0)
    wrong = build(tree, {"fix.py": BUGGY.encode(), "extra.py": b"new\n"})
    snapped(snaps, 120.0)
    journal = tmp_path / "proofs.jsonl"
    spans = [span for span in read_spans(journal) if span.name == SNAPSHOT_SPAN]
    assert [span.argv[2] for span in spans] == [tree_hash(right), tree_hash(wrong)]
    folder = tmp_path / SNAPSHOT_DIR
    assert kept(folder / "m60" / TREE_DIR) == right
    assert kept(folder / "m120" / TREE_DIR) == wrong
    assert marks_held(folder) == [60, 120]
    assert record_of(folder, 120)["files"] == 2
    assert verify_journal(journal) == []


def test_a_copy_is_sealed_once_however_many_looks_follow_it(tmp_path: Path) -> None:
    """Known-bad (the span sealed again at every look, or a copy that takes the tree of a look
    after the one that sealed it): the ledger would hold several records of one tree, and a reader
    could not tell how many trees the run kept."""
    tree = tmp_path / "tree"
    early = build(tree, {"a.py": b"a = 1\n"})
    snaps = holder(tmp_path, (60, 180), tree=tree)
    journal = tmp_path / "proofs.jsonl"
    assert snapped(snaps, 60.0) == ["m60"]
    build(tree, {"a.py": b"a = 2\n"})
    assert snapped(snaps, 120.0) == []
    assert snapped(snaps, 179.9) == []
    assert sealed_marks(journal) == ["m60"]
    assert snapped(snaps, 180.0) == ["m180"]  # a mark a further look reached
    assert sealed_marks(journal) == ["m60", "m180"]
    folder = tmp_path / SNAPSHOT_DIR
    assert kept(folder / "m60" / TREE_DIR) == early
    assert kept(folder / "m180" / TREE_DIR) == {"a.py": b"a = 2\n"}
    assert verify_journal(journal) == []


# -- a real run: its marks come from its own clock ------------------------------


@dataclass
class Clock:
    """A clock a scripted run moves each time the run asks the model, so a run reaches its marks
    and the elapsed seconds a copy records are the seconds this test moved the clock to."""

    now: float = 0.0
    per_ask: float = 0.0

    def __call__(self) -> float:
        return self.now

    def ask(self) -> None:
        self.now += self.per_ask


class Timed(Scripted):
    """A scripted model that moves the run's clock every time the run asks it."""

    def __init__(
        self, rounds: list[Any], tail: list[Any] | None = None, clock: Clock | None = None
    ) -> None:
        super().__init__(rounds, tail)
        self.clock = clock if clock is not None else Clock()

    def stream_chat(self, *args: Any, **kwargs: Any) -> Iterator[Any]:
        self.clock.ask()
        return super().stream_chat(*args, **kwargs)


def write(name: str, content: str) -> list[Any]:
    """One round of the model writing one file into the run's worktree."""
    return [call("write_file", f"c-{name}", path=name, content=content)]


def mark_run(
    root: Path, clock: Clock, marks: tuple[int, ...], rounds: list[Any], **kwargs: Any
) -> AutoResult:
    """One scripted `saddle auto` run whose clock moves `clock.per_ask` seconds at each request,
    given `--snapshot-marks` values `marks`."""
    return auto(root, Timed(rounds, clock=clock), clock=clock, snapshot_marks_s=marks, **kwargs)


def snapshots_folder(result: AutoResult) -> Path:
    """A run's timed copies, where they sit beside its journal: `<journal dir>/snapshots`."""
    folder = result.journal.parent / SNAPSHOT_DIR
    assert isinstance(folder, Path)
    return folder


def snapshot_spans(result: AutoResult) -> list[Any]:
    """The `snapshot` spans a run sealed, oldest first."""
    return [span for span in read_spans(result.journal) if span.name == SNAPSHOT_SPAN]


def test_a_run_holds_one_copy_per_reached_mark_holding_its_tree_at_that_mark(
    repo: Path,  # noqa: F811
) -> None:
    """Known-good: marks at 100, 200, 300 and 400 seconds, 100 seconds of clock per request, and a
    model that fixes `calc.py`, breaks it again, then fixes it once more before finishing. Each
    reached mark has `snapshots/m<mark>/tree` holding the worktree as it stood then, and
    `snapshots/m<mark>.json` holding the plain agent's record beside it, with its hash sealed in a
    `snapshot` span, in a journal that still verifies. The tree that was right at 100s is readable
    after the run broke it at 200s, which its final tree alone could not show."""
    clock = Clock(per_ask=100.0)
    rounds = [
        write("calc.py", FIXED_TREE),
        write("calc.py", BUGGY),
        write("calc.py", FIXED_TREE),
        finish(),
    ]
    result = mark_run(repo, clock, (100, 200, 300, 400), rounds)
    assert result.outcome == "finished"
    folder = snapshots_folder(result)
    assert marks_held(folder) == [100, 200, 300, 400]

    at_100 = kept(folder / "m100" / TREE_DIR)
    at_200 = kept(folder / "m200" / TREE_DIR)
    at_300 = kept(folder / "m300" / TREE_DIR)
    at_400 = kept(folder / "m400" / TREE_DIR)
    assert at_100["calc.py"].decode() == FIXED_TREE  # right at 100s, and that copy was kept
    assert at_200["calc.py"].decode() == BUGGY  # the run broke it, and the mark remembers
    assert at_300["calc.py"].decode() == FIXED_TREE
    assert at_400["calc.py"].decode() == FIXED_TREE
    assert tree_hash(at_100) != tree_hash(at_200)
    assert "tests/test_calc.py" in at_100  # a copy of the tree, not of the files it changed
    assert at_100.keys() == at_200.keys() == at_300.keys() == at_400.keys()
    for tree in (at_100, at_200, at_300, at_400):
        assert not any(part in (".git", ".saddle") for rel in tree for part in rel.split("/"))

    assert record_of(folder, 100) == {
        "mark_s": 100,
        "taken_at_s": 100.0,
        "files": len(at_100),
        "bytes": sum(len(body) for body in at_100.values()),
        "content_sha256": tree_hash(at_100),
    }
    assert [record_of(folder, mark)["content_sha256"] for mark in (200, 300, 400)] == [
        tree_hash(at_200),
        tree_hash(at_300),
        tree_hash(at_400),
    ]
    assert [record_of(folder, mark)["taken_at_s"] for mark in (100, 200, 300, 400)] == [
        100.0,
        200.0,
        300.0,
        400.0,
    ]

    sealed = snapshot_spans(result)
    assert [span.argv[1] for span in sealed] == ["m100", "m200", "m300", "m400"]
    assert [span.argv[2] for span in sealed] == [
        tree_hash(at_100),
        tree_hash(at_200),
        tree_hash(at_300),
        tree_hash(at_400),
    ]
    for span, tree in zip(sealed, (at_100, at_200, at_300, at_400), strict=True):
        assert span.argv[1] in span.detail
        assert tree_hash(tree) in span.detail
    assert verify_journal(result.journal) == []


def test_a_run_that_ends_before_a_mark_holds_no_copy_and_no_span_of_it(
    repo: Path,  # noqa: F811
) -> None:
    """Known-bad (a copy for a mark the run did not live to): this run's clock reaches 100s, 200s
    and 300s before it ends, so 400s and 1200s have no folder, no record and no span, while the
    marks it lived to hold all three."""
    clock = Clock(per_ask=100.0)
    rounds = [write("calc.py", FIXED_TREE), write("extra.py", "extra = 1\n"), finish()]
    result = mark_run(repo, clock, (100, 200, 300, 400, 1200), rounds)
    folder = snapshots_folder(result)
    assert sorted(p.name for p in folder.iterdir()) == [
        "m100",
        "m100.json",
        "m200",
        "m200.json",
        "m300",
        "m300.json",
    ]
    assert marks_held(folder) == [100, 200, 300]
    for mark in (400, 1200):
        assert not (folder / f"m{mark}").exists()
        assert not (folder / f"m{mark}.json").exists()
    assert [span.argv[1] for span in snapshot_spans(result)] == ["m100", "m200", "m300"]
    assert "extra.py" not in kept(folder / "m100" / TREE_DIR)  # reached at 200s, not at 100s
    assert "extra.py" in kept(folder / "m200" / TREE_DIR)
    assert verify_journal(result.journal) == []


def test_the_end_of_the_run_copies_a_mark_its_last_reply_ran_past(
    repo: Path,  # noqa: F811
) -> None:
    """Known-bad (the tree copied only on the way to a request, and nowhere else): the clock reaches
    300s while the run's final reply is being asked and its answer is being run, so no request-bound
    look can copy `m300`, and the look at the end of the run is what copies it -- holding the tree
    the run ended on."""
    clock = Clock(per_ask=150.0)
    result = mark_run(repo, clock, (300, 1200), [write("calc.py", FIXED_TREE), finish()])
    folder = snapshots_folder(result)
    assert marks_held(folder) == [300]
    assert kept(folder / "m300" / TREE_DIR)["calc.py"].decode() == FIXED_TREE
    assert record_of(folder, 300)["taken_at_s"] == 300.0
    assert not (folder / "m1200.json").exists()
    assert [span.argv[1] for span in snapshot_spans(result)] == ["m300"]
    assert verify_journal(result.journal) == []


def test_a_copy_at_a_mark_holds_the_tree_the_next_request_was_shown(
    repo: Path,  # noqa: F811
) -> None:
    """Known-good, both halves: each request's look copies the tree that request was about to be
    shown, so a file written after a mark is missing from that mark and present at the next.
    Known-bad (the tree copied once, at the end): every mark would hold `late.py`, and a reader
    could not tell what the model was actually shown at 100s."""
    clock = Clock(per_ask=100.0)
    rounds = [write("early.py", "early = 1\n"), write("late.py", "late = 1\n"), finish()]
    result = mark_run(repo, clock, (100, 200), rounds)
    folder = snapshots_folder(result)
    assert marks_held(folder) == [100, 200]
    assert kept(folder / "m100" / TREE_DIR)["early.py"] == b"early = 1\n"  # the first request ran
    assert "late.py" not in kept(folder / "m100" / TREE_DIR)  # the second had not run yet
    assert kept(folder / "m200" / TREE_DIR)["late.py"] == b"late = 1\n"
    assert kept(folder / "m200" / TREE_DIR).keys() >= kept(folder / "m100" / TREE_DIR).keys()
    assert record_of(folder, 100)["taken_at_s"] == 100.0  # the look, not the mark, dates the copy
    assert verify_journal(result.journal) == []


def test_the_plain_agent_marks_are_the_marks_the_run_measures(
    repo: Path,  # noqa: F811
) -> None:
    """Known-good: a run given the plain agent's own marks with 1800 seconds of clock per request
    holds a copy at each of the four, named `m1800` through `m7200` and dated 1800s through 7200s,
    so saddle's marks and a plain agent's marks line up mark for mark. Each holds the tree as it
    grew, so the four hashes are four different trees."""
    clock = Clock(per_ask=1800.0)
    rounds = [
        write("at30.py", "a\n"),
        write("at60.py", "b\n"),
        write("at90.py", "c\n"),
        write("at120.py", "d\n"),
        finish(),
    ]
    result = mark_run(repo, clock, PLAIN_MARKS, rounds)
    folder = snapshots_folder(result)
    wanted = [f"m{mark}" for mark in PLAIN_MARKS] + [f"m{mark}.json" for mark in PLAIN_MARKS]
    assert sorted(p.name for p in folder.iterdir()) == sorted(wanted)
    assert marks_held(folder) == list(PLAIN_MARKS)
    assert [record_of(folder, mark)["taken_at_s"] for mark in PLAIN_MARKS] == [
        1800.0,
        3600.0,
        5400.0,
        7200.0,
    ]
    trees = [kept(folder / f"m{mark}" / TREE_DIR) for mark in PLAIN_MARKS]
    assert [tree["calc.py"].decode() for tree in trees] == [BUGGY] * 4  # never fixed
    assert trees[0].keys() < trees[1].keys() < trees[2].keys() < trees[3].keys()
    assert "at120.py" in trees[3]  # the tree the run ended on is the 7200s one
    assert len({tree_hash(tree) for tree in trees}) == 4
    assert [record_of(folder, mark)["content_sha256"] for mark in PLAIN_MARKS] == [
        tree_hash(tree) for tree in trees
    ]
    assert [span.argv[1] for span in snapshot_spans(result)] == [
        "m1800",
        "m3600",
        "m5400",
        "m7200",
    ]
    assert verify_journal(result.journal) == []


def test_a_run_stopped_before_it_finished_keeps_the_marks_it_reached(
    repo: Path,  # noqa: F811
) -> None:
    """Both halves of the reason item 3 exists: a run its budget stops before any `finish` is still
    measured by the copies it reached, and a reached mark holds the tree that was still wrong.
    Known-bad (only a finished run keeps its copies): the run #109 wants to measure would be the one
    run with no copies at all."""
    clock = Clock(per_ask=1800.0)
    rounds = [write("calc.py", BUGGY), write("calc.py", BUGGY)]
    result = mark_run(repo, clock, (1800, 3600), rounds, time_budget_s=3000)
    assert result.outcome == "stopped"
    folder = snapshots_folder(result)
    assert marks_held(folder) == [1800, 3600]
    assert kept(folder / "m1800" / TREE_DIR) == kept(folder / "m3600" / TREE_DIR)
    assert kept(folder / "m3600" / TREE_DIR)["calc.py"].decode() == BUGGY
    assert [span.argv[1] for span in snapshot_spans(result)] == ["m1800", "m3600"]
    assert verify_journal(result.journal) == []


def test_a_run_handed_back_to_a_person_keeps_the_marks_it_reached(
    repo: Path,  # noqa: F811
) -> None:
    """The run #109 exists to measure: it ends needing a person with its tree still the wrong one,
    and every mark its clock reached is readable afterwards, in a journal that still verifies.
    Known-bad (copies dropped, or the last look skipped, when the run is handed back): a refused run
    would leave only the tree it was wrong about, which is what item 3 is for."""
    clock = Clock(per_ask=100.0)
    rounds = [write("calc.py", BUGGY), write("calc.py", BUGGY), [call("blocked", **BLOCKER)]]
    result = mark_run(repo, clock, (100, 200, 5000), rounds)
    assert result.outcome == "stopped"  # ended needing a person, and 5000s was never reached
    folder = snapshots_folder(result)
    assert marks_held(folder) == [100, 200]
    assert kept(folder / "m200" / TREE_DIR)["calc.py"].decode() == BUGGY
    assert not (folder / "m5000.json").exists()
    assert [span.argv[1] for span in snapshot_spans(result)] == ["m100", "m200"]
    assert verify_journal(result.journal) == []


def test_a_run_without_marks_writes_no_snapshots_and_seals_no_span(
    repo: Path,  # noqa: F811
) -> None:
    """Known-good (the default): no `snapshots` folder beside the journal, and no `snapshot` span,
    in a run whose clock ran past every mark a plain agent would have used. Known-bad (a copy taken
    anyway, or an empty folder made for it): the run asked for no copies, and inventing one makes
    the comparison with a plain agent's run unequal from the start."""
    clock = Clock(per_ask=3600.0)
    result = mark_run(repo, clock, (), [write("calc.py", FIXED_TREE), finish()])
    assert result.outcome == "finished"
    assert not snapshots_folder(result).exists()
    assert snapshot_spans(result) == []
    assert SNAPSHOT_SPAN not in [span.name for span in read_spans(result.journal)]
    assert verify_journal(result.journal) == []
    assert git(repo, "show", f"{result.branch}:calc.py") == FIXED_TREE


def test_copies_are_kept_where_the_run_cannot_reach_them(
    repo: Path,  # noqa: F811
) -> None:
    """Known-bad (the copies kept inside the worktree they copy): the run's own tools would list and
    read them, its changed-file list would name them, and its commit would carry them. They sit
    beside the journal, outside the worktree, and the run's commit carries only its own work."""
    clock = Clock(per_ask=100.0)
    result = mark_run(repo, clock, (100,), [write("calc.py", FIXED_TREE), finish()])
    folder = snapshots_folder(result)
    assert folder.is_dir()
    assert folder.parent == result.journal.parent
    assert result.worktree not in folder.parents
    assert not (result.worktree / SNAPSHOT_DIR).exists()
    assert not any(SNAPSHOT_DIR in path for path in changed_files(result.worktree))
    assert not any(SNAPSHOT_DIR in path for path in tree_under(result.worktree))
    committed = git(repo, "show", "--name-only", "--format=", result.branch).split()
    assert not any(SNAPSHOT_DIR in path for path in committed)
    assert "calc.py" in committed
    assert verify_journal(result.journal) == []


# -- the command line -----------------------------------------------------------


def parse(argv: list[str]) -> tuple[int, ...]:
    marks: tuple[int, ...] = cli.build_parser().parse_args(argv).snapshot_marks
    return marks


def test_the_flag_names_the_marks_a_run_will_copy() -> None:
    """Known-good: `saddle auto --snapshot-marks 1800,3600,5400,7200`, and the default is empty,
    which takes no snapshots."""
    assert parse(["auto", "make add add", "--snapshot-marks", "1800,3600,5400,7200"]) == (
        1800,
        3600,
        5400,
        7200,
    )
    assert parse(["auto", "make add add"]) == ()
    assert parse(["auto", "make add add", "--snapshot-marks", "3600, 1800"]) == (3600, 1800)


@pytest.mark.parametrize("given", ["1800,abc", "1800, abc x", "abc", "0", "-5", "1800.5"])
def test_a_mark_that_is_not_a_positive_whole_second_is_refused(given: str) -> None:
    """Known-bad: a mark of 0 or a negative one is reached by every request the run makes, so the
    run would copy its tree once per request instead of once per mark; a word and a fraction are
    not moments in time."""
    with pytest.raises(SystemExit):
        parse(["auto", "make add add", "--snapshot-marks", given])


def test_an_empty_value_asks_for_no_copies() -> None:
    """Known-good: `--snapshot-marks ""` is no marks, not a mark at 0 seconds, and `1800,` asks for
    one mark rather than two."""
    assert parse(["auto", "make add add", "--snapshot-marks", ""]) == ()
    assert parse(["auto", "make add add", "--snapshot-marks", "1800,"]) == (1800,)
