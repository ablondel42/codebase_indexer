"""Async MCP handlers for symbol source, context, references, and change-risk analysis."""

import asyncio

from core.utils.logging import get_logger
from engine.models.queries import ContextQuery, SourceQuery, SymbolReferencesQuery
from engine.services.facade import IndexerEngine
from mcp_server.models.inputs import (
    AnalyzeRiskInput,
    GetSymbolContextInput,
    GetSymbolReferencesInput,
    GetSymbolSourceInput,
)
from mcp_server.utils.compact import (
    adapt_risk_assessment,
    adapt_symbol_context,
    adapt_symbol_references,
    adapt_symbol_source,
    serialize_mcp_json,
)
from mcp_server.utils.errors import success_result

logger = get_logger("mcp_server.services.symbol_handlers")


class SymbolMcpHandlers:
    """Async MCP handlers for symbol source, context, reference, and risk tools."""

    def __init__(self, engine: IndexerEngine) -> None:
        """Initialize symbol MCP handlers over an ``IndexerEngine``.

        Args:
            engine: Headless ``IndexerEngine`` application facade.
        """
        self._engine = engine

    def _run_get_symbol_source(self, inp: GetSymbolSourceInput) -> str:
        """Execute ``get_symbol_source`` synchronously on a worker thread.

        Args:
            inp: Validated ``GetSymbolSourceInput`` model.

        Returns:
            Serialized compact JSON success envelope.
        """
        scoped = self._engine.for_db(inp.db_path)
        snippet = scoped.queries.get_symbol_source(
            SourceQuery(
                symbol=inp.symbol_id_or_name,
                repo=inp.repo_id,
                max_lines=inp.max_lines,
                offset=inp.offset,
            )
        )
        return serialize_mcp_json(success_result(adapt_symbol_source(snippet)))

    def _run_get_symbol_context(self, inp: GetSymbolContextInput) -> str:
        """Execute ``get_symbol_context`` synchronously on a worker thread.

        Args:
            inp: Validated ``GetSymbolContextInput`` model.

        Returns:
            Serialized compact JSON success envelope.
        """
        scoped = self._engine.for_db(inp.db_path)
        ctx = scoped.queries.get_symbol_context(
            ContextQuery(
                symbol=inp.symbol_id_or_name,
                repo=inp.repo_id,
                include_source=inp.include_source,
                max_source_lines=inp.max_source_lines,
                edge_limit=inp.edge_limit,
                child_limit=inp.edge_limit,
                sibling_limit=inp.edge_limit,
            )
        )
        return serialize_mcp_json(success_result({"context": adapt_symbol_context(ctx)}))

    def _run_get_symbol_references(self, inp: GetSymbolReferencesInput) -> str:
        """Execute ``get_symbol_references`` synchronously on a worker thread.

        Args:
            inp: Validated ``GetSymbolReferencesInput`` model.

        Returns:
            Serialized compact JSON success envelope.
        """
        scoped = self._engine.for_db(inp.db_path)
        report = scoped.queries.get_symbol_references(
            SymbolReferencesQuery(
                symbol=inp.symbol_id_or_name,
                repo=inp.repo_id,
                direction=inp.direction,
                refresh_lsp=inp.refresh_lsp,
                limit=inp.limit,
                offset=inp.offset,
                include_previews=True,
            )
        )
        return serialize_mcp_json(success_result(adapt_symbol_references(report)))

    def _run_analyze_risk(self, inp: AnalyzeRiskInput) -> str:
        """Execute ``analyze_risk`` synchronously on a worker thread.

        Args:
            inp: Validated ``AnalyzeRiskInput`` model.

        Returns:
            Serialized compact JSON success envelope.
        """
        scoped = self._engine.for_db(inp.db_path)
        assessment = scoped.queries.analyze_risk(inp.symbols, repo=inp.repo_id)
        return serialize_mcp_json(success_result({"risk": adapt_risk_assessment(assessment)}))

    async def get_symbol_source(self, inp: GetSymbolSourceInput) -> str:
        """Return line-numbered source code for a symbol ID or qualified name.

        Args:
            inp: Validated ``GetSymbolSourceInput`` model.

        Returns:
            Serialized compact JSON MCP success envelope string.
        """
        logger.debug("Executing MCP get_symbol_source (symbol=%r)", inp.symbol_id_or_name)
        return await asyncio.to_thread(self._run_get_symbol_source, inp)

    async def get_symbol_context(self, inp: GetSymbolContextInput) -> str:
        """Return compact symbol hierarchy, relationships, freshness, and confidence.

        Args:
            inp: Validated ``GetSymbolContextInput`` model.

        Returns:
            Serialized compact JSON MCP success envelope string.
        """
        logger.debug("Executing MCP get_symbol_context (symbol=%r)", inp.symbol_id_or_name)
        return await asyncio.to_thread(self._run_get_symbol_context, inp)

    async def get_symbol_references(self, inp: GetSymbolReferencesInput) -> str:
        """Return paginated LSP references and AST dependency sites with 1-line previews.

        Args:
            inp: Validated ``GetSymbolReferencesInput`` model.

        Returns:
            Serialized compact JSON MCP success envelope string.
        """
        logger.debug("Executing MCP get_symbol_references (symbol=%r)", inp.symbol_id_or_name)
        return await asyncio.to_thread(self._run_get_symbol_references, inp)

    async def analyze_risk(self, inp: AnalyzeRiskInput) -> str:
        """Analyze deterministic change impact and risk for 1 to 50 target symbols.

        Args:
            inp: Validated ``AnalyzeRiskInput`` model.

        Returns:
            Serialized compact JSON MCP success envelope string.
        """
        logger.debug("Executing MCP analyze_risk (symbol_count=%d)", len(inp.symbols))
        return await asyncio.to_thread(self._run_analyze_risk, inp)


__all__ = [
    "SymbolMcpHandlers",
]
