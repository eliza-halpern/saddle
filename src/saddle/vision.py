"""Showing the model an image file: what counts as one, and whether the server can see it.

`read_file` on a screenshot used to hand the model the file's bytes as text,
which it cannot look at. With images on (the Ask and Edit lanes; a Task run
never turns them on), `read_file` sends the image itself, in a user message
that follows the tool result, when the served model accepts images. The
standard OpenAI tool message carries text only and templates differ in what
they do with other parts, so the image rides in an ordinary user message,
which the server's own image path has to handle.

Whether the server accepts images is asked once per server with two tiny
pictures of known colours (`server_accepts_images`), and cached: a model that
names both colours right sees images, one that errors or guesses does not.
"""

from __future__ import annotations

import base64
import struct
import zlib
from collections.abc import Iterator, Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Final, Protocol

from saddle.vllm import StreamToken, VllmRequestError

IMAGE_MAX_BYTES: Final = 4_194_304
"""The largest image file `read_file` will send: 4 MiB. A screenshot is
a few hundred KiB; past this it is a photograph, and the server's own
limit on one request is what it would meet next."""


@dataclass(frozen=True)
class ImageInfo:
    mime: str
    kind: str
    width: int
    height: int


def image_info(data: bytes) -> ImageInfo | None:
    """What the bytes are, by their own header and never by the file's name:
    a PNG, JPEG, WebP or GIF with a readable size, else None."""
    if data.startswith(b"\x89PNG\r\n\x1a\n") and data[12:16] == b"IHDR" and len(data) >= 24:
        width, height = struct.unpack(">II", data[16:24])
        return ImageInfo("image/png", "PNG", width, height)
    if data[:6] in (b"GIF87a", b"GIF89a") and len(data) >= 10:
        width, height = struct.unpack("<HH", data[6:10])
        return ImageInfo("image/gif", "GIF", width, height)
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        size = _webp_size(data)
        return ImageInfo("image/webp", "WebP", *size) if size else None
    if data[:2] == b"\xff\xd8":
        size = _jpeg_size(data)
        return ImageInfo("image/jpeg", "JPEG", *size) if size else None
    return None


def _webp_size(data: bytes) -> tuple[int, int] | None:
    chunk = data[12:16]
    if chunk == b"VP8X" and len(data) >= 30:
        return (
            int.from_bytes(data[24:27], "little") + 1,
            int.from_bytes(data[27:30], "little") + 1,
        )
    if chunk == b"VP8L" and len(data) >= 25:
        bits = int.from_bytes(data[21:25], "little")
        return (bits & 0x3FFF) + 1, ((bits >> 14) & 0x3FFF) + 1
    if chunk == b"VP8 " and len(data) >= 30:
        width, height = struct.unpack("<HH", data[26:30])
        return width & 0x3FFF, height & 0x3FFF
    return None


def _jpeg_size(data: bytes) -> tuple[int, int] | None:
    pos = 2
    while pos + 9 <= len(data):
        if data[pos] != 0xFF:
            return None
        marker = data[pos + 1]
        if marker in range(0xC0, 0xD0) and marker not in (0xC4, 0xC8, 0xCC):
            height, width = struct.unpack(">HH", data[pos + 5 : pos + 9])
            return width, height
        pos += 2 + struct.unpack(">H", data[pos + 2 : pos + 4])[0]
    return None


def data_url(mime: str, data: bytes) -> str:
    return f"data:{mime};base64,{base64.b64encode(data).decode()}"


def solid_png(width: int, height: int, rgb: tuple[int, int, int]) -> bytes:
    """A plain one-colour PNG, built here so a probe needs no file."""
    row = b"\x00" + bytes(rgb) * width

    def chunk(kind: bytes, body: bytes) -> bytes:
        return (
            struct.pack(">I", len(body)) + kind + body + struct.pack(">I", zlib.crc32(kind + body))
        )

    return (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0))
        + chunk(b"IDAT", zlib.compress(row * height))
        + chunk(b"IEND", b"")
    )


IMAGE_FOLLOWUP: Final = "Image from read_file call "
"""How the user message carrying a read image begins; the page does not show
that message as something the person said (`is_image_followup`)."""


def images_message(attached: Sequence[tuple[str, str, str]]) -> dict[str, Any]:
    """The user message that shows the model the images `read_file` queued,
    each named by the tool call and path it answers."""
    parts: list[dict[str, Any]] = []
    for call_id, name, url in attached:
        parts.append({"type": "text", "text": f"{IMAGE_FOLLOWUP}{call_id} ({name}):"})
        parts.append({"type": "image_url", "image_url": {"url": url}})
    return {"role": "user", "content": parts}


def is_image_followup(message: Mapping[str, Any]) -> bool:
    """Whether a stored message is `images_message`'s, not the person's."""
    content = message.get("content")
    return (
        message.get("role") == "user"
        and isinstance(content, list)
        and bool(content)
        and str(content[0].get("text", "")).startswith(IMAGE_FOLLOWUP)
    )


class _Streamer(Protocol):
    def stream_chat(
        self,
        messages: Sequence[Mapping[str, Any]],
        *,
        max_tokens: int = ...,
        temperature: float = ...,
        reasoning_effort: str = ...,
    ) -> Iterator[Any]: ...


PROBES: Final = (("red", (255, 0, 0)), ("blue", (0, 0, 255)))
PROBE_QUESTION: Final = "What is the solid colour of this image? Answer with one word."

_CACHE: dict[object, bool] = {}


def reset_cache() -> None:
    _CACHE.clear()


def _answer(client: _Streamer, rgb: tuple[int, int, int]) -> str:
    image = {
        "type": "image_url",
        "image_url": {"url": data_url("image/png", solid_png(16, 16, rgb))},
    }
    messages = [{"role": "user", "content": [{"type": "text", "text": PROBE_QUESTION}, image]}]
    text = ""
    for item in client.stream_chat(
        messages, max_tokens=24, temperature=0.0, reasoning_effort="none"
    ):
        if isinstance(item, StreamToken) and item.stream == "content":
            text += item.text
    return text.lower()


def server_accepts_images(client: _Streamer) -> bool:
    """Whether this server's model reads images, asked once and remembered.

    True only when the model names a red picture red and a blue one blue, so
    a server that ignores the image part (and answers from the words) is not
    mistaken for one that sees it. A server that refuses the request (HTTP
    4xx) is a definite no. Any other failure raises: a transient error must
    not be remembered as "cannot see".
    """
    key = getattr(client, "server_key", None) or id(client)
    if key in _CACHE:
        return _CACHE[key]
    try:
        verdict = all(
            name in (answer := _answer(client, rgb))
            and not any(other in answer for other, _ in PROBES if other != name)
            for name, rgb in PROBES
        )
    except VllmRequestError as exc:
        if not str(exc).startswith("server returned HTTP 4"):
            raise
        verdict = False
    _CACHE[key] = verdict
    return verdict
