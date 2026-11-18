"""Graph queries for LSP references and AST dependency edges using SQLAlchemy Core."""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

from sqlalchemy import and_, case, distinct, func, or_, select
from sqlalchemy.engine import Connection

from repo_context.constants import (
    EDGE_KIND_CALLS,
    EDGE_KIND_IMPORTS,
    EDGE_KIND_INHERITS,
    EDGE_KIND_REFERENCES,
)
from repo_context.storage.edges import row_to_edge
from repo_context.storage.reference_refresh import get_reference_refresh_state
from repo_context.storage.schema import edges_table, files_table, nodes_table

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


def _apply_pagination(stmt: Any, limit: int | None, offset: int) -> Any:
    if offset > 0:
        stmt = stmt.offset(offset)
    if limit is not None:
        stmt = stmt.limit(limit)
    return stmt


def _normalize_kinds(kinds: Sequence[str] | None, default: tuple[str, ...]) -> tuple[str, ...]:
    """Normalize optional edge-kind sequence to a deduplicated tuple."""
    if kinds is None:
        return default
    return tuple(dict.fromkeys(k for k in kinds if k))


def _evidence_line_expr() -> Any:
    return func.coalesce(
        func.json_extract(edges_table.c.evidence_range_json, "$.start.line"),
        0,
    )


def _evidence_char_expr() -> Any:
    return func.coalesce(
        func.json_extract(edges_table.c.evidence_range_json, "$.start.character"),
        0,
    )


def list_reference_edges_for_target(
    conn: Connection,
    target_id: str,
    *,
    limit: int | None = None,
    offset: int = 0,
) -> list[dict[str, Any]]:
    """List LSP reference edges targeting a symbol ordered by site location."""
    if limit is not None and limit <= 0:
        return []
    stmt = (
        select(edges_table)
        .where(
            edges_table.c.kind == EDGE_KIND_REFERENCES,
            edges_table.c.to_id == target_id,
        )
        .order_by(
            edges_table.c.evidence_uri,
            _evidence_line_expr(),
            _evidence_char_expr(),
            edges_table.c.id,
        )
    )
    stmt = _apply_pagination(stmt, limit, offset)
    return [row_to_edge(row) for row in conn.execute(stmt).mappings().all()]


def list_referenced_by(
    conn: Connection,
    target_id: str,
    *,
    kinds: Sequence[str] | None = None,
    limit: int | None = None,
    offset: int = 0,
) -> list[dict[str, Any]]:
    """List distinct source symbols referencing a target symbol."""
    if limit is not None and limit <= 0:
        return []
    resolved_kinds = _normalize_kinds(kinds, (EDGE_KIND_REFERENCES,))
    if not resolved_kinds:
        return []

    stmt = (
        select(
            nodes_table.c.id,
            nodes_table.c.repo_id,
            nodes_table.c.file_id,
            nodes_table.c.language,
            nodes_table.c.kind,
            nodes_table.c.name,
            nodes_table.c.qualified_name,
            nodes_table.c.uri,
            nodes_table.c.start_line,
            nodes_table.c.end_line,
            nodes_table.c.signature,
            nodes_table.c.scope,
            nodes_table.c.lexical_parent_id,
            files_table.c.file_path,
            files_table.c.module_path,
        )
        .distinct()
        .select_from(
            edges_table.join(nodes_table, edges_table.c.from_id == nodes_table.c.id).outerjoin(
                files_table, files_table.c.id == nodes_table.c.file_id
            )
        )
        .where(
            edges_table.c.kind.in_(resolved_kinds),
            edges_table.c.to_id == target_id,
        )
        .order_by(nodes_table.c.kind, nodes_table.c.qualified_name, nodes_table.c.id)
    )
    stmt = _apply_pagination(stmt, limit, offset)
    return [dict(row) for row in conn.execute(stmt).mappings().all()]


def list_references_from_symbol(
    conn: Connection,
    source_id: str,
    *,
    limit: int | None = None,
    offset: int = 0,
) -> list[dict[str, Any]]:
    """List LSP reference edges originating from a source symbol."""
    if limit is not None and limit <= 0:
        return []
    stmt = (
        select(edges_table)
        .where(
            edges_table.c.kind == EDGE_KIND_REFERENCES,
            edges_table.c.from_id == source_id,
        )
        .order_by(
            edges_table.c.to_id,
            edges_table.c.evidence_uri,
            _evidence_line_expr(),
            _evidence_char_expr(),
            edges_table.c.id,
        )
    )
    stmt = _apply_pagination(stmt, limit, offset)
    return [row_to_edge(row) for row in conn.execute(stmt).mappings().all()]


def list_incoming_symbol_edges(
    conn: Connection,
    target_id: str,
    *,
    kinds: Sequence[str] | None = None,
    limit: int | None = None,
    offset: int = 0,
) -> list[dict[str, Any]]:
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
    conn: Connection,
    source_id: str,
    *,
    kinds: Sequence[str] | None = None,
    limit: int | None = None,
    offset: int = 0,
) -> list[dict[str, Any]]:
    """List outgoing reference/dependency edges originating from ``source_id``."""
    return list_symbol_reference_edges(
        conn,
        source_id,
        direction="outgoing",
        kinds=kinds,
        limit=limit,
        offset=offset,
    )


def _direction_condition(
    symbol_id: str,
    direction: str,
    kinds: tuple[str, ...],
) -> Any:
    """Build SQLAlchemy filter expression for directed reference/dependency queries."""
    kind_cond = edges_table.c.kind.in_(kinds)
    if direction == "incoming":
        return and_(edges_table.c.to_id == symbol_id, kind_cond)
    if direction == "outgoing":
        return and_(edges_table.c.from_id == symbol_id, kind_cond)
    if direction == "both":
        return and_(
            or_(edges_table.c.to_id == symbol_id, edges_table.c.from_id == symbol_id),
            kind_cond,
        )
    raise ValueError(f"Invalid direction {direction!r}; expected 'incoming', 'outgoing', or 'both'")


def list_symbol_reference_edges(
    conn: Connection,
    symbol_id: str,
    *,
    direction: str = "incoming",
    kinds: Sequence[str] | None = None,
    limit: int | None = None,
    offset: int = 0,
) -> list[dict[str, Any]]:
    """List paginated LSP references and/or AST calls/imports/inherits edges for a symbol."""
    if limit is not None and limit <= 0:
        return []
    resolved_kinds = _normalize_kinds(kinds, REFERENCE_AND_DEPENDENCY_EDGE_KINDS)
    if not resolved_kinds:
        return []

    kind_priority = case(
        (edges_table.c.kind == "references", 0),
        (edges_table.c.kind == "calls", 1),
        (edges_table.c.kind == "imports", 2),
        (edges_table.c.kind == "inherits", 3),
        else_=4,
    )
    stmt = (
        select(edges_table)
        .where(_direction_condition(symbol_id, direction, resolved_kinds))
        .order_by(
            kind_priority.asc(),
            edges_table.c.evidence_uri,
            _evidence_line_expr(),
            _evidence_char_expr(),
            edges_table.c.id,
        )
    )
    stmt = _apply_pagination(stmt, limit, offset)
    return [row_to_edge(row) for row in conn.execute(stmt).mappings().all()]


def count_symbol_reference_edges(
    conn: Connection,
    symbol_id: str,
    *,
    direction: str = "incoming",
    kinds: Sequence[str] | None = None,
) -> int:
    """Count LSP references and/or AST dependency edges for a symbol."""
    resolved_kinds = _normalize_kinds(kinds, REFERENCE_AND_DEPENDENCY_EDGE_KINDS)
    if not resolved_kinds:
        return 0
    stmt = (
        select(func.count())
        .select_from(edges_table)
        .where(_direction_condition(symbol_id, direction, resolved_kinds))
    )
    return int(conn.execute(stmt).scalar_one())


def get_referencing_modules_for_target(
    conn: Connection,
    target_id: str,
    *,
    kinds: Sequence[str] | None = None,
) -> list[str]:
    """Return distinct referencing ``files.module_path`` values for edges targeting ``target_id``."""
    resolved_kinds = _normalize_kinds(kinds, (EDGE_KIND_REFERENCES,))
    if not resolved_kinds:
        return []

    resolved_file_id = func.coalesce(edges_table.c.evidence_file_id, nodes_table.c.file_id)
    stmt = (
        select(files_table.c.module_path)
        .distinct()
        .select_from(
            edges_table.outerjoin(
                nodes_table, edges_table.c.from_id == nodes_table.c.id
            ).join(files_table, files_table.c.id == resolved_file_id)
        )
        .where(
            edges_table.c.kind.in_(resolved_kinds),
            edges_table.c.to_id == target_id,
        )
        .order_by(files_table.c.module_path)
    )
    return [str(mod) for mod in conn.execute(stmt).scalars().all() if mod]


def _compute_edge_stats_for_kinds(
    conn: Connection,
    target_id: str,
    kinds: tuple[str, ...],
) -> tuple[int, int, int]:
    """Compute (edge_count, file_count, module_count) using ``files.module_path``."""
    resolved_file_id = func.coalesce(edges_table.c.evidence_file_id, nodes_table.c.file_id)
    resolved_mod_key = func.coalesce(
        files_table.c.module_path,
        edges_table.c.evidence_file_id,
        nodes_table.c.file_id,
    )
    stmt = (
        select(
            func.count().label("ref_count"),
            func.count(distinct(resolved_file_id)).label("file_count"),
            func.count(distinct(resolved_mod_key)).label("module_count"),
        )
        .select_from(
            edges_table.outerjoin(
                nodes_table, edges_table.c.from_id == nodes_table.c.id
            ).outerjoin(files_table, files_table.c.id == resolved_file_id)
        )
        .where(
            edges_table.c.kind.in_(kinds),
            edges_table.c.to_id == target_id,
        )
    )
    row = conn.execute(stmt).mappings().first()
    if row is None:
        return 0, 0, 0
    return int(row["ref_count"]), int(row["file_count"]), int(row["module_count"])


def build_reference_stats(conn: Connection, target_id: str) -> dict[str, Any]:
    """Build reference statistics for a target symbol with AST fallback."""
    refresh_state = get_reference_refresh_state(conn, target_id)
    if refresh_state["available"]:
        reference_source = "lsp"
        kinds: tuple[str, ...] = (EDGE_KIND_REFERENCES,)
    else:
        has_lsp = conn.execute(
            select(edges_table.c.id)
            .where(
                edges_table.c.kind == EDGE_KIND_REFERENCES,
                edges_table.c.to_id == target_id,
            )
            .limit(1)
        ).scalar_one_or_none()
        if has_lsp is not None:
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
