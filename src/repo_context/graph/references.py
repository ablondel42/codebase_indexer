"""Graph queries for LSP references and AST dependency edges."""

from collections.abc import Sequence
import sqlite3

from repo_context.constants import (
    EDGE_KIND_CALLS,
    EDGE_KIND_IMPORTS,
    EDGE_KIND_INHERITS,
    EDGE_KIND_REFERENCES,
)
from repo_context.storage.edges import row_to_edge
from repo_context.storage.reference_refresh import get_reference_refresh_state

AST_DEPENDENCY_EDGE_KINDS: tuple[str, ...] = (
    EDGE_KIND_CALLS,
    EDGE_KIND_IMPORTS,
    EDGE_KIND_INHERITS,
)

REFERENCE_AND_DEPENDENCY_EDGE_KINDS: tuple[str, ...] = (
    EDGE_KIND_REFERENCES,
    EDGE_KIND_CALLS,
    EDGE_KIND_IMPORTS,
    EDGE_KIND_INHERITS,
)

_EDGE_COLUMNS = (
    "id, repo_id, kind, from_id, to_id, source, confidence, "
    "evidence_file_id, evidence_uri, evidence_range_json, payload_json, last_indexed_at"
)


def _limit_offset_clause(limit: int | None, offset: int) -> tuple[str, tuple[int, ...]]:
    """Build SQLite LIMIT/OFFSET clause safely."""
    if limit is not None:
        return " LIMIT ? OFFSET ?", (int(limit), max(0, int(offset)))
    if offset > 0:
        return " LIMIT -1 OFFSET ?", (int(offset),)
    return "", ()


def _normalize_kinds(kinds: Sequence[str] | None, default: tuple[str, ...]) -> tuple[str, ...]:
    """Normalize optional edge-kind sequence to a deduplicated tuple."""
    if kinds is None:
        return default
    return tuple(dict.fromkeys(k for k in kinds if k))


def list_reference_edges_for_target(
    conn: sqlite3.Connection,
    target_id: str,
    *,
    limit: int | None = None,
    offset: int = 0,
) -> list[dict]:
    """List LSP reference edges targeting a symbol ordered by site location."""
    if limit is not None and limit <= 0:
        return []
    limit_sql, limit_params = _limit_offset_clause(limit, offset)
    cursor = conn.execute(
        f"""
        SELECT {_EDGE_COLUMNS}
        FROM edges
        WHERE kind = ? AND to_id = ?
        ORDER BY evidence_uri,
                 COALESCE(json_extract(evidence_range_json, '$.start.line'), 0),
                 COALESCE(json_extract(evidence_range_json, '$.start.character'), 0),
                 id
        {limit_sql}
        """,
        (EDGE_KIND_REFERENCES, target_id, *limit_params),
    )
    return [row_to_edge(row) for row in cursor.fetchall()]


def list_referenced_by(
    conn: sqlite3.Connection,
    target_id: str,
    *,
    kinds: Sequence[str] | None = None,
    limit: int | None = None,
    offset: int = 0,
) -> list[dict]:
    """List distinct source symbols referencing a target symbol."""
    if limit is not None and limit <= 0:
        return []
    resolved_kinds = _normalize_kinds(kinds, (EDGE_KIND_REFERENCES,))
    if not resolved_kinds:
        return []
    placeholders = ",".join("?" * len(resolved_kinds))
    limit_sql, limit_params = _limit_offset_clause(limit, offset)
    cursor = conn.execute(
        f"""
        SELECT DISTINCT n.id, n.repo_id, n.file_id, n.language, n.kind, n.name,
               n.qualified_name, n.uri, n.start_line, n.end_line, n.signature,
               n.scope, n.lexical_parent_id, f.file_path, f.module_path
        FROM edges e
        JOIN nodes n ON e.from_id = n.id
        LEFT JOIN files f ON f.id = n.file_id
        WHERE e.kind IN ({placeholders}) AND e.to_id = ?
        ORDER BY n.kind, n.qualified_name, n.id
        {limit_sql}
        """,
        (*resolved_kinds, target_id, *limit_params),
    )
    return [dict(row) for row in cursor.fetchall()]


def list_references_from_symbol(
    conn: sqlite3.Connection,
    source_id: str,
    *,
    limit: int | None = None,
    offset: int = 0,
) -> list[dict]:
    """List LSP reference edges originating from a source symbol."""
    if limit is not None and limit <= 0:
        return []
    limit_sql, limit_params = _limit_offset_clause(limit, offset)
    cursor = conn.execute(
        f"""
        SELECT {_EDGE_COLUMNS}
        FROM edges
        WHERE kind = ? AND from_id = ?
        ORDER BY to_id, evidence_uri,
                 COALESCE(json_extract(evidence_range_json, '$.start.line'), 0),
                 COALESCE(json_extract(evidence_range_json, '$.start.character'), 0),
                 id
        {limit_sql}
        """,
        (EDGE_KIND_REFERENCES, source_id, *limit_params),
    )
    return [row_to_edge(row) for row in cursor.fetchall()]


def list_incoming_symbol_edges(
    conn: sqlite3.Connection,
    target_id: str,
    *,
    kinds: Sequence[str] | None = None,
    limit: int | None = None,
    offset: int = 0,
) -> list[dict]:
    """List incoming reference/dependency edges targeting ``target_id``."""
    return list_symbol_reference_edges(
        conn,
        target_id,
        direction="incoming",
        kinds=kinds,
        limit=limit,
        offset=offset,
    )


def list_outgoing_symbol_edges(
    conn: sqlite3.Connection,
    source_id: str,
    *,
    kinds: Sequence[str] | None = None,
    limit: int | None = None,
    offset: int = 0,
) -> list[dict]:
    """List outgoing reference/dependency edges originating from ``source_id``."""
    return list_symbol_reference_edges(
        conn,
        source_id,
        direction="outgoing",
        kinds=kinds,
        limit=limit,
        offset=offset,
    )


def _direction_where(
    symbol_id: str,
    direction: str,
    kinds: tuple[str, ...],
) -> tuple[str, tuple[str, ...]]:
    """Build parameterized WHERE clause for directed reference/dependency queries."""
    placeholders = ",".join("?" * len(kinds))
    if direction == "incoming":
        return f"to_id = ? AND kind IN ({placeholders})", (symbol_id, *kinds)
    if direction == "outgoing":
        return f"from_id = ? AND kind IN ({placeholders})", (symbol_id, *kinds)
    if direction == "both":
        return (
            f"(to_id = ? OR from_id = ?) AND kind IN ({placeholders})",
            (symbol_id, symbol_id, *kinds),
        )
    raise ValueError(f"Invalid direction {direction!r}; expected 'incoming', 'outgoing', or 'both'")


def list_symbol_reference_edges(
    conn: sqlite3.Connection,
    symbol_id: str,
    *,
    direction: str = "incoming",
    kinds: Sequence[str] | None = None,
    limit: int | None = None,
    offset: int = 0,
) -> list[dict]:
    """List paginated LSP references and/or AST calls/imports/inherits edges for a symbol."""
    if limit is not None and limit <= 0:
        return []
    resolved_kinds = _normalize_kinds(kinds, REFERENCE_AND_DEPENDENCY_EDGE_KINDS)
    if not resolved_kinds:
        return []

    where_sql, where_params = _direction_where(symbol_id, direction, resolved_kinds)
    limit_sql, limit_params = _limit_offset_clause(limit, offset)
    cursor = conn.execute(
        f"""
        SELECT {_EDGE_COLUMNS}
        FROM edges
        WHERE {where_sql}
        ORDER BY CASE kind
                     WHEN 'references' THEN 0
                     WHEN 'calls' THEN 1
                     WHEN 'imports' THEN 2
                     WHEN 'inherits' THEN 3
                     ELSE 4
                 END ASC,
                 evidence_uri,
                 COALESCE(json_extract(evidence_range_json, '$.start.line'), 0),
                 COALESCE(json_extract(evidence_range_json, '$.start.character'), 0),
                 id
        {limit_sql}
        """,
        (*where_params, *limit_params),
    )
    return [row_to_edge(row) for row in cursor.fetchall()]


def count_symbol_reference_edges(
    conn: sqlite3.Connection,
    symbol_id: str,
    *,
    direction: str = "incoming",
    kinds: Sequence[str] | None = None,
) -> int:
    """Count LSP references and/or AST dependency edges for a symbol."""
    resolved_kinds = _normalize_kinds(kinds, REFERENCE_AND_DEPENDENCY_EDGE_KINDS)
    if not resolved_kinds:
        return 0
    where_sql, where_params = _direction_where(symbol_id, direction, resolved_kinds)
    row = conn.execute(
        f"SELECT COUNT(*) FROM edges WHERE {where_sql}",
        where_params,
    ).fetchone()
    return int(row[0]) if row else 0


def get_referencing_modules_for_target(
    conn: sqlite3.Connection,
    target_id: str,
    *,
    kinds: Sequence[str] | None = None,
) -> list[str]:
    """Return distinct referencing ``files.module_path`` values for edges targeting ``target_id``."""
    resolved_kinds = _normalize_kinds(kinds, (EDGE_KIND_REFERENCES,))
    if not resolved_kinds:
        return []
    placeholders = ",".join("?" * len(resolved_kinds))
    rows = conn.execute(
        f"""
        SELECT DISTINCT f.module_path
        FROM edges e
        LEFT JOIN nodes n ON e.from_id = n.id
        JOIN files f ON f.id = COALESCE(e.evidence_file_id, n.file_id)
        WHERE e.kind IN ({placeholders}) AND e.to_id = ?
        ORDER BY f.module_path
        """,
        (*resolved_kinds, target_id),
    ).fetchall()
    return [row["module_path"] for row in rows if row["module_path"]]


def _compute_edge_stats_for_kinds(
    conn: sqlite3.Connection,
    target_id: str,
    kinds: tuple[str, ...],
) -> tuple[int, int, int]:
    """Compute (edge_count, file_count, module_count) using ``files.module_path``."""
    placeholders = ",".join("?" * len(kinds))
    row = conn.execute(
        f"""
        SELECT COUNT(*) AS ref_count,
               COUNT(DISTINCT COALESCE(e.evidence_file_id, n.file_id)) AS file_count,
               COUNT(DISTINCT COALESCE(f.module_path, e.evidence_file_id, n.file_id)) AS module_count
        FROM edges e
        LEFT JOIN nodes n ON e.from_id = n.id
        LEFT JOIN files f ON f.id = COALESCE(e.evidence_file_id, n.file_id)
        WHERE e.kind IN ({placeholders}) AND e.to_id = ?
        """,
        (*kinds, target_id),
    ).fetchone()
    if row is None:
        return 0, 0, 0
    return int(row["ref_count"]), int(row["file_count"]), int(row["module_count"])


def build_reference_stats(conn: sqlite3.Connection, target_id: str) -> dict:
    """Build reference statistics for a target symbol with AST fallback.

    Uses LSP ``references`` edges when LSP refresh is available (or when LSP
    reference edges are already stored); otherwise transparently falls back to
    AST ``calls``, ``imports``, and ``inherits`` edge counts.
    """
    refresh_state = get_reference_refresh_state(conn, target_id)
    if refresh_state["available"]:
        reference_source = "lsp"
        kinds: tuple[str, ...] = (EDGE_KIND_REFERENCES,)
    else:
        lsp_row = conn.execute(
            "SELECT 1 FROM edges WHERE kind = ? AND to_id = ? LIMIT 1",
            (EDGE_KIND_REFERENCES, target_id),
        ).fetchone()
        if lsp_row is not None:
            reference_source = "lsp"
            kinds = (EDGE_KIND_REFERENCES,)
        else:
            reference_source = "ast"
            kinds = AST_DEPENDENCY_EDGE_KINDS

    reference_count, referencing_file_count, referencing_module_count = (
        _compute_edge_stats_for_kinds(conn, target_id, kinds)
    )

    return {
        "reference_count": reference_count,
        "referencing_file_count": referencing_file_count,
        "referencing_module_count": referencing_module_count,
        "available": refresh_state["available"],
        "last_refreshed_at": refresh_state["last_refreshed_at"],
        "reference_source": reference_source,
    }
