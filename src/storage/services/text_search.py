"""SQLAlchemy Core adapter implementing ``TextSearchRepositoryProtocol``."""

import sqlite3
from collections.abc import Sequence
from typing import Any

from sqlalchemy import delete, select, text
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.engine import Connection
from sqlalchemy.exc import SQLAlchemyError

from core.models.exceptions import DatabaseError
from core.models.search import (
    TextCandidateQuery,
    TextDocumentMeta,
    TextDocumentRecord,
)
from core.utils.logging import get_logger
from storage.utils.schema import text_files_table
from storage.utils.sql_helpers import (
    chunked_sequence,
    db_error_message,
    escape_like,
    normalize_path_filter,
)

logger = get_logger("storage.services.text_search")

_UPSERT_CHUNK_SIZE = 200
_DELETE_CHUNK_SIZE = 400


class SqlTextSearchRepository:
    """SQLite adapter managing ``text_files`` and external-content trigram ``text_fts``."""

    def __init__(self, conn: Connection) -> None:
        """Initialize the repository over an active SQLAlchemy ``Connection``."""
        self._conn = conn

    @staticmethod
    def _doc_to_row(doc: TextDocumentRecord) -> dict[str, Any]:
        """Serialize a ``TextDocumentRecord`` into a ``text_files`` column dictionary."""
        return {
            "id": doc.id,
            "repo_id": doc.repo_id,
            "file_path": doc.file_path,
            "language": doc.language,
            "content_hash": doc.content_hash,
            "size_bytes": doc.size_bytes,
            "line_count": doc.line_count,
            "mtime_ns": doc.mtime_ns,
            "is_test": doc.is_test,
            "content": doc.content,
        }

    @staticmethod
    def _build_upsert_stmt() -> Any:
        """Build ``INSERT ... ON CONFLICT(id) DO UPDATE`` preserving ``rowid`` triggers."""
        ins = sqlite_insert(text_files_table)
        return ins.on_conflict_do_update(
            index_elements=[text_files_table.c.id],
            set_={
                "repo_id": ins.excluded.repo_id,
                "file_path": ins.excluded.file_path,
                "language": ins.excluded.language,
                "content_hash": ins.excluded.content_hash,
                "size_bytes": ins.excluded.size_bytes,
                "line_count": ins.excluded.line_count,
                "mtime_ns": ins.excluded.mtime_ns,
                "is_test": ins.excluded.is_test,
                "content": ins.excluded.content,
            },
        )

    def upsert_many(self, documents: Sequence[TextDocumentRecord]) -> None:
        """Insert or update a batch of ``TextDocumentRecord`` rows and sync ``text_fts``."""
        if not documents:
            return
        stmt = self._build_upsert_stmt()
        try:
            for chunk in chunked_sequence(documents, _UPSERT_CHUNK_SIZE):
                self._conn.execute(stmt, [self._doc_to_row(doc) for doc in chunk])
        except (SQLAlchemyError, sqlite3.Error) as exc:
            msg = db_error_message(exc)
            logger.error("Failed upserting %d text document(s): %s", len(documents), msg)
            raise DatabaseError(f"Failed to upsert text documents: {msg}") from exc

    def delete_by_paths(self, repo_id: str, file_paths: Sequence[str]) -> int:
        """Delete ``text_files`` rows matching ``file_paths`` under ``repo_id``."""
        if not file_paths:
            return 0
        deleted = 0
        try:
            for chunk in chunked_sequence(list(file_paths), _DELETE_CHUNK_SIZE):
                res = self._conn.execute(
                    delete(text_files_table).where(
                        text_files_table.c.repo_id == repo_id,
                        text_files_table.c.file_path.in_(chunk),
                    )
                )
                deleted += int(res.rowcount or 0)
            return deleted
        except (SQLAlchemyError, sqlite3.Error) as exc:
            msg = db_error_message(exc)
            logger.error("Failed deleting text documents in %s: %s", repo_id, msg)
            raise DatabaseError(f"Failed to delete text documents: {msg}") from exc

    def prune_not_in_ids(self, repo_id: str, keep_ids: set[str]) -> int:
        """Delete ``text_files`` rows for ``repo_id`` absent from ``keep_ids``."""
        try:
            rows = self._conn.execute(
                select(text_files_table.c.id).where(text_files_table.c.repo_id == repo_id)
            ).fetchall()
            stale_ids = [str(row[0]) for row in rows if str(row[0]) not in keep_ids]
            if not stale_ids:
                return 0
            deleted = 0
            for chunk in chunked_sequence(stale_ids, _DELETE_CHUNK_SIZE):
                res = self._conn.execute(
                    delete(text_files_table).where(text_files_table.c.id.in_(chunk))
                )
                deleted += int(res.rowcount or 0)
            return deleted
        except (SQLAlchemyError, sqlite3.Error) as exc:
            msg = db_error_message(exc)
            logger.error("Failed pruning stale text documents in %s: %s", repo_id, msg)
            raise DatabaseError(f"Failed to prune stale text documents: {msg}") from exc

    def list_metadata_for_repo(self, repo_id: str) -> list[TextDocumentMeta]:
        """List lightweight ``TextDocumentMeta`` rows for ``repo_id`` without ``content``."""
        stmt = (
            select(
                text_files_table.c.id,
                text_files_table.c.repo_id,
                text_files_table.c.file_path,
                text_files_table.c.language,
                text_files_table.c.content_hash,
                text_files_table.c.size_bytes,
                text_files_table.c.line_count,
                text_files_table.c.mtime_ns,
                text_files_table.c.is_test,
            )
            .where(text_files_table.c.repo_id == repo_id)
            .order_by(text_files_table.c.file_path)
        )
        try:
            rows = self._conn.execute(stmt).mappings().fetchall()
            return [
                TextDocumentMeta(
                    id=str(row["id"]),
                    repo_id=str(row["repo_id"]),
                    file_path=str(row["file_path"]),
                    language=str(row["language"]),
                    content_hash=str(row["content_hash"]),
                    size_bytes=int(row["size_bytes"]),
                    line_count=int(row["line_count"]),
                    mtime_ns=int(row["mtime_ns"]),
                    is_test=bool(row["is_test"]),
                )
                for row in rows
            ]
        except (SQLAlchemyError, sqlite3.Error) as exc:
            msg = db_error_message(exc)
            logger.error("Failed listing text metadata for %s: %s", repo_id, msg)
            raise DatabaseError(f"Failed to list text document metadata: {msg}") from exc

    @staticmethod
    def _build_candidate_filters(query: TextCandidateQuery) -> list[Any]:
        """Construct SQLAlchemy ``WHERE`` clauses for ``query_candidates``."""
        clauses: list[Any] = [text_files_table.c.repo_id == query.repo_id]
        if not query.include_tests:
            clauses.append(text_files_table.c.is_test.is_(False))
        if query.language:
            clauses.append(text_files_table.c.language == query.language.strip().lower())
        clean_prefix = normalize_path_filter(query.path_prefix) if query.path_prefix else None
        if clean_prefix:
            escaped = escape_like(clean_prefix)
            clauses.append(text_files_table.c.file_path.like(f"{escaped}%", escape="\\"))
        if query.required_trigrams:
            match_expr = " AND ".join(
                '"' + trig.replace('"', '""') + '"' for trig in query.required_trigrams
            )
            clauses.append(
                text(
                    "text_files.rowid IN "
                    "(SELECT rowid FROM text_fts WHERE text_fts MATCH :match_expr)"
                ).bindparams(match_expr=match_expr)
            )
        return clauses

    def query_candidates(self, query: TextCandidateQuery) -> list[TextDocumentRecord]:
        """Fetch candidate ``TextDocumentRecord`` rows pruned by ``required_trigrams``."""
        stmt = (
            select(text_files_table)
            .where(*self._build_candidate_filters(query))
            .order_by(text_files_table.c.file_path)
        )
        try:
            rows = self._conn.execute(stmt).mappings().fetchall()
            return [
                TextDocumentRecord(
                    id=str(row["id"]),
                    repo_id=str(row["repo_id"]),
                    file_path=str(row["file_path"]),
                    language=str(row["language"]),
                    content_hash=str(row["content_hash"]),
                    size_bytes=int(row["size_bytes"]),
                    line_count=int(row["line_count"]),
                    mtime_ns=int(row["mtime_ns"]),
                    is_test=bool(row["is_test"]),
                    content=str(row["content"]),
                )
                for row in rows
            ]
        except (SQLAlchemyError, sqlite3.Error) as exc:
            msg = db_error_message(exc)
            logger.error("Failed querying text candidates in %s: %s", query.repo_id, msg)
            raise DatabaseError(f"Failed to query text search candidates: {msg}") from exc

    def check_fts_integrity(self) -> bool:
        """Run SQLite FTS5 ``'integrity-check'`` on ``text_fts``."""
        try:
            self._conn.exec_driver_sql("INSERT INTO text_fts(text_fts) VALUES ('integrity-check')")
            return True
        except (SQLAlchemyError, sqlite3.Error) as exc:
            msg = db_error_message(exc)
            logger.error("text_fts integrity-check failed: %s", msg)
            raise DatabaseError(f"text_fts integrity-check failed: {msg}") from exc


__all__ = [
    "SqlTextSearchRepository",
]
