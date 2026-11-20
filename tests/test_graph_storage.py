"""Graph storage tests for SQLite UnitOfWork repositories, FTS5, batch pruning, and savepoints."""

import json
from pathlib import Path

import pytest

from core.models import (
    DatabaseError,
    DirectedEdgeQuery,
    Edge,
    EdgeKind,
    EdgeTargetUpdate,
    FilePageQuery,
    FileRecord,
    IndexRunMetrics,
    Position,
    Range,
    ReferenceRefreshState,
    RefreshStatus,
    RepoRecord,
    SymbolKind,
    SymbolNode,
    SymbolPageQuery,
    SymbolScope,
    SymbolSearchQuery,
)
from storage import SqlUnitOfWorkFactory


@pytest.fixture
def uow_factory(tmp_path: Path) -> SqlUnitOfWorkFactory:
    """Create and seed an initialized v2 SQLite DB with a default repo and file row."""
    factory = SqlUnitOfWorkFactory(tmp_path / "storage.db")
    repo = RepoRecord(
        id="project:test:12345678",
        root_path=str(tmp_path),
        name="test",
        created_at="2026-01-01T00:00:00Z",
        last_indexed_at="2026-01-01T00:00:00Z",
    )
    file_rec = FileRecord(
        id="file:project:test:12345678:test.py",
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
    with factory.open(initialize=True) as uow:
        uow.repos.upsert(repo)
        uow.files.upsert_many([file_rec])
    return factory


def _node(
    node_id: str,
    kind: SymbolKind,
    qualified_name: str,
    *,
    file_id: str = "file:project:test:12345678:test.py",
    parent_id: str | None = None,
    lexical_parent_id: str | None = None,
    scope: SymbolScope = SymbolScope.MODULE,
    start_line: int | None = 1,
    end_line: int | None = 5,
    signature: str | None = None,
    visibility_hint: str = "public",
    doc_summary: str = "Doc summary",
) -> SymbolNode:
    """Construct a valid test SymbolNode with structured Range and payload."""
    return SymbolNode(
        id=node_id,
        repo_id="project:test:12345678",
        file_id=file_id,
        kind=kind,
        name=qualified_name.split(".")[-1],
        qualified_name=qualified_name,
        uri="file:///tmp/test.py",
        decl_range=Range(start=Position(line=0, character=0), end=Position(line=4, character=0)),
        selection_range=Range(
            start=Position(line=0, character=0),
            end=Position(line=0, character=4),
        ),
        start_line=start_line,
        end_line=end_line,
        signature=signature,
        parent_id=parent_id,
        visibility_hint=visibility_hint,
        doc_summary=doc_summary,
        content_hash="sha256:abc123",
        semantic_hash="sha256:def456",
        source="python-ast",
        confidence=1.0,
        payload={"test": "data"},
        scope=scope,
        lexical_parent_id=lexical_parent_id,
        last_indexed_at="2026-01-01T00:00:00Z",
    )


def _edge(
    edge_id: str,
    kind: EdgeKind,
    from_id: str,
    to_id: str,
    *,
    file_id: str = "file:project:test:12345678:test.py",
    confidence: float = 1.0,
) -> Edge:
    """Construct a valid test Edge with structured evidence_range and payload."""
    return Edge(
        id=edge_id,
        repo_id="project:test:12345678",
        kind=kind,
        from_id=from_id,
        to_id=to_id,
        source="python-ast",
        confidence=confidence,
        evidence_file_id=file_id,
        evidence_uri="file:///tmp/test.py",
        evidence_range=Range(
            start=Position(line=0, character=0),
            end=Position(line=1, character=0),
        ),
        payload={"test": "edge_data"},
        last_indexed_at="2026-01-01T00:00:00Z",
    )


def test_node_upsert_read_summaries_fts_and_delete(uow_factory: SqlUnitOfWorkFactory) -> None:
    """Node CRUD, summaries, FTS5 + LIKE fallback search, and file deletion work end-to-end."""
    mod = _node("node:mod", SymbolKind.MODULE, "test", start_line=1, end_line=20)
    cls = _node(
        "node:cls",
        SymbolKind.CLASS,
        "test.OrderService",
        parent_id="node:mod",
        start_line=2,
        end_line=10,
        signature="class OrderService",
        doc_summary="Processes customer checkout orders.",
    )
    fn = _node(
        "node:fn",
        SymbolKind.FUNCTION,
        "test._internal_helper",
        parent_id="node:mod",
        start_line=12,
        end_line=20,
        signature="def _internal_helper()",
        visibility_hint="private_like",
    )
    local = _node(
        "node:local",
        SymbolKind.LOCAL_FUNCTION,
        "test._internal_helper.inner",
        parent_id="node:fn",
        lexical_parent_id="node:fn",
        scope=SymbolScope.FUNCTION,
        start_line=14,
        end_line=16,
    )

    with uow_factory.open() as uow:
        uow.nodes.upsert(mod)
        uow.nodes.upsert_many([cls, fn, local])

        fetched = uow.nodes.get_by_id("node:cls")
        assert fetched is not None
        assert fetched.signature == "class OrderService"
        assert fetched.start_line == 2
        assert fetched.end_line == 10
        assert fetched.payload == {"test": "data"}
        assert (
            uow.nodes.get_by_qualified_name("project:test:12345678", "test.OrderService")
            is not None
        )

        summaries = uow.nodes.get_summaries_by_ids(["node:cls", "node:fn"])
        assert summaries["node:cls"].file_path == "test.py"
        assert summaries["node:cls"].module_path == "test"

        assert [n.id for n in uow.nodes.list_children("node:mod")] == ["node:cls", "node:fn"]
        assert [n.id for n in uow.nodes.list_lexical_children("node:fn")] == ["node:local"]
        public_members = uow.nodes.list_member_summaries("node:mod", include_private=False)
        assert [m.id for m in public_members] == ["node:cls"]
        assert len(uow.nodes.list_for_file("file:project:test:12345678:test.py")) == 4
        assert len(uow.nodes.list_for_repo("project:test:12345678")) == 4

        page = uow.nodes.list_paginated_summaries(
            SymbolPageQuery(repo_id="project:test:12345678", limit=2, offset=0)
        )
        assert page.total_count == 4
        assert len(page.items) == 2

        # FTS5 exact/prefix + doc_summary token search + LIKE substring fallback
        fts_hits = uow.nodes.search_fts(
            SymbolSearchQuery(repo_id="project:test:12345678", query="checkout")
        )
        assert [h.id for h in fts_hits] == ["node:cls"]

        like_hits = uow.nodes.search_fts(
            SymbolSearchQuery(repo_id="project:test:12345678", query="rderServ")
        )
        assert [h.id for h in like_hits] == ["node:cls"]

        # Update triggers nodes_fts_au
        updated_cls = cls.model_copy(update={"doc_summary": "Handles billing invoices."})
        uow.nodes.upsert(updated_cls)
        assert (
            uow.nodes.search_fts(
                SymbolSearchQuery(repo_id="project:test:12345678", query="checkout")
            )
            == []
        )
        assert [
            h.id
            for h in uow.nodes.search_fts(
                SymbolSearchQuery(repo_id="project:test:12345678", query="invoices")
            )
        ] == ["node:cls"]

        uow.files.delete_file_graph("file:project:test:12345678:test.py")
        assert uow.nodes.list_for_file("file:project:test:12345678:test.py") == []
        assert (
            uow.nodes.search_fts(
                SymbolSearchQuery(repo_id="project:test:12345678", query="invoices")
            )
            == []
        )


def test_edge_upsert_single_json_encoding_updates_and_file_delete(
    uow_factory: SqlUnitOfWorkFactory,
) -> None:
    """Edges store single-encoded JSON, support target updates, and clean up on delete."""
    other_file = FileRecord(
        id="file:project:test:12345678:other.py",
        repo_id="project:test:12345678",
        file_path="other.py",
        uri="file:///tmp/other.py",
        module_path="other",
        content_hash="sha256:other",
        size_bytes=20,
        line_count=2,
        last_modified_at="2026-01-01T00:00:00Z",
    )
    with uow_factory.open() as uow:
        uow.files.upsert_many([other_file])
        uow.nodes.upsert_many([
            _node("node:a", SymbolKind.FUNCTION, "test.a"),
            _node("node:b", SymbolKind.FUNCTION, "other.b", file_id=other_file.id),
        ])

        e1 = _edge("edge:1", EdgeKind.CALLS, "node:a", "unresolved:other.b")
        e2 = _edge("edge:2", EdgeKind.IMPORTS, "node:b", "node:a", file_id=other_file.id)
        uow.edges.upsert(e1)
        uow.edges.upsert_many([e2])

        updated = uow.edges.update_resolved_targets([
            EdgeTargetUpdate(edge_id="edge:1", to_id="node:b", confidence=0.95)
        ])
        assert updated == 1
        resolved_e1 = uow.edges.get_by_id("edge:1")
        assert resolved_e1 is not None
        assert resolved_e1.to_id == "node:b"
        assert resolved_e1.confidence == 0.95

        assert len(uow.edges.list_for_repo("project:test:12345678")) == 2
        assert len(uow.edges.list_outgoing("node:a", EdgeKind.CALLS)) == 1
        assert len(uow.edges.list_incoming("node:b", EdgeKind.CALLS)) == 1
        assert len(uow.edges.list_for_target_and_kinds("node:a", [EdgeKind.IMPORTS])) == 1
        assert len(uow.edges.list_for_file("file:project:test:12345678:test.py")) == 1
        assert (
            uow.edges.count_directed(
                DirectedEdgeQuery(symbol_id="node:a", direction="outgoing", kinds=(EdgeKind.CALLS,))
            )
            == 1
        )
        assert len(uow.edges.list_neighborhood_edges(["node:a"])) == 2

    raw_res = uow_factory.execute_readonly_sql(
        "SELECT payload_json, evidence_range_json FROM edges WHERE id = 'edge:1'"
    )
    raw_row = raw_res.rows[0]
    assert isinstance(json.loads(raw_row["payload_json"]), dict)
    assert isinstance(json.loads(raw_row["evidence_range_json"]), dict)

    with uow_factory.open() as uow:
        # Deleting all edges for test.py removes e1 (evidence/from_id) AND e2 (to_id == sym:a)
        uow.edges.delete_all_for_file("file:project:test:12345678:test.py")
        assert uow.edges.list_for_repo("project:test:12345678") == []


def test_replace_file_graph_is_savepoint_safe_without_commit(
    uow_factory: SqlUnitOfWorkFactory,
) -> None:
    """replace_file_graph isolates failures in a SAVEPOINT and preserves outer transaction work."""
    file_id = "file:project:test:12345678:test.py"
    with uow_factory.open() as uow:
        file_rec = uow.files.get_by_id(file_id)
        assert file_rec is not None
        uow.replace_file_graph(
            file_rec,
            [_node("node:initial", SymbolKind.MODULE, "test", file_id=file_id)],
            [],
        )

    other_file = FileRecord(
        id="file:project:test:12345678:keep.py",
        repo_id="project:test:12345678",
        file_path="keep.py",
        uri="file:///tmp/keep.py",
        module_path="keep",
        content_hash="sha256:keep",
        size_bytes=10,
        line_count=1,
        last_modified_at="2026-01-01T00:00:00Z",
    )
    with uow_factory.open() as uow:
        uow.files.upsert_many([other_file])
        file_rec = uow.files.get_by_id(file_id)
        assert file_rec is not None
        bad_fk_node = _node(
            "node:broken",
            SymbolKind.FUNCTION,
            "test.broken",
            file_id="file:project:test:12345678:missing_fk.py",
        )
        with pytest.raises(DatabaseError):
            uow.replace_file_graph(file_rec, [bad_fk_node], [])

        # Outer transaction work (keep.py) and prior file graph (sym:initial) must both survive
        assert uow.files.get_by_path("project:test:12345678", "keep.py") is not None
        assert [n.id for n in uow.nodes.list_for_file(file_id)] == ["node:initial"]


def test_prune_stale_files_batched_over_500_files(uow_factory: SqlUnitOfWorkFactory) -> None:
    """prune_not_in_ids deletes >500 stale files cleanly in bounded chunks."""
    repo_id = "project:test:12345678"
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
    with uow_factory.open() as uow:
        uow.files.upsert_many(records)
        assert len(uow.files.list_for_repo(repo_id, include_tests=False)) == 326
        assert len(uow.files.list_for_repo(repo_id, include_tests=True)) == 651

        page = uow.files.list_page(FilePageQuery(repo_id=repo_id, include_tests=True, limit=10))
        assert page.total_count == 651
        assert len(page.items) == 10

        keep = {"file:project:test:12345678:test.py", f"file:{repo_id}:mod_0.py"}
        pruned = uow.files.prune_not_in_ids(repo_id, keep)
        assert pruned == 649
        remaining = uow.files.list_for_repo(repo_id, include_tests=True)
        assert {r.id for r in remaining} == keep


def test_repos_runs_refresh_and_diagnostics(
    uow_factory: SqlUnitOfWorkFactory,
    tmp_path: Path,
) -> None:
    """repos, runs, refresh, and diagnostics repositories work end-to-end."""
    repo_id = "project:test:12345678"
    with uow_factory.open() as uow:
        assert len(uow.repos.list_all()) == 1
        assert uow.repos.resolve_id() == repo_id
        assert uow.repos.resolve_id("test") == repo_id
        assert uow.repos.resolve_id(str(tmp_path)) == repo_id

        repo2 = RepoRecord(
            id="project:test:87654321",
            root_path=str(tmp_path / "other"),
            name="test",
            created_at="2026-01-02T00:00:00Z",
        )
        uow.repos.upsert(repo2)
        with pytest.raises(DatabaseError) as exc_info:
            uow.repos.resolve_id("test")
        assert exc_info.value.error_code == "ambiguous_repo"

        with pytest.raises(DatabaseError) as missing_exc:
            uow.repos.resolve_id("nonexistent")
        assert missing_exc.value.error_code == "repo_not_found"

        run_id = uow.runs.start_run(repo_id, run_type="incremental")
        uow.runs.finish_run(
            run_id,
            "completed",
            IndexRunMetrics(files_scanned=1, nodes_extracted=4, edges_extracted=1),
        )
        runs = uow.runs.list_recent(repo_id, limit=5)
        assert len(runs) == 1
        assert runs[0].status == "completed"
        assert runs[0].nodes_extracted == 4

        uow.nodes.upsert_many([
            _node("node:m", SymbolKind.MODULE, "test"),
            _node("node:c", SymbolKind.CLASS, "test.C"),
            _node("node:f", SymbolKind.FUNCTION, "test.f"),
            _node("node:lf", SymbolKind.LOCAL_FUNCTION, "test.f.g", scope=SymbolScope.FUNCTION),
        ])
        uow.refresh.upsert(
            ReferenceRefreshState(
                target_symbol_id="node:f",
                available=True,
                last_refreshed_at="2026-01-01T00:00:00Z",
                refresh_status=RefreshStatus.OK,
            )
        )
        assert uow.refresh.get("node:f").available is True
        assert uow.refresh.mark_symbols_stale(["node:f"]) == ["node:f"]
        assert uow.refresh.get("node:f").available is False

        stats = uow.diagnostics.get_repo_graph_stats(repo_id)
        assert stats.file_count == 1
        assert stats.total_lines == 10
        assert stats.node_count == 4
        assert stats.callable_count == 2
        assert stats.local_callable_count == 1

        status_report = uow.diagnostics.get_status_report(uow_factory.db_path, repo_id)
        assert status_report.schema_ok is True
        assert status_report.repo_count == 2
        assert len(uow.diagnostics.list_repo_summaries()) == 2
        assert uow.diagnostics.get_table_list_report(uow_factory.db_path).user_version == 4
