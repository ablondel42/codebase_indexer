"""CLI command handlers for ``rc db {status,repos,tables,runs,sql}``."""

import argparse

from cli.utils.output import emit_json, resolve_cli_db_path
from core.models.exceptions import ValidationError
from core.utils.logging import get_logger
from engine.services.facade import IndexerEngine

logger = get_logger("cli.services.db")


class DbCliCommands:
    """Executes SQLite diagnostics, repository, table, run, and read-only SQL commands."""

    def __init__(self, engine: IndexerEngine) -> None:
        """Initialize database CLI command handlers.

        Args:
            engine: Headless ``IndexerEngine`` application facade.
        """
        self._engine = engine

    def run_status(self, args: argparse.Namespace) -> int:
        """Emit database health, schema version, table counts, and LSP coverage JSON.

        Args:
            args: Parsed ``rc db status`` CLI arguments.

        Returns:
            Process exit code ``0``.
        """
        db_path = resolve_cli_db_path(args, self._engine.db_path)
        repo_arg: str | None = getattr(args, "repo", None)
        logger.debug("Running rc db status (db_path=%s, repo=%s)", db_path, repo_arg)
        report = self._engine.for_db(db_path).queries.get_db_status(repo_arg)
        emit_json(report.model_dump(mode="json"))
        return 0

    def run_repos(self, args: argparse.Namespace) -> int:
        """Emit per-repository file, node, and edge storage summaries as JSON.

        Args:
            args: Parsed ``rc db repos`` CLI arguments.

        Returns:
            Process exit code ``0``.
        """
        db_path = resolve_cli_db_path(args, self._engine.db_path)
        logger.debug("Running rc db repos (db_path=%s)", db_path)
        report = self._engine.for_db(db_path).queries.list_repos()
        emit_json(report.model_dump(mode="json"))
        return 0

    def run_tables(self, args: argparse.Namespace) -> int:
        """Emit SQLite ``user_version`` and exact table row counts as JSON.

        Args:
            args: Parsed ``rc db tables`` CLI arguments.

        Returns:
            Process exit code ``0``.
        """
        db_path = resolve_cli_db_path(args, self._engine.db_path)
        logger.debug("Running rc db tables (db_path=%s)", db_path)
        report = self._engine.for_db(db_path).queries.list_tables()
        emit_json(report.model_dump(mode="json"))
        return 0

    def run_runs(self, args: argparse.Namespace) -> int:
        """Emit recent indexing run audit records as JSON.

        Args:
            args: Parsed ``rc db runs`` CLI arguments.

        Returns:
            Process exit code ``0``.

        Raises:
            ValidationError: If ``--limit < 1``.
        """
        db_path = resolve_cli_db_path(args, self._engine.db_path)
        limit = int(getattr(args, "limit", 20))
        if limit < 1:
            raise ValidationError("--limit must be >= 1", context={"limit": limit})
        repo_arg: str | None = getattr(args, "repo", None)
        logger.debug("Running rc db runs (db_path=%s, repo=%s, limit=%d)", db_path, repo_arg, limit)
        report = self._engine.for_db(db_path).queries.list_index_runs(repo_arg, limit=limit)
        emit_json(report.model_dump(mode="json"))
        return 0

    def run_sql(self, args: argparse.Namespace) -> int:
        """Execute an authorizer-guarded read-only SQL query and emit JSON rows.

        Args:
            args: Parsed ``rc db sql`` CLI arguments.

        Returns:
            Process exit code ``0``.

        Raises:
            ValidationError: If ``--limit < 1`` or ``query`` is empty.
        """
        db_path = resolve_cli_db_path(args, self._engine.db_path)
        limit = int(getattr(args, "limit", 100))
        if limit < 1:
            raise ValidationError("--limit must be >= 1", context={"limit": limit})
        raw_query: str = getattr(args, "query", "")
        logger.debug("Running rc db sql (db_path=%s, limit=%d)", db_path, limit)
        report = self._engine.for_db(db_path).queries.execute_readonly_sql(raw_query, limit=limit)
        emit_json(report.model_dump(mode="json"), default=str)
        return 0


__all__ = [
    "DbCliCommands",
]
