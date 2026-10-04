"""With research on, the model is told to try a quick first instinct once, and to
research instead of guessing again when it fails.

Watched setup runs (2026-10-02) without research spent minutes reconstructing an
old engine's config rules from memory and guessed wrong, again after each
failure. The contract: while the `research` tool is offered, its description
leads with `RESEARCH_FIRST`; with research off, no tool the model is offered
says it.

Known-good: Edit and Ask with research on carry the sentence, first in the
research tool's description. Known-bad: research off carries it nowhere.
"""

from __future__ import annotations

import json

from saddle.research import RESEARCH_FIRST, RESEARCH_TOOL
from saddle.tools import tools_for_mode


def _descriptions(tools: list[dict[str, object]]) -> dict[str, str]:
    return {
        str(t["function"]["name"]): str(t["function"]["description"])  # type: ignore[index]
        for t in tools
    }


def test_research_on_leads_the_research_tool_with_research_first() -> None:
    for mode in ("edit", "ask"):
        found = _descriptions(tools_for_mode(mode, research=True))
        assert found[RESEARCH_TOOL].startswith(RESEARCH_FIRST), mode
        assert "do not keep guessing from memory" in found[RESEARCH_TOOL]


def test_research_off_says_it_nowhere() -> None:
    for mode in ("edit", "ask"):
        text = json.dumps(tools_for_mode(mode, processes=True, research=False))
        assert RESEARCH_TOOL not in _descriptions(tools_for_mode(mode, research=False))
        assert "Try, then research" not in text, mode
