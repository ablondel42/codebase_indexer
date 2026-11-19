"""CLI command handlers for ``rc refs`` and ``rc risk``."""

import argparse

from cli.utils.output import (
    emit_json,
    format_references_for_cli,
    resolve_cli_db_path,
    serialize_risk_for_cli,
)
from core.models.exceptions import ValidationError
from core.utils.logging import get_logger
from engine.models.queries import SymbolReferencesQuery
from engine.services.facade import IndexerEngine
from graph.models.references import ReferenceDirection

logger = get_logger("cli.services.analysis")


class AnalysisCliCommands:
    """Executes symbol reference inspection and change-impact risk CLI commands."""

    def __init__(self, engine: IndexerEngine) -> None:
        """Initialize reference and risk CLI command handlers.

        Args:
            engine: Headless ``IndexerEngine`` application facade.
        """
        self._engine = engine

    def run_refs(self, args: argparse.Namespace) -> int:
        """Emit incoming/outgoing calls and references with optional Pyright LSP refresh.

        Args:
            args: Parsed ``rc refs`` CLI arguments.

        Returns:
            Process exit code ``0``.

        Raises:
            ValidationError: If ``--limit < 1``, ``--offset < 0``, or ``--direction`` is invalid.
        """
        limit = int(getattr(args, "limit", 50))
        offset = int(getattr(args, "offset", 0))
        if limit < 1:
            raise ValidationError("--limit must be >= 1", context={"limit": limit})
        if offset < 0:
            raise ValidationError("--offset must be >= 0", context={"offset": offset})

        raw_dir = str(getattr(args, "direction", "incoming"))
        try:
            direction = ReferenceDirection(raw_dir)
        except ValueError as exc:
            raise ValidationError(
                f"Invalid direction: {raw_dir!r}",
                context={"direction": raw_dir},
            ) from exc

        db_path = resolve_cli_db_path(args, self._engine.db_path)
        refresh = bool(getattr(args, "refresh", False))
        id_or_name: str = args.id_or_name
        logger.debug(
            "Running rc refs %r (db_path=%s, direction=%s, refresh=%s)",
            id_or_name,
            db_path,
            direction,
            refresh,
        )

        report = self._engine.for_db(db_path).queries.get_symbol_references(
            SymbolReferencesQuery(
                symbol=id_or_name,
                repo=getattr(args, "repo", None),
                direction=direction,
                refresh_lsp=refresh,
                limit=limit,
                offset=offset,
                include_previews=True,
            )
        )

        emit_json(format_references_for_cli(report))
        return 0

    def run_risk(self, args: argparse.Namespace) -> int:
        """Analyze deterministic change impact and risk for one or more symbols.

        Args:
            args: Parsed ``rc risk`` CLI arguments.

        Returns:
            Process exit code ``0``.
        """
        db_path = resolve_cli_db_path(args, self._engine.db_path)
        repo_arg: str | None = getattr(args, "repo", None)
        symbol_refs: list[str] = list(args.id_or_names)
        logger.debug("Running rc risk for %d target(s) (db_path=%s)", len(symbol_refs), db_path)

        assessment = self._engine.for_db(db_path).queries.analyze_risk(
            symbol_refs,
            repo=repo_arg,
        )
        emit_json(serialize_risk_for_cli(assessment))
        return 0


__all__ = [
    "AnalysisCliCommands",
]
