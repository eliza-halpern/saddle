"""A local embeddings server for saddle's `embeddings` capability.

It runs in its own virtual environment, never saddle's, because it needs torch
and sentence-transformers:

    uv venv ~/embed/.venv
    uv pip install --python ~/embed/.venv/bin/python \\
        --index-url https://download.pytorch.org/whl/cpu torch
    uv pip install --python ~/embed/.venv/bin/python sentence-transformers
    ~/embed/.venv/bin/python tools/embed_sidecar.py --model PATH_OR_NAME

It listens on 127.0.0.1 only, and runs at the lowest CPU priority with few
threads (`--threads`, default 2): the model server on the same machine may
compute on the CPU too, and an embedding is never worth slowing it down.

    GET  /health          {"model", "dim", "images"}
    POST /v1/embeddings   {"input": [text | {"text", "image": base64}], "dimensions"?}
                          -> {"data": [{"index", "embedding"}], "model", "usage"}

The model (EmbeddingGemma 2) is Apache-2.0, and Google asks that deployments
follow its Gemma Prohibited Use Policy (https://ai.google.dev/gemma/prohibited_use_policy).

Inputs are embedded as given; an image with no text goes in bare. Task
prefixes ("task: code retrieval | query: ...") are the caller's
(`saddle.embed`), so one server serves every use.
Vectors come back normalised, so a dot product is the cosine.
"""

from __future__ import annotations

import argparse
import base64
import io
import json
import os
import sys
import threading
from collections.abc import Sequence
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

MAX_INPUTS = 256
MAX_BODY = 64 * 1024 * 1024


def load(model_path: str, threads: int) -> tuple[Any, Any]:
    """The model, on the CPU as its card says, and torch."""
    # torch, sentence-transformers and PIL live in the sidecar's own environment,
    # never saddle's (the docstring's setup), so saddle's type check cannot see them.
    import torch  # type: ignore[import-not-found]
    from sentence_transformers import SentenceTransformer  # type: ignore[import-not-found]

    torch.set_num_threads(threads)
    # The model card: float32 on CPUs (bfloat16 only with native support; never
    # float16, which returns NaN or silently degraded vectors), and the audio
    # encoder left unloaded (740M parameters with it, 440M without).
    model = SentenceTransformer(
        model_path,
        device="cpu",
        model_kwargs={"dtype": torch.float32},
        config_kwargs={"audio_config": None},
    )
    return model, torch


def prepare(items: list[Any]) -> list[Any]:
    """The inputs as the model takes them: text as given, an image decoded.
    Prefixes are for text only (model card): an image alone goes in bare."""
    from PIL import Image  # type: ignore[import-not-found]

    prepared = []
    for item in items:
        if isinstance(item, dict) and "image" in item:
            image = Image.open(io.BytesIO(base64.b64decode(item["image"]))).convert("RGB")
            text = item.get("text")
            prepared.append({"text": text, "image": image} if text else image)
        else:
            prepared.append(item)
    return prepared


def server(model: Any, torch: Any, name: str, port: int) -> ThreadingHTTPServer:
    """The HTTP server for `model` on 127.0.0.1:`port` (0 picks a free one)."""
    dim = model.get_embedding_dimension()
    lock = threading.Lock()

    def encode(items: list[Any], dimensions: int | None) -> list[list[float]]:
        prepared = prepare(items)
        with lock, torch.inference_mode():
            vectors = model.encode(
                prepared, normalize_embeddings=True, truncate_dim=dimensions, batch_size=8
            )
        return [[round(float(x), 6) for x in v] for v in vectors]

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_: Any) -> None:
            pass

        def reply(self, code: int, body: dict[str, Any]) -> None:
            data = json.dumps(body).encode()
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def do_GET(self) -> None:
            if self.path == "/health":
                self.reply(200, {"model": name, "dim": dim, "images": True})
            else:
                self.reply(404, {"error": "not found"})

        def do_POST(self) -> None:
            if self.path != "/v1/embeddings":
                self.reply(404, {"error": "not found"})
                return
            size = int(self.headers.get("Content-Length") or 0)
            if size > MAX_BODY:
                self.reply(413, {"error": f"body over {MAX_BODY} bytes"})
                return
            try:
                request = json.loads(self.rfile.read(size))
                items = request["input"]
                items = [items] if isinstance(items, (str, dict)) else items
                if not items or len(items) > MAX_INPUTS:
                    msg = f"input must hold 1 to {MAX_INPUTS} items"
                    raise ValueError(msg)
                vectors = encode(items, request.get("dimensions"))
            except (KeyError, TypeError, ValueError) as error:
                self.reply(400, {"error": str(error)[:300]})
                return
            texts = [i if isinstance(i, str) else i.get("text", "") for i in items]
            tokens = sum(len(model.tokenizer.tokenize(t)) for t in texts)
            self.reply(
                200,
                {
                    "data": [{"index": i, "embedding": v} for i, v in enumerate(vectors)],
                    "model": name,
                    "usage": {"prompt_tokens": tokens},
                },
            )

    return ThreadingHTTPServer(("127.0.0.1", port), Handler)


def start(argv: Sequence[str] | None = None) -> ThreadingHTTPServer:
    """Parse `argv`, drop to the lowest CPU priority, load the model, bind."""
    parser = argparse.ArgumentParser(description=(__doc__ or "").split("\n\n")[0])
    parser.add_argument("--model", required=True)
    parser.add_argument("--port", type=int, default=18031)
    parser.add_argument("--threads", type=int, default=2)
    args = parser.parse_args(argv)
    os.nice(19)
    model, torch = load(args.model, args.threads)
    name = os.path.basename(os.path.normpath(args.model))
    bound = server(model, torch, name, args.port)
    where = f"127.0.0.1:{bound.server_address[1]}, {args.threads} threads"
    print(f"embeddings: {name} ({model.get_embedding_dimension()}d) on {where}", file=sys.stderr)
    return bound


def main(argv: Sequence[str] | None = None) -> None:
    start(argv).serve_forever()


if __name__ == "__main__":  # pragma: no cover
    main()
