"""Index run tracking helpers using SQLAlchemy Core."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any
import uuid

from sqlalchemy import insert, literal_column, select, update
from sqlalchemy.engine import Connection

from repo_context.storage.schema import index_runs_table


def record_index_run_start(
    conn: Connection,
    repo_id: str,
    run_type: str = "full",
    *,
    run_id: str | None = None,
    started_at: str | None = None,
) -> str:
    """Record the start of an indexing run and return its ``run_id``."""
    resolved_id = run_id or f"run:{uuid.uuid4().hex[:12]}"
    resolved_started_at = started_at or datetime.now(timezone.utc).isoformat()
    conn.execute(
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
    return resolved_id


def record_index_run_finish(
    conn: Connection,
    run_id: str,
    status: str,
    *,
    files_scanned: int = 0,
    nodes_extracted: int = 0,
    edges_extracted: int = 0,
    error_message: str | None = None,
    completed_at: str | None = None,
) -> None:
    """Update an existing indexing run with completion status and counters."""
    resolved_completed_at = completed_at or datetime.now(timezone.utc).isoformat()
    conn.execute(
        update(index_runs_table)
        .where(index_runs_table.c.id == run_id)
        .values(
            completed_at=resolved_completed_at,
            status=status,
            files_scanned=files_scanned,
            nodes_extracted=nodes_extracted,
            edges_extracted=edges_extracted,
            error_message=error_message,
        )
    )


def list_index_runs(
    conn: Connection,
    repo_id: str | None = None,
    *,
    limit: int = 20,
) -> list[dict[str, Any]]:
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
    return [dict(row) for row in conn.execute(stmt).mappings().all()]
