"""SQLAlchemy Core adapter implementing ``IndexRunRepositoryProtocol``."""

import sqlite3
import uuid

from sqlalchemy import insert, literal_column, select, update
from sqlalchemy.engine import Connection
from sqlalchemy.exc import SQLAlchemyError

from core.models.exceptions import DatabaseError
from core.models.runs import IndexRunMetrics, IndexRunRecord
from core.utils.logging import get_logger
from core.utils.timestamps import utc_now_iso
from storage.utils.mappers import row_to_run
from storage.utils.schema import index_runs_table
from storage.utils.sql_helpers import db_error_message

logger = get_logger("storage.services.runs")


class SqlIndexRunRepository:
    """SQLite repository adapter for ``IndexRunRecord`` audit entries."""

    def __init__(self, conn: Connection) -> None:
        """Initialize the repository over an active SQLAlchemy ``Connection``.

        Args:
            conn: Active SQLAlchemy ``Connection``.
        """
        self._conn = conn

    def start_run(
        self,
        repo_id: str,
        run_type: str = "full",
        *,
        run_id: str | None = None,
        started_at: str | None = None,
    ) -> str:
        """Insert a ``running`` audit record and return its ``run_id``.

        Args:
            repo_id: Target repository identifier.
            run_type: Run category (e.g., ``"full"``, ``"incremental"``).
            run_id: Optional caller-supplied run identifier.
            started_at: Optional explicit ISO 8601 start timestamp.

        Returns:
            Persisted ``run_id`` string.

        Raises:
            DatabaseError: If SQLite execution fails.
        """
        resolved_id = run_id or f"run:{uuid.uuid4().hex[:12]}"
        resolved_started_at = started_at or utc_now_iso()
        try:
            self._conn.execute(
                insert(index_runs_table).values(
                    id=resolved_id,
                    repo_id=repo_id,
                    run_type=run_type,
                    started_at=resolved_started_at,
                    completed_at=None,
                    status="running",
                    files_scanned=0,
                    nodes_extracted=0,
                    edges_extracted=0,
                    error_message=None,
                )
            )
            logger.debug("Started index run %s (repo=%s, type=%s)", resolved_id, repo_id, run_type)
            return resolved_id
        except (SQLAlchemyError, sqlite3.Error) as exc:
            msg = db_error_message(exc)
            logger.error("Failed starting index run %s for %s: %s", resolved_id, repo_id, msg)
            raise DatabaseError(
                f"Failed to record index run start for {repo_id}: {msg}",
                context={"repo_id": repo_id, "run_id": resolved_id},
            ) from exc

    def finish_run(self, run_id: str, status: str, metrics: IndexRunMetrics) -> None:
        """Complete an existing index run with final status and extraction counters.

        Args:
            run_id: Identifier of the run created by ``start_run``.
            status: Terminal status string (e.g., ``"completed"``, ``"failed"``).
            metrics: Scanned/extracted counters, completion timestamp, and optional error.

        Raises:
            DatabaseError: If SQLite execution fails.
        """
        resolved_completed_at = metrics.completed_at or utc_now_iso()
        try:
            self._conn.execute(
                update(index_runs_table)
                .where(index_runs_table.c.id == run_id)
                .values(
                    completed_at=resolved_completed_at,
                    status=status,
                    files_scanned=metrics.files_scanned,
                    nodes_extracted=metrics.nodes_extracted,
                    edges_extracted=metrics.edges_extracted,
                    error_message=metrics.error_message,
                )
            )
            logger.debug("Finished index run %s with status=%s", run_id, status)
        except (SQLAlchemyError, sqlite3.Error) as exc:
            msg = db_error_message(exc)
            logger.error("Failed finishing index run %s: %s", run_id, msg)
            raise DatabaseError(
                f"Failed to record index run completion for {run_id}: {msg}",
                context={"run_id": run_id, "status": status},
            ) from exc

    def list_recent(
        self,
        repo_id: str | None = None,
        *,
        limit: int = 20,
    ) -> list[IndexRunRecord]:
        """List recent indexing runs ordered newest first.

        Args:
            repo_id: Optional repository scope filter.
            limit: Maximum number of records to return.

        Returns:
            Ordered list of ``IndexRunRecord`` entities.

        Raises:
            DatabaseError: If SQLite execution fails.
        """
        if limit <= 0:
            return []
        stmt = select(index_runs_table)
        if repo_id is not None:
            stmt = stmt.where(index_runs_table.c.repo_id == repo_id)
        stmt = stmt.order_by(
            index_runs_table.c.started_at.desc(),
            literal_column("index_runs.rowid").desc(),
        ).limit(limit)

        try:
            rows = self._conn.execute(stmt).mappings().all()
        except (SQLAlchemyError, sqlite3.Error) as exc:
            msg = db_error_message(exc)
            logger.error("Failed listing index runs (repo_id=%s): %s", repo_id, msg)
            raise DatabaseError(
                f"Failed to query index runs: {msg}",
                context={"repo_id": repo_id},
            ) from exc
        return [row_to_run(row) for row in rows]
