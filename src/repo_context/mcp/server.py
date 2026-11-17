"""MCP server wiring and stdio lifecycle management."""

from __future__ import annotations

from pathlib import Path
import sys
from typing import Any

from repo_context.config import get_config
from repo_context.lsp import LspSessionPool
from repo_context.mcp.tools import register_tools


def _load_mcp_server_class() -> type[Any]:
    """Load the installed MCP server class (mcp 2.x ``MCPServer`` or 1.x ``FastMCP``)."""
    try:
        from mcp.server import MCPServer

        return MCPServer
    except ImportError:
        pass
    try:
        from mcp.server.fastmcp import FastMCP

        return FastMCP
    except ImportError as exc:
        raise RuntimeError("The 'mcp' package is required to run the MCP server.") from exc


def create_server(
    db_path: str | Path | None = None,
    debug: bool = False,
    *,
    lsp_pool: LspSessionPool | None = None,
) -> Any:
    """Create and configure the token-lean MCP server (MCPServer / FastMCP)."""
    server_cls = _load_mcp_server_class()
    cfg = get_config()
    mcp = server_cls(cfg.app_name)
    pool = lsp_pool if lsp_pool is not None else LspSessionPool(config=cfg)

    register_tools(mcp, db_path=db_path, lsp_pool=pool)

    if debug:
        print(
            "MCP server created with tools: get_repo_overview, list_files, "
            "get_file_outline, search_symbols, get_symbol_source, "
            "get_symbol_context, get_symbol_references, analyze_risk",
            file=sys.stderr,
        )

    return mcp


def run_server(db_path: str | Path | None = None, debug: bool = False) -> None:
    """Start the MCP server on stdio transport and close warm LSP sessions on exit."""
    cfg = get_config()
    pool = LspSessionPool(config=cfg)
    mcp = create_server(db_path=db_path, debug=debug, lsp_pool=pool)
    print(
        f"Starting MCP server on stdio (db_path={db_path or cfg.db_path})",
        file=sys.stderr,
    )
    try:
        mcp.run(transport="stdio")
    finally:
        pool.close_all()
