"""Context layer for symbol-centered views, file outlines, and source slicing."""

from repo_context.context.builders import (
    adapt_edge_to_context_edge,
    adapt_node_to_symbol,
    build_symbol_context,
)
from repo_context.context.freshness import build_freshness
from repo_context.context.outline import build_file_outline
from repo_context.context.source_reader import read_file_snippet, read_symbol_source
from repo_context.context.summaries import build_confidence, build_structural_summary
from repo_context.models import SymbolContext

__all__ = [
    "SymbolContext",
    "adapt_node_to_symbol",
    "adapt_edge_to_context_edge",
    "build_symbol_context",
    "build_file_outline",
    "read_symbol_source",
    "read_file_snippet",
    "build_structural_summary",
    "build_freshness",
    "build_confidence",
]
