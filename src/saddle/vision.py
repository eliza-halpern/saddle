"""Showing the model an image file: what counts as one, and whether the server can see it.

`read_file` on a screenshot used to hand the model the file's bytes as text,
which it cannot look at. With images on (the Ask and Edit lanes; a Task run
never turns them on), `read_file` sends the image itself, in a user message
that follows the tool result, when the served model accepts images. The
standard OpenAI tool message carries text only and templates differ in what
they do with other parts, so the image rides in an ordinary user message,
which the server's own image path has to handle.

Whether the server accepts images is asked once per server and cached
(`server_accepts_images`): a server whose own model card says it takes no
images is a no without a request; otherwise the model is shown a picture of a
short made-up string (`PROBE_TEXT`) and must read it back. Naming a colour
proved only that it sees colour; a screenshot is worth showing only to a model
that can read the words on it.
"""

from __future__ import annotations

import base64
import re
import struct
import zlib
from collections.abc import Callable, Iterator, Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Final, Protocol

from saddle.vllm import StreamToken, VllmError, VllmRequestError

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


def _png(width: int, height: int, pixel: Callable[[int, int], tuple[int, int, int]]) -> bytes:
    """An RGB PNG whose pixel at (x, y) is `pixel(x, y)`."""
    rows = b"".join(
        b"\x00" + b"".join(bytes(pixel(x, y)) for x in range(width)) for y in range(height)
    )

    def chunk(kind: bytes, body: bytes) -> bytes:
        return (
            struct.pack(">I", len(body)) + kind + body + struct.pack(">I", zlib.crc32(kind + body))
        )

    return (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0))
        + chunk(b"IDAT", zlib.compress(rows))
        + chunk(b"IEND", b"")
    )


def solid_png(width: int, height: int, rgb: tuple[int, int, int]) -> bytes:
    """A plain one-colour PNG, built here so a probe needs no file."""
    return _png(width, height, lambda _x, _y: rgb)


GLYPHS: Final[Mapping[str, tuple[str, ...]]] = {
    "K": ("X...X", "X..X.", "X.X..", "XX...", "X.X..", "X..X.", "X...X"),
    "7": ("XXXXX", "....X", "...X.", "..X..", ".X...", ".X...", ".X..."),
    "P": ("XXXX.", "X...X", "X...X", "XXXX.", "X....", "X....", "X...."),
    "M": ("X...X", "XX.XX", "X.X.X", "X.X.X", "X...X", "X...X", "X...X"),
    "3": ("XXXX.", "....X", "....X", ".XXX.", "....X", "....X", "XXXX."),
    "X": ("X...X", "X...X", ".X.X.", "..X..", ".X.X.", "X...X", "X...X"),
}
"""A 5x7 bitmap of each character `PROBE_TEXT` uses: a picture of text built
here, with no font, no imaging library and no file."""

PROBE_TEXT: Final = "K7PM3X"
"""What the probe picture says: made up, so it cannot be answered from the
words of the question, and free of the pairs a reader confuses (O/0, I/1, S/5)."""

GLYPH_SCALE: Final = 8
"""Each bitmap cell is this many pixels square: 40x56-pixel letters, far from
the small-text case, since the probe asks whether the model reads at all."""


def text_png(text: str, scale: int = GLYPH_SCALE) -> bytes:
    """`text` in black capitals on white, one cell of space between letters and
    two around them. Only the characters in `GLYPHS` can be drawn."""
    rows, cols = 7, 5
    width = (len(text) * (cols + 1) + 3) * scale
    height = (rows + 4) * scale

    def pixel(x: int, y: int) -> tuple[int, int, int]:
        cx, cy = x // scale - 2, y // scale - 2
        index, col = divmod(cx, cols + 1)
        inked = (
            0 <= cy < rows
            and cx >= 0
            and index < len(text)
            and col < cols
            and GLYPHS[text[index]][cy][col] == "X"
        )
        return (0, 0, 0) if inked else (255, 255, 255)

    return _png(width, height, pixel)


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


PROBE_QUESTION: Final = (
    "What text is written in this image? Answer with that text only, or 'none' if there is none."
)

_CACHE: dict[object, bool] = {}


IMAGE_LIMIT: Final = re.compile(r"At most (\d+) image\(s\) may be provided in one prompt")
"""vLLM's refusal of a request carrying more images than `--limit-mm-per-prompt`
allows. The server publishes the limit nowhere else (not on its model card), so
its own sentence is how saddle learns it."""

_LIMITS: dict[object, int] = {}


def reset_cache() -> None:
    _CACHE.clear()
    _LIMITS.clear()


def image_limit(client: object) -> int | None:
    """The most images this client's server takes in one request, once a refusal
    has said (`learn_image_limit`); None while none has."""
    return _LIMITS.get(getattr(client, "server_key", None) or id(client))


def learn_image_limit(client: object, error: str) -> int | None:
    """Remember the limit `error` states for this client's server and return it;
    None, remembering nothing, when `error` is not that refusal."""
    found = IMAGE_LIMIT.search(error)
    if found is None:
        return None
    limit = int(found.group(1))
    _LIMITS[getattr(client, "server_key", None) or id(client)] = limit
    return limit


def _answer(client: _Streamer, png: bytes) -> str:
    image = {"type": "image_url", "image_url": {"url": data_url("image/png", png)}}
    messages = [{"role": "user", "content": [{"type": "text", "text": PROBE_QUESTION}, image]}]
    text = ""
    for item in client.stream_chat(
        messages, max_tokens=24, temperature=0.0, reasoning_effort="none"
    ):
        if isinstance(item, StreamToken) and item.stream == "content":
            text += item.text
    return text


def reads(answer: str, text: str = PROBE_TEXT) -> bool:
    """Whether `answer` reads back `text`: its letters and digits, case,
    spaces and punctuation aside, hold `text` exactly once."""
    kept = "".join(c for c in answer.upper() if c.isalnum())
    return kept.count(text) == 1


def server_accepts_images(client: _Streamer) -> bool:
    """Whether this server's model reads images, asked once and remembered.

    A server whose own card says it takes no images (`declares_images`) is a
    no without a request. Otherwise the model is shown `PROBE_TEXT` drawn by
    `text_png` and must read it back (`reads`), so a server that ignores the
    image part, or a model that sees colour but cannot read, is not mistaken
    for one that can read a screenshot. A server that refuses the request
    (HTTP 4xx) is a definite no. Any other failure raises: a transient error
    must not be remembered as "cannot see".
    """
    key = getattr(client, "server_key", None) or id(client)
    if key in _CACHE:
        return _CACHE[key]
    declared = getattr(client, "declares_images", None)
    try:
        says = declared() if declared is not None else None
    except VllmError:
        says = None  # a card that could not be read says nothing: the model is asked
    if says is False:
        _CACHE[key] = False
        return False
    try:
        verdict = reads(_answer(client, text_png(PROBE_TEXT)))
    except VllmRequestError as exc:
        if not str(exc).startswith("server returned HTTP 4"):
            raise
        verdict = False
    _CACHE[key] = verdict
    return verdict


def known_verdict(client: object) -> bool | None:
    """What `server_accepts_images` found for this client's server, without
    asking: None when it has not been asked yet."""
    return _CACHE.get(getattr(client, "server_key", None) or id(client))
