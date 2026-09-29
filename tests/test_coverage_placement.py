"""The packet places every uncovered line of a file the audited tree holds.

The coverage finding's sidecar seals the text of each file it names, so the
packet can put each line in its function (`coverage_text`). The sidecar
writer redacted that text like any other string, and redaction rewrote code:
in a dogfood run on saddle itself, `CHARS_PER_TOKEN: Final = 4` was sealed as
`CHARS_PER_TOKEN=*** = 4` and `VllmClient(api_key=key, ...)` as
`VllmClient(api_key=***, ...)`. Neither file then parsed, and the packet said
their 7 lines were "not placed (file not in the tree read)" for two files the
tree held.
"""

from __future__ import annotations

import json
from pathlib import Path

from test_auditor import clean_tree

from saddle.auditor import Auditor, AuditorConfig
from saddle.journal import attempt_sidecar_path, read_spans, verify_journal
from saddle.packet import compile_packet

__all__ = ["clean_tree"]

LIMITS = (
    "from typing import Final\n"
    "\n"
    "CHARS_PER_TOKEN: Final = 4\n"
    "\n"
    "\n"
    "def client(key):\n"
    "    return dict(api_key=key, per=CHARS_PER_TOKEN)\n"
)
"""A new module no test runs, holding the two shapes redaction broke."""


def test_a_file_redaction_would_break_is_placed_in_its_functions(
    clean_tree: Path, tmp_path: Path
) -> None:
    (clean_tree / "limits.py").write_text(LIMITS)
    journal = tmp_path / "proofs.jsonl"
    result = Auditor(clean_tree, config=AuditorConfig(journal=journal)).tier1()
    coverage = next(f for f in result.findings if f.gate == "coverage")
    assert coverage.verdict == "fail"
    assert "limits.py:3" in coverage.detail
    assert verify_journal(journal) == []
    audit = next(r for r in compile_packet(journal).rows if r.key == "audit")
    assert "not placed" not in audit.summary
    assert "not in the tree read" not in audit.recap
    assert "  - limits.py client: 2 of 2 changed lines never run -- nothing exercises client" in (
        audit.summary
    )
    assert "limits.py module level:" in audit.summary
    # What a person reads stays redacted, as before.
    assert "      3: CHARS_PER_TOKEN=*** = 4\n" in audit.summary
    assert "      7:     return dict(api_key=***, per=CHARS_PER_TOKEN)\n" in audit.summary


def test_the_sidecar_holds_the_audited_source_verbatim(clean_tree: Path, tmp_path: Path) -> None:
    """The cost, on the record: a secret-shaped line of an audited file is
    sealed as it stands in the tree, beside the ledger, as the diff already is."""
    (clean_tree / "limits.py").write_text(LIMITS)
    journal = tmp_path / "proofs.jsonl"
    Auditor(clean_tree, config=AuditorConfig(journal=journal)).tier1()
    span = next(s for s in read_spans(journal) if s.name == "audit-tier1:coverage")
    sealed = json.loads(attempt_sidecar_path(journal, span.span_id).read_text())
    assert sealed["sources"]["limits.py"] == LIMITS.splitlines()
    assert "    return dict(api_key=key, per=CHARS_PER_TOKEN)" in sealed["sources"]["limits.py"]


def test_a_source_over_the_sidecar_cap_is_now_sealed_whole_and_placed(tmp_path: Path) -> None:
    """The other half of the flip in test_packet.py: a one-string source over
    4000 characters was cut by the writer and not placed; it is sealed whole."""
    from test_packet import coverage_evidence, coverage_finding, coverage_sources, coverage_span

    journal = tmp_path / "proofs.jsonl"
    sealed = coverage_evidence(tmp_path)
    assert len(coverage_sources()["money.py"]) > 4000
    sealed["sources"]["money.py"] = coverage_sources()["money.py"]
    coverage_span(journal, coverage_finding(), sealed)
    audit = next(r for r in compile_packet(journal).rows if r.key == "audit")
    assert "not placed" not in audit.summary
    assert "money.py convert" in audit.summary
