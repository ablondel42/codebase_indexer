"""Node persistence and FTS5/LIKE search helpers using SQLAlchemy Core."""

from __future__ import annotations

import json
import re
from collections.abc import Mapping, Sequence
from typing import Any

from sqlalchemy import case, delete, func, literal_column, or_, select
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.engine import Connection, RowMapping
from sqlalchemy.exc import OperationalError, SQLAlchemyError

from repo_context.exceptions import DatabaseError, ValidationError
from repo_context.logging_config import get_logger
from repo_context.models.node import SymbolNode
from repo_context.storage.schema import files_table, nodes_fts_table, nodes_table

logger = get_logger("storage.nodes")

_SPECIAL_FTS_CHARS_RE = re.compile(r"[^A-Za-z0-9_.\s]")


def _ensure_json_string(value: Any, *, default: str | None = None) -> str | None:
    """Return an existing JSON string unchanged or serialize non-string values."""
    if value is None:
        return default
    if isinstance(value, str):
        return value
    return json.dumps(value, sort_keys=True)


def _deserialize_json(value: Any) -> Any:
    """Deserialize a JSON string if needed; pass already-decoded Python objects through."""
    if value is None or not isinstance(value, str):
        return value
    return json.loads(value)


def _escape_like(value: str) -> str:
    """Escape SQLite LIKE metacharacters (\\, %, _) for ``ESCAPE '\\'``."""
    return value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def _validate_and_normalize_node(node: Mapping[str, Any] | SymbolNode) -> SymbolNode:
    """Validate and normalize a node input."""
    if isinstance(node, SymbolNode):
        return node
    try:
        return SymbolNode.model_validate(dict(node))
    except Exception as exc:
        logger.error(
            "Node validation failed",
            extra={
                "node_id": node.get("id") if isinstance(node, Mapping) else None,
                "error": str(exc),
            },
        )
        raise ValidationError(f"Invalid node data: {exc}") from exc


def node_to_row(node: dict[str, Any]) -> dict[str, Any]:
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


def row_to_node(row: RowMapping | Mapping[str, Any]) -> dict[str, Any]:
    """Convert a database mapping row to a node dictionary with decoded JSON fields."""
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


def _build_upsert_node_stmt() -> Any:
    base = sqlite_insert(nodes_table)
    return base.on_conflict_do_update(
        index_elements=[nodes_table.c.id],
        set_={
            col.name: base.excluded[col.name]
            for col in nodes_table.columns
            if col.name != "id"
        },
    )


def upsert_node(conn: Connection, node: Mapping[str, Any] | SymbolNode) -> None:
    """Insert or update a single node."""
    validated = _validate_and_normalize_node(node)
    logger.debug("Upserting node", extra={"node_id": validated.id})
    row = node_to_row(validated.model_dump())
    try:
        conn.execute(_build_upsert_node_stmt(), row)
    except SQLAlchemyError as exc:
        logger.error(
            "Database error upserting node",
            extra={"node_id": validated.id, "error": str(exc)},
        )
        raise DatabaseError(f"Failed to upsert node {validated.id}: {exc}") from exc


def upsert_nodes(conn: Connection, nodes: Sequence[Mapping[str, Any] | SymbolNode]) -> None:
    """Insert or update multiple nodes in a single batch."""
    if not nodes:
        return
    rows = [node_to_row(_validate_and_normalize_node(node).model_dump()) for node in nodes]
    try:
        conn.execute(_build_upsert_node_stmt(), rows)
    except SQLAlchemyError as exc:
        logger.error("Database error upserting nodes batch", extra={"error": str(exc)})
        raise DatabaseError(f"Failed to upsert {len(rows)} nodes: {exc}") from exc


def get_node_by_id(conn: Connection, node_id: str) -> dict[str, Any] | None:
    """Get a node by its ID."""
    row = conn.execute(select(nodes_table).where(nodes_table.c.id == node_id)).mappings().first()
    if row is None:
        return None
    return row_to_node(row)


def get_node_by_qualified_name(
    conn: Connection,
    repo_id: str,
    qualified_name: str,
    kind: str | None = None,
) -> dict[str, Any] | None:
    """Get a node by exact qualified name (returns None if missing or ambiguous)."""
    stmt = select(nodes_table).where(
        nodes_table.c.repo_id == repo_id,
        nodes_table.c.qualified_name == qualified_name,
    )
    if kind is not None:
        stmt = stmt.where(nodes_table.c.kind == kind)
    rows = conn.execute(stmt).mappings().all()
    if len(rows) != 1:
        return None
    return row_to_node(rows[0])


def list_nodes_for_file(conn: Connection, file_id: str) -> list[dict[str, Any]]:
    """List all nodes for a specific file ordered by source start_line."""
    stmt = (
        select(nodes_table)
        .where(nodes_table.c.file_id == file_id)
        .order_by(
            func.coalesce(nodes_table.c.start_line, 0),
            nodes_table.c.kind,
            nodes_table.c.qualified_name,
            nodes_table.c.id,
        )
    )
    return [row_to_node(row) for row in conn.execute(stmt).mappings().all()]


def list_nodes_for_repo(conn: Connection, repo_id: str) -> list[dict[str, Any]]:
    """List all nodes for a specific repository ordered by kind, qualified_name, id."""
    stmt = (
        select(nodes_table)
        .where(nodes_table.c.repo_id == repo_id)
        .order_by(nodes_table.c.kind, nodes_table.c.qualified_name, nodes_table.c.id)
    )
    return [row_to_node(row) for row in conn.execute(stmt).mappings().all()]


def find_nodes_by_name(
    conn: Connection,
    repo_id: str,
    name_pattern: str,
    kind: str | None = None,
    limit: int = 50,
) -> list[dict[str, Any]]:
    """Find nodes by name or qualified name using safely escaped SQL LIKE."""
    if not name_pattern or limit <= 0:
        return []
    like_pattern = f"%{_escape_like(name_pattern)}%"
    stmt = select(nodes_table).where(
        nodes_table.c.repo_id == repo_id,
        or_(
            nodes_table.c.name.like(like_pattern, escape="\\"),
            nodes_table.c.qualified_name.like(like_pattern, escape="\\"),
        ),
    )
    if kind is not None:
        stmt = stmt.where(nodes_table.c.kind == kind)
    stmt = stmt.order_by(
        nodes_table.c.kind,
        nodes_table.c.qualified_name,
        nodes_table.c.id,
    ).limit(limit)
    return [row_to_node(row) for row in conn.execute(stmt).mappings().all()]


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


def _search_rank_case(raw_query: str, prefix_pattern: str) -> Any:
    """Build deterministic exact/prefix match priority ranking CASE expression."""
    return case(
        (func.lower(nodes_table.c.name) == func.lower(raw_query), 0),
        (func.lower(nodes_table.c.qualified_name) == func.lower(raw_query), 1),
        (func.lower(nodes_table.c.name).like(func.lower(prefix_pattern), escape="\\"), 2),
        (
            func.lower(nodes_table.c.qualified_name).like(
                func.lower(prefix_pattern), escape="\\"
            ),
            3,
        ),
        else_=4,
    )


def _search_nodes_like_fallback(
    conn: Connection,
    repo_id: str,
    raw_query: str,
    *,
    kind: str | None = None,
    file_path: str | None = None,
    limit: int = 20,
) -> list[dict[str, Any]]:
    """Fallback escaped LIKE search when query uses special tokens or FTS has no hits."""
    escaped = _escape_like(raw_query)
    sub_pattern = f"%{escaped}%"
    prefix_pattern = f"{escaped}%"

    from_clause = (
        nodes_table.join(files_table, files_table.c.id == nodes_table.c.file_id)
        if file_path is not None
        else nodes_table.outerjoin(files_table, files_table.c.id == nodes_table.c.file_id)
    )
    stmt = (
        select(nodes_table)
        .select_from(from_clause)
        .where(
            nodes_table.c.repo_id == repo_id,
            or_(
                nodes_table.c.name.like(sub_pattern, escape="\\"),
                nodes_table.c.qualified_name.like(sub_pattern, escape="\\"),
                func.coalesce(nodes_table.c.signature, "").like(sub_pattern, escape="\\"),
                func.coalesce(nodes_table.c.doc_summary, "").like(sub_pattern, escape="\\"),
            ),
        )
    )
    if kind is not None:
        stmt = stmt.where(nodes_table.c.kind == kind)
    if file_path is not None:
        fp_clean = file_path.rstrip("/")
        stmt = stmt.where(
            or_(
                files_table.c.file_path == file_path,
                files_table.c.file_path.like(f"{_escape_like(fp_clean)}/%", escape="\\"),
            )
        )

    stmt = stmt.order_by(
        _search_rank_case(raw_query, prefix_pattern).asc(),
        nodes_table.c.kind.asc(),
        nodes_table.c.qualified_name.asc(),
        nodes_table.c.id.asc(),
    ).limit(limit)
    return [row_to_node(row) for row in conn.execute(stmt).mappings().all()]


def search_nodes_fts(
    conn: Connection,
    repo_id: str,
    query: str,
    *,
    kind: str | None = None,
    file_path: str | None = None,
    limit: int = 20,
) -> list[dict[str, Any]]:
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
    from_clause = nodes_fts_table.join(
        nodes_table,
        literal_column("nodes.rowid") == nodes_fts_table.c.rowid,
    )
    if file_path is not None:
        from_clause = from_clause.join(files_table, files_table.c.id == nodes_table.c.file_id)
    else:
        from_clause = from_clause.outerjoin(files_table, files_table.c.id == nodes_table.c.file_id)

    stmt = (
        select(nodes_table)
        .select_from(from_clause)
        .where(
            literal_column("nodes_fts").op("MATCH")(fts_expr),
            nodes_table.c.repo_id == repo_id,
        )
    )
    if kind is not None:
        stmt = stmt.where(nodes_table.c.kind == kind)
    if file_path is not None:
        fp_clean = file_path.rstrip("/")
        stmt = stmt.where(
            or_(
                files_table.c.file_path == file_path,
                files_table.c.file_path.like(f"{_escape_like(fp_clean)}/%", escape="\\"),
            )
        )

    stmt = stmt.order_by(
        _search_rank_case(raw_query, prefix_pattern).asc(),
        func.bm25(literal_column("nodes_fts"), 0.0, 0.0, 10.0, 8.0, 3.0, 1.0).asc(),
        nodes_table.c.kind.asc(),
        nodes_table.c.qualified_name.asc(),
        nodes_table.c.id.asc(),
    ).limit(limit)

    try:
        rows = conn.execute(stmt).mappings().all()
    except OperationalError:
        return _search_nodes_like_fallback(
            conn, repo_id, raw_query, kind=kind, file_path=file_path, limit=limit
        )

    if not rows:
        return _search_nodes_like_fallback(
            conn, repo_id, raw_query, kind=kind, file_path=file_path, limit=limit
        )
    return [row_to_node(row) for row in rows]


def list_child_nodes(conn: Connection, parent_id: str) -> list[dict[str, Any]]:
    """List structural child nodes by ``parent_id``."""
    stmt = (
        select(nodes_table)
        .where(nodes_table.c.parent_id == parent_id)
        .order_by(
            func.coalesce(nodes_table.c.start_line, 0),
            nodes_table.c.kind,
            nodes_table.c.name,
            nodes_table.c.id,
        )
    )
    return [row_to_node(row) for row in conn.execute(stmt).mappings().all()]


def list_lexical_children(conn: Connection, lexical_parent_id: str) -> list[dict[str, Any]]:
    """List lexical child nodes by ``lexical_parent_id``."""
    stmt = (
        select(nodes_table)
        .where(nodes_table.c.lexical_parent_id == lexical_parent_id)
        .order_by(
            func.coalesce(nodes_table.c.start_line, 0),
            nodes_table.c.kind,
            nodes_table.c.name,
            nodes_table.c.id,
        )
    )
    return [row_to_node(row) for row in conn.execute(stmt).mappings().all()]


def delete_nodes_for_file(conn: Connection, file_id: str) -> None:
    """Delete all nodes owned by a specific file."""
    conn.execute(delete(nodes_table).where(nodes_table.c.file_id == file_id))
