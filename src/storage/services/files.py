"""SQLAlchemy Core adapter implementing ``FileRepositoryProtocol``."""

from collections.abc import Sequence
from pathlib import Path
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.engine import Connection

from core.models.file import (
    FilePageQuery,
    FilePageResult,
    FileRecord,
    FileSymbolCountRow,
)
from core.utils.logging import get_logger
from core.utils.pathing import extract_uri_candidates, normalize_rel_path_prefix
from storage.utils.mappers import file_to_row, row_to_file
from storage.utils.schema import files_table, nodes_table, repos_table
from storage.utils.sql_helpers import (
    build_rel_path_prefix_clause,
    build_sqlite_upsert,
    chunked_sequence,
    db_operation,
    delete_file_graph_batches,
)

logger = get_logger("storage.services.files")

_PRUNE_BATCH_SIZE = 400


class SqlFileRepository:
    """SQLite repository adapter for tracked ``FileRecord`` entities."""

    def __init__(self, conn: Connection) -> None:
        """Initialize the repository over an active SQLAlchemy ``Connection``."""
        self._conn = conn

    def upsert(self, file_record: FileRecord) -> None:
        """Insert or update a single ``FileRecord``."""
        stmt = build_sqlite_upsert(files_table)
        with db_operation(
            f"Failed to upsert file {file_record.id}",
            context={"file_id": file_record.id},
        ):
            self._conn.execute(stmt, file_to_row(file_record))
            logger.debug("Upserted file %s", file_record.id)

    def upsert_many(self, file_records: Sequence[FileRecord]) -> None:
        """Insert or update a batch of ``FileRecord`` entities."""
        if not file_records:
            return
        stmt = build_sqlite_upsert(files_table)
        with db_operation(
            f"Failed to upsert {len(file_records)} files",
            context={"count": len(file_records)},
        ):
            for chunk in chunked_sequence(file_records, _PRUNE_BATCH_SIZE):
                self._conn.execute(stmt, [file_to_row(rec) for rec in chunk])
            logger.debug("Upserted %d files", len(file_records))

    def get_by_id(self, file_id: str) -> FileRecord | None:
        """Fetch a ``FileRecord`` by its exact identifier."""
        with db_operation(f"Failed to fetch file {file_id}", context={"file_id": file_id}):
            row = (
                self._conn
                .execute(select(files_table).where(files_table.c.id == file_id))
                .mappings()
                .first()
            )
        return row_to_file(row) if row is not None else None

    def get_by_ids(self, file_ids: Sequence[str]) -> dict[str, FileRecord]:
        """Batch-fetch ``FileRecord`` entities keyed by ``file_id``."""
        unique_ids = list(dict.fromkeys(fid for fid in file_ids if fid))
        if not unique_ids:
            return {}
        result: dict[str, FileRecord] = {}
        with db_operation("Failed to batch-fetch files"):
            for chunk in chunked_sequence(unique_ids, _PRUNE_BATCH_SIZE):
                rows = (
                    self._conn
                    .execute(select(files_table).where(files_table.c.id.in_(chunk)))
                    .mappings()
                    .all()
                )
                for row in rows:
                    rec = row_to_file(row)
                    result[rec.id] = rec
        return result

    def _query_by_exact_path(self, repo_id: str, candidate_path: str) -> FileRecord | None:
        """Query ``files_table`` by exact ``(repo_id, file_path)``."""
        row = (
            self._conn
            .execute(
                select(files_table).where(
                    files_table.c.repo_id == repo_id,
                    files_table.c.file_path == candidate_path,
                )
            )
            .mappings()
            .first()
        )
        return row_to_file(row) if row is not None else None

    def get_by_path(self, repo_id: str, file_path: str) -> FileRecord | None:
        """Fetch a ``FileRecord`` by repository ID and relative POSIX path."""
        trimmed = file_path.strip()
        if not trimmed:
            return None
        normalized = normalize_rel_path_prefix(trimmed)
        with db_operation(
            f"Failed to fetch file {file_path!r} in {repo_id}",
            context={"repo_id": repo_id, "file_path": file_path},
        ):
            exact = self._query_by_exact_path(repo_id, trimmed)
            if exact is not None or normalized == trimmed or not normalized:
                return exact
            return self._query_by_exact_path(repo_id, normalized)

    def _resolve_by_repo_relative(
        self,
        resolved_path: Path,
        repo_id: str | None,
    ) -> FileRecord | None:
        """Match ``resolved_path`` relative to registered repository roots."""
        repo_stmt = select(repos_table.c.id, repos_table.c.root_path)
        if repo_id is not None:
            repo_stmt = repo_stmt.where(repos_table.c.id == repo_id)

        for repo_row in self._conn.execute(repo_stmt).mappings().all():
            root_val = repo_row["root_path"]
            if not root_val:
                continue
            try:
                rel_path = resolved_path.relative_to(Path(root_val).resolve()).as_posix()
            except (OSError, ValueError) as exc:
                logger.debug("Path %s is not relative to %s: %s", resolved_path, root_val, exc)
                continue

            matched = self._query_by_exact_path(str(repo_row["id"]), rel_path)
            if matched is not None:
                return matched
        return None

    def resolve_by_uri(self, uri: str, repo_id: str | None = None) -> FileRecord | None:
        """Resolve a ``file://`` URI (including symlinked variants) to a ``FileRecord``."""
        if not uri:
            return None
        candidate_uris, resolved_path = extract_uri_candidates(uri)
        with db_operation(
            f"Failed to resolve file by URI {uri!r}",
            context={"uri": uri, "repo_id": repo_id},
        ):
            stmt = select(files_table).where(files_table.c.uri.in_(candidate_uris))
            if repo_id is not None:
                stmt = stmt.where(files_table.c.repo_id == repo_id)
            row = self._conn.execute(stmt.limit(1)).mappings().first()
            if row is not None:
                return row_to_file(row)
            if resolved_path is not None:
                return self._resolve_by_repo_relative(resolved_path, repo_id)
        return None

    def list_for_repo(self, repo_id: str, *, include_tests: bool = True) -> list[FileRecord]:
        """List tracked files belonging to ``repo_id`` ordered by ``file_path``."""
        stmt = select(files_table).where(files_table.c.repo_id == repo_id)
        if not include_tests:
            stmt = stmt.where(files_table.c.is_test.is_(False))
        stmt = stmt.order_by(files_table.c.file_path)
        with db_operation(
            f"Failed to list files for repository {repo_id}",
            context={"repo_id": repo_id},
        ):
            rows = self._conn.execute(stmt).mappings().all()
        return [row_to_file(row) for row in rows]

    @staticmethod
    def _build_page_filters(query: FilePageQuery) -> list[Any]:
        """Build SQLAlchemy ``WHERE`` clauses for ``FilePageQuery``."""
        filters: list[Any] = [files_table.c.repo_id == query.repo_id]
        if not query.include_tests:
            filters.append(files_table.c.is_test.is_(False))
        prefix_clause = build_rel_path_prefix_clause(files_table.c.file_path, query.path_prefix)
        if prefix_clause is not None:
            filters.append(prefix_clause)
        return filters

    def list_page(self, query: FilePageQuery) -> FilePageResult:
        """Return a filtered, paginated page of tracked repository files."""
        filters = self._build_page_filters(query)
        with db_operation(
            f"Failed to list paginated files for {query.repo_id}",
            context={"repo_id": query.repo_id},
        ):
            total_count = int(
                self._conn.execute(
                    select(func.count()).select_from(files_table).where(*filters)
                ).scalar_one()
            )
            rows = (
                self._conn
                .execute(
                    select(files_table)
                    .where(*filters)
                    .order_by(files_table.c.file_path)
                    .limit(query.limit)
                    .offset(query.offset)
                )
                .mappings()
                .all()
            )
        return FilePageResult(
            total_count=total_count,
            items=[row_to_file(row) for row in rows],
        )

    def list_with_symbol_counts(
        self,
        repo_id: str,
        *,
        include_tests: bool = True,
    ) -> list[FileSymbolCountRow]:
        """List tracked files paired with their extracted symbol counts."""
        stmt = (
            select(
                files_table,
                func.count(nodes_table.c.id).label("_symbol_count"),
            )
            .select_from(
                files_table.outerjoin(nodes_table, nodes_table.c.file_id == files_table.c.id)
            )
            .where(files_table.c.repo_id == repo_id)
        )
        if not include_tests:
            stmt = stmt.where(files_table.c.is_test.is_(False))
        stmt = stmt.group_by(files_table.c.id).order_by(files_table.c.file_path)

        with db_operation(
            f"Failed to list files with symbol counts for {repo_id}",
            context={"repo_id": repo_id},
        ):
            rows = self._conn.execute(stmt).mappings().all()

        return [
            FileSymbolCountRow(
                file=row_to_file(row),
                symbol_count=int(row["_symbol_count"] or 0),
            )
            for row in rows
        ]

    def prune_not_in_ids(self, repo_id: str, keep_file_ids: set[str]) -> int:
        """Delete files in ``repo_id`` whose IDs are not in ``keep_file_ids``."""
        with db_operation(
            f"Failed to prune stale files in {repo_id}",
            context={"repo_id": repo_id},
        ):
            existing_ids = [
                str(fid)
                for fid in self._conn
                .execute(select(files_table.c.id).where(files_table.c.repo_id == repo_id))
                .scalars()
                .all()
            ]
            stale_ids = [fid for fid in existing_ids if fid not in keep_file_ids]
            pruned = delete_file_graph_batches(self._conn, stale_ids, repo_id=repo_id)
            logger.debug("Pruned %d stale files by ID in %s", pruned, repo_id)
            return pruned

    def delete_file_graph(
        self,
        file_id: str,
        *,
        preserve_incoming_ast_edges: bool = True,
    ) -> int:
        """Delete a file record, its symbol nodes, owned edges, and stale LSP references."""
        with db_operation(
            f"Failed to delete file graph for {file_id}",
            context={"file_id": file_id},
        ):
            deleted = delete_file_graph_batches(
                self._conn,
                [file_id],
                preserve_incoming_ast_edges=preserve_incoming_ast_edges,
            )
            logger.debug(
                "Deleted file graph for %s (deleted=%d, preserve_incoming_ast=%s)",
                file_id,
                deleted,
                preserve_incoming_ast_edges,
            )
            return deleted
