"""Kahn asyncio scheduler (ARCHITECTURE.md §3 Phase 2).

An asyncio.Queue tracks in-degrees: a node enters the queue only at
in-degree 0, and only a successful worker run decrements dependents — so
in-degree 0 implies every parent proof exists, which is exactly
proof-gated dispatch. A failed node produces no proof, leaving its
downstream undispatched instead of retrying on a broken foundation.

Precondition: `dag` already passed `validate_dag` (Phase 1). Unknown
dependencies, duplicate ids, and cycles cannot reach the scheduler.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import dataclass

from saddle.dag import Dag, ExecutionConstraints, Node


@dataclass(frozen=True)
class Proof:
    """Worker-produced evidence that a node completed; `detail` is freeform."""

    node_id: str
    detail: str = ""


@dataclass(frozen=True)
class RunResult:
    """Outcome of a scheduler run: every node lands in exactly one bucket."""

    proofs: dict[str, Proof]
    failures: dict[str, Exception]
    undispatched: frozenset[str]


Worker = Callable[[Node, ExecutionConstraints], Awaitable[Proof]]
"""Runs one node under its per-node budget; returns its proof or raises."""


async def _serve(
    queue: asyncio.Queue[str],
    worker: Worker,
    by_id: dict[str, Node],
    dependents: dict[str, list[str]],
    indegree: dict[str, int],
    proofs: dict[str, Proof],
    failures: dict[str, Exception],
) -> None:
    # Single-threaded event loop: the decrement block below holds no `await`,
    # so no two workers can interleave inside it.
    while True:
        node_id = await queue.get()
        node = by_id[node_id]
        try:
            proof = await worker(node, node.execution_constraints)
        except Exception as exc:
            failures[node_id] = exc
        else:
            if proof.node_id != node_id:
                msg = f"worker proved {proof.node_id!r} while running node {node_id!r}"
                failures[node_id] = ValueError(msg)
            else:
                proofs[node_id] = proof
                for child in dependents[node_id]:
                    indegree[child] -= 1
                    if indegree[child] == 0:
                        queue.put_nowait(child)
        finally:
            queue.task_done()


async def schedule(dag: Dag, worker: Worker, *, max_workers: int = 5) -> RunResult:
    """Run `dag` to completion: at most `max_workers` nodes concurrently.

    Each dispatch hands the worker its node's own execution constraints as
    its budget. Parent puts strictly precede the parent's `task_done`, so
    `queue.join` returns exactly when every dispatchable node has settled;
    whatever remains is blocked behind a failure and reported undispatched.
    """
    if max_workers < 1:
        msg = f"max_workers must be >= 1, got {max_workers}"
        raise ValueError(msg)
    by_id = {node.id: node for node in dag.nodes}
    dependents: dict[str, list[str]] = {node.id: [] for node in dag.nodes}
    indegree = {node.id: len(node.dependencies) for node in dag.nodes}
    for node in dag.nodes:
        for dep in node.dependencies:
            dependents[dep].append(node.id)
    queue: asyncio.Queue[str] = asyncio.Queue()
    for node_id, degree in indegree.items():
        if degree == 0:
            queue.put_nowait(node_id)
    proofs: dict[str, Proof] = {}
    failures: dict[str, Exception] = {}
    workers = [
        asyncio.create_task(_serve(queue, worker, by_id, dependents, indegree, proofs, failures))
        for _ in range(max_workers)
    ]
    try:
        await queue.join()
    finally:
        for task in workers:
            task.cancel()
        await asyncio.gather(*workers, return_exceptions=True)
    undispatched = frozenset(by_id.keys() - proofs.keys() - failures.keys())
    return RunResult(proofs=proofs, failures=failures, undispatched=undispatched)
