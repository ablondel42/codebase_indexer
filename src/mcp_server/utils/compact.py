"""Token-lean MCP wire payload adapters omitting null noise and verbose internal fields."""

import json
from collections.abc import Mapping, Sequence
from typing import Any

from context.models.outline import FileOutline, OutlineNode
from context.models.source import SymbolSnippet
from context.models.symbol_context import (
    CompactSymbol,
    ContextEdge,
    FocusSymbol,
    RelationshipCounts,
    StructuralSummary,
    SymbolContext,
)
from core.models.enums import EdgeKind
from core.models.node import SymbolSummary
from core.models.search import CodeSearchReport
from core.utils.file_io import format_line_span
from engine.models.responses import FileListPage, RepoOverview, SymbolSearchPage
from engine.utils.projections import (
    omit_none,
    project_code_search,
    project_risk_assessment,
    project_symbol_references,
    project_symbol_search,
    project_symbol_source,
    project_symbol_summary,
)
from graph.models.references import SymbolReferencesReport
from graph.models.risk import RiskAssessment

_COMPACT_OUTLINE_NODE_THRESHOLD = 20
_MAX_OUTLINE_CHILDREN = 60
_MAX_CHILD_DOCS = 10


def serialize_mcp_json(payload: Mapping[str, Any]) -> str:
    """Serialize an MCP response dictionary into compact single-line JSON.

    Args:
        payload: Response envelope mapping.

    Returns:
        Compact JSON string encoded with ``(",", ":")`` separators.
    """
    return json.dumps(payload, separators=(",", ":"))


def adapt_repo_overview(overview: RepoOverview) -> dict[str, Any]:
    """Adapt a ``RepoOverview`` model into the ``get_repo_overview`` wire payload.

    Args:
        overview: Typed ``RepoOverview`` query response.

    Returns:
        Token-lean dictionary matching the legacy ``get_repo_overview`` schema.
    """
    repo = overview.repo
    stats = overview.stats
    recent_runs = [
        omit_none({
            "id": run.id,
            "run_type": run.run_type,
            "status": run.status,
            "files_scanned": run.files_scanned,
            "nodes_extracted": run.nodes_extracted,
            "edges_extracted": run.edges_extracted,
            "completed_at": run.completed_at,
        })
        for run in overview.recent_runs
    ]
    return {
        "repo": {
            "id": repo.id,
            "name": repo.name,
            "root_path": repo.root_path,
            "language": str(repo.default_language),
            "last_indexed_at": repo.last_indexed_at,
        },
        "stats": {
            "file_count": stats.file_count,
            "test_file_count": stats.test_file_count,
            "non_test_file_count": stats.file_count - stats.test_file_count,
            "total_lines": stats.total_lines,
            "node_count": stats.node_count,
            "edge_count": stats.edge_count,
            "kind_counts": dict(stats.kind_counts),
            "edge_kind_counts": dict(stats.edge_kind_counts),
        },
        "top_directories": [
            {"path": item.path, "file_count": item.file_count} for item in overview.top_directories
        ],
        "recent_runs": recent_runs,
    }


def adapt_file_list(page: FileListPage) -> dict[str, Any]:
    """Adapt a ``FileListPage`` model into the ``list_files`` wire payload.

    Args:
        page: Paginated ``FileListPage`` response.

    Returns:
        Token-lean dictionary matching the legacy ``list_files`` schema.
    """
    return {
        "repo_id": page.repo_id,
        "total_count": page.total_count,
        "returned_count": page.returned_count,
        "offset": page.offset,
        "limit": page.limit,
        "has_more": page.has_more,
        "files": [
            {
                "file_path": item.file_path,
                "module_path": item.module_path,
                "line_count": item.line_count,
                "size_bytes": item.size_bytes,
                "is_test": item.is_test,
            }
            for item in page.files
        ],
    }


def _count_outline_nodes(nodes: Sequence[OutlineNode]) -> int:
    """Count total symbol nodes across a recursive ``OutlineNode`` forest.

    Args:
        nodes: Top-level or child ``OutlineNode`` sequence.

    Returns:
        Total node count including nested descendants.
    """
    total = 0
    for node in nodes:
        total += 1
        if node.children:
            total += _count_outline_nodes(node.children)
    return total


def _adapt_outline_node(
    node: OutlineNode,
    *,
    depth: int = 1,
    compact_nested: bool = False,
) -> dict[str, Any]:
    """Compact one ``OutlineNode``, trimming nested metadata on large file outlines.

    Args:
        node: ``OutlineNode`` instance to serialize.
        depth: One-based nesting depth in the outline tree.
        compact_nested: Whether nested nodes (``depth >= 2``) omit IDs and docs.

    Returns:
        Compact dictionary for ``node``.
    """
    raw_children = list(node.children)
    truncated_children = 0
    if len(raw_children) > _MAX_OUTLINE_CHILDREN:
        truncated_children = len(raw_children) - _MAX_OUTLINE_CHILDREN
        raw_children = raw_children[:_MAX_OUTLINE_CHILDREN]

    children = [
        _adapt_outline_node(child, depth=depth + 1, compact_nested=compact_nested)
        for child in raw_children
    ]
    signature = node.signature
    has_signature = bool(signature and signature.strip())
    if compact_nested and depth >= 2:
        entry: dict[str, Any] = {
            "kind": None if has_signature else str(node.kind),
            "name": None if has_signature else node.name,
            "lines": node.lines,
            "signature": signature if has_signature else None,
        }
    else:
        entry = {
            "id": node.id,
            "kind": str(node.kind),
            "name": node.name,
            "lines": node.lines,
            "signature": signature,
            "doc": node.doc,
        }

    if children:
        entry["children"] = children
    if truncated_children > 0:
        entry["truncated_children"] = truncated_children
    return omit_none(entry)


def adapt_outline(outline: FileOutline) -> dict[str, Any]:
    """Adapt a ``FileOutline`` model into a token-lean MCP outline payload.

    Args:
        outline: Typed ``FileOutline`` response.

    Returns:
        Compact outline dictionary omitting null fields and empty ``children`` lists.
    """
    total_nodes = _count_outline_nodes(outline.symbols)
    compact_nested = total_nodes > _COMPACT_OUTLINE_NODE_THRESHOLD
    return omit_none({
        "file_path": outline.file_path,
        "location": outline.location,
        "module_path": outline.module_path,
        "line_count": outline.line_count,
        "is_fresh": outline.is_fresh,
        "stale_reason": outline.stale_reason,
        "doc": outline.doc,
        "imports": list(outline.imports),
        "symbols": [
            _adapt_outline_node(item, depth=1, compact_nested=compact_nested)
            for item in outline.symbols
        ],
    })


def adapt_symbol_summary(symbol: SymbolSummary) -> dict[str, Any]:
    """Adapt a ``SymbolSummary`` into a compact MCP symbol entry."""
    return project_symbol_summary(symbol, compact=True)


def adapt_symbol_search(page: SymbolSearchPage) -> dict[str, Any]:
    """Adapt a ``SymbolSearchPage`` into the ``search_symbols`` wire payload."""
    return project_symbol_search(page, compact=True)


def adapt_symbol_source(snippet: SymbolSnippet) -> dict[str, Any]:
    """Adapt a ``SymbolSnippet`` into the ``get_symbol_source`` wire payload."""
    return project_symbol_source(snippet, drop_none=True)


def _adapt_parent_stub(parent: FocusSymbol | None) -> dict[str, Any] | None:
    """Adapt a structural or lexical parent ``FocusSymbol`` into a minimal stub.

    Args:
        parent: Optional parent ``FocusSymbol``.

    Returns:
        Minimal reference stub dictionary, or ``None`` when ``parent`` is ``None``.
    """
    if parent is None:
        return None
    lines = parent.lines or format_line_span(parent.start_line, parent.end_line)
    return omit_none({
        "id": parent.id,
        "kind": str(parent.kind),
        "qualified_name": parent.qualified_name or parent.name,
        "lines": lines,
    })


def _adapt_child_stub(child: CompactSymbol, *, include_doc: bool = True) -> dict[str, Any]:
    """Adapt a ``CompactSymbol`` child entry, suppressing docstrings on wide classes.

    Args:
        child: Child ``CompactSymbol`` model.
        include_doc: Whether to include ``doc_summary``.

    Returns:
        Compact child skeleton dictionary.
    """
    return omit_none({
        "id": child.id,
        "kind": str(child.kind),
        "name": child.name,
        "lines": child.lines,
        "signature": child.signature,
        "doc_summary": child.doc_summary if include_doc else None,
    })


def _adapt_focus_symbol(focus: FocusSymbol) -> dict[str, Any]:
    """Compact ``FocusSymbol`` omitting ``raw_source`` and raw range/parent IDs.

    Args:
        focus: Focus ``FocusSymbol`` model.

    Returns:
        Compact focus symbol dictionary for ``get_symbol_context``.
    """
    lines = focus.lines or format_line_span(focus.start_line, focus.end_line)
    return omit_none({
        "id": focus.id,
        "kind": str(focus.kind),
        "name": focus.name,
        "qualified_name": focus.qualified_name,
        "location": focus.location,
        "file_path": focus.file_path,
        "module_path": focus.module_path,
        "lines": lines,
        "signature": focus.signature,
        "doc_summary": focus.doc_summary,
        "scope": str(focus.scope) if focus.scope is not None else None,
        "visibility_hint": focus.visibility_hint,
        "confidence": focus.confidence,
        "source": focus.source,
        "source_truncated": True if focus.source_truncated else None,
        "source_error": focus.source_error,
    })


def _adapt_context_edge_item(edge: ContextEdge, *, is_incoming: bool) -> dict[str, Any]:
    """Convert one ``ContextEdge`` into a compact peer relationship item.

    Args:
        edge: ``ContextEdge`` model.
        is_incoming: ``True`` when grouping incoming edges, ``False`` for outgoing.

    Returns:
        Compact peer relationship dictionary.
    """
    peer_id = edge.from_id if is_incoming else edge.to_id
    return omit_none({
        "id": peer_id,
        "edge_id": edge.id,
        "name": edge.peer_name,
        "location": edge.location,
        "file_path": edge.file_path,
        "line": edge.line,
        "confidence": edge.confidence,
    })


def _group_context_edges(
    edges: Sequence[ContextEdge],
    *,
    is_incoming: bool,
) -> dict[EdgeKind, list[dict[str, Any]]]:
    """Group incoming or outgoing ``ContextEdge`` items by ``EdgeKind``.

    Args:
        edges: Bounded sequence of ``ContextEdge`` models.
        is_incoming: Direction flag forwarded to ``_adapt_context_edge_item``.

    Returns:
        Mapping from ``EdgeKind`` to adapted peer edge dictionaries.
    """
    by_kind: dict[EdgeKind, list[dict[str, Any]]] = {}
    for edge in edges:
        by_kind.setdefault(edge.kind, []).append(
            _adapt_context_edge_item(edge, is_incoming=is_incoming)
        )
    return by_kind


def _build_hierarchy_fields(ctx: SymbolContext) -> dict[str, Any]:
    """Build deduplicated structural and lexical parent/child fields for ``ctx``.

    Args:
        ctx: ``SymbolContext`` model.

    Returns:
        Dictionary of structural/lexical parent and child entries prior to ``omit_none``.
    """
    struct_children = ctx.structural_children
    lex_children = ctx.lexical_children
    struct_parent_id = ctx.structural_parent.id if ctx.structural_parent is not None else None
    lex_parent_id = ctx.lexical_parent.id if ctx.lexical_parent is not None else None
    include_lex_parent = ctx.lexical_parent is not None and (
        struct_parent_id is None or lex_parent_id != struct_parent_id
    )
    struct_ids = [item.id for item in struct_children]
    lex_ids = [item.id for item in lex_children]
    include_lex_children = bool(lex_children) and lex_ids != struct_ids

    return {
        "structural_parent": _adapt_parent_stub(ctx.structural_parent),
        "structural_children": (
            [
                _adapt_child_stub(item, include_doc=len(struct_children) <= _MAX_CHILD_DOCS)
                for item in struct_children
            ]
            if struct_children
            else None
        ),
        "lexical_parent": _adapt_parent_stub(ctx.lexical_parent) if include_lex_parent else None,
        "lexical_children": (
            [
                _adapt_child_stub(item, include_doc=len(lex_children) <= _MAX_CHILD_DOCS)
                for item in lex_children
            ]
            if include_lex_children
            else None
        ),
    }


def _populate_relationship_buckets(
    payload: dict[str, Any],
    in_by_kind: Mapping[EdgeKind, list[dict[str, Any]]],
    out_by_kind: Mapping[EdgeKind, list[dict[str, Any]]],
    rel_counts: RelationshipCounts,
) -> None:
    """Attach non-empty semantic relationship buckets onto ``payload``.

    Args:
        payload: Mutable context output dictionary being populated.
        in_by_kind: Incoming edges grouped by ``EdgeKind``.
        out_by_kind: Outgoing edges grouped by ``EdgeKind``.
        rel_counts: Exact unbounded ``RelationshipCounts`` totals.
    """
    bucket_specs: tuple[tuple[str, list[dict[str, Any]], int], ...] = (
        ("callers", in_by_kind.get(EdgeKind.CALLS, []), rel_counts.callers),
        ("callees", out_by_kind.get(EdgeKind.CALLS, []), rel_counts.callees),
        ("imported_by", in_by_kind.get(EdgeKind.IMPORTS, []), rel_counts.imported_by),
        ("imports", out_by_kind.get(EdgeKind.IMPORTS, []), rel_counts.imports),
        ("subclasses", in_by_kind.get(EdgeKind.INHERITS, []), rel_counts.subclasses),
        ("bases", out_by_kind.get(EdgeKind.INHERITS, []), rel_counts.bases),
        ("references_in", in_by_kind.get(EdgeKind.REFERENCES, []), rel_counts.references_in),
        ("references_out", out_by_kind.get(EdgeKind.REFERENCES, []), rel_counts.references_out),
    )
    for key, items, total in bucket_specs:
        count = total if total > 0 else len(items)
        if count > 0 or items:
            payload[key] = {"total": count, "items": items}


def _adapt_structural_summary(summary: StructuralSummary) -> dict[str, Any]:
    """Strip false/zero optional flags from ``StructuralSummary`` into a compact dict.

    Args:
        summary: ``StructuralSummary`` model.

    Returns:
        Compact structural summary dictionary.
    """
    return omit_none({
        "scope": str(summary.scope) if summary.scope is not None else None,
        "structural_child_count": summary.structural_child_count,
        "structural_children_truncated": summary.structural_children_truncated or None,
        "lexical_child_count": summary.lexical_child_count or None,
        "incoming_edge_count": summary.incoming_edge_count,
        "outgoing_edge_count": summary.outgoing_edge_count,
        "same_file_sibling_count": summary.same_file_sibling_count,
        "is_local_declaration": summary.is_local_declaration or None,
        "is_nested_declaration": summary.is_nested_declaration or None,
    })


def adapt_symbol_context(ctx: SymbolContext) -> dict[str, Any]:
    """Adapt a ``SymbolContext`` model into a grouped, token-lean MCP payload.

    Args:
        ctx: Immutable ``SymbolContext`` model.

    Returns:
        Compact dictionary matching the legacy ``get_symbol_context`` wire shape.
    """
    payload: dict[str, Any] = {
        "focus_symbol": _adapt_focus_symbol(ctx.focus_symbol),
        **_build_hierarchy_fields(ctx),
    }
    _populate_relationship_buckets(
        payload,
        _group_context_edges(ctx.incoming_edges, is_incoming=True),
        _group_context_edges(ctx.outgoing_edges, is_incoming=False),
        ctx.structural_summary.relationship_counts,
    )
    payload["structural_summary"] = _adapt_structural_summary(ctx.structural_summary)
    payload["reference_summary"] = omit_none(ctx.reference_summary.model_dump(mode="json"))
    payload["freshness"] = omit_none(ctx.freshness.model_dump(mode="json"))
    payload["confidence"] = omit_none(ctx.confidence.model_dump(mode="json"))
    return omit_none(payload)


def adapt_symbol_references(report: SymbolReferencesReport) -> dict[str, Any]:
    """Adapt a ``SymbolReferencesReport`` into the ``get_symbol_references`` wire payload."""
    return project_symbol_references(report, compact=True)


def adapt_risk_assessment(result: RiskAssessment) -> dict[str, Any]:
    """Adapt a ``RiskAssessment`` model into the deterministic ``analyze_risk`` wire payload."""
    return project_risk_assessment(result)


def adapt_code_search(report: CodeSearchReport) -> dict[str, Any]:
    """Adapt a ``CodeSearchReport`` into a compact MCP payload with full ``file_counts``."""
    return project_code_search(report)


__all__ = [
    "adapt_code_search",
    "adapt_file_list",
    "adapt_outline",
    "adapt_repo_overview",
    "adapt_risk_assessment",
    "adapt_symbol_context",
    "adapt_symbol_references",
    "adapt_symbol_search",
    "adapt_symbol_source",
    "adapt_symbol_summary",
    "omit_none",
    "serialize_mcp_json",
]
