"""Public re-exports for ``context.utils``."""

from context.utils.formatting import (
    PLACEHOLDER_PREFIXES,
    deduplicate_imports,
    edge_sort_key,
    edges_have_placeholder_targets,
    format_import_label,
    is_lsp_enriched,
    is_nested_lexical_declaration,
    is_private_symbol,
    is_unresolved_edge,
    resolve_symbol_line_bounds,
    select_balanced_edges,
    slice_source_window,
    validate_window_bounds,
)
from context.utils.trigram import (
    compile_search_pattern,
    extract_required_ascii_trigrams,
    match_document_lines,
)

__all__ = [
    "PLACEHOLDER_PREFIXES",
    "compile_search_pattern",
    "deduplicate_imports",
    "edge_sort_key",
    "edges_have_placeholder_targets",
    "extract_required_ascii_trigrams",
    "format_import_label",
    "is_lsp_enriched",
    "is_nested_lexical_declaration",
    "is_private_symbol",
    "is_unresolved_edge",
    "match_document_lines",
    "resolve_symbol_line_bounds",
    "select_balanced_edges",
    "slice_source_window",
    "validate_window_bounds",
]
