"""Handlers for `rc db {status,repos,tables,runs,sql}`."""

from __future__ import annotations

import argparse
from contextlib import closing
from pathlib import Path
import sqlite3
from typing import Any

from repo_context.cli.formatting import (
    format_ascii_table,
    format_bytes,
    format_key_value_section,
    print_json,
)
from repo_context.cli.utils import resolve_db_path
from repo_context.exceptions import DatabaseError, RepoContextError, ValidationError
from repo_context.logging_config import get_logger
from repo_context.models.repo import RepoRecord
from repo_context.storage import (
    SCHEMA_VERSION,
    connection_scope,
    get_repo_by_id,
    initialize_database,
    list_index_runs,
    list_repos,
    open_read_only_sql_connection,
    resolve_repo_id,
)

logger = get_logger("cli.db")

_REQUIRED_STATUS_TABLES = frozenset(
    {"repos", "nodes", "edges", "index_runs", "reference_refresh"}
)


def _quote_identifier(identifier: str) -> str:
    """Safely double-quote a SQLite table identifier."""
    return '"' + identifier.replace('"', '""') + '"'


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


def _read_schema_version(conn: sqlite3.Connection) -> int:
    """Read ``PRAGMA user_version`` from an open SQLite connection."""
    try:
        row = conn.execute("PRAGMA user_version").fetchone()
        version = int(row[0]) if row else 0
        logger.debug("Read SQLite PRAGMA user_version=%d", version)
        return version
    except sqlite3.Error as exc:
        logger.error("Failed reading SQLite PRAGMA user_version: %s", exc)
        raise DatabaseError(f"Failed to read SQLite schema version: {exc}") from exc


def _list_table_counts(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    """Return exact row counts for all user tables and FTS virtual tables."""
    current_table: str | None = None
    try:
        rows = conn.execute(
            """
            SELECT name
            FROM sqlite_master
            WHERE type = 'table'
              AND name NOT LIKE 'sqlite_%'
              AND name NOT LIKE 'nodes_fts_%'
            ORDER BY name
            """
        ).fetchall()
        tables: list[dict[str, Any]] = []
        for row in rows:
            current_table = str(row["name"])
            count_row = conn.execute(
                f"SELECT COUNT(*) FROM {_quote_identifier(current_table)}"
            ).fetchone()
            tables.append(
                {
                    "table": current_table,
                    "row_count": int(count_row[0]) if count_row else 0,
                }
            )
        logger.debug("Collected row counts for %d tables", len(tables))
        return tables
    except sqlite3.Error as exc:
        logger.error(
            "Failed reading table row counts (table=%s): %s",
            current_table or "<sqlite_master>",
            exc,
        )
        raise DatabaseError(
            f"Failed to read table row counts: {exc}",
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
    conn: sqlite3.Connection,
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
    except sqlite3.Error as exc:
        logger.error("Database failure resolving repository (repo=%r): %s", repo_arg, exc)
        raise DatabaseError(
            f"Failed to resolve repository from database: {exc}",
            context={"repo": repo_arg},
        ) from exc


def _fetch_edge_counts_by_kind(
    conn: sqlite3.Connection,
    repo_id: str | None,
) -> dict[str, int]:
    """Query edge counts grouped by ``kind``, optionally scoped to ``repo_id``."""
    try:
        if repo_id is not None:
            rows = conn.execute(
                "SELECT kind, COUNT(*) AS cnt FROM edges "
                "WHERE repo_id = ? GROUP BY kind ORDER BY cnt DESC, kind ASC",
                (repo_id,),
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT kind, COUNT(*) AS cnt FROM edges "
                "GROUP BY kind ORDER BY cnt DESC, kind ASC"
            ).fetchall()
        counts = {str(r["kind"]): int(r["cnt"]) for r in rows}
        logger.debug(
            "Fetched edge counts across %d kinds (repo_id=%s)",
            len(counts),
            repo_id,
        )
        return counts
    except sqlite3.Error as exc:
        logger.error("Failed querying edge counts (repo_id=%s): %s", repo_id, exc)
        raise DatabaseError(
            f"Failed to query edge counts by kind: {exc}",
            context={"repo_id": repo_id},
        ) from exc


def _fetch_lsp_coverage(
    conn: sqlite3.Connection,
    repo_id: str | None,
) -> dict[str, Any]:
    """Compute non-module symbol LSP refresh coverage, optionally scoped to ``repo_id``."""
    try:
        if repo_id is not None:
            sym_row = conn.execute(
                "SELECT COUNT(*) FROM nodes WHERE repo_id = ? AND kind != 'module'",
                (repo_id,),
            ).fetchone()
            refreshed_row = conn.execute(
                """
                SELECT COUNT(*)
                FROM reference_refresh rr
                JOIN nodes n ON n.id = rr.target_symbol_id
                WHERE n.repo_id = ? AND rr.available = 1
                """,
                (repo_id,),
            ).fetchone()
            lsp_edge_row = conn.execute(
                "SELECT COUNT(*) FROM edges WHERE repo_id = ? AND kind = 'references'",
                (repo_id,),
            ).fetchone()
        else:
            sym_row = conn.execute(
                "SELECT COUNT(*) FROM nodes WHERE kind != 'module'"
            ).fetchone()
            refreshed_row = conn.execute(
                "SELECT COUNT(*) FROM reference_refresh WHERE available = 1"
            ).fetchone()
            lsp_edge_row = conn.execute(
                "SELECT COUNT(*) FROM edges WHERE kind = 'references'"
            ).fetchone()

        total_syms = int(sym_row[0]) if sym_row else 0
        refreshed_syms = int(refreshed_row[0]) if refreshed_row else 0
        lsp_edges = int(lsp_edge_row[0]) if lsp_edge_row else 0
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
    except sqlite3.Error as exc:
        logger.error("Failed querying LSP coverage (repo_id=%s): %s", repo_id, exc)
        raise DatabaseError(
            f"Failed to compute LSP coverage metrics: {exc}",
            context={"repo_id": repo_id},
        ) from exc


def _fetch_latest_run(
    conn: sqlite3.Connection,
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
    except sqlite3.Error as exc:
        logger.error("Failed querying latest index run (repo_id=%s): %s", repo_id, exc)
        raise DatabaseError(
            f"Failed to query latest index run: {exc}",
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
    """Assemble the unified status payload used for JSON and ASCII output."""
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


def _format_status_overview(payload: dict[str, Any]) -> str:
    """Format the top-level ``Database Status`` key-value section."""
    size_bytes = payload["size_bytes"]
    user_version = payload["user_version"]
    expected_version = payload["expected_schema_version"]
    selected_repo = payload["selected_repo"]
    lsp_coverage = payload["lsp_coverage"]

    overview_items: list[tuple[str, Any]] = [
        ("Database", payload["db_path"]),
        ("Size", f"{format_bytes(size_bytes)} ({size_bytes} bytes)"),
        ("Schema Version", f"v{user_version} (expected v{expected_version})"),
        ("Indexed Repos", payload["repo_count"]),
    ]
    if selected_repo is not None:
        overview_items.extend(
            [
                ("Active Repo", f"{selected_repo['name']} ({selected_repo['id']})"),
                ("Root Path", selected_repo["root_path"]),
                ("Last Indexed", selected_repo["last_indexed_at"] or "-"),
            ]
        )
    elif not payload["schema_ok"]:
        overview_items.append(("Notice", "Schema mismatch; run `rc index` to upgrade."))

    overview_items.append(
        (
            "LSP Coverage",
            f"{lsp_coverage['refreshed_symbols']}/{lsp_coverage['non_module_symbols']} symbols "
            f"({lsp_coverage['coverage_pct']}%), {lsp_coverage['lsp_reference_edges']} reference edges",
        )
    )
    return format_key_value_section("Database Status", overview_items)


def _format_latest_run_section(latest_run: dict[str, Any] | None) -> str:
    """Format the ``Latest Index Run`` section."""
    if latest_run is None:
        return "Latest Index Run\n----------------\n  (none)"

    return format_key_value_section(
        "Latest Index Run",
        [
            ("Run ID", latest_run["id"]),
            ("Repo ID", latest_run["repo_id"]),
            ("Type", latest_run["run_type"]),
            ("Status", latest_run["status"]),
            ("Files Scanned", latest_run["files_scanned"]),
            ("Nodes Extracted", latest_run["nodes_extracted"]),
            ("Edges Extracted", latest_run["edges_extracted"]),
            ("Started", latest_run["started_at"]),
            ("Completed", latest_run["completed_at"] or "-"),
            ("Error", latest_run["error_message"] or "-"),
        ],
    )


def _format_status_report(payload: dict[str, Any]) -> str:
    """Render the full multi-section ASCII ``rc db status`` report."""
    tables = payload["tables"]
    edge_counts = payload["edge_counts_by_kind"]

    sections = [
        _format_status_overview(payload),
        "",
        "Table Row Counts",
        "----------------",
        format_ascii_table(
            ["Table", "Rows"],
            [[t["table"], t["row_count"]] for t in tables],
            alignments=["left", "right"],
        ),
        "",
        "Edges by Kind",
        "-------------",
        format_ascii_table(
            ["Kind", "Count"],
            [[kind, count] for kind, count in edge_counts.items()],
            alignments=["left", "right"],
        ),
        "",
        _format_latest_run_section(payload["latest_run"]),
    ]
    return "\n".join(sections)


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

    if getattr(args, "json", False):
        print_json(payload)
        return 0

    print(_format_status_report(payload))
    return 0


def _collect_repo_summaries(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    """Collect per-repository file, node, and edge counts."""
    try:
        initialize_database(conn)
        repos = list_repos(conn)
        file_counts = {
            r["repo_id"]: int(r["cnt"])
            for r in conn.execute(
                "SELECT repo_id, COUNT(*) AS cnt FROM files GROUP BY repo_id"
            ).fetchall()
        }
        node_counts = {
            r["repo_id"]: int(r["cnt"])
            for r in conn.execute(
                "SELECT repo_id, COUNT(*) AS cnt FROM nodes GROUP BY repo_id"
            ).fetchall()
        }
        edge_counts = {
            r["repo_id"]: int(r["cnt"])
            for r in conn.execute(
                "SELECT repo_id, COUNT(*) AS cnt FROM edges GROUP BY repo_id"
            ).fetchall()
        }
        logger.debug("Collected summaries for %d repositories", len(repos))
    except RepoContextError:
        raise
    except sqlite3.Error as exc:
        logger.error("Failed collecting repository summaries: %s", exc)
        raise DatabaseError(f"Failed to query repository summaries: {exc}") from exc

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

    if getattr(args, "json", False):
        print_json({"db_path": str(db_path), "repos": items})
        return 0

    print(
        format_ascii_table(
            ["ID", "Name", "Root Path", "Files", "Nodes", "Edges", "Last Indexed"],
            [
                [
                    item["id"],
                    item["name"],
                    item["root_path"],
                    item["files"],
                    item["nodes"],
                    item["edges"],
                    item["last_indexed_at"] or "-",
                ]
                for item in items
            ],
            max_col_widths=[None, 24, None, 8, 8, 8, 28],
            alignments=["left", "left", "left", "right", "right", "right", "left"],
            empty_message="(no repositories indexed)",
        )
    )
    return 0


def cmd_db_tables(args: argparse.Namespace) -> int:
    """List SQLite tables and exact row counts."""
    db_path = resolve_db_path(args)
    logger.debug("Running db tables (db_path=%s)", db_path)

    with connection_scope(db_path, read_only=True) as conn:
        user_version = _read_schema_version(conn)
        tables = _list_table_counts(conn)

    if getattr(args, "json", False):
        print_json(
            {
                "db_path": str(db_path),
                "user_version": user_version,
                "tables": tables,
            }
        )
        return 0

    print(f"Database: {db_path} (user_version={user_version})")
    print(
        format_ascii_table(
            ["Table", "Rows"],
            [[t["table"], t["row_count"]] for t in tables],
            alignments=["left", "right"],
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
    except sqlite3.Error as exc:
        logger.error("Failed querying index runs (repo=%r): %s", repo_arg, exc)
        raise DatabaseError(
            f"Failed to query index runs: {exc}",
            context={"db_path": str(db_path), "repo": repo_arg},
        ) from exc

    if getattr(args, "json", False):
        print_json({"db_path": str(db_path), "repo_id": repo_id, "runs": runs})
        return 0

    print(
        format_ascii_table(
            ["Run ID", "Repo ID", "Type", "Status", "Files", "Nodes", "Edges", "Started", "Completed", "Error"],
            [
                [
                    r["id"],
                    r["repo_id"],
                    r["run_type"],
                    r["status"],
                    r["files_scanned"],
                    r["nodes_extracted"],
                    r["edges_extracted"],
                    r["started_at"],
                    r["completed_at"] or "-",
                    r["error_message"] or "-",
                ]
                for r in runs
            ],
            max_col_widths=[None, None, 12, 12, 7, 7, 7, 26, 26, 36],
            alignments=["left", "left", "left", "left", "right", "right", "right", "left", "left", "left"],
            empty_message="(no index runs recorded)",
        )
    )
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

    try:
        with closing(open_read_only_sql_connection(db_path)) as conn:
            cursor = conn.execute(query)
            columns = [desc[0] for desc in cursor.description] if cursor.description else []
            fetched = cursor.fetchmany(limit + 1) if cursor.description else []
            truncated = len(fetched) > limit
            rows = [list(row) for row in fetched[:limit]]
    except RepoContextError:
        raise
    except sqlite3.Error as exc:
        logger.error("Read-only SQL query failed: %s", exc)
        raise DatabaseError(
            f"Read-only SQL query failed: {exc}",
            context={"db_path": str(db_path)},
        ) from exc

    if getattr(args, "json", False):
        print_json(
            {
                "db_path": str(db_path),
                "columns": columns,
                "row_count": len(rows),
                "truncated": truncated,
                "rows": [dict(zip(columns, row)) for row in rows],
            }
        )
        return 0

    if not columns:
        print("(query executed; 0 columns returned)")
        return 0

    print(
        format_ascii_table(
            columns,
            rows,
            max_col_widths=[60] * len(columns),
            empty_message="(0 rows)",
        )
    )
    if truncated:
        print(f"(truncated to --limit {limit} rows)")
    return 0
