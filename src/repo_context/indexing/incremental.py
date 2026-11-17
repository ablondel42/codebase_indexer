"""Incremental reindexing and deletion cleanup for changed files."""

from pathlib import Path
import sqlite3

from repo_context.config import AppConfig, get_config
from repo_context.exceptions import ParseError, PathSecurityError
from repo_context.indexing.events import _is_ignored_path
from repo_context.indexing.invalidation import (
    _chunked,
    invalidate_reference_summaries_for_file,
    mark_symbol_ids_stale,
    mark_symbols_in_file_stale,
)
from repo_context.logging_config import get_logger
from repo_context.parsing.linker import resolve_repo_edges
from repo_context.parsing.pathing import derive_file_id, derive_repo_id, ensure_safe_repo_file, to_relative_path
from repo_context.parsing.pipeline import extract_file_graph
from repo_context.parsing.scanner import build_file_record, build_repo_record, is_supported_source_file
from repo_context.storage import (
    get_file_by_id,
    get_file_by_path,
    get_repo_by_id,
    replace_file_graph,
    upsert_files,
    upsert_repo,
)

logger = get_logger("indexing.incremental")

_EDGE_DELETE_CHUNK_SIZE = 200


def _rollback_transaction(conn: sqlite3.Connection, savepoint_name: str | None) -> None:
    """Roll back either a nested savepoint or the active connection transaction."""
    if savepoint_name is not None:
        conn.execute(f"ROLLBACK TO SAVEPOINT {savepoint_name}")
        conn.execute(f"RELEASE SAVEPOINT {savepoint_name}")
    elif conn.in_transaction:
        conn.rollback()


def reindex_changed_file(
    conn: sqlite3.Connection,
    repo_root: Path,
    file_path: Path | str,
    config: AppConfig | None = None,
    *,
    commit: bool = True,
) -> dict:
    """Reindex a created or modified Python file inside ``repo_root``.

    Validates path safety with ``ensure_safe_repo_file``, parses and extracts the
    AST graph before mutating SQLite, updates graph and cross-file links inside
    a single transaction/savepoint, invalidates stale reference summaries, and
    commits when ``commit=True``.

    Args:
        conn: Writable SQLite connection.
        repo_root: Repository root path.
        file_path: Candidate file path (absolute or relative to ``repo_root``).
        config: Optional application configuration (defaults to ``get_config()``).
        commit: Whether to commit on success (default True).

    Returns:
        Structured summary dictionary containing ``file_path``, ``status``,
        ``node_count``, and ``edge_count``.
    """
    cfg = config or get_config()
    raw_root = Path(repo_root)
    resolved_root = raw_root.resolve()
    display_path = str(file_path)

    try:
        safe_path = ensure_safe_repo_file(raw_root, file_path)
    except PathSecurityError as exc:
        if isinstance(exc.__cause__, FileNotFoundError):
            logger.info("Skipping missing file during reindex: %s", file_path)
            return {
                "file_path": display_path,
                "status": "skipped",
                "node_count": 0,
                "edge_count": 0,
            }
        logger.exception("Path security check failed during reindex for %s", file_path)
        if commit and conn.in_transaction:
            conn.rollback()
        return {
            "file_path": display_path,
            "status": "error",
            "node_count": 0,
            "edge_count": 0,
        }
    except Exception:
        logger.exception("Unexpected error resolving file path during reindex for %s", file_path)
        if commit and conn.in_transaction:
            conn.rollback()
        return {
            "file_path": display_path,
            "status": "error",
            "node_count": 0,
            "edge_count": 0,
        }

    repo_relative = to_relative_path(resolved_root, safe_path)

    if not is_supported_source_file(safe_path, cfg.supported_extensions):
        return {
            "file_path": repo_relative,
            "status": "skipped",
            "node_count": 0,
            "edge_count": 0,
        }

    if _is_ignored_path(
        repo_relative,
        cfg.ignored_dirs,
        include_tests=cfg.include_tests,
        test_dir_names=cfg.test_dir_names,
    ):
        return {
            "file_path": repo_relative,
            "status": "skipped",
            "node_count": 0,
            "edge_count": 0,
        }

    repo_id = derive_repo_id(resolved_root)

    try:
        file_record = build_file_record(
            repo_id,
            resolved_root,
            safe_path,
            max_file_size_bytes=cfg.max_file_size_bytes,
            test_dir_names=cfg.test_dir_names,
        )
        nodes, edges, _summary = extract_file_graph(
            repo_id,
            file_record,
            resolved_root,
            config=cfg,
        )
    except (ParseError, SyntaxError):
        logger.exception("Parse failure reindexing %s; keeping previous graph", repo_relative)
        if commit and conn.in_transaction:
            conn.rollback()
        return {
            "file_path": repo_relative,
            "status": "parse_failed",
            "node_count": 0,
            "edge_count": 0,
        }
    except PathSecurityError as exc:
        if isinstance(exc.__cause__, FileNotFoundError):
            logger.info("File disappeared before extraction: %s", repo_relative)
            return {
                "file_path": repo_relative,
                "status": "skipped",
                "node_count": 0,
                "edge_count": 0,
            }
        logger.exception("Security error extracting %s", repo_relative)
        if commit and conn.in_transaction:
            conn.rollback()
        return {
            "file_path": repo_relative,
            "status": "error",
            "node_count": 0,
            "edge_count": 0,
        }
    except FileNotFoundError:
        logger.info("File disappeared before extraction: %s", repo_relative)
        return {
            "file_path": repo_relative,
            "status": "skipped",
            "node_count": 0,
            "edge_count": 0,
        }
    except Exception:
        logger.exception("Unexpected error extracting %s", repo_relative)
        if commit and conn.in_transaction:
            conn.rollback()
        return {
            "file_path": repo_relative,
            "status": "error",
            "node_count": 0,
            "edge_count": 0,
        }

    savepoint_name: str | None = "_reindex_changed_file" if (not commit and conn.in_transaction) else None
    if savepoint_name is not None:
        conn.execute(f"SAVEPOINT {savepoint_name}")

    try:
        if get_repo_by_id(conn, repo_id) is None:
            upsert_repo(conn, build_repo_record(resolved_root))

        # Invalidate reference refresh state and clear old reference edges before
        # replace_file_graph deletes edges with evidence_file_id = file_record.id.
        invalidated_count = invalidate_reference_summaries_for_file(conn, file_record.id)

        upsert_files(conn, [file_record])
        replace_file_graph(conn, file_record.id, nodes, edges)

        # Re-resolve repo-wide AST edges so surviving cross-file edges to removed
        # or renamed symbols reset cleanly while intact targets stay linked.
        link_stats = resolve_repo_edges(conn, repo_id)

        # Prune any cached LSP reference edges whose target symbol no longer exists.
        conn.execute(
            """
            DELETE FROM edges
            WHERE repo_id = ?
              AND kind = 'references'
              AND NOT EXISTS (
                  SELECT 1 FROM nodes n WHERE n.id = edges.to_id
              )
            """,
            (repo_id,),
        )

        mark_symbols_in_file_stale(conn, file_record.id)

        if savepoint_name is not None:
            conn.execute(f"RELEASE SAVEPOINT {savepoint_name}")
        elif commit:
            conn.commit()

        return {
            "file_path": repo_relative,
            "status": "reindexed",
            "node_count": len(nodes),
            "edge_count": len(edges),
            "invalidated_reference_edge_count": invalidated_count,
            "resolved_edge_count": link_stats.get("total_resolved", 0),
        }
    except Exception:
        logger.exception("Failed to persist reindex update for %s", repo_relative)
        _rollback_transaction(conn, savepoint_name)
        return {
            "file_path": repo_relative,
            "status": "error",
            "node_count": 0,
            "edge_count": 0,
        }


def handle_deleted_file(
    conn: sqlite3.Connection,
    repo_id: str,
    repo_relative_path: str,
    *,
    commit: bool = True,
) -> dict:
    """Clean up all graph state, edges, and stale target references for a deleted file.

    Args:
        conn: Writable SQLite connection.
        repo_id: Repository ID.
        repo_relative_path: Repository-relative POSIX path of the deleted file.
        commit: Whether to commit atomically on success (default True).

    Returns:
        Structured summary with deleted node/edge counts and invalidated external targets.
    """
    file_row = get_file_by_path(conn, repo_id, repo_relative_path)
    if file_row is None:
        fallback_id = derive_file_id(repo_id, repo_relative_path)
        file_row = get_file_by_id(conn, fallback_id)

    if file_row is None:
        return {
            "file_path": repo_relative_path,
            "status": "not_tracked",
            "deleted_node_count": 0,
            "deleted_edge_count": 0,
            "invalidated_target_symbol_count": 0,
        }

    file_id = file_row["id"]
    savepoint_name: str | None = "_handle_deleted_file" if (not commit and conn.in_transaction) else None
    if savepoint_name is not None:
        conn.execute(f"SAVEPOINT {savepoint_name}")

    try:
        # Collect local symbols and external impacted targets before deleting nodes/edges.
        symbol_ids = [
            row["id"]
            for row in conn.execute("SELECT id FROM nodes WHERE file_id = ?", (file_id,)).fetchall()
        ]
        symbol_id_set = set(symbol_ids)

        target_rows = conn.execute(
            "SELECT DISTINCT to_id FROM edges WHERE evidence_file_id = ?",
            (file_id,),
        ).fetchall()
        external_target_ids = [
            row["to_id"] for row in target_rows if row["to_id"] not in symbol_id_set
        ]

        # Delete all edges anchored on this file or referencing any symbol declared in it.
        deleted_edge_count = 0
        cursor = conn.execute(
            "DELETE FROM edges WHERE evidence_file_id = ?",
            (file_id,),
        )
        deleted_edge_count += max(cursor.rowcount, 0)

        for chunk in _chunked(symbol_ids, _EDGE_DELETE_CHUNK_SIZE):
            placeholders = ",".join("?" * len(chunk))
            params = tuple(chunk) + tuple(chunk)
            cursor = conn.execute(
                f"DELETE FROM edges WHERE from_id IN ({placeholders}) OR to_id IN ({placeholders})",
                params,
            )
            deleted_edge_count += max(cursor.rowcount, 0)

        # Mark surviving external target symbols stale in reference_refresh.
        marked_external = mark_symbol_ids_stale(conn, external_target_ids)

        # Delete nodes explicitly so FTS5 delete triggers fire, then delete the file row.
        conn.execute("DELETE FROM nodes WHERE file_id = ?", (file_id,))
        conn.execute("DELETE FROM files WHERE id = ?", (file_id,))

        if savepoint_name is not None:
            conn.execute(f"RELEASE SAVEPOINT {savepoint_name}")
        elif commit:
            conn.commit()

        return {
            "file_path": repo_relative_path,
            "status": "deleted",
            "deleted_node_count": len(symbol_ids),
            "deleted_edge_count": deleted_edge_count,
            "invalidated_target_symbol_count": len(marked_external),
        }
    except Exception:
        logger.exception("Failed to delete graph state for %s (%s)", repo_relative_path, file_id)
        _rollback_transaction(conn, savepoint_name)
        return {
            "file_path": repo_relative_path,
            "status": "error",
            "deleted_node_count": 0,
            "deleted_edge_count": 0,
            "invalidated_target_symbol_count": 0,
        }


def process_event_batch(
    conn: sqlite3.Connection,
    repo_root: Path,
    events: list,
    config: AppConfig | None = None,
) -> list[dict]:
    """Process a batch of collapsed file change events sequentially."""
    cfg = config or get_config()
    resolved_root = Path(repo_root).resolve()
    default_repo_id = derive_repo_id(resolved_root)
    results: list[dict] = []

    for event in events:
        if event.event_type == "deleted":
            repo_id = getattr(event, "repo_id", None) or default_repo_id
            summary = handle_deleted_file(conn, repo_id, event.repo_relative_path)
        else:
            summary = reindex_changed_file(conn, resolved_root, event.absolute_path, cfg)
        results.append(summary)

    return results
