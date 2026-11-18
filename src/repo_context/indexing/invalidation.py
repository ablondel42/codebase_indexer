"""Reference invalidation helpers using SQLAlchemy Core."""

from __future__ import annotations

from collections.abc import Iterable, Iterator, Sequence
from typing import Any

from sqlalchemy import delete, literal, or_, select
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.engine import Connection

from repo_context.storage.schema import edges_table, nodes_table, reference_refresh_table

_SQLITE_IN_CHUNK_SIZE = 400


def _chunked(items: Sequence[str], size: int = _SQLITE_IN_CHUNK_SIZE) -> Iterator[Sequence[str]]:
    """Yield slices of at most ``size`` items to stay safely below SQLite bind limits."""
    for start in range(0, len(items), size):
        yield items[start : start + size]


def _upsert_stale_from_node_select(conn: Connection, node_filter: Any) -> None:
    """Execute ``INSERT INTO reference_refresh ... SELECT ... ON CONFLICT DO UPDATE``."""
    source_select = select(
        nodes_table.c.id,
        literal(False),
        literal(None),
        literal("stale"),
        literal(None),
    ).where(node_filter)

    ins = sqlite_insert(reference_refresh_table).from_select(
        [
            "target_symbol_id",
            "available",
            "last_refreshed_at",
            "refresh_status",
            "error_code",
        ],
        source_select,
    )
    stmt = ins.on_conflict_do_update(
        index_elements=[reference_refresh_table.c.target_symbol_id],
        set_={
            "available": False,
            "last_refreshed_at": None,
            "refresh_status": "stale",
            "error_code": None,
        },
    )
    conn.execute(stmt)


def mark_symbol_ids_stale(conn: Connection, symbol_ids: Iterable[str]) -> list[str]:
    """Mark ``reference_refresh`` entries stale for any existing nodes in ``symbol_ids``."""
    unique_ids = sorted({sid for sid in symbol_ids if sid})
    if not unique_ids:
        return []

    marked: list[str] = []
    for chunk in _chunked(unique_ids):
        existing_ids = [
            str(nid)
            for nid in conn.execute(
                select(nodes_table.c.id)
                .where(nodes_table.c.id.in_(chunk))
                .order_by(nodes_table.c.id)
            )
            .scalars()
            .all()
        ]
        if not existing_ids:
            continue

        _upsert_stale_from_node_select(conn, nodes_table.c.id.in_(chunk))
        marked.extend(existing_ids)

    return marked


def mark_symbols_in_file_stale(conn: Connection, file_id: str) -> list[str]:
    """Mark ``reference_refresh`` entries stale for all symbols declared in ``file_id``."""
    symbol_ids = [
        str(nid)
        for nid in conn.execute(
            select(nodes_table.c.id)
            .where(nodes_table.c.file_id == file_id)
            .order_by(nodes_table.c.id)
        )
        .scalars()
        .all()
    ]
    if not symbol_ids:
        return []

    _upsert_stale_from_node_select(conn, nodes_table.c.file_id == file_id)
    return symbol_ids


def invalidate_reference_summaries_for_file(conn: Connection, file_id: str) -> int:
    """Invalidate reference refresh states and remove reference edges for ``file_id``."""
    referenced_targets = select(edges_table.c.to_id).where(
        edges_table.c.kind == "references",
        edges_table.c.evidence_file_id == file_id,
    )
    _upsert_stale_from_node_select(
        conn,
        or_(
            nodes_table.c.file_id == file_id,
            nodes_table.c.id.in_(referenced_targets),
        ),
    )

    res = conn.execute(
        delete(edges_table).where(
            edges_table.c.kind == "references",
            edges_table.c.evidence_file_id == file_id,
        )
    )
    return max(res.rowcount or 0, 0)


def collect_impacted_symbol_ids(conn: Connection, file_id: str) -> list[str]:
    """Collect all symbol IDs declared in ``file_id`` or targeted by its ``references`` edges."""
    declared_ids = {
        str(nid)
        for nid in conn.execute(
            select(nodes_table.c.id).where(nodes_table.c.file_id == file_id)
        )
        .scalars()
        .all()
    }
    referenced_ids = {
        str(tid)
        for tid in conn.execute(
            select(edges_table.c.to_id)
            .distinct()
            .where(
                edges_table.c.kind == "references",
                edges_table.c.evidence_file_id == file_id,
            )
        )
        .scalars()
        .all()
    }
    return sorted(declared_ids | referenced_ids)
