"""LSP query position resolver and file URI lookup using SQLAlchemy Core."""

from __future__ import annotations

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
        if (
            len(decoded_path) >= 3
            and decoded_path[0] == "/"
            and decoded_path[2] == ":"
            and decoded_path[1].isalpha()
        ):
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
        repo_stmt = select(repos_table.c.id, repos_table.c.root_path)
        if repo_id is not None:
            repo_stmt = repo_stmt.where(repos_table.c.id == repo_id)

        for repo_row in conn.execute(repo_stmt).mappings().all():
            root_val = repo_row["root_path"]
            if not root_val:
                continue
            try:
                repo_root = Path(root_val).resolve()
                rel_path = resolved_path.relative_to(repo_root).as_posix()
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
