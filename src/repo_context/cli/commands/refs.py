"""Handler for `rc refs`."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

from sqlalchemy.engine import Connection

from repo_context.cli.utils import (
    extract_edge_line,
    extract_site_preview,
    resolve_db_path,
    resolve_symbol_target,
)
from repo_context.config import AppConfig, get_config
from repo_context.context.helpers import format_line_span
from repo_context.exceptions import DatabaseError, ValidationError
from repo_context.graph import (
    build_reference_stats,
    count_symbol_reference_edges,
    get_files_by_ids,
    get_symbols_by_ids,
    list_symbol_reference_edges,
)
from repo_context.lsp import LspSessionPool
from repo_context.lsp.references import enrich_references_for_symbol
from repo_context.models.repo import RepoRecord
from repo_context.storage import (
    connection_scope,
    get_file_by_id,
    get_repo_by_id,
    initialize_database,
    resolve_repo_id,
)


def _resolve_peer_label(
    node_id: str,
    nodes_by_id: dict[str, dict[str, Any]],
    edge: dict[str, Any],
) -> str:
    """Resolve human-readable peer label for a reference or dependency edge."""
    node = nodes_by_id.get(node_id)
    if node is not None:
        return node.get("qualified_name") or node.get("name") or node_id
    for prefix in ("external_or_unresolved:", "unresolved_base:", "unresolved_call:"):
        if node_id.startswith(prefix):
            return node_id[len(prefix) :]
    payload = edge.get("payload_json")
    if isinstance(payload, dict):
        return (
            payload.get("target")
            or payload.get("base_name")
            or payload.get("module")
            or node_id
        )
    return node_id


def _refresh_target_lsp(
    conn: Connection,
    target: dict[str, Any],
    repo_record: RepoRecord | None,
    cfg: AppConfig,
) -> None:
    """Refresh LSP reference edges for ``target`` using a temporary ``LspSessionPool``."""
    file_record = get_file_by_id(conn, target["file_id"])
    if repo_record is None or file_record is None:
        raise DatabaseError(
            "Repository or file metadata missing for LSP refresh.",
            error_code="references_unavailable",
            context={"symbol_id": target["id"]},
        )
    target_symbol = {
        "id": target["id"],
        "repo_id": target["repo_id"],
        "file_id": target["file_id"],
        "file_path": file_record["file_path"],
        "uri": target["uri"],
        "qualified_name": target["qualified_name"],
        "kind": target["kind"],
        "scope": target.get("scope"),
        "range_json": target.get("range_json"),
        "selection_range_json": target.get("selection_range_json"),
        "repo_root": repo_record.root_path,
    }
    with LspSessionPool(config=cfg) as pool:
        client = pool.get_or_start(repo_record.root_path)
        enrich_references_for_symbol(
            conn,
            client,
            target_symbol,
            open_all_files=False,
        )


def _resolve_repo_root_dir(repo_record: RepoRecord | None) -> Path | None:
    """Return existing repository root directory path if available."""
    if repo_record and repo_record.root_path:
        candidate_root = Path(repo_record.root_path)
        if candidate_root.is_dir():
            return candidate_root
    return None


def _collect_graph_lookups(
    conn: Connection,
    target_file_id: str,
    edges: list[dict[str, Any]],
) -> tuple[dict[str, dict[str, Any]], dict[str, dict[str, Any]]]:
    """Batch-load endpoint symbol records and evidence file metadata for ``edges``."""
    node_ids: list[str] = []
    file_ids: list[str] = [target_file_id]
    for edge in edges:
        node_ids.append(edge["from_id"])
        node_ids.append(edge["to_id"])
        if edge.get("evidence_file_id"):
            file_ids.append(edge["evidence_file_id"])
    return get_symbols_by_ids(conn, node_ids), get_files_by_ids(conn, file_ids)


def _classify_edge_peer(edge: dict[str, Any], target_id: str) -> tuple[str, str]:
    """Classify edge direction relative to ``target_id`` and identify the peer node ID."""
    if edge["from_id"] == target_id and edge["to_id"] == target_id:
        return "self", target_id
    if edge["to_id"] == target_id:
        return "incoming", edge["from_id"]
    return "outgoing", edge["to_id"]


def _format_site_location(file_path: str | None, line_num: int | None) -> str | None:
    """Format a compact ``file:line`` reference site label."""
    if file_path and line_num is not None:
        return f"{file_path}:{line_num}"
    if file_path:
        return file_path
    if line_num is not None:
        return f"line {line_num}"
    return None


def _build_single_ref_entry(
    edge: dict[str, Any],
    target_id: str,
    nodes_by_id: dict[str, dict[str, Any]],
    ev_file: dict[str, Any] | None,
    preview: str | None,
) -> dict[str, Any]:
    """Format one reference/call edge record for CLI JSON output."""
    file_path = ev_file.get("file_path") if ev_file else None
    line_num = extract_edge_line(edge)
    edge_dir, peer_id = _classify_edge_peer(edge, target_id)
    return {
        "direction": edge_dir,
        "kind": edge["kind"],
        "from_id": edge["from_id"],
        "to_id": edge["to_id"],
        "peer_id": peer_id,
        "peer_name": _resolve_peer_label(peer_id, nodes_by_id, edge),
        "file_path": file_path,
        "line": line_num,
        "location": _format_site_location(file_path, line_num),
        "confidence": edge.get("confidence"),
        "source": edge.get("source"),
        "preview": preview,
    }


def _build_reference_entries(
    conn: Connection,
    target: dict[str, Any],
    edges: list[dict[str, Any]],
    repo_root_path: Path | None,
    max_file_size_bytes: int,
) -> tuple[dict[str, dict[str, Any]], list[dict[str, Any]]]:
    """Build formatted CLI reference entries with 1-line source previews."""
    nodes_by_id, files_by_id = _collect_graph_lookups(conn, target["file_id"], edges)
    line_cache: dict[str, list[str] | None] = {}
    entries: list[dict[str, Any]] = []
    for edge in edges:
        ev_file = files_by_id.get(edge.get("evidence_file_id") or "")
        entries.append(
            _build_single_ref_entry(
                edge,
                target["id"],
                nodes_by_id,
                ev_file,
                extract_site_preview(
                    edge,
                    repo_root_path,
                    ev_file,
                    max_file_size_bytes,
                    line_cache,
                ),
            )
        )
    return files_by_id, entries


def _query_refs_payload(
    conn: Connection,
    args: argparse.Namespace,
    cfg: AppConfig,
) -> dict[str, Any]:
    """Resolve target symbol, optionally refresh LSP, and assemble the ``rc refs`` payload."""
    direction: str = getattr(args, "direction", "incoming")
    limit = int(getattr(args, "limit", 50))
    offset = int(getattr(args, "offset", 0))
    id_or_name: str = args.id_or_name
    repo_arg: str | None = getattr(args, "repo", None)

    target = resolve_symbol_target(
        conn,
        resolve_repo_id(conn, repo_arg)
        if (repo_arg is not None or not id_or_name.strip().startswith("sym:"))
        else None,
        id_or_name,
    )
    repo_record = get_repo_by_id(conn, target["repo_id"])

    if bool(getattr(args, "refresh", False)):
        _refresh_target_lsp(conn, target, repo_record, cfg)

    total_count = count_symbol_reference_edges(conn, target["id"], direction=direction)
    files_by_id, entries = _build_reference_entries(
        conn,
        target,
        list_symbol_reference_edges(
            conn,
            target["id"],
            direction=direction,
            limit=limit,
            offset=offset,
        ),
        _resolve_repo_root_dir(repo_record),
        cfg.max_file_size_bytes,
    )
    target_file = files_by_id.get(target["file_id"])
    return {
        "symbol": {
            "id": target["id"],
            "kind": target["kind"],
            "qualified_name": target["qualified_name"],
            "file_path": target_file.get("file_path") if target_file else None,
            "lines": format_line_span(target),
            "signature": target.get("signature"),
        },
        "direction": direction,
        "total_count": total_count,
        "returned_count": len(entries),
        "offset": offset,
        "limit": limit,
        "has_more": (offset + len(entries)) < total_count,
        "reference_summary": build_reference_stats(conn, target["id"]),
        "references": entries,
    }


def cmd_refs(args: argparse.Namespace) -> int:
    """Show incoming/outgoing calls and references with optional live LSP refresh."""
    limit = int(getattr(args, "limit", 50))
    offset = int(getattr(args, "offset", 0))
    if limit < 1:
        raise ValidationError("--limit must be >= 1", context={"limit": limit})
    if offset < 0:
        raise ValidationError("--offset must be >= 0", context={"offset": offset})

    db_path = resolve_db_path(args)
    cfg = get_config(db_path=db_path)
    refresh = bool(getattr(args, "refresh", False))

    with connection_scope(db_path, read_only=not refresh, must_exist=True) as conn:
        initialize_database(conn)
        payload = _query_refs_payload(conn, args, cfg)

    print(json.dumps(payload, indent=2))
    return 0
