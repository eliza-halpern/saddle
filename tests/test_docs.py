"""Pin: ARCHITECTURE.md's example node is a valid saddle.dag.Node."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from pydantic import ValidationError

from saddle.dag import Node

ARCHITECTURE_MD = Path(__file__).parent.parent / "docs" / "ARCHITECTURE.md"


def _first_json_fence() -> dict[str, object]:
    text = ARCHITECTURE_MD.read_text()
    start = text.index("```json\n") + len("```json\n")
    end = text.index("```", start)
    obj = json.loads(text[start:end])
    assert isinstance(obj, dict)
    return obj


def test_example_node_validates() -> None:
    obj = _first_json_fence()
    node = Node.model_validate(obj)
    assert node.id == obj["id"]


def test_example_node_without_kind_is_rejected() -> None:
    obj = _first_json_fence()
    del obj["kind"]
    with pytest.raises(ValidationError):
        Node.model_validate(obj)
