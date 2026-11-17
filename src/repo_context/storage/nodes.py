"""Node persistence and FTS5/LIKE search helpers."""

import json
import re
import sqlite3
from typing import Any

from repo_context.exceptions import DatabaseError, ValidationError
from repo_context.logging_config import get_logger
from repo_context.models.node import SymbolNode

logger = get_logger("storage.nodes")

_NODE_COLUMNS = (
    "id, repo_id, file_id, language, kind, name, qualified_name, uri, "
    "range_json, selection_range_json, start_line, end_line, signature, "
    "parent_id, visibility_hint, doc_summary, content_hash, semantic_hash, "
    "source, confidence, payload_json, scope, lexical_parent_id, last_indexed_at"
)

_UPSERT_NODE_SQL = """
INSERT INTO nodes (
    id, repo_id, file_id, language, kind, name, qualified_name, uri,
    range_json, selection_range_json, start_line, end_line, signature,
    parent_id, visibility_hint, doc_summary, content_hash, semantic_hash,
    source, confidence, payload_json, scope, lexical_parent_id, last_indexed_at
) VALUES (
    :id, :repo_id, :file_id, :language, :kind, :name, :qualified_name, :uri,
    :range_json, :selection_range_json, :start_line, :end_line, :signature,
    :parent_id, :visibility_hint, :doc_summary, :content_hash, :semantic_hash,
    :source, :confidence, :payload_json, :scope, :lexical_parent_id, :last_indexed_at
)
ON CONFLICT(id) DO UPDATE SET
    repo_id = excluded.repo_id,
    file_id = excluded.file_id,
    language = excluded.language,
    kind = excluded.kind,
    name = excluded.name,
    qualified_name = excluded.qualified_name,
    uri = excluded.uri,
    range_json = excluded.range_json,
    selection_range_json = excluded.selection_range_json,
    start_line = excluded.start_line,
    end_line = excluded.end_line,
    signature = excluded.signature,
    parent_id = excluded.parent_id,
    visibility_hint = excluded.visibility_hint,
    doc_summary = excluded.doc_summary,
    content_hash = excluded.content_hash,
    semantic_hash = excluded.semantic_hash,
    source = excluded.source,
    confidence = excluded.confidence,
    payload_json = excluded.payload_json,
    scope = excluded.scope,
    lexical_parent_id = excluded.lexical_parent_id,
    last_indexed_at = excluded.last_indexed_at
"""

_SPECIAL_FTS_CHARS_RE = re.compile(r"[^A-Za-z0-9_.\s]")


def _ensure_json_string(value: Any, *, default: str | None = None) -> str | None:
    """Return an existing JSON string unchanged or serialize non-string values."""
    if value is None:
        return default
    if isinstance(value, str):
        return value
    return json.dumps(value, sort_keys=True)


def _deserialize_json(value: str | None) -> Any:
    """Deserialize a JSON string to a Python object."""
    if value is None:
        return None
    return json.loads(value)


def _escape_like(value: str) -> str:
    """Escape SQLite LIKE metacharacters (\\, %, _) for ``ESCAPE '\\'``."""
    return value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def _validate_and_normalize_node(node: dict | SymbolNode) -> SymbolNode:
    """Validate and normalize a node input."""
    if isinstance(node, SymbolNode):
        return node
    try:
        return SymbolNode(**node)
    except Exception as exc:
        logger.error(
            "Node validation failed",
            extra={
                "node_id": node.get("id") if isinstance(node, dict) else None,
                "error": str(exc),
            },
        )
        raise ValidationError(f"Invalid node data: {exc}") from exc


def node_to_row(node: dict) -> dict:
    """Convert node dictionary to database row values without double JSON encoding."""
    return {
        "id": node["id"],
        "repo_id": node["repo_id"],
        "file_id": node["file_id"],
        "language": node["language"],
        "kind": node["kind"],
        "name": node["name"],
        "qualified_name": node["qualified_name"],
        "uri": node["uri"],
        "range_json": _ensure_json_string(node.get("range_json"), default=None),
        "selection_range_json": _ensure_json_string(node.get("selection_range_json"), default=None),
        "start_line": node.get("start_line"),
        "end_line": node.get("end_line"),
        "signature": node.get("signature"),
        "parent_id": node.get("parent_id"),
        "visibility_hint": node.get("visibility_hint"),
        "doc_summary": node.get("doc_summary"),
        "content_hash": node["content_hash"],
        "semantic_hash": node["semantic_hash"],
        "source": node["source"],
        "confidence": node["confidence"],
        "payload_json": _ensure_json_string(node.get("payload_json"), default="{}"),
        "scope": node.get("scope"),
        "lexical_parent_id": node.get("lexical_parent_id"),
        "last_indexed_at": node.get("last_indexed_at"),
    }


def row_to_node(row: sqlite3.Row) -> dict:
    """Convert a SQLite row to a node dictionary with deserialized JSON fields."""
    return {
        "id": row["id"],
        "repo_id": row["repo_id"],
        "file_id": row["file_id"],
        "language": row["language"],
        "kind": row["kind"],
        "name": row["name"],
        "qualified_name": row["qualified_name"],
        "uri": row["uri"],
        "range_json": _deserialize_json(row["range_json"]),
        "selection_range_json": _deserialize_json(row["selection_range_json"]),
        "start_line": row["start_line"],
        "end_line": row["end_line"],
        "signature": row["signature"],
        "parent_id": row["parent_id"],
        "visibility_hint": row["visibility_hint"],
        "doc_summary": row["doc_summary"],
        "content_hash": row["content_hash"],
        "semantic_hash": row["semantic_hash"],
        "source": row["source"],
        "confidence": row["confidence"],
        "payload_json": _deserialize_json(row["payload_json"]),
        "scope": row["scope"],
        "lexical_parent_id": row["lexical_parent_id"],
        "last_indexed_at": row["last_indexed_at"],
    }


def upsert_node(conn: sqlite3.Connection, node: dict | SymbolNode) -> None:
    """Insert or update a single node."""
    validated = _validate_and_normalize_node(node)
    logger.debug("Upserting node", extra={"node_id": validated.id})
    row = node_to_row(validated.model_dump())
    try:
        conn.execute(_UPSERT_NODE_SQL, row)
    except sqlite3.Error as exc:
        logger.error(
            "Database error upserting node",
            extra={"node_id": validated.id, "error": str(exc)},
        )
        raise DatabaseError(f"Failed to upsert node {validated.id}: {exc}") from exc


def upsert_nodes(conn: sqlite3.Connection, nodes: list[dict | SymbolNode]) -> None:
    """Insert or update multiple nodes in a single batch."""
    if not nodes:
        return
    rows = [node_to_row(_validate_and_normalize_node(node).model_dump()) for node in nodes]
    try:
        conn.executemany(_UPSERT_NODE_SQL, rows)
    except sqlite3.Error as exc:
        logger.error("Database error upserting nodes batch", extra={"error": str(exc)})
        raise DatabaseError(f"Failed to upsert {len(rows)} nodes: {exc}") from exc


def get_node_by_id(conn: sqlite3.Connection, node_id: str) -> dict | None:
    """Get a node by its ID."""
    row = conn.execute(
        f"SELECT {_NODE_COLUMNS} FROM nodes WHERE id = ?",
        (node_id,),
    ).fetchone()
    if row is None:
        return None
    return row_to_node(row)


def get_node_by_qualified_name(
    conn: sqlite3.Connection,
    repo_id: str,
    qualified_name: str,
    kind: str | None = None,
) -> dict | None:
    """Get a node by exact qualified name (returns None if missing or ambiguous)."""
    if kind is not None:
        rows = conn.execute(
            f"""
            SELECT {_NODE_COLUMNS}
            FROM nodes
            WHERE repo_id = ? AND qualified_name = ? AND kind = ?
            """,
            (repo_id, qualified_name, kind),
        ).fetchall()
    else:
        rows = conn.execute(
            f"""
            SELECT {_NODE_COLUMNS}
            FROM nodes
            WHERE repo_id = ? AND qualified_name = ?
            """,
            (repo_id, qualified_name),
        ).fetchall()

    if len(rows) != 1:
        return None
    return row_to_node(rows[0])


def list_nodes_for_file(conn: sqlite3.Connection, file_id: str) -> list[dict]:
    """List all nodes for a specific file ordered by source start_line."""
    rows = conn.execute(
        f"""
        SELECT {_NODE_COLUMNS}
        FROM nodes
        WHERE file_id = ?
        ORDER BY COALESCE(start_line, 0), kind, qualified_name, id
        """,
        (file_id,),
    ).fetchall()
    return [row_to_node(row) for row in rows]


def list_nodes_for_repo(conn: sqlite3.Connection, repo_id: str) -> list[dict]:
    """List all nodes for a specific repository ordered by kind, qualified_name, id."""
    rows = conn.execute(
        f"""
        SELECT {_NODE_COLUMNS}
        FROM nodes
        WHERE repo_id = ?
        ORDER BY kind, qualified_name, id
        """,
        (repo_id,),
    ).fetchall()
    return [row_to_node(row) for row in rows]


def find_nodes_by_name(
    conn: sqlite3.Connection,
    repo_id: str,
    name_pattern: str,
    kind: str | None = None,
    limit: int = 50,
) -> list[dict]:
    """Find nodes by name or qualified name using safely escaped SQL LIKE."""
    if not name_pattern or limit <= 0:
        return []
    like_pattern = f"%{_escape_like(name_pattern)}%"
    params: list[Any] = [repo_id, like_pattern, like_pattern]
    kind_clause = ""
    if kind is not None:
        kind_clause = " AND kind = ?"
        params.append(kind)
    params.append(limit)

    rows = conn.execute(
        f"""
        SELECT {_NODE_COLUMNS}
        FROM nodes
        WHERE repo_id = ?
          AND (name LIKE ? ESCAPE '\\' OR qualified_name LIKE ? ESCAPE '\\')
          {kind_clause}
        ORDER BY kind, qualified_name, id
        LIMIT ?
        """,
        params,
    ).fetchall()
    return [row_to_node(row) for row in rows]


def _build_fts_match_expression(raw_query: str) -> str | None:
    """Build a safely quoted FTS5 MATCH query or return None if special tokens require LIKE."""
    if _SPECIAL_FTS_CHARS_RE.search(raw_query):
        return None
    tokens = [tok.strip(".") for tok in raw_query.split()]
    tokens = [tok for tok in tokens if tok and any(ch.isalnum() or ch == "_" for ch in tok)]
    if not tokens:
        return None
    quoted_terms = []
    for token in tokens:
        escaped = token.replace('"', '""')
        quoted_terms.append(f'"{escaped}"*')
    return " ".join(quoted_terms)


def _search_nodes_like_fallback(
    conn: sqlite3.Connection,
    repo_id: str,
    raw_query: str,
    *,
    kind: str | None = None,
    file_path: str | None = None,
    limit: int = 20,
) -> list[dict]:
    """Fallback escaped LIKE search when query uses special tokens or FTS has no hits."""
    escaped = _escape_like(raw_query)
    sub_pattern = f"%{escaped}%"
    prefix_pattern = f"{escaped}%"

    join_sql = "LEFT JOIN files f ON f.id = n.file_id"
    where_clauses = [
        "n.repo_id = ?",
        (
            "(n.name LIKE ? ESCAPE '\\' "
            "OR n.qualified_name LIKE ? ESCAPE '\\' "
            "OR COALESCE(n.signature, '') LIKE ? ESCAPE '\\' "
            "OR COALESCE(n.doc_summary, '') LIKE ? ESCAPE '\\')"
        ),
    ]
    params: list[Any] = [repo_id, sub_pattern, sub_pattern, sub_pattern, sub_pattern]

    if kind is not None:
        where_clauses.append("n.kind = ?")
        params.append(kind)
    if file_path is not None:
        join_sql = "JOIN files f ON f.id = n.file_id"
        fp_clean = file_path.rstrip("/")
        where_clauses.append("(f.file_path = ? OR f.file_path LIKE ? ESCAPE '\\')")
        params.extend([file_path, f"{_escape_like(fp_clean)}/%"])

    order_params = [raw_query, raw_query, prefix_pattern, prefix_pattern]
    node_cols = ", ".join(f"n.{col.strip()}" for col in _NODE_COLUMNS.split(","))
    sql = f"""
        SELECT {node_cols}
        FROM nodes n
        {join_sql}
        WHERE {' AND '.join(where_clauses)}
        ORDER BY
            CASE
                WHEN LOWER(n.name) = LOWER(?) THEN 0
                WHEN LOWER(n.qualified_name) = LOWER(?) THEN 1
                WHEN LOWER(n.name) LIKE LOWER(?) ESCAPE '\\' THEN 2
                WHEN LOWER(n.qualified_name) LIKE LOWER(?) ESCAPE '\\' THEN 3
                ELSE 4
            END ASC,
            n.kind ASC,
            n.qualified_name ASC,
            n.id ASC
        LIMIT ?
    """
    rows = conn.execute(sql, [*params, *order_params, limit]).fetchall()
    return [row_to_node(row) for row in rows]


def search_nodes_fts(
    conn: sqlite3.Connection,
    repo_id: str,
    query: str,
    *,
    kind: str | None = None,
    file_path: str | None = None,
    limit: int = 20,
) -> list[dict]:
    """Search symbol nodes via FTS5 with exact/prefix ranking boost and safe LIKE fallback."""
    if not query or not query.strip() or limit <= 0:
        return []

    raw_query = query.strip()
    fts_expr = _build_fts_match_expression(raw_query)
    if fts_expr is None:
        return _search_nodes_like_fallback(
            conn, repo_id, raw_query, kind=kind, file_path=file_path, limit=limit
        )

    prefix_pattern = f"{_escape_like(raw_query)}%"
    join_files = "LEFT JOIN files f ON f.id = n.file_id"
    where_clauses = ["nodes_fts MATCH ?", "n.repo_id = ?"]
    params: list[Any] = [fts_expr, repo_id]

    if kind is not None:
        where_clauses.append("n.kind = ?")
        params.append(kind)
    if file_path is not None:
        join_files = "JOIN files f ON f.id = n.file_id"
        fp_clean = file_path.rstrip("/")
        where_clauses.append("(f.file_path = ? OR f.file_path LIKE ? ESCAPE '\\')")
        params.extend([file_path, f"{_escape_like(fp_clean)}/%"])

    order_params = [raw_query, raw_query, prefix_pattern, prefix_pattern]
    node_cols = ", ".join(f"n.{col.strip()}" for col in _NODE_COLUMNS.split(","))
    sql = f"""
        SELECT {node_cols}
        FROM nodes_fts
        JOIN nodes n ON n.rowid = nodes_fts.rowid
        {join_files}
        WHERE {' AND '.join(where_clauses)}
        ORDER BY
            CASE
                WHEN LOWER(n.name) = LOWER(?) THEN 0
                WHEN LOWER(n.qualified_name) = LOWER(?) THEN 1
                WHEN LOWER(n.name) LIKE LOWER(?) ESCAPE '\\' THEN 2
                WHEN LOWER(n.qualified_name) LIKE LOWER(?) ESCAPE '\\' THEN 3
                ELSE 4
            END ASC,
            bm25(nodes_fts, 0.0, 0.0, 10.0, 8.0, 3.0, 1.0) ASC,
            n.kind ASC,
            n.qualified_name ASC,
            n.id ASC
        LIMIT ?
    """
    try:
        rows = conn.execute(sql, [*params, *order_params, limit]).fetchall()
    except sqlite3.OperationalError:
        return _search_nodes_like_fallback(
            conn, repo_id, raw_query, kind=kind, file_path=file_path, limit=limit
        )

    if not rows:
        return _search_nodes_like_fallback(
            conn, repo_id, raw_query, kind=kind, file_path=file_path, limit=limit
        )
    return [row_to_node(row) for row in rows]


def list_child_nodes(conn: sqlite3.Connection, parent_id: str) -> list[dict]:
    """List structural child nodes by ``parent_id``."""
    rows = conn.execute(
        f"""
        SELECT {_NODE_COLUMNS}
        FROM nodes
        WHERE parent_id = ?
        ORDER BY COALESCE(start_line, 0), kind, name, id
        """,
        (parent_id,),
    ).fetchall()
    return [row_to_node(row) for row in rows]


def list_lexical_children(conn: sqlite3.Connection, lexical_parent_id: str) -> list[dict]:
    """List lexical child nodes by ``lexical_parent_id``."""
    rows = conn.execute(
        f"""
        SELECT {_NODE_COLUMNS}
        FROM nodes
        WHERE lexical_parent_id = ?
        ORDER BY COALESCE(start_line, 0), kind, name, id
        """,
        (lexical_parent_id,),
    ).fetchall()
    return [row_to_node(row) for row in rows]


def delete_nodes_for_file(conn: sqlite3.Connection, file_id: str) -> None:
    """Delete all nodes owned by a specific file."""
    conn.execute("DELETE FROM nodes WHERE file_id = ?", (file_id,))
