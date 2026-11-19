"""Public re-exports for ``context.utils``."""

from context.utils.formatting import (
    PLACEHOLDER_PREFIXES,
    deduplicate_imports,
    edge_sort_key,
    edges_have_placeholder_targets,
    format_import_label,
    is_lsp_enriched,
    is_private_symbol,
    resolve_symbol_line_bounds,
    select_balanced_edges,
    slice_source_window,
)

__all__ = [
    "PLACEHOLDER_PREFIXES",
    "deduplicate_imports",
    "edge_sort_key",
    "edges_have_placeholder_targets",
    "format_import_label",
    "is_lsp_enriched",
    "is_private_symbol",
    "resolve_symbol_line_bounds",
    "select_balanced_edges",
    "slice_source_window",
]
