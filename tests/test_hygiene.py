"""Suite hygiene from ledgers: tests that never killed a mutant, duplicates, text pins (#80b).

Known-good: a test that ran only surviving mutants is never-killed; two tests with one
map's same fingerprint are duplicates; a test whose asserts all compare a literal with
file text is a text pin; each row cites span ids in the ledgers. Known-bad, never
reported: a test that ran a killed mutant (one-way: the tools do not say which test
killed it); a mutant row naming no tests; fingerprints compared across maps; a test
with any behavioural assert; a test no map recorded.
"""

from __future__ import annotations

import io
import json
import os
import subprocess
import uuid
from pathlib import Path
from typing import Any

import pytest

from saddle import cli
from saddle.auditor import Auditor, AuditorConfig
from saddle.hygiene import MAP_SPAN, MUTATION_SPAN, ONE_WAY, Row, report
from saddle.journal import append_span, build_span, write_attempt_sidecar


def sealed(journal: Path, name: str, evidence: dict[str, Any]) -> str:
    """Append a span `name` whose sidecar holds `evidence`; its span id."""
    span_id = uuid.uuid4().hex
    digest = write_attempt_sidecar(journal, span_id, evidence)
    append_span(
        journal,
        build_span(
            node_id="chat#1",
            argv=["x"],
            duration_ms=0,
            exit_code=0,
            detail="",
            name=name,
            span_id=span_id,
            attempt_hash=digest,
        ),
    )
    return span_id


def mutants(*rows: tuple[str, str, list[str] | None]) -> dict[str, Any]:
    detail = []
    for name, status, tests in rows:
        row: dict[str, Any] = {"name": name, "status": status, "show": "x"}
        if tests is not None:
            row["tests"] = tests
        detail.append(row)
    return {"mutant_detail": detail}


def rows_of(found: Any, kind: str) -> list[Row]:
    return [r for r in found.rows if r.kind == kind]


# -- never-killed ------------------------------------------------------------------


def test_a_test_that_ran_only_survivors_never_killed_and_one_that_ran_a_kill_is_not_listed(
    tmp_path: Path,
) -> None:
    ledger = tmp_path / "proofs.jsonl"
    span = sealed(
        ledger,
        MUTATION_SPAN,
        mutants(
            ("m1", "killed", ["t.py::a", "t.py::b"]),
            ("m2", "survived", ["t.py::b", "t.py::c"]),
            ("m3", "timeout", ["t.py::e"]),
            ("m4", "Survived", ["tests/c.test.js"]),
        ),
    )
    found = report([ledger], tmp_path)
    assert [(r.tests, r.records) for r in rows_of(found, "never-killed")] == [
        (("t.py::c",), (span,)),
        (("tests/c.test.js",), (span,)),
    ]
    assert rows_of(found, "never-killed")[0].note == "ran 1 scored mutant(s), none killed"
    assert found.mutation_audits == 1


def test_a_kill_in_any_ledger_takes_a_test_off_the_list(tmp_path: Path) -> None:
    one, two = tmp_path / "one.jsonl", tmp_path / "two.jsonl"
    sealed(one, MUTATION_SPAN, mutants(("m1", "survived", ["t.py::c", "t.py::f"])))
    later = sealed(
        two, MUTATION_SPAN, mutants(("m1", "killed", ["t.py::c"]), ("m2", "survived", ["t.py::f"]))
    )
    found = report([one, two], tmp_path)
    (row,) = rows_of(found, "never-killed")
    assert row.tests == ("t.py::f",)
    assert later in row.records
    assert len(row.records) == 2
    assert row.note == "ran 2 scored mutant(s), none killed"


def test_a_mutant_row_that_names_no_tests_supports_no_row(tmp_path: Path) -> None:
    """A record sealed before `tests` existed, or a run with no stats pass: the
    absence of a record, never a claim that no test ran."""
    ledger = tmp_path / "proofs.jsonl"
    sealed(ledger, MUTATION_SPAN, mutants(("m1", "survived", None), ("m2", "survived", [])))
    sealed(ledger, MUTATION_SPAN, {"survivors": []})
    found = report([ledger], tmp_path)
    assert found.rows == ()
    assert found.mutation_audits == 2


# -- duplicates --------------------------------------------------------------------


H1, H2 = "1" * 64, "2" * 64


def test_tests_one_map_saw_cover_the_same_lines_are_duplicates(tmp_path: Path) -> None:
    ledger = tmp_path / "proofs.jsonl"
    span = sealed(
        ledger, MAP_SPAN, {"test_fingerprints": {"t.py::a": H1, "t.py::b": H1, "t.py::c": H2}}
    )
    (row,) = rows_of(report([ledger], tmp_path), "duplicate")
    assert (row.tests, row.records) == (("t.py::a", "t.py::b"), (span,))
    assert "111111111111" in row.note


def test_fingerprints_from_different_maps_are_never_compared(tmp_path: Path) -> None:
    ledger = tmp_path / "proofs.jsonl"
    sealed(ledger, MAP_SPAN, {"test_fingerprints": {"t.py::a": H1, "t.py::c": H2}})
    sealed(ledger, MAP_SPAN, {"test_fingerprints": {"t.py::b": H1, "t.py::d": H2}})
    sealed(ledger, MAP_SPAN, {"test_fingerprints": None})  # a span that drew no map
    found = report([ledger], tmp_path)
    assert rows_of(found, "duplicate") == []
    assert found.maps == 2


# -- text pins ---------------------------------------------------------------------


PINS = """import inspect
import re
from pathlib import Path

import pytest


def f():
    return 1


def test_pin():
    text = Path("x.py").read_text()
    assert "def f" in text


def test_pin_getsource():
    assert inspect.getsource(f).startswith("def f")


def test_pin_regex(case):
    tries = 3
    src = open("x.py").read()
    copy = src
    assert re.search("return", copy)


def test_pin_with_open():
    with open("x.py") as fh:
        body = fh.read()
    assert body != "x"
    assert not body.endswith(("y", "z")) and "w" not in body


def test_mixed():
    assert "def f" in Path("x.py").read_text()
    assert f() == 1


def test_raises():
    assert "x" in Path("x.py").read_text()
    with pytest.raises(ValueError):
        int("x")


def test_no_assert():
    Path("x.py").read_text()


def test_compared_with_a_value_not_a_literal():
    assert Path("x.py").read_text() == f.__name__


def test_unrecorded():
    assert "x" in Path("x.py").read_text()


def test_pin_bare_getsource():
    from inspect import getsource

    assert getsource(f).startswith("def f")


def test_helper_checks_behaviour():
    def check():
        assert f() == 1

    check()
    assert "x" in Path("x.py").read_text()


def test_attribute_not_file_text(data):
    assert "x" in data.text


def test_call_of_a_call(loaders):
    assert "x" in loaders[0]()


def test_another_method_of_a_path():
    assert "x" in Path("x.py").as_posix()


def test_an_ordering_compare():
    assert "a" < Path("x.py").read_text()


def test_another_method_of_the_text():
    assert Path("x.py").read_text().isascii()


def test_bare_truthiness():
    text = Path("x.py").read_text()
    assert text


def test_open_without_a_name():
    with open("x.py"):
        pass
    assert "x" in Path("x.py").read_text()


class TestGroup:
    def test_in_class(self):
        assert "x" in Path("x.py").read_text()
"""


def test_a_test_whose_asserts_all_compare_a_literal_with_file_text_is_a_text_pin(
    tmp_path: Path,
) -> None:
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests" / "test_p.py").write_text(PINS)
    names = [
        "test_pin",
        "test_pin_getsource",
        "test_pin_regex[1]",
        "test_pin_regex[2]",
        "test_pin_with_open",
        "test_mixed",
        "test_raises",
        "test_no_assert",
        "test_compared_with_a_value_not_a_literal",
        "TestGroup::test_in_class",
        "test_missing_from_the_file",
        "NoSuchClass::test_in_class",
        "test_pin_bare_getsource",
        "test_helper_checks_behaviour",
        "test_attribute_not_file_text",
        "test_call_of_a_call",
        "test_another_method_of_a_path",
        "test_an_ordering_compare",
        "test_another_method_of_the_text",
        "test_open_without_a_name",
        "test_bare_truthiness",
    ]
    prints = {f"tests/test_p.py::{n}": f"{i:064d}" for i, n in enumerate(names)}
    prints["tests/gone.py::test_x"] = "9" * 64  # a file no longer in the tree
    prints["tests/a.test.js"] = "8" * 64  # not Python
    ledger = tmp_path / "proofs.jsonl"
    span = sealed(ledger, MAP_SPAN, {"test_fingerprints": prints})
    pins = rows_of(report([ledger], tmp_path), "text-pin")
    assert [r.tests[0] for r in pins] == [
        "tests/test_p.py::TestGroup::test_in_class",
        "tests/test_p.py::test_open_without_a_name",
        "tests/test_p.py::test_pin",
        "tests/test_p.py::test_pin_bare_getsource",
        "tests/test_p.py::test_pin_getsource",
        "tests/test_p.py::test_pin_regex",
        "tests/test_p.py::test_pin_with_open",
    ]
    assert all(r.records == (span,) for r in pins)
    assert "tests/test_p.py:12" in pins[2].note


# -- the command -------------------------------------------------------------------


def hygiene(*args: str) -> tuple[int, str]:
    out = io.StringIO()
    return cli.main(["hygiene", *args], stdout=out), out.getvalue()


def test_saddle_hygiene_prints_the_rows_and_says_the_claim_is_one_way(tmp_path: Path) -> None:
    ledger = tmp_path / "proofs.jsonl"
    span = sealed(ledger, MUTATION_SPAN, mutants(("m1", "survived", ["t.py::c"])))
    code, text = hygiene("--journal", str(ledger), "--repo", str(tmp_path))
    assert code == 0
    assert text.splitlines()[:2] == [
        "saddle hygiene: 1 ledger(s), 1 mutation audit(s) with scored mutants, 0 impact map(s)",
        ONE_WAY,
    ]
    assert f"never-killed  t.py::c -- ran 1 scored mutant(s), none killed [records: {span}]" in text
    code, text = hygiene("--journal", str(ledger), "--json")
    assert code == 0
    data = json.loads(text)
    assert data["rows"] == [
        {
            "kind": "never-killed",
            "tests": ["t.py::c"],
            "records": [span],
            "note": "ran 1 scored mutant(s), none killed",
        }
    ]


def test_saddle_hygiene_says_when_nothing_supports_a_row(tmp_path: Path) -> None:
    ledger = tmp_path / "proofs.jsonl"
    sealed(ledger, "audit:other", {"x": 1})
    code, text = hygiene("--journal", str(ledger))
    assert code == 0
    assert "no rows: nothing in these ledgers supports one" in text


def test_saddle_hygiene_refuses_a_missing_or_unverifiable_ledger(tmp_path: Path) -> None:
    code, text = hygiene("--journal", str(tmp_path / "missing.jsonl"))
    assert (code, text) == (1, f"error: no journal at {tmp_path / 'missing.jsonl'}\n")
    ledger = tmp_path / "proofs.jsonl"
    sealed(ledger, MUTATION_SPAN, mutants(("m1", "survived", ["t.py::c"])))
    ledger.write_text(ledger.read_text().replace('"exit_code": 0', '"exit_code": 7', 1))
    code, text = hygiene("--journal", str(ledger))
    assert code == 1
    assert "does not verify" in text


# -- a ledger a real audit sealed --------------------------------------------------


METHOD_BASE = "def f():\n    return 1\n\n\nclass Box:\n    def get(self, k):\n        return 1\n"
METHOD_FIXED = "def f():\n    return 2\n\n\nclass Box:\n    def get(self, k):\n        return 2\n"
METHOD_TEST = (
    "from n import Box, f\n\n\ndef test_f():\n    assert f() == 2\n\n\n"
    "def test_get():\n    assert Box().get(1) == 2\n"
)
SHOW = "--- n.py\n+++ n.py\n@@ -2 +2 @@\n-    return 2\n+    return 3\n"


def git(root: Path, *argv: str) -> None:
    subprocess.run(["git", *argv], cwd=root, check=True, capture_output=True)


def test_a_ledger_a_real_tier2_audit_sealed_names_the_test_that_never_killed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The auditor seals `mutant_detail` with each mutant's tests (#80 part a1).
    mutmut is a stub that leaves its stats pass's record, as the real one does:
    `f`'s mutants ran under test_f (one killed), `Box.get`'s under test_get (none)."""
    root = tmp_path / "tree"
    root.mkdir()
    git(root, "init", "-q")
    git(root, "config", "user.email", "t@t")
    git(root, "config", "user.name", "t")
    (root / "n.py").write_text(METHOD_BASE)
    git(root, "add", "-A")
    git(root, "commit", "-q", "-m", "base")
    (root / "n.py").write_text(METHOD_FIXED)
    (root / "test_n.py").write_text(METHOD_TEST)
    stats = json.dumps(
        {
            "tests_by_mangled_function_name": {
                "n.x_f": ["test_n.py::test_f"],
                "n.xǁBoxǁget": ["test_n.py::test_get"],
            }
        }
    )
    results = (
        "  n.x_f__mutmut_1: killed\n  n.x_f__mutmut_2: survived\n  n.xǁBoxǁget__mutmut_1: survived"
    )
    stub = tmp_path / "stub"
    stub.mkdir()
    script = stub / "mutmut"
    script.write_text(
        "#!/bin/sh\n"
        'case "$1" in\n'
        "  run) mkdir -p mutants && printf '%s' '" + stats + "' > mutants/mutmut-stats.json;;\n"
        f"  results) printf '%s\\n' '{results}';;\n"
        f"  show) printf '%s' '{SHOW}';;\n"
        "esac\n"
    )
    script.chmod(0o755)
    monkeypatch.setenv("PATH", f"{stub}{os.pathsep}{os.environ['PATH']}")
    journal = tmp_path / "proofs.jsonl"
    Auditor(root, config=AuditorConfig(journal=journal)).tier2()
    found = report([journal], root)
    (row,) = rows_of(found, "never-killed")
    assert row.tests == ("test_n.py::test_get",)
    assert found.mutation_audits == 1
