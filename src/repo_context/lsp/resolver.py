"""LSP query position resolver and file URI lookup."""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any
from urllib.parse import unquote, urlparse


def _parse_range_field(value: Any) -> dict[str, Any] | None:
    """Parse a JSON range field if it is a string."""
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except json.JSONDecodeError:
            return None
    if isinstance(value, dict):
        return value
    return None


def get_reference_query_position(symbol: Any) -> dict[str, int]:
    """Get the query position for finding references to a symbol.

    Prefers selection_range_json.start (most precise - name only).
    Falls back to range_json.start (full declaration range).

    Args:
        symbol: Symbol dictionary or object with range_json and selection_range_json.

    Returns:
        Position dict with 'line' and 'character' keys.

    Raises:
        ValueError: If symbol has no usable range information.
    """
    if isinstance(symbol, dict):
        selection_range = symbol.get("selection_range_json")
        range_json = symbol.get("range_json")
        symbol_id = symbol.get("id", "unknown")
    else:
        selection_range = getattr(symbol, "selection_range_json", None)
        range_json = getattr(symbol, "range_json", None)
        symbol_id = getattr(symbol, "id", "unknown")

    selection_range = _parse_range_field(selection_range)
    range_json = _parse_range_field(range_json)

    if selection_range and isinstance(selection_range.get("start"), dict):
        return selection_range["start"]
    if range_json and isinstance(range_json.get("start"), dict):
        return range_json["start"]
    raise ValueError(f"Symbol {symbol_id} has no stored range for references query")


def _row_to_dict(cursor: sqlite3.Cursor, row: Any) -> dict[str, Any] | None:
    """Convert a SQLite row (Row or tuple) to a plain dict."""
    if row is None:
        return None
    if isinstance(row, dict):
        return row
    if hasattr(row, "keys"):
        return dict(row)
    columns = [col[0] for col in cursor.description]
    return dict(zip(columns, row))


def _extract_file_candidates(uri: str) -> tuple[list[str], Path | None]:
    """Build candidate file URIs and canonical filesystem path from a URI string."""
    candidates: list[str] = []

    def add_candidate(value: str) -> None:
        if value and value not in candidates:
            candidates.append(value)

    add_candidate(uri)
    unquoted_uri = unquote(uri)
    add_candidate(unquoted_uri)

    parsed = urlparse(uri)
    resolved_path: Path | None = None

    if parsed.scheme in ("file", "") and parsed.path:
        decoded_path = unquote(parsed.path)
        if len(decoded_path) >= 3 and decoded_path[0] == "/" and decoded_path[2] == ":" and decoded_path[1].isalpha():
            decoded_path = decoded_path[1:]

        raw_path = Path(decoded_path)
        if raw_path.is_absolute():
            try:
                raw_uri = raw_path.as_uri()
                add_candidate(raw_uri)
                add_candidate(unquote(raw_uri))
            except ValueError:
                pass

        try:
            resolved_path = raw_path.resolve()
        except OSError:
            resolved_path = raw_path

        if resolved_path.is_absolute():
            try:
                canonical_uri = resolved_path.as_uri()
                add_candidate(canonical_uri)
                add_candidate(unquote(canonical_uri))
            except ValueError:
                pass

    return candidates, resolved_path


def resolve_file_by_uri(
    conn: sqlite3.Connection,
    uri: str,
    repo_id: str | None = None,
) -> dict[str, Any] | None:
    """Resolve a file record by URI, percent-decoded URI, or canonical resolved path.

    Args:
        conn: SQLite connection.
        uri: File URI to look up.
        repo_id: Optional repository ID to scope the query.

    Returns:
        File record dict or None if not found.
    """
    if not uri:
        return None

    candidate_uris, resolved_path = _extract_file_candidates(uri)
    placeholders = ",".join("?" for _ in candidate_uris)

    if repo_id is not None:
        cursor = conn.execute(
            f"SELECT * FROM files WHERE repo_id = ? AND uri IN ({placeholders}) LIMIT 1",
            (repo_id, *candidate_uris),
        )
    else:
        cursor = conn.execute(
            f"SELECT * FROM files WHERE uri IN ({placeholders}) LIMIT 1",
            tuple(candidate_uris),
        )
    row = cursor.fetchone()
    if row is not None:
        return _row_to_dict(cursor, row)

    # Canonical path fallback via repos.root_path handles symlinked roots (e.g. /var -> /private/var)
    if resolved_path is not None:
        if repo_id is not None:
            repo_cursor = conn.execute(
                "SELECT id, root_path FROM repos WHERE id = ?",
                (repo_id,),
            )
        else:
            repo_cursor = conn.execute("SELECT id, root_path FROM repos")

        for repo_row in repo_cursor.fetchall():
            repo_dict = _row_to_dict(repo_cursor, repo_row)
            if not repo_dict or not repo_dict.get("root_path"):
                continue
            try:
                repo_root = Path(repo_dict["root_path"]).resolve()
                rel_path = resolved_path.relative_to(repo_root).as_posix()
            except (OSError, ValueError):
                continue

            file_cursor = conn.execute(
                "SELECT * FROM files WHERE repo_id = ? AND file_path = ? LIMIT 1",
                (repo_dict["id"], rel_path),
            )
            matched = file_cursor.fetchone()
            if matched is not None:
                return _row_to_dict(file_cursor, matched)

    return None
