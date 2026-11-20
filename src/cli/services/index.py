"""CLI command handlers for ``cbi index`` and ``cbi watch``."""

import argparse
from pathlib import Path

from cli.utils.output import (
    emit_json,
    format_index_result_for_cli,
    format_watch_event_payload,
    resolve_scoped_engine,
)
from core.models.base import FrozenModel
from core.utils.logging import get_logger
from engine.services.facade import IndexerEngine
from indexing.models.results import BatchIndexOptions

logger = get_logger("cli.services.index")


class IndexCliCommands:
    """Executes ``cbi index`` batch indexing and ``cbi watch`` incremental daemon commands."""

    def __init__(self, engine: IndexerEngine) -> None:
        """Initialize indexing CLI command handlers."""
        self._engine = engine

    def run_index(self, args: argparse.Namespace) -> int:
        """Scan, extract, link, and optionally LSP-enrich a repository."""
        scoped = resolve_scoped_engine(self._engine, args)
        include_tests = bool(getattr(args, "include_tests", False))
        force = bool(getattr(args, "force", False))
        enrich_lsp = bool(getattr(args, "lsp", False))
        repo_root: Path = getattr(args, "repo_path", None) or Path.cwd()

        logger.debug(
            "Running cbi index on %s (db_path=%s, include_tests=%s, force=%s, lsp=%s)",
            repo_root,
            scoped.db_path,
            include_tests,
            force,
            enrich_lsp,
        )
        result = scoped.commands.index_repository(
            repo_root,
            BatchIndexOptions(force=force, include_tests=include_tests, enrich_lsp=enrich_lsp),
        )
        emit_json(format_index_result_for_cli(result, include_tests=include_tests, force=force))
        return 0

    @staticmethod
    def _emit_watch_event(event: FrozenModel) -> None:
        """Write one formatted watch lifecycle or file event to ``stdout`` immediately."""
        emit_json(format_watch_event_payload(event), flush=True)

    def run_watch(self, args: argparse.Namespace) -> int:
        """Watch a repository directory and stream incremental JSON update events."""
        scoped = resolve_scoped_engine(self._engine, args)
        include_tests = bool(getattr(args, "include_tests", False))
        debounce_ms = int(getattr(args, "debounce_ms", 500))
        verbose = bool(getattr(args, "verbose", False) or getattr(args, "debug", False))
        repo_root: Path = getattr(args, "repo_path", None) or Path.cwd()

        logger.debug(
            "Running cbi watch on %s (db_path=%s, debounce_ms=%d, include_tests=%s)",
            repo_root,
            scoped.db_path,
            debounce_ms,
            include_tests,
        )
        scoped.commands.watch_repository(
            repo_root,
            debounce_ms=debounce_ms,
            include_tests=include_tests,
            verbose=verbose,
            on_event=self._emit_watch_event,
        )
        return 0


__all__ = [
    "IndexCliCommands",
]
