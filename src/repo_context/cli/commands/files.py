"""Handlers for `rc files` and `rc outline` using SQLAlchemy Core."""

from __future__ import annotations

import argparse
import fnmatch
import json
from pathlib import Path
from typing import Any

from sqlalchemy import func, select

from repo_context.cli.utils import format_freshness_label, resolve_db_path
from repo_context.context import build_file_outline
from repo_context.exceptions import ValidationError
from repo_context.graph import check_file_disk_freshness
from repo_context.storage import (
    connection_scope,
    files_table,
    get_repo_by_id,
    initialize_database,
    nodes_table,
    resolve_repo_id,
)


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

        stmt = (
            select(
                files_table,
                func.count(nodes_table.c.id).label("symbol_count"),
            )
            .select_from(
                files_table.outerjoin(nodes_table, nodes_table.c.file_id == files_table.c.id)
            )
            .where(files_table.c.repo_id == repo_id)
        )
        if not include_tests:
            stmt = stmt.where(files_table.c.is_test.is_(False))
        stmt = stmt.group_by(files_table.c.id).order_by(files_table.c.file_path)

        rows = conn.execute(stmt).mappings().all()

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

    print(
        json.dumps(
            {
                "repo_id": repo_id,
                "total_count": total_count,
                "returned_count": len(entries),
                "files": entries,
            },
            indent=2,
        )
    )
    return 0


def cmd_outline(args: argparse.Namespace) -> int:
    """Display hierarchical symbol outline for an indexed file."""
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
                file_path_arg = (
                    candidate.resolve().relative_to(Path(repo.root_path).resolve()).as_posix()
                )
            except ValueError:
                pass

        outline = build_file_outline(
            conn,
            repo_id,
            file_path_arg,
            include_private=include_private,
            max_depth=max_depth,
        )

    print(json.dumps(outline, indent=2))
    return 0
