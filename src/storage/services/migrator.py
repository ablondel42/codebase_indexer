"""SQLite schema v4 initializer and v1/v2/v3-to-v4 migrator."""

import sqlite3

from sqlalchemy.engine import Connection
from sqlalchemy.exc import OperationalError, SQLAlchemyError

from core.models.exceptions import DatabaseError
from core.utils.logging import get_logger
from storage.utils.schema import SCHEMA_VERSION, metadata
from storage.utils.sql_helpers import db_error_message

logger = get_logger("storage.services.migrator")

_LEGACY_VARIABLE_KINDS_SQL = "('constant', 'attribute', 'type_alias')"
_LEGACY_STRUCTURAL_EDGES_SQL = "('contains', 'scope_parent')"

_DROP_LEGACY_FTS_DDL: tuple[str, ...] = (
    "DROP TRIGGER IF EXISTS nodes_fts_ai",
    "DROP TRIGGER IF EXISTS nodes_fts_au",
    "DROP TRIGGER IF EXISTS nodes_fts_ad",
    "DROP TABLE IF EXISTS nodes_fts",
    "DROP TRIGGER IF EXISTS text_files_ai",
    "DROP TRIGGER IF EXISTS text_files_au",
    "DROP TRIGGER IF EXISTS text_files_ad",
    "DROP TABLE IF EXISTS text_fts",
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
    """
    CREATE VIRTUAL TABLE IF NOT EXISTS text_fts USING fts5(
        repo_id UNINDEXED,
        file_path UNINDEXED,
        content,
        content='text_files',
        content_rowid='rowid',
        tokenize='trigram'
    )
    """,
    """
    CREATE TRIGGER IF NOT EXISTS text_files_ai AFTER INSERT ON text_files BEGIN
        INSERT INTO text_fts(rowid, repo_id, file_path, content)
        VALUES (new.rowid, new.repo_id, new.file_path, new.content);
    END
    """,
    """
    CREATE TRIGGER IF NOT EXISTS text_files_ad AFTER DELETE ON text_files BEGIN
        INSERT INTO text_fts(text_fts, rowid, repo_id, file_path, content)
        VALUES ('delete', old.rowid, old.repo_id, old.file_path, old.content);
    END
    """,
    """
    CREATE TRIGGER IF NOT EXISTS text_files_au AFTER UPDATE ON text_files BEGIN
        INSERT INTO text_fts(text_fts, rowid, repo_id, file_path, content)
        VALUES ('delete', old.rowid, old.repo_id, old.file_path, old.content);
        INSERT INTO text_fts(rowid, repo_id, file_path, content)
        VALUES (new.rowid, new.repo_id, new.file_path, new.content);
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
        """Create schema v4 core tables, FTS5 virtual tables, and sync triggers.

        Args:
            conn: Writable SQLAlchemy ``Connection``.
        """
        metadata.create_all(conn)
        for ddl in _CREATE_FTS_DDL:
            conn.exec_driver_sql(ddl)

    @staticmethod
    def _prune_legacy_v3_graph_rows(conn: Connection) -> None:
        """Remove legacy variable nodes, their refresh state, and structural edges."""
        legacy_node_subq = f"SELECT id FROM nodes WHERE kind IN {_LEGACY_VARIABLE_KINDS_SQL}"
        statements = (
            f"DELETE FROM reference_refresh WHERE target_symbol_id IN ({legacy_node_subq})",
            f"DELETE FROM edges WHERE kind IN {_LEGACY_STRUCTURAL_EDGES_SQL}",
            f"DELETE FROM edges WHERE from_id IN ({legacy_node_subq}) "
            f"OR to_id IN ({legacy_node_subq})",
            f"DELETE FROM nodes WHERE kind IN {_LEGACY_VARIABLE_KINDS_SQL}",
        )
        for stmt in statements:
            conn.exec_driver_sql(stmt)

    def _apply_upgrade(self, conn: Connection, current_version: int) -> None:
        """Apply DDL creation and versioned legacy cleanup before setting user_version."""
        if current_version < 2:
            self._drop_legacy_v1_tables(conn)
        self._create_v2_schema(conn)
        if 2 <= current_version < SCHEMA_VERSION:
            self._prune_legacy_v3_graph_rows(conn)
        conn.exec_driver_sql(f"PRAGMA user_version = {SCHEMA_VERSION}")
        conn.commit()
        logger.info(
            "Initialized SQLite schema v%d (previous user_version=%d)",
            SCHEMA_VERSION,
            current_version,
        )

    def initialize(self, conn: Connection) -> None:
        """Initialize or upgrade ``conn`` to SQLite schema v4.

        No-ops when ``PRAGMA user_version == 4``. Upgrades ``current_version < 2``
        by dropping legacy v1 objects, upgrades ``2 <= current_version < 4``
        non-destructively while pruning legacy variable nodes and structural edges,
        and rejects mismatched read-only connections or unsupported future versions.
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
                "Database schema mismatch; run `cbi index` to upgrade.",
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
            self._apply_upgrade(conn, current_version)
        except OperationalError as exc:
            if "readonly" in str(exc).lower():
                logger.warning("Read-only operational error during schema migration: %s", exc)
                raise DatabaseError(
                    "Database schema mismatch; run `cbi index` to upgrade.",
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
