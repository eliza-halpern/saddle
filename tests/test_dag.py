"""Tests for saddle.dag: models, wire-schema derivation, static validation."""

from __future__ import annotations

import json
from typing import Any

import pytest
from pydantic import ValidationError

from saddle.dag import (
    Dag,
    DagIssue,
    Node,
    dag_json_schema,
    planned_requirement_ids,
    validate_dag,
)


def _node(
    node_id: str,
    *,
    deps: list[str] | None = None,
    tools: list[str] | None = None,
    tokens: int = 8000,
    reqs: list[str] | None = None,
    budget: str = "low",
) -> dict[str, Any]:
    return {
        "id": node_id,
        "kind": "refactor",
        "dependencies": deps if deps is not None else [],
        "task_prompt": f"Do {node_id}.",
        "requirements": [
            {"id": r, "statement": f"{r} holds."}
            for r in (reqs if reqs is not None else ["REQ-001"])
        ],
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
                "max_mutants": 100,
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
    assert _check(_valid_dag(), ceiling=8000) == []


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
    del node["requirements"]
    with pytest.raises(ValidationError) as exc_info:
        Dag.model_validate({"nodes": [node]})
    locs = [error["loc"] for error in exc_info.value.errors()]
    assert ("nodes", 0, "requirements") in locs


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
        "kind",
        "dependencies",
        "task_prompt",
        "requirements",
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


def test_gate_thresholds_below_the_spec_floor_are_unrepresentable() -> None:
    """The planner cannot set its own bar below ARCHITECTURE.md's.

    F6/F12: the planner set T3's kill threshold to 50.0 and T7's coverage
    bar to 0.0, and T7's node then reported `PASS (98.8% >= 0.0%)` while
    an infinite loop shipped. A gate whose strictness the graded party
    chooses is not a gate.
    """
    for value in (0.0, 50.0, 80.0):
        node = _node("n1")
        node["deterministic_gate"]["mutation_sample"]["kill_threshold"] = value
        with pytest.raises(ValidationError):
            Dag.model_validate({"nodes": [node]})

    for value in (0.0, 80.0, 99.9):
        node = _node("n1")
        node["deterministic_gate"]["changed_line_coverage_min"] = value
        with pytest.raises(ValidationError):
            Dag.model_validate({"nodes": [node]})


@pytest.mark.parametrize("value", [85.0, 90.0, 95.0, 100.0])
def test_gate_thresholds_at_the_floor_and_above_are_representable(value: float) -> None:
    """Every legal `KillThreshold` member is accepted, not just the floor."""
    node = _node("n1")
    node["deterministic_gate"]["mutation_sample"]["kill_threshold"] = value
    Dag.model_validate({"nodes": [node]})


def test_gate_thresholds_constrain_the_token_mask_not_just_validation() -> None:
    """Floors must be enums, not numeric bounds.

    A `minimum` in JSON Schema is not expressible in a decoding grammar,
    so it is checked *after* the packet is produced -- the class of
    constraint the v2 sweep showed leaks every time. `const`/`enum` is
    what `red_phase_required` uses, and that one has never leaked.
    """
    # dag_json_schema() is what reaches XGrammar -- inlined, no $defs.
    # Asserting on Dag.model_json_schema() would check a schema the
    # decoder never sees.
    schema = json.dumps(dag_json_schema())
    for probe in ("changed_line_coverage_min", "kill_threshold"):
        field = json.loads("{" + schema.split(f'"{probe}": {{', 1)[1].split("}", 1)[0] + "}")
        assert "minimum" not in field
        assert "const" in field or "enum" in field


def test_shrinking_the_mutant_cap_is_unrepresentable() -> None:
    """max_mutants is the third lever on the same gate, and the cheapest.

    With coverage pinned at 100.0 and kill_threshold floored at 85.0, a
    planner minimising gate strength has one move left: sample one
    mutant. Kill it and the node reports 100% >= 85% having tested
    almost nothing -- F1's T1 result (2 mutants, 100% kill, a validator
    that accepts `user@example..com`) made worse by design.

    The cap is a ceiling, not a target: lowering it only discards mutants
    the changed lines already admit. ARCHITECTURE.md's own example pins
    100, and the wall-clock bound is enforced separately by
    _MUTATION_TIMEOUT_S, matching its "<=100 mutants or <=10 minutes,
    whichever binds first".
    """
    for value in (1, 5, 10, 99):
        node = _node("n1")
        node["deterministic_gate"]["mutation_sample"]["max_mutants"] = value
        with pytest.raises(ValidationError):
            Dag.model_validate({"nodes": [node]})

    schema = json.dumps(dag_json_schema())
    field = json.loads("{" + schema.split('"max_mutants": {', 1)[1].split("}", 1)[0] + "}")
    assert "minimum" not in field
    assert "const" in field or "enum" in field


def test_requirements_carry_testable_statements() -> None:
    """A bare ID states nothing, so no gate can check it.

    F5: the worker receives `Requirements: REQ-001, REQ-002` -- two
    opaque strings -- invents what they mean, writes a test asserting its
    own invention, and the gate greps for the substring. On T1 that gave
    7/7 gates and 12/18 on hidden behaviour.
    """
    node = _node("n1")
    node["requirements"] = [{"id": "REQ-001", "statement": "Rejects a local part ending in a dot."}]
    dag = Dag.model_validate({"nodes": [node]})
    assert dag.nodes[0].requirements[0].statement.startswith("Rejects")
    # Downstream consumers (journal, transcript, gates) keep reading IDs.
    assert dag.nodes[0].requirement_ids == ["REQ-001"]


def test_requirement_without_a_statement_is_unrepresentable() -> None:
    node = _node("n1")
    node["requirements"] = [{"id": "REQ-001"}]
    with pytest.raises(ValidationError):
        Dag.model_validate({"nodes": [node]})
    node["requirements"] = [{"id": "REQ-001", "statement": "   "}]
    with pytest.raises(ValidationError):
        Dag.model_validate({"nodes": [node]})


def test_requirement_id_shape_is_grammar_constrained() -> None:
    """A regex `pattern` compiles into the decoding grammar (verified for
    DIFF_HEADER_PATTERN via xgrammar), so a malformed ID is unrepresentable
    rather than rejected after the packet exists."""
    node = _node("n1")
    node["requirements"] = [{"id": "REQUIREMENT ONE", "statement": "Does a thing."}]
    with pytest.raises(ValidationError):
        Dag.model_validate({"nodes": [node]})

    schema = json.dumps(dag_json_schema())
    assert "REQ-" in schema


def test_node_kind_is_constrained_to_the_three_kinds() -> None:
    """An unconstrained kind silently falls through the scope gate.

    `check_node_scope` exempts "refactor" and otherwise treats the node
    as a test node, so an invented kind would be graded under rules it
    never declared. An enum is grammar-expressible, so the invalid value
    is unrepresentable rather than mis-handled.
    """
    node = _node("n1")
    node["kind"] = "implementation"
    with pytest.raises(ValidationError):
        Dag.model_validate({"nodes": [node]})

    schema = json.dumps(dag_json_schema())
    assert '"refactor"' in schema


def test_target_files_default_empty_and_repo_relative_accepted() -> None:
    """Known-good (T3-2, widened T3-13): the field is optional and plain
    repo-relative POSIX paths, including nested ones and a dotfile-led
    segment that is not a bare '.' segment, are representable."""
    plain = Node.model_validate(_node("n1"))
    assert plain.target_files == []
    node = _node("n1")
    node["target_files"] = [
        "n.py",
        "src/app/login.py",
        "tests/test_login.py",
        ".github/x.yml",
    ]
    assert Node.model_validate(node).target_files == [
        "n.py",
        "src/app/login.py",
        "tests/test_login.py",
        ".github/x.yml",
    ]


@pytest.mark.parametrize(
    "bad",
    [
        "/etc/passwd",
        "../n.py",
        "src/../n.py",
        "src\\n.py",
        " n.py",
        "",
        "./n.py",
        "a//b.py",
        "src/./x.py",
        "dir/",
    ],
)
def test_target_files_rejects_escapes_and_absolute_paths(bad: str) -> None:
    """Known-bad (T3-2, widened T3-13): anything that could name a file
    outside the repo, is not a clean POSIX path, or contains an empty or
    '.' segment that the gate's exact-string match could never see again
    as the node's own file, is refused at validation."""
    node = _node("n1")
    node["target_files"] = [bad]
    with pytest.raises(ValidationError):
        Node.model_validate(node)


# --- T3-24: the ids a plan declares, for the binding gate's orphan half ----


def test_planned_requirement_ids_is_the_sorted_union_over_every_node() -> None:
    """Every node's ids, whatever its position: a `test` node cites the id
    of the `impl` node downstream of it, and a sibling's id is planned too.
    """
    dag = Dag.model_validate(
        {
            "nodes": [
                _node("c", deps=["b"], reqs=["REQ-003", "REQ-001"]),
                _node("b", deps=["a"], reqs=["REQ-002"]),
                _node("a"),
                _node("d", reqs=["REQ-004"]),
            ]
        }
    )
    assert planned_requirement_ids(dag) == ("REQ-001", "REQ-002", "REQ-003", "REQ-004")


def test_planned_requirement_ids_of_a_single_node_plan_is_its_own_ids() -> None:
    dag = Dag.model_validate({"nodes": [_node("a", reqs=["REQ-002", "REQ-001"])]})
    assert planned_requirement_ids(dag) == ("REQ-001", "REQ-002")
