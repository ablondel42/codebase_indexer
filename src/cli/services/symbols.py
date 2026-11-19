"""CLI command handlers for ``rc files``, ``rc outline``, ``rc symbols``, and ``rc source``."""

import argparse

from cli.utils.output import (
    emit_json,
    format_files_for_cli,
    format_outline_for_cli,
    format_source_for_cli,
    format_symbol_context_for_cli,
    format_symbol_list_for_cli,
    format_symbol_search_for_cli,
    resolve_cli_db_path,
)
from core.models.enums import SymbolKind
from core.models.exceptions import ValidationError
from core.utils.logging import get_logger
from engine.models.queries import (
    ContextQuery,
    ListFilesQuery,
    ListSymbolsQuery,
    OutlineQuery,
    SearchSymbolsQuery,
    SourceQuery,
)
from engine.services.facade import IndexerEngine

logger = get_logger("cli.services.symbols")


class SymbolCliCommands:
    """Executes file listing, outline, symbol search/list/show, and source CLI commands."""

    def __init__(self, engine: IndexerEngine) -> None:
        """Initialize file and symbol CLI command handlers.

        Args:
            engine: Headless ``IndexerEngine`` application facade.
        """
        self._engine = engine

    @staticmethod
    def _parse_optional_kind(raw_kind: str | None) -> SymbolKind | None:
        """Coerce an optional CLI ``--kind`` string into a ``SymbolKind`` enum.

        Args:
            raw_kind: Optional ``--kind`` flag string or ``None``.

        Returns:
            Validated ``SymbolKind`` member, or ``None``.

        Raises:
            ValidationError: If ``raw_kind`` is not a valid ``SymbolKind``.
        """
        if raw_kind is None:
            return None
        try:
            return SymbolKind(raw_kind)
        except ValueError as exc:
            raise ValidationError(
                f"Invalid symbol kind: {raw_kind!r}",
                context={"kind": raw_kind},
            ) from exc

    def run_files(self, args: argparse.Namespace) -> int:
        """List indexed files with line count, symbol count, and live disk freshness.

        Args:
            args: Parsed ``rc files`` CLI arguments.

        Returns:
            Process exit code ``0``.

        Raises:
            ValidationError: If ``--limit < 1``.
        """
        db_path = resolve_cli_db_path(args, self._engine.db_path)
        limit = int(getattr(args, "limit", 50))
        if limit < 1:
            raise ValidationError("--limit must be >= 1", context={"limit": limit})

        logger.debug("Running rc files (db_path=%s, limit=%d)", db_path, limit)
        page = self._engine.for_db(db_path).queries.list_files(
            ListFilesQuery(
                repo=getattr(args, "repo", None),
                pattern=getattr(args, "pattern", None),
                include_tests=bool(getattr(args, "include_tests", False)),
                check_freshness=True,
                limit=limit,
                offset=0,
            )
        )
        emit_json(format_files_for_cli(page))
        return 0

    def run_outline(self, args: argparse.Namespace) -> int:
        """Emit hierarchical symbol outline and imports for an indexed file.

        Args:
            args: Parsed ``rc outline`` CLI arguments.

        Returns:
            Process exit code ``0``.

        Raises:
            ValidationError: If ``--max-depth < 0``.
        """
        db_path = resolve_cli_db_path(args, self._engine.db_path)
        max_depth = int(getattr(args, "max_depth", 3))
        if max_depth < 0:
            raise ValidationError("--max-depth must be >= 0", context={"max_depth": max_depth})

        include_private = not bool(getattr(args, "no_private", False))
        file_path_arg: str = args.file_path
        logger.debug("Running rc outline for %s (db_path=%s)", file_path_arg, db_path)
        outline = self._engine.for_db(db_path).queries.get_file_outline(
            OutlineQuery(
                file_path=file_path_arg,
                repo=getattr(args, "repo", None),
                include_private=include_private,
                max_depth=max_depth,
            )
        )
        emit_json(format_outline_for_cli(outline))
        return 0

    def run_search(self, args: argparse.Namespace) -> int:
        """Search symbols via FTS5 with exact/prefix ranking and ``LIKE`` fallback.

        Args:
            args: Parsed ``rc symbols search`` CLI arguments.

        Returns:
            Process exit code ``0``.

        Raises:
            ValidationError: If ``--limit < 1`` or ``--kind`` is invalid.
        """
        db_path = resolve_cli_db_path(args, self._engine.db_path)
        limit = int(getattr(args, "limit", 20))
        if limit < 1:
            raise ValidationError("--limit must be >= 1", context={"limit": limit})

        kind = self._parse_optional_kind(getattr(args, "kind", None))
        logger.debug("Running rc symbols search %r (db_path=%s)", args.query, db_path)
        page = self._engine.for_db(db_path).queries.search_symbols(
            SearchSymbolsQuery(
                query=args.query,
                repo=getattr(args, "repo", None),
                kind=kind,
                file_path=getattr(args, "file", None),
                limit=limit,
            )
        )
        emit_json(format_symbol_search_for_cli(page))
        return 0

    def run_list(self, args: argparse.Namespace) -> int:
        """List repository symbols with pagination and optional ``--kind``/``--file`` filters.

        Args:
            args: Parsed ``rc symbols list`` CLI arguments.

        Returns:
            Process exit code ``0``.

        Raises:
            ValidationError: If ``--limit < 1``, ``--offset < 0``, or ``--kind`` is invalid.
        """
        db_path = resolve_cli_db_path(args, self._engine.db_path)
        limit = int(getattr(args, "limit", 50))
        offset = int(getattr(args, "offset", 0))
        if limit < 1:
            raise ValidationError("--limit must be >= 1", context={"limit": limit})
        if offset < 0:
            raise ValidationError("--offset must be >= 0", context={"offset": offset})

        kind = self._parse_optional_kind(getattr(args, "kind", None))
        logger.debug(
            "Running rc symbols list (db_path=%s, limit=%d, offset=%d)",
            db_path,
            limit,
            offset,
        )
        page = self._engine.for_db(db_path).queries.list_symbols(
            ListSymbolsQuery(
                repo=getattr(args, "repo", None),
                kind=kind,
                file_path=getattr(args, "file", None),
                limit=limit,
                offset=offset,
            )
        )
        emit_json(format_symbol_list_for_cli(page))
        return 0

    def run_show(self, args: argparse.Namespace) -> int:
        """Emit rich symbol context including hierarchy, relationships, and freshness.

        Args:
            args: Parsed ``rc symbols show`` CLI arguments.

        Returns:
            Process exit code ``0``.
        """
        db_path = resolve_cli_db_path(args, self._engine.db_path)
        include_source = bool(getattr(args, "source", False))
        id_or_name: str = args.id_or_name
        logger.debug("Running rc symbols show %r (db_path=%s)", id_or_name, db_path)
        ctx = self._engine.for_db(db_path).queries.get_symbol_context(
            ContextQuery(
                symbol=id_or_name,
                repo=getattr(args, "repo", None),
                include_source=include_source,
                max_source_lines=120,
                edge_limit=20,
                child_limit=20,
                sibling_limit=20,
            )
        )
        emit_json(format_symbol_context_for_cli(ctx))
        return 0

    def run_source(self, args: argparse.Namespace) -> int:
        """Emit line-numbered source snippet for a symbol.

        Args:
            args: Parsed ``rc source`` CLI arguments.

        Returns:
            Process exit code ``0``.

        Raises:
            ValidationError: If ``--max-lines < 1`` or ``--offset < 0``.
        """
        db_path = resolve_cli_db_path(args, self._engine.db_path)
        max_lines = int(getattr(args, "max_lines", 120))
        offset = int(getattr(args, "offset", 0))
        if max_lines < 1:
            raise ValidationError("--max-lines must be >= 1", context={"max_lines": max_lines})
        if offset < 0:
            raise ValidationError("--offset must be >= 0", context={"offset": offset})

        id_or_name: str = args.id_or_name
        logger.debug("Running rc source %r (db_path=%s)", id_or_name, db_path)
        snippet = self._engine.for_db(db_path).queries.get_symbol_source(
            SourceQuery(
                symbol=id_or_name,
                repo=getattr(args, "repo", None),
                max_lines=max_lines,
                offset=offset,
            )
        )
        emit_json(format_source_for_cli(snippet))
        return 0


__all__ = [
    "SymbolCliCommands",
]
