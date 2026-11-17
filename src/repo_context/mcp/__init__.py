"""Token-lean MCP server package."""

from repo_context.mcp.server import create_server, run_server
from repo_context.mcp.tools import (
    analyze_risk,
    close_default_lsp_pool,
    get_file_outline,
    get_repo_overview,
    get_symbol_context,
    get_symbol_references,
    get_symbol_source,
    list_files,
    register_tools,
    search_symbols,
)

__all__ = [
    "create_server",
    "run_server",
    "register_tools",
    "close_default_lsp_pool",
    "get_repo_overview",
    "list_files",
    "get_file_outline",
    "search_symbols",
    "get_symbol_source",
    "get_symbol_context",
    "get_symbol_references",
    "analyze_risk",
]
