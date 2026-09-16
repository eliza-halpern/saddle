"""DAG models and zero-LLM static validation (ARCHITECTURE.md §3 Phase 1).

The Pydantic models are the single source of truth for the DAG shape: the
guided-emission wire schema is derived from them, and these checks run with
zero model involvement before anything executes. Numeric fields stay lax on
purpose — JSON Schema `number` accepts ints, so `float` fields must too.
"""

from __future__ import annotations

from collections.abc import Collection
from dataclasses import dataclass
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field

NonEmptyStr = Annotated[str, Field(min_length=1)]
NodeId = Annotated[str, Field(min_length=1, max_length=64)]
ReasoningBudget = Literal["zero", "low", "medium", "high", "xhigh"]


class MutationSample(BaseModel):
    model_config = ConfigDict(extra="forbid")

    scope: Literal["changed-lines"]
    max_mutants: int = Field(ge=1, le=1000)
    kill_threshold: float = Field(ge=0, le=100)


class DeterministicGate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    test_command: NonEmptyStr
    changed_line_coverage_min: float = Field(ge=0, le=100)
    red_phase_required: bool
    mutation_sample: MutationSample


class ExecutionConstraints(BaseModel):
    model_config = ConfigDict(extra="forbid")

    reasoning_budget: ReasoningBudget
    allowed_tools: list[NonEmptyStr] = Field(min_length=1)
    max_context_tokens: int = Field(ge=1000, le=30000)


class Node(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: NodeId
    dependencies: list[NonEmptyStr]
    task_prompt: NonEmptyStr
    requirement_ids: list[NonEmptyStr] = Field(min_length=1)
    execution_constraints: ExecutionConstraints
    deterministic_gate: DeterministicGate


class Dag(BaseModel):
    model_config = ConfigDict(extra="forbid")

    nodes: list[Node] = Field(min_length=1, max_length=32)


@dataclass(frozen=True)
class DagIssue:
    """One machine-readable validation finding (`[]` from validate_dag is valid)."""

    code: str
    node_id: str | None
    message: str


def _duplicate_ids(dag: Dag) -> list[DagIssue]:
    seen: set[str] = set()
    issues: list[DagIssue] = []
    for node in dag.nodes:
        if node.id in seen:
            issues.append(
                DagIssue(
                    code="duplicate-node-id",
                    node_id=node.id,
                    message=f"duplicate node id: {node.id!r}",
                )
            )
        else:
            seen.add(node.id)
    return issues


def _unknown_dependencies(dag: Dag) -> list[DagIssue]:
    known = {node.id for node in dag.nodes}
    issues: list[DagIssue] = []
    for node in dag.nodes:
        for dep in node.dependencies:
            if dep not in known:
                issues.append(
                    DagIssue(
                        code="unknown-dependency",
                        node_id=node.id,
                        message=f"node {node.id!r} depends on unknown node {dep!r}",
                    )
                )
    return issues


def _find_cycle(adj: dict[str, list[str]], order: list[str]) -> list[str] | None:
    # Kahn's algorithm: peel nodes with no unresolved deps; whatever remains
    # sits on or behind a cycle. Every leftover node keeps a leftover dep,
    # so the spelling walk below always terminates with a genuine cycle.
    pending = {node: len(deps) for node, deps in adj.items()}
    dependents: dict[str, list[str]] = {node: [] for node in adj}
    for node, deps in adj.items():
        for dep in deps:
            dependents[dep].append(node)
    queue = [node for node in order if pending[node] == 0]
    while queue:
        node = queue.pop()
        for dependent in dependents[node]:
            pending[dependent] -= 1
            if pending[dependent] == 0:
                queue.append(dependent)
    leftover = [node for node in order if pending[node] > 0]
    if not leftover:
        return None
    start = leftover[0]
    path = [start]
    node = start
    while True:
        nxt = next(dep for dep in adj[node] if dep in leftover)
        if nxt in path:
            return [*path[path.index(nxt) :], nxt]
        path.append(nxt)
        node = nxt


def _cycle_issues(dag: Dag) -> list[DagIssue]:
    known = {node.id for node in dag.nodes}
    adj = {node.id: [dep for dep in node.dependencies if dep in known] for node in dag.nodes}
    order = [node.id for node in dag.nodes]
    cycle = _find_cycle(adj, order)
    if cycle is None:
        return []
    return [
        DagIssue(
            code="dependency-cycle",
            node_id=None,
            message=f"dependency cycle: {' -> '.join(cycle)}",
        )
    ]


def _disallowed_tools(dag: Dag, allowed_tools: Collection[str]) -> list[DagIssue]:
    issues: list[DagIssue] = []
    for node in dag.nodes:
        for tool in node.execution_constraints.allowed_tools:
            if tool not in allowed_tools:
                issues.append(
                    DagIssue(
                        code="tool-not-allowed",
                        node_id=node.id,
                        message=f"node {node.id!r} uses disallowed tool {tool!r}",
                    )
                )
    return issues


def _ceiling_violations(dag: Dag, context_ceiling: int) -> list[DagIssue]:
    issues: list[DagIssue] = []
    for node in dag.nodes:
        tokens = node.execution_constraints.max_context_tokens
        if tokens > context_ceiling:
            issues.append(
                DagIssue(
                    code="context-ceiling-exceeded",
                    node_id=node.id,
                    message=f"node {node.id!r} requests {tokens} context tokens,"
                    f" ceiling is {context_ceiling}",
                )
            )
    return issues


def _uncovered_requirements(dag: Dag, required_ids: Collection[str]) -> list[DagIssue]:
    covered = {req for node in dag.nodes for req in node.requirement_ids}
    return [
        DagIssue(
            code="requirement-uncovered",
            node_id=None,
            message=f"requirement {req!r} is not covered by any node",
        )
        for req in required_ids
        if req not in covered
    ]


def _inline_refs(schema: dict[str, Any]) -> dict[str, Any]:
    """Inline `#/$defs/…` references.

    Assumes Pydantic's shape: pure single-key `{"$ref": …}` nodes plus a
    top-level `$defs` table. `$ref` nodes are replaced wholesale.
    """
    defs = schema["$defs"]

    def resolve(node: Any) -> Any:
        if isinstance(node, dict):
            if "$ref" in node:
                name = node["$ref"].removeprefix("#/$defs/")
                return resolve(defs[name])
            return {key: resolve(value) for key, value in node.items()}
        if isinstance(node, list):
            return [resolve(item) for item in node]
        return node

    inlined: dict[str, Any] = resolve(
        {key: value for key, value in schema.items() if key != "$defs"}
    )
    return inlined


def dag_json_schema() -> dict[str, Any]:
    """Return the guided-emission wire schema derived from the Dag model."""
    return _inline_refs(Dag.model_json_schema())


def validate_dag(
    dag: Dag,
    *,
    allowed_tools: Collection[str],
    context_ceiling: int,
    required_ids: Collection[str],
) -> list[DagIssue]:
    """Zero-LLM semantic checks over a parsed DAG. Empty list means valid."""
    issues: list[DagIssue] = []
    issues.extend(_duplicate_ids(dag))
    issues.extend(_unknown_dependencies(dag))
    issues.extend(_cycle_issues(dag))
    issues.extend(_disallowed_tools(dag, allowed_tools))
    issues.extend(_ceiling_violations(dag, context_ceiling))
    issues.extend(_uncovered_requirements(dag, required_ids))
    return issues
