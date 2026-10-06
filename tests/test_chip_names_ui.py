"""The status pill and the cite chips carry an accessible name (#86).

Two parts of the page say what they are only in shape or in hex:

* The status pill's face is a kaomoji the stylesheet draws in front of the word
  (`.status.idle::before` and its siblings), which a screen reader reads as
  punctuation. Its accessible name is the plain state -- `idle`, `working`,
  `needs`, `error` -- however the pill is dressed, including the state where a
  detail (`setStatus("working", "stopping…")`) covers the word on screen.
* A cite chip's visible text is eight hex digits of a record hash, which names
  nothing a listener can act on. Its accessible name says what the chip does and
  keeps the hash it shows inside that name (WCAG 2.5.3, Label in Name).

Both read back from a real Chrome page: the pill through the states `setStatus`
paints, the chips through the two painters that build them -- `citeButton`, the
packet row's cite, and `sessionCite`, the session-log line's.

Known-bad, each a different value in the compared dictionary: a pill named by
its drawn face or by its detail instead of its state; a chip with no name at all
(the attribute absent leaves an unnamed button to a screen reader); a name that
dropped the eight digits the chip shows; two kinds of chip named by different
rules; and a name that replaced the face the page draws instead of naming the
state beside it. Every behaviour is compared, so one passing run checked all
four states and both kinds of chip.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest
from browser_guard import BROWSER
from chrome_page import drive_page, served_chat

needs_chrome = pytest.mark.skipif(not BROWSER, reason="needs node and google-chrome")

CSS = Path(__file__).resolve().parents[1] / "src/saddle/web/static/app.css"


def _face(shown: str) -> str:
    """A kaomoji face with the quoting and line wrapping of its source removed."""
    return " ".join(shown.strip().strip('"').split())


# The faces the stylesheet draws, read from the sheet itself: the pill's name may
# not repeat them, and they are still painted.
FACES = {
    kind: _face(face)
    for kind, face in re.findall(
        r'\.status\.(\w+)::before \{ content: "([^"]+)"', CSS.read_text(), re.S
    )
}
FACES_SEEN = {kind: face for kind, face in FACES.items() if not face.isascii()}
FACE_MARKS = {mark for face in FACES_SEEN.values() for mark in face if not mark.isascii()}

# A record's 40-hex hash, and the eight digits its chip shows.
PACKET_HASH = "1a2b3c4d5e6f7a8b9c0d1e2f3a4b5c6d7e8f9a0b"
LINE_HASH = "ff00ff00ff00ff00ff00ff00ff00ff00ff00ff00ff00"
SHOWN = {"packet": PACKET_HASH[:8], "line": LINE_HASH[:8]}

# state the page was read in -> (the name it carries, its class, the words it shows)
EXPECTED_PILL = {
    "idle": ("idle", "status idle", "idle"),
    "working": ("working", "status working", "stopping…"),
    "needs": ("needs", "status needs", "needs"),
    "error": ("error", "status error", "error"),
}
EXPECTED_CHIPS = {
    "packet": (f"Open the sealed ledger record {PACKET_HASH[:8]}", PACKET_HASH[:8], "cite"),
    "line": (f"Open the sealed ledger record {LINE_HASH[:8]}", LINE_HASH[:8], "cite tl-cite"),
}


@needs_chrome
def test_the_status_pill_is_named_by_its_state_in_every_state(tmp_path: Path) -> None:
    with served_chat(tmp_path) as site:
        got = drive_page(
            site.base,
            """
            await page.chat(args.sid);
            const read = () => {
              const pill = document.querySelector("#status");
              return {
                label: pill.getAttribute("aria-label") || "",
                text: pill.textContent,
                className: pill.className,
                face: getComputedStyle(pill, "::before").content,
              };
            };
            const idle = await page.js(read);
            await page.js(() => setStatus("working", "stopping…"));
            const working = await page.js(read);
            await page.js(() => setStatus("needs"));
            const needs = await page.js(read);
            await page.js(() => setStatus("error"));
            const error = await page.js(read);
            return { idle, working, needs, error };
            """,
            sid=site.sid,
        )
    seen = {state: (pill["label"], pill["className"], pill["text"]) for state, pill in got.items()}
    assert FACES_SEEN, "the stylesheet drew no kaomoji face, so this test checked nothing"
    assert seen == EXPECTED_PILL, seen
    # The name is the state, never a mark of the face the stylesheet draws.
    assert all(mark not in label for label, _, _ in seen.values() for mark in FACE_MARKS), seen
    # The face is still painted: naming the state did not replace what is shown.
    painted = {state: _face(got[state]["face"]) for state in FACES_SEEN}
    assert painted == FACES_SEEN, painted


@needs_chrome
def test_a_cite_chip_names_the_record_it_opens_and_keeps_the_hash_it_shows(
    tmp_path: Path,
) -> None:
    with served_chat(tmp_path) as site:
        got = drive_page(
            site.base,
            f"""
            await page.chat(args.sid);
            return await page.js(
              (packetHash, lineHash) => {{
                const host = document.createElement("div");
                document.querySelector("#transcript").appendChild(host);
                const packet = citeButton(packetHash, {{ hash: packetHash, note: "sealed" }}, host);
                host.appendChild(packet);
                const line = sessionCite({{ lines: host }}, lineHash);
                host.appendChild(line);
                const read = (chip) => ({{
                  label: chip.getAttribute("aria-label") || "",
                  text: chip.textContent,
                  className: chip.className,
                }});
                return {{ packet: read(packet), line: read(line) }};
              }},
              {json.dumps(PACKET_HASH)},
              {json.dumps(LINE_HASH)},
            );
            """,
            sid=site.sid,
        )
    seen = {kind: (chip["label"], chip["text"], chip["className"]) for kind, chip in got.items()}
    assert seen == EXPECTED_CHIPS, seen
    names = {kind: label for kind, (label, _, _) in seen.items()}
    # Label in Name: each name ends with the eight digits its chip shows.
    assert names["packet"].endswith(SHOWN["packet"]), names
    assert names["line"].endswith(SHOWN["line"]), names
    # Both kinds of chip are named by one rule; only the hash inside it differs.
    rules = {name[: -len(SHOWN[kind])] for kind, name in names.items()}
    assert rules == {"Open the sealed ledger record "}, names
