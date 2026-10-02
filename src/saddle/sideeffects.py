"""What a full-access session changed outside its folder, and how to put it back (#137).

A sandboxed session can only change its folder, and the folder has its own
undo (`undo`). With full access (#124) a session can change anything the
person can: a config file under `~/.config`, a launcher entry, a downloaded
archive, a package. This module is the record of that and the way back.

What it keeps, per session, under one directory:

- **Files.** The first time a file tool or a command is about to change a path
  outside the folder, the original is copied to `blobs/` and a record names
  it. Whether the path was then created, changed or deleted is read from the
  disk when the record is shown, so a write that changed nothing shows
  nothing. Undo copies the backup back byte for byte; a file the session
  created is removed only when the person confirms (`undo`).
- **Downloads and packages.** A command that is `curl`/`wget` with a URL, or a
  package manager's install/remove, is recorded from its words, with the
  download's size read from the file it wrote.
- **Not tracked.** A command whose effects cannot be read from its arguments
  (a script, an installer, `make install`, anything with a variable in a path)
  is listed as such, with the reason. It is never left out.

The limit, stated everywhere the record is shown (`LIMITS`): a command is
attributed by the paths it names, read before and after it runs. A change to a
path it does not name is invisible here. That is why only a short list of
programs whose effects are their arguments counts as tracked (`KNOWN`); every
other command is "not tracked".
"""

from __future__ import annotations

import glob
import json
import os
import re
import shlex
import shutil
import time
import uuid
from dataclasses import dataclass, field
from itertools import pairwise
from pathlib import Path
from typing import Any, Final
from urllib.parse import urlsplit

RECORD: Final = "record.jsonl"
BLOBS: Final = "blobs"

MAX_FILE_BYTES: Final = 8 * 1024 * 1024
"""A file larger than this is not copied aside: it is listed as changed with
"no backup", and Undo says it cannot restore it."""

MAX_WATCHED: Final = 2000
"""Entries of a directory a command names that are read before and after it."""

MAX_BACKUPS: Final = 200
"""Files of one command's named directories that are copied aside."""

LIMITS: Final = (
    "A command is attributed by the paths it names, read before and after it runs. "
    "A change to a path it does not name, or made by a program it starts, is not "
    "seen; such commands are listed as not tracked. Undo restores files only: a "
    "package or a download is reversed by hand."
)

KNOWN: Final = frozenset(
    {
        "cp", "mv", "rm", "rmdir", "mkdir", "touch", "tee", "ln", "chmod", "install",
        "ls", "cat", "echo", "printf", "true", "false", "cd", "pwd", "head", "tail",
        "grep", "diff", "stat", "file", "wc", "test", "[", "which", "readlink",
        "realpath", "date", "whoami", "id", "uname", "df", "du", "sed", "tar", "unzip",
        "curl", "wget", "dirname", "basename", "sleep", "export", "set",
    }
)  # fmt: skip
"""Programs whose effects are their arguments' paths. Anything else is opaque."""

_NAMES_PATHS: Final = frozenset(
    {"tar", "unzip", "cp", "mv", "rm", "mkdir", "touch", "tee", "ln", "chmod", "install"}
    | {"sed", "curl", "wget", "echo", "printf", "cat", "ls"}
)
"""Known programs whose plain arguments are paths they touch."""
_WRAPPERS: Final = frozenset({"sudo", "env", "command", "nohup", "setsid", "time", "exec"})
_SEPARATORS: Final = frozenset({";", "&&", "||", "|", "&", "|&", ";;"})
_REDIRECTS: Final = frozenset({">", ">>", ">|", "&>", "2>", "2>>"})
_URL: Final = re.compile(r"^(?:https?|ftp)://", re.IGNORECASE)
_HEREDOC: Final = re.compile(r"<<-?\s*(['\"]?)([A-Za-z_][A-Za-z0-9_]*)\1")
_ASSIGN: Final = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*=")


_URL_IN_TEXT: Final = re.compile(r"(?:https?|ftp)://[^\s'\"]+", re.IGNORECASE)
MAX_COMMAND: Final = 400


def _clean_url(url: str) -> str:
    """The URL without credentials or query: where it points, not how it was let in."""
    parts = urlsplit(url)
    return f"{parts.scheme}://{parts.hostname or ''}{parts.path}"


def _clean_command(command: str) -> str:
    """A command as the record keeps it: URLs cleaned of credentials and query
    strings (a token in `?key=` must not be written to a file the page shows),
    and cut to `MAX_COMMAND` characters."""
    text = _URL_IN_TEXT.sub(lambda found: _clean_url(found.group(0)), command)
    return text if len(text) <= MAX_COMMAND else text[: MAX_COMMAND - 1] + "…"


def _expand_home(token: str) -> str:
    home = str(Path.home())
    if token == "~" or token.startswith("~/"):
        return home + token[1:]
    for form in ("${HOME}", "$HOME"):
        if token.startswith(form):
            return home + token[len(form) :]
    return token


@dataclass
class Plan:
    """What a command's words say it will touch."""

    paths: list[Path] = field(default_factory=list)
    downloads: list[tuple[str, Path | None]] = field(default_factory=list)
    packages: list[dict[str, Any]] = field(default_factory=list)
    opaque: list[str] = field(default_factory=list)
    """Why part of the command is not attributable; empty when all of it is."""


def _strip_heredocs(command: str) -> str:
    """The command without the bodies of its here-documents: a body is data."""
    lines = command.split("\n")
    out: list[str] = []
    ending: str | None = None
    for line in lines:
        if ending is not None:
            if line.strip() == ending:
                ending = None
            continue
        out.append(line)
        found = _HEREDOC.search(line)
        if found:
            ending = found.group(2)
    return "\n".join(out)


def _package_call(program: str, words: list[str]) -> dict[str, Any] | None:
    """The package change a call names, or None when it names none.

    `words` are the program's arguments. Returns manager, action and packages
    for install and remove; an `update` or `upgrade` is not a call this reads.
    """
    flags = {w for w in words if w.startswith("-")}
    names = [w for w in words if not w.startswith("-")]
    manager, action, packages = program, "", []
    if program in ("apt", "apt-get", "dnf", "yum", "flatpak", "pip", "pip3", "npm"):
        verb = names[0] if names else ""
        packages = names[1:]
        installs = {"install", "add", "i"} if program == "npm" else {"install"}
        removes = {"remove", "purge", "erase", "uninstall", "rm", "un"}
        if verb in installs:
            action = "install"
        elif verb in removes:
            action = "remove"
        if program == "npm" and not ({"-g", "--global"} & flags):
            return None  # a project's own dependencies are the folder's business
        if program in ("pip", "pip3") and {"-t", "--target"} & flags:
            return None
        if {"-r", "--requirement", "-e", "--editable", "-c", "--constraint"} & flags or any(
            name.startswith((".", "/", "~")) or "://" in name for name in packages
        ):
            return None  # a file or a path, not a package name: what it installs is not read
    elif program == "pacman":
        for flag in words:
            if flag.startswith("-S") and not flag.startswith("--"):
                action = "install"
            elif flag.startswith("-R") and not flag.startswith("--"):
                action = "remove"
        packages = names
    if not action or not packages:
        return None
    return {"manager": manager, "action": action, "packages": packages}


def _download(program: str, words: list[str], cwd: Path) -> tuple[str, Path | None] | None:
    """(source, destination file) of a `curl` or `wget` call, or None.

    The destination is None when the body goes to the terminal or a pipe."""
    url = next((w for w in words if _URL.match(w)), None)
    if url is None:
        return None
    out: str | None = None
    prefix = cwd
    keep_name = False
    for index, word in enumerate(words):
        following = words[index + 1] if index + 1 < len(words) else None
        if program == "curl":
            if word in ("-o", "--output") and following:
                out = following
            elif word.startswith("--output="):
                out = word.split("=", 1)[1]
            elif word in ("-O", "--remote-name") or (
                word.startswith("-") and not word.startswith("--") and "O" in word
            ):
                keep_name = True
        else:
            if word in ("-O", "--output-document") and following:
                out = following
            elif word.startswith("--output-document="):
                out = word.split("=", 1)[1]
            elif word in ("-P", "--directory-prefix") and following:
                prefix = Path(_expand_home(following))
            elif word.startswith("--directory-prefix="):
                prefix = Path(_expand_home(word.split("=", 1)[1]))
    name = Path(urlsplit(url).path).name or "index.html"
    if out is not None:
        return url, (cwd / _expand_home(out))
    if program == "wget" or keep_name:
        return url, prefix / name
    return url, None


def plan_command(command: str, workdir: Path) -> Plan:
    """Read a shell command's words for the paths, downloads and package calls
    in it, and for the reasons it cannot all be attributed (`Plan.opaque`).

    It does not run anything. It reads only what a shell would split out of the
    text; a command it cannot parse is opaque, never guessed at.
    """
    plan = Plan()
    try:
        lexer = shlex.shlex(_strip_heredocs(command), posix=True, punctuation_chars=True)
        lexer.whitespace_split = True
        tokens = list(lexer)
    except ValueError as exc:
        plan.opaque.append(f"the command could not be read ({exc})")
        return plan
    cwd = workdir.resolve()
    segment: list[str] = []
    segments: list[list[str]] = []
    for token in tokens:
        if token in _SEPARATORS:
            segments.append(segment)
            segment = []
        else:
            segment.append(token)
    segments.append(segment)
    seen_opaque: set[str] = set()
    for words in segments:
        cwd = _read_segment(words, cwd, workdir, plan, seen_opaque)
    return plan


def _read_segment(
    words: list[str], cwd: Path, workdir: Path, plan: Plan, seen_opaque: set[str]
) -> Path:
    """Add one simple command to `plan`; returns the directory the next one runs in."""
    args: list[str] = []
    skip = False
    for index, word in enumerate(words):
        if skip:
            skip = False
            continue
        if word in _REDIRECTS or re.fullmatch(r"\d?>>?", word):
            target = words[index + 1] if index + 1 < len(words) else ""
            _add_path(target, cwd, workdir, plan)
            skip = True
            continue
        if word in ("<", "<<", "<<<", "<&"):
            skip = True
            continue
        args.append(word)
    while args and (_ASSIGN.match(args[0]) or args[0] in _WRAPPERS):
        wrapper = args.pop(0)
        while wrapper in _WRAPPERS and args and args[0].startswith("-"):
            args.pop(0)  # sudo -n, env -i
    if not args:
        return cwd
    program = os.path.basename(args[0])
    rest = args[1:]
    package = _package_call(program, rest)
    if package is not None:
        plan.packages.append(package)
    elif program in ("apt", "apt-get", "dnf", "yum", "pacman", "flatpak", "pip", "pip3", "npm"):
        if program != "npm" and not rest:
            return cwd
        _opaque(plan, seen_opaque, f"`{program}` changes packages and files it does not name")
    elif program == "python" or program == "python3":
        if rest[:2] == ["-m", "pip"]:
            nested = _package_call("pip", rest[2:])
            if nested is not None:
                plan.packages.append(nested)
            else:
                _opaque(plan, seen_opaque, "`python -m pip` changes files it does not name")
        else:
            _opaque(plan, seen_opaque, f"`{program}` runs code whose changes are not visible here")
    elif program not in KNOWN:
        _opaque(plan, seen_opaque, f"`{program}` runs code whose changes are not visible here")
    if program in ("curl", "wget"):
        found = _download(program, [_expand_home(w) for w in rest], cwd)
        if found is not None:
            plan.downloads.append(found)
    if program == "cd" and rest:
        return (cwd / _expand_home(rest[0])).resolve()
    if program == "sed" and not any(w.startswith("-i") or w == "--in-place" for w in rest):
        return cwd  # sed without -i only reads its files
    if program in _NAMES_PATHS:
        for word in rest:
            if not word.startswith("-"):
                _add_path(word, cwd, workdir, plan)
        for word, following in pairwise(rest):
            if word in ("-C", "-d", "-o", "-O", "-P", "--output", "--directory", "-t"):
                _add_path(following, cwd, workdir, plan)
        for word in rest:
            for prefix in ("--output=", "--directory=", "--output-document="):
                if word.startswith(prefix):
                    _add_path(word.split("=", 1)[1], cwd, workdir, plan)
    elif program not in KNOWN:
        for word in rest:  # an opaque command still names paths worth watching
            if not word.startswith("-"):
                _add_path(word, cwd, workdir, plan, quiet=True)
    for _url, destination in plan.downloads:
        if destination is not None and destination not in plan.paths:
            _add_resolved(destination, workdir, plan)
    return cwd


def _opaque(plan: Plan, seen: set[str], reason: str) -> None:
    if reason not in seen:
        seen.add(reason)
        plan.opaque.append(reason)


def _add_resolved(path: Path, workdir: Path, plan: Plan) -> None:
    resolved = path.resolve()
    base = workdir.resolve()
    if resolved != base and base not in resolved.parents and resolved not in plan.paths:
        plan.paths.append(resolved)


def _add_path(token: str, cwd: Path, workdir: Path, plan: Plan, *, quiet: bool = False) -> None:
    """Name `token` as a path the command touches, when it is outside the folder."""
    if not token or _URL.match(token):
        return
    expanded = _expand_home(token)
    if any(mark in expanded for mark in ("$", "`")):
        if not quiet:
            plan.opaque.append(f"`{token}` names a path through a variable or substitution")
        return
    candidates = (
        [Path(p) for p in sorted(glob.glob(str(cwd / expanded)))]
        if any(c in expanded for c in "*?[")
        else [cwd / expanded]
    )
    for candidate in candidates:
        _add_resolved(candidate, workdir, plan)


# --- the record ---------------------------------------------------------------


@dataclass
class Watch:
    """One command being watched: the plan, and the paths as they were before."""

    command: str
    plan: Plan
    before: dict[Path, bool] = field(default_factory=dict)
    """Each named path -> whether it existed before the command."""
    listed: dict[Path, set[Path]] = field(default_factory=dict)
    """Each named directory -> the entries read from it before the command."""
    started: float = field(default_factory=time.time)


def _same(a: Path, b: Path) -> bool:
    try:
        return a.read_bytes() == b.read_bytes()
    except OSError:
        return False


class SideEffects:
    """A session's record of what it changed outside its folder."""

    def __init__(self, root: Path) -> None:
        self.root = Path(root)

    # -- storage --------------------------------------------------------

    def _path(self) -> Path:
        return self.root / RECORD

    def _read(self) -> list[dict[str, Any]]:
        try:
            lines = self._path().read_text(encoding="utf-8").splitlines()
        except OSError:
            return []
        out: list[dict[str, Any]] = []
        for line in lines:
            try:
                out.append(json.loads(line))
            except ValueError:
                continue  # a torn tail loses one record, not the record
        return out

    def _write_all(self, records: list[dict[str, Any]]) -> None:
        self.root.mkdir(parents=True, exist_ok=True)
        self._path().write_text(
            "".join(json.dumps(r, ensure_ascii=False) + "\n" for r in records), encoding="utf-8"
        )

    def _append(self, record: dict[str, Any]) -> None:
        self.root.mkdir(parents=True, exist_ok=True)
        if isinstance(record.get("command"), str):
            record = {**record, "command": _clean_command(record["command"])}
        with self._path().open("a", encoding="utf-8") as handle:
            handle.write(json.dumps({**record, "t": time.time()}, ensure_ascii=False) + "\n")

    def _recorded(self) -> set[str]:
        return {str(r.get("path")) for r in self._read() if r.get("kind") == "file"}

    # -- files ----------------------------------------------------------

    def before_file(
        self,
        path: Path,
        *,
        via: str,
        command: str | None = None,
        known: set[str] | None = None,
        copy: bool = True,
    ) -> bool:
        """Back `path` up, once, before the session first changes it.

        Returns False when the path already has a record (the earlier original
        is the one to keep) or is a directory. A file too big or unreadable is
        recorded with `backup` saying so, never skipped. `known` is the paths
        already recorded, when the caller has just read them."""
        recorded = self._recorded() if known is None else known
        if str(path) in recorded:
            return False
        recorded.add(str(path))
        existed = path.is_file()
        blob: str | None = None
        backup = "none needed"
        stamp: list[int] | None = None
        if existed:
            try:
                info = path.stat()
                stamp = [info.st_size, info.st_mtime_ns]
                if not copy:
                    backup = "not copied: its directory holds too many files"
                elif info.st_size > MAX_FILE_BYTES:
                    backup = "too large to back up"
                else:
                    blob = uuid.uuid4().hex
                    (self.root / BLOBS).mkdir(parents=True, exist_ok=True)
                    shutil.copy2(path, self.root / BLOBS / blob)
                    backup = "backed up"
            except OSError:
                blob, backup = None, "could not be read"
        self._append(
            {
                "kind": "file",
                "path": str(path),
                "existed": existed,
                "blob": blob,
                "stamp": stamp,
                "backup": backup,
                "via": via,
                "command": command,
            }
        )
        return True

    def settle_files(self, paths: set[Path]) -> None:
        """Drop the record of each of `paths` the session left as it found it.

        Without this a stale backup would outlive an unchanged file, and a
        later Undo would put an old copy over what the person did since."""
        records = self._read()
        keep: list[dict[str, Any]] = []
        for record in records:
            if (
                record.get("kind") == "file"
                and Path(record["path"]) in paths
                and self._state(record) == "unchanged"
            ):
                if record.get("blob"):
                    (self.root / BLOBS / record["blob"]).unlink(missing_ok=True)
                continue
            keep.append(record)
        if len(keep) != len(records):
            self._write_all(keep)

    def _state(self, record: dict[str, Any]) -> str:
        path = Path(record["path"])
        here = path.exists()
        if record.get("existed"):
            if not here:
                return "deleted"
            if record.get("blob"):
                same = _same(path, self.root / BLOBS / record["blob"])
            else:
                info = path.stat()
                same = record.get("stamp") == [info.st_size, info.st_mtime_ns]
            return "unchanged" if same else "changed"
        return "created" if here else "unchanged"

    # -- commands -------------------------------------------------------

    def watch(self, command: str, workdir: Path) -> Watch:
        """Plan `command` and back up every existing path it names, before it runs."""
        watch = Watch(command=command, plan=plan_command(command, workdir))
        backups = 0
        known = self._recorded()
        for path in watch.plan.paths:
            watch.before[path] = path.exists()
            if path.is_dir():
                entries: set[Path] = set()
                for entry in self._walk(path):
                    entries.add(entry)
                    backups += 1
                    self.before_file(
                        entry,
                        via="run_command",
                        command=command,
                        known=known,
                        copy=backups <= MAX_BACKUPS,
                    )
                watch.listed[path] = entries
                if len(entries) >= MAX_WATCHED:
                    watch.plan.opaque.append(
                        f"{path} has more than {MAX_WATCHED} entries; only some were watched"
                    )
            else:
                self.before_file(path, via="run_command", command=command, known=known)
        return watch

    @staticmethod
    def _walk(top: Path) -> list[Path]:
        out: list[Path] = []
        for dirpath, dirnames, filenames in os.walk(top):
            dirnames.sort()
            for name in sorted(filenames):
                out.append(Path(dirpath) / name)
                if len(out) >= MAX_WATCHED:
                    return out
        return out

    def settle(self, watch: Watch, *, exit_code: int | None, background: bool = False) -> None:
        """Record what the command did, after it ran (or, in the background, started)."""
        command = watch.command
        reasons = list(watch.plan.opaque)
        if background:
            reasons.append(
                "it runs in the background, so what it changes after it returns is not seen"
            )
        known = self._recorded()
        for path, existed in watch.before.items():
            if existed or not path.exists():
                continue
            created = [path] if path.is_file() else [path, *self._walk(path)]
            for item in created:
                self.before_file_created(item, command, known)
        for top, entries in watch.listed.items():
            for entry in self._walk(top):
                if entry not in entries:
                    self.before_file_created(entry, command, known)
        self.settle_files({*watch.before, *(e for es in watch.listed.values() for e in es)})
        for source, destination in watch.plan.downloads:
            parts = urlsplit(source)
            shown = _clean_url(source)
            size: int | None = None
            if destination is not None and destination.is_file():
                size = destination.stat().st_size
            self._append(
                {
                    "kind": "download",
                    "source": shown,
                    "host": parts.hostname or "",
                    "path": str(destination) if destination is not None else None,
                    "size": size,
                    "command": command,
                    "exit": exit_code,
                }
            )
        for package in watch.plan.packages:
            self._append({"kind": "package", **package, "command": command, "exit": exit_code})
        if reasons or watch.plan.packages:
            more = ["the files a package manager installs or removes are not listed"]
            self._append(
                {
                    "kind": "command",
                    "command": command,
                    "reasons": reasons or more,
                    "exit": exit_code,
                }
            )

    def before_file_created(self, path: Path, command: str, known: set[str]) -> None:
        """Record that `path` did not exist before `command` (it does now)."""
        if str(path) in known:
            return
        known.add(str(path))
        self._append(
            {
                "kind": "file",
                "path": str(path),
                "existed": False,
                "blob": None,
                "backup": "none needed",
                "via": "run_command",
                "command": command,
                "dir": path.is_dir(),
            }
        )

    # -- reading and undoing --------------------------------------------

    def view(self) -> dict[str, Any]:
        """The record as the page shows it: what changed, what was fetched or
        installed, what is not tracked, and what Undo would do."""
        files: list[dict[str, Any]] = []
        out: dict[str, Any] = {
            "files": files,
            "downloads": [],
            "packages": [],
            "not_tracked": [],
            "undone": [],
            "limits": LIMITS,
        }
        for record in self._read():
            kind = record.get("kind")
            if kind == "file":
                change = self._state(record)
                if change != "unchanged":
                    files.append(
                        {
                            "path": record["path"],
                            "change": change,
                            "backup": record.get("backup"),
                            "via": record.get("via"),
                            "command": record.get("command"),
                            "dir": bool(record.get("dir")),
                        }
                    )
            elif kind == "download":
                out["downloads"].append(record)
            elif kind == "package":
                out["packages"].append(record)
            elif kind == "command":
                out["not_tracked"].append(record)
            else:
                out["undone"].append(record)
        out["can_restore"] = sum(
            1 for f in files if f["change"] in ("changed", "deleted") and f["backup"] == "backed up"
        )
        out["can_delete"] = sum(1 for f in files if f["change"] == "created")
        out["empty"] = not any(out[k] for k in ("files", "downloads", "packages", "not_tracked"))
        return out

    def undo(self, *, delete_created: bool) -> dict[str, Any]:
        """Put back what has a backup; remove what the session created only
        when `delete_created` (the person's confirmation).

        Returns what was restored, deleted, kept (created files not confirmed)
        and failed (no backup, or the write failed)."""
        restored: list[str] = []
        deleted: list[str] = []
        kept: list[str] = []
        failed: list[str] = []
        records = self._read()
        done: set[str] = set()
        dirs: list[Path] = []
        for record in records:
            if record.get("kind") != "file":
                continue
            path = Path(record["path"])
            change = self._state(record)
            if change == "unchanged":
                continue
            try:
                if change in ("changed", "deleted"):
                    if not record.get("blob"):
                        failed.append(str(path))
                        continue
                    path.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(self.root / BLOBS / record["blob"], path)
                    restored.append(str(path))
                    done.add(str(path))
                elif not delete_created:
                    kept.append(str(path))
                elif record.get("dir") or path.is_dir():
                    dirs.append(path)
                    done.add(str(path))
                else:
                    path.unlink(missing_ok=True)
                    deleted.append(str(path))
                    done.add(str(path))
            except OSError:
                failed.append(str(path))
        for directory in sorted(dirs, key=lambda p: len(p.parts), reverse=True):
            try:
                directory.rmdir()
                deleted.append(str(directory))
            except OSError:
                kept.append(str(directory))
                done.discard(str(directory))
        if done:
            for record in records:
                if record.get("path") in done and record.get("blob"):
                    (self.root / BLOBS / record["blob"]).unlink(missing_ok=True)
            left = [r for r in records if not (r.get("kind") == "file" and r.get("path") in done)]
            left.append(
                {"kind": "undo", "t": time.time(), "restored": restored, "deleted": deleted}
            )
            self._write_all(left)
        return {"restored": restored, "deleted": deleted, "kept": kept, "failed": failed}
