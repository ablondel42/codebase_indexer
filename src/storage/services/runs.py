"""SQLAlchemy Core adapter implementing ``IndexRunRepositoryProtocol``."""

import uuid

from sqlalchemy import insert, literal_column, select, update
from sqlalchemy.engine import Connection

from core.models.runs import IndexRunMetrics, IndexRunRecord
from core.utils.logging import get_logger
from core.utils.timestamps import utc_now_iso
from storage.utils.mappers import row_to_run
from storage.utils.schema import index_runs_table
from storage.utils.sql_helpers import db_operation

logger = get_logger("storage.services.runs")


class SqlIndexRunRepository:
    """SQLite repository adapter for ``IndexRunRecord`` audit entries."""

    def __init__(self, conn: Connection) -> None:
        """Initialize the repository over an active SQLAlchemy ``Connection``."""
        self._conn = conn

    def start_run(
        self,
        repo_id: str,
        run_type: str = "full",
        *,
        run_id: str | None = None,
        started_at: str | None = None,
    ) -> str:
        """Insert a ``running`` audit record and return its ``run_id``."""
        resolved_id = run_id or f"run:{uuid.uuid4().hex[:12]}"
        resolved_started_at = started_at or utc_now_iso()
        with db_operation(
            f"Failed to record index run start for {repo_id}",
            context={"repo_id": repo_id, "run_id": resolved_id},
        ):
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

    def finish_run(self, run_id: str, status: str, metrics: IndexRunMetrics) -> None:
        """Complete an existing index run with final status and extraction counters."""
        resolved_completed_at = metrics.completed_at or utc_now_iso()
        with db_operation(
            f"Failed to record index run completion for {run_id}",
            context={"run_id": run_id, "status": status},
        ):
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

    def list_recent(
        self,
        repo_id: str | None = None,
        *,
        limit: int = 20,
    ) -> list[IndexRunRecord]:
        """List recent indexing runs ordered newest first."""
        if limit <= 0:
            return []
        stmt = select(index_runs_table)
        if repo_id is not None:
            stmt = stmt.where(index_runs_table.c.repo_id == repo_id)
        stmt = stmt.order_by(
            index_runs_table.c.started_at.desc(),
            literal_column("index_runs.rowid").desc(),
        ).limit(limit)

        with db_operation("Failed to query index runs", context={"repo_id": repo_id}):
            rows = self._conn.execute(stmt).mappings().all()
        return [row_to_run(row) for row in rows]
