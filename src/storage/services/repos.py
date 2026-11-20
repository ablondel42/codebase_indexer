"""SQLAlchemy Core adapter implementing ``RepoRepositoryProtocol``."""

from typing import Any

from sqlalchemy import ColumnElement, select
from sqlalchemy.engine import Connection

from core.models.exceptions import DatabaseError
from core.models.repo import RepoRecord
from core.utils.logging import get_logger
from storage.utils.mappers import repo_to_row, row_to_repo
from storage.utils.schema import repos_table
from storage.utils.sql_helpers import build_sqlite_upsert, db_operation

logger = get_logger("storage.services.repos")


class SqlRepoRepository:
    """SQLite repository adapter for ``RepoRecord`` entities."""

    def __init__(self, conn: Connection) -> None:
        """Initialize the repository over an active SQLAlchemy ``Connection``."""
        self._conn = conn

    def upsert(self, repo: RepoRecord) -> None:
        """Insert or update a ``RepoRecord`` row."""
        stmt = build_sqlite_upsert(repos_table)
        with db_operation(f"Failed to upsert repository {repo.id}", context={"repo_id": repo.id}):
            self._conn.execute(stmt, repo_to_row(repo))
            logger.debug("Upserted repository %s", repo.id)

    def get_by_id(self, repo_id: str) -> RepoRecord | None:
        """Fetch a ``RepoRecord`` by its exact identifier."""
        with db_operation(f"Failed to fetch repository {repo_id}", context={"repo_id": repo_id}):
            row = (
                self._conn
                .execute(select(repos_table).where(repos_table.c.id == repo_id))
                .mappings()
                .first()
            )
        return row_to_repo(row) if row is not None else None

    def list_all(self) -> list[RepoRecord]:
        """List all indexed repositories ordered deterministically by ``(name, id)``."""
        with db_operation("Failed to list repositories"):
            rows = (
                self._conn
                .execute(select(repos_table).order_by(repos_table.c.name, repos_table.c.id))
                .mappings()
                .all()
            )
        return [row_to_repo(row) for row in rows]

    def _select_sole_repo_id(self) -> str:
        """Return the sole repository ID or raise when zero or multiple exist."""
        repos = self.list_all()
        if not repos:
            raise DatabaseError(
                "No repositories found in database; run `cbi index` first.",
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
        """Return the unique ``repo_id`` matching ``column == target`` or raise if ambiguous."""
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
        """Resolve an explicit repository ID/name/root_path or sole-repo default."""
        if repo_id_or_name is None or not str(repo_id_or_name).strip():
            return self._select_sole_repo_id()

        target = str(repo_id_or_name).strip()
        with db_operation(
            f"Failed to resolve repository {target!r}",
            context={"repo_id_or_name": target},
        ):
            exact_id = self._conn.execute(
                select(repos_table.c.id).where(repos_table.c.id == target)
            ).scalar_one_or_none()
            if exact_id is not None:
                return str(exact_id)

            for col, label in (
                (repos_table.c.name, "name"),
                (repos_table.c.root_path, "root_path"),
            ):
                matched = self._match_unique_column(col, target, label)
                if matched is not None:
                    return matched

        raise DatabaseError(
            f"Repository not found: {target!r}",
            error_code="repo_not_found",
            context={"repo_id_or_name": target},
        )
