"""Tests for context summary and confidence builders (v0.2.0)."""

from repo_context.context.helpers import PLACEHOLDER_PREFIXES
from repo_context.context.summaries import (
    _is_local_declaration,
    _is_nested_declaration,
    build_confidence,
    build_structural_summary,
)
from repo_context.models import SymbolContext


def test_build_structural_summary_counts_and_truncation() -> None:
    """build_structural_summary exposes exact totals, truncation flag, and relationship counts."""
    context = SymbolContext(
        focus_symbol={"id": "sym1", "kind": "class", "scope": "module"},
        structural_parent={"id": "mod1"},
        structural_children=[{"id": "m1"}, {"id": "m2"}],
        incoming_edges=[{"id": "e1"}],
        outgoing_edges=[{"id": "e2"}],
        file_siblings=[{"id": "sib1"}],
    )
    summary = build_structural_summary(
        context,
        total_structural_child_count=5,
        total_incoming_count=10,
        relationship_counts={"callers": 4},
    )
    assert summary["has_structural_parent"] is True
    assert summary["structural_child_count"] == 5
    assert summary["structural_children_truncated"] is True
    assert summary["incoming_edge_count"] == 10
    assert summary["outgoing_edge_count"] == 1
    assert summary["same_file_sibling_count"] == 1
    assert summary["is_local_declaration"] is False
    assert summary["is_nested_declaration"] is False
    assert summary["relationship_counts"] == {"callers": 4}


def test_nested_declaration_distinguishes_class_methods_from_function_locals() -> None:
    """Class methods are never classified as nested lexical declarations."""
    assert _is_local_declaration({"kind": "local_function", "scope": "function"}) is True
    assert _is_local_declaration({"kind": "class", "scope": "function"}) is True
    assert _is_local_declaration({"kind": "method", "scope": "class"}) is False

    assert _is_nested_declaration({"kind": "local_function", "scope": "function", "lexical_parent_id": "p"}) is True
    assert _is_nested_declaration({"kind": "method", "scope": "class", "lexical_parent_id": "cls"}) is False
    assert _is_nested_declaration({"kind": "function", "scope": "module", "lexical_parent_id": None}) is False


def test_build_confidence_none_when_no_edges_and_placeholders() -> None:
    """edge_confidence_min/max are None when 0 edges exist; unresolved_call: is detected."""
    no_edges = SymbolContext(focus_symbol={"id": "sym1", "confidence": 1.0})
    conf_empty = build_confidence(no_edges)
    assert conf_empty["edge_confidence_min"] is None
    assert conf_empty["edge_confidence_max"] is None
    assert conf_empty["contains_placeholder_targets"] is False
    assert conf_empty["graph_only"] is True

    assert "unresolved_call:" in PLACEHOLDER_PREFIXES
    with_call_placeholder = SymbolContext(
        focus_symbol={"id": "sym1", "confidence": 0.9},
        outgoing_edges=[{"from_id": "sym1", "to_id": "unresolved_call:foo", "confidence": 0.7}],
    )
    conf_edges = build_confidence(with_call_placeholder)
    assert conf_edges["edge_confidence_min"] == 0.7
    assert conf_edges["edge_confidence_max"] == 0.7
    assert conf_edges["contains_placeholder_targets"] is True
