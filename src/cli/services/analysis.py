"""CLI command handlers for ``cbi refs`` and ``cbi risk``."""

import argparse

from cli.utils.output import (
    emit_json,
    format_references_for_cli,
    resolve_scoped_engine,
    serialize_risk_for_cli,
)
from core.utils.logging import get_logger
from engine.models.queries import SymbolReferencesQuery
from engine.services.facade import IndexerEngine
from graph.models.references import ReferenceDirection

logger = get_logger("cli.services.analysis")


class AnalysisCliCommands:
    """Executes symbol reference inspection and change-impact risk CLI commands."""

    def __init__(self, engine: IndexerEngine) -> None:
        """Initialize reference and risk CLI command handlers."""
        self._engine = engine

    def run_refs(self, args: argparse.Namespace) -> int:
        """Emit incoming/outgoing calls and references with optional Pyright LSP refresh."""
        scoped = resolve_scoped_engine(self._engine, args)
        direction = ReferenceDirection(str(getattr(args, "direction", "incoming")))
        refresh = bool(getattr(args, "refresh", False))
        id_or_name: str = args.id_or_name
        logger.debug(
            "Running cbi refs %r (db_path=%s, direction=%s, refresh=%s)",
            id_or_name,
            scoped.db_path,
            direction,
            refresh,
        )
        report = scoped.queries.get_symbol_references(
            SymbolReferencesQuery(
                symbol=id_or_name,
                repo=getattr(args, "repo", None),
                direction=direction,
                refresh_lsp=refresh,
                limit=int(getattr(args, "limit", 50)),
                offset=int(getattr(args, "offset", 0)),
                include_previews=True,
            )
        )
        emit_json(format_references_for_cli(report))
        return 0

    def run_risk(self, args: argparse.Namespace) -> int:
        """Analyze deterministic change impact and risk for one or more symbols."""
        scoped = resolve_scoped_engine(self._engine, args)
        symbol_refs: list[str] = list(args.id_or_names)
        logger.debug(
            "Running cbi risk for %d target(s) (db_path=%s)", len(symbol_refs), scoped.db_path
        )
        assessment = scoped.queries.analyze_risk(symbol_refs, repo=getattr(args, "repo", None))
        emit_json(serialize_risk_for_cli(assessment))
        return 0


__all__ = [
    "AnalysisCliCommands",
]
