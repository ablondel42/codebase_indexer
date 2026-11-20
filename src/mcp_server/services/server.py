"""MCP server factory and stdio transport lifecycle runner."""

from pathlib import Path
from typing import Any

from mcp.server import MCPServer

from core.models.config import AppConfig
from core.utils.logging import configure_logging, get_logger, is_logging_configured
from engine.services.facade import IndexerEngine
from mcp_server.services.registry import McpToolRegistry

logger = get_logger("mcp_server.services.server")


class McpServerRunner:
    """Creates configured ``MCPServer`` instances and runs the stdio transport loop."""

    def __init__(
        self,
        config: AppConfig | None = None,
        *,
        engine: IndexerEngine | None = None,
    ) -> None:
        """Initialize the MCP server runner."""
        self._config = config if config is not None else AppConfig()
        self._engine = engine

    def _acquire_engine(self, db_path: Path | str | None) -> tuple[IndexerEngine, bool]:
        """Acquire or scope an ``IndexerEngine`` and report whether this call owns closing it."""
        if self._engine is not None:
            return self._engine.for_db(db_path), False
        scoped_cfg = (
            self._config.model_copy(update={"db_path": Path(db_path)})
            if db_path is not None
            else self._config
        )
        return IndexerEngine(scoped_cfg), True

    def _build_server_with_engine(
        self,
        db_path: Path | str | None,
        *,
        debug: bool,
    ) -> tuple[MCPServer[Any], IndexerEngine, bool]:
        """Construct an ``MCPServer`` bound to a scoped ``IndexerEngine``."""
        engine, owns_engine = self._acquire_engine(db_path)
        server = MCPServer(engine.config.app_name)
        McpToolRegistry(engine).bind(server, db_path=engine.db_path)
        if debug:
            logger.debug(
                "MCP server created with tools: %s",
                ", ".join(McpToolRegistry.TOOL_NAMES),
            )
        return server, engine, owns_engine

    def create_server(
        self,
        *,
        db_path: Path | str | None = None,
        debug: bool = False,
    ) -> MCPServer[Any]:
        """Construct an ``MCPServer`` with all 9 token-lean tools registered."""
        server, _, _ = self._build_server_with_engine(db_path, debug=debug)
        return server

    def run_stdio(
        self,
        *,
        db_path: Path | str | None = None,
        debug: bool = False,
    ) -> None:
        """Run the MCP server on ``stdio`` transport and close owned ``engine`` resources."""
        if debug and not is_logging_configured():
            configure_logging(level="DEBUG")

        server, engine, owns_engine = self._build_server_with_engine(db_path, debug=debug)
        logger.info("Starting MCP server on stdio (db_path=%s)", engine.db_path)
        try:
            server.run(transport="stdio")
        finally:
            if owns_engine:
                engine.close()


__all__ = [
    "McpServerRunner",
]
