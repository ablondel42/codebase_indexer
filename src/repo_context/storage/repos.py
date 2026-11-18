"""Repository persistence helpers using SQLAlchemy Core."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from sqlalchemy import ColumnElement, select
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.engine import Connection, RowMapping

from repo_context.exceptions import DatabaseError
from repo_context.models.repo import RepoRecord
from repo_context.storage.schema import repos_table


def _row_to_repo(row: RowMapping | Mapping[str, Any]) -> RepoRecord:
    """Convert a database mapping row into a ``RepoRecord``."""
    return RepoRecord(
        id=row["id"],
        root_path=row["root_path"],
        name=row["name"],
        default_language=row["default_language"],
        created_at=row["created_at"],
        last_indexed_at=row["last_indexed_at"],
    )


def upsert_repo(conn: Connection, repo: RepoRecord) -> None:
    """Insert or update a repository record."""
    default_lang = (
        repo.default_language.value
        if hasattr(repo.default_language, "value")
        else str(repo.default_language)
    )
    values = {
        "id": repo.id,
        "root_path": repo.root_path,
        "name": repo.name,
        "default_language": default_lang,
        "created_at": repo.created_at,
        "last_indexed_at": repo.last_indexed_at,
    }
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
    conn.execute(stmt)


def get_repo_by_id(conn: Connection, repo_id: str) -> RepoRecord | None:
    """Get a repository record by ID."""
    row = conn.execute(select(repos_table).where(repos_table.c.id == repo_id)).mappings().first()
    if row is None:
        return None
    return _row_to_repo(row)


def list_repos(conn: Connection) -> list[RepoRecord]:
    """List all tracked repositories ordered by name and ID."""
    rows = (
        conn.execute(select(repos_table).order_by(repos_table.c.name, repos_table.c.id))
        .mappings()
        .all()
    )
    return [_row_to_repo(row) for row in rows]


def _select_sole_repo_id(conn: Connection) -> str:
    """Return the sole repository ID or raise if zero or multiple exist."""
    repos = list_repos(conn)
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


def _match_unique_repo_column(
    conn: Connection,
    column: ColumnElement[Any],
    target: str,
    field_name: str,
) -> str | None:
    """Return the single matching repo ID for ``column == target`` or raise if ambiguous."""
    matched_ids = [
        str(rid)
        for rid in conn.execute(select(repos_table.c.id).where(column == target))
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


def resolve_repo_id(conn: Connection, repo_id_or_name: str | None = None) -> str:
    """Resolve an explicit repo ID/name/path or auto-select the sole indexed repository."""
    if repo_id_or_name is None or not str(repo_id_or_name).strip():
        return _select_sole_repo_id(conn)

    target = str(repo_id_or_name).strip()

    exact_id = conn.execute(
        select(repos_table.c.id).where(repos_table.c.id == target)
    ).scalar_one_or_none()
    if exact_id is not None:
        return str(exact_id)

    name_match = _match_unique_repo_column(conn, repos_table.c.name, target, "name")
    if name_match is not None:
        return name_match

    path_match = _match_unique_repo_column(conn, repos_table.c.root_path, target, "root_path")
    if path_match is not None:
        return path_match

    raise DatabaseError(
        f"Repository not found: {target!r}",
        error_code="repo_not_found",
        context={"repo_id_or_name": target},
    )
