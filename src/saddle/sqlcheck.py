"""`saddle sql-check`: resolve the tables and columns of `.sql` files against a schema snapshot.

The contract, written once: `saddle sql-check --dialect D --schema S PATH...` parses every
query in each named `.sql` file with sqlglot in dialect D and resolves every table and
column against the schema snapshot S, a JSON object mapping each table name to the list of
its column names; it exits 0 when everything resolves and 1 otherwise, printing one line
per miss, `PATH:LINE:COL: unknown table NAME` or `PATH:LINE:COL: unknown column NAME`,
where LINE and COL are 1-based, counted in the whole file, and mark where the unknown name
itself starts — for a qualified column such as `u.total`, where `total` starts, after the
qualifier — and NAME is the bare name as written, `total` rather than `u.total`.

The pieces the contract leaves open, and what this module chose for each:

- A qualified column resolves only against the table or source its qualifier (or alias)
  names, and an unqualified one against the sources of its query; a column of another table
  does not resolve. A name that belongs to no snapshot is still resolved when a source the
  file wrote is known to hold it: through a CTE or derived table's output names, which a
  `SELECT x AS y` gives as `y`, and which its `AS t(a, b)` or `AS s(id)` renames outright;
  and through a star, since a `SELECT *`, a bare `*` or a qualified `u.*` says no name about
  what it outputs, so anything read from that source resolves.
- Every name resolves case-insensitively, as the snapshot spells it and as the file spells
  it, because SQL reads a bare name that way. A miss reports the name as the file wrote
  it, with its qualifier and its quotes taken off, and the file's own text is what the
  report names rather than a resolved spelling of it.
- A name a query registered is the source its columns name. A CTE wins its name over a
  snapshot table of the same name for as long as its statement lasts, and within a
  statement a name always selects a CTE, a derived table or a resolved table rather than a
  snapshot table. A CTE's body sees the CTEs declared before itself and not itself; a
  derived table's body sees the query around it, as a correlated subquery does, but not
  its own name. A recursive `WITH` is the one SQL lets a source name its own body, and
  what its body may read through that name is the column list the CTE writes over itself:
  a recursive arm's `walk.id` of `walk(id)` resolves and its `walk.user_id` does not. A
  `VALUES` table named with a column list — `FROM (VALUES (1, 2), (3, 4)) AS totals(id,
  amount)` — states the names it holds and names no table, so it is that list alone, as a
  derived table's own column list is, and a source named without one states nothing an
  unqualified column of that query may read. A `db` written before a table selects only
  the table's last name; neither the schema a table was written under nor a catalog is
  looked at.
- A statement that holds no query — a `CREATE`, a `DROP`, a `PRAGMA`, `ALTER`, a bare
  command — is not resolved at all, and neither is a name that appears only in a comment.
  A derived table whose own query missed a name says nothing about what its columns are,
  and a table the snapshot names with no columns holds no column. A `FROM` that reads a
  table function or a file rather than naming a table — `read_csv('x.csv') AS t` — names
  no table, so nothing is asked of the snapshot, and its alias states no columns of its
  own: a column qualified by it (`t.total`) is a name read from that source and resolves,
  while an unqualified `total` of the same query resolves against no source of it and is
  named.
- A file that does not tokenize or parse is one problem, named with the line and column the
  failure points at and the reason sqlglot gave; it never exits 0. A file that is not there
  or is not text, a snapshot that is not JSON, that is not an object, or whose value is not
  a list of names, and a dialect sqlglot does not know, are each refused the same way: the
  first three name their own file as exit 1, and an unknown dialect is a fault in how the
  command was run, so it goes to stderr as exit 2.

Nothing here needs a database or a model: a schema snapshot is a JSON file, and a run
resolves files against it without connecting anywhere. `check_text`, `check_file`,
`check_paths` and `load_schema` are the parts the tests reach, and `check_paths` is what
the `sql-check` command in `src/saddle/cli.py` calls, so the wiring itself is tested too.

Known-good: a two-statement file where every name resolves, aliases and joins, a CTE (also
one whose `AS t(a, b)` renames its output, one above a UNION, one named for a snapshot
table, and a recursive one), a derived table (including `SELECT *`, `AS s(id)`, one named
for a snapshot table and one under a `FULL OUTER JOIN`), a `VALUES` table named with a
column list, a `FROM` that reads a file, a `SELECT` column renamed by its
alias, correlated subqueries qualified by a table of the query around them, an `INSERT` of
a column list of values and of a `SELECT`, a column list naming a CTE, `UPDATE` (aliased,
and one taking its columns from another table), `DELETE`, UNIONs, a bare `*` and a qualified
`u.*`, output names a `WHERE`, `GROUP BY`, `HAVING`, `ORDER BY` or `LIMIT` reads, quoted
names (also a quoted spelled name, backticks, and a quoted alias), a name written in another
case than the snapshot's, a table that is a name chain, a `FROM` that names no table, a
comment naming names no query reads, statements that hold no query, a table the snapshot
names without columns, and an empty file. Known-bad: a misspelt table and a misspelt column
(in a later statement too), one query with several misses, the same misspelt name in two
statements and twice in one query, a column of another table, a column qualified by a source
that does not hold it and by a name that names no source, a column of a table the snapshot
names without columns, a dotted column that is a chain still resolved against its table, a
column not among a CTE's or a derived table's output names, a miss inside a CTE body or a
derived table body, a column not among a named `VALUES` source's own list and a column
named under a `FROM` that names no table, a recursive CTE column its own list does not name
and a CTE name its own body may not read, misspellings on a UNION side, a column of a name
that is no table, a subquery that lends nothing to the query around it, text that tokenizes
wrong and text that
does not parse, a file that is not there and one that is not text, a snapshot that is not
JSON, that is not an object, or whose value is not a list of names, and a dialect sqlglot
does not know.

Contract mutants, each applied to this file or to `src/saddle/cli.py`, the file confirmed
changed and the run confirmed red before the source was restored (`tests/test_sql_check.py`
is the run, and fixtures live in `tests/sql_check_fixtures.py` as GOOD and BAD):

A. Count a position 0-based, as sqlglot counts an offset, instead of as a person counts a
   line and column: killed by every BAD fixture, whose line and column count from 1, and
   directly by `test_a_name_is_at_the_line_and_column_where_its_own_spelling_starts`, which
   reads both numbers back off the text.
B. Report the position of a name's qualifier instead of of the name itself: killed by
   `test_a_qualified_column_is_reported_after_its_qualifier`, whose known-bad fixture is
   `SELECT u.name FROM orders AS u` with the miss at the column `name` starts at, and by
   every qualified fixture read through `test_a_report_points_at_where_the_file_spells_the_name`.
C. Take a miss's position from the first spelling of that name in the file instead of the
   spelling the check resolved: killed by
   `test_a_name_misspelt_in_two_statements_reports_each_statement` (whose known-bad fixture
   gives the second statement's own line), by
   `test_a_query_that_misses_one_name_twice_reports_both_spellings`, and by every quoted
   fixture, whose miss reports its own opening quote and not the bare spelling earlier in
   the file.
D. Read a snapshot name as case-sensitive: killed by
   `test_names_resolve_case_insensitively_and_are_reported_as_written`.
E. Resolve a qualified column against every table of its query instead of only the source its
   qualifier names: killed by `test_a_qualified_column_resolves_only_against_the_source_it_names`,
   by `test_a_column_of_another_table_does_not_resolve`, and by
   `test_a_dotted_qualified_column_does_not_walk_a_name_chain`.
F. Let a column resolve against nothing but its nearest query: killed by
   `test_a_correlated_subquery_reads_the_tables_of_the_query_it_sits_in`, and by the CTE and
   derived-table tests, whose bodies read the tables the query above them registered.
G. Read a name a query registered as a snapshot table instead of as the source that query
   wrote: killed by `test_a_named_source_states_its_columns_or_names_no_column`, whose
   known-good half names `unknown table notes` and `unknown column id` once the registration
   is gone, by the GOOD fixture `cte-wins-its-name-over-a-snapshot-table`, and by both
   `VALUES` tests.
H. Name only the first miss of a file: killed by `test_a_query_with_several_misses_names_each_one`
   and by every BAD fixture that holds more than one miss.
I. Resolve a name written by a statement that holds no query: killed by
   `test_a_statement_that_holds_no_query_is_not_resolved`, and by the `named` `VALUES` sources
   of `test_a_check_of_nothing_to_check_exits_0`, which the statement filter hands to the
   resolver as an expression with no table.
K. Read a declared column list — `AS t(a, b)`, `AS s(id)`, a named `VALUES` list — as no part
   of what the source holds: killed by `test_a_named_values_source_holds_only_its_own_column_list`
   and by `test_a_declared_column_list_replaces_the_names_its_body_writes`.
L. Hide even a recursive CTE from its own body: killed by
   `test_a_recursive_cte_holds_only_its_own_column_list`, whose known-good half reports
   `unknown table walk` and `unknown column id`.
M. Keep every `WITH` open to its own body: killed by that test's known-bad fixture
   `WITH t(id) AS (SELECT id FROM t)`, which resolves as a pass.
N. Take the reach of a `FROM` that names no table away: killed by
   `test_a_named_source_states_its_columns_or_names_no_column`, whose known-good half reports
   `unknown column total` for `file.total` of `read_csv('totals.csv') AS file`.
O. Let an unqualified column read that alias, which states no columns of its own: killed by
   that test's known-bad half, `SELECT total FROM read_csv('totals.csv') AS file`.
P. Report the path the check resolved rather than the path the command was given: killed by
   `test_the_path_is_printed_as_it_was_given`, which spells one file four ways.
Q. Name every path given, good or bad: killed by
   `test_of_a_good_file_and_a_bad_file_only_the_bad_one_is_named`.
R. Accept a snapshot that is not an object of name lists: killed by
   `test_a_snapshot_that_is_not_an_object_of_name_lists_is_refused`, whose four known-bad
   snapshots are not JSON, not an object, not a list of names, and not there at all.
S. Leave `sql-check` out of the commands `main` handles: killed by
   `test_the_sql_check_command_refuses_a_bad_file`.
T. Treat a missing sqlglot as a pass: killed by
   `test_the_sql_check_command_names_sqlglot_when_it_is_missing`.

Two further readings the tests hold, each of which the code guards with a branch no single
mutant names: a file that does not parse is never a pass
(`test_a_file_that_does_not_parse_never_exits_0`, over a parse failure and a tokenize
failure); and a file or snapshot that cannot be read, a file that is not text, a check that
breaks inside, and a dialect sqlglot does not know are each named as a problem rather than
crashing the command or passing the file.
"""

from __future__ import annotations

import json
import re
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import IO, Final

from sqlglot import exp
from sqlglot.dialects.dialect import Dialect
from sqlglot.errors import SqlglotError

#: What sqlglot writes after its own reason: the line and column it stopped at, in its
#: own words. Taken off the reason, because the line and column of a problem are the
#: ones `not_parsable` names.
SQLGLOT_POSITION: Final = re.compile(r"\. Line \d+, Col: \d+\.\Z")

#: The statements that hold a query whose tables and columns resolve. Everything else
#: a file may hold (`CREATE`, `DROP`, `PRAGMA`, an `ALTER`, a `Command`) is taken as it is.
QUERY_STATEMENTS: Final = (exp.Select, exp.SetOperation, exp.Insert, exp.Update, exp.Delete)

#: The name a star writes for the columns of a `SELECT *`, a bare `*` or a qualified `u.*`.
#: Such a source says no name about what it outputs, so every name read from it resolves.
STAR: Final = "*"

#: What a snapshot must be, spelled once so the refusal says what a snapshot is.
NOT_A_SNAPSHOT: Final = "it is not an object mapping each table name to its column names"

#: The columns one query's scope holds, under the name each source is read by here, and
#: what each of them outputs: tables resolved here, CTEs and derived tables named here, and
#: whatever the queries around this one left visible to it.
Context = dict[str, list[str]]

#: A query that can name sources of its own. A CTE's body is one, so it is resolved as its
#: own query rather than as the node that names it.
Query = exp.Query | exp.Subquery | exp.CTE

#: A source a query names in its FROM: a CTE, a derived table, or a VALUES table written with
#: a column list of its own (`FROM (VALUES (1), (2)) AS t(id)`). A CTE resolves as its own
#: query, through its body; the other two are resolved as they are written.
Source = exp.CTE | exp.Subquery | exp.Values

#: A statement or query the check resolves. sqlglot keeps its abstract `Query` apart from
#: `Expression`, so the two together name every node a file's text can hand to `_query`: a
#: `SELECT`, a set operation, an `INSERT`, an `UPDATE`, a `DELETE`, or a query nested in one.
Statement = exp.Expression | exp.Query


@dataclass(frozen=True)
class SqlProblem:
    """One way one file fails: where in the file it fails, and why.

    A problem with no line and column is a whole-file failure, such as a file that is not
    there, is not text, or breaks the check in a way no SQL error covers, so it is named by
    its path alone.
    """

    path: str
    message: str
    line: int | None = None
    column: int | None = None

    def __str__(self) -> str:
        if self.line is None or self.column is None:
            return f"{self.path}: {self.message}"
        return f"{self.path}:{self.line}:{self.column}: {self.message}"

    def order(self) -> tuple[str, int, int, str]:
        """Sort a file's problems by where they are in it, then by what they say.

        Line first, then column: the order a person running their eye down the file meets
        them in. A later line whose miss starts in an earlier column still comes after an
        earlier line's, which is not the order a sort on the two numbers alone gives.
        """
        return (self.path, self.line or 0, self.column or 0, self.message)


def _line_and_column(text: str, start: int) -> tuple[int, int]:
    """Where an offset into a file reads as a person counts: 1-based line and column."""
    return text.count("\n", 0, start) + 1, start - text.rfind("\n", 0, start)


def _written(spelled: exp.Expression, text: str) -> tuple[int, int]:
    """Where on the file this name was written: its line, and the 1-based column its own
    spelling starts at.

    The parser records, on every name it read, the offset in the file it read it at, counted
    from 0 over the whole text of a file of several statements, so each miss is named at the
    spelling of the name the check resolved and never at the spelling of another name that
    happens to read the same: `SELECT 'a' FROM t WHERE t.bogus = 'bogus'` names its miss on
    the second line, not on the string that reads `bogus`, and a quoted name is read at its
    opening quote. `_names_of` collects only names the parser read, so the offset is always
    there, and a name the parser invented — the `*` of `u.*` — is never collected. A name
    read from a CTE, a derived table or an `INSERT` column list was written as a bare
    Identifier, which is where its own columns are named.
    """
    return _line_and_column(text, spelled.meta.get("start", 0))


def _names_of(
    node: Statement,
    columns: list[tuple[str, str, exp.Expression]],
    tables: list[exp.Table],
    output: list[exp.CTE],
    contexts: list[Source],
    nested: list[Statement],
) -> None:
    """The names one query writes, at its own level.

    The tables and columns of its direct scope, the `SELECT` or set operation that is a
    nested query of it (the source of an `INSERT ... SELECT`, a side of a `UNION` that is
    not a statement of its own, the query in a scalar subquery), and the CTEs and derived
    tables that name a source here. The names under one of those belong to that query, which
    resolves them itself; only the tables and columns of this level are collected here. A
    `Schema`'s list — `INSERT INTO users (id, name)` — writes a column as a bare name rather
    than as a column node, so it is collected there as a name no source qualifies. A star
    column (`u.*`, `*`) writes no name that can miss, and is not collected. A `TABLE('x')`
    is no table node at all, so nothing here is collected for it either. Each column comes
    with the name the query wrote it as, so a miss reports the name where it was written.
    """
    for child in node.iter_expressions():
        if isinstance(child, exp.Column):
            if child.name != STAR and isinstance(child.this, exp.Identifier):
                columns.append((child.table, child.name, child.this))
        elif isinstance(child, exp.Table):
            tables.append(child)
        elif isinstance(child, exp.CTE):
            output.append(child)
        elif isinstance(child, exp.Subquery):
            contexts.append(child)
        elif isinstance(child, exp.Values) and child.args.get("alias") is not None:
            # A VALUES table named with a column list is registered as a source of its own,
            # because that list is what its columns are.
            contexts.append(child)
        elif isinstance(child, (Query, Source)):
            nested.append(child)
        elif isinstance(child, exp.Schema):
            columns.extend(
                ("", name.name, name)
                for name in child.expressions
                if isinstance(name, exp.Identifier)
            )
            _names_of(child, columns, tables, output, contexts, nested)
        else:
            _names_of(child, columns, tables, output, contexts, nested)


def _source(qualifier: str, enclosing_scope: Sequence[Context]) -> list[str] | None:
    """The columns of the source this name names, nearest scope first.

    A qualifier names one source, so a column qualified by it resolves against that source's
    columns alone, and `None` when it names nothing the query can read.
    """
    for context in enclosing_scope:
        if qualifier in context:
            return context[qualifier]
    return None


def _union(*sources: list[str]) -> list[str]:
    """The columns of a table named under two names: one table is one source, so a column
    held under any of those names is a column of it."""
    return list(dict.fromkeys(column for columns in sources for column in columns))


def _output_names(source: Source) -> list[str]:
    """The column names one registered source holds: the names it writes over its own body
    (`AS t(total, amount)`, over a query or over a VALUES table), or the names its body writes
    out. A body that writes neither — a derived table whose `FROM` is a table function — states
    no names.

    `alias_column_names` and `named_selects` are sqlglot's two attributes for the two ways a
    source's columns are known. They are read separately because neither covers every node that
    can name a source: only a node with a `TableAlias` — a CTE or a derived table — declares
    names, and only a `Query` writes named selects.
    """
    declared = _declared_names(source)
    return declared or _named_selects(source)


def _declared_names(source: Source) -> list[str]:
    """The names a source declares over its body, as `AS t(a, b)` does, or nothing when it
    declares none. `alias_column_names` is sqlglot's own name for that list."""
    return list(source.alias_column_names)


def _named_selects(source: Source) -> list[str]:
    """The names a source's own query writes out, or nothing when its body is no query."""
    body = source.this if isinstance(source, exp.CTE) else source
    if isinstance(body, (exp.Query, exp.Subquery)):
        return list(body.named_selects)
    return []


def _recursive(statement: Statement) -> list[str]:
    """The CTE names a recursive `WITH` names for its own body.

    A recursive query is the one case where SQL lets a source read its own name, so the name a
    hidden body cannot see is put back for it, holding the names the CTE writes. sqlglot records
    recursion on the `WITH` node, under the key `with_` a query keeps it at.
    """
    spelled: object = statement.args.get("with_")
    if isinstance(spelled, exp.With) and spelled.args.get("recursive"):
        return [named.alias_or_name.lower() for named in spelled.expressions]
    return []


def _query(
    statement: Statement,
    schema: dict[str, list[str]],
    enclosing: Sequence[Context],
    misses: list[tuple[str, str, exp.Expression]],
) -> None:
    """Resolve one query, and every query nested in it, against the snapshot.

    `misses` gathers what does not resolve, as (`table` or `column`, the name as it is
    reported, the name as the query wrote it), so each miss carries its own position.

    Sources named here register before any name of this query resolves, so a CTE wins its
    name over a snapshot table of the same name. A body registered here resolves only after
    this query's tables, which is what lets a correlated subquery read the tables of the
    query it sits in while its own tables stay out of that scope, and it resolves with its
    own name held back from itself — except in a recursive `WITH`, where SQL lets a source read
    its own name — so a CTE cannot read its own output names and a derived table's `SELECT bogus`
    is a column of that body's own FROM rather than an output name it lends to itself, while a
    source named beside it is one its body can read; a body that resolved before this query's
    tables would report its miss as a miss of this query instead. Tables resolve before columns,
    so the source a qualified column names is registered by the time the column reads it. A set
    operation needs no branch of its own: its sides and its `WITH` are children of it, so a
    side resolves as a nested query against the CTEs registered here.
    """
    columns: list[tuple[str, str, exp.Expression]] = []
    tables: list[exp.Table] = []
    output: list[exp.CTE] = []
    contexts: list[Source] = []
    nested: list[Statement] = []
    _names_of(statement, columns, tables, output, contexts, nested)
    registered: list[Source] = [*output, *contexts]

    sources: Context = {}
    unstated: list[str] = []
    for named in registered:
        alias = named.alias_or_name.lower()
        if not alias:
            continue
        # A source's output names are what its own query says it holds: a star writes no name
        # and holds every name read from that source, and a column renamed by its alias is
        # held under its new name, so reading the old name from it is a miss.
        outputs = _output_names(named)
        sources[alias] = [STAR] if STAR in outputs else [written.lower() for written in outputs]

    for table in tables:
        # A table's own name is what the snapshot holds: a `db.` or a catalog written before
        # it is not part of the snapshot, and the alias is what the query's columns are named
        # under, so a resolved table holds them under both names.
        name, alias = table.name.lower(), table.alias_or_name.lower()
        spelled = table.this
        if not isinstance(spelled, exp.Identifier):
            # A FROM that reads a table function or a file rather than naming a table —
            # `read_csv('x.csv') AS t` — names nothing the snapshot is asked about, so its
            # alias resolves whatever is qualified by it. That reach is kept to columns
            # qualified by it: an unqualified name of the same query still resolves only
            # against the sources that state their columns.
            unstated.append(alias)
            continue
        columns_of = _source(name, (sources, *enclosing))
        if columns_of is None:
            columns_of = schema.get(name)
        if columns_of is None:
            misses.append(("table", table.name, spelled))
            continue
        sources[name] = columns_of if alias == name else _union(columns_of, sources.get(name, []))
        if alias != name:
            sources[alias] = _union(columns_of, sources.get(alias, []))

    scope = (sources, *enclosing)
    recursive = _recursive(statement)
    for named in registered:
        # A source's own name is out of reach while its own body resolves, so a name qualified
        # by it is a name the body must read from elsewhere: a `SELECT s.bogus` inside
        # `AS s (...)` is not a column `s` is in the middle of writing. Each name is put back
        # as soon as that body has resolved, which is what lets a CTE read a CTE named beside
        # it in the same `WITH`, and a recursive `WITH` names its source for its own body, as
        # SQL does.
        own = named.alias_or_name.lower()
        own_names: list[str] | None = sources.pop(own, None) if own else None
        if own_names is not None and own in recursive:
            sources[own] = own_names
        body: Statement = named.this if isinstance(named, exp.CTE) else named
        _query(body, schema, scope, misses)
        if own_names is not None:
            sources[own] = own_names

    every = list(
        dict.fromkeys(
            column for context in scope for columns in context.values() for column in columns
        )
    )
    for qualifier, name, written in columns:
        # A qualified column resolves only against the source its qualifier names, so a
        # qualifier naming nothing the query can read resolves nothing, whatever its other
        # tables hold. An unqualified column resolves against every source of its query. A
        # source that states no columns — the alias of a `FROM` that reads a table function
        # rather than naming a table — holds what is qualified by it, so a column named under
        # it is not a miss; a name in such a chain is a qualifier of this kind even though no
        # table the snapshot might hold is named by it.
        qualified = _source(qualifier.lower(), scope) if qualifier else None
        if qualified is not None:
            held: Sequence[str] = qualified
        else:
            # An unqualified column reads every column this query's sources state, and a column
            # qualified by a name that states no column of its own reads only what is qualified
            # by it, because nothing else can be a column of a thing whose columns are unknown.
            held = every if not qualifier else [STAR] if qualifier.lower() in unstated else []
        if STAR in held or name.lower() in held:
            continue
        misses.append(("column", name, written))

    for query in nested:
        _query(query, schema, scope, misses)


def not_parsable(path: str, exc: SqlglotError, text: str) -> SqlProblem:
    """The file that does not read, at the line and column the failure points at.

    A parse failure carries its own line and column on `errors`; a tokenize failure names
    only an offset into the text (or nothing), so that offset becomes a line and a column
    here, the way a person counts them. Either way the file has a problem, and a file that
    does not parse never ends this command in a pass. The reason is what sqlglot says, with
    the position sqlglot wrote after it taken off, because the line and column of the
    problem are the ones named here.
    """
    errors: list[dict[str, int | None]] = getattr(exc, "errors", None) or []
    if errors:
        line, column = errors[0]["line"] or 1, errors[0]["col"] or 1
    else:
        start: int = getattr(exc, "start", None) or 0
        line, column = _line_and_column(text, start)
    reason = SQLGLOT_POSITION.sub("", _first_line(str(exc)))
    return SqlProblem(path, f"does not parse: {reason} Line {line}, Col: {column}.", line, column)


def check_text(
    path: str, text: str, dialect: str, schema: dict[str, list[str]]
) -> list[SqlProblem]:
    """Every name of every query of one file's text that the snapshot does not hold.

    The file's statements are what sqlglot's own parser split from the text, so each miss
    takes its line and column from a name of its own statement: a miss in a later statement
    of a multi-statement file reports its own line. A file that does not tokenize or parse
    is one problem, at the position the failure names. Problems come back ordered by where
    they are in the file, so the report reads top to bottom.
    """
    try:
        statements = Dialect.get_or_raise(dialect).parse(text)
    except SqlglotError as exc:
        return [not_parsable(path, exc, text)]
    problems: list[SqlProblem] = []
    for statement in statements:
        if statement is None or not isinstance(statement, QUERY_STATEMENTS):
            continue
        misses: list[tuple[str, str, exp.Expression]] = []
        _query(statement, schema, (), misses)
        for kind, name, written in misses:
            line, column = _written(written, text)
            problems.append(SqlProblem(path, f"unknown {kind} {name}", line, column))
    return sorted(set(problems), key=SqlProblem.order)


def load_schema(path: str) -> tuple[dict[str, list[str]], SqlProblem | None]:
    """The snapshot `--schema S` names, or the problem with that file.

    A snapshot is an object mapping each table name to a list of column names; a file that
    is not there, not readable, not JSON, or not that shape is named here, so a run that
    cannot hold its files against the names it was given is not a run that found nothing
    wrong. A snapshot the check cannot build is empty, and a run that has one always has a
    problem named first. Lowercased on the way in, because a name resolves case-insensitively
    and reports as written.
    """
    try:
        raw = Path(path).read_text(encoding="utf-8-sig")
    except OSError as exc:
        return {}, SqlProblem(path, f"cannot be read: {exc.strerror or exc}")
    try:
        loaded = json.loads(raw)
        if not isinstance(loaded, dict) or not all(
            isinstance(columns, list) and all(isinstance(column, str) for column in columns)
            for columns in loaded.values()
        ):
            raise ValueError(NOT_A_SNAPSHOT)
    except (ValueError, TypeError) as exc:
        return {}, SqlProblem(path, f"is not a schema snapshot: {_first_line(exc)}")
    schema = {
        name.lower(): [column.lower() for column in columns] for name, columns in loaded.items()
    }
    return schema, None


def _first_line(reason: object) -> str:
    """What a failure says, on one line: the report is one line per problem."""
    return str(reason).splitlines()[0]


def check_file(path: str, dialect: str, schema: dict[str, list[str]]) -> list[SqlProblem]:
    """Every unresolved name of one file: read it, then resolve the text that is there.

    A file that is not there, is not text, or breaks the check in a way no SQL error covers
    is named as a problem, so a file never ends the command in a traceback and never ends it
    in a pass either.
    """
    try:
        text = Path(path).read_text(encoding="utf-8-sig")
    except OSError as exc:
        return [SqlProblem(path, f"cannot be read: {exc.strerror or exc}")]
    except UnicodeDecodeError as exc:
        return [SqlProblem(path, f"is not UTF-8 text: {exc.reason}")]
    try:
        return check_text(path, text, dialect, schema)
    except Exception as exc:
        return [SqlProblem(path, f"cannot be checked: {type(exc).__name__}: {exc}")]


def check_paths(
    paths: Sequence[str], *, dialect: str, schema: str, stdout: IO[str], stderr: IO[str]
) -> int:
    """`saddle sql-check --dialect D --schema S PATH...`: print every miss, and 1 if any.

    The path in each line is the path as it was given on the command line, so the line a
    person can paste into their shell is the line the miss is in. Of the paths given, only
    the ones with a problem are named. A snapshot that cannot be read or is not a snapshot,
    and a dialect sqlglot does not know, are named before any file is checked, and no file
    is checked: a run that cannot hold its files against the names it was given is not a run
    that found nothing wrong. An unknown dialect is an error in how the command was run, not
    a miss in a file, so it goes to stderr as exit 2, where a miss to a file or a bad
    snapshot exits 1.
    """
    try:
        Dialect.get_or_raise(dialect)
    except ValueError as exc:
        print(f"error: {exc} (--dialect {dialect!r})", file=stderr)
        return 2
    resolved, problem = load_schema(schema)
    if problem is not None:
        print(problem, file=stdout)
        return 1
    failed = False
    for path in paths:
        problems = check_file(path, dialect, resolved)
        if problems:
            failed = True
            for problem in problems:
                print(problem, file=stdout)
    return 1 if failed else 0
