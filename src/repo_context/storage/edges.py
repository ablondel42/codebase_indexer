"""Edge persistence helpers using SQLAlchemy Core."""

from __future__ import annotations

import json
from collections.abc import Iterable, Mapping, Sequence
from typing import Any

from sqlalchemy import delete, or_, select
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.engine import Connection, RowMapping
from sqlalchemy.exc import SQLAlchemyError

from repo_context.exceptions import DatabaseError, ValidationError
from repo_context.logging_config import get_logger
from repo_context.models.edge import Edge
from repo_context.storage.schema import edges_table, nodes_table

logger = get_logger("storage.edges")


def _ensure_json_string(value: Any, *, default: str | None = None) -> str | None:
    """Pass pre-serialized JSON strings straight through; serialize dicts/lists once."""
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


def _validate_and_normalize_edge(edge: Mapping[str, Any] | Edge) -> Edge:
    """Validate and normalize an edge input."""
    if isinstance(edge, Edge):
        return edge
    try:
        return Edge.model_validate(dict(edge))
    except Exception as exc:
        logger.error(
            "Edge validation failed",
            extra={
                "edge_id": edge.get("id") if isinstance(edge, Mapping) else None,
                "error": str(exc),
            },
        )
        raise ValidationError(f"Invalid edge data: {exc}") from exc


def edge_to_row(edge: dict[str, Any]) -> dict[str, Any]:
    """Convert an edge dictionary to database row values without double JSON encoding."""
    return {
        "id": edge["id"],
        "repo_id": edge["repo_id"],
        "kind": edge["kind"],
        "from_id": edge["from_id"],
        "to_id": edge["to_id"],
        "source": edge["source"],
        "confidence": edge["confidence"],
        "evidence_file_id": edge.get("evidence_file_id"),
        "evidence_uri": edge.get("evidence_uri"),
        "evidence_range_json": _ensure_json_string(edge.get("evidence_range_json"), default=None),
        "payload_json": _ensure_json_string(edge.get("payload_json"), default="{}"),
        "last_indexed_at": edge.get("last_indexed_at"),
    }


def row_to_edge(row: RowMapping | Mapping[str, Any]) -> dict[str, Any]:
    """Convert a database mapping row to an edge dictionary with decoded JSON fields."""
    return {
        "id": row["id"],
        "repo_id": row["repo_id"],
        "kind": row["kind"],
        "from_id": row["from_id"],
        "to_id": row["to_id"],
        "source": row["source"],
        "confidence": row["confidence"],
        "evidence_file_id": row["evidence_file_id"],
        "evidence_uri": row["evidence_uri"],
        "evidence_range_json": _deserialize_json(row["evidence_range_json"]),
        "payload_json": _deserialize_json(row["payload_json"]),
        "last_indexed_at": row["last_indexed_at"],
    }


def _build_upsert_edge_stmt() -> Any:
    base = sqlite_insert(edges_table)
    return base.on_conflict_do_update(
        index_elements=[edges_table.c.id],
        set_={
            col.name: base.excluded[col.name]
            for col in edges_table.columns
            if col.name != "id"
        },
    )


def upsert_edge(conn: Connection, edge: Mapping[str, Any] | Edge) -> None:
    """Insert or update a single edge."""
    validated = _validate_and_normalize_edge(edge)
    logger.debug("Upserting edge", extra={"edge_id": validated.id})
    row = edge_to_row(validated.model_dump())
    try:
        conn.execute(_build_upsert_edge_stmt(), row)
    except SQLAlchemyError as exc:
        logger.error(
            "Database error upserting edge",
            extra={"edge_id": validated.id, "error": str(exc)},
        )
        raise DatabaseError(f"Failed to upsert edge {validated.id}: {exc}") from exc


def upsert_edges(conn: Connection, edges: Sequence[Mapping[str, Any] | Edge]) -> None:
    """Insert or update multiple edges in a single batch."""
    if not edges:
        return
    rows = [edge_to_row(_validate_and_normalize_edge(edge).model_dump()) for edge in edges]
    try:
        conn.execute(_build_upsert_edge_stmt(), rows)
    except SQLAlchemyError as exc:
        logger.error("Database error upserting edges batch", extra={"error": str(exc)})
        raise DatabaseError(f"Failed to upsert {len(rows)} edges: {exc}") from exc


def get_edge_by_id(conn: Connection, edge_id: str) -> dict[str, Any] | None:
    """Get an edge by its ID."""
    row = conn.execute(select(edges_table).where(edges_table.c.id == edge_id)).mappings().first()
    if row is None:
        return None
    return row_to_edge(row)


def list_edges_for_repo(conn: Connection, repo_id: str) -> list[dict[str, Any]]:
    """List all edges for a specific repository."""
    stmt = (
        select(edges_table)
        .where(edges_table.c.repo_id == repo_id)
        .order_by(
            edges_table.c.kind,
            edges_table.c.from_id,
            edges_table.c.to_id,
            edges_table.c.id,
        )
    )
    return [row_to_edge(row) for row in conn.execute(stmt).mappings().all()]


def list_outgoing_edges(
    conn: Connection,
    from_id: str,
    kind: str | None = None,
) -> list[dict[str, Any]]:
    """List outgoing edges from a node."""
    stmt = select(edges_table).where(edges_table.c.from_id == from_id)
    if kind is not None:
        stmt = stmt.where(edges_table.c.kind == kind)
    stmt = stmt.order_by(edges_table.c.kind, edges_table.c.to_id, edges_table.c.id)
    return [row_to_edge(row) for row in conn.execute(stmt).mappings().all()]


def list_incoming_edges(
    conn: Connection,
    to_id: str,
    kind: str | None = None,
) -> list[dict[str, Any]]:
    """List incoming edges to a node."""
    stmt = select(edges_table).where(edges_table.c.to_id == to_id)
    if kind is not None:
        stmt = stmt.where(edges_table.c.kind == kind)
    stmt = stmt.order_by(edges_table.c.kind, edges_table.c.from_id, edges_table.c.id)
    return [row_to_edge(row) for row in conn.execute(stmt).mappings().all()]


def list_edges_for_target_and_kinds(
    conn: Connection,
    node_id: str,
    kinds: Iterable[str],
) -> list[dict[str, Any]]:
    """List incoming edges targeting ``node_id`` filtered by ``kinds``."""
    kind_list = list(kinds)
    if not kind_list:
        return []
    stmt = (
        select(edges_table)
        .where(
            edges_table.c.to_id == node_id,
            edges_table.c.kind.in_(kind_list),
        )
        .order_by(edges_table.c.kind, edges_table.c.from_id, edges_table.c.id)
    )
    return [row_to_edge(row) for row in conn.execute(stmt).mappings().all()]


def list_edges_for_file(conn: Connection, file_id: str) -> list[dict[str, Any]]:
    """List all edges with evidence in a specific file."""
    stmt = (
        select(edges_table)
        .where(edges_table.c.evidence_file_id == file_id)
        .order_by(
            edges_table.c.kind,
            edges_table.c.from_id,
            edges_table.c.to_id,
            edges_table.c.id,
        )
    )
    return [row_to_edge(row) for row in conn.execute(stmt).mappings().all()]


def delete_owned_edges_for_file(conn: Connection, file_id: str) -> None:
    """Delete edges evidenced by or originating from symbols in ``file_id``."""
    file_node_ids = select(nodes_table.c.id).where(nodes_table.c.file_id == file_id)
    conn.execute(
        delete(edges_table).where(
            or_(
                edges_table.c.evidence_file_id == file_id,
                edges_table.c.from_id.in_(file_node_ids),
            )
        )
    )


def delete_edges_for_file(conn: Connection, file_id: str) -> None:
    """Delete all edges owned by or referencing symbols defined in ``file_id``."""
    file_node_ids = select(nodes_table.c.id).where(nodes_table.c.file_id == file_id)
    conn.execute(
        delete(edges_table).where(
            or_(
                edges_table.c.evidence_file_id == file_id,
                edges_table.c.from_id.in_(file_node_ids),
                edges_table.c.to_id.in_(file_node_ids),
            )
        )
    )
