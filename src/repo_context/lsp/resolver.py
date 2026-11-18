"""LSP query position resolver and file URI lookup using SQLAlchemy Core."""

from __future__ import annotations

import contextlib
import json
from pathlib import Path
from typing import Any
from urllib.parse import unquote, urlparse

from sqlalchemy import select
from sqlalchemy.engine import Connection

from repo_context.storage.schema import files_table, repos_table


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
    """Get the query position for finding references to a symbol."""
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


def _add_unique_uri(candidates: list[str], value: str) -> None:
    """Append ``value`` to ``candidates`` when non-empty and not yet present."""
    if value and value not in candidates:
        candidates.append(value)


def _add_path_uris(candidates: list[str], path: Path) -> None:
    """Append ``as_uri()`` and unquoted URI variants for an absolute ``Path``."""
    if not path.is_absolute():
        return
    with contextlib.suppress(ValueError):
        path_uri = path.as_uri()
        _add_unique_uri(candidates, path_uri)
        _add_unique_uri(candidates, unquote(path_uri))


def _normalize_parsed_uri_path(raw_path: str) -> Path:
    """Decode percent-escaped URI path and strip leading slash on Windows drive paths."""
    decoded = unquote(raw_path)
    if len(decoded) >= 3 and decoded[0] == "/" and decoded[2] == ":" and decoded[1].isalpha():
        decoded = decoded[1:]
    return Path(decoded)


def _extract_file_candidates(uri: str) -> tuple[list[str], Path | None]:
    """Build candidate file URIs and canonical filesystem path from a URI string."""
    candidates: list[str] = []
    _add_unique_uri(candidates, uri)
    _add_unique_uri(candidates, unquote(uri))

    parsed = urlparse(uri)
    if parsed.scheme not in ("file", "") or not parsed.path:
        return candidates, None

    raw_path = _normalize_parsed_uri_path(parsed.path)
    _add_path_uris(candidates, raw_path)

    try:
        resolved_path = raw_path.resolve()
    except OSError:
        resolved_path = raw_path
    _add_path_uris(candidates, resolved_path)

    return candidates, resolved_path


def _resolve_file_by_repo_relative(
    conn: Connection,
    resolved_path: Path,
    repo_id: str | None,
) -> dict[str, Any] | None:
    """Match ``resolved_path`` relative to registered repository roots."""
    repo_stmt = select(repos_table.c.id, repos_table.c.root_path)
    if repo_id is not None:
        repo_stmt = repo_stmt.where(repos_table.c.id == repo_id)

    for repo_row in conn.execute(repo_stmt).mappings().all():
        root_val = repo_row["root_path"]
        if not root_val:
            continue
        try:
            rel_path = resolved_path.relative_to(Path(root_val).resolve()).as_posix()
        except (OSError, ValueError):
            continue

        matched = (
            conn.execute(
                select(files_table)
                .where(
                    files_table.c.repo_id == repo_row["id"],
                    files_table.c.file_path == rel_path,
                )
                .limit(1)
            )
            .mappings()
            .first()
        )
        if matched is not None:
            return dict(matched)

    return None


def resolve_file_by_uri(
    conn: Connection,
    uri: str,
    repo_id: str | None = None,
) -> dict[str, Any] | None:
    """Resolve a file record by URI, percent-decoded URI, or canonical resolved path."""
    if not uri:
        return None

    candidate_uris, resolved_path = _extract_file_candidates(uri)
    stmt = select(files_table).where(files_table.c.uri.in_(candidate_uris))
    if repo_id is not None:
        stmt = stmt.where(files_table.c.repo_id == repo_id)
    row = conn.execute(stmt.limit(1)).mappings().first()
    if row is not None:
        return dict(row)

    if resolved_path is not None:
        return _resolve_file_by_repo_relative(conn, resolved_path, repo_id)

    return None
