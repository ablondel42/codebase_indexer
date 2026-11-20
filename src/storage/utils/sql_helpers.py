"""Stateless SQL, FTS5, upsert, error-boundary, cascade, and SQLite authorizer helpers."""

import re
import sqlite3
from collections.abc import Collection, Generator, Iterator, Mapping, Sequence
from contextlib import contextmanager
from typing import Any, TypeVar

from sqlalchemy import ColumnElement, Table, delete, func, or_, select
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.engine import Connection
from sqlalchemy.exc import SQLAlchemyError

from core.models.enums import EdgeKind
from core.models.exceptions import DatabaseError, IndexerError
from core.utils.logging import get_logger
from core.utils.pathing import normalize_rel_path_prefix
from storage.utils.schema import edges_table, files_table, nodes_table

logger = get_logger("storage.utils.sql_helpers")

T = TypeVar("T")

_SPECIAL_FTS_CHARS_RE = re.compile(r"[^A-Za-z0-9_.\s]")
_DEFAULT_CHUNK_SIZE = 400
_BIDIRECTIONAL_CHUNK_SIZE = 200

_ALLOWED_READ_ONLY_SQL_ACTIONS: frozenset[int] = frozenset({
    sqlite3.SQLITE_SELECT,
    sqlite3.SQLITE_READ,
    sqlite3.SQLITE_FUNCTION,
    sqlite3.SQLITE_PRAGMA,
})


def chunked_sequence(items: Sequence[T], size: int = _DEFAULT_CHUNK_SIZE) -> Iterator[Sequence[T]]:
    """Yield contiguous slices of ``items`` bounded below SQLite bind-parameter limits."""
    if size < 1:
        raise ValueError(f"Chunk size must be >= 1, got {size}")
    for start in range(0, len(items), size):
        yield items[start : start + size]


def escape_like(value: str) -> str:
    """Escape SQLite ``LIKE`` metacharacters (``\\``, ``%``, ``_``) for ``ESCAPE '\\'``."""
    return value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def build_rel_path_prefix_clause(
    column: ColumnElement[Any],
    path_prefix: str | None,
    *,
    directory_boundary: bool = True,
) -> ColumnElement[bool] | None:
    """Build an escaped SQLite ``file_path`` prefix filter clause (or ``None`` if empty)."""
    cleaned = normalize_rel_path_prefix(path_prefix)
    if not cleaned:
        return None
    escaped = escape_like(cleaned)
    if not directory_boundary:
        return column.like(f"{escaped}%", escape="\\")
    return or_(column == cleaned, column.like(f"{escaped}/%", escape="\\"))


def build_sqlite_upsert(
    table: Table,
    *,
    conflict_col: str = "id",
    exclude_cols: Collection[str] = ("id",),
) -> Any:
    """Build ``INSERT ... ON CONFLICT(conflict_col) DO UPDATE`` for ``table``."""
    base = sqlite_insert(table)
    return base.on_conflict_do_update(
        index_elements=[table.c[conflict_col]],
        set_={
            col.name: base.excluded[col.name]
            for col in table.columns
            if col.name not in exclude_cols
        },
    )


def db_error_message(exc: BaseException) -> str:
    """Extract the underlying DBAPI message from a SQLAlchemy or SQLite exception."""
    orig = getattr(exc, "orig", None)
    if orig is not None:
        return str(orig)
    return str(exc)


@contextmanager
def db_operation(
    message: str,
    *,
    context: Mapping[str, Any] | None = None,
) -> Generator[None, None, None]:
    """Execute a SQLite block and wrap SQLAlchemy/sqlite3 failures in ``DatabaseError``."""
    try:
        yield
    except IndexerError:
        raise
    except (SQLAlchemyError, sqlite3.Error) as exc:
        detail = db_error_message(exc)
        logger.error("%s: %s", message, detail)
        raise DatabaseError(
            f"{message}: {detail}",
            context=dict(context) if context is not None else None,
        ) from exc


def delete_lsp_references_for_symbol_ids(conn: Connection, symbol_ids: Sequence[str]) -> int:
    """Delete LSP ``references`` edges touching ``symbol_ids`` in bounded chunks."""
    unique_ids = list(dict.fromkeys(sid for sid in symbol_ids if sid))
    if not unique_ids:
        return 0
    deleted = 0
    for chunk in chunked_sequence(unique_ids, _BIDIRECTIONAL_CHUNK_SIZE):
        res = conn.execute(
            delete(edges_table).where(
                edges_table.c.kind == str(EdgeKind.REFERENCES),
                or_(edges_table.c.from_id.in_(chunk), edges_table.c.to_id.in_(chunk)),
            )
        )
        deleted += max(res.rowcount or 0, 0)
    return deleted


def delete_file_graph_batches(
    conn: Connection,
    file_ids: Sequence[str],
    *,
    repo_id: str | None = None,
    preserve_incoming_ast_edges: bool = True,
) -> int:
    """Delete ``files``, ``nodes``, owned edges, and incoming LSP refs in bounded chunks."""
    if not file_ids:
        return 0
    deleted = 0
    for batch in chunked_sequence(file_ids, _DEFAULT_CHUNK_SIZE):
        node_subq = select(nodes_table.c.id).where(nodes_table.c.file_id.in_(batch))
        conn.execute(
            delete(edges_table).where(
                or_(
                    edges_table.c.evidence_file_id.in_(batch),
                    edges_table.c.from_id.in_(node_subq),
                )
            )
        )
        incoming_cond = (
            (
                edges_table.c.kind == str(EdgeKind.REFERENCES),
                edges_table.c.to_id.in_(node_subq),
            )
            if preserve_incoming_ast_edges
            else (edges_table.c.to_id.in_(node_subq),)
        )
        conn.execute(delete(edges_table).where(*incoming_cond))
        conn.execute(delete(nodes_table).where(nodes_table.c.file_id.in_(batch)))

        del_stmt = delete(files_table).where(files_table.c.id.in_(batch))
        if repo_id is not None:
            del_stmt = del_stmt.where(files_table.c.repo_id == repo_id)
        res = conn.execute(del_stmt)
        deleted += max(res.rowcount or 0, 0)
    return deleted


def build_fts_match_expression(raw_query: str) -> str | None:
    """Build a safely quoted FTS5 prefix ``MATCH`` expression or ``None`` for ``LIKE`` fallback."""
    if _SPECIAL_FTS_CHARS_RE.search(raw_query):
        return None
    tokens = [tok.strip(".") for tok in raw_query.split()]
    valid_tokens = [tok for tok in tokens if tok and any(ch.isalnum() or ch == "_" for ch in tok)]
    if not valid_tokens:
        return None
    quoted_terms = [f'"{tok.replace(chr(34), chr(34) * 2)}"*' for tok in valid_tokens]
    return " ".join(quoted_terms)


def evidence_line_expr() -> ColumnElement[Any]:
    """Return ``COALESCE(json_extract(edges.evidence_range_json, '$.start.line'), 0)``."""
    return func.coalesce(
        func.json_extract(edges_table.c.evidence_range_json, "$.start.line"),
        0,
    )


def evidence_char_expr() -> ColumnElement[Any]:
    """Return ``COALESCE(json_extract(edges.evidence_range_json, '$.start.character'), 0)``."""
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
    """SQLite authorizer callback permitting strictly read-only ``SELECT``/``PRAGMA`` actions."""
    _ = (arg1, arg2, db_name, trigger_name)
    if action in _ALLOWED_READ_ONLY_SQL_ACTIONS:
        return sqlite3.SQLITE_OK
    return sqlite3.SQLITE_DENY
