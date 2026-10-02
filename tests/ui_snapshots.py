"""Compare the chat page's key views with committed references (SNAPSHOT tests).

Two checks per view, in each theme, with different portability:

* Pixels. A screenshot of the view against a reference PNG, through
  pixelmatch (`fixtures/snapshot_compare.mjs`). Font rasterisation differs
  between machines, so the references are tied to the rendering environment
  that made them (`environment_mismatch`): on another environment the pixel
  check is not run and the test skips, naming the difference, instead of
  passing on nothing. `REQUIRE_ENV` turns that skip into a failure.
* Structure. For a fixed list of selectors per view: whether the element is
  there, its colours, display, visibility, opacity and visible text exactly,
  and its box within `BOX_TOLERANCE_PX` of the reference; plus relations that
  need no reference at all (an element has a size, a child sits inside its
  parent, one block sits below another, two siblings do not overlap). This
  holds on any machine, because fonts move text widths by a few pixels and
  never make a button vanish.

Both are checked by instance in tests/test_ui_snapshots.py: an identical
capture passes; a missing, collapsed or recoloured element, and one changed
region, each fail naming the view and the selector or the pixel count.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any, NoReturn

import pytest

HERE = Path(__file__).parent
COMPARE = HERE / "fixtures" / "snapshot_compare.mjs"
REFERENCES = HERE / "fixtures" / "ui_snapshots"

UPDATE_ENV = "SADDLE_UPDATE_SNAPSHOTS"
"""Set to `1` to rewrite the references from a fresh capture instead of comparing."""
REQUIRE_ENV = "SADDLE_REQUIRE_SNAPSHOTS"
"""Set to `1` where a pixel check that cannot run (a different rendering
environment, or no reference for it) must be a failure, not a skip."""

THEMES = ("dark", "light")
VIEWS = ("chat", "tool-collapsed", "tool-expanded", "packet")
IMAGE_NAMES = tuple(f"{theme}-{view}.png" for theme in THEMES for view in VIEWS)
"""The reference images, one per theme and view; `test_images` excepts exactly these."""

PIXEL_THRESHOLD = 0.1
"""pixelmatch's per-pixel colour distance (0 identical, 1 opposite) above
which a pixel differs; its own default, left alone."""
MAX_DIFFERING_PIXELS = 12
"""Differing pixels one view may show and still pass. Twenty captures of each
of the eight views, on an idle machine and with four captures at once on a
loaded one, were pixel-identical (0 differing), so this is headroom against
a rasteriser's rare one-pixel wobble, not a measured need. It is below what a
changed glyph or a moved control costs (dozens to thousands of pixels)."""
BOX_TOLERANCE_PX: dict[str, int | None] = {
    "chat": 20,
    "tool-collapsed": 20,
    "tool-expanded": 20,
    "packet": None,
}
"""How far an element's box may move from the reference, per coordinate, in each
view; None means the boxes are not compared with the reference at all.

Chosen from swapping the page's sans and monospace families for four other
pairs on this machine: in the chat and tool views no coordinate or size moved
by more than 14 pixels (an inline `code` chip's left edge, a longer label),
and 20 leaves headroom. In the packet a narrower font lets the last action
button join the first row (that button's left edge moved by 600 pixels and the
card changed height by 41), so no box tolerance is portable there: the packet
is held to its colours, text and the relations in `RELATIONS`, and to the
pixels where the environment matches."""

FIELDS_EXACT = ("color", "background", "display", "visibility", "opacity", "text")


def _set(name: str, environ: Mapping[str, str] | None) -> bool:
    return (os.environ if environ is None else environ).get(name, "") not in ("", "0")


def update_requested(environ: Mapping[str, str] | None = None) -> bool:
    """Whether `UPDATE_ENV` asks for the references to be rewritten."""
    return _set(UPDATE_ENV, environ)


def strict_requested(environ: Mapping[str, str] | None = None) -> bool:
    """Whether `REQUIRE_ENV` asks that a pixel check that cannot run fail."""
    return _set(REQUIRE_ENV, environ)


Capture = dict[str, Any]
"""What `snapshot_cdp.mjs` prints: chrome, platform, views."""


# -- the rendering environment ------------------------------------------------


def environment_of(capture: Mapping[str, Any]) -> dict[str, Any]:
    """Chrome's major version, the platform and the fonts the views used.

    The fonts are the family and PostScript names Chrome reports for the
    elements of every view (so a symbol falling back to another family counts),
    and `fonts_sha256` is the hash of that sorted list.
    """
    fonts = sorted({f for view in capture["views"].values() for f in view["fonts"]})
    digest = hashlib.sha256("\n".join(fonts).encode()).hexdigest()
    return {
        "chrome_major": str(capture["chrome"]).rpartition("/")[2].split(".")[0],
        "platform": capture["platform"],
        "fonts_sha256": digest,
        "fonts": fonts,
    }


def environment_mismatch(reference: Mapping[str, Any], current: Mapping[str, Any]) -> list[str]:
    """Each way the current environment differs from the one the references were made in."""
    reasons: list[str] = []
    for key, label in (("chrome_major", "Chrome major version"), ("platform", "platform")):
        if reference[key] != current[key]:
            reasons.append(f"{label} is {current[key]}, references were made with {reference[key]}")
    if reference["fonts_sha256"] != current["fonts_sha256"]:
        gone = sorted(set(reference["fonts"]) - set(current["fonts"]))
        new = sorted(set(current["fonts"]) - set(reference["fonts"]))
        reasons.append(
            f"fonts differ (not here: {', '.join(gone) or 'none'}; "
            f"only here: {', '.join(new) or 'none'})"
        )
    return reasons


# -- pixels -------------------------------------------------------------------


@dataclass(frozen=True)
class PixelResult:
    view: str
    differing: int
    problem: str | None
    """None when within `MAX_DIFFERING_PIXELS`; otherwise the failure, naming the view."""


def pixel_diff(
    view: str,
    reference: Path,
    actual: Path,
    diff: Path,
    *,
    budget: int = MAX_DIFFERING_PIXELS,
    node: str | None = None,
) -> PixelResult:
    """Compare two PNGs; on a difference, `diff` gets an image of where.

    A failing lookup raises: a comparison that could not run must not read as
    "no pixels differ".
    """
    run = subprocess.run(
        [
            node or shutil.which("node") or "node",
            str(COMPARE),
            str(reference),
            str(actual),
            str(diff),
            str(PIXEL_THRESHOLD),
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    if run.returncode != 0:
        message = f"{COMPARE.name} failed on {view}: {run.stderr.strip()}"
        raise RuntimeError(message)
    got: dict[str, Any] = json.loads(run.stdout)
    if got.get("error") == "size":
        problem = (
            f"{view}: the picture is {got['actual'][0]}x{got['actual'][1]}, "
            f"the reference {got['reference'][0]}x{got['reference'][1]}"
        )
        return PixelResult(view, -1, problem)
    differing = int(got["differing"])
    if differing <= budget:
        return PixelResult(view, differing, None)
    problem = (
        f"{view}: {differing} pixels differ from the reference "
        f"(allowed {budget}); the diff image is {diff}"
    )
    return PixelResult(view, differing, problem)


# -- structure ----------------------------------------------------------------

Element = dict[str, Any]
Elements = Mapping[str, "Element | None"]


def structure_problems(
    view: str,
    reference: Elements,
    actual: Elements,
    *,
    tolerance: int | None,
) -> list[str]:
    """Every selector whose element is gone, changed or moved, named with the view.

    `tolerance` is the most an element's box may move, per coordinate; None
    leaves the boxes out and compares everything else.
    """
    problems: list[str] = []
    for label, was in reference.items():
        now = actual.get(label)
        if was is None:  # the reference had none: nothing to hold it to
            continue
        if now is None:
            problems.append(f"{view}: {label} ({was['selector']}) is not on the page")
            continue
        for field in FIELDS_EXACT:
            if now[field] != was[field]:
                problems.append(
                    f"{view}: {label} ({was['selector']}) {field} is {now[field]!r}, "
                    f"was {was[field]!r}"
                )
        for field in ("x", "y", "w", "h") if tolerance is not None else ():
            if abs(now[field] - was[field]) > tolerance:
                problems.append(
                    f"{view}: {label} ({was['selector']}) {field} is {now[field]}, "
                    f"was {was[field]} (tolerance {tolerance})"
                )
    for label, now in actual.items():
        if now is not None and label not in reference:
            problems.append(f"{view}: {label} ({now['selector']}) is not in the reference")
    return problems


def _box(e: Element) -> tuple[int, int, int, int]:
    return e["x"], e["y"], e["x"] + e["w"], e["y"] + e["h"]


# Relations that hold in any rendering, per view. Each is (kind, a, b):
#   shown  a            a has a size and is visible
#   inside a b          a's box lies within b's
#   below  a b          a starts at or under b's bottom edge
#   apart  a b          a and b do not overlap
RELATIONS: dict[str, tuple[tuple[str, ...], ...]] = {
    "chat": (
        ("shown", "user"),
        ("shown", "paragraph"),
        ("shown", "code-block"),
        ("shown", "copy"),
        ("inside", "copy", "code-block"),
        ("inside", "code-text", "code-block"),
        ("below", "heading", "user"),
        ("below", "paragraph", "heading"),
        ("below", "list", "paragraph"),
        ("below", "code-block", "list"),
    ),
    "tool-collapsed": (
        ("shown", "row"),
        ("shown", "label"),
        ("shown", "copy"),
        ("inside", "summary", "row"),
        ("inside", "label", "summary"),
        ("inside", "copy", "summary"),
        ("apart", "label", "copy"),
    ),
    "tool-expanded": (
        ("shown", "row"),
        ("shown", "copy"),
        ("shown", "output"),
        ("inside", "copy", "summary"),
        ("inside", "output", "row"),
        ("below", "output", "summary"),
        ("apart", "label", "copy"),
    ),
    "packet": (
        ("shown", "verdict-word"),
        ("shown", "view-diff"),
        ("shown", "merge"),
        ("shown", "discard"),
        ("shown", "ask"),
        ("shown", "download"),
        ("shown", "band"),
        ("shown", "tests-line"),
        ("shown", "not-proven-line"),
        ("shown", "rows"),
        ("shown", "details"),
        ("inside", "actions", "packet"),
        ("inside", "band", "packet"),
        ("below", "actions", "verdict"),
        ("below", "band", "actions"),
        ("below", "rows", "band"),
        ("below", "details", "rows"),
        ("inside", "view-diff", "actions"),
        ("inside", "merge", "actions"),
        ("inside", "discard", "actions"),
        ("apart", "view-diff", "merge"),
        ("apart", "merge", "discard"),
        ("apart", "discard", "ask"),
    ),
}


def relation_problems(label: str, view: str, elements: Elements) -> list[str]:
    """Relations among the current elements that must hold on any machine.

    `label` names the capture in each problem (`dark/chat`); `view` picks the
    relations (`chat`).
    """
    problems: list[str] = []
    for kind, *names in RELATIONS[view]:
        found = [elements.get(n) for n in names]
        missing = [n for n, e in zip(names, found, strict=True) if e is None]
        if missing:
            problems.append(f"{label}: {', '.join(missing)} is not on the page ({kind} {names})")
            continue
        boxes = [e for e in found if e is not None]
        first = boxes[0]
        if kind == "shown":
            if first["w"] <= 0 or first["h"] <= 0:
                problems.append(f"{label}: {names[0]} has no size ({first['w']}x{first['h']})")
            if (
                first["visibility"] != "visible"
                or first["display"] == "none"
                or first["opacity"] == "0"
            ):
                problems.append(
                    f"{label}: {names[0]} is not visible "
                    f"(visibility {first['visibility']}, display {first['display']}, "
                    f"opacity {first['opacity']})"
                )
            continue
        second = boxes[1]
        ax0, ay0, ax1, ay1 = _box(first)
        bx0, by0, bx1, by1 = _box(second)
        if kind == "inside" and not (ax0 >= bx0 and ay0 >= by0 and ax1 <= bx1 and ay1 <= by1):
            problems.append(f"{label}: {names[0]} is not inside {names[1]}")
        elif kind == "below" and ay0 < by1:
            problems.append(f"{label}: {names[0]} starts above the bottom of {names[1]}")
        elif kind == "apart" and ax0 < bx1 and bx0 < ax1 and ay0 < by1 and by0 < ay1:
            problems.append(f"{label}: {names[0]} overlaps {names[1]}")
    return problems


# -- references on disk -------------------------------------------------------


def reference_pngs() -> list[str]:
    """The picture files in the committed reference directory, sorted."""
    return sorted(path.name for path in REFERENCES.glob("*.png"))


def capture_names() -> list[str]:
    return [f"{theme}/{view}" for theme in THEMES for view in VIEWS]


def write_references(directory: Path, capture: Mapping[str, Any], shots: Path) -> None:
    """Replace the references in `directory` with this capture (the update path)."""
    directory.mkdir(parents=True, exist_ok=True)
    for name in IMAGE_NAMES:
        shutil.copyfile(shots / name, directory / name)
    structure = {key: view["elements"] for key, view in capture["views"].items()}
    sizes = {key: [view["width"], view["height"]] for key, view in capture["views"].items()}
    (directory / "structure.json").write_text(
        json.dumps({"sizes": sizes, "elements": structure}, indent=1, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    (directory / "environment.json").write_text(
        json.dumps(environment_of(capture), indent=1, sort_keys=True) + "\n", encoding="utf-8"
    )


def read_structure(directory: Path) -> dict[str, Any]:
    got: dict[str, Any] = json.loads((directory / "structure.json").read_text(encoding="utf-8"))
    return got


def read_environment(directory: Path) -> dict[str, Any]:
    got: dict[str, Any] = json.loads((directory / "environment.json").read_text(encoding="utf-8"))
    return got


def stop_for_environment(mismatch: list[str], *, strict: bool) -> NoReturn:
    """End a pixel test whose references came from another environment.

    A skip, naming each difference and the regeneration command, so the
    audit lists the test as not proven; a failure when `strict` (set by
    `REQUIRE_ENV`). Never a pass.
    """
    message = (
        "pixel references were made in another rendering environment: "
        + "; ".join(mismatch)
        + f". Regenerate here with {UPDATE_ENV}=1 python -m pytest tests/test_ui_snapshots.py"
    )
    if strict:
        pytest.fail(f"{REQUIRE_ENV} is set, so this may not skip: {message}")
    pytest.skip(message)


def check_capture(
    directory: Path,
    capture: Mapping[str, Any],
    shots: Path,
    diffs: Path,
    *,
    pixel_runner: Callable[..., PixelResult] = pixel_diff,
) -> tuple[list[str], list[str], list[str]]:
    """(structure problems, pixel problems, environment mismatches) of a capture.

    Structure is always checked. Pixels are checked only when the environment
    matches the references' own; otherwise the pixel list is empty and the
    mismatch list says why, for the caller to skip or fail on.
    """
    reference = read_structure(directory)["elements"]
    structure: list[str] = []
    for key, view in capture["views"].items():
        name = key.partition("/")[2]
        structure += structure_problems(
            key, reference[key], view["elements"], tolerance=BOX_TOLERANCE_PX[name]
        )
        structure += relation_problems(key, name, view["elements"])
    mismatch = environment_mismatch(read_environment(directory), environment_of(capture))
    pixels: list[str] = []
    if not mismatch:
        diffs.mkdir(parents=True, exist_ok=True)
        for name in IMAGE_NAMES:
            result = pixel_runner(
                name.removesuffix(".png"), directory / name, shots / name, diffs / name
            )
            if result.problem:
                pixels.append(result.problem)
    return structure, pixels, mismatch
