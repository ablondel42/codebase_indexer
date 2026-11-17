"""Repository persistence helpers."""

import sqlite3

from repo_context.exceptions import DatabaseError
from repo_context.models.repo import RepoRecord


def _row_to_repo(row: sqlite3.Row) -> RepoRecord:
    """Convert a SQLite row into a RepoRecord."""
    return RepoRecord(
        id=row["id"],
        root_path=row["root_path"],
        name=row["name"],
        default_language=row["default_language"],
        created_at=row["created_at"],
        last_indexed_at=row["last_indexed_at"],
    )


def upsert_repo(conn: sqlite3.Connection, repo: RepoRecord) -> None:
    """Insert or update a repository record."""
    default_lang = (
        repo.default_language.value
        if hasattr(repo.default_language, "value")
        else str(repo.default_language)
    )
    conn.execute(
        """
        INSERT INTO repos (id, root_path, name, default_language, created_at, last_indexed_at)
        VALUES (?, ?, ?, ?, ?, ?)
        ON CONFLICT(id) DO UPDATE SET
            root_path = excluded.root_path,
            name = excluded.name,
            default_language = excluded.default_language,
            created_at = excluded.created_at,
            last_indexed_at = excluded.last_indexed_at
        """,
        (
            repo.id,
            repo.root_path,
            repo.name,
            default_lang,
            repo.created_at,
            repo.last_indexed_at,
        ),
    )


def get_repo_by_id(conn: sqlite3.Connection, repo_id: str) -> RepoRecord | None:
    """Get a repository record by ID."""
    row = conn.execute(
        """
        SELECT id, root_path, name, default_language, created_at, last_indexed_at
        FROM repos
        WHERE id = ?
        """,
        (repo_id,),
    ).fetchone()
    if row is None:
        return None
    return _row_to_repo(row)


def list_repos(conn: sqlite3.Connection) -> list[RepoRecord]:
    """List all tracked repositories ordered by name and ID."""
    rows = conn.execute(
        """
        SELECT id, root_path, name, default_language, created_at, last_indexed_at
        FROM repos
        ORDER BY name, id
        """
    ).fetchall()
    return [_row_to_repo(row) for row in rows]


def resolve_repo_id(conn: sqlite3.Connection, repo_id_or_name: str | None = None) -> str:
    """Resolve an explicit repo ID/name/path or auto-select the sole indexed repository.

    Raises:
        DatabaseError: If no repository exists, the target is not found, or multiple
            repositories match.
    """
    if repo_id_or_name is None or not str(repo_id_or_name).strip():
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

    target = str(repo_id_or_name).strip()

    row = conn.execute("SELECT id FROM repos WHERE id = ?", (target,)).fetchone()
    if row is not None:
        return row["id"]

    rows = conn.execute("SELECT id FROM repos WHERE name = ?", (target,)).fetchall()
    if len(rows) == 1:
        return rows[0]["id"]
    if len(rows) > 1:
        ids = ", ".join(r["id"] for r in rows)
        raise DatabaseError(
            f"Multiple repositories match name {target!r}: {ids}. Specify the full repo ID.",
            error_code="ambiguous_repo",
            context={"name": target, "repo_ids": [r["id"] for r in rows]},
        )

    rows = conn.execute("SELECT id FROM repos WHERE root_path = ?", (target,)).fetchall()
    if len(rows) == 1:
        return rows[0]["id"]
    if len(rows) > 1:
        ids = ", ".join(r["id"] for r in rows)
        raise DatabaseError(
            f"Multiple repositories match root_path {target!r}: {ids}. Specify the full repo ID.",
            error_code="ambiguous_repo",
            context={"root_path": target, "repo_ids": [r["id"] for r in rows]},
        )

    raise DatabaseError(
        f"Repository not found: {target!r}",
        error_code="repo_not_found",
        context={"repo_id_or_name": target},
    )
