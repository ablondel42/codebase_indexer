"""Freshness metadata and disk state verification for symbol context."""

from collections.abc import Sequence
from datetime import datetime
from pathlib import Path
import sqlite3

from repo_context.context.helpers import is_lsp_enriched
from repo_context.graph.queries import check_file_disk_freshness
from repo_context.models import SymbolContext


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
    focus_node: dict,
    conn: sqlite3.Connection | None,
    raw_edges: Sequence[dict] | None,
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
        row = conn.execute(
            """
            SELECT MAX(last_indexed_at) AS latest_ts
            FROM edges
            WHERE from_id = ? OR to_id = ?
            """,
            (focus_node["id"], focus_node["id"]),
        ).fetchone()
        if row is not None:
            return row["latest_ts"]

    return None


def build_freshness(
    context: SymbolContext,
    focus_node: dict,
    conn: sqlite3.Connection | None = None,
    *,
    raw_edges: Sequence[dict] | None = None,
    file_record: dict | None = None,
    repo_root: str | Path | None = None,
) -> dict:
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
