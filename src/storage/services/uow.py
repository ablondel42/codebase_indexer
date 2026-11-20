"""SQLite ``UnitOfWorkProtocol`` and ``UnitOfWorkFactoryProtocol`` implementations."""

import sqlite3
import uuid
from collections.abc import Generator, Sequence
from contextlib import contextmanager
from pathlib import Path
from types import TracebackType
from typing import Any, Self

from sqlalchemy import delete, select
from sqlalchemy.engine import Connection
from sqlalchemy.exc import SQLAlchemyError

from core.models.diagnostics import SqlQueryResult
from core.models.edge import Edge
from core.models.exceptions import DatabaseError, ValidationError
from core.models.file import FileRecord
from core.models.node import SymbolNode
from core.ports.repositories import (
    DiagnosticsRepositoryProtocol,
    EdgeRepositoryProtocol,
    FileRepositoryProtocol,
    IndexRunRepositoryProtocol,
    NodeRepositoryProtocol,
    ReferenceRefreshRepositoryProtocol,
    RepoRepositoryProtocol,
    TextSearchRepositoryProtocol,
)
from core.ports.uow import UnitOfWorkProtocol
from core.utils.logging import get_logger
from storage.models.options import ConnectionOptions
from storage.services.connection import ConnectionFactory
from storage.services.diagnostics import SqlDiagnosticsRepository
from storage.services.edges import SqlEdgeRepository
from storage.services.files import SqlFileRepository
from storage.services.migrator import SchemaMigrator
from storage.services.nodes import SqlNodeRepository
from storage.services.refresh import SqlReferenceRefreshRepository
from storage.services.repos import SqlRepoRepository
from storage.services.runs import SqlIndexRunRepository
from storage.services.text_search import SqlTextSearchRepository
from storage.utils.schema import nodes_table
from storage.utils.sql_helpers import chunked_sequence, db_error_message

logger = get_logger("storage.services.uow")

_NODE_DELETE_CHUNK_SIZE = 400


class SqlUnitOfWork:
    """Transactional SQLite Unit of Work binding all 7 repository adapters over one connection."""

    def __init__(self, conn: Connection, *, read_only: bool = False) -> None:
        """Initialize repository ports bound to ``conn``.

        Args:
            conn: Active SQLAlchemy ``Connection``.
            read_only: Whether the Unit of Work enforces read-only semantics.
        """
        self._conn = conn
        self._read_only = read_only
        self._repos = SqlRepoRepository(conn)
        self._files = SqlFileRepository(conn)
        self._nodes = SqlNodeRepository(conn)
        self._edges = SqlEdgeRepository(conn)
        self._refresh = SqlReferenceRefreshRepository(conn)
        self._runs = SqlIndexRunRepository(conn)
        self._diagnostics = SqlDiagnosticsRepository(conn)
        self._text_search = SqlTextSearchRepository(conn)

    @property
    def repos(self) -> RepoRepositoryProtocol:
        """Repository port for ``RepoRecord`` persistence."""
        return self._repos

    @property
    def files(self) -> FileRepositoryProtocol:
        """Repository port for ``FileRecord`` persistence."""
        return self._files

    @property
    def nodes(self) -> NodeRepositoryProtocol:
        """Repository port for ``SymbolNode`` and ``SymbolSummary`` access."""
        return self._nodes

    @property
    def edges(self) -> EdgeRepositoryProtocol:
        """Repository port for ``Edge`` persistence and reference queries."""
        return self._edges

    @property
    def refresh(self) -> ReferenceRefreshRepositoryProtocol:
        """Repository port for ``ReferenceRefreshState`` tracking."""
        return self._refresh

    @property
    def runs(self) -> IndexRunRepositoryProtocol:
        """Repository port for ``IndexRunRecord`` lifecycle auditing."""
        return self._runs

    @property
    def diagnostics(self) -> DiagnosticsRepositoryProtocol:
        """Repository port for database health and graph statistics."""
        return self._diagnostics

    @property
    def text_search(self) -> TextSearchRepositoryProtocol:
        """Repository port for full-codebase UTF-8 ``text_files`` + trigram ``text_fts``."""
        return self._text_search

    def __enter__(self) -> Self:
        """Enter the Unit of Work context.

        Returns:
            Active ``SqlUnitOfWork`` instance.
        """
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_val: BaseException | None,
        exc_tb: TracebackType | None,
    ) -> None:
        """Roll back uncommitted write transactions when exiting on an exception.

        Args:
            exc_type: Exception class if raised inside the ``with`` block, else ``None``.
            exc_val: Exception instance if raised inside the ``with`` block, else ``None``.
            exc_tb: Traceback if an exception was raised, else ``None``.
        """
        _ = exc_tb
        if exc_type is not None and not self._read_only and self._conn.in_transaction():
            logger.warning(
                "Rolling back UnitOfWork transaction due to %s: %s",
                exc_type.__name__,
                exc_val,
            )
            self.rollback()

    def commit(self) -> None:
        """Commit the current SQLite transaction.

        Raises:
            DatabaseError: If called on a read-only Unit of Work or commit fails.
        """
        if self._read_only:
            raise DatabaseError("Cannot commit a read-only UnitOfWork transaction.")
        try:
            if self._conn.in_transaction():
                self._conn.commit()
                logger.debug("Committed SQLite UnitOfWork transaction")
        except (SQLAlchemyError, sqlite3.Error) as exc:
            msg = db_error_message(exc)
            logger.error("Failed committing SQLite transaction: %s", msg)
            raise DatabaseError(f"Failed to commit database transaction: {msg}") from exc

    def rollback(self) -> None:
        """Roll back the current SQLite transaction if active.

        Raises:
            DatabaseError: If rolling back the transaction fails.
        """
        try:
            if self._conn.in_transaction():
                self._conn.rollback()
                logger.debug("Rolled back SQLite UnitOfWork transaction")
        except (SQLAlchemyError, sqlite3.Error) as exc:
            msg = db_error_message(exc)
            logger.error("Failed rolling back SQLite transaction: %s", msg)
            raise DatabaseError(f"Failed to roll back database transaction: {msg}") from exc

    @contextmanager
    def savepoint(self) -> Generator[None, None, None]:
        """Open a nested SQLite ``SAVEPOINT`` isolating per-item writes.

        Yields:
            ``None`` within the active ``SAVEPOINT`` scope.

        Raises:
            DatabaseError: If opening or releasing the ``SAVEPOINT`` fails.
        """
        if self._read_only:
            raise DatabaseError("Cannot open a savepoint on a read-only UnitOfWork.")
        try:
            nested = self._conn.begin_nested()
        except (SQLAlchemyError, sqlite3.Error) as exc:
            msg = db_error_message(exc)
            logger.error("Failed opening SQLite savepoint: %s", msg)
            raise DatabaseError(f"Failed to open savepoint: {msg}") from exc

        try:
            yield
        except BaseException as exc:
            logger.debug("Rolling back SQLite savepoint due to %s: %s", type(exc).__name__, exc)
            if nested.is_active:
                nested.rollback()
            raise
        else:
            try:
                nested.commit()
            except (SQLAlchemyError, sqlite3.Error) as exc:
                msg = db_error_message(exc)
                logger.error("Failed releasing SQLite savepoint: %s", msg)
                raise DatabaseError(f"Failed to release savepoint: {msg}") from exc

    def _delete_removed_file_nodes(
        self,
        file_id: str,
        keep_node_ids: set[str],
    ) -> None:
        """Delete removed symbol nodes in ``file_id`` and their LSP ``references`` edges.

        Args:
            file_id: Containing file identifier.
            keep_node_ids: Set of active ``SymbolNode`` IDs retained in ``file_id``.

        Raises:
            DatabaseError: If querying or deleting removed nodes fails.
        """
        try:
            existing_ids = [
                str(nid)
                for nid in self._conn
                .execute(select(nodes_table.c.id).where(nodes_table.c.file_id == file_id))
                .scalars()
                .all()
            ]
        except (SQLAlchemyError, sqlite3.Error) as exc:
            msg = db_error_message(exc)
            logger.error("Failed reading existing nodes for %s: %s", file_id, msg)
            raise DatabaseError(
                f"Failed reading existing nodes for {file_id}: {msg}",
                context={"file_id": file_id},
            ) from exc

        removed_ids = [nid for nid in existing_ids if nid not in keep_node_ids]
        if not removed_ids:
            return

        self._edges.delete_lsp_references_for_symbols(removed_ids)
        try:
            for chunk in chunked_sequence(removed_ids, _NODE_DELETE_CHUNK_SIZE):
                self._conn.execute(delete(nodes_table).where(nodes_table.c.id.in_(chunk)))
            logger.debug("Deleted %d removed symbol nodes for %s", len(removed_ids), file_id)
        except (SQLAlchemyError, sqlite3.Error) as exc:
            msg = db_error_message(exc)
            logger.error("Failed deleting removed symbol nodes for %s: %s", file_id, msg)
            raise DatabaseError(
                f"Failed deleting removed symbol nodes for {file_id}: {msg}",
                context={"file_id": file_id},
            ) from exc

    def replace_file_graph(
        self,
        file_record: FileRecord,
        nodes: Sequence[SymbolNode],
        edges: Sequence[Edge],
    ) -> None:
        """Atomically replace a file's persisted record, symbol nodes, and owned edges.

        Upserts ``file_record``, deletes owned edges for ``file_record.id``, deletes
        removed symbol nodes in ``file_record.id`` whose IDs are not in ``nodes``
        along with their LSP ``references`` edges (preserving surviving callers' AST
        edges for batch placeholder reset), and upserts ``nodes`` and ``edges`` inside
        a ``SAVEPOINT``.

        Args:
            file_record: Updated ``FileRecord`` to upsert.
            nodes: Extracted ``SymbolNode`` declarations for the file.
            edges: Extracted ``Edge`` relationships owned by the file.

        Raises:
            DatabaseError: If any persistence operation inside the savepoint fails.
        """
        with self.savepoint():
            self._files.upsert(file_record)
            self._edges.delete_owned_for_file(file_record.id)
            self._delete_removed_file_nodes(file_record.id, {node.id for node in nodes})
            self._nodes.upsert_many(nodes)
            self._edges.upsert_many(edges)


class SqlUnitOfWorkFactory:
    """Factory producing scoped ``SqlUnitOfWork`` sessions and guarded read-only SQL runs."""

    def __init__(
        self,
        db_path: Path | str,
        *,
        connection_factory: ConnectionFactory | None = None,
        migrator: SchemaMigrator | None = None,
    ) -> None:
        """Initialize a Unit of Work factory targeting ``db_path``.

        When ``db_path`` is ``':memory:'``, allocates an isolated shared-memory SQLite
        URI pinned by an internal anchor connection so state persists across sequential
        ``open()`` and ``execute_readonly_sql()`` calls until ``close()`` is invoked.

        Args:
            db_path: Path to the SQLite database file (or ``':memory:'``).
            connection_factory: Optional custom ``ConnectionFactory`` instance.
            migrator: Optional custom ``SchemaMigrator`` instance.

        Raises:
            DatabaseError: If opening the shared-memory SQLite anchor connection fails.
        """
        self._db_path = Path(db_path)
        self._connection_factory = connection_factory or ConnectionFactory()
        self._migrator = migrator or SchemaMigrator()
        self._memory_uri: str | None = None
        self._memory_anchor: sqlite3.Connection | None = None
        if str(self._db_path) == ":memory:":
            mem_uri = f"file:indexer_mem_{uuid.uuid4().hex}?mode=memory&cache=shared"
            try:
                self._memory_anchor = sqlite3.connect(
                    mem_uri,
                    uri=True,
                    check_same_thread=False,
                )
                self._memory_uri = mem_uri
            except sqlite3.Error as exc:
                logger.error("Failed opening shared-memory SQLite anchor: %s", exc)
                raise DatabaseError(
                    f"Failed to initialize in-memory SQLite database: {exc}"
                ) from exc

    @property
    def db_path(self) -> Path:
        """Path to the backing SQLite database file."""
        return self._db_path

    def close(self) -> None:
        """Close the shared-memory SQLite anchor connection if held by this factory."""
        if self._memory_anchor is None:
            return
        anchor = self._memory_anchor
        self._memory_anchor = None
        try:
            anchor.close()
            logger.debug("Closed shared-memory SQLite anchor (%s)", self._memory_uri)
        except sqlite3.Error as exc:
            logger.debug("Ignoring sqlite3 error closing shared-memory anchor: %s", exc)

    @contextmanager
    def open(
        self,
        *,
        read_only: bool = False,
        must_exist: bool = False,
        initialize: bool = False,
    ) -> Generator[UnitOfWorkProtocol, None, None]:
        """Open a scoped ``UnitOfWorkProtocol`` session over ``db_path``.

        Auto-commits write sessions on clean exit when a transaction remains open,
        rolls back uncommitted writes on exception, and always disposes the underlying
        connection on exit.

        Args:
            read_only: When ``True``, enforces read-only connection semantics.
            must_exist: When ``True``, raises ``DatabaseNotFoundError`` if ``db_path`` is missing.
            initialize: When ``True``, applies/verifies schema v2 on open.

        Yields:
            Active ``UnitOfWorkProtocol`` session.

        Raises:
            DatabaseNotFoundError: If ``read_only`` or ``must_exist`` and file is missing.
            DatabaseError: If opening, migrating, or committing SQLite fails.
        """
        options = ConnectionOptions(
            db_path=self._db_path,
            read_only=read_only,
            must_exist=must_exist,
            with_select_authorizer=False,
            sqlite_uri=self._memory_uri,
        )
        conn = self._connection_factory.open(options)
        try:
            if initialize:
                self._migrator.initialize(conn)
            uow = SqlUnitOfWork(conn, read_only=read_only)
            with uow:
                yield uow
            if not read_only and conn.in_transaction():
                uow.commit()
        finally:
            self._connection_factory.close(conn)

    def execute_readonly_sql(
        self,
        query: str,
        *,
        limit: int = 100,
    ) -> SqlQueryResult:
        """Execute a SQLite authorizer-guarded ``SELECT``-only SQL query.

        Args:
            query: Raw read-only SQL statement.
            limit: Maximum number of rows to fetch before marking ``truncated = True``.

        Returns:
            ``SqlQueryResult`` containing column names, row dicts, and truncation flag.

        Raises:
            ValidationError: If ``limit < 1`` or ``query`` is blank.
            DatabaseNotFoundError: If ``db_path`` does not exist on disk.
            DatabaseError: If SQLite rejects or fails executing ``query``.
        """
        if limit < 1:
            raise ValidationError("limit must be >= 1", context={"limit": limit})
        cleaned_query = query.strip()
        if not cleaned_query:
            raise ValidationError("SQL query must not be empty.")

        options = ConnectionOptions(
            db_path=self._db_path,
            read_only=True,
            must_exist=True,
            with_select_authorizer=True,
            sqlite_uri=self._memory_uri,
        )
        conn = self._connection_factory.open(options)
        try:
            result = conn.exec_driver_sql(cleaned_query)
            if result.returns_rows:
                columns = list(result.keys())
                fetched = result.fetchmany(limit + 1)
                truncated = len(fetched) > limit
                rows: list[dict[str, Any]] = [
                    dict(zip(columns, row, strict=False)) for row in fetched[:limit]
                ]
            else:
                columns = []
                truncated = False
                rows = []
        except (SQLAlchemyError, sqlite3.Error) as exc:
            msg = db_error_message(exc)
            logger.error("Read-only SQL query failed on %s: %s", self._db_path, msg)
            raise DatabaseError(
                f"Read-only SQL query failed: {msg}",
                context={"db_path": str(self._db_path)},
            ) from exc
        finally:
            self._connection_factory.close(conn)

        return SqlQueryResult(
            db_path=str(self._db_path),
            columns=columns,
            row_count=len(rows),
            truncated=truncated,
            rows=rows,
        )
