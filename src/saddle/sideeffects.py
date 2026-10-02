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
from itertools import pairwise, takewhile
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
_VAR: Final = re.compile(r"\$(?:\{([A-Za-z_][A-Za-z0-9_]*)\}|([A-Za-z_][A-Za-z0-9_]*))")
_BRACES: Final = re.compile(r"\{[^{}]*(?:,|\.\.)[^{}]*\}")
_SHELLS: Final = frozenset({"bash", "sh", "zsh", "dash"})

READ_ONLY: Final = frozenset(
    {
        "cat", "ls", "find", "grep", "egrep", "fgrep", "head", "tail", "wc", "file", "du",
        "df", "echo", "printf", "stat", "which", "type", "readlink", "realpath", "pwd",
        "date", "whoami", "id", "uname", "basename", "dirname", "sort", "uniq", "cut", "tr",
        "diff", "cmp", "sha256sum", "sha1sum", "md5sum", "tree", "nl", "od", "xxd", "ps",
        "pgrep", "hostname", "nproc", "free", "uptime", "lsblk", "test", "[", "[[", "true",
        "false", "sleep", "cd", "read",
    }
)  # fmt: skip
"""Programs that only read: a command made of nothing else is not recorded (#136
F2). `find` and `sort` are in it only without the flags that write
(`_WRITING_FLAGS`); an output redirect to anything but `/dev/null` takes a
command out of it, and so does a substitution, which can run anything."""

_WRITING_FLAGS: Final = {
    "find": frozenset(
        {"-delete", "-exec", "-execdir", "-ok", "-okdir", "-fprint", "-fprint0", "-fprintf", "-fls"}
    ),
    "sort": frozenset({"-o", "--output"}),
}
_SHELL_WORDS: Final = frozenset(
    {"do", "then", "else", "elif", "if", "while", "until", "!", "{", "}", "(", ")"}
)
_SHELL_ENDS: Final = frozenset({"done", "fi", "esac", ";;"})
_SHELL_HEADS: Final = frozenset({"for", "select"})

KILLERS: Final = frozenset({"pkill", "killall"})
"""Programs that stop processes by name or pattern, so they can hit the person's
other programs: held in a full-access session (F1)."""
_DESTROYERS: Final = frozenset({"rm", "rmdir", "shred", "unlink", "mv"})
_EXECS: Final = frozenset({"-exec", "-execdir", "-ok", "-okdir"})
DEVICE_ROOTS: Final = frozenset({"dev", "proc", "sys"})


_URL_IN_TEXT: Final = re.compile(r"(?:https?|ftp)://[^\s'\"]+", re.IGNORECASE)
MAX_COMMAND: Final = 400


def _clean_url(url: str) -> str:
    """The URL without credentials or query: where it points, not how it was let in."""
    parts = urlsplit(url)
    host = parts.hostname or ""
    if ":" in host:
        host = f"[{host}]"  # an IPv6 literal
    try:
        port = parts.port
    except ValueError:
        port = None
    return f"{parts.scheme}://{host}{f':{port}' if port else ''}{parts.path}"


def clean_command(command: str) -> str:
    """A command as the record keeps it: URLs cleaned of credentials and query
    strings (a token in `?key=` must not be written to a file the page shows),
    and cut to `MAX_COMMAND` characters."""
    text = _URL_IN_TEXT.sub(lambda found: _clean_url(found.group(0)), command)
    return text if len(text) <= MAX_COMMAND else text[: MAX_COMMAND - 1] + "…"


def expand_home(token: str) -> str:
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
    read_only: bool = False
    """The command is made only of programs that read (`READ_ONLY`): nothing to record."""
    targets: list[Path] = field(default_factory=list)
    """Concrete paths outside the folder a destructive command (`_note_risks`) removes,
    moves, truncates or syncs over."""
    unresolved: list[str] = field(default_factory=list)
    """Targets of a destructive command that could not be resolved to paths, each with why."""
    kills: list[str] = field(default_factory=list)
    """Pattern-killing programs (`KILLERS`) the command runs."""


def is_special_path(path: Path) -> bool:
    """A path under `/dev`, `/proc` or `/sys`: not a file a session creates or loses."""
    parts = path.parts
    return len(parts) >= 2 and parts[0] == "/" and parts[1] in DEVICE_ROOTS


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
                prefix = Path(expand_home(following))
            elif word.startswith("--directory-prefix="):
                prefix = Path(expand_home(word.split("=", 1)[1]))
    name = Path(urlsplit(url).path).name or "index.html"
    if out is not None:
        return url, (cwd / expand_home(out))
    if program == "wget" or keep_name:
        return url, prefix / name
    return url, None


def _statements(text: str) -> str:
    """`text` with each unquoted newline made a `;` and comments and line
    continuations removed: shlex reads a newline as a space, which would fold a
    script's second line into the first one's arguments."""
    out: list[str] = []
    quote = ""
    i = 0
    while i < len(text):
        char = text[i]
        if quote:
            out.append(char)
            if char == "\\" and quote == '"' and i + 1 < len(text):
                out.append(text[i + 1])
                i += 2
                continue
            if char == quote:
                quote = ""
        elif char == "\\" and i + 1 < len(text):
            if text[i + 1] != "\n":
                out.extend((char, text[i + 1]))
            i += 2
            continue
        elif char in "'\"":
            quote = char
            out.append(char)
        elif char == "#" and (i == 0 or text[i - 1] in " \t\n;&|("):
            while i < len(text) and text[i] != "\n":
                i += 1
            continue
        elif char == "\n":
            out.append(";")
        else:
            out.append(char)
        i += 1
    return "".join(out)


def _expand(token: str, env: dict[str, str | None]) -> tuple[str, str]:
    """(`token` with `~`, `$HOME` and the variables the command set earlier filled
    in, "") or ("", why it cannot be). No general shell evaluation: a command
    substitution, an unset variable and `~user` are not read."""
    home = str(Path.home())
    if token == "~" or token.startswith("~/"):
        token = home + token[1:]
    elif token.startswith("~"):
        return "", "names another user's home, which is not read"
    if "`" in token or "$(" in token:
        return "", "names a path through a command substitution"
    missing: list[str] = []

    def fill(found: re.Match[str]) -> str:
        name = found.group(1) or found.group(2)
        value = env[name] if name in env else (home if name == "HOME" else None)
        if value is None:
            missing.append(name)
            return found.group(0)
        return value

    text = _VAR.sub(fill, token)
    if missing:
        return (
            "",
            f"names a path through ${missing[0]}, which it does not set to a path it can read",
        )
    if "$" in text:
        return "", "names a path through a variable it cannot read"
    return text, ""


def _resolve(token: str, cwd: Path | None, env: dict[str, str | None]) -> tuple[list[Path], str]:
    """(the paths `token` names, "") or ([], why they cannot be read). A glob
    names what it matches now."""
    text, why = _expand(token, env)
    if not why and _BRACES.search(text):
        why = "names a path through brace expansion"
    if not why and cwd is None and not text.startswith("/"):
        why = "is relative to a directory a cd moved to that could not be read"
    if why:
        return [], why
    base = cwd or Path("/")
    if any(c in text for c in "*?["):
        return [Path(p) for p in sorted(glob.glob(str(base / text)))], ""
    return [base / text], ""


def _only_reads(segments: list[list[str]], text: str) -> bool:
    """Whether every program in the command is a read-only one (`READ_ONLY`), with
    no output redirect to a file, no writing flag and no substitution."""
    if "$(" in text or "`" in text or "<(" in text or ">(" in text:
        return False
    for words in segments:
        args: list[str] = []
        skip = False
        for index, word in enumerate(words):
            if skip:
                skip = False
                continue
            if re.fullmatch(r"\d*(?:>>?|>\||&>>?|>&)", word):
                target = words[index + 1] if index + 1 < len(words) else ""
                dup = word.endswith("&") and (target.isdigit() or target == "-")
                if target != "/dev/null" and not dup:
                    return False
                skip = True
            elif word in ("<", "<<", "<<<", "<&"):
                skip = True
            else:
                args.append(word)
        while args and (args[0] in _SHELL_WORDS or _ASSIGN.match(args[0]) or args[0] in _WRAPPERS):
            wrapper = args.pop(0)
            while wrapper in _WRAPPERS and args and args[0].startswith("-"):
                args.pop(0)
        if not args or args[0] in _SHELL_HEADS or args[0] in _SHELL_ENDS:
            continue
        program = os.path.basename(args[0])
        if program not in READ_ONLY or _WRITING_FLAGS.get(program, frozenset()) & set(args[1:]):
            return False
    return True


def plan_command(command: str, workdir: Path) -> Plan:
    """Read a shell command's words for the paths, downloads and package calls
    in it, and for the reasons it cannot all be attributed (`Plan.opaque`).

    It does not run anything. It reads only what a shell would split out of the
    text; a command it cannot parse is opaque, never guessed at.
    """
    plan = Plan()
    text = _statements(_strip_heredocs(command))
    segments = _segments(text, plan)
    if segments is None:
        return plan
    if _only_reads(segments, text):
        plan.read_only = True
        return plan
    _read_segments(segments, workdir.resolve(), workdir, plan, set(), {})
    return plan


def _segments(text: str, plan: Plan) -> list[list[str]] | None:
    """The simple commands of `text` as word lists, or None (with the reason in
    `plan.opaque`) when the text cannot be split."""
    try:
        lexer = shlex.shlex(text, posix=True, punctuation_chars=True)
        lexer.whitespace_split = True
        tokens = list(lexer)
    except ValueError as exc:
        plan.opaque.append(f"the command could not be read ({exc})")
        return None
    segment: list[str] = []
    segments: list[list[str]] = []
    for token in tokens:
        if token in _SEPARATORS:
            segments.append(segment)
            segment = []
        else:
            segment.append(token)
    segments.append(segment)
    return segments


def _read_segments(
    segments: list[list[str]],
    cwd: Path | None,
    workdir: Path,
    plan: Plan,
    seen_opaque: set[str],
    env: dict[str, str | None],
) -> None:
    for words in segments:
        cwd = _read_segment(words, cwd, workdir, plan, seen_opaque, env)


def _assign(words: list[str], env: dict[str, str | None]) -> None:
    """Remember `NAME=value` words: the value as far as it can be read, else None."""
    for word in words:
        name, _, value = word.partition("=")
        text, why = _expand(value, env)
        env[name] = None if why else text


def _destroy(
    plan: Plan,
    what: str,
    token: str,
    cwd: Path | None,
    workdir: Path,
    env: dict[str, str | None],
) -> None:
    """Name `token` as a target of a destructive command: a path outside the folder
    that exists is backed up (or the command is held when it is too big for that),
    and one that cannot be resolved holds the command."""
    paths, why = _resolve(token, cwd, env)
    if why:
        plan.unresolved.append(f"`{token}` ({what}) {why}")
        return
    base = workdir.resolve()
    for path in paths:
        resolved = path.resolve()
        if is_special_path(resolved) or resolved == base or base in resolved.parents:
            continue
        plan.targets.append(resolved)
        _add_resolved(resolved, workdir, plan)


def _note_risks(
    program: str,
    rest: list[str],
    cwd: Path | None,
    workdir: Path,
    plan: Plan,
    env: dict[str, str | None],
) -> None:
    """The destructive calls this explicit set of programs makes (F1): `rm` that
    recurses or removes a directory, `rmdir`, `shred`, `unlink`, `truncate`, `mv`,
    `find` with `-delete` or an `-exec` of one of those, `rsync` with `--delete`,
    and `xargs` handing one of them arguments from a pipe; plus `KILLERS`."""
    ended = rest.index("--") if "--" in rest else len(rest)
    plain = [w for i, w in enumerate(rest) if w != "--" and (i >= ended or not w.startswith("-"))]
    short = "".join(w[1:] for w in rest[:ended] if w.startswith("-") and not w.startswith("--"))
    longs = {w.split("=")[0] for w in rest[:ended] if w.startswith("--")}

    def hit(what: str, tokens: list[str]) -> None:
        for token in tokens:
            _destroy(plan, what, token, cwd, workdir, env)

    if program in KILLERS:
        plan.kills.append(program)
    elif program == "rm" and (set("rRd") & set(short) or {"--recursive", "--dir"} & longs):
        hit("rm removes", plain)
    elif program in ("rmdir", "shred", "unlink", "truncate"):
        hit(f"{program} changes", plain)
    elif program == "mv":
        hit("mv moves or overwrites", plain + [f for a, f in pairwise(rest) if a == "-t"])
    elif program == "find":
        execs = [rest[i + 1] for i, w in enumerate(rest[:-1]) if w in _EXECS]
        removes = any(os.path.basename(x) in _DESTROYERS for x in execs)
        if "-delete" in rest or removes:
            starts = list(
                takewhile(lambda w: not w.startswith("-") and w not in ("(", "!", ")"), rest)
            )
            hit("find removes under", starts or ["."])
    elif program == "rsync" and (
        {"--remove-source-files"} | {w for w in longs if w.startswith("--delete")}
    ):
        hit("rsync --delete overwrites", plain[-1:])
    elif program == "xargs" and any(os.path.basename(w) in _DESTROYERS for w in rest):
        plan.unresolved.append(
            "`xargs` (removes or moves) takes its targets from another command's output"
        )


def _read_segment(
    words: list[str],
    cwd: Path | None,
    workdir: Path,
    plan: Plan,
    seen_opaque: set[str],
    env: dict[str, str | None],
) -> Path | None:
    """Add one simple command to `plan`; returns the directory the next one runs in
    (None when a `cd` moved it somewhere that could not be read)."""
    args: list[str] = []
    skip = False
    for index, word in enumerate(words):
        if skip:
            skip = False
            continue
        if word in _REDIRECTS or re.fullmatch(r"\d?>>?", word):
            target = words[index + 1] if index + 1 < len(words) else ""
            _add_path(target, cwd, workdir, plan, env)
            if ">>" not in word:
                _destroy(plan, "truncates", target, cwd, workdir, env)
            skip = True
            continue
        if word in ("<", "<<", "<<<", "<&"):
            skip = True
            continue
        args.append(word)
    assigned: list[str] = []
    while args and (_ASSIGN.match(args[0]) or args[0] in _WRAPPERS):
        wrapper = args.pop(0)
        if _ASSIGN.match(wrapper):
            assigned.append(wrapper)
        while wrapper in _WRAPPERS and args and args[0].startswith("-"):
            args.pop(0)  # sudo -n, env -i
    if not args:
        _assign(assigned, env)
        return cwd
    program = os.path.basename(args[0])
    rest = args[1:]
    if program == "export":
        _assign([w for w in rest if _ASSIGN.match(w)], env)
    _note_risks(program, rest, cwd, workdir, plan, env)
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
    elif program in _SHELLS and "-c" in rest[:-1]:
        inner = _segments(_statements(rest[rest.index("-c") + 1]), plan)
        if inner is not None:
            _read_segments(inner, cwd, workdir, plan, seen_opaque, dict(env))
    elif program not in KNOWN:
        _opaque(plan, seen_opaque, f"`{program}` runs code whose changes are not visible here")
    if program in ("curl", "wget"):
        found = _download(program, [expand_home(w) for w in rest], cwd or workdir)
        if found is not None:
            plan.downloads.append(found)
    if program == "cd":
        paths, _why = _resolve(rest[0] if rest else "~", cwd, env)
        return paths[0].resolve() if len(paths) == 1 and rest[:1] != ["-"] else None
    if program in ("pushd", "popd"):
        return None
    if program == "sed" and not any(w.startswith("-i") or w == "--in-place" for w in rest):
        return cwd  # sed without -i only reads its files
    if program in _NAMES_PATHS:
        for word in rest:
            if not word.startswith("-"):
                _add_path(word, cwd, workdir, plan, env)
        for word, following in pairwise(rest):
            if word in ("-C", "-d", "-o", "-O", "-P", "--output", "--directory", "-t"):
                _add_path(following, cwd, workdir, plan, env)
        for word in rest:
            for prefix in ("--output=", "--directory=", "--output-document="):
                if word.startswith(prefix):
                    _add_path(word.split("=", 1)[1], cwd, workdir, plan, env)
    elif program not in KNOWN:
        for word in rest:  # an opaque command still names paths worth watching
            if not word.startswith("-"):
                _add_path(word, cwd, workdir, plan, env, quiet=True)
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
    if is_special_path(resolved):
        return
    if resolved != base and base not in resolved.parents and resolved not in plan.paths:
        plan.paths.append(resolved)


def _add_path(
    token: str,
    cwd: Path | None,
    workdir: Path,
    plan: Plan,
    env: dict[str, str | None],
    *,
    quiet: bool = False,
) -> None:
    """Name `token` as a path the command touches, when it is outside the folder."""
    if not token or _URL.match(token):
        return
    candidates, why = _resolve(token, cwd, env)
    if why:
        if not quiet:
            plan.opaque.append(f"`{token}` {why}")
        return
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
            record = {**record, "command": clean_command(record["command"])}
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
        if str(path) in recorded or is_special_path(path):
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

    def hold(self, command: str, workdir: Path) -> list[str]:
        """Why `command` must be put to the person before it runs; empty when it may
        run (F1). A destructive command (`_note_risks`) is held when a target cannot
        be resolved to a path, or what it removes is too big to back up under
        `MAX_FILE_BYTES` and `MAX_BACKUPS`; a pattern kill (`KILLERS`) is always held."""
        plan = plan_command(command, workdir)
        reasons = list(dict.fromkeys(plan.unresolved))
        for program in dict.fromkeys(plan.kills):
            reasons.append(
                f"`{program}` stops programs by name or pattern, so it can stop your other "
                "programs, not only this session's; the processes tool stops this "
                "session's own"
            )
        files = 0
        for target in dict.fromkeys(plan.targets):
            try:
                if not (target.exists() or target.is_symlink()):
                    continue
                inside = (
                    [target] if target.is_symlink() or not target.is_dir() else self._walk(target)
                )
                if len(inside) >= MAX_WATCHED and target.is_dir():
                    reasons.append(
                        f"{target} holds {MAX_WATCHED} or more files, too many to back up"
                    )
                    continue
                files += len(inside)
                big = next((f for f in inside if f.lstat().st_size > MAX_FILE_BYTES), None)
                if big is not None:
                    reasons.append(
                        f"{big} is over {MAX_FILE_BYTES // 2**20} MB, too large to back up"
                    )
            except OSError as exc:
                reasons.append(f"{target} could not be read to back it up ({type(exc).__name__})")
        if files > MAX_BACKUPS:
            reasons.append(
                f"it removes {files} files, more than the {MAX_BACKUPS} backed up per command"
            )
        return reasons

    def watch(self, command: str, workdir: Path, approved: tuple[str, ...] = ()) -> Watch:
        """Plan `command` and back up every existing path it names, before it runs.
        `approved` are the reasons the person approved a held command despite; they
        are listed as not tracked."""
        watch = Watch(command=command, plan=plan_command(command, workdir))
        watch.plan.opaque.extend(
            f"approved by the person, not fully backed up: {r}" for r in approved
        )
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
        if background and not watch.plan.read_only:
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

    def note_untracked(self, command: str, reason: str) -> None:
        """Record a command as not tracked, for a reason found while watching it."""
        self._append({"kind": "command", "command": command, "reasons": [reason], "exit": None})

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
