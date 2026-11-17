"""Handlers for `rc db {status,repos,tables,runs,sql}`."""

from __future__ import annotations

import argparse
from contextlib import closing
import sqlite3
from typing import Any

from repo_context.exceptions import ValidationError
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
from repo_context.cli.formatting import (
    format_ascii_table,
    format_bytes,
    format_key_value_section,
    print_json,
)
from repo_context.cli.utils import resolve_db_path


def _quote_identifier(identifier: str) -> str:
    """Safely double-quote a SQLite table identifier."""
    return '"' + identifier.replace('"', '""') + '"'


def _list_table_counts(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    """Return exact row counts for all user tables and FTS virtual tables."""
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
        name = row["name"]
        count_row = conn.execute(f"SELECT COUNT(*) FROM {_quote_identifier(name)}").fetchone()
        tables.append({"table": name, "row_count": int(count_row[0]) if count_row else 0})
    return tables


def cmd_db_status(args: argparse.Namespace) -> int:
    """Display database health, schema version, table counts, edge breakdown, and LSP coverage."""
    db_path = resolve_db_path(args)
    repo_arg: str | None = getattr(args, "repo", None)

    with connection_scope(db_path, read_only=True) as conn:
        db_size_bytes = db_path.stat().st_size
        user_version = int(conn.execute("PRAGMA user_version").fetchone()[0])
        tables = _list_table_counts(conn)
        table_names = {t["table"] for t in tables}

        selected_repo = None
        all_repos: list[Any] = []
        edge_counts_by_kind: dict[str, int] = {}
        lsp_coverage: dict[str, Any] = {
            "non_module_symbols": 0,
            "refreshed_symbols": 0,
            "coverage_pct": 0.0,
            "lsp_reference_edges": 0,
        }
        latest_run: dict[str, Any] | None = None

        if user_version == SCHEMA_VERSION and {"repos", "nodes", "edges", "index_runs", "reference_refresh"} <= table_names:
            all_repos = list_repos(conn)
            selected_repo_id: str | None = None
            if repo_arg:
                selected_repo_id = resolve_repo_id(conn, repo_arg)
                selected_repo = get_repo_by_id(conn, selected_repo_id)
            elif len(all_repos) == 1:
                selected_repo_id = resolve_repo_id(conn, None)
                selected_repo = get_repo_by_id(conn, selected_repo_id)

            if selected_repo_id is not None:
                edge_rows = conn.execute(
                    "SELECT kind, COUNT(*) AS cnt FROM edges WHERE repo_id = ? GROUP BY kind ORDER BY cnt DESC, kind ASC",
                    (selected_repo_id,),
                ).fetchall()
                sym_row = conn.execute(
                    "SELECT COUNT(*) FROM nodes WHERE repo_id = ? AND kind != 'module'",
                    (selected_repo_id,),
                ).fetchone()
                refreshed_row = conn.execute(
                    """
                    SELECT COUNT(*)
                    FROM reference_refresh rr
                    JOIN nodes n ON n.id = rr.target_symbol_id
                    WHERE n.repo_id = ? AND rr.available = 1
                    """,
                    (selected_repo_id,),
                ).fetchone()
                lsp_edge_row = conn.execute(
                    "SELECT COUNT(*) FROM edges WHERE repo_id = ? AND kind = 'references'",
                    (selected_repo_id,),
                ).fetchone()
                runs = list_index_runs(conn, selected_repo_id, limit=1)
            else:
                edge_rows = conn.execute(
                    "SELECT kind, COUNT(*) AS cnt FROM edges GROUP BY kind ORDER BY cnt DESC, kind ASC"
                ).fetchall()
                sym_row = conn.execute(
                    "SELECT COUNT(*) FROM nodes WHERE kind != 'module'"
                ).fetchone()
                refreshed_row = conn.execute(
                    "SELECT COUNT(*) FROM reference_refresh WHERE available = 1"
                ).fetchone()
                lsp_edge_row = conn.execute(
                    "SELECT COUNT(*) FROM edges WHERE kind = 'references'"
                ).fetchone()
                runs = list_index_runs(conn, None, limit=1)

            edge_counts_by_kind = {r["kind"]: int(r["cnt"]) for r in edge_rows}
            total_syms = int(sym_row[0]) if sym_row else 0
            refreshed_syms = int(refreshed_row[0]) if refreshed_row else 0
            lsp_edges = int(lsp_edge_row[0]) if lsp_edge_row else 0
            pct = round((refreshed_syms / total_syms) * 100.0, 1) if total_syms > 0 else 0.0
            lsp_coverage = {
                "non_module_symbols": total_syms,
                "refreshed_symbols": refreshed_syms,
                "coverage_pct": pct,
                "lsp_reference_edges": lsp_edges,
            }
            latest_run = runs[0] if runs else None

    repo_summary: dict[str, Any] | None = None
    if selected_repo is not None:
        repo_summary = {
            "id": selected_repo.id,
            "name": selected_repo.name,
            "root_path": selected_repo.root_path,
            "last_indexed_at": selected_repo.last_indexed_at,
        }

    payload: dict[str, Any] = {
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

    if getattr(args, "json", False):
        print_json(payload)
        return 0

    sections: list[str] = []
    overview_items: list[tuple[str, Any]] = [
        ("Database", str(db_path)),
        ("Size", f"{format_bytes(db_size_bytes)} ({db_size_bytes} bytes)"),
        ("Schema Version", f"v{user_version} (expected v{SCHEMA_VERSION})"),
        ("Indexed Repos", len(all_repos)),
    ]
    if selected_repo is not None:
        overview_items.extend(
            [
                ("Active Repo", f"{selected_repo.name} ({selected_repo.id})"),
                ("Root Path", selected_repo.root_path),
                ("Last Indexed", selected_repo.last_indexed_at or "-"),
            ]
        )
    elif user_version != SCHEMA_VERSION:
        overview_items.append(("Notice", "Schema mismatch; run `rc index` to upgrade."))

    overview_items.extend(
        [
            (
                "LSP Coverage",
                f"{lsp_coverage['refreshed_symbols']}/{lsp_coverage['non_module_symbols']} symbols "
                f"({lsp_coverage['coverage_pct']}%), {lsp_coverage['lsp_reference_edges']} reference edges",
            ),
        ]
    )
    sections.append(format_key_value_section("Database Status", overview_items))

    sections.append("")
    sections.append("Table Row Counts")
    sections.append("----------------")
    sections.append(
        format_ascii_table(
            ["Table", "Rows"],
            [[t["table"], t["row_count"]] for t in tables],
            alignments=["left", "right"],
        )
    )

    sections.append("")
    sections.append("Edges by Kind")
    sections.append("-------------")
    sections.append(
        format_ascii_table(
            ["Kind", "Count"],
            [[kind, count] for kind, count in edge_counts_by_kind.items()],
            alignments=["left", "right"],
        )
    )

    sections.append("")
    if latest_run is not None:
        sections.append(
            format_key_value_section(
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
        )
    else:
        sections.append("Latest Index Run\n----------------\n  (none)")

    print("\n".join(sections))
    return 0


def cmd_db_repos(args: argparse.Namespace) -> int:
    """List indexed repositories."""
    db_path = resolve_db_path(args)

    with connection_scope(db_path, read_only=True) as conn:
        initialize_database(conn)
        repos = list_repos(conn)
        file_counts = {
            r["repo_id"]: int(r["cnt"])
            for r in conn.execute("SELECT repo_id, COUNT(*) AS cnt FROM files GROUP BY repo_id").fetchall()
        }
        node_counts = {
            r["repo_id"]: int(r["cnt"])
            for r in conn.execute("SELECT repo_id, COUNT(*) AS cnt FROM nodes GROUP BY repo_id").fetchall()
        }
        edge_counts = {
            r["repo_id"]: int(r["cnt"])
            for r in conn.execute("SELECT repo_id, COUNT(*) AS cnt FROM edges GROUP BY repo_id").fetchall()
        }

    items = [
        {
            "id": r.id,
            "name": r.name,
            "root_path": r.root_path,
            "language": r.default_language.value if hasattr(r.default_language, "value") else str(r.default_language),
            "files": file_counts.get(r.id, 0),
            "nodes": node_counts.get(r.id, 0),
            "edges": edge_counts.get(r.id, 0),
            "created_at": r.created_at,
            "last_indexed_at": r.last_indexed_at,
        }
        for r in repos
    ]

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

    with connection_scope(db_path, read_only=True) as conn:
        user_version = int(conn.execute("PRAGMA user_version").fetchone()[0])
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

    with connection_scope(db_path, read_only=True) as conn:
        initialize_database(conn)
        repo_id = resolve_repo_id(conn, repo_arg) if repo_arg else None
        runs = list_index_runs(conn, repo_id=repo_id, limit=limit)

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

    with closing(open_read_only_sql_connection(db_path)) as conn:
        cursor = conn.execute(query)
        columns = [desc[0] for desc in cursor.description] if cursor.description else []
        fetched = cursor.fetchmany(limit + 1) if cursor.description else []
        truncated = len(fetched) > limit
        rows = [list(row) for row in fetched[:limit]]

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
