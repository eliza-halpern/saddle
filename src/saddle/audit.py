"""Audit a tree against a baseline with the Tier-1 battery and no plan (P1-2).

`audit_tree(tree, baseline)` gates `tree` against `baseline` and returns one
verdict: `nothing-to-audit` when the staged tree equals the baseline's tree,
`accept` when every applicable check passed, `refuse` otherwise. The tree is
taken as it is on disk: untracked files count (a new module is the least-
tested code in a tree, and `git diff` never shows it) and ignored noise does
not. The audit works on a copy, so the audited tree -- its files and its
`.git/index` alike -- is never written to.

Four checks are plan-relative: `node-scope`, `target-scope`,
`requirement-binding` and `property-coverage` each need something only a plan
supplies. Without one they are reported `not-applicable`, never `pass`, since
a `pass` would be a claim nothing measured. The remaining checks run exactly
as `runner.run_node_gate` runs them, on a synthesized `refactor` node: the
one kind whose coverage gate has full force.

Layering: this module imports `dag`, `evidence` and `runner`; nothing imports
it except the CLI (P1-5).
"""

from __future__ import annotations

import shutil
import tempfile
from collections.abc import Mapping
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Final, Literal

from saddle.dag import Node
from saddle.evidence import MutationOutcome, run_capture
from saddle.journal import SpanRecorder
from saddle.runner import run_node_gate

AUDIT_TEST_COMMAND: Final = "python -m pytest -q"

# Machine noise a run leaves behind. None of it is a change, so none of it may
# reach `git add -A` in the copy, whatever the audited tree's own `.gitignore`.
COPY_IGNORE: Final[tuple[str, ...]] = (
    "__pycache__",
    "*.pyc",
    ".pytest_cache",
    ".coverage*",
    ".saddle",
    ".hypothesis",
    ".ruff_cache",
    ".mutmut-cache",
    "mutants",
)

# Check name -> the reason printed as its detail.
NOT_APPLICABLE: Final[Mapping[str, str]] = {
    "node-scope": "audit: no plan assigns the diff a node kind",
    "target-scope": "audit: no plan declares target files",
    "requirement-binding": "audit: no plan declares requirements",
    "property-coverage": "audit: no plan pairs a property test with an implementation",
}


class AuditError(RuntimeError):
    """An unresolvable baseline, or a tree that is not a git repository."""


@dataclass(frozen=True)
class AuditCheck:
    name: str
    status: Literal["pass", "fail", "not-applicable"]
    detail: str
    basis: str | None


@dataclass(frozen=True)
class AuditResult:
    verdict: Literal["accept", "refuse", "nothing-to-audit"]
    tree: str  # `git write-tree` of the staged copy
    baseline: str  # the full 40-hex commit id `baseline` resolved to
    test_command: str
    checks: tuple[AuditCheck, ...]  # run_tier1's order; () for nothing-to-audit
    mutation: MutationOutcome | None  # None for nothing-to-audit

    def to_dict(self) -> dict[str, object]:
        """A JSON-serialisable view; `mutation` goes through `dataclasses.asdict`."""
        return {
            "verdict": self.verdict,
            "tree": self.tree,
            "baseline": self.baseline,
            "test_command": self.test_command,
            "checks": [asdict(check) for check in self.checks],
            "mutation": asdict(self.mutation) if self.mutation is not None else None,
        }


def audit_node(test_command: str = AUDIT_TEST_COMMAND) -> Node:
    """The synthesized node the audit gates as: kind `refactor`, no plan behind it."""
    return Node.model_validate(
        {
            "id": "audit",
            "kind": "refactor",
            "dependencies": [],
            "task_prompt": "Audit the tree against its baseline.",
            "requirements": [
                {
                    "id": "REQ-000",
                    "statement": "REQ-000 holds.",
                    "accepts": ["0"],
                    "rejects": ["1"],
                }
            ],
            "execution_constraints": {
                "reasoning_budget": "low",
                "allowed_tools": ["read_file", "write_file", "run_tests", "lint"],
                "max_context_tokens": 8000,
            },
            "deterministic_gate": {
                "test_command": test_command,
                "changed_line_coverage_min": 100.0,
                "red_phase_required": True,
                "mutation_sample": {"scope": "changed-lines"},
            },
        }
    )


def _git(copy: Path, *argv: str) -> str:
    """Stdout of `git <argv>` run in `copy`; an `AuditError` when it exits nonzero."""
    run = run_capture(["git", *argv], copy)
    if run.exit_code != 0:
        msg = f"git {' '.join(argv)} failed: {run.stderr.strip()}"
        raise AuditError(msg)
    return run.stdout.strip()


def audit_tree(
    tree: Path,
    baseline: str = "HEAD",
    *,
    test_command: str = AUDIT_TEST_COMMAND,
    recorder: SpanRecorder | None = None,
) -> AuditResult:
    """Gate `tree`, as it is on disk, against `baseline`; `tree` is never written to."""
    # A linked worktree's `.git` is a file naming a gitdir outside the tree: a
    # copy of it would still write to that original, and `git add -A` changes
    # the index even when no source byte moves.
    if not (tree / ".git").is_dir():
        msg = f"{tree} is not a git repository (no .git directory)"
        raise AuditError(msg)
    with tempfile.TemporaryDirectory() as scratch:
        copy = Path(scratch) / "tree"
        shutil.copytree(tree, copy, ignore=shutil.ignore_patterns(*COPY_IGNORE))
        # Untracked files must be staged: `git diff <ref>` sees tracked files
        # only, so a new module would be invisible to every gate.
        _git(copy, "add", "-A")
        try:
            resolved = _git(copy, "rev-parse", "--verify", f"{baseline}^{{commit}}")
        except AuditError as exc:
            msg = f"cannot resolve baseline {baseline!r}: {exc}"
            raise AuditError(msg) from exc
        staged = _git(copy, "write-tree")
        if staged == _git(copy, "rev-parse", f"{resolved}^{{tree}}"):
            return AuditResult(
                verdict="nothing-to-audit",
                tree=staged,
                baseline=resolved,
                test_command=test_command,
                checks=(),
                mutation=None,
            )
        gated = run_node_gate(audit_node(test_command), copy, baseline=resolved, recorder=recorder)
    checks = tuple(
        AuditCheck(
            name=check.name,
            status="not-applicable"
            if check.name in NOT_APPLICABLE
            else ("pass" if check.passed else "fail"),
            detail=NOT_APPLICABLE.get(check.name, check.detail),
            basis=check.basis,
        )
        for check in gated.checks
    )
    return AuditResult(
        verdict="refuse" if any(check.status == "fail" for check in checks) else "accept",
        tree=staged,
        baseline=resolved,
        test_command=test_command,
        checks=checks,
        mutation=gated.mutation,
    )
