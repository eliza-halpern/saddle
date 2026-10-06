"""Get back what compaction took away, by meaning, for the `embeddings` switch.

Compaction keeps a long session inside the window by shortening old tool
results and dropping the oldest exchanges (`memory.compact`); the note it
leaves names topics, not text. What it takes is handed here whole: each
dropped message, and the full text of each result it shortened, every result
labelled with the call that produced it. The pieces are embedded on a
background thread at the lowest priority the embeddings server runs at, so a
compaction never waits on them, and `recall` returns the few nearest a
question in full.

A search always says how many pieces it searched and how many are still
being embedded: a piece that is not embedded yet must never read as "nothing
there".
"""

from __future__ import annotations

import hashlib
import threading
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, Final, Protocol

from saddle.embed import EmbedClient, EmbedError, cosine, document, query
from saddle.memory import CHARS_PER_TOKEN, describe_call

PIECE_LINES: Final = 40
PIECE_TOKENS: Final = 1_000
"""A piece ends at `PIECE_LINES` lines or about this many tokens, whichever
comes first (estimated at `CHARS_PER_TOKEN`), so three fit a tool result."""

TOP: Final = 3
DIMENSIONS: Final = 256
BATCH: Final = 8


class Embedder(Protocol):
    def embed(
        self, items: list[Any], dimensions: int | None = None
    ) -> list[list[float]]: ...  # pragma: no cover - a protocol


@dataclass
class Piece:
    label: str
    text: str
    vector: list[float] | None = None


def _text(content: Any) -> str:
    if isinstance(content, list):
        return "\n".join(p.get("text", "") for p in content if p.get("type") == "text")
    return str(content or "")


def render(message: dict[str, Any]) -> tuple[str, str]:
    """A message as (label, text): who said it, and what."""
    role = message.get("role", "?")
    text = _text(message.get("content"))
    if role == "tool":
        return f"result of {message.get('call', 'a tool call')}", text
    if role == "assistant" and message.get("tool_calls"):
        called = "; ".join(describe_call(c) for c in message["tool_calls"])
        return f"assistant called {called}", text
    return role, text


def pieces(label: str, text: str) -> list[Piece]:
    """`text` cut into pieces of at most `PIECE_LINES` lines and about
    `PIECE_TOKENS` tokens; blank pieces left out, and one line longer than the
    budget cut across."""
    budget = PIECE_TOKENS * CHARS_PER_TOKEN
    lines = [
        line[i : i + budget] for line in text.splitlines() for i in range(0, len(line) or 1, budget)
    ]
    windows: list[list[str]] = [[]]
    size = 0
    for line in lines:
        if len(windows[-1]) == PIECE_LINES or size + len(line) > budget:
            windows.append([])
            size = 0
        windows[-1].append(line)
        size += len(line) + 1
    bodies = [body for body in ("\n".join(w) for w in windows) if body.strip()]
    if len(bodies) == 1:
        return [Piece(label, bodies[0])]
    return [Piece(f"{label} (part {n} of {len(bodies)})", b) for n, b in enumerate(bodies, 1)]


class Recall:
    """One session's archive of compacted text, embedded in the background."""

    def __init__(
        self,
        client: Callable[[], Embedder] = EmbedClient,
        *,
        background: bool = True,
    ) -> None:
        self._client = client
        self._background = background
        self._lock = threading.Lock()
        self._pieces: list[Piece] = []
        self._seen: set[str] = set()
        self._worker: threading.Thread | None = None
        self.error: str | None = None
        """Why the latest embedding failed, until one succeeds."""

    def __len__(self) -> int:
        with self._lock:
            return len(self._pieces)

    def add(self, messages: list[dict[str, Any]]) -> int:
        """Archive `messages` (as compaction loses them); the pieces added.
        Text archived before, word for word, is not archived twice."""
        added = []
        for message in messages:
            label, text = render(message)
            digest = hashlib.sha256(f"{label}\0{text}".encode()).hexdigest()
            if digest in self._seen:
                continue
            self._seen.add(digest)
            added.extend(pieces(label, text))
        with self._lock:
            self._pieces.extend(added)
        if added:
            self._embed_later()
        return len(added)

    def _embed_later(self) -> None:
        if not self._background:
            self.embed_pending()
            return
        with self._lock:
            if self._worker is not None and self._worker.is_alive():
                return
            self._worker = threading.Thread(
                target=self.embed_pending, name="saddle-recall", daemon=True
            )
            self._worker.start()

    def embed_pending(self) -> None:
        """Embed every piece without a vector, a batch at a time, until none
        is left or the server fails (`error` says why; the next `add` or
        `search` tries again)."""
        while True:
            with self._lock:
                batch = [p for p in self._pieces if p.vector is None][:BATCH]
            if not batch:
                return
            try:
                vectors = self._client().embed(
                    [document(p.text, p.label) for p in batch], dimensions=DIMENSIONS
                )
            except EmbedError as exc:
                self.error = str(exc)
                return
            self.error = None
            with self._lock:
                for piece, vector in zip(batch, vectors, strict=True):
                    piece.vector = vector

    def search(self, question: str, top: int = TOP) -> str:
        """The `top` archived pieces nearest `question`, in full."""
        with self._lock:
            archived = list(self._pieces)
        if not archived:
            return "Nothing has been compacted away in this session, so there is nothing to recall."
        try:
            asked = self._client().embed([query("search result", question)], dimensions=DIMENSIONS)[
                0
            ]
        except EmbedError as exc:
            return f"error: recall could not search: {exc}"
        ready = [p for p in archived if p.vector is not None]
        waiting = len(archived) - len(ready)
        if waiting:
            self._embed_later()
        scope = f"Searched {len(ready)} of {len(archived)} archived pieces"
        if waiting:
            scope += f"; {waiting} not embedded yet"
            scope += f" ({self.error})" if self.error else " (still embedding; ask again shortly)"
        if not ready:
            return f"error: {scope}, so nothing was searched."
        scored = sorted(
            ((cosine(asked, p.vector), p) for p in ready if p.vector is not None),
            key=lambda pair: -pair[0],
        )
        lines = [scope + ". Nearest first (similarity 0 to 1; compare them, not their size):"]
        for score, piece in scored[:top]:
            lines.append(f"--- {piece.label} ({score:.2f})")
            lines.append(piece.text)
        return "\n".join(lines)
