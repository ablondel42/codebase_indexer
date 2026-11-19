"""SQLite schema v2 initializer and v1-to-v2 migrator."""

import sqlite3

from sqlalchemy.engine import Connection
from sqlalchemy.exc import OperationalError, SQLAlchemyError

from core.models.exceptions import DatabaseError
from core.utils.logging import get_logger
from storage.utils.schema import SCHEMA_VERSION, metadata
from storage.utils.sql_helpers import db_error_message

logger = get_logger("storage.services.migrator")

_DROP_LEGACY_FTS_DDL: tuple[str, ...] = (
    "DROP TRIGGER IF EXISTS nodes_fts_ai",
    "DROP TRIGGER IF EXISTS nodes_fts_au",
    "DROP TRIGGER IF EXISTS nodes_fts_ad",
    "DROP TABLE IF EXISTS nodes_fts",
)

_CREATE_FTS_DDL: tuple[str, ...] = (
    """
    CREATE VIRTUAL TABLE IF NOT EXISTS nodes_fts USING fts5(
        node_id UNINDEXED,
        repo_id UNINDEXED,
        name,
        qualified_name,
        signature,
        doc_summary,
        tokenize="unicode61 tokenchars '_'"
    )
    """,
    """
    CREATE TRIGGER IF NOT EXISTS nodes_fts_ai AFTER INSERT ON nodes BEGIN
        INSERT INTO nodes_fts(rowid, node_id, repo_id, name, qualified_name, signature, doc_summary)
        VALUES (
            new.rowid,
            new.id,
            new.repo_id,
            new.name,
            new.qualified_name,
            COALESCE(new.signature, ''),
            COALESCE(new.doc_summary, '')
        );
    END
    """,
    """
    CREATE TRIGGER IF NOT EXISTS nodes_fts_au AFTER UPDATE ON nodes BEGIN
        DELETE FROM nodes_fts WHERE rowid = old.rowid;
        INSERT INTO nodes_fts(rowid, node_id, repo_id, name, qualified_name, signature, doc_summary)
        VALUES (
            new.rowid,
            new.id,
            new.repo_id,
            new.name,
            new.qualified_name,
            COALESCE(new.signature, ''),
            COALESCE(new.doc_summary, '')
        );
    END
    """,
    """
    CREATE TRIGGER IF NOT EXISTS nodes_fts_ad AFTER DELETE ON nodes BEGIN
        DELETE FROM nodes_fts WHERE rowid = old.rowid;
    END
    """,
)


class SchemaMigrator:
    """Initializes and upgrades SQLite databases to schema version v2."""

    def read_user_version(self, conn: Connection) -> int:
        """Read ``PRAGMA user_version`` from an open SQLite connection.

        Args:
            conn: Active SQLAlchemy ``Connection``.

        Returns:
            Integer ``user_version`` reported by SQLite (``0`` for uninitialized DBs).

        Raises:
            DatabaseError: If querying ``PRAGMA user_version`` fails.
        """
        try:
            raw = conn.exec_driver_sql("PRAGMA user_version").scalar_one_or_none()
            version = int(raw) if raw is not None else 0
            logger.debug("Read SQLite PRAGMA user_version=%d", version)
            return version
        except (SQLAlchemyError, sqlite3.Error) as exc:
            msg = db_error_message(exc)
            logger.error("Failed reading PRAGMA user_version: %s", msg)
            raise DatabaseError(f"Failed to read SQLite schema version: {msg}") from exc

    @staticmethod
    def _read_query_only(conn: Connection) -> bool:
        """Return ``True`` when ``PRAGMA query_only`` is enabled on ``conn``.

        Args:
            conn: Active SQLAlchemy ``Connection``.

        Returns:
            ``True`` if ``PRAGMA query_only`` evaluates to non-zero.

        Raises:
            DatabaseError: If querying ``PRAGMA query_only`` fails.
        """
        try:
            raw = conn.exec_driver_sql("PRAGMA query_only").scalar_one_or_none()
            return bool(int(raw)) if raw is not None else False
        except (SQLAlchemyError, sqlite3.Error) as exc:
            msg = db_error_message(exc)
            logger.error("Failed reading PRAGMA query_only: %s", msg)
            raise DatabaseError(f"Failed to read SQLite query_only pragma: {msg}") from exc

    @staticmethod
    def _drop_legacy_v1_tables(conn: Connection) -> None:
        """Drop legacy v1 FTS triggers, FTS virtual table, and core tables.

        Args:
            conn: Writable SQLAlchemy ``Connection``.
        """
        for ddl in _DROP_LEGACY_FTS_DDL:
            conn.exec_driver_sql(ddl)
        metadata.drop_all(conn)

    @staticmethod
    def _create_v2_schema(conn: Connection) -> None:
        """Create schema v2 core tables, FTS5 virtual table, and sync triggers.

        Args:
            conn: Writable SQLAlchemy ``Connection``.
        """
        metadata.create_all(conn)
        for ddl in _CREATE_FTS_DDL:
            conn.exec_driver_sql(ddl)
        conn.exec_driver_sql(f"PRAGMA user_version = {SCHEMA_VERSION}")

    def initialize(self, conn: Connection) -> None:
        """Initialize or upgrade ``conn`` to SQLite schema v2.

        No-ops when ``PRAGMA user_version == 2``. Upgrades ``0 <= user_version < 2``
        by dropping legacy v1 objects and creating v2 tables/FTS5 triggers. Rejects
        mismatched read-only connections and unsupported future schema versions.

        Args:
            conn: Active SQLAlchemy ``Connection``.

        Raises:
            DatabaseError: If ``conn`` is read-only with a mismatched schema version,
                uses an unsupported future ``user_version``, or DDL execution fails.
        """
        current_version = self.read_user_version(conn)
        if current_version == SCHEMA_VERSION:
            return

        if self._read_query_only(conn):
            logger.warning(
                "Schema mismatch on read-only connection (current=%d, expected=%d)",
                current_version,
                SCHEMA_VERSION,
            )
            raise DatabaseError(
                "Database schema mismatch; run `rc index` to upgrade.",
                context={"current_version": current_version, "expected_version": SCHEMA_VERSION},
            )

        if current_version > SCHEMA_VERSION:
            logger.error(
                "Unsupported future schema version %d (expected %d)",
                current_version,
                SCHEMA_VERSION,
            )
            raise DatabaseError(
                f"Unsupported database schema version {current_version} "
                f"(expected {SCHEMA_VERSION}).",
                context={"current_version": current_version, "expected_version": SCHEMA_VERSION},
            )

        try:
            self._drop_legacy_v1_tables(conn)
            self._create_v2_schema(conn)
            conn.commit()
            logger.info(
                "Initialized SQLite schema v%d (previous user_version=%d)",
                SCHEMA_VERSION,
                current_version,
            )
        except OperationalError as exc:
            if "readonly" in str(exc).lower():
                logger.warning("Read-only operational error during schema migration: %s", exc)
                raise DatabaseError(
                    "Database schema mismatch; run `rc index` to upgrade.",
                    context={
                        "current_version": current_version,
                        "expected_version": SCHEMA_VERSION,
                    },
                ) from exc
            msg = db_error_message(exc)
            logger.error("Failed initializing schema v%d: %s", SCHEMA_VERSION, msg)
            raise DatabaseError(
                f"Failed to initialize database schema v{SCHEMA_VERSION}: {msg}",
                context={"current_version": current_version},
            ) from exc
        except (SQLAlchemyError, sqlite3.Error) as exc:
            msg = db_error_message(exc)
            logger.error("Failed initializing schema v%d: %s", SCHEMA_VERSION, msg)
            raise DatabaseError(
                f"Failed to initialize database schema v{SCHEMA_VERSION}: {msg}",
                context={"current_version": current_version},
            ) from exc
