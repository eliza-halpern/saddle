"""DAG models and zero-LLM static validation (ARCHITECTURE.md §3 Phase 1).

The Pydantic models are the single source of truth for the DAG shape: the
guided-emission wire schema is derived from them, and these checks run with
zero model involvement before anything executes. Numeric fields stay lax on
purpose — JSON Schema `number` accepts ints, so `float` fields must too.
"""

from __future__ import annotations

from collections.abc import Callable, Collection, Mapping
from dataclasses import dataclass
from typing import Annotated, Any, Final, Literal

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StringConstraints,
    field_validator,
    model_validator,
)

NonEmptyStr = Annotated[str, Field(min_length=1)]
# min_length=1 admits "   ", which states nothing. Requirement statements
# are the one field whose whole purpose is to be readable by a test
# author, so blankness has to be rejected rather than counted.
Statement = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1)]
NodeId = Annotated[str, Field(min_length=1, max_length=64)]
ReasoningBudget = Literal["zero", "low", "medium", "xhigh"]
# PEP 586 forbids float Literals for the type checker; pydantic
# compiles them to a JSON-schema enum, which is what the decoder needs (see
# test_gate_thresholds_constrain_the_token_mask_not_just_validation).
KillThreshold = Literal[85.0, 90.0, 95.0, 100.0]  # type: ignore[valid-type]
NodeKind = Literal["test", "impl", "refactor"]
# A regex `pattern` compiles into the decoding grammar -- verified for
# DIFF_HEADER_PATTERN, where xgrammar emits
# `Regex("^diff --git ", json_string=true)` -- so a malformed ID is
# unrepresentable rather than rejected after the packet exists.
RequirementId = Annotated[str, Field(pattern=r"^REQ-\d{3}$")]
# How far, in edits, a reject may sit from its nearest accept (T6-4).
# Lives here rather than in gates.py because dag sits below gates in the
# layering and the validator that enforces it is the model's own. Start
# at 3; T6-1's row B tunes it.
REQ_NEAR_MISS_K: Final = 3


def edit_distance(a: str, b: str) -> int:
    """Levenshtein distance: insertions, deletions and substitutions, each cost 1."""
    previous = list(range(len(b) + 1))
    for i, x in enumerate(a, start=1):
        current = [i]
        for j, y in enumerate(b, start=1):
            current.append(min(previous[j] + 1, current[j - 1] + 1, previous[j - 1] + (x != y)))
        previous = current
    return previous[-1]


class Requirement(BaseModel):
    """One acceptance criterion: an ID plus what it actually requires.

    A bare ID states nothing, so no gate can check whether a test tests
    it (F5). The worker received `Requirements: REQ-001, REQ-002`, invented
    what they meant, asserted its own invention and the gate greped for the
    substring -- 7/7 gates and 12/18 on hidden behaviour for T1. The
    statement is what makes the binding checkable by anything other than
    the party being graded.
    """

    model_config = ConfigDict(extra="forbid")

    id: RequirementId
    statement: Statement
    # Concrete inputs the statement admits and refuses (T6-4). A statement
    # alone left T1's REQ-002 with no reject at all, and a reject far from
    # every accept -- REQ-001's `"user"`, 12 edits from `user@example.com`
    # -- rejects nothing a lazy validator would not; a near-miss is what
    # tells the requirement from a looser one. The lists are floored at
    # one entry each in the wire schema (`minItems`); the distance rule is
    # checked here, after the packet exists, because no grammar can state
    # it (T3-2's lesson on patterns).
    accepts: list[str] = Field(min_length=1)
    rejects: list[str] = Field(min_length=1)

    @model_validator(mode="after")
    def _rejects_are_near_misses(self) -> Requirement:
        for reject in self.rejects:
            nearest = min(self.accepts, key=lambda accept: edit_distance(reject, accept))
            distance = edit_distance(reject, nearest)
            if distance > REQ_NEAR_MISS_K:
                msg = (
                    f"{self.id}: reject {reject!r} is {distance} edits from its nearest "
                    f"accept {nearest!r}; a near-miss is within {REQ_NEAR_MISS_K}"
                )
                raise ValueError(msg)
        return self

    @property
    def examples(self) -> list[tuple[str, str, str]]:
        """`(id, "accepts"|"rejects", text)` for every example, accepts first."""
        return [(self.id, "accepts", text) for text in self.accepts] + [
            (self.id, "rejects", text) for text in self.rejects
        ]


class MutationSample(BaseModel):
    model_config = ConfigDict(extra="forbid")

    scope: Literal["changed-lines"]
    # A ceiling, not a target: lowering it only discards mutants the
    # changed lines already admit, and with the two thresholds below now
    # floored it is the last lever a planner has on this gate. Pinned to
    # ARCHITECTURE.md's example; the wall-clock half of its "<=100
    # mutants or <=10 minutes, whichever binds first" is enforced
    # separately by evidence._MUTATION_TIMEOUT_S.
    max_mutants: Literal[100] = 100
    # ARCHITECTURE.md's worked example is 85.0 and its prose allows "lower
    # or waived for mechanical glue" -- which is the waiver the planner
    # actually took (T3: 50.0). An enum floors it at the spec's own bar
    # while still permitting a stricter node. Deliberately not `ge=85`: a
    # JSON Schema `minimum` cannot be expressed in a decoding grammar, so
    # it would be checked after the packet is produced rather than making
    # the weak value unrepresentable.
    kill_threshold: KillThreshold = 85.0


class DeterministicGate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    test_command: NonEmptyStr
    # "Every changed line must be executed" is the gate's own contract and
    # ARCHITECTURE.md's example; the planner emitted 0.0 for T7 and the
    # gate reported `PASS (98.8% >= 0.0%)`. Const, for the reason above.
    # PEP 586 forbids float Literals for the type checker; pydantic
    # compiles them to a JSON-schema enum, which is what the decoder needs (see
    # test_gate_thresholds_constrain_the_token_mask_not_just_validation).
    changed_line_coverage_min: Literal[100.0] = 100.0  # type: ignore[valid-type]
    # ARCHITECTURE.md gate 4 is the tautology killer, so the waiver is not
    # representable: guided decoding can only emit `true` for this field.
    red_phase_required: Literal[True] = True
    mutation_sample: MutationSample


class ExecutionConstraints(BaseModel):
    model_config = ConfigDict(extra="forbid")

    reasoning_budget: ReasoningBudget
    allowed_tools: list[NonEmptyStr] = Field(min_length=1)
    # Floor is a read budget, not a style knob: below ~8K a worker cannot
    # hold the files it must change (ARCHITECTURE.md §2 sizes it at ~30K).
    max_context_tokens: int = Field(ge=8000, le=30000)


class Node(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: NodeId
    # The test/implementation split: an `impl` node may not edit tests and
    # a `test` node may not ship the implementation, so a misreading of
    # the contract cannot be encoded twice by the same worker (F5, #44).
    # `refactor` is the behaviour-preserving case, which has to move code
    # and its tests together.
    kind: NodeKind
    dependencies: list[NonEmptyStr]
    task_prompt: NonEmptyStr
    requirements: list[Requirement] = Field(min_length=1)
    execution_constraints: ExecutionConstraints
    deterministic_gate: DeterministicGate
    # Opt-in localisation (T3-2, #64): repo-relative files this node may
    # touch. Empty means unrestricted, so an omitted field changes nothing
    # and a declared list can only narrow the node's own scope. Validated
    # here rather than by a JSON-schema `pattern`: the decoder compiles a
    # pattern as a full match (CLAUDE.md), and no lookahead-free regex
    # says "no `..` segment" -- a wrong pattern would make every path
    # unrepresentable, silently.
    target_files: list[NonEmptyStr] = Field(default_factory=list)

    @field_validator("target_files")
    @classmethod
    def _repo_relative_posix(cls, paths: list[str]) -> list[str]:
        for path in paths:
            if (
                "\\" in path
                or path != path.strip()
                or any(segment in ("", ".", "..") for segment in path.split("/"))
            ):
                msg = (
                    f"target_files entry {path!r} must be a repo-relative POSIX path "
                    "without empty, '.' or '..' segments"
                )
                raise ValueError(msg)
        return paths

    @property
    def requirement_ids(self) -> list[str]:
        """Declared IDs, for the journal, transcript and binding gate."""
        return [requirement.id for requirement in self.requirements]

    @property
    def requirement_examples(self) -> list[tuple[str, str, str]]:
        """Every accept and reject the node's requirements cite, for the binding gate."""
        return [example for requirement in self.requirements for example in requirement.examples]


class Dag(BaseModel):
    model_config = ConfigDict(extra="forbid")

    nodes: list[Node] = Field(min_length=1, max_length=32)


def planned_requirement_ids(dag: Dag) -> tuple[str, ...]:
    """Every requirement id some node of `dag` declares, sorted (T3-24).

    The binding gate's orphan half reads citations from every discovered
    test source, and a plan that splits its ids across nodes puts one
    node's id in the tests another node gates against: the `test` node
    that writes the specification cites the id of the `impl` node that
    will make it pass, and an `impl` node reads the file its dependency
    wrote. An id declared anywhere in the plan is a planned requirement,
    not a hallucinated one, so it is what the gate subtracts before
    calling a citation an orphan. Each node's *own* ids still have to be
    cited: that half stays per node.
    """
    return tuple(sorted({rid for node in dag.nodes for rid in node.requirement_ids}))


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


# Emission estimate for a node (T6-8): what its diff costs to write, from the
# repo, not from the planner. A unified diff re-emits the changed lines with
# context and headers; measured on round 3 (T5: ~250 diff lines, ~4000
# tokens), 16 tokens per baseline line of the declared files plus a fixed
# header/hunk overhead is a ceiling, not a mean. Both constants live here so
# nothing the planner emits can move them.
TOKENS_PER_LINE: Final = 16
DIFF_OVERHEAD_TOKENS: Final = 2048
SCOPED_KINDS: Final = ("impl", "refactor")


def emission_estimate(node: Node, file_lines: Mapping[str, int]) -> int | None:
    """Tokens a diff over `node.target_files` needs; None when no scope is declared.

    A declared file the repo lacks is one the node creates: it costs what
    the node writes, bounded by the budget alone, so it adds nothing here.
    """
    if not node.target_files:
        return None
    lines = sum(file_lines.get(path, 0) for path in node.target_files)
    return lines * TOKENS_PER_LINE + DIFF_OVERHEAD_TOKENS


def _undeclared_scope(dag: Dag) -> list[DagIssue]:
    return [
        DagIssue(
            code="undeclared-scope",
            node_id=node.id,
            message=f"node {node.id!r} is {node.kind} and declares no target_files;"
            " name the files it will touch so its size can be checked",
        )
        for node in dag.nodes
        if node.kind in SCOPED_KINDS and not node.target_files
    ]


def _oversized_nodes(
    dag: Dag, file_lines: Mapping[str, int], emission_budget: Callable[[Node], int]
) -> list[DagIssue]:
    issues: list[DagIssue] = []
    for node in dag.nodes:
        estimate = emission_estimate(node, file_lines)
        if estimate is None:
            continue
        budget = emission_budget(node)
        if estimate > budget:
            issues.append(
                DagIssue(
                    code="node-too-large",
                    node_id=node.id,
                    message=f"node {node.id!r} declares {len(node.target_files)} file(s)"
                    f" whose diff is estimated at {estimate} tokens, over its"
                    f" {budget}-token emission budget; split it by module or behaviour",
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
    file_lines: Mapping[str, int] | None = None,
    emission_budget: Callable[[Node], int] | None = None,
) -> list[DagIssue]:
    """Zero-LLM semantic checks over a parsed DAG. Empty list means valid.

    With `file_lines` (baseline line count per repo file) the scope checks
    run too (T6-8): an `impl`/`refactor` node must declare `target_files`,
    and with `emission_budget` a node whose estimated diff exceeds the
    budget its caller allows is `node-too-large`. Callers that validate a
    DAG with no repo behind it leave both unset.
    """
    issues: list[DagIssue] = []
    issues.extend(_duplicate_ids(dag))
    issues.extend(_unknown_dependencies(dag))
    issues.extend(_cycle_issues(dag))
    issues.extend(_disallowed_tools(dag, allowed_tools))
    issues.extend(_ceiling_violations(dag, context_ceiling))
    issues.extend(_uncovered_requirements(dag, required_ids))
    if file_lines is not None:
        issues.extend(_undeclared_scope(dag))
        if emission_budget is not None:
            issues.extend(_oversized_nodes(dag, file_lines, emission_budget))
    return issues
