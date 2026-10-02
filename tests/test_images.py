"""The images in the repository are real, small, safe and used.

Contract: every tracked image is named by a tracked Markdown file (or listed
in `UNREFERENCED` with its reason), is a well-formed file of its type, is
under a size limit, and an SVG carries no script, event handler, DTD or
reference to anything outside itself. An image that a doc names but that is
missing is the link checker's finding (test_doc_links), not repeated here.
"""

from __future__ import annotations

import re
import struct
import xml.etree.ElementTree as ET
import zlib
from collections.abc import Collection
from pathlib import Path

import pytest
from doc_links import collect_links, local_target
from doc_tree import ROOT, tracked
from ui_snapshots import IMAGE_NAMES as SNAPSHOT_IMAGES

IMAGE_PATTERNS = (
    "*.png",
    "*.svg",
    "*.jpg",
    "*.jpeg",
    "*.gif",
    "*.webp",
    "*.ico",
    "*.bmp",
    "*.avif",
)

# Raster images may be 1 MiB: the five screenshots under docs/screenshots are
# 282 to 577 KiB, so this leaves room to retake one at a higher resolution
# while stopping a 20 MB phone photo, which every later clone would carry.
RASTER_MAX_BYTES = 1_048_576
# The logo is 6 KiB; an SVG past 64 KiB has embedded a bitmap or a font.
SVG_MAX_BYTES = 65_536

# Tracked images that no tracked Markdown file names, with the reason. The
# docs' own images are all shown in README.md or docs/USING-SADDLE.md; the rest
# are the UI snapshot tests' reference pictures, read by tests/test_ui_snapshots.py.
UNREFERENCED: dict[str, str] = {
    f"tests/fixtures/ui_snapshots/{name}": "reference picture read by tests/test_ui_snapshots.py"
    for name in SNAPSHOT_IMAGES
}

SVG_NS = "{http://www.w3.org/2000/svg}"
FORBIDDEN_ELEMENTS = frozenset({"script", "foreignobject", "iframe", "embed", "object"})
PNG_MAGIC = b"\x89PNG\r\n\x1a\n"


def png_check(data: bytes) -> tuple[int, int] | str:
    """(width, height) of a well-formed PNG, or the reason it is not one."""
    if not data.startswith(PNG_MAGIC):
        return "not a PNG: wrong signature"
    pos = len(PNG_MAGIC)
    kinds: list[bytes] = []
    size = (0, 0)
    while pos < len(data):
        if pos + 8 > len(data):
            return "truncated chunk header"
        length, kind = struct.unpack(">I4s", data[pos : pos + 8])
        body_end = pos + 8 + length
        if body_end + 4 > len(data):
            return f"truncated {kind.decode('latin-1')} chunk"
        body = data[pos + 8 : body_end]
        (crc,) = struct.unpack(">I", data[body_end : body_end + 4])
        if zlib.crc32(kind + body) != crc:
            return f"bad CRC in {kind.decode('latin-1')} chunk"
        if not kinds and kind != b"IHDR":
            return "first chunk is not IHDR"
        if kind == b"IHDR":
            if length != 13:
                return "IHDR is not 13 bytes"
            size = struct.unpack(">II", body[:8])
        kinds.append(kind)
        pos = body_end + 4
        if kind == b"IEND":
            break
    if kinds[-1:] != [b"IEND"] or pos != len(data):
        return "no IEND chunk at the end of the file"
    if b"IDAT" not in kinds:
        return "no image data"
    if 0 in size:
        return f"zero dimension {size}"
    return size


def svg_problems(data: bytes) -> list[str]:
    """Why an SVG is malformed or unsafe to show in a README; empty if it is fine."""
    head = data.decode("utf-8", errors="replace")
    problems: list[str] = []
    # An internal entity is how a few hundred bytes expand to gigabytes, and
    # nothing in a logo needs one: refuse the declarations before parsing.
    if re.search(r"<!(?:DOCTYPE|ENTITY)", head, re.IGNORECASE):
        problems.append("declares a DOCTYPE or ENTITY")
    if "<?xml-stylesheet" in head:
        problems.append("loads an external stylesheet")
    if problems:
        return problems
    try:
        root = ET.fromstring(data)
    except ET.ParseError as exc:
        return [f"not well-formed XML: {exc}"]
    if root.tag != f"{SVG_NS}svg":
        problems.append(f"root element is {root.tag!r}, not an SVG <svg>")
    for element in root.iter():
        name = element.tag.rpartition("}")[2].lower()
        if name in FORBIDDEN_ELEMENTS:
            problems.append(f"contains <{name}>")
        texts = [element.text or ""] if name == "style" else []
        for attr, value in element.attrib.items():
            local = attr.rpartition("}")[2].lower()
            if local.startswith("on"):
                problems.append(f"event handler attribute {local}")
            if local in {"href", "src"} and not value.startswith("#"):
                problems.append(f"external reference {local}={value!r}")
            if "javascript:" in value.lower():
                problems.append(f"javascript: in {local}")
            texts.append(value)
        for text in texts:
            if re.search(r"url\(\s*['\"]?(?!#)", text) or "@import" in text:
                problems.append(f"external reference in {text.strip()[:40]!r}")
    return problems


def image_problems(path: str, data: bytes) -> list[str]:
    """Everything wrong with one image file: type, well-formedness, size."""
    suffix = Path(path).suffix.lower()
    if suffix == ".png":
        checked = png_check(data)
        if isinstance(checked, str):
            return [checked]
        found: list[str] = []
    elif suffix == ".svg":
        found = svg_problems(data)
    elif suffix in {".jpg", ".jpeg"}:
        found = [] if data.startswith(b"\xff\xd8\xff") else ["not a JPEG: wrong signature"]
    elif suffix == ".gif":
        found = [] if data[:6] in {b"GIF87a", b"GIF89a"} else ["not a GIF: wrong signature"]
    else:
        return [f"no validator for {suffix} images: add one before tracking the file"]
    limit = SVG_MAX_BYTES if suffix == ".svg" else RASTER_MAX_BYTES
    if len(data) > limit:
        found.append(f"{len(data)} bytes exceeds the {limit} byte limit")
    return found


def unreferenced_images(
    root: Path,
    markdown: Collection[str],
    images: Collection[str],
    exceptions: Collection[str] = (),
) -> list[str]:
    """Images that no Markdown file names (and that are not listed as exceptions)."""
    named: set[str] = set()
    for rel in markdown:
        links, _ = collect_links((root / rel).read_text(encoding="utf-8"))
        named.update(t for link in links if (t := local_target(link.dest, rel)) is not None)
    return sorted(set(images) - named - set(exceptions))


def test_the_tracked_images_are_found_and_valid() -> None:
    images = tracked(*IMAGE_PATTERNS)
    # Five screenshots and the logo: an empty list would let every check pass.
    assert "docs/saddle-logo.svg" in images
    assert sum(name.endswith(".png") for name in images) >= 1
    problems = {
        name: found
        for name in images
        if (found := image_problems(name, (ROOT / name).read_bytes()))
    }
    assert problems == {}


def test_every_tracked_image_is_named_by_a_doc() -> None:
    images = tracked(*IMAGE_PATTERNS)
    assert unreferenced_images(ROOT, tracked("*.md"), images, UNREFERENCED) == []


def test_the_unreferenced_exceptions_are_still_images_in_the_tree() -> None:
    images = set(tracked(*IMAGE_PATTERNS))
    assert set(UNREFERENCED) <= images
    assert all(reason.strip() for reason in UNREFERENCED.values())


def test_an_image_no_doc_names_is_reported(tmp_path: Path) -> None:
    (tmp_path / "docs").mkdir()
    (tmp_path / "README.md").write_text("![a](docs/used.png)\n<img src='docs/also.svg'>\n")
    (tmp_path / "docs" / "guide.md").write_text("![x](../docs/used.png)\n")
    images = ["docs/used.png", "docs/also.svg", "docs/orphan.png"]
    markdown = ["README.md", "docs/guide.md"]
    assert unreferenced_images(tmp_path, markdown, images) == ["docs/orphan.png"]
    assert unreferenced_images(tmp_path, markdown, images, ["docs/orphan.png"]) == []


def _png(width: int = 3, height: int = 2) -> bytes:
    def chunk(kind: bytes, body: bytes) -> bytes:
        return (
            struct.pack(">I", len(body)) + kind + body + struct.pack(">I", zlib.crc32(kind + body))
        )

    rows = b"".join(b"\x00" + b"\xff\xff\xff" * width for _ in range(height))
    ihdr = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)
    return (
        PNG_MAGIC + chunk(b"IHDR", ihdr) + chunk(b"IDAT", zlib.compress(rows)) + chunk(b"IEND", b"")
    )


def _png_with_ihdr(ihdr: bytes) -> bytes:
    kind = b"IHDR"
    return (
        PNG_MAGIC
        + struct.pack(">I", len(ihdr))
        + kind
        + ihdr
        + struct.pack(">I", zlib.crc32(kind + ihdr))
    )


def test_a_real_png_reports_its_dimensions() -> None:
    assert png_check(_png(3, 2)) == (3, 2)
    assert image_problems("x.png", _png()) == []


@pytest.mark.parametrize(
    ("data", "message"),
    [
        (b"\xff\xd8\xff\xe0 a jpeg renamed to .png", "wrong signature"),
        (b"", "wrong signature"),
        (_png()[:-4], "truncated"),
        (_png()[:-12], "no IEND"),
        (_png() + b"trailing", "IEND"),
        (_png().replace(b"IDAT", b"IDAX"), "bad CRC"),
        (
            PNG_MAGIC + struct.pack(">I", 0) + b"IDAT" + struct.pack(">I", zlib.crc32(b"IDAT")),
            "IHDR",
        ),
        (_png(0, 2), "zero dimension"),
        (PNG_MAGIC + b"\x00\x00", "truncated chunk header"),
        (_png_with_ihdr(b"\x00" * 12), "IHDR is not 13 bytes"),
    ],
)
def test_a_corrupt_png_is_rejected(data: bytes, message: str) -> None:
    found = image_problems("x.png", data)
    assert found, "accepted a corrupt PNG"
    assert message in found[0]


def test_png_without_image_data_is_rejected() -> None:
    ihdr = struct.pack(">IIBBBBB", 1, 1, 8, 2, 0, 0, 0)

    def chunk(kind: bytes, body: bytes) -> bytes:
        return (
            struct.pack(">I", len(body)) + kind + body + struct.pack(">I", zlib.crc32(kind + body))
        )

    data = PNG_MAGIC + chunk(b"IHDR", ihdr) + chunk(b"IEND", b"")
    assert image_problems("x.png", data) == ["no image data"]


def test_other_raster_types_are_checked_by_signature_and_unknown_types_fail() -> None:
    assert image_problems("a.jpg", b"\xff\xd8\xff\xdb") == []
    assert image_problems("a.jpeg", b"\xff\xd8\xff\xdb") == []
    assert image_problems("a.gif", b"GIF89a....") == []
    assert image_problems("a.jpg", b"GIF89a") == ["not a JPEG: wrong signature"]
    assert image_problems("a.gif", b"\xff\xd8\xff") == ["not a GIF: wrong signature"]
    assert "no validator for .webp" in image_problems("a.webp", b"RIFF")[0]


def test_the_size_limit_is_per_type_and_exact() -> None:
    padding = b"\x00" * RASTER_MAX_BYTES
    assert image_problems("a.jpg", b"\xff\xd8\xff" + padding[:-3]) == []
    over = image_problems("a.jpg", b"\xff\xd8\xff" + padding[:-2])
    assert over == [f"{RASTER_MAX_BYTES + 1} bytes exceeds the {RASTER_MAX_BYTES} byte limit"]
    svg = b'<svg xmlns="http://www.w3.org/2000/svg"><!--' + b"x" * SVG_MAX_BYTES + b"--></svg>"
    assert any("exceeds the 65536 byte limit" in p for p in image_problems("a.svg", svg))


GOOD_SVG = (
    b'<?xml version="1.0"?>\n<!-- a comment -->\n'
    b'<svg xmlns="http://www.w3.org/2000/svg" xmlns:xlink="http://www.w3.org/1999/xlink">'
    b'<defs><linearGradient id="g"><stop offset="0"/></linearGradient><path id="p" d="M0 0"/>'
    b"<style>.a{fill:url(#g)}</style></defs>"
    b'<use xlink:href="#p" fill="url(#g)"/><text>safe</text></svg>'
)


def test_a_safe_svg_passes() -> None:
    assert svg_problems(GOOD_SVG) == []


@pytest.mark.parametrize(
    ("svg", "message"),
    [
        (b'<svg xmlns="http://www.w3.org/2000/svg"><script>alert(1)</script></svg>', "<script>"),
        (b'<svg xmlns="http://www.w3.org/2000/svg"><foreignObject/></svg>', "<foreignobject>"),
        (
            b'<svg xmlns="http://www.w3.org/2000/svg" onload="x()"/>',
            "event handler attribute onload",
        ),
        (b'<svg xmlns="http://www.w3.org/2000/svg"><a onClick="x()"/></svg>', "onclick"),
        (
            b'<svg xmlns="http://www.w3.org/2000/svg"><image href="https://x/y.png"/></svg>',
            "external reference href",
        ),
        (
            b'<svg xmlns="http://www.w3.org/2000/svg" xmlns:xlink="http://www.w3.org/1999/xlink">'
            b'<image xlink:href="data:image/png;base64,AAAA"/></svg>',
            "external reference href",
        ),
        (
            b'<svg xmlns="http://www.w3.org/2000/svg"><a href="javascript:x()"/></svg>',
            "external reference href",
        ),
        (
            b'<svg xmlns="http://www.w3.org/2000/svg" fill="url(https://x/g)"/>',
            "external reference in",
        ),
        (
            b'<svg xmlns="http://www.w3.org/2000/svg"><style>@import "x.css";</style></svg>',
            "external reference in",
        ),
        (
            b'<svg xmlns="http://www.w3.org/2000/svg"><style>a{b:url( "x.png")}</style></svg>',
            "external reference in",
        ),
        (
            b'<!DOCTYPE svg [<!ENTITY a "aaaa">]><svg xmlns="http://www.w3.org/2000/svg">&a;</svg>',
            "DOCTYPE",
        ),
        (b'<?xml-stylesheet href="x.css"?><svg xmlns="http://www.w3.org/2000/svg"/>', "stylesheet"),
        (b'<svg xmlns="http://www.w3.org/2000/svg"><g></svg>', "not well-formed XML"),
        (b"<html xmlns='http://www.w3.org/1999/xhtml'/>", "not an SVG"),
        (b'<svg xmlns="http://example.com/other"/>', "not an SVG"),
    ],
)
def test_an_unsafe_or_broken_svg_is_rejected(svg: bytes, message: str) -> None:
    found = svg_problems(svg)
    assert found, "accepted an unsafe SVG"
    assert any(message in problem for problem in found), found
