"""`tools/embed_sidecar.py`, with stand-ins for torch, sentence-transformers and PIL.

Contract: the model is loaded on the CPU in float32 without its audio encoder,
on the threads asked for and at the lowest CPU priority; a bare image reaches
the model with no text and an image with text as a pair; every reply holds
one vector per input, in order, truncated as asked; a request with no input,
too many, or too large a body is refused, never embedded.
"""

from __future__ import annotations

import base64
import contextlib
import sys
import threading
import types
from collections.abc import Iterator
from typing import Any

import httpx
import pytest

from tools import embed_sidecar as sidecar

FLOAT32 = object()


class FakeModel:
    def __init__(self, path: str, **kwargs: Any) -> None:
        self.path, self.kwargs = path, kwargs
        self.seen: list[Any] = []
        self.calls: list[dict[str, Any]] = []
        self.tokenizer = types.SimpleNamespace(tokenize=lambda t: t.split())

    def get_embedding_dimension(self) -> int:
        return 4

    def encode(self, prepared: list[Any], **kwargs: Any) -> list[list[float]]:
        self.seen.extend(prepared)
        self.calls.append(kwargs)
        width = kwargs["truncate_dim"] or 4
        return [[float(i)] * width for i in range(len(prepared))]


class FakeImage:
    def __init__(self, data: bytes) -> None:
        self.data = data
        self.mode: str | None = None

    def convert(self, mode: str) -> FakeImage:
        self.mode = mode
        return self


@pytest.fixture
def fakes(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    made: dict[str, Any] = {"threads": None, "nice": None}
    torch = types.ModuleType("torch")
    torch.float32 = FLOAT32  # type: ignore[attr-defined]
    torch.bfloat16 = object()  # type: ignore[attr-defined]  # so a wrong dtype fails the check, not the import
    torch.set_num_threads = lambda n: made.__setitem__("threads", n)  # type: ignore[attr-defined]
    torch.inference_mode = contextlib.nullcontext  # type: ignore[attr-defined]

    def build(path: str, **kwargs: Any) -> FakeModel:
        model = FakeModel(path, **kwargs)
        made["model"] = model
        return model

    st = types.ModuleType("sentence_transformers")
    st.SentenceTransformer = build  # type: ignore[attr-defined]
    pil = types.ModuleType("PIL")
    image = types.ModuleType("PIL.Image")
    image.open = lambda f: FakeImage(f.read())  # type: ignore[attr-defined]
    pil.Image = image  # type: ignore[attr-defined]
    for name, module in (
        ("torch", torch),
        ("sentence_transformers", st),
        ("PIL", pil),
        ("PIL.Image", image),
    ):
        monkeypatch.setitem(sys.modules, name, module)
    monkeypatch.setattr("os.nice", lambda n: made.__setitem__("nice", n))
    return made


@pytest.fixture
def served(fakes: dict[str, Any]) -> Iterator[tuple[str, dict[str, Any]]]:
    bound = sidecar.start(["--model", "/models/gemma/", "--port", "0", "--threads", "3"])
    thread = threading.Thread(target=bound.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{bound.server_address[1]}", fakes
    finally:
        bound.shutdown()
        bound.server_close()


def test_the_model_loads_on_the_cpu_in_float32_without_audio_at_low_priority(
    served: tuple[str, dict[str, Any]],
) -> None:
    url, made = served
    model = made["model"]
    assert model.path == "/models/gemma/"
    assert model.kwargs == {
        "device": "cpu",
        "model_kwargs": {"dtype": FLOAT32},
        "config_kwargs": {"audio_config": None},
    }
    assert (made["threads"], made["nice"]) == (3, 19)
    assert httpx.get(f"{url}/health").json() == {"model": "gemma", "dim": 4, "images": True}


def test_images_go_in_bare_or_paired_and_each_input_gets_its_vector(
    served: tuple[str, dict[str, Any]],
) -> None:
    url, made = served
    png = base64.b64encode(b"PNG").decode()
    reply = httpx.post(
        f"{url}/v1/embeddings",
        json={
            "input": ["task: x | query: y", {"image": png}, {"image": png, "text": "caption"}],
            "dimensions": 2,
        },
    ).json()
    assert [row["embedding"] for row in reply["data"]] == [[0.0, 0.0], [1.0, 1.0], [2.0, 2.0]]
    assert reply["usage"] == {"prompt_tokens": 6}
    text, bare, paired = made["model"].seen
    assert text == "task: x | query: y"
    assert isinstance(bare, FakeImage)
    assert bare.data == b"PNG"
    assert bare.mode == "RGB"
    assert paired["text"] == "caption"
    assert isinstance(paired["image"], FakeImage)
    assert made["model"].calls[0] == {
        "normalize_embeddings": True,
        "truncate_dim": 2,
        "batch_size": 8,
    }
    one = httpx.post(f"{url}/v1/embeddings", json={"input": "alone"}).json()
    assert len(one["data"]) == 1


@pytest.mark.parametrize("body", [{"input": []}, {"nothing": 1}, {"input": ["x"] * 257}])
def test_a_request_with_no_or_too_many_inputs_is_refused(
    served: tuple[str, dict[str, Any]], body: dict[str, Any]
) -> None:
    url, made = served
    reply = httpx.post(f"{url}/v1/embeddings", json=body)
    assert reply.status_code == 400
    assert made["model"].seen == []


def test_a_body_too_large_or_a_wrong_path_is_refused(
    served: tuple[str, dict[str, Any]], monkeypatch: pytest.MonkeyPatch
) -> None:
    url, _ = served
    monkeypatch.setattr(sidecar, "MAX_BODY", 10)
    assert httpx.post(f"{url}/v1/embeddings", json={"input": ["long enough"]}).status_code == 413
    assert httpx.get(f"{url}/elsewhere").status_code == 404
    assert httpx.post(f"{url}/elsewhere", json={}).status_code == 404


def test_main_serves_what_start_bound(monkeypatch: pytest.MonkeyPatch) -> None:
    served: list[bool] = []
    monkeypatch.setattr(
        sidecar,
        "start",
        lambda argv: types.SimpleNamespace(serve_forever=lambda: served.append(True)),
    )
    sidecar.main([])
    assert served == [True]
