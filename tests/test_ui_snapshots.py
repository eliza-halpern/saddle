"""The chat page's key views against committed references, in both themes.

Contract: the chat transcript (a user turn, markdown, a fenced block with its
copy button), a tool row collapsed and expanded, and a finished run's packet
each render, in the dark and the light theme, as the committed reference
shows: the structure (which elements exist, their colours, visibility, text,
and their boxes within a tolerance) on any machine, and the pixels where the
machine renders as the references' machine did.

Known-good: a capture from a real Chrome against the references made by the
same capture; twenty such captures in a row were pixel-identical. An identical
image passes; a wobble below pixelmatch's threshold passes; exactly
`MAX_DIFFERING_PIXELS` differing pixels pass.

Known-bad, by instance, with no browser: one changed region fails naming the
view and the pixel count and writes a diff image; a picture of another size
fails; a missing element, a zero-size element, a hidden element, a changed
colour, changed text and a box moved past the tolerance each fail naming the
view and the selector; an element outside its parent, two siblings that
overlap and a block above the one it follows each fail. Known-bad through the
real browser: a CSS rule that hides the copy button, one that recolours the
light theme's text, and one that only changes pixels, each make the test that
should see it fail, and the structure test stays green for the last.

Environment. Font rasterisation differs between machines, so the pixel
references are recorded with the Chrome major version, the platform and the
fonts Chrome used (`environment.json`). In another environment the pixel
tests SKIP, naming the difference, and fail instead under
`SADDLE_REQUIRE_SNAPSHOTS=1`; the structure tests run everywhere. They are
never passed without having compared anything. Regenerate the references in
the environment you want them for with
`SADDLE_UPDATE_SNAPSHOTS=1 python -m pytest tests/test_ui_snapshots.py -k "not update_flag"`;
that run's own `update_flag` test fails by design, so the variable cannot stay
set unnoticed.

Notes on what was measured (this machine, Chrome 154, one sans and one
monospace family): 100 captures of each view, 20 idle and 80 as four at once
with every core busy, differ from the first by 0 pixels under the comparison
used, while raw bytes differ at one rounded corner by at most 2 of 255 in at
most 38 pixels (why the comparison has a threshold). Swapping the page's
fonts for four other pairs moved no box in the chat and tool views by more
than 14 pixels, and rewrapped the packet's buttons (why the packet has no box
tolerance).
"""

from __future__ import annotations

import copy
import json
import os
import struct
import subprocess
import zlib
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from browser_guard import BROWSER
from packet_seed import make_repo, seed
from test_images import png_check
from test_ui3_mode import NoModel, serving
from ui_snapshots import (
    BOX_TOLERANCE_PX,
    IMAGE_NAMES,
    MAX_DIFFERING_PIXELS,
    REFERENCES,
    RELATIONS,
    REQUIRE_ENV,
    THEMES,
    UPDATE_ENV,
    VIEWS,
    capture_names,
    check_capture,
    environment_mismatch,
    environment_of,
    pixel_diff,
    read_environment,
    read_structure,
    reference_pngs,
    relation_problems,
    stop_for_environment,
    strict_requested,
    structure_problems,
    update_requested,
    write_references,
)

from saddle.sessions import SessionStore
from saddle.web.app import build_app

ROOT = Path(__file__).resolve().parents[1]
CDP = Path(__file__).parent / "fixtures" / "snapshot_cdp.mjs"
# The pixel comparison needs the repository's node_modules, which mutmut's
# work copy does not carry (see test_mutmut_layout); the marker is `.git`, so
# a real checkout with a missing package fails instead of skipping.
pytestmark = [
    pytest.mark.skipif(not BROWSER, reason="needs node and google-chrome"),
    pytest.mark.skipif(
        not (ROOT / ".git").exists(), reason="needs a git checkout, not a mutant work copy"
    ),
]

CODE = 'def greet(name):\n    print("hi, " + name)\n    return len(name)'
MARKDOWN = (
    "## Plan\n\nHere is **greet**, with `len` and a list:\n\n- one\n- two\n\n"
    f"```python\n{CODE}\n```\n"
)


def _png(
    path: Path, width: int, height: int, paint: dict[tuple[int, int], tuple[int, int, int]]
) -> Path:
    """A solid mid-grey RGB PNG of the size, with the listed pixels painted."""

    def chunk(kind: bytes, body: bytes) -> bytes:
        return (
            struct.pack(">I", len(body)) + kind + body + struct.pack(">I", zlib.crc32(kind + body))
        )

    rows = b""
    for y in range(height):
        row = bytearray(b"\x00")
        for x in range(width):
            row += bytes(paint.get((x, y), (128, 128, 128)))
        rows += bytes(row)
    ihdr = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)
    path.write_bytes(
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", ihdr)
        + chunk(b"IDAT", zlib.compress(rows))
        + chunk(b"IEND", b"")
    )
    return path


def _block(
    x0: int, y0: int, size: int, rgb: tuple[int, int, int]
) -> dict[tuple[int, int], tuple[int, int, int]]:
    return {(x, y): rgb for x in range(x0, x0 + size) for y in range(y0, y0 + size)}


# -- the pixel comparison, by instance ----------------------------------------


def test_identical_images_pass_and_leave_no_diff(tmp_path: Path) -> None:
    ref = _png(tmp_path / "ref.png", 40, 40, {})
    got = _png(tmp_path / "got.png", 40, 40, {})
    result = pixel_diff("dark/chat", ref, got, tmp_path / "diff.png")
    assert (result.differing, result.problem) == (0, None)
    assert not (tmp_path / "diff.png").exists()


def test_one_changed_region_fails_naming_the_view_and_the_count(tmp_path: Path) -> None:
    ref = _png(tmp_path / "ref.png", 40, 40, {})
    got = _png(tmp_path / "got.png", 40, 40, _block(10, 10, 6, (255, 0, 0)))
    result = pixel_diff("light-packet", ref, got, tmp_path / "diff.png")
    assert result.differing == 36
    assert result.problem is not None
    assert result.problem.startswith("light-packet: 36 pixels differ")
    assert str(tmp_path / "diff.png") in result.problem
    assert png_check((tmp_path / "diff.png").read_bytes()) == (40, 40)  # a real image of where


def test_the_pixel_budget_is_exact(tmp_path: Path) -> None:
    ref = _png(tmp_path / "ref.png", 40, 40, {})
    exact = {(x, 0): (255, 0, 0) for x in range(MAX_DIFFERING_PIXELS)}
    over = {(x, 0): (255, 0, 0) for x in range(MAX_DIFFERING_PIXELS + 1)}
    at = pixel_diff("v", ref, _png(tmp_path / "a.png", 40, 40, exact), tmp_path / "d.png")
    assert (at.differing, at.problem) == (MAX_DIFFERING_PIXELS, None)
    past = pixel_diff("v", ref, _png(tmp_path / "b.png", 40, 40, over), tmp_path / "d.png")
    assert past.differing == MAX_DIFFERING_PIXELS + 1
    assert past.problem is not None


def test_a_colour_wobble_below_the_threshold_is_not_a_difference(tmp_path: Path) -> None:
    """The measured corner wobble: two of 255 on a few pixels, which must not flake."""
    ref = _png(tmp_path / "ref.png", 40, 40, {})
    wobble = {(x, y): (130, 128, 127) for x in range(40) for y in range(3)}
    got = _png(tmp_path / "got.png", 40, 40, wobble)
    assert pixel_diff("v", ref, got, tmp_path / "d.png").differing == 0


def test_a_picture_of_another_size_fails_naming_both_sizes(tmp_path: Path) -> None:
    ref = _png(tmp_path / "ref.png", 40, 40, {})
    got = _png(tmp_path / "got.png", 40, 52, {})
    result = pixel_diff("dark/packet", ref, got, tmp_path / "d.png")
    assert result.problem == "dark/packet: the picture is 40x52, the reference 40x40"


def test_a_comparison_that_cannot_run_raises_instead_of_reading_as_equal(tmp_path: Path) -> None:
    got = _png(tmp_path / "got.png", 4, 4, {})
    with pytest.raises(RuntimeError, match=r"snapshot_compare\.mjs failed on v"):
        pixel_diff("v", tmp_path / "missing.png", got, tmp_path / "d.png")


# -- the environment ----------------------------------------------------------


def _capture(fonts: tuple[str, ...] = ("A|a|system", "B|b|system")) -> dict[str, Any]:
    return {
        "chrome": "Chrome/154.0.8037.92",
        "platform": "linux",
        "views": {"dark/chat": {"fonts": list(fonts)}, "light/chat": {"fonts": list(fonts[:1])}},
    }


def test_the_environment_records_major_version_platform_and_the_fonts_used() -> None:
    env = environment_of(_capture())
    assert (env["chrome_major"], env["platform"]) == ("154", "linux")
    assert env["fonts"] == ["A|a|system", "B|b|system"]  # the union over views, sorted
    assert len(env["fonts_sha256"]) == 64


def test_the_same_environment_has_no_mismatch_and_a_minor_version_is_not_one() -> None:
    reference = environment_of(_capture())
    other = _capture()
    other["chrome"] = "Chrome/154.9.1.2"
    assert environment_mismatch(reference, environment_of(other)) == []


@pytest.mark.parametrize(
    ("change", "expected"),
    [
        (lambda c: c.update(chrome="Chrome/155.0.1.2"), "Chrome major version is 155"),
        (lambda c: c.update(platform="darwin"), "platform is darwin"),
        (lambda c: c.update(views=_capture(("A|a|system", "C|c|system"))["views"]), "fonts differ"),
    ],
    ids=["chrome", "platform", "fonts"],
)
def test_each_environment_difference_is_named(change: Any, expected: str) -> None:
    now = _capture()
    change(now)
    reasons = environment_mismatch(environment_of(_capture()), environment_of(now))
    assert len(reasons) == 1
    assert expected in reasons[0]


def test_the_font_mismatch_says_which_fonts_are_missing_and_which_are_new() -> None:
    now = _capture(("A|a|system", "C|c|system"))
    (reason,) = environment_mismatch(environment_of(_capture()), environment_of(now))
    assert "not here: B|b|system" in reason
    assert "only here: C|c|system" in reason


def test_another_environment_skips_naming_the_difference_and_strict_fails() -> None:
    with pytest.raises(pytest.skip.Exception) as skipped:
        stop_for_environment(["platform is darwin, references were made with linux"], strict=False)
    text = str(skipped.value)
    assert "platform is darwin" in text
    assert f"{UPDATE_ENV}=1 python -m pytest tests/test_ui_snapshots.py" in text
    with pytest.raises(pytest.fail.Exception) as failed:
        stop_for_environment(["platform is darwin"], strict=True)
    assert REQUIRE_ENV in str(failed.value)


def test_the_two_switches_read_the_environment_the_way_the_browser_guard_does() -> None:
    for name, read in ((UPDATE_ENV, update_requested), (REQUIRE_ENV, strict_requested)):
        assert read({}) is False
        assert read({name: ""}) is False
        assert read({name: "0"}) is False
        assert read({name: "1"}) is True


# -- structure, by instance ---------------------------------------------------


def _reference_elements(key: str) -> dict[str, Any]:
    got: dict[str, Any] = copy.deepcopy(read_structure(REFERENCES)["elements"][key])
    return got


def test_the_references_hold_up_against_themselves() -> None:
    for key in capture_names():
        view = key.partition("/")[2]
        elements = _reference_elements(key)
        assert structure_problems(key, elements, elements, tolerance=BOX_TOLERANCE_PX[view]) == []
        assert relation_problems(key, view, elements) == []


def test_a_missing_element_fails_naming_the_view_and_the_selector() -> None:
    now = _reference_elements("dark/chat")
    now["copy"] = None
    problems = structure_problems("dark/chat", _reference_elements("dark/chat"), now, tolerance=20)
    assert problems == ["dark/chat: copy (.assistant pre .code-copy) is not on the page"]


def test_a_changed_colour_fails_naming_the_field() -> None:
    now = _reference_elements("light/chat")
    was = now["user"]["color"]
    now["user"]["color"] = "rgb(255, 0, 255)"
    problems = structure_problems(
        "light/chat", _reference_elements("light/chat"), now, tolerance=20
    )
    assert problems == [f"light/chat: user (.user) color is 'rgb(255, 0, 255)', was {was!r}"]


@pytest.mark.parametrize("field", ["visibility", "display", "opacity", "text", "background"])
def test_every_exact_field_is_held(field: str) -> None:
    now = _reference_elements("dark/packet")
    now["merge"][field] = "changed"
    problems = structure_problems(
        "dark/packet", _reference_elements("dark/packet"), now, tolerance=None
    )
    assert len(problems) == 1
    assert f"merge (.packet .act-merge) {field} is 'changed'" in problems[0]


def test_a_box_within_the_tolerance_passes_and_one_past_it_fails() -> None:
    reference = _reference_elements("dark/chat")
    near = _reference_elements("dark/chat")
    near["paragraph"]["x"] += 20
    assert structure_problems("dark/chat", reference, near, tolerance=20) == []
    near["paragraph"]["x"] += 1
    problems = structure_problems("dark/chat", reference, near, tolerance=20)
    assert len(problems) == 1
    assert "paragraph (.assistant p) x is" in problems[0]
    assert "tolerance 20" in problems[0]


def test_no_box_tolerance_leaves_boxes_out_but_still_holds_everything_else() -> None:
    reference = _reference_elements("dark/packet")
    moved = _reference_elements("dark/packet")
    moved["download"]["x"] += 600
    assert structure_problems("dark/packet", reference, moved, tolerance=None) == []
    moved["download"]["color"] = "rgb(1, 2, 3)"
    assert len(structure_problems("dark/packet", reference, moved, tolerance=None)) == 1


def test_an_element_the_reference_does_not_know_is_reported() -> None:
    now = _reference_elements("dark/chat")
    now["stray"] = {**now["user"], "selector": ".stray"}
    problems = structure_problems("dark/chat", _reference_elements("dark/chat"), now, tolerance=20)
    assert problems == ["dark/chat: stray (.stray) is not in the reference"]


def test_a_reference_element_that_was_absent_is_not_held_against_the_page() -> None:
    """The committed references have none (see the census below); the rule is only
    that an absent reference entry cannot be compared, not that it fails."""
    reference = _reference_elements("dark/chat")
    reference["copy"] = None
    assert (
        structure_problems("dark/chat", reference, _reference_elements("dark/chat"), tolerance=20)
        == []
    )


def test_a_zero_size_element_fails_the_shown_relation() -> None:
    now = _reference_elements("dark/chat")
    now["copy"]["w"] = 0
    problems = relation_problems("dark/chat", "chat", now)
    assert "dark/chat: copy has no size (0x" in problems[0]


def test_a_hidden_element_fails_the_shown_relation() -> None:
    now = _reference_elements("light/tool-collapsed")
    now["copy"]["visibility"] = "hidden"
    assert any(
        p.startswith("light/tool-collapsed: copy is not visible (visibility hidden")
        for p in relation_problems("light/tool-collapsed", "tool-collapsed", now)
    )
    now["copy"]["visibility"] = "visible"
    now["copy"]["opacity"] = "0"
    assert any(
        "opacity 0" in p for p in relation_problems("light/tool-collapsed", "tool-collapsed", now)
    )


def test_a_missing_element_fails_every_relation_that_names_it() -> None:
    now = _reference_elements("dark/tool-expanded")
    now["output"] = None
    problems = relation_problems("dark/tool-expanded", "tool-expanded", now)
    assert problems
    assert all(p.startswith("dark/tool-expanded: output is not on the page") for p in problems)


def test_a_button_pushed_out_of_its_summary_fails_inside() -> None:
    now = _reference_elements("dark/tool-collapsed")
    now["copy"]["x"] = now["summary"]["x"] + now["summary"]["w"] + 5
    assert "dark/tool-collapsed: copy is not inside summary" in relation_problems(
        "dark/tool-collapsed", "tool-collapsed", now
    )


def test_the_output_above_its_summary_fails_below() -> None:
    now = _reference_elements("dark/tool-expanded")
    now["output"]["y"] = now["summary"]["y"] - 20
    problems = relation_problems("dark/tool-expanded", "tool-expanded", now)
    assert "dark/tool-expanded: output starts above the bottom of summary" in problems


def test_two_buttons_on_top_of_each_other_fail_apart() -> None:
    now = _reference_elements("dark/packet")
    now["merge"]["x"], now["merge"]["y"] = now["view-diff"]["x"], now["view-diff"]["y"]
    assert "dark/packet: view-diff overlaps merge" in relation_problems(
        "dark/packet", "packet", now
    )


def test_adjacent_boxes_that_touch_do_not_overlap() -> None:
    now = _reference_elements("dark/tool-collapsed")
    label, copy_ = now["label"], now["copy"]
    copy_["x"] = label["x"] + label["w"]
    copy_["y"] = label["y"]
    assert not any(
        "overlaps" in p for p in relation_problems("dark/tool-collapsed", "tool-collapsed", now)
    )


def test_every_relation_names_elements_the_reference_has() -> None:
    assert set(RELATIONS) == set(VIEWS)
    for key in capture_names():
        view = key.partition("/")[2]
        elements = _reference_elements(key)
        assert all(v is not None for v in elements.values()), (
            f"{key}: a reference element is absent"
        )
        named = {n for _kind, *names in RELATIONS[view] for n in names}
        assert named <= set(elements), f"{key}: {sorted(named - set(elements))}"


# -- references on disk and the update path -----------------------------------


def test_the_committed_references_are_complete_and_agree_with_each_other() -> None:
    structure = read_structure(REFERENCES)
    assert sorted(structure["elements"]) == sorted(capture_names())
    assert reference_pngs() == sorted(IMAGE_NAMES)
    for key in capture_names():
        name = key.replace("/", "-") + ".png"
        assert png_check((REFERENCES / name).read_bytes()) == tuple(structure["sizes"][key])
    assert set(read_environment(REFERENCES)) == {
        "chrome_major",
        "platform",
        "fonts_sha256",
        "fonts",
    }


def _synthetic(tmp_path: Path) -> tuple[dict[str, Any], Path]:
    """A capture of the reference structure and eight grey pictures of the recorded sizes."""
    structure = read_structure(REFERENCES)
    shots = tmp_path / "shots"
    shots.mkdir()
    views = {}
    for key in capture_names():
        width, height = structure["sizes"][key]
        _png(shots / (key.replace("/", "-") + ".png"), width // 20, height // 20, {})
        views[key] = {
            "width": width // 20,
            "height": height // 20,
            "fonts": ["A|a|system"],
            "elements": copy.deepcopy(structure["elements"][key]),
        }
    return {"chrome": "Chrome/154.0.1.1", "platform": "linux", "views": views}, shots


def test_the_update_path_writes_references_a_later_check_accepts(tmp_path: Path) -> None:
    capture, shots = _synthetic(tmp_path)
    target = tmp_path / "refs"
    write_references(target, capture, shots)
    assert sorted(p.name for p in target.iterdir()) == sorted(
        [*IMAGE_NAMES, "environment.json", "structure.json"]
    )
    structure, pixels, mismatch = check_capture(target, capture, shots, tmp_path / "diffs")
    assert (structure, pixels, mismatch) == ([], [], [])


def test_a_changed_capture_fails_against_what_the_update_path_wrote(tmp_path: Path) -> None:
    capture, shots = _synthetic(tmp_path)
    target = tmp_path / "refs"
    write_references(target, capture, shots)
    capture["views"]["light/chat"]["elements"]["copy"]["visibility"] = "hidden"
    _png(shots / "dark-packet.png", 20, 20, _block(0, 0, 8, (255, 0, 0)))
    structure, pixels, mismatch = check_capture(target, capture, shots, tmp_path / "diffs")
    assert any(p.startswith("light/chat: copy") for p in structure)
    assert mismatch == []
    assert len(pixels) == 1
    assert pixels[0].startswith("dark-packet:")  # a size mismatch or a pixel count, naming the view
    assert (tmp_path / "diffs").is_dir()


def test_another_environment_checks_structure_and_leaves_the_pixels_to_the_skip(
    tmp_path: Path,
) -> None:
    capture, shots = _synthetic(tmp_path)
    target = tmp_path / "refs"
    write_references(target, capture, shots)
    capture["platform"] = "darwin"
    _png(shots / "dark-packet.png", 20, 20, _block(0, 0, 8, (255, 0, 0)))  # would fail if compared
    structure, pixels, mismatch = check_capture(target, capture, shots, tmp_path / "diffs")
    assert structure == []
    assert pixels == []
    assert mismatch == ["platform is darwin, references were made with linux"]


def test_the_update_flag_is_not_set_in_a_normal_run() -> None:
    """References are rewritten on purpose, never as a side effect of the environment."""
    assert not update_requested(os.environ), (
        f"{UPDATE_ENV} is set: this run rewrote tests/fixtures/ui_snapshots. Review and commit "
        "the new references, then run again without it"
    )


# -- the real browser ---------------------------------------------------------


@pytest.fixture(scope="module")
def served(tmp_path_factory: pytest.TempPathFactory) -> Iterator[tuple[str, str]]:
    """A server on a seeded session: the chat turns, a tool row, a finished run's packet."""
    tmp = tmp_path_factory.mktemp("ui")
    repo = make_repo(tmp / "repo")
    store = SessionStore(tmp / "s")
    sid = store.create(title="Snapshots", workdir=str(repo)).id
    call = {
        "id": "a",
        "type": "function",
        "function": {"name": "run_command", "arguments": json.dumps({"command": "make test"})},
    }
    store.save_messages(
        sid,
        [
            {"role": "user", "content": "show me greet and run the tests"},
            {"role": "assistant", "content": MARKDOWN},
            {"role": "assistant", "content": "", "tool_calls": [call]},
            {"role": "tool", "tool_call_id": "a", "content": "exit 0\n2 passed in 0.01s\n"},
            {"role": "assistant", "content": "All green."},
        ],
    )
    seed(store, repo, "audited", sid=sid)
    app = build_app(store, NoModel, default_workdir=repo)
    with serving(app) as base:
        yield base, sid


def render(served: tuple[str, str], out: Path, css: str = "") -> dict[str, Any]:
    """Run the driver once: its pictures land in `out`, its capture is returned."""
    out.mkdir(parents=True, exist_ok=True)
    base, sid = served
    run = subprocess.run(
        ["node", str(CDP), base, sid, str(out), css],
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )
    assert run.returncode == 0, run.stderr
    got: dict[str, Any] = json.loads(run.stdout.strip().splitlines()[-1])
    return got


@pytest.fixture(scope="module")
def baseline(
    served: tuple[str, str], tmp_path_factory: pytest.TempPathFactory
) -> tuple[dict[str, Any], Path]:
    shots = tmp_path_factory.mktemp("shots")
    capture = render(served, shots)
    if update_requested():
        write_references(REFERENCES, capture, shots)
    return capture, shots


def _checked(
    capture: dict[str, Any], shots: Path, tmp_path: Path
) -> tuple[list[str], list[str], list[str]]:
    return check_capture(REFERENCES, capture, shots, tmp_path / "diffs")


def test_every_view_renders_in_both_themes_with_every_element_found(
    baseline: tuple[dict[str, Any], Path],
) -> None:
    capture, _shots = baseline
    assert sorted(capture["views"]) == sorted(capture_names())
    assert THEMES == ("dark", "light")
    for key, view in capture["views"].items():
        assert [n for n, e in view["elements"].items() if e is None] == [], key


def test_the_structure_of_every_view_matches_its_reference(
    baseline: tuple[dict[str, Any], Path], tmp_path: Path
) -> None:
    capture, shots = baseline
    structure, _pixels, _mismatch = _checked(capture, shots, tmp_path)
    assert structure == []


def test_the_pixels_of_every_view_match_the_references_where_the_environment_does(
    baseline: tuple[dict[str, Any], Path], tmp_path: Path
) -> None:
    capture, shots = baseline
    _structure, pixels, mismatch = _checked(capture, shots, tmp_path)
    if mismatch:
        stop_for_environment(mismatch, strict=strict_requested())
    assert pixels == []


MUTANTS = {
    # The copy button vanishes from the fenced block: both themes, named.
    "hidden-copy": (".assistant pre .code-copy { visibility: hidden }", "chat: copy"),
    # The light theme's text colour changes: the light views fail and the dark ones do not.
    "light-ink": (':root[data-theme="light"] { --ink: #ff00ff }', "light/chat: user"),
}


@pytest.mark.parametrize("name", sorted(MUTANTS))
def test_an_injected_defect_fails_the_structure_check_naming_the_selector(
    name: str, served: tuple[str, str], baseline: tuple[dict[str, Any], Path], tmp_path: Path
) -> None:
    css, expected = MUTANTS[name]
    capture = render(served, tmp_path / "shots", css)
    structure, _pixels, _mismatch = check_capture(
        REFERENCES, capture, tmp_path / "shots", tmp_path / "diffs"
    )
    assert any(expected in problem for problem in structure), structure
    if name == "light-ink":
        assert [p for p in structure if p.startswith("dark/")] == []
    # The same capture without the rule is clean, so the failure is the rule's.
    assert _checked(baseline[0], baseline[1], tmp_path)[0] == []


def test_a_change_only_pixels_show_fails_the_pixel_check_and_not_the_structure_check(
    served: tuple[str, str], baseline: tuple[dict[str, Any], Path], tmp_path: Path
) -> None:
    css = ".packet { box-shadow: inset 0 0 0 3px #f00 }"
    capture = render(served, tmp_path / "shots", css)
    structure, pixels, mismatch = check_capture(
        REFERENCES, capture, tmp_path / "shots", tmp_path / "diffs"
    )
    assert structure == []  # a box shadow moves no box and recolours no text
    if mismatch:
        stop_for_environment(mismatch, strict=strict_requested())
    assert len(pixels) == 2
    assert pixels[0].startswith("dark-packet:")
    assert pixels[1].startswith("light-packet:")
    assert (tmp_path / "diffs" / "dark-packet.png").exists()
    assert not (tmp_path / "diffs" / "dark-chat.png").exists()  # only the views it touched
