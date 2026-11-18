"""Incremental reindexing and deletion cleanup for changed files using SQLAlchemy Core."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from sqlalchemy import delete, exists, or_, select
from sqlalchemy.engine import Connection

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
from repo_context.parsing.pathing import (
    derive_file_id,
    derive_repo_id,
    ensure_safe_repo_file,
    to_relative_path,
)
from repo_context.parsing.pipeline import extract_file_graph
from repo_context.parsing.scanner import (
    build_file_record,
    build_repo_record,
    is_supported_source_file,
)
from repo_context.storage import (
    edges_table,
    files_table,
    get_file_by_id,
    get_file_by_path,
    get_repo_by_id,
    nodes_table,
    replace_file_graph,
    upsert_files,
    upsert_repo,
)

logger = get_logger("indexing.incremental")

_EDGE_DELETE_CHUNK_SIZE = 200


@dataclass(frozen=True)
class _ExtractedFileGraph:
    """Intermediate extracted file metadata, nodes, and edges ready for persistence."""

    repo_id: str
    repo_relative: str
    file_record: Any
    nodes: list[Any]
    edges: list[Any]


def _rollback_if_committing(conn: Connection, *, commit: bool) -> None:
    """Roll back an active transaction when running in standalone commit mode."""
    if commit and conn.in_transaction():
        conn.rollback()


def _empty_reindex_summary(file_path: str, status: str) -> dict[str, Any]:
    """Build a zero-count reindex result dictionary."""
    return {
        "file_path": file_path,
        "status": status,
        "node_count": 0,
        "edge_count": 0,
    }


def _run_in_tx_scope(
    conn: Connection,
    operation: Callable[[], dict[str, Any]],
    *,
    commit: bool,
) -> dict[str, Any]:
    """Execute ``operation`` inside a savepoint when ``commit=False`` or commit on completion."""
    if not commit and conn.in_transaction():
        with conn.begin_nested():
            return operation()
    result = operation()
    if commit:
        conn.commit()
    return result


def _resolve_reindex_target(
    conn: Connection,
    raw_root: Path,
    file_path: Path | str,
    cfg: AppConfig,
    *,
    commit: bool,
) -> tuple[Path, str] | dict[str, Any]:
    """Validate path safety and filter rules; return ``(safe_path, repo_relative)`` or summary."""
    display_path = str(file_path)
    try:
        safe_path = ensure_safe_repo_file(raw_root, file_path)
    except PathSecurityError as exc:
        if isinstance(exc.__cause__, FileNotFoundError):
            logger.info("Skipping missing file during reindex: %s", file_path)
            return _empty_reindex_summary(display_path, "skipped")
        logger.exception("Path security check failed during reindex for %s", file_path)
        _rollback_if_committing(conn, commit=commit)
        return _empty_reindex_summary(display_path, "error")
    except Exception:
        logger.exception("Unexpected error resolving file path during reindex for %s", file_path)
        _rollback_if_committing(conn, commit=commit)
        return _empty_reindex_summary(display_path, "error")

    repo_relative = to_relative_path(raw_root.resolve(), safe_path)
    is_skipped = not is_supported_source_file(
        safe_path, cfg.supported_extensions
    ) or _is_ignored_path(
        repo_relative,
        cfg.ignored_dirs,
        include_tests=cfg.include_tests,
        test_dir_names=cfg.test_dir_names,
    )
    if is_skipped:
        return _empty_reindex_summary(repo_relative, "skipped")
    return safe_path, repo_relative


def _extract_reindex_payload(
    conn: Connection,
    resolved_root: Path,
    safe_path: Path,
    repo_relative: str,
    cfg: AppConfig,
    *,
    commit: bool,
) -> _ExtractedFileGraph | dict[str, Any]:
    """Parse file and build extracted graph payload or return early status summary."""
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
        return _ExtractedFileGraph(
            repo_id=repo_id,
            repo_relative=repo_relative,
            file_record=file_record,
            nodes=nodes,
            edges=edges,
        )
    except (ParseError, SyntaxError):
        logger.exception("Parse failure reindexing %s; keeping previous graph", repo_relative)
        _rollback_if_committing(conn, commit=commit)
        return _empty_reindex_summary(repo_relative, "parse_failed")
    except (FileNotFoundError, PathSecurityError) as exc:
        if isinstance(exc, FileNotFoundError) or isinstance(exc.__cause__, FileNotFoundError):
            logger.info("File disappeared before extraction: %s", repo_relative)
            return _empty_reindex_summary(repo_relative, "skipped")
        logger.exception("Security error extracting %s", repo_relative)
        _rollback_if_committing(conn, commit=commit)
        return _empty_reindex_summary(repo_relative, "error")
    except Exception:
        logger.exception("Unexpected error extracting %s", repo_relative)
        _rollback_if_committing(conn, commit=commit)
        return _empty_reindex_summary(repo_relative, "error")


def _persist_reindexed_file(
    conn: Connection,
    resolved_root: Path,
    extracted: _ExtractedFileGraph,
) -> dict[str, Any]:
    """Write extracted file graph, re-link repo edges, and prune dangling references."""
    if get_repo_by_id(conn, extracted.repo_id) is None:
        upsert_repo(conn, build_repo_record(resolved_root))

    invalidated_count = invalidate_reference_summaries_for_file(conn, extracted.file_record.id)
    upsert_files(conn, [extracted.file_record])
    replace_file_graph(conn, extracted.file_record.id, extracted.nodes, extracted.edges)
    link_stats = resolve_repo_edges(conn, extracted.repo_id)

    conn.execute(
        delete(edges_table).where(
            edges_table.c.repo_id == extracted.repo_id,
            edges_table.c.kind == "references",
            ~exists(select(1).where(nodes_table.c.id == edges_table.c.to_id)),
        )
    )
    mark_symbols_in_file_stale(conn, extracted.file_record.id)

    return {
        "file_path": extracted.repo_relative,
        "status": "reindexed",
        "node_count": len(extracted.nodes),
        "edge_count": len(extracted.edges),
        "invalidated_reference_edge_count": invalidated_count,
        "resolved_edge_count": link_stats.get("total_resolved", 0),
    }


def reindex_changed_file(
    conn: Connection,
    repo_root: Path,
    file_path: Path | str,
    config: AppConfig | None = None,
    *,
    commit: bool = True,
) -> dict[str, Any]:
    """Reindex a created or modified Python file inside ``repo_root``."""
    cfg = config or get_config()
    raw_root = Path(repo_root)
    resolved_root = raw_root.resolve()

    target = _resolve_reindex_target(conn, raw_root, file_path, cfg, commit=commit)
    if isinstance(target, dict):
        return target
    safe_path, repo_relative = target

    extracted = _extract_reindex_payload(
        conn, resolved_root, safe_path, repo_relative, cfg, commit=commit
    )
    if isinstance(extracted, dict):
        return extracted

    try:
        return _run_in_tx_scope(
            conn,
            lambda: _persist_reindexed_file(conn, resolved_root, extracted),
            commit=commit,
        )
    except Exception:
        logger.exception("Failed to persist reindex update for %s", repo_relative)
        _rollback_if_committing(conn, commit=commit)
        return _empty_reindex_summary(repo_relative, "error")


def _delete_file_graph_records(
    conn: Connection,
    file_id: str,
    repo_relative_path: str,
) -> dict[str, Any]:
    """Delete edges, nodes, and file row for ``file_id`` and mark external targets stale."""
    symbol_ids = [
        str(nid)
        for nid in conn.execute(
            select(nodes_table.c.id).where(nodes_table.c.file_id == file_id)
        )
        .scalars()
        .all()
    ]
    symbol_id_set = set(symbol_ids)

    target_ids = [
        str(tid)
        for tid in conn.execute(
            select(edges_table.c.to_id)
            .distinct()
            .where(edges_table.c.evidence_file_id == file_id)
        )
        .scalars()
        .all()
    ]
    external_target_ids = [tid for tid in target_ids if tid not in symbol_id_set]

    res = conn.execute(delete(edges_table).where(edges_table.c.evidence_file_id == file_id))
    deleted_edge_count = max(res.rowcount or 0, 0)

    for chunk in _chunked(symbol_ids, _EDGE_DELETE_CHUNK_SIZE):
        res = conn.execute(
            delete(edges_table).where(
                or_(
                    edges_table.c.from_id.in_(chunk),
                    edges_table.c.to_id.in_(chunk),
                )
            )
        )
        deleted_edge_count += max(res.rowcount or 0, 0)

    marked_external = mark_symbol_ids_stale(conn, external_target_ids)
    conn.execute(delete(nodes_table).where(nodes_table.c.file_id == file_id))
    conn.execute(delete(files_table).where(files_table.c.id == file_id))

    return {
        "file_path": repo_relative_path,
        "status": "deleted",
        "deleted_node_count": len(symbol_ids),
        "deleted_edge_count": deleted_edge_count,
        "invalidated_target_symbol_count": len(marked_external),
    }


def handle_deleted_file(
    conn: Connection,
    repo_id: str,
    repo_relative_path: str,
    *,
    commit: bool = True,
) -> dict[str, Any]:
    """Clean up all graph state, edges, and stale target references for a deleted file."""
    file_row = get_file_by_path(conn, repo_id, repo_relative_path)
    if file_row is None:
        file_row = get_file_by_id(conn, derive_file_id(repo_id, repo_relative_path))

    if file_row is None:
        return {
            "file_path": repo_relative_path,
            "status": "not_tracked",
            "deleted_node_count": 0,
            "deleted_edge_count": 0,
            "invalidated_target_symbol_count": 0,
        }

    file_id = str(file_row["id"])
    try:
        return _run_in_tx_scope(
            conn,
            lambda: _delete_file_graph_records(conn, file_id, repo_relative_path),
            commit=commit,
        )
    except Exception:
        logger.exception("Failed to delete graph state for %s (%s)", repo_relative_path, file_id)
        _rollback_if_committing(conn, commit=commit)
        return {
            "file_path": repo_relative_path,
            "status": "error",
            "deleted_node_count": 0,
            "deleted_edge_count": 0,
            "invalidated_target_symbol_count": 0,
        }


def process_event_batch(
    conn: Connection,
    repo_root: Path,
    events: list[Any],
    config: AppConfig | None = None,
) -> list[dict[str, Any]]:
    """Process a batch of collapsed file change events sequentially."""
    cfg = config or get_config()
    resolved_root = Path(repo_root).resolve()
    default_repo_id = derive_repo_id(resolved_root)
    results: list[dict[str, Any]] = []

    for event in events:
        if event.event_type == "deleted":
            repo_id = getattr(event, "repo_id", None) or default_repo_id
            summary = handle_deleted_file(conn, repo_id, event.repo_relative_path)
        else:
            summary = reindex_changed_file(conn, resolved_root, event.absolute_path, cfg)
        results.append(summary)

    return results
