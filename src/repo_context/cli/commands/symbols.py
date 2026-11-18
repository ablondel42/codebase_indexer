"""Handlers for `rc symbols {search,list,show}` and `rc source` using SQLAlchemy Core."""

from __future__ import annotations

import argparse
import json
from typing import Any

from sqlalchemy import func, or_, select
from sqlalchemy.engine import Connection

from repo_context.cli.utils import (
    _escape_like,
    resolve_db_path,
    resolve_symbol_target,
)
from repo_context.context import build_symbol_context, read_symbol_source
from repo_context.exceptions import SymbolNotFoundError, ValidationError
from repo_context.graph import get_files_by_ids
from repo_context.storage import (
    connection_scope,
    files_table,
    initialize_database,
    nodes_table,
    resolve_repo_id,
    row_to_node,
    search_nodes_fts,
)


def _serialize_symbol_entry(
    node: dict[str, Any],
    file_info: dict[str, Any] | None,
) -> dict[str, Any]:
    """Build clean JSON dict for search/list results."""
    return {
        "id": node["id"],
        "kind": node["kind"],
        "name": node["name"],
        "qualified_name": node["qualified_name"],
        "file_path": file_info.get("file_path") if file_info else None,
        "module_path": file_info.get("module_path") if file_info else None,
        "start_line": node.get("start_line"),
        "end_line": node.get("end_line"),
        "signature": node.get("signature"),
        "doc_summary": node.get("doc_summary"),
    }


def _normalize_file_filter(file_filter: str) -> str:
    """Strip leading ``./`` segments and trailing slashes without mangling dot-directories."""
    cleaned = file_filter.strip()
    while cleaned.startswith("./"):
        cleaned = cleaned.removeprefix("./")
    return cleaned.rstrip("/")


def cmd_symbols_search(args: argparse.Namespace) -> int:
    """Search symbols via FTS5 with ranking boost and LIKE fallback."""
    db_path = resolve_db_path(args)
    limit = int(getattr(args, "limit", 20))
    if limit < 1:
        raise ValidationError("--limit must be >= 1", context={"limit": limit})
    repo_arg: str | None = getattr(args, "repo", None)
    kind: str | None = getattr(args, "kind", None)
    raw_file_filter: str | None = getattr(args, "file", None)
    file_filter = _normalize_file_filter(raw_file_filter) if raw_file_filter is not None else None

    with connection_scope(db_path, read_only=True) as conn:
        initialize_database(conn)
        repo_id = resolve_repo_id(conn, repo_arg)
        nodes = search_nodes_fts(
            conn,
            repo_id,
            args.query,
            kind=kind,
            file_path=file_filter,
            limit=limit,
        )
        files_by_id = get_files_by_ids(conn, [n.get("file_id", "") for n in nodes])

    print(
        json.dumps(
            {
                "repo_id": repo_id,
                "query": args.query,
                "count": len(nodes),
                "symbols": [
                    _serialize_symbol_entry(n, files_by_id.get(n.get("file_id", "")))
                    for n in nodes
                ],
            },
            indent=2,
        )
    )
    return 0


def _build_symbol_list_filters(
    repo_id: str,
    kind: str | None,
    file_filter: str | None,
) -> list[Any]:
    """Build SQLAlchemy WHERE clauses for paginated symbol listing."""
    filters: list[Any] = [nodes_table.c.repo_id == repo_id]
    if kind is not None:
        filters.append(nodes_table.c.kind == kind)
    if file_filter is not None:
        cleaned = _normalize_file_filter(file_filter)
        filters.append(
            or_(
                files_table.c.file_path == cleaned,
                files_table.c.file_path.like(f"{_escape_like(cleaned)}/%", escape="\\"),
            )
        )
    return filters


def _fetch_paginated_symbols(
    conn: Connection,
    filters: list[Any],
    *,
    limit: int,
    offset: int,
) -> tuple[int, list[dict[str, Any]]]:
    """Count matching symbols and fetch one serialized page."""
    joined = nodes_table.join(files_table, files_table.c.id == nodes_table.c.file_id)
    total_count = int(
        conn.execute(select(func.count()).select_from(joined).where(*filters)).scalar_one()
    )
    rows = (
        conn.execute(
            select(
                nodes_table,
                files_table.c.file_path.label("_file_path"),
                files_table.c.module_path.label("_module_path"),
            )
            .select_from(joined)
            .where(*filters)
            .order_by(
                files_table.c.file_path,
                func.coalesce(nodes_table.c.start_line, 0),
                nodes_table.c.kind,
                nodes_table.c.qualified_name,
                nodes_table.c.id,
            )
            .limit(limit)
            .offset(offset)
        )
        .mappings()
        .all()
    )
    symbols = [
        _serialize_symbol_entry(
            row_to_node(r),
            {"file_path": r["_file_path"], "module_path": r["_module_path"]},
        )
        for r in rows
    ]
    return total_count, symbols


def cmd_symbols_list(args: argparse.Namespace) -> int:
    """List symbols with pagination and optional kind/file filters."""
    db_path = resolve_db_path(args)
    limit = int(getattr(args, "limit", 50))
    offset = int(getattr(args, "offset", 0))
    if limit < 1:
        raise ValidationError("--limit must be >= 1", context={"limit": limit})
    if offset < 0:
        raise ValidationError("--offset must be >= 0", context={"offset": offset})

    with connection_scope(db_path, read_only=True) as conn:
        initialize_database(conn)
        repo_id = resolve_repo_id(conn, getattr(args, "repo", None))
        filters = _build_symbol_list_filters(
            repo_id,
            getattr(args, "kind", None),
            getattr(args, "file", None),
        )
        total_count, symbols = _fetch_paginated_symbols(
            conn,
            filters,
            limit=limit,
            offset=offset,
        )

    print(
        json.dumps(
            {
                "repo_id": repo_id,
                "total_count": total_count,
                "returned_count": len(symbols),
                "offset": offset,
                "limit": limit,
                "has_more": (offset + len(symbols)) < total_count,
                "symbols": symbols,
            },
            indent=2,
        )
    )
    return 0


def cmd_symbols_show(args: argparse.Namespace) -> int:
    """Show rich symbol context via ``build_symbol_context``."""
    db_path = resolve_db_path(args)
    repo_arg: str | None = getattr(args, "repo", None)
    include_source = bool(getattr(args, "source", False))
    id_or_name: str = args.id_or_name

    with connection_scope(db_path, read_only=True) as conn:
        initialize_database(conn)
        repo_id = (
            resolve_repo_id(conn, repo_arg)
            if (repo_arg is not None or not id_or_name.strip().startswith("sym:"))
            else None
        )
        target = resolve_symbol_target(conn, repo_id, id_or_name)
        ctx = build_symbol_context(
            conn,
            target["id"],
            include_source=include_source,
            max_source_lines=120,
        )
        if ctx is None:
            raise SymbolNotFoundError(
                f"Symbol not found: {target['id']}",
                context={"symbol": target["id"]},
            )

    print(json.dumps(ctx.model_dump(mode="json"), indent=2))
    return 0


def cmd_source(args: argparse.Namespace) -> int:
    """Display line-numbered source snippet for a symbol via ``read_symbol_source``."""
    db_path = resolve_db_path(args)
    repo_arg: str | None = getattr(args, "repo", None)
    max_lines = int(getattr(args, "max_lines", 120))
    offset = int(getattr(args, "offset", 0))
    id_or_name: str = args.id_or_name

    with connection_scope(db_path, read_only=True) as conn:
        initialize_database(conn)
        repo_id = (
            resolve_repo_id(conn, repo_arg)
            if (repo_arg is not None or not id_or_name.strip().startswith("sym:"))
            else None
        )
        target = resolve_symbol_target(conn, repo_id, id_or_name)
        data = read_symbol_source(
            conn,
            target["id"],
            max_lines=max_lines,
            offset=offset,
        )

    print(json.dumps(data, indent=2))
    return 0
