"""Search/replace edit blocks: what a worker emits instead of a diff (T6-75).

`DIFF_GRAMMAR` could express only whole-file writes -- every section was
`--- /dev/null` with a `@@ -0,0 +1,N @@` anchor and a body of `+` lines --
so editing a file meant re-emitting it entire. At the measured 80 tok/s
that is ~6 minutes per draw for a 2 300-line module, times three
concurrent draws, and every untouched line has to be reproduced exactly
or the file is corrupted. The cost and the error rate both scale with
file size, which is backwards: saddle could not be pointed at a real
repository at all.

The format here names what to replace instead of restating the file, so
an edit costs the size of the change rather than the size of the file.
It carries no line numbers -- the v3 arm died on `git apply` exit 128 on
every attempt, and hunk arithmetic is the part a model reliably gets
wrong. Uniqueness does the work instead: a search block that matches
zero times, or more than once, is refused rather than guessed at.

Prefix discipline is kept from `DIFF_GRAMMAR`: every content line carries
a leading `-` or `+`, which is what makes "where does this block end?"
decidable from the next line's first character, and lets a file line that
itself begins with `-`, `+`, `=` or `>` round-trip unambiguously. The
applier strips exactly one leading character.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

EDIT_GRAMMAR = r"""root ::= op+
op      ::= edit | create | delete
edit    ::= "edit " path "\n" oline+ "=======\n" nline* ">>>>>>>\n"
create  ::= "create " path "\n" nline* ">>>>>>>\n"
delete  ::= "delete " path "\n"
oline   ::= "-" line "\n"
nline   ::= "+" line "\n"
path    ::= [^/\n] [^\n]*
line    ::= [^\n]*
"""

SEPARATOR = "======="
TERMINATOR = ">>>>>>>"


class EditError(RuntimeError):
    """A malformed edit block, or one that does not apply."""


@dataclass(frozen=True)
class Edit:
    """One operation: replace `search` with `replace` in `path`."""

    op: str
    path: str
    search: str = ""
    replace: str = ""


def _body(lines: list[str], index: int, prefix: str, stop: tuple[str, ...]) -> tuple[str, int]:
    """Consume prefixed content lines until one of `stop`; return text and index."""
    out: list[str] = []
    while index < len(lines):
        line = lines[index]
        if line in stop:
            return "".join(out), index
        if not line.startswith(prefix):
            msg = f"line {index + 1}: expected a {prefix!r}-prefixed line, got {line!r}"
            raise EditError(msg)
        out.append(line[1:] + "\n")
        index += 1
    joined = " or ".join(repr(s) for s in stop)
    msg = f"unterminated block: expected {joined}"
    raise EditError(msg)


def parse_edits(text: str) -> list[Edit]:
    """Parse edit blocks. The grammar constrains the decoder; this refuses
    anything that reaches us malformed anyway (a server without grammar
    support, a replayed fixture), so a bad packet is a spent attempt
    rather than a corrupted tree."""
    lines = text.split("\n")
    # `str.split` always yields at least one element, so the only question
    # is whether the text ended in a newline; a final "" is that newline,
    # not an empty last line.
    if lines[-1] == "":
        lines.pop()
    edits: list[Edit] = []
    index = 0
    while index < len(lines):
        line = lines[index]
        if line.startswith("delete "):
            edits.append(Edit("delete", line[len("delete ") :]))
            index += 1
            continue
        if line.startswith("create "):
            path = line[len("create ") :]
            replace, index = _body(lines, index + 1, "+", (TERMINATOR,))
            edits.append(Edit("create", path, "", replace))
            index += 1
            continue
        if line.startswith("edit "):
            path = line[len("edit ") :]
            search, index = _body(lines, index + 1, "-", (SEPARATOR,))
            if not search:
                msg = f"edit {path!r}: the search block is empty, so it names nothing to replace"
                raise EditError(msg)
            replace, index = _body(lines, index + 1, "+", (TERMINATOR,))
            edits.append(Edit("edit", path, search, replace))
            index += 1
            continue
        msg = f"line {index + 1}: expected 'edit ', 'create ' or 'delete ', got {line!r}"
        raise EditError(msg)
    if not edits:
        msg = "no edit blocks in the worker's response"
        raise EditError(msg)
    return edits


def _resolved(workdir: Path, path: str) -> Path:
    """`path` inside `workdir`, or an error -- a traversal is never in scope."""
    target = (workdir / path).resolve()
    root = workdir.resolve()
    if target != root and root not in target.parents:
        msg = f"{path!r} resolves outside the worktree"
        raise EditError(msg)
    return target


def loose_spans(source: str, search: str) -> list[tuple[int, int]]:
    """Every span of `source` matching `search` up to blank lines and
    trailing spaces, stopping at two -- the caller only needs to tell
    "none" from "one" from "more than one".

    A live draw against a 73-line file omitted a single blank line from an
    otherwise correct search block and was refused, while two sibling
    draws of the same prompt kept it. The contract is that a block names
    ONE site; reproducing the file's blank lines byte for byte was never
    the contract, and enforcing it rejected a known-good edit. So the
    significant lines must still match in order and the result must still
    be unique -- an ambiguous loose match is refused exactly as an
    ambiguous exact one is.
    """
    src = source.split("\n")
    want = [line.rstrip() for line in search.split("\n") if line.strip()]
    if not want:
        return []
    hits: list[tuple[int, int]] = []
    for start in range(len(src)):
        if not src[start].strip():
            continue
        index, taken = start, 0
        while index < len(src) and taken < len(want):
            current = src[index].rstrip()
            if not current:
                index += 1
                continue
            if current != want[taken]:
                break
            index += 1
            taken += 1
        if taken == len(want):
            hits.append((start, index))
            if len(hits) > 1:
                return hits
    return hits


def _replace_once(source: str, search: str, replace: str, path: str) -> str:
    """Replace the single occurrence of `search`, or refuse.

    Uniqueness is the whole safety property: without line numbers, a
    search block that matches twice names no particular site, and
    guessing one is how an edit silently lands in the wrong place.
    """
    count = source.count(search)
    if count == 0 and search.endswith("\n"):
        # The block always ends a line; the file's last line may not.
        trimmed = search[:-1]
        if source.count(trimmed) == 1:
            search, replace = trimmed, replace[:-1] if replace.endswith("\n") else replace
            count = 1
    if count == 0:
        spans = loose_spans(source, search)
        if len(spans) == 1:
            start, end = spans[0]
            lines = source.split("\n")
            # `_body` terminates every line it collects, so `replace` is
            # either empty or newline-ended; `splitlines` drops that
            # terminator without inventing a trailing blank line.
            body = replace.splitlines()
            return "\n".join(lines[:start] + body + lines[end:])
        if spans:
            msg = f"{path}: the search block names more than one site, so it names none"
            raise EditError(msg)
        msg = f"{path}: the search block does not appear in the file"
        raise EditError(msg)
    if count > 1:
        msg = f"{path}: the search block appears {count} times, so it names no single site"
        raise EditError(msg)
    return source.replace(search, replace, 1)


def apply_edits(workdir: Path, text: str) -> list[str]:
    """Apply every block to the worktree; return the paths touched, in order.

    Applies in order and stops at the first failure. A partially applied
    set is the caller's to abandon -- `_run_node` already restores the
    baseline when an attempt fails, which is what keeps a refused edit
    from leaving a half-written tree.
    """
    touched: list[str] = []
    for edit in parse_edits(text):
        target = _resolved(workdir, edit.path)
        if edit.op == "delete":
            if not target.is_file():
                msg = f"{edit.path}: cannot delete, no such file"
                raise EditError(msg)
            target.unlink()
        elif edit.op == "create":
            if target.exists():
                msg = f"{edit.path}: cannot create, the file already exists"
                raise EditError(msg)
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(edit.replace)
        else:
            if not target.is_file():
                msg = f"{edit.path}: cannot edit, no such file"
                raise EditError(msg)
            source = target.read_text()
            target.write_text(_replace_once(source, edit.search, edit.replace, edit.path))
        touched.append(edit.path)
    return touched
