"""Stateless SQL, FTS5, URI candidate, chunking, and SQLite authorizer helpers."""

import re
import sqlite3
from collections.abc import Iterator, Sequence
from pathlib import Path
from typing import Any, TypeVar
from urllib.parse import unquote, urlparse

from sqlalchemy import ColumnElement, func

from core.utils.logging import get_logger
from storage.utils.schema import edges_table

logger = get_logger("storage.utils.sql_helpers")

T = TypeVar("T")

_SPECIAL_FTS_CHARS_RE = re.compile(r"[^A-Za-z0-9_.\s]")

_ALLOWED_READ_ONLY_SQL_ACTIONS: frozenset[int] = frozenset({
    sqlite3.SQLITE_SELECT,
    sqlite3.SQLITE_READ,
    sqlite3.SQLITE_FUNCTION,
    sqlite3.SQLITE_PRAGMA,
})


def chunked_sequence(items: Sequence[T], size: int = 400) -> Iterator[Sequence[T]]:
    """Yield contiguous slices of ``items`` bounded below SQLite bind-parameter limits.

    Args:
        items: Sequence of elements to partition.
        size: Maximum slice length (default ``400``, must be ``>= 1``).

    Yields:
        Contiguous sub-sequences of length at most ``size``.

    Raises:
        ValueError: If ``size < 1``.
    """
    if size < 1:
        raise ValueError(f"Chunk size must be >= 1, got {size}")
    for start in range(0, len(items), size):
        yield items[start : start + size]


def escape_like(value: str) -> str:
    """Escape SQLite ``LIKE`` metacharacters (``\\``, ``%``, ``_``) for ``ESCAPE '\\'``.

    Args:
        value: Raw literal search string.

    Returns:
        Escaped string safe for ``column.like(..., escape='\\')``.
    """
    return value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def normalize_path_filter(path_filter: str) -> str:
    """Normalize a relative path/directory filter without stripping dot-directories.

    Args:
        path_filter: User-supplied file path or directory prefix (e.g., ``"./.github/"``).

    Returns:
        Normalized POSIX filter with leading ``"./"`` and trailing ``"/"`` stripped.
    """
    cleaned = path_filter.strip()
    while cleaned.startswith("./"):
        cleaned = cleaned.removeprefix("./")
    return cleaned.rstrip("/")


def build_fts_match_expression(raw_query: str) -> str | None:
    """Build a safely quoted FTS5 prefix ``MATCH`` expression or ``None`` for ``LIKE`` fallback.

    Args:
        raw_query: Trimmed user search query.

    Returns:
        Space-joined quoted FTS5 prefix terms (e.g., ``'"foo"* "bar"*'``), or ``None``
        when ``raw_query`` contains punctuation/operators requiring escaped ``LIKE``.
    """
    if _SPECIAL_FTS_CHARS_RE.search(raw_query):
        return None
    tokens = [tok.strip(".") for tok in raw_query.split()]
    valid_tokens = [tok for tok in tokens if tok and any(ch.isalnum() or ch == "_" for ch in tok)]
    if not valid_tokens:
        return None
    quoted_terms = [f'"{tok.replace(chr(34), chr(34) * 2)}"*' for tok in valid_tokens]
    return " ".join(quoted_terms)


def evidence_line_expr() -> ColumnElement[Any]:
    """Return a SQLAlchemy expression extracting ``evidence_range_json.start.line`` (or ``0``).

    Returns:
        ``COALESCE(json_extract(edges.evidence_range_json, '$.start.line'), 0)`` expression.
    """
    return func.coalesce(
        func.json_extract(edges_table.c.evidence_range_json, "$.start.line"),
        0,
    )


def evidence_char_expr() -> ColumnElement[Any]:
    """Return a SQLAlchemy expression extracting ``evidence_range_json.start.character`` (or ``0``).

    Returns:
        ``COALESCE(json_extract(edges.evidence_range_json, '$.start.character'), 0)`` expression.
    """
    return func.coalesce(
        func.json_extract(edges_table.c.evidence_range_json, "$.start.character"),
        0,
    )


def sqlite_select_only_authorizer(
    action: int,
    arg1: str | None,
    arg2: str | None,
    db_name: str | None,
    trigger_name: str | None,
) -> int:
    """SQLite authorizer callback permitting strictly read-only ``SELECT``/``PRAGMA`` actions.

    Args:
        action: SQLite action code (e.g., ``sqlite3.SQLITE_SELECT``).
        arg1: Action-specific first argument supplied by SQLite.
        arg2: Action-specific second argument supplied by SQLite.
        db_name: Database name supplied by SQLite.
        trigger_name: Trigger or view name supplied by SQLite.

    Returns:
        ``sqlite3.SQLITE_OK`` for read-only actions, ``sqlite3.SQLITE_DENY`` otherwise.
    """
    _ = (arg1, arg2, db_name, trigger_name)
    if action in _ALLOWED_READ_ONLY_SQL_ACTIONS:
        return sqlite3.SQLITE_OK
    return sqlite3.SQLITE_DENY


def _add_unique_uri(candidates: list[str], value: str) -> None:
    """Append ``value`` to ``candidates`` when non-empty and not yet present.

    Args:
        candidates: Mutable list of candidate URI strings.
        value: Candidate URI string to append.
    """
    if value and value not in candidates:
        candidates.append(value)


def _add_path_uris(candidates: list[str], path: Path) -> None:
    """Append RFC 8089 ``as_uri()`` and unquoted URI variants for an absolute ``Path``.

    Args:
        candidates: Mutable list of candidate URI strings.
        path: Filesystem path to convert when absolute.
    """
    if not path.is_absolute():
        return
    try:
        path_uri = path.as_uri()
    except ValueError as exc:
        logger.debug("Cannot convert path %s to file URI: %s", path, exc)
        return
    _add_unique_uri(candidates, path_uri)
    _add_unique_uri(candidates, unquote(path_uri))


def _normalize_parsed_uri_path(raw_path: str) -> Path:
    """Decode a percent-escaped URI path and normalize Windows drive prefixes.

    Args:
        raw_path: Raw ``urlparse(uri).path`` string.

    Returns:
        Decoded ``Path`` instance.
    """
    decoded = unquote(raw_path)
    if len(decoded) >= 3 and decoded[0] == "/" and decoded[2] == ":" and decoded[1].isalpha():
        decoded = decoded[1:]
    return Path(decoded)


def extract_uri_candidates(uri: str) -> tuple[list[str], Path | None]:
    """Extract normalized ``file://`` URI candidates and resolved filesystem path.

    Args:
        uri: Raw ``file://`` URI or path string.

    Returns:
        Tuple of ``(candidate_uris, resolved_path_or_none)``.
    """
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
    except OSError as exc:
        logger.debug("Cannot resolve URI filesystem path %s: %s", raw_path, exc)
        resolved_path = raw_path
    _add_path_uris(candidates, resolved_path)

    return candidates, resolved_path


def db_error_message(exc: BaseException) -> str:
    """Extract the underlying DBAPI message from a SQLAlchemy or SQLite exception.

    Args:
        exc: Raised ``SQLAlchemyError``, ``sqlite3.Error``, or ``OSError``.

    Returns:
        Concise error description string.
    """
    orig = getattr(exc, "orig", None)
    return str(orig) if orig is not None else str(exc)
