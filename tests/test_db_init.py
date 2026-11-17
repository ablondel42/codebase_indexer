"""SQLite schema v2 initialization, read-only guards, and connection lifecycle tests."""

from collections.abc import Iterator
from pathlib import Path
import sqlite3

import pytest

from repo_context.exceptions import DatabaseError, DatabaseNotFoundError
from repo_context.storage import (
    SCHEMA_VERSION,
    close_connection,
    connection_scope,
    get_connection,
    initialize_database,
    open_read_only_sql_connection,
)


@pytest.fixture
def temp_db_path(tmp_path: Path) -> Path:
    """Return a temporary SQLite database path."""
    return tmp_path / "test.db"


@pytest.fixture
def writable_conn(temp_db_path: Path) -> Iterator[sqlite3.Connection]:
    """Open and initialize a v2 writable connection, closing in teardown."""
    conn = get_connection(temp_db_path)
    try:
        initialize_database(conn)
        yield conn
    finally:
        close_connection(conn)


def test_schema_version_is_2(writable_conn: sqlite3.Connection) -> None:
    """Initialized DB sets PRAGMA user_version == 2 and enables WAL + FK pragmas."""
    version = int(writable_conn.execute("PRAGMA user_version").fetchone()[0])
    fk = int(writable_conn.execute("PRAGMA foreign_keys").fetchone()[0])
    journal = str(writable_conn.execute("PRAGMA journal_mode").fetchone()[0]).lower()
    busy_timeout = int(writable_conn.execute("PRAGMA busy_timeout").fetchone()[0])

    assert SCHEMA_VERSION == 2
    assert version == 2
    assert fk == 1
    assert journal == "wal"
    assert busy_timeout == 5000


def test_initialize_database_creates_v2_tables_fts_and_triggers(
    writable_conn: sqlite3.Connection,
) -> None:
    """Schema v2 creates core tables, FTS5 table, triggers, and v2 indexes."""
    tables = {
        row[0]
        for row in writable_conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        ).fetchall()
    }
    expected_tables = {
        "repos",
        "files",
        "nodes",
        "edges",
        "index_runs",
        "reference_refresh",
        "nodes_fts",
    }
    assert expected_tables.issubset(tables)

    triggers = {
        row[0]
        for row in writable_conn.execute(
            "SELECT name FROM sqlite_master WHERE type='trigger'"
        ).fetchall()
    }
    assert {"nodes_fts_ai", "nodes_fts_au", "nodes_fts_ad"}.issubset(triggers)

    indexes = {
        row[0]
        for row in writable_conn.execute(
            "SELECT name FROM sqlite_master WHERE type='index'"
        ).fetchall()
    }
    expected_indexes = {
        "idx_files_repo_id",
        "idx_files_uri",
        "idx_nodes_repo_id",
        "idx_nodes_file_id",
        "idx_nodes_qualified_name",
        "idx_nodes_name",
        "idx_nodes_repo_kind_qname",
        "idx_nodes_parent_id",
        "idx_nodes_lexical_parent_id",
        "idx_nodes_file_line",
        "idx_edges_repo_id",
        "idx_edges_from_kind",
        "idx_edges_to_kind",
        "idx_edges_evfile_kind",
        "idx_index_runs_repo_id",
        "idx_index_runs_status",
        "idx_reference_refresh_available",
    }
    assert expected_indexes.issubset(indexes)


def test_initialize_database_is_idempotent(writable_conn: sqlite3.Connection) -> None:
    """Calling initialize_database again on a v2 DB is a no-op."""
    initialize_database(writable_conn)
    assert int(writable_conn.execute("PRAGMA user_version").fetchone()[0]) == 2


def test_foreign_key_constraints_enforced(writable_conn: sqlite3.Connection) -> None:
    """Inserting a node referencing a missing repo/file fails due to FK enforcement."""
    with pytest.raises(sqlite3.IntegrityError):
        writable_conn.execute(
            """
            INSERT INTO nodes (
                id, repo_id, file_id, language, kind, name, qualified_name, uri,
                content_hash, semantic_hash, source, confidence, payload_json
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                "sym:missing",
                "repo:missing",
                "file:missing",
                "python",
                "module",
                "m",
                "m",
                "file:///m.py",
                "sha256:a",
                "sha256:b",
                "python-ast",
                1.0,
                "{}",
            ),
        )


def test_v1_upgrade_drops_legacy_tables_and_upgrades_to_v2(temp_db_path: Path) -> None:
    """Upgrading a v1 database cleanly replaces legacy tables with v2 schema."""
    raw = sqlite3.connect(temp_db_path)
    try:
        raw.execute(
            "CREATE TABLE repos (id TEXT PRIMARY KEY, root_path TEXT, name TEXT, "
            "default_language TEXT, created_at TEXT, last_indexed_at TEXT)"
        )
        raw.execute("PRAGMA user_version = 1")
        raw.commit()
    finally:
        raw.close()

    conn = get_connection(temp_db_path)
    try:
        initialize_database(conn)
        assert int(conn.execute("PRAGMA user_version").fetchone()[0]) == 2
        cols = {
            row[1]
            for row in conn.execute("PRAGMA table_info(files)").fetchall()
        }
        assert {"line_count", "is_test"}.issubset(cols)
    finally:
        close_connection(conn)


def test_read_only_missing_db_raises_without_creating_file(temp_db_path: Path) -> None:
    """read_only=True and must_exist=True raise DatabaseNotFoundError with zero disk writes."""
    assert not temp_db_path.exists()

    with pytest.raises(DatabaseNotFoundError):
        get_connection(temp_db_path, read_only=True)
    assert not temp_db_path.exists()

    with pytest.raises(DatabaseNotFoundError):
        get_connection(temp_db_path, must_exist=True)
    assert not temp_db_path.exists()


def test_read_only_mode_blocks_writes_and_flags_schema_mismatch(temp_db_path: Path) -> None:
    """read_only=True enforces query_only=ON and rejects mismatched schema versions."""
    conn = get_connection(temp_db_path)
    try:
        conn.execute("CREATE TABLE legacy (id TEXT PRIMARY KEY)")
        conn.commit()
    finally:
        close_connection(conn)

    ro_conn = get_connection(temp_db_path, read_only=True)
    try:
        assert int(ro_conn.execute("PRAGMA query_only").fetchone()[0]) == 1
        with pytest.raises(DatabaseError, match="schema mismatch"):
            initialize_database(ro_conn)
        with pytest.raises(sqlite3.OperationalError):
            ro_conn.execute("INSERT INTO legacy (id) VALUES ('x')")
    finally:
        close_connection(ro_conn)


def test_open_read_only_sql_connection_authorizer(temp_db_path: Path) -> None:
    """open_read_only_sql_connection permits SELECT/PRAGMA and denies mutations."""
    with connection_scope(temp_db_path) as conn:
        initialize_database(conn)

    sql_conn = open_read_only_sql_connection(temp_db_path)
    try:
        assert sql_conn.execute("SELECT 1 AS one").fetchone()["one"] == 1
        assert int(sql_conn.execute("PRAGMA user_version").fetchone()[0]) == 2
        for stmt in (
            "DELETE FROM repos",
            "DROP TABLE repos",
            "UPDATE repos SET name = 'x'",
            "INSERT INTO repos (id, root_path, name, default_language, created_at) "
            "VALUES ('repo:x', '/x', 'x', 'python', '2026-01-01T00:00:00Z')",
        ):
            with pytest.raises(sqlite3.DatabaseError):
                sql_conn.execute(stmt)
    finally:
        close_connection(sql_conn)


def test_connection_scope_commits_and_rolls_back(temp_db_path: Path) -> None:
    """connection_scope commits on success and rolls back on exception."""
    with connection_scope(temp_db_path) as conn:
        initialize_database(conn)
        conn.execute(
            "INSERT INTO repos (id, root_path, name, default_language, created_at) "
            "VALUES ('repo:ok', '/tmp/ok', 'ok', 'python', '2026-01-01T00:00:00Z')"
        )

    with pytest.raises(RuntimeError, match="boom"):
        with connection_scope(temp_db_path) as conn:
            conn.execute(
                "INSERT INTO repos (id, root_path, name, default_language, created_at) "
                "VALUES ('repo:fail', '/tmp/fail', 'fail', 'python', '2026-01-01T00:00:00Z')"
            )
            raise RuntimeError("boom")

    with connection_scope(temp_db_path, read_only=True) as conn:
        ids = {row["id"] for row in conn.execute("SELECT id FROM repos").fetchall()}
    assert ids == {"repo:ok"}
