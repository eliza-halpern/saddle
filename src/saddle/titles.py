"""A session's name, summarised from the message that started it.

"New session" three times over is a sidebar you cannot navigate, so the
first turn names its own session. The model writes the title, but it is
never trusted to write it *well*: models answer this prompt with preamble
("Title:"), with quotes around it, with a trailing full stop, and sometimes
with a paragraph. `clean_title` is what makes the result fit in a sidebar,
and it is the part with tests.

Two rules follow from the title being cosmetic:

- it may never fail the turn. Any failure falls back to the user's own
  words, which is a worse title but never a wrong one;
- it may never overwrite a name the user chose. `Session.auto_title` goes
  false the moment someone renames, and stays false.
"""

from __future__ import annotations

import re
from typing import Any, Final

MAX_TITLE: Final = 48
"""Enough to be a sentence fragment, short enough for a 252px sidebar."""

TITLE_TOKENS: Final = 64
"""Generous for a six-word title. It costs nothing to be generous: measured
against the live model at effort "none", latency is the same at 24 tokens as
at 200 (153-224ms either way), and `complete` treats a truncated answer as an
error -- so a tight cap buys no speed and risks throwing away a good title."""

TITLE_EFFORT: Final = "none"
"""Measured, not assumed. Titling three sample requests took 153-224ms at
"none" against 1179-1299ms at "low" -- 6x -- for titles of equal quality
("SSE Stream Splitting Between Tabs" vs "SSE stream split between tabs").
Naming a session is not a task that benefits from deliberation."""

_PREAMBLE: Final = re.compile(
    # Each branch carries its own separator. A bare "title" may not be
    # stripped without one, or "Title bar rendering bug" loses its first word.
    r"^\s*(?:"
    r"here(?:'s| is)\b[^:\n]{0,40}:"  # "Here's a title:"
    r"|(?:session\s+)?title\s*[:\-]"  # "Title:", "Session title -"
    r"|summary\s*[:\-]"
    r")\s*",
    re.IGNORECASE,
)

PROMPT: Final = """\
Write a short title for a chat session that opens with the request below.

Rules:
- 2 to 6 words
- no quotation marks, no trailing period
- no preamble: reply with the title and nothing else

Request:
{request}
"""


def clean_title(raw: str) -> str:
    """Reduce a model's answer to something that fits in the sidebar."""
    text = (raw or "").strip()
    # A model that thought out loud puts the answer last; one that answered
    # directly has only one line. Either way the first non-empty line after
    # any preamble is the title.
    for line in text.splitlines():
        candidate = _PREAMBLE.sub("", line).strip()
        candidate = candidate.strip("\"'`*#").strip()
        candidate = re.sub(r"\s+", " ", candidate).rstrip(".!,;:").strip()
        if candidate:
            return _clip(candidate)
    return ""


def _clip(text: str) -> str:
    if len(text) <= MAX_TITLE:
        return text
    cut = text[:MAX_TITLE].rsplit(" ", 1)[0]
    return (cut or text[:MAX_TITLE]).rstrip(".,;:") + "…"


def fallback_title(request: str) -> str:
    """The user's own words, when the model cannot be asked or is no help."""
    for line in (request or "").splitlines():
        stripped = re.sub(r"\s+", " ", line).strip()
        if stripped:
            return _clip(stripped)
    return ""


def title_for(client: Any, request: str) -> str:
    """Ask the model to name this session; never raise, never return "".

    Returns "" only when the request itself carries no words, in which case
    the caller leaves the existing name alone.
    """
    fallback = fallback_title(request)
    if not fallback:
        return ""
    try:
        answer = client.complete(
            PROMPT.format(request=request[:600]),
            max_tokens=TITLE_TOKENS,
            temperature=0.0,
            reasoning_effort=TITLE_EFFORT,
        )
    except Exception:  # a cosmetic call may not take the turn down with it
        return fallback
    return clean_title(answer) or fallback
