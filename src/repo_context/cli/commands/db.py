"""Handlers for `rc db {status,repos,tables,runs,sql}` using SQLAlchemy Core."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

from sqlalchemy import MetaData, Table, func, inspect, select
from sqlalchemy.engine import Connection
from sqlalchemy.exc import SQLAlchemyError

from repo_context.cli.utils import resolve_db_path
from repo_context.exceptions import DatabaseError, RepoContextError, ValidationError
from repo_context.logging_config import get_logger
from repo_context.models.repo import RepoRecord
from repo_context.storage import (
    SCHEMA_VERSION,
    close_connection,
    connection_scope,
    edges_table,
    files_table,
    get_repo_by_id,
    initialize_database,
    list_index_runs,
    list_repos,
    nodes_table,
    open_read_only_sql_connection,
    reference_refresh_table,
    resolve_repo_id,
)

logger = get_logger("cli.db")

_REQUIRED_STATUS_TABLES = frozenset(
    {"repos", "nodes", "edges", "index_runs", "reference_refresh"}
)


def _db_error_text(exc: SQLAlchemyError) -> str:
    """Return the underlying DBAPI message when present for concise error reporting."""
    orig = getattr(exc, "orig", None)
    return str(orig) if orig is not None else str(exc)


def _empty_lsp_coverage() -> dict[str, Any]:
    """Return zeroed default LSP reference coverage metrics."""
    return {
        "non_module_symbols": 0,
        "refreshed_symbols": 0,
        "coverage_pct": 0.0,
        "lsp_reference_edges": 0,
    }


def _get_db_size_bytes(db_path: Path) -> int:
    """Read the on-disk size of ``db_path`` in bytes."""
    try:
        size_bytes = db_path.stat().st_size
        logger.debug("Database file size: %d bytes (%s)", size_bytes, db_path)
        return size_bytes
    except OSError as exc:
        logger.error("Failed reading database file size for %s: %s", db_path, exc)
        raise DatabaseError(
            f"Failed to read database file size for {db_path}: {exc}",
            context={"db_path": str(db_path)},
        ) from exc


def _read_schema_version(conn: Connection) -> int:
    """Read ``PRAGMA user_version`` from an open SQLite connection."""
    try:
        raw = conn.exec_driver_sql("PRAGMA user_version").scalar_one_or_none()
        version = int(raw) if raw is not None else 0
        logger.debug("Read SQLite PRAGMA user_version=%d", version)
        return version
    except SQLAlchemyError as exc:
        msg = _db_error_text(exc)
        logger.error("Failed reading SQLite PRAGMA user_version: %s", msg)
        raise DatabaseError(f"Failed to read SQLite schema version: {msg}") from exc


def _list_table_counts(conn: Connection) -> list[dict[str, Any]]:
    """Return exact row counts for all user tables and FTS virtual tables."""
    current_table: str | None = None
    try:
        table_names = [
            name
            for name in sorted(inspect(conn).get_table_names())
            if not name.startswith("nodes_fts_")
        ]
        temp_meta = MetaData()
        tables: list[dict[str, Any]] = []
        for name in table_names:
            current_table = name
            tbl = Table(name, temp_meta)
            count = int(conn.execute(select(func.count()).select_from(tbl)).scalar_one())
            tables.append({"table": name, "row_count": count})
        logger.debug("Collected row counts for %d tables", len(tables))
        return tables
    except SQLAlchemyError as exc:
        msg = _db_error_text(exc)
        logger.error(
            "Failed reading table row counts (table=%s): %s",
            current_table or "<inspect>",
            msg,
        )
        raise DatabaseError(
            f"Failed to read table row counts: {msg}",
            context={"table": current_table},
        ) from exc


def _has_compatible_schema(user_version: int, tables: list[dict[str, Any]]) -> bool:
    """Return True when the database matches ``SCHEMA_VERSION`` and contains required tables."""
    table_names = {t["table"] for t in tables}
    missing_tables = sorted(_REQUIRED_STATUS_TABLES - table_names)
    if user_version != SCHEMA_VERSION or missing_tables:
        logger.warning(
            "Database schema not ready for full status inspection "
            "(user_version=%d, expected=%d, missing_tables=%s)",
            user_version,
            SCHEMA_VERSION,
            missing_tables,
        )
        return False
    return True


def _resolve_status_repo(
    conn: Connection,
    repo_arg: str | None,
) -> tuple[list[RepoRecord], RepoRecord | None]:
    """Load all indexed repositories and resolve the active repository if unambiguous."""
    try:
        all_repos = list_repos(conn)
        selected_repo: RepoRecord | None = None
        if repo_arg:
            selected_repo_id = resolve_repo_id(conn, repo_arg)
            selected_repo = get_repo_by_id(conn, selected_repo_id)
        elif len(all_repos) == 1:
            selected_repo = all_repos[0]
        logger.debug(
            "Resolved status repository (repo_count=%d, selected=%s)",
            len(all_repos),
            selected_repo.id if selected_repo else None,
        )
        return all_repos, selected_repo
    except RepoContextError as exc:
        logger.debug("Repository resolution failed (repo=%r): %s", repo_arg, exc)
        raise
    except SQLAlchemyError as exc:
        msg = _db_error_text(exc)
        logger.error("Database failure resolving repository (repo=%r): %s", repo_arg, msg)
        raise DatabaseError(
            f"Failed to resolve repository from database: {msg}",
            context={"repo": repo_arg},
        ) from exc


def _fetch_edge_counts_by_kind(
    conn: Connection,
    repo_id: str | None,
) -> dict[str, int]:
    """Query edge counts grouped by ``kind``, optionally scoped to ``repo_id``."""
    try:
        cnt_col = func.count().label("cnt")
        stmt = select(edges_table.c.kind, cnt_col)
        if repo_id is not None:
            stmt = stmt.where(edges_table.c.repo_id == repo_id)
        stmt = stmt.group_by(edges_table.c.kind).order_by(
            cnt_col.desc(), edges_table.c.kind.asc()
        )
        rows = conn.execute(stmt).mappings().all()
        counts = {str(r["kind"]): int(r["cnt"]) for r in rows}
        logger.debug(
            "Fetched edge counts across %d kinds (repo_id=%s)",
            len(counts),
            repo_id,
        )
        return counts
    except SQLAlchemyError as exc:
        msg = _db_error_text(exc)
        logger.error("Failed querying edge counts (repo_id=%s): %s", repo_id, msg)
        raise DatabaseError(
            f"Failed to query edge counts by kind: {msg}",
            context={"repo_id": repo_id},
        ) from exc


def _fetch_lsp_coverage(
    conn: Connection,
    repo_id: str | None,
) -> dict[str, Any]:
    """Compute non-module symbol LSP refresh coverage, optionally scoped to ``repo_id``."""
    try:
        sym_stmt = (
            select(func.count())
            .select_from(nodes_table)
            .where(nodes_table.c.kind != "module")
        )
        edge_stmt = (
            select(func.count())
            .select_from(edges_table)
            .where(edges_table.c.kind == "references")
        )
        if repo_id is not None:
            sym_stmt = sym_stmt.where(nodes_table.c.repo_id == repo_id)
            edge_stmt = edge_stmt.where(edges_table.c.repo_id == repo_id)
            ref_stmt = (
                select(func.count())
                .select_from(
                    reference_refresh_table.join(
                        nodes_table,
                        nodes_table.c.id == reference_refresh_table.c.target_symbol_id,
                    )
                )
                .where(
                    nodes_table.c.repo_id == repo_id,
                    reference_refresh_table.c.available.is_(True),
                )
            )
        else:
            ref_stmt = (
                select(func.count())
                .select_from(reference_refresh_table)
                .where(reference_refresh_table.c.available.is_(True))
            )

        total_syms = int(conn.execute(sym_stmt).scalar_one())
        refreshed_syms = int(conn.execute(ref_stmt).scalar_one())
        lsp_edges = int(conn.execute(edge_stmt).scalar_one())
        pct = round((refreshed_syms / total_syms) * 100.0, 1) if total_syms > 0 else 0.0
        coverage = {
            "non_module_symbols": total_syms,
            "refreshed_symbols": refreshed_syms,
            "coverage_pct": pct,
            "lsp_reference_edges": lsp_edges,
        }
        logger.debug(
            "Computed LSP coverage: %d/%d (%.1f%%), %d edges (repo_id=%s)",
            refreshed_syms,
            total_syms,
            pct,
            lsp_edges,
            repo_id,
        )
        return coverage
    except SQLAlchemyError as exc:
        msg = _db_error_text(exc)
        logger.error("Failed querying LSP coverage (repo_id=%s): %s", repo_id, msg)
        raise DatabaseError(
            f"Failed to compute LSP coverage metrics: {msg}",
            context={"repo_id": repo_id},
        ) from exc


def _fetch_latest_run(
    conn: Connection,
    repo_id: str | None,
) -> dict[str, Any] | None:
    """Fetch the most recent indexing run, optionally scoped to ``repo_id``."""
    try:
        runs = list_index_runs(conn, repo_id, limit=1)
        latest = runs[0] if runs else None
        logger.debug(
            "Fetched latest index run: %s (repo_id=%s)",
            latest["id"] if latest else None,
            repo_id,
        )
        return latest
    except SQLAlchemyError as exc:
        msg = _db_error_text(exc)
        logger.error("Failed querying latest index run (repo_id=%s): %s", repo_id, msg)
        raise DatabaseError(
            f"Failed to query latest index run: {msg}",
            context={"repo_id": repo_id},
        ) from exc


def _build_status_payload(
    *,
    db_path: Path,
    db_size_bytes: int,
    user_version: int,
    all_repos: list[RepoRecord],
    selected_repo: RepoRecord | None,
    tables: list[dict[str, Any]],
    edge_counts_by_kind: dict[str, int],
    lsp_coverage: dict[str, Any],
    latest_run: dict[str, Any] | None,
) -> dict[str, Any]:
    """Assemble the database status payload."""
    repo_summary: dict[str, Any] | None = None
    if selected_repo is not None:
        repo_summary = {
            "id": selected_repo.id,
            "name": selected_repo.name,
            "root_path": selected_repo.root_path,
            "last_indexed_at": selected_repo.last_indexed_at,
        }

    return {
        "db_path": str(db_path),
        "size_bytes": db_size_bytes,
        "user_version": user_version,
        "expected_schema_version": SCHEMA_VERSION,
        "schema_ok": user_version == SCHEMA_VERSION,
        "repo_count": len(all_repos),
        "selected_repo": repo_summary,
        "tables": tables,
        "edge_counts_by_kind": edge_counts_by_kind,
        "lsp_coverage": lsp_coverage,
        "latest_run": latest_run,
    }


def cmd_db_status(args: argparse.Namespace) -> int:
    """Display database health, schema version, table counts, edge breakdown, and LSP coverage."""
    db_path = resolve_db_path(args)
    repo_arg: str | None = getattr(args, "repo", None)
    logger.debug("Running db status (db_path=%s, repo=%s)", db_path, repo_arg)

    all_repos: list[RepoRecord] = []
    selected_repo: RepoRecord | None = None
    edge_counts_by_kind: dict[str, int] = {}
    lsp_coverage = _empty_lsp_coverage()
    latest_run: dict[str, Any] | None = None

    with connection_scope(db_path, read_only=True) as conn:
        db_size_bytes = _get_db_size_bytes(db_path)
        user_version = _read_schema_version(conn)
        tables = _list_table_counts(conn)

        if _has_compatible_schema(user_version, tables):
            all_repos, selected_repo = _resolve_status_repo(conn, repo_arg)
            repo_id = selected_repo.id if selected_repo is not None else None
            edge_counts_by_kind = _fetch_edge_counts_by_kind(conn, repo_id)
            lsp_coverage = _fetch_lsp_coverage(conn, repo_id)
            latest_run = _fetch_latest_run(conn, repo_id)

    payload = _build_status_payload(
        db_path=db_path,
        db_size_bytes=db_size_bytes,
        user_version=user_version,
        all_repos=all_repos,
        selected_repo=selected_repo,
        tables=tables,
        edge_counts_by_kind=edge_counts_by_kind,
        lsp_coverage=lsp_coverage,
        latest_run=latest_run,
    )
    print(json.dumps(payload, indent=2))
    return 0


def _count_by_repo(conn: Connection, table: Table) -> dict[str, int]:
    rows = (
        conn.execute(
            select(table.c.repo_id, func.count().label("cnt")).group_by(table.c.repo_id)
        )
        .mappings()
        .all()
    )
    return {str(r["repo_id"]): int(r["cnt"]) for r in rows}


def _collect_repo_summaries(conn: Connection) -> list[dict[str, Any]]:
    """Collect per-repository file, node, and edge counts."""
    try:
        initialize_database(conn)
        repos = list_repos(conn)
        file_counts = _count_by_repo(conn, files_table)
        node_counts = _count_by_repo(conn, nodes_table)
        edge_counts = _count_by_repo(conn, edges_table)
        logger.debug("Collected summaries for %d repositories", len(repos))
    except RepoContextError:
        raise
    except SQLAlchemyError as exc:
        msg = _db_error_text(exc)
        logger.error("Failed collecting repository summaries: %s", msg)
        raise DatabaseError(f"Failed to query repository summaries: {msg}") from exc

    return [
        {
            "id": r.id,
            "name": r.name,
            "root_path": r.root_path,
            "language": (
                r.default_language.value
                if hasattr(r.default_language, "value")
                else str(r.default_language)
            ),
            "files": file_counts.get(r.id, 0),
            "nodes": node_counts.get(r.id, 0),
            "edges": edge_counts.get(r.id, 0),
            "created_at": r.created_at,
            "last_indexed_at": r.last_indexed_at,
        }
        for r in repos
    ]


def cmd_db_repos(args: argparse.Namespace) -> int:
    """List indexed repositories."""
    db_path = resolve_db_path(args)
    logger.debug("Running db repos (db_path=%s)", db_path)

    with connection_scope(db_path, read_only=True) as conn:
        items = _collect_repo_summaries(conn)

    print(json.dumps({"db_path": str(db_path), "repos": items}, indent=2))
    return 0


def cmd_db_tables(args: argparse.Namespace) -> int:
    """List SQLite tables and exact row counts."""
    db_path = resolve_db_path(args)
    logger.debug("Running db tables (db_path=%s)", db_path)

    with connection_scope(db_path, read_only=True) as conn:
        user_version = _read_schema_version(conn)
        tables = _list_table_counts(conn)

    print(
        json.dumps(
            {
                "db_path": str(db_path),
                "user_version": user_version,
                "tables": tables,
            },
            indent=2,
        )
    )
    return 0


def cmd_db_runs(args: argparse.Namespace) -> int:
    """List recent indexing runs."""
    db_path = resolve_db_path(args)
    limit = int(getattr(args, "limit", 20))
    if limit < 1:
        raise ValidationError("--limit must be >= 1", context={"limit": limit})
    repo_arg: str | None = getattr(args, "repo", None)
    logger.debug("Running db runs (db_path=%s, repo=%s, limit=%d)", db_path, repo_arg, limit)

    try:
        with connection_scope(db_path, read_only=True) as conn:
            initialize_database(conn)
            repo_id = resolve_repo_id(conn, repo_arg) if repo_arg else None
            runs = list_index_runs(conn, repo_id=repo_id, limit=limit)
    except RepoContextError:
        raise
    except SQLAlchemyError as exc:
        msg = _db_error_text(exc)
        logger.error("Failed querying index runs (repo=%r): %s", repo_arg, msg)
        raise DatabaseError(
            f"Failed to query index runs: {msg}",
            context={"db_path": str(db_path), "repo": repo_arg},
        ) from exc

    print(json.dumps({"db_path": str(db_path), "repo_id": repo_id, "runs": runs}, indent=2))
    return 0


def cmd_db_sql(args: argparse.Namespace) -> int:
    """Execute a strictly read-only SQL query guarded by SQLite's authorizer."""
    db_path = resolve_db_path(args)
    limit = int(getattr(args, "limit", 100))
    if limit < 1:
        raise ValidationError("--limit must be >= 1", context={"limit": limit})
    query: str = args.query.strip()
    if not query:
        raise ValidationError("SQL query must not be empty.")
    logger.debug("Executing read-only SQL query (db_path=%s, limit=%d)", db_path, limit)

    conn = open_read_only_sql_connection(db_path)
    try:
        result = conn.exec_driver_sql(query)
        if result.returns_rows:
            columns = list(result.keys())
            fetched = result.fetchmany(limit + 1)
            truncated = len(fetched) > limit
            rows = [list(row) for row in fetched[:limit]]
        else:
            columns = []
            truncated = False
            rows = []
    except RepoContextError:
        raise
    except SQLAlchemyError as exc:
        msg = _db_error_text(exc)
        logger.error("Read-only SQL query failed: %s", msg)
        raise DatabaseError(
            f"Read-only SQL query failed: {msg}",
            context={"db_path": str(db_path)},
        ) from exc
    finally:
        close_connection(conn)

    print(
        json.dumps(
            {
                "db_path": str(db_path),
                "columns": columns,
                "row_count": len(rows),
                "truncated": truncated,
                "rows": [dict(zip(columns, row)) for row in rows],
            },
            indent=2,
            default=str,
        )
    )
    return 0
