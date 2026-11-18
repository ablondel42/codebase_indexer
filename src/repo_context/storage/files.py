"""File persistence helpers using SQLAlchemy Core."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from sqlalchemy import delete, select
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.engine import Connection, RowMapping

from repo_context.models.file import FileRecord
from repo_context.storage.schema import edges_table, files_table, nodes_table

_PRUNE_BATCH_SIZE = 500


def _file_values(file_record: FileRecord) -> dict[str, Any]:
    return {
        "id": file_record.id,
        "repo_id": file_record.repo_id,
        "file_path": file_record.file_path,
        "uri": file_record.uri,
        "module_path": file_record.module_path,
        "language": file_record.language,
        "content_hash": file_record.content_hash,
        "size_bytes": file_record.size_bytes,
        "line_count": file_record.line_count,
        "is_test": bool(file_record.is_test),
        "last_modified_at": file_record.last_modified_at,
        "last_indexed_at": file_record.last_indexed_at,
    }


def _build_upsert_file_stmt() -> Any:
    base = sqlite_insert(files_table)
    return base.on_conflict_do_update(
        index_elements=[files_table.c.id],
        set_={
            col.name: base.excluded[col.name]
            for col in files_table.columns
            if col.name != "id"
        },
    )


def _row_to_file_record(row: RowMapping | Mapping[str, Any]) -> FileRecord:
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


def _row_to_file_dict(row: RowMapping | Mapping[str, Any]) -> dict[str, Any]:
    data: dict[str, Any] = {str(k): v for k, v in row.items()}
    if "is_test" in data:
        data["is_test"] = bool(data["is_test"])
    return data


def upsert_file(conn: Connection, file_record: FileRecord) -> None:
    """Insert or update a single file record."""
    conn.execute(_build_upsert_file_stmt(), _file_values(file_record))


def upsert_files(conn: Connection, file_records: list[FileRecord]) -> None:
    """Insert or update multiple file records in a single batch."""
    if not file_records:
        return
    conn.execute(_build_upsert_file_stmt(), [_file_values(rec) for rec in file_records])


def list_files_for_repo(
    conn: Connection,
    repo_id: str,
    *,
    include_tests: bool = True,
) -> list[FileRecord]:
    """List file records for a repository sorted by ``file_path``."""
    stmt = select(files_table).where(files_table.c.repo_id == repo_id)
    if not include_tests:
        stmt = stmt.where(files_table.c.is_test.is_(False))
    stmt = stmt.order_by(files_table.c.file_path)
    rows = conn.execute(stmt).mappings().all()
    return [_row_to_file_record(row) for row in rows]


def _delete_files_by_ids_batched(
    conn: Connection,
    repo_id: str,
    stale_file_ids: list[str],
) -> int:
    """Delete edges, nodes, and files for stale file IDs in 500-item batches."""
    if not stale_file_ids:
        return 0

    deleted = 0
    for start in range(0, len(stale_file_ids), _PRUNE_BATCH_SIZE):
        batch = stale_file_ids[start : start + _PRUNE_BATCH_SIZE]
        node_subq = select(nodes_table.c.id).where(nodes_table.c.file_id.in_(batch))

        conn.execute(delete(edges_table).where(edges_table.c.evidence_file_id.in_(batch)))
        conn.execute(delete(edges_table).where(edges_table.c.from_id.in_(node_subq)))
        conn.execute(delete(edges_table).where(edges_table.c.to_id.in_(node_subq)))
        conn.execute(delete(nodes_table).where(nodes_table.c.file_id.in_(batch)))
        res = conn.execute(
            delete(files_table).where(
                files_table.c.repo_id == repo_id,
                files_table.c.id.in_(batch),
            )
        )
        deleted += max(res.rowcount or 0, 0)
    return deleted


def prune_stale_files(
    conn: Connection,
    repo_id: str,
    keep_file_ids: set[str],
) -> int:
    """Delete files (and their edges and nodes) for ``repo_id`` not in ``keep_file_ids``."""
    existing_ids = [
        str(fid)
        for fid in conn.execute(select(files_table.c.id).where(files_table.c.repo_id == repo_id))
        .scalars()
        .all()
    ]
    stale_ids = [fid for fid in existing_ids if fid not in keep_file_ids]
    return _delete_files_by_ids_batched(conn, repo_id, stale_ids)


def delete_files_not_in_set(
    conn: Connection,
    repo_id: str,
    keep_paths: set[str],
) -> int:
    """Delete file records (and associated graph state) whose file_path is not in ``keep_paths``."""
    rows = (
        conn.execute(
            select(files_table.c.id, files_table.c.file_path).where(
                files_table.c.repo_id == repo_id
            )
        )
        .mappings()
        .all()
    )
    stale_ids = [str(row["id"]) for row in rows if row["file_path"] not in keep_paths]
    return _delete_files_by_ids_batched(conn, repo_id, stale_ids)


def get_file_by_id(conn: Connection, file_id: str) -> dict[str, Any] | None:
    """Get a file record dictionary by its ID."""
    row = conn.execute(select(files_table).where(files_table.c.id == file_id)).mappings().first()
    if row is None:
        return None
    return _row_to_file_dict(row)


def get_file_by_path(
    conn: Connection,
    repo_id: str,
    file_path: str,
) -> dict[str, Any] | None:
    """Get a file record dictionary by ``(repo_id, file_path)``."""
    row = (
        conn.execute(
            select(files_table).where(
                files_table.c.repo_id == repo_id,
                files_table.c.file_path == file_path,
            )
        )
        .mappings()
        .first()
    )
    if row is None:
        return None
    return _row_to_file_dict(row)
