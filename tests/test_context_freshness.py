"""Tests for ContextEvaluator freshness metadata and live disk hash checks."""

from pathlib import Path

from context import (
    ContextEdge,
    ContextEvaluator,
    FocusSymbol,
    FreshnessEvalInputs,
    SymbolContext,
)
from core.models import (
    Edge,
    EdgeKind,
    FileRecord,
    SymbolKind,
    SymbolNode,
    SymbolReferenceStats,
)
from core.utils.hashing import sha256_file


def _focus_symbol(sym_id: str = "node:1") -> FocusSymbol:
    """Create a minimal FocusSymbol for freshness unit tests."""
    return FocusSymbol(
        id=sym_id,
        kind=SymbolKind.FUNCTION,
        name="fn",
        qualified_name="mod.fn",
    )


def _focus_node(
    sym_id: str = "node:1",
    *,
    file_id: str = "file:project:1:mod.py",
    last_indexed_at: str | None = "2026-01-01T00:00:00Z",
) -> SymbolNode:
    """Create a minimal SymbolNode for freshness unit tests."""
    return SymbolNode(
        id=sym_id,
        repo_id="project:1",
        file_id=file_id,
        kind=SymbolKind.FUNCTION,
        name="fn",
        qualified_name="mod.fn",
        uri="file:///tmp/mod.py",
        last_indexed_at=last_indexed_at,
    )


def test_evaluate_freshness_ast_only_and_edge_snapshot() -> None:
    """evaluate_freshness reports ast_only by default and computes latest edge timestamp."""
    evaluator = ContextEvaluator()
    focus_node = _focus_node()
    raw_edges = [
        Edge(
            id="edge:in",
            repo_id="project:1",
            from_id="node:other",
            to_id="node:1",
            kind=EdgeKind.CALLS,
            last_indexed_at="2026-01-01T01:00:00Z",
        ),
        Edge(
            id="edge:out",
            repo_id="project:1",
            from_id="node:1",
            to_id="node:dest",
            kind=EdgeKind.CALLS,
            last_indexed_at="2026-01-02T03:00:00Z",
        ),
    ]
    context = SymbolContext(
        focus_symbol=_focus_symbol(),
        incoming_edges=[
            ContextEdge(
                id="edge:in",
                kind=EdgeKind.CALLS,
                from_id="node:other",
                to_id="node:1",
                last_indexed_at="2026-01-01T01:00:00Z",
            )
        ],
        outgoing_edges=[
            ContextEdge(
                id="edge:out",
                kind=EdgeKind.CALLS,
                from_id="node:1",
                to_id="node:dest",
                last_indexed_at="2026-01-02T03:00:00Z",
            )
        ],
    )

    freshness = evaluator.evaluate_freshness(
        context,
        FreshnessEvalInputs(focus_node=focus_node, raw_edges=raw_edges),
    )
    assert freshness.node_last_indexed_at == "2026-01-01T00:00:00Z"
    assert freshness.edge_snapshot_last_indexed_at == "2026-01-02T03:00:00Z"
    assert freshness.has_incoming_edges is True
    assert freshness.has_outgoing_edges is True
    assert freshness.context_source == "ast_only"
    assert freshness.is_fresh is True


def test_evaluate_freshness_lsp_plus_ast() -> None:
    """evaluate_freshness reports lsp+ast when LSP reference summary or edges are present."""
    evaluator = ContextEvaluator()
    focus_node = _focus_node()
    context = SymbolContext(
        focus_symbol=_focus_symbol(),
        reference_summary=SymbolReferenceStats(available=True, reference_source="lsp"),
    )
    freshness = evaluator.evaluate_freshness(
        context,
        FreshnessEvalInputs(focus_node=focus_node, raw_edges=[]),
    )
    assert freshness.context_source == "lsp+ast"
    assert freshness.edge_snapshot_last_indexed_at is None


def test_evaluate_freshness_missing_timestamp_and_disk_staleness(tmp_path: Path) -> None:
    """evaluate_freshness marks missing timestamp or mismatched disk hash as stale."""
    evaluator = ContextEvaluator()
    context = SymbolContext(focus_symbol=_focus_symbol())

    missing_ts = evaluator.evaluate_freshness(
        context,
        FreshnessEvalInputs(focus_node=_focus_node(last_indexed_at=None)),
    )
    assert missing_ts.node_last_indexed_at is None
    assert missing_ts.is_fresh is False
    assert missing_ts.stale_reason == "missing_last_indexed_at"

    src = tmp_path / "mod.py"
    src.write_text("x = 1\n", encoding="utf-8")
    file_rec = FileRecord(
        id="file:project:1:mod.py",
        repo_id="project:1",
        file_path="mod.py",
        uri=src.as_uri(),
        module_path="mod",
        content_hash=sha256_file(src),
        size_bytes=src.stat().st_size,
        last_modified_at="2026-01-01T00:00:00Z",
    )
    focus = _focus_node()

    fresh = evaluator.evaluate_freshness(
        context,
        FreshnessEvalInputs(focus_node=focus, file_record=file_rec, repo_root=tmp_path),
    )
    assert fresh.is_fresh is True
    assert fresh.stale_reason is None

    src.write_text("x = 2\n", encoding="utf-8")
    stale = evaluator.evaluate_freshness(
        context,
        FreshnessEvalInputs(focus_node=focus, file_record=file_rec, repo_root=tmp_path),
    )
    assert stale.is_fresh is False
    assert stale.stale_reason == "content_hash_mismatch"
