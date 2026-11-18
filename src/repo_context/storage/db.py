"""SQLAlchemy SQLite connection and read-only SQL authorizer helpers."""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from sqlalchemy import create_engine, event
from sqlalchemy.engine import Connection, Engine
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.pool import NullPool

from repo_context.exceptions import DatabaseError, DatabaseNotFoundError

_ALLOWED_READ_ONLY_SQL_ACTIONS = frozenset(
    {
        sqlite3.SQLITE_SELECT,
        sqlite3.SQLITE_READ,
        sqlite3.SQLITE_FUNCTION,
        sqlite3.SQLITE_PRAGMA,
    }
)


def _read_only_authorizer(
    action: int,
    arg1: str | None,
    arg2: str | None,
    db_name: str | None,
    trigger_name: str | None,
) -> int:
    """SQLite authorizer allowing strictly read-only SELECT/PRAGMA operations."""
    if action in _ALLOWED_READ_ONLY_SQL_ACTIONS:
        return sqlite3.SQLITE_OK
    return sqlite3.SQLITE_DENY


def _build_engine(
    db_path: Path | str,
    *,
    read_only: bool = False,
    must_exist: bool = False,
    with_authorizer: bool = False,
) -> Engine:
    """Create a short-lived ``NullPool`` SQLAlchemy Engine configured for SQLite."""
    path = Path(db_path)
    is_memory = str(db_path) == ":memory:"

    if (read_only or must_exist) and not is_memory and not path.is_file():
        raise DatabaseNotFoundError(
            f"Database file not found: {path}",
            context={"db_path": str(path)},
        )

    if not read_only and not is_memory:
        path.parent.mkdir(parents=True, exist_ok=True)

    def _creator() -> sqlite3.Connection:
        if read_only and not is_memory:
            uri = f"file:{path.resolve().as_posix()}?mode=ro"
            return sqlite3.connect(uri, uri=True)
        return sqlite3.connect(":memory:" if is_memory else path)

    engine = create_engine("sqlite://", creator=_creator, poolclass=NullPool)

    @event.listens_for(engine, "connect")
    def _configure_sqlite(dbapi_conn: sqlite3.Connection, _record: Any) -> None:
        cursor = dbapi_conn.cursor()
        try:
            if read_only:
                cursor.execute("PRAGMA query_only = ON;")
                cursor.execute("PRAGMA busy_timeout = 5000;")
            else:
                cursor.execute("PRAGMA journal_mode = WAL;")
                cursor.execute("PRAGMA synchronous = NORMAL;")
                cursor.execute("PRAGMA foreign_keys = ON;")
                cursor.execute("PRAGMA busy_timeout = 5000;")
        finally:
            cursor.close()
        if with_authorizer:
            dbapi_conn.set_authorizer(_read_only_authorizer)

    return engine


def get_connection(
    db_path: Path | str,
    *,
    read_only: bool = False,
    must_exist: bool = False,
) -> Connection:
    """Open a configured SQLAlchemy SQLite ``Connection``.

    Raises:
        DatabaseNotFoundError: If ``read_only`` or ``must_exist`` and ``db_path`` does not exist.
        DatabaseError: If opening or configuring SQLite fails.
    """
    path = Path(db_path)
    engine = _build_engine(db_path, read_only=read_only, must_exist=must_exist)
    try:
        return engine.connect()
    except (SQLAlchemyError, sqlite3.Error, OSError) as exc:
        engine.dispose()
        raise DatabaseError(
            f"Failed to open SQLite database at {path}: {exc}",
            context={"db_path": str(path), "read_only": read_only},
        ) from exc


def open_read_only_sql_connection(db_path: Path | str) -> Connection:
    """Open a read-only SQLAlchemy connection guarded by SQLite's SELECT-only authorizer."""
    path = Path(db_path)
    engine = _build_engine(db_path, read_only=True, must_exist=True, with_authorizer=True)
    try:
        return engine.connect()
    except (SQLAlchemyError, sqlite3.Error, OSError) as exc:
        engine.dispose()
        raise DatabaseError(
            f"Failed to open read-only SQLite database at {path}: {exc}",
            context={"db_path": str(path), "read_only": True},
        ) from exc


def close_connection(conn: Connection | None) -> None:
    """Close a SQLAlchemy connection and dispose its underlying ``NullPool`` engine."""
    if conn is not None:
        engine = conn.engine
        conn.close()
        engine.dispose()


@contextmanager
def connection_scope(
    db_path: Path | str,
    *,
    read_only: bool = False,
    must_exist: bool = False,
) -> Iterator[Connection]:
    """Context manager that opens, commits/rolls back, and closes a SQLAlchemy connection."""
    conn = get_connection(db_path, read_only=read_only, must_exist=must_exist)
    try:
        yield conn
        if not read_only and conn.in_transaction():
            conn.commit()
    except Exception:
        if not read_only and conn.in_transaction():
            conn.rollback()
        raise
    finally:
        close_connection(conn)
