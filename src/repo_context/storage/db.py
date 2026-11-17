"""SQLite connection and read-only SQL authorizer helpers."""

from contextlib import contextmanager
from pathlib import Path
import sqlite3
from typing import Iterator

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


def get_connection(
    db_path: Path | str,
    *,
    read_only: bool = False,
    must_exist: bool = False,
) -> sqlite3.Connection:
    """Open a configured SQLite database connection.

    Args:
        db_path: Path to the SQLite database file.
        read_only: If True, open in SQLite read-only mode with ``query_only = ON``.
        must_exist: If True (or when ``read_only=True``), verify the file exists first.

    Returns:
        Configured ``sqlite3.Connection`` with ``row_factory = sqlite3.Row``.

    Raises:
        DatabaseNotFoundError: If ``read_only`` or ``must_exist`` and ``db_path`` does not exist.
        DatabaseError: If opening or configuring SQLite fails.
    """
    path = Path(db_path)
    is_memory = str(db_path) == ":memory:"

    if (read_only or must_exist) and not path.is_file():
        raise DatabaseNotFoundError(
            f"Database file not found: {path}",
            context={"db_path": str(path)},
        )

    try:
        if read_only:
            uri = f"file:{path.resolve().as_posix()}?mode=ro"
            conn = sqlite3.connect(uri, uri=True)
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA query_only = ON;")
            conn.execute("PRAGMA busy_timeout = 5000;")
            return conn

        if not is_memory:
            path.parent.mkdir(parents=True, exist_ok=True)

        conn = sqlite3.connect(path)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode = WAL;")
        conn.execute("PRAGMA synchronous = NORMAL;")
        conn.execute("PRAGMA foreign_keys = ON;")
        conn.execute("PRAGMA busy_timeout = 5000;")
        return conn
    except sqlite3.Error as exc:
        raise DatabaseError(
            f"Failed to open SQLite database at {path}: {exc}",
            context={"db_path": str(path), "read_only": read_only},
        ) from exc


def open_read_only_sql_connection(db_path: Path | str) -> sqlite3.Connection:
    """Open a read-only SQLite connection guarded by a strict SQL authorizer."""
    conn = get_connection(db_path, read_only=True)
    conn.set_authorizer(_read_only_authorizer)
    return conn


def close_connection(conn: sqlite3.Connection | None) -> None:
    """Close a database connection safely."""
    if conn is not None:
        conn.close()


@contextmanager
def connection_scope(
    db_path: Path | str,
    *,
    read_only: bool = False,
    must_exist: bool = False,
) -> Iterator[sqlite3.Connection]:
    """Context manager that opens, commits/rolls back, and closes a SQLite connection."""
    conn = get_connection(db_path, read_only=read_only, must_exist=must_exist)
    try:
        yield conn
        if not read_only and conn.in_transaction:
            conn.commit()
    except Exception:
        if not read_only and conn.in_transaction:
            conn.rollback()
        raise
    finally:
        close_connection(conn)
