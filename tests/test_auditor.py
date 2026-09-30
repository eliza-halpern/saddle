"""Tests for saddle.auditor: the tiered battery and its tree-keyed cache (Phase 2).

Fixtures follow test_audit.py: the changed source line is `    return 2`, which
the conftest's autouse `mutmut` stub reports five killed mutants on.
"""

from __future__ import annotations

import io
import json
import os
import sys
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Any

import pytest

from saddle import auditor as auditor_mod
from saddle import evidence, gates, runner
from saddle.audit import AuditError, audit_node
from saddle.auditor import (
    REASONS,
    REUSES,
    TASK_REQUIREMENTS,
    TIER0,
    TIER1,
    TIER2,
    Auditor,
    AuditorConfig,
    Findings,
    check_imports,
    gate_interpreter_finds,
)
from saddle.cli import main
from saddle.evidence import MutationOutcome, run_argv
from saddle.journal import read_spans, verify_journal

BASE_CODE = "def f():\n    return 1\n"
FIXED_CODE = "def f():\n    return 2\n"
TEST_BODY = "from n import f\n\n\ndef test_f():\n    assert f() == {value}\n"

# The thirteen gates of `gates.run_tier1`, the denominator every tier split
# must account for exactly once (`full-suite` reuses `tests`' predicate).
THIRTEEN = {
    "syntax",
    "ruff",
    "tests",
    "coverage",
    "dead-code",
    "public-deletions",
    "red-phase",
    "node-scope",
    "target-scope",
    "property-coverage",
    "assertion-preservation",
    "requirement-binding",
    "mutation",
}


def _git(root: Path, *argv: str) -> None:
    assert run_argv(["git", *argv], root) == 0


def _init(root: Path, files: dict[str, str]) -> None:
    root.mkdir(parents=True, exist_ok=True)
    _git(root, "init")
    _git(root, "config", "user.email", "test@example.com")
    _git(root, "config", "user.name", "test")
    for name, text in files.items():
        (root / name).write_text(text)
    _git(root, "add", "-A")
    _git(root, "commit", "-m", "baseline")


@pytest.fixture
def clean_tree(tmp_path: Path) -> Path:
    tree = tmp_path / "tree"
    _init(tree, {"n.py": BASE_CODE})
    (tree / "n.py").write_text(FIXED_CODE)
    (tree / "test_n.py").write_text(TEST_BODY.format(value=2))
    return tree


@pytest.fixture
def uncovered_tree(clean_tree: Path) -> Path:
    (clean_tree / "m.py").write_text("def g():\n    return 7\n")
    return clean_tree


class _Spy:
    """Counts calls through to the wrapped function."""

    def __init__(self, target: Callable[..., Any]) -> None:
        self.target = target
        self.calls = 0

    def __call__(self, *args: Any, **kwargs: Any) -> Any:
        self.calls += 1
        return self.target(*args, **kwargs)


@pytest.fixture
def gate_spy(monkeypatch: pytest.MonkeyPatch) -> _Spy:
    spy = _Spy(runner.run_node_gate)
    monkeypatch.setattr(runner, "run_node_gate", spy)
    return spy


@pytest.fixture
def mutation_spy(monkeypatch: pytest.MonkeyPatch) -> _Spy:
    spy = _Spy(evidence.mutation_sample)  # the object runner.mutation_sample names
    monkeypatch.setattr(runner, "mutation_sample", spy)
    return spy


def _verdicts(result: Findings) -> dict[str, str]:
    return {f.gate: f.verdict for f in result.findings}


def test_tiers_partition_the_thirteen_gates() -> None:
    gates = set(TIER0) - {"imports"} | set(TIER1) | set(TIER2) - {"full-suite"}
    assert gates == THIRTEEN
    assert len(TIER0) + len(TIER1) + len(TIER2) == 15
    # P1's gate is the one tier-1 finding emitted only with a requirements
    # file (`AuditorConfig.task_requirements`); it is not one of the thirteen.
    assert TASK_REQUIREMENTS not in THIRTEEN | set(TIER1)
    assert set(REUSES) == set(REASONS) == set(TIER0) | set(TIER1) | set(TIER2) | {TASK_REQUIREMENTS}
    assert set(REASONS.values()) <= {"code-wrong", "evidence-thin", "scope", "unknown"}


# -- known-good --------------------------------------------------------------


def test_correct_change_passes_every_tier_then_hits_the_cache(
    clean_tree: Path, gate_spy: _Spy, monkeypatch: pytest.MonkeyPatch
) -> None:
    auditor = Auditor(clean_tree)
    results = auditor.audit()
    assert [r.tier for r in results] == [0, 0, 1, 2]
    for result in results:
        assert result.passed, result.findings
    # one run of the battery: tier 2 ran it, tier 1 was read from its cache
    assert [r.cached for r in results] == [False, False, True, False]
    assert [f.gate for f in results[2].findings] == list(TIER1)
    assert [f.gate for f in results[3].findings] == list(TIER2)
    assert _verdicts(results[2])["node-scope"] == "not-applicable"
    mutation = next(f for f in results[3].findings if f.gate == "mutation")
    assert mutation.verdict == "pass"
    assert mutation.cites == ("saddle.gates.check_mutation", "sampled n=5")
    # one run of the battery for both tiers (it used to be one per tier)
    assert gate_spy.calls == 1
    syntax_spy = _Spy(gates.check_syntax)  # the object auditor_mod.check_syntax names
    monkeypatch.setattr(auditor_mod, "check_syntax", syntax_spy)
    again = auditor.audit()
    assert all(r.cached for r in again)
    assert gate_spy.calls == 1
    assert syntax_spy.calls == 0
    assert [r.findings for r in again] == [r.findings for r in results]


def test_disk_cache_serves_a_new_auditor(clean_tree: Path, gate_spy: _Spy, tmp_path: Path) -> None:
    config = AuditorConfig(cache_dir=tmp_path / "cache")
    first = Auditor(clean_tree, config=config).tier1()
    second = Auditor(clean_tree, config=config).tier1()
    assert gate_spy.calls == 1
    assert second.cached
    assert second.findings == first.findings
    # A corrupt or mismatched file is a miss, never a verdict.
    (tmp_path / "cache" / f"{first.key}.json").write_text("{not json")
    Auditor(clean_tree, config=config).tier1()
    assert gate_spy.calls == 2
    other = json.loads((tmp_path / "cache" / f"{first.key}.json").read_text())
    other["key"] = "0" * 64
    (tmp_path / "cache" / f"{first.key}.json").write_text(json.dumps(other))
    Auditor(clean_tree, config=config).tier1()
    assert gate_spy.calls == 3


def test_every_finding_is_a_journal_record(clean_tree: Path, tmp_path: Path) -> None:
    journal = tmp_path / "proofs.jsonl"
    auditor = Auditor(clean_tree, config=AuditorConfig(journal=journal))
    result = auditor.tier1()
    spans = read_spans(journal)
    assert [s.name for s in spans] == [f"audit-tier1:{g}" for g in TIER1]
    assert json.loads(spans[1].detail)["gate"] == "coverage"
    assert verify_journal(journal) == []
    auditor.tier1()  # a cache hit writes nothing
    assert len(read_spans(journal)) == len(result.findings)


def test_a_declared_plan_node_makes_scope_checks_applicable(clean_tree: Path) -> None:
    node = audit_node().model_copy(update={"id": "n1", "target_files": ["other.py"]})
    result = Auditor(clean_tree, config=AuditorConfig(node=node)).tier1()
    verdicts = _verdicts(result)
    assert verdicts["target-scope"] == "fail"
    target = next(f for f in result.findings if f.gate == "target-scope")
    assert target.reason == "scope"


def test_tier1_skips_the_tier2_evidence_legs(clean_tree: Path, mutation_spy: _Spy) -> None:
    Auditor(clean_tree).tier1()
    assert mutation_spy.calls == 0


# -- known-bad ---------------------------------------------------------------


def test_uncovered_changed_line_fails_tier1_and_blocks_tier2(
    uncovered_tree: Path, gate_spy: _Spy, mutation_spy: _Spy
) -> None:
    auditor = Auditor(uncovered_tree)
    first = auditor.tier1()
    assert _verdicts(first)["coverage"] == "fail"
    coverage = next(f for f in first.findings if f.gate == "coverage")
    assert coverage.reason == "evidence-thin"
    assert "m.py" in coverage.detail
    second = auditor.tier2()
    assert [(f.verdict, f.reason) for f in second.findings] == [("blocked", "unknown")]
    assert "coverage" in second.findings[0].detail
    assert mutation_spy.calls == 0
    assert gate_spy.calls == 1


def test_tier2_alone_derives_tier1_from_its_own_run_and_is_blocked_when_it_fails(
    uncovered_tree: Path, gate_spy: _Spy
) -> None:
    """One run of the battery: tier 2 derives tier 1 from it and caches it;
    a failing tier 1 still blocks tier 2 (its mutation result is discarded)."""
    auditor = Auditor(uncovered_tree)
    result = auditor.tier2()
    assert result.findings[0].verdict == "blocked"
    first = auditor.tier1()
    assert first.cached
    assert _verdicts(first)["coverage"] == "fail"
    assert gate_spy.calls == 1


def _untested_mutmut(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A mutmut stub whose five mutants on n.py:2 no test runs."""
    stub = tmp_path / "untested-stub"
    stub.mkdir()
    results = "\n".join(f"  m{i}: no tests" for i in range(1, 6))
    show = "--- n.py\n+++ n.py\n@@ -2 +2 @@\n-    return 2\n+    return 3\n"
    script = stub / "mutmut"
    script.write_text(
        "#!/bin/sh\n"
        'case "$1" in\n'
        "  run) exit 0;;\n"
        f"  results) printf '%s\\n' '{results}';;\n"
        f"  show) printf '%s' '{show}';;\n"
        "esac\n"
    )
    script.chmod(0o755)
    monkeypatch.setenv("PATH", f"{stub}{os.pathsep}{os.environ['PATH']}")


def test_new_tests_that_kill_no_mutant_fail_tier2_counting_untested(
    clean_tree: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _untested_mutmut(tmp_path, monkeypatch)
    auditor = Auditor(clean_tree)
    assert auditor.tier1().passed
    result = auditor.tier2()
    mutation = next(f for f in result.findings if f.gate == "mutation")
    assert mutation.verdict == "fail"
    assert mutation.reason == "evidence-thin"
    assert "killed 0 of 5" in mutation.detail
    assert "5 untested" in mutation.detail
    assert not result.passed


def test_syntax_breaking_edit_fails_tier0(clean_tree: Path) -> None:
    result = Auditor(clean_tree).tier0("n.py", "def f(:\n    return 2\n")
    assert _verdicts(result) == {"syntax": "fail", "ruff": "fail", "imports": "pass"}
    syntax = result.findings[0]
    assert syntax.reason == "code-wrong"
    assert syntax.cites == ("saddle.gates.check_syntax",)
    assert "not checked" in result.findings[2].detail


def test_tier0_ruff_charges_only_introduced_findings(tmp_path: Path) -> None:
    tree = tmp_path / "tree"
    _init(tree, {"n.py": "import os\n\n\ndef f():\n    return 1\n"})
    auditor = Auditor(tree)
    inherited = auditor.tier0("n.py", "import os\n\n\ndef f():\n    return 2\n")
    assert _verdicts(inherited)["ruff"] == "pass"
    assert "inherited: 1" in inherited.findings[1].detail
    introduced = auditor.tier0("m.py", "import os\n")
    assert _verdicts(introduced)["ruff"] == "fail"
    assert "F401" in introduced.findings[1].detail


def test_tier0_import_resolution(clean_tree: Path) -> None:
    auditor = Auditor(clean_tree)
    good = auditor.tier0("test_x.py", "import json\nimport pytest\nfrom n import f\n")
    assert _verdicts(good)["imports"] == "pass"
    bad = auditor.tier0("test_y.py", "import no_such_module_xyz\n\nno_such_module_xyz.f()\n")
    assert _verdicts(bad)["imports"] == "fail"
    assert "no_such_module_xyz" in bad.findings[2].detail


def test_check_imports_counts_relative_imports(tmp_path: Path) -> None:
    check = check_imports("p/a.py", "from . import b\nfrom .c import d\n", [tmp_path])
    assert check.passed
    assert "2 relative import(s) not checked" in check.detail
    missing = check_imports("a.py", "from . import b\nimport zz_missing\n", [tmp_path])
    assert not missing.passed
    assert "1 relative" in missing.detail
    (tmp_path / "pkg").mkdir()
    assert check_imports("a.py", "import pkg.sub\n", [tmp_path]).passed


def test_a_name_saddle_lacks_is_put_to_the_interpreter_finder(tmp_path: Path) -> None:
    """Only the names saddle's own interpreter cannot find are asked; a name
    the finder resolves passes (known-good), one it does not stays
    unresolved (known-bad)."""
    asked: list[list[str]] = []

    def finds(names: Sequence[str]) -> set[str]:
        asked.append(list(names))
        return {"zz_elsewhere"} & set(names)

    source = "import json\nimport zz_elsewhere\n"
    assert check_imports("a.py", source, [tmp_path], finds).passed
    assert asked == [["zz_elsewhere"]]
    missing = check_imports("a.py", "import zz_nowhere\n", [tmp_path], finds)
    assert not missing.passed
    assert "zz_nowhere" in missing.detail


def _project_python(root: Path, package: str) -> Path:
    """A PATH dir whose `python` is this interpreter with `package` importable:
    a project environment holding a dependency saddle's venv lacks."""
    site = root / "site"
    (site / package).mkdir(parents=True)
    (site / package / "__init__.py").write_text("")
    bin_dir = root / "bin"
    bin_dir.mkdir()
    wrapper = bin_dir / "python"
    wrapper.write_text(f'#!/bin/sh\nPYTHONPATH={site} exec {sys.executable} "$@"\n')
    wrapper.chmod(0o755)
    return bin_dir


def test_the_gate_interpreter_finds_what_the_projects_python_has(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    project = _project_python(tmp_path / "project", "zz_project_only")
    monkeypatch.setenv("PATH", f"{project}{os.pathsep}/usr/bin{os.pathsep}/bin")
    assert gate_interpreter_finds(["zz_project_only", "zz_nowhere"]) == {"zz_project_only"}
    assert gate_interpreter_finds([]) == set()


def test_the_gate_interpreter_without_the_project_finds_nothing_extra(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Known-bad: PATH without the project's python; saddle's is asked instead."""
    monkeypatch.setenv("PATH", f"/usr/bin{os.pathsep}/bin")
    assert gate_interpreter_finds(["zz_project_only"]) == set()


@pytest.mark.parametrize("answer", ["exit 1", "echo not-json", "echo 7"])
def test_a_lookup_that_fails_finds_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, answer: str
) -> None:
    """A python that fails or answers nonsense resolves no name."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    (bin_dir / "python").write_text(f"#!/bin/sh\n{answer}\n")
    (bin_dir / "python").chmod(0o755)
    monkeypatch.setenv("PATH", str(bin_dir))
    assert gate_interpreter_finds(["json"]) == set()


def test_tier0_resolves_imports_with_the_projects_python(
    clean_tree: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The wiring: tier 0 passes an import only the project's python has."""
    project = _project_python(tmp_path / "project", "zz_project_only")
    monkeypatch.setenv("PATH", f"{project}{os.pathsep}{os.environ['PATH']}")
    found = Auditor(clean_tree).tier0("test_x.py", "import zz_project_only\n")
    assert _verdicts(found)["imports"] == "pass"


_BUILD_PYPROJECT = '[build-system]\nrequires = ["setuptools>=61", "zz-build-helper"]\n'
_SETUP_PY = "import zz_build_helper\nfrom setuptools import setup\n\nsetup()\n"


def test_tier0_resolves_what_the_build_system_installs_for_setup_py(
    clean_tree: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A root setup.py runs in the build frontend's isolated environment, which
    holds `[build-system] requires` and not the test environment: its imports of
    those names resolve (known-good). The same import from any other file, from
    a setup.py below the root, or of a name the table does not declare stays
    unresolved (known-bad). No python on PATH has zz_build_helper, so the
    verdict does not depend on what the machine happens to have installed."""
    monkeypatch.setenv("PATH", f"/usr/bin{os.pathsep}/bin")
    (clean_tree / "pyproject.toml").write_text(_BUILD_PYPROJECT)
    auditor = Auditor(clean_tree)
    good = auditor.tier0("setup.py", _SETUP_PY)
    assert _verdicts(good)["imports"] == "pass"
    assert "2 build requirement(s) not looked up" in good.findings[2].detail
    for path, source in [
        ("test_x.py", _SETUP_PY),
        ("pkg/setup.py", _SETUP_PY),
        ("setup.py", "import zz_undeclared\n"),
    ]:
        bad = auditor.tier0(path, source)
        assert _verdicts(bad)["imports"] == "fail", path
        assert "zz_" in bad.findings[2].detail


def test_imports_no_longer_vouch_that_a_declared_build_requirement_exists(
    clean_tree: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The cost of the rule above, on the record: a build requirement no index
    has (a hallucinated one) passes tier 0 when setup.py imports it. The build
    itself still fails; tier 0 no longer says so."""
    monkeypatch.setenv("PATH", f"/usr/bin{os.pathsep}/bin")
    (clean_tree / "pyproject.toml").write_text(
        '[build-system]\nrequires = ["zz-no-index-has-this"]\n'
    )
    found = Auditor(clean_tree).tier0("setup.py", "import zz_no_index_has_this\n")
    assert _verdicts(found)["imports"] == "pass"


@pytest.mark.parametrize(
    ("pyproject", "names"),
    [
        (None, {"setuptools"}),
        ('[project]\nname = "p"\n', {"setuptools"}),
        (
            '[build-system]\nrequires = ["setuptools>=61", "setuptools-scm[toml]",'
            ' "Cython ; python_version < \'4\'", "a.b", 7]\n',
            {"setuptools", "setuptools_scm", "cython", "a_b"},
        ),
        ('[build-system]\nrequires = ["hatchling"]\n', {"hatchling"}),
        ('[build-system]\nbuild-backend = "x"\n', set()),
        ('[build-system]\nrequires = "setuptools"\n', set()),
        ("build-system = 1\n", set()),
        ("[build-system\n", set()),
    ],
)
def test_build_requirement_names(pyproject: str | None, names: set[str]) -> None:
    """With no [build-system] table the frontend falls back to setuptools; a
    table names its own; a table without a readable requires list, or a file
    that does not parse, provides nothing."""
    assert auditor_mod.build_requirement_names(pyproject) == names


def test_setup_py_build_requirements_reads_the_trees_pyproject(tmp_path: Path) -> None:
    assert auditor_mod.setup_py_build_requirements(tmp_path) == {"setuptools"}
    (tmp_path / "pyproject.toml").write_text('[build-system]\nrequires = ["flit_core"]\n')
    assert auditor_mod.setup_py_build_requirements(tmp_path) == {"flit_core"}
    (tmp_path / "pyproject.toml").unlink()
    (tmp_path / "pyproject.toml").mkdir()
    assert auditor_mod.setup_py_build_requirements(tmp_path) == set()


def test_one_byte_change_misses_the_cache(clean_tree: Path, gate_spy: _Spy) -> None:
    auditor = Auditor(clean_tree)
    first = auditor.tier1()
    (clean_tree / "n.py").write_text(FIXED_CODE.replace("2", "3"))
    (clean_tree / "test_n.py").write_text(TEST_BODY.format(value=3))
    second = auditor.tier1()
    assert gate_spy.calls == 2
    assert first.key != second.key
    assert not second.cached
    zero_a = auditor.tier0("n.py", FIXED_CODE)
    zero_b = auditor.tier0("n.py", FIXED_CODE + " ")
    assert zero_a.key != zero_b.key
    assert not zero_b.cached


def test_an_unchanged_tree_is_an_audit_error(tmp_path: Path) -> None:
    tree = tmp_path / "tree"
    _init(tree, {"n.py": BASE_CODE})
    with pytest.raises(AuditError, match="nothing to audit"):
        Auditor(tree).tier1()


def test_tool_failure_is_reason_unknown() -> None:
    finding = auditor_mod._finding(
        "mutation", 2, "fail", "mutation tool failed: mutmut run exited 1", "sampled n=0"
    )
    assert finding.reason == "unknown"
    assert auditor_mod._finding("mutation", 2, "fail", "killed 0 of 5", None).reason == (
        "evidence-thin"
    )


def test_findings_round_trip() -> None:
    result = Findings(
        tier=1,
        key="k",
        findings=(auditor_mod._finding("tests", 1, "pass", "ok", None),),
    )
    assert Findings.from_dict(json.loads(json.dumps(result.to_dict()))) == result


def test_runner_tier2_false_measures_no_mutation(clean_tree: Path, mutation_spy: _Spy) -> None:
    _git(clean_tree, "add", "-A")
    gated = runner.run_node_gate(audit_node(), clean_tree, tier2=False)
    assert mutation_spy.calls == 0
    assert gated.mutation is not None
    assert gated.mutation.survivors == (runner.NOT_MEASURED_AT_TIER1,)
    full = runner.run_node_gate(audit_node(), clean_tree)
    assert mutation_spy.calls == 1
    assert full.mutation is not None
    assert full.mutation.killed == 5


# -- CLI -----------------------------------------------------------------------


def _cli(repo: Path, *extra: str) -> tuple[int, str, str]:
    out, err = io.StringIO(), io.StringIO()
    code = main(
        ["audit", "--tiered", "--no-cache", *extra, "--repo", str(repo)], stdout=out, stderr=err
    )
    return code, out.getvalue(), err.getvalue()


def test_cli_tiered_accepts_a_correct_change(clean_tree: Path) -> None:
    code, out, err = _cli(clean_tree)
    assert (code, err) == (0, "")
    assert out.rstrip().endswith("verdict: accept")
    assert "tier 2" in out
    assert "[evidence-thin]" in out


def test_cli_tiered_refuses_with_exit_one_and_json(uncovered_tree: Path) -> None:
    code, out, _err = _cli(uncovered_tree, "--json")
    assert code == 1
    payload = json.loads(out)
    assert payload["verdict"] == "refuse"
    assert [t["tier"] for t in payload["tiers"]] == [0, 0, 0, 1, 2]
    assert payload["tiers"][-1]["findings"][0]["verdict"] == "blocked"


def test_cli_tiered_rev_mode_and_exit_codes(clean_tree: Path, tmp_path: Path) -> None:
    _git(clean_tree, "add", "-A")
    _git(clean_tree, "commit", "-m", "fix")
    code, out, _err = _cli(clean_tree, "HEAD")
    assert code == 0, out
    code, out, _err = _cli(clean_tree)
    assert code == 3
    assert out.rstrip().endswith("verdict: nothing to audit")
    code, _out, err = _cli(clean_tree, "--baseline", "no-such-base")
    assert code == 2
    assert "no-such-base" in err


# -- the mutation outcome is sealed beside its finding --------------------------


def test_tier2_seals_the_mutation_outcome_in_its_findings_sidecar(
    clean_tree: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The `audit-tier2:mutation` span carries a hash-sealed sidecar holding
    the `MutationOutcome` it was decided from, so the packet can describe the
    mutants in English (mutant_text) instead of repeating the count. The
    finding's own detail and verdict are unchanged."""
    from saddle.journal import attempt_sidecar_path
    from saddle.mutant_text import describe_mutation
    from saddle.packet import compile_packet

    _untested_mutmut(tmp_path, monkeypatch)
    journal = tmp_path / "proofs.jsonl"
    result = Auditor(clean_tree, config=AuditorConfig(journal=journal)).tier2()
    mutation = next(f for f in result.findings if f.gate == "mutation")
    span = next(s for s in read_spans(journal) if s.name == "audit-tier2:mutation")
    assert span.attempt_hash
    assert json.loads(span.detail)["detail"] == mutation.detail
    sealed = json.loads(attempt_sidecar_path(journal, span.span_id).read_text())
    assert (sealed["killed"], sealed["total"], sealed["untested"]) == (0, 5, 5)
    assert sorted(sealed["survivors"]) == [f"m{i}" for i in range(1, 6)]
    assert verify_journal(journal) == []
    assert describe_mutation(sealed).untested == 5
    # The shortlist's per-mutant rows are sealed in the record shape the
    # Findings and AuditResult dicts use, so describe_mutation places them.
    assert [(m["name"], m["status"]) for m in sealed["mutant_detail"]] == [
        (f"m{i}", "no tests") for i in range(1, 6)
    ]
    assert all(m["show"].startswith("--- n.py") for m in sealed["mutant_detail"])
    assert [m.name for m in describe_mutation(sealed).mutants] == [f"m{i}" for i in range(1, 6)]
    # Every other finding of the tier is journaled as before: no sidecar.
    others = [s for s in read_spans(journal) if s.name != "audit-tier2:mutation"]
    assert others
    assert all(not s.attempt_hash for s in others)
    row = next(r for r in compile_packet(journal).rows if r.key == "mutation")
    assert "5 sampled mutants sit in code no test runs [record: untested=5]" in row.summary
    # With the shortlist's rows sealed, each survivor is described from its own
    # diff; the "no recorded diff" fallback no longer applies to any of them.
    assert "m1: `return 2` -> `return 3`" in row.summary
    assert "survived: nothing tests this [m1]" in row.summary
    assert "no recorded diff" not in row.summary


def test_a_blocked_tier2_seals_no_mutation_outcome(uncovered_tree: Path, tmp_path: Path) -> None:
    journal = tmp_path / "proofs.jsonl"
    result = Auditor(uncovered_tree, config=AuditorConfig(journal=journal)).tier2()
    assert result.findings[0].verdict == "blocked"
    span = next(s for s in read_spans(journal) if s.name == "audit-tier2:mutation")
    assert span.attempt_hash == ""
    assert verify_journal(journal) == []


def test_tier1_seals_sources_and_changed_lines_beside_a_failing_coverage_finding(
    uncovered_tree: Path, tmp_path: Path
) -> None:
    """The `audit-tier1:coverage` finding that names uncovered lines carries a
    sealed sidecar with the text of each file it names and the changed set the
    gate judged, so the packet can render `coverage_text`'s English. The
    finding itself is unchanged."""
    from saddle.journal import attempt_sidecar_path
    from saddle.packet import compile_packet

    journal = tmp_path / "proofs.jsonl"
    result = Auditor(uncovered_tree, config=AuditorConfig(journal=journal)).tier1()
    coverage = next(f for f in result.findings if f.gate == "coverage")
    assert coverage.verdict == "fail"
    assert coverage.detail.startswith("no test runs m.py:")
    span = next(s for s in read_spans(journal) if s.name == "audit-tier1:coverage")
    assert span.attempt_hash
    sealed = json.loads(attempt_sidecar_path(journal, span.span_id).read_text())
    assert sealed["sources"] == {"m.py": ["def g():", "    return 7"]}
    assert ["m.py", 1] in sealed["changed"]
    assert ["n.py", 2] in sealed["changed"]
    assert all(not p.startswith("/") for p, _ in sealed["changed"])
    assert verify_journal(journal) == []
    others = [s for s in read_spans(journal) if s.name != "audit-tier1:coverage"]
    assert all(not s.attempt_hash for s in others)
    audit = next(r for r in compile_packet(journal).rows if r.key == "audit")
    assert (
        "  - m.py g: 2 of 2 changed lines never run -- nothing exercises g [lines 1, 2]\n"
        "      1: def g():\n"
        "      2:     return 7\n"
    ) in audit.summary


def test_tier1_seals_nothing_beside_a_passing_coverage_finding(
    clean_tree: Path, tmp_path: Path
) -> None:
    journal = tmp_path / "proofs.jsonl"
    result = Auditor(clean_tree, config=AuditorConfig(journal=journal)).tier1()
    assert next(f for f in result.findings if f.gate == "coverage").verdict == "pass"
    span = next(s for s in read_spans(journal) if s.name == "audit-tier1:coverage")
    assert span.attempt_hash == ""


def test_coverage_evidence_is_none_for_a_detail_that_names_no_line(tmp_path: Path) -> None:
    from saddle.auditor import coverage_evidence

    assert coverage_evidence(tmp_path, "HEAD", "every changed line is run") is None


def test_coverage_evidence_skips_a_named_file_it_cannot_read(uncovered_tree: Path) -> None:
    """A finding may name a file that is gone from the copy (or unreadable);
    its lines are left for the packet to report "not placed", the files that
    are readable are still sealed, and the changed set is unaffected."""
    from saddle.auditor import coverage_evidence

    sealed = coverage_evidence(uncovered_tree, "HEAD", "no test runs gone.py:1, m.py:1")
    assert sealed is not None
    assert sorted(sealed["sources"]) == ["m.py"]
    # The tree's own diff against HEAD (m.py is untracked here, so n.py is the change).
    assert sealed["changed"] == [["n.py", 2]]


def test_tier1_seals_nothing_when_the_coverage_detail_names_no_line(
    uncovered_tree: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A failing coverage finding whose detail is not the "no test runs" shape
    (nothing to place) is journaled as before, with no sidecar."""
    monkeypatch.setattr(auditor_mod, "coverage_evidence", lambda *a, **k: None)
    journal = tmp_path / "proofs.jsonl"
    result = Auditor(uncovered_tree, config=AuditorConfig(journal=journal)).tier1()
    assert next(f for f in result.findings if f.gate == "coverage").verdict == "fail"
    span = next(s for s in read_spans(journal) if s.name == "audit-tier1:coverage")
    assert span.attempt_hash == ""
    assert verify_journal(journal) == []


def test_tier2_seals_nothing_when_the_gate_carries_no_outcome(
    clean_tree: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`Tier1Result.mutation` is optional; a gate result without it journals
    the tier as before and seals no sidecar."""
    import dataclasses

    _untested_mutmut(tmp_path, monkeypatch)
    real = runner.run_node_gate
    monkeypatch.setattr(
        runner, "run_node_gate", lambda *a, **k: dataclasses.replace(real(*a, **k), mutation=None)
    )
    journal = tmp_path / "proofs.jsonl"
    Auditor(clean_tree, config=AuditorConfig(journal=journal)).tier2()
    span = next(s for s in read_spans(journal) if s.name == "audit-tier2:mutation")
    assert span.attempt_hash == ""
    assert verify_journal(journal) == []


def test_tier1_seals_a_not_proven_coverage_finding_under_shortlist_so_its_english_renders(
    uncovered_tree: Path, tmp_path: Path
) -> None:
    """Under `--tier2 shortlist` coverage is `not-proven`, and it seals the
    same sidecar a failing one does, so the packet renders `coverage_text`'s
    English for it."""
    from saddle.packet import compile_packet

    journal = tmp_path / "proofs.jsonl"
    config = AuditorConfig(journal=journal, tier2="shortlist")
    result = Auditor(uncovered_tree, config=config).tier1()
    coverage = next(f for f in result.findings if f.gate == "coverage")
    assert coverage.verdict == "not-proven"
    span = next(s for s in read_spans(journal) if s.name == "audit-tier1:coverage")
    assert span.attempt_hash
    assert verify_journal(journal) == []
    audit = next(r for r in compile_packet(journal).rows if r.key == "audit")
    assert "  - m.py g: 2 of 2 changed lines never run -- nothing exercises g" in audit.summary


def test_tier0_formats_by_the_projects_own_ruff_settings(tmp_path: Path) -> None:
    """Red before: tier 0 ran `ruff format --check` in a bare temp dir, on
    ruff's defaults (88 columns), so a watched run's files, formatted to the
    project's 100, were refused as unformatted. Instances: a 95-column line
    passes under the project's line-length; one past 100 still fails."""
    tree = tmp_path / "tree"
    _init(tree, {"pyproject.toml": "[tool.ruff]\nline-length = 100\n", "n.py": BASE_CODE})
    auditor = Auditor(tree)
    wide = "def f():\n    return " + " + ".join(["1"] * 22) + "\n"
    assert 88 < max(len(line) for line in wide.splitlines()) <= 100
    assert _verdicts(auditor.tier0("n.py", wide))["ruff"] == "pass"
    too_wide = "def f():\n    return " + " + ".join(["1"] * 30) + "\n"
    assert max(len(line) for line in too_wide.splitlines()) > 100
    assert _verdicts(auditor.tier0("n.py", too_wide))["ruff"] == "fail"
    # read from the commit: the tree loosening its own config changes nothing
    (tree / "pyproject.toml").write_text("[tool.ruff]\nline-length = 200\n")
    assert _verdicts(Auditor(tree).tier0("n.py", too_wide))["ruff"] == "fail"


@pytest.mark.parametrize(
    ("budget_spent", "verdict"), [(True, "not-proven"), (False, "fail")], ids=["spent", "ran"]
)
def test_a_mutation_run_whose_budget_ran_out_before_any_mutant_is_not_proven(
    clean_tree: Path, monkeypatch: pytest.MonkeyPatch, budget_spent: bool, verdict: str
) -> None:
    """Red before: a mutation run whose time budget ran out before mutmut
    decided a single mutant refused the finish ("no mutants decided"), and
    the model could do nothing about it. It is no evidence either way, so it
    is not proven and says so; a run that decided nothing within its budget
    still fails."""

    def undecided(*_args: object, **_kwargs: object) -> MutationOutcome:
        return MutationOutcome(
            killed=0, total=0, generated=12, survivors=(), budget_spent=budget_spent
        )

    monkeypatch.setattr(runner, "mutation_sample", undecided)
    found = next(f for f in Auditor(clean_tree).tier2().findings if f.gate == "mutation")
    assert found.verdict == verdict, found.detail
    if budget_spent:
        assert found.detail.startswith("not measured: the mutation run's time budget ran out")
        assert "12 generated" in found.detail


# -- a format-only edit reuses the tiers it cannot change -------------------------


@pytest.mark.parametrize(
    ("edit", "reruns"),
    [
        ("def f():\n\n    return  2  # the fix\n", False),
        ("def f():\n    return 3\n", True),
    ],
    ids=["format-only", "behaviour"],
)
def test_a_format_only_edit_reuses_tier_1_and_2_and_a_real_edit_runs_them(
    clean_tree: Path, gate_spy: _Spy, edit: str, reruns: bool
) -> None:
    """Red before: reformatting after a passing audit re-ran the suite and
    mutation (ten minutes on saddle), though nothing the interpreter sees had
    changed. A tree whose every Python file has the same syntax tree reuses
    the passing tiers 1 and 2; any change the interpreter sees runs them."""
    auditor = Auditor(clean_tree)
    first = auditor.tier2()
    assert first.passed, first
    ran = gate_spy.calls
    (clean_tree / "n.py").write_text(edit)
    (clean_tree / "test_n.py").write_text(TEST_BODY.format(value=3 if reruns else 2))
    again = auditor.tier2()
    assert (gate_spy.calls > ran) is reruns
    if not reruns:
        assert _verdicts(again) == _verdicts(first)


def test_a_failing_audit_is_never_reused_across_a_format_only_edit(
    uncovered_tree: Path, gate_spy: _Spy
) -> None:
    """Known-bad: findings with a failure name lines a reformat may move, and
    a failure must be re-proved on the tree it is shown for."""
    auditor = Auditor(uncovered_tree)
    first = auditor.tier1()
    assert not first.passed
    ran = gate_spy.calls
    (uncovered_tree / "m.py").write_text("def g():\n\n    return  7\n")
    auditor.tier1()
    assert gate_spy.calls > ran


def test_every_tier_0_finding_names_the_file_it_checked(clean_tree: Path) -> None:
    """Red before: a failing format check read "ruff format --check exited 1",
    and a watched run formatted the wrong files twice before finding the one
    it meant. Each tier-0 finding carries its file, and the model's text names it."""
    from saddle.feed import AuditResult, render

    found = Auditor(clean_tree).tier0("n.py", "def f():\n    return  2\n").findings
    assert {f.path for f in found} == {"n.py"}
    ruff = next(f for f in found if f.gate == "ruff")
    assert ruff.verdict == "fail"
    text = render(AuditResult("finish", "t" * 12, found))
    assert "- ruff (tier 0): fail, code-wrong: n.py: " in text
