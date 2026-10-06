"""`read_text` and `compare_images`: tesseract's and ImageMagick's view of an image.

Contract: with the `ocr` or `imagediff` switch on and its programs installed,
the tool is offered (and allowed in a lane that lists its tools); otherwise it
is not. `read_text` returns the text tesseract reads, says when there is none,
and always carries the misreading caveat; `compare_images` counts the pixels
that differ between two images of one size and gives the box that holds them.
Neither hands ImageMagick a file whose own header is not an image.
"""

from __future__ import annotations

import shutil
import subprocess
from collections.abc import Sequence
from pathlib import Path
from typing import Any

import pytest

from saddle import imagetools
from saddle.imagetools import OCR_CAVEAT, compare_images, read_text, run_program
from saddle.labels import describe
from saddle.tools import (
    COMPARE_IMAGES_TOOL,
    READ_TEXT_TOOL,
    ToolContext,
    execute_tool,
    offer_image_tools,
    tools_for_mode,
)
from saddle.vision import IMAGE_MAX_BYTES, solid_png
from saddle.vllm import ToolCall

REAL = all(shutil.which(p) for p in ("convert", "compare", "tesseract"))
needs_tools = pytest.mark.skipif(not REAL, reason="ImageMagick and tesseract are not installed")


def _draw(path: Path, *draw: str, size: str = "480x160") -> Path:
    subprocess.run(["convert", "-size", size, "xc:white", *draw, f"png:{path}"], check=True)
    return path


def _names(found: list[dict[str, Any]]) -> list[str]:
    return [t["function"]["name"] for t in found]


@needs_tools
def test_read_text_reads_printed_text_and_says_it_can_misread(tmp_path: Path) -> None:
    text = [*"-fill black -pointsize 32 -annotate +20+90".split(), "SADDLE READS 42"]
    shown = read_text(_draw(tmp_path / "t.png", *text), "t.png")
    assert shown.endswith("\nSADDLE READS 42")
    assert OCR_CAVEAT in shown


@needs_tools
def test_read_text_says_a_blank_image_has_no_text(tmp_path: Path) -> None:
    shown = read_text(_draw(tmp_path / "w.png"), "w.png")
    assert shown.startswith("tesseract found no text in w.png")
    assert OCR_CAVEAT in shown


@needs_tools
def test_compare_images_counts_the_changed_pixels_and_boxes_them(tmp_path: Path) -> None:
    white = _draw(tmp_path / "w.png")
    boxed = _draw(tmp_path / "r.png", "-fill", "red", "-draw", "rectangle 100,20 159,49")
    assert compare_images(white, "w.png", boxed, "r.png") == (
        "w.png and r.png (480x160): 1,800 of 76,800 pixels differ (2.34%), "
        "all within x=100, y=20, 60x30 (left, top, width, height)."
    )
    assert compare_images(white, "w.png", white, "w2.png") == (
        "w.png and w2.png (480x160) are identical: no pixel differs."
    )


def test_compare_images_refuses_two_sizes(tmp_path: Path) -> None:
    (tmp_path / "a.png").write_bytes(solid_png(4, 4, (255, 0, 0)))
    (tmp_path / "b.png").write_bytes(solid_png(4, 5, (255, 0, 0)))
    shown = compare_images(tmp_path / "a.png", "a.png", tmp_path / "b.png", "b.png")
    assert shown.startswith("error: a.png is 4x4 and b.png is 4x5")


def test_a_file_that_is_not_an_image_by_its_header_never_reaches_imagemagick(
    tmp_path: Path,
) -> None:
    ran: list[Sequence[str]] = []

    def run(argv: Sequence[str]) -> tuple[int, str, str]:
        ran.append(argv)
        return 0, "", ""

    (tmp_path / "fake.png").write_text("push graphic-context\nviewbox 0 0 1 1\n")
    (tmp_path / "ok.png").write_bytes(solid_png(4, 4, (255, 0, 0)))
    (tmp_path / "big.png").write_bytes(b"\0" * (IMAGE_MAX_BYTES + 1))
    assert "not a PNG, JPEG, WebP or GIF" in read_text(tmp_path / "fake.png", "fake.png", run)
    assert "not a PNG" in compare_images(
        tmp_path / "ok.png", "ok.png", tmp_path / "fake.png", "fake.png", run
    )
    assert "not a PNG" in compare_images(
        tmp_path / "fake.png", "fake.png", tmp_path / "ok.png", "ok.png", run
    )
    assert "over the" in read_text(tmp_path / "big.png", "big.png", run)
    assert read_text(tmp_path / "gone.png", "gone.png", run) == "error: cannot read 'gone.png'"
    assert ran == []


def test_imagemagick_is_told_the_decoder_and_reads_one_frame(tmp_path: Path) -> None:
    ran: list[Sequence[str]] = []

    def run(argv: Sequence[str]) -> tuple[int, str, str]:
        ran.append(argv)
        return (1, "", "7\n") if argv[0] == "compare" else (0, "2x1+0+0", "")

    (tmp_path / "a.png").write_bytes(solid_png(4, 4, (255, 0, 0)))
    compare_images(tmp_path / "a.png", "a.png", tmp_path / "a.png", "b.png", run)
    assert ran[0][3:5] == [f"png:{tmp_path / 'a.png'}[0]"] * 2


@pytest.mark.parametrize(
    ("answers", "expected"),
    [
        ([(1, "", "convert: bad\n")], "error: ImageMagick could not prepare a.png"),
        ([(0, "", ""), (1, "", "tess: broke\n")], "error: tesseract could not read a.png: tess"),
        ([(0, "", ""), (0, "  \n\n", "")], "tesseract found no text in a.png"),
        ([(0, "", ""), (0, "one  \n\n two\n", "")], f"({OCR_CAVEAT}):\none\n two"),
    ],
)
def test_read_text_names_each_failure(
    tmp_path: Path, answers: list[tuple[int, str, str]], expected: str
) -> None:
    (tmp_path / "a.png").write_bytes(solid_png(4, 4, (255, 0, 0)))
    queue = iter(answers)
    shown = read_text(tmp_path / "a.png", "a.png", lambda _argv: next(queue))
    assert expected in shown


@pytest.mark.parametrize(
    ("answers", "expected"),
    [
        ([(2, "", "compare: broke\n")], "error: ImageMagick could not compare a.png and b.png"),
        ([(1, "", "")], "error: ImageMagick's compare gave no pixel count: no output"),
        ([(1, "", "many\n")], "gave no pixel count: many"),
        ([(0, "", "0\n")], "are identical"),
        ([(1, "", "3\n"), (1, "", "box: broke\n")], "3 of 16 pixels differ (18.75%); where"),
        ([(1, "", "3\n"), (0, "3x1+1+2", "")], "all within x=1, y=2, 3x1"),
    ],
)
def test_compare_images_names_each_outcome(
    tmp_path: Path, answers: list[tuple[int, str, str]], expected: str
) -> None:
    (tmp_path / "a.png").write_bytes(solid_png(4, 4, (255, 0, 0)))
    queue = iter(answers)
    shown = compare_images(
        tmp_path / "a.png", "a.png", tmp_path / "a.png", "b.png", lambda _argv: next(queue)
    )
    assert expected in shown


def test_run_program_reports_a_missing_program_and_a_timeout(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    assert run_program(["true"]) == (0, "", "")
    assert run_program(["saddle-no-such-program-x"])[0] == 127
    monkeypatch.setattr(imagetools, "TIMEOUT_S", 0.01)
    code, _, err = run_program(["sleep", "5"])
    assert code == 124
    assert "took longer" in err


def test_the_tools_are_offered_only_when_switched_on_and_installed(tmp_path: Path) -> None:
    installed = lambda name: f"/usr/bin/{name}"  # noqa: E731
    ask = ToolContext(workdir=tmp_path, ocr=True, imagediff=True)
    ask.allowed = tuple(_names(tools_for_mode("ask")))
    offered = _names(offer_image_tools(tools_for_mode("ask"), ask, installed))
    assert offered[-2:] == [READ_TEXT_TOOL, COMPARE_IMAGES_TOOL]
    assert ask.allowed is not None
    assert {READ_TEXT_TOOL, COMPARE_IMAGES_TOOL} <= set(ask.allowed)
    again = offer_image_tools(
        offer_image_tools(tools_for_mode("ask"), ask, installed), ask, installed
    )
    assert _names(again).count(READ_TEXT_TOOL) == 1

    for ctx, which in [
        (ToolContext(workdir=tmp_path), installed),  # both switches off
        (ToolContext(workdir=tmp_path, ocr=True, imagediff=True), lambda _name: None),
    ]:
        names = _names(offer_image_tools(tools_for_mode("ask"), ctx, which))
        assert READ_TEXT_TOOL not in names
        assert COMPARE_IMAGES_TOOL not in names
    only_tesseract = lambda name: None if name == "convert" else name  # noqa: E731
    ocr = ToolContext(workdir=tmp_path, ocr=True)
    assert READ_TEXT_TOOL not in _names(offer_image_tools([], ocr, only_tesseract))


@needs_tools
def test_the_handlers_read_files_in_the_folder_and_refuse_outside_it(tmp_path: Path) -> None:
    ctx = ToolContext(workdir=tmp_path, ocr=True, imagediff=True)
    _draw(tmp_path / "w.png")
    found = execute_tool(
        ToolCall(id="c1", name=READ_TEXT_TOOL, arguments='{"path": "w.png"}'),
        workdir=tmp_path,
        context=ctx,
    )
    assert found.startswith("tesseract found no text in w.png")
    same = execute_tool(
        ToolCall(
            id="c2", name=COMPARE_IMAGES_TOOL, arguments='{"first": "w.png", "second": "w.png"}'
        ),
        workdir=tmp_path,
        context=ctx,
    )
    assert same.endswith("are identical: no pixel differs.")
    outside = execute_tool(
        ToolCall(id="c3", name=READ_TEXT_TOOL, arguments='{"path": "../x.png"}'),
        workdir=tmp_path,
        context=ctx,
    )
    assert outside.startswith("error")


def test_labels_name_what_each_tool_looks_at() -> None:
    assert describe(READ_TEXT_TOOL, '{"path": "a.png"}')[1] == "Read the text in a.png"
    assert describe(COMPARE_IMAGES_TOOL, '{"first": "a.png", "second": "b.png"}')[1] == (
        "Compared a.png with b.png"
    )
