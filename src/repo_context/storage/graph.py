"""Graph-level persistence helpers."""

import sqlite3

from repo_context.storage.edges import delete_owned_edges_for_file, upsert_edges
from repo_context.storage.nodes import delete_nodes_for_file, upsert_nodes


def replace_file_graph(
    conn: sqlite3.Connection,
    file_id: str,
    nodes: list[dict],
    edges: list[dict],
) -> None:
    """Replace all nodes and edges owned by one file inside the caller's transaction.

    Callers control transaction boundaries (commit/rollback). When an outer transaction
    is already active, uses a SQLite SAVEPOINT so a failure rolls back only this file's
    changes without discarding earlier work in the transaction.
    """
    use_savepoint = conn.in_transaction
    if use_savepoint:
        conn.execute("SAVEPOINT _replace_file_graph")

    try:
        delete_owned_edges_for_file(conn, file_id)
        delete_nodes_for_file(conn, file_id)
        upsert_nodes(conn, nodes)  # type: ignore[arg-type]
        upsert_edges(conn, edges)  # type: ignore[arg-type]
    except Exception:
        if use_savepoint:
            conn.execute("ROLLBACK TO SAVEPOINT _replace_file_graph")
            conn.execute("RELEASE SAVEPOINT _replace_file_graph")
        elif conn.in_transaction:
            conn.rollback()
        raise
    else:
        if use_savepoint:
            conn.execute("RELEASE SAVEPOINT _replace_file_graph")
