"""Database schema initialization and v2 migration."""

import sqlite3

from repo_context.exceptions import DatabaseError

SCHEMA_VERSION = 2

_DROP_LEGACY_SQL = """
DROP TRIGGER IF EXISTS nodes_fts_ai;
DROP TRIGGER IF EXISTS nodes_fts_au;
DROP TRIGGER IF EXISTS nodes_fts_ad;
DROP TABLE IF EXISTS nodes_fts;
DROP TABLE IF EXISTS reference_refresh;
DROP TABLE IF EXISTS index_runs;
DROP TABLE IF EXISTS edges;
DROP TABLE IF EXISTS nodes;
DROP TABLE IF EXISTS files;
DROP TABLE IF EXISTS repos;
"""


def initialize_database(conn: sqlite3.Connection) -> None:
    """Initialize or upgrade the SQLite database to schema v2.

    If ``PRAGMA user_version == 2``, returns immediately.
    If ``0 <= user_version < 2``, drops legacy v1 tables cleanly and creates v2 schema.
    Raises ``DatabaseError`` on read-only connections with a mismatched schema version.
    """
    current_version = int(conn.execute("PRAGMA user_version").fetchone()[0])
    if current_version == SCHEMA_VERSION:
        return

    query_only = int(conn.execute("PRAGMA query_only").fetchone()[0])
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
        conn.executescript(_DROP_LEGACY_SQL)
        conn.executescript(_get_schema_sql())
        conn.execute(f"PRAGMA user_version = {SCHEMA_VERSION};")
        conn.commit()
    except sqlite3.OperationalError as exc:
        if "readonly" in str(exc).lower():
            raise DatabaseError(
                "Database schema mismatch; run `rc index` to upgrade.",
                context={"current_version": current_version, "expected_version": SCHEMA_VERSION},
            ) from exc
        raise DatabaseError(
            f"Failed to initialize database schema v{SCHEMA_VERSION}: {exc}",
            context={"current_version": current_version},
        ) from exc
    except sqlite3.Error as exc:
        raise DatabaseError(
            f"Failed to initialize database schema v{SCHEMA_VERSION}: {exc}",
            context={"current_version": current_version},
        ) from exc


def _get_schema_sql() -> str:
    """Return the complete schema v2 SQL DDL."""
    return """
CREATE TABLE IF NOT EXISTS repos (
    id TEXT PRIMARY KEY,
    root_path TEXT NOT NULL,
    name TEXT NOT NULL,
    default_language TEXT NOT NULL,
    created_at TEXT NOT NULL,
    last_indexed_at TEXT
);

CREATE TABLE IF NOT EXISTS files (
    id TEXT PRIMARY KEY,
    repo_id TEXT NOT NULL,
    file_path TEXT NOT NULL,
    uri TEXT NOT NULL,
    module_path TEXT NOT NULL,
    language TEXT NOT NULL,
    content_hash TEXT NOT NULL,
    size_bytes INTEGER NOT NULL,
    line_count INTEGER NOT NULL DEFAULT 0,
    is_test INTEGER NOT NULL DEFAULT 0,
    last_modified_at TEXT NOT NULL,
    last_indexed_at TEXT,
    UNIQUE(repo_id, file_path),
    FOREIGN KEY(repo_id) REFERENCES repos(id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS nodes (
    id TEXT PRIMARY KEY,
    repo_id TEXT NOT NULL,
    file_id TEXT NOT NULL,
    language TEXT NOT NULL,
    kind TEXT NOT NULL,
    name TEXT NOT NULL,
    qualified_name TEXT NOT NULL,
    uri TEXT NOT NULL,
    range_json TEXT,
    selection_range_json TEXT,
    start_line INTEGER,
    end_line INTEGER,
    signature TEXT,
    parent_id TEXT,
    visibility_hint TEXT,
    doc_summary TEXT,
    content_hash TEXT NOT NULL,
    semantic_hash TEXT NOT NULL,
    source TEXT NOT NULL,
    confidence REAL NOT NULL,
    payload_json TEXT NOT NULL,
    scope TEXT,
    lexical_parent_id TEXT,
    last_indexed_at TEXT,
    FOREIGN KEY(repo_id) REFERENCES repos(id) ON DELETE CASCADE,
    FOREIGN KEY(file_id) REFERENCES files(id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS edges (
    id TEXT PRIMARY KEY,
    repo_id TEXT NOT NULL,
    kind TEXT NOT NULL,
    from_id TEXT NOT NULL,
    to_id TEXT NOT NULL,
    source TEXT NOT NULL,
    confidence REAL NOT NULL,
    evidence_file_id TEXT,
    evidence_uri TEXT,
    evidence_range_json TEXT,
    payload_json TEXT NOT NULL,
    last_indexed_at TEXT,
    FOREIGN KEY(repo_id) REFERENCES repos(id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS index_runs (
    id TEXT PRIMARY KEY,
    repo_id TEXT NOT NULL,
    run_type TEXT NOT NULL,
    started_at TEXT NOT NULL,
    completed_at TEXT,
    status TEXT NOT NULL,
    files_scanned INTEGER DEFAULT 0,
    nodes_extracted INTEGER DEFAULT 0,
    edges_extracted INTEGER DEFAULT 0,
    error_message TEXT,
    FOREIGN KEY(repo_id) REFERENCES repos(id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS reference_refresh (
    target_symbol_id TEXT PRIMARY KEY,
    available INTEGER NOT NULL DEFAULT 0,
    last_refreshed_at TEXT,
    refresh_status TEXT,
    error_code TEXT,
    FOREIGN KEY(target_symbol_id) REFERENCES nodes(id) ON DELETE CASCADE
);

-- Files indexes
CREATE INDEX IF NOT EXISTS idx_files_repo_id ON files(repo_id);
CREATE INDEX IF NOT EXISTS idx_files_uri ON files(uri);

-- Nodes indexes
CREATE INDEX IF NOT EXISTS idx_nodes_repo_id ON nodes(repo_id);
CREATE INDEX IF NOT EXISTS idx_nodes_file_id ON nodes(file_id);
CREATE INDEX IF NOT EXISTS idx_nodes_qualified_name ON nodes(qualified_name);
CREATE INDEX IF NOT EXISTS idx_nodes_name ON nodes(name);
CREATE INDEX IF NOT EXISTS idx_nodes_repo_kind_qname ON nodes(repo_id, kind, qualified_name);
CREATE INDEX IF NOT EXISTS idx_nodes_parent_id ON nodes(parent_id);
CREATE INDEX IF NOT EXISTS idx_nodes_lexical_parent_id ON nodes(lexical_parent_id);
CREATE INDEX IF NOT EXISTS idx_nodes_file_line ON nodes(file_id, start_line);

-- Edges indexes
CREATE INDEX IF NOT EXISTS idx_edges_repo_id ON edges(repo_id);
CREATE INDEX IF NOT EXISTS idx_edges_from_kind ON edges(from_id, kind);
CREATE INDEX IF NOT EXISTS idx_edges_to_kind ON edges(to_id, kind);
CREATE INDEX IF NOT EXISTS idx_edges_evfile_kind ON edges(evidence_file_id, kind);

-- Index runs & reference refresh indexes
CREATE INDEX IF NOT EXISTS idx_index_runs_repo_id ON index_runs(repo_id);
CREATE INDEX IF NOT EXISTS idx_index_runs_status ON index_runs(status);
CREATE INDEX IF NOT EXISTS idx_reference_refresh_available ON reference_refresh(available);

-- FTS5 symbol search index
CREATE VIRTUAL TABLE IF NOT EXISTS nodes_fts USING fts5(
    node_id UNINDEXED,
    repo_id UNINDEXED,
    name,
    qualified_name,
    signature,
    doc_summary,
    tokenize="unicode61 tokenchars '_'"
);

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
END;

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
END;

CREATE TRIGGER IF NOT EXISTS nodes_fts_ad AFTER DELETE ON nodes BEGIN
    DELETE FROM nodes_fts WHERE rowid = old.rowid;
END;
"""
