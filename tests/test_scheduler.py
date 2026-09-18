"""Tests for saddle.scheduler: Kahn dispatch, proof gating, worker pool."""

from __future__ import annotations

import asyncio
from collections.abc import Coroutine
from typing import Any

import pytest

from saddle.dag import Dag, ExecutionConstraints, Node
from saddle.scheduler import Proof, RunResult, schedule


def _node(
    node_id: str,
    *,
    deps: list[str] | None = None,
    budget: str = "low",
) -> dict[str, Any]:
    return {
        "id": node_id,
        "dependencies": deps if deps is not None else [],
        "task_prompt": f"Do {node_id}.",
        "requirement_ids": ["REQ-001"],
        "execution_constraints": {
            "reasoning_budget": budget,
            "allowed_tools": ["read_file"],
            "max_context_tokens": 8000,
        },
        "deterministic_gate": {
            "test_command": "pytest tests/test_x.py",
            "changed_line_coverage_min": 100.0,
            "red_phase_required": True,
            "mutation_sample": {
                "scope": "changed-lines",
                "max_mutants": 10,
                "kill_threshold": 85.0,
            },
        },
    }


def _diamond() -> Dag:
    return Dag.model_validate(
        {
            "nodes": [
                _node("a", budget="low"),
                _node("b", deps=["a"], budget="xhigh"),
                _node("c", deps=["a"], budget="low"),
                _node("d", deps=["b", "c"], budget="medium"),
            ]
        }
    )


def _single() -> Dag:
    return Dag.model_validate({"nodes": [_node("a")]})


def _run(coro: Coroutine[Any, Any, RunResult]) -> RunResult:
    # Guard: a scheduler deadlock must fail loudly, never hang the suite.
    async def main() -> RunResult:
        return await asyncio.wait_for(coro, 10.0)

    return asyncio.run(main())


def test_diamond_branches_overlap_and_converge() -> None:
    entered_b = asyncio.Event()
    entered_c = asyncio.Event()
    log: list[tuple[str, str]] = []

    async def worker(node: Node, budget: ExecutionConstraints) -> Proof:
        log.append(("start", node.id))
        if node.id == "b":
            entered_b.set()
            await asyncio.wait_for(entered_c.wait(), 5.0)
        elif node.id == "c":
            entered_c.set()
            await asyncio.wait_for(entered_b.wait(), 5.0)
            await asyncio.sleep(0.05)
        else:
            await asyncio.sleep(0)
        log.append(("end", node.id))
        return Proof(node.id)

    result = _run(schedule(_diamond(), worker))
    assert set(result.proofs) == {"a", "b", "c", "d"}
    assert result.failures == {}
    assert result.undispatched == frozenset()
    ends = [node for kind, node in log if kind == "end"]
    assert ends[0] == "a"
    assert ends[-1] == "d"
    pos = {(kind, node): i for i, (kind, node) in enumerate(log)}
    assert pos[("start", "d")] > pos[("end", "b")]
    assert pos[("start", "d")] > pos[("end", "c")]
    assert pos[("end", "b")] > pos[("start", "c")]
    assert pos[("end", "c")] > pos[("start", "b")]


def test_failing_parent_blocks_downstream_with_zero_spawns() -> None:
    calls: list[str] = []

    async def worker(node: Node, budget: ExecutionConstraints) -> Proof:
        calls.append(node.id)
        await asyncio.sleep(0)
        if node.id == "b":
            msg = "boom"
            raise RuntimeError(msg)
        return Proof(node.id)

    result = _run(schedule(_diamond(), worker))
    assert set(result.proofs) == {"a", "c"}
    assert list(result.failures) == ["b"]
    assert isinstance(result.failures["b"], RuntimeError)
    assert str(result.failures["b"]) == "boom"
    assert result.undispatched == frozenset({"d"})
    assert "d" not in calls


def test_linear_chain_runs_in_order() -> None:
    ends: list[str] = []

    async def worker(node: Node, budget: ExecutionConstraints) -> Proof:
        await asyncio.sleep(0)
        ends.append(node.id)
        return Proof(node.id, detail=f"did-{node.id}")

    dag = Dag.model_validate(
        {"nodes": [_node("a"), _node("b", deps=["a"]), _node("c", deps=["b"])]}
    )
    result = _run(schedule(dag, worker))
    assert ends == ["a", "b", "c"]
    assert result.proofs["b"].detail == "did-b"
    assert result.undispatched == frozenset()


def test_single_node_dag() -> None:
    async def worker(node: Node, budget: ExecutionConstraints) -> Proof:
        await asyncio.sleep(0)
        return Proof(node.id)

    result = _run(schedule(_single(), worker))
    assert result.proofs == {"a": Proof("a")}
    assert result.failures == {}
    assert result.undispatched == frozenset()


def test_max_workers_one_serializes_diamond() -> None:
    ends: list[str] = []

    async def worker(node: Node, budget: ExecutionConstraints) -> Proof:
        await asyncio.sleep(0)
        ends.append(node.id)
        return Proof(node.id)

    result = _run(schedule(_diamond(), worker, max_workers=1))
    assert set(result.proofs) == {"a", "b", "c", "d"}
    assert ends[0] == "a"
    assert ends[-1] == "d"
    assert set(ends[1:3]) == {"b", "c"}


def test_max_workers_bound_respected() -> None:
    state = {"current": 0, "peak": 0}

    async def worker(node: Node, budget: ExecutionConstraints) -> Proof:
        state["current"] += 1
        state["peak"] = max(state["peak"], state["current"])
        await asyncio.sleep(0.05)
        state["current"] -= 1
        return Proof(node.id)

    dag = Dag.model_validate({"nodes": [_node(f"n{i}") for i in range(6)]})
    result = _run(schedule(dag, worker, max_workers=2))
    assert len(result.proofs) == 6
    assert state["peak"] == 2


def test_default_pool_runs_five_wide() -> None:
    state = {"current": 0, "peak": 0}

    async def worker(node: Node, budget: ExecutionConstraints) -> Proof:
        state["current"] += 1
        state["peak"] = max(state["peak"], state["current"])
        await asyncio.sleep(0.05)
        state["current"] -= 1
        return Proof(node.id)

    dag = Dag.model_validate({"nodes": [_node(f"n{i}") for i in range(8)]})
    result = _run(schedule(dag, worker))
    assert len(result.proofs) == 8
    assert state["peak"] == 5


def test_max_workers_zero_rejected() -> None:
    async def worker(node: Node, budget: ExecutionConstraints) -> Proof:
        await asyncio.sleep(0)
        return Proof(node.id)

    with pytest.raises(ValueError, match="max_workers"):
        _run(schedule(_single(), worker, max_workers=0))


def test_per_node_budget_passed_to_worker() -> None:
    received: dict[str, ExecutionConstraints] = {}

    async def worker(node: Node, budget: ExecutionConstraints) -> Proof:
        received[node.id] = budget
        await asyncio.sleep(0)
        return Proof(node.id)

    result = _run(schedule(_diamond(), worker))
    assert set(result.proofs) == {"a", "b", "c", "d"}
    assert {k: v.reasoning_budget for k, v in received.items()} == {
        "a": "low",
        "b": "xhigh",
        "c": "low",
        "d": "medium",
    }


def test_rogue_proof_treated_as_node_failure() -> None:
    calls: list[str] = []

    async def worker(node: Node, budget: ExecutionConstraints) -> Proof:
        calls.append(node.id)
        await asyncio.sleep(0)
        if node.id == "b":
            return Proof("not-b")
        return Proof(node.id)

    result = _run(schedule(_diamond(), worker))
    assert set(result.proofs) == {"a", "c"}
    assert list(result.failures) == ["b"]
    assert isinstance(result.failures["b"], ValueError)
    assert "running node 'b'" in str(result.failures["b"])
    assert result.undispatched == frozenset({"d"})
    assert "d" not in calls


def test_cancellation_cancels_inflight_workers() -> None:
    never = asyncio.Event()
    cleaned = asyncio.Event()

    async def worker(node: Node, budget: ExecutionConstraints) -> Proof:
        try:
            await never.wait()
        finally:
            cleaned.set()
        return Proof(node.id)

    async def main() -> None:
        # Assert inside the loop: asyncio.run's shutdown pump would step
        # orphaned workers afterwards and mask missing cleanup.
        with pytest.raises(TimeoutError):
            await asyncio.wait_for(schedule(_single(), worker), 0.05)
        assert cleaned.is_set()

    asyncio.run(asyncio.wait_for(main(), 5.0))
