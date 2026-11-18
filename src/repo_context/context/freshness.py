"""Freshness metadata and disk state verification for symbol context."""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime
from pathlib import Path
from typing import Any

from sqlalchemy import func, or_, select
from sqlalchemy.engine import Connection

from repo_context.context.helpers import is_lsp_enriched
from repo_context.graph.queries import check_file_disk_freshness
from repo_context.models import SymbolContext
from repo_context.storage.schema import edges_table


def _latest_timestamp(timestamps: Sequence[str | None]) -> str | None:
    """Return the latest non-empty ISO 8601 timestamp from ``timestamps``."""
    candidates = [ts for ts in timestamps if isinstance(ts, str) and ts.strip()]
    if not candidates:
        return None

    def _sort_key(ts: str) -> tuple[float, str]:
        try:
            return (datetime.fromisoformat(ts).timestamp(), ts)
        except ValueError:
            return (float("-inf"), ts)

    return max(candidates, key=_sort_key)


def _compute_edge_snapshot_timestamp(
    context: SymbolContext,
    focus_node: dict[str, Any],
    conn: Connection | None,
    raw_edges: Sequence[dict[str, Any]] | None,
) -> str | None:
    """Compute the latest ``last_indexed_at`` timestamp across edges for ``focus_node``."""
    if raw_edges is not None:
        return _latest_timestamp([edge.get("last_indexed_at") for edge in raw_edges])

    adapted_timestamps = [
        edge.get("last_indexed_at")
        for edge in [*context.incoming_edges, *context.outgoing_edges]
        if edge.get("last_indexed_at") is not None
    ]
    if adapted_timestamps:
        return _latest_timestamp(adapted_timestamps)

    if conn is not None and focus_node.get("id"):
        fid = focus_node["id"]
        latest = conn.execute(
            select(func.max(edges_table.c.last_indexed_at)).where(
                or_(edges_table.c.from_id == fid, edges_table.c.to_id == fid)
            )
        ).scalar_one_or_none()
        if latest is not None:
            return str(latest)

    return None


def build_freshness(
    context: SymbolContext,
    focus_node: dict[str, Any],
    conn: Connection | None = None,
    *,
    raw_edges: Sequence[dict[str, Any]] | None = None,
    file_record: dict[str, Any] | None = None,
    repo_root: str | Path | None = None,
) -> dict[str, Any]:
    """Build freshness metadata with real disk hash/existence and edge snapshot checks."""
    node_last_indexed_at = focus_node.get("last_indexed_at")
    edge_snapshot_timestamp = _compute_edge_snapshot_timestamp(
        context, focus_node, conn, raw_edges
    )

    if raw_edges is not None and focus_node.get("id"):
        focus_id = focus_node["id"]
        has_incoming = any(e.get("to_id") == focus_id for e in raw_edges)
        has_outgoing = any(e.get("from_id") == focus_id for e in raw_edges)
    else:
        has_incoming = len(context.incoming_edges) > 0
        has_outgoing = len(context.outgoing_edges) > 0

    if node_last_indexed_at is None:
        is_fresh = False
        stale_reason: str | None = "missing_last_indexed_at"
    elif (conn is not None and focus_node.get("file_id")) or (
        file_record is not None and repo_root is not None
    ):
        target_file_id = focus_node.get("file_id") or (file_record or {}).get("id", "")
        is_fresh, stale_reason = check_file_disk_freshness(
            conn,
            target_file_id,
            file_record=file_record,
            repo_root=repo_root,
        )
    else:
        is_fresh = True
        stale_reason = None

    context_source = "lsp+ast" if is_lsp_enriched(context, raw_edges) else "ast_only"

    return {
        "node_last_indexed_at": node_last_indexed_at,
        "edge_snapshot_last_indexed_at": edge_snapshot_timestamp,
        "has_incoming_edges": has_incoming,
        "has_outgoing_edges": has_outgoing,
        "context_source": context_source,
        "is_fresh": is_fresh,
        "stale_reason": stale_reason,
    }
