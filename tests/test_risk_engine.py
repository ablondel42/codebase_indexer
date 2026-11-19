"""Tests for the deterministic change-impact RiskFactCollector and RiskAnalyzer."""

from pathlib import Path

import pytest

from core.models import (
    Edge,
    EdgeKind,
    FileRecord,
    Position,
    Range,
    ReferenceRefreshState,
    RefreshStatus,
    RepoRecord,
    SymbolKind,
    SymbolNode,
    SymbolNotFoundError,
    SymbolScope,
    ValidationError,
)
from core.utils.hashing import sha256_file
from graph import (
    RiskAnalyzer,
    RiskDecision,
    RiskFactCollector,
    RiskFacts,
    RiskIssueCode,
    RiskTarget,
)
from graph.services.risk_facts import LOW_CONFIDENCE_EDGE_LIMIT
from graph.utils.predicates import is_public_like
from graph.utils.scoring import decide_risk, detect_risk_issues, score_risk
from storage import SqlUnitOfWorkFactory


@pytest.fixture
def risk_env(tmp_path: Path) -> tuple[SqlUnitOfWorkFactory, Path]:
    """Create an initialized SQLite UnitOfWorkFactory and real repo directory."""
    repo_root = tmp_path / "repo"
    (repo_root / "src").mkdir(parents=True)
    factory = SqlUnitOfWorkFactory(tmp_path / "risk.db")
    with factory.open(initialize=True):
        pass
    return factory, repo_root


def _seed_two_module_graph(factory: SqlUnitOfWorkFactory, repo_root: Path) -> str:
    """Populate a realistic two-module graph backed by real disk files."""
    repo_id = "repo:test:12345678"
    f1_disk = repo_root / "src" / "module1.py"
    f2_disk = repo_root / "src" / "module2.py"
    f1_disk.write_text(
        "class BaseModel:\n    def save(self):\n        pass\n\ndef func1():\n    pass\n",
        encoding="utf-8",
    )
    f2_disk.write_text(
        "from src.module1 import BaseModel, func1\n\n"
        "class ChildModel(BaseModel):\n    pass\n\n"
        "def func2():\n    return func1()\n",
        encoding="utf-8",
    )

    f1 = FileRecord(
        id=f"file:{repo_id}:src/module1.py",
        repo_id=repo_id,
        file_path="src/module1.py",
        uri=f1_disk.as_uri(),
        module_path="src.module1",
        content_hash=sha256_file(f1_disk),
        size_bytes=f1_disk.stat().st_size,
        line_count=6,
        is_test=False,
        last_modified_at="2026-01-01T00:00:00Z",
    )
    f2 = FileRecord(
        id=f"file:{repo_id}:src/module2.py",
        repo_id=repo_id,
        file_path="src/module2.py",
        uri=f2_disk.as_uri(),
        module_path="src.module2",
        content_hash=sha256_file(f2_disk),
        size_bytes=f2_disk.stat().st_size,
        line_count=6,
        is_test=False,
        last_modified_at="2026-01-01T00:00:00Z",
    )
    nodes = [
        SymbolNode(
            id=f"sym:{repo_id}:class:src.module1.BaseModel",
            repo_id=repo_id,
            file_id=f1.id,
            kind=SymbolKind.CLASS,
            name="BaseModel",
            qualified_name="src.module1.BaseModel",
            uri=f1.uri,
            start_line=1,
            end_line=3,
            signature="class BaseModel",
            visibility_hint="public",
            content_hash="sha256:a",
            semantic_hash="sha256:b",
            scope=SymbolScope.MODULE,
            last_indexed_at="2026-01-01T00:00:00Z",
        ),
        SymbolNode(
            id=f"sym:{repo_id}:method:src.module1.BaseModel.save",
            repo_id=repo_id,
            file_id=f1.id,
            kind=SymbolKind.METHOD,
            name="save",
            qualified_name="src.module1.BaseModel.save",
            uri=f1.uri,
            start_line=2,
            end_line=3,
            signature="def save(self)",
            parent_id=f"sym:{repo_id}:class:src.module1.BaseModel",
            visibility_hint="public",
            content_hash="sha256:c",
            semantic_hash="sha256:d",
            scope=SymbolScope.CLASS,
            last_indexed_at="2026-01-01T00:00:00Z",
        ),
        SymbolNode(
            id=f"sym:{repo_id}:function:src.module1.func1",
            repo_id=repo_id,
            file_id=f1.id,
            kind=SymbolKind.FUNCTION,
            name="func1",
            qualified_name="src.module1.func1",
            uri=f1.uri,
            start_line=5,
            end_line=6,
            signature="def func1()",
            visibility_hint="public",
            content_hash="sha256:e",
            semantic_hash="sha256:f",
            scope=SymbolScope.MODULE,
            last_indexed_at="2026-01-01T00:00:00Z",
        ),
        SymbolNode(
            id=f"sym:{repo_id}:class:src.module2.ChildModel",
            repo_id=repo_id,
            file_id=f2.id,
            kind=SymbolKind.CLASS,
            name="ChildModel",
            qualified_name="src.module2.ChildModel",
            uri=f2.uri,
            start_line=3,
            end_line=4,
            signature="class ChildModel(BaseModel)",
            visibility_hint="public",
            content_hash="sha256:g",
            semantic_hash="sha256:h",
            scope=SymbolScope.MODULE,
            last_indexed_at="2026-01-01T00:00:00Z",
        ),
        SymbolNode(
            id=f"sym:{repo_id}:function:src.module2.func2",
            repo_id=repo_id,
            file_id=f2.id,
            kind=SymbolKind.FUNCTION,
            name="func2",
            qualified_name="src.module2.func2",
            uri=f2.uri,
            start_line=6,
            end_line=7,
            signature="def func2()",
            visibility_hint="public",
            content_hash="sha256:i",
            semantic_hash="sha256:j",
            scope=SymbolScope.MODULE,
            last_indexed_at="2026-01-01T00:00:00Z",
        ),
    ]
    edges = [
        Edge(
            id=f"edge:{repo_id}:inherits:child->base",
            repo_id=repo_id,
            kind=EdgeKind.INHERITS,
            from_id=f"sym:{repo_id}:class:src.module2.ChildModel",
            to_id=f"sym:{repo_id}:class:src.module1.BaseModel",
            confidence=0.9,
            evidence_file_id=f2.id,
            evidence_uri=f2.uri,
            payload={"base_name": "BaseModel"},
            last_indexed_at="2026-01-01T00:00:00Z",
        ),
        Edge(
            id=f"edge:{repo_id}:references:func2->func1",
            repo_id=repo_id,
            kind=EdgeKind.REFERENCES,
            from_id=f"sym:{repo_id}:function:src.module2.func2",
            to_id=f"sym:{repo_id}:function:src.module1.func1",
            source="lsp",
            confidence=0.9,
            evidence_file_id=f2.id,
            evidence_uri=f2.uri,
            evidence_range=Range(
                start=Position(line=6, character=11),
                end=Position(line=6, character=16),
            ),
            payload={"mapping_mode": "exact_symbol"},
            last_indexed_at="2026-01-01T00:00:00Z",
        ),
        Edge(
            id=f"edge:{repo_id}:calls:func2->func1",
            repo_id=repo_id,
            kind=EdgeKind.CALLS,
            from_id=f"sym:{repo_id}:function:src.module2.func2",
            to_id=f"sym:{repo_id}:function:src.module1.func1",
            confidence=0.9,
            evidence_file_id=f2.id,
            evidence_uri=f2.uri,
            payload={"target": "func1"},
            last_indexed_at="2026-01-01T00:00:00Z",
        ),
    ]

    with factory.open() as uow:
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
        uow.refresh.upsert(
            ReferenceRefreshState(
                target_symbol_id=f"sym:{repo_id}:function:src.module1.func1",
                available=True,
                last_refreshed_at="2026-01-01T00:00:00Z",
                refresh_status=RefreshStatus.OK,
            )
        )

    return repo_id


def test_load_risk_targets_populates_file_metadata_from_files_join(
    risk_env: tuple[SqlUnitOfWorkFactory, Path],
) -> None:
    """RiskFactCollector.load_targets populates file_path, module_path, is_test, and lines."""
    factory, repo_root = risk_env
    repo_id = _seed_two_module_graph(factory, repo_root)
    collector = RiskFactCollector()

    with factory.open(read_only=True) as uow:
        targets = collector.load_targets(uow, [f"sym:{repo_id}:class:src.module1.BaseModel"])
        assert len(targets) == 1
        t = targets[0]
        assert t.file_path == "src/module1.py"
        assert t.module_path == "src.module1"
        assert t.is_test is False
        assert t.start_line == 1
        assert t.end_line == 3

        with pytest.raises(SymbolNotFoundError, match="Symbol not found"):
            collector.load_targets(uow, ["sym:missing"])

        with pytest.raises(ValidationError):
            collector.load_targets(uow, [])


def test_is_public_like_checks_local_scope_before_visibility_hint() -> None:
    """Local functions (scope=='function') are never public-like even with visibility='public'."""
    local_target = RiskTarget(
        symbol_id="sym:local",
        qualified_name="mod.outer.helper",
        kind=SymbolKind.LOCAL_FUNCTION,
        scope="function",
        file_id="file:mod.py",
        file_path="mod.py",
        module_path="mod",
        visibility_hint="public",
    )
    assert is_public_like(local_target) is False

    dunder = RiskTarget(
        symbol_id="sym:init",
        qualified_name="mod.Cls.__init__",
        kind=SymbolKind.METHOD,
        scope="class",
        file_id="file:mod.py",
        file_path="mod.py",
        module_path="mod",
        visibility_hint=None,
    )
    assert is_public_like(dunder) is True

    priv = RiskTarget(
        symbol_id="sym:priv",
        qualified_name="mod._helper",
        kind=SymbolKind.FUNCTION,
        scope="module",
        file_id="file:mod.py",
        file_path="mod.py",
        module_path="mod",
        visibility_hint=None,
    )
    assert is_public_like(priv) is False


def test_base_class_incoming_inherits_and_method_inheritance_risk(
    risk_env: tuple[SqlUnitOfWorkFactory, Path],
) -> None:
    """Base classes (incoming inherits), subclasses, and their methods flag inheritance_risk."""
    factory, repo_root = risk_env
    repo_id = _seed_two_module_graph(factory, repo_root)
    collector = RiskFactCollector()
    analyzer = RiskAnalyzer()

    with factory.open(read_only=True) as uow:
        for sym_id in (
            f"sym:{repo_id}:class:src.module1.BaseModel",
            f"sym:{repo_id}:class:src.module2.ChildModel",
            f"sym:{repo_id}:method:src.module1.BaseModel.save",
        ):
            targets = collector.load_targets(uow, [sym_id])
            facts = collector.collect_facts(uow, targets)
            assert facts.inheritance_involved is True

        base_result = analyzer.analyze_symbol(uow, f"sym:{repo_id}:class:src.module1.BaseModel")
        assert RiskIssueCode.INHERITANCE_RISK.value in base_result.issues


def test_multi_module_targets_and_cross_module_impact(
    risk_env: tuple[SqlUnitOfWorkFactory, Path],
) -> None:
    """Multi-target analysis detects multi-file/multi-module targets and cross-module impact."""
    factory, repo_root = risk_env
    repo_id = _seed_two_module_graph(factory, repo_root)
    analyzer = RiskAnalyzer()

    with factory.open(read_only=True) as uow:
        single = analyzer.analyze_symbol(uow, f"sym:{repo_id}:function:src.module1.func1")
        assert single.facts.cross_file_impact is True
        assert single.facts.cross_module_impact is True
        assert RiskIssueCode.CROSS_FILE_IMPACT.value in single.issues
        assert RiskIssueCode.CROSS_MODULE_IMPACT.value in single.issues
        assert single.facts.stale_symbols == []

        multi = analyzer.analyze_targets(
            uow,
            [
                f"sym:{repo_id}:function:src.module1.func1",
                f"sym:{repo_id}:function:src.module2.func2",
            ],
        )
        assert multi.facts.target_spans_multiple_files is True
        assert multi.facts.target_spans_multiple_modules is True
        assert RiskIssueCode.MULTI_FILE_CHANGE.value in multi.issues
        assert RiskIssueCode.MULTI_MODULE_CHANGE.value in multi.issues
        assert RiskIssueCode.REFERENCE_DATA_UNAVAILABLE.value in multi.issues


def test_disk_staleness_and_low_confidence_edge_capping(
    risk_env: tuple[SqlUnitOfWorkFactory, Path],
) -> None:
    """RiskFactCollector detects modified disk files and caps low_confidence_edges at 25."""
    factory, repo_root = risk_env
    repo_id = _seed_two_module_graph(factory, repo_root)
    target_id = f"sym:{repo_id}:function:src.module1.func1"
    f2_id = f"file:{repo_id}:src/module2.py"

    low_edges = [
        Edge(
            id=f"edge:low:{i}",
            repo_id=repo_id,
            kind=EdgeKind.REFERENCES,
            from_id=f"sym:{repo_id}:function:src.module2.func2",
            to_id=target_id,
            source="lsp",
            confidence=0.5,
            evidence_file_id=f2_id,
            evidence_uri=(repo_root / "src" / "module2.py").as_uri(),
            last_indexed_at="2026-01-01T00:00:00Z",
        )
        for i in range(30)
    ]
    with factory.open() as uow:
        uow.edges.upsert_many(low_edges)

    (repo_root / "src" / "module1.py").write_text("# modified on disk\n", encoding="utf-8")

    collector = RiskFactCollector()
    with factory.open(read_only=True) as ro_uow:
        targets = collector.load_targets(ro_uow, [target_id])
        facts = collector.collect_facts(ro_uow, targets)

    assert len(facts.low_confidence_edges) == LOW_CONFIDENCE_EDGE_LIMIT
    assert facts.extra["low_confidence_edge_count"] == 30
    assert facts.extra["low_confidence_edges_truncated"] is True
    assert facts.stale_symbols == [target_id]

    issues = detect_risk_issues(facts)
    assert RiskIssueCode.STALE_CONTEXT.value in issues
    assert RiskIssueCode.LOW_CONFIDENCE_MATCH.value in issues
    assert RiskIssueCode.HIGH_REFERENCE_COUNT.value in issues


def test_scoring_and_decision_rules() -> None:
    """Verify moderate/high ref issues, clamping, local mitigation, and decision overrides."""
    mod_issues = detect_risk_issues(
        RiskFacts(reference_counts={"s": 8}, reference_availability={"s": True})
    )
    assert mod_issues == [RiskIssueCode.MODERATE_REFERENCE_COUNT.value]
    assert score_risk(mod_issues, RiskFacts()) == 15

    high_issues = detect_risk_issues(
        RiskFacts(reference_counts={"s": 20}, reference_availability={"s": True})
    )
    assert high_issues == [RiskIssueCode.HIGH_REFERENCE_COUNT.value]
    assert score_risk(high_issues, RiskFacts()) == 30

    local_facts = RiskFacts(touches_local_scope_only=True, touches_public_surface=False)
    assert score_risk([RiskIssueCode.STALE_CONTEXT.value], local_facts) == 10
    assert (
        decide_risk([RiskIssueCode.STALE_CONTEXT.value], local_facts, 10)
        == RiskDecision.REVIEW_REQUIRED
    )
    assert decide_risk([], RiskFacts(), 10) == RiskDecision.SAFE_ENOUGH
    assert decide_risk([], RiskFacts(), 80) == RiskDecision.HIGH_RISK
    assert (
        decide_risk(
            [
                RiskIssueCode.REFERENCE_DATA_UNAVAILABLE.value,
                RiskIssueCode.PUBLIC_SURFACE_CHANGE.value,
            ],
            RiskFacts(),
            20,
        )
        == RiskDecision.REVIEW_REQUIRED
    )
