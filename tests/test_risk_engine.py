"""Tests for the v0.2.0 risk engine."""

from collections.abc import Iterator
import json
from pathlib import Path

import pytest
from sqlalchemy.engine import Connection

from repo_context.constants import EDGE_KIND_CALLS, EDGE_KIND_INHERITS, EDGE_KIND_REFERENCES
from repo_context.graph.risk_engine import analyze_symbol_risk, analyze_target_set_risk
from repo_context.graph.risk_facts import (
    LOW_CONFIDENCE_EDGE_LIMIT,
    build_risk_facts,
    collect_low_confidence_edges,
    collect_stale_symbols,
    target_has_inheritance_risk,
)
from repo_context.graph.risk_rules import (
    ISSUE_CROSS_FILE_IMPACT,
    ISSUE_CROSS_MODULE_IMPACT,
    ISSUE_HIGH_REFERENCE_COUNT,
    ISSUE_INHERITANCE_RISK,
    ISSUE_LOW_CONFIDENCE_MATCH,
    ISSUE_MODERATE_REFERENCE_COUNT,
    ISSUE_MULTI_FILE_CHANGE,
    ISSUE_MULTI_MODULE_CHANGE,
    ISSUE_PUBLIC_SURFACE_CHANGE,
    ISSUE_REFERENCE_DATA_UNAVAILABLE,
    ISSUE_STALE_CONTEXT,
    detect_risk_issues,
)
from repo_context.graph.risk_scoring import (
    DECISION_HIGH_RISK,
    DECISION_REVIEW_REQUIRED,
    DECISION_SAFE_ENOUGH,
    decide_risk,
    score_risk,
)
from repo_context.graph.risk_targets import is_public_like, load_risk_targets
from repo_context.graph.risk_types import RiskFacts, RiskTarget
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
    upsert_reference_refresh,
    upsert_repo,
)


@pytest.fixture
def initialized_conn(tmp_path: Path) -> Iterator[tuple[Connection, Path]]:
    """Yield an initialized SQLite connection and real repo directory, closing in teardown."""
    repo_root = tmp_path / "repo"
    (repo_root / "src").mkdir(parents=True)
    conn = get_connection(tmp_path / "risk.db")
    try:
        initialize_database(conn)
        yield conn, repo_root
    finally:
        close_connection(conn)


def _seed_two_module_graph(conn: Connection, repo_root: Path) -> str:
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
    upsert_files(conn, [f1, f2])

    nodes = [
        {
            "id": f"sym:{repo_id}:class:src.module1.BaseModel",
            "repo_id": repo_id,
            "file_id": f1.id,
            "language": "python",
            "kind": "class",
            "name": "BaseModel",
            "qualified_name": "src.module1.BaseModel",
            "uri": f1.uri,
            "start_line": 1,
            "end_line": 3,
            "signature": "class BaseModel",
            "parent_id": None,
            "visibility_hint": "public",
            "content_hash": "sha256:a",
            "semantic_hash": "sha256:b",
            "source": "python-ast",
            "confidence": 1.0,
            "payload_json": "{}",
            "scope": "module",
            "last_indexed_at": "2026-01-01T00:00:00Z",
        },
        {
            "id": f"sym:{repo_id}:method:src.module1.BaseModel.save",
            "repo_id": repo_id,
            "file_id": f1.id,
            "language": "python",
            "kind": "method",
            "name": "save",
            "qualified_name": "src.module1.BaseModel.save",
            "uri": f1.uri,
            "start_line": 2,
            "end_line": 3,
            "signature": "def save(self)",
            "parent_id": f"sym:{repo_id}:class:src.module1.BaseModel",
            "visibility_hint": "public",
            "content_hash": "sha256:c",
            "semantic_hash": "sha256:d",
            "source": "python-ast",
            "confidence": 1.0,
            "payload_json": "{}",
            "scope": "class",
            "last_indexed_at": "2026-01-01T00:00:00Z",
        },
        {
            "id": f"sym:{repo_id}:function:src.module1.func1",
            "repo_id": repo_id,
            "file_id": f1.id,
            "language": "python",
            "kind": "function",
            "name": "func1",
            "qualified_name": "src.module1.func1",
            "uri": f1.uri,
            "start_line": 5,
            "end_line": 6,
            "signature": "def func1()",
            "parent_id": None,
            "visibility_hint": "public",
            "content_hash": "sha256:e",
            "semantic_hash": "sha256:f",
            "source": "python-ast",
            "confidence": 1.0,
            "payload_json": "{}",
            "scope": "module",
            "last_indexed_at": "2026-01-01T00:00:00Z",
        },
        {
            "id": f"sym:{repo_id}:class:src.module2.ChildModel",
            "repo_id": repo_id,
            "file_id": f2.id,
            "language": "python",
            "kind": "class",
            "name": "ChildModel",
            "qualified_name": "src.module2.ChildModel",
            "uri": f2.uri,
            "start_line": 3,
            "end_line": 4,
            "signature": "class ChildModel(BaseModel)",
            "parent_id": None,
            "visibility_hint": "public",
            "content_hash": "sha256:g",
            "semantic_hash": "sha256:h",
            "source": "python-ast",
            "confidence": 1.0,
            "payload_json": "{}",
            "scope": "module",
            "last_indexed_at": "2026-01-01T00:00:00Z",
        },
        {
            "id": f"sym:{repo_id}:function:src.module2.func2",
            "repo_id": repo_id,
            "file_id": f2.id,
            "language": "python",
            "kind": "function",
            "name": "func2",
            "qualified_name": "src.module2.func2",
            "uri": f2.uri,
            "start_line": 6,
            "end_line": 7,
            "signature": "def func2()",
            "parent_id": None,
            "visibility_hint": "public",
            "content_hash": "sha256:i",
            "semantic_hash": "sha256:j",
            "source": "python-ast",
            "confidence": 1.0,
            "payload_json": "{}",
            "scope": "module",
            "last_indexed_at": "2026-01-01T00:00:00Z",
        },
    ]
    upsert_nodes(conn, nodes)

    edges = [
        {
            "id": f"edge:{repo_id}:inherits:child->base",
            "repo_id": repo_id,
            "kind": EDGE_KIND_INHERITS,
            "from_id": f"sym:{repo_id}:class:src.module2.ChildModel",
            "to_id": f"sym:{repo_id}:class:src.module1.BaseModel",
            "source": "python-ast",
            "confidence": 0.9,
            "evidence_file_id": f2.id,
            "evidence_uri": f2.uri,
            "payload_json": json.dumps({"base_name": "BaseModel"}),
            "last_indexed_at": "2026-01-01T00:00:00Z",
        },
        {
            "id": f"edge:{repo_id}:references:func2->func1",
            "repo_id": repo_id,
            "kind": EDGE_KIND_REFERENCES,
            "from_id": f"sym:{repo_id}:function:src.module2.func2",
            "to_id": f"sym:{repo_id}:function:src.module1.func1",
            "source": "lsp",
            "confidence": 0.9,
            "evidence_file_id": f2.id,
            "evidence_uri": f2.uri,
            "evidence_range_json": json.dumps({"start": {"line": 6, "character": 11}, "end": {"line": 6, "character": 16}}),
            "payload_json": json.dumps({"mapping_mode": "exact_symbol"}),
            "last_indexed_at": "2026-01-01T00:00:00Z",
        },
        {
            "id": f"edge:{repo_id}:calls:func2->func1",
            "repo_id": repo_id,
            "kind": EDGE_KIND_CALLS,
            "from_id": f"sym:{repo_id}:function:src.module2.func2",
            "to_id": f"sym:{repo_id}:function:src.module1.func1",
            "source": "python-ast",
            "confidence": 0.9,
            "evidence_file_id": f2.id,
            "evidence_uri": f2.uri,
            "payload_json": json.dumps({"target": "func1"}),
            "last_indexed_at": "2026-01-01T00:00:00Z",
        },
    ]
    upsert_edges(conn, edges)
    upsert_reference_refresh(
        conn, f"sym:{repo_id}:function:src.module1.func1", True, "2026-01-01T00:00:00Z", "ok"
    )
    conn.commit()
    return repo_id


def test_load_risk_targets_populates_file_metadata_from_files_join(
    initialized_conn: tuple[Connection, Path],
) -> None:
    """load_risk_targets joins files so file_path, module_path, is_test, and line bounds populate."""
    conn, repo_root = initialized_conn
    repo_id = _seed_two_module_graph(conn, repo_root)

    targets = load_risk_targets(conn, [f"sym:{repo_id}:class:src.module1.BaseModel"])
    assert len(targets) == 1
    t = targets[0]
    assert t.file_path == "src/module1.py"
    assert t.module_path == "src.module1"
    assert t.is_test is False
    assert t.start_line == 1
    assert t.end_line == 3

    with pytest.raises(ValueError, match="Symbol not found"):
        load_risk_targets(conn, ["sym:missing"])


def test_is_public_like_checks_local_scope_before_visibility_hint() -> None:
    """Local functions (scope=='function') are NEVER public-like even with visibility_hint='public'."""
    local_target = RiskTarget(
        symbol_id="sym:local",
        qualified_name="mod.outer.helper",
        kind="local_function",
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
        kind="method",
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
        kind="function",
        scope="module",
        file_id="file:mod.py",
        file_path="mod.py",
        module_path="mod",
        visibility_hint=None,
    )
    assert is_public_like(priv) is False


def test_base_class_incoming_inherits_and_method_inheritance_risk(
    initialized_conn: tuple[Connection, Path],
) -> None:
    """Base classes (incoming inherits) and their methods flag inheritance_risk."""
    conn, repo_root = initialized_conn
    repo_id = _seed_two_module_graph(conn, repo_root)

    base_targets = load_risk_targets(conn, [f"sym:{repo_id}:class:src.module1.BaseModel"])
    child_targets = load_risk_targets(conn, [f"sym:{repo_id}:class:src.module2.ChildModel"])
    save_targets = load_risk_targets(conn, [f"sym:{repo_id}:method:src.module1.BaseModel.save"])

    assert target_has_inheritance_risk(conn, base_targets[0]) is True
    assert target_has_inheritance_risk(conn, child_targets[0]) is True
    assert target_has_inheritance_risk(conn, save_targets[0]) is True

    base_result = analyze_symbol_risk(conn, f"sym:{repo_id}:class:src.module1.BaseModel")
    assert ISSUE_INHERITANCE_RISK in base_result.issues


def test_multi_module_targets_and_cross_module_impact(
    initialized_conn: tuple[Connection, Path],
) -> None:
    """Multi-target analysis detects multi-file/multi-module targets and cross-module impact."""
    conn, repo_root = initialized_conn
    repo_id = _seed_two_module_graph(conn, repo_root)

    single = analyze_symbol_risk(conn, f"sym:{repo_id}:function:src.module1.func1")
    assert single.facts.cross_file_impact is True
    assert single.facts.cross_module_impact is True
    assert ISSUE_CROSS_FILE_IMPACT in single.issues
    assert ISSUE_CROSS_MODULE_IMPACT in single.issues
    assert single.facts.stale_symbols == []

    multi = analyze_target_set_risk(
        conn,
        [
            f"sym:{repo_id}:function:src.module1.func1",
            f"sym:{repo_id}:function:src.module2.func2",
        ],
    )
    assert multi.facts.target_spans_multiple_files is True
    assert multi.facts.target_spans_multiple_modules is True
    assert ISSUE_MULTI_FILE_CHANGE in multi.issues
    assert ISSUE_MULTI_MODULE_CHANGE in multi.issues
    assert ISSUE_REFERENCE_DATA_UNAVAILABLE in multi.issues  # func2 unrefreshed


def test_disk_staleness_and_low_confidence_edge_capping(
    initialized_conn: tuple[Connection, Path],
) -> None:
    """collect_stale_symbols detects modified disk files and low_confidence_edges caps at 25."""
    conn, repo_root = initialized_conn
    repo_id = _seed_two_module_graph(conn, repo_root)
    target_id = f"sym:{repo_id}:function:src.module1.func1"
    f2_id = f"file:{repo_id}:src/module2.py"

    # Insert 30 low-confidence reference edges
    low_edges = [
        {
            "id": f"edge:low:{i}",
            "repo_id": repo_id,
            "kind": EDGE_KIND_REFERENCES,
            "from_id": f"sym:{repo_id}:function:src.module2.func2",
            "to_id": target_id,
            "source": "lsp",
            "confidence": 0.5,
            "evidence_file_id": f2_id,
            "evidence_uri": (repo_root / "src" / "module2.py").as_uri(),
            "payload_json": "{}",
            "last_indexed_at": "2026-01-01T00:00:00Z",
        }
        for i in range(30)
    ]
    upsert_edges(conn, low_edges)
    conn.commit()

    targets = load_risk_targets(conn, [target_id])
    assert len(collect_low_confidence_edges(conn, targets)) == LOW_CONFIDENCE_EDGE_LIMIT

    # Mutate disk file so func1 becomes stale
    (repo_root / "src" / "module1.py").write_text("# modified on disk\n", encoding="utf-8")
    assert collect_stale_symbols(conn, targets) == [target_id]

    facts = build_risk_facts(conn, targets)
    assert len(facts.low_confidence_edges) == 25
    assert facts.extra["low_confidence_edge_count"] == 30
    assert facts.extra["low_confidence_edges_truncated"] is True
    issues = detect_risk_issues(facts)
    assert ISSUE_STALE_CONTEXT in issues
    assert ISSUE_LOW_CONFIDENCE_MATCH in issues
    assert ISSUE_HIGH_REFERENCE_COUNT in issues


def test_scoring_and_decision_rules() -> None:
    """Verify moderate/high ref issues, clamping, local mitigation, and decision overrides."""
    mod_issues = detect_risk_issues(
        RiskFacts(reference_counts={"s": 8}, reference_availability={"s": True})
    )
    assert mod_issues == [ISSUE_MODERATE_REFERENCE_COUNT]
    assert score_risk(mod_issues, RiskFacts()) == 15

    high_issues = detect_risk_issues(
        RiskFacts(reference_counts={"s": 20}, reference_availability={"s": True})
    )
    assert high_issues == [ISSUE_HIGH_REFERENCE_COUNT]
    assert score_risk(high_issues, RiskFacts()) == 30

    local_facts = RiskFacts(touches_local_scope_only=True, touches_public_surface=False)
    assert score_risk([ISSUE_STALE_CONTEXT], local_facts) == 10
    assert decide_risk([ISSUE_STALE_CONTEXT], local_facts, 10) == DECISION_REVIEW_REQUIRED
    assert decide_risk([], RiskFacts(), 10) == DECISION_SAFE_ENOUGH
    assert decide_risk([], RiskFacts(), 80) == DECISION_HIGH_RISK
    assert (
        decide_risk(
            [ISSUE_REFERENCE_DATA_UNAVAILABLE, ISSUE_PUBLIC_SURFACE_CHANGE],
            RiskFacts(),
            20,
        )
        == DECISION_REVIEW_REQUIRED
    )
