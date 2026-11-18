"""Graph query layer tests covering FTS5 search, escaped LIKE, directed edges, and freshness."""

from collections.abc import Iterator
from pathlib import Path

import pytest
from sqlalchemy.engine import Connection

from repo_context.constants import (
    EDGE_KIND_CALLS,
    EDGE_KIND_CONTAINS,
    EDGE_KIND_IMPORTS,
    EDGE_KIND_INHERITS,
    EDGE_KIND_SCOPE_PARENT,
)
from repo_context.graph import (
    check_file_disk_freshness,
    find_symbols_by_name,
    get_child_symbols,
    get_file_for_outline,
    get_files_by_ids,
    get_incoming_calls,
    get_incoming_edges,
    get_incoming_imports,
    get_incoming_inherits,
    get_lexical_child_symbols,
    get_lexical_parent_symbol,
    get_outgoing_calls,
    get_outgoing_edges,
    get_outgoing_imports,
    get_outgoing_inherits,
    get_parent_symbol,
    get_symbol,
    get_symbol_by_qualified_name,
    get_symbols_by_ids,
    list_file_import_edges,
    list_file_outline_symbols,
    search_symbols_fts,
)
from repo_context.models.file import FileRecord
from repo_context.models.repo import RepoRecord
from repo_context.parsing.hashing import sha256_file
from repo_context.storage import (
    close_connection,
    get_connection,
    initialize_database,
    upsert_edges,
    upsert_files,
    upsert_nodes,
    upsert_repo,
)


@pytest.fixture
def query_db(tmp_path: Path) -> Iterator[tuple[Connection, Path]]:
    """Yield an initialized DB populated with real files on disk and rich graph edges."""
    repo_root = tmp_path / "repo"
    repo_root.mkdir()
    mod1_path = repo_root / "mod1.py"
    mod1_path.write_text("class Class1:\n    pass\n", encoding="utf-8")
    mod2_path = repo_root / "mod2.py"
    mod2_path.write_text("from mod1 import Class1\n", encoding="utf-8")

    conn = get_connection(tmp_path / "query.db")
    try:
        initialize_database(conn)
        repo_id = "repo:test:12345678"
        upsert_repo(
            conn,
            RepoRecord(
                id=repo_id,
                root_path=str(repo_root),
                name="test",
                created_at="2026-01-01T00:00:00Z",
                last_indexed_at="2026-01-01T00:00:00Z",
            ),
        )
        f1 = FileRecord(
            id=f"file:{repo_id}:mod1.py",
            repo_id=repo_id,
            file_path="mod1.py",
            uri=mod1_path.as_uri(),
            module_path="mod1",
            content_hash=sha256_file(mod1_path),
            size_bytes=mod1_path.stat().st_size,
            line_count=2,
            last_modified_at="2026-01-01T00:00:00Z",
        )
        f2 = FileRecord(
            id=f"file:{repo_id}:mod2.py",
            repo_id=repo_id,
            file_path="mod2.py",
            uri=mod2_path.as_uri(),
            module_path="mod2",
            content_hash=sha256_file(mod2_path),
            size_bytes=mod2_path.stat().st_size,
            line_count=1,
            last_modified_at="2026-01-01T00:00:00Z",
        )
        upsert_files(conn, [f1, f2])

        nodes = [
            {
                "id": "sym:mod1",
                "repo_id": repo_id,
                "file_id": f1.id,
                "language": "python",
                "kind": "module",
                "name": "mod1",
                "qualified_name": "mod1",
                "uri": f1.uri,
                "start_line": 1,
                "end_line": 20,
                "parent_id": None,
                "visibility_hint": "module",
                "doc_summary": "Module one",
                "content_hash": "sha256:a",
                "semantic_hash": "sha256:b",
                "source": "python-ast",
                "confidence": 1.0,
                "payload_json": "{}",
                "scope": "module",
                "lexical_parent_id": None,
                "last_indexed_at": "2026-01-01T00:00:00Z",
            },
            {
                "id": "sym:cls1",
                "repo_id": repo_id,
                "file_id": f1.id,
                "language": "python",
                "kind": "class",
                "name": "Class1",
                "qualified_name": "mod1.Class1",
                "uri": f1.uri,
                "start_line": 1,
                "end_line": 2,
                "signature": "class Class1",
                "parent_id": "sym:mod1",
                "visibility_hint": "public",
                "doc_summary": "Base class",
                "content_hash": "sha256:c",
                "semantic_hash": "sha256:d",
                "source": "python-ast",
                "confidence": 1.0,
                "payload_json": "{}",
                "scope": "module",
                "lexical_parent_id": None,
                "last_indexed_at": "2026-01-01T00:00:00Z",
            },
            {
                "id": "sym:func_pct",
                "repo_id": repo_id,
                "file_id": f1.id,
                "language": "python",
                "kind": "function",
                "name": "compute_100%_rate",
                "qualified_name": "mod1.compute_100%_rate",
                "uri": f1.uri,
                "start_line": 5,
                "end_line": 10,
                "signature": "def compute_100%_rate()",
                "parent_id": "sym:mod1",
                "visibility_hint": "public",
                "doc_summary": "Exact percent handler",
                "content_hash": "sha256:e",
                "semantic_hash": "sha256:f",
                "source": "python-ast",
                "confidence": 1.0,
                "payload_json": "{}",
                "scope": "module",
                "lexical_parent_id": None,
                "last_indexed_at": "2026-01-01T00:00:00Z",
            },
            {
                "id": "sym:inner",
                "repo_id": repo_id,
                "file_id": f1.id,
                "language": "python",
                "kind": "local_function",
                "name": "inner",
                "qualified_name": "mod1.compute_100%_rate.inner",
                "uri": f1.uri,
                "start_line": 6,
                "end_line": 8,
                "parent_id": "sym:func_pct",
                "visibility_hint": "local",
                "doc_summary": "Inner helper",
                "content_hash": "sha256:g",
                "semantic_hash": "sha256:h",
                "source": "python-ast",
                "confidence": 1.0,
                "payload_json": "{}",
                "scope": "function",
                "lexical_parent_id": "sym:func_pct",
                "last_indexed_at": "2026-01-01T00:00:00Z",
            },
            {
                "id": "sym:mod2",
                "repo_id": repo_id,
                "file_id": f2.id,
                "language": "python",
                "kind": "module",
                "name": "mod2",
                "qualified_name": "mod2",
                "uri": f2.uri,
                "start_line": 1,
                "end_line": 5,
                "parent_id": None,
                "visibility_hint": "module",
                "doc_summary": None,
                "content_hash": "sha256:i",
                "semantic_hash": "sha256:j",
                "source": "python-ast",
                "confidence": 1.0,
                "payload_json": "{}",
                "scope": "module",
                "lexical_parent_id": None,
                "last_indexed_at": "2026-01-01T00:00:00Z",
            },
            {
                "id": "sym:sub",
                "repo_id": repo_id,
                "file_id": f2.id,
                "language": "python",
                "kind": "class",
                "name": "SubClass",
                "qualified_name": "mod2.SubClass",
                "uri": f2.uri,
                "start_line": 2,
                "end_line": 5,
                "parent_id": "sym:mod2",
                "visibility_hint": "public",
                "doc_summary": None,
                "content_hash": "sha256:k",
                "semantic_hash": "sha256:l",
                "source": "python-ast",
                "confidence": 1.0,
                "payload_json": "{}",
                "scope": "module",
                "lexical_parent_id": None,
                "last_indexed_at": "2026-01-01T00:00:00Z",
            },
        ]
        upsert_nodes(conn, nodes)

        edges = [
            {
                "id": "edge:contains",
                "repo_id": repo_id,
                "kind": EDGE_KIND_CONTAINS,
                "from_id": "sym:mod1",
                "to_id": "sym:cls1",
                "source": "python-ast",
                "confidence": 1.0,
                "evidence_file_id": f1.id,
                "evidence_uri": f1.uri,
                "payload_json": "{}",
            },
            {
                "id": "edge:scope",
                "repo_id": repo_id,
                "kind": EDGE_KIND_SCOPE_PARENT,
                "from_id": "sym:inner",
                "to_id": "sym:func_pct",
                "source": "python-ast",
                "confidence": 1.0,
                "evidence_file_id": f1.id,
                "evidence_uri": f1.uri,
                "payload_json": "{}",
            },
            {
                "id": "edge:imp",
                "repo_id": repo_id,
                "kind": EDGE_KIND_IMPORTS,
                "from_id": "sym:mod2",
                "to_id": "sym:cls1",
                "source": "python-ast",
                "confidence": 0.9,
                "evidence_file_id": f2.id,
                "evidence_uri": f2.uri,
                "payload_json": {"import_type": "from", "module": "mod1", "symbol": "Class1"},
            },
            {
                "id": "edge:inh",
                "repo_id": repo_id,
                "kind": EDGE_KIND_INHERITS,
                "from_id": "sym:sub",
                "to_id": "sym:cls1",
                "source": "python-ast",
                "confidence": 0.9,
                "evidence_file_id": f2.id,
                "evidence_uri": f2.uri,
                "payload_json": {"base_name": "Class1"},
            },
            {
                "id": "edge:call",
                "repo_id": repo_id,
                "kind": EDGE_KIND_CALLS,
                "from_id": "sym:sub",
                "to_id": "sym:func_pct",
                "source": "python-ast",
                "confidence": 0.9,
                "evidence_file_id": f2.id,
                "evidence_uri": f2.uri,
                "payload_json": {"target": "compute_100%_rate"},
            },
        ]
        upsert_edges(conn, edges)
        conn.commit()
        yield conn, repo_root
    finally:
        close_connection(conn)


def test_symbol_and_hierarchy_lookups(query_db: tuple[Connection, Path]) -> None:
    """Symbol lookups, batch ID fetches, and parent/child navigation work cleanly."""
    conn, _ = query_db
    repo_id = "repo:test:12345678"

    cls1 = get_symbol(conn, "sym:cls1")
    assert cls1 is not None
    assert get_symbol_by_qualified_name(conn, repo_id, "mod1.Class1", "class")["id"] == "sym:cls1"
    assert get_parent_symbol(conn, cls1)["id"] == "sym:mod1"

    inner = get_symbol(conn, "sym:inner")
    assert get_lexical_parent_symbol(conn, inner)["id"] == "sym:func_pct"
    assert [c["id"] for c in get_lexical_child_symbols(conn, "sym:func_pct")] == ["sym:inner"]
    assert {c["id"] for c in get_child_symbols(conn, "sym:mod1")} == {"sym:cls1", "sym:func_pct"}

    batched_syms = get_symbols_by_ids(conn, ["sym:cls1", "sym:sub", "sym:missing"])
    assert set(batched_syms.keys()) == {"sym:cls1", "sym:sub"}

    batched_files = get_files_by_ids(conn, [f"file:{repo_id}:mod1.py", f"file:{repo_id}:mod2.py"])
    assert len(batched_files) == 2


def test_directed_edge_queries(query_db: tuple[Connection, Path]) -> None:
    """Directed edge queries return incoming/outgoing calls, imports, and inherits."""
    conn, _ = query_db

    assert len(get_outgoing_edges(conn, "sym:mod1")) == 1
    assert len(get_incoming_edges(conn, "sym:func_pct")) == 2  # scope_parent + calls

    assert len(get_incoming_calls(conn, "sym:func_pct")) == 1
    assert len(get_outgoing_calls(conn, "sym:sub")) == 1
    assert len(get_incoming_imports(conn, "sym:cls1")) == 1
    assert len(get_outgoing_imports(conn, "sym:mod2")) == 1
    assert len(get_incoming_inherits(conn, "sym:cls1")) == 1
    assert len(get_outgoing_inherits(conn, "sym:sub")) == 1


def test_fts5_search_and_escaped_like(query_db: tuple[Connection, Path]) -> None:
    """search_symbols_fts handles exact/prefix ranking, filters, and special tokens safely."""
    conn, _ = query_db
    repo_id = "repo:test:12345678"

    exact_hits = search_symbols_fts(conn, repo_id, "Class1")
    assert exact_hits and exact_hits[0]["id"] == "sym:cls1"

    prefix_hits = search_symbols_fts(conn, repo_id, "Sub")
    assert [h["id"] for h in prefix_hits] == ["sym:sub"]

    filtered = search_symbols_fts(conn, repo_id, "Class", kind="class", file_path="mod2.py")
    assert [h["id"] for h in filtered] == ["sym:sub"]

    # Escaped LIKE: '%' and '_' must NOT act as SQLite wildcards
    pct_hits = find_symbols_by_name(conn, repo_id, "100%")
    assert {h["id"] for h in pct_hits} == {"sym:func_pct", "sym:inner"}
    assert [h["id"] for h in find_symbols_by_name(conn, repo_id, "%_rate", kind="function")] == [
        "sym:func_pct"
    ]
    assert find_symbols_by_name(conn, repo_id, "100_rate") == []

    fts_special = search_symbols_fts(conn, repo_id, "100%", kind="function")
    assert [h["id"] for h in fts_special] == ["sym:func_pct"]

    # FTS operator / punctuation edge cases never raise sqlite3.OperationalError
    assert search_symbols_fts(conn, repo_id, "NOT OR * \"bad\"") == []


def test_outline_helpers_and_disk_freshness(query_db: tuple[Connection, Path]) -> None:
    """Outline helpers and check_file_disk_freshness reflect live disk edits."""
    conn, repo_root = query_db
    repo_id = "repo:test:12345678"

    f1 = get_file_for_outline(conn, repo_id, "./mod1.py")
    assert f1 is not None
    assert len(list_file_outline_symbols(conn, f1["id"])) == 4

    f2 = get_file_for_outline(conn, repo_id, "mod2.py")
    assert f2 is not None
    assert len(list_file_import_edges(conn, f2["id"])) == 1

    assert check_file_disk_freshness(conn, f1["id"]) == (True, None)

    (repo_root / "mod1.py").write_text("class Class1:\n    x = 1\n", encoding="utf-8")
    assert check_file_disk_freshness(conn, f1["id"]) == (False, "content_hash_mismatch")

    (repo_root / "mod1.py").unlink()
    assert check_file_disk_freshness(conn, f1["id"]) == (False, "file_missing")
