"""Handler for `rc refs`."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

from repo_context.config import get_config
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
from repo_context.storage import (
    connection_scope,
    get_file_by_id,
    get_repo_by_id,
    initialize_database,
    resolve_repo_id,
)
from repo_context.cli.utils import (
    extract_edge_line,
    extract_site_preview,
    resolve_db_path,
    resolve_symbol_target,
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


def cmd_refs(args: argparse.Namespace) -> int:
    """Show incoming/outgoing calls and references with optional live LSP refresh."""
    db_path = resolve_db_path(args)
    repo_arg: str | None = getattr(args, "repo", None)
    direction: str = getattr(args, "direction", "incoming")
    refresh: bool = bool(getattr(args, "refresh", False))
    limit = int(getattr(args, "limit", 50))
    offset = int(getattr(args, "offset", 0))
    id_or_name: str = args.id_or_name

    if limit < 1:
        raise ValidationError("--limit must be >= 1", context={"limit": limit})
    if offset < 0:
        raise ValidationError("--offset must be >= 0", context={"offset": offset})

    cfg = get_config(db_path=db_path)

    with connection_scope(
        db_path,
        read_only=not refresh,
        must_exist=True,
    ) as conn:
        initialize_database(conn)
        repo_id = (
            resolve_repo_id(conn, repo_arg)
            if (repo_arg is not None or not id_or_name.strip().startswith("sym:"))
            else None
        )
        target = resolve_symbol_target(conn, repo_id, id_or_name)
        repo_record = get_repo_by_id(conn, target["repo_id"])

        if refresh:
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

        edges = list_symbol_reference_edges(
            conn,
            target["id"],
            direction=direction,
            limit=limit,
            offset=offset,
        )
        total_count = count_symbol_reference_edges(
            conn,
            target["id"],
            direction=direction,
        )
        stats = build_reference_stats(conn, target["id"])

        node_ids: list[str] = []
        file_ids: list[str] = [target["file_id"]]
        for edge in edges:
            node_ids.append(edge["from_id"])
            node_ids.append(edge["to_id"])
            if edge.get("evidence_file_id"):
                file_ids.append(edge["evidence_file_id"])

        nodes_by_id = get_symbols_by_ids(conn, node_ids)
        files_by_id = get_files_by_ids(conn, file_ids)

        repo_root_path: Path | None = None
        if repo_record and repo_record.root_path:
            candidate_root = Path(repo_record.root_path)
            if candidate_root.is_dir():
                repo_root_path = candidate_root

        line_cache: dict[str, list[str] | None] = {}
        entries: list[dict[str, Any]] = []
        for edge in edges:
            ev_file = files_by_id.get(edge.get("evidence_file_id") or "")
            file_path = ev_file.get("file_path") if ev_file else None
            line_num = extract_edge_line(edge)
            preview = extract_site_preview(
                edge,
                repo_root_path,
                ev_file,
                cfg.max_file_size_bytes,
                line_cache,
            )

            if edge["from_id"] == target["id"] and edge["to_id"] == target["id"]:
                edge_dir = "self"
                peer_id = target["id"]
            elif edge["to_id"] == target["id"]:
                edge_dir = "incoming"
                peer_id = edge["from_id"]
            else:
                edge_dir = "outgoing"
                peer_id = edge["to_id"]

            peer_name = _resolve_peer_label(peer_id, nodes_by_id, edge)
            site_location = (
                f"{file_path}:{line_num}"
                if file_path and line_num is not None
                else (file_path or (f"line {line_num}" if line_num is not None else None))
            )
            entries.append(
                {
                    "direction": edge_dir,
                    "kind": edge["kind"],
                    "from_id": edge["from_id"],
                    "to_id": edge["to_id"],
                    "peer_id": peer_id,
                    "peer_name": peer_name,
                    "file_path": file_path,
                    "line": line_num,
                    "location": site_location,
                    "confidence": edge.get("confidence"),
                    "source": edge.get("source"),
                    "preview": preview,
                }
            )

    target_file = files_by_id.get(target["file_id"])
    target_summary = {
        "id": target["id"],
        "kind": target["kind"],
        "qualified_name": target["qualified_name"],
        "file_path": target_file.get("file_path") if target_file else None,
        "lines": format_line_span(target),
        "signature": target.get("signature"),
    }

    payload = {
        "symbol": target_summary,
        "direction": direction,
        "total_count": total_count,
        "returned_count": len(entries),
        "offset": offset,
        "limit": limit,
        "has_more": (offset + len(entries)) < total_count,
        "reference_summary": stats,
        "references": entries,
    }

    print(json.dumps(payload, indent=2))
    return 0
