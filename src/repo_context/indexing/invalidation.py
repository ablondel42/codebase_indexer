"""Reference invalidation helpers.

When a file changes or is deleted, stored reference edges whose evidence comes
from that file and reference refresh states for affected symbols must be
invalidated inside the caller's transaction.
"""

from collections.abc import Iterable, Iterator, Sequence
import sqlite3

_SQLITE_IN_CHUNK_SIZE = 400


def _chunked(items: Sequence[str], size: int = _SQLITE_IN_CHUNK_SIZE) -> Iterator[Sequence[str]]:
    """Yield slices of at most ``size`` items to stay safely below SQLite bind limits."""
    for start in range(0, len(items), size):
        yield items[start : start + size]


def mark_symbol_ids_stale(conn: sqlite3.Connection, symbol_ids: Iterable[str]) -> list[str]:
    """Mark ``reference_refresh`` entries stale for any existing nodes in ``symbol_ids``.

    Chunks queries into batches of at most 400 IDs and filters against ``nodes``
    so foreign-key constraints are never violated and SQLite variable limits are
    never exceeded. Does not commit.
    """
    unique_ids = sorted({sid for sid in symbol_ids if sid})
    if not unique_ids:
        return []

    marked: list[str] = []
    for chunk in _chunked(unique_ids):
        placeholders = ",".join("?" * len(chunk))
        params = tuple(chunk)
        existing_rows = conn.execute(
            f"SELECT id FROM nodes WHERE id IN ({placeholders}) ORDER BY id",
            params,
        ).fetchall()
        if not existing_rows:
            continue

        conn.execute(
            f"""
            INSERT INTO reference_refresh (
                target_symbol_id, available, last_refreshed_at, refresh_status, error_code
            )
            SELECT id, 0, NULL, 'stale', NULL
            FROM nodes
            WHERE id IN ({placeholders})
            ON CONFLICT(target_symbol_id) DO UPDATE SET
                available = 0,
                last_refreshed_at = NULL,
                refresh_status = 'stale',
                error_code = NULL
            """,
            params,
        )
        marked.extend(row["id"] for row in existing_rows)

    return marked


def mark_symbols_in_file_stale(conn: sqlite3.Connection, file_id: str) -> list[str]:
    """Mark ``reference_refresh`` entries stale for all symbols declared in ``file_id``.

    Uses single-statement ``INSERT ... SELECT ... ON CONFLICT DO UPDATE`` so there
    is no SQLite bind-variable limit regardless of symbol count. Does not commit.

    Args:
        conn: SQLite connection.
        file_id: ID of the changed file.

    Returns:
        Sorted list of symbol IDs marked stale.
    """
    cursor = conn.execute(
        "SELECT id FROM nodes WHERE file_id = ? ORDER BY id",
        (file_id,),
    )
    symbol_ids = [row["id"] for row in cursor.fetchall()]
    if not symbol_ids:
        return []

    conn.execute(
        """
        INSERT INTO reference_refresh (
            target_symbol_id, available, last_refreshed_at, refresh_status, error_code
        )
        SELECT id, 0, NULL, 'stale', NULL
        FROM nodes
        WHERE file_id = ?
        ON CONFLICT(target_symbol_id) DO UPDATE SET
            available = 0,
            last_refreshed_at = NULL,
            refresh_status = 'stale',
            error_code = NULL
        """,
        (file_id,),
    )
    return symbol_ids


def invalidate_reference_summaries_for_file(conn: sqlite3.Connection, file_id: str) -> int:
    """Invalidate reference refresh states and remove reference edges for ``file_id``.

    Marks stale BOTH symbols defined in ``file_id`` AND external target symbols
    whose incoming ``references`` edges have ``evidence_file_id = file_id``, then
    deletes ``references`` edges whose ``evidence_file_id = file_id``.

    Caller owns the transaction boundary (this function never calls ``conn.commit()``).

    Args:
        conn: SQLite connection.
        file_id: ID of the changed file.

    Returns:
        Number of ``references`` edges removed.
    """
    conn.execute(
        """
        INSERT INTO reference_refresh (
            target_symbol_id, available, last_refreshed_at, refresh_status, error_code
        )
        SELECT id, 0, NULL, 'stale', NULL
        FROM nodes
        WHERE file_id = ?
           OR id IN (
               SELECT to_id
               FROM edges
               WHERE kind = 'references' AND evidence_file_id = ?
           )
        ON CONFLICT(target_symbol_id) DO UPDATE SET
            available = 0,
            last_refreshed_at = NULL,
            refresh_status = 'stale',
            error_code = NULL
        """,
        (file_id, file_id),
    )

    cursor = conn.execute(
        "DELETE FROM edges WHERE kind = 'references' AND evidence_file_id = ?",
        (file_id,),
    )
    return cursor.rowcount


def collect_impacted_symbol_ids(conn: sqlite3.Connection, file_id: str) -> list[str]:
    """Collect all symbol IDs declared in ``file_id`` or targeted by its ``references`` edges."""
    cursor = conn.execute(
        "SELECT id FROM nodes WHERE file_id = ?",
        (file_id,),
    )
    declared_ids = {row["id"] for row in cursor.fetchall()}

    cursor = conn.execute(
        """
        SELECT DISTINCT to_id FROM edges
        WHERE kind = 'references' AND evidence_file_id = ?
        """,
        (file_id,),
    )
    referenced_ids = {row["to_id"] for row in cursor.fetchall()}

    return sorted(declared_ids | referenced_ids)
