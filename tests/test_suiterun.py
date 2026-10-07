"""Which shell lines start the whole test suite (#178, `saddle.suiterun`).

Known-bad lines are the spellings a watched run used (a background run with
its output redirected, a run with one file ignored) and their wrapped forms;
known-good lines are the narrowed runs the prompt invites, plus lines that only
mention pytest. Both halves, so neither a detector that refuses everything nor
one that refuses nothing passes.
"""

from __future__ import annotations

import pytest

from saddle.suiterun import starts_whole_suite

WHOLE = [
    "cd /w && python -m pytest -q -n 8 -p no:randomly > /tmp/suite.log 2>&1; echo done",
    "cd /w && python -m pytest -q -n 8 -p no:randomly --ignore=tests/test_ya.py",
    "pytest --ignore tests/test_a.py --deselect tests/test_b.py::test_c",
    "pytest",
    "py.test -x",
    "uv run pytest -q",
    "uv run --frozen python -m pytest",
    ".venv/bin/python -m pytest tests/",
    "python3.12 -X dev -m pytest",
    "timeout -s KILL 600 pytest -x",
    "FOO=1 coverage run -m pytest",
    "env A=b nice pytest -q",
    "./check.sh",
    "bash check.sh",
    "PATH=x:$PATH ./check.sh 2>&1 | tail",
    "cd /w\npytest -q",
    "cd /w && \\\npytest -q",
    "pytest tests/test_a.py tests",
    "python - <<'EOF'\nimport subprocess\nEOF\npytest",
    "ruff check . || pytest",
]

NARROWED = [
    "pytest tests/test_a.py -q",
    "pytest tests/test_a.py::test_b",
    "pytest test_calc.py -q",
    "pytest src/pkg/test_x.py",
    "pytest test_calc.py::test_a",
    "python -m pytest tests/test_yaml_check.py -q --no-cov -p no:randomly 2>&1 | tail",
    "pytest -k yaml",
    "pytest -kyaml",
    "pytest --lf",
    "pytest --collect-only -q",
    "pytest tests/unit",
    "pytest --cov saddle tests/test_a.py",
    "ruff check .",
    "grep -rn pytest src",
    "python -c 'import pytest'",
    "cat > t.sh <<'EOF'\npytest -q\nEOF\nchmod +x t.sh",
    "cat > t.sh <<-EOF\n./check.sh\nEOF",
    "echo 'unbalanced",
    "FOO=1",
    "python -m http.server 8000",
    "",
]


@pytest.mark.parametrize("line", WHOLE)
def test_a_line_that_starts_the_whole_suite_is_seen(line: str) -> None:
    assert starts_whole_suite(line)


@pytest.mark.parametrize("line", NARROWED)
def test_a_narrowed_run_or_a_mention_is_not(line: str) -> None:
    assert not starts_whole_suite(line)
