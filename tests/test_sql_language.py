"""`.sql` is its own language, so a project can scope a gate stage to SQL changes.

Contract: `languages.classify` names the language of a `.sql` file `sql`, in any
case; a project may name `sql` in `gate-stage-languages`, and a stage declared for
sql runs for a change that touches a `.sql` file (edited or deleted), still runs for
a change that holds a wildcard file such as `pyproject.toml`, and does not run for a
Python-only change; a change touching both a `.py` and a `.sql` file is both
languages; a SQL-only change shows neither a passing Python coverage finding nor an
unproven mutation finding, and shows a finding that refuses (the SQL change that
breaks the Python suite). Instances are real audits of a tiny project; the units are
the lookups. The SQL checks themselves (a linter, a schema check) are later parts of
the same task, not this one.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from test_auditor import _init

from saddle import runner
from saddle.auditor import Auditor, Findings
from saddle.evidence import MutationOutcome, SuiteLimitError, gate_stage_languages
from saddle.languages import classify, stage_visible, touches, visible

NOTHING = MutationOutcome(killed=0, total=0, generated=0, survivors=())


@pytest.fixture(autouse=True)
def python_mutation_is_not_under_test(monkeypatch: pytest.MonkeyPatch) -> None:
    def fake(*_args: Any, **_kwargs: Any) -> MutationOutcome:
        return NOTHING

    monkeypatch.setattr(runner, "mutation_sample", fake)


SQL_SADDLE = (
    "[tool.saddle]\n"
    'gate-checks = [["sh", "py_stage.sh"], ["sh", "sql_stage.sh"]]\n'
    'gate-stage-languages = {"sh py_stage.sh" = ["python"], "sh sql_stage.sh" = ["sql"]}\n'
)
SQL_PY_TEST = (
    "from pathlib import Path\n\nfrom n import f\n\n\n"
    "def test_f():\n    assert f() == 1\n\n\n"
    "def test_query_is_declared():\n"
    "    assert 'SELECT' in (Path(__file__).parent / 'db' / 'q.sql').read_text()\n"
)
SQL_FILES = {
    "n.py": "def f():\n    return 1\n",
    "test_n.py": SQL_PY_TEST,
    "db/q.sql": "SELECT 1;\n",
    "db/report.sql": "SELECT 3;\n",
    # the stages judge their own tree with a shell built-in, so they need no tool:
    # `sql_stage.sh` is green at the baseline (no db/extra.sql) and red once a SQL
    # change adds one, which is how a run is shown to have happened.
    "py_stage.sh": "exit 0\n",
    "sql_stage.sh": "[ ! -e db/extra.sql ]\n",
    "pyproject.toml": SQL_SADDLE,
}
# a content edit to a `.sql` file that leaves the stage green
SQL_CHANGE = {"db__q.sql": "SELECT 1;\n-- edited\n"}
# the Python side of a change, with its test rewritten to match
PYTHON_HEAD = "def f():\n    return 2\n"
PYTHON_TEST_HEAD = SQL_PY_TEST.replace("== 1", "== 2")
SQL_ONLY_TABLE = '[tool.saddle]\ngate-stage-languages = {sqlfluff = ["sql"]}\n'
MISSPELT_TABLE = '[tool.saddle]\ngate-stage-languages = {sqlfluff = ["sqll"]}\n'


def sql_project(tmp_path: Path, **head: str | None) -> Path:
    """A committed project with `.sql` files and a gate stage declared for `sql`.

    `head` rewrites (or, None, deletes) files in the working tree; `__` is `/`."""
    root = tmp_path / "sqltree"
    (root / "db").mkdir(parents=True)
    _init(root, SQL_FILES)
    for rel, text in head.items():
        path = root / rel.replace("__", "/")
        if text is None:
            path.unlink()
        else:
            path.write_text(text)
    return root


def gates(found: Findings) -> set[str]:
    return {f.gate for f in found.findings}


def gate_line(found: Findings) -> str:
    detail = next(f.detail for f in found.findings if f.gate == "project-gate")
    return detail.splitlines()[0]


def test_the_sql_lookups() -> None:
    assert classify(["db/q.sql"]) == {"sql"}
    # the suffix is matched in any case, as other languages' suffixes are
    assert classify(["db/Q.SQL"]) == {"sql"}
    # a change that touches a Python and a SQL file is both languages
    assert classify(["n.py", "db/q.sql"]) == {"python", "sql"}
    assert not touches({"sql"}, {"python"})
    assert touches({"sql"}, {"sql"})
    # a passing coverage finding and an unproven mutation finding are not SQL's
    assert visible("coverage", "pass", {"sql"}) is False
    assert visible("mutation", "not-proven", {"sql"}) is False
    # a refusal about a SQL change is shown, as any refusal is
    assert visible("coverage", "fail", {"sql"}) is True
    assert visible("tests", "blocked", {"sql"}) is True
    declared = {"ruff": ("python",), "sh js.sh": ("javascript",), "sh sql.sh": ("sql",)}
    assert stage_visible("sh sql.sh", {"sql"}, declared) is True
    assert stage_visible("sh sql.sh", {"python"}, declared) is False
    # a wildcard file in the change hides nothing, not even a stage scoped to sql
    assert stage_visible("sh sql.sh", {"config"}, declared) is True


def test_sql_is_a_gate_stage_language_a_project_may_name(tmp_path: Path) -> None:
    _init(tmp_path / "good", {"pyproject.toml": SQL_ONLY_TABLE})
    assert gate_stage_languages(tmp_path / "good", "HEAD") == {"sqlfluff": ("sql",)}
    _init(tmp_path / "misspelt", {"pyproject.toml": MISSPELT_TABLE})
    with pytest.raises(SuiteLimitError, match="gate stage languages"):
        gate_stage_languages(tmp_path / "misspelt", "HEAD")


def test_a_sql_only_change_runs_the_sql_stage_and_not_the_python_stage(tmp_path: Path) -> None:
    found = Auditor(sql_project(tmp_path, **SQL_CHANGE, **{"db__extra.sql": "SELECT 2;\n"})).tier1()
    line = gate_line(found)
    # one stage, and the failing one is the SQL stage: the Python stage was skipped
    assert "1 stage" in line
    assert "head ✗ (sh sql_stage.sh)" in line
    assert not gates(found) & {"tests", "coverage", "dead-code", "public-deletions"}
    assert not found.passed


def test_a_python_only_change_does_not_run_the_sql_stage(tmp_path: Path) -> None:
    found = Auditor(
        sql_project(
            tmp_path,
            **{"n.py": PYTHON_HEAD, "test_n.py": PYTHON_TEST_HEAD},
        )
    ).tier1()
    assert gate_line(found) == "Gate: base ✓, head ✓ (1 stage)"
    assert found.passed


def test_a_deleted_sql_file_counts_as_touching_sql(tmp_path: Path) -> None:
    found = Auditor(sql_project(tmp_path, **{"db__report.sql": None})).tier1()
    assert gate_line(found) == "Gate: base ✓, head ✓ (1 stage)"
    assert not gates(found) & {"tests", "coverage", "dead-code", "public-deletions"}


def test_a_wildcard_change_runs_the_sql_stage_too(tmp_path: Path) -> None:
    found = Auditor(
        sql_project(tmp_path, **SQL_CHANGE, **{"pyproject.toml": SQL_SADDLE + "# edited\n"})
    ).tier1()
    assert gate_line(found) == "Gate: base ✓, head ✓ (2 stages)"


def test_a_change_touching_py_and_sql_runs_both_stages(tmp_path: Path) -> None:
    found = Auditor(
        sql_project(
            tmp_path,
            **SQL_CHANGE,
            **{"n.py": PYTHON_HEAD, "test_n.py": PYTHON_TEST_HEAD},
        )
    ).tier1()
    assert gate_line(found) == "Gate: base ✓, head ✓ (2 stages)"


def test_a_sql_only_change_hides_the_python_findings_that_refuse_nothing(tmp_path: Path) -> None:
    root = sql_project(tmp_path, **SQL_CHANGE)
    first = Auditor(root).tier1()
    assert "project-gate" in gates(first)
    assert not gates(first) & {"tests", "coverage", "public-deletions", "assertion-preservation"}
    second = Auditor(root).tier2()
    assert not gates(second) & {"mutation", "coverage", "full-suite", "red-phase", "dead-code"}


def test_a_sql_change_that_breaks_the_python_suite_is_still_refused(tmp_path: Path) -> None:
    found = Auditor(sql_project(tmp_path, **{"db__q.sql": "-- nothing here\n"})).tier1()
    refused = next(f for f in found.findings if f.gate == "tests")
    assert refused.verdict == "fail"
    assert not found.passed
