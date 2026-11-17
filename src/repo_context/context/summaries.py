"""Summary builders for symbol context."""

from collections.abc import Sequence

from repo_context.constants import (
    SYMBOL_KIND_ASYNC_METHOD,
    SYMBOL_KIND_LOCAL_ASYNC_FUNCTION,
    SYMBOL_KIND_LOCAL_FUNCTION,
    SYMBOL_KIND_METHOD,
)
from repo_context.context.helpers import (
    edges_have_placeholder_targets,
    has_placeholder_targets,
    is_lsp_enriched,
)
from repo_context.models import SymbolContext

LOCAL_CALLABLE_KINDS = {
    SYMBOL_KIND_LOCAL_FUNCTION,
    SYMBOL_KIND_LOCAL_ASYNC_FUNCTION,
}


def _is_local_declaration(focus_symbol: dict) -> bool:
    """Check if symbol is a function-local declaration."""
    kind = focus_symbol.get("kind", "")
    scope = focus_symbol.get("scope", "")

    if kind in LOCAL_CALLABLE_KINDS:
        return True
    if kind == "class" and scope == "function":
        return True
    return False


def _is_nested_declaration(focus_symbol: dict) -> bool:
    """Return True strictly for lexically nested function-scoped declarations."""
    lexical_parent_id = focus_symbol.get("lexical_parent_id")
    if lexical_parent_id is None:
        return False
    # Normal class methods are structural class members, never lexical nested declarations.
    if (
        focus_symbol.get("kind") in (SYMBOL_KIND_METHOD, SYMBOL_KIND_ASYNC_METHOD)
        and focus_symbol.get("scope") == "class"
    ):
        return False
    return True


def build_structural_summary(
    context: SymbolContext,
    *,
    total_structural_child_count: int | None = None,
    total_lexical_child_count: int | None = None,
    total_incoming_count: int | None = None,
    total_outgoing_count: int | None = None,
    total_sibling_count: int | None = None,
    relationship_counts: dict[str, int] | None = None,
) -> dict:
    """Build a compact structural and relationship summary from symbol context."""
    focus = context.focus_symbol
    structural_child_total = (
        total_structural_child_count
        if total_structural_child_count is not None
        else len(context.structural_children)
    )
    lexical_child_total = (
        total_lexical_child_count
        if total_lexical_child_count is not None
        else len(context.lexical_children)
    )
    incoming_total = (
        total_incoming_count if total_incoming_count is not None else len(context.incoming_edges)
    )
    outgoing_total = (
        total_outgoing_count if total_outgoing_count is not None else len(context.outgoing_edges)
    )
    sibling_total = (
        total_sibling_count if total_sibling_count is not None else len(context.file_siblings)
    )

    summary = {
        "has_structural_parent": context.structural_parent is not None,
        "structural_child_count": structural_child_total,
        "structural_children_truncated": structural_child_total > len(context.structural_children),
        "has_lexical_parent": context.lexical_parent is not None,
        "lexical_child_count": lexical_child_total,
        "incoming_edge_count": incoming_total,
        "outgoing_edge_count": outgoing_total,
        "same_file_sibling_count": sibling_total,
        "scope": focus.get("scope"),
        "is_local_declaration": _is_local_declaration(focus),
        "is_nested_declaration": _is_nested_declaration(focus),
    }
    if relationship_counts:
        summary["relationship_counts"] = relationship_counts
    return summary


def build_confidence(
    context: SymbolContext,
    *,
    raw_edges: Sequence[dict] | None = None,
) -> dict:
    """Build confidence metadata from focus symbol and edges.

    ``edge_confidence_min`` and ``edge_confidence_max`` are ``None`` when no edges exist.
    """
    focus = context.focus_symbol
    all_edges = (
        list(raw_edges)
        if raw_edges is not None
        else [*context.incoming_edges, *context.outgoing_edges]
    )
    edge_confidences = [
        float(e["confidence"]) for e in all_edges if e.get("confidence") is not None
    ]

    contains_placeholders = (
        edges_have_placeholder_targets(all_edges)
        if raw_edges is not None
        else has_placeholder_targets(context)
    )

    return {
        "focus_symbol_confidence": focus.get("confidence", 1.0),
        "edge_confidence_min": min(edge_confidences) if edge_confidences else None,
        "edge_confidence_max": max(edge_confidences) if edge_confidences else None,
        "contains_placeholder_targets": contains_placeholders,
        "graph_only": not is_lsp_enriched(context, raw_edges),
    }
