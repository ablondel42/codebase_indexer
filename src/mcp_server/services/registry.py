"""MCP tool registration and in-process tool invocation registry service."""

import asyncio
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any

from mcp.server import MCPServer
from pydantic import ValidationError as PydanticValidationError

from core.models.exceptions import IndexerError, ValidationError
from core.utils.logging import get_logger
from engine.services.facade import IndexerEngine
from mcp_server.models.inputs import (
    AnalyzeRiskInput,
    BaseMcpToolInput,
    GetFileOutlineInput,
    GetRepoOverviewInput,
    GetSymbolContextInput,
    GetSymbolReferencesInput,
    GetSymbolSourceInput,
    ListFilesInput,
    SearchCodeInput,
    SearchSymbolsInput,
)
from mcp_server.services.repo_handlers import RepoMcpHandlers
from mcp_server.services.symbol_handlers import SymbolMcpHandlers
from mcp_server.utils.compact import serialize_mcp_json
from mcp_server.utils.errors import map_exception_to_error_result, success_result
from mcp_server.utils.signatures import build_mcp_tool_signature

logger = get_logger("mcp_server.services.registry")

_ToolSpec = tuple[str, str, type[BaseMcpToolInput], Callable[[Any], dict[str, Any]]]


class McpToolRegistry:
    """Registers the 9 token-lean MCP tools on an ``MCPServer`` and dispatches local calls."""

    TOOL_NAMES: tuple[str, ...] = (
        "get_repo_overview",
        "list_files",
        "get_file_outline",
        "search_symbols",
        "search_code",
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
        """Initialize the MCP tool registry over an ``IndexerEngine``."""
        self._engine = engine
        self._repo_handlers = (
            repo_handlers if repo_handlers is not None else RepoMcpHandlers(engine)
        )
        self._symbol_handlers = (
            symbol_handlers if symbol_handlers is not None else SymbolMcpHandlers(engine)
        )
        self._specs: dict[str, _ToolSpec] = {spec[0]: spec for spec in self._build_specs()}

    def _build_specs(self) -> tuple[_ToolSpec, ...]:
        """Return the declarative specification table for all 9 MCP tools."""
        rh = self._repo_handlers
        sh = self._symbol_handlers
        return (
            (
                "get_repo_overview",
                "Get repository overview, symbol/edge statistics, and top directories.",
                GetRepoOverviewInput,
                rh.get_repo_overview,
            ),
            (
                "list_files",
                "List indexed Python files in the repository with pagination and prefix filter.",
                ListFilesInput,
                rh.list_files,
            ),
            (
                "get_file_outline",
                "Get a hierarchical symbol skeleton and imports for a repository file.",
                GetFileOutlineInput,
                rh.get_file_outline,
            ),
            (
                "search_symbols",
                "Search symbols by name, signature, or docstring using FTS5 ranking.",
                SearchSymbolsInput,
                rh.search_symbols,
            ),
            (
                "search_code",
                "Search all UTF-8 repository text files (literal or regex) with symbol spans.",
                SearchCodeInput,
                rh.search_code,
            ),
            (
                "get_symbol_source",
                "Read bounded, line-numbered source code for a symbol ID or qualified name.",
                GetSymbolSourceInput,
                sh.get_symbol_source,
            ),
            (
                "get_symbol_context",
                "Get bounded symbol context: hierarchy, relationships, and freshness.",
                GetSymbolContextInput,
                sh.get_symbol_context,
            ),
            (
                "get_symbol_references",
                "Get paginated LSP references and AST dependencies with 1-line source previews.",
                GetSymbolReferencesInput,
                sh.get_symbol_references,
            ),
            (
                "analyze_risk",
                "Analyze change risk, blast radius, and staleness for 1 to 50 target symbols.",
                AnalyzeRiskInput,
                sh.analyze_risk,
            ),
        )

    def _bind_single_tool(
        self,
        server: MCPServer[Any] | Any,
        spec: _ToolSpec,
        target_db: str,
    ) -> None:
        """Bind a single declarative ``_ToolSpec`` onto ``server``."""
        tool_name, description, input_cls, _ = spec
        signature, annotations = build_mcp_tool_signature(input_cls)

        async def _tool_entry(*args: Any, **kwargs: Any) -> str:
            bound = signature.bind(*args, **kwargs)
            payload = dict(bound.arguments)
            payload["db_path"] = target_db
            return await self.call_tool(tool_name, payload)

        fn_meta: Any = _tool_entry
        fn_meta.__name__ = tool_name
        fn_meta.__doc__ = description
        fn_meta.__signature__ = signature
        fn_meta.__annotations__ = annotations
        server.tool(name=tool_name)(_tool_entry)

    def bind(self, server: MCPServer[Any] | Any, *, db_path: Path | str | None = None) -> None:
        """Register all 9 token-lean MCP tools onto ``server``."""
        target_db = str(db_path) if db_path is not None else str(self._engine.db_path)
        for name in self.TOOL_NAMES:
            self._bind_single_tool(server, self._specs[name], target_db)

    async def call_tool(
        self,
        tool_name: str,
        arguments: Mapping[str, Any] | None = None,
    ) -> str:
        """Validate ``arguments`` and execute ``tool_name`` in-process."""
        spec = self._specs.get(tool_name)
        if spec is None:
            available = ", ".join(self.TOOL_NAMES)
            raise ValidationError(
                f"Unknown MCP tool {tool_name!r}. Available tools: {available}",
                context={"tool": tool_name},
            )

        merged: dict[str, Any] = dict(arguments) if arguments is not None else {}
        if merged.get("db_path") is None:
            merged["db_path"] = str(self._engine.db_path)

        _, _, input_cls, handler = spec
        try:
            validated = input_cls.model_validate(merged)
            data = await asyncio.to_thread(handler, validated)
            return serialize_mcp_json(success_result(data))
        except (PydanticValidationError, IndexerError) as exc:
            logger.debug(
                "MCP call_tool %s failed validation or domain check (%s): %s",
                tool_name,
                type(exc).__name__,
                exc,
            )
            return serialize_mcp_json(map_exception_to_error_result(exc, operation=tool_name))
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
