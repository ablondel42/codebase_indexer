"""Unit of Work and Unit of Work factory ``Protocol`` ports."""

from collections.abc import Sequence
from contextlib import AbstractContextManager
from pathlib import Path
from types import TracebackType
from typing import Protocol, Self, runtime_checkable

from core.models.diagnostics import SqlQueryResult
from core.models.edge import Edge
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
)


@runtime_checkable
class UnitOfWorkProtocol(Protocol):
    """Transactional Unit of Work exposing typed repository ports."""

    @property
    def repos(self) -> RepoRepositoryProtocol:
        """Repository port for ``RepoRecord`` persistence."""
        ...

    @property
    def files(self) -> FileRepositoryProtocol:
        """Repository port for ``FileRecord`` persistence."""
        ...

    @property
    def nodes(self) -> NodeRepositoryProtocol:
        """Repository port for ``SymbolNode`` and ``SymbolSummary`` access."""
        ...

    @property
    def edges(self) -> EdgeRepositoryProtocol:
        """Repository port for ``Edge`` persistence and reference queries."""
        ...

    @property
    def refresh(self) -> ReferenceRefreshRepositoryProtocol:
        """Repository port for ``ReferenceRefreshState`` tracking."""
        ...

    @property
    def runs(self) -> IndexRunRepositoryProtocol:
        """Repository port for ``IndexRunRecord`` lifecycle auditing."""
        ...

    @property
    def diagnostics(self) -> DiagnosticsRepositoryProtocol:
        """Repository port for database health and graph statistics."""
        ...

    def __enter__(self) -> Self:
        """Enter the Unit of Work context.

        Returns:
            Active ``UnitOfWorkProtocol`` instance.
        """
        ...

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_val: BaseException | None,
        exc_tb: TracebackType | None,
    ) -> None:
        """Exit the Unit of Work context, rolling back uncommitted work on error.

        Args:
            exc_type: Exception class if raised inside the ``with`` block, else ``None``.
            exc_val: Exception instance if raised inside the ``with`` block, else ``None``.
            exc_tb: Traceback if an exception was raised, else ``None``.
        """
        ...

    def commit(self) -> None:
        """Commit the current database transaction."""
        ...

    def rollback(self) -> None:
        """Roll back the current database transaction."""
        ...

    def savepoint(self) -> AbstractContextManager[None]:
        """Open a nested transaction ``SAVEPOINT`` for isolated per-item operations.

        Returns:
            Context manager releasing the savepoint on clean exit or rolling back to it on error.
        """
        ...

    def replace_file_graph(
        self,
        file_record: FileRecord,
        nodes: Sequence[SymbolNode],
        edges: Sequence[Edge],
    ) -> None:
        """Atomically replace a file's persisted record, symbol nodes, and owned edges.

        Args:
            file_record: Updated ``FileRecord`` to upsert.
            nodes: Extracted ``SymbolNode`` declarations for the file.
            edges: Extracted ``Edge`` relationships owned by the file.
        """
        ...


@runtime_checkable
class UnitOfWorkFactoryProtocol(Protocol):
    """Factory contract producing scoped ``UnitOfWorkProtocol`` sessions for a database."""

    @property
    def db_path(self) -> Path:
        """Path to the backing SQLite database file."""
        ...

    def open(
        self,
        *,
        read_only: bool = False,
        must_exist: bool = False,
        initialize: bool = False,
    ) -> AbstractContextManager[UnitOfWorkProtocol]:
        """Open a scoped ``UnitOfWorkProtocol`` session.

        Args:
            read_only: When ``True``, enforces read-only connection semantics.
            must_exist: When ``True``, raises ``DatabaseNotFoundError`` if ``db_path`` is missing.
            initialize: When ``True``, applies schema initialization/migrations on open.

        Returns:
            Context manager yielding an active ``UnitOfWorkProtocol``.
        """
        ...

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
        """
        ...
