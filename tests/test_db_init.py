"""SQLite schema v2 initialization, read-only guards, and UnitOfWork lifecycle tests."""

import sqlite3
from collections.abc import Iterator
from pathlib import Path

import pytest
from sqlalchemy.engine import Connection
from sqlalchemy.exc import DatabaseError as SADatabaseError
from sqlalchemy.exc import OperationalError

from core.models import (
    DatabaseError,
    DatabaseNotFoundError,
    Language,
    RepoRecord,
    SymbolKind,
    SymbolNode,
    ValidationError,
)
from storage import (
    ConnectionFactory,
    SchemaMigrator,
    SqlUnitOfWorkFactory,
)
from storage.utils.schema import SCHEMA_VERSION


@pytest.fixture
def temp_db_path(tmp_path: Path) -> Path:
    """Return a temporary SQLite database path."""
    return tmp_path / "test.db"


@pytest.fixture
def writable_conn(temp_db_path: Path) -> Iterator[Connection]:
    """Open and initialize a v2 writable connection, closing in teardown."""
    factory = ConnectionFactory()
    migrator = SchemaMigrator()
    conn = factory.open_path(temp_db_path)
    try:
        migrator.initialize(conn)
        yield conn
    finally:
        factory.close(conn)


def test_schema_version_is_3(writable_conn: Connection) -> None:
    """Initialized DB sets PRAGMA user_version == 3 and enables WAL + FK pragmas."""
    version = int(writable_conn.exec_driver_sql("PRAGMA user_version").scalar_one())
    fk = int(writable_conn.exec_driver_sql("PRAGMA foreign_keys").scalar_one())
    journal = str(writable_conn.exec_driver_sql("PRAGMA journal_mode").scalar_one()).lower()
    busy_timeout = int(writable_conn.exec_driver_sql("PRAGMA busy_timeout").scalar_one())

    assert SCHEMA_VERSION == 3
    assert version == 3
    assert fk == 1
    assert journal == "wal"
    assert busy_timeout == 5000


def test_initialize_database_creates_v3_tables_fts_and_triggers(
    writable_conn: Connection,
) -> None:
    """Schema v3 creates core tables, text_files, both FTS5 tables, triggers, and indexes."""
    tables = set(
        writable_conn.exec_driver_sql("SELECT name FROM sqlite_master WHERE type='table'").scalars()
    )
    expected_tables = {
        "repos",
        "files",
        "text_files",
        "nodes",
        "edges",
        "index_runs",
        "reference_refresh",
        "nodes_fts",
        "text_fts",
    }
    assert expected_tables.issubset(tables)

    triggers = set(
        writable_conn.exec_driver_sql(
            "SELECT name FROM sqlite_master WHERE type='trigger'"
        ).scalars()
    )
    assert {
        "nodes_fts_ai",
        "nodes_fts_au",
        "nodes_fts_ad",
        "text_files_ai",
        "text_files_au",
        "text_files_ad",
    }.issubset(triggers)

    indexes = set(
        writable_conn.exec_driver_sql("SELECT name FROM sqlite_master WHERE type='index'").scalars()
    )
    expected_indexes = {
        "idx_files_repo_id",
        "idx_files_uri",
        "idx_text_files_repo_id",
        "idx_text_files_repo_lang",
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


def test_initialize_database_is_idempotent_and_rejects_future_version(
    writable_conn: Connection,
) -> None:
    """SchemaMigrator.initialize is idempotent on v3 and rejects future versions."""
    migrator = SchemaMigrator()
    migrator.initialize(writable_conn)
    assert migrator.read_user_version(writable_conn) == 3

    writable_conn.exec_driver_sql("PRAGMA user_version = 99")
    writable_conn.commit()
    with pytest.raises(DatabaseError, match="Unsupported database schema version"):
        migrator.initialize(writable_conn)


def test_foreign_key_constraints_enforced(temp_db_path: Path) -> None:
    """Inserting a node referencing a missing repo/file fails due to FK enforcement."""
    uow_factory = SqlUnitOfWorkFactory(temp_db_path)
    orphan_node = SymbolNode(
        id="node:missing",
        repo_id="project:missing",
        file_id="file:missing",
        language=Language.PYTHON,
        kind=SymbolKind.MODULE,
        name="m",
        qualified_name="m",
        uri="file:///m.py",
        content_hash="sha256:a",
        semantic_hash="sha256:b",
    )
    with pytest.raises(DatabaseError), uow_factory.open(initialize=True) as uow:
        uow.nodes.upsert(orphan_node)


def test_v1_upgrade_drops_legacy_tables_and_upgrades_to_v3(temp_db_path: Path) -> None:
    """Upgrading a v1 database cleanly replaces legacy tables with v3 schema."""
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

    factory = ConnectionFactory()
    migrator = SchemaMigrator()
    conn = factory.open_path(temp_db_path)
    try:
        migrator.initialize(conn)
        assert migrator.read_user_version(conn) == 3
        cols = {row[1] for row in conn.exec_driver_sql("PRAGMA table_info(files)").fetchall()}
        assert {"line_count", "is_test"}.issubset(cols)
    finally:
        factory.close(conn)


def test_v2_upgrade_is_non_destructive_and_adds_text_fts(temp_db_path: Path) -> None:
    """Upgrading a v2 database preserves existing repos while adding text_files and text_fts."""
    factory = ConnectionFactory()
    migrator = SchemaMigrator()
    conn = factory.open_path(temp_db_path)
    try:
        migrator.initialize(conn)
        conn.exec_driver_sql(
            "INSERT INTO repos (id, root_path, name, default_language, created_at) "
            "VALUES ('project:keep', '/keep', 'keep', 'python', '2026-01-01T00:00:00Z')"
        )
        conn.exec_driver_sql("DROP TRIGGER IF EXISTS text_files_ai")
        conn.exec_driver_sql("DROP TRIGGER IF EXISTS text_files_au")
        conn.exec_driver_sql("DROP TRIGGER IF EXISTS text_files_ad")
        conn.exec_driver_sql("DROP TABLE IF EXISTS text_fts")
        conn.exec_driver_sql("DROP TABLE IF EXISTS text_files")
        conn.exec_driver_sql("PRAGMA user_version = 2")
        conn.commit()

        migrator.initialize(conn)
        assert migrator.read_user_version(conn) == 3
        kept = conn.exec_driver_sql("SELECT id FROM repos WHERE id = 'project:keep'").scalar_one()
        assert kept == "project:keep"
        tables = set(
            conn.exec_driver_sql("SELECT name FROM sqlite_master WHERE type='table'").scalars()
        )
        assert {"text_files", "text_fts"}.issubset(tables)
    finally:
        factory.close(conn)


def test_read_only_missing_db_raises_without_creating_file(temp_db_path: Path) -> None:
    """read_only=True and must_exist=True raise DatabaseNotFoundError with zero disk writes."""
    factory = ConnectionFactory()
    assert not temp_db_path.exists()

    with pytest.raises(DatabaseNotFoundError):
        factory.open_path(temp_db_path, read_only=True)
    assert not temp_db_path.exists()

    with pytest.raises(DatabaseNotFoundError):
        factory.open_path(temp_db_path, must_exist=True)
    assert not temp_db_path.exists()

    uow_factory = SqlUnitOfWorkFactory(temp_db_path)
    with pytest.raises(DatabaseNotFoundError), uow_factory.open(read_only=True):
        pass
    assert not temp_db_path.exists()


def test_read_only_mode_blocks_writes_and_flags_schema_mismatch(temp_db_path: Path) -> None:
    """read_only=True enforces query_only=ON and rejects mismatched schema versions."""
    factory = ConnectionFactory()
    migrator = SchemaMigrator()
    conn = factory.open_path(temp_db_path)
    try:
        conn.exec_driver_sql("CREATE TABLE legacy (id TEXT PRIMARY KEY)")
        conn.commit()
    finally:
        factory.close(conn)

    ro_conn = factory.open_path(temp_db_path, read_only=True)
    try:
        assert int(ro_conn.exec_driver_sql("PRAGMA query_only").scalar_one()) == 1
        with pytest.raises(DatabaseError, match="schema mismatch"):
            migrator.initialize(ro_conn)
        with pytest.raises(OperationalError):
            ro_conn.exec_driver_sql("INSERT INTO legacy (id) VALUES ('x')")
    finally:
        factory.close(ro_conn)

    uow_factory = SqlUnitOfWorkFactory(temp_db_path)
    with uow_factory.open(initialize=True):
        pass
    with uow_factory.open(read_only=True) as ro_uow:
        with pytest.raises(DatabaseError, match="read-only"):
            ro_uow.commit()
        with pytest.raises(DatabaseError, match="read-only"), ro_uow.savepoint():
            pass


def test_readonly_sql_authorizer_and_uow_execute_readonly_sql(temp_db_path: Path) -> None:
    """SELECT-only authorizer permits SELECT/PRAGMA and denies all mutations."""
    uow_factory = SqlUnitOfWorkFactory(temp_db_path)
    with uow_factory.open(initialize=True) as uow:
        uow.repos.upsert(
            RepoRecord(
                id="project:r1",
                root_path="/tmp/r1",
                name="r1",
                created_at="2026-01-01T00:00:00Z",
            )
        )
        uow.repos.upsert(
            RepoRecord(
                id="project:r2",
                root_path="/tmp/r2",
                name="r2",
                created_at="2026-01-01T00:00:00Z",
            )
        )

    res = uow_factory.execute_readonly_sql("SELECT id FROM repos ORDER BY id", limit=1)
    assert res.columns == ["id"]
    assert res.row_count == 1
    assert res.truncated is True
    assert res.rows == [{"id": "project:r1"}]

    with pytest.raises(ValidationError):
        uow_factory.execute_readonly_sql("   ")
    with pytest.raises(ValidationError):
        uow_factory.execute_readonly_sql("SELECT 1", limit=0)

    for stmt in (
        "DELETE FROM repos",
        "DROP TABLE repos",
        "UPDATE repos SET name = 'x'",
        "INSERT INTO repos (id, root_path, name, default_language, created_at) "
        "VALUES ('project:x', '/x', 'x', 'python', '2026-01-01T00:00:00Z')",
    ):
        with pytest.raises(DatabaseError):
            uow_factory.execute_readonly_sql(stmt)

    conn_factory = ConnectionFactory()
    sql_conn = conn_factory.open_readonly_sql(temp_db_path)
    try:
        assert sql_conn.exec_driver_sql("SELECT 1 AS one").mappings().one()["one"] == 1
        assert int(sql_conn.exec_driver_sql("PRAGMA user_version").scalar_one()) == 3
        with pytest.raises(SADatabaseError):
            sql_conn.exec_driver_sql("DELETE FROM repos")
    finally:
        conn_factory.close(sql_conn)


def test_uow_commits_rolls_back_and_shared_memory_persistence(temp_db_path: Path) -> None:
    """SqlUnitOfWorkFactory auto-commits on clean exit, rolls back on error, and pins :memory:."""
    uow_factory = SqlUnitOfWorkFactory(temp_db_path)
    with uow_factory.open(initialize=True) as uow:
        uow.repos.upsert(
            RepoRecord(
                id="project:ok",
                root_path="/tmp/ok",
                name="ok",
                created_at="2026-01-01T00:00:00Z",
            )
        )

    with pytest.raises(RuntimeError, match="boom"), uow_factory.open() as uow:
        uow.repos.upsert(
            RepoRecord(
                id="project:fail",
                root_path="/tmp/fail",
                name="fail",
                created_at="2026-01-01T00:00:00Z",
            )
        )
        raise RuntimeError("boom")

    with uow_factory.open(read_only=True) as ro_uow:
        assert {r.id for r in ro_uow.repos.list_all()} == {"project:ok"}

    mem_factory = SqlUnitOfWorkFactory(":memory:")
    try:
        with mem_factory.open(initialize=True) as mem_uow:
            mem_uow.repos.upsert(
                RepoRecord(
                    id="project:mem",
                    root_path="/tmp/mem",
                    name="mem",
                    created_at="2026-01-01T00:00:00Z",
                )
            )
        with mem_factory.open(read_only=True) as mem_ro:
            assert [r.id for r in mem_ro.repos.list_all()] == ["project:mem"]
        sql_res = mem_factory.execute_readonly_sql("SELECT id FROM repos")
        assert sql_res.rows == [{"id": "project:mem"}]
    finally:
        mem_factory.close()
