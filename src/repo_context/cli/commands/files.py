"""Handlers for `rc files` and `rc outline`."""

from __future__ import annotations

import argparse
import fnmatch
from pathlib import Path
from typing import Any

from repo_context.context import build_file_outline
from repo_context.exceptions import ValidationError
from repo_context.graph import check_file_disk_freshness
from repo_context.storage import (
    connection_scope,
    get_repo_by_id,
    initialize_database,
    resolve_repo_id,
)
from repo_context.cli.formatting import (
    format_ascii_table,
    format_outline_tree,
    print_json,
)
from repo_context.cli.utils import format_freshness_label, resolve_db_path


def _matches_file_pattern(file_path: str, module_path: str, pattern: str | None) -> bool:
    """Match a file or module path against an optional glob or substring pattern."""
    if not pattern:
        return True
    cleaned = pattern.strip()
    if not cleaned:
        return True
    if any(ch in cleaned for ch in "*?["):
        return fnmatch.fnmatch(file_path, cleaned) or fnmatch.fnmatch(module_path, cleaned)
    lowered = cleaned.lower()
    return lowered in file_path.lower() or lowered in module_path.lower()


def cmd_files(args: argparse.Namespace) -> int:
    """List indexed files with line count, symbol count, and live disk freshness."""
    db_path = resolve_db_path(args)
    limit = int(getattr(args, "limit", 50))
    if limit < 1:
        raise ValidationError("--limit must be >= 1", context={"limit": limit})
    include_tests = bool(getattr(args, "include_tests", False))
    pattern: str | None = getattr(args, "pattern", None)
    repo_arg: str | None = getattr(args, "repo", None)

    with connection_scope(db_path, read_only=True) as conn:
        initialize_database(conn)
        repo_id = resolve_repo_id(conn, repo_arg)
        repo = get_repo_by_id(conn, repo_id)

        where_sql = "f.repo_id = ?" + ("" if include_tests else " AND f.is_test = 0")
        rows = conn.execute(
            f"""
            SELECT f.id, f.repo_id, f.file_path, f.uri, f.module_path, f.language,
                   f.content_hash, f.size_bytes, f.line_count, f.is_test,
                   f.last_modified_at, f.last_indexed_at,
                   COUNT(n.id) AS symbol_count
            FROM files f
            LEFT JOIN nodes n ON n.file_id = f.id
            WHERE {where_sql}
            GROUP BY f.id
            ORDER BY f.file_path
            """,
            (repo_id,),
        ).fetchall()

        matched: list[dict[str, Any]] = []
        for row in rows:
            item = dict(row)
            item["is_test"] = bool(item["is_test"])
            if _matches_file_pattern(item["file_path"], item["module_path"], pattern):
                matched.append(item)

        total_count = len(matched)
        shown = matched[:limit]
        root_path = repo.root_path if repo is not None else None

        entries: list[dict[str, Any]] = []
        for item in shown:
            is_fresh, stale_reason = check_file_disk_freshness(
                conn,
                item["id"],
                file_record=item,
                repo_root=root_path,
            )
            freshness = format_freshness_label(is_fresh, stale_reason)
            entries.append(
                {
                    "id": item["id"],
                    "file_path": item["file_path"],
                    "module_path": item["module_path"],
                    "line_count": int(item["line_count"]),
                    "symbol_count": int(item["symbol_count"]),
                    "size_bytes": int(item["size_bytes"]),
                    "is_test": item["is_test"],
                    "freshness": freshness,
                    "is_fresh": is_fresh,
                    "stale_reason": stale_reason,
                }
            )

    if getattr(args, "json", False):
        print_json(
            {
                "repo_id": repo_id,
                "total_count": total_count,
                "returned_count": len(entries),
                "files": entries,
            }
        )
        return 0

    print(
        format_ascii_table(
            ["File Path", "Module", "Lines", "Symbols", "Test", "Freshness"],
            [
                [
                    entry["file_path"],
                    entry["module_path"],
                    entry["line_count"],
                    entry["symbol_count"],
                    "yes" if entry["is_test"] else "no",
                    entry["freshness"],
                ]
                for entry in entries
            ],
            max_col_widths=[None, 44, 8, 8, 6, 20],
            alignments=["left", "left", "right", "right", "left", "left"],
            empty_message="(no matching indexed files)",
        )
    )
    if total_count > len(entries):
        print(f"Showing {len(entries)} of {total_count} matching files (use --limit to view more).")
    return 0


def cmd_outline(args: argparse.Namespace) -> int:
    """Display an indented hierarchical symbol tree for an indexed file."""
    db_path = resolve_db_path(args)
    repo_arg: str | None = getattr(args, "repo", None)
    include_private = not bool(getattr(args, "no_private", False))
    max_depth = int(getattr(args, "max_depth", 3))
    file_path_arg: str = args.file_path

    with connection_scope(db_path, read_only=True) as conn:
        initialize_database(conn)
        repo_id = resolve_repo_id(conn, repo_arg)
        repo = get_repo_by_id(conn, repo_id)

        candidate = Path(file_path_arg)
        if candidate.is_absolute() and repo is not None:
            try:
                file_path_arg = candidate.resolve().relative_to(Path(repo.root_path).resolve()).as_posix()
            except ValueError:
                pass

        outline = build_file_outline(
            conn,
            repo_id,
            file_path_arg,
            include_private=include_private,
            max_depth=max_depth,
        )

    if getattr(args, "json", False):
        print_json(outline)
        return 0

    print(format_outline_tree(outline))
    return 0
