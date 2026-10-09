"""The strip of what a session runs with, read back from a real Chrome page.

Always visible under the topbar: reasoning on or off, the effort, whether past
reasoning is sent back, and each capability. A capability off that would work
and one switched on that cannot work stand out in colour *and* in words, and
each chip is named for a screen reader. A change during the session marks its
chip "before → after" and adds a line to the transcript there, which a reload
draws again.

Known-bad, each a different value in the compared lists: a strip that is not
shown, an off-but-available capability painted as a plain "off", an unavailable
one with no error, an effort change that marks nothing or adds no transcript
line, a line that a reload loses, a capability switched on that the page never
notices.
"""

from __future__ import annotations

import threading
import time
from pathlib import Path
from typing import Any

import pytest
from browser_guard import BROWSER
from chrome_page import drive_page, served_chat

from saddle import capabilities, conditions

needs_chrome = pytest.mark.skipif(not BROWSER, reason="needs node and google-chrome")


def probe() -> list[conditions.Row]:
    """Rows that follow the switches: images and ocr as switched, browser off but
    available, ocr unavailable when on, everything else off and unavailable."""
    on = capabilities.load()
    rows: list[conditions.Row] = []
    for name in capabilities.NAMES:
        if name == "ocr" and on.ocr:
            rows.append(
                {"name": name, "state": "unavailable", "reason": "not installed: tesseract"}
            )
        elif on.get(name):
            rows.append({"name": name, "state": "on", "reason": "", "available": None})
        else:
            available = name in ("browser", "ocr")
            rows.append({"name": name, "state": "off", "reason": "", "available": available})
    return rows


READ_STRIP = """() => {
  const strip = document.querySelector("#conditions");
  const box = strip.getBoundingClientRect();
  return {
    shown: box.height > 0 && getComputedStyle(strip).display !== "none",
    label: strip.getAttribute("aria-label"),
    chips: [...strip.children].map(
      (li) => [li.textContent, li.className, li.getAttribute("aria-label")]),
    colors: [...strip.children].map((li) => getComputedStyle(li).backgroundColor),
    lines: [...document.querySelectorAll("#transcript .conditions-change")]
      .map((l) => l.textContent),
  };
}"""


@needs_chrome
def test_the_strip_shows_every_condition_and_marks_the_two_that_need_the_person(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(capabilities.OVERRIDE_ENV, "images,ocr")
    with served_chat(tmp_path, capability_probe=probe) as site:
        site.store.update(site.sid, reasoning_effort="xhigh")
        got = drive_page(
            site.base,
            f"""
            await page.chat(args.sid);
            await page.until(() => document.querySelectorAll("#conditions li").length > 3);
            return await page.js({READ_STRIP});
            """,
            sid=site.sid,
        )
    assert got["shown"], got
    assert got["label"] == "What this session runs with"
    chips: list[list[Any]] = got["chips"]
    assert chips[:4] == [
        ["reasoning on", "cond", "reasoning: on"],
        ["effort xhigh", "cond", "effort: xhigh"],
        ["keep reasoning on", "cond", "keep reasoning: on"],
        ["mcp off", "cond", "mcp: off"],
    ]
    by_name = {
        chip[0].split(" ")[1] if chip[0][0] in "⚠✕" else chip[0].split(" ")[0]: chip
        for chip in chips[3:]
    }
    assert by_name["browser"] == [
        "⚠ browser off (available)",
        "cond cond-warn",
        "browser: off, but available",
    ]
    assert by_name["ocr"] == [
        "✕ ocr unavailable",
        "cond cond-error",
        "ocr: switched on but unavailable: not installed: tesseract",
    ]
    assert by_name["images"] == ["images on", "cond", "images: on"]
    assert len(chips) == 3 + len(capabilities.NAMES)
    # The error chip is filled; a plain one is not.
    error_at = chips.index(by_name["ocr"])
    assert got["colors"][error_at] != got["colors"][0]
    assert got["lines"] == []


@needs_chrome
def test_an_effort_change_is_marked_and_lined_and_a_reload_keeps_the_line(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    with served_chat(tmp_path, capability_probe=probe) as site:
        site.store.update(site.sid, reasoning_effort="medium")
        got = drive_page(
            site.base,
            f"""
            await page.chat(args.sid);
            await page.until(() => document.querySelectorAll("#conditions li").length > 3);
            await page.js(() => {{
              const pick = document.querySelector("#effort");
              pick.value = "xhigh";
              pick.dispatchEvent(new Event("change"));
            }});
            await page.until(() => document.querySelector("#transcript .conditions-change"));
            const live = await page.js({READ_STRIP});
            await page.chat(args.sid);
            await page.until(() => document.querySelector("#transcript .conditions-change"));
            const reloaded = await page.js({READ_STRIP});
            return {{ live, reloaded }};
            """,
            sid=site.sid,
        )
    for seen in (got["live"], got["reloaded"]):
        assert seen["chips"][1] == [
            "effort medium → xhigh",
            "cond cond-changed",
            "effort: xhigh, changed from medium",
        ], seen
        assert len(seen["lines"]) == 1, seen
        assert seen["lines"][0].endswith("conditions changed: effort medium → xhigh"), seen


def _switched_on_mid_session(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, looks: conditions.Probe
) -> dict[str, Any]:
    """The page's strip before and after the images switch is turned on from outside it."""
    with served_chat(tmp_path, capability_probe=looks) as site:
        # Once the page has shown the start, the switch is turned on. The start is what
        # the page's first look recorded (`check_conditions`), so the switch waits for
        # that record, not for a clock that a loaded machine's page can be slower than.
        def flip_after_the_start() -> None:
            deadline = time.monotonic() + 30.0
            while not site.store.conditions(site.sid) and time.monotonic() < deadline:
                time.sleep(0.05)
            monkeypatch.setenv(capabilities.OVERRIDE_ENV, "images")

        flip = threading.Thread(target=flip_after_the_start, daemon=True)
        flip.start()
        try:
            got: dict[str, Any] = drive_page(
                site.base,
                f"""
                await page.chat(args.sid);
                await page.until(() => document.querySelectorAll("#conditions li").length > 3);
                const before = await page.js({READ_STRIP});
                // A tab coming back into view looks again at once, as the timer does.
                await page.until(() => {{
                  document.dispatchEvent(new Event("visibilitychange"));
                  return document.querySelector("#transcript .conditions-change");
                }});
                return {{ before, after: await page.js({READ_STRIP}) }};
                """,
                sid=site.sid,
            )
        finally:
            flip.join(timeout=31.0)
    return got


@needs_chrome
def test_a_capability_switched_on_after_a_slow_first_look_is_still_noticed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The test below flaked in a full gate on 2026-10-08, timing out on the change
    line. Its switch was turned on by a 2 s timer, which can fire before a loaded
    machine's page has even looked. The page's first look then already saw images
    on, recorded that as the start, and no change ever came. A first look made slow
    here opens that window every time."""
    looked: list[bool] = []

    def slow_first_look() -> list[conditions.Row]:
        if not looked:
            time.sleep(2.5)
        looked.append(True)
        return probe()

    _noticed(_switched_on_mid_session(tmp_path, monkeypatch, slow_first_look))


@needs_chrome
def test_a_capability_switched_on_mid_session_is_noticed_and_lined(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Switched on from outside the page (`saddle capabilities enable`, say) while
    it is open: the page's next look marks the chip and lines the transcript."""
    _noticed(_switched_on_mid_session(tmp_path, monkeypatch, probe))


def _noticed(got: dict[str, Any]) -> None:
    images = 3 + capabilities.NAMES.index("images")
    assert got["before"]["chips"][images][0] == "images off"
    assert got["before"]["lines"] == []
    assert got["after"]["chips"][images] == [
        "images off → on",
        "cond cond-changed",
        "images: on, changed from off",
    ]
    assert got["after"]["lines"][0].endswith("conditions changed: images off → on")
