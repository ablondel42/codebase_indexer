"""Handlers for `rc symbols {search,list,show}` and `rc source`."""

from __future__ import annotations

import argparse
import json
from typing import Any

from repo_context.context import build_symbol_context, read_symbol_source
from repo_context.exceptions import SymbolNotFoundError, ValidationError
from repo_context.graph import get_files_by_ids
from repo_context.storage import (
    connection_scope,
    initialize_database,
    resolve_repo_id,
    search_nodes_fts,
)
from repo_context.storage.nodes import row_to_node
from repo_context.cli.utils import (
    _escape_like,
    resolve_db_path,
    resolve_symbol_target,
)


def _serialize_symbol_entry(node: dict[str, Any], file_info: dict[str, Any] | None) -> dict[str, Any]:
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


def cmd_symbols_list(args: argparse.Namespace) -> int:
    """List symbols with pagination and optional kind/file filters."""
    db_path = resolve_db_path(args)
    limit = int(getattr(args, "limit", 50))
    offset = int(getattr(args, "offset", 0))
    if limit < 1:
        raise ValidationError("--limit must be >= 1", context={"limit": limit})
    if offset < 0:
        raise ValidationError("--offset must be >= 0", context={"offset": offset})

    repo_arg: str | None = getattr(args, "repo", None)
    kind: str | None = getattr(args, "kind", None)
    file_filter: str | None = getattr(args, "file", None)

    with connection_scope(db_path, read_only=True) as conn:
        initialize_database(conn)
        repo_id = resolve_repo_id(conn, repo_arg)

        where_clauses = ["n.repo_id = ?"]
        params: list[Any] = [repo_id]
        if kind is not None:
            where_clauses.append("n.kind = ?")
            params.append(kind)
        if file_filter is not None:
            cleaned = _normalize_file_filter(file_filter)
            where_clauses.append("(f.file_path = ? OR f.file_path LIKE ? ESCAPE '\\')")
            params.extend([cleaned, f"{_escape_like(cleaned)}/%"])

        where_sql = " AND ".join(where_clauses)
        total_count = int(
            conn.execute(
                f"""
                SELECT COUNT(*)
                FROM nodes n
                JOIN files f ON f.id = n.file_id
                WHERE {where_sql}
                """,
                tuple(params),
            ).fetchone()[0]
        )

        rows = conn.execute(
            f"""
            SELECT n.id, n.repo_id, n.file_id, n.language, n.kind, n.name, n.qualified_name, n.uri,
                   n.range_json, n.selection_range_json, n.start_line, n.end_line, n.signature,
                   n.parent_id, n.visibility_hint, n.doc_summary, n.content_hash, n.semantic_hash,
                   n.source, n.confidence, n.payload_json, n.scope, n.lexical_parent_id, n.last_indexed_at,
                   f.file_path AS _file_path, f.module_path AS _module_path
            FROM nodes n
            JOIN files f ON f.id = n.file_id
            WHERE {where_sql}
            ORDER BY f.file_path, COALESCE(n.start_line, 0), n.kind, n.qualified_name, n.id
            LIMIT ? OFFSET ?
            """,
            (*params, limit, offset),
        ).fetchall()

        nodes = [row_to_node(r) for r in rows]
        file_meta = {
            r["id"]: {"file_path": r["_file_path"], "module_path": r["_module_path"]}
            for r in rows
        }

    print(
        json.dumps(
            {
                "repo_id": repo_id,
                "total_count": total_count,
                "returned_count": len(nodes),
                "offset": offset,
                "limit": limit,
                "has_more": (offset + len(nodes)) < total_count,
                "symbols": [
                    _serialize_symbol_entry(n, file_meta.get(n["id"]))
                    for n in nodes
                ],
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
