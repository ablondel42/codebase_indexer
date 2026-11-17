"""Shared helper functions for the context layer."""

from collections.abc import Sequence

from repo_context.constants import EDGE_KIND_REFERENCES
from repo_context.models import SymbolContext

PLACEHOLDER_PREFIXES = (
    "external_or_unresolved:",
    "unresolved_base:",
    "unresolved_call:",
)


def edges_have_placeholder_targets(edges: Sequence[dict]) -> bool:
    """Return True if any edge endpoint starts with a known unresolved prefix."""
    for edge in edges:
        from_id = edge.get("from_id", "") or ""
        to_id = edge.get("to_id", "") or ""
        for prefix in PLACEHOLDER_PREFIXES:
            if from_id.startswith(prefix) or to_id.startswith(prefix):
                return True
    return False


def has_placeholder_targets(context: SymbolContext) -> bool:
    """Check if any context edge has a placeholder endpoint."""
    return edges_have_placeholder_targets([*context.incoming_edges, *context.outgoing_edges])


def is_lsp_enriched(
    context: SymbolContext,
    raw_edges: Sequence[dict] | None = None,
) -> bool:
    """Determine whether context includes LSP reference enrichment."""
    ref_summary = context.reference_summary or {}
    if ref_summary.get("available") is True or ref_summary.get("reference_source") == "lsp":
        return True

    edges_to_check = (
        raw_edges
        if raw_edges is not None
        else [*context.incoming_edges, *context.outgoing_edges]
    )
    for edge in edges_to_check:
        if edge.get("kind") == EDGE_KIND_REFERENCES or edge.get("source") == "lsp":
            return True
    return False


def format_line_span(node: dict) -> str | None:
    """Format a 1-based ``'start-end'`` line span from node metadata."""
    start_line = node.get("start_line")
    end_line = node.get("end_line")
    if isinstance(start_line, int) and isinstance(end_line, int):
        return f"{start_line}-{end_line}"

    rng = node.get("range_json")
    if isinstance(rng, dict):
        start = rng.get("start")
        end = rng.get("end")
        if isinstance(start, dict) and isinstance(end, dict):
            s_line = start.get("line")
            e_line = end.get("line")
            if isinstance(s_line, int) and isinstance(e_line, int):
                return f"{s_line + 1}-{e_line + 1}"
    return None
