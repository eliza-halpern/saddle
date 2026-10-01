"""An offline checker for the links and anchors in Markdown files.

The defect it exists for: a doc is renamed or a heading reworded and every
link to it keeps rendering as a link while pointing at nothing. Nothing
fetches the network here; a link is judged against the tracked file list and
the headings of the file it names.

What counts as a link: `[text](dest)` and `![alt](dest)`, reference links
(`[text][label]`, `[label][]`) with their `[label]: dest` definitions,
`<https://...>` autolinks, and the `src`/`href` of inline HTML tags. Fenced
code blocks and inline code spans are ignored. What is not read: bare URLs,
indented code blocks (indistinguishable from list continuations without a
full parser) and setext headings (`Title` over `=====`), so a link to a setext
heading is reported missing rather than passed.
"""

from __future__ import annotations

import posixpath
import re
import unicodedata
from collections.abc import Collection, Iterable
from pathlib import Path
from typing import NamedTuple
from urllib.parse import unquote, urlsplit

FENCE_OPEN = re.compile(r"^\s*(`{3,}|~{3,})(.*)$")
CODE_SPAN = re.compile(r"(?<!`)(`+)(?!`)(.+?)(?<!`)\1(?!`)", re.DOTALL)
ATX_HEADING = re.compile(r"^ {0,3}(?:>\s*)*#{1,6}[ \t]+(.*?)[ \t]*$")
INLINE_DEST = re.compile(
    r"\]\(\s*(<[^<>\n]*>|(?:[^\s()<>]|\([^\s()]*\))*)"
    r"(?:\s+(?:\"[^\"]*\"|'[^']*'|\([^)]*\)))?\s*\)"
)
REF_USE = re.compile(r"\[([^\[\]]+)\]\[([^\[\]]*)\]")
REF_DEF = re.compile(r"^ {0,3}\[([^\]]+)\]:[ \t]*(<[^>]*>|\S*)")
AUTOLINK = re.compile(r"<((?:https?|mailto|ftp):[^\s<>]*)>")
HTML_TAG = re.compile(r"<[A-Za-z][^<>]*>")
HTML_URL_ATTR = re.compile(
    r"""\b(?:src|href)\s*=\s*(?:"([^"]*)"|'([^']*)')""",
)
HTML_ANCHOR_ATTR = re.compile(r"""\b(?:id|name)\s*=\s*(?:"([^"]+)"|'([^']+)')""")
SCHEME = re.compile(r"^([A-Za-z][A-Za-z0-9+.-]*):")


class Link(NamedTuple):
    line: int
    dest: str


class Problem(NamedTuple):
    file: str
    line: int
    target: str
    reason: str

    def __str__(self) -> str:
        return f"{self.file}:{self.line}: {self.target}: {self.reason}"


def scan_fences(text: str) -> list[tuple[str, str]]:
    """Each line of `text` with its role: "text", "fence" (an opening or closing
    delimiter) or "code" (a line inside a fenced block)."""
    out: list[tuple[str, str]] = []
    fence: tuple[str, int] | None = None
    for line in text.split("\n"):
        if fence is None:
            opened = FENCE_OPEN.match(line)
            if opened and not (opened.group(1)[0] == "`" and "`" in opened.group(2)):
                fence = (opened.group(1)[0], len(opened.group(1)))
                out.append((line, "fence"))
            else:
                out.append((line, "text"))
            continue
        stripped = line.strip()
        if stripped and set(stripped) == {fence[0]} and len(stripped) >= fence[1]:
            fence = None
            out.append((line, "fence"))
        else:
            out.append((line, "code"))
    return out


def blank_fenced_code(text: str) -> str:
    """The text with every fenced code block's lines emptied (line numbers kept)."""
    return "\n".join(line if role == "text" else "" for line, role in scan_fences(text))


def fenced_code_lines(text: str) -> list[str]:
    """The lines inside fenced code blocks, delimiters excluded."""
    return [line for line, role in scan_fences(text) if role == "code"]


def inline_code_spans(text: str) -> list[str]:
    """The contents of every inline code span outside fenced code, stripped."""
    return [m.group(2).strip() for m in CODE_SPAN.finditer(blank_fenced_code(text))]


def blank_code_spans(text: str) -> str:
    """The text with each inline code span removed (newlines inside it kept)."""
    return CODE_SPAN.sub(lambda m: "\n" * m.group(0).count("\n"), text)


def _line_of(text: str, offset: int) -> int:
    return text.count("\n", 0, offset) + 1


def _normal_label(label: str) -> str:
    return " ".join(label.lower().split())


def collect_links(text: str) -> tuple[list[Link], dict[str, str]]:
    """Every link destination in `text`, and the reference definitions it makes.

    An undefined `[text][label]` is returned as a link whose destination is
    `[label]` so the caller reports it with its line.
    """
    cleaned = blank_code_spans(blank_fenced_code(text))
    definitions: dict[str, str] = {}
    links: list[Link] = []
    for number, line in enumerate(cleaned.split("\n"), start=1):
        defined = REF_DEF.match(line)
        if defined:
            definitions.setdefault(_normal_label(defined.group(1)), defined.group(2))
            links.append(Link(number, defined.group(2)))
    for match in INLINE_DEST.finditer(cleaned):
        links.append(Link(_line_of(cleaned, match.start()), match.group(1)))
    for match in REF_USE.finditer(cleaned):
        label = _normal_label(match.group(2) or match.group(1))
        if label not in definitions:
            links.append(Link(_line_of(cleaned, match.start()), f"[{label}]"))
    for match in AUTOLINK.finditer(cleaned):
        links.append(Link(_line_of(cleaned, match.start()), match.group(1)))
    for tag in HTML_TAG.finditer(cleaned):
        for attr in HTML_URL_ATTR.finditer(tag.group(0)):
            links.append(Link(_line_of(cleaned, tag.start()), attr.group(1) or attr.group(2)))
    return sorted(links), definitions


def _heading_text(raw: str) -> str:
    """A heading's rendered text: markup removed, code kept verbatim."""
    text = re.sub(r"\s+#+$", "", raw)
    text = re.sub(r"!?\[([^\]]*)\]\([^)]*\)", r"\1", text)
    text = re.sub(r"\[([^\]]*)\]\[[^\]]*\]", r"\1", text)
    pieces = re.split(r"(?<!`)(`+)(?!`)(.+?)(?<!`)\1(?!`)", text)
    out: list[str] = []
    # re.split with two groups yields [outside, ticks, code, outside, ...].
    for index in range(0, len(pieces), 3):
        outside = HTML_TAG.sub("", pieces[index])
        outside = outside.replace("*", "").replace("~~", "")
        outside = re.sub(r"(?<![A-Za-z0-9])_+(?=\S)|(?<=\S)_+(?![A-Za-z0-9])", "", outside)
        out.append(outside)
        if index + 2 < len(pieces):
            out.append(pieces[index + 2])
    return "".join(out).strip()


def github_slug(heading: str) -> str:
    """The anchor GitHub gives a heading, before duplicate suffixes.

    Lowercase; keep letters, numbers, combining marks, `-`, `_` and the
    space; drop every other character; each space becomes a hyphen. Nothing is
    collapsed or trimmed, so "P0 -- Mechanical" (an em dash between spaces)
    keeps both hyphens.
    """
    kept = [
        ch
        for ch in _heading_text(heading).lower()
        if unicodedata.category(ch)[0] in "LNM" or ch in "-_ "
    ]
    return "".join(kept).replace(" ", "-")


def anchors_of(text: str) -> set[str]:
    """Every anchor a Markdown file offers: heading slugs, then HTML ids and names."""
    unfenced = blank_fenced_code(text)
    anchors: set[str] = set()
    repeats: dict[str, int] = {}
    for line in unfenced.split("\n"):
        heading = ATX_HEADING.match(line)
        if not heading:
            continue
        base = github_slug(heading.group(1))
        slug = base
        while slug in anchors:
            repeats[base] = repeats.get(base, 0) + 1
            slug = f"{base}-{repeats[base]}"
        anchors.add(slug)
    for tag in HTML_TAG.finditer(blank_code_spans(unfenced)):
        for attr in HTML_ANCHOR_ATTR.finditer(tag.group(0)):
            anchors.add(attr.group(1) or attr.group(2))
    return anchors


def _external_problem(dest: str) -> str | None:
    scheme = SCHEME.match(dest)
    assert scheme is not None
    name = scheme.group(1).lower()
    if name == "mailto":
        return None if "@" in dest[len("mailto:") :] else "mailto link without an address"
    if name not in {"http", "https"}:
        return f"unsupported scheme {name!r}"
    if re.search(r"[\s\x00-\x1f]", dest):
        return "whitespace in URL"
    try:
        parts = urlsplit(dest)
        parts.port  # noqa: B018 -- a bad port raises ValueError
    except ValueError as exc:
        return f"malformed URL ({exc})"
    if not parts.hostname:
        return "URL has no host"
    return None


def _unwrapped(dest: str) -> str:
    target = dest.strip()
    if target.startswith("<") and target.endswith(">"):
        target = target[1:-1]
    return target


def _resolve_path(path: str, source: str) -> str:
    """`path`, written in the file `source`, as a normalised repo-relative path."""
    if path.startswith("/"):
        return posixpath.normpath(path.lstrip("/"))
    return posixpath.normpath(posixpath.join(posixpath.dirname(source), path))


def local_target(dest: str, source: str) -> str | None:
    """The repo-relative file a local link names, ignoring `#fragment` and `?query`.

    None for an external URL, a same-file `#anchor`, an empty or undefined
    reference, or a path that leaves the repository.
    """
    target = _unwrapped(dest)
    if not target or target.startswith(("[", "//")) or SCHEME.match(target):
        return None
    path = unquote(target.partition("#")[0].partition("?")[0])
    if not path:
        return None
    resolved = _resolve_path(path, source)
    return None if resolved == ".." or resolved.startswith("../") else resolved


def check_destination(
    dest: str,
    *,
    source: str,
    tracked_files: Collection[str],
    anchors: dict[str, set[str]],
) -> str | None:
    """Why `dest`, written in `source`, does not resolve; None if it does.

    `anchors` maps each tracked Markdown path to its anchors. A path is a
    tracked file, or a directory that holds one. A `#fragment` is checked
    only against Markdown targets: `#L10` on a source file is a line anchor.
    """
    target = _unwrapped(dest)
    if not target:
        return "empty link destination"
    if target.startswith("[") and target.endswith("]"):
        return "reference link with no definition"
    if SCHEME.match(target):
        return _external_problem(target)
    if target.startswith("//"):
        return "protocol-relative URL"
    path, _, fragment = target.partition("#")
    path = unquote(path.partition("?")[0])
    fragment = unquote(fragment)
    if not path:
        resolved = source
    else:
        resolved = _resolve_path(path, source)
        if resolved == ".." or resolved.startswith("../"):
            return "path leaves the repository"
        prefix = resolved.rstrip("/") + "/"
        if resolved not in tracked_files and not any(f.startswith(prefix) for f in tracked_files):
            return f"no tracked file or directory {resolved!r}"
    if fragment and resolved in anchors and fragment not in anchors[resolved]:
        return f"no heading or anchor #{fragment} in {resolved}"
    return None


def check_links(
    root: Path, markdown: Iterable[str], tracked_files: Collection[str]
) -> list[Problem]:
    """Every unresolved link in the Markdown files `markdown`, read from `root`."""
    paths = list(markdown)
    texts = {rel: (root / rel).read_text(encoding="utf-8") for rel in paths}
    anchors = {rel: anchors_of(text) for rel, text in texts.items()}
    problems: list[Problem] = []
    for rel, text in texts.items():
        links, _ = collect_links(text)
        for link in links:
            reason = check_destination(
                link.dest, source=rel, tracked_files=tracked_files, anchors=anchors
            )
            if reason is not None:
                problems.append(Problem(rel, link.line, link.dest, reason))
    return problems
