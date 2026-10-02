"""Audit a tree against a baseline with the Tier-1 battery and no plan.

`audit_tree(tree, baseline)` gates `tree` against `baseline` and returns one
verdict: `nothing-to-audit` when the staged tree equals the baseline's tree,
`accept` when every applicable check passed, `refuse` otherwise. The tree is
taken as it is on disk: untracked files count (a new module is the least-
tested code in a tree, and `git diff` never shows it), and what git ignores
does not: it is never even copied, so no gate reads a project's `.venv`. The
audit works on a copy, so the audited tree -- its files and its `.git/index`
alike -- is never written to.

Four checks are plan-relative: `node-scope`, `target-scope`,
`requirement-binding` and `property-coverage` each need something only a plan
supplies. Without one they are reported `not-applicable`, never `pass`, since
a `pass` would be a claim nothing measured. The remaining checks run exactly
as `runner.run_node_gate` runs them, on a synthesized `refactor` node: the
one kind whose coverage gate has full force.

With `cache=<dir>`, `audit_tree` serves a stored `AuditResult` only
when the staged tree, the resolved baseline, `test_command` and the gate
surface (`gate_surface()`: a hash of the gate modules' bytes plus the shelled-
out tools' versions) all match a stored key exactly. Any other file state --
a missing file, unparseable JSON, a mismatched key -- is a miss, never a
verdict and never an exception. `nothing-to-audit` is never cached. The
tests run under the project's time limit (`evidence.suite_limit`) and on its
worker count (`evidence.suite_workers`), and with the commands the project asks
its sandbox to show (`evidence.sandbox_expose`), all read at the resolved
baseline and so already named by the key.

Layering: this module imports `dag`, `evidence`, `gates`, `runner` and `sandbox`; only the
CLI and `saddle.auditor` import it.
"""

from __future__ import annotations

import dataclasses
import hashlib
import importlib
import importlib.metadata
import json
import os
import platform
import shutil
import sys
import tempfile
from collections.abc import Callable, Iterator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import asdict, dataclass
from pathlib import Path, PurePosixPath
from typing import Any, Final, Literal, cast

from saddle import sandbox
from saddle.dag import Node
from saddle.evidence import (
    MutationOutcome,
    SuiteLimitError,
    run_capture,
    sandbox_expose,
    suite_limit,
    suite_workers,
)
from saddle.gates import GateCheck
from saddle.journal import SpanRecorder
from saddle.runner import run_node_gate

AUDIT_TEST_COMMAND: Final = "python -m pytest -q"

# The CLI expands and uses this; the library never defaults to it.
DEFAULT_AUDIT_CACHE: Final = Path("~/.cache/saddle/audit")

# The modules whose bytes decide a verdict, and the tools the gates shell out
# to, in the order `gate_surface()` hashes them.
SURFACE_MODULES: Final[tuple[str, ...]] = (
    "saddle.audit",
    "saddle.auditor",
    "saddle.runner",
    "saddle.gates",
    "saddle.evidence",
    "saddle.dag",
    "saddle.task_units",
    "saddle.task_examples",
    "saddle.task_requirements",
)
SURFACE_TOOLS: Final[tuple[str, ...]] = ("mutmut", "ruff", "coverage")
# The programs the gates start by bare name under the default test command
# (`python -m pytest`, run as `python -m coverage run -m pytest`), or under
# the `pytest ...` spelling of it. Each must resolve on `sandbox.gate_path`.
GATE_COMMANDS: Final[tuple[str, ...]] = ("python", "pytest", "coverage", "ruff", "mutmut")

# Machine noise a run leaves behind, ignored at any depth: none of it is a
# change, so none of it may reach `git add -A` in the copy, whatever the
# audited tree's own `.gitignore`.
_COPY_IGNORE_ANY_DEPTH: Final[frozenset[str]] = frozenset(
    {"__pycache__", ".pytest_cache", ".hypothesis", ".ruff_cache", ".mutmut-cache"}
)
# Noise saddle and mutmut write only at the tree's own top level: a
# pattern here must not eat a tracked file of the same name deeper in the
# tree (`pkg/mutants/__init__.py`) or a same-prefixed one at the top
# (`.coveragerc`, a project's own coverage config, is not `.coverage.*`).
_COPY_IGNORE_TOP_LEVEL: Final[frozenset[str]] = frozenset({".saddle", "mutants", ".coverage"})

# Check name -> the reason printed as its detail.
NOT_APPLICABLE: Final[Mapping[str, str]] = {
    "node-scope": "audit: no plan assigns the diff a node kind",
    "target-scope": "audit: no plan declares target files",
    "requirement-binding": "audit: no plan declares requirements",
    "property-coverage": "audit: no plan pairs a property test with an implementation",
}


class AuditError(RuntimeError):
    """An unresolvable baseline, or a tree that is not a git repository."""


class GateSetupError(AuditError):
    """saddle's own installation cannot run a gate: a tool is missing.

    Never a verdict. A gate that cannot run has measured nothing about the
    tree, so reporting it as `refuse` would refuse correct work for a fault
    in the install."""


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
    surface: str  # gate_surface() at the time this result was produced
    cached: bool = False  # True when served from a stored key match, not gated

    def to_dict(self) -> dict[str, object]:
        """A JSON-serialisable view; `mutation` goes through `dataclasses.asdict`."""
        return {
            "verdict": self.verdict,
            "tree": self.tree,
            "baseline": self.baseline,
            "test_command": self.test_command,
            "checks": [asdict(check) for check in self.checks],
            "mutation": _mutation_dict(self.mutation) if self.mutation is not None else None,
            "surface": self.surface,
            "cached": self.cached,
        }

    @staticmethod
    def from_dict(data: Mapping[str, Any]) -> AuditResult:
        """The exact inverse of `to_dict`: every JSON list becomes a tuple, recursively."""
        mutation_data = data["mutation"]
        mutation = MutationOutcome(**_tuplify(mutation_data)) if mutation_data is not None else None
        return AuditResult(
            verdict=data["verdict"],
            tree=data["tree"],
            baseline=data["baseline"],
            test_command=data["test_command"],
            checks=tuple(AuditCheck(**check) for check in data["checks"]),
            mutation=mutation,
            surface=data["surface"],
            cached=data["cached"],
        )


def _mutation_dict(mutation: MutationOutcome) -> dict[str, Any]:
    """`asdict(mutation)` without `survivor_details` or `mutant_detail`: the
    shortlist's in-process evidence and the audit span's per-mutant record,
    neither part of `saddle audit --json`."""
    data = asdict(mutation)
    del data["survivor_details"]
    del data["mutant_detail"]
    return data


def _tuplify(value: Any) -> Any:
    """Recursively turn every JSON list into a tuple: `asdict`'s exact inverse for tuples."""
    if isinstance(value, list):
        return tuple(_tuplify(item) for item in value)
    if isinstance(value, dict):
        return {key: _tuplify(item) for key, item in value.items()}
    return value


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


def _audit_ignore(root: Path) -> Callable[[str, list[str]], set[str]]:
    """`shutil.copytree`'s `ignore` callable: some names are noise at any depth,
    others only at `root` -- where saddle and mutmut actually write them. A
    pattern scoped to the top level must never key off a *prefix* that a
    same-named top-level project file also has (`.coveragerc` starts with
    `.coverage` but is not run noise).

    A name is dropped only when it matches a noise pattern **and** git does not
    track it: neither the path itself nor any path beneath it is in
    `git ls-files`. A tracked file is part of the baseline, and a copy without
    it reads as a deletion, so an unchanged tree stops being `nothing-to-audit`.
    21 of the 88 labelled bench trees (every T2, T3 and T4 tree) track
    `__pycache__`/`*.pyc`; the top-level list fixed `.coveragerc` by pattern,
    this is the rule it was an instance of.

    Every path git ignores in the tree (`git_ignored`) is dropped as well, at
    any depth. `git add -A` never stages one, so it is no part of the tree a
    verdict is keyed by, and a gate that reads the copy's files must not see
    it: a project's gitignored `.venv` put its site-packages `test_*.py` files
    in the copy, the runner read them as changed tests the baseline lacks, and
    red-phase refused a correct refactor.
    """
    root_str = os.fspath(root)
    listed = run_capture(["git", "ls-files", "-z"], root)
    if listed.exit_code != 0:
        msg = f"git ls-files failed: {listed.stderr.strip()}"
        raise AuditError(msg)
    tracked = {name for name in listed.stdout.split("\0") if name}
    kept = tracked | {
        str(parent)
        for name in tracked
        for parent in PurePosixPath(name).parents
        if str(parent) != "."
    }
    ignored = git_ignored(root)

    def ignore(directory: str, names: list[str]) -> set[str]:
        below = os.path.relpath(directory, root_str)
        prefix = "" if below == "." else f"{below}/"
        skip = {
            name
            for name in names
            if name in _COPY_IGNORE_ANY_DEPTH
            or name.endswith(".pyc")
            or f"{prefix}{name}" in ignored
        }
        if directory == root_str:
            skip |= {
                name
                for name in names
                if name in _COPY_IGNORE_TOP_LEVEL or name.startswith(".coverage.")
            }
        return {name for name in skip if f"{prefix}{name}" not in kept}

    return ignore


def git_ignored(root: Path) -> frozenset[str]:
    """Every path under `root` that git ignores there, posix-relative.

    `git ls-files --others --ignored --exclude-standard --directory`: the
    `.gitignore` files at every depth, `.git/info/exclude` and the user's
    excludes file all count; a directory ignored whole is named once (`.venv`,
    never its files); a tracked file is never named, since git ignores only
    untracked ones. An `AuditError` when git cannot say: a failed lookup must
    not read as "nothing is ignored", which would copy a project's venv again.
    """
    listed = run_capture(
        ["git", "ls-files", "-z", "--others", "--ignored", "--exclude-standard", "--directory"],
        root,
    )
    if listed.exit_code != 0:
        msg = f"git ls-files failed: {listed.stderr.strip()}"
        raise AuditError(msg)
    return frozenset(name.rstrip("/") for name in listed.stdout.split("\0") if name)


def gate_surface(
    files: Sequence[Path] | None = None,
    versions: Mapping[str, str] | None = None,
) -> str:
    """A hex sha256 of what decides a verdict: gate module bytes, tool versions,
    and the interpreter version. Two audits agree only when all three agree.

    With no arguments, hashes each module named in `SURFACE_MODULES`' `__file__`
    bytes in order, then `importlib.metadata.version(t)` for each tool named in
    `SURFACE_TOOLS`, then `platform.python_version()`. The arguments let a test
    pass explicit files and versions instead.
    """
    if files is None:
        # `__file__` is `str | None` in general (a frozen or namespace import has
        # none), but every real module on disk has one; `cast` records that
        # without adding a branch a test would have to exercise to cover.
        files = [
            Path(cast(str, importlib.import_module(name).__file__)) for name in SURFACE_MODULES
        ]
    if versions is None:
        check_gate_commands()
        versions = {tool: _installed_version(tool) for tool in SURFACE_TOOLS}
    digest = hashlib.sha256()
    for path in files:
        digest.update(Path(path).read_bytes())
    for tool, version in versions.items():
        digest.update(f"{tool}={version}\n".encode())
    digest.update(platform.python_version().encode())
    return digest.hexdigest()


def check_gate_commands() -> None:
    """A `GateSetupError` naming every `GATE_COMMANDS` program the gates
    cannot find, looked up exactly as they look it up (`sandbox.gate_path`).

    Checked before any gate runs: a tool that fails to launch mid-battery
    reads as a failing check, and a failing check is a refusal."""
    path = sandbox.gate_path(os.environ.get("PATH", ""))
    missing = [name for name in GATE_COMMANDS if shutil.which(name, path=path) is None]
    if missing:
        msg = (
            f"setup: gate tools not found beside saddle ({sandbox.tool_dir()}) or on PATH:"
            f" {', '.join(missing)}; reinstall saddle-harness with its dependencies"
        )
        raise GateSetupError(msg)


def _installed_version(tool: str) -> str:
    """`tool`'s installed version, or a `GateSetupError` naming it."""
    try:
        return importlib.metadata.version(tool)
    except importlib.metadata.PackageNotFoundError as exc:
        msg = (
            f"setup: the gate tool {tool!r} is not installed beside saddle"
            f" ({sys.executable}); reinstall saddle-harness with its dependencies"
        )
        raise GateSetupError(msg) from exc


def _cache_key(tree: str, baseline: str, test_command: str, surface: str) -> dict[str, str]:
    return {"tree": tree, "baseline": baseline, "test_command": test_command, "surface": surface}


def _cache_path(cache: Path, key: Mapping[str, str]) -> Path:
    digest = hashlib.sha256(json.dumps(key, sort_keys=True).encode()).hexdigest()
    return cache / f"{digest}.json"


def _cache_read(path: Path, key: Mapping[str, str]) -> AuditResult | None:
    """A stored result whose key matches `key` exactly; any other file state is
    a miss, never a verdict and never an exception."""
    try:
        raw = path.read_text()
    except OSError:
        return None
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        return None
    if not isinstance(data, dict) or data.get("key") != key or "result" not in data:
        return None
    try:
        stored = AuditResult.from_dict(data["result"])
    except (KeyError, TypeError, ValueError):
        # A matching key over a result that cannot be rebuilt (a hand edit, a
        # partial write that still parses) is a miss like any other bad file.
        return None
    return dataclasses.replace(stored, cached=True)


def _cache_write(cache: Path, path: Path, key: Mapping[str, str], result: AuditResult) -> None:
    """Write `{"key": key, "result": ...}` atomically: a temp file, then `os.replace`."""
    cache.mkdir(parents=True, exist_ok=True)
    payload = json.dumps({"key": key, "result": result.to_dict()}, sort_keys=True)
    handle, tmp_name = tempfile.mkstemp(dir=cache, prefix=".audit-tmp-")
    with os.fdopen(handle, "w") as tmp_file:
        tmp_file.write(payload)
    os.replace(tmp_name, path)


def _spelled_from_the_root(
    checks: tuple[AuditCheck, ...], mutation: MutationOutcome | None, copy: Path
) -> tuple[tuple[AuditCheck, ...], MutationOutcome | None]:
    """Rewrite every path the gates spelled inside `copy` relative to the tree's root.

    The gates gate a temporary copy and name files as `<copy>/accounts.py:84`,
    a directory deleted when the audit returns and named differently every
    run. No string of an `AuditResult` may name it: `detail` and `basis` lose
    the `<copy>/` prefix, and each `survivor_lines` path goes relative, so two
    audits of one tree are byte-identical and an editor can open what is named.
    """
    prefix = f"{copy}{os.sep}"
    checks = tuple(
        dataclasses.replace(
            check,
            detail=check.detail.replace(prefix, ""),
            basis=None if check.basis is None else check.basis.replace(prefix, ""),
        )
        for check in checks
    )
    if mutation is not None:
        mutation = dataclasses.replace(
            mutation,
            survivor_lines=tuple(
                (os.path.relpath(path, copy), line) for path, line in mutation.survivor_lines
            ),
            survivor_details=tuple(
                (name, status, os.path.relpath(path, copy), line, text, message)
                for name, status, path, line, text, message in mutation.survivor_details
            ),
        )
    return checks, mutation


@contextmanager
def staged_copy(tree: Path, baseline: str) -> Iterator[tuple[Path, str, str]]:
    """A scratch copy of `tree` with everything staged: `(copy, staged, resolved)`.

    The copy leaves out what git ignores in `tree` and saddle's own run
    noise (`_audit_ignore`), and holds every other file, tracked or not.
    `staged` is the copy's `git write-tree`, the key every audit verdict is
    stored under; `resolved` is the 40-hex commit `baseline` names. The
    copy is deleted on exit, so `tree` -- its files and `.git/index` -- is
    never written to. Shared by `audit_tree` and `saddle.auditor`.
    """
    # A linked worktree's `.git` is a file naming a gitdir outside the tree: a
    # copy of it would still write to that original, and `git add -A` changes
    # the index even when no source byte moves.
    if not (tree / ".git").is_dir():
        msg = f"{tree} is not a git repository (no .git directory)"
        raise AuditError(msg)
    with tempfile.TemporaryDirectory() as scratch:
        copy = Path(scratch) / "tree"
        shutil.copytree(tree, copy, ignore=_audit_ignore(tree))
        # Untracked files must be staged: `git diff <ref>` sees tracked files
        # only, so a new module would be invisible to every gate.
        _git(copy, "add", "-A")
        try:
            resolved = _git(copy, "rev-parse", "--verify", f"{baseline}^{{commit}}")
        except AuditError as exc:
            msg = f"cannot resolve baseline {baseline!r}: {exc}"
            raise AuditError(msg) from exc
        yield copy, _git(copy, "write-tree"), resolved


def baseline_tree(copy: Path, resolved: str) -> str:
    """The tree id of commit `resolved`, read in `copy`."""
    return _git(copy, "rev-parse", f"{resolved}^{{tree}}")


def audit_checks(
    gate_checks: Sequence[GateCheck], mutation: MutationOutcome | None, copy: Path
) -> tuple[tuple[AuditCheck, ...], MutationOutcome | None]:
    """Gate checks as audit checks: plan-relative ones `not-applicable`, paths
    spelled from the tree's root rather than the scratch `copy`."""
    checks = tuple(
        AuditCheck(
            name=check.name,
            status="not-applicable"
            if check.name in NOT_APPLICABLE
            else ("pass" if check.passed else "fail"),
            detail=NOT_APPLICABLE.get(check.name, check.detail),
            basis=check.basis,
        )
        for check in gate_checks
    )
    return _spelled_from_the_root(checks, mutation, copy)


def audit_tree(
    tree: Path,
    baseline: str = "HEAD",
    *,
    test_command: str = AUDIT_TEST_COMMAND,
    recorder: SpanRecorder | None = None,
    cache: Path | None = None,
) -> AuditResult:
    """Gate `tree`, as it is on disk, against `baseline`; `tree` is never written to.

    With `cache=None`, exactly the uncached audit's behaviour, plus `surface` and `cached` on
    the result. With a directory, a stored result is served only on a key hit
    (see the module docstring); `nothing-to-audit` is never cached.
    """
    surface = gate_surface()
    with staged_copy(tree, baseline) as (copy, staged, resolved):
        if staged == baseline_tree(copy, resolved):
            return AuditResult(
                verdict="nothing-to-audit",
                tree=staged,
                baseline=resolved,
                test_command=test_command,
                checks=(),
                mutation=None,
                surface=surface,
                cached=False,
            )
        key = _cache_key(staged, resolved, test_command, surface)
        cache_file = _cache_path(cache, key) if cache is not None else None
        if cache_file is not None:
            hit = _cache_read(cache_file, key)
            if hit is not None:
                return hit
        try:
            limit = suite_limit(copy, resolved).seconds
            workers = suite_workers(copy, resolved).count
            exposed = sandbox_expose(copy, resolved)
        except SuiteLimitError as exc:
            raise AuditError(str(exc)) from exc
        with sandbox.also_exposing(exposed):
            gated = run_node_gate(
                audit_node(test_command),
                copy,
                baseline=resolved,
                recorder=recorder,
                test_timeout=limit,
                test_workers=workers,
                test_only_additions=True,
                js_tools=tree,
            )
        checks, mutation = audit_checks(gated.checks, gated.mutation, copy)
    result = AuditResult(
        verdict="refuse" if any(check.status == "fail" for check in checks) else "accept",
        tree=staged,
        baseline=resolved,
        test_command=test_command,
        checks=checks,
        mutation=mutation,
        surface=surface,
        cached=False,
    )
    if cache is not None and cache_file is not None:
        _cache_write(cache, cache_file, key, result)
    return result
