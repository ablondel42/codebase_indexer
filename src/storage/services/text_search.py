"""SQLAlchemy Core adapter implementing ``TextSearchRepositoryProtocol``."""

from collections.abc import Sequence
from typing import Any

from sqlalchemy import delete, select, text
from sqlalchemy.engine import Connection

from core.models.search import (
    TextCandidateQuery,
    TextDocumentMeta,
    TextDocumentRecord,
)
from core.utils.logging import get_logger
from storage.utils.mappers import (
    row_to_text_doc_meta,
    row_to_text_doc_record,
    text_doc_to_row,
)
from storage.utils.schema import text_files_table
from storage.utils.sql_helpers import (
    build_rel_path_prefix_clause,
    build_sqlite_upsert,
    chunked_sequence,
    db_operation,
)

logger = get_logger("storage.services.text_search")

_UPSERT_CHUNK_SIZE = 200
_DELETE_CHUNK_SIZE = 400


class SqlTextSearchRepository:
    """SQLite adapter managing ``text_files`` and external-content trigram ``text_fts``."""

    def __init__(self, conn: Connection) -> None:
        """Initialize the repository over an active SQLAlchemy ``Connection``."""
        self._conn = conn

    def upsert_many(self, documents: Sequence[TextDocumentRecord]) -> None:
        """Insert or update a batch of ``TextDocumentRecord`` rows and sync ``text_fts``."""
        if not documents:
            return
        stmt = build_sqlite_upsert(text_files_table)
        with db_operation("Failed to upsert text documents"):
            for chunk in chunked_sequence(documents, _UPSERT_CHUNK_SIZE):
                self._conn.execute(stmt, [text_doc_to_row(doc) for doc in chunk])

    def delete_by_paths(self, repo_id: str, file_paths: Sequence[str]) -> int:
        """Delete ``text_files`` rows matching ``file_paths`` under ``repo_id``."""
        if not file_paths:
            return 0
        deleted = 0
        with db_operation("Failed to delete text documents", context={"repo_id": repo_id}):
            for chunk in chunked_sequence(list(file_paths), _DELETE_CHUNK_SIZE):
                res = self._conn.execute(
                    delete(text_files_table).where(
                        text_files_table.c.repo_id == repo_id,
                        text_files_table.c.file_path.in_(chunk),
                    )
                )
                deleted += int(res.rowcount or 0)
        return deleted

    def prune_not_in_ids(self, repo_id: str, keep_ids: set[str]) -> int:
        """Delete ``text_files`` rows for ``repo_id`` absent from ``keep_ids``."""
        with db_operation("Failed to prune stale text documents", context={"repo_id": repo_id}):
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
        with db_operation("Failed to list text document metadata", context={"repo_id": repo_id}):
            rows = self._conn.execute(stmt).mappings().fetchall()
            return [row_to_text_doc_meta(row) for row in rows]

    @staticmethod
    def _build_candidate_filters(query: TextCandidateQuery) -> list[Any]:
        """Construct SQLAlchemy ``WHERE`` clauses for ``query_candidates``."""
        clauses: list[Any] = [text_files_table.c.repo_id == query.repo_id]
        if not query.include_tests:
            clauses.append(text_files_table.c.is_test.is_(False))
        if query.language:
            clauses.append(text_files_table.c.language == query.language.strip().lower())
        prefix_clause = build_rel_path_prefix_clause(
            text_files_table.c.file_path,
            query.path_prefix,
            directory_boundary=False,
        )
        if prefix_clause is not None:
            clauses.append(prefix_clause)
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
        with db_operation(
            "Failed to query text search candidates",
            context={"repo_id": query.repo_id},
        ):
            rows = self._conn.execute(stmt).mappings().fetchall()
            return [row_to_text_doc_record(row) for row in rows]

    def check_fts_integrity(self) -> bool:
        """Run SQLite FTS5 ``'integrity-check'`` on ``text_fts``."""
        with db_operation("text_fts integrity-check failed"):
            self._conn.exec_driver_sql("INSERT INTO text_fts(text_fts) VALUES ('integrity-check')")
            return True


__all__ = [
    "SqlTextSearchRepository",
]
