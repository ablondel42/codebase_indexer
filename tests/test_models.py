"""Tests for canonical data models and serialization helpers (v0.2.0)."""

import dataclasses
import json
from pathlib import Path

from pydantic import ValidationError as PydanticValidationError
import pytest

from repo_context.constants import (
    EDGE_KIND_CALLS,
    EDGE_KIND_SCOPE_PARENT,
    SYMBOL_KIND_ATTRIBUTE,
    SYMBOL_KIND_CONSTANT,
    SYMBOL_KIND_TYPE_ALIAS,
    VALID_EDGE_KINDS,
    VALID_SYMBOL_KINDS,
)
from repo_context.models import (
    Edge,
    FileRecord,
    PlanAssessment,
    Position,
    Range,
    RepoRecord,
    SymbolContext,
    SymbolNode,
    from_json,
    normalize_json_field,
    to_json,
    validate_iso_timestamp,
)


def test_position_and_range_frozen() -> None:
    """Position and Range are immutable frozen dataclasses."""
    pos = Position(line=10, character=5)
    assert pos.line == 10
    assert pos.character == 5
    with pytest.raises(dataclasses.FrozenInstanceError):
        pos.line = 20  # type: ignore[misc]

    rng = Range(start=pos, end=Position(line=10, character=15))
    assert rng.start == pos
    assert rng.end.character == 15
    with pytest.raises(dataclasses.FrozenInstanceError):
        rng.start = Position(line=0, character=0)  # type: ignore[misc]


def test_repo_and_file_record_v2_fields() -> None:
    """RepoRecord and FileRecord support v0.2.0 fields and ISO timestamp validation."""
    repo = RepoRecord(
        id="repo:test:1234abcd",
        root_path="/path/to/repo",
        name="test-repo",
        default_language="python",
        created_at="2026-03-23T00:00:00Z",
    )
    assert repo.id == "repo:test:1234abcd"
    assert repo.last_indexed_at is None

    file_rec = FileRecord(
        id="file:repo:test:1234abcd:app/main.py",
        repo_id=repo.id,
        file_path="app/main.py",
        uri="file:///path/to/repo/app/main.py",
        module_path="app.main",
        language="python",
        content_hash="sha256:abc123",
        size_bytes=1024,
        line_count=42,
        is_test=True,
        last_modified_at="2026-03-23T00:00:00Z",
    )
    assert file_rec.line_count == 42
    assert file_rec.is_test is True

    with pytest.raises(PydanticValidationError):
        FileRecord(
            id="file:repo:test:1234abcd:app/main.py",
            repo_id=repo.id,
            file_path="app/main.py",
            uri="file:///path/to/repo/app/main.py",
            module_path="app.main",
            content_hash="sha256:abc123",
            size_bytes=10,
            line_count=-1,
            last_modified_at="2026-03-23T00:00:00Z",
        )


@pytest.mark.parametrize(
    "kind",
    [
        SYMBOL_KIND_CONSTANT,
        SYMBOL_KIND_ATTRIBUTE,
        SYMBOL_KIND_TYPE_ALIAS,
        "module",
        "class",
        "function",
        "async_function",
        "method",
        "async_method",
        "local_function",
        "local_async_function",
    ],
)
def test_symbol_node_supports_all_v2_kinds_and_fields(kind: str) -> None:
    """SymbolNode validates all v2 symbol kinds, long names (<=1024), and line/signature fields."""
    assert kind in VALID_SYMBOL_KINDS
    long_name = "S" * 512
    node = SymbolNode(
        id=f"sym:repo:test:{kind}:app.{long_name}",
        repo_id="repo:test:1234abcd",
        file_id="file:repo:test:1234abcd:app/service.py",
        kind=kind,  # type: ignore[arg-type]
        name=long_name,
        qualified_name=f"app.{long_name}",
        uri="file:///path/to/repo/app/service.py",
        range_json={"start": {"line": 0, "character": 0}, "end": {"line": 2, "character": 0}},
        selection_range_json={"start": {"line": 0, "character": 0}, "end": {"line": 0, "character": 5}},
        start_line=1,
        end_line=3,
        signature="MAX_RETRIES: int = 3",
        payload_json={"value_hint": "3"},
    )
    assert node.kind == kind
    assert node.start_line == 1
    assert node.end_line == 3
    assert node.signature == "MAX_RETRIES: int = 3"
    assert json.loads(node.payload_json) == {"value_hint": "3"}


def test_symbol_node_rejects_malformed_json_and_oversized_name() -> None:
    """SymbolNode validates JSON string fields and name max_length=1024."""
    with pytest.raises(PydanticValidationError):
        SymbolNode(
            id="sym:repo:test:constant:app.X",
            repo_id="repo:test:1234abcd",
            file_id="file:repo:test:1234abcd:app/service.py",
            kind="constant",
            name="X",
            qualified_name="app.X",
            uri="file:///path/to/repo/app/service.py",
            payload_json="{bad json",
        )

    with pytest.raises(PydanticValidationError):
        SymbolNode(
            id="sym:repo:test:constant:app.X",
            repo_id="repo:test:1234abcd",
            file_id="file:repo:test:1234abcd:app/service.py",
            kind="constant",
            name="X" * 1025,
            qualified_name="app.X",
            uri="file:///path/to/repo/app/service.py",
        )


@pytest.mark.parametrize(
    "kind",
    ["contains", "imports", "inherits", EDGE_KIND_SCOPE_PARENT, EDGE_KIND_CALLS, "references"],
)
def test_edge_supports_normalized_v2_kinds_and_json_validation(kind: str) -> None:
    """Edge accepts normalized v2 kinds (scope_parent, calls, etc.) and rejects bad JSON."""
    assert kind in VALID_EDGE_KINDS
    edge = Edge(
        id=f"edge:repo:test:{kind}:a->b",
        repo_id="repo:test:1234abcd",
        kind=kind,  # type: ignore[arg-type]
        from_id="sym:a",
        to_id="sym:b",
        payload_json={"target": "b"},
        evidence_range_json={"start": {"line": 1, "character": 0}, "end": {"line": 1, "character": 3}},
    )
    assert edge.kind == kind
    assert json.loads(edge.payload_json) == {"target": "b"}

    with pytest.raises(PydanticValidationError):
        Edge(
            id="edge:bad",
            repo_id="repo:test:1234abcd",
            kind="calls",
            from_id="sym:a",
            to_id="sym:b",
            evidence_range_json="[unclosed",
        )

    with pytest.raises(PydanticValidationError):
        Edge(
            id="edge:legacy_kind",
            repo_id="repo:test:1234abcd",
            kind="SCOPE_PARENT",  # type: ignore[arg-type]
            from_id="sym:a",
            to_id="sym:b",
        )


def test_symbol_context_and_plan_assessment_no_mutable_defaults() -> None:
    """SymbolContext and PlanAssessment keep independent default lists/dicts."""
    ctx1 = SymbolContext(focus_symbol={"id": "sym1"})
    ctx2 = SymbolContext(focus_symbol={"id": "sym2"})
    ctx1.structural_children.append({"id": "child1"})
    assert ctx2.structural_children == []

    a1 = PlanAssessment(plan_summary="Plan 1")
    a2 = PlanAssessment(plan_summary="Plan 2")
    a1.issues.append("issue1")
    assert a2.issues == []


def test_to_json_from_json_and_helpers() -> None:
    """to_json handles dicts, dataclasses, Paths, and Pydantic BaseModels."""
    assert to_json({"z": 1, "a": 2}) == '{"a": 2, "z": 1}'
    assert to_json(Position(line=10, character=5)) == '{"character": 5, "line": 10}'

    repo = RepoRecord(
        id="repo:test:1234abcd",
        root_path="/tmp/repo",
        name="repo",
        created_at="2026-01-01T00:00:00Z",
    )
    dumped = from_json(to_json({"repo": repo, "path": Path("/tmp/repo")}))
    assert dumped["repo"]["id"] == "repo:test:1234abcd"
    assert dumped["path"] == "/tmp/repo"
    assert from_json(to_json(repo))["name"] == "repo"

    with pytest.raises(TypeError):
        to_json(RepoRecord)

    assert validate_iso_timestamp(None) is None
    assert validate_iso_timestamp("2026-01-01T00:00:00Z") == "2026-01-01T00:00:00Z"
    with pytest.raises(ValueError):
        validate_iso_timestamp("not-a-timestamp")

    assert normalize_json_field(None) is None
    assert normalize_json_field('{"a": 1}') == '{"a": 1}'
    assert normalize_json_field({"a": 1}) == '{"a": 1}'
    with pytest.raises(ValueError):
        normalize_json_field("{invalid")
