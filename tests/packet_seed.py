"""Seed a run's branch, ledger and chat reference without a model (PACKET tests).

The ledger is built from the same `build_span`/`write_attempt_sidecar`
calls the engine seals with, so `compile_packet` reads it exactly as it
reads a real run's; the branch is a real `saddle/auto/<id>` worktree branch
made the way `auto.create_worktree` makes it.
"""

from __future__ import annotations

import json
import subprocess
import uuid
from pathlib import Path
from typing import Literal

from saddle.auditor import Auditor, AuditorConfig
from saddle.auto import create_worktree, ledger_path
from saddle.engine import GUARDED_STOP
from saddle.journal import append_span, build_span, write_attempt_sidecar
from saddle.packet import compile_packet, render_packet_text
from saddle.sessions import SessionStore
from saddle.web.tasks import RUN_REF, TaskRun, recap_message

FIXTURES = Path(__file__).parent / "fixtures"

GUARDED_SEEDED = ("src/saddle/audit.py", "tests/test_audit.py")
"""The guarded paths a `guarded` seed says its run changed."""

Kind = Literal[
    "audited", "mutated", "summarised", "unaudited", "stopped", "failed", "edit-checked",
    "budget", "guarded",
]  # fmt: skip


def git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(repo), "-c", "user.name=t", "-c", "user.email=t@t", *args],
        check=True,
        capture_output=True,
        text=True,
    ).stdout


def make_repo(root: Path) -> Path:
    (root / "tests").mkdir(parents=True)
    (root / "calc.py").write_text("def add(a, b):\n    return a - b\n")
    (root / "tests" / "test_calc.py").write_text(
        "from calc import add\n\n\ndef test_add():\n    assert add(2, 2) == 4\n"
    )
    (root / "README").write_text("calc\n")
    git(root, "init", "-q", "-b", "main")
    # The checkout's own identity: saddle's cherry-pick commits as the user, and
    # a run with no global git config (the audit's sandbox has an empty HOME)
    # has no other.
    git(root, "config", "user.name", "t")
    git(root, "config", "user.email", "t@t")
    git(root, "add", "-A")
    git(root, "commit", "-q", "-m", "init")
    return root


def seed(
    store: SessionStore,
    repo: Path,
    kind: Kind,
    *,
    sid: str | None = None,
    task: str = "make add add",
) -> tuple[str, str, str]:
    """A run of `kind` on `repo`, referenced from session `sid`: (sid, rid, branch)."""
    sid = sid or store.create(title="calc work", workdir=str(repo)).id
    rid = uuid.uuid4().hex[:12]
    worktree, branch = create_worktree(repo, rid)
    (worktree / "calc.py").write_text("def add(a, b):\n    return a + b\n")
    (worktree / "tests" / "test_zero.py").write_text(
        "from calc import add\n\n\ndef test_zero():\n    assert add(0, 0) == 0\n"
    )
    git(worktree, "add", "-A")
    git(worktree, "commit", "-q", "-m", f"saddle auto {rid}")
    journal = ledger_path(repo, rid)
    start = build_span(
        node_id="chat#1",
        argv=["auto:start", task],
        duration_ms=0,
        exit_code=0,
        detail=f"arm E+A+F; branch {branch}; test edits allowed",
        kind="agent",
    )
    append_span(journal, start)
    audits = {
        "audited": [("audit:tests", 0, "2 passed"), ("audit:coverage", 0, "covered")],
        "guarded": [("audit:tests", 0, "2 passed"), ("audit:coverage", 0, "covered")],
        "edit-checked": [("audit:tests", 0, "2 passed"), ("audit:coverage", 0, "covered")],
        "mutated": [
            ("audit:tests", 0, "2 passed"),
            ("audit:mutation", 0, "killed 2 of 2 changed-line mutants"),
            ("audit:coverage", 0, "covered"),
        ],
        "summarised": [("audit:tests", 0, "2 passed"), ("audit:coverage", 0, "covered")],
        "unaudited": [],
        "budget": [],  # stopped on its token budget before any audit
        "stopped": [("audit:tests", 0, "2 passed"), ("audit:coverage", 1, "line 2 uncovered")],
        "failed": [("audit:tests", 1, "1 failed")],
    }[kind]
    for name, code, detail in audits:
        append_span(
            journal,
            build_span(
                node_id="chat#1",
                argv=[name],
                duration_ms=0,
                exit_code=code,
                detail=detail,
                kind="agent",
                name=name,
                parent_id=start.span_id,
            ),
        )
    if kind == "summarised":
        # The real auditor's tier-2 mutation finding, with the MutationOutcome
        # it was decided from sealed beside it: the calibration tree
        # E-t5-s1's record, survivors and their diffs, verbatim.
        fixture = json.loads((FIXTURES / "mutant_text" / "E-t5-s1.json").read_text())
        sealed = {**fixture["outcome"], "survivor_detail": fixture["survivor_detail"]}
        finding = {
            "gate": "mutation",
            "tier": 2,
            "verdict": "fail",
            "reason": "evidence-thin",
            "detail": "killed 220 of 322 sampled mutants; 21 untested",
            "cites": [],
        }
        mutation_id = uuid.uuid4().hex
        append_span(
            journal,
            build_span(
                node_id="chat#1",
                argv=["saddle-audit", "tier2", "mutation", "k"],
                duration_ms=0,
                exit_code=1,
                detail=json.dumps(finding, sort_keys=True),
                name="audit-tier2:mutation",
                span_id=mutation_id,
                attempt_hash=write_attempt_sidecar(journal, mutation_id, sealed),
            ),
        )
    if kind == "edit-checked":
        # The real auditor's tier-0 records (syntax, ruff, imports) on the edit.
        config = AuditorConfig(journal=journal)
        Auditor(worktree, config=config).tier0("calc.py", (worktree / "calc.py").read_text())
    outcome = "stopped" if kind in ("stopped", "budget", "guarded") else "finished"
    reason = {
        "stopped": "audit unresolved",
        "budget": "token budget exhausted: ~100000 of 100000 generated tokens spent",
        # the self-guard's hold, as engine._hold_guarded seals it
        "guarded": GUARDED_STOP.format(paths=", ".join(GUARDED_SEEDED)),
    }.get(kind, "finish called")
    evidence = {
        "outcome": outcome,
        "reason": reason,
        "narrative": "Tests pass now. add adds.",
        "files_changed": ["calc.py", "tests/test_zero.py"],
        "rounds": 3,
        "tool_span_hashes": [],
        "tokens_spent": 1200,
        "token_source": "usage",
        "token_budget": 100000,
        "elapsed_s": 42.0,
        "time_budget_s": 600,
        "unresolved_findings": (
            [{"gate": "coverage", "reason": "evidence-thin"}] if kind == "stopped" else []
        ),
        **({"guarded_paths": list(GUARDED_SEEDED)} if kind == "guarded" else {}),
    }
    span_id = uuid.uuid4().hex
    digest = write_attempt_sidecar(journal, span_id, evidence)
    append_span(
        journal,
        build_span(
            node_id="chat#1",
            argv=[f"auto:{outcome}"],
            duration_ms=42000,
            exit_code=0 if outcome == "finished" else 3,
            detail=f"{outcome}: {reason}; arm E+A+F",
            kind="agent",
            parent_id=start.span_id,
            span_id=span_id,
            attempt_hash=digest,
        ),
    )
    run = TaskRun(
        run_id=rid,
        session_id=sid,
        task=task,
        time_budget_s=600,
        token_budget=100000,
        journal=journal,
    )
    append_span(
        store.journal_path(sid),
        build_span(
            node_id=f"task:{rid}",
            argv=[RUN_REF, rid, task, str(journal), start.span_id],
            duration_ms=0,
            exit_code=0,
            detail=f"{outcome}: seeded",
            kind="agent",
            name=RUN_REF,
        ),
    )
    messages = store.load_messages(sid)
    messages.append(recap_message(run, render_packet_text(compile_packet(journal, run_id=rid))))
    store.save_messages(sid, messages)
    return sid, rid, branch
