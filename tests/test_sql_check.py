"""`saddle sql-check`: the tables and columns of a `.sql` file, held against a snapshot.

Every rule of the contract is judged from a file in `tests/sql_check_fixtures.py`, which
holds the `.sql` text as a person would write it and, for each known-bad file, the misses the
command must print. Both halves of a rule are on the record where the fixtures hold both: a
known-good file that must exit 0 saying nothing, and a known-bad file of the same shape that
must exit 1 naming the name at the line and column where it starts. The sweep of every
known-good and every known-bad file runs as a test of its own, for the command and for the
text, so a rule broken for one fixture is named.

`check_text` is called directly too, because a miss's position is a fact about the file's text
before it is a fact about a report line: a test that read only the report could not tell a
position read off the text from one that merely matched the first spelling of that name in the
file. `check_paths` is the whole command, with its snapshot and its dialect, and `main` is the
wiring, so the flags, the values they take and the refusals they raise are checked as well.
"""

from __future__ import annotations

import json
import re
import sqlite3
import sys
from io import StringIO
from pathlib import Path
from typing import Any

import pytest
from sql_check_fixtures import BAD, BAD_SNAPSHOTS, GOOD, NOT_UTF8, SCHEMA

from saddle.cli import main
from saddle.sqlcheck import SqlProblem, check_file, check_paths, check_text

#: One report line, read back as the contract spells it: the path as it was given, the 1-based
#: line and column where the unknown name itself starts, the kind of name, and the name.
MISS: re.Pattern[str] = re.compile(
    r"^(?P<path>.+):(?P<line>\d+):(?P<column>\d+): unknown (?P<kind>table|column) (?P<name>.+)$"
)

#: A known-bad file SQL does read, so its fixture can name each miss it holds.
WITH_MISSES: list[str] = [name for name in sorted(BAD) if BAD[name][2]]

#: A known-bad file SQL does not read at all: a quote that never closes, or a statement with no
#: expression where one is required. Its test only needs the command to refuse it.
UNPARSABLE: list[str] = [name for name in sorted(BAD) if not BAD[name][2]]

#: The quote each SQL dialect puts around a name, which the report takes off.
_QUOTES: frozenset[str] = frozenset({'"', "'", "`"})


def _file(tmp_path: Path, name: str, text: str) -> str:
    """One fixture written out as the `.sql` file a person would hand the command."""
    path = tmp_path / name
    path.write_text(text, encoding="utf-8")
    return str(path)


def _snapshot(tmp_path: Path, schema: object = SCHEMA) -> str:
    """The schema snapshot the command is given: a JSON object of table names and columns."""
    path = tmp_path / "schema.json"
    path.write_text(json.dumps(schema), encoding="utf-8")
    return str(path)


def _run(
    paths: list[str],
    tmp_path: Path,
    *,
    dialect: str = "duckdb",
    schema: str | None = None,
) -> tuple[int, list[str], str, str]:
    """What `saddle sql-check --dialect D --schema S PATH...` does: its exit code, its report
    lines, its raw report, and what it said on the other stream."""
    out, err = StringIO(), StringIO()
    code = check_paths(
        paths, dialect=dialect, schema=schema or _snapshot(tmp_path), stdout=out, stderr=err
    )
    raw = out.getvalue()
    return code, raw.splitlines(), raw, err.getvalue()


def _misses(lines: list[str]) -> list[tuple[int, int, str, str]]:
    """Each report line read back as a person reads it: line, column, kind, name."""
    found = [MISS.match(line) for line in lines]
    assert all(match is not None for match in found), lines
    return [
        (
            int(match.group("line")),
            int(match.group("column")),
            match.group("kind"),
            match.group("name"),
        )
        for match in found
        if match is not None
    ]


def _fixture(name: str) -> tuple[str, str]:
    """A known-bad fixture's dialect and text."""
    dialect, text, _ = BAD[name]
    return dialect, text


def _expected(name: str) -> list[tuple[int, int, str, str]]:
    """A known-bad fixture's misses, as (line, column, kind, name) in report order."""
    return [(line, column, kind, reported) for kind, reported, line, column in BAD[name][2]]


def _says(name: str) -> list[tuple[int | None, int | None, str]]:
    """A known-bad fixture's misses, spelled the way one problem spells them."""
    return [
        (line, column, f"unknown {kind} {reported}")
        for kind, reported, line, column in BAD[name][2]
    ]


def _reported(problems: list[SqlProblem]) -> list[tuple[int | None, int | None, str]]:
    """What the check says about a file's text: each problem's line, column and words."""
    return [(problem.line, problem.column, problem.message) for problem in problems]


def _only(text: str, dialect: str = "duckdb") -> list[tuple[int | None, int | None, str]]:
    """What the check says about one file's text, read straight off that text."""
    return _reported(check_text("f.sql", text, dialect, dict(SCHEMA)))


def _text_case(text: str, expected: list[tuple[str, str]], dialect: str = "duckdb") -> None:
    """One file's text, written out here rather than in the fixture file, refused with exactly
    the misses this test names as (kind, name), in the order the report gives them. Each
    reported position is read back off the text: the line and column it names is where that
    text spells the name the report prints."""
    reported = _only(text, dialect)
    spelled = text.splitlines()
    kinds_and_names: list[tuple[str, str]] = []
    for line, column, message in reported:
        assert line is not None, (text, message)
        assert column is not None, (text, message)
        found = re.fullmatch(r"unknown (table|column) (.+)", message)
        assert found is not None, (text, message)
        kind, name = found.group(1), found.group(2)
        row = spelled[line - 1]
        assert row[column - 1 : column - 1 + len(name)] == name, (text, message, row)
        kinds_and_names.append((kind, name))
    assert kinds_and_names == expected, (text, reported)


def _good(name: str) -> None:
    """One known-good fixture taken with nothing reported."""
    dialect, text = GOOD[name]
    assert check_text("f.sql", text, dialect, dict(SCHEMA)) == [], text


def _fixture_case(name: str) -> None:
    """One known-bad fixture refused with exactly the misses its fixture names."""
    dialect, text = _fixture(name)
    assert _reported(check_text("f.sql", text, dialect, dict(SCHEMA))) == _says(name), text


def _pair(good_name: str, bad_name: str) -> None:
    """One rule read both ways, from two fixtures of the same shape: the known-good file the
    snapshot holds is taken with nothing reported, and the known-bad file is refused with
    exactly the misses its fixture names. A rule that resolved the name either way, or that
    held a source as holding every column or no column, cannot survive both halves."""
    _good(good_name)
    _fixture_case(bad_name)


@pytest.mark.parametrize("name", sorted(GOOD))
def test_a_file_where_every_name_resolves_is_taken(name: str, tmp_path: Path) -> None:
    """A `.sql` file whose every table and column the snapshot holds exits 0 and prints no miss,
    read in the dialect its own fixture names."""
    dialect, text = GOOD[name]
    path = _file(tmp_path, "good.sql", text)
    code, lines, raw, err = _run([path], tmp_path, dialect=dialect)
    assert code == 0, raw
    assert lines == [], raw
    assert err == "", err
    assert check_text("f.sql", text, dialect, dict(SCHEMA)) == [], text


@pytest.mark.parametrize("name", WITH_MISSES)
def test_a_file_with_a_miss_is_refused_at_the_line_and_column_it_starts_at(
    name: str, tmp_path: Path
) -> None:
    """A file the snapshot does not hold is exit 1, and every miss is one line naming the path
    as it was given, the line and column where the unknown name itself starts, and the name."""
    dialect, text = _fixture(name)
    path = _file(tmp_path, "bad.sql", text)
    code, lines, raw, _ = _run([path], tmp_path, dialect=dialect)
    assert code == 1, raw
    assert all(line.startswith(f"{path}:") for line in lines), raw
    assert _misses(lines) == _expected(name), raw


@pytest.mark.parametrize("name", WITH_MISSES)
def test_a_file_with_a_miss_is_refused_when_read_as_text(name: str) -> None:
    """The same file read as text, with no command and no snapshot file in the way: the same
    misses, the same positions, and the same words."""
    dialect, text = _fixture(name)
    assert _reported(check_text("f.sql", text, dialect, dict(SCHEMA))) == _says(name), text


@pytest.mark.parametrize("name", WITH_MISSES)
def test_a_report_points_at_where_the_file_spells_the_name(name: str) -> None:
    """LINE and COL are 1-based and counted in the whole file, and mark where the unknown name
    itself starts. The fixture text is where a miss is, so each reported position is read back
    off the file: at that line, starting at that column, the file spells the very name the
    report prints, and what precedes it is not part of a longer name."""
    text = _fixture(name)[1]
    spelled_lines = text.splitlines()
    for kind, reported, line, column in BAD[name][2]:
        spelled = spelled_lines[line - 1]
        at = spelled[column - 1]
        if at in _QUOTES:
            # A quoted name is reported bare, so the file's own spelling at that position is
            # the opening quote, with the name the report prints just after it.
            assert spelled.startswith(reported, column), (name, kind, reported, spelled)
        else:
            assert spelled.startswith(reported, column - 1), (name, kind, reported, spelled)
        before = spelled[: column - 1]
        assert before == "" or not (before[-1].isalnum() or before[-1] == "_"), (
            name,
            reported,
            before,
        )


@pytest.mark.parametrize("name", UNPARSABLE)
def test_a_file_that_does_not_parse_never_exits_0(name: str, tmp_path: Path) -> None:
    """A file SQL does not read is one problem, on a line naming the file, the reason, and the
    line and column the failure points at, and it is never a pass. The position is named once:
    what sqlglot spelled after its own reason is not repeated as this commands report."""
    dialect, text = _fixture(name)
    path = _file(tmp_path, "broken.sql", text)
    code, lines, raw, _ = _run([path], tmp_path, dialect=dialect)
    assert code == 1, raw
    assert len(lines) == 1, raw
    where = re.match(rf"^{re.escape(path)}:(\d+):(\d+): does not parse: ", lines[0])
    assert where is not None, raw
    assert lines[0].count("Col:") == 1, lines[0]
    assert check_text(path, text, dialect, dict(SCHEMA)) != [], raw


def test_a_name_is_at_the_line_and_column_where_its_own_spelling_starts() -> None:
    """Both numbers a report gives come out of the file: the line is the line of the file the
    name is written on and the column counts the characters before the name itself, so a person
    who opens the file at that line finds that name written there."""
    text = "SELECT name,\n    bogus_name FROM users;\n"
    line, column = _only(text)[0][0], _only(text)[0][1]
    assert line is not None, _only(text)
    assert column is not None, _only(text)
    assert (line, column) == (2, 5), _only(text)
    assert text.splitlines()[line - 1][column - 1 : column + 9] == "bogus_name", text


def test_a_qualified_column_is_reported_after_its_qualifier(tmp_path: Path) -> None:
    """NAME is the bare name as written and the column is where that name starts: `u.name` of
    `orders AS u`, which holds no `name`, is reported as `unknown column name` at the column
    `name` starts at, after the qualifier and the dot."""
    dialect, text = _fixture("column-qualified-by-a-source-without-it")
    path = _file(tmp_path, "qualified.sql", text)
    code, lines, raw, _ = _run([path], tmp_path, dialect=dialect)
    assert code == 1, raw
    assert _misses(lines) == [(1, 10, "column", "name")], raw
    assert "u.name" not in raw, raw
    assert text[9:13] == "name", text


def test_a_misspelt_table_is_named_and_holds_no_column() -> None:
    """A table the snapshot does not name is a miss of its own, and a column read from it is a
    miss too: an unknown table holds no column that could resolve."""
    _fixture_case("misspelt-table")
    _text_case(
        "SELECT id, name FROM oder;\n", [("column", "id"), ("column", "name"), ("table", "oder")]
    )


def test_a_misspelt_column_is_named_at_its_own_column() -> None:
    """A column the table it is read from does not hold is named at the column where it starts,
    with the name as the file wrote it."""
    _fixture_case("misspelt-column")
    _text_case("SELECT id, total FROM users;\n", [("column", "total")])


def test_a_miss_in_a_later_statement_reports_that_statements_own_line(tmp_path: Path) -> None:
    """Line and column are counted in the whole file, so a file's later query reports the line
    its own miss sits on and not the line of an earlier query that named the same table."""
    dialect, text = _fixture("miss-in-a-later-statement")
    path = _file(tmp_path, "later.sql", text)
    code, lines, raw, _ = _run([path], tmp_path, dialect=dialect)
    assert code == 1, raw
    assert _misses(lines) == [(3, 14, "column", "total")], raw
    assert text.splitlines()[2][13:18] == "total", text


def test_a_query_with_several_misses_names_each_one(tmp_path: Path) -> None:
    """Every miss is printed and not only the first: a query that misses two of its columns and
    the table it reads is three lines."""
    dialect, text = _fixture("several-misses-one-query")
    path = _file(tmp_path, "several.sql", text)
    code, lines, raw, _ = _run([path], tmp_path, dialect=dialect)
    assert code == 1, raw
    assert len(lines) == 3, raw
    assert _misses(lines) == _expected("several-misses-one-query"), raw


def test_a_query_that_misses_one_name_twice_reports_both_spellings() -> None:
    """A name written twice in one query is two misses at two positions: the report keeps each
    spelling its own place rather than naming the name once."""
    _fixture_case("same-name-twice-one-query")
    _text_case(
        "SELECT bogon, bogon FROM bogus;\n",
        [("column", "bogon"), ("column", "bogon"), ("table", "bogus")],
    )


def test_a_name_misspelt_in_two_statements_reports_each_statement() -> None:
    """Each miss takes its position from a token of its own statement: a name misspelt in two
    statements is reported in both, and the second report names the second statement's own line
    rather than the first statement's spelling of it again."""
    _fixture_case("same-misspelt-name-two-statements")


def test_a_miss_on_either_side_of_a_union_is_named() -> None:
    """A set operation is two queries: a name that misses on one side is named where that side
    wrote it, and a table that misses on the other side is a miss of its own."""
    _fixture_case("miss-either-side-of-a-union")


def test_of_a_good_file_and_a_bad_file_only_the_bad_one_is_named(tmp_path: Path) -> None:
    """Of several paths, only the files with a miss are named: a file the snapshot holds adds no
    line, and every line names the file its own miss is in."""
    good = _file(tmp_path, "good.sql", GOOD["two-statements"][1])
    dialect, text = _fixture("misspelt-column")
    bad = _file(tmp_path, "bad.sql", text)
    code, lines, raw, _ = _run([good, bad], tmp_path, dialect=dialect)
    assert code == 1, raw
    assert all(not line.startswith(f"{good}:") for line in lines), raw
    assert _misses(lines) == _expected("misspelt-column"), raw
    assert lines[0].startswith(f"{bad}:"), raw


def test_the_path_is_printed_as_it_was_given(tmp_path: Path) -> None:
    """The PATH of a report line is the argument the person wrote, not a path this command
    resolved for itself. One file is named four ways — a bare name read in the working
    directory, the same name with a `.` step, the whole path, and that path with a `.` step
    inside it — and each refusal spells the name back exactly as it was written."""
    dialect, text = _fixture("misspelt-column")
    written = Path.cwd() / "given.sql"
    written.write_text(text, encoding="utf-8")
    spellings = [
        "given.sql",
        "./given.sql",
        str(written),
        f"{written.parent}/./given.sql",
    ]
    for spelling in spellings:
        assert Path(spelling).resolve() == written, spelling
    for given in spellings:
        code, lines, raw, _ = _run([given], tmp_path, dialect=dialect)
        assert code == 1, (given, raw)
        assert _misses(lines) == _expected("misspelt-column"), (given, raw)
        assert raw.startswith(f"{given}:1:8: "), (given, raw)


def test_problems_are_printed_by_line_and_only_then_by_column(tmp_path: Path) -> None:
    """A report reads top to bottom: a file's misses come out in the order the file wrote them,
    so a name written on a later line is never printed before a name written earlier."""
    text = (
        "SELECT bogus_name FROM users;\n"
        "SELECT id FROM users;\n"
        "\nSELECT name, bogus_name FROM oder;\n"
    )
    path = _file(tmp_path, "ordered.sql", text)
    code, lines, raw, _ = _run([path], tmp_path)
    assert code == 1, raw
    where = [
        (int(match.group("line")), int(match.group("column")))
        for match in (MISS.match(line) for line in lines)
        if match is not None
    ]
    assert where == sorted(where), raw
    assert _misses(lines) == [
        (1, 8, "column", "bogus_name"),
        (4, 8, "column", "name"),
        (4, 14, "column", "bogus_name"),
        (4, 30, "table", "oder"),
    ], raw


def test_a_column_of_another_table_does_not_resolve() -> None:
    """A column resolves against the tables of its own query: `name` is a column of `users`, and
    this query reads `orders`, which holds no `name`."""
    _pair("aliases-and-joins", "column-of-another-table")


def test_a_qualified_column_resolves_only_against_the_source_it_names() -> None:
    """A qualified column resolves against the table its qualifier or alias names and against
    nothing else: `u.name` of `orders AS u` is a miss although `users` holds `name`, and a
    column qualified by a name that is no source at all resolves against nothing the query
    holds, whatever its other tables."""
    _fixture_case("column-qualified-by-a-source-without-it")
    _fixture_case("column-qualified-by-a-name-that-is-no-source")
    _text_case("SELECT u.id FROM orders AS u;\n", [])


def test_a_dotted_qualified_column_does_not_walk_a_name_chain() -> None:
    """A `db.table.column` resolves against the table and not along the chain: neither the schema
    a table was written under nor the table named in the middle is looked at, and a miss names
    the bare name, after the whole chain."""
    _pair("name-chain-resolves-against-the-table", "name-chain-still-resolves-against-the-table")


def test_names_resolve_case_insensitively_and_are_reported_as_written() -> None:
    """A bare name is read case-insensitively, so a file that writes `USERS` and `ID` where the
    snapshot writes `users` and `id` resolves; a miss reports the file's own spelling, so `TOTAL`
    is named as `TOTAL` and not as the snapshot spells it."""
    _pair("another-case-than-the-snapshot", "reported-as-written")


def test_a_quoted_name_that_misses_is_named_at_its_opening_quote() -> None:
    """NAME is the name inside the quotes, with the quotes taken off, and its position is the
    opening quotes, which is where the spelling itself starts. Each dialect quotes a name its
    own way, and each reports the same bare name."""
    _good("quoted-names")
    _fixture_case("quoted-postgres-miss")
    _fixture_case("quoted-mysql-miss")


def test_a_quoted_name_is_reported_without_its_quotes() -> None:
    """A quoted name that misses is reported without its quotes and with the file's own case, in
    the quoting style the dialect uses."""
    _fixture_case("reported-as-written-quoted")


def test_a_correlated_subquery_reads_the_tables_of_the_query_it_sits_in() -> None:
    """A subquery resolves against the tables of the query it sits in, which are also the names
    its qualifiers may name: `users.id` resolves through the outer query. A table of the
    subquery's own `FROM` stays out of the scope above it, and a column of a qualified table that
    does not hold it is a miss named where the subquery wrote it."""
    _pair("correlated-subquery-qualifier", "correlated-subquery-wrong-qualifier")
    _text_case("SELECT id FROM users WHERE (SELECT user_id FROM orders) > 1;\n", [])
    _text_case(
        "SELECT id FROM users WHERE (SELECT bogus_name FROM orders) > 1;\n",
        [("column", "bogus_name")],
    )


def test_a_cte_holds_its_own_output_names_and_not_the_snapshot() -> None:
    """A CTE gives the query above it the columns its own query writes out: a column of the table
    its body reads that its body does not output is unknown, and the CTE is not the snapshot
    table its name might match."""
    _pair("cte-names-a-source", "column-not-in-a-ctes-output")
    _pair("cte-wins-its-name-over-a-snapshot-table", "named-source-is-no-table")


def test_a_cte_column_list_names_what_it_outputs() -> None:
    """`WITH t(x, y) AS (SELECT id, name FROM users)` outputs `x` and `y`: the list writes the
    names, and a name the list does not write is not a column of that source."""
    _good("cte-columns-renamed")
    _text_case(
        "WITH t(x, y) AS (SELECT id, name FROM users)\nSELECT id FROM t;\n", [("column", "id")]
    )


def test_a_derived_table_holds_its_own_output_names() -> None:
    """A derived table holds what its own query writes out, and resolves its body against its own
    `FROM` as a CTE does: the body's miss is named where the body wrote it, and a name the body
    does not output is unknown to the query that reads the source."""
    _pair("derived-table-names-a-source", "miss-in-a-derived-tables-body")
    _text_case(
        "SELECT surname FROM (SELECT id AS name FROM users) AS s;\n", [("column", "surname")]
    )


def test_a_declared_column_list_replaces_the_names_its_body_writes() -> None:
    """A source that writes a column list over its body — `AS s(id)`, over a query or over a
    `VALUES` table — is held as that list alone, in place of the names the body writes out and
    not beside them. `name` is written only by `SELECT id AS name FROM users`, so it is unknown
    to a query that reads the source as `AS s(id)`, and `id`, which the list writes, is a column
    of that source."""
    _pair(
        "source-holds-its-declared-list-in-place-of-its-body", "column-a-declared-list-renames-away"
    )
    _pair("derived-table-columns-renamed-over-star", "column-not-in-a-values-column-list")


def test_a_named_values_source_holds_only_its_own_column_list() -> None:
    """A `VALUES` table named with a column list states the names it holds and names no table, so
    the source is that list alone. `amount` is the one name with two results: it resolves against
    `AS totals(id, amount)`, and is unknown to the file whose list names `id` alone. A source
    named with no list at all states nothing an unqualified column of that query may read."""
    _good("values-column-list-names-a-source")
    _fixture_case("column-not-in-a-values-column-list")
    _fixture_case("values-source-named-without-a-column-list")


def test_a_recursive_cte_holds_only_its_own_column_list() -> None:
    """A recursive `WITH` is the one SQL lets a source name its own body, and what that body may
    read through the name is the column list the CTE writes over itself: `walk.id` of `walk(id)`
    resolves and `walk.user_id` of the same arm does not. A `WITH` that is not recursive keeps
    its own name out of its body reach altogether, and the name it cannot read holds no column
    for that body."""
    _pair("recursive-cte-reads-its-own-name", "recursive-cte-column-its-own-list-does-not-name")
    _fixture_case("cte-cannot-read-its-own-name")


def test_a_named_source_states_its_columns_or_names_no_column() -> None:
    """A `FROM` that reads a file rather than naming a table names nothing the snapshot is asked
    about, and its alias states no columns of its own. `total` is the one name with two results:
    `file.total` of `read_csv('totals.csv') AS file` is a name read from that file and resolves,
    while `total` written unqualified of the same query resolves against no source of it."""
    _pair("table-function-holds-what-its-alias-qualifies", "column-named-under-a-table-function")


def test_a_star_names_no_column_that_can_miss() -> None:
    """A bare `*` and a qualified `u.*` name no column of their own, and a `SELECT *` says nothing
    about a source's columns, so every name read from such a source resolves: even a name the
    snapshot holds for no table at all is a column of a source that states no columns of its own,
    and the star is a name no file wrote, so nothing there can miss."""
    for name in ("stars", "derived-table-star-resolves-each-column-read-from-it"):
        _good(name)
    for text in (
        "SELECT total FROM (SELECT * FROM users) AS s;\n",
        "SELECT anything FROM (SELECT * FROM logs) AS s;\n",
        "WITH t AS (SELECT * FROM logs) SELECT anything FROM t;\n",
        "SELECT s.user_id FROM (SELECT *, 1 AS n FROM orders) AS s;\n",
    ):
        _text_case(text, [])


def test_a_statement_that_holds_no_query_is_not_resolved(tmp_path: Path) -> None:
    """A `CREATE`, an index, a `DROP` and a bare command name no table and no column for the check
    to resolve, and are not asked of the snapshot: a file of nothing but statements that hold no
    query is taken, and a file that holds one is resolved as the query it holds."""
    dialect, text = GOOD["statements-that-hold-no-query"]
    path = _file(tmp_path, "no-query.sql", text)
    code, lines, raw, _ = _run([path], tmp_path, dialect=dialect)
    assert code == 0, raw
    assert lines == [], raw
    _text_case("CREATE TABLE notes (id INTEGER);\n", [])


def test_creating_a_table_names_no_column_the_file_later_reads() -> None:
    """A `CREATE TABLE` is not a source and is not a snapshot: the columns it spells are not what
    a later `SELECT` of that table is resolved against, so a column the snapshot does not hold for
    that table is refused."""
    _fixture_case("create-table-names-no-columns")


def test_a_comment_or_a_string_holds_no_name_that_can_miss() -> None:
    """A name inside a comment, a string literal or a keyword of the dialect is never a table and
    never a column, whatever names it spells."""
    _good("comment-names-nothing")
    for text in (
        "SELECT id FROM users -- orders.name and nosuch.table\n",
        "SELECT 'orders.user_id' AS label, id FROM users;\n",
        "SELECT id AS name FROM users;\n",
    ):
        _text_case(text, [])


def test_a_string_or_a_comment_is_never_a_misses_position() -> None:
    """A miss reports the position of its own name and nothing else: a file that spells the
    misspelt name inside a string as well reports the column where the name is written as a name,
    never a column of the text that only looks like it."""
    text = "SELECT bogus_name FROM bogus; -- bogus_name in a comment\n"
    _text_case(text, [("column", "bogus_name"), ("table", "bogus")])
    comment_starts = text.index("-- bogus_name")
    assert text[comment_starts + 3 : comment_starts + 13] == "bogus_name", text
    assert all(column != comment_starts + 4 for _, column, _ in _only(text)), _only(text)


def test_a_table_the_snapshot_names_with_no_columns_holds_no_column(tmp_path: Path) -> None:
    """A table the snapshot names with an empty list of columns holds nothing: a column read from
    it is a miss, while the table itself is a name the snapshot holds."""
    dialect, text = _fixture("column-of-a-table-with-no-columns")
    path = _file(tmp_path, "no-columns.sql", text)
    code, lines, raw, _ = _run([path], tmp_path, dialect=dialect)
    assert code == 1, raw
    assert _misses(lines) == [(1, 8, "column", "id")], raw
    _text_case("SELECT logs.id FROM logs;\n", [("column", "id")])
    _text_case("SELECT * FROM logs;\n", [])


def test_a_statement_that_writes_names_its_table_and_its_columns() -> None:
    """A statement that writes resolves what it names too: the column list of an `INSERT`, the
    `SET` of an `UPDATE` and the `WHERE` of a `DELETE` are held against the table they name, and a
    name that table does not hold is refused."""
    for name in ("insert-column-list-of-values", "insert-of-a-select", "update", "delete"):
        _good(name)
    _text_case("INSERT INTO users (bogus_name) VALUES (1);\n", [("column", "bogus_name")])
    _text_case("UPDATE users SET user_id = 1;\n", [("column", "user_id")])
    _text_case("DELETE FROM oder WHERE id = 1;\n", [("table", "oder"), ("column", "id")])


def test_a_file_is_read_in_the_dialect_the_flag_names(tmp_path: Path) -> None:
    """The dialect is the one the flag names: a name quoted one way and a chain of names written
    another are read the way that dialect writes them, so the miss each fixture names is found in
    the dialect its fixture names."""
    for name in (
        "quoted-postgres-miss",
        "quoted-mysql-miss",
        "name-chain-still-resolves-against-the-table",
    ):
        dialect, text = _fixture(name)
        path = _file(tmp_path, f"{name}.sql", text)
        code, lines, raw, _ = _run([path], tmp_path, dialect=dialect)
        assert code == 1, raw
        assert _misses(lines) == _expected(name), raw


@pytest.mark.parametrize("name", sorted(BAD_SNAPSHOTS))
def test_a_snapshot_that_is_not_an_object_of_name_lists_is_refused(
    name: str, tmp_path: Path
) -> None:
    """A snapshot that is not JSON, is not an object, or holds something other than a list of
    column names for a table is named on a line of its own and no file is checked: a run that
    cannot hold its files against the names it was given is not a run that found nothing wrong."""
    schema = tmp_path / f"snapshot-{name}.json"
    schema.write_text(BAD_SNAPSHOTS[name], encoding="utf-8")
    good = _file(tmp_path, "good.sql", GOOD["two-statements"][1])
    code, lines, raw, _ = _run([good], tmp_path, schema=str(schema))
    assert code == 1, raw
    assert len(lines) == 1, raw
    assert lines[0].startswith(f"{schema}:"), raw
    assert "schema snapshot" in lines[0], raw


def test_a_snapshot_that_is_not_there_is_named_and_not_a_crash(tmp_path: Path) -> None:
    """The file `--schema S` names is read like every other file the command is given: a
    snapshot that is not there is one problem line naming it, and no file is checked against a
    snapshot that does not exist."""
    missing = str(tmp_path / "absent.json")
    good = _file(tmp_path, "good.sql", GOOD["two-statements"][1])
    code, lines, raw, _ = _run([good], tmp_path, schema=missing)
    assert code == 1, raw
    assert len(lines) == 1, raw
    assert lines[0].startswith(f"{missing}:"), raw
    assert "cannot be read" in lines[0], raw


def test_a_snapshot_held_by_a_file_is_held_against_every_path(tmp_path: Path) -> None:
    """The snapshot is read once and every file is resolved against it, in the order the paths
    were given: each miss names its own file and each file that resolves adds no line."""
    bad_one = _file(tmp_path, "one.sql", _fixture("misspelt-column")[1])
    good = _file(tmp_path, "two.sql", GOOD["two-statements"][1])
    bad_two = _file(tmp_path, "three.sql", _fixture("misspelt-table")[1])
    code, lines, raw, _ = _run([bad_one, good, bad_two], tmp_path)
    assert code == 1, raw
    assert _misses(lines) == _expected("misspelt-column") + _expected("misspelt-table"), raw
    assert lines[0].startswith(f"{bad_one}:"), raw
    assert lines[-1].startswith(f"{bad_two}:"), raw


def test_a_query_whose_names_its_own_file_wrote_is_taken_by_an_empty_snapshot(
    tmp_path: Path,
) -> None:
    """A snapshot may name no table at all: a query whose every name reads from a source the file
    itself wrote resolves against it, and the command still exits 0."""
    schema = _snapshot(tmp_path, {})
    for name in (
        "values-column-list-names-a-source",
        "table-function-holds-what-its-alias-qualifies",
    ):
        dialect, text = GOOD[name]
        path = _file(tmp_path, f"{name}.sql", text)
        code, lines, raw, _ = _run([path], tmp_path, dialect=dialect, schema=schema)
        assert code == 0, raw
        assert lines == [], raw
        assert check_text(path, text, dialect, {}) == [], text


def test_a_check_of_nothing_to_check_exits_0(tmp_path: Path) -> None:
    """A file that holds no query at all, a file of no text at all, and a snapshot that names no
    table are a check with nothing to say: exit 0, and no line."""
    for name in sorted(GOOD):
        dialect, text = GOOD[name]
        path = _file(tmp_path, f"{name}.sql", text)
        code, lines, raw, _ = _run([path], tmp_path, dialect=dialect)
        assert code == 0, raw
        assert lines == [], raw
    empty = _file(tmp_path, "nothing.sql", GOOD["empty-file"][1])
    code, lines, raw, _ = _run([empty], tmp_path, schema=_snapshot(tmp_path, {}))
    assert code == 0, raw
    assert lines == [], raw


def test_a_dialect_sqlglot_does_not_know_is_refused_without_a_traceback(tmp_path: Path) -> None:
    """A dialect sqlglot does not know is an error in how the command was run: it is named on
    stderr as exit 2, before any file is read, so no file is checked under a dialect no one
    named."""
    good = _file(tmp_path, "good.sql", GOOD["two-statements"][1])
    name = "no_such_dialect"
    code, lines, raw, err = _run([good], tmp_path, dialect=name)
    assert code == 2, (raw, err)
    assert lines == [], raw
    assert name in err, err


def test_a_file_that_is_not_there_is_named_and_not_a_crash(tmp_path: Path) -> None:
    """A path the command cannot read is one problem line naming it, and not a traceback and not
    a pass."""
    missing = str(tmp_path / "absent.sql")
    code, lines, raw, _ = _run([missing], tmp_path)
    assert code == 1, raw
    assert len(lines) == 1, raw
    assert lines[0].startswith(f"{missing}:"), raw
    assert "cannot be read" in lines[0], raw


def test_a_file_that_is_not_text_is_refused_without_a_traceback(tmp_path: Path) -> None:
    """A file that is not UTF-8 text is named as a problem, and never read as SQL."""
    path = tmp_path / "not-text.sql"
    path.write_bytes(NOT_UTF8)
    code, lines, raw, _ = _run([str(path)], tmp_path)
    assert code == 1, raw
    assert len(lines) == 1, raw
    assert lines[0].startswith(f"{path}:"), raw
    assert "is not UTF-8" in lines[0], raw


def test_a_failure_inside_the_check_is_a_problem_and_not_a_traceback(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A file whose check breaks in a way no SQL error covers is still one problem line naming the
    file, so a defect inside the check can neither crash the command nor pass the file."""
    import saddle.sqlcheck as module

    failure = "the check broke"

    def broken(*args: Any, **kwargs: Any) -> list[SqlProblem]:
        raise RuntimeError(failure)

    monkeypatch.setattr(module, "check_text", broken)
    path = _file(tmp_path, "broke.sql", GOOD["two-statements"][1])
    problems = check_file(path, "duckdb", dict(SCHEMA))
    assert len(problems) == 1, problems
    assert problems[0].message.startswith("cannot be checked: RuntimeError"), problems
    code, lines, raw, _ = _run([path], tmp_path)
    assert code == 1, raw
    assert lines != [], raw
    assert lines[0].startswith(f"{path}:"), raw


def test_the_sql_check_command_refuses_a_bad_file(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """`saddle sql-check --dialect D --schema S PATH...` is wired into `main`: a bad file prints
    the miss line the check says and exits 1, and a good file exits 0 saying nothing."""
    dialect, text = _fixture("misspelt-column")
    bad = _file(tmp_path, "bad.sql", text)
    good = _file(tmp_path, "good.sql", GOOD["two-statements"][1])
    snapshot = _snapshot(tmp_path)
    assert main(["sql-check", "--dialect", dialect, "--schema", snapshot, bad]) == 1
    report = capsys.readouterr().out
    assert report.splitlines() != [], report
    assert _misses(report.splitlines()) == _expected("misspelt-column"), report
    assert main(["sql-check", "--dialect", "duckdb", "--schema", snapshot, good]) == 0
    assert capsys.readouterr().out == ""


def test_the_sql_check_command_helps_before_it_takes_a_path(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """`--dialect` and `--schema` are both needed, and at least one path: the command says so
    rather than checking nothing or guessing a dialect. Each is refused on its own, with the name
    of the flag that is missing, so a run that leaves one out is stopped before it reads a file
    under a dialect or a snapshot the person never named."""
    for missing, named in (
        ("--dialect", ["sql-check", "a.sql"]),
        ("--schema", ["sql-check", "a.sql", "--dialect", "duckdb"]),
    ):
        with pytest.raises(SystemExit) as raised:
            main(named)
        assert raised.value.code == 2, (missing, capsys.readouterr())
        assert missing in capsys.readouterr().err, (missing, named)
    with pytest.raises(SystemExit) as raised:
        main(["sql-check", "--dialect", "duckdb", "--schema", "s.json"])
    assert raised.value.code == 2
    assert "paths" in capsys.readouterr().err


def test_the_sql_check_command_says_what_each_option_takes(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """`sql-check --help`, and the top level's listing of it, say what the command does and what
    each option takes: the metavar of every flag says which value it wants, its own description
    says that a name the snapshot does not hold is refused where it starts and that a file SQL
    does not read is refused, and the snapshot option says what a snapshot is."""
    with pytest.raises(SystemExit) as raised:
        main(["sql-check", "--help"])
    assert raised.value.code == 0
    said = " ".join(capsys.readouterr().out.split())
    for words in (
        "sql-check [-h] --dialect D --schema S.json paths [paths ...]",
        "--dialect D The SQL dialect to parse with, as sqlglot names it",
        "--schema S.json The schema snapshot: a JSON object mapping each table name to the list "
        "of its column names",
        "The .sql files to check.",
    ):
        assert words in said, (words, said)
    with pytest.raises(SystemExit) as raised:
        main(["--help"])
    assert raised.value.code == 0
    listed = " ".join(capsys.readouterr().out.split())
    for words in (
        "sql-check Resolves every table and column named in the .sql files you name",
        "A name the snapshot does not hold is refused at the line and column it starts at",
        "A file that does not parse is refused",
    ):
        assert words in listed, (words, listed)


def test_the_sql_check_command_names_sqlglot_when_it_is_missing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """A command run where the `sql` extra is not installed says which extra it needs, as exit 2,
    on the stream errors go to, and does not crash on the import."""
    monkeypatch.setitem(sys.modules, "saddle.sqlcheck", None)
    path = _file(tmp_path, "a.sql", GOOD["two-statements"][1])
    code = main(["sql-check", "--dialect", "duckdb", "--schema", _snapshot(tmp_path), path])
    assert code == 2, capsys.readouterr()
    said = capsys.readouterr().err
    assert "saddle sql-check needs sqlglot" in said, said
    assert "saddle[sql]" in said, said


def test_the_sql_check_command_needs_no_model_and_no_server(tmp_path: Path) -> None:
    """The check needs no model, no server and no database: one file of SQL, one file of JSON and
    a dialect name are the whole of what it reads, so it runs where nothing else runs."""
    good = _file(tmp_path, "good.sql", GOOD["two-statements"][1])
    assert main(["sql-check", "--dialect", "duckdb", "--schema", _snapshot(tmp_path), good]) == 0


# -- Scopes and output names, held to sqlite3 (review of the first run, 2026-10-08) -----------
#
# The run's tests held every rule above, and an engine still disagreed with 6 of 8 queries:
# output names in ORDER BY, GROUP BY and HAVING were refused, and an unqualified column read
# sources outside its own query. Each rule below is read both ways, and the differential test
# at the end lets sqlite3 decide what resolves.


def test_a_select_alias_is_read_by_the_clauses_that_read_output_names() -> None:
    """Known-bad before the fix: `ORDER BY doubled` of `SELECT ... AS doubled` read as an
    unknown column, though every SQL engine runs it. Only the query's own clauses read an
    output name: a window's `OVER (ORDER BY a)` does not, and sqlite3 refuses it there."""
    _text_case("SELECT id * 2 AS doubled FROM orders ORDER BY doubled;\n", [])
    _text_case("SELECT user_id AS who, count(*) FROM orders GROUP BY who;\n", [])
    _text_case("SELECT user_id, count(*) AS n FROM orders GROUP BY user_id HAVING n > 1;\n", [])
    _text_case("SELECT id AS a FROM users UNION SELECT id FROM orders ORDER BY a;\n", [])
    _text_case("SELECT id * 2 AS doubled FROM orders ORDER BY doubeld;\n", [("column", "doubeld")])
    _text_case("SELECT id AS a, row_number() OVER (ORDER BY a) FROM users;\n", [("column", "a")])


def test_a_where_reads_an_output_name_as_sqlite3_and_duckdb_do() -> None:
    """A loosening on the record: Postgres and MySQL refuse an output name in a WHERE, and
    this check lets it through, rather than refuse a file sqlite3 and DuckDB run."""
    _text_case("SELECT id * 2 AS doubled FROM orders WHERE doubled > 1;\n", [])


def test_a_cte_body_does_not_read_the_from_of_the_query_that_holds_it() -> None:
    """Known-bad before the fix: the CTE's `user_id` resolved against `orders`, a table of
    the query below the WITH, though `users` holds no such column."""
    _text_case(
        "WITH t AS (SELECT user_id FROM users) "
        "SELECT t.user_id FROM t JOIN orders ON orders.id = 1;\n",
        [("column", "user_id")],
    )
    _text_case(
        "WITH t AS (SELECT user_id FROM orders) "
        "SELECT t.user_id FROM t JOIN users ON users.id = 1;\n",
        [],
    )


def test_a_derived_table_does_not_read_the_sources_beside_it() -> None:
    _text_case(
        "SELECT s.user_id FROM (SELECT user_id FROM users) AS s, orders;\n",
        [("column", "user_id")],
    )
    _text_case("SELECT s.user_id FROM (SELECT user_id FROM orders) AS s, users;\n", [])


def test_a_query_reads_a_cte_only_through_its_from() -> None:
    """Known-bad before the fix, the forgotten JOIN: a CTE the query never names in its FROM
    lent it its columns."""
    _text_case(
        "WITH t AS (SELECT user_id FROM orders) SELECT name, user_id FROM users;\n",
        [("column", "user_id")],
    )
    _text_case(
        "WITH t AS (SELECT user_id FROM orders) "
        "SELECT name, user_id FROM users JOIN t ON t.user_id = users.id;\n",
        [],
    )


def test_a_lateral_source_reads_the_from_before_it() -> None:
    """Known-bad before the fix: a `LATERAL` derived table was registered with no name, so
    every column read through it was refused."""
    lateral = (
        "SELECT u.name, x.user_id FROM users AS u, "
        "LATERAL (SELECT user_id FROM orders WHERE orders.user_id = u.{}) AS x;\n"
    )
    _text_case(lateral.format("id"), [], dialect="postgres")
    _text_case(lateral.format("idd"), [("column", "idd")], dialect="postgres")


def test_an_unnamed_derived_table_lends_its_columns() -> None:
    _text_case("SELECT id FROM (SELECT id FROM users);\n", [])
    _text_case("SELECT name FROM (SELECT id FROM users);\n", [("column", "name")])


def test_a_table_read_under_an_alias_is_not_read_by_its_own_name() -> None:
    _text_case("SELECT u.name FROM users AS u;\n", [])
    _text_case("SELECT users.name FROM users AS u;\n", [("column", "name")])


def test_a_qualifier_resolves_in_any_case() -> None:
    """A survivor of the first run's review: a qualifier read case-sensitively passed every
    test, and refused `U.name` of `FROM users AS u`."""
    _text_case("SELECT U.name FROM users AS u;\n", [])
    _text_case("SELECT U.nmae FROM users AS u;\n", [("column", "nmae")])


def test_an_insert_query_does_not_read_the_table_it_fills() -> None:
    _text_case("INSERT INTO orders (id, user_id) SELECT id, id FROM users;\n", [])
    _text_case(
        "INSERT INTO orders (id, user_id) SELECT id, user_id FROM users;\n",
        [("column", "user_id")],
    )


#: Queries an engine judges: each must pass the check exactly when sqlite3 runs it against
#: tables shaped like `SCHEMA`. Good and bad are not labelled here; sqlite3 decides.
SQLITE_JUDGED: list[str] = [
    "SELECT u.name, o.user_id FROM users AS u JOIN orders AS o ON o.user_id = u.id;",
    "SELECT nmae FROM users ORDER BY id;",
    "SELECT name FROM orders;",
    "SELECT id * 2 AS doubled FROM orders ORDER BY doubled;",
    "SELECT user_id AS who, count(*) FROM orders GROUP BY who;",
    "SELECT user_id, count(*) AS n FROM orders GROUP BY user_id HAVING n > 1;",
    "SELECT id AS a FROM users UNION SELECT id FROM orders ORDER BY a;",
    "SELECT id * 2 AS doubled FROM orders WHERE doubled > 1;",
    "SELECT id AS a, row_number() OVER (ORDER BY a) FROM users;",
    "WITH t AS (SELECT user_id FROM users) SELECT t.user_id FROM t JOIN orders ON orders.id = 1;",
    "SELECT s.user_id FROM (SELECT user_id FROM users) AS s, orders;",
    "WITH t AS (SELECT user_id FROM orders) SELECT name, user_id FROM users;",
    "WITH a AS (SELECT id FROM users), b AS (SELECT id FROM a) SELECT id FROM b;",
    "WITH a AS (SELECT id FROM b), b AS (SELECT id FROM users) SELECT id FROM a;",
    "SELECT id FROM users WHERE EXISTS (SELECT 1 FROM orders WHERE orders.user_id = users.id);",
    "SELECT id, (SELECT count(*) FROM orders WHERE orders.user_id = users.id) FROM users;",
    "SELECT id FROM (SELECT id FROM users);",
    "SELECT users.name FROM users AS u;",
    "SELECT U.name FROM users AS u;",
]


def test_sql_check_agrees_with_sqlite3_on_what_resolves() -> None:
    """The enforcing engine's reading, not this module's: every query above passes the check
    exactly when sqlite3 runs it. Before the fix 6 of the first 8 such queries disagreed."""
    db = sqlite3.connect(":memory:")
    db.executescript(
        "".join(
            f"CREATE TABLE {table} ({', '.join(columns)});"
            for table, columns in SCHEMA.items()
            if columns
        )
    )
    both_ways = {True: 0, False: 0}
    disagree = []
    for query in SQLITE_JUDGED:
        try:
            db.execute(query).fetchall()
            runs = True
        except sqlite3.Error:
            runs = False
        both_ways[runs] += 1
        said = [problem.message for problem in check_text("q.sql", query, "sqlite", dict(SCHEMA))]
        if runs != (not said):
            disagree.append((query, "sqlite3 runs it" if runs else "sqlite3 refuses it", said))
    assert disagree == [], disagree
    assert both_ways[True] >= 5, both_ways  # the table holds queries sqlite3 runs
    assert both_ways[False] >= 5, both_ways  # and queries it refuses
