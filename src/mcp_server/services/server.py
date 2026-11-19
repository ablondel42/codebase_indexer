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
        """Initialize the MCP server runner.

        Args:
            config: Optional base ``AppConfig`` (defaults to ``AppConfig()``).
            engine: Optional injected ``IndexerEngine`` reused across server creation.
        """
        self._config = config if config is not None else AppConfig()
        self._engine = engine

    def _acquire_engine(self, db_path: Path | str | None) -> IndexerEngine:
        """Acquire or scope an ``IndexerEngine`` targeting ``db_path``.

        Args:
            db_path: Optional override SQLite database path.

        Returns:
            Scoped ``IndexerEngine`` instance.
        """
        if self._engine is not None:
            return self._engine.for_db(db_path)
        scoped_cfg = (
            self._config.model_copy(update={"db_path": Path(db_path)})
            if db_path is not None
            else self._config
        )
        return IndexerEngine(scoped_cfg)

    def create_server(
        self,
        *,
        db_path: Path | str | None = None,
        debug: bool = False,
    ) -> MCPServer[Any]:
        """Construct an ``MCPServer`` with all 8 token-lean tools registered.

        Args:
            db_path: Optional SQLite database path bound to registered tools.
            debug: Whether to emit debug creation diagnostics.

        Returns:
            Configured ``MCPServer`` instance.
        """
        engine = self._acquire_engine(db_path)
        server = MCPServer(engine.config.app_name)
        McpToolRegistry(engine).bind(server, db_path=engine.db_path)
        if debug:
            logger.debug(
                "MCP server created with tools: %s",
                ", ".join(McpToolRegistry.TOOL_NAMES),
            )
        return server

    def run_stdio(
        self,
        *,
        db_path: Path | str | None = None,
        debug: bool = False,
    ) -> None:
        """Run the MCP server on ``stdio`` transport and close ``engine`` on exit.

        Args:
            db_path: Optional SQLite database path bound to registered tools.
            debug: Whether to configure ``DEBUG`` logging to ``stderr`` when unconfigured.
        """
        if debug and not is_logging_configured():
            configure_logging(level="DEBUG")

        engine = self._acquire_engine(db_path)
        server = MCPServer(engine.config.app_name)
        McpToolRegistry(engine).bind(server, db_path=engine.db_path)
        logger.info("Starting MCP server on stdio (db_path=%s)", engine.db_path)
        try:
            server.run(transport="stdio")
        finally:
            engine.close()


__all__ = [
    "McpServerRunner",
]
