"""Saddle command line interface.

`run` drives one mechanical task end to end: guided DAG emission with
bounded recompile, plan confirmation, then the schedule-gate-seal path.
"""

from __future__ import annotations

import argparse
import os
import sys
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import IO, Final

from pydantic import ValidationError

from saddle import __version__
from saddle.dag import Dag, Node, validate_dag
from saddle.evidence import git_ls_files, run_argv
from saddle.slice import run_slice
from saddle.ux import ask_confirm
from saddle.vllm import (
    DEFAULT_BASE_URL,
    DEFAULT_MODEL,
    REASONING_EFFORTS,
    DiffProposal,
    VllmAuthError,
    VllmClient,
    VllmError,
    VllmRequestError,
    VllmResponseError,
)

RUN_ALLOWLIST: Final[tuple[str, ...]] = ("read_file", "write_file", "run_tests", "lint")
CONTEXT_CEILING: Final = 30000
EMIT_ROUNDS: Final = 3
MAX_FILES_IN_PROMPT: Final = 100
MAX_CONTEXT_CHARS: Final = 8000
BUDGET_TO_EFFORT: Final[dict[str, str]] = {
    "zero": "none",
    "low": "low",
    "medium": "medium",
    "xhigh": "xhigh",
}


class RunError(Exception):
    """Operational `run` failure with a message fit to print."""


@dataclass(frozen=True)
class RunOptions:
    """Resolved `run` inputs: task, repo, journal, and sampling knobs."""

    task: str
    repo: Path
    journal: Path
    max_tokens: int = 8192
    temperature: float = 0.0
    reasoning_effort: str = "medium"
    yes: bool = False


def build_emit_prompt(task: str) -> str:
    """Decomposition prompt: task plus the DAG vocabulary rules."""
    return f"""Decompose the mechanical coding task below into a DAG of 1 to 4 nodes.

Task: {task}

Rules:
- The first node has no dependencies; every other node depends on at least one earlier node.
- requirement_ids look like REQ-001, REQ-002, ... (at least one per node).
- reasoning_budget is one of: zero, low, medium, xhigh.
- allowed_tools uses only: read_file, write_file, run_tests, lint.
- max_context_tokens is between 1000 and 30000.
- test_command is a pytest invocation over test files only,
  e.g. "pytest tests/test_login.py" (never a source file).
- changed_line_coverage_min and kill_threshold are 0-100 numbers.
- mutation_sample.scope is always "changed-lines"; max_mutants is 1-1000.
- Prefer the fewest nodes that cover the task; a trivial task needs one node.
- Think through the decomposition first; then emit the plan.
"""


def build_worker_prompt(
    *, task: str, node: Node, files: Sequence[str], contents: Mapping[str, str]
) -> str:
    """Node work prompt: task, requirements, repo files, diff format rules."""
    listed = list(files)
    if not listed:
        shown = "(no tracked files)"
    else:
        shown = "\n".join(listed[:MAX_FILES_IN_PROMPT])
        if len(listed) > MAX_FILES_IN_PROMPT:
            shown += f"\n... and {len(listed) - MAX_FILES_IN_PROMPT} more"
    context = "\n\n".join(f"--- {name} ---\n{text}" for name, text in contents.items())
    if len(context) > MAX_CONTEXT_CHARS:
        context = context[:MAX_CONTEXT_CHARS] + "\n[file context truncated]"
    reqs = ", ".join(node.requirement_ids)
    return f"""Task: {task}

Node {node.id}: {node.task_prompt}
Requirements: {reqs}
Gate command: {node.deterministic_gate.test_command}

Repo files:
{shown}

File contents:
{context}

Produce a unified diff (git apply compatible) implementing this node's task.
Rules:
- Start each file section with a "diff --git a/<file> b/<file>" header line.
- Mark new files with "new file mode 100644".
- Mention each requirement ID in the new or changed test source.
- Make sure the gate command above passes after the diff applies.
- Keep new code ruff-clean: double quotes, 4-space indent,
  two blank lines between top-level definitions, final newline, no unused imports,
  sorted import blocks with stdlib, third-party, and local groups separated by blank lines.
- Every changed line must be executed by the new tests.

Output ONLY the diff, no commentary.
"""


def _git_ok(argv: Sequence[str], repo: Path, message: str) -> None:
    """Run a git setup command, raising RunError with `message` on failure."""
    if run_argv(argv, repo) != 0:
        raise RunError(message)


def _ensure_repo(repo: Path) -> bool:
    """Create and baseline a repo when missing; True when anything was set up."""
    if repo.exists() and not repo.is_dir():
        msg = f"{str(repo)!r} is not a directory"
        raise RunError(msg)
    repo.mkdir(parents=True, exist_ok=True)
    if (repo / ".git").exists():
        # -e exits 0 iff HEAD resolves (silent, so no --quiet needed).
        if run_argv(["git", "cat-file", "-e", "HEAD^{commit}"], repo) == 0:
            return False
    else:
        init = f"could not init a git repo in {str(repo)!r}"
        _git_ok(["git", "init", "--quiet"], repo, init)
    stage = f"could not stage baseline in {str(repo)!r}"
    _git_ok(["git", "add", "-A"], repo, stage)
    commit = f"could not commit baseline in {str(repo)!r}"
    _git_ok(
        [
            "git",
            "-c",
            "user.name=saddle",
            "-c",
            "user.email=saddle@local",
            "commit",
            "--quiet",
            "--allow-empty",
            "-m",
            "saddle baseline",
        ],
        repo,
        commit,
    )
    return True


def _ensure_clean(repo: Path) -> None:
    """Refuse repos with uncommitted changes against HEAD (tracked tree)."""
    if run_argv(["git", "diff-index", "--quiet", "HEAD", "--"], repo) != 0:
        msg = f"{str(repo)!r} has uncommitted changes; commit or stash first"
        raise RunError(msg)


def _emit_valid_dag(
    client: VllmClient,
    task: str,
    *,
    max_tokens: int,
    temperature: float,
    reasoning_effort: str,
) -> Dag:
    """Emit a DAG, feeding validation errors back (bounded recompile)."""
    prompt = build_emit_prompt(task)
    errors: list[str] = []
    for _ in range(EMIT_ROUNDS):
        attempt = prompt
        if errors:
            attempt += "\nPrevious attempt failed:\n" + "\n".join(errors)
        try:
            emission = client.emit_dag(
                attempt,
                max_tokens=max_tokens,
                temperature=temperature,
                reasoning_effort=reasoning_effort,
            )
        except VllmResponseError as exc:
            errors.append(str(exc))
            continue
        except (VllmAuthError, VllmRequestError) as exc:
            raise RunError(str(exc)) from exc
        try:
            dag = Dag.model_validate(emission.dag)
        except ValidationError as exc:
            errors.append(str(exc))
            continue
        required = {req for node in dag.nodes for req in node.requirement_ids}
        issues = validate_dag(
            dag,
            allowed_tools=RUN_ALLOWLIST,
            context_ceiling=CONTEXT_CEILING,
            required_ids=sorted(required),
        )
        if not issues:
            return dag
        errors.extend(f"{issue.code}: {issue.message}" for issue in issues)
    msg = f"could not emit a valid DAG in {EMIT_ROUNDS} rounds: {'; '.join(errors)}"
    raise RunError(msg)


def check_server(client: VllmClient, *, base_url: str, model: str) -> list[str]:
    """Preflight: reachability, key, model match. Returns served ids."""
    try:
        ids = client.list_models()
    except VllmError as exc:
        msg = f"preflight failed at {base_url}: {exc}"
        raise RunError(msg) from exc
    if model not in ids:
        served = ", ".join(ids) if ids else "(none)"
        msg = f"preflight failed at {base_url}: model {model!r} is not served (served: {served})"
        raise RunError(msg)
    return ids


def run_task(options: RunOptions, client: VllmClient, *, stdin: IO[str], stdout: IO[str]) -> int:
    """Drive one task: emit, confirm, schedule, gate, seal, transcribe."""
    try:
        if _ensure_repo(options.repo):
            stdout.write(f"created baseline commit in {str(options.repo)!r}\n")
        _ensure_clean(options.repo)
        dag = _emit_valid_dag(
            client,
            options.task,
            max_tokens=options.max_tokens,
            temperature=options.temperature,
            reasoning_effort=options.reasoning_effort,
        )
    except RunError as exc:
        stdout.write(f"error: {exc}\n")
        return 1
    ids = ", ".join(node.id for node in dag.nodes)
    stdout.write(f"Plan: {len(dag.nodes)} node(s): {ids}\n")
    if not options.yes and not ask_confirm(
        f"Run {len(dag.nodes)} node(s)?", stdin=stdin, stdout=stdout
    ):
        stdout.write("aborted.\n")
        return 1

    def propose(node: Node) -> DiffProposal:
        budget = node.execution_constraints.reasoning_budget
        files = git_ls_files(options.repo)
        contents = {
            name: (options.repo / name).read_text() for name in files if name.endswith(".py")
        }
        return client.propose_diff(
            build_worker_prompt(task=options.task, node=node, files=files, contents=contents),
            temperature=options.temperature,
            reasoning_effort=BUDGET_TO_EFFORT[budget],
        )

    try:
        result = run_slice(
            options.task,
            dag,
            workdir=options.repo,
            journal_path=options.journal,
            propose=propose,
        )
    except (ValueError, RuntimeError) as exc:
        stdout.write(f"error: {exc}\n")
        return 1
    stdout.write(result.transcript)
    return 0 if result.passed else 1


def run_doctor(base_url: str, model: str, client: VllmClient, *, stdout: IO[str]) -> int:
    """Report the server preflight verdict; 0 when the server is usable."""
    try:
        ids = check_server(client, base_url=base_url, model=model)
    except RunError as exc:
        stdout.write(f"error: {exc}\n")
        return 1
    stdout.write(f"OK: {base_url} serves {model} (models: {', '.join(ids)})\n")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="saddle", description="Deterministic harness for local LLMs."
    )
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    sub = parser.add_subparsers(dest="command")
    doctor = sub.add_parser("doctor", help="Check the server is usable.")
    doctor.add_argument("--base-url", default=DEFAULT_BASE_URL, help="vLLM base URL.")
    doctor.add_argument("--model", default=DEFAULT_MODEL, help="Model id.")
    run = sub.add_parser("run", help="Drive one mechanical task end to end.")
    run.add_argument("task", help="Task description to decompose and execute.")
    run.add_argument("--repo", default=".", help="Directory to work in (repo created if missing).")
    run.add_argument("--journal", help="Journal path (default: REPO/.saddle/proofs.jsonl).")
    run.add_argument("--base-url", default=DEFAULT_BASE_URL, help="vLLM base URL.")
    run.add_argument("--model", default=DEFAULT_MODEL, help="Model id.")
    run.add_argument("--max-tokens", type=int, default=8192, help="Emission max tokens.")
    run.add_argument("--temperature", type=float, default=0.0, help="Sampling temperature.")
    run.add_argument(
        "--reasoning-effort",
        choices=list(REASONING_EFFORTS),
        default="medium",
        help="Emission reasoning effort.",
    )
    run.add_argument("--yes", action="store_true", help="Skip the plan confirmation.")
    return parser


def _api_key() -> str | None:
    return os.environ.get("SADDLE_VLLM_API_KEY") or os.environ.get("VLLM_API_KEY")


def main(
    argv: list[str] | None = None,
    *,
    stdin: IO[str] | None = None,
    stdout: IO[str] | None = None,
    stderr: IO[str] | None = None,
) -> int:
    args = build_parser().parse_args(argv)
    if args.command not in ("run", "doctor"):
        return 0
    key = _api_key()
    if not key:
        print("error: set SADDLE_VLLM_API_KEY (or VLLM_API_KEY)", file=stderr or sys.stderr)
        return 1
    if args.command == "doctor":
        with VllmClient(api_key=key, base_url=args.base_url, model=args.model) as client:
            return run_doctor(args.base_url, args.model, client, stdout=stdout or sys.stdout)
    repo = Path(args.repo)
    journal = Path(args.journal) if args.journal else repo / ".saddle" / "proofs.jsonl"
    options = RunOptions(
        task=args.task,
        repo=repo,
        journal=journal,
        max_tokens=args.max_tokens,
        temperature=args.temperature,
        reasoning_effort=args.reasoning_effort,
        yes=args.yes,
    )
    with VllmClient(api_key=key, base_url=args.base_url, model=args.model) as client:
        try:
            check_server(client, base_url=args.base_url, model=args.model)
        except RunError as exc:
            print(f"error: {exc}", file=stderr or sys.stderr)
            return 1
        return run_task(
            options,
            client,
            stdin=stdin or sys.stdin,
            stdout=stdout or sys.stdout,
        )
