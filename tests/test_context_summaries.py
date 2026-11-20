"""Tests for ContextEvaluator structural and confidence summaries and formatting helpers."""

from context import (
    CompactSymbol,
    ContextEdge,
    ContextEvaluator,
    FocusSymbol,
    RelationshipCounts,
    StructuralTotals,
    SymbolContext,
)
from context.utils.formatting import PLACEHOLDER_PREFIXES, select_balanced_edges
from core.models import Edge, EdgeKind, SymbolKind, SymbolScope


def test_evaluate_structural_counts_and_truncation() -> None:
    """evaluate_structural exposes exact totals, truncation flag, and relationship counts."""
    evaluator = ContextEvaluator()
    context = SymbolContext(
        focus_symbol=FocusSymbol(
            id="node:1",
            kind=SymbolKind.CLASS,
            name="Svc",
            qualified_name="mod.Svc",
            scope=SymbolScope.MODULE,
        ),
        structural_parent=FocusSymbol(
            id="node:mod",
            kind=SymbolKind.MODULE,
            name="mod",
            qualified_name="mod",
        ),
        structural_children=[
            CompactSymbol(id="node:m1", kind=SymbolKind.METHOD, name="m1"),
            CompactSymbol(id="node:m2", kind=SymbolKind.METHOD, name="m2"),
        ],
        incoming_edges=[
            ContextEdge(id="edge:e1", kind=EdgeKind.CALLS, from_id="node:x", to_id="node:1")
        ],
        outgoing_edges=[
            ContextEdge(id="edge:e2", kind=EdgeKind.CALLS, from_id="node:1", to_id="node:y")
        ],
        file_siblings=[CompactSymbol(id="node:sib", kind=SymbolKind.FUNCTION, name="sib")],
    )
    summary = evaluator.evaluate_structural(
        context,
        StructuralTotals(
            total_structural_child_count=5,
            total_incoming_count=10,
            relationship_counts=RelationshipCounts(callers=4),
        ),
    )
    assert summary.has_structural_parent is True
    assert summary.structural_child_count == 5
    assert summary.structural_children_truncated is True
    assert summary.incoming_edge_count == 10
    assert summary.outgoing_edge_count == 1
    assert summary.same_file_sibling_count == 1
    assert summary.is_local_declaration is False
    assert summary.is_nested_declaration is False
    assert summary.relationship_counts.callers == 4


def test_nested_declaration_distinguishes_class_methods_from_function_locals() -> None:
    """Class methods are never classified as nested lexical declarations."""
    local_fn = FocusSymbol(
        id="node:lf",
        kind=SymbolKind.LOCAL_FUNCTION,
        name="inner",
        qualified_name="mod.fn.inner",
        scope=SymbolScope.FUNCTION,
        lexical_parent_id="node:fn",
    )
    local_cls = FocusSymbol(
        id="node:lc",
        kind=SymbolKind.CLASS,
        name="LocalCls",
        qualified_name="mod.fn.LocalCls",
        scope=SymbolScope.FUNCTION,
        lexical_parent_id="node:fn",
    )
    cls_method = FocusSymbol(
        id="node:m",
        kind=SymbolKind.METHOD,
        name="run",
        qualified_name="mod.Cls.run",
        scope=SymbolScope.CLASS,
        lexical_parent_id="node:cls",
    )
    mod_fn = FocusSymbol(
        id="node:f",
        kind=SymbolKind.FUNCTION,
        name="f",
        qualified_name="mod.f",
        scope=SymbolScope.MODULE,
    )

    assert ContextEvaluator.is_local_declaration(local_fn) is True
    assert ContextEvaluator.is_local_declaration(local_cls) is True
    assert ContextEvaluator.is_local_declaration(cls_method) is False

    assert ContextEvaluator.is_nested_declaration(local_fn) is True
    assert ContextEvaluator.is_nested_declaration(cls_method) is False
    assert ContextEvaluator.is_nested_declaration(mod_fn) is False


def test_evaluate_confidence_placeholders_and_balanced_edge_selection() -> None:
    """edge_confidence_min/max are None when 0 edges exist; unresolved_call: is detected."""
    evaluator = ContextEvaluator()
    no_edges = SymbolContext(
        focus_symbol=FocusSymbol(
            id="node:1",
            kind=SymbolKind.FUNCTION,
            name="f",
            qualified_name="mod.f",
            confidence=1.0,
        )
    )
    conf_empty = evaluator.evaluate_confidence(no_edges)
    assert conf_empty.edge_confidence_min is None
    assert conf_empty.edge_confidence_max is None
    assert conf_empty.contains_placeholder_targets is False
    assert conf_empty.graph_only is True

    assert "unresolved_call:" in PLACEHOLDER_PREFIXES
    with_call_placeholder = SymbolContext(
        focus_symbol=FocusSymbol(
            id="node:1",
            kind=SymbolKind.FUNCTION,
            name="f",
            qualified_name="mod.f",
            confidence=0.9,
        ),
        outgoing_edges=[
            ContextEdge(
                id="edge:p",
                kind=EdgeKind.CALLS,
                from_id="node:1",
                to_id="unresolved_call:foo",
                confidence=0.7,
            )
        ],
    )
    conf_edges = evaluator.evaluate_confidence(with_call_placeholder)
    assert conf_edges.edge_confidence_min == 0.7
    assert conf_edges.edge_confidence_max == 0.7
    assert conf_edges.contains_placeholder_targets is True

    edges = [
        Edge(id="e1", repo_id="project:r", kind=EdgeKind.CALLS, from_id="node:1", to_id="node:a"),
        Edge(id="e2", repo_id="project:r", kind=EdgeKind.CALLS, from_id="node:1", to_id="node:b"),
        Edge(id="e3", repo_id="project:r", kind=EdgeKind.IMPORTS, from_id="node:1", to_id="node:c"),
    ]
    balanced = select_balanced_edges(edges, limit=2)
    assert {e.kind for e in balanced} == {EdgeKind.CALLS, EdgeKind.IMPORTS}
