"""SQLite ``NullPool`` connection factory applying WAL, FK, and authorizer guards."""

import sqlite3
from pathlib import Path
from typing import Any

from sqlalchemy import create_engine, event
from sqlalchemy.engine import Connection, Engine
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.pool import NullPool

from core.models.exceptions import DatabaseError, DatabaseNotFoundError
from core.utils.logging import get_logger
from storage.models.options import ConnectionOptions
from storage.utils.sql_helpers import db_error_message, sqlite_select_only_authorizer

logger = get_logger("storage.services.connection")


class ConnectionFactory:
    """Creates and tears down configured ``NullPool`` SQLAlchemy SQLite connections."""

    @staticmethod
    def _apply_sqlite_pragmas(dbapi_conn: sqlite3.Connection, *, read_only: bool) -> None:
        """Apply session-level SQLite PRAGMAs on a newly opened DBAPI connection.

        Args:
            dbapi_conn: Underlying ``sqlite3.Connection`` instance.
            read_only: Whether the connection operates in read-only mode.
        """
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

    def _attach_connect_listener(self, engine: Engine, options: ConnectionOptions) -> None:
        """Register PRAGMA and optional ``SELECT``-only authorizer hooks on ``engine``.

        Args:
            engine: Target SQLAlchemy ``Engine`` instance.
            options: Connection options controlling read-only and authorizer flags.
        """

        def _on_connect(dbapi_conn: sqlite3.Connection, _record: Any) -> None:
            self._apply_sqlite_pragmas(dbapi_conn, read_only=options.read_only)
            if options.with_select_authorizer:
                dbapi_conn.set_authorizer(sqlite_select_only_authorizer)

        event.listen(engine, "connect", _on_connect)

    def _build_engine(self, options: ConnectionOptions) -> Engine:
        """Validate target path existence/parent directory and build a ``NullPool`` engine.

        Args:
            options: Immutable connection configuration.

        Returns:
            Configured ``NullPool`` SQLAlchemy ``Engine``.

        Raises:
            DatabaseNotFoundError: If ``read_only`` or ``must_exist`` and file is absent.
            DatabaseError: If creating parent directories fails.
        """
        path = options.db_path
        uses_memory_or_uri = options.is_memory or options.sqlite_uri is not None

        if (
            (options.read_only or options.must_exist)
            and not uses_memory_or_uri
            and not path.is_file()
        ):
            logger.debug("SQLite database file not found at %s", path)
            raise DatabaseNotFoundError(
                f"Database file not found: {path}",
                context={"db_path": str(path)},
            )

        if not options.read_only and not uses_memory_or_uri:
            try:
                path.parent.mkdir(parents=True, exist_ok=True)
            except OSError as exc:
                logger.error("Failed creating parent directory for %s: %s", path, exc)
                raise DatabaseError(
                    f"Failed to prepare database directory for {path}: {exc}",
                    context={"db_path": str(path)},
                ) from exc

        def _creator() -> sqlite3.Connection:
            if options.sqlite_uri is not None:
                return sqlite3.connect(
                    options.sqlite_uri,
                    uri=True,
                    check_same_thread=False,
                )
            if options.read_only and not options.is_memory:
                uri = f"file:{path.resolve().as_posix()}?mode=ro"
                return sqlite3.connect(uri, uri=True)
            return sqlite3.connect(":memory:" if options.is_memory else path)

        engine = create_engine("sqlite://", creator=_creator, poolclass=NullPool)
        self._attach_connect_listener(engine, options)
        return engine

    def open(self, options: ConnectionOptions) -> Connection:
        """Open a configured SQLAlchemy SQLite ``Connection`` for ``options``.

        Args:
            options: Immutable ``ConnectionOptions`` configuration.

        Returns:
            Open SQLAlchemy ``Connection``.

        Raises:
            DatabaseNotFoundError: If ``read_only`` or ``must_exist`` and file is missing.
            DatabaseError: If opening or configuring SQLite fails.
        """
        engine = self._build_engine(options)
        try:
            conn = engine.connect()
            logger.debug(
                "Opened SQLite connection (db_path=%s, read_only=%s, authorizer=%s)",
                options.db_path,
                options.read_only,
                options.with_select_authorizer,
            )
            return conn
        except (SQLAlchemyError, sqlite3.Error, OSError) as exc:
            engine.dispose()
            msg = db_error_message(exc)
            logger.error("Failed opening SQLite database at %s: %s", options.db_path, msg)
            raise DatabaseError(
                f"Failed to open SQLite database at {options.db_path}: {msg}",
                context={"db_path": str(options.db_path), "read_only": options.read_only},
            ) from exc

    def open_path(
        self,
        db_path: Path | str,
        *,
        read_only: bool = False,
        must_exist: bool = False,
        with_select_authorizer: bool = False,
    ) -> Connection:
        """Open a SQLAlchemy SQLite ``Connection`` directly from a path and flags.

        Args:
            db_path: Path to the SQLite database file (or ``':memory:'``).
            read_only: Whether to open in read-only mode.
            must_exist: Whether to require ``db_path`` to already exist on disk.
            with_select_authorizer: Whether to attach the ``SELECT``-only authorizer.

        Returns:
            Open SQLAlchemy ``Connection``.

        Raises:
            DatabaseNotFoundError: If ``read_only`` or ``must_exist`` and file is missing.
            DatabaseError: If opening or configuring SQLite fails.
        """
        options = ConnectionOptions(
            db_path=Path(db_path),
            read_only=read_only,
            must_exist=must_exist,
            with_select_authorizer=with_select_authorizer,
        )
        return self.open(options)

    def open_readonly_sql(self, db_path: Path | str) -> Connection:
        """Open a read-only SQLite connection guarded by the ``SELECT``-only authorizer.

        Args:
            db_path: Path to the existing SQLite database file.

        Returns:
            Authorizer-guarded read-only SQLAlchemy ``Connection``.

        Raises:
            DatabaseNotFoundError: If ``db_path`` does not exist on disk.
            DatabaseError: If opening or configuring SQLite fails.
        """
        return self.open_path(
            db_path,
            read_only=True,
            must_exist=True,
            with_select_authorizer=True,
        )

    def close(self, conn: Connection | None) -> None:
        """Close ``conn`` and dispose its backing ``NullPool`` SQLAlchemy engine.

        Args:
            conn: Open SQLAlchemy ``Connection`` or ``None``.

        Raises:
            DatabaseError: If closing the connection or disposing the engine fails.
        """
        if conn is None:
            return
        engine = conn.engine
        try:
            conn.close()
            engine.dispose()
            logger.debug("Closed SQLite connection")
        except (SQLAlchemyError, sqlite3.Error, OSError) as exc:
            msg = db_error_message(exc)
            logger.error("Failed closing SQLite connection: %s", msg)
            raise DatabaseError(f"Failed to close SQLite connection: {msg}") from exc
