"""Token-lean MCP adapters omitting null noise and double-encoded JSON."""

import json
from typing import Any

from repo_context.constants import (
    EDGE_KIND_CALLS,
    EDGE_KIND_IMPORTS,
    EDGE_KIND_INHERITS,
    EDGE_KIND_REFERENCES,
)
from repo_context.context.helpers import format_line_span


def _omit_none(data: dict[str, Any]) -> dict[str, Any]:
    """Return a shallow copy excluding keys whose value is ``None``."""
    return {k: v for k, v in data.items() if v is not None}


def _parse_json_maybe(raw: Any) -> Any:
    """Decode a JSON string once if needed; pass dicts/lists through unchanged."""
    if raw is None or isinstance(raw, (dict, list)):
        return raw
    if isinstance(raw, str):
        stripped = raw.strip()
        if not stripped:
            return None
        try:
            return json.loads(stripped)
        except ValueError:
            return raw
    return raw


def _extract_start_line_1based(edge: dict[str, Any]) -> int | None:
    """Extract 1-based start line from edge dict or ``evidence_range_json``."""
    if isinstance(edge.get("line"), int):
        return edge["line"]
    rng = _parse_json_maybe(edge.get("evidence_range_json"))
    if isinstance(rng, dict):
        start = rng.get("start")
        if isinstance(start, dict) and isinstance(start.get("line"), int):
            return start["line"] + 1
    return None


def adapt_node(node: dict[str, Any], *, file_info: dict[str, Any] | None = None) -> dict[str, Any]:
    """Adapt a stored node dict into a compact, token-lean symbol summary."""
    payload = _parse_json_maybe(node.get("payload_json"))
    if not isinstance(payload, dict):
        payload = {}

    file_path = (
        (file_info or {}).get("file_path")
        or node.get("file_path")
        or payload.get("file_path")
    )
    module_path = (
        (file_info or {}).get("module_path")
        or node.get("module_path")
        or payload.get("module_path")
    )

    return _omit_none(
        {
            "id": node["id"],
            "kind": node["kind"],
            "name": node["name"],
            "qualified_name": node["qualified_name"],
            "file_path": file_path,
            "module_path": module_path,
            "lines": node.get("lines") or format_line_span(node),
            "signature": node.get("signature"),
            "doc_summary": node.get("doc_summary"),
            "scope": node.get("scope"),
            "visibility_hint": node.get("visibility_hint"),
        }
    )


def _adapt_parent_stub(node: dict[str, Any] | None) -> dict[str, Any] | None:
    """Adapt a structural or lexical parent node to minimal reference fields."""
    if node is None:
        return None
    return _omit_none(
        {
            "id": node["id"],
            "kind": node["kind"],
            "qualified_name": node.get("qualified_name") or node.get("name"),
            "lines": node.get("lines") or format_line_span(node),
        }
    )


def _adapt_child_stub(
    node: dict[str, Any],
    *,
    include_doc: bool = True,
) -> dict[str, Any]:
    """Adapt a structural/lexical child node into a compact skeleton entry."""
    return _omit_none(
        {
            "id": node["id"],
            "kind": node["kind"],
            "name": node["name"],
            "lines": node.get("lines") or format_line_span(node),
            "signature": node.get("signature"),
            "doc_summary": node.get("doc_summary") if include_doc else None,
        }
    )


def adapt_edge(
    edge: dict[str, Any],
    *,
    preview: str | None = None,
    from_name: str | None = None,
    to_name: str | None = None,
    file_path: str | None = None,
    direction: str | None = None,
) -> dict[str, Any]:
    """Adapt an edge dict to a compact reference entry without raw JSON blobs."""
    return _omit_none(
        {
            "kind": edge.get("kind"),
            "direction": direction,
            "from_id": edge.get("from_id"),
            "from_name": from_name,
            "to_id": edge.get("to_id"),
            "to_name": to_name,
            "file_path": file_path,
            "line": _extract_start_line_1based(edge),
            "confidence": edge.get("confidence"),
            "source": edge.get("source"),
            "preview": preview,
        }
    )


_COMPACT_OUTLINE_NODE_THRESHOLD = 20
_MAX_OUTLINE_CHILDREN = 60


def _count_outline_nodes(nodes: list[dict[str, Any]]) -> int:
    """Count total symbol nodes across an outline tree."""
    total = 0
    for node in nodes:
        total += 1
        children = node.get("children") or []
        if children:
            total += _count_outline_nodes(children)
    return total


def _adapt_outline_node(
    node: dict[str, Any],
    *,
    depth: int = 1,
    compact_nested: bool = False,
) -> dict[str, Any]:
    """Compact an outline tree node, trimming nested metadata on large outlines."""
    raw_children = node.get("children") or []
    truncated_children = 0
    if len(raw_children) > _MAX_OUTLINE_CHILDREN:
        truncated_children = len(raw_children) - _MAX_OUTLINE_CHILDREN
        raw_children = raw_children[:_MAX_OUTLINE_CHILDREN]

    children = [
        _adapt_outline_node(child, depth=depth + 1, compact_nested=compact_nested)
        for child in raw_children
    ]

    signature = node.get("signature")
    has_signature = bool(isinstance(signature, str) and signature.strip())
    if compact_nested and depth >= 2:
        entry: dict[str, Any] = {
            "kind": node["kind"],
            "name": None if has_signature else node.get("name"),
            "lines": node.get("lines"),
            "signature": signature if has_signature else None,
        }
    else:
        entry = {
            "id": node["id"],
            "kind": node["kind"],
            "name": node["name"],
            "lines": node.get("lines"),
            "signature": signature,
            "doc": node.get("doc"),
        }

    if children:
        entry["children"] = children
    if truncated_children > 0:
        entry["truncated_children"] = truncated_children
    return _omit_none(entry)


def adapt_outline(outline: dict[str, Any]) -> dict[str, Any]:
    """Adapt a file outline into a token-lean MCP payload."""
    raw_symbols = outline.get("symbols") or []
    total_nodes = _count_outline_nodes(raw_symbols)
    compact_nested = total_nodes > _COMPACT_OUTLINE_NODE_THRESHOLD

    return _omit_none(
        {
            "file_path": outline["file_path"],
            "module_path": outline["module_path"],
            "line_count": outline.get("line_count", 0),
            "is_fresh": outline.get("is_fresh", True),
            "stale_reason": outline.get("stale_reason"),
            "doc": outline.get("doc"),
            "imports": outline.get("imports") or [],
            "symbols": [
                _adapt_outline_node(item, depth=1, compact_nested=compact_nested)
                for item in raw_symbols
            ],
        }
    )


def _adapt_context_edge_item(edge: dict[str, Any], *, is_incoming: bool) -> dict[str, Any]:
    """Adapt one context edge into a compact relationship item."""
    peer_id = edge.get("from_id") if is_incoming else edge.get("to_id")
    return _omit_none(
        {
            "id": peer_id,
            "name": edge.get("peer_name"),
            "file_path": edge.get("file_path"),
            "line": edge.get("line"),
            "confidence": edge.get("confidence"),
        }
    )


def _adapt_focus_symbol(focus_symbol: dict[str, Any]) -> dict[str, Any]:
    """Compact the focus symbol dict by replacing raw start/end/parent keys with ``lines``."""
    focus = dict(focus_symbol)
    lines = focus.get("lines") or format_line_span(focus)
    focus.pop("start_line", None)
    focus.pop("end_line", None)
    focus.pop("parent_id", None)
    focus.pop("lexical_parent_id", None)
    if lines is not None:
        focus["lines"] = lines
    return _omit_none(focus)


def _group_context_edges(
    edges: list[dict[str, Any]] | None,
    *,
    is_incoming: bool,
) -> dict[str, list[dict[str, Any]]]:
    """Group incoming or outgoing context edges by edge kind."""
    by_kind: dict[str, list[dict[str, Any]]] = {}
    for edge in edges or []:
        kind = edge.get("kind") or "references"
        by_kind.setdefault(kind, []).append(
            _adapt_context_edge_item(edge, is_incoming=is_incoming)
        )
    return by_kind


def _build_hierarchy_fields(ctx: Any) -> dict[str, Any]:
    """Build deduplicated structural and lexical parent/child entries for ``ctx``."""
    struct_children = ctx.structural_children or []
    lex_children = ctx.lexical_children or []

    struct_parent_id = (ctx.structural_parent or {}).get("id")
    lex_parent_id = (ctx.lexical_parent or {}).get("id")
    include_lexical_parent = (
        ctx.lexical_parent is not None
        and (struct_parent_id is None or lex_parent_id != struct_parent_id)
    )

    struct_child_ids = [c.get("id") for c in struct_children]
    lex_child_ids = [c.get("id") for c in lex_children]
    include_lexical_children = bool(lex_children) and lex_child_ids != struct_child_ids

    return {
        "structural_parent": _adapt_parent_stub(ctx.structural_parent),
        "structural_children": (
            [
                _adapt_child_stub(c, include_doc=len(struct_children) <= 10)
                for c in struct_children
            ]
            if struct_children
            else None
        ),
        "lexical_parent": (
            _adapt_parent_stub(ctx.lexical_parent) if include_lexical_parent else None
        ),
        "lexical_children": (
            [
                _adapt_child_stub(c, include_doc=len(lex_children) <= 10)
                for c in lex_children
            ]
            if include_lexical_children
            else None
        ),
    }


def _populate_relationship_buckets(
    payload: dict[str, Any],
    in_by_kind: dict[str, list[dict[str, Any]]],
    out_by_kind: dict[str, list[dict[str, Any]]],
    rel_counts: dict[str, Any],
) -> None:
    """Attach non-empty caller/callee/import/inheritance/reference buckets to ``payload``."""
    bucket_specs = (
        ("callers", in_by_kind.get(EDGE_KIND_CALLS, []), rel_counts.get("callers")),
        ("callees", out_by_kind.get(EDGE_KIND_CALLS, []), rel_counts.get("callees")),
        ("imported_by", in_by_kind.get(EDGE_KIND_IMPORTS, []), rel_counts.get("imported_by")),
        ("imports", out_by_kind.get(EDGE_KIND_IMPORTS, []), rel_counts.get("imports")),
        ("subclasses", in_by_kind.get(EDGE_KIND_INHERITS, []), rel_counts.get("subclasses")),
        ("bases", out_by_kind.get(EDGE_KIND_INHERITS, []), rel_counts.get("bases")),
        (
            "references_in",
            in_by_kind.get(EDGE_KIND_REFERENCES, []),
            rel_counts.get("references_in"),
        ),
        (
            "references_out",
            out_by_kind.get(EDGE_KIND_REFERENCES, []),
            rel_counts.get("references_out"),
        ),
    )
    for key, items, total in bucket_specs:
        count = total if total is not None else len(items)
        if count > 0 or items:
            payload[key] = {"total": count, "items": items}


def _adapt_structural_summary(structural_summary: dict[str, Any]) -> dict[str, Any]:
    """Strip zero/false structural summary flags into a compact dict."""
    return _omit_none(
        {
            "scope": structural_summary.get("scope"),
            "structural_child_count": structural_summary.get("structural_child_count"),
            "structural_children_truncated": (
                structural_summary.get("structural_children_truncated") or None
            ),
            "lexical_child_count": structural_summary.get("lexical_child_count") or None,
            "incoming_edge_count": structural_summary.get("incoming_edge_count"),
            "outgoing_edge_count": structural_summary.get("outgoing_edge_count"),
            "same_file_sibling_count": structural_summary.get("same_file_sibling_count"),
            "is_local_declaration": structural_summary.get("is_local_declaration") or None,
            "is_nested_declaration": structural_summary.get("is_nested_declaration") or None,
        }
    )


def adapt_context(ctx: Any) -> dict[str, Any]:
    """Adapt a SymbolContext into a compact, grouped, token-lean MCP payload."""
    structural_summary = dict(ctx.structural_summary or {})
    rel_counts = dict(structural_summary.pop("relationship_counts", {}) or {})

    payload: dict[str, Any] = {
        "focus_symbol": _adapt_focus_symbol(ctx.focus_symbol),
        **_build_hierarchy_fields(ctx),
    }
    _populate_relationship_buckets(
        payload,
        _group_context_edges(ctx.incoming_edges, is_incoming=True),
        _group_context_edges(ctx.outgoing_edges, is_incoming=False),
        rel_counts,
    )
    payload["structural_summary"] = _adapt_structural_summary(structural_summary)
    payload["reference_summary"] = _omit_none(dict(ctx.reference_summary or {}))
    payload["freshness"] = _omit_none(dict(ctx.freshness or {}))
    payload["confidence"] = _omit_none(dict(ctx.confidence or {}))
    return _omit_none(payload)


def adapt_risk_result(result: Any) -> dict[str, Any]:
    """Adapt a RiskResult into a compact, deterministic MCP payload."""
    targets = []
    for target in result.targets:
        lines = (
            f"{target.start_line}-{target.end_line}"
            if target.start_line is not None and target.end_line is not None
            else None
        )
        targets.append(
            _omit_none(
                {
                    "symbol_id": target.symbol_id,
                    "qualified_name": target.qualified_name,
                    "kind": target.kind,
                    "scope": target.scope,
                    "file_path": target.file_path,
                    "module_path": target.module_path,
                    "lines": lines,
                    "signature": target.signature,
                    "is_test": target.is_test,
                }
            )
        )

    facts = result.facts
    return {
        "risk_score": result.risk_score,
        "decision": result.decision,
        "issues": list(result.issues),
        "targets": targets,
        "facts": {
            "target_count": facts.target_count,
            "symbol_ids": list(facts.symbol_ids),
            "symbol_kinds": sorted(facts.symbol_kinds),
            "reference_counts": dict(facts.reference_counts),
            "reference_availability": dict(facts.reference_availability),
            "referencing_file_counts": dict(facts.referencing_file_counts),
            "referencing_module_counts": dict(facts.referencing_module_counts),
            "touches_public_surface": facts.touches_public_surface,
            "touches_local_scope_only": facts.touches_local_scope_only,
            "target_spans_multiple_files": facts.target_spans_multiple_files,
            "target_spans_multiple_modules": facts.target_spans_multiple_modules,
            "cross_file_impact": facts.cross_file_impact,
            "cross_module_impact": facts.cross_module_impact,
            "inheritance_involved": facts.inheritance_involved,
            "stale_symbols": list(facts.stale_symbols),
            "low_confidence_symbols": list(facts.low_confidence_symbols),
            "low_confidence_edges": list(facts.low_confidence_edges),
            "extra": dict(facts.extra),
        },
    }
