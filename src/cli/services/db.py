"""CLI command handlers for ``cbi db {status,repos,tables,runs,sql}``."""

import argparse

from cli.utils.output import emit_json, resolve_scoped_engine
from core.utils.logging import get_logger
from engine.services.facade import IndexerEngine

logger = get_logger("cli.services.db")


class DbCliCommands:
    """Executes SQLite diagnostics, repository, table, run, and read-only SQL commands."""

    def __init__(self, engine: IndexerEngine) -> None:
        """Initialize database CLI command handlers."""
        self._engine = engine

    def run_status(self, args: argparse.Namespace) -> int:
        """Emit database health, schema version, table counts, and LSP coverage JSON."""
        scoped = resolve_scoped_engine(self._engine, args)
        repo_arg: str | None = getattr(args, "repo", None)
        logger.debug("Running cbi db status (db_path=%s, repo=%s)", scoped.db_path, repo_arg)
        emit_json(scoped.queries.get_db_status(repo_arg).model_dump(mode="json"))
        return 0

    def run_repos(self, args: argparse.Namespace) -> int:
        """Emit per-repository file, node, and edge storage summaries as JSON."""
        scoped = resolve_scoped_engine(self._engine, args)
        logger.debug("Running cbi db repos (db_path=%s)", scoped.db_path)
        emit_json(scoped.queries.list_repos().model_dump(mode="json"))
        return 0

    def run_tables(self, args: argparse.Namespace) -> int:
        """Emit SQLite ``user_version`` and exact table row counts as JSON."""
        scoped = resolve_scoped_engine(self._engine, args)
        logger.debug("Running cbi db tables (db_path=%s)", scoped.db_path)
        emit_json(scoped.queries.list_tables().model_dump(mode="json"))
        return 0

    def run_runs(self, args: argparse.Namespace) -> int:
        """Emit recent indexing run audit records as JSON."""
        scoped = resolve_scoped_engine(self._engine, args)
        limit = int(getattr(args, "limit", 20))
        repo_arg: str | None = getattr(args, "repo", None)
        logger.debug(
            "Running cbi db runs (db_path=%s, repo=%s, limit=%d)", scoped.db_path, repo_arg, limit
        )
        emit_json(scoped.queries.list_index_runs(repo_arg, limit=limit).model_dump(mode="json"))
        return 0

    def run_sql(self, args: argparse.Namespace) -> int:
        """Execute an authorizer-guarded read-only SQL query and emit JSON rows."""
        scoped = resolve_scoped_engine(self._engine, args)
        limit = int(getattr(args, "limit", 100))
        raw_query: str = getattr(args, "query", "")
        logger.debug("Running cbi db sql (db_path=%s, limit=%d)", scoped.db_path, limit)
        report = scoped.queries.execute_readonly_sql(raw_query, limit=limit)
        emit_json(report.model_dump(mode="json"), default=str)
        return 0


__all__ = [
    "DbCliCommands",
]
