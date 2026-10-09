"""The `.sql` files `saddle sql-check` is judged on, as the text of each file.

Each GOOD entry is a file whose every table and column the snapshot holds: it must exit
0 and print no miss. Each BAD entry is (text, each miss reported): the word naming the
kind of miss (`table` or `column`), the name as the check reports it, and the 1-based
line and column where that name itself starts when the whole file is counted — for a
qualified column such as `u.total`, after the qualifier, where `total` starts, and for a
quoted name, at its opening quote. A BAD entry whose misses are empty is text SQL does
not read at all, a quote that never closes or a statement with no expression where one
is required; its test only needs the command to refuse it on a line naming the file, the
reason and a line and column, because a file that does not parse never exits 0.

The names here are generic (`users`, `orders` and misspellings of them); every quoted
string inside a fixture is an ordinary SQL string literal or identifier.
"""

from __future__ import annotations

#: The schema snapshot these files resolve against: one JSON object mapping each table
#: name to the list of its column names, which is the shape `--schema` names.
SCHEMA: dict[str, list[str]] = {
    "users": ["id", "name"],
    "orders": ["id", "user_id"],
    # A table the snapshot names with no columns at all: known, and holding nothing.
    "logs": [],
}

#: A SQL file that is not text at all.
NOT_UTF8 = b"\xff\xfe\xff\x00not text\n"

#: Snapshots a run must refuse: not JSON at all, JSON that is not an object, an object
#: whose value is not a list, and a list whose entry is not a name.
BAD_SNAPSHOTS: dict[str, str] = {
    "not-json": "users: [id, name",
    "not-an-object": '["users", "orders"]',
    "value-not-a-list": '{"users": "id name"}',
    "entry-not-a-name": '{"users": ["id", 1]}',
}

GOOD: dict[str, tuple[str, str]] = {
    # Two statements, every name held.
    "two-statements": ("duckdb", "SELECT id, name FROM users;\nSELECT id FROM orders;\n"),
    # Column and table aliases, and a join whose ON names columns of both sides.
    "aliases-and-joins": (
        "duckdb",
        "SELECT u.name, o.id\nFROM users AS u\nJOIN orders AS o ON o.user_id = u.id;\n",
    ),
    # A CTE names a source; the query above it reads its output names.
    "cte-names-a-source": (
        "duckdb",
        "WITH recent AS (SELECT id, name FROM users)\nSELECT name FROM recent;\n",
    ),
    # `AS t(x, y)` renames the CTE's output, and the query above reads the new names.
    "cte-columns-renamed": (
        "duckdb",
        "WITH t(x, y) AS (SELECT id, name FROM users)\nSELECT x, y FROM t;\n",
    ),
    # A CTE above a UNION: both of its sides read it.
    "cte-above-a-union": (
        "duckdb",
        "WITH t AS (SELECT id FROM users)\nSELECT id FROM t UNION SELECT id FROM orders;\n",
    ),
    # A CTE named for a snapshot table wins that name within its statement.
    "cte-wins-its-name-over-a-snapshot-table": (
        "duckdb",
        "WITH users AS (SELECT id FROM users)\nSELECT id FROM users;\n",
    ),
    # A derived table names a source.
    "derived-table-names-a-source": (
        "duckdb",
        "SELECT s.x FROM (SELECT id AS x FROM users) AS s;\n",
    ),
    # `AS s(id)` renames the output of a `SELECT *`.
    "derived-table-columns-renamed-over-star": (
        "duckdb",
        "SELECT s.id FROM (SELECT * FROM users) AS s(id);\n",
    ),
    # A derived table whose query is `SELECT *` holds every column name read from it,
    # because the file says nothing more about its columns.
    "derived-table-star-resolves-each-column-read-from-it": (
        "duckdb",
        "SELECT s.user_id, user_id FROM (SELECT * FROM orders) AS s;\n",
    ),
    # A VALUES table named with a column list states the names it holds and names no table,
    # so the source holds those names, as a derived table's own query names hold its own.
    "values-column-list-names-a-source": (
        "duckdb",
        "SELECT id, amount FROM (VALUES (1, 2), (3, 4)) AS totals(id, amount);\n",
    ),
    # A `FROM` that reads a file rather than naming a table names no table, so nothing is
    # asked of the snapshot: a column qualified by its alias is a name read from that source.
    "table-function-holds-what-its-alias-qualifies": (
        "duckdb",
        "SELECT file.total FROM read_csv('totals.csv') AS file;\n",
    ),
    # A source that writes a column list over its body holds those names in place of the
    # names that body writes out, not beside them.
    "source-holds-its-declared-list-in-place-of-its-body": (
        "duckdb",
        "SELECT id FROM (SELECT id AS name FROM users) AS s(id);\n",
    ),
    # A recursive `WITH` is the one source SQL lets a query name for its own body.
    "recursive-cte-reads-its-own-name": (
        "duckdb",
        "WITH RECURSIVE walk(id) AS (SELECT id FROM users UNION ALL "
        "SELECT o.user_id FROM orders AS o JOIN walk ON walk.id = o.user_id)\n"
        "SELECT id FROM walk;\n",
    ),
    # A subquery qualified by a table of the query it sits in.
    "correlated-subquery-qualifier": (
        "duckdb",
        "SELECT name FROM users\n"
        "WHERE id IN (SELECT user_id FROM orders WHERE orders.user_id = users.id);\n",
    ),
    # INSERT of a column list of values, and INSERT of a SELECT.
    "insert-column-list-of-values": (
        "duckdb",
        "INSERT INTO users (id) VALUES (2);\n",
    ),
    "insert-of-a-select": (
        "duckdb",
        "INSERT INTO users (id, name) SELECT id, name FROM users;\n",
    ),
    "update": ("duckdb", "UPDATE users SET name = 'x' WHERE id = 1;\n"),
    "delete": ("duckdb", "DELETE FROM users WHERE name = 'x';\n"),
    "union": ("duckdb", "SELECT id FROM users UNION ALL SELECT id FROM orders;\n"),
    # A bare `*` and a qualified `u.*` name no column that can miss.
    "stars": ("duckdb", "SELECT *, u.* FROM users u;\n"),
    # Quoted names resolve against a snapshot that spells them another way.
    "quoted-names": ("postgres", 'SELECT "NAME", "Name", "id" FROM "Users";\n'),
    # Names written in a comment: a comment is not a statement, so nothing resolves.
    "comment-names-nothing": (
        "duckdb",
        "-- SELECT bogus FROM nosuch\nSELECT id FROM users; -- name = 'total'\n",
    ),
    # A statement that holds no query: a CREATE of a table no query of the file reads,
    # an index, a DROP of a table the snapshot does not hold, and a PRAGMA.
    "statements-that-hold-no-query": (
        "duckdb",
        "CREATE TABLE notes (id INTEGER, body TEXT);\nCREATE INDEX ix ON notes;\n"
        "DROP TABLE IF EXISTS scratch;\nPRAGMA foreign_keys;\nSELECT id FROM users;\n",
    ),
    # A `db.table.column` resolves against the table, not along the chain.
    "name-chain-resolves-against-the-table": ("mysql", "SELECT main.users.id FROM users;\n"),
    # Written in another case than the snapshot spells it: resolves.
    "another-case-than-the-snapshot": ("duckdb", "SELECT ID, NAME FROM USERS;\n"),
    "empty-file": ("duckdb", ""),
}

# Each miss is (the kind's word, the name as reported, line, column), in the order the
# file reports them: the report line reads `unknown KIND NAME`.
BAD: dict[str, tuple[str, str, list[tuple[str, str, int, int]]]] = {
    # A misspelt table. Its column is a miss too: an unknown table holds no column.
    "misspelt-table": (
        "duckdb",
        "SELECT id FROM oder;\n",
        [("column", "id", 1, 8), ("table", "oder", 1, 16)],
    ),
    "misspelt-column": ("duckdb", "SELECT total FROM users;\n", [("column", "total", 1, 8)]),
    # A miss in a later statement reports its own line.
    "miss-in-a-later-statement": (
        "duckdb",
        "SELECT id, name FROM users;\n\nSELECT name, total FROM users;\n",
        [("column", "total", 3, 14)],
    ),
    # Every miss of one query is named, not only the first.
    "several-misses-one-query": (
        "duckdb",
        "SELECT total, amount FROM oder;\n",
        [("column", "total", 1, 8), ("column", "amount", 1, 15), ("table", "oder", 1, 27)],
    ),
    # A column of another table: `orders` holds no `name`, and `users` is not in this
    # query's plain sight.
    "column-of-another-table": ("duckdb", "SELECT name FROM orders;\n", [("column", "name", 1, 8)]),
    # A qualified column resolves only against the source its qualifier names.
    "column-qualified-by-a-source-without-it": (
        "duckdb",
        "SELECT u.name FROM orders AS u;\n",
        [("column", "name", 1, 10)],
    ),
    "column-qualified-by-a-name-that-is-no-source": (
        "duckdb",
        "SELECT zz.name FROM users;\n",
        [("column", "name", 1, 11)],
    ),
    # A `db.table.column` that misses names the bare name, after the whole chain.
    "name-chain-still-resolves-against-the-table": (
        "mysql",
        "SELECT main.orders.salary FROM orders;\n",
        [("column", "salary", 1, 20)],
    ),
    # A VALUES table named with a column list holds those names alone, and nothing else of
    # the rows it writes.
    "column-not-in-a-values-column-list": (
        "duckdb",
        "SELECT id, amount FROM (VALUES (1, 2), (3, 4)) AS totals(id);\n",
        [("column", "amount", 1, 12)],
    ),
    # A VALUES table named without a column list states no names, so no unqualified column
    # of the query that reads it resolves.
    "values-source-named-without-a-column-list": (
        "duckdb",
        "SELECT id FROM (VALUES (1, 2), (3, 4)) AS totals;\n",
        [("column", "id", 1, 8)],
    ),
    # The body writes `name` and the source's own list writes `id`: the body's name is the
    # miss, named where it starts.
    "column-a-declared-list-renames-away": (
        "duckdb",
        "SELECT name FROM (SELECT id AS name FROM users) AS s(id);\n",
        [("column", "name", 1, 8)],
    ),
    # A `WITH` that is not recursive names its source for the query above it and not for its
    # own body, and the table that body cannot name holds no column for it either.
    "cte-cannot-read-its-own-name": (
        "duckdb",
        "WITH t(id) AS (SELECT id FROM t)\nSELECT id FROM t;\n",
        [("column", "id", 1, 23), ("table", "t", 1, 31)],
    ),
    # A recursive CTE holds the column list it writes over its own body: a name it reads
    # through its own name that the list does not write is unknown.
    "recursive-cte-column-its-own-list-does-not-name": (
        "duckdb",
        "WITH RECURSIVE walk(id) AS (SELECT id FROM users UNION ALL "
        "SELECT o.user_id FROM orders AS o JOIN walk ON walk.user_id = o.user_id)\n"
        "SELECT id FROM walk;\n",
        [("column", "user_id", 1, 112)],
    ),
    # The alias of a `FROM` that reads a file reaches only columns qualified by it, so an
    # unqualified name of the same query resolves against no source of it.
    "column-named-under-a-table-function": (
        "duckdb",
        "SELECT total FROM read_csv('totals.csv') AS file;\n",
        [("column", "total", 1, 8)],
    ),
    # A CTE's output is its own column names, not the snapshot's.
    "column-not-in-a-ctes-output": (
        "duckdb",
        "WITH t AS (SELECT id FROM users)\nSELECT name FROM t;\n",
        [("column", "name", 2, 8)],
    ),
    # A miss inside a CTE's own body is named, where the body wrote it.
    "miss-in-a-ctes-own-body": (
        "duckdb",
        "WITH t AS (SELECT id FROM nosuch)\nSELECT id FROM t;\n",
        [("column", "id", 1, 19), ("table", "nosuch", 1, 27)],
    ),
    # A name a CTE gave is no table: it holds that CTE's output names and nothing else.
    "named-source-is-no-table": (
        "duckdb",
        "WITH t AS (SELECT salary FROM notes)\nSELECT id FROM t;\n",
        [("column", "salary", 1, 19), ("table", "notes", 1, 31), ("column", "id", 2, 8)],
    ),
    # A column named by a statement that holds no query is not held by the file.
    "create-table-names-no-columns": (
        "duckdb",
        "CREATE TABLE users (id INTEGER, total NUMERIC);\nSELECT total FROM users;\n",
        [("column", "total", 2, 8)],
    ),
    # A derived table's body resolves against its own FROM, as a CTE's body does.
    "miss-in-a-derived-tables-body": (
        "duckdb",
        "SELECT id FROM (SELECT bogus FROM users) AS s;\n",
        [("column", "id", 1, 8), ("column", "bogus", 1, 24)],
    ),
    "column-of-a-table-with-no-columns": (
        "duckdb",
        "SELECT id FROM logs;\n",
        [("column", "id", 1, 8)],
    ),
    # The same misspelt name in two statements reports each statement's own line, not
    # the first statement's spelling of it twice.
    "same-misspelt-name-two-statements": (
        "duckdb",
        "SELECT bogon FROM bogus;\nSELECT bogon FROM users;\n",
        [("column", "bogon", 1, 8), ("table", "bogus", 1, 19), ("column", "bogon", 2, 8)],
    ),
    # One query that misses one name twice reports both spellings.
    "same-name-twice-one-query": (
        "duckdb",
        "SELECT bogon, bogon FROM bogus;\n",
        [("column", "bogon", 1, 8), ("column", "bogon", 1, 15), ("table", "bogus", 1, 26)],
    ),
    # A miss on either side of a UNION is named.
    "miss-either-side-of-a-union": (
        "duckdb",
        "SELECT bogus FROM users UNION SELECT id FROM oder;\n",
        [("column", "bogus", 1, 8), ("column", "id", 1, 38), ("table", "oder", 1, 46)],
    ),
    # A correlated subquery reads the tables of the query it sits in, and qualifies
    # them the same way: `salary` is a column `users` does not hold.
    "correlated-subquery-wrong-qualifier": (
        "duckdb",
        "SELECT name FROM users WHERE (SELECT bogus FROM orders WHERE users.salary = users.id)"
        " IS NOT NULL;\n",
        [("column", "bogus", 1, 38), ("column", "salary", 1, 68)],
    ),
    # A quoted spelled name misses: NAME is the name inside the quotes, and its line
    # and column are the opening quote's, where the spelling starts.
    "quoted-mysql-miss": (
        "mysql",
        "SELECT `bogus_col` FROM users;\n",
        [("column", "bogus_col", 1, 8)],
    ),
    "quoted-postgres-miss": (
        "postgres",
        'SELECT "bogus_col" FROM "users";\n',
        [("column", "bogus_col", 1, 8)],
    ),
    # NAME is the bare name as the file wrote it: its qualifier and its quotes are
    # taken off, and its case is the file's. Names resolve case-insensitively, so
    # nothing here misspells a name by its case alone.
    "reported-as-written": (
        "duckdb",
        "SELECT TOTAL FROM Oder;\n",
        [("column", "TOTAL", 1, 8), ("table", "Oder", 1, 19)],
    ),
    "reported-as-written-quoted": (
        "duckdb",
        'SELECT "TOTAL" FROM "Oder";\n',
        [("column", "TOTAL", 1, 8), ("table", "Oder", 1, 21)],
    ),
    # Text that does not parse, and text that does not even tokenize.
    "does-not-parse": ("duckdb", "SELECT WHERE;\n", []),
    "does-not-tokenize": ("duckdb", "SELECT 'x FROM t;\n", []),
}
