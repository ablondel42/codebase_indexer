"""Graph storage tests for v0.2.0 SQLite persistence, batch pruning, and transactions."""

from collections.abc import Iterator
import json
from pathlib import Path
import sqlite3

import pytest

from repo_context.constants import EDGE_KIND_CALLS, EDGE_KIND_SCOPE_PARENT
from repo_context.graph import get_repo_graph_stats
from repo_context.models.file import FileRecord
from repo_context.models.repo import RepoRecord
from repo_context.storage import (
    close_connection,
    delete_edges_for_file,
    delete_nodes_for_file,
    get_connection,
    get_edge_by_id,
    get_file_by_path,
    get_node_by_id,
    get_node_by_qualified_name,
    initialize_database,
    list_child_nodes,
    list_edges_for_file,
    list_edges_for_repo,
    list_edges_for_target_and_kinds,
    list_files_for_repo,
    list_incoming_edges,
    list_index_runs,
    list_lexical_children,
    list_nodes_for_file,
    list_nodes_for_repo,
    list_outgoing_edges,
    list_repos,
    prune_stale_files,
    record_index_run_finish,
    record_index_run_start,
    replace_file_graph,
    resolve_repo_id,
    upsert_edge,
    upsert_edges,
    upsert_files,
    upsert_node,
    upsert_nodes,
    upsert_repo,
)
from repo_context.exceptions import DatabaseError


@pytest.fixture
def initialized_db(tmp_path: Path) -> Iterator[sqlite3.Connection]:
    """Yield an initialized v2 SQLite DB with a default repo and file row."""
    conn = get_connection(tmp_path / "storage.db")
    try:
        initialize_database(conn)
        repo = RepoRecord(
            id="repo:test:12345678",
            root_path=str(tmp_path),
            name="test",
            created_at="2026-01-01T00:00:00Z",
            last_indexed_at="2026-01-01T00:00:00Z",
        )
        file_rec = FileRecord(
            id="file:repo:test:12345678:test.py",
            repo_id=repo.id,
            file_path="test.py",
            uri="file:///tmp/test.py",
            module_path="test",
            content_hash="sha256:file",
            size_bytes=100,
            line_count=10,
            is_test=False,
            last_modified_at="2026-01-01T00:00:00Z",
            last_indexed_at="2026-01-01T00:00:00Z",
        )
        upsert_repo(conn, repo)
        upsert_files(conn, [file_rec])
        conn.commit()
        yield conn
    finally:
        close_connection(conn)


def _node(
    node_id: str,
    kind: str,
    qualified_name: str,
    *,
    file_id: str = "file:repo:test:12345678:test.py",
    parent_id: str | None = None,
    lexical_parent_id: str | None = None,
    scope: str = "module",
    start_line: int | None = 1,
    end_line: int | None = 5,
    signature: str | None = None,
) -> dict:
    return {
        "id": node_id,
        "repo_id": "repo:test:12345678",
        "file_id": file_id,
        "language": "python",
        "kind": kind,
        "name": qualified_name.split(".")[-1],
        "qualified_name": qualified_name,
        "uri": "file:///tmp/test.py",
        "range_json": {"start": {"line": 0, "character": 0}, "end": {"line": 4, "character": 0}},
        "selection_range_json": {"start": {"line": 0, "character": 0}, "end": {"line": 0, "character": 4}},
        "start_line": start_line,
        "end_line": end_line,
        "signature": signature,
        "parent_id": parent_id,
        "visibility_hint": "public",
        "doc_summary": "Doc summary",
        "content_hash": "sha256:abc123",
        "semantic_hash": "sha256:def456",
        "source": "python-ast",
        "confidence": 1.0,
        "payload_json": {"test": "data"},
        "scope": scope,
        "lexical_parent_id": lexical_parent_id,
        "last_indexed_at": "2026-01-01T00:00:00Z",
    }


def _edge(
    edge_id: str,
    kind: str,
    from_id: str,
    to_id: str,
    *,
    file_id: str = "file:repo:test:12345678:test.py",
) -> dict:
    return {
        "id": edge_id,
        "repo_id": "repo:test:12345678",
        "kind": kind,
        "from_id": from_id,
        "to_id": to_id,
        "source": "python-ast",
        "confidence": 1.0,
        "evidence_file_id": file_id,
        "evidence_uri": "file:///tmp/test.py",
        "evidence_range_json": {"start": {"line": 0, "character": 0}, "end": {"line": 1, "character": 0}},
        "payload_json": {"test": "edge_data"},
        "last_indexed_at": "2026-01-01T00:00:00Z",
    }


def test_node_upsert_read_and_delete(initialized_db: sqlite3.Connection) -> None:
    """Node CRUD, structural/lexical children, and file node deletion work with v2 fields."""
    conn = initialized_db
    mod = _node("sym:mod", "module", "test", start_line=1, end_line=20)
    cls = _node("sym:cls", "class", "test.Svc", parent_id="sym:mod", start_line=2, end_line=10, signature="class Svc")
    fn = _node("sym:fn", "function", "test.outer", parent_id="sym:mod", start_line=12, end_line=20, signature="def outer()")
    local = _node(
        "sym:local",
        "local_function",
        "test.outer.inner",
        parent_id="sym:fn",
        lexical_parent_id="sym:fn",
        scope="function",
        start_line=14,
        end_line=16,
    )
    upsert_node(conn, mod)
    upsert_nodes(conn, [cls, fn, local])

    fetched = get_node_by_id(conn, "sym:cls")
    assert fetched is not None
    assert fetched["signature"] == "class Svc"
    assert fetched["start_line"] == 2
    assert fetched["end_line"] == 10
    assert get_node_by_qualified_name(conn, "repo:test:12345678", "test.Svc") is not None

    assert [n["id"] for n in list_child_nodes(conn, "sym:mod")] == ["sym:cls", "sym:fn"]
    assert [n["id"] for n in list_lexical_children(conn, "sym:fn")] == ["sym:local"]
    assert len(list_nodes_for_file(conn, "file:repo:test:12345678:test.py")) == 4
    assert len(list_nodes_for_repo(conn, "repo:test:12345678")) == 4

    delete_nodes_for_file(conn, "file:repo:test:12345678:test.py")
    assert list_nodes_for_file(conn, "file:repo:test:12345678:test.py") == []


def test_edge_upsert_single_json_encoding_and_full_file_delete(
    initialized_db: sqlite3.Connection,
) -> None:
    """Edges store single-encoded JSON and delete_edges_for_file clears from/to/evidence matches."""
    conn = initialized_db
    other_file = FileRecord(
        id="file:repo:test:12345678:other.py",
        repo_id="repo:test:12345678",
        file_path="other.py",
        uri="file:///tmp/other.py",
        module_path="other",
        content_hash="sha256:other",
        size_bytes=20,
        line_count=2,
        last_modified_at="2026-01-01T00:00:00Z",
    )
    upsert_files(conn, [other_file])
    upsert_nodes(
        conn,
        [
            _node("sym:a", "function", "test.a", file_id="file:repo:test:12345678:test.py"),
            _node("sym:b", "function", "other.b", file_id=other_file.id),
        ],
    )

    e1 = _edge("edge:1", EDGE_KIND_CALLS, "sym:a", "sym:b", file_id="file:repo:test:12345678:test.py")
    e2 = _edge("edge:2", EDGE_KIND_SCOPE_PARENT, "sym:b", "sym:a", file_id=other_file.id)
    upsert_edge(conn, e1)
    upsert_edges(conn, [e2])

    raw_row = conn.execute(
        "SELECT payload_json, evidence_range_json FROM edges WHERE id = 'edge:1'"
    ).fetchone()
    assert isinstance(json.loads(raw_row["payload_json"]), dict)
    assert isinstance(json.loads(raw_row["evidence_range_json"]), dict)

    assert get_edge_by_id(conn, "edge:1") is not None
    assert len(list_edges_for_repo(conn, "repo:test:12345678")) == 2
    assert len(list_outgoing_edges(conn, "sym:a", EDGE_KIND_CALLS)) == 1
    assert len(list_incoming_edges(conn, "sym:b", EDGE_KIND_CALLS)) == 1
    assert len(list_edges_for_target_and_kinds(conn, "sym:a", [EDGE_KIND_SCOPE_PARENT])) == 1
    assert len(list_edges_for_file(conn, "file:repo:test:12345678:test.py")) == 1

    # Deleting edges for test.py removes e1 (evidence/from_id) AND e2 (to_id == sym:a in test.py)
    delete_edges_for_file(conn, "file:repo:test:12345678:test.py")
    assert list_edges_for_repo(conn, "repo:test:12345678") == []


def test_replace_file_graph_is_savepoint_safe_without_commit(
    initialized_db: sqlite3.Connection,
) -> None:
    """replace_file_graph does not auto-commit and preserves outer transaction state on failure."""
    conn = initialized_db
    file_id = "file:repo:test:12345678:test.py"
    initial = [_node("sym:initial", "module", "test", file_id=file_id)]
    replace_file_graph(conn, file_id, initial, [])
    conn.commit()

    # Start outer transaction with a second file insert + savepoint-guarded replace failure
    other_file = FileRecord(
        id="file:repo:test:12345678:keep.py",
        repo_id="repo:test:12345678",
        file_path="keep.py",
        uri="file:///tmp/keep.py",
        module_path="keep",
        content_hash="sha256:keep",
        size_bytes=10,
        line_count=1,
        last_modified_at="2026-01-01T00:00:00Z",
    )
    conn.execute("BEGIN")
    upsert_files(conn, [other_file])

    with pytest.raises(Exception):
        replace_file_graph(conn, file_id, [{"id": "invalid_node"}], [])

    # Outer transaction work (keep.py) and prior file graph (sym:initial) must both survive
    assert get_file_by_path(conn, "repo:test:12345678", "keep.py") is not None
    assert len(list_nodes_for_file(conn, file_id)) == 1
    conn.commit()


def test_prune_stale_files_batched_over_500_files(initialized_db: sqlite3.Connection) -> None:
    """prune_stale_files deletes >500 stale files cleanly in batches."""
    conn = initialized_db
    repo_id = "repo:test:12345678"
    records = [
        FileRecord(
            id=f"file:{repo_id}:mod_{i}.py",
            repo_id=repo_id,
            file_path=f"mod_{i}.py",
            uri=f"file:///tmp/mod_{i}.py",
            module_path=f"mod_{i}",
            content_hash=f"sha256:{i}",
            size_bytes=10,
            line_count=1,
            is_test=(i % 2 == 0),
            last_modified_at="2026-01-01T00:00:00Z",
        )
        for i in range(650)
    ]
    upsert_files(conn, records)
    conn.commit()

    assert len(list_files_for_repo(conn, repo_id, include_tests=False)) == 326  # 1 initial + 325 odd
    assert len(list_files_for_repo(conn, repo_id, include_tests=True)) == 651

    keep = {"file:repo:test:12345678:test.py", f"file:{repo_id}:mod_0.py"}
    pruned = prune_stale_files(conn, repo_id, keep)
    conn.commit()

    assert pruned == 649
    remaining = list_files_for_repo(conn, repo_id, include_tests=True)
    assert {r.id for r in remaining} == keep


def test_repos_runs_and_graph_stats(initialized_db: sqlite3.Connection, tmp_path: Path) -> None:
    """list_repos, resolve_repo_id, index_runs, and get_repo_graph_stats work end-to-end."""
    conn = initialized_db
    repo_id = "repo:test:12345678"

    assert len(list_repos(conn)) == 1
    assert resolve_repo_id(conn) == repo_id
    assert resolve_repo_id(conn, "test") == repo_id
    assert resolve_repo_id(conn, str(tmp_path)) == repo_id

    # Adding a second repo with same name makes bare name ambiguous
    repo2 = RepoRecord(
        id="repo:test:87654321",
        root_path=str(tmp_path / "other"),
        name="test",
        created_at="2026-01-02T00:00:00Z",
    )
    upsert_repo(conn, repo2)
    with pytest.raises(DatabaseError) as exc_info:
        resolve_repo_id(conn, "test")
    assert exc_info.value.error_code == "ambiguous_repo"

    with pytest.raises(DatabaseError) as missing_exc:
        resolve_repo_id(conn, "nonexistent")
    assert missing_exc.value.error_code == "repo_not_found"

    run_id = record_index_run_start(conn, repo_id, run_type="incremental")
    record_index_run_finish(
        conn, run_id, "completed", files_scanned=1, nodes_extracted=2, edges_extracted=1
    )
    runs = list_index_runs(conn, repo_id, limit=5)
    assert len(runs) == 1
    assert runs[0]["status"] == "completed"
    assert runs[0]["nodes_extracted"] == 2

    upsert_nodes(
        conn,
        [
            _node("sym:m", "module", "test"),
            _node("sym:c", "class", "test.C"),
            _node("sym:f", "function", "test.f"),
            _node("sym:lf", "local_function", "test.f.g", scope="function"),
        ],
    )
    stats = get_repo_graph_stats(conn, repo_id)
    assert stats["file_count"] == 1
    assert stats["total_lines"] == 10
    assert stats["node_count"] == 4
    assert stats["callable_count"] == 2
    assert stats["local_callable_count"] == 1
