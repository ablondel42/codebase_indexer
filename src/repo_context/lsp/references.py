"""LSP reference enrichment orchestrator."""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

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

logger = get_logger("lsp.references")

# Confidence levels
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
    """Build a references edge dict.

    Edge ID format: edge:{repo_id}:references:{from_id}->{to_id}:{line}:{character}
    """
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


def load_symbols_for_file(conn: sqlite3.Connection, file_id: str) -> list[dict[str, Any]]:
    """Load all symbols for a file."""
    cursor = conn.execute(
        "SELECT * FROM nodes WHERE file_id = ?",
        (file_id,),
    )
    rows = cursor.fetchall()
    if not rows:
        return []
    if hasattr(rows[0], "keys"):
        return [dict(row) for row in rows]
    columns = [col[0] for col in cursor.description]
    return [dict(zip(columns, row)) for row in rows]


def _encode_json_once(value: Any, *, default_json: str | None = None) -> str | None:
    """Serialize a dict/list to JSON once, preserving already-serialized strings."""
    if value is None:
        return default_json
    if isinstance(value, str):
        return value
    return json.dumps(value, sort_keys=True)


def replace_reference_edges_for_target(
    conn: sqlite3.Connection,
    target_symbol_id: str,
    edges: list[dict[str, Any]],
    refresh_metadata: dict[str, Any],
) -> None:
    """Replace all reference edges for a target symbol transactionally."""

    def _apply() -> None:
        conn.execute(
            "DELETE FROM edges WHERE kind = 'references' AND to_id = ?",
            (target_symbol_id,),
        )

        if edges:
            rows = [
                (
                    edge["id"],
                    edge["repo_id"],
                    edge["kind"],
                    edge["from_id"],
                    edge["to_id"],
                    edge["source"],
                    edge["confidence"],
                    edge["evidence_file_id"],
                    edge["evidence_uri"],
                    _encode_json_once(edge.get("evidence_range_json")),
                    _encode_json_once(edge.get("payload_json"), default_json="{}"),
                    edge["last_indexed_at"],
                )
                for edge in edges
            ]
            conn.executemany(
                """
                INSERT INTO edges (
                    id, repo_id, kind, from_id, to_id, source, confidence,
                    evidence_file_id, evidence_uri, evidence_range_json,
                    payload_json, last_indexed_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                rows,
            )

        conn.execute(
            """
            INSERT INTO reference_refresh (
                target_symbol_id, available, last_refreshed_at, refresh_status, error_code
            ) VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(target_symbol_id) DO UPDATE SET
                available = excluded.available,
                last_refreshed_at = excluded.last_refreshed_at,
                refresh_status = excluded.refresh_status,
                error_code = excluded.error_code
            """,
            (
                refresh_metadata["target_symbol_id"],
                int(bool(refresh_metadata["available"])),
                refresh_metadata["last_refreshed_at"],
                refresh_metadata.get("refresh_status"),
                refresh_metadata.get("error_code"),
            ),
        )

    if conn.in_transaction:
        conn.execute("SAVEPOINT sp_replace_reference_edges")
        try:
            _apply()
        except Exception:
            conn.execute("ROLLBACK TO SAVEPOINT sp_replace_reference_edges")
            conn.execute("RELEASE SAVEPOINT sp_replace_reference_edges")
            raise
        conn.execute("RELEASE SAVEPOINT sp_replace_reference_edges")
    else:
        with conn:
            _apply()


def _resolve_repo_root(conn: sqlite3.Connection, target_symbol: dict[str, Any]) -> Path:
    """Resolve and verify the repository root for a target symbol."""
    raw_root = target_symbol.get("repo_root")
    if not raw_root and target_symbol.get("repo_id"):
        row = conn.execute(
            "SELECT root_path FROM repos WHERE id = ? LIMIT 1",
            (target_symbol["repo_id"],),
        ).fetchone()
        if row is not None:
            raw_root = row["root_path"] if hasattr(row, "keys") else row[0]

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
    conn: sqlite3.Connection,
    lsp_client: Any,
    target_symbol: dict[str, Any],
    *,
    open_all_files: bool = False,
) -> dict[str, Any]:
    """Enrich references for a single target symbol.

    Args:
        conn: SQLite connection.
        lsp_client: PyrightLspClient instance (or compatible mock).
        target_symbol: Target symbol dict with id, repo_id, file_id, file_path,
            uri, qualified_name, kind, scope, range_json, selection_range_json,
            and optional repo_root.
        open_all_files: If True, also open all tracked .py files in the repo;
            defaults to False because Pyright indexes via rootUri.

    Returns:
        Reference stats dict from build_reference_stats.
    """
    cfg = get_config()

    # Step 1: Resolve query position (in Python codepoint offsets)
    position = get_reference_query_position(target_symbol)

    # Step 2: Resolve repo_root and validate target file path inside repo
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

    # Step 3: Ensure client started & open target document
    lsp_client.start(str(repo_root_path))
    target_uri = target_symbol.get("uri") or target_file_path.as_uri()
    _send_did_open(lsp_client, target_uri, target_text)

    # Step 4: Optionally open all tracked .py files in this repository
    if open_all_files:
        rows = conn.execute(
            "SELECT file_path, uri FROM files WHERE repo_id = ? AND file_path LIKE '%.py'",
            (target_symbol["repo_id"],),
        ).fetchall()
        for row in rows:
            rel_path = row["file_path"] if hasattr(row, "keys") else row[0]
            uri = row["uri"] if hasattr(row, "keys") else row[1]
            try:
                safe_aux_path = ensure_safe_repo_file(repo_root_path, repo_root_path / rel_path)
                if safe_aux_path == target_file_path:
                    continue
                aux_text = _read_safe_repo_text(safe_aux_path, cfg.max_file_size_bytes)
                _send_did_open(lsp_client, uri, aux_text)
            except (PathSecurityError, LspError, OSError) as exc:
                logger.debug("Skipping auxiliary file %s during didOpen: %s", rel_path, exc)

    # Step 5: Request references from LSP server
    locations = (
        lsp_client.find_references(
            target_uri,
            query_position["line"],
            query_position["character"],
            include_declaration=False,
        )
        or []
    )

    # Step 6: Map locations to symbols using per-call memoized caches
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
