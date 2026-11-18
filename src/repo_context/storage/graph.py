"""Graph-level persistence helpers using SQLAlchemy Core savepoints."""

from __future__ import annotations

from typing import Any

from sqlalchemy.engine import Connection

from repo_context.storage.edges import delete_owned_edges_for_file, upsert_edges
from repo_context.storage.nodes import delete_nodes_for_file, upsert_nodes


def replace_file_graph(
    conn: Connection,
    file_id: str,
    nodes: list[dict[str, Any]],
    edges: list[dict[str, Any]],
) -> None:
    """Replace all nodes and edges owned by one file inside the caller's transaction.

    Callers control transaction boundaries (commit/rollback). When an outer transaction
    is already active, uses a nested SAVEPOINT so a failure rolls back only this file's
    changes without discarding earlier work in the transaction.
    """
    if conn.in_transaction():
        with conn.begin_nested():
            delete_owned_edges_for_file(conn, file_id)
            delete_nodes_for_file(conn, file_id)
            upsert_nodes(conn, nodes)  # type: ignore[arg-type]
            upsert_edges(conn, edges)  # type: ignore[arg-type]
        return

    try:
        delete_owned_edges_for_file(conn, file_id)
        delete_nodes_for_file(conn, file_id)
        upsert_nodes(conn, nodes)  # type: ignore[arg-type]
        upsert_edges(conn, edges)  # type: ignore[arg-type]
    except Exception:
        if conn.in_transaction():
            conn.rollback()
        raise
