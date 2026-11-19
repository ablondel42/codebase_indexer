"""Bounded symbol context, hierarchy/edge bundles, and structural/freshness/confidence models."""

from pathlib import Path

from pydantic import Field

from core.models.base import FrozenModel
from core.models.edge import Edge, SymbolReferenceStats
from core.models.enums import EdgeKind, SymbolKind, SymbolScope
from core.models.file import FileRecord
from core.models.node import SymbolNode
from core.models.range import Range


class CompactSymbol(FrozenModel):
    """Token-lean child or same-file sibling symbol entry."""

    id: str
    kind: SymbolKind
    name: str
    lines: str | None = None
    signature: str | None = None
    doc_summary: str | None = None


class FocusSymbol(FrozenModel):
    """Detailed focus or parent symbol projection with optional inline source."""

    id: str
    kind: SymbolKind
    name: str
    qualified_name: str
    file_path: str | None = None
    module_path: str | None = None
    start_line: int | None = None
    end_line: int | None = None
    lines: str | None = None
    decl_range: Range | None = None
    selection_range: Range | None = None
    signature: str | None = None
    doc_summary: str | None = None
    parent_id: str | None = None
    scope: SymbolScope | None = None
    lexical_parent_id: str | None = None
    visibility_hint: str | None = None
    confidence: float = 1.0
    source: str | None = None
    raw_source: str | None = None
    source_truncated: bool = False
    source_error: str | None = None


class ContextEdge(FrozenModel):
    """Compact relationship entry connecting the focus symbol and a peer endpoint."""

    id: str
    kind: EdgeKind
    from_id: str
    to_id: str
    peer_name: str | None = None
    file_path: str | None = None
    line: int | None = None
    confidence: float = 1.0
    source: str | None = None
    last_indexed_at: str | None = None


class SymbolHierarchy(FrozenModel):
    """Internal immutable bundle of structural, lexical, and sibling nodes for a symbol."""

    structural_parent: SymbolNode | None = None
    structural_children: list[SymbolNode] = Field(default_factory=list)
    lexical_parent: SymbolNode | None = None
    lexical_children: list[SymbolNode] = Field(default_factory=list)
    direct_siblings: list[SymbolNode] = Field(default_factory=list)


class SymbolEdgeBundle(FrozenModel):
    """Internal immutable bundle of raw, semantic, and balanced-bounded symbol edges."""

    all_raw: list[Edge] = Field(default_factory=list)
    incoming_semantic: list[Edge] = Field(default_factory=list)
    outgoing_semantic: list[Edge] = Field(default_factory=list)
    bounded_incoming: list[Edge] = Field(default_factory=list)
    bounded_outgoing: list[Edge] = Field(default_factory=list)


class RelationshipCounts(FrozenModel):
    """Exact unbounded relationship totals grouped by semantic direction and kind."""

    callers: int = 0
    callees: int = 0
    imported_by: int = 0
    imports: int = 0
    subclasses: int = 0
    bases: int = 0
    references_in: int = 0
    references_out: int = 0


class StructuralTotals(FrozenModel):
    """Optional unbounded count overrides supplied to ``ContextEvaluator.evaluate_structural``."""

    total_structural_child_count: int | None = None
    total_lexical_child_count: int | None = None
    total_incoming_count: int | None = None
    total_outgoing_count: int | None = None
    total_sibling_count: int | None = None
    relationship_counts: RelationshipCounts | None = None


class FreshnessEvalInputs(FrozenModel):
    """Optional node, raw-edge, and disk-file inputs for ``ContextEvaluator.evaluate_freshness``."""

    focus_node: SymbolNode | None = None
    raw_edges: list[Edge] | None = None
    file_record: FileRecord | None = None
    repo_root: Path | str | None = None


class StructuralSummary(FrozenModel):
    """Structural hierarchy counts, truncation flags, scope flags, and relationship totals."""

    has_structural_parent: bool = False
    structural_child_count: int = 0
    structural_children_truncated: bool = False
    has_lexical_parent: bool = False
    lexical_child_count: int = 0
    incoming_edge_count: int = 0
    outgoing_edge_count: int = 0
    same_file_sibling_count: int = 0
    scope: SymbolScope | None = None
    is_local_declaration: bool = False
    is_nested_declaration: bool = False
    relationship_counts: RelationshipCounts = Field(default_factory=RelationshipCounts)


class FreshnessSummary(FrozenModel):
    """Node/edge snapshot timestamps, LSP enrichment status, and live disk freshness."""

    node_last_indexed_at: str | None = None
    edge_snapshot_last_indexed_at: str | None = None
    has_incoming_edges: bool = False
    has_outgoing_edges: bool = False
    context_source: str = "ast_only"
    is_fresh: bool = True
    stale_reason: str | None = None


class ConfidenceSummary(FrozenModel):
    """Confidence bounds, unresolved placeholder presence, and graph-only flag."""

    focus_symbol_confidence: float = 1.0
    edge_confidence_min: float | None = None
    edge_confidence_max: float | None = None
    contains_placeholder_targets: bool = False
    graph_only: bool = True


class ContextBuildOptions(FrozenModel):
    """Bounded item budgets and inline source options for ``SymbolContextBuilder``."""

    include_source: bool = False
    max_source_lines: int = Field(default=80, ge=1)
    child_limit: int = Field(default=25, ge=0)
    edge_limit: int = Field(default=25, ge=0)
    sibling_limit: int = Field(default=15, ge=0)


class SymbolContext(FrozenModel):
    """Complete bounded context assembled around a focus symbol."""

    focus_symbol: FocusSymbol
    structural_parent: FocusSymbol | None = None
    structural_children: list[CompactSymbol] = Field(default_factory=list)
    lexical_parent: FocusSymbol | None = None
    lexical_children: list[CompactSymbol] = Field(default_factory=list)
    incoming_edges: list[ContextEdge] = Field(default_factory=list)
    outgoing_edges: list[ContextEdge] = Field(default_factory=list)
    file_siblings: list[CompactSymbol] = Field(default_factory=list)
    reference_summary: SymbolReferenceStats = Field(default_factory=SymbolReferenceStats)
    structural_summary: StructuralSummary = Field(default_factory=StructuralSummary)
    freshness: FreshnessSummary = Field(default_factory=FreshnessSummary)
    confidence: ConfidenceSummary = Field(default_factory=ConfidenceSummary)


__all__ = [
    "CompactSymbol",
    "ConfidenceSummary",
    "ContextBuildOptions",
    "ContextEdge",
    "FocusSymbol",
    "FreshnessEvalInputs",
    "FreshnessSummary",
    "RelationshipCounts",
    "StructuralSummary",
    "StructuralTotals",
    "SymbolContext",
    "SymbolEdgeBundle",
    "SymbolHierarchy",
]
