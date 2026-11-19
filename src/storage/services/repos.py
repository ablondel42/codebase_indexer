"""SQLAlchemy Core adapter implementing ``RepoRepositoryProtocol``."""

import sqlite3
from typing import Any

from sqlalchemy import ColumnElement, select
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.engine import Connection
from sqlalchemy.exc import SQLAlchemyError

from core.models.exceptions import DatabaseError
from core.models.repo import RepoRecord
from core.utils.logging import get_logger
from storage.utils.mappers import repo_to_row, row_to_repo
from storage.utils.schema import repos_table
from storage.utils.sql_helpers import db_error_message

logger = get_logger("storage.services.repos")


class SqlRepoRepository:
    """SQLite repository adapter for ``RepoRecord`` entities."""

    def __init__(self, conn: Connection) -> None:
        """Initialize the repository over an active SQLAlchemy ``Connection``.

        Args:
            conn: Active SQLAlchemy ``Connection``.
        """
        self._conn = conn

    def upsert(self, repo: RepoRecord) -> None:
        """Insert or update a ``RepoRecord`` row.

        Args:
            repo: Repository entity to persist.

        Raises:
            DatabaseError: If SQLite execution fails.
        """
        values = repo_to_row(repo)
        stmt = sqlite_insert(repos_table).values(values)
        stmt = stmt.on_conflict_do_update(
            index_elements=[repos_table.c.id],
            set_={
                "root_path": stmt.excluded.root_path,
                "name": stmt.excluded.name,
                "default_language": stmt.excluded.default_language,
                "created_at": stmt.excluded.created_at,
                "last_indexed_at": stmt.excluded.last_indexed_at,
            },
        )
        try:
            self._conn.execute(stmt)
            logger.debug("Upserted repository %s", repo.id)
        except (SQLAlchemyError, sqlite3.Error) as exc:
            msg = db_error_message(exc)
            logger.error("Failed upserting repository %s: %s", repo.id, msg)
            raise DatabaseError(
                f"Failed to upsert repository {repo.id}: {msg}",
                context={"repo_id": repo.id},
            ) from exc

    def get_by_id(self, repo_id: str) -> RepoRecord | None:
        """Fetch a ``RepoRecord`` by its exact identifier.

        Args:
            repo_id: Canonical repository identifier.

        Returns:
            Matching ``RepoRecord`` or ``None`` when absent.

        Raises:
            DatabaseError: If SQLite execution fails.
        """
        try:
            row = (
                self._conn
                .execute(select(repos_table).where(repos_table.c.id == repo_id))
                .mappings()
                .first()
            )
        except (SQLAlchemyError, sqlite3.Error) as exc:
            msg = db_error_message(exc)
            logger.error("Failed fetching repository %s: %s", repo_id, msg)
            raise DatabaseError(
                f"Failed to fetch repository {repo_id}: {msg}",
                context={"repo_id": repo_id},
            ) from exc
        if row is None:
            return None
        return row_to_repo(row)

    def list_all(self) -> list[RepoRecord]:
        """List all indexed repositories ordered deterministically by ``(name, id)``.

        Returns:
            Ordered list of ``RepoRecord`` entities.

        Raises:
            DatabaseError: If SQLite execution fails.
        """
        try:
            rows = (
                self._conn
                .execute(select(repos_table).order_by(repos_table.c.name, repos_table.c.id))
                .mappings()
                .all()
            )
        except (SQLAlchemyError, sqlite3.Error) as exc:
            msg = db_error_message(exc)
            logger.error("Failed listing repositories: %s", msg)
            raise DatabaseError(f"Failed to list repositories: {msg}") from exc
        return [row_to_repo(row) for row in rows]

    def _select_sole_repo_id(self) -> str:
        """Return the sole repository ID or raise when zero or multiple exist.

        Returns:
            Canonical ``repo_id`` of the single indexed repository.

        Raises:
            DatabaseError: With ``error_code="repo_not_found"`` when empty, or
                ``error_code="ambiguous_repo"`` when multiple repositories exist.
        """
        repos = self.list_all()
        if not repos:
            raise DatabaseError(
                "No repositories found in database; run `rc index` first.",
                error_code="repo_not_found",
            )
        if len(repos) > 1:
            ids = ", ".join(r.id for r in repos)
            raise DatabaseError(
                f"Multiple repositories found ({ids}); specify a repository ID or name.",
                error_code="ambiguous_repo",
                context={"repo_ids": [r.id for r in repos]},
            )
        return repos[0].id

    def _match_unique_column(
        self,
        column: ColumnElement[Any],
        target: str,
        field_name: str,
    ) -> str | None:
        """Return the unique ``repo_id`` matching ``column == target`` or raise if ambiguous.

        Args:
            column: ``repos_table`` column expression to compare.
            target: Candidate lookup string.
            field_name: Column label for error diagnostics.

        Returns:
            Matching ``repo_id`` string or ``None`` when zero rows match.

        Raises:
            DatabaseError: With ``error_code="ambiguous_repo"`` if multiple rows match,
                or ``error_code="database_error"`` if SQLite fails.
        """
        matched_ids = [
            str(rid)
            for rid in self._conn
            .execute(select(repos_table.c.id).where(column == target))
            .scalars()
            .all()
        ]
        if len(matched_ids) == 1:
            return matched_ids[0]
        if len(matched_ids) > 1:
            joined = ", ".join(matched_ids)
            raise DatabaseError(
                f"Multiple repositories match {field_name} {target!r}: {joined}. "
                "Specify the full repo ID.",
                error_code="ambiguous_repo",
                context={field_name: target, "repo_ids": matched_ids},
            )
        return None

    def resolve_id(self, repo_id_or_name: str | None = None) -> str:
        """Resolve an explicit repository ID/name/root_path or sole-repo default.

        Args:
            repo_id_or_name: Optional repository ID, short ``name``, ``root_path``, or ``None``.

        Returns:
            Resolved canonical ``repo_id`` string.

        Raises:
            DatabaseError: With ``error_code="repo_not_found"`` or ``"ambiguous_repo"``
                when resolution cannot uniquely identify a repository, or on SQL errors.
        """
        if repo_id_or_name is None or not str(repo_id_or_name).strip():
            return self._select_sole_repo_id()

        target = str(repo_id_or_name).strip()
        try:
            exact_id = self._conn.execute(
                select(repos_table.c.id).where(repos_table.c.id == target)
            ).scalar_one_or_none()
            if exact_id is not None:
                return str(exact_id)

            name_match = self._match_unique_column(repos_table.c.name, target, "name")
            if name_match is not None:
                return name_match

            path_match = self._match_unique_column(repos_table.c.root_path, target, "root_path")
            if path_match is not None:
                return path_match
        except DatabaseError:
            raise
        except (SQLAlchemyError, sqlite3.Error) as exc:
            msg = db_error_message(exc)
            logger.error("Failed resolving repository %r: %s", target, msg)
            raise DatabaseError(
                f"Failed to resolve repository {target!r}: {msg}",
                context={"repo_id_or_name": target},
            ) from exc

        raise DatabaseError(
            f"Repository not found: {target!r}",
            error_code="repo_not_found",
            context={"repo_id_or_name": target},
        )
