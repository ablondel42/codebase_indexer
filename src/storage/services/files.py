"""SQLAlchemy Core adapter implementing ``FileRepositoryProtocol``."""

import sqlite3
from collections.abc import Sequence
from pathlib import Path
from typing import Any

from sqlalchemy import delete, func, or_, select
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.engine import Connection
from sqlalchemy.exc import SQLAlchemyError

from core.models.enums import EdgeKind
from core.models.exceptions import DatabaseError
from core.models.file import (
    FilePageQuery,
    FilePageResult,
    FileRecord,
    FileSymbolCountRow,
)
from core.utils.logging import get_logger
from storage.utils.mappers import file_to_row, row_to_file
from storage.utils.schema import edges_table, files_table, nodes_table, repos_table
from storage.utils.sql_helpers import (
    chunked_sequence,
    db_error_message,
    escape_like,
    extract_uri_candidates,
    normalize_path_filter,
)

logger = get_logger("storage.services.files")

_PRUNE_BATCH_SIZE = 400


class SqlFileRepository:
    """SQLite repository adapter for tracked ``FileRecord`` entities."""

    def __init__(self, conn: Connection) -> None:
        """Initialize the repository over an active SQLAlchemy ``Connection``.

        Args:
            conn: Active SQLAlchemy ``Connection``.
        """
        self._conn = conn

    @staticmethod
    def _build_upsert_stmt() -> Any:
        """Construct an ``INSERT ... ON CONFLICT(id) DO UPDATE`` statement for ``files_table``.

        Returns:
            SQLite dialect upsert statement.
        """
        base = sqlite_insert(files_table)
        return base.on_conflict_do_update(
            index_elements=[files_table.c.id],
            set_={
                col.name: base.excluded[col.name] for col in files_table.columns if col.name != "id"
            },
        )

    def upsert(self, file_record: FileRecord) -> None:
        """Insert or update a single ``FileRecord``.

        Args:
            file_record: File entity to persist.

        Raises:
            DatabaseError: If SQLite execution fails.
        """
        try:
            self._conn.execute(self._build_upsert_stmt(), file_to_row(file_record))
            logger.debug("Upserted file %s", file_record.id)
        except (SQLAlchemyError, sqlite3.Error) as exc:
            msg = db_error_message(exc)
            logger.error("Failed upserting file %s: %s", file_record.id, msg)
            raise DatabaseError(
                f"Failed to upsert file {file_record.id}: {msg}",
                context={"file_id": file_record.id},
            ) from exc

    def upsert_many(self, file_records: Sequence[FileRecord]) -> None:
        """Insert or update a batch of ``FileRecord`` entities.

        Args:
            file_records: Sequence of ``FileRecord`` entities to persist.

        Raises:
            DatabaseError: If SQLite execution fails.
        """
        if not file_records:
            return
        stmt = self._build_upsert_stmt()
        try:
            for chunk in chunked_sequence(file_records, _PRUNE_BATCH_SIZE):
                self._conn.execute(stmt, [file_to_row(rec) for rec in chunk])
            logger.debug("Upserted %d files", len(file_records))
        except (SQLAlchemyError, sqlite3.Error) as exc:
            msg = db_error_message(exc)
            logger.error("Failed batch-upserting %d files: %s", len(file_records), msg)
            raise DatabaseError(
                f"Failed to upsert {len(file_records)} files: {msg}",
                context={"count": len(file_records)},
            ) from exc

    def get_by_id(self, file_id: str) -> FileRecord | None:
        """Fetch a ``FileRecord`` by its exact identifier.

        Args:
            file_id: Canonical file identifier.

        Returns:
            Matching ``FileRecord`` or ``None`` if absent.

        Raises:
            DatabaseError: If SQLite execution fails.
        """
        try:
            row = (
                self._conn
                .execute(select(files_table).where(files_table.c.id == file_id))
                .mappings()
                .first()
            )
        except (SQLAlchemyError, sqlite3.Error) as exc:
            msg = db_error_message(exc)
            logger.error("Failed fetching file %s: %s", file_id, msg)
            raise DatabaseError(
                f"Failed to fetch file {file_id}: {msg}",
                context={"file_id": file_id},
            ) from exc
        if row is None:
            return None
        return row_to_file(row)

    def get_by_ids(self, file_ids: Sequence[str]) -> dict[str, FileRecord]:
        """Batch-fetch ``FileRecord`` entities keyed by ``file_id``.

        Args:
            file_ids: Sequence of canonical file identifiers.

        Returns:
            Mapping from ``file_id`` to ``FileRecord``.

        Raises:
            DatabaseError: If SQLite execution fails.
        """
        unique_ids = list(dict.fromkeys(fid for fid in file_ids if fid))
        if not unique_ids:
            return {}
        result: dict[str, FileRecord] = {}
        try:
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
        except (SQLAlchemyError, sqlite3.Error) as exc:
            msg = db_error_message(exc)
            logger.error("Failed batch-fetching %d files: %s", len(unique_ids), msg)
            raise DatabaseError(f"Failed to batch-fetch files: {msg}") from exc
        return result

    def _query_by_exact_path(self, repo_id: str, candidate_path: str) -> FileRecord | None:
        """Query ``files_table`` by exact ``(repo_id, file_path)``.

        Args:
            repo_id: Owning repository identifier.
            candidate_path: Exact candidate ``file_path`` string.

        Returns:
            Matching ``FileRecord`` or ``None`` if absent.
        """
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
        """Fetch a ``FileRecord`` by repository ID and relative POSIX path.

        Args:
            repo_id: Owning repository identifier.
            file_path: Repository-relative POSIX file path.

        Returns:
            Matching ``FileRecord`` or ``None`` if absent.

        Raises:
            DatabaseError: If SQLite execution fails.
        """
        trimmed = file_path.strip()
        if not trimmed:
            return None
        normalized = normalize_path_filter(trimmed)
        try:
            exact = self._query_by_exact_path(repo_id, trimmed)
            if exact is not None or normalized == trimmed:
                return exact
            return self._query_by_exact_path(repo_id, normalized)
        except (SQLAlchemyError, sqlite3.Error) as exc:
            msg = db_error_message(exc)
            logger.error("Failed fetching file path %r in %s: %s", file_path, repo_id, msg)
            raise DatabaseError(
                f"Failed to fetch file {file_path!r} in {repo_id}: {msg}",
                context={"repo_id": repo_id, "file_path": file_path},
            ) from exc

    def _resolve_by_repo_relative(
        self,
        resolved_path: Path,
        repo_id: str | None,
    ) -> FileRecord | None:
        """Match ``resolved_path`` relative to registered repository roots.

        Args:
            resolved_path: Canonical resolved filesystem path.
            repo_id: Optional repository scope filter.

        Returns:
            Matching ``FileRecord`` or ``None`` if outside all candidate roots.
        """
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
                logger.debug(
                    "Path %s is not relative to repo root %s: %s",
                    resolved_path,
                    root_val,
                    exc,
                )
                continue

            matched = self._query_by_exact_path(str(repo_row["id"]), rel_path)
            if matched is not None:
                return matched
        return None

    def resolve_by_uri(self, uri: str, repo_id: str | None = None) -> FileRecord | None:
        """Resolve a ``file://`` URI (including normalized/symlinked variants) to a ``FileRecord``.

        Args:
            uri: Candidate ``file://`` URI string.
            repo_id: Optional repository scope filter.

        Returns:
            Matching ``FileRecord`` or ``None`` if absent.

        Raises:
            DatabaseError: If SQLite execution fails.
        """
        if not uri:
            return None
        candidate_uris, resolved_path = extract_uri_candidates(uri)
        try:
            stmt = select(files_table).where(files_table.c.uri.in_(candidate_uris))
            if repo_id is not None:
                stmt = stmt.where(files_table.c.repo_id == repo_id)
            row = self._conn.execute(stmt.limit(1)).mappings().first()
            if row is not None:
                return row_to_file(row)
            if resolved_path is not None:
                return self._resolve_by_repo_relative(resolved_path, repo_id)
        except (SQLAlchemyError, sqlite3.Error) as exc:
            msg = db_error_message(exc)
            logger.error("Failed resolving file by URI %r: %s", uri, msg)
            raise DatabaseError(
                f"Failed to resolve file by URI {uri!r}: {msg}",
                context={"uri": uri, "repo_id": repo_id},
            ) from exc
        return None

    def list_for_repo(self, repo_id: str, *, include_tests: bool = True) -> list[FileRecord]:
        """List tracked files belonging to ``repo_id`` ordered by ``file_path``.

        Args:
            repo_id: Owning repository identifier.
            include_tests: Whether test files should be included.

        Returns:
            Ordered list of ``FileRecord`` entities.

        Raises:
            DatabaseError: If SQLite execution fails.
        """
        stmt = select(files_table).where(files_table.c.repo_id == repo_id)
        if not include_tests:
            stmt = stmt.where(files_table.c.is_test.is_(False))
        stmt = stmt.order_by(files_table.c.file_path)
        try:
            rows = self._conn.execute(stmt).mappings().all()
        except (SQLAlchemyError, sqlite3.Error) as exc:
            msg = db_error_message(exc)
            logger.error("Failed listing files for repo %s: %s", repo_id, msg)
            raise DatabaseError(
                f"Failed to list files for repository {repo_id}: {msg}",
                context={"repo_id": repo_id},
            ) from exc
        return [row_to_file(row) for row in rows]

    @staticmethod
    def _build_page_filters(query: FilePageQuery) -> list[Any]:
        """Build SQLAlchemy ``WHERE`` clauses for ``FilePageQuery``.

        Args:
            query: Pagination and filter specification.

        Returns:
            List of SQLAlchemy boolean filter expressions.
        """
        filters: list[Any] = [files_table.c.repo_id == query.repo_id]
        if not query.include_tests:
            filters.append(files_table.c.is_test.is_(False))
        if query.path_prefix is not None:
            clean_prefix = normalize_path_filter(query.path_prefix)
            if clean_prefix and clean_prefix != ".":
                escaped = escape_like(clean_prefix)
                filters.append(
                    or_(
                        files_table.c.file_path == clean_prefix,
                        files_table.c.file_path.like(f"{escaped}/%", escape="\\"),
                    )
                )
        return filters

    def list_page(self, query: FilePageQuery) -> FilePageResult:
        """Return a filtered, paginated page of tracked repository files.

        Args:
            query: Pagination and path-prefix filter parameters.

        Returns:
            ``FilePageResult`` containing ``total_count`` and ``items``.

        Raises:
            DatabaseError: If SQLite execution fails.
        """
        filters = self._build_page_filters(query)
        try:
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
        except (SQLAlchemyError, sqlite3.Error) as exc:
            msg = db_error_message(exc)
            logger.error("Failed listing file page for repo %s: %s", query.repo_id, msg)
            raise DatabaseError(
                f"Failed to list paginated files for {query.repo_id}: {msg}",
                context={"repo_id": query.repo_id},
            ) from exc
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
        """List tracked files paired with their extracted symbol counts.

        Args:
            repo_id: Owning repository identifier.
            include_tests: Whether test files should be included.

        Returns:
            Ordered list of ``FileSymbolCountRow`` records.

        Raises:
            DatabaseError: If SQLite execution fails.
        """
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

        try:
            rows = self._conn.execute(stmt).mappings().all()
        except (SQLAlchemyError, sqlite3.Error) as exc:
            msg = db_error_message(exc)
            logger.error("Failed listing files with symbol counts for %s: %s", repo_id, msg)
            raise DatabaseError(
                f"Failed to list files with symbol counts for {repo_id}: {msg}",
                context={"repo_id": repo_id},
            ) from exc

        return [
            FileSymbolCountRow(
                file=row_to_file(row),
                symbol_count=int(row["_symbol_count"] or 0),
            )
            for row in rows
        ]

    def _delete_files_by_ids_batched(
        self,
        repo_id: str | None,
        file_ids: Sequence[str],
        *,
        preserve_incoming_ast_edges: bool = True,
    ) -> int:
        """Delete file graphs in bounded chunks without exceeding SQLite bind limits.

        Args:
            repo_id: Optional repository scope filter.
            file_ids: Sequence of ``file_id`` strings to delete.
            preserve_incoming_ast_edges: Whether to retain external callers' AST edges.

        Returns:
            Total number of deleted ``files`` rows.
        """
        if not file_ids:
            return 0
        deleted = 0
        for batch in chunked_sequence(file_ids, _PRUNE_BATCH_SIZE):
            node_subq = select(nodes_table.c.id).where(nodes_table.c.file_id.in_(batch))
            self._conn.execute(
                delete(edges_table).where(
                    or_(
                        edges_table.c.evidence_file_id.in_(batch),
                        edges_table.c.from_id.in_(node_subq),
                    )
                )
            )
            if preserve_incoming_ast_edges:
                self._conn.execute(
                    delete(edges_table).where(
                        edges_table.c.kind == str(EdgeKind.REFERENCES),
                        edges_table.c.to_id.in_(node_subq),
                    )
                )
            else:
                self._conn.execute(delete(edges_table).where(edges_table.c.to_id.in_(node_subq)))

            self._conn.execute(delete(nodes_table).where(nodes_table.c.file_id.in_(batch)))
            del_stmt = delete(files_table).where(files_table.c.id.in_(batch))
            if repo_id is not None:
                del_stmt = del_stmt.where(files_table.c.repo_id == repo_id)
            res = self._conn.execute(del_stmt)
            deleted += max(res.rowcount or 0, 0)
        return deleted

    def prune_not_in_ids(self, repo_id: str, keep_file_ids: set[str]) -> int:
        """Delete files in ``repo_id`` whose IDs are not in ``keep_file_ids``.

        Args:
            repo_id: Owning repository identifier.
            keep_file_ids: Set of active ``file_id``s to retain.

        Returns:
            Number of pruned file records.

        Raises:
            DatabaseError: If SQLite execution fails.
        """
        try:
            existing_ids = [
                str(fid)
                for fid in self._conn
                .execute(select(files_table.c.id).where(files_table.c.repo_id == repo_id))
                .scalars()
                .all()
            ]
            stale_ids = [fid for fid in existing_ids if fid not in keep_file_ids]
            pruned = self._delete_files_by_ids_batched(repo_id, stale_ids)
            logger.debug("Pruned %d stale files by ID in %s", pruned, repo_id)
            return pruned
        except (SQLAlchemyError, sqlite3.Error) as exc:
            msg = db_error_message(exc)
            logger.error("Failed pruning stale files by ID in %s: %s", repo_id, msg)
            raise DatabaseError(
                f"Failed to prune stale files in {repo_id}: {msg}",
                context={"repo_id": repo_id},
            ) from exc

    def prune_not_in_paths(self, repo_id: str, keep_paths: set[str]) -> int:
        """Delete files in ``repo_id`` whose ``file_path`` values are not in ``keep_paths``.

        Args:
            repo_id: Owning repository identifier.
            keep_paths: Set of active relative POSIX paths to retain.

        Returns:
            Number of pruned file records.

        Raises:
            DatabaseError: If SQLite execution fails.
        """
        try:
            rows = (
                self._conn
                .execute(
                    select(files_table.c.id, files_table.c.file_path).where(
                        files_table.c.repo_id == repo_id
                    )
                )
                .mappings()
                .all()
            )
            stale_ids = [str(row["id"]) for row in rows if row["file_path"] not in keep_paths]
            pruned = self._delete_files_by_ids_batched(repo_id, stale_ids)
            logger.debug("Pruned %d stale files by path in %s", pruned, repo_id)
            return pruned
        except (SQLAlchemyError, sqlite3.Error) as exc:
            msg = db_error_message(exc)
            logger.error("Failed pruning stale files by path in %s: %s", repo_id, msg)
            raise DatabaseError(
                f"Failed to prune stale files by path in {repo_id}: {msg}",
                context={"repo_id": repo_id},
            ) from exc

    def delete_file_graph(
        self,
        file_id: str,
        *,
        preserve_incoming_ast_edges: bool = True,
    ) -> int:
        """Delete a file record, its symbol nodes, owned edges, and stale LSP references.

        Args:
            file_id: Target file identifier to remove.
            preserve_incoming_ast_edges: When ``True``, retains external callers' AST
                edges so batch re-linking can reset them cleanly to placeholders.

        Returns:
            Number of deleted file rows (``0`` or ``1``).

        Raises:
            DatabaseError: If SQLite execution fails.
        """
        try:
            deleted = self._delete_files_by_ids_batched(
                None,
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
        except (SQLAlchemyError, sqlite3.Error) as exc:
            msg = db_error_message(exc)
            logger.error("Failed deleting file graph for %s: %s", file_id, msg)
            raise DatabaseError(
                f"Failed to delete file graph for {file_id}: {msg}",
                context={"file_id": file_id},
            ) from exc
