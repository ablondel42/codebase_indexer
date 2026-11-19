"""Tests for canonical Pydantic v2 data models and serialization helpers."""

from pathlib import Path

import pytest
from pydantic import ValidationError as PydanticValidationError

from context.models import CompactSymbol, FocusSymbol, SymbolContext
from core.models import (
    AppConfig,
    Edge,
    EdgeKind,
    FileRecord,
    Language,
    LineSpan,
    Position,
    Range,
    RepoRecord,
    SymbolKind,
    SymbolNode,
    SymbolScope,
    SymbolSummary,
    ValidationError,
    build_config,
)
from core.utils.json_codec import from_json, to_json
from core.utils.timestamps import latest_timestamp, validate_iso_timestamp
from graph.models import RiskAssessment, RiskDecision, RiskFacts, RiskTarget


def test_position_range_and_linespan_frozen_and_strict() -> None:
    """Position, Range, and LineSpan are immutable FrozenModels rejecting extra fields."""
    pos = Position(line=10, character=5)
    assert pos.line == 10
    assert pos.character == 5
    with pytest.raises(PydanticValidationError):
        pos.line = 20

    rng = Range(start=pos, end=Position(line=10, character=15))
    assert rng.start == pos
    assert rng.end.character == 15
    with pytest.raises(PydanticValidationError):
        rng.start = Position(line=0, character=0)

    span = LineSpan(start_line=1, end_line=5)
    assert span.start_line == 1
    assert span.end_line == 5

    with pytest.raises(PydanticValidationError):
        Position.model_validate({"line": 0, "character": 0, "unexpected": True})


def test_app_config_and_build_config_defaults() -> None:
    """AppConfig preserves external defaults and rejects unknown fields."""
    cfg = build_config(db_path="custom.db")
    assert isinstance(cfg, AppConfig)
    assert cfg.app_name == "repo-context-mcp"
    assert cfg.db_path == Path("custom.db")
    assert build_config().db_path == Path("repo_context.db")

    with pytest.raises(PydanticValidationError):
        AppConfig.model_validate({"unknown_option": 1})


def test_repo_and_file_record_v2_fields() -> None:
    """RepoRecord and FileRecord support v0.2.0 fields and ISO timestamp validation."""
    repo = RepoRecord(
        id="repo:test:1234abcd",
        root_path="/path/to/repo",
        name="test-repo",
        default_language=Language.PYTHON,
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
        SymbolKind.CONSTANT,
        SymbolKind.ATTRIBUTE,
        SymbolKind.TYPE_ALIAS,
        SymbolKind.MODULE,
        SymbolKind.CLASS,
        SymbolKind.FUNCTION,
        SymbolKind.ASYNC_FUNCTION,
        SymbolKind.METHOD,
        SymbolKind.ASYNC_METHOD,
        SymbolKind.LOCAL_FUNCTION,
        SymbolKind.LOCAL_ASYNC_FUNCTION,
    ],
)
def test_symbol_node_supports_all_v2_kinds_and_structured_ranges(kind: SymbolKind) -> None:
    """SymbolNode validates all v2 symbol kinds, structured Range fields, and SymbolSummary."""
    long_name = "S" * 512
    node = SymbolNode.model_validate(
        {
            "id": f"sym:repo:test:{kind.value}:app.{long_name}",
            "repo_id": "repo:test:1234abcd",
            "file_id": "file:repo:test:1234abcd:app/service.py",
            "kind": kind.value,
            "name": long_name,
            "qualified_name": f"app.{long_name}",
            "uri": "file:///path/to/repo/app/service.py",
            "decl_range": {
                "start": {"line": 0, "character": 0},
                "end": {"line": 2, "character": 0},
            },
            "selection_range": {
                "start": {"line": 0, "character": 0},
                "end": {"line": 0, "character": 5},
            },
            "start_line": 1,
            "end_line": 3,
            "signature": "MAX_RETRIES: int = 3",
            "scope": SymbolScope.MODULE,
            "payload": {"value_hint": "3"},
        }
    )
    assert node.kind == kind
    assert node.start_line == 1
    assert node.end_line == 3
    assert node.signature == "MAX_RETRIES: int = 3"
    assert node.decl_range is not None
    assert node.decl_range.end.line == 2
    assert node.selection_range is not None
    assert node.selection_range.end.character == 5
    assert node.payload == {"value_hint": "3"}

    summary = SymbolSummary.from_node(
        node,
        file_path="app/service.py",
        module_path="app.service",
        is_test=False,
    )
    assert summary.id == node.id
    assert summary.file_path == "app/service.py"
    assert summary.module_path == "app.service"
    assert summary.decl_range == node.decl_range


def test_symbol_node_rejects_extra_legacy_json_fields_and_oversized_name() -> None:
    """SymbolNode forbids legacy *_json fields, invalid payload types, and name > 1024."""
    with pytest.raises(PydanticValidationError):
        SymbolNode.model_validate(
            {
                "id": "sym:repo:test:constant:app.X",
                "repo_id": "repo:test:1234abcd",
                "file_id": "file:repo:test:1234abcd:app/service.py",
                "kind": "constant",
                "name": "X",
                "qualified_name": "app.X",
                "uri": "file:///path/to/repo/app/service.py",
                "payload_json": '{"legacy": true}',
            }
        )

    with pytest.raises(PydanticValidationError):
        SymbolNode.model_validate(
            {
                "id": "sym:repo:test:constant:app.X",
                "repo_id": "repo:test:1234abcd",
                "file_id": "file:repo:test:1234abcd:app/service.py",
                "kind": "constant",
                "name": "X",
                "qualified_name": "app.X",
                "uri": "file:///path/to/repo/app/service.py",
                "payload": "not-a-dict",
            }
        )

    with pytest.raises(PydanticValidationError):
        SymbolNode(
            id="sym:repo:test:constant:app.X",
            repo_id="repo:test:1234abcd",
            file_id="file:repo:test:1234abcd:app/service.py",
            kind=SymbolKind.CONSTANT,
            name="X" * 1025,
            qualified_name="app.X",
            uri="file:///path/to/repo/app/service.py",
        )


@pytest.mark.parametrize(
    "kind",
    [
        EdgeKind.CONTAINS,
        EdgeKind.IMPORTS,
        EdgeKind.INHERITS,
        EdgeKind.SCOPE_PARENT,
        EdgeKind.CALLS,
        EdgeKind.REFERENCES,
    ],
)
def test_edge_supports_normalized_v2_kinds_and_structured_evidence(kind: EdgeKind) -> None:
    """Edge accepts normalized v2 kinds and structured evidence_range, rejecting bad inputs."""
    edge = Edge.model_validate(
        {
            "id": f"edge:repo:test:{kind.value}:a->b",
            "repo_id": "repo:test:1234abcd",
            "kind": kind.value,
            "from_id": "sym:a",
            "to_id": "sym:b",
            "payload": {"target": "b"},
            "evidence_range": {
                "start": {"line": 1, "character": 0},
                "end": {"line": 1, "character": 3},
            },
        }
    )
    assert edge.kind == kind
    assert edge.payload == {"target": "b"}
    assert edge.evidence_range is not None
    assert edge.evidence_range.start.line == 1

    with pytest.raises(PydanticValidationError):
        Edge.model_validate(
            {
                "id": "edge:bad",
                "repo_id": "repo:test:1234abcd",
                "kind": "calls",
                "from_id": "sym:a",
                "to_id": "sym:b",
                "evidence_range_json": "[legacy",
            }
        )

    with pytest.raises(PydanticValidationError):
        Edge.model_validate(
            {
                "id": "edge:legacy_kind",
                "repo_id": "repo:test:1234abcd",
                "kind": "SCOPE_PARENT",
                "from_id": "sym:a",
                "to_id": "sym:b",
            }
        )


def test_symbol_context_and_risk_assessment_frozen_and_independent_defaults() -> None:
    """SymbolContext and RiskAssessment are immutable FrozenModels with isolated defaults."""
    focus1 = FocusSymbol(
        id="sym:repo:test:function:app.fn1",
        kind=SymbolKind.FUNCTION,
        name="fn1",
        qualified_name="app.fn1",
    )
    focus2 = FocusSymbol(
        id="sym:repo:test:function:app.fn2",
        kind=SymbolKind.FUNCTION,
        name="fn2",
        qualified_name="app.fn2",
    )
    child = CompactSymbol(
        id="sym:repo:test:local_function:app.fn1.inner",
        kind=SymbolKind.LOCAL_FUNCTION,
        name="inner",
    )
    ctx1 = SymbolContext(focus_symbol=focus1, structural_children=[child])
    ctx2 = SymbolContext(focus_symbol=focus2)
    assert len(ctx1.structural_children) == 1
    assert ctx2.structural_children == []
    with pytest.raises(PydanticValidationError):
        ctx1.structural_children = []

    target = RiskTarget(
        symbol_id="sym:repo:test:function:app.fn1",
        qualified_name="app.fn1",
        kind=SymbolKind.FUNCTION,
        scope="module",
        file_id="file:repo:test:app.py",
        file_path="app.py",
        module_path="app",
    )
    facts = RiskFacts.model_validate({"symbol_kinds": {"method", "function"}})
    assert facts.symbol_kinds == ["function", "method"]

    a1 = RiskAssessment(
        targets=[target],
        facts=facts,
        issues=["public_surface_change"],
        risk_score=15,
        decision=RiskDecision.SAFE_ENOUGH,
    )
    a2 = RiskAssessment()
    assert a1.issues == ["public_surface_change"]
    assert a2.issues == []


def test_to_json_from_json_and_timestamp_helpers() -> None:
    """to_json, from_json, validate_iso_timestamp, and latest_timestamp handle edge cases."""
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

    with pytest.raises(ValidationError):
        from_json("{invalid")

    assert validate_iso_timestamp(None) is None
    assert validate_iso_timestamp("2026-01-01T00:00:00Z") == "2026-01-01T00:00:00Z"
    with pytest.raises(ValueError):
        validate_iso_timestamp("not-a-timestamp")

    assert (
        latest_timestamp("2026-01-01T00:00:00Z", None, "2026-06-01T00:00:00Z")
        == "2026-06-01T00:00:00Z"
    )
    assert latest_timestamp(None, "not-a-timestamp") is None
