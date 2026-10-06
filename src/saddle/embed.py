"""Embeddings from a local server, for the person's `embeddings` switch.

An embedding is a vector for a text or an image, placed so that things that
mean the same land close together: a question finds the code that answers it
without sharing a word with it. saddle asks a server on this machine for them
(`tools/embed_sidecar.py` runs EmbeddingGemma 2 on the CPU); it never loads a
model itself, so saddle keeps no dependency on torch.

The model was trained with a task prefix on every input, and asymmetric tasks
use different ones for the query and the documents searched; an input without
its prefix is embedded worse, not refused, so the prefixes live here, in one
place. Vectors come back normalised: a dot product is their cosine.
"""

from __future__ import annotations

import base64
import os
from collections.abc import Mapping, Sequence
from typing import Any, Final

import httpx

URL_ENV: Final = "SADDLE_EMBED_URL"
DEFAULT_URL: Final = "http://127.0.0.1:18031"

TIMEOUT_S: Final = 600.0
"""How long one request may take. The server runs at the lowest priority on
few threads, beside a model server that may be using the CPU itself."""

BATCH: Final = 32
"""Inputs per request."""

type Item = str | dict[str, str]
"""A text, or {"text": ..., "image": base64} for an image."""


class EmbedError(RuntimeError):
    """The embeddings server could not be reached or gave no vectors."""


def query(task: str, text: str) -> str:
    """`text` as a query for `task` ("code retrieval", "search result",
    "clustering", "sentence similarity", ...)."""
    return f"task: {task} | query: {text}"


def document(text: str, title: str = "none") -> str:
    """`text` as a document an asymmetric query searches."""
    return f"title: {title} | text: {text}"


def image(png: bytes) -> dict[str, str]:
    """An image to embed. It takes no task prefix: the model card says the
    prefixes apply to text only, and images, video and audio go in bare."""
    return {"image": base64.b64encode(png).decode()}


def cosine(a: Sequence[float], b: Sequence[float]) -> float:
    """How alike two normalised vectors are, from -1 to 1."""
    return sum(x * y for x, y in zip(a, b, strict=True))


def server_url(environ: Mapping[str, str] | None = None) -> str:
    env = os.environ if environ is None else environ
    return (env.get(URL_ENV) or DEFAULT_URL).rstrip("/")


class EmbedClient:
    """The embeddings server at `url` (`$SADDLE_EMBED_URL`, else 127.0.0.1:18031)."""

    def __init__(self, url: str | None = None, http: httpx.Client | None = None) -> None:
        self.url = url or server_url()
        self._http = http or httpx.Client(timeout=TIMEOUT_S)

    def health(self) -> dict[str, Any]:
        """The server's model, dimension and whether it embeds images."""
        try:
            reply = self._http.get(f"{self.url}/health", timeout=5.0)
            reply.raise_for_status()
            data = reply.json()
        except (httpx.HTTPError, ValueError) as exc:
            msg = f"no embeddings server at {self.url}: {exc}"
            raise EmbedError(msg) from exc
        if not isinstance(data, dict) or not isinstance(data.get("dim"), int):
            msg = f"{self.url}/health did not say its dimension: {str(data)[:200]}"
            raise EmbedError(msg)
        return data

    def embed(self, items: Sequence[Item], dimensions: int | None = None) -> list[list[float]]:
        """One normalised vector per item, in order. `dimensions` truncates
        them (the model is trained to keep 512, 256 or 128)."""
        vectors: list[list[float]] = []
        for start in range(0, len(items), BATCH):
            chunk = list(items[start : start + BATCH])
            body: dict[str, Any] = {"input": chunk}
            if dimensions is not None:
                body["dimensions"] = dimensions
            try:
                reply = self._http.post(f"{self.url}/v1/embeddings", json=body)
                reply.raise_for_status()
                data = reply.json()["data"]
                found = [list(map(float, row["embedding"])) for row in data]
            except (httpx.HTTPError, ValueError, KeyError, TypeError) as exc:
                msg = f"the embeddings server at {self.url} failed: {exc}"
                raise EmbedError(msg) from exc
            if len(found) != len(chunk):
                msg = f"asked {self.url} for {len(chunk)} vectors and got {len(found)}"
                raise EmbedError(msg)
            vectors.extend(found)
        return vectors
