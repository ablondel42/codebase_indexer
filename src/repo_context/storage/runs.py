"""Index run tracking helpers."""

from datetime import datetime, timezone
import sqlite3
import uuid


def record_index_run_start(
    conn: sqlite3.Connection,
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
        """
        INSERT INTO index_runs (
            id, repo_id, run_type, started_at, completed_at, status,
            files_scanned, nodes_extracted, edges_extracted, error_message
        ) VALUES (?, ?, ?, ?, NULL, 'running', 0, 0, 0, NULL)
        """,
        (resolved_id, repo_id, run_type, resolved_started_at),
    )
    return resolved_id


def record_index_run_finish(
    conn: sqlite3.Connection,
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
        """
        UPDATE index_runs
        SET completed_at = ?,
            status = ?,
            files_scanned = ?,
            nodes_extracted = ?,
            edges_extracted = ?,
            error_message = ?
        WHERE id = ?
        """,
        (
            resolved_completed_at,
            status,
            files_scanned,
            nodes_extracted,
            edges_extracted,
            error_message,
            run_id,
        ),
    )


def list_index_runs(
    conn: sqlite3.Connection,
    repo_id: str | None = None,
    *,
    limit: int = 20,
) -> list[dict]:
    """List recent indexing runs ordered newest first."""
    if limit <= 0:
        return []
    if repo_id is not None:
        rows = conn.execute(
            """
            SELECT id, repo_id, run_type, started_at, completed_at, status,
                   files_scanned, nodes_extracted, edges_extracted, error_message
            FROM index_runs
            WHERE repo_id = ?
            ORDER BY started_at DESC, rowid DESC
            LIMIT ?
            """,
            (repo_id, limit),
        ).fetchall()
    else:
        rows = conn.execute(
            """
            SELECT id, repo_id, run_type, started_at, completed_at, status,
                   files_scanned, nodes_extracted, edges_extracted, error_message
            FROM index_runs
            ORDER BY started_at DESC, rowid DESC
            LIMIT ?
            """,
            (limit,),
        ).fetchall()
    return [dict(row) for row in rows]
