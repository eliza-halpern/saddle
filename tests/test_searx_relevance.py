"""SearXNG rows that share no meaningful word with the query never reach the reader.

Contract: before SearXNG results reach the reader, a row is dropped unless it
shares enough meaningful words with the query (`searx.on_topic`): a query word
of three or more letters, outside a short stopword list, counts as shared when
the row's title, snippet or address (split on everything but letters) holds the
same word, or, for a query word of four or more letters, a word that starts with
it. One shared word is enough for a query of up to three meaningful words; a
query of four or more needs two. A dropped row's address is never one the reader
may open. When every row is dropped, the reader is told the search found nothing
relevant and to search again with fewer, plainer keywords, never an empty list
and never the junk. Brave's rows are not filtered.

The rows below are the off-topic ones a live SearXNG returned for this query.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import httpx

from saddle.brave import BraveSearch, Outcome
from saddle.mcpclient import Approvals
from saddle.research import ReaderGate, Researcher
from saddle.searx import meaningful_words, on_topic

QUERY = "Harry Potter Philosopher's Stone pcgamingwiki Wine"

PRINCE = {
    "title": "Prince Harry, Duke of Sussex - Wikipedia",
    "url": "https://en.wikipedia.org/wiki/Prince_Harry,_Duke_of_Sussex",
    "content": "Prince Harry, Duke of Sussex, is a member of the British royal family.",
}
WHATSAPP = {
    "title": "WhatsApp Web",
    "url": "https://web.whatsapp.com/",
    "content": "Log in to WhatsApp Web for simple, reliable and private messaging on your desktop.",
}
FDA = {
    "title": "FDA approves elacestrant for ER-positive, HER2-negative, ESR1-mutated advanced "
    "or metastatic breast cancer",
    "url": "https://www.fda.gov/drugs/resources-information-approved-drugs/"
    "fda-approves-elacestrant-er-positive-her2-negative-esr1-mutated-advanced-or-metastatic",
    "content": "On January 27, 2023, the Food and Drug Administration approved elacestrant.",
}
PCGW = {
    "title": "Harry Potter and the Philosopher's Stone - PCGamingWiki PCGW - bugs, fixes, "
    "crashes, mods, guides and improvements for every PC game",
    "url": "https://www.pcgamingwiki.com/wiki/Harry_Potter_and_the_Philosopher%27s_Stone",
    "content": "Harry Potter and the Philosopher's Stone is a 2001 action-adventure game.",
}
WINEHQ = {
    "title": "WineHQ - Harry Potter and the Sorcerer's Stone",
    "url": "https://appdb.winehq.org/objectManager.php?sClass=application&iId=1219",
    "content": "Gold rating: runs with no tweaks.",
}


def test_the_query_words_are_the_lowercase_words_of_three_letters_outside_the_stopwords() -> None:
    assert meaningful_words(QUERY) == {
        "harry",
        "potter",
        "philosopher",
        "stone",
        "pcgamingwiki",
        "wine",
    }
    assert meaningful_words("How to install the Wine on a PC?") == {"install", "wine"}
    assert meaningful_words("https://www.example.com/Wine_Path-x9y") == {"example", "wine", "path"}


def test_the_live_junk_is_dropped_and_the_on_topic_rows_are_kept() -> None:
    kept, dropped = on_topic(QUERY, [PRINCE, PCGW, WHATSAPP, FDA, WINEHQ])
    assert kept == [PCGW, WINEHQ]  # WineHQ by the prefix rule: winehq starts with wine
    assert dropped == [PRINCE, WHATSAPP, FDA]  # Prince Harry shares one word: harry


def test_a_short_query_needs_one_shared_word_and_four_words_need_two() -> None:
    three = "harry potter wine"
    four = "harry potter wine stone"
    assert on_topic(three, [PRINCE]) == ([PRINCE], [])  # harry alone suffices
    assert on_topic(four, [PRINCE]) == ([], [PRINCE])  # harry alone does not
    both = {"title": "Harry Potter fan site", "url": "https://fans.example/", "content": ""}
    assert on_topic(four, [both]) == ([both], [])


def test_a_word_only_in_the_address_counts_and_a_three_letter_word_is_no_prefix() -> None:
    by_address = {"title": "PCGW", "url": "https://www.pcgamingwiki.com/wiki/Wine", "content": ""}
    assert on_topic("pcgamingwiki wine", [by_address]) == ([by_address], [])
    windows = {"title": "Windows 11", "url": "https://windows.example/", "content": "windows"}
    assert on_topic("win", [windows]) == ([], [windows])
    assert on_topic("wind", [windows]) == ([windows], [])


def test_stopwords_and_short_words_are_never_what_a_row_shares() -> None:
    filler = {"title": "How to do the thing", "url": "https://x.example/", "content": "and the"}
    assert on_topic("how to install the wine", [filler]) == ([], [filler])


def test_a_query_with_no_meaningful_word_keeps_every_row() -> None:
    assert on_topic("how to?", [PRINCE, WHATSAPP]) == ([PRINCE, WHATSAPP], [])


def researcher(tmp_path: Path, rows: list[dict[str, str]]) -> Researcher:
    http = httpx.Client(
        transport=httpx.MockTransport(lambda r: httpx.Response(200, json={"results": rows}))
    )
    return Researcher({}, Approvals(tmp_path / "a.json"), tmp_path, http=http)


def test_off_topic_rows_are_hidden_counted_and_never_openable(tmp_path: Path) -> None:
    gate = ReaderGate()
    shown = researcher(tmp_path, [PRINCE, PCGW, WHATSAPP, FDA])._search(QUERY, gate, None)
    assert shown.startswith("(3 off-topic results hidden")
    assert PCGW["url"] in shown
    for junk in (PRINCE, WHATSAPP, FDA):
        assert junk["title"] not in shown
        assert junk["content"] not in shown
        assert gate.check("fetch", {"url": junk["url"]}) is not None  # known-bad: openable
    assert gate.check("fetch", {"url": PCGW["url"]}) is None
    assert "Prince Harry" not in "".join(gate.corpus)
    one = researcher(tmp_path, [PRINCE, PCGW])._search(QUERY, ReaderGate(), None)
    assert one.startswith("(1 off-topic result hidden")


def test_when_every_row_is_off_topic_the_reader_is_told_to_search_plainer(
    tmp_path: Path,
) -> None:
    gate = ReaderGate()
    shown = researcher(tmp_path, [PRINCE, WHATSAPP, FDA])._search(QUERY, gate, None)
    assert "nothing relevant" in shown
    assert "3 off-topic results hidden" in shown
    assert "fewer, plainer keywords" in shown
    assert "no results" not in shown  # it is not "nothing exists"
    for junk in (PRINCE, WHATSAPP, FDA):
        assert junk["title"] not in shown
        assert gate.check("fetch", {"url": junk["url"]}) is not None
    assert gate.corpus == []


def test_an_on_topic_search_shows_no_hidden_line(tmp_path: Path) -> None:
    shown = researcher(tmp_path, [PCGW])._search(QUERY, ReaderGate(), None)
    assert shown.startswith(PCGW["title"])
    assert "hidden" not in shown


class Junk(BraveSearch):
    """A Brave that answers with the same off-topic row."""

    def search(self, query: str) -> Outcome:
        row: dict[str, Any] = {k: PRINCE[k] for k in ("title", "url")}
        return Outcome(rows=[{**row, "description": PRINCE["content"]}])


def test_brave_rows_are_not_filtered(tmp_path: Path) -> None:
    reader = researcher(tmp_path, [])
    reader.brave = Junk("k", tmp_path / "brave.json")
    gate = ReaderGate()
    shown = reader._search(QUERY, gate, None)
    assert shown.startswith(PRINCE["title"])
    assert gate.check("fetch", {"url": PRINCE["url"]}) is None
