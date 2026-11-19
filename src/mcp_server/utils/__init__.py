"""Stateless wire compaction and structured error envelope helpers for MCP tools."""

from mcp_server.utils.compact import (
    adapt_file_list,
    adapt_outline,
    adapt_repo_overview,
    adapt_risk_assessment,
    adapt_symbol_context,
    adapt_symbol_references,
    adapt_symbol_search,
    adapt_symbol_source,
    adapt_symbol_summary,
    omit_none,
    serialize_mcp_json,
)
from mcp_server.utils.errors import (
    error_result,
    map_exception_to_error_result,
    success_result,
)

__all__ = [
    "adapt_file_list",
    "adapt_outline",
    "adapt_repo_overview",
    "adapt_risk_assessment",
    "adapt_symbol_context",
    "adapt_symbol_references",
    "adapt_symbol_search",
    "adapt_symbol_source",
    "adapt_symbol_summary",
    "error_result",
    "map_exception_to_error_result",
    "omit_none",
    "serialize_mcp_json",
    "success_result",
]
