"""File persistence helpers."""

import sqlite3

from repo_context.models.file import FileRecord

_PRUNE_BATCH_SIZE = 500

_UPSERT_FILE_SQL = """
INSERT INTO files (
    id, repo_id, file_path, uri, module_path, language,
    content_hash, size_bytes, line_count, is_test,
    last_modified_at, last_indexed_at
)
VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
ON CONFLICT(id) DO UPDATE SET
    repo_id = excluded.repo_id,
    file_path = excluded.file_path,
    uri = excluded.uri,
    module_path = excluded.module_path,
    language = excluded.language,
    content_hash = excluded.content_hash,
    size_bytes = excluded.size_bytes,
    line_count = excluded.line_count,
    is_test = excluded.is_test,
    last_modified_at = excluded.last_modified_at,
    last_indexed_at = excluded.last_indexed_at
"""


def _file_params(file_record: FileRecord) -> tuple:
    return (
        file_record.id,
        file_record.repo_id,
        file_record.file_path,
        file_record.uri,
        file_record.module_path,
        file_record.language,
        file_record.content_hash,
        file_record.size_bytes,
        file_record.line_count,
        int(file_record.is_test),
        file_record.last_modified_at,
        file_record.last_indexed_at,
    )


def _row_to_file_record(row: sqlite3.Row) -> FileRecord:
    return FileRecord(
        id=row["id"],
        repo_id=row["repo_id"],
        file_path=row["file_path"],
        uri=row["uri"],
        module_path=row["module_path"],
        language=row["language"],
        content_hash=row["content_hash"],
        size_bytes=row["size_bytes"],
        line_count=row["line_count"],
        is_test=bool(row["is_test"]),
        last_modified_at=row["last_modified_at"],
        last_indexed_at=row["last_indexed_at"],
    )


def _row_to_file_dict(row: sqlite3.Row) -> dict:
    data = dict(row)
    if "is_test" in data:
        data["is_test"] = bool(data["is_test"])
    return data


def upsert_file(conn: sqlite3.Connection, file_record: FileRecord) -> None:
    """Insert or update a single file record."""
    conn.execute(_UPSERT_FILE_SQL, _file_params(file_record))


def upsert_files(conn: sqlite3.Connection, file_records: list[FileRecord]) -> None:
    """Insert or update multiple file records in a single batch."""
    if not file_records:
        return
    conn.executemany(_UPSERT_FILE_SQL, [_file_params(rec) for rec in file_records])


def list_files_for_repo(
    conn: sqlite3.Connection,
    repo_id: str,
    *,
    include_tests: bool = True,
) -> list[FileRecord]:
    """List file records for a repository sorted by ``file_path``."""
    if include_tests:
        cursor = conn.execute(
            """
            SELECT id, repo_id, file_path, uri, module_path, language,
                   content_hash, size_bytes, line_count, is_test,
                   last_modified_at, last_indexed_at
            FROM files
            WHERE repo_id = ?
            ORDER BY file_path
            """,
            (repo_id,),
        )
    else:
        cursor = conn.execute(
            """
            SELECT id, repo_id, file_path, uri, module_path, language,
                   content_hash, size_bytes, line_count, is_test,
                   last_modified_at, last_indexed_at
            FROM files
            WHERE repo_id = ? AND is_test = 0
            ORDER BY file_path
            """,
            (repo_id,),
        )
    return [_row_to_file_record(row) for row in cursor.fetchall()]


def _delete_files_by_ids_batched(
    conn: sqlite3.Connection,
    repo_id: str,
    stale_file_ids: list[str],
) -> int:
    """Delete edges, nodes, and files for stale file IDs in 500-item batches."""
    if not stale_file_ids:
        return 0

    deleted = 0
    for start in range(0, len(stale_file_ids), _PRUNE_BATCH_SIZE):
        batch = stale_file_ids[start : start + _PRUNE_BATCH_SIZE]
        placeholders = ",".join("?" * len(batch))
        batch_tuple = tuple(batch)

        conn.execute(
            f"DELETE FROM edges WHERE evidence_file_id IN ({placeholders})",
            batch_tuple,
        )
        conn.execute(
            f"DELETE FROM edges WHERE from_id IN (SELECT id FROM nodes WHERE file_id IN ({placeholders}))",
            batch_tuple,
        )
        conn.execute(
            f"DELETE FROM edges WHERE to_id IN (SELECT id FROM nodes WHERE file_id IN ({placeholders}))",
            batch_tuple,
        )
        conn.execute(
            f"DELETE FROM nodes WHERE file_id IN ({placeholders})",
            batch_tuple,
        )
        cursor = conn.execute(
            f"DELETE FROM files WHERE repo_id = ? AND id IN ({placeholders})",
            (repo_id, *batch_tuple),
        )
        deleted += cursor.rowcount
    return deleted


def prune_stale_files(
    conn: sqlite3.Connection,
    repo_id: str,
    keep_file_ids: set[str],
) -> int:
    """Delete files (and their edges and nodes) for ``repo_id`` not in ``keep_file_ids``."""
    rows = conn.execute(
        "SELECT id FROM files WHERE repo_id = ?",
        (repo_id,),
    ).fetchall()
    stale_ids = [row["id"] for row in rows if row["id"] not in keep_file_ids]
    return _delete_files_by_ids_batched(conn, repo_id, stale_ids)


def delete_files_not_in_set(
    conn: sqlite3.Connection,
    repo_id: str,
    keep_paths: set[str],
) -> int:
    """Delete file records (and associated graph state) whose file_path is not in ``keep_paths``."""
    rows = conn.execute(
        "SELECT id, file_path FROM files WHERE repo_id = ?",
        (repo_id,),
    ).fetchall()
    stale_ids = [row["id"] for row in rows if row["file_path"] not in keep_paths]
    return _delete_files_by_ids_batched(conn, repo_id, stale_ids)


def get_file_by_id(conn: sqlite3.Connection, file_id: str) -> dict | None:
    """Get a file record dictionary by its ID."""
    row = conn.execute(
        """
        SELECT id, repo_id, file_path, uri, module_path, language,
               content_hash, size_bytes, line_count, is_test,
               last_modified_at, last_indexed_at
        FROM files
        WHERE id = ?
        """,
        (file_id,),
    ).fetchone()
    if row is None:
        return None
    return _row_to_file_dict(row)


def get_file_by_path(
    conn: sqlite3.Connection,
    repo_id: str,
    file_path: str,
) -> dict | None:
    """Get a file record dictionary by ``(repo_id, file_path)``."""
    row = conn.execute(
        """
        SELECT id, repo_id, file_path, uri, module_path, language,
               content_hash, size_bytes, line_count, is_test,
               last_modified_at, last_indexed_at
        FROM files
        WHERE repo_id = ? AND file_path = ?
        """,
        (repo_id, file_path),
    ).fetchone()
    if row is None:
        return None
    return _row_to_file_dict(row)
