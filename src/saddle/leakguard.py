"""Refuse private paths, host identities, secrets and internal references in public text.

The contract: a commit or push that would publish a match of any rule below
is refused, in file content, in commit messages and in author/committer
identities, unless the matched text is, character for character, one of the
known test fakes in `ALLOWED_VALUES`. Allowing is by value, never by file:
a real key pasted into a fixture file is still caught.

Three entry points, wired by the hooks in `tools/githooks/`:

- `pre-commit`: the staged (index) content of every added or modified file;
- `commit-msg FILE`: the message being written, comment lines dropped;
- `pre-push REMOTE [URL]`: for every commit being pushed that the remote
  does not have (`--not --remotes=REMOTE`, and the remote's old tip), its
  message, its author and committer identities, and the lines it adds.
  History the remote already holds is never re-judged.

Two more for audits, `tree [REV]` and `messages RANGE`, report without a
hook.

Built-in rules are generic on purpose: this file is public, so it must not
contain the private names it looks for. Those come from a per-maintainer,
untracked pattern file, one Python regex per line (`#` comments and blank
lines skipped), read from `$SADDLE_LEAK_PATTERNS` or else
`~/.config/saddle/leak-patterns.txt`. A missing file is announced, not
fatal; an unreadable or invalid one is an error (exit 2), because a lookup
that fails must never read as "nothing found".

Tests build their known-bad values at run time from fragments, so that this
guard, run over the tree, does not refuse its own tests.
"""

from __future__ import annotations

import argparse
import os
import re
import subprocess
import sys
from collections import Counter
from collections.abc import Callable, Iterable, Iterator, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Final, TextIO

PATTERNS_ENV: Final = "SADDLE_LEAK_PATTERNS"
DEFAULT_PATTERNS: Final = Path("~/.config/saddle/leak-patterns.txt")

EXIT_CLEAN: Final = 0
EXIT_HITS: Final = 1
EXIT_CONFIG: Final = 2

ZERO_SHA: Final = "0" * 40

# Domains a machine gives itself, never a mailbox: git's fallback identity is
# `user@<fully qualified hostname>`, and home routers hand out these search
# domains over DHCP.
HOST_SUFFIXES: Final = (
    ".local",
    ".localdomain",
    ".lan",
    ".home",
    ".internal",
    ".localhost",
    ".(none)",
    ".home.arpa",
    ".mynetworksettings.com",
    ".attlocal.net",
)
_COMCAST_DHCP: Final = re.compile(r"\.hsd1\.[a-z]{2}\.comcast\.net$")


def is_host_domain(email: str) -> bool:
    """True when the address's domain names a machine, not a mail domain.

    A domain names a machine when a host label sits in front of a suffix in
    `HOST_SUFFIXES` (`<user>@<host>.lan`, git's `<user>@<host>.(none)`), or
    when it is a Comcast DHCP name. The bare suffix names no machine (`saddle@localhost`,
    `user@localhost` in validator fixtures), and neither does a dotless
    domain: test data is full of `user@example` fragments, and git never
    produces one (it appends `.(none)` when the host has no domain).
    """
    domain = "." + email.rsplit("@", 1)[1].lower()
    named = any(domain.endswith(s) and len(domain) > len(s) for s in HOST_SUFFIXES)
    return named or _COMCAST_DHCP.search(domain) is not None


def _always(_value: str) -> bool:
    return True


@dataclass(frozen=True)
class Rule:
    """A named pattern; `accept` may veto a match (e.g. a real mail domain)."""

    name: str
    pattern: re.Pattern[str]
    accept: Callable[[str], bool] = _always


BUILTIN_RULES: Final[tuple[Rule, ...]] = (
    # An absolute home directory names a local account. `/home/<user>/`
    # placeholders do not match: `<` is not a name character.
    Rule("home-path", re.compile(r"(?<![\w.~-])/(?:home|Users)/[A-Za-z0-9._-]+/")),
    # Coding-agent scratch directories under /tmp. (`[-]` so the rule's own
    # text is not an instance of it.)
    Rule("agent-scratch", re.compile(r"/tmp/claude[-]")),
    Rule(
        "host-email",
        re.compile(r"(?<![\w.+/%-])[\w.%+-]+@[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)*(?:\.\(none\))?"),
        is_host_domain,
    ),
    Rule("aws-key", re.compile(r"\b(?:AKIA|ASIA)[0-9A-Z]{16}\b")),
    Rule("api-key", re.compile(r"\bsk-(?:[A-Za-z0-9]+-)*[A-Za-z0-9_]{12,}")),
    Rule("private-key", re.compile(r"-{5}BEGIN (?:[A-Z0-9]+ )*PRIVATE KEY-{5}")),
    Rule("github-token", re.compile(r"\b(?:gh[pousr]_[A-Za-z0-9]{36,}|github_pat_\w{22,})")),
    # Internal finding and recommendation numbers (`F<n>.<m>`, `Rec <n>`) cite
    # records that are not public. Scoped: a capital F glued to two dotted
    # integers, and the word Rec/Recommendation followed by a number.
    Rule("finding-ref", re.compile(r"(?<![\w.])F\d{1,3}\.\d{1,3}(?![\w.]*\d)")),
    Rule("rec-ref", re.compile(r"\bRec(?:ommendation)? \d{1,3}\b")),
)

ALLOWED_VALUES: Final[frozenset[str]] = frozenset(
    {
        # The documented shape of git's fallback identity (evidence.py).
        "user@host.(none)",
        # Fake keys the test suite plants to prove redaction and sealing.
        "AKIAABCDEFGHIJKLMNOP",
        "sk-abcdefghijkl",
        "sk-abcdefgh1234",
        "sk-livekey12345678",
    }
)
"""Exact matched values that are known test fakes. Nothing else is exempt."""


class GuardConfigError(Exception):
    """The private pattern file exists but cannot be used."""


@dataclass(frozen=True)
class Hit:
    where: str
    line: int
    rule: str
    value: str

    def render(self) -> str:
        return f"{self.where}:{self.line}: {self.rule}: {self.value}"


def patterns_path() -> Path:
    """Where the private pattern file is read from."""
    return Path(os.environ.get(PATTERNS_ENV) or DEFAULT_PATTERNS).expanduser()


def load_private_rules(path: Path) -> tuple[Rule, ...]:
    """One rule per non-blank, non-comment line; `()` if the file is absent."""
    if not path.exists():
        return ()
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except (OSError, UnicodeDecodeError) as exc:
        msg = f"{path}: {exc}"
        raise GuardConfigError(msg) from exc
    rules = []
    for number, raw in enumerate(lines, start=1):
        text = raw.strip()
        if not text or text.startswith("#"):
            continue
        try:
            pattern = re.compile(text)
        except re.error as exc:
            msg = f"{path}:{number}: {exc}"
            raise GuardConfigError(msg) from exc
        rules.append(Rule(f"private:{number}", pattern))
    return tuple(rules)


def scan_line(
    line: str,
    where: str,
    number: int,
    rules: Sequence[Rule],
    allowed: frozenset[str] = ALLOWED_VALUES,
) -> list[Hit]:
    hits = []
    for rule in rules:
        for match in rule.pattern.finditer(line):
            value = match.group(0)
            if value in allowed or not rule.accept(value):
                continue
            hits.append(Hit(where, number, rule.name, value))
    return hits


def scan_text(
    text: str,
    where: str,
    rules: Sequence[Rule],
    allowed: frozenset[str] = ALLOWED_VALUES,
) -> list[Hit]:
    hits = []
    for number, line in enumerate(text.splitlines(), start=1):
        hits.extend(scan_line(line, where, number, rules, allowed))
    return hits


def scan_message(text: str, where: str, rules: Sequence[Rule]) -> list[Hit]:
    """A commit message as git will store it: `#` comment lines dropped."""
    kept = ["" if line.startswith("#") else line for line in text.splitlines()]
    return scan_text("\n".join(kept), where, rules)


def _git(repo: Path, *args: str, stdin: bytes | None = None) -> bytes:
    return subprocess.run(
        ["git", "-C", str(repo), *args],
        input=stdin,
        capture_output=True,
        check=True,
    ).stdout


def _text(data: bytes) -> str:
    return data.decode("utf-8", errors="replace")


def read_blobs(repo: Path, specs: Sequence[str]) -> Iterator[tuple[str, str]]:
    """(spec, content) for each blob spec (`:path`, `REV:path`), one git call."""
    if not specs:
        return
    out = _git(repo, "cat-file", "--batch", stdin="".join(f"{s}\n" for s in specs).encode())
    pos = 0
    for spec in specs:
        end = out.index(b"\n", pos)
        size = int(out[pos:end].split()[2])
        yield spec, _text(out[end + 1 : end + 1 + size])
        pos = end + 1 + size + 1


def _names(listing: bytes) -> list[str]:
    return [name for name in _text(listing).split("\0") if name]


def scan_staged(repo: Path, rules: Sequence[Rule]) -> list[Hit]:
    """The index version of every file the next commit adds or modifies."""
    names = _names(_git(repo, "diff", "--cached", "--name-only", "-z", "--diff-filter=ACMR"))
    hits = []
    for spec, content in read_blobs(repo, [f":{name}" for name in names]):
        hits.extend(scan_text(content, spec[1:], rules))
    return hits


def scan_tree(repo: Path, rev: str, rules: Sequence[Rule]) -> list[Hit]:
    """Every blob tracked at `rev`."""
    names = _names(_git(repo, "ls-tree", "-r", "-z", "--name-only", rev))
    hits = []
    for spec, content in read_blobs(repo, [f"{rev}:{name}" for name in names]):
        hits.extend(scan_text(content, spec.split(":", 1)[1], rules))
    return hits


_HUNK: Final = re.compile(r"^@@ -\S+ \+(\d+)")


def added_lines(diff: str) -> Iterator[tuple[str, int, str]]:
    """(path, new line number, text) for each added line of a `-U0` patch.

    `+++ ` names the file only in a file header (between `diff --git` and the
    first hunk): an added line whose own text begins `++ ` reads `+++ ...`
    in the patch and is content, not a header.
    """
    path = ""
    number = 0
    header = False
    for line in diff.splitlines():
        if line.startswith("diff --git "):
            header = True
        elif header and line.startswith("+++ "):
            path = line[6:] if line.startswith("+++ b/") else line[4:]
        elif hunk := _HUNK.match(line):
            header = False
            number = int(hunk.group(1))
        elif not header and line.startswith("+"):
            yield path, number, line[1:]
            number += 1


def scan_commit(repo: Path, sha: str, rules: Sequence[Rule], *, content: bool) -> list[Hit]:
    """A commit's identities and message, and (with `content`) its added lines."""
    short = sha[:12]
    ident = _text(_git(repo, "log", "-1", "--format=%an <%ae>%n%cn <%ce>", sha))
    hits = scan_text(ident, f"{short} identity", rules)
    message = _text(_git(repo, "log", "-1", "--format=%B", sha))
    hits.extend(scan_text(message, f"{short} message", rules))
    if content:
        args = ("-p", "-U0", "--no-color", "--no-renames", "--root", "--no-commit-id")
        patch = _text(_git(repo, "diff-tree", *args, sha))
        for path, number, text in added_lines(patch):
            hits.extend(scan_line(text, f"{short}:{path}", number, rules))
    return hits


def _is_commit(repo: Path, sha: str) -> bool:
    probe = subprocess.run(
        ["git", "-C", str(repo), "rev-parse", "--verify", "--quiet", f"{sha}^{{commit}}"],
        capture_output=True,
        check=False,
    )
    return probe.returncode == 0


def commits_to_push(repo: Path, remote: str, updates: Iterable[str]) -> list[str]:
    """Commits in the pushed refs that the remote does not already have.

    `updates` are pre-push stdin lines: `<local ref> <local sha> <remote ref>
    <remote sha>`. A deletion (zero local sha) pushes nothing.
    """
    seen: dict[str, None] = {}
    for update in updates:
        fields = update.split()
        if len(fields) != 4 or fields[1] == ZERO_SHA:
            continue
        local, old = fields[1], fields[3]
        exclude = [f"--remotes={remote}"]
        if old != ZERO_SHA and _is_commit(repo, old):
            exclude.append(old)
        listing = _text(_git(repo, "rev-list", local, "--not", *exclude))
        seen.update(dict.fromkeys(listing.split()))
    return list(seen)


def scan_push(repo: Path, remote: str, updates: Iterable[str], rules: Sequence[Rule]) -> list[Hit]:
    hits = []
    for sha in commits_to_push(repo, remote, updates):
        hits.extend(scan_commit(repo, sha, rules, content=True))
    return hits


def scan_messages(repo: Path, revs: str, rules: Sequence[Rule]) -> list[Hit]:
    """Messages and identities (not content) of every commit in `revs`."""
    hits = []
    for sha in _text(_git(repo, "rev-list", revs)).split():
        hits.extend(scan_commit(repo, sha, rules, content=False))
    return hits


def report(hits: Sequence[Hit], out: TextIO) -> int:
    for hit in hits:
        print(hit.render(), file=out)
    if not hits:
        return EXIT_CLEAN
    counts = Counter(hit.rule for hit in hits)
    summary = ", ".join(f"{rule} {n}" for rule, n in sorted(counts.items()))
    print(f"leakguard: refused, {len(hits)} hit(s): {summary}", file=out)
    print(
        "leakguard: remove the text, or, for a known test fake only, add its exact "
        "value to ALLOWED_VALUES in src/saddle/leakguard.py",
        file=out,
    )
    return EXIT_HITS


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="python -m saddle.leakguard", description=__doc__)
    parser.add_argument("--repo", type=Path, default=Path())
    sub = parser.add_subparsers(dest="mode", required=True)
    sub.add_parser("pre-commit")
    sub.add_parser("commit-msg").add_argument("file", type=Path)
    push = sub.add_parser("pre-push")
    push.add_argument("remote")
    push.add_argument("url", nargs="?")
    sub.add_parser("tree").add_argument("rev", nargs="?", default="HEAD")
    sub.add_parser("messages").add_argument("revs")
    return parser


def main(
    argv: Sequence[str] | None = None, stdin: TextIO = sys.stdin, out: TextIO = sys.stderr
) -> int:
    args = _parser().parse_args(argv)
    path = patterns_path()
    try:
        private = load_private_rules(path)
    except GuardConfigError as exc:
        print(f"leakguard: unusable private pattern file: {exc}", file=out)
        return EXIT_CONFIG
    if not path.exists():
        print(f"leakguard: no private pattern file at {path}; built-in rules only", file=out)
    rules = (*BUILTIN_RULES, *private)
    repo: Path = args.repo
    hits: list[Hit]
    if args.mode == "pre-commit":
        hits = scan_staged(repo, rules)
    elif args.mode == "commit-msg":
        hits = scan_message(_text(args.file.read_bytes()), "commit message", rules)
    elif args.mode == "pre-push":
        hits = scan_push(repo, args.remote, stdin.read().splitlines(), rules)
    elif args.mode == "tree":
        hits = scan_tree(repo, args.rev, rules)
    else:
        hits = scan_messages(repo, args.revs, rules)
    return report(hits, out)


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
