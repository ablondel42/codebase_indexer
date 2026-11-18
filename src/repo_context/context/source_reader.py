"""Safe, line-bounded source reader for indexed symbols and file snippets."""

import hashlib
from pathlib import Path

from sqlalchemy.engine import Connection

from repo_context.config import get_config
from repo_context.context.helpers import format_line_span
from repo_context.exceptions import (
    DatabaseError,
    ParseError,
    SymbolNotFoundError,
    ValidationError,
)
from repo_context.parsing.pathing import ensure_safe_repo_file
from repo_context.storage.files import get_file_by_id
from repo_context.storage.nodes import get_node_by_id
from repo_context.storage.repos import get_repo_by_id


def _read_verified_file_text(
    safe_path: Path,
    *,
    max_file_size_bytes: int,
) -> tuple[str, str]:
    """Read UTF-8 file text and compute raw-byte SHA-256 hash safely."""
    try:
        size_bytes = safe_path.stat().st_size
    except OSError as exc:
        raise ParseError(
            f"Unable to stat file {safe_path}: {exc}",
            context={"file_path": str(safe_path)},
        ) from exc

    if size_bytes > max_file_size_bytes:
        raise ParseError(
            f"File exceeds max_file_size_bytes ({max_file_size_bytes}): {safe_path}",
            context={"file_path": str(safe_path), "size_bytes": size_bytes},
        )

    try:
        raw_bytes = safe_path.read_bytes()
    except OSError as exc:
        raise ParseError(
            f"Unable to read file {safe_path}: {exc}",
            context={"file_path": str(safe_path)},
        ) from exc

    if len(raw_bytes) > max_file_size_bytes:
        raise ParseError(
            f"File exceeds max_file_size_bytes ({max_file_size_bytes}): {safe_path}",
            context={"file_path": str(safe_path), "size_bytes": len(raw_bytes)},
        )

    try:
        text = raw_bytes.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ParseError(
            f"Failed to decode UTF-8 file {safe_path}: {exc}",
            context={"file_path": str(safe_path)},
        ) from exc

    content_hash = f"sha256:{hashlib.sha256(raw_bytes).hexdigest()}"
    return text, content_hash


def read_file_snippet(
    repo_root: Path | str,
    file_path: str,
    start_line: int,
    end_line: int,
    *,
    max_lines: int = 120,
    offset: int = 0,
    expected_content_hash: str | None = None,
    max_file_size_bytes: int | None = None,
) -> dict:
    """Safely read a line-numbered 1-based slice from a repository file.

    Enforces symlink/root confinement via ``ensure_safe_repo_file`` and verifies
    disk freshness against ``expected_content_hash`` when provided.
    """
    if start_line < 1:
        raise ValidationError("start_line must be >= 1", context={"start_line": start_line})
    if end_line < start_line:
        raise ValidationError(
            "end_line must be >= start_line",
            context={"start_line": start_line, "end_line": end_line},
        )
    if max_lines < 1:
        raise ValidationError("max_lines must be >= 1", context={"max_lines": max_lines})
    if offset < 0:
        raise ValidationError("offset must be >= 0", context={"offset": offset})

    safe_path = ensure_safe_repo_file(Path(repo_root), file_path)
    limit = (
        max_file_size_bytes
        if max_file_size_bytes is not None
        else get_config().max_file_size_bytes
    )
    text, actual_hash = _read_verified_file_text(safe_path, max_file_size_bytes=limit)

    if expected_content_hash is not None:
        is_fresh = actual_hash == expected_content_hash
        stale_reason = None if is_fresh else "content_hash_mismatch"
    else:
        is_fresh = True
        stale_reason = None

    all_lines = text.splitlines()
    if not all_lines or start_line > len(all_lines):
        return {
            "file_path": file_path,
            "start_line": start_line,
            "end_line": min(end_line, len(all_lines)),
            "source": "",
            "truncated": False,
            "total_lines": 0,
            "returned_lines": 0,
            "next_offset": None,
            "is_fresh": is_fresh,
            "stale_reason": stale_reason,
        }

    clamped_start = max(1, start_line)
    clamped_end = min(end_line, len(all_lines))
    slice_lines = all_lines[clamped_start - 1 : clamped_end]
    total_lines = len(slice_lines)

    window = slice_lines[offset : offset + max_lines]
    returned_lines = len(window)
    truncated = (offset + returned_lines) < total_lines
    next_offset = (offset + returned_lines) if truncated else None

    numbered = [
        f"{clamped_start + offset + idx}: {line}"
        for idx, line in enumerate(window)
    ]

    return {
        "file_path": file_path,
        "start_line": clamped_start,
        "end_line": clamped_end,
        "source": "\n".join(numbered),
        "truncated": truncated,
        "total_lines": total_lines,
        "returned_lines": returned_lines,
        "next_offset": next_offset,
        "is_fresh": is_fresh,
        "stale_reason": stale_reason,
    }


def _resolve_symbol_line_bounds(node: dict) -> tuple[int, int]:
    """Resolve 1-based inclusive start and end lines for a symbol node."""
    start_line = node.get("start_line")
    end_line = node.get("end_line")
    if isinstance(start_line, int) and isinstance(end_line, int):
        return start_line, end_line

    rng = node.get("range_json")
    if isinstance(rng, dict):
        start = rng.get("start")
        end = rng.get("end")
        if isinstance(start, dict) and isinstance(end, dict):
            s_line = start.get("line")
            e_line = end.get("line")
            if isinstance(s_line, int) and isinstance(e_line, int):
                return s_line + 1, e_line + 1

    raise ValidationError(
        f"Symbol {node.get('id', 'unknown')} has no line range metadata.",
        context={"symbol_id": node.get("id")},
    )


def read_symbol_source(
    conn: Connection,
    symbol_id: str,
    *,
    max_lines: int = 120,
    offset: int = 0,
) -> dict:
    """Safely read line-numbered source code for an indexed symbol.

    Resolves ``repos.root_path`` and ``files.file_path``, enforces repository
    path confinement, verifies disk hash freshness, and slices the 1-based
    ``start_line..end_line`` declaration range (inclusive of decorators).
    """
    node = get_node_by_id(conn, symbol_id)
    if node is None:
        raise SymbolNotFoundError(
            f"Symbol not found: {symbol_id}",
            context={"symbol_id": symbol_id},
        )

    file_record = get_file_by_id(conn, node["file_id"])
    if file_record is None:
        raise DatabaseError(
            f"File record not found for symbol: {symbol_id}",
            error_code="file_not_found",
            context={"symbol_id": symbol_id, "file_id": node["file_id"]},
        )

    repo_record = get_repo_by_id(conn, node["repo_id"])
    if repo_record is None:
        raise DatabaseError(
            f"Repository not found: {node['repo_id']}",
            error_code="repo_not_found",
            context={"repo_id": node["repo_id"]},
        )

    start_line, end_line = _resolve_symbol_line_bounds(node)
    snippet = read_file_snippet(
        repo_record.root_path,
        file_record["file_path"],
        start_line,
        end_line,
        max_lines=max_lines,
        offset=offset,
        expected_content_hash=file_record["content_hash"],
    )

    result = {
        "symbol_id": node["id"],
        "qualified_name": node["qualified_name"],
        "kind": node["kind"],
        "module_path": file_record["module_path"],
        "lines": format_line_span(node) or f"{snippet['start_line']}-{snippet['end_line']}",
        "signature": node.get("signature"),
        **snippet,
    }
    return result
