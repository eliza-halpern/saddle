"""Two image tools the person can switch on (`capabilities` `ocr`, `imagediff`).

`read_text` is tesseract's reading of the text in an image: a check on what a
model that sees images reads there, and the only reading a model without
images gets. `compare_images` is ImageMagick's count of the pixels that differ
between two images of one size, and the box that holds them all: whether a
screen changed, and where, without asking anyone to look.

Neither understands anything. tesseract misreads look-alike characters
(measured here on a 14 px dialog: "MissingIni" read "Missingini"; at 10 px
"ZX4R81" read "2X4R81"), so its result says so. A pixel count says that
something changed, never whether the change is right.

Both read only files whose own header is a PNG, JPEG, WebP or GIF
(`vision.image_info`), and name that decoder to ImageMagick (`png:path`):
ImageMagick otherwise picks a decoder from the file's contents, and some of
its decoders run other programs, so a file shaped to look like one must
never reach them. Only the first frame of an animation is read.
"""

from __future__ import annotations

import subprocess
import tempfile
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Final

from saddle.screen import OCR_PREP
from saddle.vision import IMAGE_MAX_BYTES, ImageInfo, image_info

type Run = Callable[[Sequence[str]], tuple[int, str, str]]
"""Runs one program: (exit code, stdout, stderr)."""

TIMEOUT_S: Final = 60
"""How long one ImageMagick or tesseract call may take."""

CODERS: Final = {"image/png": "png", "image/jpeg": "jpeg", "image/webp": "webp", "image/gif": "gif"}
"""The ImageMagick decoder for each type `image_info` recognises, by name."""

READ_TEXT_PSM: Final = "6"
"""tesseract's page segmentation: one block of text. On a chat-page screenshot
it kept each line whole (8 lines), where the sparse mode (11) split them (12)."""

OCR_CAVEAT: Final = (
    "character recognition, which misreads look-alike characters (I and l, Z and 2, "
    "O and 0): a check, not the truth"
)


def run_program(argv: Sequence[str]) -> tuple[int, str, str]:
    """`Run` for real: no shell, a timeout, and the output as text."""
    try:
        done = subprocess.run(
            list(argv), capture_output=True, text=True, timeout=TIMEOUT_S, check=False
        )
    except subprocess.TimeoutExpired:
        return 124, "", f"{argv[0]} took longer than {TIMEOUT_S} s"
    except FileNotFoundError:
        return 127, "", f"{argv[0]} is not installed"
    return done.returncode, done.stdout, done.stderr


def _image(path: Path, name: str) -> tuple[ImageInfo, str] | str:
    """`path`'s image header and the decoder-named spec ImageMagick is given,
    or "error: ..." naming why it is not an image these tools read."""
    if not path.is_file():
        return f"error: cannot read {name!r}"
    size = path.stat().st_size
    if size > IMAGE_MAX_BYTES:
        return f"error: {name} is {size:,} bytes, over the {IMAGE_MAX_BYTES:,}-byte image limit"
    info = image_info(path.read_bytes())
    if info is None:
        return f"error: {name} is not a PNG, JPEG, WebP or GIF image (judged by its header)"
    return info, f"{CODERS[info.mime]}:{path}[0]"


def _last(text: str) -> str:
    lines = text.strip().splitlines()
    return lines[-1].strip() if lines else "no output"


def read_text(path: Path, name: str, run: Run = run_program) -> str:
    """The text tesseract reads in the image at `path` (`name` as the model
    gave it), after the same greyscale, threshold and enlargement the
    screenshot tool's `find` uses (`screen.OCR_PREP`); "error: ..." when it
    cannot run, never an empty answer."""
    found = _image(path, name)
    if isinstance(found, str):
        return found
    _, spec = found
    with tempfile.TemporaryDirectory(prefix="saddle-ocr-") as scratch:
        big = Path(scratch) / "big.png"
        code, _, err = run(["convert", spec, *OCR_PREP, f"png:{big}"])
        if code != 0:
            return f"error: ImageMagick could not prepare {name} for reading: {_last(err)}"
        code, out, err = run(["tesseract", str(big), "-", "--psm", READ_TEXT_PSM])
    if code != 0:
        return f"error: tesseract could not read {name}: {_last(err)}"
    text = "\n".join(line.rstrip() for line in out.strip().splitlines() if line.strip())
    if not text:
        return f"tesseract found no text in {name} ({OCR_CAVEAT})."
    return f"Text tesseract read in {name} ({OCR_CAVEAT}):\n{text}"


def compare_images(
    first: Path, first_name: str, second: Path, second_name: str, run: Run = run_program
) -> str:
    """How many pixels differ between two images of one size, and the box
    (left, top, width, height) that holds every one; "error: ..." when the two
    cannot be compared."""
    first_found = _image(first, first_name)
    if isinstance(first_found, str):
        return first_found
    second_found = _image(second, second_name)
    if isinstance(second_found, str):
        return second_found
    (a, a_spec), (b, b_spec) = first_found, second_found
    if (a.width, a.height) != (b.width, b.height):
        return (
            f"error: {first_name} is {a.width}x{a.height} and {second_name} is "
            f"{b.width}x{b.height}; compare_images needs two images of one size"
        )
    both = f"{first_name} and {second_name} ({a.width}x{a.height})"
    code, _, err = run(["compare", "-metric", "AE", a_spec, b_spec, "null:"])
    if code not in (0, 1):
        return f"error: ImageMagick could not compare {first_name} and {second_name}: {_last(err)}"
    try:
        differ = round(float(err.strip().split()[0]))
    except (ValueError, IndexError):
        return f"error: ImageMagick's compare gave no pixel count: {_last(err)}"
    if differ == 0:
        return f"{both} are identical: no pixel differs."
    box = [
        "convert",
        a_spec,
        b_spec,
        "-compose",
        "difference",
        "-composite",
        "-colorspace",
        "gray",
        "-threshold",
        "0",
        "-format",
        "%@",
        "info:",
    ]
    code, out, err = run(box)
    total = a.width * a.height
    counted = f"{both}: {differ:,} of {total:,} pixels differ ({100 * differ / total:.2f}%)"
    try:
        size, x, y = out.strip().split("+")
        width, height = size.split("x")
        where = f", all within x={int(x)}, y={int(y)}, {int(width)}x{int(height)}"
    except ValueError:
        return f"{counted}; where could not be found: {_last(err or out)}."
    return f"{counted}{where} (left, top, width, height)."
