"""Shared CLI database, symbol resolution, and preview utilities."""

from __future__ import annotations

import argparse
from collections.abc import Sequence
from pathlib import Path
import sqlite3
from typing import Any

from repo_context.config import get_config
from repo_context.context.helpers import format_line_span
from repo_context.context.source_reader import _read_verified_file_text
from repo_context.exceptions import ParseError, PathSecurityError, RepoContextError, SymbolNotFoundError
from repo_context.graph.queries import get_files_by_ids
from repo_context.parsing.pathing import ensure_safe_repo_file
from repo_context.storage import get_node_by_id, resolve_repo_id
from repo_context.storage.nodes import row_to_node

_MAX_PREVIEW_CHARS = 120


def resolve_db_path(args: argparse.Namespace) -> Path:
    """Resolve explicit ``--db-path`` or fall back to ``AppConfig.db_path``."""
    raw = getattr(args, "db_path", None)
    if raw is not None:
        return Path(raw)
    return get_config().db_path


def _escape_like(value: str) -> str:
    """Escape SQLite LIKE metacharacters for ``ESCAPE '\\'``."""
    return value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def _build_candidate_entries(
    conn: sqlite3.Connection,
    candidates: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Format candidate symbols for ambiguity error reporting."""
    file_ids = [c.get("file_id") for c in candidates if c.get("file_id")]
    files_by_id = get_files_by_ids(conn, file_ids)
    items: list[dict[str, Any]] = []
    for cand in candidates[:25]:
        finfo = files_by_id.get(cand.get("file_id") or "")
        items.append(
            {
                "id": cand["id"],
                "kind": cand["kind"],
                "qualified_name": cand["qualified_name"],
                "file_path": finfo.get("file_path") if finfo else None,
                "lines": format_line_span(cand),
                "signature": cand.get("signature"),
            }
        )
    return items


def resolve_repo_for_cli(
    conn: sqlite3.Connection,
    repo_arg: str | None = None,
) -> str:
    """Resolve ``--repo`` explicitly or auto-select when one repo is indexed."""
    return resolve_repo_id(conn, repo_arg)


def resolve_symbol_target(
    conn: sqlite3.Connection,
    repo_id: str | None,
    symbol_ref: str,
) -> dict[str, Any]:
    """Resolve a symbol by ``sym:...`` ID, exact qualified name, exact name, or unique suffix."""
    cleaned = symbol_ref.strip()
    if not cleaned:
        raise SymbolNotFoundError("Symbol identifier must not be empty.")

    if cleaned.startswith("sym:"):
        node = get_node_by_id(conn, cleaned)
        if node is None or (repo_id is not None and node["repo_id"] != repo_id):
            raise SymbolNotFoundError(
                f"Symbol not found: {cleaned}",
                context={"symbol": cleaned, "repo_id": repo_id},
            )
        return node

    resolved_repo_id = repo_id if repo_id is not None else resolve_repo_id(conn, None)

    exact_qname_rows = conn.execute(
        "SELECT * FROM nodes WHERE repo_id = ? AND qualified_name = ? ORDER BY kind, id",
        (resolved_repo_id, cleaned),
    ).fetchall()
    if len(exact_qname_rows) == 1:
        return row_to_node(exact_qname_rows[0])
    if len(exact_qname_rows) > 1:
        candidates = [row_to_node(r) for r in exact_qname_rows]
        raise RepoContextError(
            f"Ambiguous symbol '{cleaned}' ({len(candidates)} matches).",
            error_code="ambiguous_symbol",
            context={"candidates": _build_candidate_entries(conn, candidates)},
        )

    exact_name_rows = conn.execute(
        "SELECT * FROM nodes WHERE repo_id = ? AND name = ? ORDER BY kind, qualified_name, id",
        (resolved_repo_id, cleaned),
    ).fetchall()
    if len(exact_name_rows) == 1:
        return row_to_node(exact_name_rows[0])
    if len(exact_name_rows) > 1:
        candidates = [row_to_node(r) for r in exact_name_rows]
        raise RepoContextError(
            f"Ambiguous symbol '{cleaned}' ({len(candidates)} matches).",
            error_code="ambiguous_symbol",
            context={"candidates": _build_candidate_entries(conn, candidates)},
        )

    suffix_pattern = f"%.{_escape_like(cleaned)}"
    suffix_rows = conn.execute(
        """
        SELECT * FROM nodes
        WHERE repo_id = ? AND qualified_name LIKE ? ESCAPE '\\'
        ORDER BY kind, qualified_name, id
        LIMIT 25
        """,
        (resolved_repo_id, suffix_pattern),
    ).fetchall()
    if len(suffix_rows) == 1:
        return row_to_node(suffix_rows[0])
    if len(suffix_rows) > 1:
        candidates = [row_to_node(r) for r in suffix_rows]
        raise RepoContextError(
            f"Ambiguous symbol '{cleaned}' ({len(candidates)} matches).",
            error_code="ambiguous_symbol",
            context={"candidates": _build_candidate_entries(conn, candidates)},
        )

    raise SymbolNotFoundError(
        f"Symbol not found: {cleaned}",
        context={"symbol": cleaned, "repo_id": resolved_repo_id},
    )


def resolve_symbol_targets(
    conn: sqlite3.Connection,
    repo_arg: str | None,
    symbol_refs: Sequence[str],
) -> tuple[str | None, list[dict[str, Any]]]:
    """Resolve one or more symbol references, preserving order and deduplicating IDs."""
    needs_repo = repo_arg is not None or any(not ref.strip().startswith("sym:") for ref in symbol_refs)
    resolved_repo_id: str | None = resolve_repo_id(conn, repo_arg) if needs_repo else None

    targets: list[dict[str, Any]] = []
    seen_ids: set[str] = set()
    for ref in symbol_refs:
        node = resolve_symbol_target(conn, resolved_repo_id, ref)
        if node["id"] not in seen_ids:
            seen_ids.add(node["id"])
            targets.append(node)
    return resolved_repo_id, targets


def extract_edge_line(edge: dict[str, Any]) -> int | None:
    """Return 1-based line number from ``edge['evidence_range_json']``."""
    rng = edge.get("evidence_range_json")
    if isinstance(rng, dict):
        start = rng.get("start")
        if isinstance(start, dict) and isinstance(start.get("line"), int):
            return start["line"] + 1
    return None


def _load_safe_file_lines(
    repo_root: Path | None,
    file_info: dict[str, Any] | None,
    max_bytes: int,
    cache: dict[str, list[str] | None],
) -> list[str] | None:
    """Safely read and cache file lines for reference site previews."""
    if repo_root is None or file_info is None:
        return None
    file_id = file_info["id"]
    if file_id in cache:
        return cache[file_id]

    rel_path = file_info.get("file_path")
    if not rel_path:
        cache[file_id] = None
        return None

    try:
        safe_path = ensure_safe_repo_file(repo_root, rel_path)
        text, _ = _read_verified_file_text(safe_path, max_file_size_bytes=max_bytes)
        lines = text.splitlines()
    except (PathSecurityError, ParseError, OSError):
        lines = None

    cache[file_id] = lines
    return lines


def extract_site_preview(
    edge: dict[str, Any],
    repo_root: Path | None,
    file_info: dict[str, Any] | None,
    max_bytes: int,
    cache: dict[str, list[str] | None],
    *,
    max_chars: int = _MAX_PREVIEW_CHARS,
) -> str | None:
    """Extract a 1-line trimmed source preview at an edge's evidence line."""
    rng = edge.get("evidence_range_json")
    if not isinstance(rng, dict):
        return None
    start = rng.get("start")
    if not isinstance(start, dict):
        return None
    line_idx = start.get("line")
    if not isinstance(line_idx, int) or line_idx < 0:
        return None

    lines = _load_safe_file_lines(repo_root, file_info, max_bytes, cache)
    if not lines or line_idx >= len(lines):
        return None

    trimmed = lines[line_idx].strip()
    if not trimmed:
        return None
    if len(trimmed) > max_chars:
        return trimmed[: max_chars - 3] + "..."
    return trimmed


def format_freshness_label(is_fresh: bool, stale_reason: str | None) -> str:
    """Convert ``check_file_disk_freshness`` output into ``fresh``/``modified``/``deleted``."""
    if is_fresh:
        return "fresh"
    if stale_reason == "file_missing":
        return "deleted"
    if stale_reason == "content_hash_mismatch":
        return "modified"
    return stale_reason or "stale"
