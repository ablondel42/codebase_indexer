"""Edge persistence helpers."""

import json
import sqlite3
from typing import Any, Iterable

from repo_context.exceptions import DatabaseError, ValidationError
from repo_context.logging_config import get_logger
from repo_context.models.edge import Edge

logger = get_logger("storage.edges")

_EDGE_COLUMNS = (
    "id, repo_id, kind, from_id, to_id, source, confidence, "
    "evidence_file_id, evidence_uri, evidence_range_json, payload_json, last_indexed_at"
)

_UPSERT_EDGE_SQL = """
INSERT INTO edges (
    id, repo_id, kind, from_id, to_id, source, confidence,
    evidence_file_id, evidence_uri, evidence_range_json,
    payload_json, last_indexed_at
) VALUES (
    :id, :repo_id, :kind, :from_id, :to_id, :source, :confidence,
    :evidence_file_id, :evidence_uri, :evidence_range_json,
    :payload_json, :last_indexed_at
)
ON CONFLICT(id) DO UPDATE SET
    repo_id = excluded.repo_id,
    kind = excluded.kind,
    from_id = excluded.from_id,
    to_id = excluded.to_id,
    source = excluded.source,
    confidence = excluded.confidence,
    evidence_file_id = excluded.evidence_file_id,
    evidence_uri = excluded.evidence_uri,
    evidence_range_json = excluded.evidence_range_json,
    payload_json = excluded.payload_json,
    last_indexed_at = excluded.last_indexed_at
"""


def _ensure_json_string(value: Any, *, default: str | None = None) -> str | None:
    """Pass pre-serialized JSON strings straight through; serialize dicts/lists once."""
    if value is None:
        return default
    if isinstance(value, str):
        return value
    return json.dumps(value, sort_keys=True)


def _deserialize_json(value: str | None) -> Any:
    """Deserialize a JSON string to Python object."""
    if value is None:
        return None
    return json.loads(value)


def _validate_and_normalize_edge(edge: dict | Edge) -> Edge:
    """Validate and normalize an edge input."""
    if isinstance(edge, Edge):
        return edge
    try:
        return Edge(**edge)
    except Exception as exc:
        logger.error(
            "Edge validation failed",
            extra={
                "edge_id": edge.get("id") if isinstance(edge, dict) else None,
                "error": str(exc),
            },
        )
        raise ValidationError(f"Invalid edge data: {exc}") from exc


def edge_to_row(edge: dict) -> dict:
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


def row_to_edge(row: sqlite3.Row) -> dict:
    """Convert a SQLite row to an edge dictionary with deserialized JSON fields."""
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


def upsert_edge(conn: sqlite3.Connection, edge: dict | Edge) -> None:
    """Insert or update a single edge."""
    validated = _validate_and_normalize_edge(edge)
    logger.debug("Upserting edge", extra={"edge_id": validated.id})
    row = edge_to_row(validated.model_dump())
    try:
        conn.execute(_UPSERT_EDGE_SQL, row)
    except sqlite3.Error as exc:
        logger.error(
            "Database error upserting edge",
            extra={"edge_id": validated.id, "error": str(exc)},
        )
        raise DatabaseError(f"Failed to upsert edge {validated.id}: {exc}") from exc


def upsert_edges(conn: sqlite3.Connection, edges: list[dict | Edge]) -> None:
    """Insert or update multiple edges in a single batch."""
    if not edges:
        return
    rows = [edge_to_row(_validate_and_normalize_edge(edge).model_dump()) for edge in edges]
    try:
        conn.executemany(_UPSERT_EDGE_SQL, rows)
    except sqlite3.Error as exc:
        logger.error("Database error upserting edges batch", extra={"error": str(exc)})
        raise DatabaseError(f"Failed to upsert {len(rows)} edges: {exc}") from exc


def get_edge_by_id(conn: sqlite3.Connection, edge_id: str) -> dict | None:
    """Get an edge by its ID."""
    row = conn.execute(
        f"SELECT {_EDGE_COLUMNS} FROM edges WHERE id = ?",
        (edge_id,),
    ).fetchone()
    if row is None:
        return None
    return row_to_edge(row)


def list_edges_for_repo(conn: sqlite3.Connection, repo_id: str) -> list[dict]:
    """List all edges for a specific repository."""
    rows = conn.execute(
        f"""
        SELECT {_EDGE_COLUMNS}
        FROM edges
        WHERE repo_id = ?
        ORDER BY kind, from_id, to_id, id
        """,
        (repo_id,),
    ).fetchall()
    return [row_to_edge(row) for row in rows]


def list_outgoing_edges(
    conn: sqlite3.Connection,
    from_id: str,
    kind: str | None = None,
) -> list[dict]:
    """List outgoing edges from a node."""
    if kind is not None:
        rows = conn.execute(
            f"""
            SELECT {_EDGE_COLUMNS}
            FROM edges
            WHERE from_id = ? AND kind = ?
            ORDER BY kind, to_id, id
            """,
            (from_id, kind),
        ).fetchall()
    else:
        rows = conn.execute(
            f"""
            SELECT {_EDGE_COLUMNS}
            FROM edges
            WHERE from_id = ?
            ORDER BY kind, to_id, id
            """,
            (from_id,),
        ).fetchall()
    return [row_to_edge(row) for row in rows]


def list_incoming_edges(
    conn: sqlite3.Connection,
    to_id: str,
    kind: str | None = None,
) -> list[dict]:
    """List incoming edges to a node."""
    if kind is not None:
        rows = conn.execute(
            f"""
            SELECT {_EDGE_COLUMNS}
            FROM edges
            WHERE to_id = ? AND kind = ?
            ORDER BY kind, from_id, id
            """,
            (to_id, kind),
        ).fetchall()
    else:
        rows = conn.execute(
            f"""
            SELECT {_EDGE_COLUMNS}
            FROM edges
            WHERE to_id = ?
            ORDER BY kind, from_id, id
            """,
            (to_id,),
        ).fetchall()
    return [row_to_edge(row) for row in rows]


def list_edges_for_target_and_kinds(
    conn: sqlite3.Connection,
    node_id: str,
    kinds: Iterable[str],
) -> list[dict]:
    """List incoming edges targeting ``node_id`` filtered by ``kinds``."""
    kind_list = list(kinds)
    if not kind_list:
        return []
    placeholders = ",".join("?" * len(kind_list))
    rows = conn.execute(
        f"""
        SELECT {_EDGE_COLUMNS}
        FROM edges
        WHERE to_id = ? AND kind IN ({placeholders})
        ORDER BY kind, from_id, id
        """,
        (node_id, *kind_list),
    ).fetchall()
    return [row_to_edge(row) for row in rows]


def list_edges_for_file(conn: sqlite3.Connection, file_id: str) -> list[dict]:
    """List all edges with evidence in a specific file."""
    rows = conn.execute(
        f"""
        SELECT {_EDGE_COLUMNS}
        FROM edges
        WHERE evidence_file_id = ?
        ORDER BY kind, from_id, to_id, id
        """,
        (file_id,),
    ).fetchall()
    return [row_to_edge(row) for row in rows]


def delete_owned_edges_for_file(conn: sqlite3.Connection, file_id: str) -> None:
    """Delete edges evidenced by or originating from symbols in ``file_id``.

    Preserves incoming edges from unmodified files pointing to symbols in ``file_id``
    during incremental file re-indexing.
    """
    conn.execute(
        """
        DELETE FROM edges
        WHERE evidence_file_id = ?
           OR from_id IN (SELECT id FROM nodes WHERE file_id = ?)
        """,
        (file_id, file_id),
    )


def delete_edges_for_file(conn: sqlite3.Connection, file_id: str) -> None:
    """Delete all edges owned by or referencing symbols defined in ``file_id``."""
    conn.execute(
        """
        DELETE FROM edges
        WHERE evidence_file_id = ?
           OR from_id IN (SELECT id FROM nodes WHERE file_id = ?)
           OR to_id IN (SELECT id FROM nodes WHERE file_id = ?)
        """,
        (file_id, file_id, file_id),
    )
