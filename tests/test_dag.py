"""Tests for saddle.dag: models, wire-schema derivation, static validation."""

from __future__ import annotations

from typing import Any

import pytest
from pydantic import ValidationError

from saddle.dag import Dag, DagIssue, dag_json_schema, validate_dag


def _node(
    node_id: str,
    *,
    deps: list[str] | None = None,
    tools: list[str] | None = None,
    tokens: int = 5000,
    reqs: list[str] | None = None,
    budget: str = "low",
) -> dict[str, Any]:
    return {
        "id": node_id,
        "dependencies": deps if deps is not None else [],
        "task_prompt": f"Do {node_id}.",
        "requirement_ids": reqs if reqs is not None else ["REQ-001"],
        "execution_constraints": {
            "reasoning_budget": budget,
            "allowed_tools": tools if tools is not None else ["read_file"],
            "max_context_tokens": tokens,
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


def _valid_dag() -> Dag:
    # b is declared first so the validator must resolve a forward reference.
    return Dag.model_validate({"nodes": [_node("b", deps=["a"], reqs=["REQ-002"]), _node("a")]})


def _check(
    dag: Dag,
    *,
    tools: set[str] | None = None,
    ceiling: int = 30000,
    required: list[str] | None = None,
) -> list[DagIssue]:
    return validate_dag(
        dag,
        allowed_tools=tools if tools is not None else {"read_file", "write_file"},
        context_ceiling=ceiling,
        required_ids=required if required is not None else ["REQ-001", "REQ-002"],
    )


def test_valid_dag_passes() -> None:
    assert _check(_valid_dag()) == []


def test_ceiling_boundary_is_inclusive() -> None:
    assert _check(_valid_dag(), ceiling=5000) == []


def test_duplicate_ids_rejected() -> None:
    dag = Dag.model_validate({"nodes": [_node("a"), _node("b", deps=["a"]), _node("a")]})
    assert _check(dag, required=["REQ-001"]) == [
        DagIssue(code="duplicate-node-id", node_id="a", message="duplicate node id: 'a'")
    ]


def test_unknown_dependency_rejected() -> None:
    dag = Dag.model_validate({"nodes": [_node("a"), _node("b", deps=["a", "ghost"])]})
    assert _check(dag, required=["REQ-001"]) == [
        DagIssue(
            code="unknown-dependency",
            node_id="b",
            message="node 'b' depends on unknown node 'ghost'",
        )
    ]


def test_self_cycle_rejected() -> None:
    dag = Dag.model_validate({"nodes": [_node("a", deps=["a"])]})
    assert _check(dag, required=["REQ-001"]) == [
        DagIssue(code="dependency-cycle", node_id=None, message="dependency cycle: a -> a")
    ]


def test_two_cycle_reports_exact_path() -> None:
    dag = Dag.model_validate({"nodes": [_node("a", deps=["b"]), _node("b", deps=["a"])]})
    assert _check(dag, required=["REQ-001"]) == [
        DagIssue(code="dependency-cycle", node_id=None, message="dependency cycle: a -> b -> a")
    ]


def test_longer_cycle_in_second_component() -> None:
    dag = Dag.model_validate(
        {
            "nodes": [
                _node("ok"),
                _node("x", deps=["y"]),
                _node("y", deps=["z"]),
                _node("z", deps=["x"]),
            ]
        }
    )
    assert _check(dag, required=["REQ-001"]) == [
        DagIssue(
            code="dependency-cycle",
            node_id=None,
            message="dependency cycle: x -> y -> z -> x",
        )
    ]


def test_tail_into_cycle_reports_cycle_only() -> None:
    dag = Dag.model_validate(
        {"nodes": [_node("a", deps=["b"]), _node("b", deps=["c"]), _node("c", deps=["b"])]}
    )
    assert _check(dag, required=["REQ-001"]) == [
        DagIssue(code="dependency-cycle", node_id=None, message="dependency cycle: b -> c -> b")
    ]


def test_cycle_walk_skips_peeled_deps() -> None:
    dag = Dag.model_validate(
        {"nodes": [_node("p"), _node("x", deps=["p", "y"]), _node("y", deps=["x"])]}
    )
    assert _check(dag, required=["REQ-001"]) == [
        DagIssue(code="dependency-cycle", node_id=None, message="dependency cycle: x -> y -> x")
    ]


def test_diamond_has_no_cycle() -> None:
    dag = Dag.model_validate(
        {
            "nodes": [
                _node("a"),
                _node("b", deps=["a"]),
                _node("c", deps=["a"]),
                _node("d", deps=["b", "c"]),
            ]
        }
    )
    assert _check(dag, required=["REQ-001"]) == []


def test_disallowed_tool_rejected() -> None:
    dag = Dag.model_validate({"nodes": [_node("a", tools=["read_file", "rm"])]})
    assert _check(dag, tools={"read_file"}, required=["REQ-001"]) == [
        DagIssue(
            code="tool-not-allowed",
            node_id="a",
            message="node 'a' uses disallowed tool 'rm'",
        )
    ]


def test_ceiling_violation_rejected() -> None:
    dag = Dag.model_validate({"nodes": [_node("a", tokens=30000)]})
    assert _check(dag, ceiling=28000, required=["REQ-001"]) == [
        DagIssue(
            code="context-ceiling-exceeded",
            node_id="a",
            message="node 'a' requests 30000 context tokens, ceiling is 28000",
        )
    ]


def test_uncovered_requirement_rejected() -> None:
    assert _check(_valid_dag(), required=["REQ-001", "REQ-002", "REQ-009"]) == [
        DagIssue(
            code="requirement-uncovered",
            node_id=None,
            message="requirement 'REQ-009' is not covered by any node",
        )
    ]


def test_multiple_issues_come_back_in_check_order() -> None:
    dag = Dag.model_validate(
        {"nodes": [_node("a", deps=["ghost"]), _node("b", tools=["rm"]), _node("a")]}
    )
    assert _check(dag, tools={"read_file"}, required=["REQ-001"]) == [
        DagIssue(code="duplicate-node-id", node_id="a", message="duplicate node id: 'a'"),
        DagIssue(
            code="unknown-dependency",
            node_id="a",
            message="node 'a' depends on unknown node 'ghost'",
        ),
        DagIssue(
            code="tool-not-allowed", node_id="b", message="node 'b' uses disallowed tool 'rm'"
        ),
    ]


def test_missing_gate_rejected_with_location() -> None:
    node = _node("a")
    del node["deterministic_gate"]
    with pytest.raises(ValidationError) as exc_info:
        Dag.model_validate({"nodes": [node]})
    locs = [error["loc"] for error in exc_info.value.errors()]
    assert ("nodes", 0, "deterministic_gate") in locs


def test_missing_requirement_ids_rejected_with_location() -> None:
    node = _node("a")
    del node["requirement_ids"]
    with pytest.raises(ValidationError) as exc_info:
        Dag.model_validate({"nodes": [node]})
    locs = [error["loc"] for error in exc_info.value.errors()]
    assert ("nodes", 0, "requirement_ids") in locs


def test_bad_budget_rejected_as_literal_error() -> None:
    # "high" is the meaningful near-miss: real vLLM literal, but neither a
    # node budget nor a wire effort on this model.
    for budget in ("high", "turbo"):
        with pytest.raises(ValidationError) as exc_info:
            Dag.model_validate({"nodes": [_node("a", budget=budget)]})
        errors = exc_info.value.errors()
        assert ("nodes", 0, "execution_constraints", "reasoning_budget") in [
            error["loc"] for error in errors
        ]
        assert "literal_error" in [error["type"] for error in errors]


def test_extra_key_rejected() -> None:
    node = _node("a")
    node["zzz"] = 1
    with pytest.raises(ValidationError) as exc_info:
        Dag.model_validate({"nodes": [node]})
    errors = exc_info.value.errors()
    assert ("nodes", 0, "zzz") in [error["loc"] for error in errors]
    assert "extra_forbidden" in [error["type"] for error in errors]


def test_empty_nodes_rejected() -> None:
    with pytest.raises(ValidationError) as exc_info:
        Dag.model_validate({"nodes": []})
    errors = exc_info.value.errors()
    assert ("nodes",) in [error["loc"] for error in errors]
    assert "too_short" in [error["type"] for error in errors]


def test_number_fields_accept_ints() -> None:
    node = _node("a")
    node["deterministic_gate"]["changed_line_coverage_min"] = 100
    dag = Dag.model_validate({"nodes": [node]})
    assert dag.nodes[0].deterministic_gate.changed_line_coverage_min == 100.0


def test_all_budgets_parse() -> None:
    for budget in ("zero", "low", "medium", "xhigh"):
        dag = Dag.model_validate({"nodes": [_node("a", budget=budget)]})
        assert dag.nodes[0].execution_constraints.reasoning_budget == budget


def _find_keys(node: Any, wanted: set[str]) -> list[str]:
    found: list[str] = []
    if isinstance(node, dict):
        for key, value in node.items():
            if key in wanted:
                found.append(key)
            found.extend(_find_keys(value, wanted))
    elif isinstance(node, list):
        for item in node:
            found.extend(_find_keys(item, wanted))
    return found


def test_derived_schema_has_no_refs() -> None:
    assert _find_keys(dag_json_schema(), {"$ref", "$defs"}) == []


def test_derived_schema_has_arch_node_shape() -> None:
    schema = dag_json_schema()
    assert schema["required"] == ["nodes"]
    assert schema["additionalProperties"] is False
    node = schema["properties"]["nodes"]["items"]
    assert node["required"] == [
        "id",
        "dependencies",
        "task_prompt",
        "requirement_ids",
        "execution_constraints",
        "deterministic_gate",
    ]
    assert node["additionalProperties"] is False
    constraints = node["properties"]["execution_constraints"]
    assert constraints["required"] == ["reasoning_budget", "allowed_tools", "max_context_tokens"]
    assert constraints["properties"]["reasoning_budget"]["enum"] == [
        "zero",
        "low",
        "medium",
        "xhigh",
    ]
    gate = node["properties"]["deterministic_gate"]
    assert gate["properties"]["mutation_sample"]["properties"]["scope"]["const"] == "changed-lines"


def test_derived_schema_returns_independent_copy() -> None:
    first = dag_json_schema()
    first["properties"]["nodes"]["minItems"] = 999
    fresh = dag_json_schema()
    assert fresh["properties"]["nodes"]["minItems"] == 1


def test_derived_schema_matches_model_both_directions() -> None:
    jsonschema = pytest.importorskip("jsonschema")
    validator = jsonschema.Draft202012Validator(dag_json_schema())
    good = {"nodes": [_node("a"), _node("b", deps=["a"])]}
    Dag.model_validate(good)
    assert list(validator.iter_errors(good)) == []
    bad = {"nodes": [_node("a", budget="turbo")]}
    with pytest.raises(ValidationError):
        Dag.model_validate(bad)
    assert len(list(validator.iter_errors(bad))) > 0
