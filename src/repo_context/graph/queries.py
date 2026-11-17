"""Graph query layer for symbol, file outline, and edge retrieval."""

from collections.abc import Iterable
from pathlib import Path
import sqlite3

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

_EDGE_SELECT_COLUMNS = (
    "id, repo_id, kind, from_id, to_id, source, confidence, "
    "evidence_file_id, evidence_uri, evidence_range_json, payload_json, last_indexed_at"
)

_NODE_SELECT_COLUMNS = (
    "id, repo_id, file_id, language, kind, name, qualified_name, uri, "
    "range_json, selection_range_json, start_line, end_line, signature, "
    "parent_id, visibility_hint, doc_summary, content_hash, semantic_hash, "
    "source, confidence, payload_json, scope, lexical_parent_id, last_indexed_at"
)

_BATCH_SIZE = 400


def _limit_offset_clause(limit: int | None, offset: int) -> tuple[str, tuple[int, ...]]:
    """Build SQLite LIMIT/OFFSET clause safely."""
    if limit is not None:
        return " LIMIT ? OFFSET ?", (int(limit), max(0, int(offset)))
    if offset > 0:
        return " LIMIT -1 OFFSET ?", (int(offset),)
    return "", ()


def get_symbol(conn: sqlite3.Connection, node_id: str) -> dict | None:
    """Get a symbol by its ID."""
    return get_node_by_id(conn, node_id)


def get_symbol_by_qualified_name(
    conn: sqlite3.Connection,
    repo_id: str,
    qualified_name: str,
    kind: str | None = None,
) -> dict | None:
    """Get a symbol by exact qualified name (returns None if missing or ambiguous)."""
    return get_node_by_qualified_name(conn, repo_id, qualified_name, kind)


def find_symbols_by_name(
    conn: sqlite3.Connection,
    repo_id: str,
    name_pattern: str,
    kind: str | None = None,
    limit: int = 50,
) -> list[dict]:
    """Find symbols by name or qualified name using escaped SQL LIKE."""
    return find_nodes_by_name(conn, repo_id, name_pattern, kind, limit)


def search_symbols_fts(
    conn: sqlite3.Connection,
    repo_id: str,
    query: str,
    *,
    kind: str | None = None,
    file_path: str | None = None,
    limit: int = 20,
) -> list[dict]:
    """Search symbol nodes via FTS5 with exact/prefix ranking and LIKE fallback."""
    return search_nodes_fts(
        conn,
        repo_id,
        query,
        kind=kind,
        file_path=file_path,
        limit=limit,
    )


def get_symbols_by_ids(conn: sqlite3.Connection, node_ids: Iterable[str]) -> dict[str, dict]:
    """Batch-load symbols keyed by node ID."""
    unique_ids = list(dict.fromkeys(nid for nid in node_ids if nid))
    if not unique_ids:
        return {}

    result: dict[str, dict] = {}
    for start in range(0, len(unique_ids), _BATCH_SIZE):
        batch = unique_ids[start : start + _BATCH_SIZE]
        placeholders = ",".join("?" * len(batch))
        rows = conn.execute(
            f"SELECT {_NODE_SELECT_COLUMNS} FROM nodes WHERE id IN ({placeholders})",
            tuple(batch),
        ).fetchall()
        for row in rows:
            node = row_to_node(row)
            result[node["id"]] = node
    return result


def get_files_by_ids(conn: sqlite3.Connection, file_ids: Iterable[str]) -> dict[str, dict]:
    """Batch-load file metadata rows keyed by file ID."""
    unique_ids = list(dict.fromkeys(fid for fid in file_ids if fid))
    if not unique_ids:
        return {}

    result: dict[str, dict] = {}
    for start in range(0, len(unique_ids), _BATCH_SIZE):
        batch = unique_ids[start : start + _BATCH_SIZE]
        placeholders = ",".join("?" * len(batch))
        rows = conn.execute(
            f"""
            SELECT id, repo_id, file_path, uri, module_path, language,
                   content_hash, size_bytes, line_count, is_test,
                   last_modified_at, last_indexed_at
            FROM files
            WHERE id IN ({placeholders})
            """,
            tuple(batch),
        ).fetchall()
        for row in rows:
            item = dict(row)
            item["is_test"] = bool(item["is_test"])
            result[item["id"]] = item
    return result


def get_parent_symbol(conn: sqlite3.Connection, node: dict) -> dict | None:
    """Get the structural parent symbol of a node."""
    parent_id = node.get("parent_id")
    if parent_id is None:
        return None
    return get_node_by_id(conn, parent_id)


def get_child_symbols(conn: sqlite3.Connection, node_id: str) -> list[dict]:
    """Get structural child symbols of a node ordered by source position."""
    return list_child_nodes(conn, node_id)


def get_lexical_parent_symbol(conn: sqlite3.Connection, node: dict) -> dict | None:
    """Get the lexical parent symbol of a node."""
    lexical_parent_id = node.get("lexical_parent_id")
    if lexical_parent_id is None:
        return None
    return get_node_by_id(conn, lexical_parent_id)


def get_lexical_child_symbols(conn: sqlite3.Connection, node_id: str) -> list[dict]:
    """Get lexical child symbols of a node ordered by source position."""
    return list_lexical_children(conn, node_id)


def get_outgoing_edges(
    conn: sqlite3.Connection,
    node_id: str,
    kind: str | None = None,
) -> list[dict]:
    """Get outgoing edges from a node."""
    return list_outgoing_edges(conn, node_id, kind)


def get_incoming_edges(
    conn: sqlite3.Connection,
    node_id: str,
    kind: str | None = None,
) -> list[dict]:
    """Get incoming edges to a node."""
    return list_incoming_edges(conn, node_id, kind)


def _query_directed_edges(
    conn: sqlite3.Connection,
    node_id: str,
    kind: str,
    *,
    incoming: bool,
    limit: int | None = None,
    offset: int = 0,
) -> list[dict]:
    """Query incoming or outgoing edges of a single kind with optional pagination."""
    if limit is not None and limit <= 0:
        return []
    column = "to_id" if incoming else "from_id"
    order_col = "from_id" if incoming else "to_id"
    limit_sql, limit_params = _limit_offset_clause(limit, offset)
    rows = conn.execute(
        f"""
        SELECT {_EDGE_SELECT_COLUMNS}
        FROM edges
        WHERE {column} = ? AND kind = ?
        ORDER BY evidence_uri,
                 COALESCE(json_extract(evidence_range_json, '$.start.line'), 0),
                 COALESCE(json_extract(evidence_range_json, '$.start.character'), 0),
                 {order_col},
                 id
        {limit_sql}
        """,
        (node_id, kind, *limit_params),
    ).fetchall()
    return [row_to_edge(row) for row in rows]


def get_incoming_calls(
    conn: sqlite3.Connection,
    node_id: str,
    *,
    limit: int | None = None,
    offset: int = 0,
) -> list[dict]:
    """List incoming AST ``calls`` edges targeting ``node_id``."""
    return _query_directed_edges(
        conn, node_id, EDGE_KIND_CALLS, incoming=True, limit=limit, offset=offset
    )


def get_outgoing_calls(
    conn: sqlite3.Connection,
    node_id: str,
    *,
    limit: int | None = None,
    offset: int = 0,
) -> list[dict]:
    """List outgoing AST ``calls`` edges originating from ``node_id``."""
    return _query_directed_edges(
        conn, node_id, EDGE_KIND_CALLS, incoming=False, limit=limit, offset=offset
    )


def get_incoming_imports(
    conn: sqlite3.Connection,
    node_id: str,
    *,
    limit: int | None = None,
    offset: int = 0,
) -> list[dict]:
    """List incoming AST ``imports`` edges targeting ``node_id``."""
    return _query_directed_edges(
        conn, node_id, EDGE_KIND_IMPORTS, incoming=True, limit=limit, offset=offset
    )


def get_outgoing_imports(
    conn: sqlite3.Connection,
    node_id: str,
    *,
    limit: int | None = None,
    offset: int = 0,
) -> list[dict]:
    """List outgoing AST ``imports`` edges originating from ``node_id``."""
    return _query_directed_edges(
        conn, node_id, EDGE_KIND_IMPORTS, incoming=False, limit=limit, offset=offset
    )


def get_incoming_inherits(
    conn: sqlite3.Connection,
    node_id: str,
    *,
    limit: int | None = None,
    offset: int = 0,
) -> list[dict]:
    """List incoming AST ``inherits`` edges (subclasses of ``node_id``)."""
    return _query_directed_edges(
        conn, node_id, EDGE_KIND_INHERITS, incoming=True, limit=limit, offset=offset
    )


def get_outgoing_inherits(
    conn: sqlite3.Connection,
    node_id: str,
    *,
    limit: int | None = None,
    offset: int = 0,
) -> list[dict]:
    """List outgoing AST ``inherits`` edges (base classes of ``node_id``)."""
    return _query_directed_edges(
        conn, node_id, EDGE_KIND_INHERITS, incoming=False, limit=limit, offset=offset
    )


def get_symbols_for_file(conn: sqlite3.Connection, file_id: str) -> list[dict]:
    """Get all symbols for a specific file ordered by source start_line."""
    return list_nodes_for_file(conn, file_id)


def get_file_for_outline(
    conn: sqlite3.Connection,
    repo_id: str,
    file_path: str,
) -> dict | None:
    """Resolve a tracked file record by repo ID and repo-relative POSIX path."""
    normalized = file_path.strip()
    if normalized.startswith("./"):
        normalized = normalized[2:]
    record = get_file_by_path(conn, repo_id, normalized)
    if record is None and normalized != file_path.strip():
        record = get_file_by_path(conn, repo_id, file_path.strip())
    return record


def list_file_outline_symbols(conn: sqlite3.Connection, file_id: str) -> list[dict]:
    """List symbol nodes for a file outline ordered by 1-based start_line."""
    return list_nodes_for_file(conn, file_id)


def list_file_import_edges(conn: sqlite3.Connection, file_id: str) -> list[dict]:
    """List import edges recorded in a specific file ordered by line."""
    rows = conn.execute(
        f"""
        SELECT {_EDGE_SELECT_COLUMNS}
        FROM edges
        WHERE evidence_file_id = ? AND kind = ?
        ORDER BY COALESCE(json_extract(evidence_range_json, '$.start.line'), 0),
                 COALESCE(json_extract(evidence_range_json, '$.start.character'), 0),
                 id
        """,
        (file_id, EDGE_KIND_IMPORTS),
    ).fetchall()
    return [row_to_edge(row) for row in rows]


def check_file_disk_freshness(
    conn: sqlite3.Connection | None,
    file_id: str,
    *,
    file_record: dict | None = None,
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
        repo_row = conn.execute(
            "SELECT root_path FROM repos WHERE id = ? LIMIT 1",
            (record["repo_id"],),
        ).fetchone()
        if repo_row is not None:
            raw_root = repo_row["root_path"]

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


def get_repo_graph_stats(conn: sqlite3.Connection, repo_id: str) -> dict:
    """Get graph and file statistics for a repository."""
    kind_rows = conn.execute(
        """
        SELECT kind, COUNT(*) AS count
        FROM nodes
        WHERE repo_id = ?
        GROUP BY kind
        """,
        (repo_id,),
    ).fetchall()
    kind_counts = {row["kind"]: int(row["count"]) for row in kind_rows}
    node_count = sum(kind_counts.values())

    edge_kind_rows = conn.execute(
        """
        SELECT kind, COUNT(*) AS count
        FROM edges
        WHERE repo_id = ?
        GROUP BY kind
        """,
        (repo_id,),
    ).fetchall()
    edge_kind_counts = {row["kind"]: int(row["count"]) for row in edge_kind_rows}
    edge_count = sum(edge_kind_counts.values())

    file_stats = conn.execute(
        """
        SELECT COUNT(*) AS file_count,
               COALESCE(SUM(CASE WHEN is_test != 0 THEN 1 ELSE 0 END), 0) AS test_file_count,
               COALESCE(SUM(line_count), 0) AS total_lines
        FROM files
        WHERE repo_id = ?
        """,
        (repo_id,),
    ).fetchone()

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
