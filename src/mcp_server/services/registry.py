"""MCP tool registration and in-process tool invocation registry service."""

from collections.abc import Coroutine, Mapping
from pathlib import Path
from typing import Any

from mcp.server import MCPServer
from pydantic import ValidationError as PydanticValidationError

from core.models.enums import SymbolKind
from core.models.exceptions import IndexerError, ValidationError
from core.utils.logging import get_logger
from engine.services.facade import IndexerEngine
from graph.models.references import ReferenceDirection
from mcp_server.models.inputs import (
    AnalyzeRiskInput,
    GetFileOutlineInput,
    GetRepoOverviewInput,
    GetSymbolContextInput,
    GetSymbolReferencesInput,
    GetSymbolSourceInput,
    ListFilesInput,
    SearchSymbolsInput,
)
from mcp_server.services.repo_handlers import RepoMcpHandlers
from mcp_server.services.symbol_handlers import SymbolMcpHandlers
from mcp_server.utils.compact import serialize_mcp_json
from mcp_server.utils.errors import map_exception_to_error_result

logger = get_logger("mcp_server.services.registry")


class McpToolRegistry:
    """Registers the 8 token-lean MCP tools on an ``MCPServer`` and dispatches local calls."""

    TOOL_NAMES: tuple[str, ...] = (
        "get_repo_overview",
        "list_files",
        "get_file_outline",
        "search_symbols",
        "get_symbol_source",
        "get_symbol_context",
        "get_symbol_references",
        "analyze_risk",
    )

    def __init__(
        self,
        engine: IndexerEngine,
        *,
        repo_handlers: RepoMcpHandlers | None = None,
        symbol_handlers: SymbolMcpHandlers | None = None,
    ) -> None:
        """Initialize the MCP tool registry over an ``IndexerEngine``.

        Args:
            engine: Headless ``IndexerEngine`` application facade.
            repo_handlers: Optional ``RepoMcpHandlers`` override.
            symbol_handlers: Optional ``SymbolMcpHandlers`` override.
        """
        self._engine = engine
        self._repo_handlers = (
            repo_handlers if repo_handlers is not None else RepoMcpHandlers(engine)
        )
        self._symbol_handlers = (
            symbol_handlers if symbol_handlers is not None else SymbolMcpHandlers(engine)
        )

    def _register_repo_tools(self, server: MCPServer[Any] | Any, target_db: str) -> None:
        """Bind the 4 repository and file MCP tools onto ``server``.

        Args:
            server: Target ``MCPServer`` (or compatible ``.tool()`` stub).
            target_db: Default database path string bound to registered tools.
        """

        @server.tool(name="get_repo_overview")
        async def get_repo_overview(repo_id: str | None = None) -> str:
            """Get repository overview, symbol/edge statistics, and top directories."""
            return await self.call_tool(
                "get_repo_overview",
                {"repo_id": repo_id, "db_path": target_db},
            )

        @server.tool(name="list_files")
        async def list_files(
            repo_id: str | None = None,
            path_prefix: str | None = None,
            include_tests: bool = False,
            limit: int = 100,
            offset: int = 0,
        ) -> str:
            """List indexed Python files in the repository with pagination and prefix filter."""
            return await self.call_tool(
                "list_files",
                {
                    "repo_id": repo_id,
                    "path_prefix": path_prefix,
                    "include_tests": include_tests,
                    "limit": limit,
                    "offset": offset,
                    "db_path": target_db,
                },
            )

        @server.tool(name="get_file_outline")
        async def get_file_outline(
            file_path: str,
            repo_id: str | None = None,
            include_private: bool = True,
            max_depth: int = 3,
        ) -> str:
            """Get a hierarchical symbol skeleton and imports for a repository file."""
            return await self.call_tool(
                "get_file_outline",
                {
                    "file_path": file_path,
                    "repo_id": repo_id,
                    "include_private": include_private,
                    "max_depth": max_depth,
                    "db_path": target_db,
                },
            )

        @server.tool(name="search_symbols")
        async def search_symbols(
            query: str,
            repo_id: str | None = None,
            kind: SymbolKind | None = None,
            file_path: str | None = None,
            limit: int = 20,
        ) -> str:
            """Search symbols by name, signature, or docstring using FTS5 ranking."""
            return await self.call_tool(
                "search_symbols",
                {
                    "query": query,
                    "repo_id": repo_id,
                    "kind": kind,
                    "file_path": file_path,
                    "limit": limit,
                    "db_path": target_db,
                },
            )

    def _register_symbol_tools(self, server: MCPServer[Any] | Any, target_db: str) -> None:
        """Bind the 4 symbol source, context, reference, and risk MCP tools onto ``server``.

        Args:
            server: Target ``MCPServer`` (or compatible ``.tool()`` stub).
            target_db: Default database path string bound to registered tools.
        """

        @server.tool(name="get_symbol_source")
        async def get_symbol_source(
            symbol: str,
            repo_id: str | None = None,
            max_lines: int = 120,
            offset: int = 0,
        ) -> str:
            """Read bounded, line-numbered source code for a symbol ID or qualified name."""
            return await self.call_tool(
                "get_symbol_source",
                {
                    "symbol": symbol,
                    "repo_id": repo_id,
                    "max_lines": max_lines,
                    "offset": offset,
                    "db_path": target_db,
                },
            )

        @server.tool(name="get_symbol_context")
        async def get_symbol_context(
            symbol: str,
            repo_id: str | None = None,
            edge_limit: int = 20,
            include_source: bool = False,
            max_source_lines: int = 80,
        ) -> str:
            """Get bounded symbol context: hierarchy, relationships, and freshness."""
            return await self.call_tool(
                "get_symbol_context",
                {
                    "symbol": symbol,
                    "repo_id": repo_id,
                    "edge_limit": edge_limit,
                    "include_source": include_source,
                    "max_source_lines": max_source_lines,
                    "db_path": target_db,
                },
            )

        @server.tool(name="get_symbol_references")
        async def get_symbol_references(
            symbol: str,
            repo_id: str | None = None,
            direction: ReferenceDirection = ReferenceDirection.INCOMING,
            refresh_lsp: bool = False,
            limit: int = 30,
            offset: int = 0,
        ) -> str:
            """Get paginated LSP references and AST dependencies with 1-line source previews."""
            return await self.call_tool(
                "get_symbol_references",
                {
                    "symbol": symbol,
                    "repo_id": repo_id,
                    "direction": direction,
                    "refresh_lsp": refresh_lsp,
                    "limit": limit,
                    "offset": offset,
                    "db_path": target_db,
                },
            )

        @server.tool(name="analyze_risk")
        async def analyze_risk(
            symbols: list[str],
            repo_id: str | None = None,
        ) -> str:
            """Analyze change risk, blast radius, and staleness for 1 to 50 target symbols."""
            return await self.call_tool(
                "analyze_risk",
                {"symbols": symbols, "repo_id": repo_id, "db_path": target_db},
            )

    def bind(self, server: MCPServer[Any] | Any, *, db_path: Path | str | None = None) -> None:
        """Register all 8 token-lean MCP tools onto ``server``.

        Args:
            server: ``MCPServer`` instance (or compatible ``.tool()`` decorator target).
            db_path: Optional override SQLite database path bound to the tools.
        """
        target_db = str(db_path) if db_path is not None else str(self._engine.db_path)
        self._register_repo_tools(server, target_db)
        self._register_symbol_tools(server, target_db)

    def _dispatch_repo_tool(
        self,
        tool_name: str,
        payload: Mapping[str, Any],
    ) -> Coroutine[Any, Any, str] | None:
        """Validate ``payload`` and return the matching repo-tool coroutine if matched.

        Args:
            tool_name: Validated MCP tool name.
            payload: Raw argument mapping including ``db_path``.

        Returns:
            Handler coroutine when ``tool_name`` is a repository tool, else ``None``.
        """
        if tool_name == "get_repo_overview":
            return self._repo_handlers.get_repo_overview(
                GetRepoOverviewInput.model_validate(payload)
            )
        if tool_name == "list_files":
            return self._repo_handlers.list_files(ListFilesInput.model_validate(payload))
        if tool_name == "get_file_outline":
            return self._repo_handlers.get_file_outline(
                GetFileOutlineInput.model_validate(payload)
            )
        if tool_name == "search_symbols":
            return self._repo_handlers.search_symbols(SearchSymbolsInput.model_validate(payload))
        return None

    def _dispatch_symbol_tool(
        self,
        tool_name: str,
        payload: Mapping[str, Any],
    ) -> Coroutine[Any, Any, str]:
        """Validate ``payload`` and return the matching symbol-tool coroutine.

        Args:
            tool_name: Validated symbol MCP tool name.
            payload: Raw argument mapping including ``db_path``.

        Returns:
            Handler coroutine for ``tool_name``.
        """
        if tool_name == "get_symbol_source":
            return self._symbol_handlers.get_symbol_source(
                GetSymbolSourceInput.model_validate(payload)
            )
        if tool_name == "get_symbol_context":
            return self._symbol_handlers.get_symbol_context(
                GetSymbolContextInput.model_validate(payload)
            )
        if tool_name == "get_symbol_references":
            return self._symbol_handlers.get_symbol_references(
                GetSymbolReferencesInput.model_validate(payload)
            )
        return self._symbol_handlers.analyze_risk(AnalyzeRiskInput.model_validate(payload))

    async def call_tool(
        self,
        tool_name: str,
        arguments: Mapping[str, Any] | None = None,
    ) -> str:
        """Validate ``arguments`` and execute ``tool_name`` in-process.

        Args:
            tool_name: Registered MCP tool name (one of ``TOOL_NAMES``).
            arguments: Optional raw keyword argument mapping.

        Returns:
            Compact JSON string containing the MCP success or error envelope.

        Raises:
            ValidationError: If ``tool_name`` is not one of the 8 registered tools.
        """
        if tool_name not in self.TOOL_NAMES:
            available = ", ".join(self.TOOL_NAMES)
            raise ValidationError(
                f"Unknown MCP tool {tool_name!r}. Available tools: {available}",
                context={"tool": tool_name},
            )

        merged: dict[str, Any] = dict(arguments) if arguments is not None else {}
        if merged.get("db_path") is None:
            merged["db_path"] = str(self._engine.db_path)

        try:
            repo_coro = self._dispatch_repo_tool(tool_name, merged)
            if repo_coro is not None:
                return await repo_coro
            return await self._dispatch_symbol_tool(tool_name, merged)
        except (PydanticValidationError, IndexerError) as exc:
            logger.debug(
                "MCP call_tool %s failed validation or domain check (%s): %s",
                tool_name,
                type(exc).__name__,
                exc,
            )
            return serialize_mcp_json(
                map_exception_to_error_result(exc, operation=tool_name)
            )
        except Exception as exc:
            logger.exception("Unhandled error in MCP tool %s", tool_name)
            return serialize_mcp_json(
                map_exception_to_error_result(
                    exc,
                    operation=tool_name,
                    log_unexpected=False,
                )
            )


__all__ = [
    "McpToolRegistry",
]
