"""Search a repository's code by meaning, for the person's `embeddings` switch.

`search` finds text that contains the query; `code_search` finds code that
answers it ("where is the compaction limit computed" finds
`compaction_limit` without sharing a word with it). The tracked files are cut
into windows of `CHUNK_LINES` lines, each embedded once as a document titled
with its place, and the query is embedded as a code-retrieval query
(`saddle.embed`).

Embedding is slow on a CPU (measured here: about 0.8 windows of 40 lines a
second on two threads), so it never happens inside a tool call: `saddle index`
embeds the repository ahead of time, and `code_search` searches what is
already embedded. Vectors are cached by the file's contents, the model and the
dimension, outside the repository, so an unchanged file is never embedded
twice and a changed one is embedded again. A search always says how many of
the tracked files it covered: a partial index must never read as "nothing
there".
"""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import IO, Final

from saddle.embed import EmbedClient, EmbedError, cosine, document, query

CHUNK_LINES: Final = 40
DIMENSIONS: Final = 256
"""Truncated (Matryoshka) vectors: a quarter of the storage of 768, which the
model card says costs little quality."""

MAX_FILE_BYTES: Final = 200_000
SKIPPED: Final = frozenset(
    {"uv.lock", "package-lock.json", "yarn.lock", "poetry.lock", "Cargo.lock"}
)
"""Generated files whose size would dominate an index and whose text no
question is about."""

TOP: Final = 5
PREVIEW_LINES: Final = 3
CACHE_ENV: Final = "SADDLE_EMBED_CACHE"


class CodeSearchError(RuntimeError):
    """The repository's files could not be listed."""


@dataclass(frozen=True)
class Chunk:
    path: str
    start: int
    end: int
    text: str


def cache_root(environ: Mapping[str, str] | None = None) -> Path:
    env = os.environ if environ is None else environ
    if env.get(CACHE_ENV):
        return Path(env[CACHE_ENV]).expanduser()
    base = Path(env.get("XDG_CACHE_HOME") or "~/.cache").expanduser()
    return base / "saddle" / "embeddings"


def tracked_files(root: Path) -> list[str]:
    """The files git tracks under `root`, relative to it."""
    listed = subprocess.run(
        ["git", "-C", str(root), "ls-files", "-z"], capture_output=True, check=False
    )
    if listed.returncode != 0:
        msg = f"{root} is not a git repository: {listed.stderr.decode(errors='replace').strip()}"
        raise CodeSearchError(msg)
    return [name for name in listed.stdout.decode().split("\0") if name]


def readable(root: Path, name: str) -> str | None:
    """`name`'s text, or None for a file an index leaves out: generated, too
    large, binary, not UTF-8, or gone."""
    path = root / name
    if Path(name).name in SKIPPED or not path.is_file() or path.stat().st_size > MAX_FILE_BYTES:
        return None
    data = path.read_bytes()
    if b"\0" in data[:8192]:
        return None
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError:
        return None


def chunks(name: str, text: str) -> list[Chunk]:
    """`text` in windows of `CHUNK_LINES` lines, blank windows left out."""
    lines = text.splitlines()
    found = []
    for start in range(0, len(lines), CHUNK_LINES):
        window = lines[start : start + CHUNK_LINES]
        if any(line.strip() for line in window):
            end = start + len(window)
            found.append(Chunk(name, start + 1, end, "\n".join(window)))
    return found


class Index:
    """The embedded windows of one repository, cached by content."""

    def __init__(self, root: Path, client: EmbedClient, cache: Path | None = None) -> None:
        self.root = root
        self.client = client
        health = client.health()
        self.model = f"{health.get('model', '?')}-{health['dim']}d"
        self.cache = (cache or cache_root()) / self.model.replace("/", "_")

    def _key(self, text: str) -> str:
        return hashlib.sha256(f"{self.model}\0{DIMENSIONS}\0{text}".encode()).hexdigest()

    def _cached(self, text: str) -> list[list[float]] | None:
        try:
            data = json.loads((self.cache / f"{self._key(text)}.json").read_text())
        except (OSError, ValueError):
            return None
        return data if isinstance(data, list) else None

    def build(self, progress: Callable[[str, int], None] | None = None) -> tuple[int, int]:
        """Embed every tracked file not cached yet. Returns (files embedded now,
        files that can be indexed). `progress` hears each file and its windows."""
        embedded = indexable = 0
        for name in tracked_files(self.root):
            text = readable(self.root, name)
            if text is None:
                continue
            indexable += 1
            if self._cached(text) is not None:
                continue
            windows = chunks(name, text)
            vectors = self.client.embed(
                [document(c.text, f"{c.path}:{c.start}-{c.end}") for c in windows],
                dimensions=DIMENSIONS,
            )
            self.cache.mkdir(parents=True, exist_ok=True)
            (self.cache / f"{self._key(text)}.json").write_text(json.dumps(vectors))
            embedded += 1
            if progress is not None:
                progress(name, len(windows))
        return embedded, indexable

    def search(self, question: str, top: int = TOP) -> str:
        """The `top` windows nearest `question`, with how much was searched."""
        scored: list[tuple[float, Chunk]] = []
        covered = indexable = 0
        missing: list[str] = []
        asked = self.client.embed([query("code retrieval", question)], dimensions=DIMENSIONS)[0]
        for name in tracked_files(self.root):
            text = readable(self.root, name)
            if text is None:
                continue
            indexable += 1
            vectors = self._cached(text)
            windows = chunks(name, text)
            if vectors is None or len(vectors) != len(windows):
                missing.append(name)
                continue
            covered += 1
            scored.extend((cosine(asked, v), c) for v, c in zip(vectors, windows, strict=True))
        if not covered:
            return (
                f"error: none of the {indexable} indexable files is indexed yet, so nothing "
                "was searched. The person can run `saddle index` to embed them; use `search` "
                "for exact text meanwhile."
            )
        scope = f"Searched {covered} of {indexable} indexable files"
        if missing:
            shown = ", ".join(missing[:5]) + (
                f" and {len(missing) - 5} more" if len(missing) > 5 else ""
            )
            scope += f"; not indexed (changed since `saddle index`, or new): {shown}"
        lines = [scope + ". Nearest first (similarity 0 to 1; compare them, not their size):"]
        for score, chunk in sorted(scored, key=lambda pair: -pair[0])[:top]:
            preview = [line.rstrip() for line in chunk.text.splitlines() if line.strip()]
            lines.append(f"{chunk.path}:{chunk.start}-{chunk.end} ({score:.2f})")
            lines.extend(f"    {line[:120]}" for line in preview[:PREVIEW_LINES])
        return "\n".join(lines)


def run_index(
    repo: Path, *, stdout: IO[str], stderr: IO[str], client: EmbedClient | None = None
) -> int:
    """`saddle index`: embed `repo`'s tracked files that are not cached yet."""
    try:
        index = Index(repo, client or EmbedClient())

        def said(name: str, windows: int) -> None:
            print(f"embedded {name} ({windows} windows)", file=stdout, flush=True)

        embedded, indexable = index.build(said)
    except (EmbedError, CodeSearchError) as exc:
        print(f"saddle index: {exc}", file=stderr)
        return 1
    print(
        f"{indexable} indexable files; {embedded} embedded now, "
        f"{indexable - embedded} already cached ({index.model}).",
        file=stdout,
    )
    return 0
