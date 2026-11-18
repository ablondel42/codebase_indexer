"""Database schema initialization and v2 migration via SQLAlchemy Core."""

from __future__ import annotations

from sqlalchemy.engine import Connection
from sqlalchemy.exc import OperationalError, SQLAlchemyError

from repo_context.exceptions import DatabaseError
from repo_context.storage.schema import metadata

SCHEMA_VERSION = 2

_DROP_LEGACY_FTS_DDL = (
    "DROP TRIGGER IF EXISTS nodes_fts_ai",
    "DROP TRIGGER IF EXISTS nodes_fts_au",
    "DROP TRIGGER IF EXISTS nodes_fts_ad",
    "DROP TABLE IF EXISTS nodes_fts",
)

_CREATE_FTS_DDL = (
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


def initialize_database(conn: Connection) -> None:
    """Initialize or upgrade the SQLite database to schema v2.

    If ``PRAGMA user_version == 2``, returns immediately.
    If ``0 <= user_version < 2``, drops legacy v1 objects cleanly and creates v2 schema.
    Raises ``DatabaseError`` on read-only connections with a mismatched schema version.
    """
    current_version = int(conn.exec_driver_sql("PRAGMA user_version").scalar_one())
    if current_version == SCHEMA_VERSION:
        return

    query_only = int(conn.exec_driver_sql("PRAGMA query_only").scalar_one())
    if query_only:
        raise DatabaseError(
            "Database schema mismatch; run `rc index` to upgrade.",
            context={"current_version": current_version, "expected_version": SCHEMA_VERSION},
        )

    if current_version > SCHEMA_VERSION:
        raise DatabaseError(
            f"Unsupported database schema version {current_version} (expected {SCHEMA_VERSION}).",
            context={"current_version": current_version, "expected_version": SCHEMA_VERSION},
        )

    try:
        for ddl in _DROP_LEGACY_FTS_DDL:
            conn.exec_driver_sql(ddl)
        metadata.drop_all(conn)
        metadata.create_all(conn)
        for ddl in _CREATE_FTS_DDL:
            conn.exec_driver_sql(ddl)
        conn.exec_driver_sql(f"PRAGMA user_version = {SCHEMA_VERSION}")
        conn.commit()
    except OperationalError as exc:
        if "readonly" in str(exc).lower():
            raise DatabaseError(
                "Database schema mismatch; run `rc index` to upgrade.",
                context={"current_version": current_version, "expected_version": SCHEMA_VERSION},
            ) from exc
        raise DatabaseError(
            f"Failed to initialize database schema v{SCHEMA_VERSION}: {exc}",
            context={"current_version": current_version},
        ) from exc
    except SQLAlchemyError as exc:
        raise DatabaseError(
            f"Failed to initialize database schema v{SCHEMA_VERSION}: {exc}",
            context={"current_version": current_version},
        ) from exc
