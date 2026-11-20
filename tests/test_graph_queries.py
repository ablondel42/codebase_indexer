"""Graph query layer tests covering GraphNavigator, ReferenceAnalyzer, FTS5, and freshness."""

from pathlib import Path

import pytest

from core.models import (
    Edge,
    EdgeKind,
    FileRecord,
    FreshnessStatus,
    Position,
    Range,
    RepoRecord,
    SymbolKind,
    SymbolNode,
    SymbolNotFoundError,
    SymbolScope,
    SymbolSearchQuery,
)
from core.utils.hashing import sha256_file
from graph import (
    GraphNavigator,
    ReferenceAnalyzer,
    ReferenceDirection,
    ReferenceQueryOptions,
    SubgraphOptions,
)
from storage import SqlUnitOfWorkFactory


@pytest.fixture
def query_env(tmp_path: Path) -> tuple[SqlUnitOfWorkFactory, Path]:
    """Create an initialized DB populated with real files on disk and rich graph edges."""
    repo_root = tmp_path / "repo"
    repo_root.mkdir()
    mod1_path = repo_root / "mod1.py"
    mod1_path.write_text("class Class1:\n    pass\n", encoding="utf-8")
    mod2_path = repo_root / "mod2.py"
    mod2_path.write_text("from mod1 import Class1\n", encoding="utf-8")

    factory = SqlUnitOfWorkFactory(tmp_path / "query.db")
    repo_id = "project:test:12345678"
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
    nodes = [
        SymbolNode(
            id="node:mod1",
            repo_id=repo_id,
            file_id=f1.id,
            kind=SymbolKind.MODULE,
            name="mod1",
            qualified_name="mod1",
            uri=f1.uri,
            start_line=1,
            end_line=20,
            visibility_hint="module",
            doc_summary="Module one",
            content_hash="sha256:a",
            semantic_hash="sha256:b",
            scope=SymbolScope.MODULE,
            last_indexed_at="2026-01-01T00:00:00Z",
        ),
        SymbolNode(
            id="node:cls1",
            repo_id=repo_id,
            file_id=f1.id,
            kind=SymbolKind.CLASS,
            name="Class1",
            qualified_name="mod1.Class1",
            uri=f1.uri,
            start_line=1,
            end_line=2,
            signature="class Class1",
            parent_id="node:mod1",
            visibility_hint="public",
            doc_summary="Base class",
            content_hash="sha256:c",
            semantic_hash="sha256:d",
            scope=SymbolScope.MODULE,
            last_indexed_at="2026-01-01T00:00:00Z",
        ),
        SymbolNode(
            id="node:func_pct",
            repo_id=repo_id,
            file_id=f1.id,
            kind=SymbolKind.FUNCTION,
            name="compute_100%_rate",
            qualified_name="mod1.compute_100%_rate",
            uri=f1.uri,
            start_line=5,
            end_line=10,
            signature="def compute_100%_rate()",
            parent_id="node:mod1",
            visibility_hint="public",
            doc_summary="Exact percent handler",
            content_hash="sha256:e",
            semantic_hash="sha256:f",
            scope=SymbolScope.MODULE,
            last_indexed_at="2026-01-01T00:00:00Z",
        ),
        SymbolNode(
            id="node:inner",
            repo_id=repo_id,
            file_id=f1.id,
            kind=SymbolKind.LOCAL_FUNCTION,
            name="inner",
            qualified_name="mod1.compute_100%_rate.inner",
            uri=f1.uri,
            start_line=6,
            end_line=8,
            parent_id="node:func_pct",
            visibility_hint="local",
            doc_summary="Inner helper",
            content_hash="sha256:g",
            semantic_hash="sha256:h",
            scope=SymbolScope.FUNCTION,
            lexical_parent_id="node:func_pct",
            last_indexed_at="2026-01-01T00:00:00Z",
        ),
        SymbolNode(
            id="node:mod2",
            repo_id=repo_id,
            file_id=f2.id,
            kind=SymbolKind.MODULE,
            name="mod2",
            qualified_name="mod2",
            uri=f2.uri,
            start_line=1,
            end_line=5,
            visibility_hint="module",
            content_hash="sha256:i",
            semantic_hash="sha256:j",
            scope=SymbolScope.MODULE,
            last_indexed_at="2026-01-01T00:00:00Z",
        ),
        SymbolNode(
            id="node:sub",
            repo_id=repo_id,
            file_id=f2.id,
            kind=SymbolKind.CLASS,
            name="SubClass",
            qualified_name="mod2.SubClass",
            uri=f2.uri,
            start_line=2,
            end_line=5,
            parent_id="node:mod2",
            visibility_hint="public",
            content_hash="sha256:k",
            semantic_hash="sha256:l",
            scope=SymbolScope.MODULE,
            last_indexed_at="2026-01-01T00:00:00Z",
        ),
    ]
    edges = [
        Edge(
            id="edge:imp",
            repo_id=repo_id,
            kind=EdgeKind.IMPORTS,
            from_id="node:mod2",
            to_id="node:cls1",
            confidence=0.9,
            evidence_file_id=f2.id,
            evidence_uri=f2.uri,
            evidence_range=Range(
                start=Position(line=0, character=0),
                end=Position(line=0, character=23),
            ),
            payload={"import_type": "from", "module": "mod1", "symbol": "Class1"},
        ),
        Edge(
            id="edge:inh",
            repo_id=repo_id,
            kind=EdgeKind.INHERITS,
            from_id="node:sub",
            to_id="node:cls1",
            confidence=0.9,
            evidence_file_id=f2.id,
            evidence_uri=f2.uri,
            payload={"base_name": "Class1"},
        ),
        Edge(
            id="edge:call",
            repo_id=repo_id,
            kind=EdgeKind.CALLS,
            from_id="node:sub",
            to_id="node:func_pct",
            confidence=0.9,
            evidence_file_id=f2.id,
            evidence_uri=f2.uri,
            payload={"target": "compute_100%_rate"},
        ),
    ]

    with factory.open(initialize=True) as uow:
        uow.repos.upsert(
            RepoRecord(
                id=repo_id,
                root_path=str(repo_root),
                name="test",
                created_at="2026-01-01T00:00:00Z",
                last_indexed_at="2026-01-01T00:00:00Z",
            )
        )
        uow.files.upsert_many([f1, f2])
        uow.nodes.upsert_many(nodes)
        uow.edges.upsert_many(edges)

    return factory, repo_root


def test_symbol_and_hierarchy_lookups(query_env: tuple[SqlUnitOfWorkFactory, Path]) -> None:
    """Symbol lookups, batch ID fetches, members, and parent/child navigation work cleanly."""
    factory, _ = query_env
    repo_id = "project:test:12345678"
    navigator = GraphNavigator()

    with factory.open(read_only=True) as uow:
        cls1 = navigator.get_symbol(uow, "node:cls1")
        assert cls1 is not None
        summary = navigator.get_symbol_summary(uow, "node:cls1")
        assert summary is not None and summary.file_path == "mod1.py"

        by_qname = uow.nodes.get_by_qualified_name(repo_id, "mod1.Class1", SymbolKind.CLASS)
        assert by_qname is not None and by_qname.id == "node:cls1"
        parent_sym = navigator.get_parent(uow, cls1)
        assert parent_sym is not None and parent_sym.id == "node:mod1"

        inner = navigator.get_symbol(uow, "node:inner")
        assert inner is not None
        lex_parent = navigator.get_lexical_parent(uow, inner)
        assert lex_parent is not None and lex_parent.id == "node:func_pct"
        assert [c.id for c in navigator.get_lexical_children(uow, "node:func_pct")] == [
            "node:inner"
        ]
        assert {c.id for c in navigator.get_children(uow, "node:mod1")} == {
            "node:cls1",
            "node:func_pct",
        }
        assert {m.id for m in navigator.get_members(uow, "node:mod1")} == {
            "node:cls1",
            "node:func_pct",
        }
        with pytest.raises(SymbolNotFoundError):
            navigator.get_members(uow, "node:missing")

        batched_syms = uow.nodes.get_by_ids(["node:cls1", "node:sub", "node:missing"])
        assert set(batched_syms.keys()) == {"node:cls1", "node:sub"}

        batched_files = uow.files.get_by_ids([f"file:{repo_id}:mod1.py", f"file:{repo_id}:mod2.py"])
        assert len(batched_files) == 2


def test_directed_edge_queries_subgraph_and_reference_report(
    query_env: tuple[SqlUnitOfWorkFactory, Path],
) -> None:
    """Directed edge queries, SubgraphSlice BFS, and ReferenceAnalyzer reports work cleanly."""
    factory, _ = query_env
    navigator = GraphNavigator()
    ref_analyzer = ReferenceAnalyzer()

    with factory.open(read_only=True) as uow:
        assert len(navigator.list_outgoing_edges(uow, "node:mod1")) == 0
        assert len(navigator.list_incoming_edges(uow, "node:func_pct")) == 1

        assert len(navigator.list_incoming_edges(uow, "node:func_pct", EdgeKind.CALLS)) == 1
        assert len(navigator.list_outgoing_edges(uow, "node:sub", EdgeKind.CALLS)) == 1
        assert len(navigator.list_incoming_edges(uow, "node:cls1", EdgeKind.IMPORTS)) == 1
        assert len(navigator.list_outgoing_edges(uow, "node:mod2", EdgeKind.IMPORTS)) == 1
        assert len(navigator.list_incoming_edges(uow, "node:cls1", EdgeKind.INHERITS)) == 1
        assert len(navigator.list_outgoing_edges(uow, "node:sub", EdgeKind.INHERITS)) == 1

        subgraph = navigator.build_subgraph(
            uow,
            ["node:cls1"],
            SubgraphOptions(depth=1, max_nodes=10),
        )
        node_ids = {n.id for n in subgraph.nodes}
        assert {"node:cls1", "node:mod2", "node:sub"}.issubset(node_ids)
        assert subgraph.truncated is False

        trunc_sub = navigator.build_subgraph(
            uow,
            ["node:cls1"],
            SubgraphOptions(depth=1, max_nodes=2),
        )
        assert len(trunc_sub.nodes) == 2
        assert trunc_sub.truncated is True

        report = ref_analyzer.inspect_references(
            uow,
            "node:cls1",
            ReferenceQueryOptions(direction=ReferenceDirection.INCOMING),
        )
        assert report.total_count == 2  # imports + inherits
        assert any(r.preview == "from mod1 import Class1" for r in report.references)


def test_fts5_search_and_escaped_like(query_env: tuple[SqlUnitOfWorkFactory, Path]) -> None:
    """search_fts handles exact/prefix ranking, filters, and special tokens safely."""
    factory, _ = query_env
    repo_id = "project:test:12345678"

    with factory.open(read_only=True) as uow:
        exact_hits = uow.nodes.search_fts(SymbolSearchQuery(repo_id=repo_id, query="Class1"))
        assert exact_hits and exact_hits[0].id == "node:cls1"

        prefix_hits = uow.nodes.search_fts(SymbolSearchQuery(repo_id=repo_id, query="Sub"))
        assert [h.id for h in prefix_hits] == ["node:sub"]

        filtered = uow.nodes.search_fts(
            SymbolSearchQuery(
                repo_id=repo_id,
                query="Class",
                kind=SymbolKind.CLASS,
                file_path="mod2.py",
            )
        )
        assert [h.id for h in filtered] == ["node:sub"]

        # Escaped LIKE: '%' and '_' must NOT act as SQLite wildcards
        pct_hits = uow.nodes.find_by_name(repo_id, "100%")
        assert {h.id for h in pct_hits} == {"node:func_pct", "node:inner"}
        rate_hits = uow.nodes.find_by_name(repo_id, "%_rate", kind=SymbolKind.FUNCTION)
        assert [h.id for h in rate_hits] == ["node:func_pct"]
        assert uow.nodes.find_by_name(repo_id, "100_rate") == []

        fts_special = uow.nodes.search_fts(
            SymbolSearchQuery(repo_id=repo_id, query="100%", kind=SymbolKind.FUNCTION)
        )
        assert [h.id for h in fts_special] == ["node:func_pct"]

        # FTS operator / punctuation edge cases never raise sqlite3.OperationalError
        bad_fts = uow.nodes.search_fts(SymbolSearchQuery(repo_id=repo_id, query='NOT OR * "bad"'))
        assert bad_fts == []


def test_outline_helpers_and_disk_freshness(query_env: tuple[SqlUnitOfWorkFactory, Path]) -> None:
    """Outline helpers and check_file_freshness reflect live disk edits."""
    factory, repo_root = query_env
    repo_id = "project:test:12345678"
    navigator = GraphNavigator()

    with factory.open(read_only=True) as uow:
        f1 = navigator.get_file_for_outline(uow, repo_id, "./mod1.py")
        assert f1 is not None
        assert len(navigator.list_file_symbols(uow, f1.id)) == 4

        f2 = navigator.get_file_for_outline(uow, repo_id, "mod2.py")
        assert f2 is not None
        assert len(navigator.list_file_imports(uow, f2.id)) == 1

        fresh = navigator.check_file_freshness(uow, f1.id)
        assert fresh.is_fresh is True
        assert fresh.stale_reason is None
        assert fresh.freshness == FreshnessStatus.FRESH

        (repo_root / "mod1.py").write_text("class Class1:\n    x = 1\n", encoding="utf-8")
        stale = navigator.check_file_freshness(uow, f1.id)
        assert stale.is_fresh is False
        assert stale.stale_reason == "content_hash_mismatch"
        assert stale.freshness == FreshnessStatus.STALE

        (repo_root / "mod1.py").unlink()
        missing = navigator.check_file_freshness(uow, f1.id)
        assert missing.is_fresh is False
        assert missing.stale_reason == "file_missing"
        assert missing.freshness == FreshnessStatus.MISSING
