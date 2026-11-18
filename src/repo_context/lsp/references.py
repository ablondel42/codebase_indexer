"""LSP reference enrichment orchestrator using SQLAlchemy Core."""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from sqlalchemy import delete, select
from sqlalchemy.engine import Connection

from repo_context.config import get_config
from repo_context.exceptions import LspError, PathSecurityError
from repo_context.graph.references import build_reference_stats
from repo_context.logging_config import get_logger
from repo_context.lsp.mapper import (
    find_module_node_for_file,
    normalize_location,
    pick_smallest_containing_symbol,
    position_codepoint_to_utf16,
    range_utf16_to_codepoint,
)
from repo_context.lsp.resolver import get_reference_query_position, resolve_file_by_uri
from repo_context.parsing.pathing import ensure_safe_repo_file
from repo_context.storage import (
    edges_table,
    files_table,
    list_nodes_for_file,
    repos_table,
    upsert_edges,
    upsert_reference_refresh,
)

logger = get_logger("lsp.references")

CONFIDENCE_EXACT = 0.9
CONFIDENCE_MODULE = 0.7


def build_reference_edge(
    repo_id: str,
    from_id: str,
    to_id: str,
    confidence: float,
    evidence_file_id: str,
    evidence_uri: str,
    evidence_range_json: dict[str, Any],
    payload_json: dict[str, Any],
) -> dict[str, Any]:
    """Build a references edge dict."""
    start = evidence_range_json["start"]
    edge_id = f"edge:{repo_id}:references:{from_id}->{to_id}:{start['line']}:{start['character']}"
    return {
        "id": edge_id,
        "repo_id": repo_id,
        "kind": "references",
        "from_id": from_id,
        "to_id": to_id,
        "source": "lsp",
        "confidence": confidence,
        "evidence_file_id": evidence_file_id,
        "evidence_uri": evidence_uri,
        "evidence_range_json": evidence_range_json,
        "payload_json": payload_json,
        "last_indexed_at": datetime.now(timezone.utc).isoformat(),
    }


def load_symbols_for_file(conn: Connection, file_id: str) -> list[dict[str, Any]]:
    """Load all symbols for a file."""
    return list_nodes_for_file(conn, file_id)


def replace_reference_edges_for_target(
    conn: Connection,
    target_symbol_id: str,
    edges: list[dict[str, Any]],
    refresh_metadata: dict[str, Any],
) -> None:
    """Replace all reference edges for a target symbol transactionally."""

    def _apply() -> None:
        conn.execute(
            delete(edges_table).where(
                edges_table.c.kind == "references",
                edges_table.c.to_id == target_symbol_id,
            )
        )
        if edges:
            upsert_edges(conn, edges)
        upsert_reference_refresh(
            conn,
            refresh_metadata["target_symbol_id"],
            bool(refresh_metadata["available"]),
            refresh_metadata["last_refreshed_at"],
            refresh_metadata.get("refresh_status"),
            refresh_metadata.get("error_code"),
        )

    if conn.in_transaction():
        with conn.begin_nested():
            _apply()
    else:
        try:
            _apply()
            conn.commit()
        except Exception:
            if conn.in_transaction():
                conn.rollback()
            raise


def _resolve_repo_root(conn: Connection, target_symbol: dict[str, Any]) -> Path:
    """Resolve and verify the repository root for a target symbol."""
    raw_root = target_symbol.get("repo_root")
    if not raw_root and target_symbol.get("repo_id"):
        raw_root = conn.execute(
            select(repos_table.c.root_path)
            .where(repos_table.c.id == target_symbol["repo_id"])
            .limit(1)
        ).scalar_one_or_none()

    if not raw_root:
        raise LspError(
            f"Cannot determine repo_root for symbol {target_symbol.get('id', 'unknown')}.",
            context={"symbol_id": target_symbol.get("id"), "repo_id": target_symbol.get("repo_id")},
        )

    repo_root_path = Path(raw_root).resolve()
    if not repo_root_path.is_dir():
        raise LspError(
            f"Repository root directory does not exist: {repo_root_path}",
            context={"repo_root": str(repo_root_path)},
        )
    return repo_root_path


def _read_safe_repo_text(file_path: Path, max_bytes: int) -> str:
    """Read UTF-8 text from a validated file while enforcing size bounds."""
    try:
        size_bytes = file_path.stat().st_size
    except OSError as exc:
        raise LspError(f"Unable to stat file {file_path}: {exc}") from exc

    if size_bytes > max_bytes:
        raise LspError(
            f"File {file_path} ({size_bytes} bytes) exceeds max_file_size_bytes ({max_bytes}).",
            context={"file_path": str(file_path), "size_bytes": size_bytes, "limit": max_bytes},
        )

    try:
        return file_path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        raise LspError(f"Unable to read file {file_path}: {exc}") from exc


def _send_did_open(lsp_client: Any, uri: str, text: str) -> None:
    """Open or update a document on an LSP client or mock client."""
    if hasattr(lsp_client, "did_open_or_change"):
        lsp_client.did_open_or_change(uri, text)
    else:
        lsp_client.did_open(uri, text)


def _load_file_lines_for_mapping(
    repo_root: Path,
    file_row: dict[str, Any],
    max_bytes: int,
    cache: dict[str, list[str] | None],
) -> list[str] | None:
    """Safely load lines for a referenced file to convert UTF-16 ranges when present on disk."""
    file_id = file_row["id"]
    if file_id in cache:
        return cache[file_id]

    rel_path = file_row.get("file_path")
    if not rel_path:
        cache[file_id] = None
        return None

    try:
        safe_path = ensure_safe_repo_file(repo_root, repo_root / rel_path)
        text = _read_safe_repo_text(safe_path, max_bytes)
        lines = text.splitlines()
    except (PathSecurityError, LspError):
        lines = None

    cache[file_id] = lines
    return lines


def enrich_references_for_symbol(
    conn: Connection,
    lsp_client: Any,
    target_symbol: dict[str, Any],
    *,
    open_all_files: bool = False,
) -> dict[str, Any]:
    """Enrich references for a single target symbol."""
    cfg = get_config()

    position = get_reference_query_position(target_symbol)
    repo_root_path = _resolve_repo_root(conn, target_symbol)
    raw_file_path = target_symbol.get("file_path")
    if not raw_file_path:
        raise LspError(
            f"Target symbol {target_symbol.get('id', 'unknown')} is missing file_path."
        )

    candidate_path = repo_root_path / raw_file_path
    try:
        target_file_path = ensure_safe_repo_file(repo_root_path, candidate_path)
    except PathSecurityError:
        raise
    except OSError as exc:
        raise LspError(f"Target file is missing or inaccessible: {candidate_path}") from exc

    target_text = _read_safe_repo_text(target_file_path, cfg.max_file_size_bytes)
    target_lines = target_text.splitlines()
    query_position = position_codepoint_to_utf16(target_lines, position)

    lsp_client.start(str(repo_root_path))
    target_uri = target_symbol.get("uri") or target_file_path.as_uri()
    _send_did_open(lsp_client, target_uri, target_text)

    if open_all_files:
        rows = (
            conn.execute(
                select(files_table.c.file_path, files_table.c.uri).where(
                    files_table.c.repo_id == target_symbol["repo_id"],
                    files_table.c.file_path.like("%.py"),
                )
            )
            .mappings()
            .all()
        )
        for row in rows:
            rel_path = row["file_path"]
            uri = row["uri"]
            try:
                safe_aux_path = ensure_safe_repo_file(repo_root_path, repo_root_path / rel_path)
                if safe_aux_path == target_file_path:
                    continue
                aux_text = _read_safe_repo_text(safe_aux_path, cfg.max_file_size_bytes)
                _send_did_open(lsp_client, uri, aux_text)
            except (PathSecurityError, LspError, OSError) as exc:
                logger.debug("Skipping auxiliary file %s during didOpen: %s", rel_path, exc)

    locations = (
        lsp_client.find_references(
            target_uri,
            query_position["line"],
            query_position["character"],
            include_declaration=False,
        )
        or []
    )

    edges: list[dict[str, Any]] = []
    seen: set[tuple[str, str, str, int, int]] = set()
    file_by_uri_cache: dict[str, dict[str, Any] | None] = {}
    symbols_by_file_cache: dict[str, list[dict[str, Any]]] = {}
    file_lines_cache: dict[str, list[str] | None] = {target_symbol["file_id"]: target_lines}
    repo_id = target_symbol["repo_id"]

    for loc in locations:
        normalized = normalize_location(loc)
        loc_uri = normalized["uri"]
        if not loc_uri:
            continue

        if loc_uri not in file_by_uri_cache:
            file_by_uri_cache[loc_uri] = resolve_file_by_uri(conn, loc_uri, repo_id=repo_id)
        file_row = file_by_uri_cache[loc_uri]
        if not file_row:
            continue

        file_id = file_row["id"]
        if file_id not in symbols_by_file_cache:
            symbols_by_file_cache[file_id] = load_symbols_for_file(conn, file_id)
        symbols = symbols_by_file_cache[file_id]

        usage_range = normalized["range"]
        lines = _load_file_lines_for_mapping(
            repo_root_path,
            file_row,
            cfg.max_file_size_bytes,
            file_lines_cache,
        )
        if lines is not None:
            usage_range = range_utf16_to_codepoint(lines, usage_range)

        source_symbol = pick_smallest_containing_symbol(symbols, usage_range)
        mapping_mode = "exact_symbol"
        confidence = CONFIDENCE_EXACT

        if source_symbol is None:
            source_symbol = find_module_node_for_file(symbols)
            mapping_mode = "module_fallback"
            confidence = CONFIDENCE_MODULE

        if source_symbol is None:
            continue

        dedupe_key = (
            target_symbol["id"],
            source_symbol["id"],
            loc_uri,
            usage_range["start"]["line"],
            usage_range["start"]["character"],
        )
        if dedupe_key in seen:
            continue
        seen.add(dedupe_key)

        edge = build_reference_edge(
            repo_id=repo_id,
            from_id=source_symbol["id"],
            to_id=target_symbol["id"],
            confidence=confidence,
            evidence_file_id=file_id,
            evidence_uri=loc_uri,
            evidence_range_json=usage_range,
            payload_json={
                "mapping_mode": mapping_mode,
                "target_symbol_kind": target_symbol["kind"],
                "source_symbol_kind": source_symbol["kind"],
                "source_symbol_scope": source_symbol.get("scope"),
            },
        )
        edges.append(edge)

    refresh_metadata = {
        "target_symbol_id": target_symbol["id"],
        "available": True,
        "last_refreshed_at": datetime.now(timezone.utc).isoformat(),
        "refresh_status": "ok",
        "error_code": None,
    }

    replace_reference_edges_for_target(conn, target_symbol["id"], edges, refresh_metadata)
    return build_reference_stats(conn, target_symbol["id"])
