"""CLI command handlers for ``cbi files|outline|symbols|source|search``."""

import argparse

from cli.utils.output import (
    emit_json,
    format_code_search_for_cli,
    format_files_for_cli,
    format_outline_for_cli,
    format_source_for_cli,
    format_symbol_context_for_cli,
    format_symbol_list_for_cli,
    format_symbol_search_for_cli,
    resolve_scoped_engine,
)
from core.models.enums import SymbolKind
from core.models.search import CodeSearchQuery
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
    """Executes file listing, outline, symbol search/list/show, source, and search CLI commands."""

    def __init__(self, engine: IndexerEngine) -> None:
        """Initialize file and symbol CLI command handlers."""
        self._engine = engine

    @staticmethod
    def _parse_optional_kind(raw_kind: str | None) -> SymbolKind | None:
        """Coerce an optional CLI ``--kind`` string into a ``SymbolKind`` enum."""
        return SymbolKind(raw_kind) if raw_kind is not None else None

    def run_files(self, args: argparse.Namespace) -> int:
        """List indexed files with line count, symbol count, and live disk freshness."""
        scoped = resolve_scoped_engine(self._engine, args)
        limit = int(getattr(args, "limit", 50))
        logger.debug("Running cbi files (db_path=%s, limit=%d)", scoped.db_path, limit)
        page = scoped.queries.list_files(
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
        """Emit hierarchical symbol outline and imports for an indexed file."""
        scoped = resolve_scoped_engine(self._engine, args)
        file_path_arg: str = args.file_path
        logger.debug("Running cbi outline for %s (db_path=%s)", file_path_arg, scoped.db_path)
        outline = scoped.queries.get_file_outline(
            OutlineQuery(
                file_path=file_path_arg,
                repo=getattr(args, "repo", None),
                include_private=not bool(getattr(args, "no_private", False)),
                max_depth=int(getattr(args, "max_depth", 3)),
            )
        )
        emit_json(format_outline_for_cli(outline))
        return 0

    def run_search(self, args: argparse.Namespace) -> int:
        """Search symbols via FTS5 with exact/prefix ranking and ``LIKE`` fallback."""
        scoped = resolve_scoped_engine(self._engine, args)
        logger.debug("Running cbi symbols search %r (db_path=%s)", args.query, scoped.db_path)
        page = scoped.queries.search_symbols(
            SearchSymbolsQuery(
                query=args.query,
                repo=getattr(args, "repo", None),
                kind=self._parse_optional_kind(getattr(args, "kind", None)),
                file_path=getattr(args, "file", None),
                limit=int(getattr(args, "limit", 20)),
            )
        )
        emit_json(format_symbol_search_for_cli(page))
        return 0

    def run_list(self, args: argparse.Namespace) -> int:
        """List repository symbols with pagination and optional ``--kind``/``--file`` filters."""
        scoped = resolve_scoped_engine(self._engine, args)
        limit = int(getattr(args, "limit", 50))
        offset = int(getattr(args, "offset", 0))
        logger.debug("Running cbi symbols list (db_path=%s, limit=%d)", scoped.db_path, limit)
        page = scoped.queries.list_symbols(
            ListSymbolsQuery(
                repo=getattr(args, "repo", None),
                kind=self._parse_optional_kind(getattr(args, "kind", None)),
                file_path=getattr(args, "file", None),
                limit=limit,
                offset=offset,
            )
        )
        emit_json(format_symbol_list_for_cli(page))
        return 0

    def run_show(self, args: argparse.Namespace) -> int:
        """Emit rich symbol context including hierarchy, relationships, and freshness."""
        scoped = resolve_scoped_engine(self._engine, args)
        id_or_name: str = args.id_or_name
        logger.debug("Running cbi symbols show %r (db_path=%s)", id_or_name, scoped.db_path)
        ctx = scoped.queries.get_symbol_context(
            ContextQuery(
                symbol=id_or_name,
                repo=getattr(args, "repo", None),
                include_source=bool(getattr(args, "source", False)),
                max_source_lines=120,
                edge_limit=20,
                child_limit=20,
                sibling_limit=20,
            )
        )
        emit_json(format_symbol_context_for_cli(ctx))
        return 0

    def run_source(self, args: argparse.Namespace) -> int:
        """Emit line-numbered source snippet for a symbol."""
        scoped = resolve_scoped_engine(self._engine, args)
        id_or_name: str = args.id_or_name
        logger.debug("Running cbi source %r (db_path=%s)", id_or_name, scoped.db_path)
        snippet = scoped.queries.get_symbol_source(
            SourceQuery(
                symbol=id_or_name,
                repo=getattr(args, "repo", None),
                max_lines=int(getattr(args, "max_lines", 120)),
                offset=int(getattr(args, "offset", 0)),
            )
        )
        emit_json(format_source_for_cli(snippet))
        return 0

    def run_code_search(self, args: argparse.Namespace) -> int:
        """Search all UTF-8 repository text files via ``cbi search``."""
        scoped = resolve_scoped_engine(self._engine, args)
        report = scoped.queries.search_code(
            CodeSearchQuery(
                query=args.query,
                repo=getattr(args, "repo", None),
                regex=bool(getattr(args, "regex", False)),
                case_sensitive=bool(getattr(args, "case_sensitive", False)),
                path_prefix=getattr(args, "path_prefix", None),
                language=getattr(args, "language", None),
                include_tests=not bool(getattr(args, "no_tests", False)),
                verify_disk=True,
                context_lines=int(getattr(args, "context_lines", 0)),
                limit=int(getattr(args, "limit", 50)),
                offset=int(getattr(args, "offset", 0)),
            )
        )
        emit_json(format_code_search_for_cli(report))
        return 0


__all__ = [
    "SymbolCliCommands",
]
