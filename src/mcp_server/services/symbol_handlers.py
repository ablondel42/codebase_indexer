"""Synchronous MCP handlers for symbol source, context, references, and risk analysis."""

from typing import Any

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
)

logger = get_logger("mcp_server.services.symbol_handlers")


class SymbolMcpHandlers:
    """MCP handlers for symbol source, context, reference, and risk tools."""

    def __init__(self, engine: IndexerEngine) -> None:
        """Initialize symbol MCP handlers over an ``IndexerEngine``."""
        self._engine = engine

    def get_symbol_source(self, inp: GetSymbolSourceInput) -> dict[str, Any]:
        """Return line-numbered source code for a symbol ID or qualified name."""
        logger.debug("Executing MCP get_symbol_source (symbol=%r)", inp.symbol_id_or_name)
        snippet = self._engine.for_db(inp.db_path).queries.get_symbol_source(
            SourceQuery(
                symbol=inp.symbol_id_or_name,
                repo=inp.repo_id,
                max_lines=inp.max_lines,
                offset=inp.offset,
            )
        )
        return adapt_symbol_source(snippet)

    def get_symbol_context(self, inp: GetSymbolContextInput) -> dict[str, Any]:
        """Return compact symbol hierarchy, relationships, freshness, and confidence."""
        logger.debug("Executing MCP get_symbol_context (symbol=%r)", inp.symbol_id_or_name)
        ctx = self._engine.for_db(inp.db_path).queries.get_symbol_context(
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
        return {"context": adapt_symbol_context(ctx)}

    def get_symbol_references(self, inp: GetSymbolReferencesInput) -> dict[str, Any]:
        """Return paginated LSP references and AST dependency sites with 1-line previews."""
        logger.debug("Executing MCP get_symbol_references (symbol=%r)", inp.symbol_id_or_name)
        report = self._engine.for_db(inp.db_path).queries.get_symbol_references(
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
        return adapt_symbol_references(report)

    def analyze_risk(self, inp: AnalyzeRiskInput) -> dict[str, Any]:
        """Analyze deterministic change impact and risk for 1 to 50 target symbols."""
        logger.debug("Executing MCP analyze_risk (symbol_count=%d)", len(inp.symbols))
        assessment = self._engine.for_db(inp.db_path).queries.analyze_risk(
            inp.symbols,
            repo=inp.repo_id,
        )
        return {"risk": adapt_risk_assessment(assessment)}


__all__ = [
    "SymbolMcpHandlers",
]
