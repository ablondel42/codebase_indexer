"""Reference refresh state persistence using SQLAlchemy Core."""

from __future__ import annotations

from typing import Any

from sqlalchemy import select
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.engine import Connection

from repo_context.storage.schema import reference_refresh_table


def upsert_reference_refresh(
    conn: Connection,
    target_symbol_id: str,
    available: bool,
    last_refreshed_at: str,
    refresh_status: str | None = None,
    error_code: str | None = None,
) -> None:
    """Upsert reference refresh state for a target symbol."""
    stmt = sqlite_insert(reference_refresh_table).values(
        target_symbol_id=target_symbol_id,
        available=bool(available),
        last_refreshed_at=last_refreshed_at,
        refresh_status=refresh_status,
        error_code=error_code,
    )
    stmt = stmt.on_conflict_do_update(
        index_elements=[reference_refresh_table.c.target_symbol_id],
        set_={
            "available": stmt.excluded.available,
            "last_refreshed_at": stmt.excluded.last_refreshed_at,
            "refresh_status": stmt.excluded.refresh_status,
            "error_code": stmt.excluded.error_code,
        },
    )
    conn.execute(stmt)


def get_reference_refresh_state(conn: Connection, target_symbol_id: str) -> dict[str, Any]:
    """Get reference refresh state for a target symbol."""
    row = (
        conn.execute(
            select(reference_refresh_table).where(
                reference_refresh_table.c.target_symbol_id == target_symbol_id
            )
        )
        .mappings()
        .first()
    )

    if row is None:
        return {
            "target_symbol_id": target_symbol_id,
            "available": False,
            "last_refreshed_at": None,
            "refresh_status": None,
            "error_code": None,
        }
    return {
        "target_symbol_id": row["target_symbol_id"],
        "available": bool(row["available"]),
        "last_refreshed_at": row["last_refreshed_at"],
        "refresh_status": row["refresh_status"],
        "error_code": row["error_code"],
    }
