"""Graph query layer for symbol, file outline, and edge retrieval using SQLAlchemy Core."""

from __future__ import annotations

from collections.abc import Iterable
from pathlib import Path
from typing import Any

from sqlalchemy import case, func, select
from sqlalchemy.engine import Connection

from repo_context.config import get_config
from repo_context.constants import (
    EDGE_KIND_CALLS,
    EDGE_KIND_IMPORTS,
    EDGE_KIND_INHERITS,
    SYMBOL_KIND_ASYNC_FUNCTION,
    SYMBOL_KIND_ASYNC_METHOD,
    SYMBOL_KIND_CLASS,
    SYMBOL_KIND_FUNCTION,
    SYMBOL_KIND_LOCAL_ASYNC_FUNCTION,
    SYMBOL_KIND_LOCAL_FUNCTION,
    SYMBOL_KIND_METHOD,
    SYMBOL_KIND_MODULE,
)
from repo_context.exceptions import ParseError, PathSecurityError
from repo_context.parsing.hashing import sha256_file
from repo_context.parsing.pathing import ensure_safe_repo_file
from repo_context.storage.edges import (
    list_incoming_edges,
    list_outgoing_edges,
    row_to_edge,
)
from repo_context.storage.files import get_file_by_id, get_file_by_path
from repo_context.storage.nodes import (
    find_nodes_by_name,
    get_node_by_id,
    get_node_by_qualified_name,
    list_child_nodes,
    list_lexical_children,
    list_nodes_for_file,
    row_to_node,
    search_nodes_fts,
)
from repo_context.storage.schema import edges_table, files_table, nodes_table, repos_table

CALLABLE_KINDS = {
    SYMBOL_KIND_FUNCTION,
    SYMBOL_KIND_ASYNC_FUNCTION,
    SYMBOL_KIND_METHOD,
    SYMBOL_KIND_ASYNC_METHOD,
    SYMBOL_KIND_LOCAL_FUNCTION,
    SYMBOL_KIND_LOCAL_ASYNC_FUNCTION,
}

LOCAL_CALLABLE_KINDS = {
    SYMBOL_KIND_LOCAL_FUNCTION,
    SYMBOL_KIND_LOCAL_ASYNC_FUNCTION,
}

_BATCH_SIZE = 400


def get_symbol(conn: Connection, node_id: str) -> dict[str, Any] | None:
    """Get a symbol by its ID."""
    return get_node_by_id(conn, node_id)


def get_symbol_by_qualified_name(
    conn: Connection,
    repo_id: str,
    qualified_name: str,
    kind: str | None = None,
) -> dict[str, Any] | None:
    """Get a symbol by exact qualified name (returns None if missing or ambiguous)."""
    return get_node_by_qualified_name(conn, repo_id, qualified_name, kind)


def find_symbols_by_name(
    conn: Connection,
    repo_id: str,
    name_pattern: str,
    kind: str | None = None,
    limit: int = 50,
) -> list[dict[str, Any]]:
    """Find symbols by name or qualified name using escaped SQL LIKE."""
    return find_nodes_by_name(conn, repo_id, name_pattern, kind, limit)


def search_symbols_fts(
    conn: Connection,
    repo_id: str,
    query: str,
    *,
    kind: str | None = None,
    file_path: str | None = None,
    limit: int = 20,
) -> list[dict[str, Any]]:
    """Search symbol nodes via FTS5 with exact/prefix ranking and LIKE fallback."""
    return search_nodes_fts(
        conn,
        repo_id,
        query,
        kind=kind,
        file_path=file_path,
        limit=limit,
    )


def get_symbols_by_ids(conn: Connection, node_ids: Iterable[str]) -> dict[str, dict[str, Any]]:
    """Batch-load symbols keyed by node ID."""
    unique_ids = list(dict.fromkeys(nid for nid in node_ids if nid))
    if not unique_ids:
        return {}

    result: dict[str, dict[str, Any]] = {}
    for start in range(0, len(unique_ids), _BATCH_SIZE):
        batch = unique_ids[start : start + _BATCH_SIZE]
        rows = (
            conn.execute(select(nodes_table).where(nodes_table.c.id.in_(batch)))
            .mappings()
            .all()
        )
        for row in rows:
            node = row_to_node(row)
            result[node["id"]] = node
    return result


def get_files_by_ids(conn: Connection, file_ids: Iterable[str]) -> dict[str, dict[str, Any]]:
    """Batch-load file metadata rows keyed by file ID."""
    unique_ids = list(dict.fromkeys(fid for fid in file_ids if fid))
    if not unique_ids:
        return {}

    result: dict[str, dict[str, Any]] = {}
    for start in range(0, len(unique_ids), _BATCH_SIZE):
        batch = unique_ids[start : start + _BATCH_SIZE]
        rows = (
            conn.execute(select(files_table).where(files_table.c.id.in_(batch)))
            .mappings()
            .all()
        )
        for row in rows:
            item = dict(row)
            item["is_test"] = bool(item["is_test"])
            result[item["id"]] = item
    return result


def get_parent_symbol(conn: Connection, node: dict[str, Any]) -> dict[str, Any] | None:
    """Get the structural parent symbol of a node."""
    parent_id = node.get("parent_id")
    if parent_id is None:
        return None
    return get_node_by_id(conn, parent_id)


def get_child_symbols(conn: Connection, node_id: str) -> list[dict[str, Any]]:
    """Get structural child symbols of a node ordered by source position."""
    return list_child_nodes(conn, node_id)


def get_lexical_parent_symbol(conn: Connection, node: dict[str, Any]) -> dict[str, Any] | None:
    """Get the lexical parent symbol of a node."""
    lexical_parent_id = node.get("lexical_parent_id")
    if lexical_parent_id is None:
        return None
    return get_node_by_id(conn, lexical_parent_id)


def get_lexical_child_symbols(conn: Connection, node_id: str) -> list[dict[str, Any]]:
    """Get lexical child symbols of a node ordered by source position."""
    return list_lexical_children(conn, node_id)


def get_outgoing_edges(
    conn: Connection,
    node_id: str,
    kind: str | None = None,
) -> list[dict[str, Any]]:
    """Get outgoing edges from a node."""
    return list_outgoing_edges(conn, node_id, kind)


def get_incoming_edges(
    conn: Connection,
    node_id: str,
    kind: str | None = None,
) -> list[dict[str, Any]]:
    """Get incoming edges to a node."""
    return list_incoming_edges(conn, node_id, kind)


def _query_directed_edges(
    conn: Connection,
    node_id: str,
    kind: str,
    *,
    incoming: bool,
    limit: int | None = None,
    offset: int = 0,
) -> list[dict[str, Any]]:
    """Query incoming or outgoing edges of a single kind with optional pagination."""
    if limit is not None and limit <= 0:
        return []
    filter_col = edges_table.c.to_id if incoming else edges_table.c.from_id
    order_col = edges_table.c.from_id if incoming else edges_table.c.to_id

    stmt = (
        select(edges_table)
        .where(filter_col == node_id, edges_table.c.kind == kind)
        .order_by(
            edges_table.c.evidence_uri,
            func.coalesce(
                func.json_extract(edges_table.c.evidence_range_json, "$.start.line"),
                0,
            ),
            func.coalesce(
                func.json_extract(edges_table.c.evidence_range_json, "$.start.character"),
                0,
            ),
            order_col,
            edges_table.c.id,
        )
    )
    if offset > 0:
        stmt = stmt.offset(offset)
    if limit is not None:
        stmt = stmt.limit(limit)

    return [row_to_edge(row) for row in conn.execute(stmt).mappings().all()]


def get_incoming_calls(
    conn: Connection,
    node_id: str,
    *,
    limit: int | None = None,
    offset: int = 0,
) -> list[dict[str, Any]]:
    """List incoming AST ``calls`` edges targeting ``node_id``."""
    return _query_directed_edges(
        conn, node_id, EDGE_KIND_CALLS, incoming=True, limit=limit, offset=offset
    )


def get_outgoing_calls(
    conn: Connection,
    node_id: str,
    *,
    limit: int | None = None,
    offset: int = 0,
) -> list[dict[str, Any]]:
    """List outgoing AST ``calls`` edges originating from ``node_id``."""
    return _query_directed_edges(
        conn, node_id, EDGE_KIND_CALLS, incoming=False, limit=limit, offset=offset
    )


def get_incoming_imports(
    conn: Connection,
    node_id: str,
    *,
    limit: int | None = None,
    offset: int = 0,
) -> list[dict[str, Any]]:
    """List incoming AST ``imports`` edges targeting ``node_id``."""
    return _query_directed_edges(
        conn, node_id, EDGE_KIND_IMPORTS, incoming=True, limit=limit, offset=offset
    )


def get_outgoing_imports(
    conn: Connection,
    node_id: str,
    *,
    limit: int | None = None,
    offset: int = 0,
) -> list[dict[str, Any]]:
    """List outgoing AST ``imports`` edges originating from ``node_id``."""
    return _query_directed_edges(
        conn, node_id, EDGE_KIND_IMPORTS, incoming=False, limit=limit, offset=offset
    )


def get_incoming_inherits(
    conn: Connection,
    node_id: str,
    *,
    limit: int | None = None,
    offset: int = 0,
) -> list[dict[str, Any]]:
    """List incoming AST ``inherits`` edges (subclasses of ``node_id``)."""
    return _query_directed_edges(
        conn, node_id, EDGE_KIND_INHERITS, incoming=True, limit=limit, offset=offset
    )


def get_outgoing_inherits(
    conn: Connection,
    node_id: str,
    *,
    limit: int | None = None,
    offset: int = 0,
) -> list[dict[str, Any]]:
    """List outgoing AST ``inherits`` edges (base classes of ``node_id``)."""
    return _query_directed_edges(
        conn, node_id, EDGE_KIND_INHERITS, incoming=False, limit=limit, offset=offset
    )


def get_symbols_for_file(conn: Connection, file_id: str) -> list[dict[str, Any]]:
    """Get all symbols for a specific file ordered by source start_line."""
    return list_nodes_for_file(conn, file_id)


def get_file_for_outline(
    conn: Connection,
    repo_id: str,
    file_path: str,
) -> dict[str, Any] | None:
    """Resolve a tracked file record by repo ID and repo-relative POSIX path."""
    normalized = file_path.strip()
    if normalized.startswith("./"):
        normalized = normalized[2:]
    record = get_file_by_path(conn, repo_id, normalized)
    if record is None and normalized != file_path.strip():
        record = get_file_by_path(conn, repo_id, file_path.strip())
    return record


def list_file_outline_symbols(conn: Connection, file_id: str) -> list[dict[str, Any]]:
    """List symbol nodes for a file outline ordered by 1-based start_line."""
    return list_nodes_for_file(conn, file_id)


def list_file_import_edges(conn: Connection, file_id: str) -> list[dict[str, Any]]:
    """List import edges recorded in a specific file ordered by line."""
    stmt = (
        select(edges_table)
        .where(
            edges_table.c.evidence_file_id == file_id,
            edges_table.c.kind == EDGE_KIND_IMPORTS,
        )
        .order_by(
            func.coalesce(
                func.json_extract(edges_table.c.evidence_range_json, "$.start.line"),
                0,
            ),
            func.coalesce(
                func.json_extract(edges_table.c.evidence_range_json, "$.start.character"),
                0,
            ),
            edges_table.c.id,
        )
    )
    return [row_to_edge(row) for row in conn.execute(stmt).mappings().all()]


def check_file_disk_freshness(
    conn: Connection | None,
    file_id: str,
    *,
    file_record: dict[str, Any] | None = None,
    repo_root: str | Path | None = None,
) -> tuple[bool, str | None]:
    """Verify indexed file freshness against disk existence, confinement, and SHA-256 hash."""
    record = file_record
    if record is None and conn is not None:
        record = get_file_by_id(conn, file_id)
    if record is None:
        return False, "file_record_missing"

    raw_root = repo_root
    if raw_root is None and conn is not None:
        raw_root = conn.execute(
            select(repos_table.c.root_path).where(repos_table.c.id == record["repo_id"]).limit(1)
        ).scalar_one_or_none()

    if not raw_root:
        return False, "repo_record_missing"

    root_path = Path(raw_root)
    if not root_path.is_dir():
        return False, "repo_root_missing"

    rel_path = record["file_path"]
    try:
        safe_path = ensure_safe_repo_file(root_path, rel_path)
    except PathSecurityError:
        candidate = root_path / rel_path
        if not candidate.exists() and not candidate.is_symlink():
            return False, "file_missing"
        return False, "path_security_violation"

    try:
        max_bytes = get_config().max_file_size_bytes
        disk_hash = sha256_file(safe_path, max_file_size_bytes=max_bytes)
    except (OSError, ParseError):
        return False, "file_unreadable"

    if disk_hash != record.get("content_hash"):
        return False, "content_hash_mismatch"

    return True, None


def get_repo_graph_stats(conn: Connection, repo_id: str) -> dict[str, Any]:
    """Get graph and file statistics for a repository."""
    kind_rows = (
        conn.execute(
            select(nodes_table.c.kind, func.count().label("count"))
            .where(nodes_table.c.repo_id == repo_id)
            .group_by(nodes_table.c.kind)
        )
        .mappings()
        .all()
    )
    kind_counts = {str(row["kind"]): int(row["count"]) for row in kind_rows}
    node_count = sum(kind_counts.values())

    edge_kind_rows = (
        conn.execute(
            select(edges_table.c.kind, func.count().label("count"))
            .where(edges_table.c.repo_id == repo_id)
            .group_by(edges_table.c.kind)
        )
        .mappings()
        .all()
    )
    edge_kind_counts = {str(row["kind"]): int(row["count"]) for row in edge_kind_rows}
    edge_count = sum(edge_kind_counts.values())

    file_stats = (
        conn.execute(
            select(
                func.count().label("file_count"),
                func.coalesce(
                    func.sum(case((files_table.c.is_test.is_(True), 1), else_=0)),
                    0,
                ).label("test_file_count"),
                func.coalesce(func.sum(files_table.c.line_count), 0).label("total_lines"),
            )
            .select_from(files_table)
            .where(files_table.c.repo_id == repo_id)
        )
        .mappings()
        .first()
    )

    callable_count = sum(kind_counts.get(k, 0) for k in CALLABLE_KINDS)
    local_callable_count = sum(kind_counts.get(k, 0) for k in LOCAL_CALLABLE_KINDS)

    return {
        "repo_id": repo_id,
        "file_count": int(file_stats["file_count"]) if file_stats else 0,
        "test_file_count": int(file_stats["test_file_count"]) if file_stats else 0,
        "total_lines": int(file_stats["total_lines"]) if file_stats else 0,
        "node_count": node_count,
        "edge_count": edge_count,
        "module_count": kind_counts.get(SYMBOL_KIND_MODULE, 0),
        "class_count": kind_counts.get(SYMBOL_KIND_CLASS, 0),
        "callable_count": callable_count,
        "local_callable_count": local_callable_count,
        "kind_counts": kind_counts,
        "edge_kind_counts": edge_kind_counts,
    }
