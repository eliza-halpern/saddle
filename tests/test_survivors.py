"""T6-29b: the survivor-driven test machinery, each contract pinned twice.

Every contract below gets an instance it must accept and one it must
reject; asserting that a filter exists would pin nothing (CONTRIBUTING.md).
The brief's known-bad is the load-bearing one: a brief that leaks a line
of the implementation produces a test of the code, which passes whatever
the code does.
"""

from __future__ import annotations

import shutil
import sys
from collections.abc import Mapping
from pathlib import Path

import pytest

from saddle.dag import Node
from saddle.evidence import run_argv
from saddle.runner import _stub_module
from saddle.survivors import (
    TOP_LEVEL,
    CandidateRun,
    CandidateRunner,
    Verdict,
    build_survivor_brief,
    candidate_test_path,
    enclosing_functions,
    keep_candidate,
    stubbed_sandbox,
)

CHANGED_SOURCE = '''"""The node's module."""

import functools

CONSTANT = 1


@functools.cache
def wrapped(value):
    def inner(step):
        return step + CONSTANT

    return inner(value)


async def gathered(value):
    return value


class Holder:
    label = "held"

    def run(self, value):
        return value * 2
'''

UNCHANGED_SOURCE = '''"""A module this node never touched."""


def untouched(value):
    return value - 1
'''

EXISTING_TEST = '''"""Conventions the next draw should match."""

from n import wrapped


def test_wrapped_adds_one():  # REQ-001
    assert wrapped(1) == 2
'''


def _line(source: str, needle: str) -> int:
    """The 1-based line of `needle` in `source`; line numbers must not be typed out."""
    lines = source.splitlines()
    matches = [number for number, text in enumerate(lines, start=1) if needle in text]
    assert len(matches) == 1, f"{needle!r} occurs {len(matches)} time(s)"
    return matches[0]


def _worktree(root: Path) -> None:
    """A repo whose `n.py` and `notes.txt` differ from HEAD and whose `m.py` does not."""
    setup = (
        ["git", "init"],
        ["git", "config", "user.email", "test@example.com"],
        ["git", "config", "user.name", "test"],
    )
    for argv in setup:
        assert run_argv(argv, root) == 0
    (root / "n.py").write_text("def wrapped(value):\n    return value\n")
    (root / "m.py").write_text(UNCHANGED_SOURCE)
    (root / "notes.txt").write_text("before\n")
    (root / "tests").mkdir()
    (root / "tests" / "test_n.py").write_text(EXISTING_TEST)
    assert run_argv(["git", "add", "-A"], root) == 0
    assert run_argv(["git", "commit", "-m", "baseline"], root) == 0
    (root / "n.py").write_text(CHANGED_SOURCE)
    (root / "notes.txt").write_text("after\n")
    assert run_argv(["git", "add", "-A"], root) == 0


def _node(node_id: str = "n1", statement: str = "wrapped(v) returns v + 1.") -> Node:
    return Node.model_validate(
        {
            "id": node_id,
            "kind": "impl",
            "dependencies": [],
            "task_prompt": "Implement wrapped.",
            "requirements": [
                {"id": "REQ-001", "statement": statement, "accepts": ["2"], "rejects": ["3"]}
            ],
            "execution_constraints": {
                "reasoning_budget": "low",
                "allowed_tools": ["read_file"],
                "max_context_tokens": 8000,
            },
            "deterministic_gate": {
                "test_command": "python -m pytest -q",
                "changed_line_coverage_min": 100.0,
                "red_phase_required": True,
                "mutation_sample": {
                    "scope": "changed-lines",
                    "max_mutants": 100,
                    "kill_threshold": 85.0,
                },
            },
        }
    )


# Contract 1: enclosing_functions locates every gap in a changed module.


def test_enclosing_functions_names_the_outermost_def_of_every_gap(tmp_path: Path) -> None:
    """Known-good: nested defs report their outermost def, decorators count,
    a method carries its class, and a line in no def is the module itself."""
    _worktree(tmp_path)
    nested = _line(CHANGED_SOURCE, "return step + CONSTANT")
    decorator = _line(CHANGED_SOURCE, "@functools.cache")
    method = _line(CHANGED_SOURCE, "return value * 2")
    coroutine = _line(CHANGED_SOURCE, "async def gathered")
    attribute = _line(CHANGED_SOURCE, 'label = "held"')
    found = enclosing_functions(
        tmp_path,
        [f"n.py:{nested}", f"n.py:{decorator}", f"n.py:{coroutine}"],
        [("n.py", method), ("n.py", attribute)],
    )
    assert found == {"n.py": {"wrapped", "gathered", "Holder.run", TOP_LEVEL}}


def test_enclosing_functions_ignores_a_line_in_an_unchanged_module(tmp_path: Path) -> None:
    """Known-bad: `m.py` is identical to HEAD, so its gap is not the node's."""
    _worktree(tmp_path)
    untouched = _line(UNCHANGED_SOURCE, "return value - 1")
    found = enclosing_functions(
        tmp_path,
        [f"m.py:{untouched}"],
        [("m.py", untouched), ("n.py", _line(CHANGED_SOURCE, "return value * 2"))],
    )
    assert found == {"n.py": {"Holder.run"}}


def test_enclosing_functions_accepts_absolute_and_relative_spellings(tmp_path: Path) -> None:
    """The gate's own line sets are absolute; git's are worktree-relative."""
    _worktree(tmp_path)
    method = _line(CHANGED_SOURCE, "return value * 2")
    absolute = enclosing_functions(tmp_path, [f"{tmp_path / 'n.py'}:{method}"], [])
    relative = enclosing_functions(tmp_path, [], [(str(tmp_path / "n.py"), method)])
    assert absolute == relative == {"n.py": {"Holder.run"}}


def test_enclosing_functions_drops_a_survivor_that_is_not_a_location(tmp_path: Path) -> None:
    """Known-bad: `MutationOutcome.survivors` carries engine failures too."""
    _worktree(tmp_path)
    method = _line(CHANGED_SOURCE, "return value * 2")
    found = enclosing_functions(
        tmp_path,
        [
            "mutmut not on PATH",
            "mutmut run exited 1: failed to collect stats. runner returned 1",
            f"n.py:{method}",
        ],
        [],
    )
    assert found == {"n.py": {"Holder.run"}}


# Contract 2: stubbed_sandbox is the tree a candidate must fail against.


def test_stubbed_sandbox_exposes_every_def_and_keeps_no_body(tmp_path: Path) -> None:
    """Known-good: the stub imports and every def is there. Known-bad: no body line."""
    _worktree(tmp_path)
    sandbox = stubbed_sandbox(tmp_path, ["n.py"])
    try:
        stub = (sandbox / "n.py").read_text()
        namespace: dict[str, object] = {}
        exec(compile(stub, "n.py", "exec"), namespace)
        assert {"wrapped", "gathered", "Holder"} <= set(namespace)
        holder = namespace["Holder"]
        assert callable(holder)
        with pytest.raises(NotImplementedError):
            holder().run(1)
        assert "return value * 2" not in stub
        assert "return step + CONSTANT" not in stub
        assert "return value - 1" in (sandbox / "m.py").read_text()
        assert (sandbox / ".git").is_dir()
    finally:
        shutil.rmtree(sandbox)


def test_stubbed_sandbox_skips_a_module_the_tree_does_not_hold(tmp_path: Path) -> None:
    """A module the node deleted is named by the diff and absent from the tree."""
    _worktree(tmp_path)
    sandbox = stubbed_sandbox(tmp_path, ["n.py", "gone.py"])
    try:
        assert not (sandbox / "gone.py").exists()
        assert "return value * 2" not in (sandbox / "n.py").read_text()
    finally:
        shutil.rmtree(sandbox)


# Contract 3: the brief carries the requirement and the signatures, never the code.


def test_survivor_brief_carries_requirement_functions_stubs_and_conventions() -> None:
    """Known-good: every one of the four pieces reaches the prompt."""
    brief = build_survivor_brief(
        _node(),
        {"REQ-001": "wrapped(v) returns v + 1 for every int v."},
        {"n.py": _stub_module(CHANGED_SOURCE)},
        {"n.py": {"Holder.run", "wrapped"}},
        {"tests/test_n.py": EXISTING_TEST, "tests/test_other.py": "# other\n"},
    )
    assert "REQ-001: wrapped(v) returns v + 1 for every int v." in brief
    assert "  n.py: Holder.run, wrapped" in brief
    assert "def wrapped(value):" in brief
    assert "tests/test_n.py" in brief
    assert "tests/test_other.py" in brief
    assert "def test_wrapped_adds_one():  # REQ-001" in brief
    assert "python -m pytest -q" in brief


def test_survivor_brief_contains_no_implementation_line_and_no_mutant_name(
    tmp_path: Path,
) -> None:
    """Known-bad, end to end: neither a body line nor a mutant name may leak."""
    _worktree(tmp_path)
    method = _line(CHANGED_SOURCE, "return value * 2")
    mutant = "n.x_Holder_run__mutmut_3"
    functions = enclosing_functions(tmp_path, [mutant, f"n.py:{method}"], [])
    brief = build_survivor_brief(
        _node(),
        {},
        {"n.py": _stub_module((tmp_path / "n.py").read_text())},
        functions,
        {"tests/test_n.py": EXISTING_TEST},
    )
    assert "return value * 2" not in brief
    assert "return step + CONSTANT" not in brief
    assert mutant not in brief
    assert "Holder.run" in brief


def test_survivor_brief_falls_back_to_the_node_statement() -> None:
    """An id the plan's map omits still reaches the worker with its text."""
    brief = build_survivor_brief(_node(statement="Only the node knows this."), {}, {}, {}, {})
    assert "REQ-001: Only the node knows this." in brief


def test_survivor_brief_without_stubs_or_tests_says_so() -> None:
    """Empty sections render as placeholders, never as a blank the model fills in."""
    brief = build_survivor_brief(_node(), {}, {}, {}, {})
    assert "(no existing tests)" in brief
    assert "(none)" in brief


# Contract 4: one candidate file per requirement and seed.


def test_candidate_test_path_is_distinct_per_seed_and_pytest_collects_it(
    tmp_path: Path,
) -> None:
    """Known-good: the path pytest is handed is one pytest actually runs."""
    assert candidate_test_path("REQ-001", 0) == "tests/test_REQ-001_s0.py"
    paths = {candidate_test_path("REQ-001", seed) for seed in range(3)}
    assert len(paths) == 3
    target = tmp_path / candidate_test_path("REQ-042", 2)
    target.parent.mkdir()
    target.write_text("def test_collected():  # REQ-042\n    assert True\n")
    argv = [sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider", "-o", "addopts="]
    assert run_argv([*argv, candidate_test_path("REQ-042", 2)], tmp_path) == 0


# Contract 5: three filters as one decision.


def _runner(answers: Mapping[Path, CandidateRun], calls: list[Path]) -> CandidateRunner:
    def run(tree: Path, candidate_file: str) -> CandidateRun:
        assert candidate_file == "tests/test_REQ-001_s0.py"
        calls.append(tree)
        return answers[tree]

    return run


def _verdict(
    stub: CandidateRun,
    real: CandidateRun,
    calls: list[Path],
    *,
    survivors: tuple[str, ...] = ("n.x_run__mutmut_1",),
    uncovered: tuple[tuple[str, int], ...] = (("n.py", 22),),
) -> Verdict:
    return keep_candidate(
        Path("/stub"),
        Path("/real"),
        "tests/test_REQ-001_s0.py",
        survivors,
        uncovered,
        run=_runner({Path("/stub"): stub, Path("/real"): real}, calls),
    )


def test_keep_candidate_keeps_a_test_that_kills_a_survivor() -> None:
    """Known-good: red on the stubs, green on the code, and a mutant dies."""
    calls: list[Path] = []
    verdict = _verdict(
        CandidateRun(exit_code=1),
        CandidateRun(exit_code=0, survivors=(), covered=(("n.py", 9),)),
        calls,
    )
    assert verdict.decision == "kept"
    assert verdict.killed == ("n.x_run__mutmut_1",)
    assert verdict.covered == ()
    assert calls == [Path("/stub"), Path("/real")]


def test_keep_candidate_keeps_a_test_that_only_closes_a_coverage_gap() -> None:
    """Either half of filter three is enough on its own."""
    calls: list[Path] = []
    verdict = _verdict(
        CandidateRun(exit_code=1),
        CandidateRun(exit_code=0, survivors=("n.x_run__mutmut_1",), covered=(("n.py", 22),)),
        calls,
    )
    assert verdict.decision == "kept"
    assert verdict.killed == ()
    assert verdict.covered == (("n.py", 22),)


def test_keep_candidate_drops_a_test_green_on_the_stub_tree() -> None:
    """Known-bad: it passed against defs that raise, so it asserts nothing."""
    calls: list[Path] = []
    verdict = _verdict(
        CandidateRun(exit_code=0),
        CandidateRun(exit_code=0, survivors=(), covered=(("n.py", 22),)),
        calls,
    )
    assert verdict.decision == "dropped"
    assert "pins nothing" in verdict.detail
    assert calls == [Path("/stub")]


def test_keep_candidate_returns_a_test_red_on_the_real_tree_as_failing() -> None:
    """Known-bad for keeping, but the claim may be true: it is not dropped."""
    calls: list[Path] = []
    verdict = _verdict(CandidateRun(exit_code=1), CandidateRun(exit_code=2), calls)
    assert verdict.decision == "failing"
    assert "exited 2" in verdict.detail
    assert verdict.killed == ()


def test_keep_candidate_drops_a_test_that_kills_and_covers_nothing() -> None:
    """Known-bad: it passes both trees and pins nothing the suite did not."""
    calls: list[Path] = []
    verdict = _verdict(
        CandidateRun(exit_code=1),
        CandidateRun(exit_code=0, survivors=("n.x_run__mutmut_1",), covered=(("n.py", 9),)),
        calls,
    )
    assert verdict.decision == "dropped"
    assert "kills no survivor and covers no gap" in verdict.detail
