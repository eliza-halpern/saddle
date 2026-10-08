"""The web packet says the auditor's codes in plain words (#85 item 5).

The packet page is read by a person. It showed the auditor's own vocabulary: reason codes
("coverage (evidence-thin)"), anchor statuses ("anchor-missing"), hyphenated gate ids,
tier numbers ("tier-2 audit", "the tier-0 guard") and a findings ratio ("0 of 1 finding
passed."). `Packet.payload()`, which only the page reads, now spells them out through
`packet.plain_words`; the terminal packet, packet.md and the chat recap keep the codes.

Known-bad: a code, a tier number or the ratio in what the page is sent, or a code rewritten
inside a path. Known-good: every row still there with its text, the audit row still says
what passed and failed, the reproduce row still speaks of the anchor, a stopped run's
verdict still names coverage, and the terminal packet unchanged.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import pytest
from packet_seed import make_repo, seed

from saddle import auditor, packet
from saddle.auto import ledger_path
from saddle.packet import compile_packet, plain_words, render_packet_text
from saddle.sessions import SessionStore

# What a person should never read on the page: the oracle's list (reason codes, anchor
# statuses, hyphenated gate ids, "tier-N", the findings ratio).
CODES = sorted({*packet.PLAIN_CODES, *packet.PLAIN_GATES})
BANNED = [re.compile(rf"(?<![\w-]){re.escape(c)}(?![\w-])") for c in CODES] + [
    re.compile(r"\btier-?\d\b", re.I),
    re.compile(r"\b\d+ of \d+ findings? passed\b", re.I),
]


@pytest.mark.parametrize(
    ("raw", "plain"),
    [
        (
            "finish refused on the same findings: coverage (evidence-thin)",
            "finish refused on the same findings: coverage (the evidence is thin)",
        ),
        ("0 of 1 finding passed.", "Findings: 1 failed."),
        ("1 of 1 finding passed.", "Findings: 1 passed."),
        ("12 of 16 findings passed, 4 not proven.", "Findings: 12 passed, 4 not proven."),
        ("1 of 3 findings passed, 1 need you.", "Findings: 1 passed, 1 failed, 1 need you."),
        ("0 of 0 findings passed.", "No findings."),
        (
            "✓ dead-code: tier 1, pass: every private definition added is mentioned",
            "✓ dead code passed: every private definition added is mentioned",
        ),
        ("? project-gate: tier 1, not-proven: Gate: base", "? project gate not proven: Gate: base"),
        ("coverage (tier 2): body", "coverage: body"),
        ("The tier-0 guard refused nothing.", "The edit guard refused nothing."),
        (
            "Changed-line mutation is a tier-2 audit, and no auditor has written one",
            "Changed-line mutation is part of the full audit, and no auditor has written one",
        ),
        (
            "The branch anchor does not match: anchor-missing. Covers:",
            "The branch anchor does not match: the branch carries no outcome anchor. Covers:",
        ),
    ],
)
def test_each_code_reads_in_words(raw: str, plain: str) -> None:
    assert plain_words(raw) == plain


def test_a_code_inside_a_path_or_a_longer_name_is_left_alone() -> None:
    text = (
        "src/dead-code.py, docs/red-phase.md, tests/test_dead_code.py, my-red-phase-x, "
        "the folder tests/red-phase here, a dotfile .dead-code"
    )
    assert plain_words(text) == text


def test_the_table_names_every_hyphenated_gate_the_auditor_writes() -> None:
    """packet spells the gate names itself (it may not import the auditor): this holds the
    table to the auditor's, so a new hyphenated gate cannot reach the page as a code."""
    gates = {*auditor.REASONS} | {
        auditor.NOT_MEASURABLE_GATE,
        auditor.JS_TESTS_GATE,
        auditor.JS_COVERAGE_GATE,
        auditor.JS_RED_PHASE_GATE,
        auditor.SKIPPED_GATE,
        auditor.PROMPT_EFFECT_GATE,
    }
    hyphenated = {g for g in gates if "-" in g}
    assert hyphenated <= set(packet.PLAIN_GATES), hyphenated - set(packet.PLAIN_GATES)


def _payload_texts(payload: dict[str, Any]) -> list[str]:
    rows = payload["rows"]
    return [
        payload["verdict_text"],
        *payload["header"],
        *(r["text"] for r in rows),
        *(i for r in rows for i in r["items"]),
        *(r.get("summary", "") for r in rows),
    ]


@pytest.mark.parametrize("kind", ["stopped", "budget", "audited"])
def test_what_the_page_is_sent_has_no_code_and_keeps_its_meaning(tmp_path: Path, kind: str) -> None:
    repo = make_repo(tmp_path / "repo")
    _sid, rid, _branch = seed(SessionStore(tmp_path / "s"), repo, kind)  # type: ignore[arg-type]
    # As the page's endpoint compiles it (app.task_packet): with its anchor repo.
    compiled = compile_packet(ledger_path(repo, rid), run_id=rid, anchor_repo=repo)
    payload = compiled.payload()
    found = [(rx.pattern, t) for t in _payload_texts(payload) for rx in BANNED if rx.search(t)]
    assert not found, found
    rows = {r["key"]: r["text"] for r in payload["rows"]}
    assert rows.keys() == {row.key for row in compiled.rows}  # no row dropped
    assert all(t.strip() for t in rows.values()), rows
    if "audit" in rows:
        assert re.search(r"passed|failed", rows["audit"]), rows["audit"]
    assert "anchor" in rows["reproduce"], rows["reproduce"]
    if kind == "stopped":
        assert "coverage" in payload["verdict_text"].lower(), payload["verdict_text"]
        # The terminal packet keeps the auditor's codes: only the page is translated.
        assert "evidence-thin" in render_packet_text(compiled)
