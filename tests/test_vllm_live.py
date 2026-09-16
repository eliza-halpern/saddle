"""Server-gated live proof for guided emission (durable form of the #2 proof).

Skipped unless SADDLE_VLLM_API_KEY (or VLLM_API_KEY) is set. When run, this
exercises the real XGrammar snap on the self-operated server — not a mock —
with client defaults: one DAG emission must be schema-valid with freeform
reasoning preceding it.
"""

from __future__ import annotations

import os
from typing import Any

import pytest

from saddle.dag import dag_json_schema
from saddle.vllm import VllmClient

jsonschema = pytest.importorskip("jsonschema")

_LIVE_KEY = os.environ.get("SADDLE_VLLM_API_KEY") or os.environ.get("VLLM_API_KEY")

pytestmark = pytest.mark.skipif(not _LIVE_KEY, reason="live proof needs SADDLE_VLLM_API_KEY")

LIVE_PROMPT = """Decompose the mechanical coding task below into a DAG of 2 to 4 nodes.

Task: Add email-format validation to the login form and reject blanks.

Rules:
- The first node has no dependencies; every other node depends on at least one earlier node.
- requirement_ids look like REQ-001, REQ-002, ... (at least one per node).
- reasoning_budget is one of: zero, low, medium, xhigh.
- allowed_tools uses only: read_file, write_file, run_tests, lint.
- max_context_tokens is between 1000 and 30000.
- test_command is a pytest invocation, e.g. "pytest tests/test_login.py".
- changed_line_coverage_min and kill_threshold are 0-100 numbers.
- mutation_sample.scope is always "changed-lines"; max_mutants is 1-1000.
- Think through the decomposition first; then emit the plan.
"""


def test_live_guided_emission_is_schema_valid_with_reasoning() -> None:
    assert _LIVE_KEY is not None
    validator: Any = jsonschema.Draft202012Validator(dag_json_schema())
    with VllmClient(api_key=_LIVE_KEY) as client:
        emission = client.emit_dag(LIVE_PROMPT, max_tokens=16384)
    assert len(emission.reasoning.strip()) >= 20
    errors = [
        f"{'/'.join(map(str, e.path))}: {e.message}" for e in validator.iter_errors(emission.dag)
    ]
    assert errors == []
