"""T6-75: the search/replace emission format, parser and applier."""

from __future__ import annotations

from pathlib import Path

import pytest

from saddle.edits import Edit, EditError, apply_edits, parse_edits

EDIT = "edit fees.py\n-FLAT_FEE = 0.30\n=======\n+FEES = {}\n>>>>>>>\n"


def test_parse_reads_an_edit_a_create_and_a_delete() -> None:
    """Known-good: the three operations, in one response, in order."""
    text = EDIT + "create money.py\n+from decimal import Decimal\n>>>>>>>\ndelete legacy.py\n"
    assert parse_edits(text) == [
        Edit("edit", "fees.py", "FLAT_FEE = 0.30\n", "FEES = {}\n"),
        Edit("create", "money.py", "", "from decimal import Decimal\n"),
        Edit("delete", "legacy.py"),
    ]


def test_parse_keeps_content_that_looks_like_the_markers() -> None:
    """Known-good: prefixing is what lets a body line start with -, + or >."""
    text = "edit m.py\n--x = a - b\n=======\n++y = a + b\n+>>> not a terminator\n>>>>>>>\n"
    (edit,) = parse_edits(text)
    assert edit.search == "-x = a - b\n"
    assert edit.replace == "+y = a + b\n>>> not a terminator\n"


def test_parse_accepts_an_empty_replacement() -> None:
    """Known-good: deleting lines is a replacement by nothing."""
    (edit,) = parse_edits("edit m.py\n-dead\n=======\n>>>>>>>\n")
    assert edit.replace == ""


def test_parse_refuses_an_empty_search_block() -> None:
    """Known-bad: an edit that names nothing to replace names no site."""
    with pytest.raises(EditError, match="names nothing to replace"):
        parse_edits("edit m.py\n=======\n+x = 1\n>>>>>>>\n")


def test_parse_refuses_an_unterminated_block() -> None:
    """Known-bad: a truncated completion must not read as a complete edit."""
    with pytest.raises(EditError, match="unterminated block"):
        parse_edits("edit m.py\n-x = 1\n=======\n+x = 2\n")


def test_parse_refuses_an_unprefixed_body_line() -> None:
    """Known-bad: an unprefixed line is where a block silently swallows prose."""
    with pytest.raises(EditError, match="expected a '-'-prefixed line"):
        parse_edits("edit m.py\nx = 1\n=======\n+x = 2\n>>>>>>>\n")


def test_parse_refuses_an_unknown_operation() -> None:
    """Known-bad: only the three operations exist."""
    with pytest.raises(EditError, match="expected 'edit ', 'create ' or 'delete '"):
        parse_edits("patch m.py\n-x\n=======\n+y\n>>>>>>>\n")


def test_parse_refuses_an_empty_response() -> None:
    """Known-bad: a response with no blocks is a spent attempt, not a no-op."""
    with pytest.raises(EditError, match="no edit blocks"):
        parse_edits("")


def _tree(tmp_path: Path) -> Path:
    (tmp_path / "fees.py").write_text("FLAT_FEE = 0.30\nBASE = 1\n")
    return tmp_path


def test_apply_replaces_the_single_occurrence(tmp_path: Path) -> None:
    """Known-good: the edit lands and nothing else moves."""
    _tree(tmp_path)
    assert apply_edits(tmp_path, EDIT) == ["fees.py"]
    assert (tmp_path / "fees.py").read_text() == "FEES = {}\nBASE = 1\n"


def test_apply_refuses_a_search_block_that_is_absent(tmp_path: Path) -> None:
    """Known-bad: a stale search is refused, never guessed at."""
    _tree(tmp_path)
    with pytest.raises(EditError, match="does not appear in the file"):
        apply_edits(tmp_path, "edit fees.py\n-NO_SUCH = 1\n=======\n+x = 2\n>>>>>>>\n")


def test_apply_refuses_a_search_block_that_matches_twice(tmp_path: Path) -> None:
    """Known-bad, the load-bearing half: without line numbers, uniqueness is
    the only thing that names a site, so an ambiguous block must not apply."""
    (tmp_path / "m.py").write_text("x = 1\ny = 2\nx = 1\n")
    with pytest.raises(EditError, match="appears 2 times"):
        apply_edits(tmp_path, "edit m.py\n-x = 1\n=======\n+x = 3\n>>>>>>>\n")
    assert (tmp_path / "m.py").read_text() == "x = 1\ny = 2\nx = 1\n"


def test_apply_matches_a_final_line_with_no_trailing_newline(tmp_path: Path) -> None:
    """Known-good: a block always ends a line; a file's last line need not."""
    (tmp_path / "m.py").write_text("a = 1\nb = 2")
    apply_edits(tmp_path, "edit m.py\n-b = 2\n=======\n+b = 3\n>>>>>>>\n")
    assert (tmp_path / "m.py").read_text() == "a = 1\nb = 3"


def test_apply_deletes_a_final_line_with_no_trailing_newline(tmp_path: Path) -> None:
    """Known-good: the same fallback with nothing to put back."""
    (tmp_path / "m.py").write_text("a = 1\nb = 2")
    apply_edits(tmp_path, "edit m.py\n-b = 2\n=======\n>>>>>>>\n")
    assert (tmp_path / "m.py").read_text() == "a = 1\n"


def test_apply_creates_a_file_in_a_new_directory(tmp_path: Path) -> None:
    """Known-good: a create makes its parents."""
    apply_edits(tmp_path, "create pkg/money.py\n+VALUE = 1\n>>>>>>>\n")
    assert (tmp_path / "pkg" / "money.py").read_text() == "VALUE = 1\n"


def test_apply_refuses_to_create_over_an_existing_file(tmp_path: Path) -> None:
    """Known-bad: a create that clobbers is an edit that skipped the search."""
    _tree(tmp_path)
    with pytest.raises(EditError, match="the file already exists"):
        apply_edits(tmp_path, "create fees.py\n+x = 1\n>>>>>>>\n")


def test_apply_deletes_a_file(tmp_path: Path) -> None:
    """Known-good."""
    _tree(tmp_path)
    assert apply_edits(tmp_path, "delete fees.py\n") == ["fees.py"]
    assert not (tmp_path / "fees.py").exists()


def test_apply_refuses_to_delete_a_file_that_is_not_there(tmp_path: Path) -> None:
    """Known-bad: a delete of nothing is a stale plan, not a success."""
    with pytest.raises(EditError, match="cannot delete, no such file"):
        apply_edits(tmp_path, "delete gone.py\n")


def test_apply_refuses_to_edit_a_file_that_is_not_there(tmp_path: Path) -> None:
    """Known-bad."""
    with pytest.raises(EditError, match="cannot edit, no such file"):
        apply_edits(tmp_path, "edit gone.py\n-x\n=======\n+y\n>>>>>>>\n")


def test_apply_refuses_a_path_outside_the_worktree(tmp_path: Path) -> None:
    """Known-bad: scope is the node's, and traversal is never in it."""
    with pytest.raises(EditError, match="resolves outside the worktree"):
        apply_edits(tmp_path, "edit ../escape.py\n-x\n=======\n+y\n>>>>>>>\n")


def test_an_edit_costs_the_size_of_the_change_not_the_size_of_the_file(
    tmp_path: Path,
) -> None:
    """Known-good, and the reason the format exists (T6-75).

    Under `DIFF_GRAMMAR` the only way to touch a file was to re-emit it
    whole, so a one-line change to a 2 000-line module cost 2 000 lines of
    emission and had to reproduce all of them exactly. Here the same
    change is three lines. The assertion is the ratio, because that ratio
    is what decides whether saddle can be pointed at a real repository.
    """
    big = "".join(f"LINE_{n} = {n}\n" for n in range(2000))
    (tmp_path / "big.py").write_text(big)
    block = "edit big.py\n-LINE_1500 = 1500\n=======\n+LINE_1500 = 1501\n>>>>>>>\n"
    apply_edits(tmp_path, block)
    assert (tmp_path / "big.py").read_text() == big.replace("LINE_1500 = 1500", "LINE_1500 = 1501")
    assert len(block) * 50 < len(big), "an edit must be orders of magnitude cheaper than the file"


def test_a_block_whose_last_line_has_no_newline_parses(tmp_path: Path) -> None:
    """A packet is not always newline-terminated.

    The decoder stops at the terminator, and whether a trailing newline
    survives the transport is not something the applier gets to assume.
    Both spellings must mean the same edit, or a correct draw is lost to
    the byte that happened to end it.
    """
    (tmp_path / "m.py").write_text("a = 1\n")
    block = "edit m.py\n-a = 1\n=======\n+a = 2\n>>>>>>>"
    assert not block.endswith("\n")
    apply_edits(tmp_path, block)
    assert (tmp_path / "m.py").read_text() == "a = 2\n"
    assert parse_edits(block) == parse_edits(block + "\n")


def test_a_search_block_missing_a_blank_line_still_names_its_site(tmp_path: Path) -> None:
    """Known-good, observed live and frozen (`edit_blank_line_slip.txt`).

    Three draws of one prompt against a 73-line file: two reproduced the
    blank line between `balance` and `deposit`, one did not, and only the
    third was refused. The block still names exactly one site, which is
    the contract; byte-identical blank lines never were.
    """
    fixtures = Path(__file__).parent / "fixtures"
    body = (fixtures / "edit_target_accounts.txt").read_text()
    (tmp_path / "accounts.py").write_text(body)
    block = (fixtures / "edit_blank_line_slip.txt").read_text()
    assert "-        return self._balance\n-    def deposit" in block, "the slip itself"

    apply_edits(tmp_path, block)

    after = (tmp_path / "accounts.py").read_text()
    assert "def currencies(self):" in after
    assert "def deposit(self, amount):" in after
    assert "def balance(self):" in after


def test_a_loose_match_that_names_two_sites_is_still_refused(tmp_path: Path) -> None:
    """The load-bearing half: relaxing whitespace must not relax uniqueness.

    The search block carries a blank line the file has at neither site, so
    it matches nowhere exactly and at both sites loosely. Ambiguity is
    refused either way, and the message says which problem it is -- "does
    not appear" would send the worker hunting a typo when what it needs is
    more surrounding context.
    """
    (tmp_path / "m.py").write_text("a = 1\nb = 2\n\nprint(0)\n\na = 1\nb = 2\n")
    block = "edit m.py\n-a = 1\n-\n-b = 2\n=======\n+a = 9\n+b = 2\n>>>>>>>\n"
    assert "a = 1\n\nb = 2\n" not in (tmp_path / "m.py").read_text(), "no exact match"
    with pytest.raises(EditError, match="more than one site"):
        apply_edits(tmp_path, block)


def test_a_loose_match_still_requires_the_significant_lines_to_agree(tmp_path: Path) -> None:
    """Blank lines are skippable; real lines are not."""
    (tmp_path / "m.py").write_text("a = 1\n\nb = 2\n")
    with pytest.raises(EditError, match="does not appear"):
        apply_edits(tmp_path, "edit m.py\n-a = 1\n-c = 3\n=======\n+a = 9\n>>>>>>>\n")


def test_a_search_block_of_only_blank_lines_names_nothing(tmp_path: Path) -> None:
    """`-` accepts an empty line, so a block can carry no content at all.

    A file with no newline in it gives that block no exact match either,
    which is the one route into the loose matcher with nothing to look
    for. It must refuse rather than match the whole file.
    """
    (tmp_path / "m.py").write_text("abc")
    with pytest.raises(EditError, match="does not appear"):
        apply_edits(tmp_path, "edit m.py\n-\n=======\n+x = 1\n>>>>>>>\n")
    assert (tmp_path / "m.py").read_text() == "abc"


def test_an_edit_that_only_deletes_lines_leaves_no_blank_behind(tmp_path: Path) -> None:
    """An empty replacement removes the span; it does not blank it out."""
    (tmp_path / "m.py").write_text("keep = 1\ndrop = 2\n\ndrop = 3\nkeep = 4\n")
    apply_edits(tmp_path, "edit m.py\n-drop = 2\n-drop = 3\n=======\n>>>>>>>\n")
    assert (tmp_path / "m.py").read_text() == "keep = 1\nkeep = 4\n"
