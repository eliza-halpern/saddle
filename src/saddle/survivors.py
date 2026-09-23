"""Survivor-driven test generation: locate, stub, brief, filter (T6-29b).

A node can seal with a surviving mutant or an uncovered changed line:
the gates it passed are thresholds, not proofs, so code it wrote can
ship with nothing able to see it change. The answer is one more test --
but a worker shown the implementation writes a test *of the code*, which
passes whatever the code does. That is the tautology red-phase exists to
reject, and handing the worker the implementation is the surest way to
produce one.

So the brief carries the requirement and each module's *signature*,
never its body: the same `runner._stub_module` red-phase already
materializes for a module that does not exist at baseline. Five pieces,
one decision:

- `enclosing_functions` names what is untested, from the survivor and
  coverage locations the gate already produced.
- `stubbed_sandbox` builds the tree a candidate must fail against.
- `build_survivor_brief` renders one draw's prompt.
- `candidate_test_path` keeps k draws in k files.
- `keep_candidate` decides, over injected runs, whether a candidate
  bought anything.

Nothing here runs a worker or touches the node loop; that splice is
T6-29c. This module imports from `dag`, `evidence` and `runner` and
edits none of them (layering: `dag -> gates -> evidence -> runner ->
slice`).
"""

from __future__ import annotations

import ast
import os
import shutil
import tempfile
from collections.abc import Callable, Collection, Iterable, Mapping
from dataclasses import dataclass
from pathlib import Path, PurePath
from typing import Final, Literal

from saddle.dag import Node
from saddle.evidence import git_changed_files, run_argv
from saddle.runner import _stub_module

TOP_LEVEL: Final = "<module>"
"""Name reported for a line that falls outside every def in its module."""


def _parse_location(survivor: str) -> tuple[str, int] | None:
    """`path:row` from a resolved survivor, or None when it is not one.

    `MutationOutcome.survivors` is not a homogeneous list of locations:
    the collector reports engine failures through the same tuple
    ("mutmut not on PATH", "mutmut run exited 1: ..."), so a member that
    does not end in `:<row>` is dropped rather than parsed into a line
    number no file has.
    """
    head, _, row = survivor.rpartition(":")
    if not head or not row.isdigit():
        return None
    return head, int(row)


def _relative(workdir: Path, path: str) -> str:
    """`path` as a POSIX path relative to the worktree root `workdir`.

    The gate's own line sets are absolute -- `runner.run_node_gate` joins
    every changed and covered line onto the worktree -- while `git`
    speaks worktree-relative, so both spellings have to land on one key.
    """
    joined = os.path.realpath(workdir / path)
    return PurePath(os.path.relpath(joined, os.path.realpath(workdir))).as_posix()


def _function_spans(source: str) -> list[tuple[str, int, int]]:
    """(name, first line, last line) for every outermost def in `source`.

    Nested defs are not listed: a line inside one already falls within
    its outermost def's span, which is the name the brief gives the
    worker. Methods are qualified by their enclosing classes, so two
    `run` methods do not collapse into a single entry. A decorator counts
    as part of the def it decorates.
    """
    spans: list[tuple[str, int, int]] = []

    def walk(body: Iterable[ast.stmt], prefix: str) -> None:
        for statement in body:
            if isinstance(statement, ast.FunctionDef | ast.AsyncFunctionDef):
                start = min(
                    (decorator.lineno for decorator in statement.decorator_list),
                    default=statement.lineno,
                )
                spans.append((prefix + statement.name, start, statement.end_lineno or start))
            elif isinstance(statement, ast.ClassDef):
                walk(statement.body, f"{prefix}{statement.name}.")

    walk(ast.parse(source).body, "")
    return spans


def _enclosing(spans: Iterable[tuple[str, int, int]], row: int) -> str:
    """The outermost def containing `row`, else the module's top level."""
    for name, start, end in spans:
        if start <= row <= end:
            return name
    return TOP_LEVEL


def enclosing_functions(
    workdir: Path,
    survivors: Collection[str],
    uncovered: Collection[tuple[str, int]],
    *,
    baseline: str = "HEAD",
) -> dict[str, set[str]]:
    """Untested functions per changed module, from survivors and coverage gaps.

    `workdir` is the worktree root. `survivors` are `mutmut show`-style
    `path:row` locations -- what `evidence.mutation_sample` already
    resolves each mutant to before it sizes the sample -- and `uncovered`
    is the coverage gate's own `changed - covered` set. Either spelling
    of a path is accepted, absolute or worktree-relative.

    Only modules `git` reports as differing from `baseline` are keyed.
    Both inputs are produced by changed-line analyses, so a location in a
    module this node did not touch is a defect in whoever assembled them,
    and briefing a worker to write tests for untouched code is how a
    recovery grows past the node's own diff.
    """
    changed = set(git_changed_files(workdir, baseline))
    locations = set(uncovered)
    for survivor in survivors:
        parsed = _parse_location(survivor)
        if parsed is not None:
            locations.add(parsed)
    spans: dict[str, list[tuple[str, int, int]]] = {}
    found: dict[str, set[str]] = {}
    for path, row in sorted(locations):
        rel = _relative(workdir, path)
        if rel not in changed:
            continue
        if rel not in spans:
            spans[rel] = _function_spans((workdir / rel).read_text())
        found.setdefault(rel, set()).add(_enclosing(spans[rel], row))
    return found


def stubbed_sandbox(workdir: Path, changed_modules: Collection[str]) -> Path:
    """A throwaway copy of `workdir` whose changed modules are stubs.

    The copy carries `.git` and refreshes the index exactly as
    `slice._evaluate_candidate` does: `copytree` rewrites mtimes, so git
    reads every file as modified until the stat cache is restored.

    Each changed module is replaced by `runner._stub_module`'s
    signature-preserving stub -- same defs, same arguments, no bodies --
    so a candidate test that exercises the node's behaviour fails here
    while one that merely restates the code passes. Entries the tree does
    not hold (a module the node deleted) are skipped.

    The caller owns the returned directory and removes it.
    """
    sandbox = Path(tempfile.mkdtemp(prefix="saddle-stub-tree-"))
    shutil.copytree(workdir, sandbox, symlinks=True, dirs_exist_ok=True)
    run_argv(["git", "update-index", "--refresh"], sandbox)
    for rel in sorted(changed_modules):
        target = sandbox / rel
        if not target.is_file():
            continue
        target.write_text(_stub_module(target.read_text()))
    return sandbox


def candidate_test_path(requirement_id: str, seed: int) -> str:
    """`tests/test_<req>_s<seed>.py`: one file per requirement and seed.

    k draws for one requirement land in k distinct files, so their diffs
    never collide and each candidate is gated, kept or dropped on its
    own. pytest collects the hyphen in `REQ-001` without complaint; the
    file is never imported by name.
    """
    return f"tests/test_{requirement_id}_s{seed}.py"


def _listing(lines: Iterable[str]) -> str:
    """A rendered block, or `(none)` when there is nothing to list."""
    return "\n".join(lines) or "(none)"


def build_survivor_brief(
    node: Node,
    requirements_text: Mapping[str, str],
    stubs: Mapping[str, str],
    functions: Mapping[str, Collection[str]],
    test_conventions: Mapping[str, str],
) -> str:
    """One draw's prompt: requirement, signatures, gaps -- never the code.

    `requirements_text` maps requirement id to statement across the whole
    plan, so a node citing a sibling's id still gets that id's text; the
    node's own statement is the fallback. `stubs` holds the stub source
    of each changed module (`stubbed_sandbox` writes the same text into
    the sandbox), `functions` is `enclosing_functions`' output, and
    `test_conventions` maps existing test paths to their source: every
    name is listed and the first is shown in full, so a draw matches the
    suite's imports and layout instead of inventing them.

    The implementation's bodies and the survivors' mutant names are both
    absent by construction -- neither is a parameter -- which is the
    whole point: a worker that can read the code writes a test of the
    code, and such a test passes however the code behaves.
    """
    statements = {requirement.id: requirement.statement for requirement in node.requirements}
    ids = node.requirement_ids
    reqs = _listing(f"  {rid}: {requirements_text.get(rid) or statements[rid]}" for rid in ids)
    gaps = _listing(f"  {path}: {', '.join(sorted(functions[path]))}" for path in sorted(functions))
    signatures = _listing(f"--- {path} ---\n{stubs[path]}" for path in sorted(stubs))
    known = sorted(test_conventions)
    sample = f"--- {known[0]} ---\n{test_conventions[known[0]]}" if known else "(no existing tests)"
    return f"""Node {node.id} sealed with code nothing can see change: a mutant survived on
it, or a changed line never ran. Write ONE new test file that pins the
requirement below hard enough to catch that.

Requirements (each test must fail if its statement is violated):
{reqs}
Gate command: {node.deterministic_gate.test_command}

Untested functions (a surviving mutant or an uncovered changed line falls
inside each):
{gaps}

Module signatures, bodies removed:
{signatures}

Existing test files:
{_listing(known)}

Conventions, from one of them:
{sample}

Rules:
- You are given signatures, not implementations, on purpose. Write the test the
  requirement demands, not the test the code would pass. A test derived from
  the code passes whatever the code does and is rejected.
- Put every new test in a single new file under tests/.
- Mention each requirement ID above in that file.
- Import only names the signatures above declare.
- Add a hypothesis property (`@given(...)`) alongside the examples where the
  requirement covers a range of inputs rather than one case.
- Keep the file ruff-clean: double quotes, 4-space indent, two blank lines
  between top-level definitions, final newline, no unused imports, sorted
  import blocks with stdlib, third-party and local groups separated.

Output ONLY the diff, no commentary.
"""


@dataclass(frozen=True)
class CandidateRun:
    """What one tree reports about one candidate test file.

    `exit_code` is the candidate's own tests run in that tree.
    `survivors` and `covered` are read from the real tree alone -- the
    stub tree contributes its exit code and nothing else -- and they are
    what the third filter consumes: the survivor names a
    `mutation_sample` re-run scoped to the candidate file still reports,
    and the (path, line) pairs the candidate's run executed.
    """

    exit_code: int
    survivors: tuple[str, ...] = ()
    covered: tuple[tuple[str, int], ...] = ()


CandidateRunner = Callable[[Path, str], CandidateRun]
"""Runs `candidate_file`'s tests in a tree and reports what they did.

Injected rather than imported, so the decision below is a pure function
of two observations (`gates` holds the predicates, runners arrive at the
boundary). An implementation runs only the candidate file, and against
the real tree also re-runs `evidence.mutation_sample` with `run_tests`
scoped to that file. Since P0-8 (T6-61) that scores every decided
mutant on a changed line, so the candidate and the original gate score
the same population, reporting the lines the run executed alongside.

A runner that reports `survivors=()` without having re-run the sample
makes every previously surviving mutant read as killed. Reporting the
prior survivors unchanged when the sample did not run is the runner's
half of this contract.
"""


@dataclass(frozen=True)
class Verdict:
    """`keep_candidate`'s decision, with what the candidate bought.

    `kept` passed all three filters. `failing` is a claim about the
    implementation rather than about the test, so it is returned instead
    of dropped: T6-29c hands it to an impl node. `dropped` pins nothing.
    """

    decision: Literal["kept", "dropped", "failing"]
    detail: str
    killed: tuple[str, ...] = ()
    covered: tuple[tuple[str, int], ...] = ()


def keep_candidate(
    stub_tree: Path,
    real_tree: Path,
    candidate_file: str,
    survivors_before: Collection[str],
    uncovered_before: Collection[tuple[str, int]],
    *,
    run: CandidateRunner,
) -> Verdict:
    """Three filters as one decision: red on stubs, green on code, and useful.

    1. Red against `stub_tree`. A candidate green there passed against
       defs whose bodies are `raise NotImplementedError`, so it asserts
       nothing about behaviour. This is the only filter that catches a
       test written from the implementation.
    2. Green against `real_tree`. A candidate red there is a claim that
       the implementation is wrong; it comes back as `failing` rather
       than dropped, because the claim may be true.
    3. It has to buy something: kill a mutant that survived before, or
       execute a line the coverage gate reported uncovered. One that
       does neither passes both trees and pins nothing the suite did not
       already pin.

    `killed` is `survivors_before` minus the survivors the scoped re-run
    still reports, because `MutationOutcome` names survivors and never
    kills; `covered` is the executed lines that were in the gap.

    Measure-first result (T6-29a, ten reasoning-off draws on round 3d's
    gap): pending T6-29a.
    """
    against_stub = run(stub_tree, candidate_file)
    if against_stub.exit_code == 0:
        return Verdict(
            decision="dropped",
            detail=f"{candidate_file} passes against the stub tree: it pins nothing",
        )
    against_real = run(real_tree, candidate_file)
    if against_real.exit_code != 0:
        return Verdict(
            decision="failing",
            detail=f"{candidate_file} exited {against_real.exit_code} against the real tree",
        )
    killed = tuple(sorted(set(survivors_before) - set(against_real.survivors)))
    covered = tuple(sorted(set(uncovered_before) & set(against_real.covered)))
    if not killed and not covered:
        return Verdict(
            decision="dropped",
            detail=f"{candidate_file} kills no survivor and covers no gap",
        )
    return Verdict(
        decision="kept",
        detail=f"{candidate_file} killed {len(killed)}, covered {len(covered)} line(s)",
        killed=killed,
        covered=covered,
    )
