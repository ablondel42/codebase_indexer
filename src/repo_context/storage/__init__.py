"""Storage layer for SQLite persistence."""

from repo_context.storage.db import (
    close_connection,
    connection_scope,
    get_connection,
    open_read_only_sql_connection,
)
from repo_context.storage.edges import (
    delete_edges_for_file,
    delete_owned_edges_for_file,
    edge_to_row,
    get_edge_by_id,
    list_edges_for_file,
    list_edges_for_repo,
    list_edges_for_target_and_kinds,
    list_incoming_edges,
    list_outgoing_edges,
    row_to_edge,
    upsert_edge,
    upsert_edges,
)
from repo_context.storage.files import (
    delete_files_not_in_set,
    get_file_by_id,
    get_file_by_path,
    list_files_for_repo,
    prune_stale_files,
    upsert_file,
    upsert_files,
)
from repo_context.storage.graph import replace_file_graph
from repo_context.storage.migrations import SCHEMA_VERSION, initialize_database
from repo_context.storage.nodes import (
    delete_nodes_for_file,
    find_nodes_by_name,
    get_node_by_id,
    get_node_by_qualified_name,
    list_child_nodes,
    list_lexical_children,
    list_nodes_for_file,
    list_nodes_for_repo,
    node_to_row,
    row_to_node,
    search_nodes_fts,
    upsert_node,
    upsert_nodes,
)
from repo_context.storage.reference_refresh import (
    get_reference_refresh_state,
    upsert_reference_refresh,
)
from repo_context.storage.repos import (
    get_repo_by_id,
    list_repos,
    resolve_repo_id,
    upsert_repo,
)
from repo_context.storage.runs import (
    list_index_runs,
    record_index_run_finish,
    record_index_run_start,
)

__all__ = [
    # Database
    "SCHEMA_VERSION",
    "get_connection",
    "open_read_only_sql_connection",
    "close_connection",
    "connection_scope",
    "initialize_database",
    # Repos
    "upsert_repo",
    "get_repo_by_id",
    "list_repos",
    "resolve_repo_id",
    # Files
    "upsert_file",
    "upsert_files",
    "list_files_for_repo",
    "delete_files_not_in_set",
    "prune_stale_files",
    "get_file_by_id",
    "get_file_by_path",
    # Nodes - row mapping
    "node_to_row",
    "row_to_node",
    # Nodes - upsert
    "upsert_node",
    "upsert_nodes",
    # Nodes - read
    "get_node_by_id",
    "get_node_by_qualified_name",
    "list_nodes_for_file",
    "list_nodes_for_repo",
    "find_nodes_by_name",
    "search_nodes_fts",
    "list_child_nodes",
    "list_lexical_children",
    # Nodes - delete
    "delete_nodes_for_file",
    # Edges - row mapping
    "edge_to_row",
    "row_to_edge",
    # Edges - upsert
    "upsert_edge",
    "upsert_edges",
    # Edges - read
    "get_edge_by_id",
    "list_edges_for_repo",
    "list_outgoing_edges",
    "list_incoming_edges",
    "list_edges_for_target_and_kinds",
    "list_edges_for_file",
    # Edges - delete
    "delete_edges_for_file",
    "delete_owned_edges_for_file",
    # Graph
    "replace_file_graph",
    # Reference refresh
    "upsert_reference_refresh",
    "get_reference_refresh_state",
    # Index runs
    "record_index_run_start",
    "record_index_run_finish",
    "list_index_runs",
]
