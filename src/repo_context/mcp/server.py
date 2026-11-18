"""MCP server wiring and stdio lifecycle management."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from mcp.server import MCPServer

from repo_context.config import get_config
from repo_context.logging_config import get_logger
from repo_context.lsp import LspSessionPool
from repo_context.mcp.tools import register_tools

logger = get_logger("mcp.server")


def create_server(
    db_path: str | Path | None = None,
    debug: bool = False,
    *,
    lsp_pool: LspSessionPool | None = None,
) -> MCPServer[Any]:
    """Create and configure the token-lean MCP server."""
    cfg = get_config()
    mcp = MCPServer(cfg.app_name)
    pool = lsp_pool if lsp_pool is not None else LspSessionPool(config=cfg)

    register_tools(mcp, db_path=db_path, lsp_pool=pool)

    if debug:
        logger.debug(
            "MCP server created with tools: get_repo_overview, list_files, "
            "get_file_outline, search_symbols, get_symbol_source, "
            "get_symbol_context, get_symbol_references, analyze_risk"
        )

    return mcp


def run_server(db_path: str | Path | None = None, debug: bool = False) -> None:
    """Start the MCP server on stdio transport and close warm LSP sessions on exit."""
    cfg = get_config()
    pool = LspSessionPool(config=cfg)
    mcp = create_server(db_path=db_path, debug=debug, lsp_pool=pool)
    logger.info("Starting MCP server on stdio (db_path=%s)", db_path or cfg.db_path)
    try:
        mcp.run(transport="stdio")
    finally:
        pool.close_all()
